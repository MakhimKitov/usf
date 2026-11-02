"""Tracing: burst span attributes, the link to the predecessor, log correlation."""

import re

import structlog
from control_plane.models import BurstRecord, Role
from control_plane.telemetry import (
    AGENT_OTLP_ENDPOINT_ENV,
    agent_telemetry_env,
    burst_span,
    init_telemetry,
    link_to_burst,
)

TRACEPARENT_RE = re.compile(r"^00-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}$")


def _parent(burst_id="b1", trace="a" * 32, span="b" * 16, role=Role.DEV):
    """A predecessor as the ledger hands it back — the leaf of the session tree."""
    return BurstRecord(
        burst_id=burst_id, task_id="owner/product#42", role=role, trace_id=trace, span_id=span
    )


def test_burst_span_mints_valid_traceparent():
    init_telemetry()
    with burst_span("42", "b1", "dev", "work") as (span, traceparent):
        assert TRACEPARENT_RE.match(traceparent)
        ctx = span.get_span_context()
        assert f"{ctx.trace_id:032x}" in traceparent
        assert f"{ctx.span_id:016x}" in traceparent


def test_burst_span_carries_the_model():
    init_telemetry()
    with burst_span("42", "b1", "dev", "work", model="claude-sonnet-5") as (span, _):
        assert span.attributes.get("burst.model") == "claude-sonnet-5"
    with burst_span("42", "b2", "dev", "work") as (span, _):
        assert "burst.model" not in span.attributes


def test_each_burst_gets_its_own_trace():
    init_telemetry()
    with burst_span("42", "b1", "dev", "work") as (_, tp1):
        pass
    with burst_span("42", "b2", "dev", "review") as (_, tp2):
        pass
    assert tp1.split("-")[1] != tp2.split("-")[1]  # outside a turn, each burst is a root of its own


# --- lineage across traces (B1) ---


def test_a_resumed_burst_links_to_its_predecessors_span():
    """The join that makes a task readable: trace-per-burst means the burst before
    this one is in a different trace, and the link is the only thing that says so."""
    init_telemetry()
    with burst_span("owner/product#42", "b2", "dev", "work", parent=_parent()) as (span, _):
        (link,) = span.links
        assert f"{link.context.trace_id:032x}" == "a" * 32
        assert f"{link.context.span_id:016x}" == "b" * 16
        assert link.attributes["burst.id"] == "b1"
        assert link.attributes["burst.link"] == "resumed_from"
        assert link.context.is_remote  # someone else's span, not one of ours to close
        # and on the span itself, for the viewers that render no links at all
        assert span.attributes["burst.resumed_from"] == "b1"


def test_a_first_burst_links_to_nothing():
    init_telemetry()
    with burst_span("owner/product#42", "b1", "dev", "work") as (span, _):
        assert span.links == ()
        assert "burst.resumed_from" not in span.attributes


def test_a_predecessor_that_recorded_no_span_is_named_but_not_linked():
    """Every burst from before B1 has a trace id and no span id. A link needs the
    pair, and a link carrying a zero span id points at nothing — it would read as a
    broken parent rather than an unrecorded one, so there is no link. The attribute
    still names the burst, which is all that row can honestly support."""
    init_telemetry()
    pre_b1 = _parent(span="")
    assert link_to_burst(pre_b1) is None
    with burst_span("owner/product#42", "b2", "dev", "work", parent=pre_b1) as (span, _):
        assert span.links == ()
        assert span.attributes["burst.resumed_from"] == "b1"


def test_ids_that_cannot_be_a_span_are_not_made_into_one():
    assert link_to_burst(_parent(trace="", span="")) is None  # tracing was off
    assert link_to_burst(_parent(trace="0" * 32)) is None  # parses, means "no trace"
    assert link_to_burst(_parent(span="0" * 16)) is None  # parses, means "no span"
    assert link_to_burst(_parent(trace="not hex at all!" * 2)) is None  # a hand-edited row


def test_the_predecessors_role_travels_with_the_link():
    """Which agent's session this burst inherited is the first question asked of a
    link, and answering it from the link saves fetching the other trace."""
    link = link_to_burst(_parent(role=Role.TESTER))
    assert link.attributes["burst.role"] == "tester"


# --- the agent's own telemetry (B2) ---


def test_a_task_id_stays_readable_in_the_resource_attributes(monkeypatch):
    """`/` and `#` are in every task id and separate nothing, so they are left as
    they are — a dashboard filtering on task.id should show the id."""
    monkeypatch.setenv(AGENT_OTLP_ENDPOINT_ENV, "http://collector:4318")
    env = agent_telemetry_env("MakhimKitov/discord-bot#1", "cd99ecaae580", "dev", "work")
    assert env["OTEL_RESOURCE_ATTRIBUTES"].startswith("task.id=MakhimKitov/discord-bot#1,")


