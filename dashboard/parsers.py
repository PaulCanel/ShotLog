"""Parsing utilities extracted from ``shot_log_reader3.py``.

This module exposes pure functions that transform the legacy parsing
logic into reusable building blocks for the Streamlit dashboard.
"""
from __future__ import annotations

import csv
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Iterable, List, Sequence, Set, Tuple

import streamlit as st

from data_models import (
    CameraSummary,
    CombinedAlignment,
    DisplayRow,
    GlobalSummary,
    ParsedLog,
    ParsedManual,
    ParsedMotor,
    ShotRecord,
)


GREEN_BG = "green"
BLUE_BG = "blue"
RED_BG = "red"
ORANGE_BG = "orange"


def parse_log_file(path: str) -> ParsedLog:
    """Parse the log file and return structured data.

    The implementation mirrors the legacy ``LogShotAnalyzer`` class but
    returns simple dataclasses for easier manipulation in Streamlit.
    """

    log_path = Path(path)
    analyzer = _LogShotAnalyzer()
    shots = analyzer.parse_log_file(log_path)
    shots_table = _build_log_rows(shots)
    global_summary = _compute_global_summary(shots)
    camera_summary = _compute_camera_summary(shots)
    return ParsedLog(
        shots=shots,
        shots_table=shots_table,
        global_summary=global_summary,
        per_camera_summary=camera_summary,
    )


def parse_manual_csv(path: str, log_data: ParsedLog | None = None) -> ParsedManual:
    """Parse the Manual CSV while keeping the same classification rules."""

    header, rows = _parse_csv(Path(path))
    display_rows = _build_csv_rows(header, rows, source="manual")
    return ParsedManual(header=header, rows=display_rows)


def parse_motor_csv(path: str, log_data: ParsedLog | None = None) -> ParsedMotor:
    """Parse the Motor CSV while keeping the same classification rules."""

    header, rows = _parse_csv(Path(path))
    display_rows = _build_csv_rows(header, rows, source="motor")
    return ParsedMotor(header=header, rows=display_rows)


def align_datasets(log_data: ParsedLog, manual: ParsedManual, motor: ParsedMotor) -> CombinedAlignment:
    """Apply yellow key detection and row completion across datasets."""

    log_rows = list(log_data.shots_table)
    manual_rows = list(manual.rows)
    motor_rows = list(motor.rows)

    all_keys = {row.key for row in log_rows} | {row.key for row in manual_rows} | {row.key for row in motor_rows}
    yellow_keys = _compute_yellow_keys(log_rows, manual_rows, motor_rows, all_keys)

    log_rows = _apply_log_backgrounds(log_rows, yellow_keys)
    if manual.header:
        manual_rows = _ensure_rows(manual_rows, all_keys, "manual", manual.header)
    if motor.header:
        motor_rows = _ensure_rows(motor_rows, all_keys, "motor", motor.header)

    return CombinedAlignment(
        log_rows=log_rows,
        manual_rows=manual_rows,
        motor_rows=motor_rows,
        yellow_keys=yellow_keys,
    )


@st.cache_data(show_spinner=False)
def load_log(log_path: str, signature: object) -> ParsedLog:
    return parse_log_file(log_path)


@st.cache_data(show_spinner=False)
def load_manual_csv(manual_path: str, signature: object, log_data: ParsedLog | None = None) -> ParsedManual:
    return parse_manual_csv(manual_path, log_data)


@st.cache_data(show_spinner=False)
def load_motor_csv(motor_path: str, signature: object, log_data: ParsedLog | None = None) -> ParsedMotor:
    return parse_motor_csv(motor_path, log_data)


# ---------------------------------------------------------------------------
# Legacy logic lifted from LogShotAnalyzer
# ---------------------------------------------------------------------------


