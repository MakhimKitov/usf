"""What the box is given, and how docker packages it — pure, no docker required.

Two layers, tested apart (E4): `burst_env` is the box's contract — the same
environment wherever a burst runs, so the credential and telemetry rules live
in one function every runner calls; `DockerRunner.build_command` is one
runner's packaging of it — the ``docker run`` line, its mounts and its seccomp
profile. What every runner does with a burst once it runs — the exit code, the
tee, the heartbeat, the fence, `alive` — is `test_runner_contract.py`, the
suite parameterized over runners; the tests here are the docker-shaped rest.
"""

import json
import subprocess
from pathlib import Path

from control_plane import runner as runner_mod
from control_plane import telemetry
from control_plane.runner import SECCOMP_PROFILE, BurstSpec, DockerRunner, burst_env
from dev_agent import env as agentenv


def _spec(**overrides) -> BurstSpec:
    defaults = dict(
        task_id="42",
        burst_id="b1",
        volume_dir=Path("/data/volumes/b1"),
        repo_mount=Path("/clones/product"),
        prompt="fix the bug",
        traceparent="00-" + "a" * 32 + "-" + "b" * 16 + "-01",
    )
    return BurstSpec(**(defaults | overrides))


# --- the box's contract: burst_env, every runner's ---


def test_the_ids_and_the_session_home_are_always_set(monkeypatch):
    monkeypatch.delenv(agentenv.ANTHROPIC_API_KEY, raising=False)
    env = burst_env(_spec())
    assert env[agentenv.TASK_ID] == "42"
    assert env[agentenv.BURST_ID] == "b1"
    assert env[agentenv.ROLE] == "dev"
    assert env[agentenv.BURST_KIND] == "work"
    assert env[agentenv.TRACEPARENT].startswith("00-")
    assert env[agentenv.CLAUDE_CONFIG_DIR] == agentenv.CONTAINER_SESSION_DIR


def test_subscription_token_preferred_and_exclusive(monkeypatch):
    """Exactly one credential enters the box; the OAuth token wins (the CLI
    would prefer an API key, silently switching billing to the API)."""
    monkeypatch.setenv(agentenv.CLAUDE_CODE_OAUTH_TOKEN, "oat-test")
    monkeypatch.setenv(agentenv.ANTHROPIC_API_KEY, "sk-test")
    env = burst_env(_spec())
    assert env[agentenv.CLAUDE_CODE_OAUTH_TOKEN] == "oat-test"
    assert agentenv.ANTHROPIC_API_KEY not in env


def test_the_picked_subscription_pays_over_this_machines_credential(monkeypatch):
    """F2: a burst stamped with a member's subscription has to actually run on
    it. The pick's token wins over whatever this machine holds — including an
    API key, which would otherwise switch billing to the API behind the pool's
    back."""
    monkeypatch.setenv(agentenv.CLAUDE_CODE_OAUTH_TOKEN, "the-machines-own")
    monkeypatch.setenv(agentenv.ANTHROPIC_API_KEY, "sk-test")
    env = burst_env(_spec(oauth_token="adas-token"))
    assert env[agentenv.CLAUDE_CODE_OAUTH_TOKEN] == "adas-token"
    assert agentenv.ANTHROPIC_API_KEY not in env

    # an empty pool passes none, and the machine's own credential is used as before
    assert burst_env(_spec())[agentenv.CLAUDE_CODE_OAUTH_TOKEN] == "the-machines-own"
    # and a `check` burst still gets no credential at all, picked or not
    assert agentenv.CLAUDE_CODE_OAUTH_TOKEN not in burst_env(_spec(command="check", oauth_token="adas-token"))


def test_api_key_is_the_fallback(monkeypatch):
    monkeypatch.delenv(agentenv.CLAUDE_CODE_OAUTH_TOKEN, raising=False)
    monkeypatch.setenv(agentenv.ANTHROPIC_API_KEY, "sk-test")
    assert burst_env(_spec())[agentenv.ANTHROPIC_API_KEY] == "sk-test"


