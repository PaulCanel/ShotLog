"""Helpers for manual parameter management."""
from __future__ import annotations

import csv
import io
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Iterable, Sequence

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


def _format_date(date_str: str | None) -> str:
    if not date_str:
        return ""
    date_str = str(date_str)
    if re.match(r"^\d{8}$", date_str):
        return f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"
    try:
        return datetime.fromisoformat(date_str).date().isoformat()
    except ValueError:
        return date_str


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
    date_str: str,
    shot_index: int,
    trigger_time: datetime | str | None,
    values: dict[str, str],
):
    output_path = output_path.with_suffix(".csv")
    ensure_dir(output_path.parent)

    param_names = [p.name for p in manual_params]
    headers = ["shot", "date", "trigger_time"] + param_names
    row_values: list[str] = []

    row_values.append(str(shot_index))
    row_values.append(_format_date(date_str))
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
