"""Helpers for manual parameter management."""
from __future__ import annotations

import csv
import io
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Sequence

from .config import ManualParam
from .utils import ensure_dir

logger = logging.getLogger(__name__)


def _param_name_list(params: Iterable[ManualParam | str]) -> list[str]:
    names: list[str] = []
    for p in params:
        name = p.name if isinstance(p, ManualParam) else str(p)
        name = name.strip()
        if name:
            names.append(name)
    return names


def build_empty_manual_values(params: Iterable[ManualParam | str]) -> dict[str, str]:
    return {name: "" for name in _param_name_list(params)}


def _format_trigger_time(trigger_time: datetime | str | None) -> str:
    if isinstance(trigger_time, datetime):
        return trigger_time.strftime("%H:%M:%S")
    if isinstance(trigger_time, str):
        txt = trigger_time.strip()
        if not txt:
            return ""
        try:
            return datetime.fromisoformat(txt).strftime("%H:%M:%S")
        except ValueError:
            pass
        if " " in txt:
            txt = txt.split(" ")[-1]
        if "T" in txt:
            txt = txt.split("T")[-1]
        if re.match(r"^\d{2}:\d{2}:\d{2}", txt):
            return txt[:8]
        return txt
    return ""


def _quote_text(value: str) -> str:
    if value == "":
        return ""
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=",", quoting=csv.QUOTE_MINIMAL)
    writer.writerow([value])
    field = buf.getvalue().strip("\r\n")
    if not field.startswith('"'):
        field = f'"{field}"'
    return field


def _normalize_value(raw: str | None) -> str:
    if raw is None:
        return ""
    val = str(raw).strip()
    return "" if val == "-" else val


def write_manual_params_row(
    output_path: Path,
    manual_params: Sequence[ManualParam],
    shot_index: int,
    trigger_time: datetime | str | None,
    values: dict[str, str],
):
    output_path = output_path.with_suffix(".csv")
    ensure_dir(output_path.parent)

    param_names = [p.name for p in manual_params]
    headers = ["shot", "trigger_time"] + param_names
    row_values: list[str] = []

    row_values.append(str(shot_index))
    row_values.append(_format_trigger_time(trigger_time))

    for param in manual_params:
        name = param.name
        ptype = param.type.lower() if param.type else "text"
        raw_val = _normalize_value(values.get(name))

        if raw_val == "":
            row_values.append("")
            continue

        if ptype == "number":
            try:
                float(raw_val)
            except Exception:
                logger.warning("Invalid number for manual parameter '%s': %s", name, raw_val)
                row_values.append(raw_val)
            else:
                row_values.append(raw_val)
        else:
            row_values.append(_quote_text(raw_val))

    file_exists = output_path.exists()
    if not file_exists:
        header_line = ",".join(headers)
        output_path.write_text(header_line + "\n", encoding="utf-8")
    with output_path.open("a", encoding="utf-8") as f:
        f.write(",".join(row_values) + "\n")


