"""GUI simulator that replays cloud synchronisation for ShotLog.

The simulator copies RAW files and motor history events into a test directory
over time, mimicking the behaviour of the cloud uploads without touching the
original data. It is intentionally separate from the detection logic so it can
be used to validate ``shot_log.py`` against recorded experiments.
"""

from __future__ import annotations

import csv
import os
import queue
import random
import shutil
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable, List, Optional

import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext

from motor_data import MotorEvent, parse_motor_history


# ============================================================
#  DATA STRUCTURES
# ============================================================

@dataclass
class RawEvent:
    """Represents a RAW file becoming visible through cloud sync."""

    time: datetime
    rel_path: Path
    visible_time: datetime


@dataclass
class MotorEventVisibility:
    """Wraps a MotorEvent with its simulated visibility timestamp."""

    event: MotorEvent
    visible_time: datetime


@dataclass
class SimulationState:
    """Holds the prepared events and filesystem configuration."""

    raw_events: List[RawEvent]
    motor_events: List[MotorEventVisibility]
    raw_data_root: Path
    test_root: Path
    dest_raw_root: Path
    initial_csv_source: Path
    history_csv_source: Path
    initial_csv_dest: Path
    history_csv_dest: Path
    raw_index: int = 0
    motor_index: int = 0
    sim_time: Optional[datetime] = None
    t_min: Optional[datetime] = None


# ============================================================
#  HELPERS
# ============================================================

def _log_to_queue(gui_queue: queue.Queue[str], level: str, message: str) -> None:
    line = f"[{level}] {message}"
    gui_queue.put(line)


def _parse_datetime(value: str) -> Optional[datetime]:
    value = value.strip()
    if not value:
        return None
    parsers = [
        datetime.fromisoformat,
        lambda v: datetime.strptime(v, "%Y-%m-%d %H:%M:%S"),
        lambda v: datetime.strptime(v, "%d/%m/%Y %H:%M:%S"),
        lambda v: datetime.strptime(v, "%Y/%m/%d %H:%M:%S"),
    ]
    for parser in parsers:
        try:
            return parser(value)
        except Exception:
            continue
    return None


# ============================================================
#  SIMULATION CORE
# ============================================================

