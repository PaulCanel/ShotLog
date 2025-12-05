from __future__ import annotations

import csv
import json
import os
import queue
import re
import shutil
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers.polling import PollingObserver

from shot_log_reader import LogShotAnalyzer

from .config import ManualParam, ShotLogConfig
from .logging_utils import create_logger
from .motors import MotorStateManager, parse_initial_positions, parse_motor_history
from .utils import ensure_dir, extract_shot_index_from_name, format_dt_for_name


# ============================================================
#  FILESYSTEM EVENT HANDLER (watchdog)
# ============================================================

class RawFileEventHandler(FileSystemEventHandler):
    """
    Watchdog handler: logs every filesystem event and notifies the ShotManager
    when new or updated files appear under RAW root.
    """

    def __init__(self, manager):
        super().__init__()
        self.manager = manager

    # ---- helper interne pour log + dispatch ----
    def _handle_path(self, label: str, path_str: str):
        from pathlib import Path
        p = Path(path_str)
        exists = p.exists()

        # Log clair côté ShotManager (pour la console de shot_log)
        try:
            self.manager._log(
                "INFO",
                f"[WATCHDOG {label}] event on file: {p} | exists={exists}"
            )
        except Exception as e:
            # fallback minimal au cas où le logger n'est pas prêt
            print(f"[WATCHDOG {label} DEBUG ERROR] {e} for path {p}")

        # Passer au pipeline normal seulement si le fichier existe
        if exists:
            self.manager.handle_new_raw_file(path_str)

    # ---- events watchdog ----
    def on_created(self, event):
        if event.is_directory:
            return
        self._handle_path("CREATED", event.src_path)

    def on_moved(self, event):
        if event.is_directory:
            return
        # on s'intéresse à la destination
        self._handle_path("MOVED", event.dest_path)

    def on_modified(self, event):
        """
        Important pour PollingObserver: sur certains systèmes / modes de copie,
        la création d'un fichier peut apparaître comme une séquence de 'modified'.
        On route donc aussi ces events vers le manager.
        """
        if event.is_directory:
            return
        self._handle_path("MODIFIED", event.src_path)


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
        self.config = config.clone()
        self.project_root = Path(self.config.project_root or root_path).resolve()
        self.root_path = self.project_root
        self.gui_queue = gui_queue

        self.manual_date_str: str | None = manual_date_str or self.config.manual_date_override
        self.last_seen_date_str: str | None = None

        self._default_manual_params_path: Path | None = None
        self._default_manual_clean_root: Path | None = None
        self._default_motor_positions_path: Path | None = None
        self._default_motor_clean_root: Path | None = None

        self._apply_path_config()
        self.motor_state_manager: MotorStateManager | None = None
        self._motor_sources_mtime: dict[str, float] | None = None
        self._refresh_motor_paths()
        self.manual_params = []
        for p in self.config.manual_params:
            param = ManualParam.from_raw(p)
            if param:
                self.manual_params.append(param)
        self._refresh_manual_params_path()
        raw_root_exists = self.raw_root.exists()
        ensure_dir(self.raw_root)
        ensure_dir(self.clean_root)
        ensure_dir(self.log_dir)

        self.running = False
        self.paused = False
        self.worker_thread = None
        self.observer = None
        self.lock = threading.Lock()
        
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
        self._log_current_paths()
        if not raw_root_exists:
            self._log("WARNING", f"RAW root does not exist: {self.raw_root}")
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

    def _reset_manual_default_path(self):
        self._default_manual_params_path = None
        self._default_manual_clean_root = None

    def _reset_motor_default_path(self):
        self._default_motor_positions_path = None
        self._default_motor_clean_root = None

    def _apply_path_config(self):
        previous_clean_root = getattr(self, "clean_root", None)
        base_root = self.project_root
        if (
            base_root.name == self.config.raw_root_suffix
            and not (base_root / self.config.raw_root_suffix).exists()
        ):
            base_root = base_root.parent
            self.raw_root = self.project_root
        else:
            self.raw_root = base_root / self.config.raw_root_suffix

        self.project_root = base_root
        self.root_path = base_root
        self.config.project_root = str(base_root)
        self.clean_root = base_root / self.config.clean_root_suffix
        self.log_dir = base_root / self.config.rename_log_folder_suffix
        self.state_file = base_root / self.config.state_file

        if previous_clean_root and previous_clean_root != self.clean_root:
            self._reset_manual_default_path()
            self._reset_motor_default_path()

    def _refresh_motor_paths(self):
        self.motor_initial_path = self._resolve_path(self.config.motor_initial_csv)
        self.motor_history_path = self._resolve_path(self.config.motor_history_csv)
        if self.config.use_default_motor_positions_path:
            if self._default_motor_clean_root != self.clean_root:
                self._reset_motor_default_path()
                self._default_motor_clean_root = self.clean_root
            if self._default_motor_positions_path is None:
                stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                motor_dir = self.clean_root / "motors_parameters"
                ensure_dir(motor_dir)
                self._default_motor_positions_path = (motor_dir / f"shot_motor_positions_{stamp}.csv").resolve()
            self.motor_positions_output = self._default_motor_positions_path
        else:
            self._reset_motor_default_path()
            path = self._resolve_path(
                self.config.motor_positions_output, default="motor_positions_by_shot.csv"
            )
            self.motor_positions_output = path.with_suffix(".csv") if path else None

    def _refresh_manual_params_path(self):
        if self.config.use_default_manual_params_path:
            if self._default_manual_clean_root != self.clean_root:
                self._reset_manual_default_path()
                self._default_manual_clean_root = self.clean_root
            if self._default_manual_params_path is None:
                stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                manual_dir = self.clean_root / "manual_parameters"
                ensure_dir(manual_dir)
                self._default_manual_params_path = (manual_dir / f"shot_manual_params_{stamp}.csv").resolve()
            self.manual_params_csv_path = self._default_manual_params_path
        else:
            self._reset_manual_default_path()
            path = self._resolve_path(
                self.config.manual_params_csv_path, default="manual_params_by_shot.csv"
            )
            self.manual_params_csv_path = path.with_suffix(".csv") if path else None

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

    def _get_motor_history_fallback_date(self) -> date:
        """Return a date used to anchor time-only motor events."""

        date_str = self._get_active_date_str()
        try:
            return datetime.strptime(date_str, "%Y%m%d").date()
        except Exception:
            return datetime.now().date()

    # ---------------------------
    # LOGGING
    # ---------------------------

    def _setup_logging(self):
        self.logger = create_logger(self.log_dir, f"ShotManager_{id(self)}")

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

    def _log_current_paths(self):
        self._log("INFO", f"Project root = {self.project_root}")
        self._log("INFO", f"RAW root    = {self.raw_root}")
        self._log("INFO", f"CLEAN root  = {self.clean_root}")
        self._log("INFO", f"Log folder  = {self.log_dir}")

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
                self.motor_history_path,
                logger=self._log,
                axis_to_motor=axis_to_motor,
                fallback_date=self._get_motor_history_fallback_date(),
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
        if self.config.use_default_motor_positions_path:
            self._refresh_motor_paths()
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

        def _normalize_trigger_time(raw_time):
            if raw_time is None:
                return ""
            if isinstance(raw_time, datetime):
                return raw_time.strftime("%H:%M:%S")
            txt = str(raw_time).strip()
            if not txt:
                return ""
            try:
                return datetime.fromisoformat(txt).strftime("%H:%M:%S")
            except Exception:
                pass
            if " " in txt:
                txt = txt.split(" ")[-1]
            if "." in txt:
                txt = txt.split(".")[0]
            if re.match(r"^\d{2}:\d{2}:\d{2}$", txt):
                return txt
            return txt

        if output_path.exists():
            try:
                with output_path.open("r", newline="", encoding="utf-8") as f:
                    reader = csv.DictReader(f)
                    existing_header = reader.fieldnames
                    existing_rows = list(reader) if existing_header else []
            except Exception as exc:
                self._log("WARNING", f"Could not read existing motor positions file: {exc}")

        for row in existing_rows:
            row["trigger_time"] = _normalize_trigger_time(row.get("trigger_time"))

        if existing_header and len(existing_header) >= 2:
            known_motors = existing_header[2:]
            all_motors = sorted(set(known_motors) | set(desired_motors))
        else:
            all_motors = desired_motors

        header = header_prefix + all_motors
        positions = manager.get_positions_at(trigger_time)
        row = {
            "shot_number": shot.get("shot_index"),
            "trigger_time": _normalize_trigger_time(trigger_time),
        }
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
        if self.config.use_default_motor_positions_path:
            self._refresh_motor_paths()
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

        observer_type = type(self.observer).__name__
        self._log("INFO", f"Observer type: {observer_type}")

        raw_root_resolved = self.raw_root.resolve()
        if not self.raw_root.exists():
            self._log("WARNING", f"Watchdog path does not exist: {raw_root_resolved}")
        else:
            self._log("INFO", f"Watchdog path exists: {raw_root_resolved}")

        self._log("INFO", f"Scheduling watchdog on path: {raw_root_resolved} (recursive=True)")
        self.observer.schedule(handler, str(self.raw_root), recursive=True)

        try:
            self._log("INFO", "Starting watchdog observer.")
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
            previous_raw_root = getattr(self, "raw_root", None)
            previous_log_dir = getattr(self, "log_dir", None)
            self.config = new_config.clone()
            self.manual_date_str = self.config.manual_date_override
            self.project_root = Path(self.config.project_root or self.root_path).resolve()
            self.root_path = self.project_root
            self._apply_path_config()
            self._refresh_motor_paths()
            self.manual_params = []
            for p in self.config.manual_params:
                param = ManualParam.from_raw(p)
                if param:
                    self.manual_params.append(param)
            self._refresh_manual_params_path()
            ensured_expected = self._ensure_expected_cameras()

        raw_root_exists = self.raw_root.exists()
        ensure_dir(self.raw_root)
        ensure_dir(self.clean_root)
        ensure_dir(self.log_dir)

        if previous_log_dir and previous_log_dir != self.log_dir:
            for handler in list(self.logger.handlers):
                self.logger.removeHandler(handler)
                try:
                    handler.close()
                except Exception:
                    pass
            self._setup_logging()

        self._log(
            "INFO",
            f"Configuration updated for running manager. Expected cameras: {ensured_expected}",
        )
        self._log("INFO", f"Trigger cameras (from folder configs): {self.config.trigger_folders}")
        self._log_current_paths()
        if not raw_root_exists:
            self._log("WARNING", f"RAW root does not exist: {self.raw_root}")

        if self.running and previous_raw_root and previous_raw_root != self.raw_root:
            if self.observer:
                self.observer.stop()
                self.observer.join(timeout=5.0)
                self.observer = None
            self._start_observer()
        self.log_keyword_config()

    def set_manual_date(self, date_str: str | None):
        """
        Set or clear a manual date override for 'today'.

        :param date_str: YYYYMMDD or None to disable manual mode.
        """
        with self.lock:
            self.manual_date_str = date_str
            self._refresh_manual_params_path()
            self._refresh_motor_paths()
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

            def fmt(dt_obj):
                return dt_obj.isoformat(sep=" ") if isinstance(dt_obj, datetime) else None

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
                "last_shot_trigger_time": None,
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
                "current_shot_trigger_time": None,

                # Timing
                "full_window": self.config.full_window_s,
                "timeout": self.config.timeout_s,
                "current_keyword": self.config.global_trigger_keyword,
            }

            expected = self.config.expected_folders
            status["last_shot_trigger_time"] = fmt(self._get_last_trigger_time_for_date(active_date))

            # CURRENT SHOT: most recent collecting shot
            if collecting:
                cur = collecting[-1]
                present = set(cur["images_by_camera"].keys())
                waiting_for = [c for c in expected if c not in present]
                status["current_shot_state"] = "acquiring"
                status["current_shot_date"] = cur["date_str"]
                status["current_shot_index"] = cur["shot_index"]
                status["current_shot_trigger_time"] = fmt(cur.get("trigger_time"))
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
                status["last_completed_trigger_time"] = fmt(trig)

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

    def handle_new_raw_file(self, path_str: str | Path):
        with self.lock:
            if not self.running or self.paused:
                return

        path = Path(path_str)

        # \U0001f525 DEBUG : confirme que handle_new_raw_file est bien appelé
        self._log("INFO", f"[MANAGER] Handling new RAW file: {path}")

        try:
            # NOTE: On Windows + cloud sync the filesystem creation time is unreliable.
            # For all shot logic we always rely on the filesystem Modified Time (mtime).
            mtime = os.path.getmtime(path)  # always use Modified time
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

        shot_to_check: dict | None = None
        reuse_shot: dict | None = None
        new_shot_created = False

        with self.lock:
            matching_shots: list[dict] = []
            for s in self.open_shots:
                if s["date_str"] != date_str:
                    continue
                if s["status"] != "collecting":
                    continue
                if not (s["window_start"] <= dt <= s["window_end"]):
                    continue
                matching_shots.append(s)

            for s in matching_shots:
                if camera not in s["images_by_camera"]:
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
                    f"{reuse_shot['shot_index']:03d} (camera {camera})",
                )
            elif matching_shots:
                self._log(
                    "INFO",
                    f"Ignoring duplicate trigger for camera={camera} at {dt}, "
                    f"already present in shot(s) "
                    f"{', '.join(str(s['shot_index']) for s in matching_shots)}: {info['path']}",
                )
                return
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
                new_shot_created = True

        # If it's a brand new shot, log it nicely
        if new_shot_created:
            self._log(
                "INFO",
                f"*** New shot detected: date={date_str}, "
                f"shot={shot_to_check['shot_index']:03d}, "
                f"camera={camera}, ref_time={dt.strftime('%H:%M:%S')} ***",
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

            self._log(
                "INFO",
                (
                    f"[DEBUG] Closing shot {idx:03d} on {date_str}, "
                    f"last_shot_index_by_date[{date_str}] -> {self.last_shot_index_by_date[date_str]:03d}"
                ),
            )

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


