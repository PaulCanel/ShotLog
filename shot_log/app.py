from __future__ import annotations

import tkinter as tk

from .gui import ShotManagerGUI


def main():
    root = tk.Tk()
    app = ShotManagerGUI(root)
    root.geometry("1100x800")
    root.mainloop()
