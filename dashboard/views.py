"""View helpers for the Streamlit dashboard."""
from __future__ import annotations

from datetime import datetime
from typing import Iterable, List, Optional

import matplotlib.cm as cm
import matplotlib.colors as mcolors
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
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
        st.plotly_chart(chart, width="stretch")

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
    st.plotly_chart(fig, width="stretch")


def shots_tab(log_data: ParsedLog):
    st.markdown("### Shots detail")
    df = _build_shot_df(log_data)
    if df.empty:
        st.info("No shots parsed yet.")
        return

    all_cams = sorted({cam for cams in df["expected_cams_list"] for cam in cams})
    cam_filter = st.multiselect("Filter by camera", options=all_cams)
    only_missing = st.checkbox(
        "Only shots with missing cameras", value=False, key="shots_only_missing"
    )
    max_shot = int(df["shot_number"].max()) if not df.empty else 0
    shot_range = st.slider("Shot index range", 0, max_shot, (0, max_shot)) if max_shot > 0 else (0, 0)

    filtered = df
    if cam_filter:
        filtered = filtered[
            filtered["expected_cams_list"].apply(lambda s: any(c in s for c in cam_filter))
        ]
    if only_missing:
        filtered = filtered[filtered["missing_count"] > 0]
    filtered = filtered[(filtered["shot_number"] >= shot_range[0]) & (filtered["shot_number"] <= shot_range[1])]

    def status_row(row):
        return "OK" if row["missing_count"] == 0 else "Missing"

    filtered = filtered.assign(status=filtered.apply(status_row, axis=1))
    display_df = filtered.drop(columns=["expected_cams_list"], errors="ignore")
    styled = display_df.style.apply(_style_shot_row, axis=1)
    st.dataframe(styled, use_container_width=True, height=500)


