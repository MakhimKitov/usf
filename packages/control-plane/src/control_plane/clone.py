"""Platform-owned product clones (issue #4), one working tree per task (H3).

Bursts must never mount the operator's working copy: a burst mutates its
checkout — branch left checked out, ``--local`` git config rewritten to the
bot identity, bytecode churn — and all of it leaked into the operator's
environment. The control plane owns the clones under ``<data>/clones/`` and
re-syncs a burst's tree to origin's default branch before every burst; whatever
a burst leaves behind is platform state, not the operator's.

Per runner, per repository, one **cache** (``cache/<owner>__<name>.git``, bare)
holds the objects; per task, one **tree** (``tasks/<owner>__<name>/<issue>``)
is what a burst mounts. Two replicas on one host share the data dir, so two
bursts of one product may run here at once — never two of one *task*, whose
bursts the ledger serializes. So a tree is never shared between bursts that
can overlap, and it stays warm (its ignored ``.venv`` survives the sync) across
the bursts of its own task — work, rounds, assess, test. The first burst of a
task pays a cold ``uv sync``; that is the price of isolation (#105).

A tree is a plain local clone of the cache — objects hardlinked, no alternates,
no worktree link. The box mounts it at a path the host never sees, so a tree
that pointed outside itself (``git worktree``'s ``gitdir:``, ``--shared``'s
alternates) would be a broken repository in there. Trees of finished tasks are
swept by `prune_trees`.

Always HTTPS (the container has no ssh, by design). For private repos the
token travels as a per-invocation header — it is never written into the
clone's config, which gets mounted into the box.
"""

from __future__ import annotations

import base64
import fcntl
import shutil
import subprocess
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from control_plane.telemetry import step_in_trace


class CloneError(RuntimeError):
    """The platform clone could not be created or synced."""


def _auth_config(token: str | None) -> list[str]:
    if not token:
        return []
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return ["-c", f"http.https://github.com/.extraheader=Authorization: Basic {basic}"]


def _git(args: list[str], cwd: Path | None = None, capture: bool = False) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        verb = next((a for a in args if not a.startswith("-") and "extraheader" not in a), "?")
        # never echo args — the auth header may be among them
        raise CloneError(f"git {verb} failed: {result.stderr.strip()[:300]}")
    return result.stdout.strip() if capture else ""


def _key(slug: str) -> str:
    return slug.replace("/", "__")


def cache_dir(clones_root: Path, slug: str) -> Path:
    return clones_root / "cache" / f"{_key(slug)}.git"


def tree_dir(clones_root: Path, slug: str, issue_number: int) -> Path:
    return clones_root / "tasks" / _key(slug) / str(issue_number)


@contextmanager
def _locked(clones_root: Path, slug: str) -> Iterator[None]:
    """The cache's critical section, across the replicas sharing this data dir:
    a fetch and a clone-from-cache must not interleave. Held for one git
    command's span, never for a burst's — and the kernel lets go if the holder
    dies, so a crashed replica cannot wedge its peers."""
    lock = clones_root / "cache" / f"{_key(slug)}.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def prepare_cache(clones_root: Path, slug: str, token: str | None = None, url: str | None = None) -> Path:
    """Ensure this runner's cache of ``slug`` exists and holds origin's
    branches. The cache is never mounted; it only seeds trees."""
    cache = cache_dir(clones_root, slug)
    origin = url or f"https://github.com/{slug}.git"
    auth = _auth_config(token) if url is None else []
    with _locked(clones_root, slug):
        if not (cache / "HEAD").is_file():
            shutil.rmtree(cache, ignore_errors=True)  # a clone a crash cut short
            _git([*auth, "clone", "--quiet", "--bare", origin, str(cache)])
        _git([*auth, "fetch", "--quiet", "--prune", "origin", "+refs/heads/*:refs/heads/*"], cwd=cache)
    return cache


def prepare_clone(
    clones_root: Path,
    slug: str,
    issue_number: int,
    token: str | None = None,
    url: str | None = None,
    ref: str | None = None,
) -> Path:
    """Ensure the tree of task ``issue_number`` exists and exactly matches
    origin's default branch — or ``ref`` (a sha: reviewer bursts check out the
    PR head, detached). Returns its path. ``url`` overrides the origin (tests
    use file:// origins)."""
    with step_in_trace("clone.sync", **{"task.repository": slug, "clone.ref": ref or "default"}) as span:
        clone = tree_dir(clones_root, slug, issue_number)
        origin = url or f"https://github.com/{slug}.git"
        auth = _auth_config(token) if url is None else []
        fresh = not (clone / ".git").is_dir()
        span.set_attribute("clone.fresh", fresh)  # a task's first burst syncs cold (H3)
        if fresh:
            cache = prepare_cache(clones_root, slug, token, url)
            shutil.rmtree(clone, ignore_errors=True)  # a tree a crash cut short
            clone.parent.mkdir(parents=True, exist_ok=True)
            with _locked(clones_root, slug):
                _git(["clone", "--quiet", "--no-checkout", str(cache), str(clone)])  # local: hardlinks
            _git(["remote", "set-url", "origin", origin], cwd=clone)
        _git([*auth, "fetch", "--quiet", "--prune", "origin"], cwd=clone)
        _git([*auth, "remote", "set-head", "origin", "--auto"], cwd=clone)
        _git(["reset", "--hard", "--quiet"], cwd=clone)  # clean tree so checkout can't conflict
        if ref:
            _git(["checkout", "--quiet", "--detach", ref], cwd=clone)
        else:
            default_ref = _git(["rev-parse", "--abbrev-ref", "origin/HEAD"], cwd=clone, capture=True)
            branch = default_ref.split("/", 1)[1]
            _git(["checkout", "--quiet", "-B", branch, default_ref], cwd=clone)
        _git(["clean", "-fdq"], cwd=clone)  # ignored files survive (warm .venv)
        return clone


def prune_trees(clones_root: Path, slug: str, finished: Callable[[int], bool]) -> list[int]:
    """Remove the trees of tasks ``finished`` says are over — the sweep that
    keeps one tree per *live* task. Asked of the ledger, so a tree left on a
    host whose replica never saw the task end goes on any later sync there.
    Returns the issue numbers swept."""
    root = clones_root / "tasks" / _key(slug)
    if not root.is_dir():
        return []
    swept = []
    for tree in sorted(root.iterdir()):
        if tree.name.isdigit() and finished(int(tree.name)):
            shutil.rmtree(tree, ignore_errors=True)
            swept.append(int(tree.name))
    return swept


def pinned_origin(clone: Path) -> tuple[str, str]:
    """``(url, ref)`` — where this clone came from, and what it is sitting on.

    What a runner whose phases run beside the burst needs (G3): a pod inherits no
    mount, so it fetches the working tree itself, and `prepare_clone` has already
    decided what that tree is. The ref answers in the same two shapes this
    function's neighbour produces — the branch name when the clone is on a branch
    (the dev's, synced to origin's default), the commit when it is detached (a
    reviewer's, at the PR head) — so the pod's checkout is the same checkout for
    the same reason, not a second policy.

    The origin is the plain URL: the token never enters this clone's config (it
    travels per invocation), so what comes back here is safe to hand on.
    """
    url = _git(["remote", "get-url", "origin"], cwd=clone, capture=True)
    branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=clone, capture=True)
    if branch and branch != "HEAD":
        return url, branch
    return url, _git(["rev-parse", "HEAD"], cwd=clone, capture=True)