class _LogShotAnalyzer:
    def __init__(self):
        self.shots: List[ShotRecord] = []
        self.open_shots: dict[tuple[str, int], ShotRecord] = {}
        self.current_expected: set[str] = set()
        self.all_expected_cameras: set[str] = set()

    def parse_log_file(self, path: Path) -> List[ShotRecord]:
        self.shots = []
        self.open_shots = {}
        self.current_expected = set()
        self.all_expected_cameras = set()

        re_updated_expected = re.compile(r"Updated expected cameras \(used diagnostics\): \[(.*)\]")
        re_new_shot = re.compile(
            r"\*\*\* New shot detected: date=(\d{8}), shot=(\d+), camera=([A-Za-z0-9_]+), ref_time=(\d{2}:\d{2}:\d{2}) \*\*\*"
        )
        re_shot_acquired_missing_expected = re.compile(
            r"Shot (\d+) \((\d{8})\) acquired.*?expected=\[(.*)\].*missing cameras: \[(.*)\]"
        )
        re_shot_acquired_missing = re.compile(
            r"Shot (\d+) \((\d{8})\) acquired.*missing cameras: \[(.*)\]"
        )
        re_shot_acquired_ok_expected = re.compile(
            r"Shot (\d+) \((\d{8})\) acquired successfully, expected=\[(.*)\], all cameras present\."
        )
        re_shot_acquired_ok = re.compile(
            r"Shot (\d+) \((\d{8})\) acquired successfully, all cameras present\."
        )
        re_trigger_assigned = re.compile(r"Trigger .* assigned to existing shot (\d+).*camera ([A-Za-z0-9_]+)\)")
        re_clean_copy = re.compile(r"CLEAN copy: .*?-> (.*)")
        re_timing = re.compile(
            r"Shot\s+(\d+)\s+\((\d{8})\)\s+timing:\s+"
            r"trigger_cam=([^,]+),\s*"
            r"trigger_time=([^,]+),\s*"
            r"min_mtime=([^,]+),\s*"
            r"max_mtime=([^,]+),\s*"
            r"first_camera=([^,]+),\s*"
            r"last_camera=([^\s,]+)"
        )

        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.rstrip("\n")

                m = re_updated_expected.search(line)
                if m:
                    cam_list_text = m.group(1)
                    cams = _parse_list_of_names(cam_list_text)
                    self.current_expected = set(cams)
                    self.all_expected_cameras.update(cams)
                    continue

                m = re_new_shot.search(line)
                if m:
                    date_str = m.group(1)
                    shot_idx = int(m.group(2))
                    cam = m.group(3)
                    shot = ShotRecord(
                        date=date_str,
                        shot_number=shot_idx,
                        trigger_cams={cam},
                    )
                    self.shots.append(shot)
                    self.open_shots[(date_str, shot_idx)] = shot
                    continue

                m = re_trigger_assigned.search(line)
                if m:
                    shot_idx = int(m.group(1))
                    cam = m.group(2)
                    shot = self._find_open_shot_by_index(shot_idx)
                    if shot is not None:
                        shot.trigger_cams.add(cam)
                    continue

                m = re_clean_copy.search(line)
                if m:
                    dest_path = m.group(1).strip()
                    filename = os.path.basename(dest_path)
                    name_match = re.match(r"([A-Za-z0-9_]+)_(\d{8})_(\d{6})_shot(\d+)", filename)
                    if name_match:
                        cam = name_match.group(1)
                        date_str = name_match.group(2)
                        time_str = name_match.group(3)
                        shot_idx = int(name_match.group(4))
                        dt = _parse_datetime(date_str, time_str)
                        shot = self._find_open_or_recent_shot(date_str, shot_idx)
                        if shot is not None and dt is not None:
                            shot.image_times.append(dt)
                    continue

                m = re_shot_acquired_missing_expected.search(line)
                if m:
                    shot_idx = int(m.group(1))
                    date_str = m.group(2)
                    expected_text = m.group(3)
                    missing_text = m.group(4)
                    expected_cams = set(_parse_list_of_names(expected_text))
                    missing_cams = set(_parse_list_of_names(missing_text))

                    shot = self._find_open_or_recent_shot(date_str, shot_idx)
                    if shot is not None:
                        shot.expected_cams = expected_cams
                        shot.missing_cams = missing_cams
                        self.all_expected_cameras.update(expected_cams)
                    self.open_shots.pop((date_str, shot_idx), None)
                    continue

                m = re_shot_acquired_missing.search(line)
                if m:
                    shot_idx = int(m.group(1))
                    date_str = m.group(2)
                    missing_text = m.group(3)
                    missing_cams = set(_parse_list_of_names(missing_text))

                    shot = self._find_open_or_recent_shot(date_str, shot_idx)
                    if shot is not None:
                        shot.expected_cams = set(self.current_expected)
                        shot.missing_cams = missing_cams
                    self.open_shots.pop((date_str, shot_idx), None)
                    continue

                m = re_shot_acquired_ok_expected.search(line)
                if m:
                    shot_idx = int(m.group(1))
                    date_str = m.group(2)
                    expected_text = m.group(3)
                    expected_cams = set(_parse_list_of_names(expected_text))
                    shot = self._find_open_or_recent_shot(date_str, shot_idx)
                    if shot is not None:
                        shot.expected_cams = expected_cams
                        shot.missing_cams = set()
                        self.all_expected_cameras.update(expected_cams)
                    self.open_shots.pop((date_str, shot_idx), None)
                    continue

                m = re_shot_acquired_ok.search(line)
                if m:
                    shot_idx = int(m.group(1))
                    date_str = m.group(2)
                    shot = self._find_open_or_recent_shot(date_str, shot_idx)
                    if shot is not None:
                        shot.expected_cams = set(self.current_expected)
                        shot.missing_cams = set()
                    self.open_shots.pop((date_str, shot_idx), None)
                    continue

                m = re_timing.search(line)
                if m:
                    shot_idx = int(m.group(1))
                    date_str = m.group(2)
                    trigger_cam = m.group(3).strip()
                    trigger_time_str = m.group(4).strip()
                    min_time_str = m.group(5).strip()
                    max_time_str = m.group(6).strip()
                    first_cam = m.group(7).strip()
                    last_cam = m.group(8).strip()

                    shot = self._find_open_or_recent_shot(date_str, shot_idx)
                    if shot is not None:
                        shot.trigger_camera = trigger_cam
                        shot.trigger_time = _parse_datetime_full(trigger_time_str)
                        shot.min_time = _parse_datetime_full(min_time_str)
                        shot.max_time = _parse_datetime_full(max_time_str)
                        shot.first_camera = first_cam if first_cam != "N/A" else None
                        shot.last_camera = last_cam if last_cam != "N/A" else None
                    continue

        for shot in self.shots:
            if shot.min_time is None and shot.image_times:
                shot.min_time = min(shot.image_times)
            if shot.max_time is None and shot.image_times:
                shot.max_time = max(shot.image_times)

        return self.shots

    def _find_open_shot_by_index(self, shot_idx: int) -> ShotRecord | None:
        for (d, i), shot in self.open_shots.items():
            if i == shot_idx:
                return shot
        return None

    def _find_open_or_recent_shot(self, date_str: str, shot_idx: int) -> ShotRecord | None:
        key = (date_str, shot_idx)
        if key in self.open_shots:
            return self.open_shots[key]
        for shot in reversed(self.shots):
            if shot.date == date_str and shot.shot_number == shot_idx:
                return shot
        return None


