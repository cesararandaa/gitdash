# gitdash

A terminal UI dashboard for managing multiple git repos at once. Built with [Textual](https://textual.textualize.io/) and [GitPython](https://gitpython.readthedocs.io/).

Like the VS Code/Cursor git sidebar, but in your terminal — no editor needed.

## Features

- See all repos, branches, and changed files in a single view
- Fetch, pull, push, commit, stash, diff — per repo or all at once
- Switch or create branches with a filterable picker
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
```

## Keybindings

| Key | Action |
|-----|--------|
| `j` / `Down` | Next repo |
| `k` / `Up` | Previous repo |
| `b` | Switch / create branch |
| `c` | Commit |
| `d` | View diff |
| `s` | Stash / pop |
| `Space` | Collapse / expand repo |
| `F` | Fetch all repos |
| `P` | Pull all repos |
| `r` | Refresh all |
| `q` | Quit |

## License

MIT
