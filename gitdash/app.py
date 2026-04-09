"""GitDash - Multi-repo git TUI dashboard."""

from __future__ import annotations

import os
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import threading

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


def style_diff(diff_text: str) -> Text:
    """Convert raw diff/patch text into a Rich Text with per-line coloring."""
    styled = Text()
    for i, line in enumerate(diff_text.splitlines()):
        if i > 0:
            styled.append("\n")
        if line.startswith("diff --git") or line.startswith("---") or line.startswith("+++"):
            styled.append(line, style="bold")
        elif line.startswith("@@"):
            styled.append(line, style="cyan bold")
        elif line.startswith("+"):
            styled.append(line, style="green")
        elif line.startswith("-"):
            styled.append(line, style="red")
        elif line.startswith(("index ", "old mode", "new mode", "new file mode",
                              "deleted file mode", "similarity index", "rename",
                              "copy ", "commit ", "Author:", "Date:")):
            styled.append(line, style="dim")
        else:
            styled.append(line)
    return styled


def short_status(repo: Repo) -> dict:
    """Return a dict summarising the repo status.

    Uses a single ``git status --porcelain=v2 --branch`` call plus one
    ``git stash list`` instead of 6-7 separate git operations.
    """
    branch = "?"
    tracking = None
    ahead = behind = 0
    detached = False
    staged: list[str] = []
    unstaged: list[str] = []
    untracked: list[str] = []
    conflicted = False

    try:
        raw = repo.git.status("--porcelain=v2", "--branch", "-uall")
    except GitCommandError:
        raw = ""

    for line in raw.splitlines():
        if line.startswith("# branch.head "):
            head = line[len("# branch.head "):]
            if head == "(detached)":
                branch = "DETACHED"
                detached = True
            else:
                branch = head
        elif line.startswith("# branch.upstream "):
            tracking = line[len("# branch.upstream "):]
        elif line.startswith("# branch.ab "):
            parts = line.split()
            # format: # branch.ab +N -M
            for p in parts:
                if p.startswith("+"):
                    try:
                        ahead = int(p)
                    except ValueError:
                        pass
                elif p.startswith("-"):
                    try:
                        behind = abs(int(p))
                    except ValueError:
                        pass
        elif line.startswith("? "):
            # Untracked file
            untracked.append(line[2:])
        elif line.startswith("u "):
            # Unmerged (conflict) entry
            conflicted = True
            # Extract filename: u XY sub m1 m2 m3 mW h1 h2 h3 <path>
            u_parts = line.split(" ", 10)
            if len(u_parts) == 11:
                unstaged.append(u_parts[10])
        elif line.startswith("1 ") or line.startswith("2 "):
            # Changed entry: field index 1 has XY status
            parts = line.split(" ", 8)
            if len(parts) >= 9:
                xy = parts[1]
                # For rename entries (2), filename is after tab
                if line.startswith("2 "):
                    tab_parts = line.split("\t")
                    fname = tab_parts[1] if len(tab_parts) >= 2 else parts[-1]
                else:
                    fname = parts[8]
                if xy[0] not in (".", "?"):
                    staged.append(fname)
                if xy[1] not in (".", "?"):
                    unstaged.append(fname)

    try:
        stash_output = repo.git.stash("list")
        stashes = len(stash_output.splitlines()) if stash_output else 0
    except GitCommandError:
        stashes = 0

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
        "detached": detached,
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


_AI_PROMPT = (
    "Generate a concise git commit message (one line, max 72 chars, imperative mood) "
    "for this diff. Reply with ONLY the commit message, nothing else.\n\n"
)

_DEFAULT_MODELS: dict[str, str] = {
    "anthropic": "claude-sonnet-4-20250514",
    "openai": "gpt-4o-mini",
    "ollama": "llama3",
}


def _generate_commit_message(diff_text: str, ai_cfg: "AIConfig | None" = None) -> tuple[str | None, str]:
    """Generate a commit message via the configured AI provider.

    Returns (message, error).  On success error is empty; on failure message is None.
    """
    if not ai_cfg or not ai_cfg.provider or not diff_text.strip():
        return None, "AI not configured"

    api_key = ai_cfg.resolve_api_key()
    provider = ai_cfg.provider.lower()
    model = ai_cfg.model or _DEFAULT_MODELS.get(provider, "")
    truncated = diff_text[:8000]
    prompt = _AI_PROMPT + truncated

    if provider not in _DEFAULT_MODELS:
        return None, f"Unsupported provider: {ai_cfg.provider}"

    if provider in ("anthropic", "openai") and not api_key:
        return None, f"API key not set for {provider}"

    try:
        if provider == "anthropic":
            import anthropic
            client = anthropic.Anthropic(api_key=api_key)
            resp = client.messages.create(
                model=model,
                max_tokens=100,
                messages=[{"role": "user", "content": prompt}],
            )
            return resp.content[0].text.strip().strip('"').strip("'"), ""

        elif provider == "openai":
            import openai
            client = openai.OpenAI(api_key=api_key)
            resp = client.chat.completions.create(
                model=model,
                max_tokens=100,
                messages=[{"role": "user", "content": prompt}],
            )
            return (resp.choices[0].message.content or "").strip().strip('"').strip("'"), ""

        elif provider == "ollama":
            import json
            import urllib.request
            base = ai_cfg.base_url or "http://localhost:11434"
            url = f"{base}/api/generate"
            payload = json.dumps({"model": model, "prompt": prompt, "stream": False}).encode()
            req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
            return (data.get("response") or "").strip().strip('"').strip("'"), ""

    except Exception as e:
        return None, str(e)


