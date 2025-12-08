import csv
import re
from dataclasses import dataclass, field
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from openpyxl import Workbook
from openpyxl.styles import PatternFill, Font
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer


# ==========================
# Helpers / models
# ==========================

@dataclass
class LogShot:
    date: str
    shot: int
    expected: set[str] = field(default_factory=set)
    missing: set[str] = field(default_factory=set)
    trigger_cams: set[str] = field(default_factory=set)
    trigger_time: str | None = None
    status: str = "ongoing"  # ongoing / complete
    raw_line: str | None = None


@dataclass
class ShotViewRow:
    key: tuple[str, int]
    values: list[str]
    bg: str
    yellow_text: bool


class LogParser:
    def __init__(self):
        self.shots: dict[tuple[str, int], LogShot] = {}

    def parse(self, path: Path):
        self.shots = {}
        re_new = re.compile(
            r"\*\*\* New shot detected: date=(\d{8}), shot=(\d+), camera=([A-Za-z0-9_]+), ref_time=(\d{2}:\d{2}:\d{2}) \*\*\*"
        )
        re_missing_expected = re.compile(
            r"Shot (\d+) \((\d{8})\) acquired.*?expected=\[(.*)\].*missing cameras: \[(.*)\]"
        )
        re_missing = re.compile(r"Shot (\d+) \((\d{8})\) acquired.*missing cameras: \[(.*)\]")
        re_ok_expected = re.compile(
            r"Shot (\d+) \((\d{8})\) acquired successfully, expected=\[(.*)\], all cameras present\."
        )
        re_ok = re.compile(r"Shot (\d+) \((\d{8})\) acquired successfully, all cameras present\.")
        re_expected_update = re.compile(r"Updated expected cameras \(used diagnostics\): \[(.*)\]")

        with path.open("r", encoding="utf-8") as f:
            current_expected: set[str] = set()
            for line in f:
                line = line.strip()
                m = re_expected_update.search(line)
                if m:
                    current_expected = set(self._parse_list(m.group(1)))
                    continue

                m = re_new.search(line)
                if m:
                    date_str = m.group(1)
                    shot = int(m.group(2))
                    cam = m.group(3)
                    ref_time = m.group(4)
                    self.shots[(date_str, shot)] = LogShot(
                        date=date_str,
                        shot=shot,
                        trigger_cams={cam},
                        trigger_time=ref_time,
                        expected=set(current_expected),
                        missing=set(),
                        status="ongoing",
                        raw_line=line,
                    )
                    continue

                m = re_missing_expected.search(line)
                if m:
                    shot = int(m.group(1))
                    date_str = m.group(2)
                    expected = set(self._parse_list(m.group(3)))
                    missing = set(self._parse_list(m.group(4)))
                    self._finalize(date_str, shot, expected, missing, line)
                    continue

                m = re_missing.search(line)
                if m:
                    shot = int(m.group(1))
                    date_str = m.group(2)
                    missing = set(self._parse_list(m.group(3)))
                    self._finalize(date_str, shot, current_expected, missing, line)
                    continue

                m = re_ok_expected.search(line)
                if m:
                    shot = int(m.group(1))
                    date_str = m.group(2)
                    expected = set(self._parse_list(m.group(3)))
                    self._finalize(date_str, shot, expected, set(), line)
                    continue

                m = re_ok.search(line)
                if m:
                    shot = int(m.group(1))
                    date_str = m.group(2)
                    self._finalize(date_str, shot, current_expected, set(), line)
                    continue

    def _finalize(self, date_str: str, shot: int, expected: set[str], missing: set[str], line: str):
        key = (date_str, shot)
        shot_obj = self.shots.get(key) or LogShot(date=date_str, shot=shot)
        shot_obj.expected = set(expected)
        shot_obj.missing = set(missing)
        shot_obj.status = "complete"
        shot_obj.raw_line = line
        self.shots[key] = shot_obj

    @staticmethod
    def _parse_list(text: str) -> list[str]:
        return [p.strip() for p in text.split(",") if p.strip()]


# ==========================
# Watchdog wrapper
# ==========================

