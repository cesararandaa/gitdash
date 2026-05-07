# gitdash

A terminal UI dashboard for managing multiple git repos at once. Built with [Textual](https://textual.textualize.io/) and [GitPython](https://gitpython.readthedocs.io/).

Like the VS Code/Cursor git sidebar, but in your terminal — no editor needed.

## Features

- See all repos, branches, and changed files in a single view
- Changes count visible in repo header even when collapsed
- Fetch, pull, push, commit, stash, diff — per repo or all at once
- Smart sync: auto-stashes local changes before pulling, restores after
- Switch or create branches with a filterable picker (local + remote)
- Per-file diff viewer with file list and filter, syntax-highlighted
- Revert changes per file or per repo
- Stash manager: push, pop, apply, or drop individual stashes
- Commit log viewer with per-commit diffs, syntax-highlighted
- Markdown file viewer for `.md` files
- Stage individual files with toggle picker
- Open files or repos in your editor (`e` key)
- Search across all repos with `git grep` (`/` key)
- Git operations log for debugging (`!` key)
- Built-in help overlay with keybindings and status cues (`?` key)
- Group switcher to jump between repo sets on the fly
- In-app group editor to add, remove, reorder, and configure groups
- In-app terminal panel scoped to the focused repo directory
- Undo last commit (soft reset, only before push)
- Create pull requests with AI-generated title and description (requires `gh` CLI)
- Auto-refresh every 60 seconds
- Fetch on startup (configurable)
- AI-generated commit messages (BYOK: Claude, OpenAI, or Ollama)
- Fully keyboard-driven

## Install

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/cesararandaa/gitdash.git
cd gitdash
```

### Quick start (pass a path)

```bash
uv run python -m gitdash.app ~/code/myproject
```

### Config file (recommended)

```bash
uv run python -m gitdash.app --init   # creates ~/.config/gitdash/config.toml
```

Edit the config to define your repo groups:

```toml
default_group = "work"
fetch_on_startup = true
editor = "subl"  # or "code", "cursor", "nvim", etc.

[[groups]]
name = "work"
path = "~/code/work"
# auto-discovers all git repos in this directory

[[groups]]
name = "personal"
path = "~/code/personal"
repos = ["blog", "dotfiles"]  # or list specific repos
```

Then just run:

```bash
uv run python -m gitdash.app              # opens default group
uv run python -m gitdash.app --group personal  # opens a specific group
uv run python -m gitdash.app --fetch      # fetch all on startup
```

## AI commit messages (optional)

gitdash can auto-generate commit messages using an AI provider of your choice. When you press `c` to commit, the input will pre-fill with a suggested message based on your staged diff. You can edit it, replace it, or just press Enter to accept.

### Setup

1. Install the SDK for your provider:

```bash
pip install anthropic   # for Claude
pip install openai      # for OpenAI
# Ollama needs no pip package — just a running Ollama instance
```

2. Add an `[ai]` section to `~/.config/gitdash/config.toml`:

```toml
[ai]
provider = "anthropic"
api_key = "env:ANTHROPIC_API_KEY"
```

3. Set your API key as an environment variable:

```bash
export ANTHROPIC_API_KEY="sk-ant-..."
```

### Provider examples

**Claude (Anthropic)**

```toml
[ai]
provider = "anthropic"
model = "claude-sonnet-4-20250514"  # optional, this is the default
api_key = "env:ANTHROPIC_API_KEY"
```

**OpenAI**

```toml
[ai]
provider = "openai"
model = "gpt-4o-mini"  # optional, this is the default
api_key = "env:OPENAI_API_KEY"
```

**Ollama (local, no API key needed)**

```toml
[ai]
provider = "ollama"
model = "llama3"  # optional, this is the default
base_url = "http://localhost:11434"  # optional, this is the default
```

### Configuration reference

| Field | Required | Description |
|-------|----------|-------------|
| `provider` | yes | `"anthropic"`, `"openai"`, or `"ollama"` |
| `model` | no | Model name. Defaults: `claude-sonnet-4-20250514`, `gpt-4o-mini`, `llama3` |
| `api_key` | yes* | `"env:VAR_NAME"` (recommended) or a raw API key. *Not needed for Ollama. |
| `base_url` | no | Custom endpoint. Only needed for Ollama or self-hosted providers. |

> **Security tip:** Use `api_key = "env:VAR_NAME"` to reference an environment variable instead of putting your key directly in the config file. This way your key never touches disk.

If no `[ai]` section is present, the feature is silently disabled — everything works as before.

## Tray companion (optional)

`gitdash-tray` is a small system-tray indicator that shows aggregate repo status (dirty count, ahead/behind, conflicts) at a glance and opens the full TUI on click — like RepoBar for git working trees.

```bash
pip install -e '.[tray]'   # adds pystray + Pillow
gitdash-tray
```

Menu items:
- summary line (`<group>: N repos — M dirty — ↑X ↓Y`)
- one row per repo with its branch state — clicking opens the TUI focused on that repo (`gitdash --repo <name>`)
- group submenu (when more than one group is configured)
- `Open dashboard`, `Refresh`, `Quit`

**Autostart on Linux** — copy the sample unit:

```bash
cp contrib/gitdash-tray.desktop ~/.config/autostart/
```

**Custom icon** — drop a 64×64 PNG at `~/.config/gitdash/tray-icon.png` to override the built-in branch glyph.

The tray reuses your existing gitdash config (`~/.config/gitdash/config.toml`). Refresh runs every 60 s in a background thread.

**Modern Linux desktops (Wayland / Hyprland / GNOME / KDE)** use the StatusNotifierItem (SNI) protocol, which pystray reaches via PyGObject + AyatanaAppIndicator. Install the system libraries (Arch: `sudo pacman -S python-gobject libayatana-appindicator`) and create the venv pinned to **system** Python with `--system-site-packages` so pystray can import `gi`:

```bash
uv venv --python /usr/bin/python --system-site-packages
uv pip install -e '.[tray]'
uv run gitdash-tray
```

Without this, pystray falls back to the legacy XEmbed Xorg backend, which can't dock into waybar / GNOME Shell / KDE plasma trays and prints `Failed to dock icon`.

## Keybindings

| Key | Action |
|-----|--------|
| `j` / `Down` | Next repo |
| `k` / `Up` | Previous repo |
| `a` | Stage / unstage individual files |
| `b` | Switch / create branch (local + remote) |
| `c` | Commit |
| `d` | Per-file diff viewer |
| `e` | Open repo in editor |
| `l` | Commit log viewer |
| `p` | Create pull request (pushes branch, AI-generated title/description) |
| `s` | Stash manager (push, pop, apply, drop) |
| `t` | Toggle inline terminal (runs in focused repo dir) |
| `u` | Undo last commit (before push) |
| `x` | Revert all changes in repo |
| `/` | Search across all repos |
| `!` | Toggle git operations log |
| `g` | Switch repo group |
| `G` (Shift+G) | Edit groups (add, remove, reorder, set paths) |
| `?` | Show help |
| `J` (Shift+J) | Move repo down |
| `K` (Shift+K) | Move repo up |
| `S` (Shift+S) | Save repo order to config |
| `Space` | Collapse / expand repo |
| `F` | Fetch all repos |
| `P` | Pull all repos |
| `r` | Refresh all |
| `q` | Quit |

Clicking a file in the changes tree opens its diff (or renders it if `.md`). Falls back to `$EDITOR` if no editor is set in config.

The sync bar shows pull/push status and auto-stashes local changes when pulling.

Repo headers now show `staged/mod/new` change counts, `↑/↓` sync counts, and flags like `CONFLICT`, `NO-UPSTREAM`, and `CLEAN` for faster scanning.

## License

MIT
