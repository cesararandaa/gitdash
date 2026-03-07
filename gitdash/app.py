"""GitDash - Multi-repo git TUI dashboard."""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from git import Repo, GitCommandError, InvalidGitRepositoryError
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Header,
    Input,
    Label,
    ListItem,
    ListView,
    Markdown,
    RichLog,
    Static,
    Tree,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def find_repos(base: Path) -> list[Path]:
    """Find all git repos (one level deep) under *base*."""
    repos = []
    for entry in sorted(base.iterdir()):
        if entry.is_dir() and (entry / ".git").exists():
            repos.append(entry)
    return repos


def short_status(repo: Repo) -> dict:
    """Return a dict summarising the repo status."""
    branch = repo.active_branch.name if not repo.head.is_detached else "DETACHED"
    tracking = None
    ahead = behind = 0
    try:
        tb = repo.active_branch.tracking_branch()
        if tb:
            tracking = tb.name
            commits_behind = list(repo.iter_commits(f"{branch}..{tb.name}"))
            commits_ahead = list(repo.iter_commits(f"{tb.name}..{branch}"))
            ahead = len(commits_ahead)
            behind = len(commits_behind)
    except (ValueError, GitCommandError):
        pass

    staged = [d.a_path for d in repo.index.diff("HEAD")] if repo.head.is_valid() else []
    unstaged = [d.a_path for d in repo.index.diff(None)]
    untracked = repo.untracked_files
    stashes = len(list(repo.git.stash("list").splitlines())) if repo.git.stash("list") else 0
    conflicted = bool(repo.index.unmerged_blobs())

    return {
        "branch": branch,
        "tracking": tracking,
        "ahead": ahead,
        "behind": behind,
        "staged": staged,
        "unstaged": unstaged,
        "untracked": untracked,
        "stashes": stashes,
        "conflicted": conflicted,
        "dirty": bool(staged or unstaged or untracked),
        "detached": repo.head.is_detached,
    }


# ---------------------------------------------------------------------------
# Modals
# ---------------------------------------------------------------------------