class TargetWatcher(FileSystemEventHandler):
    def __init__(self, path: Path, callback):
        super().__init__()
        self.path = path
        self.callback = callback

    def on_modified(self, event):
        if Path(event.src_path) == self.path:
            self.callback()

    def on_created(self, event):
        if Path(event.src_path) == self.path:
            self.callback()


# ==========================
# Main GUI
# ==========================

class ShotLogReader:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Shot Log Reader 3")

        self.log_path: Path | None = None
        self.manual_path: Path | None = None
        self.motor_path: Path | None = None

        self.log_parser = LogParser()
        self.manual_rows: list[dict] = []
        self.motor_rows: list[dict] = []

        self.observer = Observer()
        self.watch_schedules = []
        self._handlers: list[TargetWatcher] = []

        self.previous_rows = {
            "log": [],
            "manual": [],
            "motor": [],
        }

        self._build_ui()

    # ---------- UI ----------
    def _build_ui(self):
        btn_frame = tk.Frame(self.root)
        btn_frame.pack(fill="x", padx=10, pady=10)

        tk.Button(btn_frame, text="Select LOG (.txt)", command=self._select_log).pack(side="left", padx=5)
        tk.Button(btn_frame, text="Select MANUAL CSV", command=self._select_manual).pack(side="left", padx=5)
        tk.Button(btn_frame, text="Select MOTOR CSV", command=self._select_motor).pack(side="left", padx=5)
        tk.Button(btn_frame, text="Export Excel", command=self._export_excel).pack(side="right", padx=5)

        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True, padx=10, pady=10)

        self.trees = {}
        for key, title, columns in [
            ("log", "Logs", ["Date", "Shot", "Expected", "Missing", "Trigger", "Status"]),
            ("manual", "Manual Params", ["Date", "Shot", "Time", "Data"]),
            ("motor", "Motor Params", ["Date", "Shot", "Time", "Data"]),
        ]:
            frame = ttk.Frame(self.notebook)
            self.notebook.add(frame, text=title)
            tree = ttk.Treeview(frame, columns=columns, show="headings")
            for col in columns:
                tree.heading(col, text=col)
                tree.column(col, width=140, anchor="center")
            tree.pack(fill="both", expand=True)
            tree.tag_configure("bg_green", background="#7FFF7F")
            tree.tag_configure("bg_blue", background="#7FBFFF")
            tree.tag_configure("bg_red", background="#FF7F7F")
            tree.tag_configure("bg_orange", background="#FFB347")
            tree.tag_configure("fg_yellow", foreground="#FFFF00")
            self.trees[key] = tree

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------- File selection ----------
    def _select_log(self):
        selected = filedialog.askopenfilename(title="Select log file", filetypes=[("Log files", "*.txt"), ("All", "*.*")])
        if not selected:
            return
        self.log_path = Path(selected)
        self._refresh_watchers()
        self._parse_all()

    def _select_manual(self):
        selected = filedialog.askopenfilename(title="Select manual CSV", filetypes=[("CSV", "*.csv"), ("All", "*.*")])
        if not selected:
            return
        self.manual_path = Path(selected)
        self._refresh_watchers()
        self._parse_all()

    def _select_motor(self):
        selected = filedialog.askopenfilename(title="Select motor CSV", filetypes=[("CSV", "*.csv"), ("All", "*.*")])
        if not selected:
            return
        self.motor_path = Path(selected)
        self._refresh_watchers()
        self._parse_all()

    # ---------- Watchdog ----------
    def _refresh_watchers(self):
        for sched in self.watch_schedules:
            try:
                self.observer.unschedule(sched)
            except Exception:
                pass
        self.watch_schedules = []
        self._handlers = []

        for path in [self.log_path, self.manual_path, self.motor_path]:
            if path is None:
                continue
            watcher = TargetWatcher(path, self._parse_all)
            self._handlers.append(watcher)
            schedule = self.observer.schedule(watcher, path.parent, recursive=False)
            self.watch_schedules.append(schedule)

        if not self.observer.is_alive():
            self.observer.start()

    # ---------- Parsing ----------
    def _parse_all(self):
        try:
            if self.log_path:
                self.log_parser.parse(self.log_path)
            if self.manual_path:
                self.manual_rows = self._parse_csv(self.manual_path)
            if self.motor_path:
                self.motor_rows = self._parse_csv(self.motor_path)
            self._refresh_tables()
        except Exception as e:
            messagebox.showerror("Error", str(e))

    def _parse_csv(self, path: Path) -> list[dict]:
        rows: list[dict] = []
        with path.open("r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                rows.append(row)
        return rows

    # ---------- Table rendering ----------
    def _refresh_tables(self):
        self.previous_rows = {"log": [], "manual": [], "motor": []}
        log_rows = self._build_log_rows()
        manual_rows = self._build_csv_rows(self.manual_rows)
        motor_rows = self._build_csv_rows(self.motor_rows)

        union_keys = {row.key for row in log_rows} | {row.key for row in manual_rows} | {row.key for row in motor_rows}
        log_rows = self._ensure_rows(log_rows, union_keys, "log")
        manual_rows = self._ensure_rows(manual_rows, union_keys, "manual")
        motor_rows = self._ensure_rows(motor_rows, union_keys, "motor")

        yellow_keys = self._compute_yellow_keys(log_rows, manual_rows, motor_rows)

        self._render_rows("log", log_rows, yellow_keys)
        self._render_rows("manual", manual_rows, yellow_keys)
        self._render_rows("motor", motor_rows, yellow_keys)

    def _build_log_rows(self) -> list[ShotViewRow]:
        rows: list[ShotViewRow] = []
        for key in sorted(self.log_parser.shots.keys()):
            shot = self.log_parser.shots[key]
            status = "ongoing" if shot.status == "ongoing" else ("complete" if not shot.missing else "missing")
            values = [
                shot.date,
                f"{shot.shot:04d}",
                ", ".join(sorted(shot.expected)) if shot.expected else "",
                ", ".join(sorted(shot.missing)) if shot.missing else "",
                ", ".join(sorted(shot.trigger_cams)),
                status,
            ]
            bg = self._determine_bg("log", values, incomplete=(shot.status == "ongoing"))
            if shot.status == "ongoing":
                bg = "orange"
            rows.append(ShotViewRow(key=key, values=values, bg=bg, yellow_text=bool(shot.missing)))
        return rows

    def _build_csv_rows(self, csv_rows: list[dict]) -> list[ShotViewRow]:
        rows: list[ShotViewRow] = []
        for row in csv_rows:
            date = row.get("Date") or row.get("date") or ""
            shot_str = row.get("Shot") or row.get("shot") or row.get("Index") or ""
            try:
                shot_num = int(shot_str)
                shot_disp = f"{shot_num:04d}"
            except Exception:
                shot_num = 0
                shot_disp = shot_str
            time_val = row.get("Time") or row.get("time") or ""
            other = [f"{k}={v}" for k, v in row.items() if k not in {"Date", "date", "Shot", "shot", "Index", "Time", "time"}]
            values = [date, shot_disp, time_val, "; ".join(other)]
            key = (date, shot_num)
            incomplete = all(not v for v in other)
            bg = self._determine_bg("manual" if csv_rows is self.manual_rows else "motor", values, incomplete=incomplete)
            rows.append(ShotViewRow(key=key, values=values, bg=bg, yellow_text=False))
        rows.sort(key=lambda r: (r.key[0], r.key[1]))
        return rows

    def _ensure_rows(self, rows: list[ShotViewRow], all_keys: set[tuple[str, int]], source: str) -> list[ShotViewRow]:
        existing = {r.key for r in rows}
        for key in all_keys - existing:
            date_str, shot_idx = key
            shot_disp = f"{shot_idx:04d}" if shot_idx else ""
            if source == "log":
                values = [date_str, shot_disp, "", "", "", "incomplete"]
            else:
                values = [date_str, shot_disp, "", ""]
            bg = self._determine_bg(source, values, incomplete=True)
            rows.append(ShotViewRow(key=key, values=values, bg=bg, yellow_text=False))
        rows.sort(key=lambda r: (r.key[0], r.key[1]))
        return rows

    def _determine_bg(self, source: str, values: list[str], incomplete: bool) -> str:
        prev = self.previous_rows.get(source, [])
        bg = "blue"
        if incomplete:
            return "red"
        if prev:
            last_vals = prev[-1]
            bg = "blue" if last_vals == values else "green"
        self.previous_rows[source] = prev + [values]
        return bg

    def _compute_yellow_keys(self, log_rows, manual_rows, motor_rows):
        all_keys = {row.key for row in log_rows} | {row.key for row in manual_rows} | {row.key for row in motor_rows}
        present_log = {row.key for row in log_rows}
        present_manual = {row.key for row in manual_rows}
        present_motor = {row.key for row in motor_rows}
        yellow = set()
        for key in all_keys:
            log_shot = self.log_parser.shots.get(key)
            missing_cam = bool(log_shot and log_shot.missing)
            sources_present = sum([key in present_log, key in present_manual, key in present_motor])
            if sources_present < 3 or missing_cam:
                yellow.add(key)
        return yellow

    def _render_rows(self, source: str, rows: list[ShotViewRow], yellow_keys: set[tuple[str, int]]):
        tree = self.trees[source]
        tree.delete(*tree.get_children())
        for row in rows:
            tags = []
            if row.bg == "green":
                tags.append("bg_green")
            elif row.bg == "blue":
                tags.append("bg_blue")
            elif row.bg == "red":
                tags.append("bg_red")
            elif row.bg == "orange":
                tags.append("bg_orange")
            if row.key in yellow_keys or row.yellow_text:
                tags.append("fg_yellow")
            tree.insert("", "end", values=row.values, tags=tags)

    # ---------- Excel export ----------
    def _export_excel(self):
        if not any([self.log_path, self.manual_path, self.motor_path]):
            messagebox.showerror("Error", "Select at least one source to export")
            return
        xlsx_path = filedialog.asksaveasfilename(
            title="Export to Excel",
            defaultextension=".xlsx",
            filetypes=[("Excel", "*.xlsx"), ("All", "*.*")],
        )
        if not xlsx_path:
            return
        log_rows = self._build_log_rows()
        manual_rows = self._build_csv_rows(self.manual_rows)
        motor_rows = self._build_csv_rows(self.motor_rows)
        yellow_keys = self._compute_yellow_keys(log_rows, manual_rows, motor_rows)

        wb = Workbook()
        sheets = {
            "Logs": (log_rows, ["Date", "Shot", "Expected", "Missing", "Trigger", "Status"]),
            "Manual_Params": (manual_rows, ["Date", "Shot", "Time", "Data"]),
            "Motor_Params": (motor_rows, ["Date", "Shot", "Time", "Data"]),
        }

        for idx, (title, (rows, headers)) in enumerate(sheets.items()):
            ws = wb.active if idx == 0 else wb.create_sheet(title=title)
            ws.title = title
            ws.append(headers)
            for row in rows:
                ws.append(row.values)
                self._apply_excel_styles(ws, ws.max_row, row, yellow_keys)

        try:
            wb.save(xlsx_path)
            messagebox.showinfo("Export", f"Exported to {xlsx_path}")
        except Exception as e:
            messagebox.showerror("Error", f"Failed to save: {e}")

    def _apply_excel_styles(self, ws, row_idx: int, row: ShotViewRow, yellow_keys):
        color_map = {
            "green": "7FFF7F",
            "blue": "7FBFFF",
            "red": "FF7F7F",
            "orange": "FFB347",
        }
        fill_color = color_map.get(row.bg)
        fill = PatternFill(start_color=fill_color, end_color=fill_color, fill_type="solid") if fill_color else None
        font = Font(color="FFFF00") if (row.key in yellow_keys or row.yellow_text) else None
        for cell in ws[row_idx]:
            if fill:
                cell.fill = fill
            if font:
                cell.font = font

    # ---------- Close ----------
    def _on_close(self):
        try:
            if self.observer.is_alive():
                self.observer.stop()
                self.observer.join(timeout=1)
        finally:
            self.root.destroy()


def main():
    root = tk.Tk()
    app = ShotLogReader(root)
    root.mainloop()


if __name__ == "__main__":
    main()
