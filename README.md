# lowlevel-computer-use-mcp

A **low-level computer-use MCP server** for Windows (most tools also work on
macOS/Linux). It exposes raw desktop control to any MCP client — Claude Code,
Codex, Claude Desktop, etc. — as a set of well-described tools:

- 🖱️ **Mouse** — move, click, double/right/middle click, drag, scroll, cursor position
- ⌨️ **Keyboard** — type text, press hotkey combinations (Ctrl+C, Alt+Tab, …)
- 🖥️ **Shell commands** — run arbitrary system commands and capture output
- 🪟 **Windows** — list, move, resize, focus, minimize, maximize, restore, close, **show/hide**
- ⚙️ **Processes** — list running processes; kill by PID or name
- 📸 **Screenshots** — all monitors, one monitor, a region, or **one window via PrintWindow**
- ✂️ **Cropping** — crop any saved image to a sub-region
- 🎥 **Screen recording** — record a monitor or region to mp4 in the background
- 🎯 **Background / unfocused targeting** — drive a specific window via Win32 messages **without focusing it**
- 🫥 **Headless GUI** — run real GUI apps on an off-screen desktop; show them only when a human login is needed
- 🛡️ **Run-as-admin** — per-command UAC elevation or whole-server elevation
- 🚀 **Auto-start on boot** — register a logon scheduled task (optionally as admin)
- 🟢 **AutoHotkey add-in** — run AHK scripts; `ControlSend`/`ControlClick` for rock-solid background input
- 🧩 **GUI installer** — one window that installs everything automatically

> ⚠️ **This server performs real, unsandboxed actions on the host machine** —
> clicking, typing, killing processes and running shell/elevated commands with your
> user's privileges. Only register it in environments where that is acceptable.

---

## Tools