class ManualParamsManager:
    """State machine to track manual parameters per shot.

    A pending row is only written when a new shot starts or when the user presses
    Stop. Confirmed values are never discarded without being written.
    """

    def __init__(
        self,
        manual_params: Sequence[ManualParam],
        csv_path_provider: Callable[[], Path | None],
        log_fn: Callable[[str], None] | None = None,
    ):
        self.manual_params: list[ManualParam] = list(manual_params)
        self.param_names: list[str] = [p.name for p in self.manual_params]
        self._csv_path_provider = csv_path_provider
        self.log_fn = log_fn or (lambda msg: logger.info(msg))

        self.current_date_str: str | None = None
        self.current_shot_index: int | None = None
        self.current_trigger_time_str: str | None = None
        self.current_confirmed_values: list[str] = ["" for _ in self.param_names]
        self._current_has_confirm: bool = False

        self.pending_date_str: str | None = None
        self.pending_shot_index: int | None = None
        self.pending_trigger_time_str: str | None = None
        self.pending_values: list[str] = []
        self.has_pending_row: bool = False
        self.active_date_str: str | None = None

    # ---------------------------
    # Helpers
    # ---------------------------
    def _log(self, msg: str):
        try:
            self.log_fn(msg)
        except Exception:
            logger.info(msg)

    def _reset_current_confirmed(self):
        self.current_confirmed_values = ["" for _ in self.param_names]
        self._current_has_confirm = False

    def _build_empty_values(self) -> list[str]:
        return ["" for _ in self.param_names]

    def _get_csv_path(self) -> Path | None:
        path = self._csv_path_provider()
        return path.with_suffix(".csv") if path else None

    def set_active_date(self, active_date_str: str | None):
        self.active_date_str = active_date_str

    def update_manual_params(self, manual_params: Sequence[ManualParam]):
        self.manual_params = list(manual_params)
        self.param_names = [p.name for p in self.manual_params]
        self._reset_current_confirmed()
        if not self.has_pending_row:
            self.pending_values = []

    # ---------------------------
    # Events
    # ---------------------------
    def on_shot_started(self, date_str: str, shot_index: int, trigger_time_str: str | None):
        if self.has_pending_row:
            self._write_pending_row_to_csv()

        self.current_date_str = date_str
        self.current_shot_index = shot_index
        self.current_trigger_time_str = _format_trigger_time(trigger_time_str)
        self._reset_current_confirmed()

    def on_shot_closed(
        self,
        date_str: str,
        shot_index: int,
        trigger_time_str: str | None,
        acquired_ok: bool,
        missing_cameras_list: Sequence[str] | None = None,
    ):
        self.pending_date_str = date_str
        self.pending_shot_index = shot_index
        self.pending_trigger_time_str = _format_trigger_time(trigger_time_str)

        if self.current_shot_index == shot_index and self._current_has_confirm:
            self.pending_values = list(self.current_confirmed_values)
        else:
            self.pending_values = self._build_empty_values()

        self.has_pending_row = True

    def on_confirm_clicked(self, new_values: Sequence[str]):
        self.current_confirmed_values = [str(v).strip() if v is not None else "" for v in new_values]
        self._current_has_confirm = True

        if self.has_pending_row and self.pending_shot_index == self.current_shot_index:
            self.pending_values = list(self.current_confirmed_values)

    def flush_pending_on_stop(self):
        if self.has_pending_row:
            self._write_pending_row_to_csv()
        self.current_date_str = None
        self.current_shot_index = None
        self.current_trigger_time_str = None
        self._reset_current_confirmed()

    # ---------------------------
    # Writing
    # ---------------------------
    def _write_pending_row_to_csv(self):
        if not self.has_pending_row:
            return

        manual_shot_number = self.pending_shot_index
        manual_shot_date = self.pending_date_str
        active_date_str = self.active_date_str

        if manual_shot_number is None:
            self._log("[WARNING] Manual params pending row has no shot number; skipping write.")
            self.has_pending_row = False
            self.pending_values = []
            self.pending_date_str = None
            self.pending_shot_index = None
            self.pending_trigger_time_str = None
            return

        if (
            manual_shot_date is not None
            and active_date_str is not None
            and manual_shot_date != active_date_str
        ):
            self._log(
                "[WARNING] Manual params pending row date %s does not match active date %s; skipping write.",
                manual_shot_date,
                active_date_str,
            )
            self.has_pending_row = False
            self.pending_values = []
            self.pending_date_str = None
            self.pending_shot_index = None
            self.pending_trigger_time_str = None
            return

        csv_path = self._get_csv_path()
        if csv_path is None:
            self._log("[WARNING] Manual params CSV path not set; skipping write of pending row.")
            return

        values_dict = {name: val for name, val in zip(self.param_names, self.pending_values)}
        write_manual_params_row(
            csv_path,
            self.manual_params,
            int(self.pending_shot_index) if self.pending_shot_index is not None else 0,
            self.pending_trigger_time_str,
            values_dict,
        )

        self._log(
            "Manual parameters recorded for shot %03d (%s) -> %s"
            % (
                int(self.pending_shot_index) if self.pending_shot_index is not None else 0,
                self.pending_date_str,
                csv_path,
            )
        )

        self.has_pending_row = False
        self.pending_values = []
        self.pending_date_str = None
        self.pending_shot_index = None
        self.pending_trigger_time_str = None