# ---------------------------------------------------------------------------
# Helpers used by log/manual/motor parsing
# ---------------------------------------------------------------------------


def _parse_list_of_names(text: str) -> List[str]:
    if not text.strip():
        return []
    parts = text.split(",")
    names = []
    for p in parts:
        s = p.strip()
        if s.startswith("'") or s.startswith('"'):
            s = s[1:]
        if s.endswith("'") or s.endswith('"'):
            s = s[:-1]
        if s:
            names.append(s)
    return names


def _parse_datetime(date_str: str, time_str: str) -> datetime | None:
    try:
        return datetime.strptime(date_str + time_str, "%Y%m%d%H%M%S")
    except Exception:
        return None


def _parse_datetime_full(dt_str: str | None) -> datetime | None:
    if dt_str is None or dt_str == "N/A":
        return None
    try:
        return datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def _build_log_rows(shots: Sequence[ShotRecord]) -> List[DisplayRow]:
    rows: List[DisplayRow] = []
    for shot in sorted(shots, key=lambda s: (s.date, s.shot_number)):
        key = _make_key(shot.shot_number, _format_time(shot.trigger_time))
        rows.append(
            DisplayRow(
                key=key,
                values=[shot.date, f"{shot.shot_number:04d}"],
                bg=RED_BG if shot.missing_cams else GREEN_BG,
                yellow_text=False,
                incomplete=bool(shot.missing_cams),
            )
        )
    return rows


