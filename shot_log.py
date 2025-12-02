import os
import sys
import time
import json
import threading
import queue
import shutil
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path

import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext

from watchdog.observers.polling import PollingObserver
from watchdog.events import FileSystemEventHandler


# ============================================================
#  DEFAULT CONFIGURATION
# ============================================================

DEFAULT_CONFIG = {
    "raw_root_suffix": "ELI50069_RAW_DATA",
    "clean_root_suffix": "ELI50069_CLEAN_DATA",

    # Cameras / main folders (must match actual folder names)
    "main_folders": [
        "Lanex1", "Lanex2", "Lanex3", "Lanex4", "Lanex5",
        "LanexGamma", "Lyso", "Csi", "DarkShadow",
        "SideView", "TopView", "FROG"
    ],

    # Cameras expected for every shot (can be reduced via GUI)
    "expected_cameras": [
        "Lanex1", "Lanex2", "Lanex3", "Lanex4", "Lanex5",
        "LanexGamma", "Lyso", "Csi", "DarkShadow",
        "SideView", "TopView", "FROG"
    ],

    # Full time window around trigger mtime (in seconds).
    # An image belongs to shot if |mtime - ref_mtime| <= full_window_s / 2.
    "full_window_s": 10.0,

    # Timeout (in seconds) after trigger (wall-clock time) to stop waiting.
    # If all expected cameras arrive earlier, shot closes immediately (green).
    "timeout_s": 20.0,

    # Trigger system: list of cameras that can trigger a shot
    "trigger_cameras": [
        "Lanex5"   # default: only Lanex5, you can change from GUI
    ],

    # Global trigger keyword (can be any string, numbers, special chars, etc.)
    "global_trigger_keyword": "shot",

    # Keywords marking test images (ignored)
    # !!! make sure "dark" is NOT here if you want DarkShadow to be used !!!
    "test_keywords": ["test", "align"],

    # State & logs
    "state_file": "eli50069_state.json",
    "log_dir": "rename_log",

    # Worker loop interval (for checking shot timeouts)
    "check_interval_s": 0.5,
}


# ============================================================
#  UTILITIES
# ============================================================

def ensure_dir(path: Path):
    path.mkdir(parents=True, exist_ok=True)


def format_dt_for_name(dt: datetime):
    date_str = dt.strftime("%Y%m%d")
    time_str = dt.strftime("%H%M%S")
    return date_str, time_str


def extract_shot_index_from_name(filename: str):
    """
    Extracts shot index from a CLEAN filename like:
        Cam_YYYYMMDD_HHMMSS_shotNNN.tif
    Returns int or None.
    """
    m = re.search(r"_shot(\d+)\.", filename)
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


# ============================================================
#  FILESYSTEM EVENT HANDLER (watchdog)
# ============================================================

class RawFileEventHandler(FileSystemEventHandler):
    """
    Watchdog handler: notifies the ShotManager when new files appear.
    """

    def __init__(self, manager):
        super().__init__()
        self.manager = manager

    def on_created(self, event):
        if event.is_directory:
            return
        self.manager.handle_new_raw_file(event.src_path)

    def on_moved(self, event):
        if event.is_directory:
            return
        self.manager.handle_new_raw_file(event.dest_path)


# ============================================================
#  SHOT MANAGER
# ============================================================

