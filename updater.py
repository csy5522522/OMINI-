"""GitHub Release updates with private-repository auth and verified installer files."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import threading
from urllib.parse import urljoin, urlsplit

import requests

from campus_network import protect_password, unprotect_password

REPOSITORY = "csy5522522/OMINI-"
RELEASES_URL = f"https://github.com/{REPOSITORY}/releases"
API_ROOT = "https://api.github.com"
DOWNLOAD_HOSTS = {"github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com",
                  "github-releases.githubusercontent.com"}


class UpdateError(ValueError):
    pass


class UpdateCancelled(UpdateError):
    pass


def version_tuple(value):
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", value.strip())
    if not match:
        raise UpdateError("版本号格式有误，应为 v1.2.3 形式的正式版本。")
    return tuple(map(int, match.groups()))


@dataclass(frozen=True)
class Release:
    version: str
    tag: str
    notes: str
    asset_id: int
    asset_name: str
    size: int
    digest: str
    checksum_id: int | None


@dataclass(frozen=True)
class Installer:
    path: Path
    sha256: str
    version: str


def select_release(data):
    if data.get("draft") or data.get("prerelease"):
        raise UpdateError("更新源未提供正式发布版本。")
    tag = data.get("tag_name", "")
    version_tuple(tag)
    version = tag.lstrip("v")
    accepted = {f"omini-toolbox-{version}-setup.exe", f"omini工具箱-{version}-setup.exe", f"omini.-{version}-setup.exe"}
    assets = [item for item in data.get("assets", []) if item.get("state") == "uploaded"]
    installers = [item for item in assets if str(item.get("name", "")).lower() in accepted]
    if len(installers) != 1:
        raise UpdateError("此版本尚未提供对应的 Windows 安装包，请在发布页检查附件。")
    asset = installers[0]
    size = asset.get("size", 0)
    if not isinstance(size, int) or not 0 < size < 2 * 1024 ** 3:
        raise UpdateError("安装包大小信息有误。")
    digest = asset.get("digest") or ""
    digest = digest[7:] if re.fullmatch(r"sha256:[a-fA-F0-9]{64}", digest) else ""
    checksum = next((item for item in assets if item["name"] == asset["name"] + ".sha256"), None)
    if not digest and not checksum:
        raise UpdateError("发布版本缺少 SHA-256 校验信息，暂未启用下载安装。")
    try:
        asset_id = int(asset["id"])
        checksum_id = int(checksum["id"]) if checksum else None
        if asset_id <= 0 or checksum_id is not None and checksum_id <= 0:
            raise ValueError()
    except (KeyError, TypeError, ValueError) as error:
        raise UpdateError("安装包附件信息有误。") from error
    return Release(version, tag, str(data.get("body") or "此版本未填写更新说明。")[:20000],
                   asset_id, asset["name"], size, digest.lower(), checksum_id)


class TokenStore:
    def __init__(self, path):
        self.path = Path(path)
        self.error = ""
        self.token = ""
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                self.token = unprotect_password(data.get("token_protected", ""))
            except (OSError, ValueError, TypeError, RuntimeError):
                self.error = "更新凭据读取失败，请重新填写 GitHub Token。原配置已保留。"

    def save(self, token, remember):
        if not remember or not token:
            self.path.unlink(missing_ok=True)
            self.token = ""
            return
        encrypted = protect_password(token)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps({"token_protected": encrypted}), encoding="utf-8")
        os.replace(temp, self.path)
        self.token = token


class GitHubUpdater:
    def __init__(self, current_version, token="", api_root=API_ROOT):
        version_tuple(current_version)
        self.current_version = current_version
        self.token = token.strip()
        if any(char.isspace() for char in self.token):
            raise UpdateError("GitHub Token 中含有空格或换行，请重新填写。")
        self.api_root = api_root.rstrip("/")
        self.api_origin = urlsplit(self.api_root)
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "Omini-Toolbox-Updater/" + current_version,
                                     "X-GitHub-Api-Version": "2022-11-28"})

    def close(self):
        self.session.close()

    def _valid_url(self, url):
        parsed = urlsplit(url)
        own_origin = (parsed.scheme, parsed.netloc) == (self.api_origin.scheme, self.api_origin.netloc)
        if parsed.username or parsed.password or parsed.fragment:
            return False
        if own_origin:
            return True
        return parsed.scheme == "https" and parsed.hostname in DOWNLOAD_HOSTS and parsed.port in (443, None)

    def _open(self, endpoint, accept):
        url = self.api_root + f"/repos/{REPOSITORY}/" + endpoint
        for _ in range(6):
            if not self._valid_url(url):
                raise UpdateError("下载地址不属于 GitHub 官方分发域名，已停止下载。")
            parsed = urlsplit(url)
            own_origin = (parsed.scheme, parsed.netloc) == (self.api_origin.scheme, self.api_origin.netloc)
            headers = {"Accept": accept}
            # Release assets redirect to signed storage URLs; never forward the token.
            if self.token and own_origin:
                headers["Authorization"] = "Bearer " + self.token
            try:
                response = self.session.get(url, headers=headers, stream=True,
                                            allow_redirects=False, timeout=(10, 20))
            except requests.RequestException as error:
                raise UpdateError("连接 GitHub 失败，请检查网络或代理后重试。") from error
            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get("Location", "")
                response.close()
                if not location:
                    raise UpdateError("GitHub 下载跳转缺少目标地址。")
                url = urljoin(url, location)
                continue
            if not response.ok:
                status = response.status_code
                response.close()
                errors = {401: "GitHub Token 已失效或填写有误，请更新凭据。",
                          403: "GitHub 拒绝访问，请检查 Token 的仓库读取权限或 API 调用限额。",
                          404: "仓库为私有、Token 缺少访问权限，或尚未发布正式版本。请填写可访问此仓库的 Token。",
                          429: "GitHub 请求过于频繁，请稍后重试。"}
                raise UpdateError(errors.get(status, f"GitHub 返回 HTTP {status}，请稍后重试。"))
            return response
        raise UpdateError("GitHub 下载跳转次数过多。")

    @staticmethod
    def _body(response, limit):
        try:
            chunks = []
            length = 0
            for chunk in response.iter_content(65536):
                length += len(chunk)
                if length > limit:
                    raise UpdateError("GitHub 响应超过预期大小。")
                chunks.append(chunk)
            return b"".join(chunks)
        except requests.RequestException as error:
            raise UpdateError("读取 GitHub 响应时连接中断，请重试。") from error
        finally:
            response.close()

    def check(self):
        response = self._open("releases/latest", "application/vnd.github+json")
        try:
            data = json.loads(self._body(response, 2 * 1024 * 1024))
            release = select_release(data)
        except (json.JSONDecodeError, TypeError, AttributeError) as error:
            raise UpdateError("GitHub 返回的版本信息格式有误。") from error
        return release if version_tuple(release.version) > version_tuple(self.current_version) else None

    def _expected_hash(self, release):
        digest = release.digest
        if release.checksum_id:
            response = self._open(f"releases/assets/{release.checksum_id}", "application/octet-stream")
            text = self._body(response, 65536).decode("utf-8-sig", "replace")
            checksum = ""
            for line in text.splitlines():
                match = re.fullmatch(r"([a-fA-F0-9]{64})(?:\s+\*?(.+))?", line.strip())
                if match and (not match[2] or match[2] == release.asset_name):
                    checksum = match[1].lower()
                    break
            if not checksum or digest and digest != checksum:
                raise UpdateError("发布页的 SHA-256 校验信息不一致，请等待发布者修正。")
            digest = checksum
        if not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise UpdateError("缺少有效的 SHA-256 校验值。")
        return digest

    def download(self, release, directory, cancel=None, progress=None):
        if version_tuple(release.version) <= version_tuple(self.current_version):
            raise UpdateError("此版本不高于当前版本，无需下载安装。")
        cancel = cancel or threading.Event()
        if cancel.is_set():
            raise UpdateCancelled("更新下载已取消。")
        digest = self._expected_hash(release)
        folder = Path(directory).resolve() / release.version
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / f"Omini-Toolbox-{release.version}-Setup.exe"
        artifact = Installer(target, digest, release.version)
        if target.exists():
            try:
                verify_installer(artifact)
                if target.stat().st_size == release.size:
                    if progress:
                        progress(release.size, release.size)
                    return artifact
            except UpdateError:
                pass
        temp = target.with_suffix(".exe.part")
        response = None
        try:
            response = self._open(f"releases/assets/{release.asset_id}", "application/octet-stream")
            downloaded = 0
            hasher = hashlib.sha256()
            with temp.open("wb") as stream:
                for chunk in response.iter_content(256 * 1024):
                    if cancel.is_set():
                        raise UpdateCancelled("更新下载已取消。")
                    if not chunk:
                        continue
                    downloaded += len(chunk)
                    if downloaded > release.size:
                        raise UpdateError("下载内容超过安装包标注大小，已停止下载。")
                    stream.write(chunk)
                    hasher.update(chunk)
                    if progress:
                        progress(downloaded, release.size)
                stream.flush()
                os.fsync(stream.fileno())
            if cancel.is_set():
                raise UpdateCancelled("更新下载已取消。")
            if downloaded != release.size or hasher.hexdigest() != digest:
                raise UpdateError("安装包大小或 SHA-256 校验未通过，请重新下载。")
            with temp.open("rb") as stream:
                if stream.read(2) != b"MZ":
                    raise UpdateError("下载的文件不是有效的 Windows 安装程序。")
            os.replace(temp, target)
            return artifact
        except requests.RequestException as error:
            raise UpdateError("安装包下载中断，请重试。") from error
        except OSError as error:
            raise UpdateError("更新文件保存失败，请检查磁盘空间与目录权限。") from error
        finally:
            if response is not None:
                response.close()
            temp.unlink(missing_ok=True)


def verify_installer(artifact):
    try:
        with artifact.path.open("rb") as stream:
            if stream.read(2) != b"MZ":
                raise UpdateError("更新安装包格式有误，请重新下载。")
            stream.seek(0)
            if hashlib.file_digest(stream, "sha256").hexdigest() != artifact.sha256:
                raise UpdateError("更新安装包已变化，校验未通过，请重新下载。")
    except OSError as error:
        raise UpdateError("更新安装包读取失败，请重新下载。") from error


def launch_installer(artifact):
    verify_installer(artifact)
    environment = os.environ.copy()
    # A frozen app restarting through Setup must not pass its PyInstaller
    # child-process state to the newly installed application.
    environment["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    return subprocess.Popen([str(artifact.path), "/SP-", "/NORESTART"], close_fds=True, env=environment)