def _compute_global_summary(shots: Sequence[ShotRecord]) -> GlobalSummary:
    if not shots:
        return GlobalSummary(dates=[], start_time=None, end_time=None, total_shots=0, shots_with_missing=0)

    dates = sorted({s.date for s in shots})
    all_starts = [s.min_time for s in shots if s.min_time is not None]
    all_ends = [s.max_time for s in shots if s.max_time is not None]
    start_time = min(all_starts) if all_starts else None
    end_time = max(all_ends) if all_ends else None
    total_shots = len(shots)
    shots_with_missing = sum(1 for s in shots if s.missing_cams)
    return GlobalSummary(
        dates=dates,
        start_time=start_time,
        end_time=end_time,
        total_shots=total_shots,
        shots_with_missing=shots_with_missing,
    )


def _compute_camera_summary(shots: Sequence[ShotRecord]) -> List[CameraSummary]:
    cams = sorted({c for s in shots for c in s.expected_cams})
    summary = []
    for cam in cams:
        used_count = sum(1 for s in shots if cam in s.expected_cams)
        missing_count = sum(1 for s in shots if cam in s.missing_cams)
        summary.append(CameraSummary(camera=cam, shots_used=used_count, shots_missing=missing_count))
    return summary


def _parse_csv(path: Path) -> tuple[list[str], list[list[str]]]:
    header: list[str] = []
    rows: list[list[str]] = []
    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.reader(f, delimiter=",", quotechar='"')
        header = next(reader, [])
        for row in reader:
            rows.append(row)
    return header, rows


def _build_csv_rows(header: list[str], csv_rows: list[list[str]], source: str) -> List[DisplayRow]:
    rows: list[DisplayRow] = []
    if not header:
        return rows

    for csv_row in csv_rows:
        original_len = len(csv_row)
        values = csv_row + [""] * (len(header) - len(csv_row))
        key = _extract_key_from_header(header, values)
        incomplete = _is_csv_row_incomplete(header, values, original_len, source)
        rows.append(
            DisplayRow(
                key=key,
                values=values,
                bg=RED_BG if incomplete else BLUE_BG,
                yellow_text=False,
                incomplete=incomplete,
            )
        )
    rows.sort(key=lambda r: (r.key[0], r.key[1]))
    prev_values: list[str] | None = None
    for row in rows:
        row.bg = _determine_generic_bg(prev_values, row.values, row.incomplete)
        prev_values = row.values
    return rows


def _ensure_rows(
    rows: List[DisplayRow],
    all_keys: Set[tuple[int, str]],
    source: str,
    header: list[str] | None = None,
) -> List[DisplayRow]:
    existing = {r.key for r in rows}
    for key in all_keys - existing:
        shot_idx, trigger_time = key
        shot_disp = f"{shot_idx:04d}" if shot_idx and shot_idx > 0 else ""
        cols = header or []
        values = [""] * len(cols)
        if cols:
            shot_col = _find_header_index(cols, {"shot", "shot_number", "shot_", "shot__", "index", "shot#"})
            time_col = _find_header_index(cols, {"trigger_time", "time", "trigger_time_"})
            if shot_col is not None and shot_col < len(values):
                values[shot_col] = shot_disp
            if time_col is not None and time_col < len(values):
                values[time_col] = trigger_time
        bg = RED_BG
        rows.append(DisplayRow(key=key, values=values, bg=bg, yellow_text=False, incomplete=True))
    rows.sort(key=lambda r: (r.key[0], r.key[1]))
    return rows


def _determine_generic_bg(prev_values: list[str] | None, values: list[str], incomplete: bool) -> str:
    if incomplete:
        return RED_BG
    if prev_values is None or prev_values == values:
        return BLUE_BG
    return GREEN_BG


def _compute_yellow_keys(
    log_rows: List[DisplayRow],
    manual_rows: List[DisplayRow],
    motor_rows: List[DisplayRow],
    all_keys: Set[tuple[int, str]],
) -> set[tuple[int, str]]:
    yellow = set()

    def build_counts(rows: List[DisplayRow]):
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


