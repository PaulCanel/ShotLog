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

        self.cameras: list[CameraConfig] = []
        for cam_name, spec_list in CAMERA_CONFIG.items():
            specs = [CameraFileSpec(keyword=s["keyword"], ext=s["ext"]) for s in spec_list]
            self.cameras.append(CameraConfig(name=cam_name, specs=specs))

        # Dictionnaire Axis -> position courante (pour les moteurs)
        self.motor_positions = {}
        self._load_initial_motor_positions()

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
        init_path = self.motor_root / INITIAL_MOTOR_FILE
        if not init_path.exists():
            print(f"[SIM] No initial motor file found at {init_path}, starting with empty positions.")
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
        self._ensure_dir(self.motor_root)
        hist_path = self._motor_history_path_for_today()
        file_exists = hist_path.exists()

        # Choisir un axis existant ou en créer un nouveau par défaut
        available_axes = list(self.motor_positions.keys())
        if not available_axes:
            axis = "1"  # axis par défaut
        else:
            axis = random.choice(available_axes)

        old_pos = self.motor_positions.get(axis, 0.0)
        # On fait un petit delta aléatoire
        delta = random.uniform(-5.0, 5.0)
        new_pos = old_pos + delta
        self.motor_positions[axis] = new_pos

        time_str = self._now_time_str()

        # Append dans le CSV
        with hist_path.open("a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow(["Time", "Axis", "Old Position", "New Position"])
            writer.writerow([time_str, axis, f"{old_pos:.3f}", f"{new_pos:.3f}"])

        print(f"[SIM] Motor event: time={time_str}, axis={axis}, "
              f"old={old_pos:.3f}, new={new_pos:.3f}, file={hist_path}")

    # ---------- génération de fichiers pour un shot ----------

    def _generate_file_for_camera(self, cam: CameraConfig, spec: CameraFileSpec, shot_time: datetime):
        """
        Génère un fichier 'image' artificiel pour une caméra donnée, avec :
          - un délai aléatoire autour du shot_time
          - un nom contenant la date, l'heure, le keyword et le numéro de shot
          - l'extension spécifiée (spec.ext)
        On se fiche du contenu : un petit header texte suffit.
        """
        # Délai aléatoire
        offset_sec = random.uniform(DELAY_MIN_SEC, DELAY_MAX_SEC)
        file_time = shot_time + timedelta(seconds=offset_sec)

        date_str = self._date_str()
        time_str = self._time_str(file_time)

        # Nom de fichier de type :   <Cam>_<keyword>_<YYYYMMDD>_<HHMMSS>_shotXXXX.ext
        # (tu peux adapter ce pattern si tu veux coller plus précisément au naming ELI)
        shot_str = f"{self.shot_index:04d}"
        keyword_part = f"_{spec.keyword}" if spec.keyword else ""
        filename = f"{cam.name}{keyword_part}_{date_str}_{time_str}_shot{shot_str}{spec.ext}"

        # Dossier RAW : RAW_ROOT / Cam / YYYYMMDD
        cam_dir = self.raw_root / cam.name / date_str
        self._ensure_dir(cam_dir)
        file_path = cam_dir / filename

        # Contenu minimaliste
        content = (
            f"Fake image file for ShotLog simulation\n"
            f"Camera: {cam.name}\n"
            f"Shot index: {self.shot_index}\n"
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
        - Pour chaque caméra, pour chaque spec, on génère un fichier
          avec un offset aléatoire autour de shot_time.
        """
        self._ensure_dir(self.raw_root)
        shot_time = datetime.now()
        print(f"[SIM] Generating shot {self.shot_index} at logical time {shot_time}")

        for cam in self.cameras:
            for spec in cam.specs:
                self._generate_file_for_camera(cam, spec, shot_time)

        print(f"[SIM] Shot {self.shot_index} generated.\n")
        self.shot_index += 1


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

        frm_root = tk.Frame(root)
        frm_root.pack(padx=10, pady=10, fill="x")

        tk.Label(frm_root, text="Project root:").grid(row=0, column=0, sticky="w")
        self.entry_root = tk.Entry(frm_root, textvariable=self.project_var, width=60)
        self.entry_root.grid(row=0, column=1, sticky="we", padx=5)
        btn_browse = tk.Button(frm_root, text="Browse...", command=self._browse_root)
        btn_browse.grid(row=0, column=2, padx=5)

        frm_root.columnconfigure(1, weight=1)

        # Info caméra
        camera_names = ", ".join(cam.name for cam in self.sim.cameras)
        tk.Label(root, text=f"Cameras: {camera_names}").pack(padx=10, pady=(0, 5), anchor="w")

        # Info RAW / MOTOR
        self.lbl_paths = tk.Label(
            root,
            text=self._format_paths_text(),
            justify="left"
        )
        self.lbl_paths.pack(padx=10, pady=(0, 10), anchor="w")

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
