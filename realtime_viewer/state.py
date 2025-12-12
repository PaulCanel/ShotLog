from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json


SETTINGS_PATH = Path("realtime_viewer/settings.json")
DEFAULT_POINTING_DISTANCE_MM = 1000.0


@dataclass
class RuntimeState:
    """Runtime configuration for the realtime viewer.

    The pointing distance is stored in millimeters to match the UI, with a
    convenience property exposing the distance in meters for calculations.
    """

    pointing_distance_mm: float = DEFAULT_POINTING_DISTANCE_MM

    @property
    def pointing_distance_m(self) -> float:
        return self.pointing_distance_mm / 1000.0

    @classmethod
    def load(cls, path: Path = SETTINGS_PATH) -> "RuntimeState":
        """Load persisted settings or return defaults."""
        if path.exists():
            try:
                with path.open("r", encoding="utf-8") as f:
                    data = json.load(f)
                distance = float(data.get("pointing_distance_mm", DEFAULT_POINTING_DISTANCE_MM))
                return cls(pointing_distance_mm=distance)
            except (json.JSONDecodeError, OSError, ValueError):
                pass
        return cls()

    def save(self, path: Path = SETTINGS_PATH) -> None:
        """Persist runtime settings to disk."""
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            json.dump({"pointing_distance_mm": self.pointing_distance_mm}, f, indent=2)