def _apply_log_backgrounds(log_rows: List[DisplayRow], yellow_keys: Set[tuple[int, str]]):
    prev_values: list[str] | None = None
    for row in log_rows:
        if row.incomplete:
            row.bg = RED_BG
        else:
            csv_ok = row.key not in yellow_keys
            if csv_ok and (prev_values is None or prev_values == row.values):
                row.bg = BLUE_BG
            else:
                row.bg = GREEN_BG
        prev_values = row.values
    return log_rows


def _make_key(shot_num: int | None, trigger_time: str | None) -> tuple[int, str]:
    try:
        shot_idx = int(shot_num) if shot_num is not None else -1
    except Exception:
        shot_idx = -1
    trigger_time = trigger_time or ""
    return shot_idx, trigger_time


def _extract_key_from_header(header: list[str], values: list[str]) -> tuple[int, str]:
    shot_idx = _find_header_index(header, {"shot", "shot_number", "shot_", "shot__", "index", "shot#"})
    time_idx = _find_header_index(header, {"trigger_time", "time", "trigger_time_"})
    shot_val = values[shot_idx] if shot_idx is not None and shot_idx < len(values) else ""
    trigger_time = values[time_idx] if time_idx is not None and time_idx < len(values) else ""
    return _make_key(shot_val if shot_val != "" else -1, trigger_time)


def _is_csv_row_incomplete(header: list[str], values: list[str], original_len: int, source: str) -> bool:
    shot_idx = _find_header_index(header, {"shot", "shot_number", "shot_", "shot__", "index", "shot#"})
    time_idx = _find_header_index(header, {"trigger_time", "time", "trigger_time_"})
    shot_val = values[shot_idx].strip() if shot_idx is not None and shot_idx < len(values) else ""
    time_val = values[time_idx].strip() if time_idx is not None and time_idx < len(values) else ""
    shot_valid = _parse_int_or_none(shot_val) is not None
    missing_fields = original_len < len(header)
    if source == "manual":
        return missing_fields or not shot_valid or time_val == ""

    other_values = [
        values[i].strip()
        for i in range(len(header))
        if i < len(values) and i not in {shot_idx, time_idx}
    ]
    empty_other = all(v == "" for v in other_values)
    return missing_fields or shot_val == "" or time_val == "" or empty_other


def _normalize_header(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.strip().lower())


def _find_header_index(header: list[str], names: set[str]) -> int | None:
    normalized = [_normalize_header(h) for h in header]
    for i, name in enumerate(normalized):
        if name in names:
            return i
    return None


def _parse_int_or_none(value: str) -> int | None:
    try:
        return int(value)
    except Exception:
        return None


def _parse_float_or_none(value: str) -> float | None:
    try:
        return float(value)
    except Exception:
        return None


def parse_time_to_seconds(value: str) -> float | None:
    try:
        t = datetime.strptime(value.strip(), "%H:%M:%S")
        return float(t.hour * 3600 + t.minute * 60 + t.second)
    except Exception:
        return None


def collect_series(header: list[str], rows: List[DisplayRow], value_idx: int):
    shot_idx = _find_header_index(header, {"shot", "shot_number", "shot_", "shot__", "index", "shot#"})
    normalized_value = _normalize_header(header[value_idx]) if value_idx < len(header) else ""
    is_time = normalized_value in {"trigger_time", "time", "trigger_time_"}

    x_values: list[int] = []
    y_values: list[float | None] = []
    for row in rows:
        shot_val = None
        if shot_idx is not None and shot_idx < len(row.values):
            shot_val = _parse_int_or_none(row.values[shot_idx].strip())
        if shot_val is None or shot_val < 0:
            shot_val = row.key[0]
        if shot_val is None or shot_val < 0:
            continue

        raw_value = row.values[value_idx] if value_idx < len(row.values) else ""
        if is_time:
            parsed_value = parse_time_to_seconds(raw_value) if raw_value else None
        else:
            parsed_value = _parse_float_or_none(raw_value) if raw_value != "" else None

        x_values.append(shot_val)
        y_values.append(parsed_value)

    return x_values, y_values, is_time


def _format_time(dt: datetime | None) -> str:
    return dt.strftime("%H:%M:%S") if dt else ""


__all__ = [
    "parse_log_file",
    "parse_manual_csv",
    "parse_motor_csv",
    "align_datasets",
    "collect_series",
    "parse_time_to_seconds",
]
