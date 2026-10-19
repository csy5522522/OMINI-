"""Finish an already-built toolbox distribution and create its portable ZIP."""
from pathlib import Path
import hashlib
import json
import shutil
import sys
import zipfile


def package():
    root = Path(__file__).resolve().parents[1]
    version = sys.argv[1] if len(sys.argv) > 1 else "1.1.2"
    if not all(part.isdigit() for part in version.split(".")):
        raise ValueError("Version should contain dot-separated numbers")
    release_root = root / ("dist-sync-" + version)
    release = release_root / "Omini工具箱"
    executable = release / "Omini工具箱.exe"
    if not executable.is_file():
        raise RuntimeError("Build the Windows application before packaging")
    shutil.copytree(root / "tools", release / "tools", dirs_exist_ok=True)
    shutil.copyfile(root / "README.md", release / "README.md")
    (release / "文件夹同步使用说明.txt").write_text(
        f"Omini 工具箱 {version} · 多电脑文件夹同步\n\n"
        "1. 完整解压此包，在每台 Windows x64 电脑运行 Omini工具箱.exe。\n"
        "2. 打开侧栏【文件夹同步】，点击【启动同步服务】。\n"
        "3. 两台电脑分别复制自己的设备 ID，在对方【电脑设备 → 添加电脑】中粘贴，互相添加。\n"
        "   已组建 Tailscale 网络时，连接地址直接填写对方的 Tailscale IP 或 MagicDNS 名称。\n"
        "   默认端口 22000 自动补齐，也可读取 Tailscale 设备后自行选择电脑。\n"
        "4. 第一台添加本机文件夹、勾选对方电脑，保存后复制共享 ID。\n"
        "5. 第二台添加自己的目录，填写相同共享 ID，勾选第一台电脑。\n"
        "6. 默认自动同步。也可编辑文件夹，选择手动、间隔定时或每天定时。\n\n"
        "手动/定时任务在所有共享电脑完成后自动暂停；对方也需要启用对应文件夹。\n"
        "如果设备离线或文件夹暂停，任务会等待连接。请在两边保持程序运行。\n"
        "勾选托盘后台后，关闭窗口仍会同步；右键托盘【退出工具箱】结束。\n"
        "双向同步会传播删除；远程覆盖/删除文件的旧版本保存在 .stversions，约保留30天。\n"
        "并发修改保留 sync-conflict 冲突副本。首次同步会合并双方内容。\n\n"
        "每台电脑的身份单独生成。分发时发送完整压缩包，保留 _internal 和 tools 目录。\n"
        "配置保存在 %APPDATA%/Omini工具箱/sync/。\n"
        "更多细节见程序的【使用说明】或 README.md。\n", encoding="utf-8-sig")
    archive = release_root / f"Omini工具箱-{version}-文件夹同步.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=3) as zipped:
        for path in sorted(release.rglob("*")):
            if path.is_file():
                zipped.write(path, path.relative_to(release_root))
    with archive.open("rb") as stream:
        checksum = hashlib.file_digest(stream, "sha256").hexdigest()
    (archive.with_suffix(".zip.sha256")).write_text(checksum + "  " + archive.name + "\n", encoding="utf-8")
    report = {"version": version, "zip": str(archive), "bytes": archive.stat().st_size,
              "sha256": checksum, "engine_included": (release / "tools/syncthing.exe").is_file()}
    (root / "verification/folder-sync-package.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    package()
