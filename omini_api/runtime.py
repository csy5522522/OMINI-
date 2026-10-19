"""Run the original Tk interface in an isolated, embeddable process."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def default_workspace() -> Path:
    return Path(os.environ.get("APPDATA", Path.home())) / "Omini工具箱" / "omini-api"


def initial_workspace(saved: str = "") -> Path:
    if saved.strip():
        return Path(saved).expanduser().resolve()
    # Reuse the existing local installation when upgrading this toolbox.
    candidates = (
        Path(__file__).resolve().parents[2] / "Omini Open Ai" / "Omini API",
        Path.home() / "Desktop" / "Omini Open Ai" / "Omini API",
    )
    for candidate in candidates:
        if (candidate / "omini_sites.json").is_file():
            return candidate
    return default_workspace()


def base_command() -> list[str]:
    if getattr(sys, "frozen", False):
        return [str(Path(sys.executable).resolve()), "--omini-api"]
    return [sys.executable, str(Path(__file__).resolve().parents[1] / "omini_api_runner.py")]


def _write_json(path: Path, data: dict) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def apply_theme(root, dark: bool) -> None:
    import tkinter as tk
    from tkinter import ttk

    background = "#111827" if dark else "#f8fafc"
    foreground = "#e5e7eb" if dark else "#172033"
    field = "#1e293b" if dark else "#ffffff"
    button = "#334155" if dark else "#e2e8f0"
    style = ttk.Style(root)
    if style.theme_use() != "clam":
        style.theme_use("clam")
    style.configure(".", background=background, foreground=foreground,
                    fieldbackground=field, font=("Microsoft YaHei UI", 10))
    style.configure("TButton", background=button, padding=(10, 6))
    style.configure("TNotebook.Tab", background=button, padding=(10, 7))
    style.configure("Treeview", background=field, fieldbackground=field,
                    foreground=foreground, rowheight=27)
    style.configure("Treeview.Heading", background=button, foreground=foreground)
    style.configure("TCombobox", fieldbackground=field, background=button,
                    foreground=foreground, arrowcolor=foreground)
    style.map("TCombobox", fieldbackground=[("readonly", field)],
              foreground=[("readonly", foreground)])
    for name in ("TButton", "TNotebook.Tab", "Treeview"):
        style.map(name, background=[("selected", "#2563eb"), ("active", button)],
                  foreground=[("selected", "#ffffff")])

    def paint(widget):
        if isinstance(widget, ttk.Widget):
            if isinstance(widget, ttk.Label) and (
                getattr(widget, "_omini_muted", False) or str(widget.cget("foreground")) == "#555"
            ):
                widget._omini_muted = True
                widget.configure(foreground="#94a3b8" if dark else "#555")
        elif isinstance(widget, (tk.Tk, tk.Frame, tk.Canvas, tk.Label)):
            widget.configure(background=background)
        elif isinstance(widget, (tk.Text, tk.Entry)):
            widget.configure(background=field, foreground=foreground,
                             insertbackground=foreground, selectbackground="#2563eb")
        elif isinstance(widget, tk.Listbox):
            widget.configure(background=field, foreground=foreground, selectbackground="#2563eb")
        for child in widget.winfo_children():
            paint(child)

    paint(root)


def _run_embedded(toolbox, control: Path) -> int:
    control.mkdir(parents=True, exist_ok=True)
    root = toolbox.ToolboxApp()
    root.overrideredirect(True)
    root.minsize(1, 1)
    root.update_idletasks()
    root.withdraw()
    hwnd = int(root.frame(), 0)
    last_command = ""
    dark = True
    parent_handle = None
    parent_expected = sys.platform == "win32" and bool(os.environ.get("OMINI_API_PARENT_PID"))
    if parent_expected:
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        parent_handle = kernel.OpenProcess(0x100000, False, int(os.environ["OMINI_API_PARENT_PID"]))
    _write_json(control / "ready.json", {"pid": os.getpid(), "hwnd": hwnd})

    def shutdown() -> None:
        # Save before stopping workers, so even forced process-tree cleanup keeps settings.
        root._save_settings()
        root.save_email_receiver_settings(show_message=False)
        for name in list(root.running_processes):
            root.stop_process(name)
        root.destroy()

    def poll() -> None:
        nonlocal last_command, dark
        parent_exited = parent_expected and (not parent_handle or kernel.WaitForSingleObject(parent_handle, 0) == 0)
        if (control / "stop").exists() or not control.exists() or parent_exited:
            shutdown()
            return
        try:
            command_text = (control / "command.json").read_text(encoding="utf-8")
            if command_text != last_command:
                command = json.loads(command_text)
                dark = bool(command.get("dark", True))
                apply_theme(root, dark)
                if command.get("attached"):
                    root.deiconify()
                last_command = command_text
            running = [name for name, process in root.running_processes.items()
                       if process.poll() is None]
            _write_json(control / "status.json", {
                "tasks": running, "dark": dark,
                "tabs": [root.notebook.tab(tab, "text") for tab in root.notebook.tabs()],
                "current_tab": root.notebook.index("current"),
            })
        except (OSError, ValueError):
            pass
        root.after(150, poll)

    root.after(100, poll)
    try:
        root.mainloop()
    finally:
        if parent_handle:
            kernel.CloseHandle(parent_handle)
    return 0


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    workspace = Path(os.environ.get("OMINI_API_TOOLBOX_HOME", "") or default_workspace()).expanduser().resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    os.environ["OMINI_API_TOOLBOX_HOME"] = str(workspace)
    from omini_api import omini_api_toolbox as toolbox

    toolbox.WORKSPACE_DIR = workspace
    toolbox.worker_base_command = base_command
    if arguments[:1] == ["--worker"]:
        return toolbox.run_worker(arguments[1:])
    if arguments[:1] == ["--embed"] and len(arguments) == 2:
        return _run_embedded(toolbox, Path(arguments[1]))
    if arguments:
        raise ValueError("Unknown Omini API arguments")
    return toolbox.main()
