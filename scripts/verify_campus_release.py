"""Launch the packaged app with isolated APPDATA and verify its window/exit."""
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time


def main():
    # When launched from scripts/, add the repository to the import path.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from app import APP_NAME, APP_VERSION
    executable = Path(sys.argv[1]).resolve()
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    user32.FindWindowW.restype = wintypes.HWND
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    with tempfile.TemporaryDirectory(prefix="omini-release-smoke-") as directory:
        environment = os.environ.copy()
        environment["APPDATA"] = directory
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = 0
        process = subprocess.Popen([str(executable)], env=environment, startupinfo=startup)
        try:
            deadline = time.monotonic() + 20
            hwnd = None
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError(f"Packaged app exited early: {process.returncode}")
                hwnd = user32.FindWindowW(None, f"{APP_NAME} {APP_VERSION}")
                if hwnd:
                    pid = wintypes.DWORD()
                    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                    if pid.value == process.pid: break
                    hwnd = None
                time.sleep(0.1)
            if not hwnd: raise RuntimeError("Packaged app window did not appear")
            # Allow the startup WLAN worker to run before verifying clean exit.
            time.sleep(2)
            user32.PostMessageW(hwnd, 0x0010, 0, 0)
            exit_code = process.wait(timeout=20)
            if exit_code != 0: raise RuntimeError(f"Packaged app exit code: {exit_code}")
            report = dict(version=APP_VERSION, executable=str(executable), window_verified=True,
                          startup_wifi_worker_verified=True, exit_code=exit_code, isolated_config=True)
            Path(f"artifacts/verification/campus-release-{APP_VERSION}.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            print("Packaged app startup and clean shutdown passed (isolated config).")
        finally:
            if process.poll() is None:
                process.terminate(); process.wait(timeout=10)


if __name__ == "__main__": main()
