from __future__ import annotations

import streamlit as st

from streamlit_autorefresh import st_autorefresh

from dashboard.acquisition_tab import show_acquisition_page
from dashboard.main_app import show_diagnostics_page, show_overview_page
from dashboard.model import DashboardShotStore


st.set_page_config(page_title="ShotLog Dashboard", layout="wide")

if "dashboard_store" not in st.session_state:
    st.session_state["dashboard_store"] = DashboardShotStore()

store: DashboardShotStore = st.session_state["dashboard_store"]

page = st.sidebar.radio(
    "Navigation",
    ["Overview", "Acquisition", "Diagnostics"],
    key="dashboard_current_page",
)

if page == "Overview":
    show_overview_page(store)
elif page == "Acquisition":
    status = store.get_status()
    is_running = status.get("system_status") in {"WAITING", "ACQUIRING", "RUNNING"}
    if is_running:
        st_autorefresh(interval=1000, key="acquisition_auto_refresh")
    show_acquisition_page(store)
elif page == "Diagnostics":
    show_diagnostics_page(store)
