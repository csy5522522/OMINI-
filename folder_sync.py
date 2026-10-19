"""Managed Syncthing process, folder configuration and serialized sync scheduling.

The engine owns TLS device identities, block transfer, conflict copies and versions.
All methods are called by a single worker; no network request runs on the GUI thread.
"""
from __future__ import annotations

from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import re
import secrets
import socket
import subprocess
import sys
import time
from urllib.parse import quote
import xml.etree.ElementTree as ET

import requests
from tailscale_sync import normalize_addresses

MODES = {"auto": "自动同步", "manual": "手动同步", "interval": "间隔定时", "daily": "每天定时"}
TYPES = {"sendreceive": "双向同步", "sendonly": "仅发送", "receiveonly": "仅接收"}
CREATE_FLAGS = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


class SyncError(RuntimeError):
    pass


def engine_path():
    name = "syncthing.exe" if os.name == "nt" else "syncthing"
    base = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
    path = base / "tools" / name
    if not path.is_file():
        raise SyncError("缺少同步引擎，请保留发布包中的 tools 文件夹。开发环境请运行 python scripts/fetch_syncthing.py。")
    return path


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    with temp.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)


def next_due(policy, now):
    if policy["mode"] == "interval":
        return now + int(policy.get("minutes", 30)) * 60
    if policy["mode"] == "daily":
        hour, minute = map(int, policy.get("time", "20:00").split(":"))
        current = datetime.fromtimestamp(now)
        target = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= current:
            target += timedelta(days=1)
        return target.timestamp()
    return 0


def validate_policy(value):
    mode = value.get("mode", "auto")
    if mode not in MODES:
        raise SyncError("请选择有效的同步模式。")
    minutes = int(value.get("minutes", 30))
    if not 1 <= minutes <= 10080:
        raise SyncError("定时间隔应为 1～10080 分钟。")
    clock = value.get("time", "20:00")
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", clock):
        raise SyncError("每天定时应使用 HH:MM 格式。")
    return {"mode": mode, "minutes": minutes, "time": clock, "suspended": bool(value.get("suspended", False))}


