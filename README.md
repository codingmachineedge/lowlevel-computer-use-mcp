# lowlevel-computer-use-mcp

A **low-level computer-use MCP server** for Windows (works on macOS/Linux for most
tools too). It exposes raw desktop control to any MCP client — Claude Code, Codex,
Claude Desktop, etc. — as a set of well-described tools:

- 🖱️ **Mouse** — move, click, double/right/middle click, drag, scroll, cursor position
- ⌨️ **Keyboard** — type text, press hotkey combinations (Ctrl+C, Alt+Tab, …)
- 🖥️ **Shell commands** — run arbitrary system commands and capture stdout/stderr/exit code
- 🪟 **Windows** — list, move, resize, focus, minimize, maximize, restore, close
- ⚙️ **Processes** — list running processes; kill by PID or name
- 📸 **Screenshots** — capture all monitors, one monitor, or a pixel region (PNG)
- ✂️ **Cropping** — crop any saved image to a sub-region
- 🎥 **Screen recording** — record a monitor or region to an mp4 in the background

> ⚠️ **This server performs real, unsandboxed actions on the host machine** —
> clicking, typing, killing processes and running shell commands with your user's
> privileges. Only register it in environments where that is acceptable.

---

## Tools

| Tool | Description |
|------|-------------|
| `get_screen_size` | Primary screen resolution |
| `get_cursor_position` | Current mouse position |
| `mouse_move` | Move cursor to `(x, y)` |
| `mouse_click` | Click (left/right/middle), with click count for double-clicks |
| `mouse_drag` | Press-drag-release between two points |
| `mouse_scroll` | Scroll the wheel up/down |
| `type_text` | Type a string at the current focus |
| `press_keys` | Press a key / hotkey combo, e.g. `["ctrl","c"]` |
| `run_command` | Run a shell command, capture output, exit code, timeout |
| `list_windows` | List top-level windows (title, handle, geometry, state) |
| `get_active_window` | Info about the focused window |
| `move_window` | Move a window (by handle or title) |
| `resize_window` | Resize a window |
| `window_action` | `focus` / `minimize` / `maximize` / `restore` / `close` |
| `list_processes` | List processes (pid, name, memory, cpu), sortable |
| `kill_process` | Kill by PID, or all by exact name; graceful or forced |
| `screenshot` | Capture a monitor or region to PNG |
| `crop_image` | Crop an existing image to a box |
| `start_screen_recording` | Start recording to mp4 (background thread) |
| `stop_screen_recording` | Stop & finalize the mp4 |
| `recording_status` | Whether a recording is active |

Every tool returns a JSON string of the form `{"ok": true, ...}` on success or
`{"ok": false, "error": "..."}` on failure.

---

## Requirements

- **Python 3.10+**
- **[uv](https://docs.astral.sh/uv/)** (recommended) — handles the virtualenv and
  dependencies automatically. (Plain `pip` also works.)
- Windows is the primary target. Window management uses `pygetwindow` (Windows-best);
  mouse/keyboard via `pyautogui`; screenshots via `mss`; recording via
  `imageio` + `imageio-ffmpeg` (bundled ffmpeg, no separate install).

## Install / run

Clone and let `uv` resolve everything on first run:

```bash
git clone https://github.com/<your-org>/lowlevel-computer-use-mcp.git
cd lowlevel-computer-use-mcp
uv run lowlevel-computer-use-mcp     # starts the stdio MCP server
```

Or with pip:

```bash
pip install -e .
lowlevel-computer-use-mcp
```

Captured screenshots and recordings are written to
`~/lowlevel-computer-use-captures` by default. Override with the
`LOWLEVEL_CU_CAPTURE_DIR` environment variable.

---

## Registering with clients

Replace the directory path below with wherever you cloned this repo.

### Claude Code

Either run:

```bash
claude mcp add lowlevel-computer-use -- uv run --directory "C:\path\to\lowlevel-computer-use-mcp" lowlevel-computer-use-mcp
```

…or add it to your `~/.claude.json` under `mcpServers`:

```json
{
  "mcpServers": {
    "lowlevel-computer-use": {
      "command": "uv",
      "args": [
        "run",
        "--directory",
        "C:\\path\\to\\lowlevel-computer-use-mcp",
        "lowlevel-computer-use-mcp"
      ]
    }
  }
}
```

### Codex (OpenAI Codex CLI)

Add to `~/.codex/config.toml`:

```toml
[mcp_servers.lowlevel-computer-use]
command = "uv"
args = ["run", "--directory", "C:\\path\\to\\lowlevel-computer-use-mcp", "lowlevel-computer-use-mcp"]
```

Then restart the client. The tools appear namespaced under the server name.

---

## Example calls

```jsonc
// Take a screenshot of the primary monitor
screenshot { "monitor": 1 }

// Crop the top-left 400x300 of it
crop_image { "input_path": "C:\\Users\\me\\lowlevel-computer-use-captures\\screenshot-...png",
             "left": 0, "top": 0, "width": 400, "height": 300 }

// Move the mouse and double-click
mouse_click { "x": 960, "y": 540, "clicks": 2 }

// Run a command
run_command { "command": "ipconfig /all" }

// Move a window by title
move_window { "title": "Notepad", "x": 100, "y": 100 }

// Kill a process by name (forced)
kill_process { "name": "notepad.exe", "force": true }

// Record the screen for a few seconds
start_screen_recording { "fps": 15, "monitor": 1 }
// ...later...
stop_screen_recording {}
```

---

## Safety notes

- `run_command`, `kill_process` and `window_action(close)` are marked **destructive**.
- `pyautogui`'s fail-safe is disabled so automation isn't interrupted by the cursor
  reaching a screen corner; be deliberate with coordinates.
- The server has no authentication of its own — it trusts the MCP client that spawns it.

## License

MIT
