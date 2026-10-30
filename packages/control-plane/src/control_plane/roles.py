"""Roles as rows — ceilings in code, composition in data (TO-BE §4, D5).

A role row is a declarative execution contract: preamble, tools, model, budget,
turns, mount, identity, sink. The store keeps the rows; this module is the
*engine* side — the ceilings no row composes past, and the one place a row is
turned into what a burst actually gets:

- **Ceilings (code).** `TOOL_ALLOWLIST` bounds what a row's `tools` may name;
  the image's own role preamble and `walls.py` deny floor run beneath whatever
  the row says; reviewer/tester/monitor mounts are read-only by code — a row
  can narrow the dev to read-only, it can never widen a reader to writable.
- **Composition (data).** `prompt_preamble`, `model`, `effort`,
  `max_budget_usd`, `max_turns`, `mount_mode` are the operator's (`role set`,
  audited). The wall-bearing fields — `role`, `kind`, `identity`, `sink`,
  `tools` — of a pipeline row are frozen: refused here by name before any SQL,
  and by the store's trigger beneath (Postgres `roles_pipeline_guard`, its
  SQLite twin).

Nothing here reads the environment. The model/budget/turn knobs the env used
to carry (`DEV_AGENT_MODEL`, `REVIEWER_MODEL`, `TESTER_MODEL`,
`SCHEDULER_BURST_BUDGET_USD`, `SCHEDULER_BURST_MAX_TURNS`) are retired — one
source, the row, or "editing an open field changes the next burst" is false.
"""

from __future__ import annotations

from collections.abc import Mapping

from dev_agent.env import EFFORT_LEVELS

from control_plane.models import RoleRecord

PIPELINE_ROLES: tuple[str, ...] = ("dev", "reviewer", "tester", "monitor")

# The code allowlist a row's `tools` must stay inside (TO-BE §4: "a role's tools
# are a subset of a code-defined allowlist"). These are the SDK's own tool names;
# a new name here is a platform release, deliberately. `ALL_TOOLS` is the
# seeded value — no narrowing — and the only one a pipeline row can carry today.
TOOL_ALLOWLIST: frozenset[str] = frozenset(
    {
        "Bash", "BashOutput", "KillShell",
        "Read", "Write", "Edit", "MultiEdit", "NotebookEdit",
        "Glob", "Grep",
        "WebFetch", "WebSearch",
        "Task", "TodoWrite", "Skill",
    }
)
ALL_TOOLS = "*"

# What `role set` may touch — the open fields, exactly. Everything else is a
# wall or nobody's (key, timestamps, trigger — T4's).
ROLE_SET_COLUMNS: tuple[str, ...] = (
    "prompt_preamble",
    "model",
    "effort",
    "max_budget_usd",
    "max_turns",
    "mount_mode",
)
ROLE_WALL_COLUMNS: tuple[str, ...] = ("role", "kind", "identity", "sink", "tools")
MOUNT_MODES: tuple[str, ...] = ("rw", "ro")

# The mount floor: these roles read and never write, as code — REVIEWERS.md /
# TESTER.md call it "a wall, not an instruction". The row's `mount_mode` can
# only narrow from here.
READ_ONLY_ROLES: frozenset[str] = frozenset({"reviewer", "tester", "monitor"})

# The env knobs the row replaced. Set on a machine that runs the scheduler, they
# would silently govern nothing — so startup names them and refuses (the
# declared-but-unhonorable rule, applied to a declaration that no longer exists).
RETIRED_ENV: dict[str, str] = {
    "DEV_AGENT_MODEL": "role set dev --model",
    "REVIEWER_MODEL": "role set reviewer --model",
    "TESTER_MODEL": "role set tester --model",
    "SCHEDULER_BURST_BUDGET_USD": "role set <role> --budget",
    "SCHEDULER_BURST_MAX_TURNS": "role set <role> --turns",
}


class RoleError(RuntimeError):
    """A role row the engine cannot execute, or a `role set` it must refuse."""


