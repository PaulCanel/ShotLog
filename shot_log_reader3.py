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


GREEN_BG = "#006400"  # DarkGreen
BLUE_BG = "#00008B"  # DarkBlue
RED_BG = "#8B0000"  # DarkRed
ORANGE_BG = "#FF8C00"  # DarkOrange
TEXT_DEFAULT = "white"
TEXT_WARNING = "#FFFF00"


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
    key: tuple[int, str]
    values: list[str]
    bg: str
    yellow_text: bool
    incomplete: bool = False


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
        self.manual_rows: list[list[str]] = []
        self.motor_rows: list[list[str]] = []
        self.manual_header: list[str] = []
        self.motor_header: list[str] = []

        self.observer = Observer()
        self.watch_schedules = []
        self._handlers: list[TargetWatcher] = []

        self.previous_rows = {
            "log": [],
            "manual": [],
            "motor": [],
        }
        self.shot_lookup: dict[tuple[int, str], LogShot] = {}

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
            ("manual", "Manual Params", []),
            ("motor", "Motor Params", []),
        ]:
            frame = ttk.Frame(self.notebook)
            self.notebook.add(frame, text=title)
            tree = ttk.Treeview(frame, columns=columns, show="headings")
            if columns:
                for col in columns:
                    tree.heading(col, text=col)
                    tree.column(col, width=140, anchor="center")
            tree.pack(fill="both", expand=True)
            tree.tag_configure("bg_green", background=GREEN_BG, foreground=TEXT_DEFAULT)
            tree.tag_configure("bg_blue", background=BLUE_BG, foreground=TEXT_DEFAULT)
            tree.tag_configure("bg_red", background=RED_BG, foreground=TEXT_DEFAULT)
            tree.tag_configure("bg_orange", background=ORANGE_BG, foreground=TEXT_DEFAULT)
            tree.tag_configure("fg_yellow", foreground=TEXT_WARNING)
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
                self.manual_header, self.manual_rows = self._parse_csv(self.manual_path)
                self._configure_csv_tree("manual", self.manual_header)
            if self.motor_path:
                self.motor_header, self.motor_rows = self._parse_csv(self.motor_path)
                self._configure_csv_tree("motor", self.motor_header)
            self._refresh_tables()
        except Exception as e:
            messagebox.showerror("Error", str(e))

    def _parse_csv(self, path: Path) -> tuple[list[str], list[list[str]]]:
        header: list[str] = []
        rows: list[list[str]] = []
        with path.open("r", encoding="utf-8", newline="") as f:
            reader = csv.reader(f, delimiter=",", quotechar='"')
            header = next(reader, [])
            for row in reader:
                rows.append(row)
        return header, rows

    def _configure_csv_tree(self, source: str, header: list[str]):
        tree = self.trees[source]
        tree.delete(*tree.get_children())
        tree["columns"] = header
        tree["show"] = "headings"
        for col in header:
            tree.heading(col, text=col)
            tree.column(col, width=140, anchor="center")

    # ---------- Table rendering ----------
    def _refresh_tables(self):
        self.previous_rows = {"log": [], "manual": [], "motor": []}
        log_rows = self._build_log_rows()
        manual_rows = self._build_csv_rows(self.manual_header, self.manual_rows, "manual")
        motor_rows = self._build_csv_rows(self.motor_header, self.motor_rows, "motor")

        all_keys = {row.key for row in log_rows} | {row.key for row in manual_rows} | {row.key for row in motor_rows}
        yellow_keys = self._compute_yellow_keys(log_rows, manual_rows, motor_rows, all_keys)
        log_rows = self._apply_log_backgrounds(log_rows, yellow_keys)

        log_rows = self._ensure_rows(log_rows, all_keys, "log")
        manual_rows = self._ensure_rows(manual_rows, all_keys, "manual", header=self.manual_header)
        motor_rows = self._ensure_rows(motor_rows, all_keys, "motor", header=self.motor_header)

        self._render_rows("log", log_rows, yellow_keys)
        self._render_rows("manual", manual_rows, yellow_keys)
        self._render_rows("motor", motor_rows, yellow_keys)

    def _build_log_rows(self) -> list[ShotViewRow]:
        rows: list[ShotViewRow] = []
        self.shot_lookup = {}
        shots = sorted(self.log_parser.shots.values(), key=lambda s: (s.date, s.shot))
        for shot in shots:
            status = "ongoing" if shot.status == "ongoing" else ("complete" if not shot.missing else "missing")
            values = [
                shot.date,
                f"{shot.shot:04d}",
                ", ".join(sorted(shot.expected)) if shot.expected else "",
                ", ".join(sorted(shot.missing)) if shot.missing else "",
                ", ".join(sorted(shot.trigger_cams)),
                status,
            ]
            key = self._make_key(shot.shot, shot.trigger_time or shot.date)
            self.shot_lookup[key] = shot
            rows.append(
                ShotViewRow(
                    key=key,
                    values=values,
                    bg="red" if shot.missing else "green",
                    yellow_text=False,
                    incomplete=shot.status == "ongoing" or bool(shot.missing),
                )
            )
        return rows

    def _build_csv_rows(self, header: list[str], csv_rows: list[list[str]], source: str) -> list[ShotViewRow]:
        rows: list[ShotViewRow] = []
        if not header:
            return rows

        for csv_row in csv_rows:
            values = csv_row + [""] * (len(header) - len(csv_row))
            key = self._extract_key_from_header(header, values)
            incomplete = self._is_csv_row_incomplete(header, values)
            rows.append(
                ShotViewRow(
                    key=key,
                    values=values,
                    bg="red" if incomplete else "blue",
                    yellow_text=False,
                    incomplete=incomplete,
                )
            )
        rows.sort(key=lambda r: (r.key[0], r.key[1]))
        prev_values: list[str] | None = None
        for row in rows:
            row.bg = self._determine_generic_bg(prev_values, row.values, row.incomplete)
            prev_values = row.values
        return rows

    def _ensure_rows(
        self,
        rows: list[ShotViewRow],
        all_keys: set[tuple[int, str]],
        source: str,
        header: list[str] | None = None,
    ) -> list[ShotViewRow]:
        existing = {r.key for r in rows}
        for key in all_keys - existing:
            shot_idx, trigger_time = key
            shot_disp = f"{shot_idx:04d}" if shot_idx else ""
            if source == "log":
                values = ["", shot_disp, "", "", "", "incomplete"]
            else:
                cols = header or []
                values = [""] * len(cols)
            bg = "red"
            rows.append(ShotViewRow(key=key, values=values, bg=bg, yellow_text=False, incomplete=True))
        rows.sort(key=lambda r: (r.key[0], r.key[1]))
        return rows

    def _determine_generic_bg(self, prev_values: list[str] | None, values: list[str], incomplete: bool) -> str:
        if incomplete:
            return "red"
        if prev_values is None or prev_values == values:
            return "blue"
        return "green"

    def _compute_yellow_keys(self, log_rows, manual_rows, motor_rows, all_keys):
        yellow = set()

        def build_counts(rows):
            counts = {}
            incomplete_keys = set()
            for r in rows:
                counts[r.key] = counts.get(r.key, 0) + 1
                if r.incomplete:
                    incomplete_keys.add(r.key)
            return counts, incomplete_keys

        manual_counts, manual_incomplete = build_counts(manual_rows)
        motor_counts, motor_incomplete = build_counts(motor_rows)

        for key in all_keys:
            manual_issue = manual_counts.get(key, 0) != 1 or key in manual_incomplete
            motor_issue = motor_counts.get(key, 0) != 1 or key in motor_incomplete
            if manual_issue or motor_issue:
                yellow.add(key)
        return yellow

    def _apply_log_backgrounds(self, log_rows: list[ShotViewRow], yellow_keys: set[tuple[int, str]]):
        prev_values: list[str] | None = None
        for row in log_rows:
            shot = self.shot_lookup.get(row.key)
            if not shot:
                row.bg = "red"
                row.incomplete = True
                continue

            if shot.status == "ongoing":
                row.bg = "orange"
                row.incomplete = True
                prev_values = row.values
                continue

            if shot.missing:
                row.bg = "red"
                row.incomplete = True
            else:
                csv_ok = row.key not in yellow_keys
                if csv_ok and (prev_values is None or prev_values == row.values):
                    row.bg = "blue"
                else:
                    row.bg = "green"
                row.incomplete = False
            prev_values = row.values
        return log_rows

    @staticmethod
    def _make_key(shot_num: int | None, trigger_time: str | None) -> tuple[int, str]:
        try:
            shot_idx = int(shot_num) if shot_num is not None else -1
        except Exception:
            shot_idx = -1
        return shot_idx, trigger_time or ""

    def _extract_key_from_header(self, header: list[str], values: list[str]) -> tuple[int, str]:
        header_lower = [h.lower() for h in header]
        shot_idx = next((i for i, h in enumerate(header_lower) if h in {"shot_number", "shot", "index"}), None)
        time_idx = next((i for i, h in enumerate(header_lower) if h in {"trigger_time", "time"}), None)
        shot_val = values[shot_idx] if shot_idx is not None and shot_idx < len(values) else ""
        trigger_time = values[time_idx] if time_idx is not None and time_idx < len(values) else ""
        return self._make_key(shot_val if shot_val != "" else -1, trigger_time)

    def _is_csv_row_incomplete(self, header: list[str], values: list[str]) -> bool:
        header_lower = [h.lower() for h in header]
        shot_idx = next((i for i, h in enumerate(header_lower) if h in {"shot_number", "shot", "index"}), None)
        time_idx = next((i for i, h in enumerate(header_lower) if h in {"trigger_time", "time"}), None)
        shot_val = values[shot_idx].strip() if shot_idx is not None and shot_idx < len(values) else ""
        time_val = values[time_idx].strip() if time_idx is not None and time_idx < len(values) else ""
        other_values = [
            values[i].strip()
            for i in range(len(header))
            if i < len(values) and i not in {shot_idx, time_idx}
        ]
        missing_fields = len(values) < len(header)
        empty_other = all(v == "" for v in other_values)
        return missing_fields or shot_val == "" or time_val == "" or empty_other

    def _render_rows(self, source: str, rows: list[ShotViewRow], yellow_keys: set[tuple[int, str]]):
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
        manual_rows = self._build_csv_rows(self.manual_header, self.manual_rows, "manual")
        motor_rows = self._build_csv_rows(self.motor_header, self.motor_rows, "motor")
        all_keys = {row.key for row in log_rows} | {row.key for row in manual_rows} | {row.key for row in motor_rows}
        yellow_keys = self._compute_yellow_keys(log_rows, manual_rows, motor_rows, all_keys)
        log_rows = self._apply_log_backgrounds(log_rows, yellow_keys)

        wb = Workbook()
        sheets = {
            "Logs": (log_rows, ["Date", "Shot", "Expected", "Missing", "Trigger", "Status"]),
            "Manual_Params": (manual_rows, self.manual_header),
            "Motor_Params": (motor_rows, self.motor_header),
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
            "green": GREEN_BG.lstrip("#"),
            "blue": BLUE_BG.lstrip("#"),
            "red": RED_BG.lstrip("#"),
            "orange": ORANGE_BG.lstrip("#"),
        }
        fill_color = color_map.get(row.bg)
        fill = PatternFill(start_color=fill_color, end_color=fill_color, fill_type="solid") if fill_color else None
        font_color = TEXT_WARNING.lstrip("#") if (row.key in yellow_keys or row.yellow_text) else TEXT_DEFAULT
        font = Font(color=font_color)
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
