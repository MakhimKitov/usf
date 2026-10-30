"""The burst loop's result assembly, driven by a faked SDK message stream."""

import json

import pytest
from claude_agent_sdk import ResultMessage, SystemMessage
from dev_agent import burst
from dev_agent.env import BurstEnv


def _fake_stream(*messages):
    async def fake_query(*, prompt, options):
        for message in messages:
            yield message

    return fake_query


def test_stream_death_still_writes_a_resumable_result(tmp_path, monkeypatch):
    """The budget rail kills the stream mid-run (the SDK raises). The result
    file must still land — with the session id captured from the *init*
    message — or retries restart from zero instead of resuming (task 17)."""

    def _dying_stream(*messages):
        async def fake_query(*, prompt, options):
            for message in messages:
                yield message
            raise Exception("Claude Code returned an error result: Reached maximum budget ($2)")

        return fake_query

    monkeypatch.setattr(
        burst,
        "query",
        _dying_stream(
            SystemMessage(subtype="init", data={"model": "m", "session_id": "sess-cut-short"}),
        ),
    )
    env = BurstEnv(
        task_id="17", burst_id="b4", task_prompt="build it",
        repo_dir=tmp_path, session_dir=tmp_path / "session",
    )
    with pytest.raises(Exception, match="maximum budget"):
        burst.run_burst_sync(env)
    written = json.loads(env.result_path.read_text(encoding="utf-8"))
    assert written["session_id"] == "sess-cut-short"
    assert written["is_error"] is True
    assert "maximum budget" in written["error"]


def test_result_records_the_resolved_model(tmp_path, monkeypatch):
    """The init message names the model the CLI actually resolved — that, not
    the requested alias, is what lands in the burst result."""
    monkeypatch.setattr(
        burst,
        "query",
        _fake_stream(
            SystemMessage(subtype="init", data={"model": "claude-sonnet-5-20260203"}),
            ResultMessage(
                subtype="success",
                duration_ms=10,
                duration_api_ms=5,
                is_error=False,
                num_turns=1,
                session_id="sess-1",
                total_cost_usd=0.01,
                usage={"input_tokens": 3, "output_tokens": 4},
            ),
        ),
    )
    env = BurstEnv(
        task_id="42",
        burst_id="b1",
        task_prompt="do the thing",
        repo_dir=tmp_path,
        session_dir=tmp_path / "session",
        model="sonnet",
    )
    result = burst.run_burst_sync(env)

    assert result.model == "claude-sonnet-5-20260203"
    assert result.session_id == "sess-1"
    assert result.num_turns == 1
    written = json.loads(env.result_path.read_text(encoding="utf-8"))
    assert written["model"] == "claude-sonnet-5-20260203"
    # the engine's own word for how the run ended, which the branch summary
    # reads (H5): "ran out of turns" is not something the exit code can say
    assert written["subtype"] == "success"


def test_a_fork_reaches_the_engine_as_the_sdks_own_flag():
    """A new branch of the session tree (H5) is the SDK's `fork_session` on the
    resume — never transcript surgery, and nothing the worker decides itself."""
    base = dict(task_id="42", burst_id="b1", resume_session_id="sess-1")
    assert burst._to_options(BurstEnv(**base, fork_session=True)).fork_session is True
    assert burst._to_options(BurstEnv(**base)).fork_session is False


def test_effort_reaches_the_engine_only_when_the_row_set_one():
    """Unset is the CLI's own default for the model (#119): the options carry
    no effort, so the SDK passes no `--effort` — what every burst ran at before."""
    base = dict(task_id="42", burst_id="b1")
    assert burst._to_options(BurstEnv(**base, effort="xhigh")).effort == "xhigh"
    assert burst._to_options(BurstEnv(**base)).effort is None


def test_final_text_is_the_last_assistant_message(tmp_path, monkeypatch):
    """Structured reviewer output rides the final message alone — earlier
    narration must not contaminate it."""
    from claude_agent_sdk import AssistantMessage, TextBlock

    def _assistant(text):
        return AssistantMessage(content=[TextBlock(text=text)], model="m")

    monkeypatch.setattr(
        burst,
        "query",
        _fake_stream(
            _assistant("Let me look at the diff."),
            _assistant('{"verdict": "ok", "findings": []}'),
            ResultMessage(
                subtype="success",
                duration_ms=10,
                duration_api_ms=5,
                is_error=False,
                num_turns=2,
                session_id="sess-2",
                total_cost_usd=0.01,
                usage={},
            ),
        ),
    )
    env = BurstEnv(
        task_id="5", burst_id="b2", task_prompt="review it",
        repo_dir=tmp_path, session_dir=tmp_path / "session",
    )
    result = burst.run_burst_sync(env)
    assert result.final_text == '{"verdict": "ok", "findings": []}'
    assert "Let me look" in result.text_tail  # the tail still keeps the narrative


def test_a_burst_may_propose_what_it_learned_and_usually_proposes_nothing(tmp_path, monkeypatch):
    """The result contract's `memories` field (TO-BE §9, H4). Optional by
    design: a dev burst's deliverable is a pull request, not a JSON document, so
    a missing block is the ordinary case and a malformed one is worth nothing
    more than being ignored — the opposite of the tester's report, where the
    JSON *is* the deliverable and a bad one is refused."""
    from dev_agent.burst import proposed_memories

    said = (
        "I opened the PR.\n\n```json\n"
        '{"memories": [{"claim": "the sandbox has no network on port 443",'
        ' "evidence": "curl timed out in three scenarios"},'
        ' {"claim": "unsupported", "evidence": ""}]}'
        "\n```"
    )
    assert proposed_memories(said) == [
        {"claim": "the sandbox has no network on port 443",
         "evidence": "curl timed out in three scenarios"},
    ]
    assert proposed_memories("I opened the PR and left a comment.") == []
    assert proposed_memories("```json\n{not json at all}\n```") == []
    assert proposed_memories("") == []

    # and the caps: a runaway final message cannot become a thousand rows
    many = {"memories": [{"claim": f"c{i}", "evidence": "e"} for i in range(50)]}
    assert len(proposed_memories(json.dumps(many))) == 10


def test_tool_use_is_logged_but_never_contaminates_final_text(tmp_path, monkeypatch, capsys):
    """A silent burst looks hung from outside — tool calls must reach the log;
    they must not leak into final_text (the structured-output channel)."""
    from claude_agent_sdk import AssistantMessage, TextBlock, ToolUseBlock

    monkeypatch.setattr(
        burst,
        "query",
        _fake_stream(
            AssistantMessage(
                content=[ToolUseBlock(id="t1", name="Bash", input={"command": "git diff"})],
                model="m",
            ),
            AssistantMessage(content=[TextBlock(text='{"verdict": "ok", "findings": []}')], model="m"),
            ResultMessage(
                subtype="success", duration_ms=10, duration_api_ms=5, is_error=False,
                num_turns=2, session_id="s", total_cost_usd=0.01, usage={},
            ),
        ),
    )
    env = BurstEnv(task_id="7", burst_id="b3", task_prompt="review", repo_dir=tmp_path,
                   session_dir=tmp_path / "session")
    result = burst.run_burst_sync(env)
    out = capsys.readouterr().out
    assert "Bash" in out and "git diff" in out  # the structured tool event reaches the log
    assert result.final_text == '{"verdict": "ok", "findings": []}'
    assert "Bash" not in result.text_tail
