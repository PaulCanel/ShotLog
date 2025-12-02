# ShotLog configuration overview

ShotLog uses a JSON-based configuration that can be exported/imported from the GUI. The format matches the data model in `config.py`:

- `raw_root_suffix`, `clean_root_suffix`, `state_file`, `log_dir`, `check_interval_s`: infrastructure paths and watchdog polling interval.
- `full_window_s`, `timeout_s`: timing parameters used by the shot logic (see `Algo.md`).
- `global_trigger_keyword`: required substring for trigger files.
- `apply_global_keyword_to_all`: when `true`, the global keyword must also be present in every file definition match (not only triggers).
- `test_keywords`: filenames containing any of these substrings are ignored.
- `motor_initial_csv`, `motor_history_csv`: absolute or project-relative paths to the motor CSV files (initial positions and movement history).
- `motor_positions_output`: destination CSV (absolute or project-relative) where motor positions will be written per shot.
- `folders`: list of folder definitions, each containing:
  - `name`: folder name as it appears under RAW/CLEAN.
  - `expected`: whether the folder is required for completeness diagnostics.
  - `trigger`: whether files from the folder can start a shot.
  - `file_specs`: list of `{ "keyword": "...", "extension": "..." }` entries. A file matches if it satisfies at least one spec (case-insensitive). An empty keyword matches anything; an empty extension matches all extensions.

The GUI “Save config…”/“Load config…” buttons serialise this structure with indentation so it remains human-editable.
