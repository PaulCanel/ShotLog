"""Configuration models for ShotLog.

The JSON produced by :func:`ShotLogConfig.to_dict` has a flat structure
containing root suffixes, timing parameters, keyword options and a
"folders" array. Each folder entry includes its name, the "expected" and
"trigger" flags plus a list of ``file_specs`` with ``keyword`` and
``extension`` fields. The format is intentionally simple to allow manual
editing when needed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List


def _normalize_extension(ext: str) -> str:
    ext = ext.strip().lower()
    if ext and not ext.startswith("."):
        ext = "." + ext
    return ext


@dataclass
class FolderFileSpec:
    keyword: str = ""
    extension: str = ""

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
        ext = _normalize_extension(self.extension)
        if ext and not filename_lower.endswith(ext):
            return False
        return True

    def to_dict(self) -> dict:
        return {"keyword": self.keyword, "extension": self.extension}

    @classmethod
    def from_dict(cls, data: dict) -> "FolderFileSpec":
        return cls(keyword=data.get("keyword", ""), extension=data.get("extension", ""))


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
    raw_root_suffix: str = "ELI50069_RAW_DATA"
    clean_root_suffix: str = "ELI50069_CLEAN_DATA"
    full_window_s: float = 10.0
    timeout_s: float = 20.0
    global_trigger_keyword: str = "shot"
    apply_global_keyword_to_all: bool = False
    test_keywords: List[str] = field(default_factory=lambda: ["test", "align"])
    state_file: str = "eli50069_state.json"
    log_dir: str = "rename_log"
    check_interval_s: float = 0.5
    folders: Dict[str, FolderConfig] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "raw_root_suffix": self.raw_root_suffix,
            "clean_root_suffix": self.clean_root_suffix,
            "full_window_s": self.full_window_s,
            "timeout_s": self.timeout_s,
            "global_trigger_keyword": self.global_trigger_keyword,
            "apply_global_keyword_to_all": self.apply_global_keyword_to_all,
            "test_keywords": list(self.test_keywords),
            "state_file": self.state_file,
            "log_dir": self.log_dir,
            "check_interval_s": self.check_interval_s,
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
        cfg = cls(
            raw_root_suffix=data.get("raw_root_suffix", "ELI50069_RAW_DATA"),
            clean_root_suffix=data.get("clean_root_suffix", "ELI50069_CLEAN_DATA"),
            full_window_s=float(data.get("full_window_s", 10.0)),
            timeout_s=float(data.get("timeout_s", 20.0)),
            global_trigger_keyword=data.get("global_trigger_keyword", "shot"),
            apply_global_keyword_to_all=bool(data.get("apply_global_keyword_to_all", False)),
            test_keywords=list(data.get("test_keywords", ["test", "align"])),
            state_file=data.get("state_file", "eli50069_state.json"),
            log_dir=data.get("log_dir", "rename_log"),
            check_interval_s=float(data.get("check_interval_s", 0.5)),
            folders=folders,
        )
        if not cfg.folders:
            cfg.folders = default_folders()
        return cfg

    def clone(self) -> "ShotLogConfig":
        return ShotLogConfig.from_dict(self.to_dict())

    @property
    def trigger_folders(self) -> List[str]:
        return [name for name, f in self.folders.items() if f.trigger]

    @property
    def expected_folders(self) -> List[str]:
        return [name for name, f in self.folders.items() if f.expected]

    @property
    def folder_names(self) -> List[str]:
        return list(self.folders.keys())

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
        keyword = self.global_trigger_keyword
        if not keyword or keyword.lower() not in filename_lower:
            return False
        return folder.matches(
            filename_lower,
            global_keyword=keyword,
            apply_global_keyword=self.apply_global_keyword_to_all,
        )


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
            file_specs=[FolderFileSpec(keyword="", extension=".tif")],
        )
    return folders


DEFAULT_CONFIG = ShotLogConfig(folders=default_folders())
