"""Streamlit entrypoint for the ShotLog dashboard."""
from __future__ import annotations

import io
from datetime import datetime

import streamlit as st
from streamlit_autorefresh import st_autorefresh

from dashboard import parsers
from dashboard.data_models import CombinedAlignment, ParsedLog, ParsedManual, ParsedMotor
from dashboard import views
from dashboard.styling import BACKGROUND_DARK
from dashboard.utils import ensure_exports_dir, export_to_excel, file_signature


st.set_page_config(page_title="ShotLog Dashboard", layout="wide")
st.markdown(
    f"""
    <style>
    body {{ background-color: {BACKGROUND_DARK}; }}
    </style>
    """,
    unsafe_allow_html=True,
)


def _render_sidebar():
    st.sidebar.header("Inputs")
    log_path = st.sidebar.text_input("Log file path", value="", key="log_path")
    manual_path = st.sidebar.text_input("Manual CSV path", value="", key="manual_path")
    motor_path = st.sidebar.text_input("Motor CSV path", value="", key="motor_path")
    refresh = st.sidebar.slider("Refresh interval (sec)", 5, 120, 15)
    force = st.sidebar.button("Force refresh")
    return log_path, manual_path, motor_path, refresh, force


def _load_sources(log_path: str, manual_path: str, motor_path: str):
    errors: list[str] = []
    log_data: ParsedLog | None = None
    manual_data: ParsedManual | None = None
    motor_data: ParsedMotor | None = None

    try:
        if log_path:
            sig = file_signature(log_path)
            if sig:
                log_data = _cached_log_parse(log_path, sig)
            else:
                errors.append(f"Log file not found: {log_path}")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"Log parse error: {exc}")

    try:
        if manual_path and log_data:
            sig = file_signature(manual_path)
            if sig:
                manual_data = _cached_manual_parse(manual_path, sig)
            else:
                errors.append(f"Manual CSV not found: {manual_path}")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"Manual parse error: {exc}")

    try:
        if motor_path and log_data:
            sig = file_signature(motor_path)
            if sig:
                motor_data = _cached_motor_parse(motor_path, sig)
            else:
                errors.append(f"Motor CSV not found: {motor_path}")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"Motor parse error: {exc}")

    return log_data, manual_data, motor_data, errors


@st.cache_data(show_spinner=False)
def _cached_log_parse(path: str, signature: tuple[str, float]):
    return parsers.parse_log_file(path)


@st.cache_data(show_spinner=False)
def _cached_manual_parse(path: str, signature: tuple[str, float]):
    return parsers.parse_manual_csv(path, log_data=None)  # type: ignore[arg-type]


@st.cache_data(show_spinner=False)
def _cached_motor_parse(path: str, signature: tuple[str, float]):
    return parsers.parse_motor_csv(path, log_data=None)  # type: ignore[arg-type]



def main():
    log_path, manual_path, motor_path, refresh, force = _render_sidebar()
    st_autorefresh(interval=refresh * 1000, key="autorefresh")
    if force:
        st.cache_data.clear()

    log_data, manual_data, motor_data, errors = _load_sources(log_path, manual_path, motor_path)

    status_placeholder = st.sidebar.empty()
    if errors:
        status_placeholder.error("🔴 Error")
        for err in errors:
            st.sidebar.write(err)
    else:
        status_placeholder.success("🟢 Live")

    if not log_data:
        st.info("Provide at least a log file path to start.")
        return

    if manual_data is None:
        manual_data = ParsedManual(header=[], rows=[])
    if motor_data is None:
        motor_data = ParsedMotor(header=[], rows=[])

    alignment = parsers.align_datasets(log_data, manual_data, motor_data)

    tabs = st.tabs(["Overview", "Per Camera", "Shots", "Manual CSV", "Motor CSV", "Diagnostics / Export"])
    with tabs[0]:
        views.overview_tab(log_data)
    with tabs[1]:
        views.per_camera_tab(log_data)
    with tabs[2]:
        views.shots_tab(log_data)
    with tabs[3]:
        views.manual_tab(manual_data, alignment)
    with tabs[4]:
        views.motor_tab(motor_data, alignment)
    with tabs[5]:
        _diagnostics_tab(log_path, manual_path, motor_path, alignment, log_data, manual_data, motor_data)


def _diagnostics_tab(
    log_path: str,
    manual_path: str,
    motor_path: str,
    alignment: CombinedAlignment,
    log_data: ParsedLog,
    manual_data: ParsedManual,
    motor_data: ParsedMotor,
):
    st.markdown("### Diagnostics / Export")
    st.write(
        f"Log: {log_path or '-'} | Manual: {manual_path or '-'} | Motor: {motor_path or '-'} | Parsed at: {datetime.now()}"
    )
    st.write(f"Warnings (yellow keys): {len(alignment.yellow_keys)}")

    exports_dir = ensure_exports_dir()
    export_name = f"shotlog_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
    export_path = exports_dir / export_name
    output = io.BytesIO()
    wb_path = export_to_excel(log_data, manual_data, motor_data, alignment, dest_path=export_path)
    with open(wb_path, "rb") as f:
        output.write(f.read())
    output.seek(0)
    st.download_button("Export to Excel", data=output, file_name=export_name)


if __name__ == "__main__":
    main()
