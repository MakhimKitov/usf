"""The container entrypoint — what it binds before it does anything else."""

import json
import os

import pytest
import structlog
from dev_agent import cli
from dev_agent import env as agentenv


def test_the_correlation_ids_are_bound_before_the_command_runs(monkeypatch):
    """Every line this process writes has to carry the ids (TO-BE §12), and its
    stdout is teed to `data/logs/<burst_id>.log` — often the only record of a burst
    a reader has, and the one they reach for when the trace backend they need is not
    the one that was running. The ids come from the environment because that is the
    only place the container can learn them.
    """
    structlog.contextvars.clear_contextvars()
    seen: dict[str, str] = {}

    def capture(env):
        seen.update(structlog.contextvars.get_contextvars())
        return 0

    monkeypatch.setattr(cli, "_check", capture)
    monkeypatch.setenv(agentenv.TASK_ID, "owner/product#42")
    monkeypatch.setenv(agentenv.BURST_ID, "b9")
    monkeypatch.setenv(agentenv.TRACEPARENT, f"00-{'a' * 32}-{'b' * 16}-01")
    try:
        assert cli.main(["check"]) == 0
    finally:
        structlog.contextvars.clear_contextvars()

    assert seen == {"task_id": "owner/product#42", "burst_id": "b9", "trace_id": "a" * 32}


def test_an_untraced_burst_still_binds_what_it_knows(monkeypatch):
    """Tracing off is a normal state — a hand-run burst, an image without an
    endpoint. `trace_id` is then absent rather than empty: a key whose value says
    nothing is worse in logfmt than no key, because it reads as a lost trace."""
    structlog.contextvars.clear_contextvars()
    seen: dict[str, str] = {}

    def capture(env):
        seen.update(structlog.contextvars.get_contextvars())
        return 0

    monkeypatch.setattr(cli, "_check", capture)
    monkeypatch.setenv(agentenv.TASK_ID, "owner/product#42")
    monkeypatch.setenv(agentenv.BURST_ID, "b9")
    monkeypatch.delenv(agentenv.TRACEPARENT, raising=False)
    try:
        assert cli.main(["check"]) == 0
    finally:
        structlog.contextvars.clear_contextvars()

    assert seen == {"task_id": "owner/product#42", "burst_id": "b9"}


@pytest.mark.parametrize("restored", [True, False])
def test_check_reports_whether_the_resumed_session_was_restored(tmp_path, monkeypatch, restored):
    """The golden replay's restore criterion without the engine (G4): told to
    resume a session, `check` looks for that session's transcript where the CLI
    would — and a box missing it is not viable."""
    session, repo = tmp_path / "session", tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    session.mkdir()
    if restored:
        (session / "projects" / "-workspace").mkdir(parents=True)
        (session / "projects" / "-workspace" / "sess-1.jsonl").write_text("{}\n", encoding="utf-8")
    env = agentenv.BurstEnv(
        task_id="t", burst_id="b", repo_dir=repo, session_dir=session, resume_session_id="sess-1"
    )
    monkeypatch.setattr(cli, "BUNDLED_CLI", _executable(tmp_path / "claude"))
    code = cli._check(env)
    findings = json.loads(env.result_path.read_text())["check"]
    assert findings["resume_transcript"] is restored
    assert code == (0 if restored else 2)


def _executable(path):
    path.write_text("#!/bin/sh\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def _viable_box(tmp_path):
    session, repo = tmp_path / "session", tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    session.mkdir()
    return agentenv.BurstEnv(task_id="t", burst_id="b", repo_dir=repo, session_dir=session)


def test_check_reports_the_engine_the_image_carries(tmp_path, monkeypatch, capsys):
    """#120: a burst drives the CLI the SDK bundles, not one on PATH — so that is
    the binary `check` looks for, and the two versions it reports are the lock's."""
    import claude_agent_sdk
    from claude_agent_sdk._cli_version import __cli_version__

    env = _viable_box(tmp_path)
    monkeypatch.setattr(cli, "BUNDLED_CLI", _executable(tmp_path / "claude"))
    monkeypatch.setenv("PATH", "")  # nothing on PATH is asked
    assert cli._check(env) == 0
    findings = json.loads(env.result_path.read_text())["check"]
    assert findings["claude_cli"] is True
    assert (findings["sdk_version"], findings["cli_version"]) == (claude_agent_sdk.__version__, __cli_version__)
    assert f'"cli_version": "{__cli_version__}"' in capsys.readouterr().out


def test_a_box_without_the_bundled_cli_is_not_viable(tmp_path, monkeypatch):
    env = _viable_box(tmp_path)
    monkeypatch.setattr(cli, "BUNDLED_CLI", tmp_path / "absent")
    assert cli._check(env) == 2
    assert json.loads(env.result_path.read_text())["check"]["claude_cli"] is False


def test_the_sdk_this_suite_runs_bundles_its_cli():
    """The lock's SDK is a platform wheel with the CLI inside it — the premise of
    dropping the image's second install. An sdist would carry no binary."""
    assert os.access(cli.BUNDLED_CLI, os.X_OK)
