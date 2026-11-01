"""Run one work burst: resume the task's session, do the work, record the outcome.

The container lives exactly one burst (ARCHITECTURE.md §6). Durable state is
the branch (GitHub), the session (the mounted volume, harvested by the control
plane) and the burst result file this module writes before exiting.
"""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import structlog
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    HookMatcher,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolUseBlock,
    query,
)

from dev_agent.contract import build_system_prompt
from dev_agent.env import BurstEnv
from dev_agent.walls import make_pre_tool_use_hook

log = structlog.get_logger()

TEXT_TAIL_CHARS = 4000
FINAL_TEXT_CHARS = 16000  # the last assistant message whole — reviewer findings ride here

# Image-baked procedures (codex-review, …). Root-owned in the image so the
# agent can invoke but never edit them; absent in local dev runs.
TOOLKIT_PLUGIN_PATH = Path("/opt/dev-agent-toolkit")


@dataclass(slots=True)
class BurstResult:
    """What the control plane learns from a finished burst."""

    task_id: str
    burst_id: str
    role: str
    burst_kind: str
    session_id: str | None = None
    model: str | None = None  # resolved by the CLI (init message), not what we requested
    num_turns: int | None = None
    total_cost_usd: float | None = None  # advisory under subscription billing
    input_tokens: int | None = None
    output_tokens: int | None = None
    is_error: bool = False
    # How the engine says the run ended — `success`, `error_max_turns`,
    # `error_max_budget_usd`, `error_during_execution`. Read by the control
    # plane's branch summary (H5), which describes an abandoned attempt from
    # what it can see without reading the transcript.
    subtype: str | None = None
    error: str | None = None  # what killed the burst, when something did
    # What this burst learned that outlives it (TO-BE §9, H2 of Stage H): claims
    # with evidence, *proposed* — the control plane stores them with provenance
    # and nothing here is trusted into recall. Empty is the normal case.
    memories: list[dict] = field(default_factory=list)
    duration_s: float | None = None
    finished_at: str = ""
    text_tail: str = ""
    final_text: str = ""  # the last assistant message — structured output lands here


def _to_options(env: BurstEnv) -> ClaudeAgentOptions:
    plugins = (
        [{"type": "local", "path": str(TOOLKIT_PLUGIN_PATH)}]
        if TOOLKIT_PLUGIN_PATH.is_dir()
        else []
    )
    return ClaudeAgentOptions(
        cwd=str(env.repo_dir),
        system_prompt=build_system_prompt(env.role, env.repo_dir),
        resume=env.resume_session_id,
        # a fork only means something with a session to fork from; without one
        # the flag is inert, and it is passed as given rather than second-guessed
        fork_session=env.fork_session,
        max_turns=env.max_turns,
        max_budget_usd=env.max_budget_usd,
        permission_mode=env.permission_mode,  # type: ignore[arg-type]
        model=env.model,
        # None is the CLI's own default for the model: the SDK passes no
        # `--effort` at all, which is what every burst ran at before the row
        # carried it (#119)
        effort=env.effort,  # type: ignore[arg-type]
        plugins=plugins,  # type: ignore[arg-type]
        hooks={
            "PreToolUse": [
                HookMatcher(hooks=[make_pre_tool_use_hook(env.repo_dir, env.violations_path)])
            ]
        },
    )


def setup_git(env: BurstEnv) -> None:
    """Commit identity (the App bot) + gh as the credential helper for pushes.

    Only touches the clone's local config; no token is written to disk — git
    asks gh, gh reads GH_TOKEN from the environment.
    """
    if not (env.github_token and (env.repo_dir / ".git").exists()):
        return
    settings = [
        ("user.name", env.git_user_name or "dev-agent"),
        ("user.email", env.git_user_email or "dev-agent@invalid"),
        ("credential.helper", "!gh auth git-credential"),
        # No ssh in the container (no keys, by design) — an SSH-origin clone
        # must reach GitHub over HTTPS through the credential helper.
        ("url.https://github.com/.insteadOf", "git@github.com:"),
    ]
    for key, value in settings:
        subprocess.run(
            ["git", "config", "--local", key, value], cwd=env.repo_dir, check=True
        )


