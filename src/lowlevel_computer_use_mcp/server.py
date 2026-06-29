#!/usr/bin/env python3
"""Low-level computer-use MCP server.

Exposes low-level desktop control as MCP tools (stdio transport):

  * Mouse control      - move, click, double-click, drag, scroll, cursor position
  * Keyboard control   - type text, press hotkey combinations
  * Shell commands     - run arbitrary system/shell commands and capture output
  * Window management  - list / move / resize / focus / minimize / maximize / close windows
  * Process control    - list processes and kill them by PID or name
  * Screenshots        - capture full screen, a single monitor, or a region
  * Image cropping      - crop a saved screenshot to a sub-region
  * Screen recording   - record the screen (or a region) to an mp4 file

This server is intended for Windows but most tools degrade gracefully on other
platforms. It performs real, unsandboxed actions on the host machine - only run
it in a context where that is acceptable.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field
from mcp.server.fastmcp import FastMCP

# --------------------------------------------------------------------------- #
# Optional / platform dependencies (imported lazily-safely so the server can
# still start and report a clear error if something is missing).
# --------------------------------------------------------------------------- #
try:
    import pyautogui

    pyautogui.FAILSAFE = False  # do not abort when the cursor hits a screen corner
    pyautogui.PAUSE = 0.0
except Exception:  # pragma: no cover - environment dependent
    pyautogui = None

try:
    import pygetwindow as gw
except Exception:  # pragma: no cover
    gw = None

try:
    import psutil
except Exception:  # pragma: no cover
    psutil = None

try:
    import mss
except Exception:  # pragma: no cover
    mss = None

try:
    import numpy as np
except Exception:  # pragma: no cover
    np = None

try:
    from PIL import Image
except Exception:  # pragma: no cover
    Image = None


# --------------------------------------------------------------------------- #
# Server + constants
# --------------------------------------------------------------------------- #
mcp = FastMCP("computer_use_mcp")

CAPTURE_DIR = Path(
    os.environ.get(
        "LOWLEVEL_CU_CAPTURE_DIR",
        str(Path.home() / "lowlevel-computer-use-captures"),
    )
)


def _capture_dir() -> Path:
    CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
    return CAPTURE_DIR


def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]


def _ok(**data: Any) -> str:
    return json.dumps({"ok": True, **data}, indent=2, default=str)


def _err(message: str, **data: Any) -> str:
    return json.dumps({"ok": False, "error": message, **data}, indent=2, default=str)


def _require(module: Any, name: str) -> Optional[str]:
    """Return an error JSON string if a required dependency is missing."""
    if module is None:
        return _err(
            f"Required dependency '{name}' is not available. "
            f"Install it (e.g. `pip install {name}`) and restart the server."
        )
    return None


# --------------------------------------------------------------------------- #
# Admin / elevation + boot-startup helpers (Windows)
# --------------------------------------------------------------------------- #
TASK_NAME = "LowLevelComputerUseMCP"
DEFAULT_HTTP_HOST = os.environ.get("LOWLEVEL_CU_HOST", "127.0.0.1")
DEFAULT_HTTP_PORT = int(os.environ.get("LOWLEVEL_CU_PORT", "8765"))


def _repo_dir() -> Path:
    """Path to the cloned repo root (parent of the src/ package)."""
    return Path(__file__).resolve().parents[2]


def _uv_path() -> str:
    """Best-effort absolute path to the uv launcher used to run this server."""
    found = shutil.which("uv")
    if found:
        return found
    candidate = Path.home() / ".local" / "bin" / ("uv.exe" if os.name == "nt" else "uv")
    return str(candidate) if candidate.exists() else "uv"


def _is_admin() -> bool:
    """Return True if the current process is running elevated (Windows admin)."""
    if os.name != "nt":
        try:
            return os.geteuid() == 0  # type: ignore[attr-defined]
        except AttributeError:
            return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _server_launch_argument(http: bool, host: str, port: int) -> str:
    """The argument string passed to uv to start this server (for a scheduled task)."""
    parts = ["run", "--directory", str(_repo_dir()), "lowlevel-computer-use-mcp"]
    if http:
        parts += ["--http", "--host", host, "--port", str(port)]
    return subprocess.list2cmdline(parts)


def _clean_transcript(text: str) -> str:
    """Strip PowerShell transcript header/footer noise, keeping the real output."""
    lines = text.splitlines()
    start = next((i for i, l in enumerate(lines) if l.startswith("Transcript started")), None)
    end = next((i for i, l in enumerate(lines) if "transcript end" in l.lower()), len(lines))
    body = lines[start + 1 : end] if start is not None else lines
    body = [l for l in body if l.strip() and set(l.strip()) != {"*"}]
    return "\n".join(body).strip()


def _run_powershell(body: str, require_admin: bool, timeout: float = 180.0) -> dict[str, Any]:
    """Run a PowerShell script.

    If require_admin is True and the process is not elevated, the script is
    relaunched through a UAC prompt (Start-Process -Verb RunAs) and its output is
    captured via a transcript file. Returns {ok, returncode, output}.
    """
    if os.name != "nt":
        return {"ok": False, "returncode": -1, "output": "Admin/startup features require Windows."}

    if not require_admin or _is_admin():
        try:
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", body],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            return {
                "ok": proc.returncode == 0,
                "returncode": proc.returncode,
                "output": (proc.stdout + proc.stderr).strip(),
            }
        except subprocess.TimeoutExpired:
            return {"ok": False, "returncode": -1, "output": f"PowerShell timed out after {timeout}s"}

    # Not elevated: relaunch the script through UAC, capturing output via transcript.
    log_fd, log_path = tempfile.mkstemp(suffix=".log", prefix="llcu-")
    os.close(log_fd)
    ps1_fd, ps1_path = tempfile.mkstemp(suffix=".ps1", prefix="llcu-")
    os.close(ps1_fd)
    try:
        wrapped = (
            f"Start-Transcript -Path '{log_path}' -Force | Out-Null\n"
            f"try {{\n{body}\n}} catch {{ Write-Output \"ERROR: $($_.Exception.Message)\" }}\n"
            f"Stop-Transcript | Out-Null\n"
        )
        Path(ps1_path).write_text(wrapped, encoding="utf-8")
        launcher = (
            "$p = Start-Process powershell -ArgumentList "
            f"@('-NoProfile','-ExecutionPolicy','Bypass','-File','{ps1_path}') "
            "-Verb RunAs -Wait -WindowStyle Hidden -PassThru; exit $p.ExitCode"
        )
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", launcher],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        output = ""
        if Path(log_path).exists():
            output = _clean_transcript(Path(log_path).read_text(encoding="utf-8", errors="replace"))
        if proc.returncode != 0 and not output:
            output = (proc.stderr or "Elevation was cancelled or failed.").strip()
        return {"ok": proc.returncode == 0, "returncode": proc.returncode, "output": output}
    except subprocess.TimeoutExpired:
        return {"ok": False, "returncode": -1, "output": "Elevated PowerShell timed out (UAC prompt unanswered?)"}
    finally:
        for p in (log_path, ps1_path):
            try:
                os.unlink(p)
            except OSError:
                pass


def _install_startup(http: bool, host: str, port: int, run_as_admin: bool) -> dict[str, Any]:
    """Register a scheduled task that starts this server at user logon."""
    uv = _uv_path()
    arg = _server_launch_argument(http, host, port).replace("'", "''")
    uv_q = uv.replace("'", "''")
    repo_q = str(_repo_dir()).replace("'", "''")
    run_level = "Highest" if run_as_admin else "Limited"
    body = (
        "$user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name\n"
        f"$action = New-ScheduledTaskAction -Execute '{uv_q}' -Argument '{arg}' -WorkingDirectory '{repo_q}'\n"
        "$trigger = New-ScheduledTaskTrigger -AtLogOn\n"
        f"$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel {run_level}\n"
        "$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries "
        "-ExecutionTimeLimit (New-TimeSpan -Seconds 0) -StartWhenAvailable\n"
        f"Register-ScheduledTask -TaskName '{TASK_NAME}' -Action $action -Trigger $trigger "
        "-Principal $principal -Settings $settings -Force | Out-Null\n"
        f"Write-Output 'Installed scheduled task {TASK_NAME} (RunLevel={run_level}, AtLogon).'\n"
    )
    return _run_powershell(body, require_admin=True)


def _uninstall_startup() -> dict[str, Any]:
    """Remove the boot-startup scheduled task."""
    body = (
        f"if (Get-ScheduledTask -TaskName '{TASK_NAME}' -ErrorAction SilentlyContinue) {{\n"
        f"  Unregister-ScheduledTask -TaskName '{TASK_NAME}' -Confirm:$false\n"
        f"  Write-Output 'Removed scheduled task {TASK_NAME}.'\n"
        "} else { Write-Output 'Task was not installed.' }\n"
    )
    return _run_powershell(body, require_admin=True)


def _startup_status() -> dict[str, Any]:
    """Report whether the boot-startup scheduled task is installed."""
    body = (
        f"$t = Get-ScheduledTask -TaskName '{TASK_NAME}' -ErrorAction SilentlyContinue\n"
        "if ($t) {\n"
        "  $i = $t | Get-ScheduledTaskInfo\n"
        "  $lvl = $t.Principal.RunLevel\n"
        "  Write-Output \"INSTALLED|State=$($t.State)|RunLevel=$lvl|LastRun=$($i.LastRunTime)|NextRun=$($i.NextRunTime)\"\n"
        "} else { Write-Output 'NOT_INSTALLED' }\n"
    )
    return _run_powershell(body, require_admin=False)


# --------------------------------------------------------------------------- #
# Enums / shared models
# --------------------------------------------------------------------------- #
class MouseButton(str, Enum):
    LEFT = "left"
    RIGHT = "right"
    MIDDLE = "middle"


# =========================================================================== #
# MOUSE + KEYBOARD
# =========================================================================== #
@mcp.tool(
    name="get_screen_size",
    annotations={
        "title": "Get Screen Size",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def get_screen_size() -> str:
    """Return the primary screen resolution in pixels.

    Returns:
        str: JSON like {"ok": true, "width": 1920, "height": 1080}.
    """
    if (e := _require(pyautogui, "pyautogui")):
        return e
    w, h = pyautogui.size()
    return _ok(width=int(w), height=int(h))


@mcp.tool(
    name="get_cursor_position",
    annotations={
        "title": "Get Cursor Position",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def get_cursor_position() -> str:
    """Return the current mouse cursor position in screen pixels.

    Returns:
        str: JSON like {"ok": true, "x": 100, "y": 200}.
    """
    if (e := _require(pyautogui, "pyautogui")):
        return e
    pos = pyautogui.position()
    return _ok(x=int(pos.x), y=int(pos.y))


class MoveInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    x: int = Field(..., description="Target X coordinate in screen pixels", ge=0)
    y: int = Field(..., description="Target Y coordinate in screen pixels", ge=0)
    duration: float = Field(
        default=0.0, description="Seconds to animate the move over (0 = instant)", ge=0, le=10
    )


@mcp.tool(
    name="mouse_move",
    annotations={
        "title": "Move Mouse",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def mouse_move(params: MoveInput) -> str:
    """Move the mouse cursor to an absolute screen coordinate.

    Args:
        params (MoveInput): x, y target and optional animation duration.

    Returns:
        str: JSON with the resulting cursor position.
    """
    if (e := _require(pyautogui, "pyautogui")):
        return e
    pyautogui.moveTo(params.x, params.y, duration=params.duration)
    pos = pyautogui.position()
    return _ok(x=int(pos.x), y=int(pos.y))


class ClickInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    x: Optional[int] = Field(
        default=None, description="X coordinate to click; omit to click at current cursor", ge=0
    )
    y: Optional[int] = Field(
        default=None, description="Y coordinate to click; omit to click at current cursor", ge=0
    )
    button: MouseButton = Field(default=MouseButton.LEFT, description="Which mouse button")
    clicks: int = Field(default=1, description="Number of clicks (2 = double-click)", ge=1, le=5)
    interval: float = Field(
        default=0.0, description="Seconds between successive clicks", ge=0, le=5
    )


@mcp.tool(
    name="mouse_click",
    annotations={
        "title": "Mouse Click",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
async def mouse_click(params: ClickInput) -> str:
    """Click a mouse button, optionally at a specific coordinate.

    Use clicks=2 for a double-click. If x/y are omitted the click happens at the
    current cursor position.

    Args:
        params (ClickInput): position, button, click count and interval.

    Returns:
        str: JSON describing the click that was performed.
    """
    if (e := _require(pyautogui, "pyautogui")):
        return e
    kwargs: dict[str, Any] = {
        "button": params.button.value,
        "clicks": params.clicks,
        "interval": params.interval,
    }
    if params.x is not None and params.y is not None:
        kwargs["x"] = params.x
        kwargs["y"] = params.y
    pyautogui.click(**kwargs)
    pos = pyautogui.position()
    return _ok(
        button=params.button.value,
        clicks=params.clicks,
        x=int(pos.x),
        y=int(pos.y),
    )


class DragInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    start_x: Optional[int] = Field(default=None, description="Start X; omit to drag from current cursor", ge=0)
    start_y: Optional[int] = Field(default=None, description="Start Y; omit to drag from current cursor", ge=0)
    end_x: int = Field(..., description="End X coordinate", ge=0)
    end_y: int = Field(..., description="End Y coordinate", ge=0)
    button: MouseButton = Field(default=MouseButton.LEFT, description="Button held during drag")
    duration: float = Field(default=0.25, description="Seconds to animate the drag", ge=0, le=10)


@mcp.tool(
    name="mouse_drag",
    annotations={
        "title": "Mouse Drag",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
async def mouse_drag(params: DragInput) -> str:
    """Press a mouse button at a start point and release it at an end point.

    Args:
        params (DragInput): optional start, required end, button and duration.

    Returns:
        str: JSON describing the drag.
    """
    if (e := _require(pyautogui, "pyautogui")):
        return e
    if params.start_x is not None and params.start_y is not None:
        pyautogui.moveTo(params.start_x, params.start_y)
    pyautogui.dragTo(params.end_x, params.end_y, button=params.button.value, duration=params.duration)
    return _ok(end_x=params.end_x, end_y=params.end_y, button=params.button.value)


class ScrollInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    amount: int = Field(..., description="Scroll clicks; positive = up, negative = down")
    x: Optional[int] = Field(default=None, description="X to move to before scrolling", ge=0)
    y: Optional[int] = Field(default=None, description="Y to move to before scrolling", ge=0)


@mcp.tool(
    name="mouse_scroll",
    annotations={
        "title": "Mouse Scroll",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
async def mouse_scroll(params: ScrollInput) -> str:
    """Scroll the mouse wheel vertically.

    Args:
        params (ScrollInput): amount (positive up / negative down) and optional position.

    Returns:
        str: JSON confirming the scroll amount.
    """
    if (e := _require(pyautogui, "pyautogui")):
        return e
    if params.x is not None and params.y is not None:
        pyautogui.moveTo(params.x, params.y)
    pyautogui.scroll(params.amount)
    return _ok(scrolled=params.amount)


class TypeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(..., description="Text to type at the current focus", min_length=1)
    interval: float = Field(default=0.0, description="Seconds between keystrokes", ge=0, le=2)


@mcp.tool(
    name="type_text",
    annotations={
        "title": "Type Text",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
async def type_text(params: TypeInput) -> str:
    """Type a string of text into the currently focused window/control.

    Args:
        params (TypeInput): the text and optional per-key interval.

    Returns:
        str: JSON confirming how many characters were typed.
    """
    if (e := _require(pyautogui, "pyautogui")):
        return e
    pyautogui.typewrite(params.text, interval=params.interval)
    return _ok(typed_chars=len(params.text))


class HotkeyInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    keys: list[str] = Field(
        ...,
        description="Keys to press together, e.g. ['ctrl','c'] or ['win','d']. "
        "Use pyautogui key names (ctrl, alt, shift, win, enter, tab, esc, f1...).",
        min_length=1,
        max_length=6,
    )


@mcp.tool(
    name="press_keys",
    annotations={
        "title": "Press Key Combination",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
async def press_keys(params: HotkeyInput) -> str:
    """Press a keyboard combination (hotkey), e.g. Ctrl+C or Alt+Tab.

    A single-element list presses one key; multiple elements are pressed together.

    Args:
        params (HotkeyInput): list of pyautogui key names.

    Returns:
        str: JSON confirming the keys pressed.
    """
    if (e := _require(pyautogui, "pyautogui")):
        return e
    keys = [k.lower() for k in params.keys]
    if len(keys) == 1:
        pyautogui.press(keys[0])
    else:
        pyautogui.hotkey(*keys)
    return _ok(keys=keys)


# =========================================================================== #
# SHELL COMMANDS
# =========================================================================== #
class RunCommandInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    command: str = Field(..., description="Command line to execute", min_length=1)
    shell: bool = Field(
        default=True,
        description="Run through the system shell (cmd/PowerShell/sh). If false, command is split into argv.",
    )
    cwd: Optional[str] = Field(default=None, description="Working directory for the command")
    timeout: float = Field(default=60.0, description="Max seconds to wait before killing", ge=1, le=3600)


@mcp.tool(
    name="run_command",
    annotations={
        "title": "Run Shell Command",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def run_command(params: RunCommandInput) -> str:
    """Run a system/shell command and capture its stdout, stderr and exit code.

    This executes arbitrary commands on the host with the server's privileges.

    Args:
        params (RunCommandInput): command, shell flag, cwd and timeout.

    Returns:
        str: JSON like {"ok": true, "returncode": 0, "stdout": "...", "stderr": "...",
             "timed_out": false}.
    """
    try:
        if params.shell:
            cmd: Any = params.command
        else:
            import shlex

            cmd = shlex.split(params.command, posix=(os.name != "nt"))
        proc = subprocess.run(
            cmd,
            shell=params.shell,
            cwd=params.cwd,
            capture_output=True,
            text=True,
            timeout=params.timeout,
        )
        return _ok(
            returncode=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
            timed_out=False,
        )
    except subprocess.TimeoutExpired as exc:
        return _err(
            f"Command timed out after {params.timeout}s",
            timed_out=True,
            stdout=exc.stdout or "",
            stderr=exc.stderr or "",
        )
    except Exception as exc:  # pragma: no cover
        return _err(f"{type(exc).__name__}: {exc}")


# =========================================================================== #
# WINDOW MANAGEMENT
# =========================================================================== #
def _window_to_dict(win: Any) -> dict[str, Any]:
    return {
        "title": win.title,
        "handle": getattr(win, "_hWnd", None),
        "left": win.left,
        "top": win.top,
        "width": win.width,
        "height": win.height,
        "is_minimized": bool(getattr(win, "isMinimized", False)),
        "is_maximized": bool(getattr(win, "isMaximized", False)),
        "is_active": bool(getattr(win, "isActive", False)),
    }


def _find_window(title: Optional[str], handle: Optional[int]) -> Any:
    """Find a single window by handle (exact) or title (case-insensitive substring)."""
    windows = gw.getAllWindows()
    if handle is not None:
        for w in windows:
            if getattr(w, "_hWnd", None) == handle:
                return w
        return None
    if title:
        title_l = title.lower()
        matches = [w for w in windows if w.title and title_l in w.title.lower()]
        return matches[0] if matches else None
    return None


class ListWindowsInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    title_filter: Optional[str] = Field(
        default=None, description="Only return windows whose title contains this (case-insensitive)"
    )
    include_empty_titles: bool = Field(
        default=False, description="Include windows that have no title text"
    )


@mcp.tool(
    name="list_windows",
    annotations={
        "title": "List Windows",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def list_windows(params: ListWindowsInput) -> str:
    """List top-level windows with their titles, handles, positions and sizes.

    Args:
        params (ListWindowsInput): optional title filter and empty-title toggle.

    Returns:
        str: JSON {"ok": true, "count": N, "windows": [{title, handle, left, top,
             width, height, is_minimized, is_maximized, is_active}, ...]}.
    """
    if (e := _require(gw, "pygetwindow")):
        return e
    windows = gw.getAllWindows()
    out = []
    for w in windows:
        if not params.include_empty_titles and not (w.title and w.title.strip()):
            continue
        if params.title_filter and params.title_filter.lower() not in (w.title or "").lower():
            continue
        out.append(_window_to_dict(w))
    return _ok(count=len(out), windows=out)


@mcp.tool(
    name="get_active_window",
    annotations={
        "title": "Get Active Window",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def get_active_window() -> str:
    """Return information about the currently focused (active) window.

    Returns:
        str: JSON with the active window's title, handle, position and size,
             or {"ok": true, "window": null} if none is active.
    """
    if (e := _require(gw, "pygetwindow")):
        return e
    win = gw.getActiveWindow()
    return _ok(window=_window_to_dict(win) if win else None)


class WindowTargetInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    title: Optional[str] = Field(
        default=None, description="Window title (case-insensitive substring match)"
    )
    handle: Optional[int] = Field(
        default=None, description="Exact native window handle (HWND) from list_windows"
    )


class MoveWindowInput(WindowTargetInput):
    x: int = Field(..., description="New left X position in pixels")
    y: int = Field(..., description="New top Y position in pixels")


@mcp.tool(
    name="move_window",
    annotations={
        "title": "Move Window",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def move_window(params: MoveWindowInput) -> str:
    """Move a window to a new top-left screen position.

    Identify the window by `handle` (preferred, exact) or `title` (substring).

    Args:
        params (MoveWindowInput): target window plus new x, y.

    Returns:
        str: JSON with the window's updated geometry, or an error if not found.
    """
    if (e := _require(gw, "pygetwindow")):
        return e
    win = _find_window(params.title, params.handle)
    if win is None:
        return _err("No matching window found.", title=params.title, handle=params.handle)
    try:
        win.moveTo(params.x, params.y)
        return _ok(window=_window_to_dict(win))
    except Exception as exc:
        return _err(f"Failed to move window: {type(exc).__name__}: {exc}")


class ResizeWindowInput(WindowTargetInput):
    width: int = Field(..., description="New width in pixels", ge=1)
    height: int = Field(..., description="New height in pixels", ge=1)


@mcp.tool(
    name="resize_window",
    annotations={
        "title": "Resize Window",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def resize_window(params: ResizeWindowInput) -> str:
    """Resize a window to a new width and height.

    Args:
        params (ResizeWindowInput): target window plus new width, height.

    Returns:
        str: JSON with the window's updated geometry, or an error if not found.
    """
    if (e := _require(gw, "pygetwindow")):
        return e
    win = _find_window(params.title, params.handle)
    if win is None:
        return _err("No matching window found.", title=params.title, handle=params.handle)
    try:
        win.resizeTo(params.width, params.height)
        return _ok(window=_window_to_dict(win))
    except Exception as exc:
        return _err(f"Failed to resize window: {type(exc).__name__}: {exc}")


class WindowActionName(str, Enum):
    FOCUS = "focus"
    MINIMIZE = "minimize"
    MAXIMIZE = "maximize"
    RESTORE = "restore"
    CLOSE = "close"


class WindowActionInput(WindowTargetInput):
    action: WindowActionName = Field(..., description="Window action to perform")


@mcp.tool(
    name="window_action",
    annotations={
        "title": "Window Action",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def window_action(params: WindowActionInput) -> str:
    """Focus, minimize, maximize, restore or close a window.

    `close` is destructive - it sends the window a close request and the app may
    prompt to save. Identify the window by `handle` or `title`.

    Args:
        params (WindowActionInput): target window plus the action to run.

    Returns:
        str: JSON confirming the action, or an error if the window was not found.
    """
    if (e := _require(gw, "pygetwindow")):
        return e
    win = _find_window(params.title, params.handle)
    if win is None:
        return _err("No matching window found.", title=params.title, handle=params.handle)
    try:
        action = params.action
        if action == WindowActionName.FOCUS:
            try:
                win.activate()
            except Exception:
                # activate() is flaky on Windows; restore+minimize toggle as fallback
                win.minimize()
                win.restore()
        elif action == WindowActionName.MINIMIZE:
            win.minimize()
        elif action == WindowActionName.MAXIMIZE:
            win.maximize()
        elif action == WindowActionName.RESTORE:
            win.restore()
        elif action == WindowActionName.CLOSE:
            win.close()
            return _ok(action=action.value, closed=True)
        return _ok(action=action.value, window=_window_to_dict(win))
    except Exception as exc:
        return _err(f"Failed to {params.action.value} window: {type(exc).__name__}: {exc}")


# =========================================================================== #
# PROCESS MANAGEMENT
# =========================================================================== #
class ListProcessesInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    name_filter: Optional[str] = Field(
        default=None, description="Only processes whose name contains this (case-insensitive)"
    )
    sort_by: str = Field(
        default="memory",
        description="Sort key: 'memory', 'cpu', 'name' or 'pid'",
    )
    limit: int = Field(default=50, description="Maximum number of processes to return", ge=1, le=1000)


@mcp.tool(
    name="list_processes",
    annotations={
        "title": "List Processes",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def list_processes(params: ListProcessesInput) -> str:
    """List running processes with pid, name, memory and CPU usage.

    Args:
        params (ListProcessesInput): optional name filter, sort key and limit.

    Returns:
        str: JSON {"ok": true, "count": N, "processes": [{pid, name, username,
             memory_mb, cpu_percent}, ...]}.
    """
    if (e := _require(psutil, "psutil")):
        return e
    procs = []
    for p in psutil.process_iter(["pid", "name", "username", "memory_info", "cpu_percent"]):
        try:
            info = p.info
            name = info.get("name") or ""
            if params.name_filter and params.name_filter.lower() not in name.lower():
                continue
            mem = info.get("memory_info")
            procs.append(
                {
                    "pid": info.get("pid"),
                    "name": name,
                    "username": info.get("username"),
                    "memory_mb": round(mem.rss / (1024 * 1024), 1) if mem else None,
                    "cpu_percent": info.get("cpu_percent"),
                }
            )
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    key = params.sort_by.lower()
    if key == "memory":
        procs.sort(key=lambda x: x["memory_mb"] or 0, reverse=True)
    elif key == "cpu":
        procs.sort(key=lambda x: x["cpu_percent"] or 0, reverse=True)
    elif key == "name":
        procs.sort(key=lambda x: (x["name"] or "").lower())
    elif key == "pid":
        procs.sort(key=lambda x: x["pid"] or 0)

    return _ok(count=len(procs[: params.limit]), processes=procs[: params.limit])


class KillProcessInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    pid: Optional[int] = Field(default=None, description="Process ID to kill", ge=0)
    name: Optional[str] = Field(
        default=None,
        description="Kill ALL processes whose name matches exactly (case-insensitive). "
        "Use with care. Ignored if pid is given.",
    )
    force: bool = Field(
        default=False,
        description="Force kill (SIGKILL/terminate) instead of a graceful terminate request",
    )


@mcp.tool(
    name="kill_process",
    annotations={
        "title": "Kill Process",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def kill_process(params: KillProcessInput) -> str:
    """Kill a process by PID, or all processes matching an exact name.

    Provide either `pid` (preferred) or `name`. `force=true` kills hard; otherwise
    a graceful terminate is requested first. This is destructive and may cause the
    target application to lose unsaved data.

    Args:
        params (KillProcessInput): pid or name, and force flag.

    Returns:
        str: JSON {"ok": true, "killed": [{pid, name}], "count": N}, or an error.
    """
    if (e := _require(psutil, "psutil")):
        return e
    if params.pid is None and not params.name:
        return _err("Provide either 'pid' or 'name'.")

    targets: list[Any] = []
    if params.pid is not None:
        try:
            targets.append(psutil.Process(params.pid))
        except psutil.NoSuchProcess:
            return _err(f"No process with pid {params.pid}.")
    else:
        name_l = params.name.lower()
        for p in psutil.process_iter(["pid", "name"]):
            try:
                if (p.info.get("name") or "").lower() == name_l:
                    targets.append(p)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        if not targets:
            return _err(f"No running process named '{params.name}'.")

    killed = []
    errors = []
    for p in targets:
        try:
            pid, pname = p.pid, p.name()
            if params.force:
                p.kill()
            else:
                p.terminate()
            killed.append({"pid": pid, "name": pname})
        except (psutil.NoSuchProcess, psutil.AccessDenied) as exc:
            errors.append({"pid": getattr(p, "pid", None), "error": type(exc).__name__})

    result: dict[str, Any] = {"killed": killed, "count": len(killed)}
    if errors:
        result["errors"] = errors
    return _ok(**result)


# =========================================================================== #
# SCREENSHOTS + CROPPING
# =========================================================================== #
class ScreenshotInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    monitor: int = Field(
        default=0,
        description="Monitor index: 0 = all monitors combined, 1 = primary, 2 = secondary, ...",
        ge=0,
    )
    region: Optional[list[int]] = Field(
        default=None,
        description="Optional [left, top, width, height] sub-region to capture (overrides monitor framing)",
        min_length=4,
        max_length=4,
    )
    output_path: Optional[str] = Field(
        default=None, description="Where to save the PNG; auto-generated in the capture dir if omitted"
    )


@mcp.tool(
    name="screenshot",
    annotations={
        "title": "Take Screenshot",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def screenshot(params: ScreenshotInput) -> str:
    """Capture a screenshot of a monitor (or a pixel region) and save it as PNG.

    Args:
        params (ScreenshotInput): monitor index, optional region and output path.

    Returns:
        str: JSON {"ok": true, "path": "...", "width": W, "height": H}.
    """
    if (e := _require(mss, "mss")):
        return e
    out = Path(params.output_path) if params.output_path else _capture_dir() / f"screenshot-{_timestamp()}.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    with mss.mss() as sct:
        if params.region:
            left, top, width, height = params.region
            grab_area = {"left": left, "top": top, "width": width, "height": height}
        else:
            if params.monitor >= len(sct.monitors):
                return _err(
                    f"Monitor {params.monitor} not found; {len(sct.monitors) - 1} monitor(s) available."
                )
            grab_area = sct.monitors[params.monitor]
        img = sct.grab(grab_area)
        mss.tools.to_png(img.rgb, img.size, output=str(out))
    return _ok(path=str(out), width=img.size.width, height=img.size.height)


class CropInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    input_path: str = Field(..., description="Path to an existing image to crop", min_length=1)
    left: int = Field(..., description="Left X of the crop box", ge=0)
    top: int = Field(..., description="Top Y of the crop box", ge=0)
    width: int = Field(..., description="Crop width in pixels", ge=1)
    height: int = Field(..., description="Crop height in pixels", ge=1)
    output_path: Optional[str] = Field(
        default=None, description="Where to save the cropped image; auto-generated if omitted"
    )


@mcp.tool(
    name="crop_image",
    annotations={
        "title": "Crop Image",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def crop_image(params: CropInput) -> str:
    """Crop an existing image file to a rectangular sub-region.

    Args:
        params (CropInput): source path, crop box (left/top/width/height), output path.

    Returns:
        str: JSON {"ok": true, "path": "...", "width": W, "height": H}.
    """
    if (e := _require(Image, "pillow")):
        return e
    src = Path(params.input_path)
    if not src.exists():
        return _err(f"Input image not found: {src}")
    out = (
        Path(params.output_path)
        if params.output_path
        else _capture_dir() / f"crop-{_timestamp()}{src.suffix or '.png'}"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(src) as im:
        box = (params.left, params.top, params.left + params.width, params.top + params.height)
        cropped = im.crop(box)
        cropped.save(out)
        w, h = cropped.size
    return _ok(path=str(out), width=w, height=h)


# =========================================================================== #
# SCREEN RECORDING
# =========================================================================== #
class _Recorder:
    """Background thread that captures the screen to an mp4 file."""

    def __init__(self) -> None:
        self.thread: Optional[threading.Thread] = None
        self.stop_event = threading.Event()
        self.path: Optional[str] = None
        self.fps: int = 15
        self.region: Optional[dict[str, int]] = None
        self.monitor: int = 1
        self.frames: int = 0
        self.started_at: Optional[float] = None
        self.error: Optional[str] = None

    @property
    def active(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def _run(self) -> None:
        import imageio.v2 as imageio

        frame_interval = 1.0 / self.fps
        try:
            with mss.mss() as sct:
                area = self.region if self.region else sct.monitors[self.monitor]
                writer = imageio.get_writer(
                    self.path, fps=self.fps, codec="libx264", quality=8, macro_block_size=None
                )
                try:
                    next_t = time.time()
                    while not self.stop_event.is_set():
                        img = sct.grab(area)
                        frame = np.asarray(img)[:, :, :3][:, :, ::-1]  # BGRA -> RGB
                        writer.append_data(frame)
                        self.frames += 1
                        next_t += frame_interval
                        sleep = next_t - time.time()
                        if sleep > 0:
                            time.sleep(sleep)
                        else:
                            next_t = time.time()
                finally:
                    writer.close()
        except Exception as exc:  # pragma: no cover
            self.error = f"{type(exc).__name__}: {exc}"


_recorder = _Recorder()


class StartRecordingInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    fps: int = Field(default=15, description="Frames per second to capture", ge=1, le=60)
    monitor: int = Field(default=1, description="Monitor index to record (1 = primary)", ge=1)
    region: Optional[list[int]] = Field(
        default=None,
        description="Optional [left, top, width, height] region to record instead of a full monitor",
        min_length=4,
        max_length=4,
    )
    output_path: Optional[str] = Field(
        default=None, description="Where to save the mp4; auto-generated in the capture dir if omitted"
    )


@mcp.tool(
    name="start_screen_recording",
    annotations={
        "title": "Start Screen Recording",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def start_screen_recording(params: StartRecordingInput) -> str:
    """Begin recording the screen to an mp4 file in a background thread.

    Only one recording can run at a time. Call `stop_screen_recording` to finish
    and flush the file.

    Args:
        params (StartRecordingInput): fps, monitor, optional region and output path.

    Returns:
        str: JSON {"ok": true, "path": "...", "fps": N, "recording": true}.
    """
    if (e := _require(mss, "mss")):
        return e
    if (e := _require(np, "numpy")):
        return e
    if _recorder.active:
        return _err("A recording is already in progress.", path=_recorder.path)

    out = Path(params.output_path) if params.output_path else _capture_dir() / f"recording-{_timestamp()}.mp4"
    out.parent.mkdir(parents=True, exist_ok=True)

    _recorder.__init__()  # reset state
    _recorder.path = str(out)
    _recorder.fps = params.fps
    _recorder.monitor = params.monitor
    _recorder.region = (
        {"left": params.region[0], "top": params.region[1], "width": params.region[2], "height": params.region[3]}
        if params.region
        else None
    )
    _recorder.started_at = time.time()
    _recorder.stop_event.clear()
    _recorder.thread = threading.Thread(target=_recorder._run, daemon=True)
    _recorder.thread.start()
    time.sleep(0.2)
    if _recorder.error:
        return _err(f"Recording failed to start: {_recorder.error}")
    return _ok(path=str(out), fps=params.fps, monitor=params.monitor, recording=True)


@mcp.tool(
    name="stop_screen_recording",
    annotations={
        "title": "Stop Screen Recording",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def stop_screen_recording() -> str:
    """Stop the active screen recording and finalize the mp4 file.

    Returns:
        str: JSON {"ok": true, "path": "...", "frames": N, "duration_seconds": S}.
    """
    if not _recorder.active and _recorder.path is None:
        return _err("No recording is in progress.")
    _recorder.stop_event.set()
    if _recorder.thread:
        _recorder.thread.join(timeout=30)
    duration = round(time.time() - _recorder.started_at, 2) if _recorder.started_at else None
    if _recorder.error:
        return _err(f"Recording error: {_recorder.error}", path=_recorder.path)
    return _ok(path=_recorder.path, frames=_recorder.frames, duration_seconds=duration)


@mcp.tool(
    name="recording_status",
    annotations={
        "title": "Recording Status",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def recording_status() -> str:
    """Report whether a screen recording is currently active.

    Returns:
        str: JSON {"ok": true, "recording": bool, "path": str|null, "frames": N,
             "elapsed_seconds": S|null}.
    """
    elapsed = round(time.time() - _recorder.started_at, 2) if (_recorder.active and _recorder.started_at) else None
    return _ok(
        recording=_recorder.active,
        path=_recorder.path,
        frames=_recorder.frames,
        elapsed_seconds=elapsed,
    )


# =========================================================================== #
# ADMIN / ELEVATION + BOOT STARTUP
# =========================================================================== #
@mcp.tool(
    name="is_admin",
    annotations={
        "title": "Check Admin Privileges",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def is_admin() -> str:
    """Report whether this server process is running elevated (as Administrator).

    Returns:
        str: JSON {"ok": true, "is_admin": bool, "platform": "Windows"}.
    """
    return _ok(is_admin=_is_admin(), platform=platform.system())


class RunAsAdminInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    command: str = Field(..., description="Command line to execute with elevation (via cmd.exe)", min_length=1)
    timeout: float = Field(default=120.0, description="Max seconds to wait", ge=1, le=3600)


@mcp.tool(
    name="run_command_as_admin",
    annotations={
        "title": "Run Command As Admin",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def run_command_as_admin(params: RunAsAdminInput) -> str:
    """Run a shell command with Administrator privileges.

    If the server is not already elevated this triggers a Windows UAC prompt that
    the user must approve. Output is captured and returned. This runs commands
    with full administrative rights - use with care.

    Args:
        params (RunAsAdminInput): the command line and a timeout.

    Returns:
        str: JSON {"ok": bool, "returncode": int, "output": "...", "elevated_prompt": bool}.
    """
    if os.name != "nt":
        return _err("run_command_as_admin requires Windows.")
    escaped = params.command.replace("'", "''")
    body = f"& $env:ComSpec /c '{escaped}'\nexit $LASTEXITCODE"
    needed_prompt = not _is_admin()
    result = _run_powershell(body, require_admin=True, timeout=params.timeout)
    return json.dumps(
        {
            "ok": result["ok"],
            "returncode": result["returncode"],
            "output": result["output"],
            "elevated_prompt": needed_prompt,
        },
        indent=2,
        default=str,
    )


class InstallStartupInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    run_as_admin: bool = Field(
        default=True,
        description="Register the task to run with highest (Administrator) privileges",
    )
    http: bool = Field(
        default=True,
        description="Start the server in HTTP mode at boot (recommended; a stdio server has no client to talk to at boot)",
    )
    host: str = Field(default=DEFAULT_HTTP_HOST, description="Host for HTTP mode")
    port: int = Field(default=DEFAULT_HTTP_PORT, description="Port for HTTP mode", ge=1, le=65535)


@mcp.tool(
    name="install_startup",
    annotations={
        "title": "Install Boot Startup",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def install_startup(params: InstallStartupInput) -> str:
    """Install a scheduled task so this server starts automatically at user logon.

    By default the task runs with Administrator privileges (RunLevel Highest) and
    launches the server in HTTP mode so it is always available after boot.
    Registering the task requires elevation - a UAC prompt appears if the server
    is not already elevated.

    Args:
        params (InstallStartupInput): run_as_admin, http, host and port options.

    Returns:
        str: JSON {"ok": bool, "output": "...", "task_name": "...", "run_as_admin": bool}.
    """
    if os.name != "nt":
        return _err("install_startup requires Windows.")
    result = _install_startup(params.http, params.host, params.port, params.run_as_admin)
    return json.dumps(
        {
            "ok": result["ok"],
            "task_name": TASK_NAME,
            "run_as_admin": params.run_as_admin,
            "mode": "http" if params.http else "stdio",
            "endpoint": f"http://{params.host}:{params.port}/mcp" if params.http else None,
            "output": result["output"],
        },
        indent=2,
        default=str,
    )


@mcp.tool(
    name="uninstall_startup",
    annotations={
        "title": "Uninstall Boot Startup",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def uninstall_startup() -> str:
    """Remove the boot-startup scheduled task (requires elevation; may prompt UAC).

    Returns:
        str: JSON {"ok": bool, "output": "...", "task_name": "..."}.
    """
    if os.name != "nt":
        return _err("uninstall_startup requires Windows.")
    result = _uninstall_startup()
    return json.dumps(
        {"ok": result["ok"], "task_name": TASK_NAME, "output": result["output"]},
        indent=2,
        default=str,
    )


@mcp.tool(
    name="startup_status",
    annotations={
        "title": "Boot Startup Status",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def startup_status() -> str:
    """Report whether the boot-startup scheduled task is installed and its state.

    Returns:
        str: JSON {"ok": bool, "installed": bool, "details": "...", "task_name": "..."}.
    """
    if os.name != "nt":
        return _err("startup_status requires Windows.")
    result = _startup_status()
    out = result["output"]
    installed = out.startswith("INSTALLED")
    return _ok(installed=installed, details=out, task_name=TASK_NAME)


# =========================================================================== #
# Entry point
# =========================================================================== #
def _serve(http: bool, host: str, port: int) -> None:
    if http:
        mcp.settings.host = host
        mcp.settings.port = port
        mcp.run(transport="streamable-http")
    else:
        mcp.run()


def main() -> None:
    """CLI entry point.

    Default: run the MCP server over stdio (for Claude Code / Codex).
    Subcommands manage the boot-startup scheduled task. Flags switch transport
    and elevation.
    """
    parser = argparse.ArgumentParser(
        prog="lowlevel-computer-use-mcp",
        description="Low-level computer-use MCP server.",
    )
    parser.add_argument("--http", action="store_true", help="Serve over streamable HTTP instead of stdio")
    parser.add_argument("--host", default=DEFAULT_HTTP_HOST, help="Host for --http mode")
    parser.add_argument("--port", type=int, default=DEFAULT_HTTP_PORT, help="Port for --http mode")
    parser.add_argument(
        "--admin",
        action="store_true",
        help="Relaunch the server elevated (UAC) if not already running as Administrator",
    )

    sub = parser.add_subparsers(dest="cmd")
    p_install = sub.add_parser("install-startup", help="Register a logon scheduled task to auto-start the server")
    p_install.add_argument("--no-admin", action="store_true", help="Do not run the task as Administrator")
    p_install.add_argument("--stdio", action="store_true", help="Start in stdio mode instead of HTTP")
    p_install.add_argument("--host", default=DEFAULT_HTTP_HOST)
    p_install.add_argument("--port", type=int, default=DEFAULT_HTTP_PORT)
    sub.add_parser("uninstall-startup", help="Remove the auto-start scheduled task")
    sub.add_parser("startup-status", help="Show the auto-start task status")

    args = parser.parse_args()

    if args.cmd == "install-startup":
        res = _install_startup(
            http=not args.stdio, host=args.host, port=args.port, run_as_admin=not args.no_admin
        )
        print(res["output"])
        sys.exit(0 if res["ok"] else 1)
    if args.cmd == "uninstall-startup":
        res = _uninstall_startup()
        print(res["output"])
        sys.exit(0 if res["ok"] else 1)
    if args.cmd == "startup-status":
        res = _startup_status()
        print(res["output"])
        sys.exit(0 if res["ok"] else 1)

    # Serve mode
    if args.admin and os.name == "nt" and not _is_admin():
        # Relaunch elevated. Note: an elevated process gets its own console, so this
        # is intended for --http mode (a stdio server must be launched elevated by
        # its parent client instead).
        forwarded = [a for a in sys.argv[1:] if a != "--admin"]
        params = subprocess.list2cmdline(["-m", "lowlevel_computer_use_mcp.server", *forwarded])
        ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, params, None, 1)
        sys.exit(0)

    _serve(http=args.http, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