def last_shot_banner(log_data: ParsedLog, font_size: int = 64):
    if "banner_css_loaded" not in st.session_state:
        st.session_state["banner_css_loaded"] = True
        st.markdown(
            """
            <style>
            .last-shot-banner-wrapper {
                position: fixed;
                top: 3.4rem;
                left: 0;
                right: 0;
                z-index: 999;
                display: flex;
                justify-content: center;
                pointer-events: none;
            }
            .last-shot-banner-box {
                margin: 0;
                padding: 0.4rem 1rem 0.3rem 1rem;
                background-color: #ffffff;
                border-bottom: 1px solid #ccc;
                display: inline-block;
                pointer-events: auto;
                text-align: center;
            }
            .last-shot-main {
                font-weight: bold;
                text-align: center;
                transition: color 0.4s linear;
            }
            </style>
            """,
            unsafe_allow_html=True,
        )

    last_shot = None
    if log_data.shots:
        shots_with_time = [s for s in log_data.shots if s.trigger_time is not None]
        if shots_with_time:
            last_shot = max(shots_with_time, key=lambda s: s.trigger_time)

    elapsed_text = "N/A"
    seconds = 0.0
    if last_shot and last_shot.trigger_time:
        now = datetime.now()
        delta = now - last_shot.trigger_time
        seconds = max(delta.total_seconds(), 0.0)

        if seconds > 10 * 3600:
            elapsed_text = "> 10 h"
        else:
            minutes = int(seconds // 60)
            secs = int(seconds % 60)
            if minutes > 0:
                elapsed_text = f"{minutes} min {secs} s"
            else:
                elapsed_text = f"{secs} s"

    color = _jet_color_for_elapsed(seconds)
    text_color = color
    timer_color = "#000000"

    if last_shot and last_shot.trigger_time:
        size_px = max(int(font_size), 16)
        st.markdown(
            f"""
            <div class="last-shot-banner-wrapper">
                <div class="last-shot-banner-box">
                <div class="last-shot-main" style="font-size: {size_px}px; color: {text_color};">
                  Last Shot : {last_shot.shot_number}
                </div>
                <div style="font-size: 18px; margin-top: 0.2rem; color: {timer_color};">
                  Time since last shot: {elapsed_text}
                </div>
              </div>
            </div>
            """,
            unsafe_allow_html=True,
        )


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

    if "shot" in df.columns:
        shot_col = "shot"
    elif "shot_number" in df.columns:
        shot_col = "shot_number"
    else:
        shot_col = None

    if shot_col:
        if selected_col == shot_col:
            numeric_df = df[[shot_col]].copy()
        else:
            numeric_df = df[[shot_col, selected_col]].copy()
    else:
        numeric_df = df[[selected_col]].copy()
        numeric_df["shot_index"] = range(len(numeric_df))
        shot_col = "shot_index"

    numeric_df = numeric_df.dropna(subset=[shot_col])

    shot_series = numeric_df[shot_col]
    if isinstance(shot_series, pd.DataFrame):
        shot_series = shot_series.iloc[:, 0]

    shot_series = shot_series.squeeze()
    shot_series = pd.to_numeric(shot_series, errors="coerce")
    numeric_df[shot_col] = shot_series

    numeric_df = numeric_df.dropna(subset=[shot_col])
    numeric_df[shot_col] = numeric_df[shot_col].astype(int)

    series = numeric_df[selected_col]
    if isinstance(series, pd.DataFrame):
        series = series.iloc[:, 0]
    series = series.astype(str)
    times = series.apply(parse_time_to_seconds)
    is_time = times.notnull().all()

    if is_time:
        numeric_df["value"] = times
    else:
        numeric_df["value"] = pd.to_numeric(series, errors="coerce")

    numeric_df = numeric_df.dropna(subset=["value"])
    numeric_df = numeric_df.sort_values(shot_col)

    if numeric_df.empty:
        st.info("No valid data to plot for this column.")
        return

    if title.startswith("Manual"):
        main_color = "rgba(255, 0, 0, 1.0)"
    elif title.startswith("Motor"):
        main_color = "rgba(0, 0, 255, 1.0)"
    else:
        main_color = "rgba(200, 200, 200, 1.0)"

    if "255, 0, 0" in main_color:
        gap_color = "rgba(255, 150, 150, 0.6)"
    elif "0, 0, 255" in main_color:
        gap_color = "rgba(150, 150, 255, 0.6)"
    else:
        gap_color = "rgba(220, 220, 220, 0.6)"

    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=numeric_df[shot_col],
            y=numeric_df["value"],
            mode="markers",
            name=selected_col,
            marker=dict(color=main_color),
        )
    )

    shots = numeric_df[shot_col].tolist()
    vals = numeric_df["value"].tolist()
    solid_x, solid_y = [], []
    gap_x, gap_y = [], []

    for i in range(len(shots) - 1):
        x0, x1 = shots[i], shots[i + 1]
        y0, y1 = vals[i], vals[i + 1]
        if x1 == x0 + 1:
            solid_x += [x0, x1, None]
            solid_y += [y0, y1, None]
        elif x1 > x0 + 1:
            gap_x += [x0, x1, None]
            gap_y += [y0, y1, None]

    if solid_x:
        fig.add_trace(
            go.Scatter(
                x=solid_x,
                y=solid_y,
                mode="lines",
                line=dict(color=main_color, dash="solid"),
                showlegend=False,
            )
        )

    if gap_x:
        fig.add_trace(
            go.Scatter(
                x=gap_x,
                y=gap_y,
                mode="lines",
                line=dict(color=gap_color, dash="dash"),
                showlegend=False,
            )
        )

    fig.update_layout(
        title=f"{selected_col} vs shot",
        xaxis_title="Shot number",
        yaxis_title=selected_col,
        template="plotly_dark",
    )

    st.plotly_chart(fig, width="stretch")
    hist_fig = px.histogram(
        numeric_df, x="value", nbins=bins, title=f"{selected_col} - histogram"
    )
    hist_fig.update_traces(marker=dict(color=main_color, opacity=0.75))
    st.plotly_chart(hist_fig, width="stretch")

    stats_series = numeric_df["value"]
    st.write(
        {
            "min": seconds_to_clock(stats_series.min()) if is_time else float(stats_series.min()),
            "max": seconds_to_clock(stats_series.max()) if is_time else float(stats_series.max()),
            "mean": seconds_to_clock(stats_series.mean()) if is_time else float(stats_series.mean()),
            "median": seconds_to_clock(stats_series.median()) if is_time else float(stats_series.median()),
        }
    )


def _infer_numeric_columns(df: pd.DataFrame) -> List[str]:
    numeric_cols: List[str] = []
    for col in df.columns:
        name = str(col).strip()
        if not name:
            continue

        sample = df[col].dropna().astype(str).head(50)
        if sample.empty:
            continue

        if sample.apply(lambda v: parse_time_to_seconds(v) is not None).all():
            numeric_cols.append(col)
            continue

        numeric = pd.to_numeric(sample, errors="coerce")
        if numeric.notna().any():
            numeric_cols.append(col)

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
                "expected_cams_list": sorted(shot.expected_cams),
                "expected_cams": ", ".join(sorted(shot.expected_cams)),
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


def _jet_color_for_elapsed(seconds: float) -> str:
    """Return a hex color based on elapsed seconds using a reversed jet colormap.

    0–60 s: continuous gradient along jet_r from red to blue
    >60 s: final blue from jet_r
    """

    s = max(0.0, min(seconds, 60.0))
    t = s / 60.0

    cmap = cm.get_cmap("jet_r")
    r, g, b, _ = cmap(t)
    return mcolors.to_hex((r, g, b))
