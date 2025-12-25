from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

import streamlit as st

from shot_log.config import ManualParam, ShotLogConfig
from shot_log.utils import ensure_dir

from .model import DashboardShotStore


def _ensure_state(key: str, value):
    if key not in st.session_state:
        st.session_state[key] = value


def _sync_state_from_config(config: ShotLogConfig) -> None:
    st.session_state["paths_project_root"] = config.project_root or ""
    st.session_state["paths_raw_suffix"] = config.raw_root_suffix or ""
    st.session_state["paths_clean_suffix"] = config.clean_root_suffix or ""
    st.session_state["paths_log_suffix"] = config.rename_log_folder_suffix or ""

    st.session_state["date_mode"] = "manual" if config.manual_date_override else "auto"
    st.session_state["manual_date"] = config.manual_date_override or ""

    st.session_state["timing_full_window"] = float(config.full_window_s or 0.0)
    st.session_state["timing_timeout"] = float(config.timeout_s or 0.0)

    st.session_state["global_keyword"] = config.global_trigger_keyword or ""
    st.session_state["apply_global_keyword"] = bool(config.apply_global_keyword_to_all)
    st.session_state["trigger_cameras"] = sorted(config.trigger_folders)
    st.session_state["used_cameras"] = sorted(config.expected_folders)
    st.session_state["folders_table_data"] = _build_folder_table(config)

    st.session_state["manual_params_data"] = _serialize_manual_params(config.manual_params)
    st.session_state["manual_params_csv"] = config.manual_params_csv_path or ""
    st.session_state["manual_default_path"] = bool(config.use_default_manual_params_path)

    st.session_state["motor_initial_csv"] = config.motor_initial_csv or ""
    st.session_state["motor_history_csv"] = config.motor_history_csv or ""
    st.session_state["motor_output_csv"] = config.motor_positions_output or ""
    st.session_state["motor_default_path"] = bool(config.use_default_motor_positions_path)


def _config_root(config: ShotLogConfig) -> Path:
    if config.project_root:
        return Path(config.project_root)
    return Path.cwd()


def _apply_config(store: DashboardShotStore, config: ShotLogConfig) -> None:
    store.update_config(config)


def _serialize_manual_params(params: Iterable[ManualParam]) -> list[dict[str, str]]:
    return [{"name": p.name, "type": p.type or "text"} for p in params]


def _deserialize_manual_params(rows: list[dict[str, str]]) -> list[ManualParam]:
    manual_params: list[ManualParam] = []
    for row in rows:
        param = ManualParam.from_raw(row)
        if param:
            manual_params.append(param)
    return manual_params


def _build_folder_table(config: ShotLogConfig) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for name, folder in sorted(config.folders.items()):
        specs = []
        for spec in folder.file_specs:
            exts = ", ".join(spec.extensions) if spec.extensions else "*"
            keyword = spec.keyword or "(none)"
            specs.append(f"{keyword}: {exts}")
        rows.append(
            {
                "name": name,
                "expected": "yes" if folder.expected else "no",
                "trigger": "yes" if folder.trigger else "no",
                "file_specs": " | ".join(specs) if specs else "-",
            }
        )
    return rows


def _apply_paths(store: DashboardShotStore, config: ShotLogConfig) -> None:
    project_root = st.session_state["paths_project_root"].strip() or None
    config.project_root = project_root
    config.raw_root_suffix = st.session_state["paths_raw_suffix"].strip() or config.raw_root_suffix
    config.clean_root_suffix = st.session_state["paths_clean_suffix"].strip() or config.clean_root_suffix
    config.rename_log_folder_suffix = (
        st.session_state["paths_log_suffix"].strip() or config.rename_log_folder_suffix
    )
    _apply_config(store, config)
    root = _config_root(config)
    ensure_dir(root / config.raw_root_suffix)
    ensure_dir(root / config.clean_root_suffix)
    ensure_dir(root / config.rename_log_folder_suffix)
    st.success("Paths updated.")


