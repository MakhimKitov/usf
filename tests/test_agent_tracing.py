"""The worker's span (`dev_agent.tracing`): the box's link in a burst's trace.

The control plane hands the box its `burst.run` step as `TRACEPARENT`; the
worker opens `agent.burst` under it and keeps it current while the SDK runs,
and the SDK hands the CLI whatever context is current. These tests hold each
link of that chain on this side of the process boundary — the CLI's half is
checked live (OBSERVABILITY.md, "What a burst looks like in Phoenix").
"""

import asyncio

import pytest
from claude_agent_sdk import ResultMessage, SystemMessage
from dev_agent import burst, tracing
from dev_agent.env import BurstEnv
from dev_agent.walls import make_pre_tool_use_hook
from opentelemetry import propagate
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

PARENT_TRACE = "4bf92f3577b34da6a3ce929d0e0e4736"
PARENT_SPAN = "00f067aa0ba902b7"
TRACEPARENT = f"00-{PARENT_TRACE}-{PARENT_SPAN}-01"

ON = {
    "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
    "OTEL_TRACES_EXPORTER": "otlp",
    "OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector:4318",
}


def _env(tmp_path, **overrides):
    fields = dict(
        task_id="owner/product#7", burst_id="b1", task_prompt="fix it",
        repo_dir=tmp_path, session_dir=tmp_path / "session", traceparent=TRACEPARENT,
    )
    return BurstEnv(**(fields | overrides))


def _result(is_error=False, subtype="success"):
    return ResultMessage(
        subtype=subtype, duration_ms=10, duration_api_ms=5, is_error=is_error, num_turns=3,
        session_id="sess-1", total_cost_usd=0.02, usage={"input_tokens": 3, "output_tokens": 4},
    )


def _stream(*messages, handed=None):
    """A fake `query` that reads back what the SDK would hand the CLI."""

    async def fake_query(*, prompt, options):
        if handed is not None:
            carrier: dict[str, str] = {}
            propagate.inject(carrier)  # what the SDK's transport does before it spawns the CLI
            handed.append(carrier.get("traceparent"))
        for message in messages:
            yield message

    return fake_query


def test_the_switch_is_the_one_the_cli_reads():
    assert tracing.enabled(ON)
    assert not tracing.enabled({})
    assert not tracing.enabled(ON | {"OTEL_TRACES_EXPORTER": "none"})
    assert not tracing.enabled(ON | {"OTEL_EXPORTER_OTLP_ENDPOINT": " "})
    assert tracing.configure({}) is None
    assert tracing.configure(ON) is not None  # built, not yet exporting anything


def test_the_worker_span_sits_under_the_control_planes_and_the_cli_under_it(tmp_path, monkeypatch):
    exporter = InMemorySpanExporter()
    handed: list[str | None] = []
    monkeypatch.setattr(burst, "query", _stream(
        SystemMessage(subtype="init", data={"model": "claude-sonnet-5-20260203", "session_id": "sess-1"}),
        _result(),
        handed=handed,
    ))
    burst.run_burst_sync(_env(tmp_path, model="sonnet", effort="high"), tracing.configure({}, exporter=exporter))

    (span,) = exporter.get_finished_spans()
    assert span.name == "agent.burst"
    assert f"{span.context.trace_id:032x}" == PARENT_TRACE
    assert f"{span.parent.span_id:016x}" == PARENT_SPAN
    # what the CLI is handed is this span — so its tree lands under the worker
    assert handed == [f"00-{PARENT_TRACE}-{span.context.span_id:016x}-01"]
    assert span.attributes["task.id"] == "owner/product#7"
    assert span.attributes["burst.effort"] == "high"
    assert span.attributes["burst.model"] == "claude-sonnet-5-20260203"  # resolved beats requested
    assert span.attributes["burst.session_id"] == "sess-1"
    assert span.attributes["burst.num_turns"] == 3
    assert span.attributes["burst.subtype"] == "success"
    assert span.status.status_code != StatusCode.ERROR


def test_telemetry_off_leaves_the_cli_the_inherited_context(tmp_path, monkeypatch):
    """No provider, no current span: the SDK injects nothing, and the CLI reads
    the control plane's TRACEPARENT from the environment as it always did."""
    handed: list[str | None] = []
    monkeypatch.setattr(burst, "query", _stream(_result(), handed=handed))
    burst.run_burst_sync(_env(tmp_path), None)
    assert handed == [None]


def test_an_engine_error_marks_the_span(tmp_path, monkeypatch):
    exporter = InMemorySpanExporter()
    monkeypatch.setattr(burst, "query", _stream(_result(is_error=True, subtype="error_max_turns")))
    burst.run_burst_sync(_env(tmp_path), tracing.configure({}, exporter=exporter))
    (span,) = exporter.get_finished_spans()
    assert span.status.status_code == StatusCode.ERROR
    assert span.status.description == "error_max_turns"


def test_a_malformed_traceparent_costs_the_parent_not_the_burst(tmp_path, monkeypatch):
    exporter = InMemorySpanExporter()
    monkeypatch.setattr(burst, "query", _stream(_result()))
    burst.run_burst_sync(_env(tmp_path, traceparent="garbage"), tracing.configure({}, exporter=exporter))
    (span,) = exporter.get_finished_spans()
    assert span.parent is None


def test_a_denied_call_is_an_event_on_the_worker_span(tmp_path):
    """The hook runs in the span's context; `tool.use_id` joins the event to the
    CLI's own span for the same call. An allowed call adds nothing."""
    exporter = InMemorySpanExporter()
    provider = tracing.configure({}, exporter=exporter)
    env = _env(tmp_path)
    hook = make_pre_tool_use_hook(tmp_path, env.violations_path)
    with tracing.agent_span(provider, env):
        asyncio.run(hook({"tool_name": "Read", "tool_input": {"file_path": "README.md"}}, "toolu_ok", None))
        denied = asyncio.run(hook({"tool_name": "Read", "tool_input": {"file_path": "/etc/hostname"}}, "toolu_1", None))
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
    (span,) = exporter.get_finished_spans()
    (event,) = span.events
    assert event.name == "wall.denied"
    assert event.attributes["tool.name"] == "Read"
    assert event.attributes["tool.use_id"] == "toolu_1"
    assert "escapes the repository clone" in event.attributes["wall.reason"]


@pytest.mark.parametrize("environ", [{}, ON])
def test_the_worker_never_installs_a_global_provider(environ):
    """A span only has to be current for the SDK to hand it on; installing one
    globally would buy nothing in the box and fight the control plane's in the
    suite, which shares this process."""
    from opentelemetry import trace

    before = trace.get_tracer_provider()
    tracing.configure(environ)
    assert trace.get_tracer_provider() is before
