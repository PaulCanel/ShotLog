"""Helpers for manual parameter management."""
from __future__ import annotations

import csv
from pathlib import Path
from typing import Iterable

from .utils import ensure_dir


def build_empty_manual_values(names: Iterable[str]) -> dict[str, str]:
    return {name: "-" for name in names}


def write_manual_params_row(
    output_path: Path,
    manual_params: list[str],
    date_str: str,
    shot_index: int,
    trigger_time_str: str | None,
    values: dict[str, str],
):
    ensure_dir(output_path.parent)
    fieldnames = ["date", "shot", "trigger_time"] + [p for p in manual_params]

    row = {
        "date": date_str,
        "shot": f"{shot_index:03d}",
        "trigger_time": trigger_time_str or "",
    }
    for name in manual_params:
        row[name] = values.get(name, "-")

    file_exists = output_path.exists()
    with open(output_path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if not file_exists:
            writer.writeheader()
        writer.writerow(row)
