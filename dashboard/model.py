from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
import json
from pathlib import Path
import queue
from typing import Dict, List

from shot_log.config import ShotLogConfig
from shot_log.manual_params import ManualParamsManager, build_empty_manual_values
from shot_log.manager import ShotManager
from shot_log.motors import MotorStateManager
from shot_log.utils import format_dt_for_name


@dataclass
class LastShotSummary:
    date_str: str
    shot_index: int
    trigger_time: datetime | None
    trigger_camera: str | None
    status: str
    present_cameras: List[str]
    missing_cameras: List[str]
    clean_files: Dict[str, Path]
    manual_params: Dict[str, str]
    motor_positions: Dict[str, float | None]


class DashboardShotManager(ShotManager):
    """ShotManager subclass that keeps an in-memory history of completed shots."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.completed_shots: list[dict] = []

    def _close_shot(self, shot: dict):
        super()._close_shot(shot)
        images_by_camera = shot.get("images_by_camera", {})
        expected = list(self._ensure_expected_cameras())
        missing = [cam for cam in expected if cam not in images_by_camera]
        snapshot = {
            "date_str": shot.get("date_str"),
            "shot_index": shot.get("shot_index"),
            "trigger_time": shot.get("trigger_time"),
            "trigger_camera": shot.get("trigger_camera"),
            "images_by_camera": {cam: info.copy() for cam, info in images_by_camera.items()},
            "missing_cameras": missing,
        }
        self.completed_shots.append(snapshot)


class DashboardShotStore:
    def __init__(self, config_path: Path | None = None, *, root_path: Path | None = None):
        self.gui_queue: queue.Queue[str] = queue.Queue()
        self.current_config = self._load_config(config_path)
        self.shot_manager: DashboardShotManager | None = None
        self.manual_params_manager = ManualParamsManager(
            self.current_config.manual_params,
            self._get_manual_params_output_path,
            log_fn=self._enqueue_log,
        )
        self.motor_state_manager: MotorStateManager | None = None
        self.config_flags: dict[str, bool] = {}
        self.config_ready: bool = False
        self._validate_config()

    def _enqueue_log(self, message: str) -> None:
        self.gui_queue.put(message)

    def _load_config(self, config_path: Path | None) -> ShotLogConfig:
        if config_path is None:
            return ShotLogConfig(
                project_root="",
                raw_root_suffix="",
                clean_root_suffix="",
                rename_log_folder_suffix="",
                state_file="",
                full_window_s=10.0,
                timeout_s=20.0,
                global_trigger_keyword="",
                apply_global_keyword_to_all=False,
                test_keywords=[],
                check_interval_s=0.5,
                motor_initial_csv="",
                motor_history_csv="",
                motor_positions_output="",
                use_default_motor_positions_path=False,
                manual_params=[],
                manual_params_csv_path=None,
                use_default_manual_params_path=False,
                manual_date_override=None,
                folders={},
            )
        raw = Path(config_path)
        data = json.loads(raw.read_text(encoding="utf-8"))
        return ShotLogConfig.from_dict(data)

    def _validate_config(self) -> None:
        cfg = self.current_config

        def is_nonempty(value: str | None) -> bool:
            return bool(value and value.strip())

        flags = {
            "project_root": is_nonempty(cfg.project_root),
            "raw_root": is_nonempty(cfg.raw_root_suffix),
            "clean_root": is_nonempty(cfg.clean_root_suffix),
            "log_folder": is_nonempty(cfg.rename_log_folder_suffix),
            "folders": bool(cfg.folders),
            "timing": (cfg.full_window_s > 0 and cfg.timeout_s > 0),
        }
        self.config_flags = flags
        self.config_ready = all(flags.values())

    def _get_manual_params_output_path(self) -> Path | None:
        if not self.shot_manager:
            return None
        return getattr(self.shot_manager, "manual_params_csv_path", None)

    def reset_shot_manager(
        self,
        *,
        root_path: str | Path,
        config: ShotLogConfig,
        manual_date_str: str | None,
    ) -> None:
        self.current_config = config.clone()
        self._validate_config()
        self.shot_manager = DashboardShotManager(
            str(root_path),
            self.current_config,
            self.gui_queue,
            manual_date_str=manual_date_str,
        )
        if manual_date_str:
            self.shot_manager.set_manual_date(manual_date_str)
        self.manual_params_manager.update_manual_params(self.current_config.manual_params)
        self.motor_state_manager = self.shot_manager.motor_state_manager

    def update_config(self, config: ShotLogConfig) -> None:
        self.current_config = config.clone()
        self._validate_config()
        if self.config_ready and self.shot_manager is None:
            base_root = (
                Path(self.current_config.project_root)
                if self.current_config.project_root
                else Path.cwd()
            )
            self.reset_shot_manager(
                root_path=base_root,
                config=self.current_config,
                manual_date_str=self.current_config.manual_date_override,
            )
            return
        if self.shot_manager is not None:
            self.shot_manager.update_config(self.current_config)
            self.motor_state_manager = self.shot_manager.motor_state_manager
        self.manual_params_manager.update_manual_params(self.current_config.manual_params)

    def start_acquisition(self) -> None:
        if self.shot_manager:
            self.shot_manager.start()

    def pause_acquisition(self) -> None:
        if self.shot_manager:
            self.shot_manager.pause()

    def resume_acquisition(self) -> None:
        if self.shot_manager:
            self.shot_manager.resume()

    def stop_acquisition(self) -> None:
        if self.shot_manager:
            self.shot_manager.stop()

    def get_status(self) -> dict:
        if not self.config_ready:
            return {
                "system_status": "-",
                "config_ready": False,
                "open_shots_count": 0,
                "last_shot_date": None,
                "last_shot_index": None,
                "last_shot_trigger_time": None,
                "next_shot_number": None,
                "last_completed_shot_index": None,
                "last_completed_shot_date": None,
                "last_completed_trigger_time": None,
                "active_date_str": None,
                "manual_date_str": self.current_config.manual_date_override,
                "last_shot_state": None,
                "current_shot_state": None,
                "full_window": self.current_config.full_window_s,
                "timeout": self.current_config.timeout_s,
                "current_keyword": self.current_config.global_trigger_keyword,
            }
        if self.shot_manager is None:
            return {
                "system_status": "IDLE",
                "config_ready": True,
                "open_shots_count": 0,
                "last_shot_date": None,
                "last_shot_index": None,
                "last_shot_trigger_time": None,
                "next_shot_number": None,
                "last_completed_shot_index": None,
                "last_completed_shot_date": None,
                "last_completed_trigger_time": None,
                "active_date_str": None,
                "manual_date_str": self.current_config.manual_date_override,
                "last_shot_state": None,
                "current_shot_state": None,
                "full_window": self.current_config.full_window_s,
                "timeout": self.current_config.timeout_s,
                "current_keyword": self.current_config.global_trigger_keyword,
            }
        status = self.shot_manager.get_status()
        status["config_ready"] = True
        return status

    def get_last_shot_summary(self) -> LastShotSummary | None:
        if not self.shot_manager:
            return None
        snapshot = self.shot_manager.completed_shots[-1] if self.shot_manager.completed_shots else None
        if not snapshot:
            return None
        date_str = snapshot.get("date_str")
        shot_index = snapshot.get("shot_index")
        if date_str is None or shot_index is None:
            return None
        trigger_time = snapshot.get("trigger_time")
        trigger_camera = snapshot.get("trigger_camera")
        images_by_camera = snapshot.get("images_by_camera", {})
        missing = list(snapshot.get("missing_cameras", []))
        present = sorted(images_by_camera.keys())
        status = "missing" if missing else "ok"
        clean_files = self._build_clean_paths(date_str, shot_index, images_by_camera)
        manual_params = self._manual_values_for_shot(date_str, shot_index)
        motor_positions = self._motor_positions_for_time(trigger_time)
        return LastShotSummary(
            date_str=date_str,
            shot_index=shot_index,
            trigger_time=trigger_time,
            trigger_camera=trigger_camera,
            status=status,
            present_cameras=present,
            missing_cameras=missing,
            clean_files=clean_files,
            manual_params=manual_params,
            motor_positions=motor_positions,
        )

    def list_shots_for_date(self, target_date: date) -> list[LastShotSummary]:
        if not self.shot_manager:
            return []
        date_str = target_date.strftime("%Y%m%d")
        results: list[LastShotSummary] = []
        for snapshot in self.shot_manager.completed_shots:
            if snapshot.get("date_str") != date_str:
                continue
            shot_index = snapshot.get("shot_index")
            if shot_index is None:
                continue
            trigger_time = snapshot.get("trigger_time")
            trigger_camera = snapshot.get("trigger_camera")
            images_by_camera = snapshot.get("images_by_camera", {})
            missing = list(snapshot.get("missing_cameras", []))
            present = sorted(images_by_camera.keys())
            status = "missing" if missing else "ok"
            clean_files = self._build_clean_paths(date_str, shot_index, images_by_camera)
            manual_params = self._manual_values_for_shot(date_str, shot_index)
            motor_positions = self._motor_positions_for_time(trigger_time)
            results.append(
                LastShotSummary(
                    date_str=date_str,
                    shot_index=shot_index,
                    trigger_time=trigger_time,
                    trigger_camera=trigger_camera,
                    status=status,
                    present_cameras=present,
                    missing_cameras=missing,
                    clean_files=clean_files,
                    manual_params=manual_params,
                    motor_positions=motor_positions,
                )
            )
        return results

    def poll_gui_queue(self) -> list[str]:
        messages: list[str] = []
        while True:
            try:
                messages.append(self.gui_queue.get_nowait())
            except queue.Empty:
                break
        return messages

    def _build_clean_paths(
        self,
        date_str: str,
        shot_index: int,
        images_by_camera: Dict[str, dict],
    ) -> Dict[str, Path]:
        clean_root = getattr(self.shot_manager, "clean_root", None)
        if clean_root is None:
            return {}
        paths: Dict[str, Path] = {}
        for camera, info in images_by_camera.items():
            dt = info.get("dt")
            if not isinstance(dt, datetime):
                continue
            date_out, time_out = format_dt_for_name(dt)
            ext = Path(info.get("path", "")).suffix or ".dat"
            dest_dir = Path(clean_root) / camera / date_out
            dest_name = f"{camera}_{date_out}_{time_out}_shot{shot_index:03d}{ext.lower()}"
            paths[camera] = dest_dir / dest_name
        return paths

    def _manual_values_for_shot(self, date_str: str, shot_index: int) -> Dict[str, str]:
        manager = self.manual_params_manager
        if not manager.manual_params:
            return {}
        values: list[str]
        if manager.pending_date_str == date_str and manager.pending_shot_index == shot_index:
            values = list(manager.pending_values)
        elif manager.current_date_str == date_str and manager.current_shot_index == shot_index:
            values = list(manager.current_confirmed_values)
        else:
            return build_empty_manual_values(manager.manual_params)
        return {
            name: values[idx] if idx < len(values) else ""
            for idx, name in enumerate(manager.param_names)
        }

    def _motor_positions_for_time(self, trigger_time: datetime | None) -> Dict[str, float | None]:
        if not isinstance(trigger_time, datetime):
            return {}
        if not self.shot_manager:
            return {}
        manager = self.shot_manager.motor_state_manager
        if not manager:
            return {}
        return manager.get_positions_at(trigger_time)


__all__ = ["DashboardShotStore", "LastShotSummary"]
