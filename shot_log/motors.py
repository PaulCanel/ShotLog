"""Utilities for parsing motor position CSV files and computing motor states."""
from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence

LoggerFn = Callable[[str, str], None]


def _log_message(logger: LoggerFn | None, level: str, message: str) -> None:
    if logger:
        logger(level, message)


@dataclass
class MotorEvent:
    """Represents a motor movement event parsed from the history CSV."""

    time: datetime
    motor: str  # Motor name (not the axis identifier)
    old_pos: float | None
    new_pos: float | None
    time_only: bool = False


class MotorStateManager:
    """Compute motor positions at arbitrary timestamps based on initial values and events."""

    def __init__(self, initial_positions: Dict[str, float], events: Sequence[MotorEvent]):
        self.initial_positions = dict(initial_positions)
        self.events: List[MotorEvent] = sorted(events, key=lambda e: e.time)
        self.motor_names = set(initial_positions.keys()) | {evt.motor for evt in self.events}

    def get_positions_at(self, t: datetime) -> Dict[str, float | None]:
        """Return motor positions at time ``t``.

        The computation replays events in chronological order up to ``t`` starting
        from the provided ``initial_positions``. The method is deliberately simple
        for correctness; performance optimisations can be added later if required.
        """

        positions: Dict[str, float | None] = {k: v for k, v in self.initial_positions.items()}
        for event in self.events:
            event_time = event.time
            if event.time_only:
                event_time = event_time.replace(year=t.year, month=t.month, day=t.day)
            if event_time > t:
                break
            if event.motor not in positions and event.motor not in self.motor_names:
                self.motor_names.add(event.motor)
            if event.new_pos is not None:
                positions[event.motor] = event.new_pos
            elif event.old_pos is not None:
                positions[event.motor] = event.old_pos
            else:
                positions.setdefault(event.motor, None)
        # Ensure every known motor has a key so the CSV writer can emit columns
        for motor in self.motor_names:
            positions.setdefault(motor, None)
        return positions


def _detect_dialect(path: Path) -> csv.Dialect:
    sample = path.read_text(encoding="utf-8", errors="ignore")
    try:
        return csv.Sniffer().sniff(sample, delimiters=[",", ";", "\t"])  # type: ignore[arg-type]
    except Exception:
        return csv.get_dialect("excel")


def _pick_column(header: Iterable[str], candidates: Iterable[str]) -> Optional[str]:
    lower_header = {col.lower(): col for col in header}
    for cand in candidates:
        if cand in lower_header:
            return lower_header[cand]
    for cand in candidates:
        for name_lower, original in lower_header.items():
            if cand in name_lower:
                return original
    return None


def _parse_float(value: str) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_datetime(value: str, *, fallback_date: date | None = None) -> tuple[Optional[datetime], bool]:
    value = value.strip()
    if not value:
        return None, False

    # Handle time-only inputs (HH:MM:SS) by anchoring them to the fallback date (or today)
    if re.fullmatch(r"\d{1,2}:\d{2}:\d{2}", value):
        base_date = fallback_date or date.today()
        try:
            dt = datetime.strptime(value, "%H:%M:%S").replace(
                year=base_date.year,
                month=base_date.month,
                day=base_date.day,
            )
            return dt, True
        except Exception:
            pass

    parsers = [
        datetime.fromisoformat,
        lambda v: datetime.strptime(v, "%Y-%m-%d %H:%M:%S"),
        lambda v: datetime.strptime(v, "%Y/%m/%d %H:%M:%S"),
        lambda v: datetime.strptime(v, "%d/%m/%Y %H:%M:%S"),
    ]
    if fallback_date is not None:
        parsers.append(
            lambda v: datetime.strptime(v, "%H:%M:%S").replace(
                year=fallback_date.year,
                month=fallback_date.month,
                day=fallback_date.day,
            )
        )
    for parser in parsers:
        try:
            return parser(value), False
        except Exception:
            continue
    return None, False


