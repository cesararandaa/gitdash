"""Config file loading for GitDash."""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:
    try:
        import tomllib
    except ModuleNotFoundError:
        import tomli as tomllib  # type: ignore[no-redef]

CONFIG_DIR = Path.home() / ".config" / "gitdash"
CONFIG_FILE = CONFIG_DIR / "config.toml"


@dataclass
class RepoGroup:
    name: str
    path: Path
    repos: list[Path] = field(default_factory=list)


@dataclass
class Config:
    groups: list[RepoGroup] = field(default_factory=list)
    default_group: str | None = None
    fetch_on_startup: bool = False
    editor: str | None = None

    @property
    def all_repos(self) -> list[Path]:
        repos = []
        for g in self.groups:
            repos.extend(g.repos)
        return repos

    def get_group(self, name: str) -> RepoGroup | None:
        for g in self.groups:
            if g.name == name:
                return g
        return None


def _discover_repos(base: Path) -> list[Path]:
    """Find git repos one level deep under base."""
    if not base.is_dir():
        return []
    repos = []
    for entry in sorted(base.iterdir()):
        if entry.is_dir() and (entry / ".git").exists():
            repos.append(entry)
    return repos


def load_config() -> Config:
    """Load config from ~/.config/gitdash/config.toml."""
    if not CONFIG_FILE.exists():
        return Config()

    with open(CONFIG_FILE, "rb") as f:
        data = tomllib.load(f)

    config = Config()
    config.default_group = data.get("default_group")
    config.fetch_on_startup = data.get("fetch_on_startup", False)
    config.editor = data.get("editor")

    for group_data in data.get("groups", []):
        name = group_data.get("name", "unnamed")
        path = Path(group_data.get("path", ".")).expanduser()
        repos_list = group_data.get("repos")

        if repos_list:
            # Explicit repo list
            repos = [path / r for r in repos_list if (path / r / ".git").exists()]
        else:
            # Auto-discover repos in path
            repos = _discover_repos(path)

        config.groups.append(RepoGroup(name=name, path=path, repos=repos))

    return config


def init_config() -> Path:
    """Create a default config file and return its path."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    if CONFIG_FILE.exists():
        return CONFIG_FILE

    default = """\
# GitDash configuration
# default_group = "work"

[[groups]]
name = "work"
path = "~/code"
# repos = ["repo1", "repo2"]  # optional: omit to auto-discover all repos in path

# [[groups]]
# name = "personal"
# path = "~/personal"
"""
    CONFIG_FILE.write_text(default)
    return CONFIG_FILE


def save_repo_order(group_name: str, repo_paths: list[Path]) -> None:
    """Update the repos list for a group in config.toml, preserving other config."""
    if not CONFIG_FILE.exists():
        raise FileNotFoundError(f"Config file not found: {CONFIG_FILE}")

    text = CONFIG_FILE.read_text()

    # Build the new repos line
    repo_names = [rp.name for rp in repo_paths]
    repos_value = "[" + ", ".join(f'"{name}"' for name in repo_names) + "]"
    repos_line = f"repos = {repos_value}"

    # Find the [[groups]] block with matching name and update/insert repos
    # Strategy: split into lines, find the group, update repos within that block
    lines = text.splitlines(keepends=True)
    result = []
    i = 0
    found = False

    while i < len(lines):
        line = lines[i]

        # Detect start of a [[groups]] block
        if re.match(r'^\s*\[\[groups\]\]\s*$', line.rstrip()):
            block_start = i
            result.append(line)
            i += 1

            # Collect lines in this block until next section or EOF
            block_lines = []
            name_match = False
            repos_line_idx = None

            while i < len(lines):
                bline = lines[i]
                # Stop at next [[...]] or [...] section header
                if re.match(r'^\s*\[', bline.rstrip()):
                    break
                # Check if this is the name line for our target group
                m = re.match(r'^\s*name\s*=\s*"([^"]+)"', bline)
                if m and m.group(1) == group_name:
                    name_match = True
                # Track existing repos line
                if re.match(r'^\s*repos\s*=', bline):
                    repos_line_idx = len(block_lines)
                block_lines.append(bline)
                i += 1

            if name_match:
                found = True
                if repos_line_idx is not None:
                    # Replace existing repos line
                    block_lines[repos_line_idx] = repos_line + "\n"
                else:
                    # Insert repos after the last key in the block
                    block_lines.append(repos_line + "\n")
            result.extend(block_lines)
        else:
            result.append(line)
            i += 1

    if not found:
        raise ValueError(f"Group '{group_name}' not found in config")

    CONFIG_FILE.write_text("".join(result))