class CommitModal(ModalScreen[str | None]):
    """Modal to enter a commit message."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, diff_text: str = "", ai_cfg: "AIConfig | None" = None) -> None:
        super().__init__()
        self._diff_text = diff_text
        self._ai_cfg = ai_cfg

    def compose(self) -> ComposeResult:
        with Vertical(id="commit-dialog"):
            yield Label("\u270e Commit Message", id="commit-title")
            ai_ready = self._diff_text and self._ai_cfg and self._ai_cfg.provider
            placeholder = "Generating commit message..." if ai_ready else "Enter commit message..."
            yield Input(placeholder=placeholder, id="commit-input")
            with Horizontal(id="commit-buttons"):
                yield Button("\u2713 Commit", variant="success", id="btn-commit")
                yield Button("\u2717 Cancel", variant="error", id="btn-cancel")

    def on_mount(self) -> None:
        inp = self.query_one("#commit-input", Input)
        inp.focus()
        if self._diff_text and self._ai_cfg and self._ai_cfg.provider:
            self._request_ai_message()

    def _request_ai_message(self) -> None:
        """Generate AI commit message in a background thread."""
        def _bg() -> None:
            msg, err = _generate_commit_message(self._diff_text, self._ai_cfg)
            if msg:
                self.app.call_from_thread(self._apply_suggestion, msg)
            else:
                self.app.call_from_thread(self._set_placeholder, f"AI failed: {err}" if err else "Enter commit message...")

        threading.Thread(target=_bg, daemon=True).start()

    def _apply_suggestion(self, msg: str) -> None:
        try:
            inp = self.query_one("#commit-input", Input)
        except Exception:
            return
        if not inp.value:
            inp.value = msg
            inp.placeholder = "Enter commit message..."

    def _set_placeholder(self, text: str = "Enter commit message...") -> None:
        try:
            inp = self.query_one("#commit-input", Input)
        except Exception:
            return
        inp.placeholder = text

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
        ("\u2423", "toggle"),
        ("j/k", "move"),
        ("J/K", "reorder"),
        ("/", "search"),
        ("F", "fetch"),
        ("P", "pull"),
        ("g", "group"),
        ("G", "edit groups"),
        ("t", "terminal"),
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
            self._build_line("\u2502 Repo", self.REPO_ITEMS, title_style, key_style, label_style, sep_style),
        )
        self.query_one("#shortcut-global", Static).update(
            self._build_line("\u2502 Global", self.GLOBAL_ITEMS, title_style, key_style, label_style, sep_style),
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
            yield Label("\u2387 Switch Branch", id="branch-title")
            yield Input(placeholder="Filter or new branch name...", id="branch-filter")
            yield ListView(id="branch-list")
            with Horizontal(id="branch-buttons"):
                yield Button("\u21c4 Switch", variant="success", id="btn-switch")
                yield Button("+ Create & Switch", variant="primary", id="btn-create")
                yield Button("\u2717 Cancel", variant="error", id="btn-cancel")

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
            yield Label("\u2261 Stash Manager", id="branch-title")
            yield ListView(id="stash-lv")
            with Horizontal(id="branch-buttons"):
                if self.has_changes:
                    yield Button("\u2913 Stash", variant="success", id="btn-stash-push")
                yield Button("\u2912 Pop", variant="primary", id="btn-stash-pop")
                yield Button("\u21b3 Apply", variant="primary", id="btn-stash-apply")
                yield Button("\u2717 Drop", variant="error", id="btn-stash-drop")
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
                target = (self.file_repo_path / self.title_text).resolve()
                repo_root = self.file_repo_path.resolve()
                if not str(target).startswith(str(repo_root) + os.sep) and target != repo_root:
                    return
                try:
                    subprocess.Popen([editor, str(target)])
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
            yield Label(f"\u2630 {self.title_text}", id="diff-title")
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
            lv.append(ListItem(Label(f"\u2022 {short_sha}  {date}  {msg}"), id=f"lg-{idx}"))

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
        log.write(style_diff(diff_text))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.app.pop_screen()

    def action_close(self) -> None:
        self.app.pop_screen()


from gitdash._terminal import Terminal


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
            yield Label("\u2713 Stage / Unstage Files", id="diff-title")
            yield ListView(id="stage-list")
            with Horizontal(id="filediff-buttons"):
                yield Button("\u2713 Stage All", variant="success", id="btn-stage-all")
                yield Button("\u2717 Unstage All", variant="warning", id="btn-unstage-all")
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
            lv.append(ListItem(Label("\u2500\u2500 \u2713 Staged (will be committed) \u2500\u2500")))
            for f in staged:
                idx = self._next_id; self._next_id += 1
                lv.append(ListItem(Label(f"  \u25c9 {f}"), id=f"sf-{idx}"))
                self._file_map[idx] = (f, "staged")
        if unstaged:
            lv.append(ListItem(Label("\u2500\u2500 \u270e Modified (not staged) \u2500\u2500")))
            for f in unstaged:
                idx = self._next_id; self._next_id += 1
                lv.append(ListItem(Label(f"  \u25cb {f}"), id=f"sf-{idx}"))
                self._file_map[idx] = (f, "unstaged")
        if untracked:
            lv.append(ListItem(Label("\u2500\u2500 + Untracked \u2500\u2500")))
            for f in untracked:
                idx = self._next_id; self._next_id += 1
                lv.append(ListItem(Label(f"  \u25cb {f}"), id=f"sf-{idx}"))
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
            yield Label("\u2315 Search Across Repos", id="diff-title")
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
                full_path = (Path(working_dir) / filepath).resolve()
                repo_root = Path(working_dir).resolve()
                if not str(full_path).startswith(str(repo_root) + os.sep):
                    return
                if full_path.exists() and full_path.is_file():
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
                full_path = (Path(working_dir) / filepath).resolve()
                repo_root = Path(working_dir).resolve()
                if str(full_path).startswith(str(repo_root) + os.sep):
                    self.app._do_open_editor_path(str(full_path))
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
        log.write(style_diff(self.diff_text) if self.diff_text else "(no diff)")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.app.pop_screen()

    def action_close(self) -> None:
        self.app.pop_screen()


class GroupEditorModal(ModalScreen):
    """Modal to create, edit, reorder, and delete repo groups."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, config) -> None:
        super().__init__()
        self._config = config
        # Working copy: list of dicts with name, path, discovered, checked
        self._groups: list[dict] = []
        for g in config.groups:
            self._groups.append({
                "name": g.name,
                "path": str(g.path),
                "discovered": list(g.repos),
                "checked": {rp.name for rp in g.repos},
            })
        self._sel = 0

    def compose(self) -> ComposeResult:
        with Vertical(id="group-editor-dialog"):
            yield Label("\u2699 Edit Groups", id="branch-title")
            with Horizontal(id="group-editor-body"):
                with Vertical(id="group-left-panel"):
                    yield Label("Groups", classes="grp-section-label")
                    yield ListView(id="group-list")
                    with Horizontal(id="group-left-buttons"):
                        yield Button("\u2191", id="btn-grp-up", variant="default")
                        yield Button("\u2193", id="btn-grp-down", variant="default")
                        yield Button("+ Add", id="btn-grp-add", variant="primary")
                        yield Button("\u2715 Del", id="btn-grp-del", variant="error")
                with Vertical(id="group-right-panel"):
                    yield Label("Name", classes="grp-section-label")
                    yield Input(placeholder="group name", id="grp-name-input")
                    yield Label("Path", classes="grp-section-label")
                    with Horizontal(id="grp-path-row"):
                        yield Input(placeholder="~/path/to/repos", id="grp-path-input")
                        yield Button("Scan", id="btn-scan", variant="primary")
                    yield Label("Repos \u2014 select to toggle (all or none checked = auto-discover)", id="grp-repos-label", classes="grp-section-label")
                    yield ListView(id="group-repo-list")
            with Horizontal(id="group-editor-buttons"):
                yield Button("\u2713 Save & Apply", variant="success", id="btn-grp-save")
                yield Button("\u2717 Cancel", variant="error", id="btn-grp-cancel")

    def on_mount(self) -> None:
        self._render_group_list()
        if self._groups:
            self._load_right_panel(0)
        else:
            self.query_one("#group-list", ListView).focus()

    # ── Rendering helpers ────────────────────────────────────────────────────

    def _render_group_list(self) -> None:
        lv = self.query_one("#group-list", ListView)
        lv.clear()
        for i, g in enumerate(self._groups):
            marker = "\u25b6 " if i == self._sel else "  "
            lv.append(ListItem(Label(f"{marker}{g['name']}"), id=f"grp-{i}"))

    def _load_right_panel(self, idx: int) -> None:
        if not self._groups or idx >= len(self._groups):
            return
        g = self._groups[idx]
        self.query_one("#grp-name-input", Input).value = g["name"]
        self.query_one("#grp-path-input", Input).value = g["path"]
        self._render_repo_list(idx)

    def _render_repo_list(self, idx: int) -> None:
        lv = self.query_one("#group-repo-list", ListView)
        lv.clear()
        if idx >= len(self._groups):
            return
        g = self._groups[idx]
        discovered = g["discovered"]
        if not discovered:
            lv.append(ListItem(Label("  (no repos \u2014 enter path above and press Scan)")))
            return
        for rp in discovered:
            checked = rp.name in g["checked"]
            mark = "\u25c9" if checked else "\u25cb"
            lv.append(ListItem(Label(f"  {mark} {rp.name}"), id=f"repo-{rp.name}"))

    def _scan_path(self, idx: int) -> None:
        if idx >= len(self._groups):
            return
        from gitdash.config import _discover_repos
        raw = self.query_one("#grp-path-input", Input).value.strip()
        if raw:
            self._groups[idx]["path"] = raw
        path = Path(self._groups[idx]["path"]).expanduser()
        discovered = _discover_repos(path)
        self._groups[idx]["discovered"] = discovered
        # Preserve existing selection; default all checked for new groups
        existing = self._groups[idx]["checked"]
        if not existing:
            self._groups[idx]["checked"] = {r.name for r in discovered}
        else:
            self._groups[idx]["checked"] = {n for n in existing if any(r.name == n for r in discovered)}
            if not self._groups[idx]["checked"]:
                self._groups[idx]["checked"] = {r.name for r in discovered}
        self._render_repo_list(idx)

    # ── State helpers ────────────────────────────────────────────────────────

    def _flush_inputs(self) -> None:
        """Save name/path inputs back into the working state for current group."""
        if not self._groups or self._sel >= len(self._groups):
            return
        name = self.query_one("#grp-name-input", Input).value.strip()
        path = self.query_one("#grp-path-input", Input).value.strip()
        if name:
            self._groups[self._sel]["name"] = name
        if path:
            self._groups[self._sel]["path"] = path

    # ── Events ───────────────────────────────────────────────────────────────

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if event.list_view.id == "group-list":
            if event.item and event.item.id and event.item.id.startswith("grp-"):
                self._flush_inputs()
                idx = int(event.item.id.removeprefix("grp-"))
                self._sel = idx
                self._render_group_list()
                self._load_right_panel(idx)
        elif event.list_view.id == "group-repo-list":
            if not event.item or not event.item.id or not event.item.id.startswith("repo-"):
                return
            repo_name = event.item.id.removeprefix("repo-")
            g = self._groups[self._sel]
            if repo_name in g["checked"]:
                g["checked"].discard(repo_name)
            else:
                g["checked"].add(repo_name)
            self._render_repo_list(self._sel)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "btn-grp-up":
            if self._sel > 0:
                self._flush_inputs()
                i = self._sel
                self._groups[i], self._groups[i - 1] = self._groups[i - 1], self._groups[i]
                self._sel -= 1
                self._render_group_list()
                self._load_right_panel(self._sel)
        elif bid == "btn-grp-down":
            if self._sel < len(self._groups) - 1:
                self._flush_inputs()
                i = self._sel
                self._groups[i], self._groups[i + 1] = self._groups[i + 1], self._groups[i]
                self._sel += 1
                self._render_group_list()
                self._load_right_panel(self._sel)
        elif bid == "btn-grp-add":
            self._flush_inputs()
            self._groups.append({"name": "new-group", "path": "~", "discovered": [], "checked": set()})
            self._sel = len(self._groups) - 1
            self._render_group_list()
            self._load_right_panel(self._sel)
            self.query_one("#grp-name-input", Input).focus()
        elif bid == "btn-grp-del":
            if self._groups:
                self._groups.pop(self._sel)
                self._sel = max(0, self._sel - 1)
                self._render_group_list()
                if self._groups:
                    self._load_right_panel(self._sel)
                else:
                    self.query_one("#grp-name-input", Input).value = ""
                    self.query_one("#grp-path-input", Input).value = ""
                    self.query_one("#group-repo-list", ListView).clear()
        elif bid == "btn-scan":
            self._scan_path(self._sel)
        elif bid == "btn-grp-save":
            self._flush_inputs()
            self._apply_save()
        elif bid == "btn-grp-cancel":
            self.dismiss(None)

    def _apply_save(self) -> None:
        from gitdash.config import save_all_groups, RepoGroup
        new_groups = []
        live_repo_map: dict[str, list[Path]] = {}
        for g in self._groups:
            name = g["name"].strip()
            if not name:
                continue
            path = Path(g["path"]).expanduser()
            discovered = g["discovered"]
            checked = g["checked"]
            all_names = {r.name for r in discovered}
            if not checked or checked == all_names:
                # Auto-discover: write empty repos list (omits key in TOML),
                # but keep discovered repos for the current session
                save_repos: list[Path] = []
                live_repos = discovered
            else:
                save_repos = [rp for rp in discovered if rp.name in checked]
                live_repos = save_repos
            new_groups.append(RepoGroup(name=name, path=path, repos=save_repos))
            live_repo_map[name] = live_repos
        self._config.groups = new_groups
        try:
            save_all_groups(self._config)
        except Exception as e:
            self.notify(f"Config save failed: {e}", severity="error")
        # Restore live repos in-memory so the current session loads correctly
        for grp in self._config.groups:
            if not grp.repos and grp.name in live_repo_map:
                grp.repos = live_repo_map[grp.name]
        self.dismiss(self._config)

    def action_cancel(self) -> None:
        self.dismiss(None)


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
            yield Label(f"\u00b1 {self.title_text}", id="diff-title")
            yield Input(placeholder="Filter files...", id="filediff-filter")
            yield ListView(id="filediff-list")
            yield RichLog(id="diff-log", wrap=True, markup=False)
            with Horizontal(id="filediff-buttons"):
                yield Button("\u270e Open in Editor", variant="success", id="btn-edit-file")
                yield Button("\u2717 Revert File", variant="error", id="btn-revert-file")
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
                label_text = {"staged": "\u2713 Staged", "unstaged": "\u270e Modified", "untracked": "+ Untracked"}.get(cat, cat)
                lv.append(ListItem(Label(f"\u2500\u2500 {label_text} \u2500\u2500")))
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
        log.write(style_diff(diff_text))

    def _get_file_diff(self, filepath: str, category: str) -> str:
        try:
            if category == "staged":
                return self.repo.git.diff("--cached", "--", filepath) or "(no diff)"
            elif category == "unstaged":
                return self.repo.git.diff("--", filepath) or "(no diff)"
            else:
                # Untracked: show file contents as new file
                full_path = Path(self.repo.working_dir) / filepath
                if full_path.is_dir():
                    return f"(directory: {filepath})"
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
                p = Path(self.repo.working_dir) / filepath
                resolved = p.resolve()
                repo_root = Path(self.repo.working_dir).resolve()
                if not str(resolved).startswith(str(repo_root) + os.sep):
                    return
                if p.is_symlink():
                    p.unlink(missing_ok=True)
                elif p.is_dir():
                    shutil.rmtree(p, ignore_errors=True)
                else:
                    p.unlink(missing_ok=True)
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
        Binding("u", "undo_commit", "Undo commit", show=True),
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
            yield Button("\u25bc", id=f"toggle-{self.repo_path.name}", classes="toggle-btn")
            yield Static(self.repo_path.name, classes="repo-name")
            yield Static("", id=f"branch-{self.repo_path.name}", classes="repo-branch")
            yield Static("", id=f"changes-{self.repo_path.name}", classes="repo-changes")
            yield Static("", id=f"sync-{self.repo_path.name}", classes="repo-sync")
            yield Static("", id=f"flags-{self.repo_path.name}", classes="repo-flags")
        yield Static("", id=f"syncbtn-{self.repo_path.name}", classes="sync-btn")
        with Vertical(id=f"body-{self.repo_path.name}", classes="repo-body"):
            yield Tree("Changes", id=f"tree-{self.repo_path.name}")
            with Horizontal(classes="repo-actions"):
                yield Button("\u21bb Fetch", id=f"fetch-{self.repo_path.name}", classes="action-btn fetch-btn")
                yield Button("\u2387 Branch", id=f"brn-{self.repo_path.name}", classes="action-btn branch-btn")
                yield Button("\u2261 Stash", id=f"stash-{self.repo_path.name}", classes="action-btn stash-btn")
                yield Button("\u2713 Commit", id=f"cmt-{self.repo_path.name}", classes="action-btn accent")
                yield Button("\u21a9 Undo", id=f"undo-{self.repo_path.name}", classes="action-btn")
                yield Button("\u00b1 Diff", id=f"diff-{self.repo_path.name}", classes="action-btn diff-btn")

    def on_mount(self) -> None:
        self._initial_refresh()

    @work(thread=True)
    def _initial_refresh(self) -> None:
        """Load git status off the main thread on first mount."""
        status = self._read_status()
        self.app.call_from_thread(self.apply_status, status)

    def _read_status(self) -> dict:
        """Read git status from disk (safe to call from any thread)."""
        try:
            return short_status(self.repo)
        except (InvalidGitRepositoryError, Exception):
            return {"branch": "?", "tracking": None, "ahead": 0, "behind": 0,
                    "staged": [], "unstaged": [], "untracked": [], "stashes": 0,
                    "conflicted": False, "dirty": False, "detached": False}

    def apply_status(self, status: dict) -> None:
        """Update widgets from a pre-computed status dict (must run on main thread)."""
        self.status = status
        self._update_widgets()

    def refresh_status(self) -> None:
        """Read git status and update widgets (convenience for main-thread callers)."""
        self.apply_status(self._read_status())

    def _theme_color(self, var: str, fallback: str = "#808080") -> str:
        """Get a theme color, stripping alpha suffix for Rich compatibility."""
        colors = self.app.get_css_variables()
        c = colors.get(var, fallback)
        return c[:7] if len(c) == 9 and c.startswith("#") else c

    def _update_widgets(self) -> None:
        """Push self.status into all child widgets (must run on main thread)."""
        name = self.repo_path.name
        try:
            branch_lbl = self.query_one(f"#branch-{name}", Static)
            branch_lbl.update(f"\u2387 {self.status['branch']}")
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
                change_parts.append(f"\u2713{staged_count}")
            if unstaged_count:
                change_parts.append(f"\u270e{unstaged_count}")
            if untracked_count:
                change_parts.append(f"+{untracked_count}")
            changes_lbl.update(" ".join(change_parts))
        except NoMatches:
            pass

        sync_parts = []
        if self.status["ahead"] or self.status["behind"]:
            sync_parts.append(f"\u2191{self.status['ahead']}\u2193{self.status['behind']}")
        if self.status["stashes"]:
            sync_parts.append(f"\u2261{self.status['stashes']}")
        try:
            sync_lbl = self.query_one(f"#sync-{name}", Static)
            sync_lbl.update(" ".join(sync_parts))
        except NoMatches:
            pass

        try:
            flags_lbl = self.query_one(f"#flags-{name}", Static)
            flags = []
            if self.status["conflicted"]:
                flags.append("\u26a0 CONFLICT")
            if not self.status["detached"] and not self.status["tracking"]:
                flags.append("\u2bee NO-UPSTREAM")
            if not total_changes and not sync_parts and not flags:
                flags.append("\u2714 CLEAN")
            flags_lbl.update(" ".join(flags))
            if self.status["conflicted"]:
                flags_lbl.styles.color = self._theme_color("error", "#f38ba8")
                flags_lbl.styles.text_style = "bold"
            elif not total_changes and not sync_parts and (self.status["detached"] or self.status["tracking"]):
                flags_lbl.styles.color = self._theme_color("success", "#a6e3a1")
                flags_lbl.styles.text_style = "none"
            else:
                flags_lbl.styles.color = self._theme_color("primary", "#89dceb")
                flags_lbl.styles.text_style = "none"
        except NoMatches:
            pass

        # Dynamic card border class based on state
        self.remove_class("clean-card", "dirty-card", "conflict-card")
        if self.status["conflicted"]:
            self.add_class("conflict-card")
        elif total_changes or sync_parts:
            self.add_class("dirty-card")
        else:
            self.add_class("clean-card")

        # Update sync bar using inline styles
        ahead = self.status["ahead"]
        behind = self.status["behind"]
        has_local = bool(self.status["staged"] or self.status["unstaged"] or self.status["untracked"])
        try:
            sync_bar = self.query_one(f"#syncbtn-{name}", Static)
            if behind or ahead:
                if behind and ahead:
                    label = f"\u2261 Stash, Pull & Push  \u2191{ahead} \u2193{behind}" if has_local else f"\u21c5 Sync  \u2191{ahead} \u2193{behind}"
                elif behind:
                    label = f"\u2261 Stash & Pull  \u2193{behind}" if has_local else f"Pull  \u2193{behind}"
                else:
                    label = f"Push  \u2191{ahead}"
                sync_bar.update(label)
                color = self._theme_color("primary", "#89b4fa") if behind else self._theme_color("success", "#a6e3a1")
                sync_bar.styles.background = color
                sync_bar.styles.color = self._theme_color("background", "#1e1e2e")
                sync_bar.styles.text_align = "center"
                sync_bar.styles.text_style = "bold"
                sync_bar.styles.padding = (0, 1)
                sync_bar.display = True
            else:
                sync_bar.update("")
                sync_bar.styles.background = None
                sync_bar.styles.padding = (0, 0)
                sync_bar.display = False
        except NoMatches:
            pass

        try:
            tree: Tree = self.query_one(f"#tree-{name}", Tree)
            tree.clear()
            total = len(self.status["staged"]) + len(self.status["unstaged"]) + len(self.status["untracked"])
            tree.root.set_label(f"\u2500 Changes ({total})")
            if self.status["staged"]:
                staged_node = tree.root.add("\u2713 Staged", expand=True)
                for f in self.status["staged"]:
                    staged_node.add_leaf(f"  {f}")
            if self.status["unstaged"]:
                mod_node = tree.root.add("\u270e Modified", expand=True)
                for f in self.status["unstaged"]:
                    mod_node.add_leaf(f"  {f}")
            if self.status["untracked"]:
                unt_node = tree.root.add("+ Untracked", expand=True)
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
            btn.label = "\u25b6" if value else "\u25bc"
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

    def action_undo_commit(self) -> None:
        self.app._do_undo_commit(self)

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
            if full_path.is_file():
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

    /* ── Repo Cards ────────────────────────────────────── */

    .repo-card {
        margin: 0 1;
        padding: 0;
        border: solid $primary-background;
        height: auto;
    }

    .repo-card:focus, .repo-card:focus-within {
        border: solid $accent;
    }

    .repo-card.clean-card {
        border: solid $primary-background;
    }

    .repo-card.clean-card:focus, .repo-card.clean-card:focus-within {
        border: solid $accent;
    }

    .repo-card.dirty-card {
        border: solid $primary-background;
    }

    .repo-card.dirty-card:focus, .repo-card.dirty-card:focus-within {
        border: solid $accent;
    }

    .repo-card.dirty-card > .repo-header {
        background: $warning 15%;
    }

    .repo-card.conflict-card {
        border: solid $primary-background;
    }

    .repo-card.conflict-card:focus, .repo-card.conflict-card:focus-within {
        border: solid $accent;
    }

    .repo-card.conflict-card > .repo-header {
        background: $error 15%;
    }

    .repo-header {
        height: 1;
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
        color: $text-muted;
    }

    .toggle-btn:hover {
        color: $text;
    }

    .repo-name {
        color: $text;
        text-style: bold;
        width: auto;
        min-width: 20;
    }

    .repo-branch {
        color: $success;
        width: auto;
        margin-left: 1;
        text-style: bold;
    }

    .repo-changes {
        color: $warning;
        width: auto;
        margin-left: 1;
    }

    .repo-sync {
        color: $accent;
        width: auto;
        margin-left: 1;
        text-style: bold;
    }

    .repo-flags {
        width: auto;
        margin-left: 1;
    }

    /* ── Card Body & Tree ─────────────────────────────── */

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
        background: transparent;
    }

    .repo-actions {
        height: 3;
        padding: 0 1;
        align: left middle;
    }

    .action-btn {
        min-width: 10;
        height: 1;
        margin: 0 1 0 0;
        border: none;
        background: $panel;
        color: $text;
    }

    .action-btn:hover {
        background: $boost;
        color: $text;
    }

    .action-btn.accent {
        background: $success;
        color: $panel;
        text-style: bold;
    }

    .action-btn.accent:hover {
        background: $success;
        color: $panel;
    }

    .action-btn.fetch-btn {
        background: $panel;
        color: $primary;
    }

    .action-btn.fetch-btn:hover {
        background: $boost;
    }

    .action-btn.branch-btn {
        background: $panel;
        color: $success;
    }

    .action-btn.branch-btn:hover {
        background: $boost;
    }

    .action-btn.stash-btn {
        background: $panel;
        color: $warning;
    }

    .action-btn.stash-btn:hover {
        background: $boost;
    }

    .action-btn.diff-btn {
        background: $panel;
        color: $accent;
    }

    .action-btn.diff-btn:hover {
        background: $boost;
    }

    .sync-btn {
        width: 100%;
        height: auto;
    }

    /* ── Modals ────────────────────────────────────────── */

    #terminal-panel {
        dock: bottom;
        height: 14;
        display: none;
        border-top: solid $accent;
        background: $surface;
    }

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

    #search-results {
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
        color: $text;
    }

    #confirm-details {
        color: $text-muted;
        margin-bottom: 1;
        width: 100%;
        padding: 0 1;
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

    #stash-lv {
        height: auto;
        max-height: 12;
        border: solid $primary-background;
        margin: 1 0;
    }

    /* ── Group Editor ─────────────────────────────────── */

    #group-editor-dialog {
        width: 95%;
        height: 85%;
        border: thick $primary;
        background: $surface;
        padding: 1 2;
    }

    #group-editor-body {
        height: 1fr;
        margin: 1 0;
    }

    #group-left-panel {
        width: 28;
        border-right: solid $primary-background;
        padding-right: 1;
    }

    #group-list {
        height: 1fr;
        border: solid $primary-background;
        margin: 1 0;
    }

    #group-left-buttons {
        height: 3;
        align: left middle;
    }

    #group-left-buttons Button {
        min-width: 5;
        margin-right: 1;
        height: 1;
        border: none;
    }

    #group-right-panel {
        width: 1fr;
        padding-left: 1;
    }

    .grp-section-label {
        color: $text-muted;
        height: 1;
        margin-top: 1;
    }

    #grp-path-row {
        height: 3;
    }

    #grp-path-row Input {
        width: 1fr;
    }

    #grp-path-row Button {
        min-width: 8;
        margin-left: 1;
    }

    #group-repo-list {
        height: 1fr;
        border: solid $primary-background;
        margin: 1 0;
    }

    #group-editor-buttons {
        height: 3;
        align: center middle;
        margin-top: 1;
    }

    /* ── Bottom Bars ──────────────────────────────────── */

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
        height: 3;
        padding: 0 1;
        background: $primary-background;
        color: $text;
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
        Binding("G", "edit_groups", "Edit Groups"),
        Binding("slash", "search", "Search"),
        Binding("t", "toggle_terminal", "Terminal"),
        Binding("ctrl+t", "toggle_terminal", "Terminal", show=False, priority=True),
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
        yield Terminal(command="bash", id="terminal-panel")
        yield ShortcutBar(id="shortcut-bar")
        yield Static("", id="status-bar")

    def on_mount(self) -> None:
        title_suffix = self.group_name or str(self.base_path)
        self.title = f"GitDash \u2014 {title_suffix}"
        self._update_status_bar("\u2713 Ready")
        cards = list(self.query(RepoCard))
        if cards:
            cards[0].focus()
        if self.fetch_on_startup:
            self._startup_fetch()
        self.set_interval(60, self._auto_refresh)

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

    def _refresh_card_from_thread(self, card: RepoCard) -> None:
        """Read status off-thread and apply on main thread."""
        status = card._read_status()
        self.call_from_thread(card.apply_status, status)

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
        elif bid.startswith("undo-"):
            self._do_undo_commit(card)
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
                    self._refresh_card_from_thread(card)
                    return
            self._refresh_card_from_thread(card)
            self._update_status_bar_from_thread(f"Synced {name}")
            self._log_action_from_thread(f"[{name}] sync complete")
        except GitCommandError as e:
            self._log_action_from_thread(f"[{name}] ERROR sync: {e}")
            if stashed:
                try:
                    card.repo.git.stash("pop")
                except GitCommandError:
                    pass
            self._refresh_card_from_thread(card)
            self._update_status_bar_from_thread(f"Sync failed: {e}")

    @work(thread=True)
    def _do_fetch(self, card: RepoCard) -> None:
        name = card.repo_path.name
        self._update_status_bar_from_thread(f"Fetching {name}...")
        self._log_action_from_thread(f"[{name}] git fetch --all --prune")
        try:
            card.repo.git.fetch("--all", "--prune")
            self._refresh_card_from_thread(card)
            self._update_status_bar_from_thread(f"Fetched {name}")
            self._log_action_from_thread(f"[{name}] fetch OK")
        except GitCommandError as e:
            self._log_action_from_thread(f"[{name}] ERROR fetch: {e}")
            self._update_status_bar_from_thread(f"Fetch failed: {e}")

    @work(thread=True)
    def _do_branch(self, card: RepoCard) -> None:
        # Load branch refs off the main thread
        local_branches = [h.name for h in card.repo.heads]
        remote_branches = []
        origin = getattr(card.repo.remotes, "origin", None) if card.repo.remotes else None
        for ref in (origin.refs if origin else []):
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

        self.call_from_thread(self.push_screen, BranchModal(branches, current), on_result)

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
        if not shutil.which(editor):
            self._update_status_bar(f"Editor '{editor}' not found on PATH")
            return
        try:
            subprocess.Popen([editor, full_path])
            self._update_status_bar(f"Opened {Path(full_path).name} in {editor}")
        except FileNotFoundError:
            self._update_status_bar(f"Editor '{editor}' not found")

    def _do_open_editor(self, card: RepoCard, filepath: str | None = None) -> None:
        if filepath:
            self._do_open_editor_path(str(Path(card.repo.working_dir) / filepath))
        else:
            self._do_open_editor_path(str(card.repo_path))

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
                        p = Path(card.repo.working_dir) / filepath
                        if p.is_dir():
                            shutil.rmtree(p, ignore_errors=True)
                        else:
                            p.unlink(missing_ok=True)
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

    def action_toggle_terminal(self) -> None:
        """Toggle the inline terminal panel, scoped to the focused repo."""
        try:
            term = self.query_one("#terminal-panel", Terminal)
        except NoMatches:
            return
        if term.display:
            # Just hide and unfocus — keep the shell alive
            term.display = False
            cards = self._get_cards()
            if cards:
                cards[max(0, self._focused_card_index())].focus()
        else:
            # Start only on first open; subsequent toggles just show/focus
            if term.emulator is None:
                idx = self._focused_card_index()
                cards = self._get_cards()
                if cards and idx >= 0:
                    term._cwd = str(cards[idx].repo_path)
                elif cards:
                    term._cwd = str(cards[0].repo_path)
                else:
                    self._update_status_bar("No repos available for terminal")
                    return
                term.start()
            term.display = True
            term.focus()

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

        # Gather diff for AI commit message generation
        diff_text = ""
        try:
            if has_staged:
                diff_text = card.repo.git.diff("--cached")
            else:
                diff_text = card.repo.git.diff()
                # Include untracked file names
                untracked = card.status.get("untracked", [])
                if untracked:
                    diff_text += "\n\nNew untracked files:\n" + "\n".join(untracked)
        except GitCommandError:
            pass

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

        ai_cfg = self.config.ai if self.config else None
        self.push_screen(CommitModal(diff_text=diff_text, ai_cfg=ai_cfg), on_message)

    def _do_undo_commit(self, card: RepoCard) -> None:
        """Undo the last commit (soft reset), only if it hasn't been pushed."""
        name = card.repo_path.name
        ahead = card.status.get("ahead", 0)
        tracking = card.status.get("tracking")
        if not tracking and not ahead:
            # No upstream — commits are local-only, safe to undo
            pass
        elif not ahead:
            self._update_status_bar(f"No unpushed commits to undo in {name}")
            return

        # Get last commit message for confirmation
        try:
            last_msg = card.repo.head.commit.message.strip().split("\n")[0][:60]
            short_sha = card.repo.head.commit.hexsha[:7]
        except Exception:
            last_msg = "(unknown)"
            short_sha = "?"

        def on_confirm(confirmed: bool) -> None:
            if not confirmed:
                return
            # Re-check ahead count with fresh status to avoid undoing pushed commits
            fresh = card._read_status()
            fresh_tracking = fresh.get("tracking")
            fresh_ahead = fresh.get("ahead", 0)
            if fresh_tracking and not fresh_ahead:
                self._update_status_bar(f"Commits already pushed; undo cancelled for {name}")
                return
            try:
                self._log_action(f"[{name}] git reset --soft HEAD~1")
                card.repo.git.reset("--soft", "HEAD~1")
                card.refresh_status()
                self._update_status_bar(f"Undid commit {short_sha} in {name}")
                self._log_action(f"[{name}] undo commit OK")
            except GitCommandError as e:
                self._log_action(f"[{name}] ERROR undo commit: {e}")
                self._update_status_bar(f"Undo commit failed: {e}")

        self.push_screen(
            ConfirmModal(
                f"Undo last commit in {name}?",
                details=f"{short_sha} {last_msg}",
                confirm_label="Undo",
                confirm_variant="warning",
            ),
            on_confirm,
        )

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

    @work(thread=True, exclusive=True, group="auto-refresh")
    def _auto_refresh(self) -> None:
        """Silently fetch and refresh all repo statuses on a timer."""
        cards = self.call_from_thread(self._get_cards)
        max_workers = min(8, len(cards)) if cards else 1

        def _fetch_one(card: RepoCard) -> RepoCard:
            try:
                card.repo.git.fetch("--all", "--prune")
            except GitCommandError:
                pass
            return card

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(_fetch_one, c): c for c in cards}
            for future in as_completed(futures):
                card = future.result()
                self._refresh_card_from_thread(card)

    @work(thread=True, exclusive=True, group="manual-refresh")
    def action_refresh_all(self) -> None:
        self._update_status_bar_from_thread("Refreshing all...")
        cards = self.call_from_thread(self._get_cards)
        total = len(cards)
        max_workers = min(8, total) if total else 1

        def _refresh_one(card: RepoCard) -> RepoCard:
            try:
                card.repo.git.fetch("--all", "--prune")
            except GitCommandError:
                pass
            return card

        done = 0
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(_refresh_one, c): c for c in cards}
            for future in as_completed(futures):
                card = future.result()
                self._refresh_card_from_thread(card)
                done += 1
                self._update_status_bar_from_thread(f"Refreshing... ({done}/{total})")
        self._update_status_bar_from_thread("Refreshed")

    def _bulk_git_op(self, verb: str, git_fn, show_summary: bool = True) -> None:
        """Run a git operation across all repos in parallel from a worker thread."""
        cards = self.call_from_thread(self._get_cards)
        total = len(cards)
        successes = 0
        failures: list[str] = []
        self._update_status_bar_from_thread(f"{verb.title()}ing all repos...")
        self._log_action_from_thread(f"{verb} all started")
        max_workers = min(8, total) if total else 1

        def _run_one(card: RepoCard) -> tuple[RepoCard, Exception | None]:
            try:
                git_fn(card)
                return card, None
            except GitCommandError as e:
                return card, e

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(_run_one, c): c for c in cards}
            done_count = 0
            for future in as_completed(futures):
                card, err = future.result()
                done_count += 1
                if err is None:
                    self._refresh_card_from_thread(card)
                    successes += 1
                    self._log_action_from_thread(f"[{card.repo_path.name}] {verb} OK")
                else:
                    failures.append(f"- `{card.repo_path.name}`: `{err}`")
                    self._log_action_from_thread(f"[{card.repo_path.name}] ERROR {verb}: {err}")
                self._update_status_bar_from_thread(f"{verb.title()}ing... ({done_count}/{total})")

        self._log_action_from_thread(f"{verb} all complete")
        if show_summary:
            self._update_status_bar_from_thread(f"{verb.title()} complete: {successes}/{total} repos")
            if failures:
                summary = "\n".join([
                    f"{verb.title()}ed `{successes}` of `{total}` repos.",
                    "",
                    "Failures:",
                    *failures,
                ])
                self.call_from_thread(self._show_message, f"{verb.title()} All Summary", summary)
        else:
            self._update_status_bar_from_thread("Ready")

    @work(thread=True)
    def _startup_fetch(self) -> None:
        self._bulk_git_op("fetch", lambda c: c.repo.git.fetch("--all", "--prune"), show_summary=False)

    @work(thread=True)
    def action_fetch_all(self) -> None:
        self._bulk_git_op("fetch", lambda c: c.repo.git.fetch("--all", "--prune"))

    @work(thread=True)
    def action_pull_all(self) -> None:
        self._bulk_git_op("pull", lambda c: c.repo.git.pull())

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
                    "- `G`: edit groups (add, remove, reorder, set paths)",
                    "- `t`: toggle inline terminal (runs in focused repo dir)",
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
                    "- `u`: undo last commit (before push)",
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

    def action_edit_groups(self) -> None:
        if not self.config:
            self._update_status_bar("No config loaded — run with a config file")
            return
        self.push_screen(GroupEditorModal(self.config), self._on_groups_edited)

    async def _on_groups_edited(self, result) -> None:
        if result is None:
            return
        self.config = result
        # Stay on current group if it still exists, else fall back to first
        group = self.config.get_group(self.group_name) if self.group_name else None
        if not group and self.config.groups:
            group = self.config.groups[0]
        if not group:
            self._update_status_bar("No groups defined")
            return
        await self._load_group(group)
        self._update_status_bar(f"Groups saved \u2014 showing: {group.name}")

    async def _on_group_selected(self, result: str | None) -> None:
        if result is None or result.startswith("__create__"):
            return
        group = self.config.get_group(result)
        if not group:
            self._update_status_bar(f"Group '{result}' not found")
            return
        if not group.repos:
            self._update_status_bar(f"No repos in group '{result}'")
            return
        await self._load_group(group)
        self._update_status_bar(f"Switched to group: {group.name}")

    async def _load_group(self, group) -> None:
        """Remove existing repo cards and mount cards for the given group."""
        scroll = self.query_one("#main-scroll", VerticalScroll)
        await scroll.remove_children(RepoCard)
        self.group_name = group.name
        self.base_path = group.path
        self.repo_paths = group.repos
        self.title = f"GitDash \u2014 {group.name}"
        for rp in self.repo_paths:
            await scroll.mount(RepoCard(rp, classes="repo-card", id=f"card-{rp.name}"))
        cards = list(self.query(RepoCard))
        if cards:
            cards[0].focus()


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
