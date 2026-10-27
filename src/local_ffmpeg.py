"""Resolve an existing local FFmpeg installation without downloading a runtime."""
from __future__ import annotations

import os
from pathlib import Path
import shutil


def resolve_ffmpeg(configured: str = "", excluded_directories=()) -> Path:
    executable = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    if configured.strip():
        path = Path(os.path.expandvars(configured.strip().strip('"'))).expanduser()
        if path.is_dir(): path /= executable
        if not path.is_file():
            raise ValueError("所选 FFmpeg 路径不存在，请在设置中重新选择 ffmpeg.exe。")
        return path.resolve()
    excluded = {Path(p).resolve() for p in excluded_directories}
    paths = [p for p in os.get_exec_path() if Path(p or os.curdir).resolve() not in excluded]
    found = shutil.which(executable, path=os.pathsep.join(paths))
    if found:
        return Path(found).resolve()
    # Common Windows installations also work before Explorer picks up a new PATH.
    candidates = []
    if os.name == "nt":
        for base in (Path("C:/ffmpeg/bin"), Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "ffmpeg/bin",
                     Path.home() / "scoop/apps/ffmpeg/current/bin"):
            candidates.append(base / executable)
        local = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
        candidates.append(local / "Microsoft/WinGet/Links" / executable)
        packages = local / "Microsoft/WinGet/Packages"
        if packages.is_dir():
            for package in packages.glob("*FFmpeg*"):
                candidates.extend(package.glob("*/bin/ffmpeg.exe"))
    for path in candidates:
        if path.is_file() and path.parent.resolve() not in excluded:
            return path.resolve()
    raise ValueError("未找到本机 FFmpeg。请先安装 FFmpeg 并加入 PATH，或在「设置 → 本机 FFmpeg」选择 ffmpeg.exe。")