def test_no_single_command_may_hang_the_burst(monkeypatch):
    """H2 (#104): the CLI's own per-call ceiling, set by the platform. Finding #6
    measured 18 minutes lost to one stuck command — with the budget draining and
    the heartbeat renewing a lease on work that had stopped."""
    monkeypatch.setenv(agentenv.CLAUDE_CODE_OAUTH_TOKEN, "oat-test")
    env = burst_env(_spec())
    assert env[agentenv.BASH_DEFAULT_TIMEOUT_MS] == str(runner_mod.BASH_CALL_TIMEOUT_S * 1000)
    assert env[agentenv.BASH_MAX_TIMEOUT_MS] == str(runner_mod.BASH_CALL_CEILING_S * 1000)
    # the ceiling is above the default, or the model could never ask for longer
    assert runner_mod.BASH_CALL_CEILING_S > runner_mod.BASH_CALL_TIMEOUT_S
    # and a `check` runs no tools, so it is told nothing about them
    assert agentenv.BASH_DEFAULT_TIMEOUT_MS not in burst_env(_spec(command="check"))


def test_no_credentials_in_check_bursts(monkeypatch):
    monkeypatch.setenv(agentenv.CLAUDE_CODE_OAUTH_TOKEN, "oat-test")
    monkeypatch.setenv(agentenv.ANTHROPIC_API_KEY, "sk-test")
    env = burst_env(_spec(command="check"))
    assert agentenv.CLAUDE_CODE_OAUTH_TOKEN not in env
    assert agentenv.ANTHROPIC_API_KEY not in env


def test_github_token_feeds_both_git_and_gh(monkeypatch):
    monkeypatch.delenv(agentenv.ANTHROPIC_API_KEY, raising=False)
    env = burst_env(_spec(github_token="ghs_x", git_user_name="bot[bot]", git_user_email="b@nr.gh"))
    assert env[agentenv.GITHUB_TOKEN] == "ghs_x"
    assert env[agentenv.GH_TOKEN] == "ghs_x"
    assert env[agentenv.GIT_USER_NAME] == "bot[bot]"
    assert env[agentenv.GIT_USER_EMAIL] == "b@nr.gh"


def test_model_and_rails_pass_through(monkeypatch):
    monkeypatch.delenv(agentenv.ANTHROPIC_API_KEY, raising=False)
    env = burst_env(_spec(model="claude-opus-4-8", max_budget_usd=2.5, max_turns=40))
    assert env[agentenv.MODEL] == "claude-opus-4-8"
    assert env[agentenv.MAX_BUDGET_USD] == "2.5"
    assert env[agentenv.MAX_TURNS] == "40"


def test_optional_env_only_when_set(monkeypatch):
    monkeypatch.delenv(agentenv.ANTHROPIC_API_KEY, raising=False)
    minimal = burst_env(_spec(prompt="", traceparent=None))
    assert agentenv.TASK_PROMPT not in minimal
    assert agentenv.TRACEPARENT not in minimal
    assert agentenv.RESUME_SESSION_ID not in minimal
    assert agentenv.MODEL not in minimal

    resumed = burst_env(_spec(resume_session_id="sess-9"))
    assert resumed[agentenv.RESUME_SESSION_ID] == "sess-9"
    assert agentenv.FORK_SESSION not in resumed

    # a branch (H5) forks the session it resumes; with nothing to resume there
    # is nothing to fork, and the flag is not sent at all
    forked = burst_env(_spec(resume_session_id="sess-9", fork_session=True))
    assert forked[agentenv.FORK_SESSION] == "1"
    assert agentenv.FORK_SESSION not in burst_env(_spec(fork_session=True))


def test_extra_env_rides_along(monkeypatch):
    """The declared-ref intersection a tester burst resolved at spawn (D4)."""
    monkeypatch.delenv(agentenv.ANTHROPIC_API_KEY, raising=False)
    env = burst_env(_spec(role="tester", kind="test", extra_env={"SANDBOX_URL": "http://sandbox"}))
    assert env["SANDBOX_URL"] == "http://sandbox"


def test_no_collector_means_no_telemetry_variables(monkeypatch):
    """A presence toggle, like the two stores: unconfigured is not a broken export,
    it is a box that was never told about telemetry (B2)."""
    monkeypatch.delenv(telemetry.AGENT_OTLP_ENDPOINT_ENV, raising=False)
    env = burst_env(_spec())
    assert not [key for key in env if key.startswith(("OTEL_", "CLAUDE_CODE_ENABLE", "CLAUDE_CODE_ENH"))]


