from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

import streamlit as st

from shot_log.config import ManualParam, ShotLogConfig
from shot_log.utils import ensure_dir

from .model import DashboardShotStore


def _ensure_state(key: str, value):
    if key not in st.session_state:
        st.session_state[key] = value


def _sync_state_from_config(config: ShotLogConfig) -> None:
    st.session_state["manual_params_data"] = _serialize_manual_params(config.manual_params)


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


def _render_status(status: dict) -> None:
    col1, col2, col3 = st.columns(3)
    with col1:
        st.metric("System", status.get("system_status", "-"))
        st.metric("Open shots", status.get("open_shots_count", 0))
        st.metric("Last shot index", status.get("last_shot_index", "-"))
    with col2:
        st.metric("Next shot", status.get("next_shot_number", "-"))
        st.metric("Current keyword", status.get("current_keyword", "-"))
        st.metric("Timing (s)", f"{status.get('full_window', '-')}/{status.get('timeout', '-')}")
    with col3:
        st.metric("Last shot status", status.get("last_shot_state", "-"))
        st.metric("Current shot status", status.get("current_shot_state", "-"))
        st.metric("Active date", status.get("active_date_str", "-"))


def show_acquisition_page(store: DashboardShotStore) -> None:
    st.header("Acquisition")

    config = store.current_config.clone()
    status = store.get_status()
    store.manual_params_manager.set_active_date(status.get("active_date_str"))

    _ensure_state("manual_params_data", _serialize_manual_params(config.manual_params))
    _ensure_state("logs", [])

    with st.expander("Paths", expanded=True):
        st.text_input("Base root", value=config.project_root or "", key="paths_project_root")
        st.text_input("RAW folder name", value=config.raw_root_suffix, key="paths_raw_suffix")
        st.text_input("CLEAN folder name", value=config.clean_root_suffix, key="paths_clean_suffix")
        st.text_input(
            "LOG folder name", value=config.rename_log_folder_suffix, key="paths_log_suffix"
        )
        if st.button("Apply paths"):
            _apply_paths(store, config)

        root = _config_root(config)
        st.caption(f"RAW data folder: {root / config.raw_root_suffix}")
        st.caption(f"CLEAN data folder: {root / config.clean_root_suffix}")
        st.caption(f"Log folder: {root / config.rename_log_folder_suffix}")

    with st.expander("Shot Date", expanded=False):
        date_mode_default = "manual" if config.manual_date_override else "auto"
        st.radio(
            "Date mode",
            ["auto", "manual"],
            key="date_mode",
            horizontal=True,
            index=0 if date_mode_default == "auto" else 1,
        )
        st.text_input(
            "Manual date (YYYYMMDD)", value=config.manual_date_override or "", key="manual_date"
        )
        if st.button("Apply date mode"):
            _apply_date_mode(store, config)

    with st.expander("Time Window / Timeout", expanded=False):
        st.number_input(
            "Full time window (s)",
            min_value=0.0,
            value=float(config.full_window_s),
            key="timing_full_window",
        )
        st.number_input(
            "Timeout (s)",
            min_value=0.0,
            value=float(config.timeout_s),
            key="timing_timeout",
        )
        if st.button("Apply timing"):
            _apply_timing(store, config)

    with st.expander("Trigger & Cameras Configuration", expanded=False):
        st.text_input(
            "Global trigger keyword", value=config.global_trigger_keyword, key="global_keyword"
        )
        st.checkbox(
            "Apply global keyword to all",
            value=config.apply_global_keyword_to_all,
            key="apply_global_keyword",
        )
        folder_names = sorted(config.folders.keys())
        st.multiselect(
            "Trigger cameras", folder_names, default=list(config.trigger_folders), key="trigger_cameras"
        )
        st.multiselect(
            "Used cameras", folder_names, default=list(config.expected_folders), key="used_cameras"
        )
        if st.button("Apply trigger config"):
            _apply_trigger_config(store, config)
        st.subheader("Folder list")
        st.dataframe(_build_folder_table(config), use_container_width=True)

    with st.expander("Configuration File", expanded=False):
        cfg_json = json.dumps(config.to_dict(), indent=2)
        st.download_button(
            "Save config",
            data=cfg_json,
            file_name="shotlog_config.json",
            mime="application/json",
        )
        uploaded = st.file_uploader("Load config", type=["json"])
        if uploaded:
            data = json.loads(uploaded.getvalue().decode("utf-8"))
            new_config = ShotLogConfig.from_dict(data)
            store.update_config(new_config)
            _sync_state_from_config(new_config)
            st.experimental_rerun()

    with st.expander("Manual parameters setup", expanded=False):
        st.caption("Define the manual parameters collected per shot.")
        edited = st.data_editor(
            st.session_state["manual_params_data"],
            key="manual_params_table",
            num_rows="dynamic",
            use_container_width=True,
        )
        st.session_state["manual_params_data"] = edited
        if st.button("Save manual params"):
            _apply_manual_params(store, config)

        st.text_input(
            "Manual params CSV path",
            value=config.manual_params_csv_path or "",
            key="manual_params_csv",
        )
        st.checkbox(
            "Use default manual params path",
            value=config.use_default_manual_params_path,
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
        st.text_input(
            "Initial positions CSV", value=config.motor_initial_csv, key="motor_initial_csv"
        )
        st.text_input(
            "Motor history CSV", value=config.motor_history_csv, key="motor_history_csv"
        )
        st.text_input(
            "Positions by shot CSV",
            value=config.motor_positions_output,
            key="motor_output_csv",
        )
        st.checkbox(
            "Use default motor output path",
            value=config.use_default_motor_positions_path,
            key="motor_default_path",
        )
        if st.button("Apply motor settings"):
            _apply_motor_config(store, config)
        if st.button("Recompute all motor positions"):
            _recompute_motor_positions(store)

    with st.expander("Next Shot Number", expanded=False):
        st.number_input(
            "Set next shot number",
            min_value=1,
            step=1,
            value=int(status.get("next_shot_number", 1)),
            key="next_shot_number",
        )
        if st.button("Set next shot"):
            _set_next_shot(store)

    with st.expander("Control", expanded=False):
        col1, col2, col3, col4 = st.columns(4)
        with col1:
            if st.button("Start"):
                store.start_acquisition()
        with col2:
            if st.button("Pause"):
                store.pause_acquisition()
        with col3:
            if st.button("Resume"):
                store.resume_acquisition()
        with col4:
            if st.button("Stop"):
                store.stop_acquisition()

    with st.expander("Status", expanded=False):
        _render_status(status)

    with st.expander("Logs", expanded=False):
        new_messages = store.poll_gui_queue()
        if new_messages:
            st.session_state["logs"].extend(new_messages)
        st.text_area("", value="\n".join(st.session_state["logs"]), height=200)
        if st.button("Clear logs"):
            st.session_state["logs"] = []
