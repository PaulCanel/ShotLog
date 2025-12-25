from __future__ import annotations

from datetime import date

import streamlit as st

from .model import DashboardShotStore


def show_overview_page(store: DashboardShotStore) -> None:
    st.header("Overview")

    summary = store.get_last_shot_summary()
    if summary:
        status = "OK" if summary.status == "ok" else "Missing"
        text = f"Last Shot : {summary.date_str} #{summary.shot_index:04d} — {status}"
    else:
        text = "Last Shot : none"

    st.markdown(
        f"<div style='text-align:center; font-weight:bold; font-size:1.2em;'>{text}</div>",
        unsafe_allow_html=True,
    )

    if summary:
        st.subheader("Détails du dernier shot")
        col1, col2 = st.columns(2)
        with col1:
            st.write(f"Trigger camera : **{summary.trigger_camera or 'N/A'}**")
            st.write(f"Trigger time   : **{summary.trigger_time}**")
        with col2:
            st.write("Cameras présentes :")
            st.write(", ".join(summary.present_cameras) or "Aucune")
            st.write("Cameras manquantes :")
            st.write(", ".join(summary.missing_cameras) or "Aucune")

        st.subheader("Paramètres manuels")
        st.json(summary.manual_params)

        st.subheader("Positions moteurs")
        st.json(summary.motor_positions)

    if st.button("Refresh overview"):
        st.experimental_rerun()


def show_diagnostics_page(store: DashboardShotStore) -> None:
    st.header("Diagnostics")

    summary = store.get_last_shot_summary()
    if summary:
        st.subheader("Last shot summary")
        st.write(
            f"{summary.date_str} #{summary.shot_index:04d} — "
            f"{'OK' if summary.status == 'ok' else 'Missing'}"
        )
        st.json(
            {
                "trigger_time": str(summary.trigger_time),
                "trigger_camera": summary.trigger_camera,
                "present_cameras": summary.present_cameras,
                "missing_cameras": summary.missing_cameras,
                "clean_files": {k: str(v) for k, v in summary.clean_files.items()},
            }
        )
    else:
        st.info("No shots recorded yet.")

    st.subheader("Shot history (in memory)")
    target_date = st.date_input("Date", value=date.today())
    shots = store.list_shots_for_date(target_date)
    if shots:
        rows = [
            {
                "shot_index": shot.shot_index,
                "status": shot.status,
                "trigger_camera": shot.trigger_camera or "-",
                "trigger_time": shot.trigger_time,
                "present_cameras": ", ".join(shot.present_cameras),
                "missing_cameras": ", ".join(shot.missing_cameras),
            }
            for shot in shots
        ]
        st.dataframe(rows, use_container_width=True)
    else:
        st.caption("No shots found for the selected date.")
