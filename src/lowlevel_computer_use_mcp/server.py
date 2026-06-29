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

import json
import os
import platform
import subprocess
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
# Entry point
# =========================================================================== #
def main() -> None:
    """Run the MCP server over stdio."""
    mcp.run()


if __name__ == "__main__":
    main()
