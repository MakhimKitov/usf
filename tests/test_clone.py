"""Platform-owned clones (issue #4): sync semantics + credential hygiene; one
tree per task out of one cache per repository (H3)."""

import shutil
import subprocess
import threading

import pytest
from control_plane.clone import CloneError, _git, cache_dir, pinned_origin, prepare_clone, prune_trees


def _origin(tmp_path):
    """A file:// origin repo with one commit on main."""
    origin = tmp_path / "origin"
    origin.mkdir(parents=True)
    run = lambda *args: subprocess.run(  # noqa: E731
        ["git", *args], cwd=origin, check=True, capture_output=True
    )
    run("init", "-q", "-b", "main")
    run("config", "user.name", "Origin Owner")
    run("config", "user.email", "owner@example.com")
    (origin / "README.md").write_text("v1\n", encoding="utf-8")
    (origin / ".gitignore").write_text(".venv/\n", encoding="utf-8")
    run("add", ".")
    run("commit", "-qm", "chore: init")
    return origin


def test_first_prepare_clones_then_resyncs_burst_damage(tmp_path):
    origin = _origin(tmp_path)
    clones = tmp_path / "clones"

    clone = prepare_clone(clones, "owner/product", 9, url=origin.as_uri())
    assert (clone / "README.md").read_text() == "v1\n"
    assert clone == clones / "tasks" / "owner__product" / "9"

    # simulate what a burst leaves behind: branch checked out, bot identity
    # in local config, dirty + untracked files
    run = lambda *args: subprocess.run(  # noqa: E731
        ["git", *args], cwd=clone, check=True, capture_output=True
    )
    run("config", "user.name", "bot[bot]")
    run("checkout", "-qb", "dev/9-something")
    (clone / "README.md").write_text("mutated", encoding="utf-8")
    (clone / "stray.txt").write_text("junk", encoding="utf-8")

    # origin moves on; the next prepare must match it exactly
    (origin / "README.md").write_text("v2\n", encoding="utf-8")
    subprocess.run(["git", "commit", "-aqm", "feat: v2"], cwd=origin, check=True, capture_output=True)

    clone2 = prepare_clone(clones, "owner/product", 9, url=origin.as_uri())
    assert clone2 == clone
    assert (clone / "README.md").read_text() == "v2\n"
    assert not (clone / "stray.txt").exists()
    branch = subprocess.run(
        ["git", "branch", "--show-current"], cwd=clone, capture_output=True, text=True
    ).stdout.strip()
    assert branch == "main"


def test_git_errors_never_echo_the_auth_header(tmp_path):
    with pytest.raises(CloneError) as err:
        _git(
            [
                "-c",
                "http.https://github.com/.extraheader=Authorization: Basic SECRET-B64",
                "clone",
                "file:///nonexistent-origin",
                str(tmp_path / "x"),
            ]
        )
    assert "SECRET-B64" not in str(err.value)
    assert "git clone failed" in str(err.value)


def test_prepare_clone_ref_checks_out_pr_head_detached(tmp_path):
    """Assess bursts review the PR head, not origin's default branch."""
    origin = _origin(tmp_path)
    run = lambda *args: subprocess.run(  # noqa: E731
        ["git", *args], cwd=origin, check=True, capture_output=True, text=True
    )
    run("checkout", "-qb", "dev/5-feature")
    (origin / "feature.txt").write_text("new\n", encoding="utf-8")
    run("add", ".")
    run("commit", "-qm", "feat: the change under review")
    head_sha = run("rev-parse", "HEAD").stdout.strip()
    run("checkout", "-q", "main")

    clones = tmp_path / "clones"
    clone = prepare_clone(clones, "owner/product", 5, url=origin.as_uri(), ref=head_sha)
    assert (clone / "feature.txt").read_text() == "new\n"
    at = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=clone, capture_output=True, text=True
    ).stdout.strip()
    assert at == head_sha

    # and the next default sync returns it to main
    clone2 = prepare_clone(clones, "owner/product", 5, url=origin.as_uri())
    assert not (clone2 / "feature.txt").exists()


