#!/usr/bin/env python3
"""
Fake ShotLog simulator

- Génère des shots artificiels dans un dossier RAW.
- Génère des événements moteurs artificiels dans un CSV d'historique.
- Ne dépend pas d'un jeu de données réel : tout est synthétique.

Configuration à modifier en haut du fichier :
- PROJECT_ROOT
- RAW_FOLDER_NAME
- MOTOR_FOLDER_NAME
- CAMERA_CONFIG
- DELAY_* et SHOT_DATE
"""

from __future__ import annotations

import random
import csv
import json
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox


# ==========================
# ===== CONFIGURATION ======
# ==========================

# Racine du projet (par défaut : dossier courant)
PROJECT_ROOT = Path.cwd()

# Nom du dossier RAW à l'intérieur de PROJECT_ROOT
RAW_FOLDER_NAME = "ELI50069_RAW_DATA"

# Dossier où seront stockés les CSV moteurs (dans PROJECT_ROOT / MOTOR_FOLDER_NAME)
MOTOR_FOLDER_NAME = "motors"

# Date logique des shots (celle qui sera utilisée pour les sous-dossiers de date)
# Tu peux mettre datetime.today().date() si tu veux la date du jour.
SHOT_DATE = datetime(2025, 12, 2).date()

# Délais min / max autour du trigger (en secondes) pour les fichiers de chaque caméra
# Exemple : entre -0.5s et +0.5s autour du trigger de shot.
DELAY_MIN_SEC = -0.5
DELAY_MAX_SEC = 0.5

# Configuration des caméras :
# Pour chaque caméra (clé), une liste de "specs" pour les fichiers générés à chaque shot.
#   keyword : chaîne de caractères qui doit apparaître dans le nom du fichier
#   ext     : extension (".tiff", ".tif", ".png", etc.)
#
# Tu peux modifier / ajouter / supprimer des caméras et des specs à ta guise.
CAMERA_CONFIG = {
    "Lanex1": [{"keyword": "2025", "ext": ".tiff"}],
    "Lanex2": [{"keyword": "2025", "ext": ".tiff"}],
    "Lanex3": [{"keyword": "2025", "ext": ".tiff"}],
    "Lanex4": [{"keyword": "2025", "ext": ".tiff"}],
    "Lanex5": [{"keyword": "2025", "ext": ".tiff"}],
    "Csi":    [{"keyword": "2025", "ext": ".tiff"}],
    "Lyso":   [{"keyword": "2025", "ext": ".tiff"}],
}

# Nom du fichier des positions initiales moteurs (optionnel)
INITIAL_MOTOR_FILE = "initial.csv"        # dans PROJECT_ROOT / MOTOR_FOLDER_NAME
# Préfixe du fichier d'historique des moteurs : istoric_YYYY-MM-DD.csv
MOTOR_HISTORY_PREFIX = "istoric_"


# ===============================
# ===== DATA STRUCTURES =========
# ===============================

@dataclass
class CameraFileSpec:
    keyword: str
    ext: str


@dataclass
class CameraConfig:
    name: str
    specs: list[CameraFileSpec]


# ==================================
# ===== SIMULATEUR DE SHOTS ========
# ==================================