async def run_burst(env: BurstEnv) -> BurstResult:
    if not env.task_prompt:
        raise ValueError("a burst needs a task prompt")
    setup_git(env)
    started = time.monotonic()
    result = BurstResult(
        task_id=env.task_id, burst_id=env.burst_id, role=env.role, burst_kind=env.burst_kind
    )
    chunks: list[str] = []
    final_parts: list[str] = []
    try:
        async for message in query(prompt=env.task_prompt, options=_to_options(env)):
            if isinstance(message, AssistantMessage):
                final_parts = []
                for block in message.content:
                    if isinstance(block, TextBlock):
                        print(block.text, flush=True)
                        chunks.append(block.text)
                        final_parts.append(block.text)
                    elif isinstance(block, ToolUseBlock):
                        # a silent burst looks identical to a hung one from outside —
                        # the log must show activity, not just narration
                        detail = json.dumps(block.input)[:160] if block.input else ""
                        log.info("tool", name=block.name, args=detail)
            elif isinstance(message, SystemMessage) and message.subtype == "init":
                result.model = message.data.get("model") or result.model
                # the init message names the session within seconds of start; the
                # final result only confirms it — a burst killed mid-run (budget
                # rail, crash, docker kill) must still leave a resumable trail
                result.session_id = message.data.get("session_id") or result.session_id
            elif isinstance(message, ResultMessage):
                result.session_id = message.session_id
                result.num_turns = getattr(message, "num_turns", None)
                result.total_cost_usd = getattr(message, "total_cost_usd", None)
                result.is_error = bool(getattr(message, "is_error", False))
                result.subtype = getattr(message, "subtype", None)
                usage = getattr(message, "usage", None) or {}
                result.input_tokens = usage.get("input_tokens")
                result.output_tokens = usage.get("output_tokens")
    except BaseException as exc:
        result.is_error = True
        result.error = f"{type(exc).__name__}: {exc}"
        log.error("burst stream died — result written for resume", error=result.error)
        raise
    finally:
        # the result file is the burst's trail; it must exist however the burst ends
        result.duration_s = round(time.monotonic() - started, 3)
        result.finished_at = datetime.now(UTC).isoformat()
        result.text_tail = "".join(chunks)[-TEXT_TAIL_CHARS:]
        result.final_text = "".join(final_parts)[-FINAL_TEXT_CHARS:]
        result.memories = proposed_memories(result.final_text)
        write_result(env, result)
    return result


# What a burst may propose, and how much of it. Caps rather than validation:
# the platform decides what a proposal *means* (§9) — this side only makes sure
# a runaway final message cannot become a thousand rows.
MAX_MEMORIES = 10
MAX_MEMORY_CHARS = 500


def proposed_memories(final_text: str) -> list[dict]:
    """The `{"memories": [...]}` block a burst may end with, if it ends with one.

    Optional by design, and never fatal: a dev burst's deliverable is a pull
    request, not a JSON document, so a missing block is the normal case and a
    malformed one is worth nothing more than being ignored. (The tester's report
    is the opposite — there the JSON *is* the deliverable, and
    `testing.parse_report` refuses rather than shrugs.)

    Claim and evidence are both required here, because an entry without evidence
    is an instruction wearing a datum's clothes, and §9's whole rule is that
    memory is data an agent may verify.
    """
    text = (final_text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.DOTALL)
    candidate = fenced.group(1) if fenced else text[text.find("{") : text.rfind("}") + 1]
    if not candidate.strip():
        return []
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, dict):
        return []
    proposals = []
    for item in (data.get("memories") or [])[:MAX_MEMORIES]:
        if not isinstance(item, dict):
            continue
        claim = str(item.get("claim") or "").strip()[:MAX_MEMORY_CHARS]
        evidence = str(item.get("evidence") or "").strip()[:MAX_MEMORY_CHARS]
        if claim and evidence:
            proposals.append({"claim": claim, "evidence": evidence})
    return proposals


def write_result(env: BurstEnv, result: BurstResult) -> None:
    env.result_path.parent.mkdir(parents=True, exist_ok=True)
    env.result_path.write_text(json.dumps(asdict(result), indent=2), encoding="utf-8")


def run_burst_sync(env: BurstEnv) -> BurstResult:
    return asyncio.run(run_burst(env))