def _git_out(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def test_two_products_and_two_concurrent_bursts_share_no_working_tree(tmp_path):
    """The done-when (#105): two tasks of one product and one of another, all
    synced at once — as two replicas over one data dir would — get three trees,
    and the dirt one burst leaves is never in another's tree, nor in its own
    next look. The ignored ``.venv`` is the one thing that stays: warm for its
    own task, absent from every other."""
    product = _origin(tmp_path / "a")
    other = _origin(tmp_path / "b")
    clones = tmp_path / "clones"
    jobs = [("owner/product", 1, product), ("owner/product", 2, product), ("owner/other", 1, other)]
    trees, errors = {}, []
    start = threading.Barrier(len(jobs))

    def sync(slug, number, origin):
        start.wait()
        try:
            trees[(slug, number)] = prepare_clone(clones, slug, number, url=origin.as_uri())
        except CloneError as exc:  # pragma: no cover — the failure this test exists to catch
            errors.append(exc)

    threads = [threading.Thread(target=sync, args=job) for job in jobs]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert len(set(trees.values())) == 3

    one, two = trees[("owner/product", 1)], trees[("owner/product", 2)]
    # burst 1 dirties its tree every way a burst can
    _git_out(one, "checkout", "-qb", "dev/1-x")
    (one / "README.md").write_text("mutated\n", encoding="utf-8")
    (one / "stray.txt").write_text("junk\n", encoding="utf-8")
    (one / ".venv").mkdir()
    (one / ".venv" / "warm").write_text("deps\n", encoding="utf-8")

    # the other task's next look sees none of it
    assert prepare_clone(clones, "owner/product", 2, url=product.as_uri()) == two
    assert (two / "README.md").read_text() == "v1\n"
    assert not (two / "stray.txt").exists() and not (two / ".venv").exists()
    assert _git_out(two, "branch", "--show-current") == "main"

    # and its own next look is clean again, the .venv kept warm
    prepare_clone(clones, "owner/product", 1, url=product.as_uri())
    assert (one / "README.md").read_text() == "v1\n"
    assert not (one / "stray.txt").exists()
    assert (one / ".venv" / "warm").read_text() == "deps\n"
    assert _git_out(one, "branch", "--show-current") == "main"


def test_a_tree_is_a_whole_repository_wherever_it_is_mounted(tmp_path):
    """The box mounts the tree at a path the host never sees: a tree that
    borrowed objects from the cache by path (alternates) or linked back to it
    (a worktree's ``gitdir:``) would be a broken repository in there. And its
    origin is the product's, never the cache — a burst pushes there."""
    origin = _origin(tmp_path)
    clones = tmp_path / "clones"
    tree = prepare_clone(clones, "owner/product", 3, url=origin.as_uri())
    assert (tree / ".git").is_dir()
    assert not (tree / ".git" / "objects" / "info" / "alternates").exists()
    assert pinned_origin(tree) == (origin.as_uri(), "main")

    elsewhere = tmp_path / "box" / "workspace"
    shutil.copytree(tree, elsewhere, symlinks=True)
    shutil.rmtree(clones / "cache")  # nothing outside the tree is reachable from the box
    assert _git_out(elsewhere, "log", "--format=%s") == "chore: init"
    assert _git_out(elsewhere, "status", "--porcelain") == ""


def test_the_cache_seeds_trees_and_is_never_a_working_tree(tmp_path):
    origin = _origin(tmp_path)
    clones = tmp_path / "clones"
    prepare_clone(clones, "owner/product", 1, url=origin.as_uri())
    cache = cache_dir(clones, "owner/product")
    assert _git_out(cache, "rev-parse", "--is-bare-repository") == "true"

    # origin moves on; a new task's tree starts at the new head, not the cache's old one
    (origin / "README.md").write_text("v2\n", encoding="utf-8")
    subprocess.run(["git", "commit", "-aqm", "feat: v2"], cwd=origin, check=True, capture_output=True)
    later = prepare_clone(clones, "owner/product", 2, url=origin.as_uri())
    assert (later / "README.md").read_text() == "v2\n"


def test_a_tree_a_crash_cut_short_is_made_again(tmp_path):
    origin = _origin(tmp_path)
    clones = tmp_path / "clones"
    half = clones / "tasks" / "owner__product" / "4"
    half.mkdir(parents=True)
    (half / "partial").write_text("", encoding="utf-8")  # no .git: the clone never finished
    tree = prepare_clone(clones, "owner/product", 4, url=origin.as_uri())
    assert tree == half and (tree / "README.md").read_text() == "v1\n"
    assert not (tree / "partial").exists()


def test_prune_sweeps_the_trees_of_finished_tasks_only(tmp_path):
    origin = _origin(tmp_path)
    clones = tmp_path / "clones"
    for number in (1, 2, 3):
        prepare_clone(clones, "owner/product", number, url=origin.as_uri())
    other = prepare_clone(clones, "owner/other", 2, url=origin.as_uri())

    swept = prune_trees(clones, "owner/product", lambda number: number != 2)
    assert swept == [1, 3]
    remaining = sorted(p.name for p in (clones / "tasks" / "owner__product").iterdir())
    assert remaining == ["2"]
    assert other.is_dir()  # another product's trees are its own sweep's business
    assert prune_trees(clones, "owner/nobody", lambda number: True) == []


def test_every_sync_sweeps_what_the_ledger_says_is_done(tmp_path, monkeypatch):
    """The wiring (cli): the scheduler's sync callables sweep before they sync,
    and "finished" is the ledger's word — a done task, or one it never heard of."""
    from control_plane import cli
    from control_plane.bursts import PlatformStores
    from control_plane.models import TaskRecord, TaskStatus, task_id_for

    origin = _origin(tmp_path)
    clones = tmp_path / "clones"
    for number in (1, 2, 3):
        prepare_clone(clones, "owner/product", number, url=origin.as_uri())
    stores = PlatformStores.open(tmp_path / "data")
    for number, status in ((1, TaskStatus.DONE), (2, TaskStatus.WORKING)):
        stores.ledger.upsert_task(TaskRecord(
            task_id=task_id_for("owner/product", number), repository="owner/product",
            issue_number=number, status=status,
        ))
    real = cli.prepare_clone
    monkeypatch.setattr(
        cli, "prepare_clone",
        lambda root, slug, number, token, ref=None: real(root, slug, number, url=origin.as_uri(), ref=ref),
    )
    sync_clone, sync_clone_ref = cli._task_trees(stores, clones, "owner/product", lambda: None)

    task = stores.ledger.get_task(task_id_for("owner/product", 2))
    assert sync_clone(task) == clones / "tasks" / "owner__product" / "2"
    assert sorted(p.name for p in (clones / "tasks" / "owner__product").iterdir()) == ["2"]
    head = _git_out(origin, "rev-parse", "HEAD")
    assert _git_out(sync_clone_ref(task, head), "rev-parse", "HEAD") == head


def test_a_sync_inside_a_turn_is_a_step_and_says_whether_it_was_cold(tmp_path, spans):
    """A task's first burst syncs cold and every later one warm (H3) — the
    difference is most of the time before a burst starts, so the span says it."""
    from control_plane.telemetry import turn_span

    origin = _origin(tmp_path)
    clones = tmp_path / "clones"
    with turn_span("owner/product#9", "work"):
        prepare_clone(clones, "owner/product", 9, url=origin.as_uri())
        prepare_clone(clones, "owner/product", 9, url=origin.as_uri())
    syncs = [span for span in spans.get_finished_spans() if span.name == "clone.sync"]
    assert [span.attributes["clone.fresh"] for span in syncs] == [True, False]
    assert syncs[0].attributes["task.repository"] == "owner/product"

    # and outside a turn — the CLI's own syncs — nothing is traced
    prepare_clone(clones, "owner/product", 9, url=origin.as_uri())
    assert len([span for span in spans.get_finished_spans() if span.name == "clone.sync"]) == 2