def test_a_value_that_would_split_the_list_is_encoded(monkeypatch):
    """Comma, space and `=` are the separators. An unencoded one wouldn't corrupt a
    value, it would invent an attribute — so they go in percent-encoded."""
    monkeypatch.setenv(AGENT_OTLP_ENDPOINT_ENV, "http://collector:4318")
    env = agent_telemetry_env("owner/repo#1", "b1", "dev", "a, b=c d")
    attributes = dict(pair.split("=", 1) for pair in env["OTEL_RESOURCE_ATTRIBUTES"].split(","))
    assert attributes["burst.kind"] == "a%2C%20b%3Dc%20d"
    assert set(attributes) == {"task.id", "burst.id", "burst.role", "burst.kind"}


def test_the_endpoint_is_the_toggle(monkeypatch):
    monkeypatch.delenv(AGENT_OTLP_ENDPOINT_ENV, raising=False)
    assert agent_telemetry_env("owner/repo#1", "b1", "dev", "work") == {}
    monkeypatch.setenv(AGENT_OTLP_ENDPOINT_ENV, "   ")  # set-but-blank is unset
    assert agent_telemetry_env("owner/repo#1", "b1", "dev", "work") == {}


# --- log correlation (TO-BE §12) ---


def test_the_ids_are_bound_to_the_log_context_for_the_span():
    init_telemetry()
    with burst_span("owner/product#42", "b7", "dev", "work") as (span, _):
        bound = structlog.contextvars.get_contextvars()
        assert bound["trace_id"] == f"{span.get_span_context().trace_id:032x}"
        assert (bound["burst_id"], bound["task_id"]) == ("b7", "owner/product#42")
    # and gone afterwards: a scheduler tick runs bursts one after another, so a
    # trace_id that outlived its burst would attach the wrong id to the next lines
    assert "trace_id" not in structlog.contextvars.get_contextvars()


def test_a_nested_span_restores_the_outer_binding():
    """Nothing nests bursts today. reset_contextvars is used anyway because it is
    the only version that composes — the alternative unbinds what it did not bind."""
    init_telemetry()
    with burst_span("owner/product#42", "outer", "dev", "work"):
        outer = dict(structlog.contextvars.get_contextvars())
        with burst_span("owner/product#42", "inner", "dev", "review"):
            assert structlog.contextvars.get_contextvars()["burst_id"] == "inner"
        assert structlog.contextvars.get_contextvars() == outer


# --- the turn: one trace through the platform and into the box ---


def _by_name(exporter):
    return {span.name: span for span in exporter.get_finished_spans()}


def test_a_turn_is_the_root_and_the_burst_sits_inside_it(spans):
    from control_plane.telemetry import step, turn_span

    with turn_span("owner/product#42", "work", "owner/product"):
        with burst_span("owner/product#42", "b1", "dev", "work"):
            with step("burst.run"):
                pass
    named = _by_name(spans)
    turn, burst, run = named["turn"], named["burst"], named["burst.run"]
    assert turn.parent is None
    assert burst.parent.span_id == turn.context.span_id
    assert run.parent.span_id == burst.context.span_id
    assert {turn.context.trace_id, burst.context.trace_id, run.context.trace_id} == {turn.context.trace_id}
    assert turn.attributes["task.id"] == "owner/product#42"
    assert turn.attributes["task.repository"] == "owner/product"


def test_the_turn_binds_its_trace_into_the_log_context(spans):
    from control_plane.telemetry import turn_span

    with turn_span("owner/product#42", "work") as span:
        bound = structlog.contextvars.get_contextvars()
        assert bound["trace_id"] == f"{span.get_span_context().trace_id:032x}"
        assert bound["task_id"] == "owner/product#42"
    assert "trace_id" not in structlog.contextvars.get_contextvars()


def test_a_step_outside_a_trace_is_nothing(spans):
    """The poll reads GitHub every tick; a span per read with no turn around it
    would be a root per request, burying the turns."""
    from control_plane.telemetry import step_in_trace

    with step_in_trace("github.request") as span:
        assert not span.is_recording()
    assert spans.get_finished_spans() == ()


def test_a_failed_step_is_marked_and_still_raises(spans):
    import pytest
    from control_plane.telemetry import step, turn_span
    from opentelemetry.trace import StatusCode

    with pytest.raises(RuntimeError):
        with turn_span("owner/product#42", "work"):
            with step("clone.sync"):
                raise RuntimeError("origin unreachable")
    named = _by_name(spans)
    assert named["clone.sync"].status.status_code == StatusCode.ERROR
    assert named["turn"].status.status_code == StatusCode.ERROR
