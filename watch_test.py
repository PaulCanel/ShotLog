#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import time
import os
from datetime import datetime

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler


class CreationEventHandler(FileSystemEventHandler):
    """
    Handler personnalisé qui réagit à la création de fichiers/dossiers.
    """

    def on_created(self, event):
        # event.src_path : chemin complet de ce qui a été créé
        # event.is_directory : True si dossier, False si fichier

        # Timestamp au moment de la détection
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # Type d’élément
        element_type = "Dossier" if event.is_directory else "Fichier"

        # Normaliser le chemin
        path = os.path.abspath(event.src_path)

        print(f"[{now}] Création détectée → {element_type} : {path}")


def main(path_to_watch):
    # Vérifier que le dossier existe
    if not os.path.isdir(path_to_watch):
        raise NotADirectoryError(f"Le chemin spécifié n'est pas un dossier : {path_to_watch}")

    event_handler = CreationEventHandler()
    observer = Observer()

    # recursive=True ⇒ surveille tous les sous-dossiers
    observer.schedule(event_handler, path=path_to_watch, recursive=True)
    observer.start()

    print(f"Surveillance démarrée sur : {os.path.abspath(path_to_watch)}")
    print("Appuyez sur Ctrl+C pour arrêter.\n")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nArrêt de la surveillance…")
        observer.stop()

    observer.join()


if __name__ == "__main__":
    # À adapter : mettre ici le chemin du dossier à surveiller
    FOLDER_TO_WATCH = r"test_root/ELI50069_RAW_DATA"  # exemple: r"C:\Users\toi\Desktop" ou "/home/toi/Documents"

    main(FOLDER_TO_WATCH)