def test_the_agents_own_otel_is_configured_from_one_variable(monkeypatch):
    monkeypatch.setenv(telemetry.AGENT_OTLP_ENDPOINT_ENV, "http://collector:4318")
    env = burst_env(_spec(task_id="owner/product#42", role="tester", kind="test"))

    # both switches, because traces are beta-gated behind the second one
    assert env[agentenv.CLAUDE_CODE_ENABLE_TELEMETRY] == "1"
    assert env[agentenv.CLAUDE_CODE_ENHANCED_TELEMETRY_BETA] == "1"
    assert env[agentenv.OTEL_TRACES_EXPORTER] == "otlp"
    assert env[agentenv.OTEL_EXPORTER_OTLP_ENDPOINT] == "http://collector:4318"
    assert env[agentenv.OTEL_EXPORTER_OTLP_PROTOCOL] == "http/protobuf"
    assert env[agentenv.OTEL_SERVICE_NAME] == agentenv.AGENT_SERVICE_NAME

    # the ids the burst span carries, stamped onto every signal the box emits
    assert env[agentenv.OTEL_RESOURCE_ATTRIBUTES] == (
        "task.id=owner/product#42,burst.id=b1,burst.role=tester,burst.kind=test"
    )
    # and the traceparent is still there — it is what makes them one trace
    assert env[agentenv.TRACEPARENT].startswith("00-")


def test_the_box_is_never_given_a_console_exporter(monkeypatch):
    """The SDK reads the CLI's stdout as its message channel, so a console exporter
    doesn't export the burst, it corrupts it."""
    monkeypatch.setenv(telemetry.AGENT_OTLP_ENDPOINT_ENV, "http://collector:4318")
    assert "console" not in " ".join(burst_env(_spec()).values())


def test_content_bodies_stay_out_of_telemetry(monkeypatch):
    """Arguments yes, bodies no. Tool inputs are the experiment's dataset (§9); the
    full input/output bodies and raw API bodies are the transcript's job, and the
    transcript is in the archive with the ledger's checksum over it."""
    monkeypatch.setenv(telemetry.AGENT_OTLP_ENDPOINT_ENV, "http://collector:4318")
    env = burst_env(_spec())
    assert env[agentenv.OTEL_LOG_TOOL_DETAILS] == "1"
    assert "OTEL_LOG_TOOL_CONTENT" not in env
    assert "OTEL_LOG_RAW_API_BODIES" not in env


def test_no_metrics_exporter_because_the_ledger_is_the_metrics_store(monkeypatch):
    """TO-BE §12: no emitted-metrics pipeline — the CLI's counters are cost and
    tokens, which the burst row already records as the authoritative copy."""
    monkeypatch.setenv(telemetry.AGENT_OTLP_ENDPOINT_ENV, "http://collector:4318")
    assert agentenv.OTEL_METRICS_EXPORTER not in burst_env(_spec())


def test_a_check_burst_gets_no_telemetry(monkeypatch):
    """`check` is the no-network boot smoke: no credential, and nothing to export."""
    monkeypatch.setenv(telemetry.AGENT_OTLP_ENDPOINT_ENV, "http://collector:4318")
    assert not [key for key in burst_env(_spec(command="check")) if key.startswith("OTEL_")]


# --- docker's packaging: build_command ---


def _env_of(cmd: list[str]) -> dict[str, str]:
    """The `-e KEY=VALUE` pairs the container will actually be given."""
    pairs = [cmd[i + 1] for i, arg in enumerate(cmd) if arg == "-e"]
    return dict(pair.split("=", 1) for pair in pairs)


def test_command_shape(monkeypatch):
    monkeypatch.delenv(agentenv.ANTHROPIC_API_KEY, raising=False)
    cmd = DockerRunner().build_command(_spec())
    assert cmd[:5] == ["docker", "run", "--rm", "--name", "burst-b1"]  # the name kill and ps go by
    assert cmd[-2:] == ["usf/dev-agent:latest", "run"]
    assert f"/data/volumes/b1:{agentenv.CONTAINER_SESSION_DIR}" in cmd
    assert f"/clones/product:{agentenv.CONTAINER_REPO_DIR}" in cmd


def test_the_env_is_burst_env_packaged_as_flags(monkeypatch):
    monkeypatch.setenv(agentenv.CLAUDE_CODE_OAUTH_TOKEN, "oat-test")
    spec = _spec(github_token="ghs_x", model="claude-sonnet-5", extra_env={"SANDBOX_URL": "http://s"})
    assert _env_of(DockerRunner().build_command(spec)) == burst_env(spec)


