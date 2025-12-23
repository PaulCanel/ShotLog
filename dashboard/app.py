"""Streamlit entrypoint for the ShotLog dashboard."""
from __future__ import annotations

import io
from datetime import datetime
from pathlib import Path

import streamlit as st
from streamlit_autorefresh import st_autorefresh

import parsers
from data_models import CombinedAlignment, ParsedLog, ParsedManual, ParsedMotor
import views
from styling import BACKGROUND_DARK
from utils import ensure_exports_dir, export_to_excel, file_signature

UPLOAD_DIR = Path("uploads")

st.set_page_config(page_title="ShotLog Dashboard", layout="wide")
st.markdown(
    f"""
    <style>
    body {{ background-color: {BACKGROUND_DARK}; }}
    </style>
    """,
    unsafe_allow_html=True,
)


def _file_browser(label: str, exts: list[str], state_prefix: str, text_input_key: str):
    if "browser_root" not in st.session_state:
        st.session_state["browser_root"] = str(Path.cwd())

    path_key = f"{state_prefix}_browser_path"
    if path_key not in st.session_state:
        st.session_state[path_key] = st.session_state["browser_root"]

    selected_path: str | None = None
    with st.sidebar.expander(label):
        current = Path(st.session_state[path_key])
        st.write(f"Current folder: `{current}`")

        if current.parent != current and st.button("⬆️ Up one level", key=f"{state_prefix}_up"):
            st.session_state[path_key] = str(current.parent)
            st.experimental_rerun()

        entries = sorted(current.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
        dirs = [e for e in entries if e.is_dir()]
        files = [e for e in entries if e.is_file()]

        dir_selected = st.selectbox(
            "Folders", ["<stay here>"] + [d.name for d in dirs], key=f"{state_prefix}_folder_select"
        )
        if dir_selected != "<stay here>":
            st.session_state[path_key] = str(current / dir_selected)
            st.experimental_rerun()

        file_names = [f.name for f in files if f.suffix.lower() in exts]
        file_selected = st.selectbox(
            "Files", ["<none>"] + file_names, key=f"{state_prefix}_file_select"
        )
        if file_selected != "<none>":
            selected_path = str(current / file_selected)
            st.session_state[text_input_key] = selected_path

    return selected_path


def _input_sidebar():
    st.sidebar.header("Inputs")

    if "refresh_nonce" not in st.session_state:
        st.session_state["refresh_nonce"] = 0

    st.sidebar.subheader("Log file")
    log_upload = st.sidebar.file_uploader(
        "Drop a log .txt file or browse", type=["txt", "log"], key="log_upload"
    )
    log_path = st.sidebar.text_input("Log file path", value="", key="log_path")
    log_browse = _file_browser("Browse log file", [".txt", ".log"], "log", "log_path")

    st.sidebar.subheader("Manual CSV")
    manual_upload = st.sidebar.file_uploader(
        "Drop a manual CSV file or browse", type=["csv"], key="manual_upload"
    )
    manual_path = st.sidebar.text_input("Manual CSV path", value="", key="manual_path")
    manual_browse = _file_browser("Browse manual CSV", [".csv"], "manual", "manual_path")

    st.sidebar.subheader("Motor CSV")
    motor_upload = st.sidebar.file_uploader(
        "Drop a motor CSV file or browse", type=["csv"], key="motor_upload"
    )
    motor_path = st.sidebar.text_input("Motor CSV path", value="", key="motor_path")
    motor_browse = _file_browser("Browse motor CSV", [".csv"], "motor", "motor_path")

    refresh = st.sidebar.slider("Refresh interval (sec)", 5, 120, 15, key="refresh_interval")
    force = st.sidebar.button("Force refresh", key="force_refresh")
    if force:
        st.session_state["refresh_nonce"] += 1

    st.sidebar.markdown("### Display options")
    show_last_shot_banner = st.sidebar.checkbox(
        "Show big last shot number", value=True, key="show_last_shot_banner"
    )

    if "shot_font_size" not in st.session_state:
        st.session_state["shot_font_size"] = 64

    col1, col2 = st.sidebar.columns(2)
    with col1:
        if st.button("+", key="shot_font_plus"):
            st.session_state["shot_font_size"] = min(
                st.session_state["shot_font_size"] + 8, 200
            )
    with col2:
        if st.button("-", key="shot_font_minus"):
            st.session_state["shot_font_size"] = max(
                st.session_state["shot_font_size"] - 8, 16
            )

    UPLOAD_DIR.mkdir(exist_ok=True)

    log_path_effective = log_browse or log_path
    manual_path_effective = manual_browse or manual_path
    motor_path_effective = motor_browse or motor_path

    if log_upload is not None:
        log_dest = UPLOAD_DIR / log_upload.name
        with open(log_dest, "wb") as f:
            f.write(log_upload.getbuffer())
        log_path_effective = str(log_dest)

    if manual_upload is not None:
        manual_dest = UPLOAD_DIR / manual_upload.name
        with open(manual_dest, "wb") as f:
            f.write(manual_upload.getbuffer())
        manual_path_effective = str(manual_dest)

    if motor_upload is not None:
        motor_dest = UPLOAD_DIR / motor_upload.name
        with open(motor_dest, "wb") as f:
            f.write(motor_upload.getbuffer())
        motor_path_effective = str(motor_dest)

    return (
        log_path_effective,
        manual_path_effective,
        motor_path_effective,
        refresh,
        show_last_shot_banner,
        st.session_state["refresh_nonce"],
    )


def _load_sources(
    log_path: str,
    manual_path: str,
    motor_path: str,
    log_sig: tuple[str, float] | None,
    manual_sig: tuple[str, float] | None,
    motor_sig: tuple[str, float] | None,
    nonce: int,
):
    errors: list[str] = []
    log_data: ParsedLog | None = None
    manual_data: ParsedManual | None = None
    motor_data: ParsedMotor | None = None

    try:
        if log_path:
            if log_sig:
                log_data = parsers.load_log(log_path, (log_sig, nonce))
            else:
                errors.append(f"Log file not found: {log_path}")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"Log parse error: {exc}")

    try:
        if manual_path and log_data:
            if manual_sig:
                manual_data = parsers.load_manual_csv(manual_path, (manual_sig, nonce), log_data)
            else:
                errors.append(f"Manual CSV not found: {manual_path}")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"Manual parse error: {exc}")

    try:
        if motor_path and log_data:
            if motor_sig:
                motor_data = parsers.load_motor_csv(motor_path, (motor_sig, nonce), log_data)
            else:
                errors.append(f"Motor CSV not found: {motor_path}")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"Motor parse error: {exc}")

    return log_data, manual_data, motor_data, errors