def _apply_date_mode(store: DashboardShotStore, config: ShotLogConfig) -> None:
    mode = st.session_state["date_mode"]
    manual_date = st.session_state["manual_date"].strip()
    config.manual_date_override = manual_date if mode == "manual" and manual_date else None
    _apply_config(store, config)
    if store.shot_manager:
        store.shot_manager.set_manual_date(config.manual_date_override)
    st.success("Date settings updated.")


def _apply_timing(store: DashboardShotStore, config: ShotLogConfig) -> None:
    config.full_window_s = float(st.session_state["timing_full_window"])
    config.timeout_s = float(st.session_state["timing_timeout"])
    _apply_config(store, config)
    if store.shot_manager:
        store.shot_manager.update_runtime_timing(config.full_window_s, config.timeout_s)
    st.success("Timing updated.")


def _apply_trigger_config(store: DashboardShotStore, config: ShotLogConfig) -> None:
    config.global_trigger_keyword = st.session_state["global_keyword"].strip()
    config.apply_global_keyword_to_all = bool(st.session_state["apply_global_keyword"])
    trigger_cams = set(st.session_state.get("trigger_cameras", []))
    used_cams = set(st.session_state.get("used_cameras", []))
    for name, folder in config.folders.items():
        folder.trigger = name in trigger_cams
        folder.expected = name in used_cams
    _apply_config(store, config)
    if store.shot_manager:
        store.shot_manager.update_keyword_settings(
            config.global_trigger_keyword, config.apply_global_keyword_to_all
        )
        store.shot_manager.update_expected_cameras(list(used_cams))
    st.session_state["folders_table_data"] = _build_folder_table(config)
    st.success("Trigger configuration updated.")


def _apply_manual_params(store: DashboardShotStore, config: ShotLogConfig) -> None:
    rows = st.session_state.get("manual_params_data", [])
    manual_params = _deserialize_manual_params(rows)
    config.manual_params = manual_params
    _apply_config(store, config)
    st.success("Manual parameter definitions updated.")


def _apply_motor_config(store: DashboardShotStore, config: ShotLogConfig) -> None:
    config.motor_initial_csv = st.session_state["motor_initial_csv"].strip()
    config.motor_history_csv = st.session_state["motor_history_csv"].strip()
    config.motor_positions_output = st.session_state["motor_output_csv"].strip()
    config.use_default_motor_positions_path = bool(st.session_state["motor_default_path"])
    _apply_config(store, config)
    st.success("Motor configuration updated.")


def _apply_manual_params_paths(store: DashboardShotStore, config: ShotLogConfig) -> None:
    config.manual_params_csv_path = st.session_state["manual_params_csv"].strip() or None
    config.use_default_manual_params_path = bool(st.session_state["manual_default_path"])
    _apply_config(store, config)
    st.success("Manual CSV configuration updated.")


def _confirm_manual_params(store: DashboardShotStore, config: ShotLogConfig) -> None:
    manager = store.manual_params_manager
    values: list[str] = []
    for param in config.manual_params:
        key = f"manual_value_{param.name}"
        raw_value = st.session_state.get(key, "")
        if param.type == "number":
            values.append(str(raw_value) if raw_value is not None else "")
        else:
            values.append(str(raw_value).strip())
    manager.on_confirm_clicked(values)
    st.success("Manual parameters confirmed for current shot.")


def _recompute_motor_positions(store: DashboardShotStore) -> None:
    if store.shot_manager:
        store.shot_manager.recompute_all_motor_positions()
        st.success("Motor positions recomputed.")


def _set_next_shot(store: DashboardShotStore) -> None:
    if not store.shot_manager:
        return
    proposed = int(st.session_state["next_shot_number"])
    conflicts = store.shot_manager.check_next_shot_conflicts(proposed)
    if conflicts.get("same") or conflicts.get("higher"):
        st.error(
            "Proposed shot number conflicts with existing data. "
            "Pick a higher number or clear conflicts manually."
        )
        return
    store.shot_manager.set_next_shot_number(proposed)
    st.success(f"Next shot number set to {proposed:03d}.")