class FakeShotSimulator:
    def __init__(self):
        # Config "runtime"
        self.project_root: Path = PROJECT_ROOT
        self.raw_root: Path = self.project_root / RAW_FOLDER_NAME
        self.motor_root: Path = self.project_root / MOTOR_FOLDER_NAME

        self.date = SHOT_DATE
        self.shot_index = 1  # shot logique (1, 2, 3, ...)
        self.max_delay_sec = 1.0

        self.cameras: list[CameraConfig] = []
        for cam_name, spec_list in CAMERA_CONFIG.items():
            specs = [CameraFileSpec(keyword=s["keyword"], ext=s["ext"]) for s in spec_list]
            self.cameras.append(CameraConfig(name=cam_name, specs=specs))

        self.motor_axes: list[str] = []

        # Dictionnaire Axis -> position courante (pour les moteurs)
        self.motor_positions = {}
        self._load_initial_motor_positions()

    def set_cameras(self, cameras: list[CameraConfig]):
        self.cameras = cameras
        print("[SIM] Cameras configuration updated:")
        for cam in self.cameras:
            print(f"  - {cam.name}: {[(s.keyword, s.ext) for s in cam.specs]}")

    def set_max_delay(self, value: float):
        self.max_delay_sec = max(0.0, float(value))
        print(f"[SIM] Max generation delay set to {self.max_delay_sec} s")

    # ---------- helpers chemin / date / heure ----------

    def _ensure_dir(self, path: Path):
        path.mkdir(parents=True, exist_ok=True)

    def _date_str(self) -> str:
        return self.date.strftime("%Y%m%d")

    def _time_str(self, dt: datetime) -> str:
        return dt.strftime("%H%M%S")

    def _now_time_str(self) -> str:
        return datetime.now().strftime("%H:%M:%S")

    # ---------- gestion des dossiers ----------

    def set_project_root(self, new_root: Path):
        self.project_root = new_root
        self.raw_root = new_root / RAW_FOLDER_NAME
        self.motor_root = new_root / MOTOR_FOLDER_NAME
        print(f"[SIM] Project root set to: {self.project_root}")
        print(f"[SIM] RAW root: {self.raw_root}")
        print(f"[SIM] Motor root: {self.motor_root}")
        self._ensure_dir(self.raw_root)
        self._ensure_dir(self.motor_root)
        self._load_initial_motor_positions()

    # ---------- moteurs ----------

    def _load_initial_motor_positions(self):
        """
        Charge éventuellement un fichier initial.csv dans motor_root
        pour initialiser les positions des axes. Si absent, on
        initialise une dict vide et on utilisera 0.0 par défaut.
        """
        self.motor_positions = {}
        self.motor_axes = []
        init_path = self.motor_root / INITIAL_MOTOR_FILE
        if not init_path.exists():
            print(
                f"[SIM] No initial motor file found at {init_path}, starting with empty positions and no axes."
            )
            return

        try:
            with init_path.open("r", newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    axis = row.get("Axis")
                    pos_str = row.get("Position")
                    if axis and pos_str:
                        try:
                            pos = float(pos_str)
                        except ValueError:
                            continue
                        self.motor_positions[axis] = pos
            self.motor_axes = list(self.motor_positions.keys())
            print(f"[SIM] Loaded initial motor positions from {init_path} "
                  f"for {len(self.motor_positions)} axes.")
        except Exception as e:
            print(f"[SIM] Error reading initial motor file {init_path}: {e}")

    def _motor_history_path_for_today(self) -> Path:
        date_str = self.date.isoformat()  # YYYY-MM-DD
        return self.motor_root / f"{MOTOR_HISTORY_PREFIX}{date_str}.csv"

    def generate_motor_event(self):
        """
        Génère une ligne d'événement moteur artificiel dans le CSV d'historique.
        Format : Time,Axis,Old Position,New Position
        """
        if not self.motor_axes:
            print("[SIM] No motor axes loaded from initial.csv, cannot generate motor event.")
            try:
                messagebox.showwarning(
                    "No motor axes",
                    "No motor axes loaded from initial.csv, cannot generate motor event.",
                )
            except Exception:
                pass
            return

        self._ensure_dir(self.motor_root)
        hist_path = self._motor_history_path_for_today()
        file_exists = hist_path.exists()

        axis = random.choice(self.motor_axes)

        old_pos = self.motor_positions.get(axis, 0.0)
        delta = random.uniform(-5.0, 5.0)
        new_pos = old_pos + delta
        self.motor_positions[axis] = new_pos

        time_str = self._now_time_str()

        with hist_path.open("a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow(["Time", "Axis", "Old Position", "New Position"])
            writer.writerow([time_str, axis, f"{old_pos:.3f}", f"{new_pos:.3f}"])

        print(f"[SIM] Motor event: time={time_str}, axis={axis}, "
              f"old={old_pos:.3f}, new={new_pos:.3f}, file={hist_path}")

    # ---------- génération de fichiers pour un shot ----------

    def _generate_file_for_camera(
        self, cam: CameraConfig, spec: CameraFileSpec, shot_time: datetime, shot_number: int
    ):
        """
        Génère un fichier 'image' artificiel pour une caméra donnée, avec :
          - un délai aléatoire autour du shot_time
          - un nom contenant la date, l'heure, le keyword et le numéro de shot
          - l'extension spécifiée (spec.ext)
        On se fiche du contenu : un petit header texte suffit.
        """
        file_time = shot_time

        date_str = self._date_str()
        time_str = self._time_str(file_time)

        # Nom de fichier de type :   <Cam>_<keyword>_<YYYYMMDD>_<HHMMSS>_shotXXXX.ext
        # (tu peux adapter ce pattern si tu veux coller plus précisément au naming ELI)
        shot_str = f"{shot_number:04d}"
        keyword_part = f"_{spec.keyword}" if spec.keyword else ""
        ext = spec.ext.strip() or ".tiff"
        filename = f"{cam.name}{keyword_part}_{date_str}_{time_str}_shot{shot_str}{ext}"

        # Dossier RAW : RAW_ROOT / Cam / YYYYMMDD
        cam_dir = self.raw_root / cam.name / date_str
        self._ensure_dir(cam_dir)
        file_path = cam_dir / filename

        # Contenu minimaliste
        content = (
            f"Fake image file for ShotLog simulation\n"
            f"Camera: {cam.name}\n"
            f"Shot index: {shot_number}\n"
            f"Logical time: {file_time.isoformat()}\n"
            f"Keyword: {spec.keyword}\n"
        )

        with file_path.open("w", encoding="utf-8") as f:
            f.write(content)

        # Ajuster les timestamps du fichier (atime, mtime)
        ts = file_time.timestamp()
        try:
            import os
            os.utime(file_path, (ts, ts))
        except Exception as e:
            print(f"[SIM] Warning: could not set mtime for {file_path}: {e}")

        print(f"[SIM] Created file: {file_path} (mtime={file_time})")

    def generate_shot(self):
        """
        Génère un shot artificiel :
        - On choisit un shot_time = datetime.now()
        - Pour chaque caméra, pour chaque spec, on planifie la création
          d'un fichier dans la fenêtre [shot_time, shot_time + max_delay_sec].
        """
        self._ensure_dir(self.raw_root)
        start_time = datetime.now()
        shot_number = self.shot_index
        print(f"[SIM] Generating shot {shot_number} at logical time {start_time}")

        for cam in self.cameras:
            for spec in cam.specs:
                delay = random.uniform(0.0, self.max_delay_sec)

                def _task(cam=cam, spec=spec, delay=delay):
                    creation_time = start_time + timedelta(seconds=delay)
                    self._generate_file_for_camera(cam, spec, creation_time, shot_number)

                timer = threading.Timer(delay, _task)
                timer.daemon = True
                timer.start()

        print(f"[SIM] Shot {shot_number} generated.\n")
        self.shot_index += 1

    def to_config_dict(self) -> dict:
        return {
            "project_root": str(self.project_root),
            "raw_folder_name": RAW_FOLDER_NAME,
            "motor_folder_name": MOTOR_FOLDER_NAME,
            "date": self.date.isoformat(),
            "max_delay_sec": self.max_delay_sec,
            "cameras": [
                {
                    "name": cam.name,
                    "files": [
                        {"keyword": spec.keyword, "ext": spec.ext}
                        for spec in cam.specs
                    ],
                }
                for cam in self.cameras
            ],
        }

    def load_from_config_dict(self, cfg: dict):
        pr = cfg.get("project_root")
        if pr:
            self.set_project_root(Path(pr))

        date_str = cfg.get("date")
        if date_str:
            self.date = datetime.fromisoformat(date_str).date()

        md = cfg.get("max_delay_sec")
        if md is not None:
            self.set_max_delay(float(md))

        cams = []
        for c in cfg.get("cameras", []):
            name = c.get("name", "")
            files = []
            for f in c.get("files", []):
                kw = f.get("keyword", "")
                ext = f.get("ext", "")
                files.append(CameraFileSpec(keyword=kw, ext=ext))
            if name:
                cams.append(CameraConfig(name=name, specs=files))
        self.set_cameras(cams)


# ==================================
# ====== INTERFACE TKINTER =========
# ==================================

class SimulatorGUI:
    def __init__(self, root: tk.Tk, simulator: FakeShotSimulator):
        self.root = root
        self.sim = simulator

        root.title("Fake ShotLog Simulator")

        # Racine projet
        self.project_var = tk.StringVar(value=str(self.sim.project_root))
        self.delay_var = tk.StringVar(value=str(self.sim.max_delay_sec))

        frm_root = tk.Frame(root)
        frm_root.pack(padx=10, pady=10, fill="x")

        tk.Label(frm_root, text="Project root:").grid(row=0, column=0, sticky="w")
        self.entry_root = tk.Entry(frm_root, textvariable=self.project_var, width=60)
        self.entry_root.grid(row=0, column=1, sticky="we", padx=5)
        btn_browse = tk.Button(frm_root, text="Browse...", command=self._browse_root)
        btn_browse.grid(row=0, column=2, padx=5)

        frm_root.columnconfigure(1, weight=1)

        # Boutons config save/load
        frm_cfg = tk.Frame(root)
        frm_cfg.pack(padx=10, pady=(0, 10), anchor="w")

        btn_save_cfg = tk.Button(frm_cfg, text="Save config...", command=self._save_config)
        btn_save_cfg.grid(row=0, column=0, padx=5)

        btn_load_cfg = tk.Button(frm_cfg, text="Load config...", command=self._load_config)
        btn_load_cfg.grid(row=0, column=1, padx=5)

        # Info caméra
        camera_names = ", ".join(cam.name for cam in self.sim.cameras)
        self.lbl_cameras = tk.Label(root, text=f"Cameras: {camera_names}")
        self.lbl_cameras.pack(padx=10, pady=(0, 5), anchor="w")

        # Info RAW / MOTOR
        self.lbl_paths = tk.Label(
            root,
            text=self._format_paths_text(),
            justify="left"
        )
        self.lbl_paths.pack(padx=10, pady=(0, 10), anchor="w")

        btn_cfg = tk.Button(root, text="Configure cameras...", command=self._open_camera_config)
        btn_cfg.pack(padx=10, pady=(0, 10), anchor="w")

        frm_delay = tk.Frame(root)
        frm_delay.pack(padx=10, pady=(0, 10), anchor="w")

        tk.Label(frm_delay, text="Max file generation delay (s):").grid(row=0, column=0, sticky="w")
        entry_delay = tk.Entry(frm_delay, textvariable=self.delay_var, width=8)
        entry_delay.grid(row=0, column=1, padx=5)

        def _on_delay_changed(*args):
            try:
                val = float(self.delay_var.get())
            except ValueError:
                return
            self.sim.set_max_delay(val)

        self.delay_var.trace_add("write", _on_delay_changed)

        # Info shot courant
        self.shot_var = tk.StringVar(value=f"Next shot index: {self.sim.shot_index}")
        tk.Label(root, textvariable=self.shot_var, font=("TkDefaultFont", 10, "bold")).pack(
            padx=10, pady=(0, 10), anchor="w"
        )

        # Boutons d'action
        frm_btn = tk.Frame(root)
        frm_btn.pack(padx=10, pady=10)

        btn_shot = tk.Button(frm_btn, text="Generate shot", command=self._on_generate_shot)
        btn_shot.grid(row=0, column=0, padx=5)

        btn_motor = tk.Button(frm_btn, text="Generate motor event", command=self._on_generate_motor)
        btn_motor.grid(row=0, column=1, padx=5)

        btn_quit = tk.Button(frm_btn, text="Quit", command=root.quit)
        btn_quit.grid(row=0, column=2, padx=5)

    def _format_paths_text(self) -> str:
        return (
            f"RAW folder  : {self.sim.raw_root}\n"
            f"Motor folder: {self.sim.motor_root}\n"
            f"Date (for folders & motor CSV): {self.sim.date.isoformat()}"
        )

    def _browse_root(self):
        new_dir = filedialog.askdirectory(
            title="Select project root",
            initialdir=str(self.sim.project_root)
        )
        if not new_dir:
            return
        p = Path(new_dir)
        self.project_var.set(str(p))
        self.sim.set_project_root(p)
        self.lbl_paths.config(text=self._format_paths_text())
        self._refresh_camera_label()

    def _on_generate_shot(self):
        try:
            self.sim.generate_shot()
            self.shot_var.set(f"Next shot index: {self.sim.shot_index}")
        except Exception as e:
            messagebox.showerror("Error", f"Error while generating shot: {e}")

    def _on_generate_motor(self):
        try:
            self.sim.generate_motor_event()
        except Exception as e:
            messagebox.showerror("Error", f"Error while generating motor event: {e}")

    def _refresh_camera_label(self):
        camera_names = ", ".join(cam.name for cam in self.sim.cameras)
        self.lbl_cameras.config(text=f"Cameras: {camera_names}")

    def _open_camera_config(self):
        top = tk.Toplevel(self.root)
        top.title("Configure cameras")

        camera_configs = [
            CameraConfig(
                name=cam.name,
                specs=[CameraFileSpec(keyword=s.keyword, ext=s.ext) for s in cam.specs],
            )
            for cam in self.sim.cameras
        ]

        frm_list = tk.Frame(top)
        frm_list.pack(padx=10, pady=10, fill="both", expand=True)

        tk.Label(frm_list, text="Cameras:").pack(anchor="w")
        listbox = tk.Listbox(frm_list, height=8)
        listbox.pack(fill="both", expand=True)

        def refresh_listbox():
            listbox.delete(0, tk.END)
            for cam in camera_configs:
                listbox.insert(tk.END, cam.name)

        def on_add():
            self._edit_camera(top, None, lambda new_cam: (camera_configs.append(new_cam), refresh_listbox()))

        def on_edit():
            sel = listbox.curselection()
            if not sel:
                return
            idx = sel[0]
            self._edit_camera(top, camera_configs[idx], lambda updated: (camera_configs.__setitem__(idx, updated), refresh_listbox()))

        def on_remove():
            sel = listbox.curselection()
            if not sel:
                return
            idx = sel[0]
            camera_configs.pop(idx)
            refresh_listbox()

        btn_frame = tk.Frame(top)
        btn_frame.pack(padx=10, pady=5, anchor="w")

        tk.Button(btn_frame, text="Add", command=on_add).grid(row=0, column=0, padx=5)
        tk.Button(btn_frame, text="Edit", command=on_edit).grid(row=0, column=1, padx=5)
        tk.Button(btn_frame, text="Remove", command=on_remove).grid(row=0, column=2, padx=5)

        def on_close():
            self.sim.set_cameras(camera_configs)
            self._refresh_camera_label()
            top.destroy()

        tk.Button(top, text="Close", command=on_close).pack(pady=10)

        top.protocol("WM_DELETE_WINDOW", on_close)

        refresh_listbox()

    def _edit_camera(self, parent, camera: CameraConfig | None, on_save):
        top = tk.Toplevel(parent)
        top.title("Edit camera" if camera else "Add camera")

        tk.Label(top, text="Camera name:").grid(row=0, column=0, sticky="w", padx=10, pady=(10, 5))
        name_var = tk.StringVar(value=camera.name if camera else "")
        entry_name = tk.Entry(top, textvariable=name_var, width=30)
        entry_name.grid(row=0, column=1, padx=10, pady=(10, 5))

        specs = [CameraFileSpec(keyword=s.keyword, ext=s.ext) for s in (camera.specs if camera else [])]

        tk.Label(top, text="File specs:").grid(row=1, column=0, sticky="nw", padx=10, pady=(5, 5))
        listbox = tk.Listbox(top, height=6)
        listbox.grid(row=1, column=1, padx=10, pady=(5, 5), sticky="nsew")

        def refresh_specs():
            listbox.delete(0, tk.END)
            for s in specs:
                listbox.insert(tk.END, f"keyword='{s.keyword}', ext='{s.ext}'")

        def _edit_spec(spec: CameraFileSpec | None, on_spec_save):
            spec_win = tk.Toplevel(top)
            spec_win.title("Edit file" if spec else "Add file")

            tk.Label(spec_win, text="Keyword:").grid(row=0, column=0, sticky="w", padx=10, pady=5)
            kw_var = tk.StringVar(value=spec.keyword if spec else "")
            tk.Entry(spec_win, textvariable=kw_var).grid(row=0, column=1, padx=10, pady=5)

            tk.Label(spec_win, text="Extension:").grid(row=1, column=0, sticky="w", padx=10, pady=5)
            ext_var = tk.StringVar(value=spec.ext if spec else "")
            tk.Entry(spec_win, textvariable=ext_var).grid(row=1, column=1, padx=10, pady=5)

            def on_ok():
                on_spec_save(CameraFileSpec(keyword=kw_var.get(), ext=ext_var.get()))
                spec_win.destroy()

            tk.Button(spec_win, text="OK", command=on_ok).grid(row=2, column=0, padx=10, pady=10)
            tk.Button(spec_win, text="Cancel", command=spec_win.destroy).grid(row=2, column=1, padx=10, pady=10)

        def on_add_spec():
            _edit_spec(None, lambda new_spec: (specs.append(new_spec), refresh_specs()))

        def on_edit_spec():
            sel = listbox.curselection()
            if not sel:
                return
            idx = sel[0]
            _edit_spec(specs[idx], lambda updated: (specs.__setitem__(idx, updated), refresh_specs()))

        def on_remove_spec():
            sel = listbox.curselection()
            if not sel:
                return
            idx = sel[0]
            specs.pop(idx)
            refresh_specs()

        btn_spec = tk.Frame(top)
        btn_spec.grid(row=2, column=1, sticky="w", padx=10, pady=5)
        tk.Button(btn_spec, text="Add file", command=on_add_spec).grid(row=0, column=0, padx=5)
        tk.Button(btn_spec, text="Edit file", command=on_edit_spec).grid(row=0, column=1, padx=5)
        tk.Button(btn_spec, text="Remove file", command=on_remove_spec).grid(row=0, column=2, padx=5)

        def on_ok():
            name = name_var.get().strip()
            if not name:
                messagebox.showerror("Error", "Camera name cannot be empty.")
                return
            on_save(CameraConfig(name=name, specs=list(specs)))
            top.destroy()

        def on_cancel():
            top.destroy()

        btn_ok_cancel = tk.Frame(top)
        btn_ok_cancel.grid(row=3, column=1, sticky="e", padx=10, pady=10)
        tk.Button(btn_ok_cancel, text="OK", command=on_ok).grid(row=0, column=0, padx=5)
        tk.Button(btn_ok_cancel, text="Cancel", command=on_cancel).grid(row=0, column=1, padx=5)

        top.columnconfigure(1, weight=1)
        refresh_specs()

    def _save_config(self):
        path = filedialog.asksaveasfilename(
            title="Save simulator config",
            defaultextension=".json",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")]
        )
        if not path:
            return
        cfg = self.sim.to_config_dict()
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=2)
            messagebox.showinfo("Config saved", f"Configuration saved to:\n{path}")
        except Exception as e:
            messagebox.showerror("Error", f"Could not save config:\n{e}")

    def _load_config(self):
        path = filedialog.askopenfilename(
            title="Load simulator config",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")]
        )
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            self.sim.load_from_config_dict(cfg)
            self.project_var.set(str(self.sim.project_root))
            self.lbl_paths.config(text=self._format_paths_text())
            self.delay_var.set(str(self.sim.max_delay_sec))
            self.shot_var.set(f"Next shot index: {self.sim.shot_index}")
            self._refresh_camera_label()
            messagebox.showinfo("Config loaded", f"Configuration loaded from:\n{path}")
        except Exception as e:
            messagebox.showerror("Error", f"Could not load config:\n{e}")


# =========================
# ======== MAIN ========== #
# =========================

def main():
    sim = FakeShotSimulator()
    root = tk.Tk()
    gui = SimulatorGUI(root, sim)
    root.mainloop()


if __name__ == "__main__":
    main()
