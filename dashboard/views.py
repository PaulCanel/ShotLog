"""View helpers for the Streamlit dashboard."""
from __future__ import annotations

from datetime import datetime
from typing import Iterable, List, Optional

import pandas as pd
import plotly.express as px
import streamlit as st

from data_models import CombinedAlignment, ParsedLog, ParsedManual, ParsedMotor
from parsers import parse_time_to_seconds
from styling import ACCENT, WARN_COLOR, render_card
from utils import format_datetime, format_ratio, seconds_to_clock

def overview_tab(log_data: ParsedLog):
    gs = log_data.global_summary
    cam_missing = sum(1 for c in log_data.per_camera_summary if c.shots_missing > 0)
    cols = st.columns(6)
    kpis = [
        ("Total shots", f"{gs.total_shots}"),
        ("Shots OK", f"{gs.ok_shots}"),
        ("Shots missing", f"{gs.shots_with_missing}"),
        ("% OK", format_ratio(gs.ok_ratio)),
        ("Total cameras", f"{len(log_data.per_camera_summary)}"),
        ("Cameras with gaps", f"{cam_missing}"),
    ]
    for col, (label, value) in zip(cols, kpis):
        col.markdown(render_card(label, value, color=ACCENT), unsafe_allow_html=True)

    st.markdown("### Missing cameras over time")
    df = _build_shot_df(log_data)
    if not df.empty:
        chart = px.line(df, x="shot_number", y="missing_count", markers=True, color_discrete_sequence=[ACCENT])
        chart.update_layout(template="plotly_dark", height=360)
        st.plotly_chart(chart, use_container_width=True)

        st.markdown("### Latest shots")
        only_missing = st.checkbox(
            "Only shots with missing cameras", value=False, key="overview_only_missing"
        )
        view_df = df.sort_values("shot_number", ascending=False)
        if only_missing:
            view_df = view_df[view_df["missing_count"] > 0]
        st.dataframe(
            view_df[["shot_number", "trigger_time", "missing_count", "missing_cams"]].rename(
                columns={"shot_number": "Shot"}
            ),
            use_container_width=True,
            height=300,
        )
    else:
        st.info("No shots parsed yet.")


def per_camera_tab(log_data: ParsedLog):
    st.markdown("### Camera reliability")
    df = pd.DataFrame(
        [
            {
                "camera": cam.camera,
                "shots_used": cam.shots_used,
                "shots_missing": cam.shots_missing,
                "missing_pct": cam.missing_ratio * 100,
            }
            for cam in log_data.per_camera_summary
        ]
    )
    if df.empty:
        st.info("No camera data available.")
        return

    only_missing = st.checkbox("Only cameras with missing shots", value=False)
    if only_missing:
        df = df[df["shots_missing"] > 0]

    styled = df.style.apply(_style_camera_row, axis=1)
    st.dataframe(styled, use_container_width=True)

    fig = px.bar(
        df.sort_values("missing_pct", ascending=False),
        x="missing_pct",
        y="camera",
        orientation="h",
        color_discrete_sequence=[WARN_COLOR],
    )
    fig.update_layout(template="plotly_dark", height=420, xaxis_title="% missing")
    st.plotly_chart(fig, use_container_width=True)


def shots_tab(log_data: ParsedLog):
    st.markdown("### Shots detail")
    df = _build_shot_df(log_data)
    if df.empty:
        st.info("No shots parsed yet.")
        return

    all_cams = sorted({cam for cams in df["expected_cams"] for cam in cams})
    cam_filter = st.multiselect("Filter by camera", options=all_cams)
    only_missing = st.checkbox(
        "Only shots with missing cameras", value=False, key="shots_only_missing"
    )
    max_shot = int(df["shot_number"].max()) if not df.empty else 0
    shot_range = st.slider("Shot index range", 0, max_shot, (0, max_shot)) if max_shot > 0 else (0, 0)

    filtered = df
    if cam_filter:
        filtered = filtered[filtered["expected_cams"].apply(lambda s: any(c in s for c in cam_filter))]
    if only_missing:
        filtered = filtered[filtered["missing_count"] > 0]
    filtered = filtered[(filtered["shot_number"] >= shot_range[0]) & (filtered["shot_number"] <= shot_range[1])]

    def status_row(row):
        return "OK" if row["missing_count"] == 0 else "Missing"

    filtered = filtered.assign(status=filtered.apply(status_row, axis=1))
    styled = filtered.style.apply(_style_shot_row, axis=1)
    st.dataframe(styled, use_container_width=True, height=500)