class SyncManager:
    def __init__(self, directory, binary=None, listen_addresses=None, local_only=False):
        self.directory = Path(directory)
        self.home = self.directory / "engine"
        self.settings_path = self.directory / "settings.json"
        self.binary = Path(binary) if binary else None
        self.listen_addresses = listen_addresses
        self.local_only = local_only
        self.settings = {"enabled": False, "background": True, "policies": {}}
        if self.settings_path.exists():
            try:
                loaded = json.loads(self.settings_path.read_text(encoding="utf-8"))
                if not isinstance(loaded, dict) or not isinstance(loaded.get("policies", {}), dict):
                    raise ValueError()
                self.settings.update(loaded)
                for policy in self.settings["policies"].values():
                    validate_policy(policy)
            except (OSError, ValueError, TypeError, KeyError) as error:
                raise SyncError("同步设置读取失败，请检查 sync/settings.json；原文件已保留。") from error
        self.process = None
        self.session = requests.Session()
        self.session.trust_env = False  # Local REST must bypass system/HTTP proxies.
        self.url = ""
        self.device_id = ""
        self.active = {}
        self.messages = {}
        self.lock_file = None
        self.log_stream = None

    @property
    def running(self):
        return self.process is not None and self.process.poll() is None

    def save(self):
        atomic_json(self.settings_path, self.settings)

    def _lock(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        stream = (self.directory / "instance.lock").open("a+b")
        try:
            if (self.directory / "instance.lock").stat().st_size == 0:
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            stream.close()
            raise SyncError("另一个工具箱窗口正在使用同步服务，请先退出那个窗口。") from error
        self.lock_file = stream

    def api(self, method, endpoint, data=None, params=None, timeout=8):
        if not self.url:
            raise SyncError("请先启动同步服务。")
        try:
            response = self.session.request(method, self.url + "/rest/" + endpoint,
                                            json=data, params=params, timeout=timeout)
        except requests.ReadTimeout as error:
            if method == "POST" and endpoint == "db/scan":
                # The scan endpoint waits for indexing. Large folders keep scanning
                # after HTTP times out; observe status rather than submit it again.
                return None
            raise SyncError("同步服务暂未响应，请检查服务状态后重试。") from error
        except requests.RequestException as error:
            raise SyncError("同步服务暂未响应，请检查服务状态后重试。") from error
        if not response.ok:
            detail = response.text.strip()[:400]
            raise SyncError(f"同步服务返回 {response.status_code}：{detail}")
        return response.json() if response.content and "json" in response.headers.get("Content-Type", "") else None

    def start(self):
        if self.running:
            return
        if self.process is not None or self.lock_file is not None:
            self.stop()
        binary = self.binary or engine_path()
        self._lock()
        try:
            self.home.mkdir(parents=True, exist_ok=True)
            env = os.environ.copy()
            env["STNODEFAULTFOLDER"] = "1"
            config_path = self.home / "config.xml"
            if not config_path.exists():
                generated = subprocess.run([str(binary), "generate", "--home", str(self.home), "--no-port-probing"],
                                           env=env, capture_output=True, timeout=45, creationflags=CREATE_FLAGS)
                if generated.returncode:
                    raise SyncError("同步设备初始化失败：" + generated.stderr.decode("utf-8", "replace")[-400:])
            # Reconcile before launching: an interrupted manual run must remain paused.
            tree = ET.parse(config_path)
            root = tree.getroot()
            for folder in root.findall("folder"):
                policy = self.settings["policies"].get(folder.get("id"), {"mode": "manual"})
                paused = folder.find("paused")
                if paused is None:
                    paused = ET.SubElement(folder, "paused")
                paused.text = "false" if policy["mode"] == "auto" and not policy.get("suspended") else "true"
                folder.attrib.pop("paused", None)
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                port = listener.getsockname()[1]
            key = secrets.token_hex(32)
            gui = root.find("gui")
            gui.set("tls", "false")
            gui.find("address").text = f"127.0.0.1:{port}"
            gui.find("apikey").text = key
            options = root.find("options")
            for name, value in {"startBrowser": "false", "autoUpgradeIntervalH": "0",
                                "urAccepted": "-1", "crashReportingEnabled": "false"}.items():
                child = options.find(name)
                if child is None:
                    child = ET.SubElement(options, name)
                child.text = value
            if self.listen_addresses is not None:
                for child in options.findall("listenAddress"):
                    options.remove(child)
                for address in self.listen_addresses:
                    ET.SubElement(options, "listenAddress").text = address
            if self.local_only:
                for name in ("globalAnnounceEnabled", "localAnnounceEnabled", "relaysEnabled", "natEnabled"):
                    options.find(name).text = "false"
            temp = config_path.with_suffix(".tmp")
            tree.write(temp, encoding="utf-8", xml_declaration=True)
            os.replace(temp, config_path)
            self.url = f"http://127.0.0.1:{port}"
            self.session.headers["X-API-Key"] = key
            self.log_stream = (self.directory / "engine-console.log").open("wb")
            self.process = subprocess.Popen(
                [str(binary), "serve", "--home", str(self.home), "--no-browser", "--no-console", "--no-restart", "--no-upgrade"],
                env=env, stdout=self.log_stream, stderr=subprocess.STDOUT, creationflags=CREATE_FLAGS)
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                if not self.running:
                    raise SyncError("同步引擎已退出，请查看 sync/engine-console.log。")
                try:
                    self.device_id = self.api("GET", "system/status", timeout=1)["myID"]
                    break
                except SyncError:
                    time.sleep(0.2)
            else:
                raise SyncError("同步服务启动超时，请查看 sync/engine-console.log。")
            self.settings["enabled"] = True
            self.save()
            self.active.clear()
        except Exception:
            self.stop()
            raise

    def stop(self):
        if self.running:
            try:
                self.api("POST", "system/shutdown", timeout=3)
            except SyncError:
                pass
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(self.process.pid), "/T", "/F"],
                                   capture_output=True, timeout=10, creationflags=CREATE_FLAGS)
                else:
                    self.process.kill()
                self.process.wait(timeout=5)
        self.process = None
        self.url = ""
        self.active.clear()
        if self.log_stream:
            self.log_stream.close()
            self.log_stream = None
        if self.lock_file:
            self.lock_file.close()
            self.lock_file = None

    def close(self):
        self.stop()
        self.session.close()

    def device(self, device_id, name, addresses=None):
        compact = re.sub(r"[\s-]", "", device_id).upper()
        if not re.fullmatch(r"[A-Z2-7]{56}", compact):
            raise SyncError("设备 ID 应为对方程序显示的完整 8 组 ID。")
        device_id = "-".join(compact[i:i + 7] for i in range(0, 56, 7))
        if device_id == self.device_id:
            raise SyncError("这是本机 ID，请填写另一台电脑的设备 ID。")
        devices = self.api("GET", "config/devices")
        template = next((d for d in devices if d["deviceID"] == device_id), None)
        if template is None:
            template = self.api("GET", "config/defaults/device")
        template.update(deviceID=device_id, name=name.strip() or device_id[:7], addresses=normalize_addresses(addresses),
                        autoAcceptFolders=False, introducer=False)
        self.api("POST", "config/devices", template)

    def rename(self, name):
        name = name.strip()
        if not name:
            raise SyncError("请填写本机名称。")
        self.api("PATCH", "config/devices/" + self.device_id, {"name": name})

    def remove_device(self, device_id):
        if device_id == self.device_id:
            raise SyncError("请选择另一台电脑。")
        self.api("DELETE", "config/devices/" + quote(device_id, safe=""))

    def folder(self, values):
        folder_id = values["id"].strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", folder_id):
            raise SyncError("共享 ID 请使用 1～64 个字母、数字、下划线或短横线。")
        raw_path = values["path"].strip()
        if not raw_path:
            raise SyncError("请选择本机文件夹。")
        path = Path(raw_path).expanduser().resolve()
        if not path.is_dir():
            raise SyncError("请选择已经存在的本机文件夹。")
        protected = self.directory.resolve()
        if path == Path(path.anchor) or path == protected or path in protected.parents or protected in path.parents:
            raise SyncError("请选择独立的数据文件夹，避开磁盘根目录和工具箱同步配置目录。")
        known_devices = {d["deviceID"] for d in self.api("GET", "config/devices")}
        peers = list(dict.fromkeys(values.get("devices", [])))
        if not peers or self.device_id in peers or set(peers) - known_devices:
            raise SyncError("请至少勾选一台已添加的其他电脑。")
        if values.get("type", "sendreceive") not in TYPES:
            raise SyncError("请选择有效的同步方向。")
        policy = validate_policy(values)
        folders = self.api("GET", "config/folders")
        existing = next((f for f in folders if f["id"] == folder_id), None)
        if existing and not values.get("editing"):
            raise SyncError("这个共享 ID 已存在，请选中原同步项后点击编辑。")
        for folder in folders:
            other = Path(folder["path"]).resolve()
            if folder["id"] != folder_id and (path == other or path in other.parents or other in path.parents):
                raise SyncError("这个路径与已有同步文件夹重叠，请选择独立的目录。")
        if existing and Path(existing["path"]).resolve() != path:
            raise SyncError("已有共享 ID 对应另一个本机路径。需要换目录时请先移除原同步项。")
        template = existing or self.api("GET", "config/defaults/folder")
        template.update(id=folder_id, label=values.get("label", "").strip() or path.name, path=str(path),
                        type=values.get("type", "sendreceive"),
                        devices=[{"deviceID": d} for d in [self.device_id] + peers],
                        paused=policy["mode"] != "auto" or policy["suspended"],
                        fsWatcherEnabled=policy["mode"] == "auto", fsWatcherDelayS=2,
                        rescanIntervalS=3600 if policy["mode"] == "auto" else 0)
        template["versioning"] = {"type": "staggered", "params": {"maxAge": "2592000"}, "cleanupIntervalS": 3600}
        old_policy = self.settings["policies"].get(folder_id)
        policy["next_run"] = next_due(policy, time.time())
        policy["last_run"] = (old_policy or {}).get("last_run", 0)
        # Persist a fail-closed policy before enabling the engine folder.
        self.settings["policies"][folder_id] = policy
        self.save()
        try:
            self.api("POST", "config/folders", template)
        except Exception:
            if old_policy is None:
                self.settings["policies"].pop(folder_id, None)
            else:
                self.settings["policies"][folder_id] = old_policy
            self.save()
            raise
        self.active.pop(folder_id, None)
        self.messages.pop(folder_id, None)

    def remove_folder(self, folder_id):
        self.api("DELETE", "config/folders/" + quote(folder_id, safe=""))
        self.settings["policies"].pop(folder_id, None)
        self.save()
        self.active.pop(folder_id, None)
        self.messages.pop(folder_id, None)

    def sync_now(self, folder_id, now=None):
        now = time.time() if now is None else now
        policy = self.settings["policies"].setdefault(folder_id, validate_policy({"mode": "manual"}))
        policy["suspended"] = False
        policy["last_run"] = now
        policy["next_run"] = next_due(policy, now)
        self.save()
        self.api("PATCH", "config/folders/" + quote(folder_id, safe=""), {"paused": False})
        self.active[folder_id] = {"started": now, "scanned": False, "stable": 0, "complete_peers": set()}
        self.messages[folder_id] = "正在扫描并等待其他电脑；手动/定时任务完成后自动暂停"

    def pause(self, folder_id):
        self.api("PATCH", "config/folders/" + quote(folder_id, safe=""), {"paused": True})
        policy = self.settings["policies"].setdefault(folder_id, validate_policy({"mode": "manual"}))
        policy["suspended"] = True
        self.active.pop(folder_id, None)
        self.messages[folder_id] = "已暂停；点击立即同步恢复"
        self.save()

    def _scheduled(self, folders, now):
        for folder in folders:
            fid = folder["id"]
            policy = self.settings["policies"].get(fid, {"mode": "manual"})
            if policy.get("suspended") or fid in self.active:
                continue
            if policy["mode"] in ("interval", "daily") and policy.get("next_run", now + 1) <= now:
                self.sync_now(fid, now)

    def snapshot(self, now=None):
        now = time.time() if now is None else now
        if not self.running:
            raise SyncError("同步服务已停止，请点击启动同步服务。")
        config = self.api("GET", "config")
        connections = self.api("GET", "system/connections").get("connections", {})
        self._scheduled(config["folders"], now)
        result = {"device_id": self.device_id, "devices": [], "folders": []}
        for device in config["devices"]:
            if device["deviceID"] == self.device_id:
                result["name"] = device["name"]
            else:
                connection = connections.get(device["deviceID"], {})
                result["devices"].append({**device, "connected": connection.get("connected", False),
                                          "address": connection.get("address", "")})
        for folder in config["folders"]:
            fid = folder["id"]
            policy = self.settings["policies"].get(fid, {"mode": "manual"})
            active = self.active.get(fid)
            row = {**folder, "policy": dict(policy), "status": "已暂停", "progress": "—", "detail": self.messages.get(fid, "")}
            # The config fetched above can precede a due job's resume request.
            if active or not folder["paused"]:
                if active and not active["scanned"]:
                    try:
                        self.api("POST", "db/scan", params={"folder": fid}, timeout=(2, 2))
                        active["scanned"] = True
                    except SyncError:
                        row["status"] = "等待文件夹启动"
                        result["folders"].append(row)
                        continue
                status = self.api("GET", "db/status", params={"folder": fid})
                row["status"] = {"idle": "已同步", "scanning": "扫描中", "scan-waiting": "等待扫描",
                                 "syncing": "同步中", "sync-waiting": "等待同步", "error": "出错"}.get(status["state"], status["state"])
                completion = self.api("GET", "db/completion", params={"folder": fid})
                progress = [float(completion.get("completion", 0))]
                local_complete = (status["state"] == "idle" and status.get("needTotalItems", 0) == 0
                                  and status.get("pullErrors", 0) == 0 and active is not None and active["scanned"])
                all_complete = local_complete
                peers = [d["deviceID"] for d in folder["devices"] if d["deviceID"] != self.device_id]
                waiting = []
                errors = status.get("pullErrors", 0)
                if errors:
                    row["detail"] = f"{errors} 个文件同步失败，请检查文件占用、权限和磁盘空间"
                if active and active.get("sequence") != status.get("sequence"):
                    active["stable"] = 0
                    active["complete_peers"].clear()
                    active["sequence"] = status.get("sequence")
                for peer in peers:
                    if not connections.get(peer, {}).get("connected"):
                        all_complete = False
                        waiting.append(peer[:7] + " 离线")
                        continue
                    remote = self.api("GET", "db/completion", params={"folder": fid, "device": peer})
                    progress.append(float(remote.get("completion", 0)))
                    complete = remote.get("needItems", -1) == 0 and remote.get("needDeletes", 0) == 0
                    remote_state = remote.get("remoteState")
                    if remote_state == "valid" and complete and active:
                        active["complete_peers"].add(peer)
                    accepted = remote_state == "valid" or (remote_state == "paused" and active and peer in active["complete_peers"])
                    all_complete = all_complete and complete and bool(accepted)
                    if remote_state != "valid":
                        waiting.append(peer[:7] + {"paused": " 文件夹已暂停", "notSharing": " 尚未配对文件夹"}.get(remote_state, " 等待索引"))
                if not peers:
                    all_complete = False
                    waiting.append("尚未选择共享电脑")
                if waiting:
                    row["status"] = "等待其他电脑"
                    row["detail"] = "；".join(waiting)
                elif not errors:
                    row["detail"] = f"本机 {status.get('localFiles', 0)} 个文件；待接收 {status.get('needBytes', 0):,} 字节"
                row["progress"] = f"{min(progress):.1f}%" if not waiting else "等待连接/配对"
                if not waiting and min(progress) < 100 and row["status"] == "已同步":
                    row["status"] = "等待同步 / 发送中"
                if active and policy["mode"] != "auto":
                    active["stable"] = active["stable"] + 1 if all_complete else 0
                    if now - active["started"] >= 20 and active["stable"] >= 3:
                        self.api("PATCH", "config/folders/" + quote(fid, safe=""), {"paused": True})
                        del self.active[fid]
                        row["status"] = "本轮完成 · 已暂停"
                        self.messages[fid] = "本轮完成；等待下一次手动或定时同步"
                        row["detail"] = self.messages[fid]
                elif active:
                    self.active.pop(fid, None)
            if policy.get("suspended"):
                row["status"] = "已暂停（含定时任务）"
            result["folders"].append(row)
        return result
