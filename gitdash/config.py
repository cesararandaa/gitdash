"""Config file loading for GitDash."""

from __future__ import annotations

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
