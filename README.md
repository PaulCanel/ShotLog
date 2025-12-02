# ShotLog
Creation and visualisation of logs for laser shooting experiments.

## Folder configuration

The GUI lets you manage cameras/sensors dynamically via **Folder list...** (per-folder expected/trigger flags and file definitions) and export/import the setup with **Save config...** / **Load config...**. See `CONFIG.md` for the JSON format and `Algo.md` for the core shot timing rules.

## Motor data correlation

Provide the initial motor positions CSV and the motor history CSV in the **Motor data** panel to record motor positions at every shot trigger. The positions for each shot are stored in `motor_positions_by_shot.csv` (configurable). Use **Recompute all motor positions** after the history file finishes syncing to refresh the output for all logged shots.

## Manual parameters per shot

Use **Manual parameters...** to define a list of free-text parameters (for example notes or environmental conditions). The names are saved in the configuration file together with the optional output path for `manual_params_by_shot.csv` (relative paths are resolved from the project root).

The **Manual parameters (per shot)** panel builds one entry per defined parameter. After a shot completes, the panel automatically targets that shot; when the following shot finishes, the current values are written to the CSV alongside `shot_number` and `trigger_time`, the fields are cleared, and the panel switches to the new shot.
