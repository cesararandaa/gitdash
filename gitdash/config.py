"""Config file loading for GitDash."""

from __future__ import annotations

import os
import re
import sys
import tempfile
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
class CustomCommand:
    name: str
    cmd: str


@dataclass
class RepoGroup:
    name: str
    path: Path
    repos: list[Path] = field(default_factory=list)
    commands: list[CustomCommand] = field(default_factory=list)


@dataclass
class AIConfig:
    provider: str = ""  # "anthropic", "openai", "ollama"
    model: str = ""     # optional override; defaults chosen per provider
    api_key: str = ""   # "env:VAR_NAME" or raw key
    base_url: str = ""  # custom endpoint (required for ollama)

    def resolve_api_key(self) -> str:
        """Resolve the API key, dereferencing env: prefix if present."""
        if self.api_key.startswith("env:"):
            return os.environ.get(self.api_key[4:], "")
        return self.api_key


@dataclass
class Config:
    groups: list[RepoGroup] = field(default_factory=list)
    default_group: str | None = None
    fetch_on_startup: bool = False
    editor: str | None = None
    ai: AIConfig = field(default_factory=AIConfig)

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

    ai_data = data.get("ai", {})
    if ai_data:
        config.ai = AIConfig(
            provider=ai_data.get("provider", ""),
            model=ai_data.get("model", ""),
            api_key=ai_data.get("api_key", ""),
            base_url=ai_data.get("base_url", ""),
        )

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

        commands = [
            CustomCommand(name=c.get("name", ""), cmd=c.get("cmd", ""))
            for c in group_data.get("commands", [])
            if c.get("name") and c.get("cmd")
        ]

        config.groups.append(RepoGroup(name=name, path=path, repos=repos, commands=commands))

    return config


def init_config() -> Path:
    """Create a default config file and return its path."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    if CONFIG_FILE.exists():
        return CONFIG_FILE

    default = """\
# GitDash configuration
# default_group = "work"

# AI commit message generation (BYOK)
# [ai]
# provider = "anthropic"              # "anthropic", "openai", or "ollama"
# model = "claude-sonnet-4-20250514"  # optional: defaults per provider
# api_key = "env:ANTHROPIC_API_KEY"   # "env:VAR_NAME" (recommended) or raw key
# base_url = ""                       # optional: custom endpoint (e.g. for ollama)

[[groups]]
name = "work"
path = "~/code"
# repos = ["repo1", "repo2"]  # optional: omit to auto-discover all repos in path

# Custom commands: one-click shortcuts that run in the terminal
# [[groups.commands]]
# name = "Run tests"
# cmd = "npm test"
#
# [[groups.commands]]
# name = "Deploy"
# cmd = "git push heroku main"

# [[groups]]
# name = "personal"
# path = "~/personal"
"""
    CONFIG_FILE.write_text(default)
    return CONFIG_FILE


def _atomic_write(path: Path, content: str) -> None:
    """Write content to a file atomically via temp-file + rename."""
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", dir=path.parent, suffix=".tmp", delete=False
        ) as tmp:
            tmp_path = tmp.name
            tmp.write(content)
        os.replace(tmp_path, path)
    except BaseException:
        if tmp_path and Path(tmp_path).exists():
            os.unlink(tmp_path)
        raise


def _toml_str(value: str) -> str:
    """Return a TOML-safe double-quoted string."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def save_all_groups(config: "Config") -> None:
    """Rewrite config.toml entirely from a Config object, preserving top-level settings."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    lines: list[str] = []

    if config.default_group:
        lines.append(f"default_group = {_toml_str(config.default_group)}\n")
    if config.fetch_on_startup:
        lines.append("fetch_on_startup = true\n")
    if config.editor:
        lines.append(f"editor = {_toml_str(config.editor)}\n")

    if lines:
        lines.append("\n")

    if config.ai.provider:
        lines.append("[ai]\n")
        lines.append(f"provider = {_toml_str(config.ai.provider)}\n")
        if config.ai.model:
            lines.append(f"model = {_toml_str(config.ai.model)}\n")
        if config.ai.api_key:
            lines.append(f"api_key = {_toml_str(config.ai.api_key)}\n")
        if config.ai.base_url:
            lines.append(f"base_url = {_toml_str(config.ai.base_url)}\n")
        lines.append("\n")

    for g in config.groups:
        lines.append("[[groups]]\n")
        lines.append(f"name = {_toml_str(g.name)}\n")
        try:
            display_path = "~/" + str(g.path.relative_to(Path.home()))
        except ValueError:
            display_path = str(g.path)
        lines.append(f"path = {_toml_str(display_path)}\n")
        if g.repos:
            repos_value = "[" + ", ".join(_toml_str(rp.name) for rp in g.repos) + "]"
            lines.append(f"repos = {repos_value}\n")
        lines.append("\n")
        for cmd in g.commands:
            lines.append("[[groups.commands]]\n")
            lines.append(f"name = {_toml_str(cmd.name)}\n")
            lines.append(f"cmd = {_toml_str(cmd.cmd)}\n")
            lines.append("\n")

    _atomic_write(CONFIG_FILE, "".join(lines))


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

    _atomic_write(CONFIG_FILE, "".join(result))