def main():
    st.markdown(
        """
        <style>
        .block-container {
            padding-top: 0.5rem;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )
    ui_tick = st_autorefresh(interval=500, key="ui_tick")

    log_path, manual_path, motor_path, refresh, show_last_shot_banner, refresh_nonce = _input_sidebar()
    st_autorefresh(interval=refresh * 1000, key="autorefresh")

    log_sig = file_signature(log_path) if log_path else None
    manual_sig = file_signature(manual_path) if manual_path else None
    motor_sig = file_signature(motor_path) if motor_path else None

    log_data, manual_data, motor_data, errors = _load_sources(
        log_path, manual_path, motor_path, log_sig, manual_sig, motor_sig, refresh_nonce
    )

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

    font_size = st.session_state.get("shot_font_size", 64)

    if manual_data is None:
        manual_data = ParsedManual(header=[], rows=[])
    if motor_data is None:
        motor_data = ParsedMotor(header=[], rows=[])

    alignment = parsers.align_datasets(log_data, manual_data, motor_data)

    if log_data and show_last_shot_banner:
        views.last_shot_banner(log_data, font_size=font_size)

    st.markdown(
        """
        <style>
        .tab-scroll-container {
            max-height: calc(100vh - 130px);
            overflow-y: auto;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    with st.container():
        st.markdown('<div class="tab-scroll-container">', unsafe_allow_html=True)

        tabs = st.tabs([
            "Overview",
            "Per Camera",
            "Shots",
            "Manual CSV",
            "Motor CSV",
            "Diagnostics / Export",
        ])
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
            _diagnostics_tab(
                log_path, manual_path, motor_path, alignment, log_data, manual_data, motor_data
            )

        st.markdown('</div>', unsafe_allow_html=True)


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
