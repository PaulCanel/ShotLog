import os
import re
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# -------------------------
# Config
# -------------------------
FONT_SIZE = 150
WINDOW_SIZE = "1500x300"
# -------------------------


def get_last_shot_number(path):
    last_num = None
    re_new = re.compile(r"New shot detected:.*shot=(\d+)")
    re_shot = re.compile(r"Shot\s+(\d+)\s+\(")

    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                m = re_new.search(line)
                if m:
                    last_num = int(m.group(1))
                    continue
                m = re_shot.search(line)
                if m:
                    last_num = int(m.group(1))
    except Exception:
        return None
    return last_num


class LogFileEventHandler(FileSystemEventHandler):
    def __init__(self, gui, path):
        super().__init__()
        self.gui = gui
        self.path = os.path.abspath(path)

    def on_modified(self, event):
        if os.path.abspath(event.src_path) == self.path:
            self.gui.root.after(0, self.gui.update_last_shot_from_watcher)


class SimpleLogWatcherGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("Simple Shot Watcher")

        self.current_log_path = None
        self.observer = None

        self._build_gui()

    def _build_gui(self):
        frame = ttk.Frame(self.root, padding=10)
        frame.pack(fill="both", expand=True)

        self.btn_open = ttk.Button(frame, text="Open log...", command=self.load_log)
        self.btn_open.grid(row=0, column=0, sticky="w")

        self.lbl_file = ttk.Label(frame, text="No log selected")
        self.lbl_file.grid(row=0, column=1, sticky="w", padx=10)

        self.lbl_last_shot = tk.Label(
            frame,
            text="Last shot: -",
            font=("TkDefaultFont", FONT_SIZE, "bold"),
            fg="red",
            bg="white"
        )
        self.lbl_last_shot.grid(row=1, column=0, columnspan=2, sticky="w", pady=20)

        frame.columnconfigure(0, weight=1)
        frame.columnconfigure(1, weight=1)

    def load_log(self):
        path = filedialog.askopenfilename(
            title="Select a log file",
            filetypes=[("Text files", "*.txt;*.log"), ("All files", "*.*")]
        )
        if not path:
            return

        self.current_log_path = path
        self.lbl_file.configure(text=os.path.basename(path))
        self.update_last_shot()
        self.start_watching()

    def update_last_shot(self):
        if not self.current_log_path:
            return
        num = get_last_shot_number(self.current_log_path)
        if num is None:
            self.lbl_last_shot.configure(text="Last shot: -")
        else:
            self.lbl_last_shot.configure(text=f"Last shot: {num}")

    def update_last_shot_from_watcher(self):
        self.update_last_shot()

    def start_watching(self):
        self.stop_watching()
        if not self.current_log_path:
            return

        directory = os.path.dirname(self.current_log_path) or "."
        handler = LogFileEventHandler(self, self.current_log_path)
        observer = Observer()
        observer.schedule(handler, directory, recursive=False)
        observer.daemon = True
        observer.start()
        self.observer = observer

    def stop_watching(self):
        if self.observer is not None:
            self.observer.stop()
            self.observer = None

    def on_close(self):
        self.stop_watching()
        self.root.destroy()


def main():
    root = tk.Tk()
    root.geometry(WINDOW_SIZE)
    app = SimpleLogWatcherGUI(root)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()


if __name__ == "__main__":
    main()