class ConfirmModal(ModalScreen[bool]):
    """Simple yes/no confirmation modal."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(
        self,
        message: str,
        details: str | None = None,
        confirm_label: str = "Yes",
        cancel_label: str = "Cancel",
        confirm_variant: str = "error",
    ) -> None:
        super().__init__()
        self.message = message
        self.details = details
        self.confirm_label = confirm_label
        self.cancel_label = cancel_label
        self.confirm_variant = confirm_variant

    def compose(self) -> ComposeResult:
        with Vertical(id="commit-dialog"):
            yield Label(self.message, id="commit-title")
            if self.details:
                yield Static(self.details, id="confirm-details")
            with Horizontal(id="commit-buttons"):
                yield Button(self.confirm_label, variant=self.confirm_variant, id="btn-yes")
                yield Button(self.cancel_label, variant="primary", id="btn-no")

    def on_mount(self) -> None:
        self.query_one("#btn-no", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "btn-yes")

    def action_cancel(self) -> None:
        self.dismiss(False)


class CommitModal(ModalScreen[str | None]):
    """Modal to enter a commit message."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def compose(self) -> ComposeResult:
        with Vertical(id="commit-dialog"):
            yield Label("Commit Message", id="commit-title")
            yield Input(placeholder="Enter commit message...", id="commit-input")
            with Horizontal(id="commit-buttons"):
                yield Button("Commit", variant="success", id="btn-commit")
                yield Button("Cancel", variant="error", id="btn-cancel")

    def on_mount(self) -> None:
        self.query_one("#commit-input", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-commit":
            msg = self.query_one("#commit-input", Input).value.strip()
            self.dismiss(msg if msg else None)
        else:
            self.dismiss(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        msg = event.value.strip()
        self.dismiss(msg if msg else None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class MessageModal(ModalScreen[None]):
    """Generic read-only modal for help and operation summaries."""

    BINDINGS = [Binding("escape", "close", "Close"), Binding("q", "close", "Close")]

    def __init__(self, title: str, content: str) -> None:
        super().__init__()
        self.title_text = title
        self.content = content

    def compose(self) -> ComposeResult:
        with Vertical(id="diff-dialog"):
            yield Label(self.title_text, id="diff-title")
            yield Markdown(self.content, id="md-viewer")
            yield Button("Close", variant="primary", id="btn-close")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.app.pop_screen()

    def action_close(self) -> None:
        self.app.pop_screen()


class ShortcutBar(Vertical):
    """Curated bottom bar showing the primary shortcuts."""

    REPO_ITEMS: list[tuple[str, str]] = [
        ("a", "stage"),
        ("b", "branch"),
        ("c", "commit"),
        ("d", "diff"),
        ("e", "edit"),
        ("l", "log"),
        ("s", "stash"),
        ("x", "discard"),
    ]
    GLOBAL_ITEMS: list[tuple[str, str]] = [
        ("space", "toggle"),
        ("j/k", "move"),
        ("J/K", "reorder"),
        ("/", "search"),
        ("F", "fetch"),
        ("P", "pull"),
        ("g", "group"),
        ("r", "refresh"),
        ("!", "ops log"),
        ("?", "help"),
        ("q", "quit"),
    ]

    def compose(self) -> ComposeResult:
        yield Static(id="shortcut-repo", classes="shortcut-line")
        yield Static(id="shortcut-global", classes="shortcut-line")

    def on_mount(self) -> None:
        colors = self.app.get_css_variables()
        title_style = f"bold {colors.get('primary', '#0178D4')}"
        key_style = f"bold {colors.get('accent', '#FEA62B')}"
        label_style = colors.get("foreground", "#E0E0E0")
        sep_color = colors.get("foreground-muted", "#808080")
        # Strip alpha suffix — Rich doesn't support 8-digit hex colors
        sep_style = sep_color[:7] if len(sep_color) == 9 and sep_color.startswith("#") else sep_color

        self.query_one("#shortcut-repo", Static).update(
            self._build_line("Repo", self.REPO_ITEMS, title_style, key_style, label_style, sep_style),
        )
        self.query_one("#shortcut-global", Static).update(
            self._build_line("Global", self.GLOBAL_ITEMS, title_style, key_style, label_style, sep_style),
        )

    @staticmethod
    def _build_line(
        title: str,
        items: list[tuple[str, str]],
        title_style: str,
        key_style: str,
        label_style: str,
        sep_style: str,
    ) -> Text:
        text = Text()
        text.append(f"{title} ", style=title_style)
        for index, (key, label) in enumerate(items):
            if index:
                text.append(" \u2022 ", style=sep_style)
            text.append(key, style=key_style)
            text.append(f" {label}", style=label_style)
        return text


class BranchModal(ModalScreen[str | None]):
    """Modal to pick or create a branch."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, branches: list[str], current: str) -> None:
        super().__init__()
        self.branches = branches
        self.current = current
        self._next_id = 0

    def compose(self) -> ComposeResult:
        with Vertical(id="branch-dialog"):
            yield Label("Switch Branch", id="branch-title")
            yield Input(placeholder="Filter or new branch name...", id="branch-filter")
            yield ListView(id="branch-list")
            with Horizontal(id="branch-buttons"):
                yield Button("Switch", variant="success", id="btn-switch")
                yield Button("Create & Switch", variant="primary", id="btn-create")
                yield Button("Cancel", variant="error", id="btn-cancel")

    def on_mount(self) -> None:
        self._populate_list("")
        self.query_one("#branch-filter", Input).focus()

    def _populate_list(self, filt: str) -> None:
        lv = self.query_one("#branch-list", ListView)
        lv.clear()
        for b in self.branches:
            if filt and filt not in b.lower():
                continue
            idx = self._next_id
            self._next_id += 1
            lv.append(ListItem(Label(f"{'* ' if b == self.current else '  '}{b}"), id=f"br-{idx}"))

    def on_input_changed(self, event: Input.Changed) -> None:
        self._populate_list(event.value.lower())

    def _selected_branch(self) -> str | None:
        lv = self.query_one("#branch-list", ListView)
        if lv.highlighted_child is not None:
            label = lv.highlighted_child.query_one(Label)
            return str(label.render()).strip().lstrip("* ").strip()
        return None

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-switch":
            self.dismiss(self._selected_branch())
        elif event.button.id == "btn-create":
            name = self.query_one("#branch-filter", Input).value.strip()
            self.dismiss(f"__create__{name}" if name else None)
        else:
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class StashModal(ModalScreen[str | None]):
    """Modal to manage stashes: list, pop, apply, drop."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, stash_list: list[str], has_changes: bool) -> None:
        super().__init__()
        self.stash_list = stash_list
        self.has_changes = has_changes

    def compose(self) -> ComposeResult:
        with Vertical(id="branch-dialog"):
            yield Label("Stash Manager", id="branch-title")
            yield ListView(id="stash-lv")
            with Horizontal(id="branch-buttons"):
                if self.has_changes:
                    yield Button("Stash", variant="success", id="btn-stash-push")
                yield Button("Pop", variant="primary", id="btn-stash-pop")
                yield Button("Apply", variant="primary", id="btn-stash-apply")
                yield Button("Drop", variant="error", id="btn-stash-drop")
                yield Button("Cancel", variant="default", id="btn-cancel")

    def on_mount(self) -> None:
        lv = self.query_one("#stash-lv", ListView)
        for i, entry in enumerate(self.stash_list):
            lv.append(ListItem(Label(entry), id=f"st-{i}"))
        if not self.stash_list and not self.has_changes:
            lv.append(ListItem(Label("  (no stashes)")))

    def _selected_index(self) -> int | None:
        lv = self.query_one("#stash-lv", ListView)
        if lv.highlighted_child is None or not lv.highlighted_child.id:
            return None
        try:
            return int(lv.highlighted_child.id.removeprefix("st-"))
        except ValueError:
            return None

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "btn-stash-push":
            self.dismiss("__push__")
        elif bid == "btn-stash-pop":
            idx = self._selected_index()
            self.dismiss(f"__pop__{idx}" if idx is not None else None)
        elif bid == "btn-stash-apply":
            idx = self._selected_index()
            self.dismiss(f"__apply__{idx}" if idx is not None else None)
        elif bid == "btn-stash-drop":
            idx = self._selected_index()
            self.dismiss(f"__drop__{idx}" if idx is not None else None)
        else:
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class MarkdownModal(ModalScreen):
    """Modal to render a markdown file."""

    BINDINGS = [Binding("escape", "close", "Close"), Binding("q", "close", "Close")]

    def __init__(self, title: str, content: str, repo_path: Path | None = None) -> None:
        super().__init__()
        self.title_text = title
        self.content = content
        self.file_repo_path = repo_path

    def compose(self) -> ComposeResult:
        with Vertical(id="diff-dialog"):
            yield Label(self.title_text, id="diff-title")
            yield Markdown(self.content, id="md-viewer")
            with Horizontal(id="filediff-buttons"):
                if self.file_repo_path:
                    yield Button("Open in Editor", variant="success", id="btn-edit-md")
                yield Button("Close", variant="primary", id="btn-close")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-edit-md" and self.file_repo_path:
            editor = self.app._get_editor()
            if editor:
                target = str(self.file_repo_path / self.title_text)
                try:
                    subprocess.Popen([editor, target])
                except FileNotFoundError:
                    pass
            return
        self.app.pop_screen()

    def action_close(self) -> None:
        self.app.pop_screen()


class LogModal(ModalScreen):
    """Modal showing commit log with per-commit diff viewer."""

    BINDINGS = [Binding("escape", "close", "Close"), Binding("q", "close", "Close")]

    def __init__(self, title: str, repo: Repo, max_commits: int = 50) -> None:
        super().__init__()
        self.title_text = title
        self.repo = repo
        self.max_commits = max_commits
        self._next_id = 0
        self._commit_map: dict[int, str] = {}

    def compose(self) -> ComposeResult:
        with Vertical(id="filediff-dialog"):
            yield Label(self.title_text, id="diff-title")
            yield ListView(id="log-list")
            yield RichLog(id="diff-log", wrap=True, markup=False)
            yield Button("Close", variant="primary", id="btn-close")

    def on_mount(self) -> None:
        lv = self.query_one("#log-list", ListView)
        for commit in self.repo.iter_commits(max_count=self.max_commits):
            short_sha = commit.hexsha[:7]
            date = commit.authored_datetime.strftime("%Y-%m-%d %H:%M")
            msg = commit.message.strip().split("\n")[0][:60]
            idx = self._next_id
            self._next_id += 1
            self._commit_map[idx] = commit.hexsha
            lv.append(ListItem(Label(f"{short_sha}  {date}  {msg}"), id=f"lg-{idx}"))

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        if event.item is None or not event.item.id or not event.item.id.startswith("lg-"):
            return
        try:
            idx = int(event.item.id.removeprefix("lg-"))
        except ValueError:
            return
        sha = self._commit_map.get(idx)
        if not sha:
            return
        try:
            diff_text = self.repo.git.show("--stat", "--patch", sha)
        except GitCommandError:
            diff_text = "(could not get diff)"
        log = self.query_one("#diff-log", RichLog)
        log.clear()
        log.write(diff_text)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.app.pop_screen()

    def action_close(self) -> None:
        self.app.pop_screen()


class StageModal(ModalScreen):
    """Modal to stage/unstage individual files."""

    BINDINGS = [Binding("escape", "close", "Close"), Binding("q", "close", "Close")]

    def __init__(self, repo: Repo) -> None:
        super().__init__()
        self.repo = repo
        self._next_id = 0
        self._file_map: dict[int, tuple[str, str]] = {}

    def compose(self) -> ComposeResult:
        with Vertical(id="filediff-dialog"):
            yield Label("Stage / Unstage Files", id="diff-title")
            yield ListView(id="stage-list")
            with Horizontal(id="filediff-buttons"):
                yield Button("Stage All", variant="success", id="btn-stage-all")
                yield Button("Unstage All", variant="warning", id="btn-unstage-all")
                yield Button("Close", variant="primary", id="btn-close")

    def on_mount(self) -> None:
        self._refresh_list()

    def _refresh_list(self) -> None:
        lv = self.query_one("#stage-list", ListView)
        lv.clear()
        self._file_map = {}

        staged = [d.a_path for d in self.repo.index.diff("HEAD")] if self.repo.head.is_valid() else []
        unstaged = [d.a_path for d in self.repo.index.diff(None)]
        untracked = self.repo.untracked_files

        if staged:
            lv.append(ListItem(Label("── Staged (will be committed) ──")))
            for f in staged:
                idx = self._next_id; self._next_id += 1
                lv.append(ListItem(Label(f"  [x] {f}"), id=f"sf-{idx}"))
                self._file_map[idx] = (f, "staged")
        if unstaged:
            lv.append(ListItem(Label("── Modified (not staged) ──")))
            for f in unstaged:
                idx = self._next_id; self._next_id += 1
                lv.append(ListItem(Label(f"  [ ] {f}"), id=f"sf-{idx}"))
                self._file_map[idx] = (f, "unstaged")
        if untracked:
            lv.append(ListItem(Label("── Untracked ──")))
            for f in untracked:
                idx = self._next_id; self._next_id += 1
                lv.append(ListItem(Label(f"  [ ] {f}"), id=f"sf-{idx}"))
                self._file_map[idx] = (f, "untracked")
        if not staged and not unstaged and not untracked:
            lv.append(ListItem(Label("  (no changes)")))

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if event.item is None or not event.item.id or not event.item.id.startswith("sf-"):
            return
        try:
            idx = int(event.item.id.removeprefix("sf-"))
        except ValueError:
            return
        if idx not in self._file_map:
            return
        filepath, cat = self._file_map[idx]
        try:
            if cat == "staged":
                self.repo.git.reset("HEAD", "--", filepath)
            else:
                self.repo.index.add([filepath])
        except GitCommandError:
            pass
        self._refresh_list()
        self._refresh_parent_card()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-stage-all":
            try:
                self.repo.git.add("-A")
            except GitCommandError:
                pass
            self._refresh_list()
            self._refresh_parent_card()
            return
        if event.button.id == "btn-unstage-all":
            try:
                self.repo.git.reset("HEAD")
            except GitCommandError:
                pass
            self._refresh_list()
            self._refresh_parent_card()
            return
        self.app.pop_screen()

    def _refresh_parent_card(self) -> None:
        for card in self.app.query(RepoCard):
            if card.repo.working_dir == self.repo.working_dir:
                card.refresh_status()
                break

    def action_close(self) -> None:
        self.app.pop_screen()


class SearchModal(ModalScreen):
    """Modal to search across all repos with git grep."""

    BINDINGS = [Binding("escape", "close", "Close"), Binding("q", "close", "Close")]

    def __init__(self, repo_cards: list[RepoCard]) -> None:
        super().__init__()
        self.repo_cards = repo_cards
        self._next_id = 0
        self._result_map: dict[int, tuple[str, str, str]] = {}  # idx -> (repo_name, filepath, line)

    def compose(self) -> ComposeResult:
        with Vertical(id="filediff-dialog"):
            yield Label("Search Across Repos", id="diff-title")
            yield Input(placeholder="Type to search...", id="search-input")
            yield ListView(id="search-results")
            yield RichLog(id="diff-log", wrap=True, markup=False)
            with Horizontal(id="filediff-buttons"):
                yield Button("Open in Editor", variant="success", id="btn-search-edit")
                yield Button("Close", variant="primary", id="btn-close")

    def on_mount(self) -> None:
        self._selected: tuple[str, str, str] | None = None
        self.query_one("#search-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "search-input":
            self._run_search(event.value.strip())

    def _run_search(self, query: str) -> None:
        if not query:
            return
        lv = self.query_one("#search-results", ListView)
        lv.clear()
        self._result_map = {}
        log = self.query_one("#diff-log", RichLog)
        log.clear()

        total = 0
        for card in self.repo_cards:
            try:
                output = card.repo.git.grep("-n", "-I", "--max-count=50", query)
            except GitCommandError:
                continue
            if not output:
                continue
            # Add repo header
            lv.append(ListItem(Label(f"── {card.repo_path.name} ──")))
            for line in output.splitlines():
                if total >= 200:
                    break
                # Format: filepath:linenum:content
                idx = self._next_id
                self._next_id += 1
                self._result_map[idx] = (card.repo_path.name, line, card.repo.working_dir)
                # Truncate long lines
                display = line[:120] + "..." if len(line) > 120 else line
                lv.append(ListItem(Label(f"  {display}"), id=f"sr-{idx}"))
                total += 1
            if total >= 200:
                break

        if total == 0:
            lv.append(ListItem(Label(f"  No results for '{query}'")))

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        if event.item is None or not event.item.id or not event.item.id.startswith("sr-"):
            return
        try:
            idx = int(event.item.id.removeprefix("sr-"))
        except ValueError:
            return
        if idx not in self._result_map:
            return
        repo_name, line, working_dir = self._result_map[idx]
        self._selected = (repo_name, line, working_dir)
        # Show context: parse filepath and show surrounding lines
        parts = line.split(":", 2)
        if len(parts) >= 2:
            filepath = parts[0]
            try:
                linenum = int(parts[1])
                full_path = Path(working_dir) / filepath
                if full_path.exists():
                    content = full_path.read_text(errors="replace").splitlines()
                    start = max(0, linenum - 5)
                    end = min(len(content), linenum + 5)
                    context_lines = []
                    for i in range(start, end):
                        marker = ">>>" if i == linenum - 1 else "   "
                        context_lines.append(f"{marker} {i+1:4d} | {content[i]}")
                    log = self.query_one("#diff-log", RichLog)
                    log.clear()
                    log.write(f"{repo_name} / {filepath}:{linenum}\n")
                    log.write("\n".join(context_lines))
                    return
            except (ValueError, OSError):
                pass
        log = self.query_one("#diff-log", RichLog)
        log.clear()
        log.write(f"{repo_name}: {line}")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-search-edit" and self._selected:
            _repo_name, line, working_dir = self._selected
            parts = line.split(":", 2)
            if parts:
                filepath = parts[0]
                self.app._do_open_editor_path(str(Path(working_dir) / filepath))
            return
        self.app.pop_screen()

    def action_close(self) -> None:
        self.app.pop_screen()


class DiffModal(ModalScreen):
    """Show a diff in a modal."""

    BINDINGS = [Binding("escape", "close", "Close"), Binding("q", "close", "Close")]

    def __init__(self, title: str, diff_text: str) -> None:
        super().__init__()
        self.title_text = title
        self.diff_text = diff_text

    def compose(self) -> ComposeResult:
        with Vertical(id="diff-dialog"):
            yield Label(self.title_text, id="diff-title")
            yield RichLog(id="diff-log", wrap=True, markup=False)
            yield Button("Close", variant="primary", id="btn-close")

    def on_mount(self) -> None:
        log = self.query_one("#diff-log", RichLog)
        log.write(self.diff_text or "(no diff)")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.app.pop_screen()

    def action_close(self) -> None:
        self.app.pop_screen()


class FileDiffModal(ModalScreen):
    """Modal showing changed files with per-file diff viewer."""

    BINDINGS = [Binding("escape", "close", "Close"), Binding("q", "close", "Close")]

    def __init__(self, title: str, files: list[tuple[str, str]], repo: Repo) -> None:
        """files: list of (filepath, category) where category is 'staged'/'unstaged'/'untracked'."""
        super().__init__()
        self.title_text = title
        self.files = files
        self.repo = repo

    def compose(self) -> ComposeResult:
        with Vertical(id="filediff-dialog"):
            yield Label(self.title_text, id="diff-title")
            yield Input(placeholder="Filter files...", id="filediff-filter")
            yield ListView(id="filediff-list")
            yield RichLog(id="diff-log", wrap=True, markup=False)
            with Horizontal(id="filediff-buttons"):
                yield Button("Open in Editor", variant="success", id="btn-edit-file")
                yield Button("Revert File", variant="error", id="btn-revert-file")
                yield Button("Close", variant="primary", id="btn-close")

    def on_mount(self) -> None:
        self._selected_file: tuple[str, str] | None = None
        self._next_id = 0
        self._populate_list("")

    def _populate_list(self, filt: str) -> None:
        lv = self.query_one("#filediff-list", ListView)
        lv.clear()
        self._file_map: dict[int, tuple[str, str]] = {}
        current_cat = None
        for filepath, cat in self.files:
            if filt and filt not in filepath.lower():
                continue
            if cat != current_cat:
                current_cat = cat
                label_text = {"staged": "Staged", "unstaged": "Modified", "untracked": "Untracked"}.get(cat, cat)
                lv.append(ListItem(Label(f"── {label_text} ──")))
            idx = self._next_id
            self._next_id += 1
            lv.append(ListItem(Label(f"  {filepath}"), id=f"fd-{idx}"))
            self._file_map[idx] = (filepath, cat)
        if not self.files:
            lv.append(ListItem(Label("  (no changes)")))

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "filediff-filter":
            self._populate_list(event.value.lower())

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        if event.item is None or not event.item.id or not event.item.id.startswith("fd-"):
            return
        try:
            idx = int(event.item.id.removeprefix("fd-"))
        except ValueError:
            return
        if idx not in self._file_map:
            return
        filepath, cat = self._file_map[idx]
        self._selected_file = (filepath, cat)
        diff_text = self._get_file_diff(filepath, cat)
        log = self.query_one("#diff-log", RichLog)
        log.clear()
        log.write(diff_text)

    def _get_file_diff(self, filepath: str, category: str) -> str:
        try:
            if category == "staged":
                return self.repo.git.diff("--cached", "--", filepath) or "(no diff)"
            elif category == "unstaged":
                return self.repo.git.diff("--", filepath) or "(no diff)"
            else:
                # Untracked: show file contents as new file
                full_path = Path(self.repo.working_dir) / filepath
                if full_path.exists():
                    content = full_path.read_text(errors="replace")
                    lines = content.splitlines()
                    diff_lines = [f"new file: {filepath}", "---", *[f"+{line}" for line in lines]]
                    return "\n".join(diff_lines)
                return "(file not found)"
        except GitCommandError:
            return "(could not get diff)"

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-edit-file":
            if not self._selected_file:
                return
            filepath, _cat = self._selected_file
            for card in self.app.query(RepoCard):
                if card.repo.working_dir == self.repo.working_dir:
                    self.app._do_open_editor(card, filepath)
                    break
            return
        if event.button.id == "btn-revert-file":
            if not self._selected_file:
                return
            filepath, cat = self._selected_file
            self.app.push_screen(
                ConfirmModal(f"Discard changes to {filepath}?"),
                lambda confirmed: self._revert_file(filepath, cat) if confirmed else None,
            )
            return
        self.app.pop_screen()

    def _revert_file(self, filepath: str, cat: str) -> None:
        try:
            if cat == "staged":
                self.repo.git.reset("HEAD", "--", filepath)
                self.repo.git.checkout("--", filepath)
            elif cat == "unstaged":
                self.repo.git.checkout("--", filepath)
            elif cat == "untracked":
                (Path(self.repo.working_dir) / filepath).unlink(missing_ok=True)
            # Remove from file list and refresh
            self.files = [(f, c) for f, c in self.files if not (f == filepath and c == cat)]
            self._populate_list(self.query_one("#filediff-filter", Input).value.lower())
            self.query_one("#diff-log", RichLog).clear()
            self._selected_file = None
            # Refresh the parent card
            for card in self.app.query(RepoCard):
                if card.repo.working_dir == self.repo.working_dir:
                    card.refresh_status()
                    break
        except GitCommandError as e:
            log = self.query_one("#diff-log", RichLog)
            log.clear()
            log.write(f"Revert failed: {e}")

    def action_close(self) -> None:
        self.app.pop_screen()


# ---------------------------------------------------------------------------
# Repo widget
# ---------------------------------------------------------------------------

class RepoCard(Vertical, can_focus=True):
    """A collapsible card showing one repo's status."""

    collapsed = reactive(True)

    BINDINGS = [
        Binding("a", "stage", "Stage", show=True),
        Binding("b", "branch", "Branch", show=True),
        Binding("c", "commit", "Commit", show=True),
        Binding("d", "diff", "Diff", show=True),
        Binding("e", "open_editor", "Edit", show=True),
        Binding("l", "log", "Log", show=True),
        Binding("s", "stash", "Stash", show=True),
        Binding("x", "revert", "Revert", show=True),
        Binding("space", "toggle_collapse", "Toggle", show=True),
        Binding("enter", "toggle_collapse", "Toggle", show=False),
    ]

    def __init__(self, repo_path: Path, **kwargs) -> None:
        super().__init__(**kwargs)
        self.repo_path = repo_path
        self.repo = Repo(repo_path)
        self.status: dict = {}

    def compose(self) -> ComposeResult:
        with Horizontal(classes="repo-header"):
            yield Button("v", id=f"toggle-{self.repo_path.name}", classes="toggle-btn")
            yield Static(self.repo_path.name, classes="repo-name")
            yield Static("", id=f"branch-{self.repo_path.name}", classes="repo-branch")
            yield Static("", id=f"changes-{self.repo_path.name}", classes="repo-changes")
            yield Static("", id=f"sync-{self.repo_path.name}", classes="repo-sync")
            yield Static("", id=f"flags-{self.repo_path.name}", classes="repo-flags")
        yield Static("", id=f"syncbtn-{self.repo_path.name}", classes="sync-btn")
        with Vertical(id=f"body-{self.repo_path.name}", classes="repo-body"):
            yield Tree("Changes", id=f"tree-{self.repo_path.name}")
            with Horizontal(classes="repo-actions"):
                yield Button("Fetch", id=f"fetch-{self.repo_path.name}", classes="action-btn")
                yield Button("Branch", id=f"brn-{self.repo_path.name}", classes="action-btn")
                yield Button("Stash", id=f"stash-{self.repo_path.name}", classes="action-btn")
                yield Button("Commit", id=f"cmt-{self.repo_path.name}", classes="action-btn accent")
                yield Button("Diff", id=f"diff-{self.repo_path.name}", classes="action-btn")

    def on_mount(self) -> None:
        self.refresh_status()

    def refresh_status(self) -> None:
        try:
            self.repo = Repo(self.repo_path)
            self.status = short_status(self.repo)
        except (InvalidGitRepositoryError, Exception):
            self.status = {"branch": "?", "tracking": None, "ahead": 0, "behind": 0,
                           "staged": [], "unstaged": [], "untracked": [], "stashes": 0,
                           "conflicted": False, "dirty": False, "detached": False}

        name = self.repo_path.name
        try:
            branch_lbl = self.query_one(f"#branch-{name}", Static)
            branch_lbl.update(f" {self.status['branch']}")
        except NoMatches:
            pass

        staged_count = len(self.status["staged"])
        unstaged_count = len(self.status["unstaged"])
        untracked_count = len(self.status["untracked"])
        total_changes = staged_count + unstaged_count + untracked_count
        try:
            changes_lbl = self.query_one(f"#changes-{name}", Static)
            change_parts = []
            if staged_count:
                change_parts.append(f"staged:{staged_count}")
            if unstaged_count:
                change_parts.append(f"mod:{unstaged_count}")
            if untracked_count:
                change_parts.append(f"new:{untracked_count}")
            changes_lbl.update(" ".join(change_parts))
        except NoMatches:
            pass

        sync_parts = []
        if self.status["ahead"] or self.status["behind"]:
            sync_parts.append(f"↑{self.status['ahead']}↓{self.status['behind']}")
        if self.status["stashes"]:
            sync_parts.append(f"stash:{self.status['stashes']}")
        try:
            sync_lbl = self.query_one(f"#sync-{name}", Static)
            sync_lbl.update(" ".join(sync_parts))
        except NoMatches:
            pass

        try:
            flags_lbl = self.query_one(f"#flags-{name}", Static)
            flags = []
            if self.status["conflicted"]:
                flags.append("CONFLICT")
            if not self.status["detached"] and not self.status["tracking"]:
                flags.append("NO-UPSTREAM")
            if not total_changes and not sync_parts and not flags:
                flags.append("CLEAN")
            flags_lbl.update(" ".join(flags))
            flags_lbl.styles.color = "#f38ba8" if self.status["conflicted"] else "#89dceb"
            flags_lbl.styles.text_style = "bold" if self.status["conflicted"] else "none"
        except NoMatches:
            pass

        # Update sync bar using inline styles (CSS classes have rendering bugs)
        ahead = self.status["ahead"]
        behind = self.status["behind"]
        has_local = bool(self.status["staged"] or self.status["unstaged"] or self.status["untracked"])
        try:
            sync_bar = self.query_one(f"#syncbtn-{name}", Static)
            if behind or ahead:
                if behind and ahead:
                    label = f"Stash, Pull & Push  ↑{ahead} ↓{behind}" if has_local else f"Sync Changes  ↑{ahead} ↓{behind}"
                elif behind:
                    label = f"Stash & Pull  ↓{behind}" if has_local else f"Pull Changes  ↓{behind}"
                else:
                    label = f"Push Changes  ↑{ahead}"
                sync_bar.update(label)
                color = "#1177bb" if behind else "#388e3c"
                sync_bar.styles.background = color
                sync_bar.styles.color = "white"
                sync_bar.styles.text_align = "center"
                sync_bar.styles.text_style = "bold"
                sync_bar.styles.padding = (0, 1)
            else:
                sync_bar.update("")
                sync_bar.styles.background = None
                sync_bar.styles.padding = (0, 0)
        except NoMatches:
            pass

        try:
            tree: Tree = self.query_one(f"#tree-{name}", Tree)
            tree.clear()
            total = len(self.status["staged"]) + len(self.status["unstaged"]) + len(self.status["untracked"])
            tree.root.set_label(f"Changes ({total})")
            if self.status["staged"]:
                staged_node = tree.root.add("Staged", expand=True)
                for f in self.status["staged"]:
                    staged_node.add_leaf(f"  {f}")
            if self.status["unstaged"]:
                mod_node = tree.root.add("Modified", expand=True)
                for f in self.status["unstaged"]:
                    mod_node.add_leaf(f"  {f}")
            if self.status["untracked"]:
                unt_node = tree.root.add("Untracked", expand=True)
                for f in self.status["untracked"]:
                    unt_node.add_leaf(f"  {f}")
            tree.root.expand()
        except NoMatches:
            pass

    def watch_collapsed(self, value: bool) -> None:
        name = self.repo_path.name
        try:
            body = self.query_one(f"#body-{name}")
            body.display = not value
            btn = self.query_one(f"#toggle-{name}", Button)
            btn.label = ">" if value else "v"
        except NoMatches:
            pass

    def on_click(self, event) -> None:
        """Handle clicks on the sync bar."""
        widget = event.widget
        if isinstance(widget, Static) and widget.id and widget.id.startswith("syncbtn-"):
            self.app._do_sync(self)

    def action_branch(self) -> None:
        self.app._do_branch(self)

    def action_commit(self) -> None:
        self.app._do_commit(self)

    def action_diff(self) -> None:
        self.app._do_diff(self)

    def action_stage(self) -> None:
        self.app._do_stage(self)

    def action_open_editor(self) -> None:
        self.app._do_open_editor(self)

    def action_log(self) -> None:
        self.app._do_log(self)

    def action_stash(self) -> None:
        self.app._do_stash(self)

    def action_revert(self) -> None:
        self.app._do_revert(self)

    def on_tree_node_selected(self, event: Tree.NodeSelected) -> None:
        """Open per-file diff when a file is clicked in the Changes tree."""
        node = event.node
        # Only act on leaf nodes (files), not category headers
        if node.children:
            return
        filepath = str(node.label).strip()
        if not filepath:
            return
        # Determine category by parent label
        parent_label = str(node.parent.label).strip() if node.parent else ""
        if parent_label.startswith("Staged"):
            cat = "staged"
        elif parent_label.startswith("Modified"):
            cat = "unstaged"
        elif parent_label.startswith("Untracked"):
            cat = "untracked"
        else:
            cat = "unstaged"
        if filepath.endswith(".md"):
            full_path = Path(self.repo.working_dir) / filepath
            if full_path.exists():
                content = full_path.read_text(errors="replace")
                self.app.push_screen(MarkdownModal(filepath, content, Path(self.repo.working_dir)))
                return
        self.app._do_diff(self, single_file=(filepath, cat))

    def action_toggle_collapse(self) -> None:
        self.collapsed = not self.collapsed


# ---------------------------------------------------------------------------
# Main app
# ---------------------------------------------------------------------------

class GitDash(App):
    """Multi-repo git dashboard."""

    CSS = """
    Screen {
        background: $surface;
    }

    #main-scroll {
        scrollbar-size: 1 1;
    }

    .repo-card {
        margin: 0 1;
        padding: 0;
        border: solid $primary-background;
        margin-bottom: 1;
        height: auto;
    }

    .repo-card:focus {
        border: solid $accent;
    }

    .repo-header {
        height: 3;
        padding: 0 1;
        background: $primary-background;
        align: left middle;
    }

    .toggle-btn {
        min-width: 3;
        max-width: 3;
        height: 1;
        margin: 0 1 0 0;
        border: none;
        background: transparent;
        color: $text;
    }

    .repo-name {
        color: $text;
        text-style: bold;
        width: auto;
        min-width: 20;
    }

    .repo-branch {
        color: #a6e3a1;
        width: auto;
        margin-left: 1;
    }

    .repo-changes {
        color: #fab387;
        width: auto;
        margin-left: 1;
    }

    .repo-sync {
        color: #f9e2af;
        width: auto;
        margin-left: 1;
    }

    .repo-flags {
        color: #89dceb;
        width: auto;
        margin-left: 1;
    }

    .repo-body {
        padding: 0 1;
        height: auto;
    }

    Tree {
        height: auto;
        min-height: 3;
        max-height: 20;
        margin: 0;
        padding: 0;
    }

    .repo-actions {
        height: 3;
        padding: 0;
        align: left middle;
    }

    .action-btn {
        min-width: 8;
        height: 1;
        margin: 0 1 0 0;
        border: none;
    }

    .accent {
        background: $success;
        color: $text;
    }

    .sync-btn {
        width: 100%;
        height: auto;
    }

    /* Modals */
    #commit-dialog, #branch-dialog, #diff-dialog {
        width: 70;
        height: auto;
        max-height: 80%;
        border: thick $primary;
        background: $surface;
        padding: 1 2;
        margin: 2 4;
    }

    #diff-dialog {
        width: 100;
        height: 80%;
    }

    #filediff-dialog {
        width: 100;
        height: 80%;
        border: thick $primary;
        background: $surface;
        padding: 1 2;
        margin: 2 4;
    }

    #filediff-list, #log-list, #stage-list {
        height: auto;
        max-height: 12;
        border: solid $primary-background;
        margin: 1 0;
    }

    .file-category {
        color: $text-muted;
        height: auto;
    }

    #filediff-buttons {
        height: 3;
        align: center middle;
        margin-top: 1;
    }

    #commit-title, #branch-title, #diff-title {
        text-style: bold;
        margin-bottom: 1;
        width: 100%;
        text-align: center;
    }

    #confirm-details {
        color: $text-muted;
        margin-bottom: 1;
        width: 100%;
    }

    #commit-buttons, #branch-buttons {
        height: 3;
        align: center middle;
        margin-top: 1;
    }

    #diff-log {
        height: 1fr;
        border: solid $primary-background;
        margin: 1 0;
    }

    #md-viewer {
        height: 1fr;
        border: solid $primary-background;
        margin: 1 0;
        overflow-y: auto;
    }

    #branch-list {
        height: auto;
        max-height: 20;
        border: solid $primary-background;
        margin: 1 0;
    }

    #git-log {
        dock: bottom;
        height: 12;
        display: none;
        border-top: solid $primary;
        margin: 0 1;
        background: $surface;
    }

    #status-bar {
        dock: bottom;
        height: 1;
        padding: 0 1;
        background: $primary-background;
        color: $text-muted;
    }

    #shortcut-bar {
        dock: bottom;
        height: 2;
        padding: 0 1;
        background: $primary-background;
        color: $text;
        border-top: solid $surface;
    }

    .shortcut-line {
        height: 1;
    }
    """

    BINDINGS = [
        Binding("j", "next_repo", "Next", show=True),
        Binding("k", "prev_repo", "Prev", show=True),
        Binding("down", "next_repo", "Next", show=False),
        Binding("up", "prev_repo", "Prev", show=False),
        Binding("r", "refresh_all", "Refresh"),
        Binding("q", "quit", "Quit"),
        Binding("F", "fetch_all", "Fetch All"),
        Binding("P", "pull_all", "Pull All"),
        Binding("g", "switch_group", "Group"),
        Binding("slash", "search", "Search"),
        Binding("question_mark", "help", "Help"),
        Binding("exclamation_mark", "toggle_log", "Log"),
        Binding("J", "move_repo_down", "Move Down"),
        Binding("K", "move_repo_up", "Move Up"),
        Binding("S", "save_repo_order", "Save Order"),
    ]

    def __init__(self, base_path: Path, repo_paths: list[Path] | None = None, group_name: str | None = None, fetch_on_startup: bool = False, config=None) -> None:
        super().__init__()
        self.base_path = base_path
        self.repo_paths = repo_paths or find_repos(base_path)
        self.group_name = group_name
        self.fetch_on_startup = fetch_on_startup
        self.config = config

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with VerticalScroll(id="main-scroll"):
            for rp in self.repo_paths:
                yield RepoCard(rp, classes="repo-card", id=f"card-{rp.name}")
        yield RichLog(id="git-log", wrap=True, markup=False)
        yield ShortcutBar(id="shortcut-bar")
        yield Static("", id="status-bar")

    def on_mount(self) -> None:
        title_suffix = self.group_name or str(self.base_path)
        self.title = f"GitDash — {title_suffix}"
        self._update_status_bar("Ready")
        cards = list(self.query(RepoCard))
        if cards:
            cards[0].focus()
        if self.fetch_on_startup:
            self._startup_fetch()
        self.set_interval(30, self._auto_refresh)

    def _log_action(self, msg: str) -> None:
        """Write a timestamped entry to the git operations log."""
        ts = datetime.now().strftime("%H:%M:%S")
        try:
            log = self.query_one("#git-log", RichLog)
            log.write(f"[{ts}] {msg}")
        except NoMatches:
            pass

    def action_toggle_log(self) -> None:
        """Toggle visibility of the git operations log panel."""
        try:
            log = self.query_one("#git-log", RichLog)
            log.display = not log.display
        except NoMatches:
            pass

    def _update_status_bar(self, msg: str) -> None:
        try:
            self.query_one("#status-bar", Static).update(msg)
        except NoMatches:
            pass

    def _update_status_bar_from_thread(self, msg: str) -> None:
        self.call_from_thread(self._update_status_bar, msg)

    def _log_action_from_thread(self, msg: str) -> None:
        self.call_from_thread(self._log_action, msg)

    def _show_message(self, title: str, content: str) -> None:
        self.push_screen(MessageModal(title, content))

    def _card_for_button(self, button_id: str) -> RepoCard | None:
        # button ids are like "fetch-reponame"
        parts = button_id.split("-", 1)
        if len(parts) < 2:
            return None
        repo_name = parts[1]
        try:
            return self.query_one(f"#card-{repo_name}", RepoCard)
        except NoMatches:
            return None

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id or ""

        # Toggle collapse
        if bid.startswith("toggle-"):
            repo_name = bid.removeprefix("toggle-")
            try:
                card = self.query_one(f"#card-{repo_name}", RepoCard)
                card.collapsed = not card.collapsed
            except NoMatches:
                pass
            return

        card = self._card_for_button(bid)
        if not card:
            return

        if bid.startswith("fetch-"):
            self._do_fetch(card)
        elif bid.startswith("brn-"):
            self._do_branch(card)
        elif bid.startswith("stash-"):
            self._do_stash(card)
        elif bid.startswith("cmt-"):
            self._do_commit(card)
        elif bid.startswith("diff-"):
            self._do_diff(card)

    # -- Git actions --

    @work(thread=True)
    def _do_sync(self, card: RepoCard) -> None:
        name = card.repo_path.name
        behind = card.status.get("behind", 0)
        ahead = card.status.get("ahead", 0)
        has_changes = bool(card.status.get("unstaged") or card.status.get("untracked") or card.status.get("staged"))
        stashed = False
        try:
            if behind and has_changes:
                self._update_status_bar_from_thread(f"Stashing changes in {name}...")
                self._log_action_from_thread(f"[{name}] git stash push -u -m 'gitdash-auto-stash'")
                card.repo.git.stash("push", "-u", "-m", "gitdash-auto-stash")
                stashed = True
            if behind:
                self._update_status_bar_from_thread(f"Pulling {name}...")
                self._log_action_from_thread(f"[{name}] git pull")
                card.repo.git.pull()
                self._log_action_from_thread(f"[{name}] pull OK")
            if ahead:
                self._update_status_bar_from_thread(f"Pushing {name}...")
                self._log_action_from_thread(f"[{name}] git push")
                card.repo.git.push()
                self._log_action_from_thread(f"[{name}] push OK")
            if stashed:
                self._update_status_bar_from_thread(f"Restoring changes in {name}...")
                self._log_action_from_thread(f"[{name}] git stash pop")
                try:
                    card.repo.git.stash("pop")
                except GitCommandError as e:
                    self._log_action_from_thread(f"[{name}] ERROR stash pop: {e}")
                    self._update_status_bar_from_thread(f"Synced {name} — stash pop had conflicts, resolve manually")
                    self.call_from_thread(card.refresh_status)
                    return
            self.call_from_thread(card.refresh_status)
            self._update_status_bar_from_thread(f"Synced {name}")
            self._log_action_from_thread(f"[{name}] sync complete")
        except GitCommandError as e:
            self._log_action_from_thread(f"[{name}] ERROR sync: {e}")
            if stashed:
                try:
                    card.repo.git.stash("pop")
                except GitCommandError:
                    pass
            self.call_from_thread(card.refresh_status)
            self._update_status_bar_from_thread(f"Sync failed: {e}")

    @work(thread=True)
    def _do_fetch(self, card: RepoCard) -> None:
        name = card.repo_path.name
        self._update_status_bar_from_thread(f"Fetching {name}...")
        self._log_action_from_thread(f"[{name}] git fetch --all --prune")
        try:
            card.repo.git.fetch("--all", "--prune")
            self.call_from_thread(card.refresh_status)
            self._update_status_bar_from_thread(f"Fetched {name}")
            self._log_action_from_thread(f"[{name}] fetch OK")
        except GitCommandError as e:
            self._log_action_from_thread(f"[{name}] ERROR fetch: {e}")
            self._update_status_bar_from_thread(f"Fetch failed: {e}")

    def _do_branch(self, card: RepoCard) -> None:
        local_branches = [h.name for h in card.repo.heads]
        remote_branches = []
        for ref in card.repo.remotes.origin.refs if card.repo.remotes else []:
            name = ref.name.replace("origin/", "", 1)
            if name != "HEAD" and name not in local_branches:
                remote_branches.append(ref.name)
        branches = local_branches + remote_branches
        current = card.status.get("branch", "")

        def on_result(result: str | None) -> None:
            if result is None:
                return
            if result.startswith("__create__"):
                new_name = result.removeprefix("__create__")
                if new_name:
                    try:
                        self._log_action(f"[{card.repo_path.name}] git checkout -b {new_name}")
                        card.repo.git.checkout("-b", new_name)
                        card.refresh_status()
                        self._update_status_bar(f"Created & switched to {new_name}")
                        self._log_action(f"[{card.repo_path.name}] branch create OK")
                    except GitCommandError as e:
                        self._log_action(f"[{card.repo_path.name}] ERROR branch create: {e}")
                        self._update_status_bar(f"Branch create failed: {e}")
            elif result.startswith("origin/"):
                local_name = result.replace("origin/", "", 1)
                try:
                    self._log_action(f"[{card.repo_path.name}] git checkout -b {local_name} --track {result}")
                    card.repo.git.checkout("-b", local_name, "--track", result)
                    card.refresh_status()
                    self._update_status_bar(f"Checked out remote branch {local_name}")
                    self._log_action(f"[{card.repo_path.name}] checkout OK")
                except GitCommandError as e:
                    self._log_action(f"[{card.repo_path.name}] ERROR checkout: {e}")
                    self._update_status_bar(f"Checkout failed: {e}")
            else:
                try:
                    self._log_action(f"[{card.repo_path.name}] git checkout {result}")
                    card.repo.git.checkout(result)
                    card.refresh_status()
                    self._update_status_bar(f"Switched to {result}")
                    self._log_action(f"[{card.repo_path.name}] checkout OK")
                except GitCommandError as e:
                    self._log_action(f"[{card.repo_path.name}] ERROR checkout: {e}")
                    self._update_status_bar(f"Checkout failed: {e}")

        self.push_screen(BranchModal(branches, current), on_result)

    def _do_stash(self, card: RepoCard) -> None:
        name = card.repo_path.name
        stash_output = card.repo.git.stash("list")
        stash_entries = stash_output.splitlines() if stash_output else []
        has_changes = bool(card.status["unstaged"] or card.status["untracked"] or card.status["staged"])

        if not stash_entries and not has_changes:
            self._update_status_bar(f"Nothing to stash in {name}")
            return

        def on_result(result: str | None) -> None:
            if result is None:
                return
            try:
                if result == "__push__":
                    self._log_action(f"[{name}] git stash push -u")
                    card.repo.git.stash("push", "-u")
                    card.refresh_status()
                    self._update_status_bar(f"Stashed changes in {name}")
                    self._log_action(f"[{name}] stash push OK")
                elif result.startswith("__pop__"):
                    idx = int(result.removeprefix("__pop__"))
                    self._log_action(f"[{name}] git stash pop stash@{{{idx}}}")
                    card.repo.git.stash("pop", f"stash@{{{idx}}}")
                    card.refresh_status()
                    self._update_status_bar(f"Popped stash@{{{idx}}} in {name}")
                    self._log_action(f"[{name}] stash pop OK")
                elif result.startswith("__apply__"):
                    idx = int(result.removeprefix("__apply__"))
                    self._log_action(f"[{name}] git stash apply stash@{{{idx}}}")
                    card.repo.git.stash("apply", f"stash@{{{idx}}}")
                    card.refresh_status()
                    self._update_status_bar(f"Applied stash@{{{idx}}} in {name}")
                    self._log_action(f"[{name}] stash apply OK")
                elif result.startswith("__drop__"):
                    idx = int(result.removeprefix("__drop__"))
                    self._log_action(f"[{name}] git stash drop stash@{{{idx}}}")
                    card.repo.git.stash("drop", f"stash@{{{idx}}}")
                    card.refresh_status()
                    self._update_status_bar(f"Dropped stash@{{{idx}}} in {name}")
                    self._log_action(f"[{name}] stash drop OK")
            except GitCommandError as e:
                self._log_action(f"[{name}] ERROR stash: {e}")
                self._update_status_bar(f"Stash failed: {e}")

        self.push_screen(StashModal(stash_entries, has_changes), on_result)

    def _get_editor(self) -> str | None:
        if self.config and self.config.editor:
            return self.config.editor
        return os.environ.get("EDITOR") or os.environ.get("VISUAL")

    def _do_open_editor_path(self, full_path: str) -> None:
        """Open an absolute file path in the editor."""
        editor = self._get_editor()
        if not editor:
            self._update_status_bar("No editor configured. Set 'editor' in config.toml or $EDITOR")
            return
        try:
            subprocess.Popen([editor, full_path])
            self._update_status_bar(f"Opened {Path(full_path).name} in {editor}")
        except FileNotFoundError:
            self._update_status_bar(f"Editor '{editor}' not found")

    def _do_open_editor(self, card: RepoCard, filepath: str | None = None) -> None:
        editor = self._get_editor()
        if not editor:
            self._update_status_bar("No editor configured. Set 'editor' in config.toml or $EDITOR")
            return
        if filepath:
            target = str(Path(card.repo.working_dir) / filepath)
        else:
            target = str(card.repo_path)
        args = [editor, target]
        try:
            subprocess.Popen(args)
            self._update_status_bar(f"Opened {filepath or card.repo_path.name} in {editor}")
        except FileNotFoundError:
            self._update_status_bar(f"Editor '{editor}' not found")

    def _do_revert(self, card: RepoCard, single_file: tuple[str, str] | None = None) -> None:
        name = card.repo_path.name
        if single_file:
            filepath, cat = single_file
            label = {"staged": "staged", "unstaged": "modified", "untracked": "untracked"}.get(cat, cat)
            msg = "Discard file changes?"
            details = f"Repo: {name}\nFile: {filepath}\nState: {label}"
        else:
            staged_count = len(card.status["staged"])
            unstaged_count = len(card.status["unstaged"])
            untracked_count = len(card.status["untracked"])
            total = staged_count + unstaged_count + untracked_count
            if not total:
                self._update_status_bar(f"Nothing to revert in {name}")
                return
            msg = "Discard all local changes?"
            details = (
                f"Repo: {name}\n"
                f"Files affected: {total}\n"
                f"Staged: {staged_count}  Modified: {unstaged_count}  Untracked: {untracked_count}"
            )

        def on_confirm(confirmed: bool) -> None:
            if not confirmed:
                return
            try:
                if single_file:
                    filepath, cat = single_file
                    self._log_action(f"[{name}] revert {filepath} ({cat})")
                    if cat == "staged":
                        card.repo.git.reset("HEAD", "--", filepath)
                        card.repo.git.checkout("--", filepath)
                    elif cat == "unstaged":
                        card.repo.git.checkout("--", filepath)
                    elif cat == "untracked":
                        (Path(card.repo.working_dir) / filepath).unlink(missing_ok=True)
                else:
                    self._log_action(f"[{name}] revert all changes")
                    card.repo.git.checkout(".")
                    card.repo.git.clean("-fd")
                    if card.status["staged"]:
                        card.repo.git.reset("HEAD")
                        card.repo.git.checkout(".")
                card.refresh_status()
                self._update_status_bar(f"Reverted {filepath if single_file else name}")
                self._log_action(f"[{name}] revert OK")
            except GitCommandError as e:
                self._log_action(f"[{name}] ERROR revert: {e}")
                self._update_status_bar(f"Revert failed: {e}")

        self.push_screen(
            ConfirmModal(msg, details=details, confirm_label="Discard", confirm_variant="error"),
            on_confirm,
        )

    def _do_stage(self, card: RepoCard) -> None:
        self.push_screen(StageModal(card.repo))

    def _do_log(self, card: RepoCard) -> None:
        self.push_screen(LogModal(f"Log — {card.repo_path.name}", card.repo))

    def _do_commit(self, card: RepoCard) -> None:
        has_staged = bool(card.status["staged"])
        has_changes = has_staged or bool(card.status["unstaged"]) or bool(card.status["untracked"])
        if not has_changes:
            self._update_status_bar(f"Nothing to commit in {card.repo_path.name}")
            return

        def on_message(msg: str | None) -> None:
            if msg is None:
                return
            try:
                if not has_staged:
                    # Nothing staged yet — stage everything
                    self._log_action(f"[{card.repo_path.name}] git add -A")
                    card.repo.git.add("-A")
                self._log_action(f"[{card.repo_path.name}] git commit -m '{msg}'")
                card.repo.git.commit("-m", msg)
                card.refresh_status()
                self._update_status_bar(f"Committed to {card.repo_path.name}")
                self._log_action(f"[{card.repo_path.name}] commit OK")
            except GitCommandError as e:
                self._log_action(f"[{card.repo_path.name}] ERROR commit: {e}")
                self._update_status_bar(f"Commit failed: {e}")

        self.push_screen(CommitModal(), on_message)

    def _do_diff(self, card: RepoCard, single_file: tuple[str, str] | None = None) -> None:
        if single_file:
            # Show diff for a single file directly
            filepath, cat = single_file
            modal = FileDiffModal(f"Diff — {card.repo_path.name}", [(filepath, cat)], card.repo)
            self.push_screen(modal)
            return
        # Collect all changed files
        files: list[tuple[str, str]] = []
        for f in card.status.get("staged", []):
            files.append((f, "staged"))
        for f in card.status.get("unstaged", []):
            files.append((f, "unstaged"))
        for f in card.status.get("untracked", []):
            files.append((f, "untracked"))
        if not files:
            self._update_status_bar(f"No changes in {card.repo_path.name}")
            return
        self.push_screen(FileDiffModal(f"Diff — {card.repo_path.name}", files, card.repo))

    # -- Navigation --

    def _get_cards(self) -> list[RepoCard]:
        return list(self.query(RepoCard))

    def _focused_card_index(self) -> int:
        cards = self._get_cards()
        focused = self.focused
        for i, card in enumerate(cards):
            if card is focused:
                return i
        return -1

    def action_next_repo(self) -> None:
        cards = self._get_cards()
        if not cards:
            return
        idx = self._focused_card_index()
        nxt = (idx + 1) % len(cards)
        cards[nxt].focus()
        cards[nxt].scroll_visible()

    def action_prev_repo(self) -> None:
        cards = self._get_cards()
        if not cards:
            return
        idx = self._focused_card_index()
        prv = (idx - 1) % len(cards)
        cards[prv].focus()
        cards[prv].scroll_visible()

    def _move_repo(self, direction: int) -> None:
        """Move the focused repo up (-1) or down (+1) in the list."""
        cards = self._get_cards()
        if len(cards) < 2:
            return
        idx = self._focused_card_index()
        if idx < 0:
            return
        new_idx = idx + direction
        if new_idx < 0 or new_idx >= len(cards):
            return
        # Swap in repo_paths
        self.repo_paths[idx], self.repo_paths[new_idx] = self.repo_paths[new_idx], self.repo_paths[idx]
        # Swap cards in the DOM using move_child
        scroll = self.query_one("#main-scroll", VerticalScroll)
        card = cards[idx]
        if direction == 1:
            # Moving down: place after the card below
            scroll.move_child(card, after=cards[new_idx])
        else:
            # Moving up: place before the card above
            scroll.move_child(card, before=cards[new_idx])
        card.focus()
        card.scroll_visible()
        self._log_action(f"Moved {self.repo_paths[new_idx].name} {'down' if direction == 1 else 'up'}")

    def action_move_repo_down(self) -> None:
        self._move_repo(1)

    def action_move_repo_up(self) -> None:
        self._move_repo(-1)

    def action_save_repo_order(self) -> None:
        """Save current repo order to config.toml."""
        if not self.group_name or not self.config:
            self._update_status_bar("No group active — cannot save order")
            return
        from gitdash.config import save_repo_order
        try:
            save_repo_order(self.group_name, self.repo_paths)
            self._update_status_bar(f"Repo order saved for group '{self.group_name}'")
            self._log_action(f"Saved repo order for group '{self.group_name}'")
        except Exception as e:
            self._update_status_bar(f"Failed to save order: {e}")

    # -- Global actions --

    def _auto_refresh(self) -> None:
        """Silently refresh all repo statuses."""
        for card in self.query(RepoCard):
            card.refresh_status()

    def action_refresh_all(self) -> None:
        self._update_status_bar("Refreshing all...")
        for card in self.query(RepoCard):
            card.refresh_status()
        self._update_status_bar("Refreshed")

    @work(thread=True)
    def _startup_fetch(self) -> None:
        cards = self.call_from_thread(self._get_cards)
        total = len(cards)
        self._update_status_bar_from_thread("Fetching all repos...")
        self._log_action_from_thread("startup fetch started")
        for index, card in enumerate(cards, start=1):
            try:
                self._update_status_bar_from_thread(f"Fetching {card.repo_path.name} ({index}/{total})...")
                self._log_action_from_thread(f"[{card.repo_path.name}] git fetch --all --prune")
                card.repo.git.fetch("--all", "--prune")
                self.call_from_thread(card.refresh_status)
                self._log_action_from_thread(f"[{card.repo_path.name}] fetch OK")
            except GitCommandError as e:
                self._log_action_from_thread(f"[{card.repo_path.name}] ERROR fetch: {e}")
        self._log_action_from_thread("startup fetch complete")
        self._update_status_bar_from_thread("Ready")

    @work(thread=True)
    def action_fetch_all(self) -> None:
        cards = self.call_from_thread(self._get_cards)
        total = len(cards)
        successes = 0
        failures: list[str] = []
        self._update_status_bar_from_thread("Fetching all repos...")
        self._log_action_from_thread("fetch all started")
        for index, card in enumerate(cards, start=1):
            try:
                self._update_status_bar_from_thread(f"Fetching {card.repo_path.name} ({index}/{total})...")
                self._log_action_from_thread(f"[{card.repo_path.name}] git fetch --all --prune")
                card.repo.git.fetch("--all", "--prune")
                self.call_from_thread(card.refresh_status)
                successes += 1
                self._log_action_from_thread(f"[{card.repo_path.name}] fetch OK")
            except GitCommandError as e:
                failures.append(f"- `{card.repo_path.name}`: `{e}`")
                self._log_action_from_thread(f"[{card.repo_path.name}] ERROR fetch: {e}")
        self._log_action_from_thread("fetch all complete")
        self._update_status_bar_from_thread(f"Fetch complete: {successes}/{total} repos")
        if failures:
            summary = "\n".join([
                f"Fetched `{successes}` of `{total}` repos.",
                "",
                "Failures:",
                *failures,
            ])
            self.call_from_thread(self._show_message, "Fetch All Summary", summary)

    @work(thread=True)
    def action_pull_all(self) -> None:
        cards = self.call_from_thread(self._get_cards)
        total = len(cards)
        successes = 0
        failures: list[str] = []
        self._update_status_bar_from_thread("Pulling all repos...")
        self._log_action_from_thread("pull all started")
        for index, card in enumerate(cards, start=1):
            try:
                self._update_status_bar_from_thread(f"Pulling {card.repo_path.name} ({index}/{total})...")
                self._log_action_from_thread(f"[{card.repo_path.name}] git pull")
                card.repo.git.pull()
                self.call_from_thread(card.refresh_status)
                successes += 1
                self._log_action_from_thread(f"[{card.repo_path.name}] pull OK")
            except GitCommandError as e:
                failures.append(f"- `{card.repo_path.name}`: `{e}`")
                self._log_action_from_thread(f"[{card.repo_path.name}] ERROR pull: {e}")
        self._log_action_from_thread("pull all complete")
        self._update_status_bar_from_thread(f"Pull complete: {successes}/{total} repos")
        if failures:
            summary = "\n".join([
                f"Pulled `{successes}` of `{total}` repos.",
                "",
                "Failures:",
                *failures,
            ])
            self.call_from_thread(self._show_message, "Pull All Summary", summary)

    def action_search(self) -> None:
        cards = list(self.query(RepoCard))
        if not cards:
            self._update_status_bar("No repos to search")
            return
        self.push_screen(SearchModal(cards))

    def action_help(self) -> None:
        self._show_message(
            "GitDash Help",
            "\n".join(
                [
                    "## Global",
                    "- `j` / `k`: move between repos",
                    "- `Space` / `Enter`: collapse or expand repo",
                    "- `/`: search across repos",
                    "- `F`: fetch all repos",
                    "- `P`: pull all repos",
                    "- `g`: switch group",
                    "- `!`: toggle git operations log",
                    "- `J` / `K`: move repo down or up",
                    "- `S`: save repo order",
                    "",
                    "## Focused Repo",
                    "- `a`: stage or unstage files",
                    "- `b`: switch or create branch",
                    "- `c`: commit staged changes, or stage all and commit",
                    "- `d`: inspect file diffs",
                    "- `e`: open repo or file in editor",
                    "- `l`: view commit log and patch",
                    "- `s`: manage stashes",
                    "- `x`: discard local changes",
                    "",
                    "## Status Cues",
                    "- `staged:n mod:n new:n`: staged, modified, and untracked counts",
                    "- `↑n↓n`: ahead and behind tracking branch",
                    "- `CONFLICT`: merge conflicts need attention",
                    "- `NO-UPSTREAM`: current branch is not tracking a remote",
                    "- `CLEAN`: no local changes and nothing to sync",
                ]
            ),
        )

    def action_switch_group(self) -> None:
        if not self.config or not self.config.groups:
            self._update_status_bar("No groups configured")
            return
        group_names = [g.name for g in self.config.groups]
        self.push_screen(
            BranchModal(group_names, self.group_name or ""),
            self._on_group_selected,
        )

    def _on_group_selected(self, result: str | None) -> None:
        if result is None or result.startswith("__create__"):
            return
        group = self.config.get_group(result)
        if not group:
            self._update_status_bar(f"Group '{result}' not found")
            return
        if not group.repos:
            self._update_status_bar(f"No repos in group '{result}'")
            return
        # Remove old cards
        scroll = self.query_one("#main-scroll", VerticalScroll)
        for card in list(self.query(RepoCard)):
            card.remove()
        # Update state
        self.group_name = group.name
        self.base_path = group.path
        self.repo_paths = group.repos
        self.title = f"GitDash — {group.name}"
        # Mount new cards
        for rp in self.repo_paths:
            scroll.mount(RepoCard(rp, classes="repo-card", id=f"card-{rp.name}"))
        cards = list(self.query(RepoCard))
        if cards:
            cards[0].focus()
        self._update_status_bar(f"Switched to group: {group.name}")


def main() -> None:
    from gitdash.config import load_config, init_config, CONFIG_FILE

    args = sys.argv[1:]

    # --init: create default config
    if "--init" in args:
        path = init_config()
        print(f"Config created at {path}")
        print("Edit it to add your repo groups, then run: gitdash")
        return

    fetch = "--fetch" in args

    # If a path is given, use it directly (backwards compatible)
    if args and not args[0].startswith("-"):
        base = Path(args[0]).expanduser()
        if not base.is_dir():
            print(f"Error: {base} is not a directory")
            sys.exit(1)
        repo_paths = find_repos(base)
        app = GitDash(base, repo_paths, fetch_on_startup=fetch)
        app.run()
        return

    # Load config file
    config = load_config()

    if not config.groups:
        if not CONFIG_FILE.exists():
            print("No config found. Run: gitdash --init")
            print("Or pass a path:    gitdash ~/code/myproject")
            sys.exit(1)
        else:
            print(f"No groups defined in {CONFIG_FILE}")
            sys.exit(1)

    # Pick group: --group NAME, or default_group, or first group
    group = None
    if "--group" in args:
        idx = args.index("--group")
        if idx + 1 < len(args):
            group = config.get_group(args[idx + 1])
            if not group:
                print(f"Group '{args[idx + 1]}' not found. Available: {', '.join(g.name for g in config.groups)}")
                sys.exit(1)

    if not group and config.default_group:
        group = config.get_group(config.default_group)

    if not group:
        group = config.groups[0]

    if not group.repos:
        print(f"No repos found in group '{group.name}' ({group.path})")
        sys.exit(1)

    fetch = fetch or config.fetch_on_startup
    app = GitDash(group.path, group.repos, group.name, fetch_on_startup=fetch, config=config)
    app.run()


if __name__ == "__main__":
    main()