class ShotManager:
    """
    Detect shots based on trigger images, group other images
    in a time interval centered on trigger mtime (full_window),
    copy them to CLEAN, and report status to GUI.

    A shot closes when:
      - all expected cameras are present (immediate close, green), OR
      - timeout has elapsed since trigger (wall-clock), possibly with missing cameras (red).
    Multiple shots can acquire in parallel.
    """

    def __init__(self, root_path: str, config: dict, gui_queue: queue.Queue):
        self.root_path = Path(root_path).resolve()
        self.config = config.copy()
        self.gui_queue = gui_queue

        self.raw_root = self.root_path / self.config["raw_root_suffix"]
        self.clean_root = self.root_path / self.config["clean_root_suffix"]

        self.state_file = self.root_path / self.config["state_file"]
        self.log_dir = self.root_path / self.config["log_dir"]
        ensure_dir(self.log_dir)

        self.running = False
        self.paused = False
        self.worker_thread = None
        self.observer = None
        self.lock = threading.Lock()

        # Shots
        self.open_shots = []  # list of shot dicts
        self.last_shot_index_by_date = {}  # { "YYYYMMDD": last_index }
        self.last_completed_shot = None    # {"date_str", "shot_index", "missing_cameras"}

        # Avoid reprocessing
        self.processed_files = {}  # { path_str: mtime_float }

        # All files seen per date: { "YYYYMMDD": [info, info, ...] }
        # info = {"camera", "path", "dt", "date_str", "time_str"}
        self.files_by_date = {}

        # Files already assigned to some shot (by path)
        self.assigned_files = set()

        # System status
        self.system_status = "IDLE"

        # Logging
        self._setup_logging()
        self._load_state()
        self._resync_last_shot_from_clean_today()

    # ---------------------------
    # LOGGING
    # ---------------------------

    def _setup_logging(self):
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_file = self.log_dir / f"eli50069_log_{ts}.txt"

        self.logger = logging.getLogger(f"ShotManager_{id(self)}")
        self.logger.setLevel(logging.INFO)

        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
        self.logger.addHandler(fh)

        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
        self.logger.addHandler(sh)

    def _log(self, level: str, msg: str):
        line = f"[{level}] {msg}"
        self.gui_queue.put(line)
        if level == "INFO":
            self.logger.info(msg)
        elif level == "WARNING":
            self.logger.warning(msg)
        elif level == "ERROR":
            self.logger.error(msg)
        else:
            self.logger.debug(msg)

    # ---------------------------
    # STATE LOAD / SAVE
    # ---------------------------

    def _load_state(self):
        if not self.state_file.exists():
            self._log("INFO", "No previous state file found, starting fresh.")
            return

        try:
            with open(self.state_file, "r", encoding="utf-8") as f:
                state = json.load(f)

            self.last_shot_index_by_date = state.get("last_shot_index_by_date", {})
            self.processed_files = state.get("processed_files", {})
            self.system_status = state.get("system_status", "IDLE")

            self._log("INFO", f"Loaded state from {self.state_file}")
        except Exception as e:
            self._log("ERROR", f"Failed to load state file: {e}")

    def _save_state(self):
        state = {
            "last_shot_index_by_date": self.last_shot_index_by_date,
            "processed_files": self.processed_files,
            "system_status": self.system_status,
        }
        try:
            with open(self.state_file, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2)
        except Exception as e:
            self._log("ERROR", f"Could not save state: {e}")

    # ---------------------------
    # RESYNC FROM CLEAN (on start)
    # ---------------------------

    def _scan_clean_shots_for_date(self, date_str: str):
        """
        Scan CLEAN_DATA for a given date, returns:
        {shot_index: set(cameras_that_have_this_shot)}
        """
        per_shot_cams = {}
        expected = self.config["expected_cameras"]

        for cam in expected:
            cam_dir = self.clean_root / cam / date_str
            if not cam_dir.exists():
                continue
            for f in cam_dir.glob("*.tif*"):
                idx = extract_shot_index_from_name(f.name)
                if idx is None:
                    continue
                per_shot_cams.setdefault(idx, set()).add(cam)

        return per_shot_cams

    def _resync_last_shot_from_clean_today(self):
        """
        On startup: look into CLEAN folders for today's date, find last shot index
        and determine if it's missing cameras.
        """
        today = datetime.now().strftime("%Y%m%d")
        per_shot_cams = self._scan_clean_shots_for_date(today)
        if not per_shot_cams:
            return

        last_idx = max(per_shot_cams.keys())
        cams_present = per_shot_cams[last_idx]
        missing = [c for c in self.config["expected_cameras"] if c not in cams_present]

        self.last_shot_index_by_date[today] = max(self.last_shot_index_by_date.get(today, 0), last_idx)
        self.last_completed_shot = {
            "date_str": today,
            "shot_index": last_idx,
            "missing_cameras": missing,
        }

        if missing:
            self._log("WARNING", f"Resynced last shot from CLEAN: {today} shot {last_idx:03d}, "
                                 f"missing cameras: {missing}")
            self.system_status = "ERROR"
        else:
            self._log("INFO", f"Resynced last shot from CLEAN: {today} shot {last_idx:03d}, all cameras present")

    # ---------------------------
    # PUBLIC CONTROL API
    # ---------------------------

    def start(self):
        with self.lock:
            if self.running:
                return
            self.running = True
            self.paused = False
            self.system_status = "RUNNING"

        # Start worker loop
        self.worker_thread = threading.Thread(target=self._worker_loop, daemon=True)
        self.worker_thread.start()

        # Start watchdog observer
        self._start_observer()

        self._log("INFO", "ShotManager started.")

    def _start_observer(self):
        if not self.raw_root.exists():
            self._log("WARNING", f"RAW root does not exist yet: {self.raw_root}")

        handler = RawFileEventHandler(self)

        # PollingObserver everywhere (robust with cloud / network and no inotify limits)
        self._log("INFO", "Using PollingObserver (cross-platform polling mode).")
        self.observer = PollingObserver()
        self.observer.schedule(handler, str(self.raw_root), recursive=True)

        try:
            self.observer.start()
            self._log("INFO", "Watchdog observer started on RAW root.")
        except OSError as e:
            self._log("ERROR", f"Failed to start file system watcher: {e}")
            with self.lock:
                self.system_status = "ERROR"
                self.running = False
            self.observer = None

    def pause(self):
        with self.lock:
            if not self.running:
                return
            self.paused = True
            self.system_status = "PAUSED"
        self._log("INFO", "ShotManager paused.")

    def resume(self):
        with self.lock:
            if not self.running:
                return
            self.paused = False
            self.system_status = "RUNNING"
        self._log("INFO", "ShotManager resumed.")

    def stop(self):
        with self.lock:
            self.running = False
            self.system_status = "IDLE"

        if self.observer:
            self.observer.stop()
            self.observer.join(timeout=5.0)
            self.observer = None

        if self.worker_thread and self.worker_thread.is_alive():
            self.worker_thread.join(timeout=5.0)

        self._save_state()
        self._log("INFO", "ShotManager stopped.")

    def update_runtime_timing(self, full_window: float, timeout: float):
        """
        Update full time window and timeout, and recompute shot windows
        for shots that are still collecting.
        """
        with self.lock:
            self.config["full_window_s"] = full_window
            self.config["timeout_s"] = timeout

            half_window = full_window / 2.0
            for s in self.open_shots:
                if s["status"] == "collecting":
                    ref = s["ref_time"]
                    s["window_start"] = ref - timedelta(seconds=half_window)
                    s["window_end"] = ref + timedelta(seconds=half_window)

        self._log("INFO", f"Updated timing parameters: full_window={full_window}s, timeout={timeout}s")

    def update_trigger_config(self, trigger_cameras, global_keyword: str):
        with self.lock:
            self.config["trigger_cameras"] = list(trigger_cameras)
            self.config["global_trigger_keyword"] = global_keyword
        self._log("INFO", f"Updated trigger config: trigger_cameras={trigger_cameras}, "
                          f"global_keyword='{global_keyword}'")

    def update_expected_cameras(self, cams):
        with self.lock:
            self.config["expected_cameras"] = list(cams)
        self._log("INFO", f"Updated expected cameras (used diagnostics): {cams}")

    def set_next_shot_number(self, k: int, date_str: str | None = None):
        if k < 1:
            k = 1
        if date_str is None:
            date_str = datetime.now().strftime("%Y%m%d")
        with self.lock:
            self.last_shot_index_by_date[date_str] = k - 1
        self._save_state()
        self._log("INFO", f"Next shot for {date_str} set to {k:03d}")

    def check_next_shot_conflicts(self, proposed_k: int, date_str: str | None = None):
        if date_str is None:
            date_str = datetime.now().strftime("%Y%m%d")
        per_shot_cams = self._scan_clean_shots_for_date(date_str)
        indices = sorted(per_shot_cams.keys())
        same = proposed_k in indices
        higher = [i for i in indices if i > proposed_k]
        return {"same": same, "higher": higher}

    def get_next_shot_number_today(self):
        today = datetime.now().strftime("%Y%m%d")
        return self.last_shot_index_by_date.get(today, 0) + 1

    # ---------------------------
    # STATUS FOR GUI
    # ---------------------------

    def get_status(self):
        with self.lock:
            open_count = len(self.open_shots)
            last_date_idx = None
            if self.last_shot_index_by_date:
                last_date = max(self.last_shot_index_by_date.keys())
                last_idx = self.last_shot_index_by_date[last_date]
                last_date_idx = (last_date, last_idx)

            # Collecting shots sorted by (date_str, shot_index)
            collecting = sorted(
                [s for s in self.open_shots if s["status"] == "collecting"],
                key=lambda s: (s["date_str"], s["shot_index"])
            )

            status = {
                "system_status": self.system_status,
                "open_shots_count": open_count,
                "last_shot_date": last_date_idx[0] if last_date_idx else None,
                "last_shot_index": last_date_idx[1] if last_date_idx else None,
                "next_shot_number": self.get_next_shot_number_today(),

                # Last shot panel
                "last_shot_state": None,           # "acquiring" / "acquired_ok" / "acquired_missing" / None
                "last_shot_waiting_for": [],
                "last_shot_missing": [],
                "last_shot_date_display": None,
                "last_shot_index_display": None,

                # Current shot panel
                "current_shot_state": None,        # "waiting" / "acquiring"
                "current_shot_waiting_for": [],
                "current_shot_date": None,
                "current_shot_index": None,

                # Timing
                "full_window": self.config["full_window_s"],
                "timeout": self.config["timeout_s"],
                "current_keyword": self.config["global_trigger_keyword"],
            }

            expected = self.config["expected_cameras"]

            # CURRENT SHOT: most recent collecting shot
            if collecting:
                cur = collecting[-1]
                present = set(cur["images_by_camera"].keys())
                waiting_for = [c for c in expected if c not in present]
                status["current_shot_state"] = "acquiring"
                status["current_shot_date"] = cur["date_str"]
                status["current_shot_index"] = cur["shot_index"]
                status["current_shot_waiting_for"] = waiting_for
            else:
                status["current_shot_state"] = "waiting"

            # LAST SHOT PANEL:
            # - If we have >=2 collecting shots: show the previous one as "acquiring".
            # - Else, show last completed shot (ok/missing).
            if len(collecting) >= 2:
                prev = collecting[-2]
                present = set(prev["images_by_camera"].keys())
                waiting_for = [c for c in expected if c not in present]
                status["last_shot_state"] = "acquiring"
                status["last_shot_date_display"] = prev["date_str"]
                status["last_shot_index_display"] = prev["shot_index"]
                status["last_shot_waiting_for"] = waiting_for
            elif self.last_completed_shot is not None:
                s = self.last_completed_shot
                missing = s["missing_cameras"]
                status["last_shot_date_display"] = s["date_str"]
                status["last_shot_index_display"] = s["shot_index"]
                if missing:
                    status["last_shot_state"] = "acquired_missing"
                    status["last_shot_missing"] = list(missing)
                else:
                    status["last_shot_state"] = "acquired_ok"
            else:
                status["last_shot_state"] = None

            return status

    # =======================================================
    #  WORKER LOOP
    # =======================================================

    def _worker_loop(self):
        self._log("INFO", "Worker loop started.")
        interval = self.config["check_interval_s"]

        while True:
            with self.lock:
                if not self.running:
                    break
                paused = self.paused

            if not paused:
                try:
                    self._check_shot_timeouts()
                except Exception as e:
                    self._log("ERROR", f"Exception in worker loop: {e}")
                    with self.lock:
                        self.system_status = "ERROR"

            time.sleep(interval)

        self._log("INFO", "Worker loop terminated.")

    # =======================================================
    #  FILE HANDLING (watchdog)
    # =======================================================

    def handle_new_raw_file(self, path_str: str):
        with self.lock:
            if not self.running or self.paused:
                return

        path = Path(path_str)
        if path.suffix.lower() not in [".tif", ".tiff"]:
            return

        try:
            mtime = os.path.getmtime(path_str)  # always use Modified time
        except FileNotFoundError:
            return

        self._process_file(path, mtime)

    def _process_file(self, path: Path, mtime: float):
        path_str = str(path)

        # Deduplicate by path + mtime
        with self.lock:
            old_mtime = self.processed_files.get(path_str)
            if old_mtime is not None and abs(old_mtime - mtime) < 1e-6:
                return
            self.processed_files[path_str] = mtime
        self._save_state()

        try:
            rel = path.relative_to(self.raw_root)
        except ValueError:
            self._log("WARNING", f"File outside RAW root ignored: {path}")
            return

        if len(rel.parts) < 3:
            self._log("WARNING", f"Unexpected RAW path structure: {path}")
            return

        main_folder = rel.parts[0]
        filename = rel.parts[-1]
        filename_lower = filename.lower()

        dt = datetime.fromtimestamp(mtime)
        date_str, time_str = format_dt_for_name(dt)

        # Test images?
        if any(kw in filename_lower for kw in self.config["test_keywords"]):
            self._log("INFO", f"[TEST] Ignoring test image: {path}")
            return

        info = {
            "camera": main_folder,
            "path": path_str,
            "dt": dt,
            "date_str": date_str,
            "time_str": time_str,
        }

        # Record this file in files_by_date
        with self.lock:
            self.files_by_date.setdefault(date_str, []).append(info)

        # Trigger or not?
        if self._is_trigger_file(main_folder, filename_lower):
            self._handle_trigger_file(info)
        else:
            self._handle_non_trigger_file(info)

    def _is_trigger_file(self, camera: str, filename_lower: str) -> bool:
        trigger_cams = set(self.config["trigger_cameras"])
        if camera not in trigger_cams:
            return False

        keyword = self.config["global_trigger_keyword"]
        if not keyword:
            return False

        return keyword.lower() in filename_lower

    # =======================================================
    #  SHOT CREATION & ASSIGNMENT
    # =======================================================

    def _handle_trigger_file(self, info: dict):
        """
        Trigger logic with multiple trigger cameras:

        - First trigger that arrives (any trigger camera) opens a new shot.
        - For each OPEN shot, a trigger image from a camera that has not yet
          contributed AND whose mtime is inside the shot time-window is
          attached to that shot (does NOT create a new shot).
        - A NEW shot is created only if:
            * the trigger does not fit in the time-window of any collecting shot,
              OR
            * for all collecting shots whose window contains this mtime, this
              camera already has an image (second trigger from this camera).
        """

        camera = info["camera"]
        dt = info["dt"]          # reference mtime of this trigger
        date_str = info["date_str"]

        full_window = self.config["full_window_s"]
        half_window = full_window / 2.0

        # Time window around this trigger
        window_start = dt - timedelta(seconds=half_window)
        window_end = dt + timedelta(seconds=half_window)

        with self.lock:
            # 1) Try to re-use an existing shot:
            #    we look for a shot that:
            #      - is collecting
            #      - same date
            #      - dt inside its window
            #      - this camera has NOT yet contributed to this shot
            reuse_shot = None
            for s in self.open_shots:
                if s["date_str"] != date_str:
                    continue
                if s["status"] != "collecting":
                    continue
                if not (s["window_start"] <= dt <= s["window_end"]):
                    continue
                if camera in s["images_by_camera"]:
                    continue  # this camera already has an image in this shot
                reuse_shot = s
                break

            if reuse_shot is not None:
                # Attach this trigger image to the existing shot instead of
                # creating a new shot.
                if info["path"] not in self.assigned_files:
                    reuse_shot["images_by_camera"][camera] = info
                    self.assigned_files.add(info["path"])
                shot_to_check = reuse_shot
                self._log(
                    "INFO",
                    f"Trigger {info['path']} assigned to existing shot "
                    f"{reuse_shot['shot_index']:03d} (camera {camera})"
                )
            else:
                # 2) No suitable shot found -> create a NEW shot
                last_idx = self.last_shot_index_by_date.get(date_str, 0)
                new_idx = last_idx + 1
                self.last_shot_index_by_date[date_str] = new_idx

                images_by_camera = {}
                date_files = self.files_by_date.get(date_str, [])

                # Collect all files in the full time window around this trigger
                for finfo in date_files:
                    fdt = finfo["dt"]
                    if not (window_start <= fdt <= window_end):
                        continue
                    p = finfo["path"]
                    if p in self.assigned_files:
                        continue
                    cam2 = finfo["camera"]
                    if cam2 not in images_by_camera:
                        images_by_camera[cam2] = finfo
                        self.assigned_files.add(p)

                # Make sure this trigger file is included
                if camera not in images_by_camera:
                    if info["path"] not in self.assigned_files:
                        images_by_camera[camera] = info
                        self.assigned_files.add(info["path"])

                new_shot = {
                    "date_str": date_str,
                    "shot_index": new_idx,
                    "ref_time": dt,
                    "window_start": window_start,
                    "window_end": window_end,
                    "start_wall_time": datetime.now(),
                    "images_by_camera": images_by_camera,
                    "status": "collecting",
                    # --- NEW FIELDS FOR LOGGING ---
                    "trigger_camera": camera,
                    "trigger_time": dt,
                }
                self.open_shots.append(new_shot)
                shot_to_check = new_shot

        # If it's a brand new shot, log it nicely
        if reuse_shot is None:
            self._log(
                "INFO",
                f"*** New shot detected: date={date_str}, "
                f"shot={shot_to_check['shot_index']:03d}, "
                f"camera={camera}, ref_time={dt.strftime('%H:%M:%S')} ***"
            )

        # Check if this shot is already complete
        self._maybe_close_if_complete(shot_to_check)
        self._save_state()

    def _handle_non_trigger_file(self, info: dict):
        camera = info["camera"]
        dt = info["dt"]
        date_str = info["date_str"]

        shot_to_check = None

        with self.lock:
            if info["path"] in self.assigned_files:
                return

            candidate = None
            for s in self.open_shots:
                if s["date_str"] != date_str:
                    continue
                if s["status"] != "collecting":
                    continue
                if s["window_start"] <= dt <= s["window_end"]:
                    candidate = s
                    break

            if candidate is None:
                # We'll keep it in files_by_date; it may be used by a later trigger
                self._log("INFO", f"Orphan image (no matching open shot window yet): {info['path']}")
                return

            if camera not in candidate["images_by_camera"]:
                candidate["images_by_camera"][camera] = info
                self.assigned_files.add(info["path"])
                self._log("INFO", f"Image assigned to shot {candidate['shot_index']:03d}, "
                                  f"camera={camera}: {info['path']}")
            else:
                self._log("WARNING", f"Duplicate image for camera={camera} in shot "
                                     f"{candidate['shot_index']:03d}, ignoring: {info['path']}")
            shot_to_check = candidate

        if shot_to_check is not None:
            self._maybe_close_if_complete(shot_to_check)

    def _maybe_close_if_complete(self, shot: dict):
        """
        If all expected cameras are present in this shot, close it immediately
        (copy to CLEAN and set status / last_shot_state), without waiting for timeout.
        """
        with self.lock:
            if shot["status"] != "collecting":
                return
            expected = self.config["expected_cameras"]
            present = set(shot["images_by_camera"].keys())
            missing = [c for c in expected if c not in present]
            if missing:
                return
            # Mark as closing and let _close_shot do the copy & final state
            shot["status"] = "closing"

        self._close_shot(shot)

        # Remove closed shots from list
        with self.lock:
            self.open_shots = [s for s in self.open_shots if s["status"] != "closed"]

    # =======================================================
    #  TIMEOUT CLOSING
    # =======================================================

    def _check_shot_timeouts(self):
        now = datetime.now()
        timeout = self.config["timeout_s"]
        to_close = []

        with self.lock:
            for s in self.open_shots:
                if s["status"] == "collecting":
                    elapsed = (now - s["start_wall_time"]).total_seconds()
                    if elapsed >= timeout:
                        s["status"] = "closing"
                        to_close.append(s)

        for s in to_close:
            self._close_shot(s)

        with self.lock:
            self.open_shots = [s for s in self.open_shots if s["status"] != "closed"]

    def _close_shot(self, shot: dict):
        date_str = shot["date_str"]
        idx = shot["shot_index"]
        images = shot["images_by_camera"]
        expected = self.config["expected_cameras"]

        missing = [cam for cam in expected if cam not in images]

        # Copy present data
        for cam, finfo in images.items():
            if cam in expected:
                self._copy_to_clean(idx, cam, finfo)

        # ---- NEW: compute timing info for logging ----
        # list of (dt, camera)
        time_cam = []
        for cam, finfo in images.items():
            dt = finfo.get("dt")
            if isinstance(dt, datetime):
                time_cam.append((dt, cam))

        if time_cam:
            time_cam.sort()
            min_dt, first_cam = time_cam[0]
            max_dt, last_cam = time_cam[-1]
        else:
            min_dt = max_dt = None
            first_cam = last_cam = None

        trigger_cam = shot.get("trigger_camera", "unknown")
        trigger_time = shot.get("trigger_time", shot.get("ref_time", None))

        # Build nicely formatted strings
        def fmt_dt(dt_obj):
            return dt_obj.strftime("%Y-%m-%d %H:%M:%S") if isinstance(dt_obj, datetime) else "N/A"

        timing_msg = (
            f"Shot {idx:03d} ({date_str}) timing: "
            f"trigger_cam={trigger_cam}, trigger_time={fmt_dt(trigger_time)}, "
            f"min_mtime={fmt_dt(min_dt)}, max_mtime={fmt_dt(max_dt)}, "
            f"first_camera={first_cam if first_cam else 'N/A'}, "
            f"last_camera={last_cam if last_cam else 'N/A'}"
        )

        with self.lock:
            self.last_completed_shot = {
                "date_str": date_str,
                "shot_index": idx,
                "missing_cameras": missing,
            }

            # First log: success / missing
            if missing:
                self.system_status = "ERROR"
                self._log("WARNING", f"Shot {idx:03d} ({date_str}) acquired (timeout or complete), "
                                      f"but missing cameras: {missing}")
            else:
                if self.system_status not in ["PAUSED"]:
                    self.system_status = "RUNNING"
                self._log("INFO", f"Shot {idx:03d} ({date_str}) acquired successfully, all cameras present.")

            # Second log: detailed timing info
            self._log("INFO", timing_msg)

        shot["status"] = "closed"
        self._save_state()

    def _copy_to_clean(self, shot_index: int, cam: str, finfo: dict):
        src = Path(finfo["path"])
        dt = finfo["dt"]
        ext = src.suffix.lower()
        if ext not in [".tif", ".tiff"]:
            ext = ".tif"

        date_str, time_str = format_dt_for_name(dt)
        dest_dir = self.clean_root / cam / date_str
        ensure_dir(dest_dir)

        dest_name = f"{cam}_{date_str}_{time_str}_shot{shot_index:03d}{ext}"
        dest = dest_dir / dest_name

        try:
            shutil.copy2(src, dest)
            self._log("INFO", f"CLEAN copy: {src} -> {dest}")
        except Exception as e:
            self._log("ERROR", f"Failed to copy {src} -> {dest}: {e}")


