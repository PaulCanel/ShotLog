"""Styling helpers and palette for the Streamlit dashboard."""
from __future__ import annotations

BACKGROUND_DARK = "#0b1021"
CARD_BACKGROUND = "#141a2e"
ACCENT = "#4f9eed"
OK_COLOR = "#1b9e77"
WARN_COLOR = "#f4c430"
ERROR_COLOR = "#d95f02"
TEXT_LIGHT = "#f2f4ff"
MUTED_TEXT = "#a9afc1"


def render_card(label: str, value: str, color: str = ACCENT) -> str:
    """Return small HTML snippet for a KPI card."""

    return f"""
    <div style="background:{CARD_BACKGROUND}; padding:12px 16px; border-radius:12px; border:1px solid {color};">
        <div style="color:{MUTED_TEXT}; font-size:12px; text-transform:uppercase; letter-spacing:0.06em;">{label}</div>
        <div style="color:{TEXT_LIGHT}; font-size:28px; font-weight:700; margin-top:4px;">{value}</div>
    </div>
    """