class SimulationController:
    """Orchestrates the replay and filesystem mutations."""

    def __init__(self, gui_queue: queue.Queue[str]):
        self.gui_queue = gui_queue
        self.state: Optional[SimulationState] = None
        self.jitter_s = 0.0
        self.cloud_period_s = 30.0
        self.running = False
        self.worker: Optional[threading.Thread] = None
        self.lock = threading.Lock()
        self.last_seed_start: Optional[datetime] = None

    # ---------------------------
    #  PREPARATION
    # ---------------------------

    def _find_raw_data_root(self, root: Path) -> Path:
        if root.name.endswith("_RAW_DATA"):
            return root
        for child in root.iterdir():
            if child.is_dir() and child.name.endswith("_RAW_DATA"):
                return child
        raise FileNotFoundError(
            "Could not find a directory ending with '_RAW_DATA' in the selected RAW source root."
        )

    def _gather_raw_events(self, raw_data_root: Path) -> List[RawEvent]:
        events: List[RawEvent] = []
        for path in sorted(raw_data_root.rglob("*")):
            if not path.is_file():
                continue
            mtime = datetime.fromtimestamp(path.stat().st_mtime)
            visible_time = mtime + timedelta(seconds=random.uniform(0, self.jitter_s))
            events.append(
                RawEvent(time=mtime, rel_path=path.relative_to(raw_data_root), visible_time=visible_time)
            )
        return events

    def _gather_motor_events(self, history_path: Path) -> List[MotorEventVisibility]:
        events: List[MotorEventVisibility] = []
        for evt in parse_motor_history(history_path, logger=self._log_callback):
            visible_time = evt.time + timedelta(seconds=random.uniform(0, self.jitter_s))
            events.append(MotorEventVisibility(event=evt, visible_time=visible_time))
        return events

    def _log_callback(self, level: str, message: str) -> None:
        _log_to_queue(self.gui_queue, level, message)

    def prepare_state(
        self,
        raw_source: Path,
        initial_csv: Path,
        history_csv: Path,
        test_root: Path,
        jitter_s: float,
        cloud_period_s: float,
    ) -> SimulationState:
        if not raw_source.exists():
            raise FileNotFoundError(f"RAW source root not found: {raw_source}")
        if not initial_csv.exists():
            raise FileNotFoundError(f"Initial motor CSV not found: {initial_csv}")
        if not history_csv.exists():
            raise FileNotFoundError(f"Motor history CSV not found: {history_csv}")
        source_resolved = raw_source.resolve()
        test_root_resolved = test_root.resolve()
        if test_root_resolved == source_resolved or test_root_resolved.is_relative_to(source_resolved):
            raise ValueError("Test root must be outside the RAW source directory to avoid modifications.")
        test_root.mkdir(parents=True, exist_ok=True)

        raw_data_root = self._find_raw_data_root(raw_source)
        dest_raw_root = test_root / raw_data_root.name
        initial_dest = test_root / initial_csv.name
        history_dest = test_root / history_csv.name

        self.jitter_s = max(0.0, jitter_s)
        self.cloud_period_s = max(0.1, cloud_period_s)

        raw_events = self._gather_raw_events(raw_data_root)
        motor_events = self._gather_motor_events(history_csv)
        if not raw_events and not motor_events:
            raise ValueError("No RAW files or motor events were found to replay.")

        t_candidates = [evt.time for evt in raw_events] + [evt.event.time for evt in motor_events]
        t_min = min(t_candidates)

        self.state = SimulationState(
            raw_events=sorted(raw_events, key=lambda e: e.visible_time),
            motor_events=sorted(motor_events, key=lambda e: e.visible_time),
            raw_data_root=raw_data_root,
            test_root=test_root,
            dest_raw_root=dest_raw_root,
            initial_csv_source=initial_csv,
            history_csv_source=history_csv,
            initial_csv_dest=initial_dest,
            history_csv_dest=history_dest,
            t_min=t_min,
        )
        self.last_seed_start = None
        return self.state

    # ---------------------------
    #  OUTPUT RESET
    # ---------------------------

    def _reset_outputs(self) -> None:
        assert self.state is not None
        paths_to_clear: Iterable[Path] = [
            self.state.dest_raw_root,
            self.state.initial_csv_dest,
            self.state.history_csv_dest,
        ]
        for target in paths_to_clear:
            if target.exists():
                if target.is_dir():
                    shutil.rmtree(target)
                else:
                    target.unlink()
        self.state.test_root.mkdir(parents=True, exist_ok=True)
        self.state.dest_raw_root.mkdir(parents=True, exist_ok=True)

    # ---------------------------
    #  FILE UPDATES
    # ---------------------------

    def _write_motor_history(self) -> None:
        assert self.state is not None
        motor_rows = self.state.motor_events[: self.state.motor_index]
        with self.state.history_csv_dest.open("w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["time", "motor", "old_pos", "new_pos"])
            for wrapped in motor_rows:
                evt = wrapped.event
                writer.writerow(
                    [
                        evt.time.strftime("%Y-%m-%d %H:%M:%S"),
                        evt.motor,
                        "" if evt.old_pos is None else evt.old_pos,
                        "" if evt.new_pos is None else evt.new_pos,
                    ]
                )

    def _copy_raw_files(self, events: List[RawEvent]) -> None:
        assert self.state is not None
        for evt in events:
            dest_path = self.state.dest_raw_root / evt.rel_path
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(self.state.raw_data_root / evt.rel_path, dest_path)
            os.utime(dest_path, (dest_path.stat().st_atime, evt.time.timestamp()))

    # ---------------------------
    #  SYNC STEPS
    # ---------------------------

    def seed_to_start(self, start_time: datetime) -> None:
        if self.state is None:
            raise RuntimeError("Simulation state is not prepared.")
        self._log_callback("INFO", "Resetting test root for new start time.")
        self._reset_outputs()
        shutil.copy2(self.state.initial_csv_source, self.state.initial_csv_dest)
        self.state.raw_index = 0
        self.state.motor_index = 0
        self.state.sim_time = start_time
        self.last_seed_start = start_time
        self.sync_until(start_time)

    def sync_until(self, target_time: datetime) -> None:
        if self.state is None:
            raise RuntimeError("Simulation state is not prepared.")
        new_raw: List[RawEvent] = []
        while self.state.raw_index < len(self.state.raw_events):
            evt = self.state.raw_events[self.state.raw_index]
            if evt.visible_time > target_time:
                break
            new_raw.append(evt)
            self.state.raw_index += 1
        new_motor = 0
        while self.state.motor_index < len(self.state.motor_events):
            evt = self.state.motor_events[self.state.motor_index]
            if evt.visible_time > target_time:
                break
            self.state.motor_index += 1
            new_motor += 1
        if new_raw:
            self._log_callback("INFO", f"Syncing {len(new_raw)} RAW files up to {target_time}.")
            self._copy_raw_files(new_raw)
        if new_motor:
            self._log_callback("INFO", f"Syncing {new_motor} motor events up to {target_time}.")
            self._write_motor_history()
        self.state.sim_time = target_time

    def advance_one_tick(self) -> None:
        if self.state is None or self.state.sim_time is None:
            raise RuntimeError("Simulation must be seeded before advancing.")
        new_time = self.state.sim_time + timedelta(seconds=self.cloud_period_s)
        self.sync_until(new_time)

    # ---------------------------
    #  LOOP CONTROL
    # ---------------------------

    def start_loop(self) -> None:
        if self.running:
            return
        if self.state is None or self.state.sim_time is None:
            raise RuntimeError("Simulation has not been initialised with a start time.")
        self.running = True
        self.worker = threading.Thread(target=self._run_loop, daemon=True)
        self.worker.start()

    def _run_loop(self) -> None:
        while self.running:
            time.sleep(self.cloud_period_s)
            try:
                with self.lock:
                    self.advance_one_tick()
            except Exception as exc:  # pragma: no cover - safety net
                self._log_callback("ERROR", f"Simulation error: {exc}")
                self.running = False
                break

    def stop_loop(self) -> None:
        self.running = False
        if self.worker and self.worker.is_alive():
            self.worker.join(timeout=1.0)


# ============================================================
#  GUI LAYER
# ============================================================

class ShotLogSimulatorApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("ShotLog Replay Simulator")
        self.gui_queue: queue.Queue[str] = queue.Queue()
        self.controller = SimulationController(self.gui_queue)

        self.raw_source_var = tk.StringVar()
        self.initial_csv_var = tk.StringVar()
        self.history_csv_var = tk.StringVar()
        self.test_root_var = tk.StringVar()
        self.cloud_period_var = tk.StringVar(value="30")
        self.jitter_var = tk.StringVar(value="0")
        self.start_time_var = tk.StringVar()

        self._build_gui()
        self.root.after(200, self._drain_log_queue)

    # ---------------------------
    #  GUI BUILDING
    # ---------------------------

    def _build_gui(self) -> None:
        path_frame = tk.LabelFrame(self.root, text="Sources & Destination")
        path_frame.pack(fill=tk.X, padx=10, pady=5)

        self._add_path_row(path_frame, "RAW source root", self.raw_source_var, is_dir=True)
        self._add_path_row(path_frame, "Motor initial CSV", self.initial_csv_var)
        self._add_path_row(path_frame, "Motor history CSV", self.history_csv_var)
        self._add_path_row(path_frame, "Test root destination", self.test_root_var, is_dir=True)

        params_frame = tk.LabelFrame(self.root, text="Simulation parameters")
        params_frame.pack(fill=tk.X, padx=10, pady=5)

        tk.Label(params_frame, text="Cloud period (s)").grid(row=0, column=0, sticky="w")
        tk.Entry(params_frame, textvariable=self.cloud_period_var, width=10).grid(row=0, column=1, padx=5)
        tk.Label(params_frame, text="Cloud jitter max (s)").grid(row=0, column=2, sticky="w")
        tk.Entry(params_frame, textvariable=self.jitter_var, width=10).grid(row=0, column=3, padx=5)

        tk.Label(params_frame, text="Start time (YYYY-MM-DD HH:MM:SS)").grid(row=1, column=0, sticky="w")
        tk.Entry(params_frame, textvariable=self.start_time_var, width=25).grid(row=1, column=1, padx=5, sticky="w")
        tk.Button(params_frame, text="Set start time", command=self._handle_set_start).grid(row=1, column=2, padx=5)

        controls = tk.Frame(self.root)
        controls.pack(fill=tk.X, padx=10, pady=5)
        self.start_btn = tk.Button(controls, text="Start", command=self._handle_start)
        self.stop_btn = tk.Button(controls, text="Stop", command=self._handle_stop)
        self.update_btn = tk.Button(controls, text="Update now", command=self._handle_update_now)
        self.start_btn.pack(side=tk.LEFT, padx=5)
        self.stop_btn.pack(side=tk.LEFT, padx=5)
        self.update_btn.pack(side=tk.LEFT, padx=5)

        log_frame = tk.LabelFrame(self.root, text="Log")
        log_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        self.log_text = scrolledtext.ScrolledText(log_frame, height=15, state=tk.DISABLED)
        self.log_text.pack(fill=tk.BOTH, expand=True)

    def _add_path_row(self, parent: tk.Widget, label: str, var: tk.StringVar, *, is_dir: bool = False) -> None:
        row = len(parent.grid_slaves()) // 3
        tk.Label(parent, text=label).grid(row=row, column=0, sticky="w")
        tk.Entry(parent, textvariable=var, width=60).grid(row=row, column=1, padx=5)
        btn = tk.Button(parent, text="Browse...", command=lambda: self._browse(var, is_dir))
        btn.grid(row=row, column=2)

    # ---------------------------
    #  LOGGING
    # ---------------------------

    def _drain_log_queue(self) -> None:
        while not self.gui_queue.empty():
            line = self.gui_queue.get()
            self.log_text.configure(state=tk.NORMAL)
            self.log_text.insert(tk.END, line + "\n")
            self.log_text.configure(state=tk.DISABLED)
            self.log_text.see(tk.END)
        self.root.after(200, self._drain_log_queue)

    def _browse(self, var: tk.StringVar, is_dir: bool) -> None:
        if is_dir:
            path = filedialog.askdirectory()
        else:
            path = filedialog.askopenfilename()
        if path:
            var.set(path)

    def _error(self, message: str) -> None:
        messagebox.showerror("ShotLog Simulator", message)
        _log_to_queue(self.gui_queue, "ERROR", message)

    # ---------------------------
    #  PARAMETER PARSING
    # ---------------------------

    def _get_float(self, var: tk.StringVar, default: float, *, minimum: float = 0.0) -> float:
        try:
            value = float(var.get())
        except ValueError:
            self._error(f"Invalid number: {var.get()}")
            raise
        if value < minimum:
            self._error(f"Value must be >= {minimum}: {value}")
            raise ValueError
        return value

    def _parse_start_time(self) -> Optional[datetime]:
        raw = self.start_time_var.get().strip()
        if not raw:
            return None
        parsed = _parse_datetime(raw)
        if parsed is None:
            self._error("Could not parse start time. Use YYYY-MM-DD HH:MM:SS or ISO format.")
            raise ValueError
        return parsed

    def _collect_paths(self) -> tuple[Path, Path, Path, Path]:
        raw_source = Path(self.raw_source_var.get().strip())
        initial_csv = Path(self.initial_csv_var.get().strip())
        history_csv = Path(self.history_csv_var.get().strip())
        test_root = Path(self.test_root_var.get().strip())
        missing = [name for name, path in [
            ("RAW source", raw_source),
            ("Initial CSV", initial_csv),
            ("Motor history CSV", history_csv),
            ("Test root", test_root),
        ] if not str(path)]
        if missing:
            self._error(f"Please provide all required paths: {', '.join(missing)}")
            raise ValueError
        return raw_source, initial_csv, history_csv, test_root

    # ---------------------------
    #  BUTTON HANDLERS
    # ---------------------------

    def _prepare_and_seed(self) -> None:
        raw_source, initial_csv, history_csv, test_root = self._collect_paths()
        jitter = self._get_float(self.jitter_var, default=0.0, minimum=0.0)
        cloud_period = self._get_float(self.cloud_period_var, default=30.0, minimum=0.1)
        start_time = self._parse_start_time()

        try:
            state = self.controller.prepare_state(
                raw_source=raw_source,
                initial_csv=initial_csv,
                history_csv=history_csv,
                test_root=test_root,
                jitter_s=jitter,
                cloud_period_s=cloud_period,
            )
        except Exception as exc:
            self._error(str(exc))
            raise

        effective_start = start_time or state.t_min
        if effective_start is None:
            self._error("Could not determine a start time from the data.")
            raise ValueError
        self.controller.seed_to_start(effective_start)
        _log_to_queue(
            self.gui_queue,
            "INFO",
            f"Start time set to {effective_start} (t_min={state.t_min}).",
        )

    def _handle_set_start(self) -> None:
        if self.controller.running:
            self._error("Stop the simulation before changing the start time.")
            return
        try:
            self._prepare_and_seed()
        except Exception:
            return

    def _handle_start(self) -> None:
        if self.controller.running:
            return
        if self.controller.state is None or self.controller.last_seed_start is None:
            try:
                self._prepare_and_seed()
            except Exception:
                return
        try:
            self.controller.start_loop()
            self.start_btn.configure(state=tk.DISABLED)
            _log_to_queue(self.gui_queue, "INFO", "Simulation started.")
        except Exception as exc:
            self._error(str(exc))

    def _handle_stop(self) -> None:
        self.controller.stop_loop()
        self.start_btn.configure(state=tk.NORMAL)
        _log_to_queue(self.gui_queue, "INFO", "Simulation stopped.")

    def _handle_update_now(self) -> None:
        try:
            if self.controller.state is None or self.controller.last_seed_start is None:
                self._prepare_and_seed()
            self.controller.advance_one_tick()
            _log_to_queue(self.gui_queue, "INFO", "Manual update completed.")
        except Exception as exc:
            self._error(str(exc))


# ============================================================
#  ENTRY POINT
# ============================================================

def main() -> None:
    root = tk.Tk()
    app = ShotLogSimulatorApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
