"""Packaged-resource smoke test for the embedded XuanwuAi service."""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from pathlib import Path

import requests

from xuanwu_ai import create_server


def run_xuanwu_test(_app_module=None) -> int:
    with tempfile.TemporaryDirectory(prefix="omini-xuanwu-check-") as temp:
        server = create_server(Path(temp) / "data")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.runtime.port}"
        client = requests.Session()
        client.trust_env = False
        report = {"bound_to_loopback": server.server_address[0] == "127.0.0.1"}
        try:
            page = client.get(base + "/", timeout=5)
            report["html_loaded"] = page.status_code == 200 and "玄武Ai".encode("utf-8") in page.content
            assets = re.findall(r"(?:src|href)=\"\.(/assets/[^\"]+)\"", page.text)
            report["assets_loaded"] = bool(assets) and all(
                client.get(base + asset, timeout=5).status_code == 200 for asset in assets
            )
            logo = client.get(base + "/omini-logo.png", timeout=5)
            report["logo_loaded"] = logo.status_code == 200 and logo.content.startswith(b"\x89PNG")
            bootstrap = client.get(base + "/api/bootstrap", timeout=5)
            report["bootstrap_loaded"] = bootstrap.status_code == 200
            token = bootstrap.json()["token"]
            report["api_guarded"] = client.get(base + "/api/models", timeout=5).status_code == 403
            server.runtime.folder_picker = lambda: str(Path(temp) / "workspace")
            picked = client.post(
                base + "/api/pick-folder",
                json={},
                headers={"X-Xuanwu-Token": token},
                timeout=5,
            )
            report["folder_picker_connected"] = picked.json().get("path") == str(Path(temp) / "workspace")
            report["data_isolated"] = server.runtime.store.path == Path(temp) / "data" / "data.json"
            print(json.dumps(report, ensure_ascii=False), flush=True)
            _write_report(report)
            return 0 if all(report.values()) else 1
        except Exception as error:
            report["error"] = str(error).splitlines()[0]
            print(json.dumps(report, ensure_ascii=False), flush=True)
            _write_report(report)
            return 1
        finally:
            client.close()
            server.runtime.speech.stop()
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


def _write_report(report: dict) -> None:
    target = os.environ.get("OMINI_SELF_TEST_LOG")
    if target:
        Path(target).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
