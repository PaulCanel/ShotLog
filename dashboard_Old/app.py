"""Streamlit entrypoint for the ShotLog dashboard."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
import threading

import streamlit as st
from streamlit_autorefresh import st_autorefresh
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

import parsers
from data_models import CombinedAlignment, ParsedLog, ParsedManual, ParsedMotor
import views
from styling import BACKGROUND_DARK
from utils import ensure_exports_dir, export_to_excel


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


def _store_upload(upload, state_prefix: str, path_key: str):
    uploads_dir = Path.cwd() / ".streamlit_uploads"
    uploads_dir.mkdir(parents=True, exist_ok=True)
    upload_bytes = upload.getvalue()
    upload_name = upload.name
    target_path = uploads_dir / f"{state_prefix}_{upload_name}"
    target_path.write_bytes(upload_bytes)
    st.session_state[f"{state_prefix}_bytes"] = upload_bytes
    st.session_state[f"{state_prefix}_name"] = upload_name
    st.session_state[path_key] = str(target_path)


def _current_source(path_key: str, bytes_key: str):
    path = st.session_state.get(path_key, "")
    if isinstance(path, str) and path.strip():
        return path.strip()
    return st.session_state.get(bytes_key)


def _input_sidebar():
    st.sidebar.header("Inputs")

    if "log_path" not in st.session_state:
        st.session_state["log_path"] = ""
    if "manual_path" not in st.session_state:
        st.session_state["manual_path"] = ""
    if "motor_path" not in st.session_state:
        st.session_state["motor_path"] = ""

    st.sidebar.subheader("Log file")
    log_upload = st.sidebar.file_uploader(
        "Drop a log .txt file or browse", type=["txt", "log"], key="log_upload"
    )
    if log_upload is not None:
        _store_upload(log_upload, "log", "log_path")
    log_browse = _file_browser("Browse log file", [".txt", ".log"], "log", "log_path")
    log_path = st.sidebar.text_input(
        "Log file path", value=st.session_state["log_path"], key="log_path"
    )
    if log_browse:
        st.session_state["log_path"] = log_browse.strip()

    st.sidebar.subheader("Manual CSV")
    manual_upload = st.sidebar.file_uploader(
        "Drop a manual CSV file or browse", type=["csv"], key="manual_upload"
    )
    if manual_upload is not None:
        _store_upload(manual_upload, "manual", "manual_path")
    manual_browse = _file_browser("Browse manual CSV", [".csv"], "manual", "manual_path")
    manual_path = st.sidebar.text_input(
        "Manual CSV path", value=st.session_state["manual_path"], key="manual_path"
    )
    if manual_browse:
        st.session_state["manual_path"] = manual_browse.strip()

    st.sidebar.subheader("Motor CSV")
    motor_upload = st.sidebar.file_uploader(
        "Drop a motor CSV file or browse", type=["csv"], key="motor_upload"
    )
    if motor_upload is not None:
        _store_upload(motor_upload, "motor", "motor_path")
    motor_browse = _file_browser("Browse motor CSV", [".csv"], "motor", "motor_path")
    motor_path = st.sidebar.text_input(
        "Motor CSV path", value=st.session_state["motor_path"], key="motor_path"
    )
    if motor_browse:
        st.session_state["motor_path"] = motor_browse.strip()

    refresh = st.sidebar.slider("Refresh interval (sec)", 5, 120, 15, key="refresh_interval")
    force = st.sidebar.button("Force refresh", key="force_refresh")
    if force:
        st.session_state["force_reparse"] = True

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

    return refresh, show_last_shot_banner


def _load_sources(
    log_source: str | bytes | None,
    manual_source: str | bytes | None,
    motor_source: str | bytes | None,
):
    errors: list[str] = []
    log_data: ParsedLog | None = None
    manual_data: ParsedManual | None = None
    motor_data: ParsedMotor | None = None

    try:
        if log_source:
            log_data = parsers.load_log(log_source)
        else:
            errors.append("Log file not provided.")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"Log parse error: {exc}")

    try:
        if manual_source and log_data:
            manual_data = parsers.load_manual_csv(manual_source, log_data)
        elif manual_source and not log_data:
            errors.append("Manual CSV provided but log failed to parse.")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"Manual parse error: {exc}")

    try:
        if motor_source and log_data:
            motor_data = parsers.load_motor_csv(motor_source, log_data)
        elif motor_source and not log_data:
            errors.append("Motor CSV provided but log failed to parse.")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"Motor parse error: {exc}")

    return log_data, manual_data, motor_data, errors


def refresh_all_data():
    log_source = _current_source("log_path", "log_bytes")
    manual_source = _current_source("manual_path", "manual_bytes")
    motor_source = _current_source("motor_path", "motor_bytes")
    log_data, manual_data, motor_data, errors = _load_sources(
        log_source,
        manual_source,
        motor_source,
    )
    st.session_state["log_data"] = log_data
    st.session_state["manual_data"] = manual_data
    st.session_state["motor_data"] = motor_data
    st.session_state["last_errors"] = errors
    return errors


def _normalize_watch_paths():
    paths = []
    for key in ("log_path", "manual_path", "motor_path"):
        raw = st.session_state.get(key, "")
        if isinstance(raw, str) and raw.strip():
            paths.append(Path(raw).expanduser().resolve())
    return paths


class _WatchdogHandler(FileSystemEventHandler):
    def __init__(self, target_paths: set[Path], trigger_event: threading.Event):
        super().__init__()
        self._target_paths = target_paths
        self._trigger_event = trigger_event

    def on_modified(self, event):
        self._handle_event(event)

    def on_created(self, event):
        self._handle_event(event)

    def _handle_event(self, event):
        if event.is_directory:
            return
        try:
            event_path = Path(event.src_path).resolve()
        except FileNotFoundError:
            return
        if event_path in self._target_paths:
            self._trigger_event.set()


def _stop_watchdog():
    observer = st.session_state.get("watchdog_observer")
    if observer:
        observer.stop()
        observer.join(timeout=1)
    st.session_state["watchdog_observer"] = None
    st.session_state["watchdog_paths"] = set()


def _configure_watchdog(target_paths: list[Path]):
    trigger_event = st.session_state.setdefault("watchdog_event", threading.Event())
    current_paths = st.session_state.get("watchdog_paths", set())
    new_paths = set(target_paths)

    if not new_paths:
        if current_paths:
            _stop_watchdog()
        return

    if current_paths == new_paths and st.session_state.get("watchdog_observer"):
        return

    _stop_watchdog()
    handler = _WatchdogHandler(new_paths, trigger_event)
    observer = Observer()
    for directory in {p.parent for p in new_paths}:
        observer.schedule(handler, str(directory), recursive=False)
    observer.daemon = True
    observer.start()
    st.session_state["watchdog_observer"] = observer
    st.session_state["watchdog_paths"] = new_paths


def main():
    st.set_page_config(page_title="ShotLog Dashboard", layout="wide")
    st.markdown(
        """
        <style>
        html, body {
            height: 100%;
            margin: 0;
            padding: 0;
            overflow: hidden;
        }
        [data-testid="stAppViewContainer"] {
            height: 100vh;
            overflow: hidden;
        }
        [data-testid="stAppViewContainer"] > .main {
            height: 100%;
            overflow: hidden;
        }
        .block-container {
            padding-top: 0;
            padding-bottom: 0;
            height: 100%;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )
    ui_tick = st_autorefresh(interval=500, key="ui_tick")

    if "log_data" not in st.session_state:
        st.session_state["log_data"] = None
        st.session_state["manual_data"] = None
        st.session_state["motor_data"] = None
    if "last_data_tick" not in st.session_state:
        st.session_state["last_data_tick"] = None

    refresh, show_last_shot_banner = _input_sidebar()
    log_source = _current_source("log_path", "log_bytes")
    manual_source = _current_source("manual_path", "manual_bytes")
    motor_source = _current_source("motor_path", "motor_bytes")
    _configure_watchdog(_normalize_watch_paths())
    refresh_ms = max(int(refresh * 1000), 1000)
    data_tick = st_autorefresh(interval=refresh_ms, key="data_tick")

    force_reparse = st.session_state.pop("force_reparse", False)
    should_reparse = False
    watchdog_event = st.session_state.get("watchdog_event")
    watchdog_triggered = bool(watchdog_event and watchdog_event.is_set())
    if data_tick != st.session_state["last_data_tick"] or force_reparse or watchdog_triggered:
        should_reparse = True
        st.session_state["last_data_tick"] = data_tick
        if watchdog_event:
            watchdog_event.clear()

    if should_reparse:
        errors = refresh_all_data()
        log_data = st.session_state["log_data"]
        manual_data = st.session_state["manual_data"]
        motor_data = st.session_state["motor_data"]
    else:
        log_data = st.session_state["log_data"]
        manual_data = st.session_state["manual_data"]
        motor_data = st.session_state["motor_data"]
        errors = st.session_state.get("last_errors", [])

    status_placeholder = st.sidebar.empty()
    if errors:
        status_placeholder.error("🔴 Error")
        for err in errors:
            st.sidebar.write(err)
    else:
        status_placeholder.success("🟢 Live")

    if not log_data:
        st.info("Provide at least a log file path or upload to start.")
        return

    font_size = st.session_state.get("shot_font_size", 64)

    if manual_data is None:
        manual_data = ParsedManual(header=[], rows=[])
    if motor_data is None:
        motor_data = ParsedMotor(header=[], rows=[])

    alignment = parsers.align_datasets(log_data, manual_data, motor_data)

    if log_data and show_last_shot_banner:
        views.last_shot_banner(log_data, font_size=font_size)
        banner_offset = 140
    else:
        banner_offset = 0

    st.markdown(
        f"""
        <style>
        .tab-scroll-container {{
            position: absolute;
            top: {banner_offset}px;
            left: 0;
            right: 0;
            bottom: 0;
            overflow-y: auto;
            overflow-x: hidden;
            background-color: {BACKGROUND_DARK};
        }}
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
            log_path_label = _source_label(log_source, "log_name")
            manual_path_label = _source_label(manual_source, "manual_name")
            motor_path_label = _source_label(motor_source, "motor_name")
            _diagnostics_tab(
                log_path_label,
                manual_path_label,
                motor_path_label,
                alignment,
                log_data,
                manual_data,
                motor_data,
            )

        st.markdown('</div>', unsafe_allow_html=True)


def _diagnostics_tab(
    log_path: str | None,
    manual_path: str | None,
    motor_path: str | None,
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

    if st.button("Generate Excel export", key="generate_excel"):
        exports_dir = ensure_exports_dir()
        export_name = f"shotlog_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
        export_path = exports_dir / export_name
        wb_path = export_to_excel(log_data, manual_data, motor_data, alignment, dest_path=export_path)
        with open(wb_path, "rb") as f:
            data = f.read()
        st.download_button("Download Excel", data=data, file_name=export_name, key="download_excel")


def _source_label(source: str | bytes | None, name_key: str) -> str | None:
    if isinstance(source, str):
        return source
    if source is not None:
        return st.session_state.get(name_key, "Uploaded file")
    return None


if __name__ == "__main__":
    main()