| Tool | Description |
|------|-------------|
| `get_screen_size` | Primary screen resolution |
| `get_cursor_position` | Current mouse position |
| `mouse_move` | Move cursor to `(x, y)` |
| `mouse_click` | Click; **`hwnd`/`window_title` → background click (client coords)** |
| `mouse_drag` | Press-drag-release between two points |
| `mouse_scroll` | Scroll the wheel up/down |
| `type_text` | Type text; **`hwnd`/`window_title` → background WM_CHAR** |
| `press_keys` | Press a key / hotkey combo, e.g. `["ctrl","c"]` |
| `run_command` | Run a shell command, capture output/exit code |
| `list_windows` | List top-level windows (title, handle, geometry, state) |
| `get_active_window` | Info about the focused window |
| `move_window` / `resize_window` | Move / resize a window |
| `window_action` | focus / minimize / maximize / restore / close |
| `show_window` / `hide_window` | Bring a window forward (e.g. for login), then hide it again |
| `list_child_windows` | Enumerate a window's child controls (class, text, rect, handle) |
| `win_set_control_text` | Set a control's text via WM_SETTEXT (reliable background text) |
| `win_send_keys` | Post key presses to a window without focusing it |
| `list_processes` / `kill_process` | List processes; kill by PID or name |
| `screenshot` | Monitor/region/**single-window (PrintWindow)** capture to PNG |
| `crop_image` | Crop an existing image to a box |
| `start_screen_recording` / `stop_screen_recording` / `recording_status` | mp4 recording |
| `create_headless_desktop` | Create an off-screen desktop |
| `launch_on_headless_desktop` | Launch a GUI app onto it |
| `list_headless_windows` | List windows on the off-screen desktop |
| `show_headless_desktop` / `hide_headless_desktop` | Temporarily make it interactive (login), then hide |
| `close_headless_desktop` | Release the off-screen desktop handle |
| `ahk_status` | Whether AutoHotkey is installed |
| `run_ahk` | Run an inline AutoHotkey script |
| `ahk_control_send` | AHK ControlSend to a background window/control |
| `is_admin` | Whether the server is running elevated |
| `run_command_as_admin` | Run a shell command elevated (UAC prompt) |
| `install_startup` / `uninstall_startup` / `startup_status` | Boot auto-start |

Every tool returns a JSON string `{"ok": true, ...}` on success or
`{"ok": false, "error": "..."}` on failure.

---

## Quick start (GUI installer — fully automatic)

The easiest path. It installs `uv` if missing, runs `uv sync`, and registers the
server with both Claude Code and Codex automatically on launch.

Clone the repo:

```bash
git clone https://github.com/codingmachineedge/lowlevel-computer-use-mcp.git
```

Enter it:

```bash
cd lowlevel-computer-use-mcp
```

Launch the installer (it auto-runs the full install):

```bash
uv run lowlevel-computer-use-mcp-installer
```

Then **restart Claude Code / Codex** so they spawn the server.

---

## Manual install

Install dependencies and start the stdio server:

```bash
uv run lowlevel-computer-use-mcp
```

Or with pip — install in editable mode:

```bash
pip install -e .
```

Then run it:

```bash
lowlevel-computer-use-mcp
```

Captures (screenshots, recordings) are written to
`~/lowlevel-computer-use-captures` by default. Override with the
`LOWLEVEL_CU_CAPTURE_DIR` environment variable.

---

## Registering with clients

Replace the path below with wherever you cloned this repo.

### Claude Code

Register at user scope (one line):

```bash
claude mcp add lowlevel-computer-use --scope user -- uv run --directory "C:\path\to\lowlevel-computer-use-mcp" lowlevel-computer-use-mcp
```

Or add this to `~/.claude.json` under `mcpServers`:

```json
{
  "mcpServers": {
    "lowlevel-computer-use": {
      "command": "uv",
      "args": ["run", "--directory", "C:\\path\\to\\lowlevel-computer-use-mcp", "lowlevel-computer-use-mcp"]
    }
  }
}
```

### Codex (OpenAI Codex CLI)

Add this block to `~/.codex/config.toml`:

```toml
[mcp_servers.lowlevel-computer-use]
command = "uv"
args = ["run", "--directory", "C:\\path\\to\\lowlevel-computer-use-mcp", "lowlevel-computer-use-mcp"]
startup_timeout_sec = 60
```

---

## Background / unfocused window targeting

`mouse_click`, `type_text` and `screenshot` accept `hwnd` or `window_title`. When
set, input is delivered to that exact window via Win32 messages **without focusing
or foregrounding it**, and `screenshot` uses `PrintWindow` so the window is captured
even if it's behind others, minimized, or on an off-screen desktop.

Typical flow:

1. Find the window:

```jsonc
list_windows { "title_filter": "Notepad" }
```

2. Find the control to target:

```jsonc
list_child_windows { "window_title": "Notepad" }
```

3. Set its text in the background (most reliable for edit controls):

```jsonc
win_set_control_text { "hwnd": 23924320, "text": "typed without focus" }
```

4. Or click it in the background (x/y are **client** coords of the window):

```jsonc
mouse_click { "window_title": "Notepad", "x": 200, "y": 120 }
```

5. See the result without bringing it forward:

```jsonc
screenshot { "window_title": "Notepad" }
```

> Caveat: message-based input is ignored by some apps (raw input / DirectInput /
> physical-key-state checks). For those, use the **AutoHotkey** `ahk_control_send`
> tool, or bring the window forward briefly.

---

## Headless-but-with-GUI mode

Run a real GUI app on an off-screen Win32 desktop so it never touches your visible
desktop, then automate and screenshot it via the background tools.

Create the off-screen desktop:

```jsonc
create_headless_desktop { "name": "work" }
```

Launch an app onto it:

```jsonc
launch_on_headless_desktop { "name": "work", "command": "notepad.exe" }
```

List its windows (to get handles):

```jsonc
list_headless_windows { "name": "work" }
```

Capture a window on it (works even though it's off-screen):

```jsonc
screenshot { "hwnd": 2495156 }
```

### Showing it for an interactive login, then hiding again

Some steps (sign-in) need a human. Temporarily switch the live screen to the
off-screen desktop:

```jsonc
show_headless_desktop { "name": "work" }
```

…let the user log in, then switch back to the normal desktop:

```jsonc
hide_headless_desktop { "name": "work" }
```

For an ordinary hidden window on the normal desktop, use `show_window` /
`hide_window` instead:

```jsonc
show_window { "window_title": "My App" }
```

```jsonc
hide_window { "window_title": "My App" }
```

---

## AutoHotkey add-in

Optional but powerful. AHK's `ControlSend`/`ControlClick` drive background windows
very reliably, and `run_ahk` is a full scripting escape hatch.

Install AutoHotkey (one line):

```bash
winget install -e --id AutoHotkey.AutoHotkey
```

Check the server can find it:

```jsonc
ahk_status {}
```

Send text to a background window by HWND:

```jsonc
ahk_control_send { "text": "hello", "window": "ahk_id 0x1A2B3C" }
```

Run an arbitrary AHK script (must call `ExitApp`):

```jsonc
run_ahk { "code": "ControlSendText \"hi\", , \"ahk_exe notepad.exe\"\nExitApp" }
```

Point the server at a specific AHK exe by setting `LOWLEVEL_CU_AHK` to its path.

---

## Macros — save repeated sequences as Skills

When you run a multi-step UI sequence the user is likely to repeat, **don't leave
it as ad-hoc tool calls — capture it as a reusable macro Skill.** The server tells
agents to do this automatically; see
[`macros/MACRO_SKILL_TEMPLATE.md`](macros/MACRO_SKILL_TEMPLATE.md) for the template
and rules (resolve handles at run time, prefer background tools, parameterize the
variable parts, verify with a screenshot).

---

## Run-as-admin mode

Per-command elevation — call `run_command_as_admin` (UAC prompt unless already
elevated):

```jsonc
run_command_as_admin { "command": "net session" }
```

Or run the whole server elevated (intended for HTTP mode):

```bash
uv run lowlevel-computer-use-mcp --http --admin
```

Check elevation:

```jsonc
is_admin {}
```

---

## Auto-start on boot

Register a logon Scheduled Task (admin + HTTP by default):

```bash
uv run lowlevel-computer-use-mcp install-startup
```

Install without admin privileges:

```bash
uv run lowlevel-computer-use-mcp install-startup --no-admin
```

Use a custom port:

```bash
uv run lowlevel-computer-use-mcp install-startup --port 9000
```

Check the task status:

```bash
uv run lowlevel-computer-use-mcp startup-status
```

Remove it:

```bash
uv run lowlevel-computer-use-mcp uninstall-startup
```

Once the boot service runs in HTTP mode, point a client at it as a remote MCP
server:

```bash
claude mcp add --transport http lowlevel-computer-use-boot http://127.0.0.1:8765/mcp
```

The task uses an **interactive logon** trigger (not SYSTEM) so the
desktop-automation tools keep access to your session.

---

## Requirements

- **Python 3.10+**
- **[uv](https://docs.astral.sh/uv/)** (recommended; the GUI installer can bootstrap it)
- Windows is the primary target. Mouse/keyboard via `pyautogui`; windows via
  `pygetwindow`; background input + capture + headless desktop via `ctypes`/Win32
  (`winio.py`); screenshots via `mss`; recording via `imageio` + bundled ffmpeg.

## Safety notes

- `run_command`, `run_command_as_admin`, `kill_process`, `run_ahk` and
  `window_action(close)` are marked **destructive**.
- `pyautogui`'s fail-safe is disabled so automation isn't interrupted by the cursor
  reaching a screen corner; be deliberate with coordinates.
- The server has no authentication of its own — it trusts the MCP client that spawns it.

## License

MIT
