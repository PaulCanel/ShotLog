import os
import sys
import time
import json
import threading
import queue
import shutil
import logging
import re
import csv
from datetime import datetime, timedelta
from pathlib import Path

import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext

from watchdog.observers.polling import PollingObserver
from watchdog.events import FileSystemEventHandler

from config import DEFAULT_CONFIG, FolderConfig, FolderFileSpec, ShotLogConfig
from motor_data import MotorStateManager, parse_initial_positions, parse_motor_history
from shot_log_reader import LogShotAnalyzer


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

    def __init__(
        self, root_path: str, config: ShotLogConfig, gui_queue: queue.Queue, manual_date_str: str | None = None
    ):
        self.root_path = Path(root_path).resolve()
        self.config = config.clone()
        self.gui_queue = gui_queue

        self.raw_root = self.root_path / self.config.raw_root_suffix
        self.clean_root = self.root_path / self.config.clean_root_suffix

        self.state_file = self.root_path / self.config.state_file
        self.log_dir = self.root_path / self.config.log_dir
        self.motor_state_manager: MotorStateManager | None = None
        self._motor_sources_mtime: dict[str, float] | None = None
        self._refresh_motor_paths()
        self.manual_params = list(self.config.manual_params)
        self._refresh_manual_params_path()
        ensure_dir(self.log_dir)

        self.running = False
        self.paused = False
        self.worker_thread = None
        self.observer = None
        self.lock = threading.Lock()
        self.manual_date_str: str | None = manual_date_str
        self.last_seen_date_str: str | None = None

        # Shots
        self.open_shots = []  # list of shot dicts
        self.last_shot_index_by_date = {}  # { "YYYYMMDD": last_index }
        self.last_shot_trigger_time_by_date: dict[str, datetime] = {}
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
        self._ensure_expected_cameras(log_prefix="Initial expected cameras")
        self._log("INFO", f"Trigger cameras (from folder configs): {self.config.trigger_folders}")
        self.log_keyword_config()
        self._load_state()
        self._resync_last_shot_from_clean_today()

    def _resolve_path(self, p: str | Path | None, *, default: str | None = None) -> Path | None:
        if not p and default is None:
            return None
        target = Path(p or default)
        if not target.is_absolute():
            target = self.root_path / target
        return target

    def _refresh_motor_paths(self):
        self.motor_initial_path = self._resolve_path(self.config.motor_initial_csv)
        self.motor_history_path = self._resolve_path(self.config.motor_history_csv)
        self.motor_positions_output = self._resolve_path(
            self.config.motor_positions_output, default="motor_positions_by_shot.csv"
        )

    def _refresh_manual_params_path(self):
        self.manual_params_csv_path = self._resolve_path(
            self.config.manual_params_csv_path, default="manual_params_by_shot.csv"
        )

    def _ensure_expected_cameras(self, log_prefix: str | None = None) -> list[str]:
        expected = self.config.expected_folders
        label = log_prefix or "Expected cameras"
        if not expected and self.config.folders:
            for folder in self.config.folders.values():
                folder.expected = True
            expected = self.config.expected_folders
            self._log(
                "WARNING",
                f"{label}: no expected cameras configured; defaulting to all configured folders.",
            )
        if log_prefix:
            self._log("INFO", f"{log_prefix}: {expected}")
        return expected

    def _get_active_date_str(self) -> str:
        """
        Returns the date string YYYYMMDD used as 'current date' for:
        - last shot index management
        - resync from CLEAN
        - next shot number, etc.

        Priority order:
        1) manual date override (if set)
        2) last RAW date observed from actual files
        3) system date (fallback only if nothing was seen yet)
        """
        manual = getattr(self, "manual_date_str", None)
        if manual:
            return manual
        if self.last_seen_date_str:
            return self.last_seen_date_str
        return datetime.now().strftime("%Y%m%d")

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
            trig_map = state.get("last_shot_trigger_time_by_date", {})
            for k, v in trig_map.items():
                try:
                    self.last_shot_trigger_time_by_date[k] = datetime.fromisoformat(v)
                except Exception:
                    continue
            self.processed_files = state.get("processed_files", {})
            self.system_status = state.get("system_status", "IDLE")
            manual_date = state.get("manual_date_str")
            self.last_seen_date_str = state.get("last_seen_date_str")
            if manual_date and self.manual_date_str is None:
                try:
                    datetime.strptime(manual_date, "%Y%m%d")
                    self.manual_date_str = manual_date
                except ValueError:
                    self.manual_date_str = None
                    self._log(
                        "WARNING",
                        f"Ignored invalid manual date in state file: {manual_date}",
                    )

            self._log("INFO", f"Loaded state from {self.state_file}")
        except Exception as e:
            self._log("ERROR", f"Failed to load state file: {e}")

    def _save_state(self):
        state = {
            "last_shot_index_by_date": self.last_shot_index_by_date,
            "last_shot_trigger_time_by_date": {
                k: v.isoformat() for k, v in self.last_shot_trigger_time_by_date.items()
            },
            "processed_files": self.processed_files,
            "system_status": self.system_status,
            "manual_date_str": self.manual_date_str,
            "last_seen_date_str": self.last_seen_date_str,
        }
        try:
            with open(self.state_file, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2)
        except Exception as e:
            self._log("ERROR", f"Could not save state: {e}")

    # ---------------------------
    # MOTOR DATA HANDLING
    # ---------------------------

    def _load_motor_state_manager(self, *, force_reload: bool = False) -> MotorStateManager | None:
        if self.motor_initial_path is None or self.motor_history_path is None:
            return None

        try:
            mtimes = {
                "initial": os.path.getmtime(self.motor_initial_path),
                "history": os.path.getmtime(self.motor_history_path),
            }
        except FileNotFoundError:
            self._log("WARNING", "Motor CSV files not found; skipping motor correlation.")
            return None

        if (
            not force_reload
            and self.motor_state_manager is not None
            and self._motor_sources_mtime == mtimes
        ):
            return self.motor_state_manager

        try:
            self._log("INFO", "Loading motor CSV files...")
            initial_positions, axis_to_motor = parse_initial_positions(
                self.motor_initial_path, logger=self._log
            )
            events = parse_motor_history(
                self.motor_history_path, logger=self._log, axis_to_motor=axis_to_motor
            )
            self.motor_state_manager = MotorStateManager(initial_positions, events)
            self._motor_sources_mtime = mtimes
            self._log(
                "INFO",
                f"Motor data loaded: {len(initial_positions)} initial positions, {len(events)} events.",
            )
        except Exception as exc:
            self.motor_state_manager = None
            self._log("WARNING", f"Failed to load motor data: {exc}")
        return self.motor_state_manager

    def _write_motor_positions_for_shot(self, shot: dict):
        manager = self._load_motor_state_manager()
        if manager is None:
            return

        trigger_time = shot.get("trigger_time") or shot.get("ref_time")
        if not isinstance(trigger_time, datetime):
            self._log(
                "WARNING",
                f"Cannot write motor positions for shot {shot.get('shot_index')}: missing trigger time",
            )
            return

        output_path = self.motor_positions_output
        if output_path is None:
            self._log("WARNING", "Motor positions output path is not configured.")
            return

        ensure_dir(output_path.parent)
        desired_motors = sorted(manager.motor_names)
        header_prefix = ["shot_number", "trigger_time"]
        existing_rows: list[dict[str, str]] = []
        existing_header: list[str] | None = None

        if output_path.exists():
            try:
                with output_path.open("r", newline="", encoding="utf-8") as f:
                    reader = csv.DictReader(f)
                    existing_header = reader.fieldnames
                    existing_rows = list(reader) if existing_header else []
            except Exception as exc:
                self._log("WARNING", f"Could not read existing motor positions file: {exc}")

        if existing_header and len(existing_header) >= 2:
            known_motors = existing_header[2:]
            all_motors = sorted(set(known_motors) | set(desired_motors))
        else:
            all_motors = desired_motors

        header = header_prefix + all_motors
        positions = manager.get_positions_at(trigger_time)
        row = {"shot_number": shot.get("shot_index"), "trigger_time": trigger_time.isoformat(sep=" ")}
        for motor in all_motors:
            val = positions.get(motor)
            row[motor] = "" if val is None else val

        existing_rows.append(row)

        try:
            with output_path.open("w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=header)
                writer.writeheader()
                writer.writerows(existing_rows)
            self._log(
                "INFO",
                f"Motor positions recorded for shot {shot.get('shot_index'):03d} -> {output_path}",
            )
        except Exception as exc:
            self._log("WARNING", f"Failed to write motor positions CSV: {exc}")

    def _load_shots_from_logs(self) -> list[dict]:
        analyzer = LogShotAnalyzer()
        shots_by_key: dict[tuple[str, int], dict] = {}

        if not self.log_dir.exists():
            self._log("WARNING", f"Log directory not found: {self.log_dir}")
            return []

        for log_file in sorted(self.log_dir.glob("*.txt")):
            try:
                shots = analyzer.parse_log_file(log_file)
            except Exception as exc:
                self._log("WARNING", f"Failed to parse log file {log_file}: {exc}")
                continue
            for shot in shots:
                date = shot.get("date")
                num = shot.get("shot_number")
                if date is None or num is None:
                    continue
                key = (date, num)
                existing = shots_by_key.get(key)
                if existing is None or (
                    existing.get("trigger_time") is None and shot.get("trigger_time") is not None
                ):
                    shots_by_key[key] = shot

        return [shots_by_key[k] for k in sorted(shots_by_key.keys())]

    def recompute_all_motor_positions(self):
        self._log("INFO", "Starting full recompute of motor positions for all shots...")
        manager = self._load_motor_state_manager(force_reload=True)
        if manager is None:
            self._log("WARNING", "Motor data unavailable; recompute aborted.")
            return

        shots = self._load_shots_from_logs()
        if not shots:
            self._log("WARNING", "No shots found in logs; nothing to recompute.")
            return

        output_path = self.motor_positions_output
        if output_path is None:
            self._log("WARNING", "Motor positions output path is not configured.")
            return

        ensure_dir(output_path.parent)
        motor_names = sorted(manager.motor_names)
        header = ["shot_number", "trigger_time"] + motor_names
        rows: list[dict[str, str | int | float | None]] = []

        for shot in shots:
            trigger_time = shot.get("trigger_time")
            if not isinstance(trigger_time, datetime):
                self._log(
                    "WARNING",
                    f"Shot {shot.get('shot_number')} missing trigger_time in logs; skipping.",
                )
                continue
            positions = manager.get_positions_at(trigger_time)
            row: dict[str, str | int | float | None] = {
                "shot_number": shot.get("shot_number"),
                "trigger_time": trigger_time.isoformat(sep=" "),
            }
            for motor in motor_names:
                row[motor] = positions.get(motor)
            rows.append(row)

        try:
            with output_path.open("w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=header)
                writer.writeheader()
                writer.writerows(rows)
            self._log(
                "INFO",
                f"Recomputed motor positions for {len(rows)} shots -> {output_path}",
            )
        except Exception as exc:
            self._log("WARNING", f"Failed to write recomputed motor positions: {exc}")

    # ---------------------------
    # RESYNC FROM CLEAN (on start)
    # ---------------------------

    def _scan_clean_shots_for_date(self, date_str: str):
        """
        Scan CLEAN_DATA for a given date, returns:
        {shot_index: set(cameras_that_have_this_shot)}
        """
        per_shot_cams = {}
        expected = self.config.expected_folders
        latest_mtime_by_shot: dict[int, float] = {}

        for cam in expected:
            cam_dir = self.clean_root / cam / date_str
            if not cam_dir.exists():
                continue
            for f in cam_dir.glob("*"):
                idx = extract_shot_index_from_name(f.name)
                if idx is None:
                    continue
                per_shot_cams.setdefault(idx, set()).add(cam)
                try:
                    latest = latest_mtime_by_shot.get(idx, 0.0)
                    mtime = os.path.getmtime(f)
                    latest_mtime_by_shot[idx] = max(latest, mtime)
                except FileNotFoundError:
                    continue

        return per_shot_cams, latest_mtime_by_shot

    def _resync_last_shot_from_clean_today(self):
        """
        On startup: look into CLEAN folders for the current active date
        (today or manual override), find last shot index and determine if
        it's missing cameras.
        """
        date_str = self._get_active_date_str()
        per_shot_cams, latest_mtime_by_shot = self._scan_clean_shots_for_date(date_str)
        if not per_shot_cams:
            with self.lock:
                if self.last_completed_shot and self.last_completed_shot.get("date_str") != date_str:
                    self.last_completed_shot = None
            return

        if self.last_completed_shot and self.last_completed_shot.get("date_str") != date_str:
            self.last_completed_shot = None

        self.last_shot_index_by_date.setdefault(date_str, 0)

        last_idx = max(per_shot_cams.keys())
        cams_present = per_shot_cams[last_idx]
        missing = [c for c in self.config.expected_folders if c not in cams_present]

        trigger_time = None
        mtime_val = latest_mtime_by_shot.get(last_idx)
        if mtime_val is not None:
            trigger_time = datetime.fromtimestamp(mtime_val)

        self.last_shot_index_by_date[date_str] = max(
            self.last_shot_index_by_date.get(date_str, 0), last_idx
        )
        if trigger_time:
            self.last_shot_trigger_time_by_date[date_str] = trigger_time

        self.last_completed_shot = {
            "date_str": date_str,
            "shot_index": last_idx,
            "missing_cameras": missing,
            "trigger_time": trigger_time,
        }

        if missing:
            self._log(
                "WARNING",
                f"Resynced last shot from CLEAN: {date_str} shot {last_idx:03d}, "
                f"missing cameras: {missing}",
            )
            self.system_status = "ERROR"
        else:
            self._log(
                "INFO",
                f"Resynced last shot from CLEAN: {date_str} shot {last_idx:03d}, all cameras present",
            )

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
            self.config.full_window_s = full_window
            self.config.timeout_s = timeout

            half_window = full_window / 2.0
            for s in self.open_shots:
                if s["status"] == "collecting":
                    ref = s["ref_time"]
                    s["window_start"] = ref - timedelta(seconds=half_window)
                    s["window_end"] = ref + timedelta(seconds=half_window)

        self._log("INFO", f"Updated timing parameters: full_window={full_window}s, timeout={timeout}s")

    def update_keyword_settings(self, global_keyword: str, apply_global_to_all: bool):
        with self.lock:
            self.config.global_trigger_keyword = global_keyword
            self.config.apply_global_keyword_to_all = apply_global_to_all
        self._log(
            "INFO",
            f"Updated keyword settings: keyword='{global_keyword}', apply_to_all={apply_global_to_all}",
        )
        self.log_keyword_config()

    def update_expected_cameras(self, cameras: list[str]):
        with self.lock:
            selected = [cam for cam in cameras if cam in self.config.folders]
            for name, folder in self.config.folders.items():
                folder.expected = name in selected
            ensured_expected = self._ensure_expected_cameras()
        self._log("INFO", f"Updated expected cameras (used diagnostics): {ensured_expected}")

    def update_config(self, new_config: ShotLogConfig):
        with self.lock:
            self.config = new_config.clone()
            self.raw_root = self.root_path / self.config.raw_root_suffix
            self.clean_root = self.root_path / self.config.clean_root_suffix
            self._refresh_motor_paths()
            self.manual_params = list(self.config.manual_params)
            self._refresh_manual_params_path()
            ensured_expected = self._ensure_expected_cameras()
        self._log(
            "INFO",
            f"Configuration updated for running manager. Expected cameras: {ensured_expected}",
        )
        self._log("INFO", f"Trigger cameras (from folder configs): {self.config.trigger_folders}")
        self.log_keyword_config()

    def set_manual_date(self, date_str: str | None):
        """
        Set or clear a manual date override for 'today'.

        :param date_str: YYYYMMDD or None to disable manual mode.
        """
        with self.lock:
            self.manual_date_str = date_str
        if date_str is None:
            self._log("INFO", "Manual date cleared, using system today().")
        else:
            self._log("INFO", f"Manual date set to {date_str}.")
        self._resync_last_shot_from_clean_today()
        self._save_state()

    def set_next_shot_number(self, k: int, date_str: str | None = None):
        if k < 1:
            k = 1
        if date_str is None:
            date_str = self._get_active_date_str()
        with self.lock:
            self.last_shot_index_by_date[date_str] = k - 1
        self._save_state()
        self._log("INFO", f"Next shot for {date_str} set to {k:03d}")

    def check_next_shot_conflicts(self, proposed_k: int, date_str: str | None = None):
        if date_str is None:
            date_str = self._get_active_date_str()
        per_shot_cams, _ = self._scan_clean_shots_for_date(date_str)
        indices = sorted(per_shot_cams.keys())
        same = proposed_k in indices
        higher = [i for i in indices if i > proposed_k]
        return {"same": same, "higher": higher}

    def get_next_shot_number_today(self):
        date_str = self._get_active_date_str()
        return self.last_shot_index_by_date.get(date_str, 0) + 1

    def _get_last_trigger_time_for_date(self, date_str: str) -> datetime | None:
        trigger_time = self.last_shot_trigger_time_by_date.get(date_str)
        if trigger_time is None and self.last_completed_shot:
            if self.last_completed_shot.get("date_str") == date_str:
                trig = self.last_completed_shot.get("trigger_time")
                if isinstance(trig, datetime):
                    trigger_time = trig
        return trigger_time

    def log_keyword_config(self, cfg: ShotLogConfig | None = None):
        """Log the effective keyword configuration (global + per folder)."""
        config_to_log = cfg or self.config
        for line in config_to_log.keyword_log_lines():
            self._log("INFO", line)

    # ---------------------------
    # STATUS FOR GUI
    # ---------------------------

    def get_status(self):
        with self.lock:
            active_date = self._get_active_date_str()
            open_count = len(self.open_shots)
            last_idx = self.last_shot_index_by_date.get(active_date)
            last_date_idx = (active_date, last_idx) if last_idx is not None else None

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
                "last_completed_shot_index": None,
                "last_completed_shot_date": None,
                "last_completed_trigger_time": None,
                "active_date_str": active_date,
                "manual_date_str": self.manual_date_str,

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
                "full_window": self.config.full_window_s,
                "timeout": self.config.timeout_s,
                "current_keyword": self.config.global_trigger_keyword,
            }

            expected = self.config.expected_folders

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

            if self.last_completed_shot is not None:
                status["last_completed_shot_index"] = self.last_completed_shot.get("shot_index")
                status["last_completed_shot_date"] = self.last_completed_shot.get("date_str")
                trig = self.last_completed_shot.get("trigger_time")
                status["last_completed_trigger_time"] = trig.isoformat(sep=" ") if isinstance(trig, datetime) else None

            return status

    # =======================================================
    #  WORKER LOOP
    # =======================================================

    def _worker_loop(self):
        self._log("INFO", "Worker loop started.")
        interval = self.config.check_interval_s

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

        try:
            # NOTE: On Windows + cloud sync the filesystem creation time is unreliable.
            # For all shot logic we always rely on the filesystem Modified Time (mtime).
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

        try:
            rel = path.relative_to(self.raw_root)
        except ValueError:
            self._log("WARNING", f"File outside RAW root ignored: {path}")
            return

        if len(rel.parts) < 2:
            self._log("WARNING", f"Unexpected RAW path structure: {path}")
            return

        main_folder = rel.parts[0]
        if main_folder not in self.config.folders:
            self._log("INFO", f"Ignoring file from unknown folder '{main_folder}': {path}")
            return

        date_from_path = rel.parts[1] if len(rel.parts) >= 2 else None
        filename = rel.parts[-1]
        filename_lower = filename.lower()

        if any(kw.lower() in filename_lower for kw in self.config.test_keywords):
            self._log("INFO", f"[TEST] Ignoring test image: {path}")
            return

        if not self.config.folder_matches(main_folder, filename_lower):
            return

        dt = datetime.fromtimestamp(mtime)
        raw_date_str = date_from_path if date_from_path and re.match(r"^\d{8}$", date_from_path) else None
        date_str = raw_date_str or format_dt_for_name(dt)[0]
        time_str = format_dt_for_name(dt)[1]

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
            self.last_seen_date_str = date_str
        self._save_state()

        # Trigger or not?
        if self._is_trigger_file(main_folder, filename_lower):
            self._handle_trigger_file(info)
        else:
            self._handle_non_trigger_file(info)

    def _is_trigger_file(self, camera: str, filename_lower: str) -> bool:
        return self.config.is_trigger_file(camera, filename_lower)

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
        file_date_str = info["date_str"]
        date_str = self._get_active_date_str()

        full_window = self.config.full_window_s
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
                last_trigger_dt = self._get_last_trigger_time_for_date(date_str)
                if last_trigger_dt and dt <= last_trigger_dt:
                    self._log(
                        "INFO",
                        f"Ignoring late trigger file {info['path']} (mtime {dt}) older than "
                        f"last acquired shot at {last_trigger_dt}",
                    )
                    return
                last_idx = self.last_shot_index_by_date.get(date_str, 0)
                new_idx = last_idx + 1
                self.last_shot_index_by_date[date_str] = new_idx

                images_by_camera = {}
                date_files = self.files_by_date.get(file_date_str, [])

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
        active_date = self._get_active_date_str()

        shot_to_check = None

        with self.lock:
            if info["path"] in self.assigned_files:
                return

            if date_str != active_date:
                self._log(
                    "INFO",
                    f"Ignoring non-trigger from different active date {date_str} (active={active_date}): {info['path']}",
                )
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
            expected = self.config.expected_folders
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
        timeout = self.config.timeout_s
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
        expected = self._ensure_expected_cameras()

        self._log("INFO", f"Closing shot {idx:03d} using expected cameras: {expected}")

        missing = [cam for cam in expected if cam not in images]

        # Copy present data (all configured folders with available files)
        for cam, finfo in images.items():
            if cam in self.config.folders:
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
            self.last_shot_index_by_date[date_str] = max(
                self.last_shot_index_by_date.get(date_str, 0), idx
            )
            if isinstance(trigger_time, datetime):
                self.last_shot_trigger_time_by_date[date_str] = trigger_time
            elif isinstance(max_dt, datetime):
                self.last_shot_trigger_time_by_date[date_str] = max(
                    self.last_shot_trigger_time_by_date.get(date_str, max_dt), max_dt
                )

            self.last_completed_shot = {
                "date_str": date_str,
                "shot_index": idx,
                "missing_cameras": missing,
                "trigger_time": trigger_time,
            }

            # First log: success / missing
            if missing:
                self.system_status = "ERROR"
                self._log(
                    "WARNING",
                    f"Shot {idx:03d} ({date_str}) acquired (timeout or complete), "
                    f"expected={expected}, missing cameras: {missing}",
                )
            else:
                if self.system_status not in ["PAUSED"]:
                    self.system_status = "RUNNING"
                self._log(
                    "INFO",
                    f"Shot {idx:03d} ({date_str}) acquired successfully, expected={expected}, all cameras present.",
                )

            # Second log: detailed timing info
            self._log("INFO", timing_msg)

        try:
            self._write_motor_positions_for_shot(shot)
        except Exception as exc:
            self._log("WARNING", f"Failed to compute motor positions for shot {idx:03d}: {exc}")

        shot["status"] = "closed"
        self._save_state()

    def _copy_to_clean(self, shot_index: int, cam: str, finfo: dict):
        src = Path(finfo["path"])
        dt = finfo["dt"]
        ext = src.suffix
        if not ext:
            ext = ".dat"
        ext = ext.lower()

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

        self.config = DEFAULT_CONFIG.clone()
        self.var_date_mode = tk.StringVar(value="auto")
        self.var_manual_date = tk.StringVar(value="")
        self.trigger_cam_vars = {}
        self.used_cam_vars = {}
        self.manual_param_vars: dict[str, tk.StringVar] = {}
        self.current_manual_shot: int | None = None
        self.current_manual_shot_date: str | None = None
        self.current_manual_trigger_time: str | None = None
        self.confirmed_manual_values: dict[str, str] = {}
        self.manual_values_pending_for_current_shot = False
        self.manual_confirm_labels: dict[str, ttk.Label] = {}
        self.var_manual_params_csv = tk.StringVar(value=self.config.manual_params_csv_path or "")

        self._build_gui()
        self._update_date_mode_label()

        self.root.after(200, self._poll_log_queue)
        self.root.after(500, self._update_status_labels)

    # ---------------------------
    # GUI LAYOUT
    # ---------------------------

    def _build_gui(self):
        # Scrollable container for main controls
        main_container = ttk.Frame(self.root)
        main_container.pack(fill="both", expand=True)

        self.content_canvas = tk.Canvas(main_container, highlightthickness=0)
        vsb = ttk.Scrollbar(main_container, orient="vertical", command=self.content_canvas.yview)
        self.content_canvas.configure(yscrollcommand=vsb.set)

        vsb.pack(side="right", fill="y")
        self.content_canvas.pack(side="left", fill="both", expand=True)

        self.content_frame = ttk.Frame(self.content_canvas)
        self._content_window = self.content_canvas.create_window(
            (0, 0), window=self.content_frame, anchor="nw"
        )

        self.content_frame.bind(
            "<Configure>",
            lambda e: self.content_canvas.configure(scrollregion=self.content_canvas.bbox("all")),
        )
        self.content_canvas.bind(
            "<Configure>",
            lambda e: self.content_canvas.itemconfigure(self._content_window, width=e.width),
        )

        # Project root
        frm_root = ttk.LabelFrame(self.content_frame, text="Project Root")
        frm_root.pack(fill="x", padx=5, pady=5)

        ttk.Label(frm_root, text="Root folder (contains ELI50069_RAW_DATA / CLEAN_DATA):") \
            .grid(row=0, column=0, sticky="w")
        self.var_root = tk.StringVar()
        ttk.Entry(frm_root, textvariable=self.var_root, width=60).grid(row=0, column=1, sticky="we", padx=5)
        ttk.Button(frm_root, text="Browse", command=self._choose_root).grid(row=0, column=2, padx=5)
        frm_root.columnconfigure(1, weight=1)

        frm_date = ttk.LabelFrame(self.content_frame, text="Shot Date")
        frm_date.pack(fill="x", padx=5, pady=5)

        ttk.Label(frm_date, text="Current date mode:").grid(row=0, column=0, sticky="w")
        self.lbl_date_mode = ttk.Label(frm_date, text="auto (today)")
        self.lbl_date_mode.grid(row=0, column=1, sticky="w", padx=5)
        ttk.Button(frm_date, text="Set manual date...", command=self._open_manual_date_dialog).grid(
            row=0, column=2, padx=10
        )

        frm_date.columnconfigure(1, weight=1)

        # Timing
        frm_timing = ttk.LabelFrame(self.content_frame, text="Time Window / Timeout")
        frm_timing.pack(fill="x", padx=5, pady=5)

        ttk.Label(frm_timing, text="Full time window (s):").grid(row=0, column=0, sticky="w")
        self.var_window = tk.StringVar(value=str(self.config.full_window_s))
        ttk.Entry(frm_timing, textvariable=self.var_window, width=10).grid(row=0, column=1, padx=5)

        ttk.Label(frm_timing, text="Timeout (s):").grid(row=1, column=0, sticky="w")
        self.var_timeout = tk.StringVar(value=str(self.config.timeout_s))
        ttk.Entry(frm_timing, textvariable=self.var_timeout, width=10).grid(row=1, column=1, padx=5)

        ttk.Button(frm_timing, text="Apply timing", command=self._apply_timing) \
            .grid(row=0, column=2, rowspan=2, padx=10)

        # Trigger config
        frm_trig = ttk.LabelFrame(self.content_frame, text="Trigger & Cameras Configuration")
        frm_trig.pack(fill="x", padx=5, pady=5)

        # Global keyword + apply
        ttk.Label(frm_trig, text="Global trigger keyword:").grid(row=0, column=0, sticky="w")
        self.var_global_kw = tk.StringVar(value=self.config.global_trigger_keyword)
        self.var_apply_global_kw = tk.BooleanVar(value=self.config.apply_global_keyword_to_all)
        self.ent_global_kw = ttk.Entry(frm_trig, textvariable=self.var_global_kw, width=20)
        self.ent_global_kw.grid(row=0, column=1, padx=5)
        ttk.Button(frm_trig, text="Apply keyword", command=self._apply_keyword) \
            .grid(row=0, column=2, padx=5)
        self.ent_global_kw.bind("<Return>", lambda e: self._apply_keyword())
        ttk.Checkbutton(
            frm_trig,
            text="Apply global keyword to all file definitions",
            variable=self.var_apply_global_kw,
            command=lambda: self._apply_keyword(apply_only_if_manager=False),
        ).grid(row=1, column=0, columnspan=3, sticky="w", padx=5)

        # Trigger cameras selection
        ttk.Button(frm_trig, text="Select trigger cameras...", command=self._open_trigger_list) \
            .grid(row=2, column=0, pady=5, sticky="w")

        self.lbl_trigger_cams = ttk.Label(frm_trig, text=self._format_trigger_cams_label())
        self.lbl_trigger_cams.grid(row=2, column=1, sticky="w")

        # Used cameras selection
        ttk.Button(frm_trig, text="Select used cameras...", command=self._open_used_list) \
            .grid(row=3, column=0, pady=5, sticky="w")

        self.lbl_used_cams = ttk.Label(frm_trig, text=self._format_used_cams_label())
        self.lbl_used_cams.grid(row=3, column=1, sticky="w")

        ttk.Button(frm_trig, text="Folder list...", command=self._open_folder_list).grid(
            row=4, column=0, pady=5, sticky="w"
        )

        frm_cfg_file = ttk.LabelFrame(self.content_frame, text="Configuration File")
        frm_cfg_file.pack(fill="x", padx=5, pady=5)
        ttk.Button(frm_cfg_file, text="Save config...", command=self._save_config).grid(row=0, column=0, padx=5, pady=5)
        ttk.Button(frm_cfg_file, text="Load config...", command=self._load_config).grid(row=0, column=1, padx=5, pady=5)

        frm_manual_cfg = ttk.LabelFrame(self.content_frame, text="Manual parameters setup")
        frm_manual_cfg.pack(fill="x", padx=5, pady=5)
        ttk.Button(frm_manual_cfg, text="Manual parameters...", command=self._open_manual_params_editor).grid(
            row=0, column=0, padx=5, pady=5, sticky="w"
        )
        ttk.Label(frm_manual_cfg, text="Manual params CSV:").grid(row=0, column=1, sticky="e")
        ttk.Entry(frm_manual_cfg, textvariable=self.var_manual_params_csv, width=50).grid(
            row=0, column=2, sticky="we", padx=5
        )
        ttk.Button(frm_manual_cfg, text="Browse...", command=self._choose_manual_params_csv).grid(
            row=0, column=3, padx=5, pady=5
        )
        frm_manual_cfg.columnconfigure(2, weight=1)

        frm_manual_params = ttk.LabelFrame(self.content_frame, text="Manual parameters (per shot)")
        frm_manual_params.pack(fill="x", padx=5, pady=5)
        manual_header = ttk.Frame(frm_manual_params)
        manual_header.grid(row=0, column=0, sticky="we", padx=5, pady=(0, 5))
        self.lbl_manual_target = ttk.Label(manual_header, text="Manual parameters – no shot yet")
        self.lbl_manual_target.grid(row=0, column=0, sticky="w")

        self.manual_confirm_values_frame = ttk.Frame(frm_manual_params)
        self.manual_confirm_values_frame.grid(row=1, column=0, sticky="we", padx=5, pady=(0, 10))

        self.frm_manual_params_fields = ttk.Frame(frm_manual_params)
        self.frm_manual_params_fields.grid(row=2, column=0, sticky="we", padx=5, pady=5)

        self.btn_manual_confirm = ttk.Button(
            frm_manual_params, text="Confirm", command=self._handle_manual_confirm, state="disabled"
        )
        self.btn_manual_confirm.grid(row=3, column=0, sticky="e", padx=5, pady=(0, 5))

        frm_manual_params.columnconfigure(0, weight=1)

        self._rebuild_manual_confirm_display()
        self._rebuild_manual_param_fields()

        frm_motor = ttk.LabelFrame(self.content_frame, text="Motor data")
        frm_motor.pack(fill="x", padx=5, pady=5)

        ttk.Label(frm_motor, text="Initial positions CSV:").grid(row=0, column=0, sticky="w")
        self.var_motor_initial = tk.StringVar(value=self.config.motor_initial_csv)
        ttk.Entry(frm_motor, textvariable=self.var_motor_initial, width=50).grid(row=0, column=1, sticky="we", padx=5)
        ttk.Button(frm_motor, text="Browse...", command=self._choose_motor_initial).grid(row=0, column=2, padx=5)

        ttk.Label(frm_motor, text="Motor history CSV:").grid(row=1, column=0, sticky="w")
        self.var_motor_history = tk.StringVar(value=self.config.motor_history_csv)
        ttk.Entry(frm_motor, textvariable=self.var_motor_history, width=50).grid(row=1, column=1, sticky="we", padx=5)
        ttk.Button(frm_motor, text="Browse...", command=self._choose_motor_history).grid(row=1, column=2, padx=5)

        ttk.Label(frm_motor, text="Positions by shot CSV:").grid(row=2, column=0, sticky="w")
        self.var_motor_output = tk.StringVar(value=self.config.motor_positions_output)
        ttk.Entry(frm_motor, textvariable=self.var_motor_output, width=50).grid(row=2, column=1, sticky="we", padx=5)
        ttk.Button(frm_motor, text="Browse...", command=self._choose_motor_output).grid(row=2, column=2, padx=5)

        ttk.Button(frm_motor, text="Recompute all motor positions", command=self._recompute_motor_positions).grid(
            row=3, column=0, columnspan=3, sticky="w", padx=5, pady=5
        )
        frm_motor.columnconfigure(1, weight=1)

        # Next shot
        frm_next = ttk.LabelFrame(self.content_frame, text="Next Shot Number")
        frm_next.pack(fill="x", padx=5, pady=5)

        ttk.Label(frm_next, text="Set next shot number:").grid(row=0, column=0, sticky="w")
        self.var_next_shot = tk.StringVar(value="")
        ttk.Entry(frm_next, textvariable=self.var_next_shot, width=10).grid(row=0, column=1, padx=5)
        ttk.Button(frm_next, text="Set", command=self._set_next_shot).grid(row=0, column=2, padx=5)

        # Control buttons
        frm_ctrl = ttk.LabelFrame(self.content_frame, text="Control")
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
        frm_status = ttk.LabelFrame(self.content_frame, text="Status")
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
        self.lbl_keyword = ttk.Label(frm_status, text=self.config.global_trigger_keyword)
        self.lbl_keyword.grid(row=4, column=1, sticky="w")

        ttk.Label(frm_status, text="Timing (s):").grid(row=5, column=0, sticky="w")
        self.lbl_timing = ttk.Label(
            frm_status,
            text=f"window={self.config.full_window_s} / timeout={self.config.timeout_s}"
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

    def _update_date_mode_label(self):
        if self.var_date_mode.get() == "manual":
            date_str = self.var_manual_date.get().strip()
            txt = f"manual {date_str}" if date_str else "manual (not set)"
        else:
            txt = "auto (today)"
        self.lbl_date_mode.configure(text=txt)

    # ---------------------------
    # TRIGGER & USED CAMERAS POPUPS
    # ---------------------------

    def _format_trigger_cams_label(self):
        trigger_cameras = self.config.trigger_folders
        if not trigger_cameras:
            return "None (no triggers)"
        if len(trigger_cameras) == len(self.config.folder_names):
            return "All cameras"
        return ", ".join(trigger_cameras)

    def _format_used_cams_label(self):
        used_cameras = self.config.expected_folders
        if not used_cameras:
            return "None (no cameras)"
        if len(used_cameras) == len(self.config.folder_names):
            return "All cameras"
        return ", ".join(used_cameras)

    def _open_trigger_list(self):
        top = tk.Toplevel(self.root)
        top.title("Select Trigger Cameras")

        self.trigger_cam_vars = {}
        folder_names = self.config.folder_names
        for i, cam in enumerate(folder_names):
            var = tk.BooleanVar(value=self.config.folders[cam].trigger)
            self.trigger_cam_vars[cam] = var
            tk.Checkbutton(top, text=cam, variable=var).grid(row=i, column=0, sticky="w", padx=5, pady=2)

        def on_ok():
            has_any = any(var.get() for var in self.trigger_cam_vars.values())
            if not has_any:
                messagebox.showwarning("Warning", "No camera selected, at least one trigger camera is recommended.")
            for cam, var in self.trigger_cam_vars.items():
                self.config.folders[cam].trigger = var.get()
            self.lbl_trigger_cams.configure(text=self._format_trigger_cams_label())
            # If manager already running, apply immediately
            if self.manager:
                self.manager.update_config(self.config.clone())
            top.destroy()

        ttk.Button(top, text="OK", command=on_ok).grid(row=len(folder_names), column=0, pady=5)

    def _open_used_list(self):
        top = tk.Toplevel(self.root)
        top.title("Select Used Cameras (Expected)")

        self.used_cam_vars = {}
        folder_names = self.config.folder_names
        for i, cam in enumerate(folder_names):
            var = tk.BooleanVar(value=self.config.folders[cam].expected)
            self.used_cam_vars[cam] = var
            tk.Checkbutton(top, text=cam, variable=var).grid(row=i, column=0, sticky="w", padx=5, pady=2)

        def on_ok():
            selected = [cam for cam, var in self.used_cam_vars.items() if var.get()]
            if not selected:
                messagebox.showwarning("Warning", "No camera selected, no diagnostics will be expected.")
                return
            for cam, var in self.used_cam_vars.items():
                self.config.folders[cam].expected = var.get()
            self.lbl_used_cams.configure(text=self._format_used_cams_label())
            # If manager already running, apply immediately
            if self.manager:
                self.manager.update_expected_cameras(selected)
            top.destroy()

        ttk.Button(top, text="OK", command=on_ok).grid(row=len(folder_names), column=0, pady=5)

    # ---------------------------
    # BUTTON HANDLERS
    # ---------------------------

    def _choose_root(self):
        d = filedialog.askdirectory(title="Choose project root")
        if d:
            self.var_root.set(d)

    def _open_manual_date_dialog(self):
        top = tk.Toplevel(self.root)
        top.title("Manual date")

        use_manual = tk.BooleanVar(value=self.var_date_mode.get() == "manual")
        date_var = tk.StringVar(value=self.var_manual_date.get())

        ttk.Checkbutton(top, text="Use manual date (YYYYMMDD)", variable=use_manual).grid(
            row=0, column=0, columnspan=2, sticky="w", padx=5, pady=5
        )
        ttk.Label(top, text="Date:").grid(row=1, column=0, sticky="e", padx=5, pady=5)
        ttk.Entry(top, textvariable=date_var, width=20).grid(row=1, column=1, sticky="w", padx=5, pady=5)

        def on_ok():
            if not use_manual.get():
                self.var_date_mode.set("auto")
                self._update_date_mode_label()
                if self.manager:
                    self.manager.set_manual_date(None)
                top.destroy()
                return

            date_str = date_var.get().strip()
            try:
                datetime.strptime(date_str, "%Y%m%d")
            except ValueError:
                messagebox.showerror("Error", "Invalid date format. Use YYYYMMDD (e.g., 20251202).")
                return

            self.var_date_mode.set("manual")
            self.var_manual_date.set(date_str)
            self._update_date_mode_label()
            if self.manager:
                self.manager.set_manual_date(date_str)
            top.destroy()

        ttk.Button(top, text="OK", command=on_ok).grid(row=2, column=0, padx=5, pady=5, sticky="e")
        ttk.Button(top, text="Cancel", command=top.destroy).grid(row=2, column=1, padx=5, pady=5, sticky="w")
        top.grab_set()

    def _choose_motor_initial(self):
        path = filedialog.askopenfilename(
            title="Choose initial motor positions CSV",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
        )
        if path:
            self.var_motor_initial.set(path)

    def _choose_motor_history(self):
        path = filedialog.askopenfilename(
            title="Choose motor history CSV",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
        )
        if path:
            self.var_motor_history.set(path)

    def _choose_motor_output(self):
        path = filedialog.asksaveasfilename(
            title="Choose output CSV for motor positions by shot",
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
        )
        if path:
            self.var_motor_output.set(path)

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

        manual_date_for_start: str | None = None
        if self.var_date_mode.get() == "manual":
            manual_date_for_start = self.var_manual_date.get().strip()
            if not manual_date_for_start:
                messagebox.showerror("Error", "Please enter a manual date or switch to auto mode.")
                self.var_date_mode.set("auto")
                manual_date_for_start = None
            else:
                try:
                    datetime.strptime(manual_date_for_start, "%Y%m%d")
                except ValueError:
                    messagebox.showerror("Error", "Invalid manual date configured. Use YYYYMMDD.")
                    self.var_date_mode.set("auto")
                    manual_date_for_start = None

        runtime_config = self._build_runtime_config()
        self.manager = ShotManager(
            root_path, runtime_config, self.log_queue, manual_date_str=manual_date_for_start
        )
        if manual_date_for_start:
            self.manager.set_manual_date(manual_date_for_start)
        elif self.manager.manual_date_str:
            self.var_date_mode.set("manual")
            self.var_manual_date.set(self.manager.manual_date_str)
        self._update_date_mode_label()
        return True

    def _build_runtime_config(self) -> ShotLogConfig:
        cfg = self.config.clone()
        cfg.global_trigger_keyword = self.var_global_kw.get()
        cfg.apply_global_keyword_to_all = self.var_apply_global_kw.get()
        try:
            cfg.full_window_s = float(self.var_window.get() or cfg.full_window_s)
            cfg.timeout_s = float(self.var_timeout.get() or cfg.timeout_s)
        except ValueError:
            messagebox.showerror("Error", "Full window and timeout must be numeric.")
        cfg.project_root = self.var_root.get().strip() or None
        if self.var_date_mode.get() == "manual":
            cfg.manual_date_override = self.var_manual_date.get().strip() or None
        else:
            cfg.manual_date_override = None
        cfg.motor_initial_csv = self.var_motor_initial.get()
        cfg.motor_history_csv = self.var_motor_history.get()
        cfg.motor_positions_output = self.var_motor_output.get()
        cfg.manual_params = list(self.config.manual_params)
        cfg.manual_params_csv_path = self.var_manual_params_csv.get()
        if not cfg.expected_folders and cfg.folders:
            for folder in cfg.folders.values():
                folder.expected = True
        return cfg

    def _recompute_motor_positions(self):
        if not self._ensure_manager():
            return
        runtime_config = self._build_runtime_config()
        self.config = runtime_config.clone()
        self.manager.update_config(runtime_config)
        self.manager.recompute_all_motor_positions()

    def _start(self):
        if not self._ensure_manager():
            return

        # Apply keyword & timing & used cameras to manager
        self._apply_keyword(apply_only_if_manager=True)
        self._apply_timing(apply_to_manager=True)
        self.manager.update_config(self._build_runtime_config())

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
            self._flush_manual_params_on_stop()
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

        self.config.full_window_s = full_window
        self.config.timeout_s = timeout

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
        self.config.global_trigger_keyword = kw
        self.config.apply_global_keyword_to_all = self.var_apply_global_kw.get()
        if self.manager:
            self.manager.update_keyword_settings(kw, self.config.apply_global_keyword_to_all)
        else:
            if not apply_only_if_manager:
                self._log_keyword_configuration()

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

    def _refresh_folder_labels(self):
        self.lbl_trigger_cams.configure(text=self._format_trigger_cams_label())
        self.lbl_used_cams.configure(text=self._format_used_cams_label())

    def _open_folder_list(self):
        top = tk.Toplevel(self.root)
        top.title("Folder list")

        columns = ("name", "expected", "trigger", "specs")
        tree = ttk.Treeview(top, columns=columns, show="headings", height=10)
        tree.heading("name", text="Folder")
        tree.heading("expected", text="Expected")
        tree.heading("trigger", text="Trigger")
        tree.heading("specs", text="# file specs")
        tree.column("name", width=120, anchor="w")
        tree.column("expected", width=80, anchor="center")
        tree.column("trigger", width=70, anchor="center")
        tree.column("specs", width=90, anchor="center")
        tree.grid(row=0, column=0, columnspan=3, sticky="nsew", padx=5, pady=5)

        top.grid_columnconfigure(0, weight=1)
        top.grid_rowconfigure(0, weight=1)

        def refresh_tree():
            tree.delete(*tree.get_children())
            for folder in self.config.folders.values():
                tree.insert(
                    "",
                    "end",
                    iid=folder.name,
                    values=(
                        folder.name,
                        "yes" if folder.expected else "no",
                        "yes" if folder.trigger else "no",
                        len(folder.file_specs),
                    ),
                )

        def on_add():
            new_cfg = self._open_folder_editor(top)
            if new_cfg:
                self.config.folders[new_cfg.name] = new_cfg
                refresh_tree()
                self._after_config_changed()

        def on_edit():
            selection = tree.selection()
            if not selection:
                messagebox.showwarning("Warning", "Please select a folder to edit.")
                return
            name = selection[0]
            current = self.config.folders.get(name)
            edited = self._open_folder_editor(top, current)
            if edited:
                if edited.name != name:
                    self.config.folders.pop(name, None)
                self.config.folders[edited.name] = edited
                refresh_tree()
                self._after_config_changed()

        def on_remove():
            selection = tree.selection()
            if not selection:
                messagebox.showwarning("Warning", "Please select a folder to remove.")
                return
            name = selection[0]
            self.config.folders.pop(name, None)
            refresh_tree()
            self._after_config_changed()

        ttk.Button(top, text="Add folder...", command=on_add).grid(row=1, column=0, sticky="w", padx=5, pady=5)
        ttk.Button(top, text="Edit folder...", command=on_edit).grid(row=1, column=1, sticky="w", padx=5, pady=5)
        ttk.Button(top, text="Remove folder", command=on_remove).grid(row=1, column=2, sticky="w", padx=5, pady=5)

        refresh_tree()

    def _open_folder_editor(self, parent, folder: FolderConfig | None = None) -> FolderConfig | None:
        top = tk.Toplevel(parent)
        top.title("Add folder" if folder is None else f"Edit folder: {folder.name}")

        name_var = tk.StringVar(value=folder.name if folder else "")
        expected_var = tk.BooleanVar(value=folder.expected if folder else True)
        trigger_var = tk.BooleanVar(value=folder.trigger if folder else False)

        ttk.Label(top, text="Folder name:").grid(row=0, column=0, sticky="w", padx=5, pady=2)
        ttk.Entry(top, textvariable=name_var, width=25).grid(row=0, column=1, padx=5, pady=2)
        ttk.Checkbutton(top, text="Expected", variable=expected_var).grid(row=1, column=0, sticky="w", padx=5, pady=2)
        ttk.Checkbutton(top, text="Trigger", variable=trigger_var).grid(row=1, column=1, sticky="w", padx=5, pady=2)

        ttk.Label(top, text="File definitions (keyword + extension)").grid(row=2, column=0, columnspan=2, sticky="w", padx=5)
        spec_columns = ("keyword", "extension")
        spec_tree = ttk.Treeview(top, columns=spec_columns, show="headings", height=5)
        spec_tree.heading("keyword", text="Keyword")
        spec_tree.heading("extension", text="Extension")
        spec_tree.column("keyword", width=160)
        spec_tree.column("extension", width=120)
        spec_tree.grid(row=3, column=0, columnspan=2, sticky="nsew", padx=5, pady=5)

        specs = [FolderFileSpec(keyword=s.keyword, extension=s.extension) for s in (folder.file_specs if folder else [FolderFileSpec(extension=".tif")])]

        def refresh_specs():
            spec_tree.delete(*spec_tree.get_children())
            for idx, spec in enumerate(specs):
                spec_tree.insert("", "end", iid=str(idx), values=(spec.keyword, spec.extension))

        kw_var = tk.StringVar()
        ext_var = tk.StringVar()

        def add_spec():
            specs.append(FolderFileSpec(keyword=kw_var.get().strip(), extension=ext_var.get().strip()))
            kw_var.set("")
            ext_var.set("")
            refresh_specs()

        def remove_spec():
            selection = spec_tree.selection()
            if not selection:
                messagebox.showwarning("Warning", "Select a file spec to remove.")
                return
            idx = spec_tree.index(selection[0])
            specs.pop(idx)
            refresh_specs()

        ttk.Label(top, text="Keyword:").grid(row=4, column=0, sticky="w", padx=5, pady=2)
        ttk.Entry(top, textvariable=kw_var, width=20).grid(row=4, column=1, sticky="w", padx=5, pady=2)
        ttk.Label(top, text="Extension:").grid(row=5, column=0, sticky="w", padx=5, pady=2)
        ttk.Entry(top, textvariable=ext_var, width=20).grid(row=5, column=1, sticky="w", padx=5, pady=2)

        ttk.Button(top, text="Add file spec", command=add_spec).grid(row=6, column=0, sticky="w", padx=5, pady=2)
        ttk.Button(top, text="Remove file spec", command=remove_spec).grid(row=6, column=1, sticky="w", padx=5, pady=2)

        ttk.Label(
            top,
            text="Global keyword can be enforced for all specs via the main checkbox.",
            foreground="gray",
        ).grid(row=7, column=0, columnspan=2, sticky="w", padx=5, pady=2)

        result: dict[str, FolderConfig | None] = {"folder": None}

        def on_ok():
            name = name_var.get().strip()
            if not name:
                messagebox.showerror("Error", "Folder name cannot be empty.")
                return
            if not specs:
                messagebox.showerror("Error", "Please add at least one file definition.")
                return
            result["folder"] = FolderConfig(
                name=name,
                expected=expected_var.get(),
                trigger=trigger_var.get(),
                file_specs=[FolderFileSpec(keyword=s.keyword, extension=s.extension) for s in specs],
            )
            top.destroy()

        ttk.Button(top, text="OK", command=on_ok).grid(row=8, column=0, sticky="e", padx=5, pady=5)
        ttk.Button(top, text="Cancel", command=top.destroy).grid(row=8, column=1, sticky="w", padx=5, pady=5)

        refresh_specs()
        top.grab_set()
        top.wait_window()
        return result["folder"]

    def _open_manual_params_editor(self):
        top = tk.Toplevel(self.root)
        top.title("Manual parameters")

        params = list(self.config.manual_params)

        lst = tk.Listbox(top, height=8, width=40)
        lst.grid(row=0, column=0, columnspan=3, padx=5, pady=5, sticky="nsew")
        top.grid_columnconfigure(0, weight=1)
        top.grid_rowconfigure(0, weight=1)

        def refresh_list():
            lst.delete(0, tk.END)
            for name in params:
                lst.insert(tk.END, name)

        name_var = tk.StringVar()

        def on_add():
            name = name_var.get().strip()
            if not name:
                messagebox.showerror("Error", "Parameter name cannot be empty.")
                return
            if name in params:
                messagebox.showerror("Error", "Parameter names must be unique.")
                return
            params.append(name)
            name_var.set("")
            refresh_list()

        def on_remove():
            selection = lst.curselection()
            if not selection:
                messagebox.showwarning("Warning", "Please select a parameter to remove.")
                return
            idx = selection[0]
            params.pop(idx)
            refresh_list()

        ttk.Label(top, text="New parameter name:").grid(row=1, column=0, sticky="w", padx=5, pady=2)
        ttk.Entry(top, textvariable=name_var, width=25).grid(row=1, column=1, sticky="w", padx=5, pady=2)
        ttk.Button(top, text="Add", command=on_add).grid(row=1, column=2, sticky="w", padx=5, pady=2)
        ttk.Button(top, text="Remove selected", command=on_remove).grid(row=2, column=0, columnspan=3, sticky="w", padx=5, pady=5)

        def on_ok():
            clean_params = [p.strip() for p in params if p.strip()]
            if len(clean_params) != len(set(clean_params)):
                messagebox.showerror("Error", "Parameter names must be unique and non-empty.")
                return
            self.config.manual_params = clean_params
            self._rebuild_manual_param_fields()
            self._rebuild_manual_confirm_display()
            self._clear_manual_param_entries()
            self._reset_manual_tracking()
            self._after_config_changed()
            top.destroy()

        ttk.Button(top, text="OK", command=on_ok).grid(row=3, column=1, sticky="e", padx=5, pady=5)
        ttk.Button(top, text="Cancel", command=top.destroy).grid(row=3, column=2, sticky="w", padx=5, pady=5)

        refresh_list()
        top.grab_set()
        top.wait_window()

    def _rebuild_manual_param_fields(self):
        for child in self.frm_manual_params_fields.winfo_children():
            child.destroy()
        self.manual_param_vars = {}

        if not self.config.manual_params:
            ttk.Label(self.frm_manual_params_fields, text="No manual parameters defined.").grid(
                row=0, column=0, sticky="w", padx=5, pady=2
            )
            return

        for idx, name in enumerate(self.config.manual_params):
            ttk.Label(self.frm_manual_params_fields, text=f"{name}:").grid(row=idx, column=0, sticky="w", padx=5, pady=2)
            var = tk.StringVar()
            self.manual_param_vars[name] = var
            ttk.Entry(self.frm_manual_params_fields, textvariable=var, width=50).grid(
                row=idx, column=1, sticky="we", padx=5, pady=2
            )
        self.frm_manual_params_fields.columnconfigure(1, weight=1)

    def _rebuild_manual_confirm_display(self):
        for child in self.manual_confirm_values_frame.winfo_children():
            child.destroy()
        self.manual_confirm_labels = {}

        if not self.config.manual_params:
            ttk.Label(self.manual_confirm_values_frame, text="No manual parameters defined.").grid(
                row=0, column=0, sticky="w", padx=5, pady=2
            )
            self.manual_confirm_values_frame.columnconfigure(0, weight=1)
            return

        for idx, name in enumerate(self.config.manual_params):
            ttk.Label(self.manual_confirm_values_frame, text=f"{name} :").grid(
                row=idx, column=0, sticky="w", padx=5, pady=2
            )
            lbl = ttk.Label(self.manual_confirm_values_frame, text="-")
            lbl.grid(row=idx, column=1, sticky="w", padx=5, pady=2)
            self.manual_confirm_labels[name] = lbl

        self.manual_confirm_values_frame.columnconfigure(1, weight=1)
        self._update_manual_confirm_display()

    def _clear_manual_param_entries(self):
        for var in self.manual_param_vars.values():
            var.set("")

    def _collect_manual_param_values(self) -> dict[str, str]:
        values: dict[str, str] = {}
        for name, var in self.manual_param_vars.items():
            values[name] = var.get().strip()
        return values

    def _build_empty_manual_values(self) -> dict[str, str]:
        return {name: "" for name in self.config.manual_params}

    def _update_manual_confirm_state(self):
        has_shot = self.current_manual_shot is not None and bool(self.config.manual_params)
        self.btn_manual_confirm.configure(state="normal" if has_shot else "disabled")

    def _update_manual_confirm_display(self):
        if self.current_manual_shot is None:
            self.lbl_manual_target.configure(text="Manual parameters – no shot yet")
        else:
            self.lbl_manual_target.configure(
                text=f"Manual parameters for shot {self.current_manual_shot}"
            )

        for name, lbl in self.manual_confirm_labels.items():
            value = self.confirmed_manual_values.get(name, "")
            lbl.configure(text=value if value else "-")

    def _start_manual_tracking_for_shot(self, shot_index: int, shot_date: str | None):
        self.current_manual_shot = shot_index
        self.current_manual_shot_date = shot_date
        self.current_manual_trigger_time = None
        self.manual_values_pending_for_current_shot = True
        self.confirmed_manual_values = self._build_empty_manual_values()
        self._update_manual_confirm_state()
        self._update_manual_confirm_display()

    def _reset_manual_tracking(self):
        self.current_manual_shot = None
        self.current_manual_shot_date = None
        self.current_manual_trigger_time = None
        self.manual_values_pending_for_current_shot = False
        self.confirmed_manual_values = self._build_empty_manual_values()
        self._update_manual_confirm_state()
        self._update_manual_confirm_display()

    def _handle_manual_confirm(self):
        if self.current_manual_shot is None:
            messagebox.showinfo(
                "Manual parameters", "No shot to attach manual parameters to yet."
            )
            return
        self.confirmed_manual_values = self._collect_manual_param_values()
        self.manual_values_pending_for_current_shot = True
        self._update_manual_confirm_display()

    def _choose_manual_params_csv(self):
        path = filedialog.asksaveasfilename(
            title="Choose output CSV for manual parameters by shot",
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
        )
        if path:
            self.var_manual_params_csv.set(path)

    def _get_manual_params_output_path(self) -> Path | None:
        path_str = (self.var_manual_params_csv.get() or "").strip()
        if not path_str:
            return None
        path = Path(path_str)
        if not path.is_absolute():
            root_dir = self.var_root.get().strip()
            if root_dir:
                path = Path(root_dir) / path
        return path

    def _write_manual_params_for_shot(
        self, shot_number: int, trigger_time: str | None, values: dict[str, str]
    ):
        output_path = self._get_manual_params_output_path()
        if output_path is None:
            self._append_log("[WARNING] Manual parameters CSV path is not configured; values not saved.")
            return

        ensure_dir(output_path.parent)

        header = ["shot_number", "trigger_time"] + list(self.config.manual_params)
        shot_display = f"{shot_number:03d}" if isinstance(shot_number, int) else str(shot_number)
        row = {
            "shot_number": shot_number,
            "trigger_time": trigger_time or "",
        }
        for name in self.config.manual_params:
            row[name] = values.get(name, "")

        file_exists = output_path.exists()
        existing_header: list[str] | None = None
        if file_exists:
            try:
                with output_path.open("r", newline="", encoding="utf-8") as f:
                    reader = csv.reader(f)
                    existing_header = next(reader)
            except Exception as exc:
                self._append_log(f"[WARNING] Could not read existing manual parameters CSV header: {exc}")

        if existing_header and existing_header != header:
            self._append_log(
                "[WARNING] Manual parameters CSV header does not match current parameter list; values not recorded to avoid mixing formats."
            )
            return

        try:
            with output_path.open("a", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=header)
                if not file_exists:
                    writer.writeheader()
                writer.writerow(row)
            self._append_log(
                f"[INFO] Manual parameters recorded for shot {shot_display} -> {output_path}"
            )
        except Exception as exc:
            self._append_log(f"[WARNING] Failed to write manual parameters CSV: {exc}")

    def _handle_manual_params_status(self, status: dict):
        last_started_idx = status.get("last_shot_index")
        last_started_date = status.get("last_shot_date")
        last_completed_idx = status.get("last_completed_shot_index")
        last_completed_date = status.get("last_completed_shot_date")
        last_completed_trigger = status.get("last_completed_trigger_time")

        current_key = (
            (last_started_date, last_started_idx)
            if last_started_idx is not None and last_started_date is not None
            else None
        )
        tracked_key = (
            (self.current_manual_shot_date, self.current_manual_shot)
            if self.current_manual_shot is not None and self.current_manual_shot_date is not None
            else None
        )

        if (
            self.current_manual_shot is not None
            and last_completed_idx == self.current_manual_shot
            and last_completed_date == self.current_manual_shot_date
        ):
            self.current_manual_trigger_time = last_completed_trigger

        if current_key is None:
            self._reset_manual_tracking()
            return

        if tracked_key is None:
            self._start_manual_tracking_for_shot(last_started_idx, last_started_date)
            return

        if current_key != tracked_key:
            if self.manual_values_pending_for_current_shot and self.current_manual_shot is not None:
                values_to_write = dict(self.confirmed_manual_values)
                self._write_manual_params_for_shot(
                    self.current_manual_shot, self.current_manual_trigger_time, values_to_write
                )
                self.manual_values_pending_for_current_shot = False
            self._start_manual_tracking_for_shot(last_started_idx, last_started_date)
        else:
            self._update_manual_confirm_display()

    def _flush_manual_params_on_stop(self):
        if self.current_manual_shot is not None and self.manual_values_pending_for_current_shot:
            values_to_write = dict(self.confirmed_manual_values)
            self._write_manual_params_for_shot(
                self.current_manual_shot, self.current_manual_trigger_time, values_to_write
            )
            self.manual_values_pending_for_current_shot = False

    def _after_config_changed(self):
        self._refresh_folder_labels()
        if self.manager:
            self.manager.update_config(self._build_runtime_config())
        else:
            self._log_keyword_configuration()

    def _save_config(self):
        cfg = self._build_runtime_config()
        cfg.project_root = self.var_root.get().strip() or None
        cfg.manual_date_override = (
            self.var_manual_date.get().strip() or None
            if self.var_date_mode.get() == "manual"
            else None
        )
        path = filedialog.asksaveasfilename(
            title="Save configuration",
            defaultextension=".json",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(cfg.to_dict(), f, indent=2)
            self._append_log(f"[INFO] Configuration saved to {path}")
        except Exception as exc:
            messagebox.showerror("Error", f"Failed to save configuration: {exc}")

    def _load_config(self):
        path = filedialog.askopenfilename(
            title="Load configuration",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.config = ShotLogConfig.from_dict(data)
            self._refresh_from_config()
            if self.config.project_root:
                self.var_root.set(self.config.project_root)
            if self.config.manual_date_override:
                self.var_date_mode.set("manual")
                self.var_manual_date.set(self.config.manual_date_override)
            else:
                self.var_date_mode.set("auto")
                self.var_manual_date.set("")
            self._update_date_mode_label()
            self._apply_timing(apply_to_manager=True)
            self._apply_keyword()
            self._after_config_changed()
            if self.manager:
                self.manager.set_manual_date(self.config.manual_date_override)
                self._update_status_labels()
                self._update_manual_confirm_state()
                self._update_manual_confirm_display()
            self._append_log(f"[INFO] Configuration loaded from {path}")
        except Exception as exc:
            messagebox.showerror("Error", f"Failed to load configuration: {exc}")

    def _log_keyword_configuration(self):
        cfg = self._build_runtime_config()
        if self.manager:
            self.manager.log_keyword_config(cfg)
        else:
            for line in cfg.keyword_log_lines():
                self._append_log(f"[INFO] {line}")

    def _refresh_from_config(self):
        self.var_window.set(str(self.config.full_window_s))
        self.var_timeout.set(str(self.config.timeout_s))
        self.var_global_kw.set(self.config.global_trigger_keyword)
        self.var_apply_global_kw.set(self.config.apply_global_keyword_to_all)
        self.var_motor_initial.set(self.config.motor_initial_csv)
        self.var_motor_history.set(self.config.motor_history_csv)
        self.var_motor_output.set(self.config.motor_positions_output)
        self.var_manual_params_csv.set(self.config.manual_params_csv_path or "")
        if not self.config.expected_folders and self.config.folders:
            for folder in self.config.folders.values():
                folder.expected = True
        self._rebuild_manual_param_fields()
        self._rebuild_manual_confirm_display()
        self._clear_manual_param_entries()
        self._reset_manual_tracking()
        self._refresh_folder_labels()
        self.lbl_keyword.configure(text=self.config.global_trigger_keyword)
        self.lbl_timing.configure(
            text=f"window={self.config.full_window_s} / timeout={self.config.timeout_s}"
        )

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
            self._handle_manual_params_status(st)
            if st.get("manual_date_str") and self.var_date_mode.get() != "manual":
                self.var_date_mode.set("manual")
                self.var_manual_date.set(st.get("manual_date_str") or "")
                self._update_date_mode_label()
            elif not st.get("manual_date_str") and self.var_date_mode.get() != "auto":
                self.var_date_mode.set("auto")
                self._update_date_mode_label()
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
            self.lbl_keyword.configure(text=self.config.global_trigger_keyword)
            self.lbl_timing.configure(
                text=f"window={self.config.full_window_s} / timeout={self.config.timeout_s}"
            )
            self._reset_manual_tracking()

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