def _format_last_shot_index(status: dict) -> str:
    last_date = status.get("last_shot_date")
    last_index = status.get("last_shot_index")
    if last_date and last_index:
        return f"{last_date} / shot {int(last_index):03d}"
    return "-"


def _format_last_shot_status(status: dict) -> tuple[str, str]:
    last_state = status.get("last_shot_state")
    if last_state is None:
        return "No shot yet", "black"
    if last_state == "acquired_ok":
        return "Acquired – all cameras present", "green"
    if last_state == "acquired_missing":
        missing = status.get("last_shot_missing") or []
        missing_text = ", ".join(missing) if missing else "unknown"
        return f"Acquired – missing: {missing_text}", "red"
    if last_state == "acquiring":
        waiting = status.get("last_shot_waiting_for") or []
        waiting_text = ", ".join(waiting) if waiting else "none"
        return f"Acquiring – waiting for: {waiting_text}", "orange"
    return "No shot yet", "black"


def _format_current_shot_status(status: dict) -> tuple[str, str]:
    cur_state = status.get("current_shot_state")
    if cur_state == "acquiring":
        waiting = status.get("current_shot_waiting_for") or []
        waiting_text = ", ".join(waiting) if waiting else "none"
        return f"Acquiring – waiting for: {waiting_text}", "orange"
    return "Waiting next shot", "blue"


def compute_status_text_and_color(status_dict: dict[str, Any]) -> tuple[str, str]:
    """
    Mirrors the ShotLog Tkinter status label logic, returning (text, css_color).
    """
    if status_dict.get("current_shot_state") == "acquiring":
        return _format_current_shot_status(status_dict)
    return _format_last_shot_status(status_dict)