def manual_tab(manual: ParsedManual, alignment: CombinedAlignment):
    _csv_tab("Manual CSV", manual.header, alignment.manual_rows, alignment.yellow_keys)


def motor_tab(motor: ParsedMotor, alignment: CombinedAlignment):
    _csv_tab("Motor CSV", motor.header, alignment.motor_rows, alignment.yellow_keys)


def diagnostics_tab(diag_text: str, warning_count: int, export_link):
    st.markdown("### Diagnostics")
    st.write(diag_text)
    st.write(f"Warnings: {warning_count}")
    st.download_button("Download Excel", data=export_link.getvalue(), file_name="export.xlsx")


def _csv_tab(title: str, header: List[str], rows: List, yellow_keys):
    st.markdown(f"### {title} overview")
    st.write(f"Rows: {len(rows)} | Columns: {len(header)}")

    if not header:
        st.info("No CSV data loaded.")
        return

    df = pd.DataFrame([r.values for r in rows], columns=header)
    suspect_only = st.checkbox("Show only suspect rows", value=False, key=f"suspect_{title}")

    def _highlight(row):
        idx = row.name
        display = rows[idx]
        color_map = {
            "green": "background-color: #006400; color: white;",
            "blue": "background-color: #00008B; color: white;",
            "red": "background-color: #8B0000; color: white;",
            "orange": "background-color: #FF8C00; color: white;",
        }
        base = color_map.get(display.bg, "")
        if display.key in yellow_keys or display.yellow_text:
            base += " color: #FFFF00;"
        return [base for _ in row]

    styled = df.style.apply(_highlight, axis=1)
    if suspect_only:
        df = df[[rows[i].key in yellow_keys or rows[i].yellow_text for i in range(len(rows))]]
        styled = df.style.apply(_highlight, axis=1)
    st.dataframe(styled, use_container_width=True, height=500)

    st.markdown("### Quick stats")
    numeric_columns = _infer_numeric_columns(df)
    if not numeric_columns:
        st.info("No numeric/time columns detected.")
        return

    selected_col = st.selectbox(
        "Column to inspect", options=numeric_columns, key=f"{title}_column_select"
    )
    bins = st.slider("Histogram bins", 5, 80, 20, key=f"{title}_bins_slider")

    series = df[selected_col].dropna()
    times = series.apply(parse_time_to_seconds)
    is_time = times.notnull().all()
    values = times if is_time else pd.to_numeric(series, errors="coerce").dropna()

    st.plotly_chart(px.line(values, title=f"{selected_col} - line"), use_container_width=True)
    st.plotly_chart(px.histogram(values, nbins=bins, title=f"{selected_col} - histogram"), use_container_width=True)

    st.write(
        {
            "min": seconds_to_clock(values.min()) if is_time else float(values.min()),
            "max": seconds_to_clock(values.max()) if is_time else float(values.max()),
            "mean": seconds_to_clock(values.mean()) if is_time else float(values.mean()),
            "median": seconds_to_clock(values.median()) if is_time else float(values.median()),
        }
    )


def _infer_numeric_columns(df: pd.DataFrame) -> List[str]:
    numeric_cols = []
    for col in df.columns:
        if col.strip() == "":
            continue
        sample = df[col].dropna().astype(str).head(10)
        if sample.empty:
            continue
        if sample.apply(lambda v: parse_time_to_seconds(v) is not None).all():
            numeric_cols.append(col)
            continue
        try:
            sample.astype(float)
            numeric_cols.append(col)
        except ValueError:
            continue
    return numeric_cols


def _build_shot_df(log_data: ParsedLog) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "shot_number": shot.shot_number,
                "date": shot.date,
                "trigger_time": format_datetime(shot.trigger_time),
                "min_time": format_datetime(shot.min_time),
                "max_time": format_datetime(shot.max_time),
                "trigger_camera": shot.trigger_camera or "",
                "first_camera": shot.first_camera or "",
                "last_camera": shot.last_camera or "",
                "expected_cams": sorted(shot.expected_cams),
                "missing_cams": ", ".join(sorted(shot.missing_cams)),
                "missing_count": len(shot.missing_cams),
            }
            for shot in log_data.shots
        ]
    )


def _style_camera_row(row):
    missing = row["shots_missing"]
    color = "background-color: #006400; color: white;" if missing == 0 else "background-color: #8B0000; color: white;"
    return [color for _ in row]


def _style_shot_row(row):
    missing = row["missing_count"]
    color = "background-color: #006400; color: white;" if missing == 0 else "background-color: #8B0000; color: white;"
    return [color for _ in row]