# ============================================================
#  TKINTER GUI
# ============================================================

class ShotManagerGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("Shot Log")

        self.log_queue = queue.Queue()
        self.manager = None

        # trigger cameras selection
        self.trigger_cameras = list(DEFAULT_CONFIG["trigger_cameras"])
        self.trigger_cam_vars = {}

        # used cameras (expected_cameras)
        self.used_cameras = list(DEFAULT_CONFIG["expected_cameras"])
        self.used_cam_vars = {}

        self._build_gui()

        self.root.after(200, self._poll_log_queue)
        self.root.after(500, self._update_status_labels)

    # ---------------------------
    # GUI LAYOUT
    # ---------------------------

    def _build_gui(self):
        # Project root
        frm_root = ttk.LabelFrame(self.root, text="Project Root")
        frm_root.pack(fill="x", padx=5, pady=5)

        ttk.Label(frm_root, text="Root folder (contains ELI50069_RAW_DATA / CLEAN_DATA):") \
            .grid(row=0, column=0, sticky="w")
        self.var_root = tk.StringVar()
        ttk.Entry(frm_root, textvariable=self.var_root, width=60).grid(row=0, column=1, sticky="we", padx=5)
        ttk.Button(frm_root, text="Browse", command=self._choose_root).grid(row=0, column=2, padx=5)
        frm_root.columnconfigure(1, weight=1)

        # Timing
        frm_timing = ttk.LabelFrame(self.root, text="Time Window / Timeout")
        frm_timing.pack(fill="x", padx=5, pady=5)

        ttk.Label(frm_timing, text="Full time window (s):").grid(row=0, column=0, sticky="w")
        self.var_window = tk.StringVar(value=str(DEFAULT_CONFIG["full_window_s"]))
        ttk.Entry(frm_timing, textvariable=self.var_window, width=10).grid(row=0, column=1, padx=5)

        ttk.Label(frm_timing, text="Timeout (s):").grid(row=1, column=0, sticky="w")
        self.var_timeout = tk.StringVar(value=str(DEFAULT_CONFIG["timeout_s"]))
        ttk.Entry(frm_timing, textvariable=self.var_timeout, width=10).grid(row=1, column=1, padx=5)

        ttk.Button(frm_timing, text="Apply timing", command=self._apply_timing) \
            .grid(row=0, column=2, rowspan=2, padx=10)

        # Trigger config
        frm_trig = ttk.LabelFrame(self.root, text="Trigger & Cameras Configuration")
        frm_trig.pack(fill="x", padx=5, pady=5)

        # Global keyword + apply
        ttk.Label(frm_trig, text="Global trigger keyword:").grid(row=0, column=0, sticky="w")
        self.var_global_kw = tk.StringVar(value=DEFAULT_CONFIG["global_trigger_keyword"])
        self.ent_global_kw = ttk.Entry(frm_trig, textvariable=self.var_global_kw, width=20)
        self.ent_global_kw.grid(row=0, column=1, padx=5)
        ttk.Button(frm_trig, text="Apply keyword", command=self._apply_keyword) \
            .grid(row=0, column=2, padx=5)
        self.ent_global_kw.bind("<Return>", lambda e: self._apply_keyword())

        # Trigger cameras selection
        ttk.Button(frm_trig, text="Select trigger cameras...", command=self._open_trigger_list) \
            .grid(row=1, column=0, pady=5, sticky="w")

        self.lbl_trigger_cams = ttk.Label(frm_trig, text=self._format_trigger_cams_label())
        self.lbl_trigger_cams.grid(row=1, column=1, sticky="w")

        # Used cameras selection
        ttk.Button(frm_trig, text="Select used cameras...", command=self._open_used_list) \
            .grid(row=2, column=0, pady=5, sticky="w")

        self.lbl_used_cams = ttk.Label(frm_trig, text=self._format_used_cams_label())
        self.lbl_used_cams.grid(row=2, column=1, sticky="w")

        # Next shot
        frm_next = ttk.LabelFrame(self.root, text="Next Shot Number")
        frm_next.pack(fill="x", padx=5, pady=5)

        ttk.Label(frm_next, text="Set next shot number:").grid(row=0, column=0, sticky="w")
        self.var_next_shot = tk.StringVar(value="")
        ttk.Entry(frm_next, textvariable=self.var_next_shot, width=10).grid(row=0, column=1, padx=5)
        ttk.Button(frm_next, text="Set", command=self._set_next_shot).grid(row=0, column=2, padx=5)

        # Control buttons
        frm_ctrl = ttk.LabelFrame(self.root, text="Control")
        frm_ctrl.pack(fill="x", padx=5, pady=5)

        self.btn_start = ttk.Button(frm_ctrl, text="Start", command=self._start)
        self.btn_start.grid(row=0, column=0, padx=5)

        self.btn_pause = ttk.Button(frm_ctrl, text="Pause", command=self._pause, state="disabled")
        self.btn_pause.grid(row=0, column=1, padx=5)

        self.btn_resume = ttk.Button(frm_ctrl, text="Resume", command=self._resume, state="disabled")
        self.btn_resume.grid(row=0, column=2, padx=5)

        self.btn_stop = ttk.Button(frm_ctrl, text="Stop", command=self._stop, state="disabled")
        self.btn_stop.grid(row=0, column=3, padx=5)

        # Status
        frm_status = ttk.LabelFrame(self.root, text="Status")
        frm_status.pack(fill="x", padx=5, pady=5)

        ttk.Label(frm_status, text="System:").grid(row=0, column=0, sticky="w")
        self.lbl_system = ttk.Label(frm_status, text="IDLE")
        self.lbl_system.grid(row=0, column=1, sticky="w")

        ttk.Label(frm_status, text="Open shots:").grid(row=1, column=0, sticky="w")
        self.lbl_open = ttk.Label(frm_status, text="0")
        self.lbl_open.grid(row=1, column=1, sticky="w")

        ttk.Label(frm_status, text="Last shot index (by date):").grid(row=2, column=0, sticky="w")
        self.lbl_last = ttk.Label(frm_status, text="-")
        self.lbl_last.grid(row=2, column=1, sticky="w")

        ttk.Label(frm_status, text="Next shot:").grid(row=3, column=0, sticky="w")
        # Next shot value: same style as "Waiting next shot" (bold, blue)
        self.lbl_next = tk.Label(
            frm_status,
            text="-",
            font=("TkDefaultFont", 11, "bold"),
            fg="blue"
        )
        self.lbl_next.grid(row=3, column=1, sticky="w")

        ttk.Label(frm_status, text="Current keyword:").grid(row=4, column=0, sticky="w")
        self.lbl_keyword = ttk.Label(frm_status, text=DEFAULT_CONFIG["global_trigger_keyword"])
        self.lbl_keyword.grid(row=4, column=1, sticky="w")

        ttk.Label(frm_status, text="Timing (s):").grid(row=5, column=0, sticky="w")
        self.lbl_timing = ttk.Label(
            frm_status,
            text=f"window={DEFAULT_CONFIG['full_window_s']} / timeout={DEFAULT_CONFIG['timeout_s']}"
        )
        self.lbl_timing.grid(row=5, column=1, sticky="w")

        ttk.Label(frm_status, text="Last shot status:").grid(row=6, column=0, sticky="w")
        self.lbl_last_status = tk.Label(
            frm_status,
            text="No shot yet",
            font=("TkDefaultFont", 11, "bold")
        )
        self.lbl_last_status.grid(row=6, column=1, sticky="w")

        ttk.Label(frm_status, text="Current shot status:").grid(row=7, column=0, sticky="w")
        self.lbl_current_status = tk.Label(
            frm_status,
            text="Waiting next shot",
            font=("TkDefaultFont", 11, "bold"),
            fg="blue"
        )
        self.lbl_current_status.grid(row=7, column=1, sticky="w")

        # Logs
        frm_logs = ttk.LabelFrame(self.root, text="Logs")
        frm_logs.pack(fill="both", expand=True, padx=5, pady=5)

        self.txt_logs = scrolledtext.ScrolledText(frm_logs, wrap="word", height=20)
        self.txt_logs.pack(fill="both", expand=True)
        self.txt_logs.configure(state="disabled")

    # ---------------------------
    # TRIGGER & USED CAMERAS POPUPS
    # ---------------------------

    def _format_trigger_cams_label(self):
        if not self.trigger_cameras:
            return "None (no triggers)"
        if len(self.trigger_cameras) == len(DEFAULT_CONFIG["main_folders"]):
            return "All cameras"
        return ", ".join(self.trigger_cameras)

    def _format_used_cams_label(self):
        if not self.used_cameras:
            return "None (no cameras)"
        if len(self.used_cameras) == len(DEFAULT_CONFIG["main_folders"]):
            return "All cameras"
        return ", ".join(self.used_cameras)

    def _open_trigger_list(self):
        top = tk.Toplevel(self.root)
        top.title("Select Trigger Cameras")

        self.trigger_cam_vars = {}
        for i, cam in enumerate(DEFAULT_CONFIG["main_folders"]):
            var = tk.BooleanVar(value=(cam in self.trigger_cameras))
            self.trigger_cam_vars[cam] = var
            tk.Checkbutton(top, text=cam, variable=var).grid(row=i, column=0, sticky="w", padx=5, pady=2)

        def on_ok():
            selected = [cam for cam, var in self.trigger_cam_vars.items() if var.get()]
            if not selected:
                messagebox.showwarning("Warning", "No camera selected, at least one trigger camera is recommended.")
            self.trigger_cameras = selected or []
            self.lbl_trigger_cams.configure(text=self._format_trigger_cams_label())
            # If manager already running, apply immediately
            if self.manager:
                self.manager.update_trigger_config(self.trigger_cameras, self.var_global_kw.get())
            top.destroy()

        ttk.Button(top, text="OK", command=on_ok).grid(row=len(DEFAULT_CONFIG["main_folders"]), column=0, pady=5)

    def _open_used_list(self):
        top = tk.Toplevel(self.root)
        top.title("Select Used Cameras (Expected)")

        self.used_cam_vars = {}
        for i, cam in enumerate(DEFAULT_CONFIG["main_folders"]):
            var = tk.BooleanVar(value=(cam in self.used_cameras))
            self.used_cam_vars[cam] = var
            tk.Checkbutton(top, text=cam, variable=var).grid(row=i, column=0, sticky="w", padx=5, pady=2)

        def on_ok():
            selected = [cam for cam, var in self.used_cam_vars.items() if var.get()]
            if not selected:
                messagebox.showwarning("Warning", "No camera selected, no diagnostics will be expected.")
            self.used_cameras = selected or []
            self.lbl_used_cams.configure(text=self._format_used_cams_label())
            # If manager already running, apply immediately
            if self.manager:
                self.manager.update_expected_cameras(self.used_cameras)
            top.destroy()

        ttk.Button(top, text="OK", command=on_ok).grid(row=len(DEFAULT_CONFIG["main_folders"]), column=0, pady=5)

    # ---------------------------
    # BUTTON HANDLERS
    # ---------------------------

    def _choose_root(self):
        d = filedialog.askdirectory(title="Choose project root")
        if d:
            self.var_root.set(d)

    def _ensure_manager(self):
        """
        Ensure self.manager exists (for operations allowed before Start),
        but do NOT start the worker/observer here.
        """
        if self.manager is not None:
            return True
        root_path = self.var_root.get().strip()
        if not root_path:
            messagebox.showerror("Error", "Please choose a project root directory first.")
            return False
        if not os.path.isdir(root_path):
            messagebox.showerror("Error", f"Invalid root directory: {root_path}")
            return False

        config = DEFAULT_CONFIG.copy()
        config["trigger_cameras"] = list(self.trigger_cameras) if self.trigger_cameras else list(
            DEFAULT_CONFIG["trigger_cameras"]
        )
        config["expected_cameras"] = list(self.used_cameras) if self.used_cameras else []
        config["global_trigger_keyword"] = self.var_global_kw.get()
        config["full_window_s"] = float(self.var_window.get() or DEFAULT_CONFIG["full_window_s"])
        config["timeout_s"] = float(self.var_timeout.get() or DEFAULT_CONFIG["timeout_s"])
        self.manager = ShotManager(root_path, config, self.log_queue)
        return True

    def _start(self):
        if not self._ensure_manager():
            return

        # Apply keyword & timing & used cameras to manager
        self._apply_keyword(apply_only_if_manager=True)
        self._apply_timing(apply_to_manager=True)
        self.manager.update_expected_cameras(self.used_cameras)

        self.manager.start()
        self.btn_start.configure(state="disabled")
        self.btn_pause.configure(state="normal")
        self.btn_resume.configure(state="disabled")
        self.btn_stop.configure(state="normal")

    def _pause(self):
        if self.manager:
            self.manager.pause()
            self.btn_pause.configure(state="disabled")
            self.btn_resume.configure(state="normal")

    def _resume(self):
        if self.manager:
            self.manager.resume()
            self.btn_pause.configure(state="normal")
            self.btn_resume.configure(state="disabled")

    def _stop(self):
        if self.manager:
            self.manager.stop()
        self.btn_start.configure(state="normal")
        self.btn_pause.configure(state="disabled")
        self.btn_resume.configure(state="disabled")
        self.btn_stop.configure(state="disabled")

    def _apply_timing(self, apply_to_manager=False):
        try:
            full_window = float(self.var_window.get())
            timeout = float(self.var_timeout.get())
        except ValueError:
            messagebox.showerror("Error", "Full window and timeout must be numeric.")
            return

        if apply_to_manager and self.manager:
            self.manager.update_runtime_timing(full_window, timeout)
        else:
            self._append_log(f"[INFO] Timing will be used at start: window={full_window}s, timeout={timeout}s")

    def _apply_keyword(self, apply_only_if_manager=False):
        kw = self.var_global_kw.get()
        if kw == "":
            messagebox.showerror("Error", "Global keyword cannot be empty.")
            return

        self.lbl_keyword.configure(text=kw)
        trigger_cams = self.trigger_cameras or DEFAULT_CONFIG["trigger_cameras"]

        if self.manager:
            self.manager.update_trigger_config(trigger_cams, kw)
        else:
            if not apply_only_if_manager:
                self._append_log(f"[INFO] Keyword will be used at start: '{kw}'")

    def _set_next_shot(self):
        val = self.var_next_shot.get().strip()
        if not val:
            messagebox.showerror("Error", "Please enter a shot number.")
            return
        try:
            k = int(val)
        except ValueError:
            messagebox.showerror("Error", "Shot number must be an integer.")
            return

        # Ensure manager exists (even if not started)
        if not self._ensure_manager():
            return

        conflicts = self.manager.check_next_shot_conflicts(k)
        if conflicts["same"] or conflicts["higher"]:
            msg_lines = []
            if conflicts["same"]:
                msg_lines.append(f"- There are already files for shot {k}.")
            if conflicts["higher"]:
                msg_lines.append(f"- There are already shots with numbers > {k}: {conflicts['higher']}")
            msg_lines.append("Setting next shot to this value may overwrite or confuse existing data.")
            msg_lines.append("Do you want to continue?")
            if not messagebox.askyesno("Warning", "\n".join(msg_lines)):
                return

        self.manager.set_next_shot_number(k)

    # ---------------------------
    # LOG POLLING
    # ---------------------------

    def _poll_log_queue(self):
        updated = False
        while True:
            try:
                line = self.log_queue.get_nowait()
            except queue.Empty:
                break
            self._append_log(line)
            updated = True

        if updated:
            self.txt_logs.see("end")

        self.root.after(200, self._poll_log_queue)

    def _append_log(self, text: str):
        self.txt_logs.configure(state="normal")
        self.txt_logs.insert("end", text + "\n")
        self.txt_logs.configure(state="disabled")

    # ---------------------------
    # STATUS UPDATE
    # ---------------------------

    def _update_status_labels(self):
        if self.manager:
            st = self.manager.get_status()
            self.lbl_system.configure(text=st["system_status"])
            self.lbl_open.configure(text=str(st["open_shots_count"]))

            # Last shot index by date
            if st["last_shot_date"] and st["last_shot_index"]:
                self.lbl_last.configure(
                    text=f"{st['last_shot_date']} / shot {st['last_shot_index']:03d}"
                )
            else:
                self.lbl_last.configure(text="-")

            # Next shot number (blue, bold)
            self.lbl_next.configure(text=f"{st['next_shot_number']:03d}", fg="blue")

            # Last shot status
            last_state = st["last_shot_state"]
            if last_state is None:
                txt_last = "No shot yet"
                color_last = "black"
            elif last_state == "acquired_ok":
                txt_last = "Acquired – all cameras present"
                color_last = "green"
            elif last_state == "acquired_missing":
                missing = st["last_shot_missing"]
                txt_last = "Acquired – missing: " + (", ".join(missing) if missing else "unknown")
                color_last = "red"
            elif last_state == "acquiring":
                waiting = st["last_shot_waiting_for"]
                txt_last = "Acquiring – waiting for: " + (", ".join(waiting) if waiting else "none")
                color_last = "orange"
            else:
                txt_last = "No shot yet"
                color_last = "black"

            self.lbl_last_status.configure(text=txt_last, fg=color_last)

            # Current shot status
            cur_state = st["current_shot_state"]
            if cur_state == "acquiring":
                waiting = st["current_shot_waiting_for"]
                txt_cur = "Acquiring – waiting for: " + (", ".join(waiting) if waiting else "none")
                color_cur = "orange"
            else:
                txt_cur = "Waiting next shot"
                color_cur = "blue"

            self.lbl_current_status.configure(text=txt_cur, fg=color_cur)

            # Keyword & timing
            self.lbl_keyword.configure(text=st["current_keyword"])
            self.lbl_timing.configure(
                text=f"window={st['full_window']} / timeout={st['timeout']}"
            )
        else:
            self.lbl_system.configure(text="IDLE")
            self.lbl_open.configure(text="0")
            self.lbl_last.configure(text="-")
            self.lbl_next.configure(text="-", fg="blue")
            self.lbl_last_status.configure(text="No shot yet", fg="black")
            self.lbl_current_status.configure(text="Waiting next shot", fg="blue")
            self.lbl_keyword.configure(text=DEFAULT_CONFIG["global_trigger_keyword"])
            self.lbl_timing.configure(
                text=f"window={DEFAULT_CONFIG['full_window_s']} / timeout={DEFAULT_CONFIG['timeout_s']}"
            )

        self.root.after(500, self._update_status_labels)


# ============================================================
#  MAIN
# ============================================================

def main():
    root = tk.Tk()
    app = ShotManagerGUI(root)
    root.geometry("1100x800")
    root.mainloop()


if __name__ == "__main__":
    main()
