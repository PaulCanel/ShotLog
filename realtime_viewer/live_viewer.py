from __future__ import annotations

from pathlib import Path
from typing import Optional

from PyQt5 import QtWidgets

from .state import RuntimeState, SETTINGS_PATH


class LiveViewerMainWindow(QtWidgets.QMainWindow):
    """Main window for the realtime viewer.

    The UI exposes a pointing distance parameter used to convert detected
    lanex positions into pointing angles. Changing the distance immediately
    recomputes theta for the current shot and updates the plotted overlays.
    """

    def __init__(self, state: Optional[RuntimeState] = None, parent=None):
        super().__init__(parent)
        self.state = state or RuntimeState.load()
        self._last_y_cm: float | None = None
        self._last_y0_cm: float = 0.0
        self._theta_label: QtWidgets.QLabel | None = None
        self._distance_label: QtWidgets.QLabel | None = None
        self.distance_input: QtWidgets.QDoubleSpinBox | None = None

        self.setWindowTitle("Realtime Viewer")
        self._build_ui()
        self._update_distance_label()
        self._update_theta_display(0.0)

    # -----------------------------
    # UI BUILDING
    # -----------------------------
    def _build_ui(self) -> None:
        central = QtWidgets.QWidget(self)
        layout = QtWidgets.QVBoxLayout(central)

        pointing_box = QtWidgets.QGroupBox("Pointing")
        pointing_layout = QtWidgets.QGridLayout(pointing_box)

        pointing_layout.addWidget(QtWidgets.QLabel("Pointing distance D [mm]"), 0, 0)
        distance_input = QtWidgets.QDoubleSpinBox()
        distance_input.setMinimum(1)
        distance_input.setMaximum(100000)
        distance_input.setSingleStep(10)
        distance_input.setDecimals(2)
        distance_input.setValue(self.state.pointing_distance_mm)
        distance_input.valueChanged.connect(self._on_distance_changed)
        pointing_layout.addWidget(distance_input, 0, 1)
        self.distance_input = distance_input

        self._theta_label = QtWidgets.QLabel("θ = 0.0 mrad")
        pointing_layout.addWidget(self._theta_label, 1, 0, 1, 2)

        self._distance_label = QtWidgets.QLabel()
        pointing_layout.addWidget(self._distance_label, 2, 0, 1, 2)

        layout.addWidget(pointing_box)

        self.setCentralWidget(central)

    # -----------------------------
    # STATE & COMPUTATION
    # -----------------------------
    def set_pointing_measurement(self, y_cm: float, y0_cm: float = 0.0) -> None:
        """Set the detected spot position and recompute theta.

        Args:
            y_cm: Physical coordinate on the lanex image in centimeters.
            y0_cm: Reference coordinate (e.g., image center) in centimeters.
        """

        self._last_y_cm = y_cm
        self._last_y0_cm = y0_cm
        self._recompute_pointing()

    def _on_distance_changed(self, value: float) -> None:
        self.state.pointing_distance_mm = float(value)
        self.state.save()
        self._update_distance_label()
        self._recompute_pointing()

    def _recompute_pointing(self) -> None:
        theta_mrad = 0.0
        if self._last_y_cm is not None:
            y_m = (self._last_y_cm - self._last_y0_cm) * 1e-2
            D_m = self.state.pointing_distance_m
            theta_mrad = (y_m / D_m) * 1e3 if D_m != 0 else 0.0
        self._update_theta_display(theta_mrad)
        self._replot_pointing(theta_mrad)
        self._replot_energy_grids(theta_mrad)

    def _update_theta_display(self, theta_mrad: float) -> None:
        if self._theta_label:
            self._theta_label.setText(f"θ = {theta_mrad:.3f} mrad")

    def _update_distance_label(self) -> None:
        if self._distance_label:
            self._distance_label.setText(f"D = {self.state.pointing_distance_mm:.1f} mm")

    # -----------------------------
    # PLACEHOLDERS FOR GRAPHICS
    # -----------------------------
    def _replot_pointing(self, theta_mrad: float) -> None:
        """Placeholder for replotting the pointing overlay."""
        # In the full application this would trigger the pointing overlay
        # refresh. Here it is intentionally left as a stub.
        pass

    def _replot_energy_grids(self, theta_mrad: float) -> None:
        """Placeholder for replotting the energy grid overlays."""
        # In the full application this would re-render the lanex energy grids
        # using the updated theta. Here it is intentionally left as a stub.
        pass


def load_window(settings_path: Path | None = None) -> LiveViewerMainWindow:
    """Convenience helper to create the window using persisted settings."""
    state = RuntimeState.load(settings_path or SETTINGS_PATH)
    return LiveViewerMainWindow(state=state)
