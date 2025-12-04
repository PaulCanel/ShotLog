from __future__ import annotations
import json
import os
import queue
import re
from datetime import datetime
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

from .config import (
    DEFAULT_CONFIG,
    FolderConfig,
    FolderFileSpec,
    ManualParam,
    ShotLogConfig,
    _parse_extensions_field,
)
from .manager import ShotManager
from .manual_params import build_empty_manual_values, write_manual_params_row

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
        self.var_root = tk.StringVar(value=self.config.project_root or "")
        self.var_raw_folder = tk.StringVar(value=self.config.raw_root_suffix)
        self.var_clean_folder = tk.StringVar(value=self.config.clean_root_suffix)
        self.var_log_folder = tk.StringVar(value=self.config.rename_log_folder_suffix)
        self.var_date_mode = tk.StringVar(value="auto")
        self.var_manual_date = tk.StringVar(value="")
        self.trigger_cam_vars = {}
        self.used_cam_vars = {}
        self.manual_param_vars: dict[str, tk.StringVar] = {}
        self.manual_entries: dict[str, ttk.Entry] = {}
        self.manual_confirm_labels: dict[str, ttk.Label] = {}
        self.manual_target_date: str | None = None
        self.manual_target_index: int | None = None
        self.manual_target_trigger_time: str | datetime | None = None
        self.manual_last_written_key: tuple[str, int] | None = None
        self.manual_confirmed_values: dict[str, str] = {}
        self.manual_enabled: bool = False
        self.var_manual_params_csv = tk.StringVar(value=self.config.manual_params_csv_path or "")
        self.var_use_default_manual_params = tk.BooleanVar(
            value=self.config.use_default_manual_params_path
        )
        self.var_use_default_motor_output = tk.BooleanVar(
            value=self.config.use_default_motor_positions_path
        )

        self._default_manual_path: Path | None = None
        self._default_manual_clean_root: Path | None = None
        self._default_motor_path: Path | None = None
        self._default_motor_clean_root: Path | None = None
        self.ent_manual_params_csv: ttk.Entry | None = None
        self.btn_manual_params_browse: ttk.Button | None = None
        self.ent_motor_output: ttk.Entry | None = None
        self.btn_motor_output_browse: ttk.Button | None = None

        self._number_validator = self.root.register(self._validate_number_entry)

        self._build_gui()
        self._update_date_mode_label()
        self._update_path_labels()
        self._reset_manual_state()
        self._apply_default_paths()

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

        # Project root and folders
        frm_root = ttk.LabelFrame(self.content_frame, text="Paths")
        frm_root.pack(fill="x", padx=5, pady=5)

        ttk.Label(frm_root, text="Base root:").grid(row=0, column=0, sticky="w")
        ttk.Entry(frm_root, textvariable=self.var_root, width=60).grid(row=0, column=1, sticky="we", padx=5)
        ttk.Button(frm_root, text="Browse", command=self._choose_root).grid(row=0, column=2, padx=5)

        ttk.Label(frm_root, text="RAW folder name:").grid(row=1, column=0, sticky="w")
        ttk.Entry(frm_root, textvariable=self.var_raw_folder, width=30).grid(row=1, column=1, sticky="w", padx=5)

        ttk.Label(frm_root, text="CLEAN folder name:").grid(row=2, column=0, sticky="w")
        ttk.Entry(frm_root, textvariable=self.var_clean_folder, width=30).grid(row=2, column=1, sticky="w", padx=5)

        ttk.Label(frm_root, text="LOG folder name:").grid(row=3, column=0, sticky="w")
        ttk.Entry(frm_root, textvariable=self.var_log_folder, width=30).grid(row=3, column=1, sticky="w", padx=5)

        ttk.Button(frm_root, text="Apply paths", command=self._apply_paths).grid(row=4, column=0, padx=5, pady=5, sticky="w")

        ttk.Label(frm_root, text="RAW data folder:").grid(row=5, column=0, sticky="w")
        self.lbl_raw_path = ttk.Label(frm_root, text="-")
        self.lbl_raw_path.grid(row=5, column=1, columnspan=2, sticky="w")

        ttk.Label(frm_root, text="CLEAN data folder:").grid(row=6, column=0, sticky="w")
        self.lbl_clean_path = ttk.Label(frm_root, text="-")
        self.lbl_clean_path.grid(row=6, column=1, columnspan=2, sticky="w")

        ttk.Label(frm_root, text="Log folder:").grid(row=7, column=0, sticky="w")
        self.lbl_log_path = ttk.Label(frm_root, text="-")
        self.lbl_log_path.grid(row=7, column=1, columnspan=2, sticky="w")

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
        self.ent_manual_params_csv = ttk.Entry(
            frm_manual_cfg, textvariable=self.var_manual_params_csv, width=50
        )
        self.ent_manual_params_csv.grid(row=0, column=2, sticky="we", padx=5)
        self.btn_manual_params_browse = ttk.Button(
            frm_manual_cfg, text="Browse...", command=self._choose_manual_params_csv
        )
        self.btn_manual_params_browse.grid(row=0, column=3, padx=5, pady=5)
        ttk.Checkbutton(
            frm_manual_cfg,
            text="Use default manual params path",
            variable=self.var_use_default_manual_params,
            command=self._on_toggle_default_manual_params,
        ).grid(row=0, column=4, padx=5, sticky="w")
        frm_manual_cfg.columnconfigure(2, weight=1)

        frm_manual_params = ttk.LabelFrame(self.content_frame, text="Manual parameters (per shot)")
        frm_manual_params.pack(fill="x", padx=5, pady=5)
        manual_header = ttk.Frame(frm_manual_params)
        manual_header.grid(row=0, column=0, sticky="we", padx=5, pady=(0, 5))
        self.lbl_manual_target = ttk.Label(manual_header, text="Manual parameters for: no shot yet")
        self.lbl_manual_target.grid(row=0, column=0, sticky="w")

        self.manual_confirm_values_frame = ttk.Frame(frm_manual_params)
        self.manual_confirm_values_frame.grid(row=1, column=0, sticky="we", padx=5, pady=(0, 10))

        self.frm_manual_params_fields = ttk.Frame(frm_manual_params)
        self.frm_manual_params_fields.grid(row=2, column=0, sticky="we", padx=5, pady=5)

        self.btn_manual_confirm = ttk.Button(
            frm_manual_params, text="Confirm", command=self._on_manual_confirm_clicked, state="disabled"
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
        self.ent_motor_output = ttk.Entry(
            frm_motor, textvariable=self.var_motor_output, width=50
        )
        self.ent_motor_output.grid(row=2, column=1, sticky="we", padx=5)
        self.btn_motor_output_browse = ttk.Button(
            frm_motor, text="Browse...", command=self._choose_motor_output
        )
        self.btn_motor_output_browse.grid(row=2, column=2, padx=5)
        ttk.Checkbutton(
            frm_motor,
            text="Use default motor positions path",
            variable=self.var_use_default_motor_output,
            command=self._on_toggle_default_motor_output,
        ).grid(row=2, column=3, padx=5, sticky="w")

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
            self._update_path_labels()

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
                self._apply_default_paths()
                self._after_config_changed()
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
            self._apply_default_paths()
            self._after_config_changed()
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
            self.var_motor_output.set(str(Path(path).with_suffix(".csv")))

    def _get_effective_date_str(self) -> str:
        if self.var_date_mode.get() == "manual":
            date_str = self.var_manual_date.get().strip()
            if date_str:
                return date_str
        return datetime.today().strftime("%Y%m%d")

    def _compute_clean_root(self) -> Path | None:
        base_root = self.var_root.get().strip()
        if not base_root:
            return None
        clean_name = self.var_clean_folder.get().strip() or self.config.clean_root_suffix
        return Path(base_root) / clean_name

    def _get_default_manual_params_path(self) -> Path | None:
        clean_root = self._compute_clean_root()
        if not clean_root:
            return None
        if self._default_manual_clean_root != clean_root:
            self._reset_default_manual_cache()
            self._default_manual_clean_root = clean_root
        if self._default_manual_path is None:
            self._default_manual_path = self._build_timestamped_default_path(
                clean_root, "manual_parameters", "shot_manual_params"
            )
        return self._default_manual_path

    def _get_default_motor_positions_path(self) -> Path | None:
        clean_root = self._compute_clean_root()
        if not clean_root:
            return None
        if self._default_motor_clean_root != clean_root:
            self._reset_default_motor_cache()
            self._default_motor_clean_root = clean_root
        if self._default_motor_path is None:
            self._default_motor_path = self._build_timestamped_default_path(
                clean_root, "motors_parameters", "shot_motor_positions"
            )
        return self._default_motor_path

    def _build_timestamped_default_path(self, clean_root: Path, subfolder: str, prefix: str) -> Path:
        now = datetime.now()
        stamp = now.strftime("%Y%m%d_%H%M%S")
        target_dir = clean_root / subfolder
        target_dir.mkdir(parents=True, exist_ok=True)
        return target_dir / f"{prefix}_{stamp}.csv"

    def _reset_default_manual_cache(self):
        self._default_manual_path = None
        self._default_manual_clean_root = None

    def _reset_default_motor_cache(self):
        self._default_motor_path = None
        self._default_motor_clean_root = None

    def _reset_default_path_cache(self):
        self._reset_default_manual_cache()
        self._reset_default_motor_cache()

    def _normalize_csv_path_str(self, raw: str | None) -> str | None:
        if not raw:
            return None
        return str(Path(raw).with_suffix(".csv"))

    def _apply_default_paths(self):
        if self.var_use_default_manual_params.get():
            default_manual = self._get_default_manual_params_path()
            if default_manual:
                self.var_manual_params_csv.set(str(default_manual.with_suffix(".csv")))
            if self.ent_manual_params_csv:
                self.ent_manual_params_csv.state(["disabled"])
            if self.btn_manual_params_browse:
                self.btn_manual_params_browse.state(["disabled"])
        else:
            if self.ent_manual_params_csv:
                self.ent_manual_params_csv.state(["!disabled"])
            if self.btn_manual_params_browse:
                self.btn_manual_params_browse.state(["!disabled"])

        if self.var_use_default_motor_output.get():
            default_motor = self._get_default_motor_positions_path()
            if default_motor:
                self.var_motor_output.set(str(default_motor.with_suffix(".csv")))
            if self.ent_motor_output:
                self.ent_motor_output.state(["disabled"])
            if self.btn_motor_output_browse:
                self.btn_motor_output_browse.state(["disabled"])
        else:
            if self.ent_motor_output:
                self.ent_motor_output.state(["!disabled"])
            if self.btn_motor_output_browse:
                self.btn_motor_output_browse.state(["!disabled"])

    def _on_toggle_default_manual_params(self):
        self._reset_default_manual_cache()
        self._apply_default_paths()
        self._after_config_changed()

    def _on_toggle_default_motor_output(self):
        self._reset_default_motor_cache()
        self._apply_default_paths()
        self._after_config_changed()

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
        cfg.raw_root_suffix = self.var_raw_folder.get().strip() or cfg.raw_root_suffix
        cfg.clean_root_suffix = self.var_clean_folder.get().strip() or cfg.clean_root_suffix
        cfg.rename_log_folder_suffix = (
            self.var_log_folder.get().strip() or cfg.rename_log_folder_suffix
        )
        if self.var_date_mode.get() == "manual":
            cfg.manual_date_override = self.var_manual_date.get().strip() or None
        else:
            cfg.manual_date_override = None
        cfg.motor_initial_csv = self.var_motor_initial.get()
        cfg.motor_history_csv = self.var_motor_history.get()
        cfg.motor_positions_output = self._normalize_csv_path_str(self.var_motor_output.get()) or ""
        cfg.use_default_motor_positions_path = self.var_use_default_motor_output.get()
        cfg.manual_params = [ManualParam(name=p.name, type=p.type) for p in self.config.manual_params]
        cfg.manual_params_csv_path = self._normalize_csv_path_str(
            self.var_manual_params_csv.get()
        )
        cfg.use_default_manual_params_path = self.var_use_default_manual_params.get()
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

    def _update_path_labels(
        self,
        *,
        raw_path: Path | None = None,
        clean_path: Path | None = None,
        log_path: Path | None = None,
    ):
        if self.manager:
            raw_val = raw_path or getattr(self.manager, "raw_root", None)
            clean_val = clean_path or getattr(self.manager, "clean_root", None)
            log_val = log_path or getattr(self.manager, "log_dir", None)
        else:
            base_root_str = self.var_root.get().strip()
            base_root = Path(base_root_str) if base_root_str else None
            raw_name = self.var_raw_folder.get().strip() or self.config.raw_root_suffix
            clean_name = self.var_clean_folder.get().strip() or self.config.clean_root_suffix
            log_name = self.var_log_folder.get().strip() or self.config.rename_log_folder_suffix
            raw_val = raw_path or (base_root / raw_name if base_root else None)
            clean_val = clean_path or (base_root / clean_name if base_root else None)
            log_val = log_path or (base_root / log_name if base_root else None)

        self.lbl_raw_path.configure(text=str(raw_val) if raw_val else "-")
        self.lbl_clean_path.configure(text=str(clean_val) if clean_val else "-")
        self.lbl_log_path.configure(text=str(log_val) if log_val else "-")

    def _apply_paths(self):
        base_root = self.var_root.get().strip()
        raw_name = self.var_raw_folder.get().strip()
        clean_name = self.var_clean_folder.get().strip()
        log_name = self.var_log_folder.get().strip()

        if not base_root:
            messagebox.showerror("Error", "Please choose a base root directory.")
            return
        if not os.path.isdir(base_root):
            messagebox.showerror("Error", f"Invalid root directory: {base_root}")
            return

        for label, value in (("RAW folder name", raw_name), ("CLEAN folder name", clean_name), ("LOG folder name", log_name)):
            if not value:
                messagebox.showerror("Error", f"{label} cannot be empty.")
                return

        root_path = Path(base_root)
        raw_path = root_path / raw_name
        clean_path = root_path / clean_name
        log_path = root_path / log_name

        for path in (raw_path, clean_path, log_path):
            path.mkdir(parents=True, exist_ok=True)

        self.config.project_root = base_root
        self.config.raw_root_suffix = raw_name
        self.config.clean_root_suffix = clean_name
        self.config.rename_log_folder_suffix = log_name

        self._apply_default_paths()
        runtime_config = self._build_runtime_config()
        self.config = runtime_config.clone()
        if self.manager:
            self.manager.update_config(runtime_config)

        self._append_log(f"[INFO] Using RAW folder: {raw_path}")
        self._append_log(f"[INFO] Using CLEAN folder: {clean_path}")
        self._append_log(f"[INFO] Using log folder: {log_path}")
        self._append_log("[INFO] Paths applied successfully.")
        self._update_path_labels(raw_path=raw_path, clean_path=clean_path, log_path=log_path)

    def _stop(self):
        if self.manager:
            try:
                self._flush_manual_params_on_stop()
            except Exception as e:
                self._append_log(f"[WARNING] Failed to flush manual parameters on stop: {e}")
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

        ttk.Label(top, text="File definitions (keyword + extensions)").grid(row=2, column=0, columnspan=2, sticky="w", padx=5)
        spec_columns = ("keyword", "extensions")
        spec_tree = ttk.Treeview(top, columns=spec_columns, show="headings", height=5)
        spec_tree.heading("keyword", text="Keyword")
        spec_tree.heading("extensions", text="Extensions")
        spec_tree.column("keyword", width=160)
        spec_tree.column("extensions", width=180)
        spec_tree.grid(row=3, column=0, columnspan=2, sticky="nsew", padx=5, pady=5)

        specs = [
            FolderFileSpec(keyword=s.keyword, extensions=list(s.extensions))
            for s in (folder.file_specs if folder else [FolderFileSpec(extensions=[".tif"])])
        ]

        def refresh_specs():
            spec_tree.delete(*spec_tree.get_children())
            for idx, spec in enumerate(specs):
                ext_desc = ", ".join(spec.normalized_extensions) if spec.normalized_extensions else "(any)"
                spec_tree.insert("", "end", iid=str(idx), values=(spec.keyword, ext_desc))

        kw_var = tk.StringVar()
        ext_var = tk.StringVar()

        def add_spec():
            extensions = _parse_extensions_field(ext_var.get())
            specs.append(FolderFileSpec(keyword=kw_var.get().strip(), extensions=extensions))
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
        ttk.Label(top, text="Extensions (comma-separated, empty = any):").grid(row=5, column=0, sticky="w", padx=5, pady=2)
        ttk.Entry(top, textvariable=ext_var, width=30).grid(row=5, column=1, sticky="w", padx=5, pady=2)

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
                file_specs=[FolderFileSpec(keyword=s.keyword, extensions=list(s.extensions)) for s in specs],
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

        params = [ManualParam(p.name, p.type) for p in self.config.manual_params]

        lst = tk.Listbox(top, height=8, width=40)
        lst.grid(row=0, column=0, columnspan=4, padx=5, pady=5, sticky="nsew")
        top.grid_columnconfigure(0, weight=1)
        top.grid_rowconfigure(0, weight=1)

        def refresh_list():
            lst.delete(0, tk.END)
            for p in params:
                lst.insert(tk.END, f"{p.name} ({p.type})")

        name_var = tk.StringVar()
        type_var = tk.StringVar(value="text")

        def on_select(event=None):
            selection = lst.curselection()
            if not selection:
                return
            p = params[selection[0]]
            name_var.set(p.name)
            type_var.set(p.type)

        def on_add_or_update():
            name = name_var.get().strip()
            param_type = type_var.get().strip().lower() or "text"
            if param_type not in {"text", "number"}:
                param_type = "text"
            if not name:
                messagebox.showerror("Error", "Parameter name cannot be empty.")
                return

            selection = lst.curselection()
            existing_names = {p.name for p in params}
            if selection:
                idx = selection[0]
                if name != params[idx].name and name in existing_names:
                    messagebox.showerror("Error", "Parameter names must be unique.")
                    return
                params[idx] = ManualParam(name=name, type=param_type)
            else:
                if name in existing_names:
                    messagebox.showerror("Error", "Parameter names must be unique.")
                    return
                params.append(ManualParam(name=name, type=param_type))

            refresh_list()
            name_var.set("")
            type_var.set("text")

        def on_remove():
            selection = lst.curselection()
            if not selection:
                messagebox.showwarning("Warning", "Please select a parameter to remove.")
                return
            idx = selection[0]
            params.pop(idx)
            refresh_list()
            name_var.set("")
            type_var.set("text")

        ttk.Label(top, text="Name:").grid(row=1, column=0, sticky="e", padx=5, pady=2)
        ttk.Entry(top, textvariable=name_var, width=25).grid(row=1, column=1, sticky="w", padx=5, pady=2)
        ttk.Label(top, text="Type:").grid(row=1, column=2, sticky="e", padx=5, pady=2)
        ttk.Combobox(top, values=["text", "number"], textvariable=type_var, state="readonly", width=10).grid(
            row=1, column=3, sticky="w", padx=5, pady=2
        )
        ttk.Button(top, text="Add / Update", command=on_add_or_update).grid(row=2, column=1, sticky="w", padx=5, pady=5)
        ttk.Button(top, text="Remove selected", command=on_remove).grid(row=2, column=2, columnspan=2, sticky="w", padx=5, pady=5)

        def on_ok():
            clean_params: list[ManualParam] = []
            seen: set[str] = set()
            for p in params:
                name = p.name.strip()
                if not name:
                    continue
                if name in seen:
                    messagebox.showerror("Error", "Parameter names must be unique and non-empty.")
                    return
                seen.add(name)
                ptype = p.type if p.type in ["text", "number"] else "text"
                clean_params.append(ManualParam(name=name, type=ptype))

            self.config.manual_params = clean_params
            self._rebuild_manual_param_fields()
            self._rebuild_manual_confirm_display()
            self._clear_manual_param_entries()
            self._reset_manual_state()
            self._after_config_changed()
            top.destroy()

        lst.bind("<<ListboxSelect>>", on_select)

        ttk.Button(top, text="OK", command=on_ok).grid(row=3, column=2, sticky="e", padx=5, pady=5)
        ttk.Button(top, text="Cancel", command=top.destroy).grid(row=3, column=3, sticky="w", padx=5, pady=5)

        refresh_list()
        top.grab_set()
        top.wait_window()

    def _rebuild_manual_param_fields(self):
        for child in self.frm_manual_params_fields.winfo_children():
            child.destroy()
        self.manual_param_vars = {}
        self.manual_entries = {}

        if not self.config.manual_params:
            ttk.Label(self.frm_manual_params_fields, text="No manual parameters defined.").grid(
                row=0, column=0, sticky="w", padx=5, pady=2
            )
            return

        for idx, param in enumerate(self.config.manual_params):
            name = param.name
            label_txt = f"{name} ({param.type})" if param.type else f"{name}"
            ttk.Label(self.frm_manual_params_fields, text=f"{label_txt}:").grid(
                row=idx, column=0, sticky="w", padx=5, pady=2
            )
            var = tk.StringVar()
            self.manual_param_vars[name] = var
            entry = ttk.Entry(self.frm_manual_params_fields, textvariable=var, width=50)
            if param.type == "number":
                entry.configure(validate="key", validatecommand=(self._number_validator, "%P"))
            entry.grid(
                row=idx, column=1, sticky="we", padx=5, pady=2
            )
            self.manual_entries[name] = entry
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

        for idx, param in enumerate(self.config.manual_params):
            name = param.name
            label_txt = f"{name} ({param.type})" if param.type else f"{name}"
            ttk.Label(self.manual_confirm_values_frame, text=f"{label_txt} :").grid(
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

    def _build_empty_manual_values(self) -> dict[str, str]:
        return build_empty_manual_values(self.config.manual_params)

    def _update_manual_target_label(self):
        if self.manual_target_index is None:
            text = "Manual parameters for: no shot yet"
        else:
            text = f"Manual parameters for: shot {self.manual_target_index:03d}"
        self.lbl_manual_target.configure(text=text)

    def _update_manual_confirm_state(self):
        state = "normal" if self.manual_enabled else "disabled"
        self.btn_manual_confirm.configure(state=state)

    def _update_manual_confirm_display(self):
        for name, lbl in self.manual_confirm_labels.items():
            value = self.manual_confirmed_values.get(name, "-")
            lbl.configure(text=value if value else "-")
        self._update_manual_target_label()

    def _validate_number_entry(self, proposed: str) -> bool:
        if proposed == "":
            return True
        pattern = r"^[+-]?((\d+(\.\d*)?)|(\.\d+))?([eE][+-]?\d*)?$"
        return bool(re.fullmatch(pattern, proposed))

    def _reset_manual_state(self):
        self.manual_target_date = None
        self.manual_target_index = None
        self.manual_target_trigger_time = None
        self.manual_last_written_key = None
        self.manual_confirmed_values = self._build_empty_manual_values()
        self.manual_enabled = False
        self._update_manual_confirm_state()
        self._update_manual_target_label()
        self._update_manual_confirm_display()

    def _on_manual_confirm_clicked(self):
        if not self.manual_enabled:
            return

        if self.manual_target_date is None or self.manual_target_index is None:
            return

        values: dict[str, str] = {}
        for name, entry in self.manual_entries.items():
            raw = entry.get().strip()
            values[name] = raw

        self.manual_confirmed_values = values
        self._update_manual_confirm_display()
        self._update_manual_confirm_state()

    def _choose_manual_params_csv(self):
        path = filedialog.asksaveasfilename(
            title="Choose output CSV for manual parameters by shot",
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
        )
        if path:
            self.var_manual_params_csv.set(str(Path(path).with_suffix(".csv")))
            self._after_config_changed()

    def _get_manual_params_output_path(self) -> Path | None:
        if self.manager and getattr(self.manager, "manual_params_csv_path", None):
            return Path(self.manager.manual_params_csv_path).with_suffix(".csv")

        if self.var_use_default_manual_params.get():
            default_path = self._get_default_manual_params_path()
            return default_path.with_suffix(".csv") if default_path else None

        cfg_path = self.config.manual_params_csv_path
        if cfg_path:
            p = Path(cfg_path)
        else:
            p = Path("manual_params_by_shot.csv")

        root_str = self.var_root.get().strip()
        root = Path(root_str) if root_str else Path(".")
        if not p.is_absolute():
            p = root / p

        return p.with_suffix(".csv")

    def _write_manual_line_for_target(self, trigger_time: str | None):
        if self.manual_target_date is None or self.manual_target_index is None:
            return

        key = (self.manual_target_date, self.manual_target_index)
        if self.manual_last_written_key == key:
            return

        output_path = self._get_manual_params_output_path()
        if output_path is None:
            self._append_log("[WARNING] Manual params CSV path not set; skipping write.")
            return

        output_path.parent.mkdir(parents=True, exist_ok=True)

        write_manual_params_row(
            output_path,
            self.config.manual_params,
            self.manual_target_date,
            self.manual_target_index,
            trigger_time,
            self.manual_confirmed_values,
        )

        self.manual_last_written_key = key
        self._append_log(
            f"Manual parameters recorded for shot {self.manual_target_index:03d} "
            f"({self.manual_target_date}) -> {output_path}"
        )

    def _handle_manual_params_status(self, status: dict):
        status = status or {}
        last_completed_idx = status.get("last_completed_shot_index")
        last_completed_date = status.get("last_completed_shot_date")
        last_completed_trig = status.get("last_completed_trigger_time")

        if last_completed_date and last_completed_idx is not None:
            last_key = (last_completed_date, last_completed_idx)
        else:
            last_key = None

        if last_key is None:
            self._reset_manual_state()
            return

        manual_target_key = (self.manual_target_date, self.manual_target_index)

        if self.manual_target_date is None or self.manual_target_index is None:
            self.manual_target_date = last_completed_date
            self.manual_target_index = last_completed_idx
            self.manual_target_trigger_time = last_completed_trig
            self.manual_last_written_key = None
            self.manual_confirmed_values = self._build_empty_manual_values()
            self.manual_enabled = True
            self._update_manual_confirm_state()
            self._update_manual_confirm_display()
            return

        if last_key == manual_target_key:
            self.manual_target_trigger_time = last_completed_trig
            self.manual_enabled = True
            self._update_manual_confirm_state()
            self._update_manual_confirm_display()
            return

        trigger_str = (
            self.manual_target_trigger_time.isoformat(sep=" ")
            if isinstance(self.manual_target_trigger_time, datetime)
            else self.manual_target_trigger_time
        )
        self._write_manual_line_for_target(trigger_str)

        self.manual_target_date = last_completed_date
        self.manual_target_index = last_completed_idx
        self.manual_target_trigger_time = last_completed_trig
        self.manual_confirmed_values = self._build_empty_manual_values()
        self.manual_enabled = True

        self._update_manual_confirm_state()
        self._update_manual_confirm_display()

    def _flush_manual_params_on_stop(self):
        if self.manual_target_date is None or self.manual_target_index is None:
            self._reset_manual_state()
            return

        key = (self.manual_target_date, self.manual_target_index)
        if self.manual_last_written_key != key:
            trigger = (
                self.manual_target_trigger_time.isoformat(sep=" ")
                if isinstance(self.manual_target_trigger_time, datetime)
                else self.manual_target_trigger_time
            )

            if trigger is None and self.manager:
                try:
                    status = self.manager.get_status()
                    if (
                        status.get("last_completed_shot_date") == self.manual_target_date
                        and status.get("last_completed_shot_index") == self.manual_target_index
                    ):
                        trigger = status.get("last_completed_trigger_time")
                except Exception:
                    pass

            self._write_manual_line_for_target(trigger)

        self._reset_manual_state()

    def _after_config_changed(self):
        self.config.use_default_manual_params_path = self.var_use_default_manual_params.get()
        self.config.manual_params_csv_path = self._normalize_csv_path_str(
            self.var_manual_params_csv.get().strip()
        )
        self.config.use_default_motor_positions_path = self.var_use_default_motor_output.get()
        self.config.motor_positions_output = self._normalize_csv_path_str(
            self.var_motor_output.get().strip()
        ) or ""
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
            self._append_log(
                f"[INFO] Loaded paths: RAW='{self.config.raw_root_suffix}', "
                f"CLEAN='{self.config.clean_root_suffix}', LOG='{self.config.rename_log_folder_suffix}'"
            )
            self._apply_paths()
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
        if self.config.project_root:
            self.var_root.set(self.config.project_root)
        self.var_raw_folder.set(self.config.raw_root_suffix)
        self.var_clean_folder.set(self.config.clean_root_suffix)
        self.var_log_folder.set(self.config.rename_log_folder_suffix)
        self.var_window.set(str(self.config.full_window_s))
        self.var_timeout.set(str(self.config.timeout_s))
        self.var_global_kw.set(self.config.global_trigger_keyword)
        self.var_apply_global_kw.set(self.config.apply_global_keyword_to_all)
        self.var_motor_initial.set(self.config.motor_initial_csv)
        self.var_motor_history.set(self.config.motor_history_csv)
        self.var_motor_output.set(self.config.motor_positions_output)
        self.var_manual_params_csv.set(self.config.manual_params_csv_path or "")
        self.var_use_default_manual_params.set(self.config.use_default_manual_params_path)
        self.var_use_default_motor_output.set(self.config.use_default_motor_positions_path)
        if not self.config.expected_folders and self.config.folders:
            for folder in self.config.folders.values():
                folder.expected = True
        self._rebuild_manual_param_fields()
        self._rebuild_manual_confirm_display()
        self._clear_manual_param_entries()
        self._reset_manual_state()
        self._refresh_folder_labels()
        self.lbl_keyword.configure(text=self.config.global_trigger_keyword)
        self.lbl_timing.configure(
            text=f"window={self.config.full_window_s} / timeout={self.config.timeout_s}"
        )
        self._reset_default_path_cache()
        self._apply_default_paths()
        self._update_path_labels()

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
            self._update_path_labels()
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
            self._update_path_labels()
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
            self._reset_manual_state()

        self.root.after(500, self._update_status_labels)


# ============================================================
#  MAIN
# ============================================================

def main():
    root = tk.Tk()
    app = ShotManagerGUI(root)
