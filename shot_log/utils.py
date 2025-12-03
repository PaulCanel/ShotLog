"""Utility helpers for the ShotLog application."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
import re


def ensure_dir(path: Path):
    path.mkdir(parents=True, exist_ok=True)


def format_dt_for_name(dt: datetime):
    date_str = dt.strftime("%Y%m%d")
    time_str = dt.strftime("%H%M%S")
    return date_str, time_str


def extract_shot_index_from_name(filename: str):
    """
    Extracts shot index from a CLEAN filename like:
        Cam_YYYYMMDD_HHMMSS_shotNNN.tif
    Returns int or None.
    """
    m = re.search(r"_shot(\d+)\.", filename)
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None
