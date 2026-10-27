"""Publish a verified installer for the source version without replacing releases."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
from app_version import APP_VERSION
from updater import REPOSITORY

API = "https://api.github.com"


def git(*arguments: str) -> str:
    environment = os.environ.copy()
    environment.update(GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="Never")
    result = subprocess.run(["git", "-C", str(ROOT), *arguments], env=environment,
                            text=True, encoding="utf-8", errors="replace",
                            capture_output=True, timeout=120)
    if result.returncode:
        raise RuntimeError(result.stderr.strip())
    return result.stdout.strip()


def session() -> requests.Session:
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        environment = os.environ.copy()
        environment.update(GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="Never")
        result = subprocess.run(["git", "credential", "fill"], cwd=ROOT, env=environment,
                                input="protocol=https\nhost=github.com\n\n", text=True,
                                capture_output=True, timeout=30)
        if result.returncode:
            raise RuntimeError("GitHub credential lookup failed")
        fields = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        token = fields.get("password")
    if not token:
        raise RuntimeError("No GitHub credential is configured")
    client = requests.Session()
    client.trust_env = False
    client.headers.update({"Authorization": "Bearer " + token,
                           "Accept": "application/vnd.github+json",
                           "X-GitHub-Api-Version": "2022-11-28"})
    return client


def api(client: requests.Session, method: str, path: str, **kwargs):
    response = client.request(method, API + path, timeout=(20, 90), **kwargs)
    if not response.ok:
        raise RuntimeError(f"GitHub {method} {path}: HTTP {response.status_code}")
    return response.json() if response.content else None


def publish(make_public: bool = False) -> dict:
    tag = "v" + APP_VERSION
    if git("status", "--porcelain"):
        raise RuntimeError("Commit the reviewed source changes before publishing")
    commit = git("rev-parse", "HEAD")
    if git("rev-parse", tag + "^{commit}") != commit:
        raise RuntimeError("The release tag must point to the checked-out source commit")
    remote_refs = git("ls-remote", "origin", "refs/heads/main", f"refs/tags/{tag}^{{}}")
    refs = dict((line.split()[1], line.split()[0]) for line in remote_refs.splitlines())
    if refs.get("refs/heads/main") != commit or refs.get(f"refs/tags/{tag}^{{}}") != commit:
        raise RuntimeError("Push the source commit and annotated release tag before publishing")
    setup = ROOT / f"artifacts/installers/{APP_VERSION}" / f"Omini工具箱-{APP_VERSION}-Setup.exe"
    with setup.open("rb") as stream:
        checksum = hashlib.file_digest(stream, "sha256").hexdigest()
    if setup.with_suffix(".exe.sha256").read_text(encoding="utf-8-sig").split()[0] != checksum:
        raise RuntimeError("The installer SHA-256 does not match its checksum file")
    verification = json.loads((ROOT / "artifacts/verification/installer-report.json").read_text(encoding="utf-8"))
    if Path(verification["installer"]).resolve() != setup.resolve():
        raise RuntimeError("Verify this exact installer before publishing")
    if not verification.get("installed_desktop_smoke") or not all(value is True for key, value in verification.items() if key != "installer"):
        raise RuntimeError("Installer verification must pass before publishing")
    notes = (ROOT / "releases" / (tag + ".md")).read_text(encoding="utf-8")
    upload_name = f"Omini-Toolbox-{APP_VERSION}-Setup.exe"
    checksum_body = (checksum + "  " + upload_name + "\n").encode("ascii")
    with session() as client:
        existing = client.get(f"{API}/repos/{REPOSITORY}/releases/tags/{tag}", timeout=30)
        if existing.status_code == 404:
            drafts = api(client, "GET", f"/repos/{REPOSITORY}/releases", params={"per_page": 100})
            release = next((item for item in drafts if item["tag_name"] == tag and item["draft"]), None)
            if release is None:
                release = api(client, "POST", f"/repos/{REPOSITORY}/releases", json={
                    "tag_name": tag, "target_commitish": commit,
                    "name": f"Omini工具箱 {APP_VERSION} — Windows 安装版",
                    "body": notes, "draft": True, "prerelease": False})
        elif existing.ok:
            release = existing.json()
        else:
            raise RuntimeError(f"Release lookup returned HTTP {existing.status_code}")
        assets = api(client, "GET", f"/repos/{REPOSITORY}/releases/{release['id']}/assets")
        for name, size, digest, content in (
            (upload_name, setup.stat().st_size, checksum, None),
            (upload_name + ".sha256", len(checksum_body), hashlib.sha256(checksum_body).hexdigest(), checksum_body),
        ):
            current = next((asset for asset in assets if asset["name"] == name), None)
            if current and release["draft"] and current.get("state") == "starter" and not current.get("digest"):
                api(client, "DELETE", f"/repos/{REPOSITORY}/releases/assets/{current['id']}")
                current = None
            if current:
                if current.get("digest") != "sha256:" + digest or current["size"] != size:
                    raise RuntimeError("An existing release asset differs; use a new version")
                print("Verified existing asset: " + name, flush=True)
                continue
            if not release["draft"]:
                raise RuntimeError("Keep published releases unchanged; use a new version")
            print(f"Uploading {name} ({size:,} bytes)...", flush=True)
            with setup.open("rb") as stream:
                response = client.post(release["upload_url"].split("{")[0], params={"name": name},
                                       data=content if content is not None else stream,
                                       headers={"Content-Type": "application/octet-stream"}, timeout=(300, 1200))
            if not response.ok:
                raise RuntimeError(f"Asset upload returned HTTP {response.status_code}")
            asset = response.json()
            if asset.get("digest") != "sha256:" + digest or asset["size"] != size:
                raise RuntimeError("Uploaded asset integrity check failed")
            print("Uploaded and SHA-256 verified: " + name, flush=True)
        if make_public and release["draft"]:
            release = api(client, "PATCH", f"/repos/{REPOSITORY}/releases/{release['id']}",
                          json={"draft": False, "make_latest": "true"})
        final_assets = api(client, "GET", f"/repos/{REPOSITORY}/releases/{release['id']}/assets")
    report = {"repository": f"https://github.com/{REPOSITORY}", "release": release["html_url"],
              "commit": commit, "tag": tag, "draft": release["draft"], "sha256": checksum,
              "assets": [{"name": item["name"], "bytes": item["size"], "url": item["browser_download_url"]}
                         for item in final_assets]}
    (ROOT / "artifacts/verification" / f"github-{tag}.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--publish", action="store_true", help="Publish the verified draft as the latest release")
    arguments = parser.parse_args()
    print(json.dumps(publish(arguments.publish), ensure_ascii=True), flush=True)
