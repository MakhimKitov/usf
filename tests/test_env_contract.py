"""The container env contract: parsing, defaults, validation."""

from pathlib import Path
from typing import get_args

import pytest
from claude_agent_sdk.types import EffortLevel
from dev_agent import env as agentenv
from dev_agent.env import BurstEnv, EnvError

MINIMAL = {agentenv.TASK_ID: "42", agentenv.BURST_ID: "b1"}


def test_minimal_env_parses_with_defaults():
    env = BurstEnv.from_environ(MINIMAL)
    assert env.task_id == "42"
    assert env.burst_id == "b1"
    assert env.role == "dev"
    assert env.burst_kind == "work"
    assert env.repo_dir == Path(agentenv.CONTAINER_REPO_DIR)
    assert env.session_dir == Path(agentenv.CONTAINER_SESSION_DIR)
    assert env.permission_mode == agentenv.DEFAULT_PERMISSION_MODE
    assert env.resume_session_id is None
    assert env.fork_session is False
    assert env.effort is None  # the CLI's default for the model
    assert env.result_path == Path(agentenv.CONTAINER_SESSION_DIR) / agentenv.BURST_RESULT_FILENAME


def test_missing_required_vars_raise():
    with pytest.raises(EnvError, match="AGENT_TASK_ID"):
        BurstEnv.from_environ({agentenv.BURST_ID: "b1"})
    with pytest.raises(EnvError, match="AGENT_BURST_ID"):
        BurstEnv.from_environ({agentenv.TASK_ID: "42"})


def test_full_env_parses():
    env = BurstEnv.from_environ(
        MINIMAL
        | {
            agentenv.ROLE: "tester",
            agentenv.BURST_KIND: "test",
            agentenv.TASK_PROMPT: "do the thing",
            agentenv.REPO_DIR: "/elsewhere/repo",
            agentenv.CLAUDE_CONFIG_DIR: "/tmp/session",
            agentenv.RESUME_SESSION_ID: "sess-1",
            agentenv.MAX_BUDGET_USD: "2.5",
            agentenv.MAX_TURNS: "40",
            agentenv.MODEL: "claude-sonnet-5",
            agentenv.EFFORT: "xhigh",
        }
    )
    assert env.role == "tester"
    assert env.burst_kind == "test"
    assert env.max_budget_usd == 2.5
    assert env.max_turns == 40
    assert env.effort == "xhigh"
    assert env.session_dir == Path("/tmp/session")
    assert env.fork_session is False  # resuming is continuing, unless told to fork
    assert BurstEnv.from_environ(MINIMAL | {agentenv.FORK_SESSION: "1"}).fork_session is True
    assert env.violations_path == Path("/tmp/session") / agentenv.VIOLATIONS_FILENAME


def test_the_trace_id_is_read_out_of_the_traceparent():
    """What the container needs for a correlated log line is the trace id, and the
    only place it exists in the box is the traceparent the control plane injected."""
    traceparent = f"00-{'a' * 32}-{'b' * 16}-01"
    assert BurstEnv.from_environ(MINIMAL | {agentenv.TRACEPARENT: traceparent}).trace_id == "a" * 32
    assert BurstEnv.from_environ(MINIMAL).trace_id == ""  # tracing off — not an error


def test_a_traceparent_that_is_not_one_costs_only_the_trace_id():
    """It arrives as a string from outside, so it gets checked. A burst is not
    something to fail over a log field: no id beats a wrong id, and the run goes on
    either way."""
    for bad in (
        "",
        "nonsense",
        f"01-{'a' * 32}-{'b' * 16}-01",  # a version we don't know how to read
        f"00-{'a' * 31}-{'b' * 16}-01",  # trace id one digit short
        f"00-{'z' * 32}-{'b' * 16}-01",  # not hex
        f"00-{'0' * 32}-{'b' * 16}-01",  # parses, and means "no trace"
    ):
        env = BurstEnv.from_environ(MINIMAL | {agentenv.TRACEPARENT: bad})
        assert env.trace_id == "", bad
        assert env.task_id == "42"  # and the burst still has everything it needs


def test_bad_values_raise():
    with pytest.raises(EnvError, match="AGENT_ROLE"):
        BurstEnv.from_environ(MINIMAL | {agentenv.ROLE: "villain"})
    with pytest.raises(EnvError, match="AGENT_BURST_KIND"):
        BurstEnv.from_environ(MINIMAL | {agentenv.BURST_KIND: "party"})
    with pytest.raises(EnvError, match="AGENT_MAX_BUDGET_USD"):
        BurstEnv.from_environ(MINIMAL | {agentenv.MAX_BUDGET_USD: "lots"})
    with pytest.raises(EnvError, match="AGENT_MAX_TURNS"):
        BurstEnv.from_environ(MINIMAL | {agentenv.MAX_TURNS: "3.5"})
    with pytest.raises(EnvError, match="AGENT_EFFORT"):
        BurstEnv.from_environ(MINIMAL | {agentenv.EFFORT: "ludicrous"})


def test_the_effort_levels_are_the_sdks():
    """The control plane checks a role row against `EFFORT_LEVELS` without
    importing the SDK, so the spelling here has to be the SDK's own: a release
    that adds or drops a level fails this, not a burst."""
    assert agentenv.EFFORT_LEVELS == get_args(EffortLevel)
