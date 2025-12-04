"""Configuration models for ShotLog.

The JSON produced by :func:`ShotLogConfig.to_dict` has a flat structure
containing root suffixes, timing parameters, keyword options and a
"folders" array. Each folder entry includes its name, the "expected" and
"trigger" flags plus a list of ``file_specs`` with ``keyword`` and
``extensions`` fields. The format is intentionally simple to allow manual
editing when needed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List


@dataclass
class ManualParam:
    name: str
    type: str = "text"

    def to_dict(self) -> dict:
        return {"name": self.name, "type": self.type or "text"}

    @classmethod
    def from_raw(cls, raw) -> "ManualParam | None":
        if isinstance(raw, cls):
            return raw
        if isinstance(raw, dict):
            name = str(raw.get("name", "")).strip()
            if not name:
                return None
            param_type = str(raw.get("type", "text")).strip().lower() or "text"
            if param_type not in {"text", "number"}:
                param_type = "text"
            return cls(name=name, type=param_type)
        name = str(raw).strip()
        if not name:
            return None
        return cls(name=name, type="text")



def _normalize_extension(ext: str) -> str:
    ext = ext.strip().lower()
    if ext and not ext.startswith("."):
        ext = "." + ext
    return ext


def _parse_extensions_field(raw_ext_field):
    """Return a normalized list of extensions from a string or list input."""

    if raw_ext_field is None:
        return []

    parts: List[str]
    if isinstance(raw_ext_field, list):
        parts = [str(x) for x in raw_ext_field]
    else:
        parts = str(raw_ext_field).split(",")

    extensions: List[str] = []
    for part in parts:
        normalized = _normalize_extension(part)
        if normalized:
            extensions.append(normalized)
    return extensions


@dataclass
class FolderFileSpec:
    keyword: str = ""
    extensions: List[str] = field(default_factory=list)

    @property
    def normalized_extensions(self) -> List[str]:
        return [_normalize_extension(ext) for ext in self.extensions if _normalize_extension(ext)]

    @property
    def normalized_extension(self) -> str:
        """Backwards-compatible single extension (first in list or empty)."""

        exts = self.normalized_extensions
        return exts[0] if exts else ""

    def matches(self, filename_lower: str, *, global_keyword: str, apply_global_keyword: bool) -> bool:
        """
        Return True if the filename matches this spec. The check is case
        insensitive and optionally enforces the global keyword when the
        configuration requires it.
        """

        if apply_global_keyword and global_keyword:
            if global_keyword.lower() not in filename_lower:
                return False
        if self.keyword and self.keyword.lower() not in filename_lower:
            return False

        extensions = self.normalized_extensions
        if not extensions:
            return True
        return any(filename_lower.endswith(ext) for ext in extensions)

    def to_dict(self) -> dict:
        extensions = self.normalized_extensions
        return {
            "keyword": self.keyword,
            "extensions": extensions,
            "extension": extensions[0] if extensions else "",
        }

    @classmethod
    def from_dict(cls, data: dict) -> "FolderFileSpec":
        extensions = _parse_extensions_field(data.get("extensions"))
        if not extensions and "extension" in data:
            extensions = _parse_extensions_field(data.get("extension"))
        return cls(keyword=data.get("keyword", ""), extensions=extensions)


@dataclass
class FolderConfig:
    name: str
    expected: bool = True
    trigger: bool = False
    file_specs: List[FolderFileSpec] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "expected": self.expected,
            "trigger": self.trigger,
            "file_specs": [spec.to_dict() for spec in self.file_specs],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "FolderConfig":
        specs = [FolderFileSpec.from_dict(item) for item in data.get("file_specs", [])]
        return cls(
            name=data.get("name", ""),
            expected=bool(data.get("expected", False)),
            trigger=bool(data.get("trigger", False)),
            file_specs=specs or [FolderFileSpec()],
        )

    def matches(self, filename_lower: str, *, global_keyword: str, apply_global_keyword: bool) -> bool:
        return any(
            spec.matches(
                filename_lower,
                global_keyword=global_keyword,
                apply_global_keyword=apply_global_keyword,
            )
            for spec in self.file_specs
        )


@dataclass
class ShotLogConfig:
    project_root: str | None = None
    raw_root_suffix: str = "ELI50069_RAW_DATA"
    clean_root_suffix: str = "ELI50069_CLEAN_DATA"
    rename_log_folder_suffix: str = "rename_log"
    full_window_s: float = 10.0
    timeout_s: float = 20.0
    global_trigger_keyword: str = "shot"
    apply_global_keyword_to_all: bool = False
    test_keywords: List[str] = field(default_factory=lambda: ["test", "align"])
    state_file: str = "eli50069_state.json"
    check_interval_s: float = 0.5
    motor_initial_csv: str = ""
    motor_history_csv: str = ""
    motor_positions_output: str = "motor_positions_by_shot.csv"
    use_default_motor_positions_path: bool = False
    manual_params: List[ManualParam] = field(default_factory=list)
    manual_params_csv_path: str | None = "manual_params_by_shot.csv"
    use_default_manual_params_path: bool = False
    manual_date_override: str | None = None
    folders: Dict[str, FolderConfig] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "project_root": self.project_root,
            "raw_root_suffix": self.raw_root_suffix,
            "clean_root_suffix": self.clean_root_suffix,
            "rename_log_folder_suffix": self.rename_log_folder_suffix,
            # Backwards compatibility aliases
            "raw_folder_name": self.raw_root_suffix,
            "clean_folder_name": self.clean_root_suffix,
            "log_folder_name": self.rename_log_folder_suffix,
            "full_window_s": self.full_window_s,
            "timeout_s": self.timeout_s,
            "global_trigger_keyword": self.global_trigger_keyword,
            "apply_global_keyword_to_all": self.apply_global_keyword_to_all,
            "test_keywords": list(self.test_keywords),
            "state_file": self.state_file,
            "log_dir": self.rename_log_folder_suffix,
            "check_interval_s": self.check_interval_s,
            "motor_initial_csv": self.motor_initial_csv,
            "motor_history_csv": self.motor_history_csv,
            "motor_positions_output": self.motor_positions_output,
            "use_default_motor_positions_path": self.use_default_motor_positions_path,
            "manual_params": [p.to_dict() for p in self.manual_params],
            "manual_params_csv_path": self.manual_params_csv_path,
            "use_default_manual_params_path": self.use_default_manual_params_path,
            "manual_date_override": self.manual_date_override,
            "folders": [folder.to_dict() for folder in self.folders.values()],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ShotLogConfig":
        folders_data = data.get("folders", [])
        folders: Dict[str, FolderConfig] = {}
        for folder_dict in folders_data:
            folder = FolderConfig.from_dict(folder_dict)
            if folder.name:
                folders[folder.name] = folder
        raw_root_suffix = (
            data.get("raw_root_suffix")
            or data.get("raw_folder_name")
            or "ELI50069_RAW_DATA"
        )
        clean_root_suffix = (
            data.get("clean_root_suffix")
            or data.get("clean_folder_name")
            or "ELI50069_CLEAN_DATA"
        )
        rename_log_folder_suffix = (
            data.get("rename_log_folder_suffix")
            or data.get("log_folder_name")
            or data.get("log_dir")
            or "rename_log"
        )

        raw_manual_params = data.get("manual_params", [])
        manual_params: List[ManualParam] = []
        for item in raw_manual_params:
            param = ManualParam.from_raw(item)
            if param:
                manual_params.append(param)

        cfg = cls(
            project_root=data.get("project_root"),
            raw_root_suffix=raw_root_suffix,
            clean_root_suffix=clean_root_suffix,
            rename_log_folder_suffix=rename_log_folder_suffix,
            full_window_s=float(data.get("full_window_s", 10.0)),
            timeout_s=float(data.get("timeout_s", 20.0)),
            global_trigger_keyword=data.get("global_trigger_keyword", "shot"),
            apply_global_keyword_to_all=bool(data.get("apply_global_keyword_to_all", False)),
            test_keywords=list(data.get("test_keywords", ["test", "align"])),
            state_file=data.get("state_file", "eli50069_state.json"),
            check_interval_s=float(data.get("check_interval_s", 0.5)),
            motor_initial_csv=data.get("motor_initial_csv", ""),
            motor_history_csv=data.get("motor_history_csv", ""),
            motor_positions_output=data.get("motor_positions_output", "motor_positions_by_shot.csv"),
            use_default_motor_positions_path=bool(
                data.get("use_default_motor_positions_path", False)
            ),
            manual_params=manual_params,
            manual_params_csv_path=data.get("manual_params_csv_path", "manual_params_by_shot.csv"),
            use_default_manual_params_path=bool(
                data.get("use_default_manual_params_path", False)
            ),
            manual_date_override=data.get("manual_date_override"),
            folders=folders,
        )
        if not cfg.folders:
            cfg.folders = default_folders()
        return cfg

    def clone(self) -> "ShotLogConfig":
        return ShotLogConfig.from_dict(self.to_dict())

    @property
    def manual_param_names(self) -> List[str]:
        return [p.name for p in self.manual_params]

    @property
    def trigger_folders(self) -> List[str]:
        return [name for name, f in self.folders.items() if f.trigger]

    @property
    def expected_folders(self) -> List[str]:
        return [name for name, f in self.folders.items() if f.expected]

    @property
    def folder_names(self) -> List[str]:
        return list(self.folders.keys())

    # Backwards compatibility aliases
    @property
    def raw_folder_name(self) -> str:
        return self.raw_root_suffix

    @raw_folder_name.setter
    def raw_folder_name(self, value: str) -> None:
        self.raw_root_suffix = value

    @property
    def clean_folder_name(self) -> str:
        return self.clean_root_suffix

    @clean_folder_name.setter
    def clean_folder_name(self, value: str) -> None:
        self.clean_root_suffix = value

    @property
    def log_dir(self) -> str:
        return self.rename_log_folder_suffix

    @log_dir.setter
    def log_dir(self, value: str) -> None:
        self.rename_log_folder_suffix = value

    @property
    def log_folder_name(self) -> str:
        return self.rename_log_folder_suffix

    @log_folder_name.setter
    def log_folder_name(self, value: str) -> None:
        self.rename_log_folder_suffix = value

    def folder_matches(self, folder_name: str, filename_lower: str) -> bool:
        folder = self.folders.get(folder_name)
        if not folder:
            return False
        return folder.matches(
            filename_lower,
            global_keyword=self.global_trigger_keyword,
            apply_global_keyword=self.apply_global_keyword_to_all,
        )

    def is_trigger_file(self, folder_name: str, filename_lower: str) -> bool:
        folder = self.folders.get(folder_name)
        if not folder or not folder.trigger:
            return False
        return folder.matches(
            filename_lower,
            global_keyword=self.global_trigger_keyword,
            apply_global_keyword=self.apply_global_keyword_to_all,
        )

    # ---------------------------------
    # Logging helpers
    # ---------------------------------
    def keyword_log_lines(self) -> List[str]:
        """
        Return human-readable log lines describing the effective keyword
        configuration, including per-folder specs.
        """
        lines: List[str] = []
        use_global = self.apply_global_keyword_to_all and bool(self.global_trigger_keyword)
        lines.append(
            f"Global keyword = '{self.global_trigger_keyword}', apply_to_all = {self.apply_global_keyword_to_all}"
        )
        for folder_name in sorted(self.folders.keys()):
            folder = self.folders[folder_name]
            lines.append(
                f"Folder {folder.name} – expected={folder.expected}, trigger={folder.trigger}"
            )
            if not folder.file_specs:
                lines.append("  (no file specs configured)")
                continue

            for idx, spec in enumerate(folder.file_specs, start=1):
                extensions = spec.normalized_extensions
                if not extensions:
                    ext_desc = "ext='' (no extension filter)"
                elif len(extensions) == 1:
                    ext_desc = f"ext='{extensions[0]}'"
                else:
                    ext_desc = f"ext in {extensions}"
                if use_global:
                    if spec.keyword:
                        kw_desc = (
                            f"keyword='{spec.keyword}' + global='{self.global_trigger_keyword}' enforced"
                        )
                    else:
                        kw_desc = f"keyword='{self.global_trigger_keyword}' (global)"
                else:
                    if spec.keyword:
                        kw_desc = f"keyword='{spec.keyword}'"
                    else:
                        kw_desc = "keyword='' (empty → matches all filenames)"
                lines.append(f"  File spec {idx}: {kw_desc}, {ext_desc}")

        return lines


def default_folders() -> Dict[str, FolderConfig]:
    names = [
        "Lanex1",
        "Lanex2",
        "Lanex3",
        "Lanex4",
        "Lanex5",
        "LanexGamma",
        "Lyso",
        "Csi",
        "DarkShadow",
        "SideView",
        "TopView",
        "FROG",
    ]
    folders: Dict[str, FolderConfig] = {}
    for name in names:
        folders[name] = FolderConfig(
            name=name,
            expected=True,
            trigger=(name in ["Lanex5"]),
            file_specs=[FolderFileSpec(keyword="", extensions=[".tif"])],
        )
    return folders


DEFAULT_CONFIG = ShotLogConfig(folders=default_folders())
