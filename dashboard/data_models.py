"""Dataclasses used by the Streamlit dashboard."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, List, Optional, Set, Tuple


@dataclass
class ShotRecord:
    """A single shot as extracted from the log file."""

    date: str
    shot_number: int
    expected_cams: Set[str] = field(default_factory=set)
    missing_cams: Set[str] = field(default_factory=set)
    trigger_cams: Set[str] = field(default_factory=set)
    trigger_camera: Optional[str] = None
    trigger_time: Optional[datetime] = None
    min_time: Optional[datetime] = None
    max_time: Optional[datetime] = None
    first_camera: Optional[str] = None
    last_camera: Optional[str] = None
    image_times: List[datetime] = field(default_factory=list)

    def missing_count(self) -> int:
        return len(self.missing_cams)

    def expected_count(self) -> int:
        return len(self.expected_cams)


@dataclass
class GlobalSummary:
    """Aggregated information about the parsed log."""

    dates: List[str]
    start_time: Optional[datetime]
    end_time: Optional[datetime]
    total_shots: int
    shots_with_missing: int

    @property
    def ok_shots(self) -> int:
        return self.total_shots - self.shots_with_missing

    @property
    def ok_ratio(self) -> float:
        if self.total_shots == 0:
            return 0.0
        return self.ok_shots / self.total_shots


@dataclass
class CameraSummary:
    camera: str
    shots_used: int
    shots_missing: int

    @property
    def missing_ratio(self) -> float:
        if self.shots_used == 0:
            return 0.0
        return self.shots_missing / self.shots_used


@dataclass
class DisplayRow:
    """Row prepared for display or export with color metadata."""

    key: Tuple[int, str]
    values: List[str]
    bg: str
    yellow_text: bool = False
    incomplete: bool = False


@dataclass
class ParsedLog:
    """Container returned by :func:`parse_log_file`."""

    shots: List[ShotRecord]
    shots_table: List[DisplayRow]
    global_summary: GlobalSummary
    per_camera_summary: List[CameraSummary]

    @property
    def all_keys(self) -> Set[Tuple[int, str]]:
        return {row.key for row in self.shots_table}


@dataclass
class ParsedCsv:
    """Generic CSV parsing result (Manual or Motor)."""

    header: List[str]
    rows: List[DisplayRow]

    @property
    def keys(self) -> Set[Tuple[int, str]]:
        return {row.key for row in self.rows}

    @property
    def incomplete_rows(self) -> int:
        return sum(1 for r in self.rows if r.incomplete)

    @property
    def complete_rows(self) -> int:
        return len(self.rows) - self.incomplete_rows


@dataclass
class ParsedManual(ParsedCsv):
    pass


@dataclass
class ParsedMotor(ParsedCsv):
    pass


@dataclass
class CombinedAlignment:
    """Alignment information between log, manual and motor tables."""

    log_rows: List[DisplayRow]
    manual_rows: List[DisplayRow]
    motor_rows: List[DisplayRow]
    yellow_keys: Set[Tuple[int, str]]

    @property
    def suspect_rows(self) -> int:
        return len(self.yellow_keys)


@dataclass
class Diagnostics:
    """Diagnostic metadata shown in the dashboard."""

    log_path: str
    manual_path: str
    motor_path: str
    last_parsed_at: datetime
    warnings: List[str] = field(default_factory=list)

    def warning_count(self) -> int:
        return len(self.warnings)


__all__ = [
    "ShotRecord",
    "GlobalSummary",
    "CameraSummary",
    "DisplayRow",
    "ParsedLog",
    "ParsedManual",
    "ParsedMotor",
    "CombinedAlignment",
    "Diagnostics",
]