def parse_initial_positions(path: Path, logger: LoggerFn | None = None) -> tuple[Dict[str, float], Dict[str, str]]:
    """Parse a CSV containing initial motor positions.

    The parser attempts to find both the motor name column (``name`` / ``motor``)
    and the axis column (``axis``) so that a stable mapping ``axis -> motor`` can
    be constructed. Lines with missing or invalid data are skipped with a warning.

    Returns a tuple ``(initial_positions, axis_to_motor)`` where ``initial_positions``
    is ``{motor_name: position}``.
    """

    if not path.exists():
        raise FileNotFoundError(f"Initial positions CSV not found: {path}")

    dialect = _detect_dialect(path)
    positions: Dict[str, float] = {}
    axis_to_motor: Dict[str, str] = {}

    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f, dialect=dialect)
        if not reader.fieldnames:
            raise ValueError("Initial positions CSV has no header")
        motor_col = _pick_column(reader.fieldnames, ["motor", "name", "motor_name"])
        axis_col = _pick_column(reader.fieldnames, ["axis", "axis_name"])
        pos_col = _pick_column(reader.fieldnames, ["position", "pos", "value"])
        if motor_col is None or pos_col is None:
            raise ValueError(
                "Could not find motor/position columns in initial positions CSV."
            )
        if axis_col is None:
            _log_message(
                logger,
                "WARNING",
                "Initial positions CSV is missing an axis column; motor history will not be mapped to names.",
            )
        for row_idx, row in enumerate(reader, start=2):
            motor = (row.get(motor_col) or "").strip()
            if not motor:
                _log_message(logger, "WARNING", f"Skipping row {row_idx}: missing motor name")
                continue
            axis = (row.get(axis_col) or "").strip() if axis_col else ""
            pos = _parse_float(row.get(pos_col, ""))
            if pos is None:
                _log_message(
                    logger,
                    "WARNING",
                    f"Skipping row {row_idx}: invalid position for motor {motor}",
                )
                continue
            positions[motor] = pos
            if axis:
                existing = axis_to_motor.get(axis)
                if existing and existing != motor:
                    _log_message(
                        logger,
                        "WARNING",
                        f"Axis '{axis}' already mapped to motor '{existing}', ignoring duplicate motor '{motor}'",
                    )
                else:
                    axis_to_motor[axis] = motor
    return positions, axis_to_motor


def parse_motor_history(
    path: Path,
    logger: LoggerFn | None = None,
    *,
    axis_to_motor: Dict[str, str] | None = None,
    fallback_date: date | None = None,
) -> List[MotorEvent]:
    """Parse the motor movement history CSV.

    The parser searches for time, motor, old position and new position columns.
    Rows without a usable timestamp or motor are skipped with a warning. Time-only
    values (``HH:MM:SS``) are anchored to ``fallback_date`` (or today) and marked
    so they can be projected onto the shot date when computing positions.
    """

    if not path.exists():
        raise FileNotFoundError(f"Motor history CSV not found: {path}")

    dialect = _detect_dialect(path)
    events: List[MotorEvent] = []
    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f, dialect=dialect)
        if not reader.fieldnames:
            raise ValueError("Motor history CSV has no header")
        motor_col = _pick_column(reader.fieldnames, ["motor", "name", "axis"])
        time_col = _pick_column(reader.fieldnames, ["time", "timestamp", "date"])
        new_col = _pick_column(reader.fieldnames, ["new", "position", "pos", "value", "new_pos"])
        old_col = _pick_column(reader.fieldnames, ["old", "previous", "old_pos", "from"])
        if motor_col is None or time_col is None or new_col is None:
            raise ValueError(
                "Could not find required columns (time, motor, new position) in motor history CSV."
            )
        for row_idx, row in enumerate(reader, start=2):
            axis_or_motor = (row.get(motor_col) or "").strip()
            raw_time = (row.get(time_col) or "").strip()
            if not axis_or_motor or not raw_time:
                _log_message(
                    logger,
                    "WARNING",
                    f"Skipping row {row_idx}: missing motor or timestamp",
                )
                continue
            dt, time_only = _parse_datetime(raw_time, fallback_date=fallback_date)
            if dt is None:
                _log_message(
                    logger,
                    "WARNING",
                    f"Skipping row {row_idx}: could not parse timestamp '{raw_time}'",
                )
                continue
            motor_name = axis_or_motor
            if axis_to_motor is not None:
                motor_name = axis_to_motor.get(axis_or_motor, "")
                if not motor_name:
                    _log_message(
                        logger,
                        "WARNING",
                        f"Skipping row {row_idx}: unknown axis '{axis_or_motor}'",
                    )
                    continue

            old_pos = _parse_float(row.get(old_col, "")) if old_col else None
            new_pos = _parse_float(row.get(new_col, ""))
            events.append(
                MotorEvent(
                    time=dt,
                    motor=motor_name,
                    old_pos=old_pos,
                    new_pos=new_pos,
                    time_only=time_only,
                )
            )
    events.sort(key=lambda e: e.time)
    return events


__all__ = [
    "MotorEvent",
    "MotorStateManager",
    "parse_initial_positions",
    "parse_motor_history",
]
