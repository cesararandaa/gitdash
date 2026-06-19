"""Repo status collection — shared between the TUI and the tray companion.

Kept free of Textual imports so non-TUI entry points (e.g. `gitdash.tray`)
can use it without pulling in the full TUI stack.
"""

from __future__ import annotations

from pathlib import Path

from git import GitCommandError, InvalidGitRepositoryError, Repo


def is_linked_worktree(path: Path) -> bool:
    """True if *path* is a linked git worktree rather than a primary checkout.

    A linked worktree's ``.git`` is a *file* containing ``gitdir: <…>/worktrees/<id>``
    (a primary checkout has a ``.git`` directory). Listing such a path as an
    independent repo is misleading — its branch is owned by the parent repo, so
    operations like branch switching would conflict."""
    gitfile = path / ".git"
    if not gitfile.is_file():
        return False
    try:
        head = gitfile.read_text(errors="ignore").strip()
    except OSError:
        return False
    return head.startswith("gitdir:") and "/worktrees/" in head.replace("\\", "/")


def find_repos(base: Path) -> list[Path]:
    """Find all git repos (one level deep) under *base*, skipping linked worktrees."""
    repos = []
    for entry in sorted(base.iterdir()):
        if entry.is_dir() and (entry / ".git").exists() and not is_linked_worktree(entry):
            repos.append(entry)
    return repos


def worktree_branch_map(repo: Repo) -> dict[str, str]:
    """Map ``local branch name -> worktree path`` for branches checked out in
    *other* worktrees of this repo (the repo's own working tree is excluded).

    Lets the UI warn that a branch can't be plainly switched to before trying.
    Returns an empty dict on any error (git failure, bare repo, stale cwd)."""
    try:
        raw = repo.git.worktree("list", "--porcelain")
        # working_dir is None for a bare repo; resolve() can raise on a stale cwd.
        own = str(Path(repo.working_dir).resolve()) if repo.working_dir else None
    except (GitCommandError, OSError, TypeError):
        return {}
    result: dict[str, str] = {}
    for block in raw.split("\n\n"):
        path = branch = None
        for line in block.splitlines():
            if line.startswith("worktree "):
                path = line[len("worktree "):]
            elif line.startswith("branch "):
                branch = line[len("branch "):].replace("refs/heads/", "", 1)
        if path and branch:
            try:
                same = str(Path(path).resolve()) == own
            except OSError:
                same = False
            if not same:
                result[branch] = path
    return result


def short_status(repo: Repo) -> dict:
    """Return a dict summarising the repo status.

    Single ``git status --porcelain=v2 --branch`` call plus one
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
            for p in line.split():
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
            untracked.append(line[2:])
        elif line.startswith("u "):
            conflicted = True
            u_parts = line.split(" ", 10)
            if len(u_parts) == 11:
                unstaged.append(u_parts[10])
        elif line.startswith("1 ") or line.startswith("2 "):
            parts = line.split(" ", 8)
            if len(parts) >= 9:
                xy = parts[1]
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


def status_for_path(path: Path) -> dict:
    """Open a repo at *path* and return its status, plus the path/name.

    Returns a dict with an `error` key if the repo can't be opened.
    """
    base = {"path": path, "name": path.name}
    try:
        repo = Repo(path)
    except (InvalidGitRepositoryError, Exception) as e:  # noqa: BLE001
        return {**base, "error": str(e)}
    s = short_status(repo)
    return {**base, **s, "error": None}


def collect_statuses(paths: list[Path]) -> list[dict]:
    """Collect statuses for a list of repo paths, swallowing per-repo errors."""
    return [status_for_path(p) for p in paths]


def summarize(statuses: list[dict]) -> dict:
    """Aggregate counts across a list of repo status dicts."""
    dirty = sum(1 for s in statuses if s.get("dirty"))
    ahead = sum(s.get("ahead", 0) for s in statuses)
    behind = sum(s.get("behind", 0) for s in statuses)
    conflicts = sum(1 for s in statuses if s.get("conflicted"))
    errors = sum(1 for s in statuses if s.get("error"))
    return {
        "total": len(statuses),
        "dirty": dirty,
        "ahead": ahead,
        "behind": behind,
        "conflicts": conflicts,
        "errors": errors,
    }
