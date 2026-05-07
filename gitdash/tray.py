"""System tray companion for gitdash (RepoBar-style).

Run with `gitdash-tray` once installed with the `tray` extra:

    pip install -e '.[tray]'
    gitdash-tray

Shows a dropdown of repos with dirty / ahead / behind state and an
"Open dashboard" item that launches the full TUI in a terminal.

Backend: pystray (Linux AppIndicator/GTK, macOS, Windows).
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import threading
from pathlib import Path

from gitdash.config import CONFIG_DIR, Config, RepoGroup, load_config
from gitdash.status import collect_statuses, summarize


REFRESH_SECONDS = 60
USER_ICON_PATH = CONFIG_DIR / "tray-icon.png"


def _format_repo_label(s: dict) -> str:
    """One-line per-repo label for the menu."""
    if s.get("error"):
        return f"⚠ {s['name']}  (error)"
    bits = [s["name"]]
    if s.get("conflicted"):
        bits.append("CONFLICT")
    counts = []
    staged = len(s.get("staged", []))
    unstaged = len(s.get("unstaged", []))
    untracked = len(s.get("untracked", []))
    if staged:
        counts.append(f"✓{staged}")
    if unstaged:
        counts.append(f"✎{unstaged}")
    if untracked:
        counts.append(f"+{untracked}")
    if counts:
        bits.append(" ".join(counts))
    sync = []
    if s.get("ahead"):
        sync.append(f"↑{s['ahead']}")
    if s.get("behind"):
        sync.append(f"↓{s['behind']}")
    if sync:
        bits.append("".join(sync))
    if not s.get("dirty") and not s.get("ahead") and not s.get("behind"):
        bits.append("✓ clean")
    return "  ".join(bits)


def _format_summary_label(group: RepoGroup, summary: dict) -> str:
    """Top-of-menu summary line."""
    parts = [f"{group.name}: {summary['total']} repos"]
    if summary["dirty"]:
        parts.append(f"{summary['dirty']} dirty")
    if summary["ahead"]:
        parts.append(f"↑{summary['ahead']}")
    if summary["behind"]:
        parts.append(f"↓{summary['behind']}")
    if summary["conflicts"]:
        parts.append(f"{summary['conflicts']} conflict")
    if summary["errors"]:
        parts.append(f"{summary['errors']} err")
    return "  —  ".join(parts) if len(parts) > 1 else parts[0]


def _detect_terminal() -> list[str] | None:
    """Pick a terminal emulator command prefix that runs `gitdash` in a window."""
    candidates = [
        ("kitty", ["kitty", "--"]),
        ("alacritty", ["alacritty", "-e"]),
        ("wezterm", ["wezterm", "start", "--"]),
        ("gnome-terminal", ["gnome-terminal", "--"]),
        ("konsole", ["konsole", "-e"]),
        ("xterm", ["xterm", "-e"]),
    ]
    for binary, prefix in candidates:
        if shutil.which(binary):
            return prefix
    return None


def _open_dashboard(group: RepoGroup, repo_name: str | None = None) -> None:
    """Spawn `gitdash --group <name> [--repo <repo>]` in a new terminal window."""
    prefix = _detect_terminal()
    cmd = [sys.executable, "-m", "gitdash.app", "--group", group.name]
    if repo_name:
        cmd += ["--repo", repo_name]
    if prefix is None:
        # Fallback: launch detached without a terminal — useless on most setups,
        # but better than crashing the tray.
        subprocess.Popen(cmd, start_new_session=True)
        return
    subprocess.Popen([*prefix, *cmd], start_new_session=True)


def _make_icon_image(dirty: bool):
    """Tray icon. Uses ~/.config/gitdash/tray-icon.png if present, else draws a
    branch-fork glyph tinted by dirty state."""
    from PIL import Image, ImageDraw

    if USER_ICON_PATH.exists():
        try:
            with Image.open(USER_ICON_PATH) as img:
                return img.convert("RGBA").copy()
        except Exception:  # noqa: BLE001
            pass  # fall back to generated glyph

    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Tinted background pill so the glyph reads at a glance.
    bg = (220, 80, 80, 255) if dirty else (90, 200, 120, 255)
    draw.rounded_rectangle((4, 4, size - 4, size - 4), radius=14, fill=bg)

    # Branch-fork glyph: trunk on the left, branch peeling off to the right.
    fg = (255, 255, 255, 255)
    line_w = 5
    # Trunk (vertical line, left side)
    draw.line([(22, 14), (22, 50)], fill=fg, width=line_w)
    # Branch curve (trunk → up-right node)
    draw.line([(22, 32), (42, 22)], fill=fg, width=line_w)
    # Three nodes (filled circles)
    for cx, cy in [(22, 14), (22, 50), (42, 22)]:
        draw.ellipse((cx - 5, cy - 5, cx + 5, cy + 5), fill=fg)
    return img


class TrayApp:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.group: RepoGroup = self._pick_initial_group()
        self.statuses: list[dict] = []
        self.summary: dict = {"total": 0, "dirty": 0, "ahead": 0, "behind": 0, "conflicts": 0, "errors": 0}
        self._stop = threading.Event()
        self._refresh_lock = threading.Lock()
        self._icon = None  # set in run()

    def _pick_initial_group(self) -> RepoGroup:
        if self.config.default_group:
            g = self.config.get_group(self.config.default_group)
            if g:
                return g
        return self.config.groups[0]

    # ------- data refresh -------

    def refresh(self) -> None:
        # Background timer + pystray event thread can both call this; the lock
        # ensures the menu, icon, and statuses always reflect the same group.
        with self._refresh_lock:
            group = self.group
            statuses = collect_statuses(group.repos)
            summary = summarize(statuses)
            self.statuses = statuses
            self.summary = summary
            if self._icon is not None:
                self._icon.icon = _make_icon_image(dirty=summary["dirty"] > 0)
                self._icon.title = _format_summary_label(group, summary)
                self._icon.menu = self._build_menu()

    def _refresh_loop(self) -> None:
        while not self._stop.wait(REFRESH_SECONDS):
            try:
                self.refresh()
            except Exception:  # noqa: BLE001
                pass

    # ------- menu -------

    def _build_menu(self):
        from pystray import Menu, MenuItem

        items: list = [
            MenuItem(_format_summary_label(self.group, self.summary), None, enabled=False),
            Menu.SEPARATOR,
        ]
        for s in self.statuses:
            items.append(MenuItem(_format_repo_label(s), self._on_repo_click(s)))
        items.append(Menu.SEPARATOR)

        if len(self.config.groups) > 1:
            group_items = [
                MenuItem(
                    g.name,
                    self._on_switch_group(g),
                    checked=lambda _i, g=g: g.name == self.group.name,
                    radio=True,
                )
                for g in self.config.groups
            ]
            items.append(MenuItem("Group", Menu(*group_items)))

        items.append(MenuItem("Open dashboard", self._on_open_dashboard, default=True))
        items.append(MenuItem("Refresh", self._on_refresh))
        items.append(MenuItem("Quit", self._on_quit))
        return Menu(*items)

    # ------- actions -------

    def _on_repo_click(self, s: dict):
        def handler(_icon, _item):
            if s.get("error"):
                _open_dashboard(self.group)
                return
            _open_dashboard(self.group, repo_name=s["name"])
        return handler

    def _on_switch_group(self, g: RepoGroup):
        def handler(_icon, _item):
            self.group = g
            self.refresh()
        return handler

    def _on_open_dashboard(self, _icon, _item):
        _open_dashboard(self.group)

    def _on_refresh(self, _icon, _item):
        self.refresh()

    def _on_quit(self, icon, _item):
        self._stop.set()
        icon.stop()

    # ------- entry point -------

    def run(self) -> None:
        from pystray import Icon

        self.refresh()  # populate before first paint
        self._icon = Icon(
            "gitdash",
            icon=_make_icon_image(dirty=self.summary["dirty"] > 0),
            title=_format_summary_label(self.group, self.summary),
            menu=self._build_menu(),
        )
        threading.Thread(target=self._refresh_loop, daemon=True).start()
        self._icon.run()


def main() -> None:
    try:
        import pystray  # noqa: F401
        import PIL  # noqa: F401
    except ImportError:
        print("gitdash-tray needs the `tray` extra:")
        print("    pip install 'gitdash[tray]'")
        sys.exit(1)

    config = load_config()
    if not config.groups:
        print("No groups configured. Run `gitdash --init` first.")
        sys.exit(1)

    TrayApp(config).run()


if __name__ == "__main__":
    main()
