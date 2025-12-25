from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from .acquisition_tab import AcquisitionTab
from .model import DashboardShotStore


class OverviewTab(ttk.Frame):
    def __init__(self, parent: tk.Widget, store: DashboardShotStore, *args, **kwargs):
        super().__init__(parent, *args, **kwargs)
        self.store = store

        header = ttk.Frame(self)
        header.pack(fill="x", pady=10)

        self.last_shot_label = ttk.Label(
            header,
            text="Last Shot : none",
            anchor="center",
        )
        self.last_shot_label.configure(font=("TkDefaultFont", 14, "bold"))
        self.last_shot_label.pack(fill="x")

        self.after(500, self._update_overview)

    def _update_overview(self):
        summary = self.store.get_last_shot_summary()
        if summary:
            status = "OK" if summary.status == "ok" else "Missing"
            text = f"Last Shot : {summary.date_str} #{summary.shot_index:04d} — {status}"
        else:
            text = "Last Shot : none"
        self.last_shot_label.configure(text=text)
        self.after(500, self._update_overview)


def main():
    root = tk.Tk()
    root.title("ShotLog Dashboard")

    notebook = ttk.Notebook(root)
    notebook.pack(fill="both", expand=True)

    store = DashboardShotStore()

    overview_tab = OverviewTab(notebook, store=store)
    notebook.add(overview_tab, text="Overview")

    acquisition_tab = AcquisitionTab(notebook, store=store)
    notebook.add(acquisition_tab, text="Acquisition")

    diagnostics_tab = ttk.Frame(notebook)
    notebook.add(diagnostics_tab, text="Diagnostics")

    root.geometry("1200x900")
    root.mainloop()


if __name__ == "__main__":
    main()