def check_settable(changes: Mapping[str, object]) -> None:
    """The store's gate for `role set`: open fields only, and each value shaped
    for its column. Wall fields are refused by name — the same answer the
    trigger gives, given before any SQL so the operator reads a sentence, not
    an error code."""
    if not changes:
        raise RoleError("nothing to set — name at least one field to change")
    walls = sorted(set(changes) & set(ROLE_WALL_COLUMNS))
    if walls:
        raise RoleError(
            f"frozen: {', '.join(walls)} — the wall-bearing fields of a role are code, "
            f"not settings (TO-BE §4); `role set` touches only {', '.join(ROLE_SET_COLUMNS)}"
        )
    unknown = sorted(set(changes) - set(ROLE_SET_COLUMNS))
    if unknown:
        raise RoleError(
            f"not settable: {', '.join(unknown)} — `role set` touches only "
            f"{', '.join(ROLE_SET_COLUMNS)}"
        )
    if "model" in changes and not str(changes["model"]).strip():
        raise RoleError("model must name a model")
    # None is a value here: it hands the choice back to the CLI's default for
    # the model. Which levels a given model accepts is not the platform's table
    # — a model that rejects one fails its burst like a wrong model name does.
    if "effort" in changes and changes["effort"] is not None and changes["effort"] not in EFFORT_LEVELS:
        raise RoleError(f"effort is one of {', '.join(EFFORT_LEVELS)}, or unset for the model's default")
    if "max_budget_usd" in changes and not float(changes["max_budget_usd"]) > 0:  # type: ignore[arg-type]
        raise RoleError("max_budget_usd must be positive — it is the SDK's hard stop")
    if "max_turns" in changes and not int(changes["max_turns"]) > 0:  # type: ignore[call-overload]
        raise RoleError("max_turns must be positive")
    if "mount_mode" in changes and changes["mount_mode"] not in MOUNT_MODES:
        raise RoleError(f"mount_mode is one of {', '.join(MOUNT_MODES)}")


def validate(row: RoleRecord) -> RoleRecord:
    """The load-time ceiling check: a row the engine would compose past its
    ceilings is refused before it shapes a burst. Pipeline rows cannot reach
    here wrong (frozen + seeded); this is what stands behind a custom row."""
    if row.tools != [ALL_TOOLS]:
        outside = sorted(set(row.tools) - TOOL_ALLOWLIST)
        if outside or not row.tools:
            raise RoleError(
                f"role {row.role!r} names tools outside the code allowlist: "
                f"{', '.join(outside) or 'nothing at all'} — allowed: {', '.join(sorted(TOOL_ALLOWLIST))}"
            )
    if row.mount_mode not in MOUNT_MODES:
        raise RoleError(f"role {row.role!r} has mount_mode {row.mount_mode!r}, not one of {MOUNT_MODES}")
    if row.effort is not None and row.effort not in EFFORT_LEVELS:
        raise RoleError(f"role {row.role!r} has effort {row.effort!r}, not one of {EFFORT_LEVELS}")
    if not row.model.strip() or row.max_budget_usd <= 0 or row.max_turns <= 0:
        raise RoleError(f"role {row.role!r} has no model, budget or turns to run with")
    return row


def compose_prompt(row: RoleRecord, prompt: str) -> str:
    """The row's preamble ahead of the task — *after* the image's own role
    preamble, which is the ceiling text (identity, walls) and lives in code."""
    preamble = row.prompt_preamble.strip()
    return f"{preamble}\n\n{prompt}" if preamble else prompt


def repo_read_only(row: RoleRecord) -> bool:
    """Narrowing is data, widening is impossible: the code floor ORed with the row."""
    return row.role in READ_ONLY_ROLES or row.mount_mode == "ro"


def retired_settings(environ: Mapping[str, str]) -> list[str]:
    """Which retired env knobs this machine still sets, with where each went."""
    return [
        f"{name} no longer governs anything — the role row does: `{verb}`"
        for name, verb in RETIRED_ENV.items()
        if (environ.get(name) or "").strip()
    ]