def show_acquisition_page(store: DashboardShotStore) -> None:
    st.header("Acquisition")

    config = store.current_config.clone()
    status = store.get_status()
    system = status.get("system_status", "-")
    config_ready = status.get("config_ready", False)

    store.manual_params_manager.set_active_date(status.get("active_date_str"))

    _ensure_state("paths_project_root", config.project_root or "")
    _ensure_state("paths_raw_suffix", config.raw_root_suffix or "")
    _ensure_state("paths_clean_suffix", config.clean_root_suffix or "")
    _ensure_state("paths_log_suffix", config.rename_log_folder_suffix or "")
    _ensure_state("date_mode", "manual" if config.manual_date_override else "auto")
    _ensure_state("manual_date", config.manual_date_override or "")
    _ensure_state("timing_full_window", float(config.full_window_s or 0.0))
    _ensure_state("timing_timeout", float(config.timeout_s or 0.0))
    _ensure_state("global_keyword", config.global_trigger_keyword or "")
    _ensure_state("apply_global_keyword", bool(config.apply_global_keyword_to_all))
    _ensure_state("trigger_cameras", sorted(config.trigger_folders))
    _ensure_state("used_cameras", sorted(config.expected_folders))
    _ensure_state("folders_table_data", _build_folder_table(config))
    _ensure_state("manual_params_data", _serialize_manual_params(config.manual_params))
    _ensure_state("manual_params_csv", config.manual_params_csv_path or "")
    _ensure_state("manual_default_path", bool(config.use_default_manual_params_path))
    _ensure_state("motor_initial_csv", config.motor_initial_csv or "")
    _ensure_state("motor_history_csv", config.motor_history_csv or "")
    _ensure_state("motor_output_csv", config.motor_positions_output or "")
    _ensure_state("motor_default_path", bool(config.use_default_motor_positions_path))
    _ensure_state("logs", [])

    status_text, status_color = compute_status_text_and_color(status)
    st.markdown(
        (
            "<div style='text-align:center; font-weight:bold; color:"
            f"{status_color}; font-size:1.2em;'>Status : {status_text}</div>"
        ),
        unsafe_allow_html=True,
    )
    st.markdown(
        (
            "<div style='text-align:center; font-weight:bold; font-size:1.2em;'>"
            f"System : {system}"
            "</div>"
        ),
        unsafe_allow_html=True,
    )

    col1, col2, col3, col4 = st.columns(4)
    with col1:
        st.write(f"System: **{system}**")
        st.write(f"Open shots: **{status.get('open_shots_count', 0)}**")
        st.write(f"Active date: **{status.get('active_date_str', '-')}**")
    with col2:
        next_shot = status.get("next_shot_number", "-")
        st.write(f"Next shot: **{next_shot}**")
        st.write(f"Current keyword: **{status.get('current_keyword', '-') or 'N/A'}**")
    with col3:
        last_shot_text, _ = _format_last_shot_status(status)
        st.write(f"Last shot index: **{_format_last_shot_index(status)}**")
        st.write(f"Last shot status: **{last_shot_text}**")
    with col4:
        current_shot_text, _ = _format_current_shot_status(status)
        st.write(f"Current shot status: **{current_shot_text}**")
        st.write(
            "Timing: "
            f"**window={status.get('full_window', '-')} / timeout={status.get('timeout', '-')}**"
        )

    st.markdown("---")

    disable_start = not config_ready or system not in ("IDLE", "-")
    is_running_like = system in ("RUNNING", "WAITING", "ACQUIRING")
    disable_pause = not is_running_like
    disable_resume = not (config_ready and system == "PAUSED")
    disable_stop = not is_running_like

    col_start, col_pause, col_resume, col_stop = st.columns(4)
    with col_start:
        if st.button("Start", disabled=disable_start):
            store.start_acquisition()
            st.rerun()
    with col_pause:
        if st.button("Pause", disabled=disable_pause):
            store.pause_acquisition()
            st.rerun()
    with col_resume:
        if st.button("Resume", disabled=disable_resume):
            store.resume_acquisition()
            st.rerun()
    with col_stop:
        if st.button("Stop", disabled=disable_stop):
            store.stop_acquisition()
            st.rerun()

    st.markdown("---")

    with st.expander("Paths", expanded=True):
        st.text_input("Base root", key="paths_project_root")
        st.text_input("RAW folder name", key="paths_raw_suffix")
        st.text_input("CLEAN folder name", key="paths_clean_suffix")
        st.text_input("LOG folder name", key="paths_log_suffix")
        if st.button("Apply paths"):
            _apply_paths(store, config)

        root = _config_root(config)
        st.caption(f"RAW data folder: {root / config.raw_root_suffix}")
        st.caption(f"CLEAN data folder: {root / config.clean_root_suffix}")
        st.caption(f"Log folder: {root / config.rename_log_folder_suffix}")

    with st.expander("Shot Date", expanded=False):
        st.radio("Date mode", ["auto", "manual"], key="date_mode", horizontal=True)
        st.text_input("Manual date (YYYYMMDD)", key="manual_date")
        if st.button("Apply date mode"):
            _apply_date_mode(store, config)

    with st.expander("Time Window / Timeout", expanded=False):
        st.number_input(
            "Full time window (s)",
            min_value=0.0,
            key="timing_full_window",
        )
        st.number_input(
            "Timeout (s)",
            min_value=0.0,
            key="timing_timeout",
        )
        if st.button("Apply timing"):
            _apply_timing(store, config)

    with st.expander("Trigger & Cameras Configuration", expanded=False):
        st.text_input("Global trigger keyword", key="global_keyword")
        st.checkbox("Apply global keyword to all", key="apply_global_keyword")
        folder_names = sorted(config.folders.keys())
        st.multiselect("Trigger cameras", folder_names, key="trigger_cameras")
        st.multiselect("Used cameras", folder_names, key="used_cameras")
        if st.button("Apply trigger config"):
            _apply_trigger_config(store, config)
        st.subheader("Folder list")
        st.dataframe(st.session_state["folders_table_data"], width="stretch")

    with st.expander("Configuration File", expanded=False):
        cfg_json = json.dumps(config.to_dict(), indent=2)
        st.download_button(
            "Save config",
            data=cfg_json,
            file_name="shotlog_config.json",
            mime="application/json",
        )
        uploaded = st.file_uploader("Load config", type=["json"], key="config_uploader")
        if uploaded is not None:
            file_key = (uploaded.name, uploaded.size)
            if st.session_state.get("last_config_upload") != file_key:
                data = json.loads(uploaded.getvalue().decode("utf-8"))
                new_config = ShotLogConfig.from_dict(data)
                store.update_config(new_config)
                _sync_state_from_config(new_config)
                st.session_state["last_config_upload"] = file_key
                st.success(f"Configuration loaded from {uploaded.name}.")
                st.rerun()

    with st.expander("Manual parameters setup", expanded=False):
        st.caption("Define the manual parameters collected per shot.")
        edited = st.data_editor(
            st.session_state["manual_params_data"],
            key="manual_params_table",
            num_rows="dynamic",
            width="stretch",
        )
        st.session_state["manual_params_data"] = edited
        if st.button("Save manual params"):
            _apply_manual_params(store, config)

        st.text_input(
            "Manual params CSV path",
            key="manual_params_csv",
        )
        st.checkbox(
            "Use default manual params path",
            key="manual_default_path",
        )
        if st.button("Apply manual params CSV settings"):
            _apply_manual_params_paths(store, config)

    with st.expander("Manual parameters (per shot)", expanded=False):
        manager = store.manual_params_manager
        if manager.current_shot_index is not None:
            st.caption(
                f"Pending shot: {manager.current_date_str} #{manager.current_shot_index:03d}"
            )
        else:
            st.caption("Manual parameters: No shot waiting")

        for param in config.manual_params:
            key = f"manual_value_{param.name}"
            default_value = ""
            if param.name in manager.param_names:
                index = manager.param_names.index(param.name)
                if index < len(manager.current_confirmed_values):
                    default_value = manager.current_confirmed_values[index]
            if param.type == "number":
                try:
                    parsed_value = float(default_value) if default_value else 0.0
                except ValueError:
                    parsed_value = 0.0
                st.number_input(param.name, key=key, value=parsed_value)
            else:
                st.text_input(param.name, key=key, value=default_value)

        if st.button("Confirm manual params"):
            _confirm_manual_params(store, config)

    with st.expander("Motor data", expanded=False):
        st.text_input("Initial positions CSV", key="motor_initial_csv")
        st.text_input("Motor history CSV", key="motor_history_csv")
        st.text_input("Positions by shot CSV", key="motor_output_csv")
        st.checkbox("Use default motor output path", key="motor_default_path")
        if st.button("Apply motor settings"):
            _apply_motor_config(store, config)
        if st.button("Recompute all motor positions"):
            _recompute_motor_positions(store)

    raw_next = status.get("next_shot_number")
    if isinstance(raw_next, int) and raw_next > 0:
        default_next = raw_next
    else:
        default_next = 1

    with st.expander("Next Shot Number", expanded=False):
        st.number_input(
            "Set next shot number",
            min_value=1,
            step=1,
            value=default_next,
            key="next_shot_number",
        )
        if st.button("Set next shot", disabled=not store.shot_manager):
            _set_next_shot(store)

    with st.expander("Logs", expanded=False):
        new_messages = store.poll_gui_queue()
        if new_messages:
            st.session_state["logs"].extend(new_messages)
        st.text_area(
            "Logs",
            value="\n".join(st.session_state["logs"]),
            height=200,
            label_visibility="collapsed",
        )
        if st.button("Clear logs"):
            st.session_state["logs"] = []
