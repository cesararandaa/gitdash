"""Regression tests for ListView-based pickers.

Textual >= 2 leaves a freshly (re)populated ``ListView`` with ``index = None``,
so nothing is highlighted. The pickers in :mod:`gitdash.app` read
``highlighted_child`` (or react to the ``Highlighted`` message) to decide what
to act on, so without an explicit default highlight the first Switch / Pop /
Open was a silent no-op and diff previews stayed blank — the "I can't change
branch" bug. These tests pin that behavior down.
"""

import asyncio
from pathlib import Path

import git
import pytest

from gitdash.app import (
    BranchModal,
    ConfirmModal,
    FileDiffModal,
    GitDash,
    LogModal,
    RepoCard,
    StashModal,
    WorktreeActionModal,
    worktree_conflict_path,
)
from textual.widgets import Button, Input, Label, ListView, RichLog


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A throwaway repo with a few branches and one commit."""
    rp = tmp_path / "repo"
    rp.mkdir()
    r = git.Repo.init(rp, initial_branch="main")
    r.config_writer().set_value("user", "email", "t@t.com").release()
    r.config_writer().set_value("user", "name", "t").release()
    (rp / "a.txt").write_text("hello\n")
    r.index.add(["a.txt"])
    r.index.commit("init")
    r.create_head("feature-x")
    r.create_head("develop")
    return rp


def _make_app(repo: Path) -> GitDash:
    return GitDash(base_path=repo.parent, repo_paths=[repo], fetch_on_startup=False)


async def _open_branch_modal(app, pilot) -> BranchModal:
    app.query(RepoCard).first().focus()
    await pilot.pause()
    await pilot.press("b")
    # _do_branch runs in a worker thread before pushing the modal.
    for _ in range(40):
        await asyncio.sleep(0.05)
        await pilot.pause()
        if isinstance(app.screen, BranchModal):
            break
    assert isinstance(app.screen, BranchModal), type(app.screen).__name__
    return app.screen


async def test_branch_modal_highlights_first_row_on_open(repo):
    app = _make_app(repo)
    async with app.run_test(size=(120, 40)) as pilot:
        screen = await _open_branch_modal(app, pilot)
        lv = screen.query_one("#branch-list", ListView)
        assert lv.highlighted_child is not None
        assert screen._selected_branch() is not None


async def test_switch_with_filter_only_changes_branch(repo):
    """Filter to a branch and click Switch without arrowing into the list."""
    app = _make_app(repo)
    async with app.run_test(size=(120, 40)) as pilot:
        screen = await _open_branch_modal(app, pilot)
        screen.query_one("#branch-filter", Input).value = "develop"
        screen._populate_list("develop")
        await asyncio.sleep(0.2)
        await pilot.pause()
        screen.on_button_pressed(Button.Pressed(screen.query_one("#btn-switch", Button)))
        await asyncio.sleep(0.4)
        await pilot.pause()
    assert git.Repo(repo).active_branch.name == "develop"


async def test_enter_switches_to_top_match(repo):
    app = _make_app(repo)
    async with app.run_test(size=(120, 40)) as pilot:
        screen = await _open_branch_modal(app, pilot)
        inp = screen.query_one("#branch-filter", Input)
        inp.value = "feature"
        screen._populate_list("feature")
        await asyncio.sleep(0.2)
        await pilot.pause()
        screen.on_input_submitted(Input.Submitted(inp, "feature"))
        await asyncio.sleep(0.4)
        await pilot.pause()
    assert git.Repo(repo).active_branch.name == "feature-x"


async def test_enter_with_no_match_creates_branch(repo):
    app = _make_app(repo)
    async with app.run_test(size=(120, 40)) as pilot:
        screen = await _open_branch_modal(app, pilot)
        inp = screen.query_one("#branch-filter", Input)
        inp.value = "totally-new"
        screen._populate_list("totally-new")
        await asyncio.sleep(0.2)
        await pilot.pause()
        screen.on_input_submitted(Input.Submitted(inp, "totally-new"))
        await asyncio.sleep(0.4)
        await pilot.pause()
    assert git.Repo(repo).active_branch.name == "totally-new"


async def test_stash_modal_highlights_first_entry(repo):
    r = git.Repo(repo)
    (repo / "a.txt").write_text("change\n")
    r.git.stash("push", "-m", "s1")
    app = _make_app(repo)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.push_screen(StashModal(r.git.stash("list").splitlines(), has_changes=False))
        await asyncio.sleep(0.3)
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, StashModal)
        # Without a default highlight this returned None and Pop/Apply/Drop no-op'd.
        assert screen._selected_index() == 0


async def test_filediff_shows_first_diff_on_open(repo):
    r = git.Repo(repo)
    (repo / "a.txt").write_text("modified\n")
    app = _make_app(repo)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.push_screen(FileDiffModal("Changes", [("a.txt", "unstaged")], r))
        await asyncio.sleep(0.3)
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, FileDiffModal)
        assert screen._selected_file == ("a.txt", "unstaged")
        assert len(screen.query_one("#diff-log", RichLog).lines) > 0


async def test_log_modal_shows_first_commit_diff_on_open(repo):
    r = git.Repo(repo)
    app = _make_app(repo)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.push_screen(LogModal("Log", r))
        await asyncio.sleep(0.4)
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, LogModal)
        assert screen.query_one("#log-list", ListView).highlighted_child is not None
        assert len(screen.query_one("#diff-log", RichLog).lines) > 0


# --- worktree conflict handling ------------------------------------------------

def test_worktree_conflict_path_parses_error():
    class FakeErr(Exception):
        stderr = (
            "fatal: 'develop' is already used by worktree at "
            "'/home/x/repo/.claude/worktrees/radv-903'"
        )
    assert (
        worktree_conflict_path(FakeErr())
        == "/home/x/repo/.claude/worktrees/radv-903"
    )
    assert worktree_conflict_path(Exception("some unrelated error")) is None


@pytest.fixture
def repo_with_worktree(repo: Path):
    """Main repo with `develop` checked out in a separate worktree, so the main
    checkout cannot switch to it without --ignore-other-worktrees."""
    r = git.Repo(repo)
    wt = repo / ".wt" / "dev-wt"
    r.git.worktree("add", str(wt), "develop")
    return repo


async def _open_branch_modal_and_pick(app, pilot, branch: str):
    app.query(RepoCard).first().focus()
    await pilot.pause()
    await pilot.press("b")
    for _ in range(40):
        await asyncio.sleep(0.05)
        await pilot.pause()
        if isinstance(app.screen, BranchModal):
            break
    screen = app.screen
    assert isinstance(screen, BranchModal)
    screen.query_one("#branch-filter", Input).value = branch
    screen._populate_list(branch)
    await asyncio.sleep(0.2)
    await pilot.pause()
    screen.on_button_pressed(Button.Pressed(screen.query_one("#btn-switch", Button)))


async def _wait_for(pred, pilot):
    for _ in range(40):
        await asyncio.sleep(0.05)
        await pilot.pause()
        if pred():
            return True
    return False


async def test_branch_picker_annotates_worktree_held_branch(repo_with_worktree):
    """The held branch shows a 🔗 marker, but selecting it still yields the bare
    branch name (decoration must not leak into the checkout)."""
    app = _make_app(repo_with_worktree)
    async with app.run_test(size=(120, 40)) as pilot:
        screen = await _open_branch_modal(app, pilot)
        labels = [
            str(item.query_one(Label).render())
            for item in screen.query_one("#branch-list", ListView).children
        ]
        assert any("🔗" in t and "develop" in t for t in labels), labels
        # The selection map returns the clean branch name despite the marker.
        screen.query_one("#branch-filter", Input).value = "develop"
        screen._populate_list("develop")
        await asyncio.sleep(0.2)
        await pilot.pause()
        assert screen._selected_branch() == "develop"


async def test_worktree_held_branch_offers_choice_not_crash(repo_with_worktree):
    repo = repo_with_worktree
    r = git.Repo(repo)
    app = _make_app(repo)
    async with app.run_test(size=(120, 40)) as pilot:
        await _open_branch_modal_and_pick(app, pilot, "develop")
        # Proactive: an open-vs-force choice appears; nothing is attempted yet.
        assert await _wait_for(lambda: isinstance(app.screen, WorktreeActionModal), pilot)
        assert r.git.rev_parse("--abbrev-ref", "HEAD").strip() == "main"


async def test_worktree_held_branch_force_switches(repo_with_worktree):
    repo = repo_with_worktree
    r = git.Repo(repo)
    app = _make_app(repo)
    async with app.run_test(size=(120, 40)) as pilot:
        await _open_branch_modal_and_pick(app, pilot, "develop")
        assert await _wait_for(lambda: isinstance(app.screen, WorktreeActionModal), pilot)
        app.screen.dismiss("force")
        await _wait_for(lambda: r.git.rev_parse("--abbrev-ref", "HEAD").strip() == "develop", pilot)
    assert r.git.rev_parse("--abbrev-ref", "HEAD").strip() == "develop"


async def test_worktree_held_branch_cancel_does_not_switch(repo_with_worktree):
    repo = repo_with_worktree
    r = git.Repo(repo)
    app = _make_app(repo)
    async with app.run_test(size=(120, 40)) as pilot:
        await _open_branch_modal_and_pick(app, pilot, "develop")
        assert await _wait_for(lambda: isinstance(app.screen, WorktreeActionModal), pilot)
        app.screen.dismiss(None)  # cancel
        await asyncio.sleep(0.3)
        await pilot.pause()
    assert r.git.rev_parse("--abbrev-ref", "HEAD").strip() == "main"


# --- discovery hygiene ---------------------------------------------------------

def test_is_linked_worktree(repo_with_worktree, tmp_path):
    from gitdash.status import find_repos, is_linked_worktree

    main = repo_with_worktree
    wt = main / ".wt" / "dev-wt"
    assert is_linked_worktree(wt) is True
    assert is_linked_worktree(main) is False

    # A linked worktree placed directly under a scanned base must not be listed
    # as an independent repo.
    base = tmp_path / "scan"
    base.mkdir()
    sibling_wt = base / "sibling-wt"
    git.Repo(main).git.worktree("add", str(sibling_wt), "-b", "wt-branch")
    found = find_repos(base)
    assert sibling_wt not in found
