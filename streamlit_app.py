from __future__ import annotations

import streamlit as st

from dashboard.acquisition_tab import show_acquisition_page
from dashboard.main_app import show_diagnostics_page, show_overview_page
from dashboard.model import DashboardShotStore


st.set_page_config(page_title="ShotLog Dashboard", layout="wide")

if "store" not in st.session_state:
    st.session_state.store = DashboardShotStore()

store: DashboardShotStore = st.session_state.store

page = st.sidebar.radio("Navigation", ["Overview", "Acquisition", "Diagnostics"])

if page == "Overview":
    show_overview_page(store)
elif page == "Acquisition":
    show_acquisition_page(store)
elif page == "Diagnostics":
    show_diagnostics_page(store)
