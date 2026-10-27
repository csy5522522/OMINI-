"""Finish an already-built toolbox distribution and create its portable ZIP."""
from pathlib import Path
import hashlib
import json
import shutil
import sys
import zipfile


def package():
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    sys.path.insert(0, str(root / "src"))
    from app_version import APP_VERSION
    version = sys.argv[1] if len(sys.argv) > 1 else APP_VERSION
    if version != APP_VERSION:
        raise ValueError("Package version must match app_version.py")
    if not all(part.isdigit() for part in version.split(".")):
        raise ValueError("Version should contain dot-separated numbers")
    release_root = root / "artifacts/releases" / version
    release = release_root / "Omini工具箱"
    executable = release / "Omini工具箱.exe"
    if not executable.is_file():
        raise RuntimeError("Build the Windows application before packaging")
    from release_packaging import copy_release_tools
    copy_release_tools(root / "tools", release / "tools")
    shutil.copyfile(root / "README.md", release / "README.md")
    shutil.copyfile(root / "installer/安装版使用说明.txt", release / "使用说明.txt")
    archive = release_root / f"Omini工具箱-{version}-便携版.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=3) as zipped:
        for path in sorted(release.rglob("*")):
            if path.is_file():
                zipped.write(path, path.relative_to(release_root))
    with archive.open("rb") as stream:
        checksum = hashlib.file_digest(stream, "sha256").hexdigest()
    (archive.with_suffix(".zip.sha256")).write_text(checksum + "  " + archive.name + "\n", encoding="utf-8")
    report = {"version": version, "zip": str(archive), "bytes": archive.stat().st_size,
              "sha256": checksum, "ai_engine_included": (release / "tools/cli-proxy-api.exe").is_file()}
    (root / "artifacts/verification/portable-package.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    package()