def test_codex_auth_mounted_only_for_run_bursts(monkeypatch):
    monkeypatch.delenv(agentenv.ANTHROPIC_API_KEY, raising=False)
    spec_run = _spec(codex_auth_dir=Path("/host/.codex"))
    spec_check = _spec(codex_auth_dir=Path("/host/.codex"), command="check")
    assert "/host/.codex:/home/agent/.codex" in DockerRunner().build_command(spec_run)
    assert "/host/.codex:/home/agent/.codex" not in DockerRunner().build_command(spec_check)


def test_seccomp_profile_applied_and_valid(monkeypatch):
    """Issue #3: bursts run under the userns-enabled profile, never unconfined."""
    monkeypatch.delenv(agentenv.ANTHROPIC_API_KEY, raising=False)
    cmd = DockerRunner().build_command(_spec())
    assert f"seccomp={SECCOMP_PROFILE}" in cmd
    assert "unconfined" not in " ".join(cmd)

    profile = json.loads(SECCOMP_PROFILE.read_text(encoding="utf-8"))
    assert profile["defaultAction"] != "SCMP_ACT_ALLOW"  # still an allowlist
    userns_rules = [
        rule
        for rule in profile["syscalls"]
        if "unshare" in rule.get("names", []) and rule["action"] == "SCMP_ACT_ALLOW" and not rule.get("args")
    ]
    assert userns_rules, "userns syscalls must be allowed unconditionally"


def test_reviewer_repo_mount_is_read_only():
    cmd = DockerRunner().build_command(_spec(role="reviewer", repo_read_only=True))
    assert any(v.endswith("/workspace/repo:ro") for v in cmd)
    # and the dev burst's stays writable
    cmd = DockerRunner().build_command(_spec())
    assert any(v.endswith("/workspace/repo") for v in cmd)
    assert not any(v.endswith(":ro") for v in cmd)


# --- the other two docker lines: kill and alive ---


def test_a_lost_lease_fences_the_burst_once_by_name(tmp_path, monkeypatch):
    """False from the heartbeat means a peer holds the task: the container is
    killed — by the runner, once, by the name every burst carries — and the
    stream is drained so the exit code is the container's own, not a guess."""
    from control_plane import runner as runner_mod

    monkeypatch.setattr(runner_mod, "HEARTBEAT_POLL_S", 0.05)
    flag = tmp_path / "killed"
    runner = DockerRunner()
    monkeypatch.setattr(
        runner, "build_command",
        lambda spec: ["sh", "-c", f"echo started; while [ ! -f {flag} ]; do sleep 0.02; done; echo drained; exit 137"],
    )
    kills = []

    def fake_kill(burst_id):  # stands in for `docker kill burst-<id>`
        kills.append(burst_id)
        flag.touch()

    monkeypatch.setattr(runner, "kill", fake_kill)
    answers = iter([True, False, False, False, False, False, False, False, False, False])
    log_path = tmp_path / "b9.log"
    code = runner.run(_spec(burst_id="b9"), log_path=log_path, heartbeat=lambda: next(answers, False))
    assert kills == ["b9"]  # fenced exactly once, by name
    assert code == 137  # the container's exit, after the drain
    assert log_path.read_text(encoding="utf-8") == "started\ndrained\n"


def test_kill_and_alive_go_by_the_containers_name(monkeypatch):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="abc123\n" if "ps" in argv else "", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    runner = DockerRunner()
    runner.kill("b9")
    assert runner.alive("b9") is True
    assert calls == [
        ["docker", "kill", "burst-b9"],
        ["docker", "ps", "-q", "--filter", "name=burst-b9"],
    ]


def test_alive_is_false_when_docker_lists_nothing(monkeypatch):
    monkeypatch.setattr(
        subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, 0, stdout="\n", stderr="")
    )
    assert DockerRunner().alive("gone") is False


def test_a_runtime_is_docker_runs_own_flag_and_absent_by_default():
    """G4: the golden replay's runtime under docker is `--runtime` (runsc, kata-runtime)."""
    assert "--runtime" not in DockerRunner().build_command(_spec())
    cmd = DockerRunner().build_command(_spec(runtime_class="runsc"))
    assert cmd[cmd.index("--runtime") + 1] == "runsc"
    assert cmd.index("--runtime") < cmd.index("usf/dev-agent:latest")
