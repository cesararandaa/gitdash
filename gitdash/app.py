"""GitDash - Multi-repo git TUI dashboard."""

from __future__ import annotations

import sys
from pathlib import Path

from git import Repo, GitCommandError, InvalidGitRepositoryError
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Footer,
    Header,
    Input,
    Label,
    ListItem,
    ListView,
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

    return {
        "branch": branch,
        "tracking": tracking,
        "ahead": ahead,
        "behind": behind,
        "staged": staged,
        "unstaged": unstaged,
        "untracked": untracked,
        "stashes": stashes,
    }


# ---------------------------------------------------------------------------
# Modals
# ---------------------------------------------------------------------------

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


class BranchModal(ModalScreen[str | None]):
    """Modal to pick or create a branch."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, branches: list[str], current: str) -> None:
        super().__init__()
        self.branches = branches
        self.current = current

    def compose(self) -> ComposeResult:
        with Vertical(id="branch-dialog"):
            yield Label("Switch Branch", id="branch-title")
            yield Input(placeholder="Filter or new branch name...", id="branch-filter")
            yield ListView(
                *[
                    ListItem(Label(f"{'* ' if b == self.current else '  '}{b}"), id=f"br-{i}")
                    for i, b in enumerate(self.branches)
                ],
                id="branch-list",
            )
            with Horizontal(id="branch-buttons"):
                yield Button("Switch", variant="success", id="btn-switch")
                yield Button("Create & Switch", variant="primary", id="btn-create")
                yield Button("Cancel", variant="error", id="btn-cancel")

    def on_mount(self) -> None:
        self.query_one("#branch-filter", Input).focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        filt = event.value.lower()
        lv = self.query_one("#branch-list", ListView)
        lv.clear()
        for i, b in enumerate(self.branches):
            if filt in b.lower():
                lv.append(ListItem(Label(f"{'* ' if b == self.current else '  '}{b}"), id=f"br-{i}"))

    def _selected_branch(self) -> str | None:
        lv = self.query_one("#branch-list", ListView)
        if lv.highlighted_child is not None:
            label = lv.highlighted_child.query_one(Label)
            return label.renderable.strip().lstrip("* ").strip()
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


# ---------------------------------------------------------------------------
# Repo widget
# ---------------------------------------------------------------------------

class RepoCard(Vertical, can_focus=True):
    """A collapsible card showing one repo's status."""

    collapsed = reactive(False)

    BINDINGS = [
        Binding("b", "branch", "Branch", show=True),
        Binding("c", "commit", "Commit", show=True),
        Binding("d", "diff", "Diff", show=True),
        Binding("s", "stash", "Stash", show=True),
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
            yield Static("", id=f"sync-{self.repo_path.name}", classes="repo-sync")
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
                           "staged": [], "unstaged": [], "untracked": [], "stashes": 0}

        name = self.repo_path.name
        try:
            branch_lbl = self.query_one(f"#branch-{name}", Static)
            branch_lbl.update(f" {self.status['branch']}")
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

        # Update sync bar using inline styles (CSS classes have rendering bugs)
        ahead = self.status["ahead"]
        behind = self.status["behind"]
        try:
            sync_bar = self.query_one(f"#syncbtn-{name}", Static)
            if behind or ahead:
                if behind and ahead:
                    sync_bar.update(f"Sync Changes  ↑{ahead} ↓{behind}")
                elif behind:
                    sync_bar.update(f"Pull Changes  ↓{behind}")
                else:
                    sync_bar.update(f"Push Changes  ↑{ahead}")
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

    def action_stash(self) -> None:
        self.app._do_stash(self)

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

    .repo-sync {
        color: #f9e2af;
        width: auto;
        margin-left: 1;
    }

    .repo-body {
        padding: 0 1;
        height: auto;
    }

    Tree {
        height: auto;
        max-height: 15;
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

    #commit-title, #branch-title, #diff-title {
        text-style: bold;
        margin-bottom: 1;
        width: 100%;
        text-align: center;
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

    #branch-list {
        height: auto;
        max-height: 20;
        border: solid $primary-background;
        margin: 1 0;
    }

    #status-bar {
        dock: bottom;
        height: 1;
        padding: 0 1;
        background: $primary-background;
        color: $text-muted;
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
    ]

    def __init__(self, base_path: Path, repo_paths: list[Path] | None = None, group_name: str | None = None, fetch_on_startup: bool = False) -> None:
        super().__init__()
        self.base_path = base_path
        self.repo_paths = repo_paths or find_repos(base_path)
        self.group_name = group_name
        self.fetch_on_startup = fetch_on_startup

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with VerticalScroll(id="main-scroll"):
            for rp in self.repo_paths:
                yield RepoCard(rp, classes="repo-card", id=f"card-{rp.name}")
        yield Static("", id="status-bar")
        yield Footer()

    def on_mount(self) -> None:
        title_suffix = self.group_name or str(self.base_path)
        self.title = f"GitDash — {title_suffix}"
        self._update_status_bar("Ready  |  j/k: navigate  b: branch  c: commit  d: diff  s: stash  F: fetch all  P: pull all")
        cards = list(self.query(RepoCard))
        if cards:
            cards[0].focus()
        if self.fetch_on_startup:
            self._startup_fetch()

    def _update_status_bar(self, msg: str) -> None:
        try:
            self.query_one("#status-bar", Static).update(msg)
        except NoMatches:
            pass

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
        try:
            if behind:
                self._update_status_bar(f"Pulling {name}...")
                card.repo.git.pull()
            if ahead:
                self._update_status_bar(f"Pushing {name}...")
                card.repo.git.push()
            self.call_from_thread(card.refresh_status)
            self._update_status_bar(f"Synced {name}")
        except GitCommandError as e:
            self._update_status_bar(f"Sync failed: {e}")

    @work(thread=True)
    def _do_fetch(self, card: RepoCard) -> None:
        name = card.repo_path.name
        self._update_status_bar(f"Fetching {name}...")
        try:
            card.repo.git.fetch("--all", "--prune")
            self.call_from_thread(card.refresh_status)
            self._update_status_bar(f"Fetched {name}")
        except GitCommandError as e:
            self._update_status_bar(f"Fetch failed: {e}")

    def _do_branch(self, card: RepoCard) -> None:
        branches = [h.name for h in card.repo.heads]
        current = card.status.get("branch", "")

        def on_result(result: str | None) -> None:
            if result is None:
                return
            if result.startswith("__create__"):
                new_name = result.removeprefix("__create__")
                if new_name:
                    try:
                        card.repo.git.checkout("-b", new_name)
                        card.refresh_status()
                        self._update_status_bar(f"Created & switched to {new_name}")
                    except GitCommandError as e:
                        self._update_status_bar(f"Branch create failed: {e}")
            else:
                try:
                    card.repo.git.checkout(result)
                    card.refresh_status()
                    self._update_status_bar(f"Switched to {result}")
                except GitCommandError as e:
                    self._update_status_bar(f"Checkout failed: {e}")

        self.push_screen(BranchModal(branches, current), on_result)

    @work(thread=True)
    def _do_stash(self, card: RepoCard) -> None:
        name = card.repo_path.name
        if card.status["unstaged"] or card.status["untracked"]:
            try:
                card.repo.git.stash("push", "-u")
                self.call_from_thread(card.refresh_status)
                self._update_status_bar(f"Stashed changes in {name}")
            except GitCommandError as e:
                self._update_status_bar(f"Stash failed: {e}")
        elif card.status["stashes"]:
            try:
                card.repo.git.stash("pop")
                self.call_from_thread(card.refresh_status)
                self._update_status_bar(f"Popped stash in {name}")
            except GitCommandError as e:
                self._update_status_bar(f"Stash pop failed: {e}")
        else:
            self._update_status_bar(f"Nothing to stash in {name}")

    def _do_commit(self, card: RepoCard) -> None:
        # Stage all changes first
        if not card.status["staged"] and not card.status["unstaged"] and not card.status["untracked"]:
            self._update_status_bar(f"Nothing to commit in {card.repo_path.name}")
            return

        def on_message(msg: str | None) -> None:
            if msg is None:
                return
            try:
                card.repo.git.add("-A")
                card.repo.git.commit("-m", msg)
                card.refresh_status()
                self._update_status_bar(f"Committed to {card.repo_path.name}")
            except GitCommandError as e:
                self._update_status_bar(f"Commit failed: {e}")

        self.push_screen(CommitModal(), on_message)

    def _do_diff(self, card: RepoCard) -> None:
        try:
            diff_text = card.repo.git.diff()
            if not diff_text:
                diff_text = card.repo.git.diff("--cached")
            if not diff_text:
                diff_text = "(no changes)"
        except GitCommandError:
            diff_text = "(could not get diff)"
        self.push_screen(DiffModal(f"Diff — {card.repo_path.name}", diff_text))

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

    # -- Global actions --

    def action_refresh_all(self) -> None:
        self._update_status_bar("Refreshing all...")
        for card in self.query(RepoCard):
            card.refresh_status()
        self._update_status_bar("Refreshed")

    @work(thread=True)
    def _startup_fetch(self) -> None:
        self._update_status_bar("Fetching all repos...")
        for card in self.query(RepoCard):
            try:
                card.repo.git.fetch("--all", "--prune")
                self.call_from_thread(card.refresh_status)
            except GitCommandError:
                pass
        self._update_status_bar("Ready  |  j/k: navigate  b: branch  c: commit  d: diff  s: stash  F: fetch all  P: pull all")

    @work(thread=True)
    def action_fetch_all(self) -> None:
        self._update_status_bar("Fetching all repos...")
        for card in self.query(RepoCard):
            try:
                card.repo.git.fetch("--all", "--prune")
                self.call_from_thread(card.refresh_status)
            except GitCommandError:
                pass
        self._update_status_bar("All repos fetched")

    @work(thread=True)
    def action_pull_all(self) -> None:
        self._update_status_bar("Pulling all repos...")
        for card in self.query(RepoCard):
            try:
                card.repo.git.pull()
                self.call_from_thread(card.refresh_status)
            except GitCommandError:
                pass
        self._update_status_bar("All repos pulled")


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
    app = GitDash(group.path, group.repos, group.name, fetch_on_startup=fetch)
    app.run()


if __name__ == "__main__":
    main()
