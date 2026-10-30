"""The control-plane CLI.

``burst`` — the M0/M1 manual driver: one burst, by hand. ``run`` — the M2
scheduler: the same choreography, driven unattended by the reconciler loop
(SCHEDULER.md); without ``--github`` it serves every active product in the
registry through one serial loop (C4). ``stop`` / ``resume`` — the org's kill
switch, a ledger row every replica reads (E3). ``github-check`` — App preflight.
``migrate`` — the Postgres ledger's schema, from numbered SQL (STORES.md).
``backfill`` — the one-shot that carries MVP history into the service stores (A6).
``product`` — the registry verbs (C1) behind registration preflight, with
``check`` as the re-runnable drift catcher (C2). A registered product's
policies reach the scheduler from its row, not the env (C3).
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import signal
import sys
from dataclasses import replace
from pathlib import Path

import psycopg
import structlog
from dotenv import load_dotenv
from opentelemetry import trace

from control_plane.backfill import BackfillError, run_backfill
from control_plane.bursts import PlatformStores, execute_burst
from control_plane.clone import CloneError, cache_dir, prepare_cache, prepare_clone, prune_trees
from control_plane.github_api import COPILOT_TOKEN_ENV, GitHubApi, GitHubApiError, slug_from_origin
from control_plane.github_auth import (
    REVIEWER_APP_PREFIX,
    ROLE_PREFIXES,
    TESTER_APP_PREFIX,
    AppCredentials,
    GitHubAuthError,
    InstallationTokenCache,
    bot_git_identity,
    mint_installation_token,
)
from control_plane.migrate import MigrationError, migrate, status
from control_plane.models import (
    ORG_TARGET,
    WAKE_SETTLE_S,
    ProductState,
    RepositoryRecord,
    RoleRecord,
    SubscriptionRecord,
    SubscriptionStatus,
    TaskRecord,
    TaskStatus,
    task_id_for,
)
from control_plane.preflight import PreflightError, run_preflight
from control_plane.roles import (
    PIPELINE_ROLES,
    ROLE_SET_COLUMNS,
    RoleError,
    retired_settings,
)
from control_plane.roles import validate as validate_role
from control_plane.review import (
    PROVIDERS_ENV,
    REVIEW_ENV,
    ReviewPolicy,
    ReviewPolicyError,
)
from control_plane.runner import DEFAULT_IMAGE, HEARTBEAT_POLL_S, open_runner
from control_plane.scheduler import Fleet, Rails, Scheduler
from control_plane.subscriptions import (
    SUBSCRIPTION_SET_COLUMNS,
    SubscriptionError,
    check_cap,
    check_expiry,
    check_ref,
    declare,
)
from secret_refs import SecretError, parse_ref
from control_plane.store import (
    LEDGER_DSN_ENV,
    ArchiveError,
    LocalSessionArchive,
    RegistryError,
    open_archive,
    open_ledger,
)
from control_plane.telemetry import init_telemetry
from control_plane.volume import Snapshot
from control_plane.testing import E2E_ENV, E2EPolicy, E2EPolicyError
from control_plane.wake import attach_wakes
from dev_agent.env import EFFORT_LEVELS
from dev_agent.log import configure_logging

log = structlog.get_logger()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="control-plane")
    sub = parser.add_subparsers(dest="command", required=True)

    burst = sub.add_parser("burst", help="run one burst container against a task, by hand")
    # An issue number, not a task id: the id is derived (A6). --github names the
    # product when the clone's origin can't (a fixture, a detached checkout).
    burst.add_argument("--issue", type=int, required=True, help="the issue number this burst works")
    burst.add_argument("--repo", type=Path, required=True, help="path to the product clone to mount")
    burst.add_argument("--github", default=None, help="owner/name (default: parsed from --repo's origin)")
    burst.add_argument("--prompt", default="", help="the task text (M0/M1: injected directly)")
    burst.add_argument("--prompt-file", type=Path, help="read the task text from a file")
    burst.add_argument("--mode", default="run", choices=["run", "check"])
    burst.add_argument("--image", default=DEFAULT_IMAGE)
    burst.add_argument("--data-dir", type=Path, default=Path("data"), help="platform store root")
    burst.add_argument("--fresh", action="store_true", help="ignore any stored session (no resume)")
    # the dev role row governs these (D5); a flag overrides for this one burst
    burst.add_argument("--budget", type=float, default=None, help="max_budget_usd (default: the dev role row)")
    burst.add_argument("--max-turns", type=int, default=None, help="default: the dev role row")
    burst.add_argument("--model", default=None, help="default: the dev role row")

    run = sub.add_parser(
        "run",
        help="the M2 scheduler: reconcile GitHub ↔ ledger, spawn bursts — "
        "every active registered product (C4), or one repo via --github",
    )
    run.add_argument(
        "--repo",
        type=Path,
        default=None,
        help="operator clone — used only to derive owner/name; bursts mount the platform clone",
    )
    run.add_argument(
        "--github",
        default=None,
        help="serve just this owner/name (env-governed dev mode when unregistered); "
        "omit to serve the registry's active products",
    )
    run.add_argument("--owner", default=None, help="the human owner's login (default: the repo owner)")
    run.add_argument("--data-dir", type=Path, default=Path("data"))
    run.add_argument("--image", default=DEFAULT_IMAGE)
    run.add_argument("--interval", type=int, default=None, help="seconds between ticks (default: rails)")
    run.add_argument("--once", action="store_true", help="one tick, then exit")

    stop = sub.add_parser(
        "stop",
        help="the org's kill switch: every replica stands down and every live burst is killed, "
        "wherever it runs; holds until `resume`",
    )
    stop.add_argument("--data-dir", type=Path, default=Path("data"))
    stop.add_argument("--reason", required=True, help="why — it lands in the audit row and in every replica's log")
    stop.add_argument("--actor", default=None, help="who is acting — lands in the audit row (default: the OS login)")

    resume = sub.add_parser("resume", help="lift the org's STOP: the replicas pick up on their next tick, no restart")
    resume.add_argument("--data-dir", type=Path, default=Path("data"))
    resume.add_argument("--reason", default="", help="lands in the audit row")
    resume.add_argument("--actor", default=None, help="who is acting — lands in the audit row (default: the OS login)")

    migrate_cmd = sub.add_parser("migrate", help="apply pending schema migrations to the Postgres ledger")
    migrate_cmd.add_argument("--dsn", default=None, help=f"Postgres DSN (default: ${LEDGER_DSN_ENV})")
    migrate_cmd.add_argument(
        "--check",
        action="store_true",
        help="report applied/pending and exit 1 if anything is pending; writes nothing",
    )

    backfill_cmd = sub.add_parser(
        "backfill",
        help="one-shot: carry the MVP's single-node history into the service stores",
    )
    backfill_cmd.add_argument(
        "--repository", required=True, help="owner/name — the product those rows belong to"
    )
    backfill_cmd.add_argument("--data-dir", type=Path, default=Path("data"), help="the source root")
    backfill_cmd.add_argument(
        "--dry-run", action="store_true", help="print the plan and write nothing"
    )

    replay_cmd = sub.add_parser(
        "replay",
        help="golden replay (G4): run one archived node + task under each runtime, "
        "and write the criteria matrix",
    )
    replay_cmd.add_argument("fixture", type=Path, help="the golden burst, as JSON (control_plane.replay)")
    replay_cmd.add_argument(
        "--runtime",
        action="append",
        required=True,
        help="a RuntimeClass name (a docker runtime under docker), or 'default'; repeat per trial",
    )
    replay_cmd.add_argument(
        "--node",
        default=None,
        help="a burst id: replay from that burst's own snapshot and session, as the ledger "
        "recorded them, instead of the fixture's node",
    )
    replay_cmd.add_argument("--out", type=Path, required=True, help="where matrix.json and matrix.md go")
    replay_cmd.add_argument("--data-dir", type=Path, default=Path("data"), help="platform store root")
    replay_cmd.add_argument(
        "--strict",
        action="store_true",
        help="exit 1 when the outcomes differ, not only when a trial did not run",
    )

    sub.add_parser(
        "github-check",
        help="preflight every configured role App (dev/reviewer/tester/monitor): "
        "mint a token, show installation + permissions",
    )

    product = sub.add_parser(
        "product",
        help="the registry verbs (TO-BE §2): register/list/show/check/pause/resume/set",
    )
    verbs = product.add_subparsers(dest="verb", required=True)

    def _operator_flags(verb: argparse.ArgumentParser) -> None:
        verb.add_argument("--data-dir", type=Path, default=Path("data"), help="platform store root")
        verb.add_argument("--actor", default=None, help="who is acting — lands in the audit row (default: the OS login)")

    register = verbs.add_parser(
        "register",
        help="register a product; every policy/quota column is materialized — platform defaults unless overridden",
    )
    register.add_argument("repository", help="owner/name")
    register.add_argument("--team", required=True, help="owning team: gate authority, where escalations go")
    register.add_argument("--review", choices=["on", "off"], default=None, help=f"override the platform ${REVIEW_ENV} default")
    register.add_argument("--review-providers", default=None, help=f"override ${PROVIDERS_ENV} (comma-separated)")
    register.add_argument("--e2e", choices=["on", "off"], default=None, help=f"override the platform ${E2E_ENV} default")
    register.add_argument(
        "--test-env",
        action="append",
        default=[],
        metavar="NAME=REF",
        help="sandbox credential ref for the tester (repeatable): env:NAME, file:PATH or helper:NAME — never a value",
    )
    register.add_argument("--concurrency", type=int, default=1)
    register.add_argument("--daily-bursts", type=int, default=None, help="default: $SCHEDULER_DAILY_BURST_CEILING")
    register.add_argument("--usage-share", type=float, default=1.0)
    register.add_argument("--reason", default="", help="lands in the audit row")
    register.add_argument(
        "--lint-issue",
        action="store_true",
        help="file the socket gaps as a `human` issue in the product repo",
    )
    _operator_flags(register)

    listing = verbs.add_parser("list", help="every registered product, one line each")
    _operator_flags(listing)

    check = verbs.add_parser(
        "check",
        help="re-run registration preflight against the live repo — the drift catcher (TO-BE §2)",
    )
    check.add_argument("repository", help="owner/name")
    check.add_argument(
        "--lint-issue",
        action="store_true",
        help="file the socket gaps as a `human` issue in the product repo",
    )
    _operator_flags(check)

    show = verbs.add_parser("show", help="one product's row, and its audit trail")
    show.add_argument("repository", help="owner/name")
    _operator_flags(show)

    pause = verbs.add_parser("pause", help="stop scheduling a product; requires a reason")
    pause.add_argument("repository", help="owner/name")
    pause.add_argument("--reason", required=True, help="why — it lands in the audit row")
    _operator_flags(pause)

    resume = verbs.add_parser("resume", help="resume a paused product")
    resume.add_argument("repository", help="owner/name")
    resume.add_argument("--reason", default="", help="lands in the audit row")
    _operator_flags(resume)

    set_verb = verbs.add_parser(
        "set",
        help="change a live product's policy/quota columns — audited, never retro-propagated from platform defaults",
    )
    set_verb.add_argument("repository", help="owner/name")
    set_verb.add_argument("--team", default=None)
    set_verb.add_argument("--review", choices=["on", "off"], default=None)
    set_verb.add_argument("--review-providers", default=None, help="comma-separated; replaces the selector")
    set_verb.add_argument("--e2e", choices=["on", "off"], default=None)
    set_verb.add_argument(
        "--test-env",
        action="append",
        default=[],
        metavar="NAME=REF",
        help="replaces the whole test-env map (repeatable within one call)",
    )
    set_verb.add_argument("--concurrency", type=int, default=None)
    set_verb.add_argument("--daily-bursts", type=int, default=None)
    set_verb.add_argument("--usage-share", type=float, default=None)
    set_verb.add_argument("--reason", default="", help="lands in the audit row")
    _operator_flags(set_verb)

    pool = sub.add_parser(
        "subscription",
        help="the subscription pool (TO-BE §4 mechanism A, F1): register/list/show/revoke/set — refs, never tokens",
    )
    pool_verbs = pool.add_subparsers(dest="verb", required=True)
    pool_register = pool_verbs.add_parser(
        "register",
        help="a member contributes their subscription: the platform stores a ref, the member keeps the token",
    )
    pool_register.add_argument("--member", required=True, help="who is contributing — the id is derived from it")
    pool_register.add_argument(
        "--ref",
        required=True,
        metavar="REF",
        help="where the member materialized their OAuth token: env:NAME, file:PATH or helper:NAME — never the token",
    )
    pool_register.add_argument("--cap", type=int, required=True, metavar="TOKENS", help="the member's ceiling, tokens per rolling window")
    pool_register.add_argument("--expires", required=True, metavar="DATE", help="when the token dies: a date (YYYY-MM-DD) or an ISO timestamp")
    pool_register.add_argument("--reason", default="", help="lands in the audit row")
    _operator_flags(pool_register)

    pool_list = pool_verbs.add_parser("list", help="every contributed subscription, one line each")
    _operator_flags(pool_list)

    pool_show = pool_verbs.add_parser("show", help="one subscription's row, and its audit trail")
    pool_show.add_argument("subscription", help="the subscription id — the member's handle")
    _operator_flags(pool_show)

    pool_revoke = pool_verbs.add_parser(
        "revoke",
        help="retire a contribution: immediately ineligible, in-flight bursts finish; requires a reason",
    )
    pool_revoke.add_argument("subscription", help="the subscription id — the member's handle")
    pool_revoke.add_argument("--reason", required=True, help="why — it lands in the audit row")
    _operator_flags(pool_revoke)

    pool_renew = pool_verbs.add_parser(
        "renew",
        help="a contribution continues: a new expiry on the same ref (or a new one) — and the way back from expired/revoked",
    )
    pool_renew.add_argument("subscription", help="the subscription id — the member's handle")
    pool_renew.add_argument("--expires", required=True, metavar="DATE", help="the new expiry: a date (YYYY-MM-DD) or an ISO timestamp")
    pool_renew.add_argument("--ref", default=None, metavar="REF", help="where the re-materialized token lives now, if it moved")
    pool_renew.add_argument("--reason", default="", help="lands in the audit row")
    _operator_flags(pool_renew)

    pool_set = pool_verbs.add_parser(
        "set",
        help=f"change a live contribution — {', '.join(SUBSCRIPTION_SET_COLUMNS)}; audited",
    )
    pool_set.add_argument("subscription", help="the subscription id — the member's handle")
    pool_set.add_argument("--cap", type=int, default=None, metavar="TOKENS", help="the new ceiling, tokens per rolling window")
    pool_set.add_argument("--expires", default=None, metavar="DATE", help="the new expiry — renewal is re-materializing at the same ref and moving this")
    pool_set.add_argument("--ref", default=None, metavar="REF", help="where the token is materialized now — still a ref, never a value")
    pool_set.add_argument("--reason", default="", help="lands in the audit row")
    _operator_flags(pool_set)

    role = sub.add_parser(
        "role",
        help="roles as rows (TO-BE §4, D5): list/show/set — the open fields of a role, audited",
    )
    role_verbs = role.add_subparsers(dest="verb", required=True)
    role_list = role_verbs.add_parser("list", help="every role row: the four pipeline slots first")
    _operator_flags(role_list)
    role_show = role_verbs.add_parser("show", help="one role row and its audit trail")
    role_show.add_argument("role")
    _operator_flags(role_show)
    role_set = role_verbs.add_parser(
        "set",
        help=f"edit the open fields — {', '.join(ROLE_SET_COLUMNS)}; the wall-bearing ones refuse",
    )
    role_set.add_argument("role")
    role_set.add_argument("--preamble", default=None, help="prompt preamble, composed after the image's role preamble ('' clears)")
    role_set.add_argument("--preamble-file", type=Path, default=None, help="read the preamble from a file")
    role_set.add_argument("--model", default=None, help="the model this role's bursts run on")
    role_set.add_argument(
        "--effort",
        choices=[*EFFORT_LEVELS, "default"],
        default=None,
        help="how hard the model thinks; 'default' leaves it to the CLI's default for the model",
    )
    role_set.add_argument("--budget", type=float, default=None, help="max_budget_usd — the SDK's hard stop per burst")
    role_set.add_argument("--turns", type=int, default=None, help="max_turns per burst")
    role_set.add_argument("--mount", choices=["rw", "ro"], default=None, help="repo mount; readers stay ro whatever the row says")
    role_set.add_argument("--reason", default="", help="lands in the audit row")
    _operator_flags(role_set)

    return parser


def _backfill(args: argparse.Namespace) -> int:
    """Move the experiment's history into whatever the toggles now point at.

    The source is always the single-node pair under --data-dir; the destination is
    whatever `LEDGER_DSN` / `ARCHIVE_URL` name, so pointing at nothing is a mistake
    worth catching here rather than discovering as a no-op copy onto itself.
    """
    data_dir = args.data_dir.expanduser().resolve()
    if not os.environ.get(LEDGER_DSN_ENV, "").strip():
        print(f"error: set {LEDGER_DSN_ENV} — the backfill's destination is the service ledger",
              file=sys.stderr)
        return 2

    destination = open_ledger(data_dir)
    source_archive = LocalSessionArchive(data_dir)
    destination_archive = open_archive(data_dir)
    try:
        plan = run_backfill(
            data_dir,
            args.repository,
            destination,
            source_archive,
            destination_archive,
            dry_run=args.dry_run,
        )
    except (BackfillError, ArchiveError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    finally:
        destination.close()

    print(plan.render())
    if args.dry_run:
        print("\n(dry run — nothing was written)")
    return 0


# --- the product registry (C1) ---
#
# The operator verbs from TO-BE §2. The CLI's whole job is to *materialize* the
# row — every policy/quota column filled from the platform's .env-shaped defaults
# unless a flag overrides it — and to hand it to the ledger, which writes the
# mutation, its audit row and its outbox event as one. No NULL-means-inherit:
# after registration the row is the entire truth about the product, and a changed
# platform default never retro-propagates.

_REPOSITORY_SHAPE = re.compile(r"^[^/\s]+/[^/\s]+$")  # 'owner/name' — the spelling tasks use
_TEST_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")  # boring env names only — no smuggling


def _parse_test_env(pairs: list[str]) -> dict[str, str]:
    refs: dict[str, str] = {}
    for pair in pairs:
        name, sep, ref = pair.partition("=")
        if not sep or not _TEST_ENV_NAME.match(name):
            raise RegistryError(f"--test-env wants NAME=ref with an env-shaped NAME, got {pair!r}")
        try:
            parse_ref(ref)  # a name, never a value — the resolver's own grammar (D4)
        except SecretError as exc:
            raise RegistryError(str(exc)) from exc
        refs[name] = ref
    return refs


def _check_quotas(concurrency: int | None, daily_bursts: int | None, usage_share: float | None) -> None:
    if concurrency is not None and concurrency < 1:
        raise RegistryError(f"concurrency must be at least 1, got {concurrency}")
    if daily_bursts is not None and daily_bursts < 1:
        raise RegistryError(f"daily bursts must be at least 1, got {daily_bursts}")
    if usage_share is not None and usage_share <= 0:
        raise RegistryError(f"usage share must be positive, got {usage_share}")


def _review_columns(
    toggle: str | None, providers: str | None, base: RepositoryRecord | None = None
) -> tuple[bool, str]:
    """The review columns, materialized. Toggle and selector are orthogonal —
    turning review off keeps the curated selector, exactly as `REVIEW=off`
    leaves `$REVIEW_PROVIDERS` standing in the env world.

    Sources, in precedence order: the flag, then the row being changed (`set`),
    then the platform env (`register`). Validation routes through
    ``ReviewPolicy.declared`` — an incoherent declaration must not become a row.
    Whether *this machine* holds the provider's credentials is deliberately not
    asked here: that is `from_env`'s startup question, and C2 relocates it to
    registration preflight.
    """
    if toggle is None:
        if base is not None:
            toggle = "on" if base.review_enabled else "off"
        else:
            toggle = (os.environ.get(REVIEW_ENV) or "off").strip().lower() or "off"
    if toggle not in ("on", "off"):
        raise ReviewPolicyError(f"{REVIEW_ENV} must be `on` or `off`, got {toggle!r}")
    if providers is None:
        providers = base.review_provider if base is not None else os.environ.get(PROVIDERS_ENV, "")
    # names are checked whenever any are declared; `on` additionally needs at
    # least one — the same questions ReviewPolicy.declared asks, asked through it
    if toggle == "on" or providers.strip():
        policy = ReviewPolicy.declared({REVIEW_ENV: "on", PROVIDERS_ENV: providers})
        selector = ",".join(policy.providers)
    else:
        selector = ""
    return toggle == "on", selector


def _describe_review(row: RepositoryRecord) -> str:
    return f"on ({row.review_provider})" if row.review_enabled else "off"


def _preflight_api(repository: str) -> GitHubApi:
    """Preflight's eye on the product repo: the org App's own installation
    token, so "the App can read the repo" and "the manifest is fetchable" are
    one fact, not two checks that can disagree."""
    creds = AppCredentials.from_env()
    if creds is None:
        raise PreflightError(
            "preflight needs the GitHub App (GITHUB_APP_ID / GITHUB_APP_PRIVATE_KEY_PATH) — "
            "it proves the App reads the repo before a declaration becomes a row"
        )
    return GitHubApi(repository, InstallationTokenCache(creds))


def _with_lint(reason: str, warnings: list[str]) -> str:
    """TO-BE §2: socket lint is recorded in the registration's audit row — the
    operator's reason keeps its own words, the lint rides behind them."""
    if not warnings:
        return reason
    lint = "lint: " + "; ".join(warnings)
    return f"{reason} [{lint}]" if reason else f"[{lint}]"


def _file_lint_issue(api, repository: str, warnings: list[str]) -> None:
    """`--lint-issue` (TO-BE §2): the socket gaps become the product's own
    backlog. Filed with `human` provenance — deliberate: the content is the
    platform's deterministic checks, relayed on the operator's explicit flag;
    the operator is the author, the CLI just types (GITHUB-INTERFACE.md)."""
    if not warnings:
        print("socket clean — no lint issue to file")
        return
    body = (
        "Registration preflight (`control-plane product check`) found gaps in this "
        "repo's platform socket. None of them blocks the platform — it works the "
        "repo conservatively — but each one filled makes the service better:\n\n"
        + "\n".join(f"- {w}" for w in warnings)
        + "\n\n_Filed by the platform operator via `product … --lint-issue` (TO-BE §2)._"
    )
    number = api.create_issue(f"Platform socket: {len(warnings)} lint gap(s)", body, ["human"])
    print(f"lint issue filed: {repository}#{number}")


def _print_product(row: RepositoryRecord) -> None:
    refs = " ".join(f"{name}={ref}" for name, ref in sorted(row.test_env_refs.items())) or "—"
    print(f"  team:       {row.team}")
    print(f"  state:      {row.state}")
    print(f"  review:     {_describe_review(row)}")
    print(f"  e2e:        {'on' if row.e2e_enabled else 'off'}  refs: {refs}")
    print(
        f"  quotas:     concurrency={row.concurrency} daily_bursts={row.daily_bursts} "
        f"usage_share={row.usage_share}"
    )
    print(f"  registered: {row.registered_at}   updated: {row.updated_at}")
    print(f"  last burst: {row.last_burst_at or '—'}")


def _product_register(args: argparse.Namespace, ledger, actor: str, api_factory) -> int:
    if not _REPOSITORY_SHAPE.match(args.repository):
        raise RegistryError(f"a product is owner/name, got {args.repository!r}")
    _check_quotas(args.concurrency, args.daily_bursts, args.usage_share)
    review_enabled, review_provider = _review_columns(args.review, args.review_providers)
    env = dict(os.environ)
    if args.e2e is not None:
        env[E2E_ENV] = args.e2e
    row = RepositoryRecord(
        repository=args.repository,
        team=args.team,
        review_enabled=review_enabled,
        review_provider=review_provider,
        e2e_enabled=E2EPolicy.from_env(env).enabled,
        test_env_refs=_parse_test_env(args.test_env),
        concurrency=args.concurrency,
        daily_bursts=(
            args.daily_bursts
            if args.daily_bursts is not None
            else Rails.from_env().daily_burst_ceiling
        ),
        usage_share=args.usage_share,
    )
    # C2: the gate runs before the write — a declaration the platform cannot
    # execute never becomes a row; the lint it returns rides the audit reason.
    api = api_factory(args.repository)
    warnings = run_preflight(row, api)
    ledger.register_repository(row, actor, _with_lint(args.reason, warnings))
    suffix = f" — {len(warnings)} socket gap(s)" if warnings else ""
    print(f"{args.repository} registered{suffix}")
    for warning in warnings:
        print(f"  ⚠ {warning}")
    _print_product(ledger.get_repository(args.repository))
    if args.lint_issue:
        _file_lint_issue(api, args.repository, warnings)
    return 0


def _product_show(args: argparse.Namespace, ledger) -> int:
    row = ledger.get_repository(args.repository)
    if row is None:
        raise RegistryError(f"{args.repository} is not registered")
    print(row.repository)
    _print_product(row)
    print("audit:")
    for entry in ledger.audit_trail(args.repository):
        reason = f" — {entry.reason}" if entry.reason else ""
        print(f"  #{entry.id} {entry.at} {entry.actor} {entry.action}{reason}")
    return 0


# columns whose change can shape the same false declaration `register` refuses —
# `set` re-asks the gate for exactly these, and stays offline for the rest
_PREFLIGHT_COLUMNS = ("review_enabled", "review_provider", "e2e_enabled", "test_env_refs")


def _product_set(args: argparse.Namespace, ledger, actor: str, api_factory) -> int:
    current = ledger.get_repository(args.repository)
    if current is None:
        raise RegistryError(f"{args.repository} is not registered")
    changes: dict = {}
    if args.team is not None:
        changes["team"] = args.team
    if args.review is not None or args.review_providers is not None:
        changes["review_enabled"], changes["review_provider"] = _review_columns(
            args.review, args.review_providers, base=current
        )
    if args.e2e is not None:
        changes["e2e_enabled"] = args.e2e == "on"
    if args.test_env:
        changes["test_env_refs"] = _parse_test_env(args.test_env)
    _check_quotas(args.concurrency, args.daily_bursts, args.usage_share)
    for column, value in (
        ("concurrency", args.concurrency),
        ("daily_bursts", args.daily_bursts),
        ("usage_share", args.usage_share),
    ):
        if value is not None:
            changes[column] = value
    warnings: list[str] = []
    if set(changes) & set(_PREFLIGHT_COLUMNS):
        # the gate, re-asked over the row this set would produce. Lint is
        # advisory here — recording it stays registration's and `check`'s.
        warnings = run_preflight(replace(current, **changes), api_factory(args.repository))
    ledger.update_repository(args.repository, changes, actor, args.reason)
    print(f"{args.repository} set: {', '.join(sorted(changes))}")
    for warning in warnings:
        print(f"  ⚠ {warning}")
    _print_product(ledger.get_repository(args.repository))
    return 0


def _product_check(args: argparse.Namespace, ledger, api_factory) -> int:
    """`product check` (TO-BE §2): registration preflight, re-run on demand.

    A read, not a mutation — it writes nothing; drift lands in the exit code
    and on the operator's terminal, and reaches the audit trail only through
    the action it prompts (`product set`, `pause`)."""
    row = ledger.get_repository(args.repository)
    if row is None:
        raise RegistryError(f"{args.repository} is not registered")
    api = api_factory(args.repository)
    try:
        warnings = run_preflight(row, api)
    except PreflightError as exc:
        print(f"{args.repository}: NOT operable — {exc}", file=sys.stderr)
        return 1
    if warnings:
        print(f"{args.repository}: operable — {len(warnings)} socket gap(s)")
        for warning in warnings:
            print(f"  ⚠ {warning}")
    else:
        print(f"{args.repository}: operable — socket clean")
    if args.lint_issue:
        _file_lint_issue(api, args.repository, warnings)
    return 0


def _product(args: argparse.Namespace, api_factory=_preflight_api) -> int:
    actor = args.actor or getpass.getuser()
    ledger = open_ledger(args.data_dir.expanduser().resolve())
    try:
        if args.verb == "register":
            return _product_register(args, ledger, actor, api_factory)
        if args.verb == "list":
            rows = ledger.list_repositories()
            if not rows:
                print("no products registered")
            for row in rows:
                print(
                    f"{row.repository}  {row.state}  team={row.team}  "
                    f"review={_describe_review(row)}  e2e={'on' if row.e2e_enabled else 'off'}  "
                    f"concurrency={row.concurrency}  daily={row.daily_bursts}  share={row.usage_share}"
                )
            return 0
        if args.verb == "show":
            return _product_show(args, ledger)
        if args.verb == "check":
            return _product_check(args, ledger, api_factory)
        if args.verb == "pause":
            ledger.set_repository_state(args.repository, ProductState.PAUSED, actor, args.reason)
            print(f"{args.repository} paused: {args.reason}")
            return 0
        if args.verb == "resume":
            ledger.set_repository_state(args.repository, ProductState.ACTIVE, actor, args.reason)
            print(f"{args.repository} active")
            return 0
        if args.verb == "set":
            return _product_set(args, ledger, actor, api_factory)
        return 2
    except (
        RegistryError,
        ReviewPolicyError,
        E2EPolicyError,
        PreflightError,
        GitHubAuthError,
        GitHubApiError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    finally:
        ledger.close()


def _print_subscription(row: SubscriptionRecord) -> None:
    backoff = f"   backing off until {row.backoff_until}" if row.backoff_until else ""
    print(f"  member:     {row.member}")
    print(f"  status:     {row.status}{backoff}")
    print(f"  ref:        {row.secret_ref}   (a name — the value never reaches the platform)")
    print(f"  cap:        {row.cap_tokens:,} tokens per window")
    print(f"  expires:    {row.expires_at}")
    print(f"  registered: {row.registered_at}")


def _subscription_set(args: argparse.Namespace, ledger, actor: str) -> int:
    changes: dict = {}
    if args.cap is not None:
        changes["cap_tokens"] = check_cap(args.cap)
    if args.expires is not None:
        changes["expires_at"] = check_expiry(args.expires)
    if args.ref is not None:
        changes["secret_ref"] = check_ref(args.ref)
    ledger.update_subscription(args.subscription, changes, actor, args.reason)  # checks the columns by name
    print(f"{args.subscription} set: {', '.join(sorted(changes))}")
    _print_subscription(ledger.get_subscription(args.subscription))
    return 0


def _subscription(args: argparse.Namespace) -> int:
    """The pool verbs (F1). No GitHub, and no secret: what goes in is a ref.

    Every refusal here is deliberately mute about its input — an operator who
    pastes a token where a ref belongs must not see it echoed into their
    terminal, their shell history's neighbour, or this process's log.
    """
    actor = args.actor or getpass.getuser()
    ledger = open_ledger(args.data_dir.expanduser().resolve())
    try:
        if args.verb == "register":
            row = declare(args.member, args.ref, args.cap, args.expires)
            ledger.register_subscription(row, actor, args.reason)
            print(f"{row.subscription_id} contributes a subscription")
            _print_subscription(ledger.get_subscription(row.subscription_id))
            return 0
        if args.verb == "list":
            rows = ledger.list_subscriptions()
            if not rows:
                print("no subscriptions contributed")
            for row in rows:
                print(
                    f"{row.subscription_id}  {row.status}  member={row.member}  ref={row.secret_ref}  "
                    f"cap={row.cap_tokens}  expires={row.expires_at}"
                )
            return 0
        if args.verb == "show":
            row = ledger.get_subscription(args.subscription)
            if row is None:
                raise SubscriptionError(f"{args.subscription} is not a registered subscription")
            print(row.subscription_id)
            _print_subscription(row)
            print("audit:")
            for entry in ledger.audit_trail(args.subscription):
                reason = f" — {entry.reason}" if entry.reason else ""
                print(f"  #{entry.id} {entry.at} {entry.actor} {entry.action}{reason}")
            return 0
        if args.verb == "revoke":
            ledger.set_subscription_status(
                args.subscription, SubscriptionStatus.REVOKED, actor, args.reason
            )
            print(
                f"{args.subscription} revoked: {args.reason} — ineligible from now; "
                f"deleting the secret at its source is the member's own lever"
            )
            return 0
        if args.verb == "renew":
            ledger.renew_subscription(
                args.subscription, check_expiry(args.expires), actor, args.reason,
                secret_ref=check_ref(args.ref) if args.ref else None,
            )
            print(f"{args.subscription} renewed — active until {check_expiry(args.expires)}")
            _print_subscription(ledger.get_subscription(args.subscription))
            return 0
        if args.verb == "set":
            return _subscription_set(args, ledger, actor)
        return 2
    except SubscriptionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    finally:
        ledger.close()


def _print_role(row: RoleRecord) -> None:
    tools = "all (no narrowing)" if row.tools == ["*"] else ", ".join(row.tools)
    preamble = repr(row.prompt_preamble) if row.prompt_preamble else "—"
    print(f"  kind:      {row.kind}   identity: {row.identity}   sink: {row.sink}   (frozen)")
    print(f"  tools:     {tools}   (frozen)")
    print(f"  model:     {row.model}   effort: {row.effort or 'default'}")
    print(f"  budget:    ${row.max_budget_usd:.2f}   turns: {row.max_turns}   mount: {row.mount_mode}")
    print(f"  preamble:  {preamble}")
    print(f"  created:   {row.created_at}   updated: {row.updated_at}")


def _role_set(args: argparse.Namespace, ledger, actor: str) -> int:
    changes: dict = {}
    if args.preamble is not None and args.preamble_file is not None:
        raise RoleError("--preamble and --preamble-file are one setting — pass one")
    if args.preamble is not None:
        changes["prompt_preamble"] = args.preamble
    if args.preamble_file is not None:
        changes["prompt_preamble"] = args.preamble_file.read_text(encoding="utf-8")
    if args.effort is not None:
        changes["effort"] = None if args.effort == "default" else args.effort  # NULL: the CLI's default
    for column, value in (
        ("model", args.model),
        ("max_budget_usd", args.budget),
        ("max_turns", args.turns),
        ("mount_mode", args.mount),
    ):
        if value is not None:
            changes[column] = value
    ledger.update_role(args.role, changes, actor, args.reason)  # checks the fields by name
    print(f"{args.role} set: {', '.join(sorted(changes))} — governs the next burst")
    _print_role(ledger.get_role(args.role))
    return 0


def _role(args: argparse.Namespace) -> int:
    """The role verbs (D5). No GitHub is involved: a role is engine data."""
    actor = args.actor or getpass.getuser()
    ledger = open_ledger(args.data_dir.expanduser().resolve())
    try:
        if args.verb == "list":
            for row in ledger.list_roles():
                print(
                    f"{row.role}  {row.kind}  model={row.model}  effort={row.effort or 'default'}  "
                    f"budget=${row.max_budget_usd:.2f}  "
                    f"turns={row.max_turns}  mount={row.mount_mode}  "
                    f"preamble={'yes' if row.prompt_preamble.strip() else 'no'}"
                )
            return 0
        if args.verb == "show":
            row = ledger.get_role(args.role)
            if row is None:
                raise RoleError(f"no role named {args.role!r}")
            print(row.role)
            _print_role(row)
            print("audit:")
            for entry in ledger.audit_trail(args.role):
                reason = f" — {entry.reason}" if entry.reason else ""
                print(f"  #{entry.id} {entry.at} {entry.actor} {entry.action}{reason}")
            return 0
        if args.verb == "set":
            return _role_set(args, ledger, actor)
        return 2
    except (RoleError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    finally:
        ledger.close()


def _roles_executable(stores: PlatformStores) -> str | None:
    """Startup's role check (D5): the retired env knobs are gone from this
    machine, and every pipeline row the engine will spawn is one it can run.
    Returns the refusal, or None."""
    stale = retired_settings(os.environ)
    if stale:
        return "retired settings still set — unset them, the role row governs:\n  " + "\n  ".join(stale)
    for role in PIPELINE_ROLES:
        row = stores.ledger.get_role(role)
        if row is None:
            return f"the ledger has no {role!r} role row — the pipeline roles are seeded (0002)"
        try:
            validate_role(row)
        except RoleError as exc:
            return str(exc)
    return None


def _github_check() -> int:
    """Preflight every configured pipeline role (D1, TO-BE §4): mint a real
    installation token per role-prefixed App and show what it can do. A role
    that is declared but can't mint fails the check; an unconfigured role is
    reported and skipped — which Apps exist is the adopter's rollout state."""
    minted = 0
    failures = []
    for role, prefix in ROLE_PREFIXES.items():
        try:
            creds = AppCredentials.from_env(prefix=prefix)
        except GitHubAuthError as exc:
            print(f"{role}: error: {exc}", file=sys.stderr)
            failures.append(role)
            continue
        if creds is None:
            print(f"{role}: not configured ({prefix}GITHUB_APP_ID / {prefix}GITHUB_APP_PRIVATE_KEY_PATH)")
            continue
        try:
            token = mint_installation_token(creds)
        except GitHubAuthError as exc:
            print(f"{role}: error: {exc}", file=sys.stderr)
            failures.append(role)
            continue
        minted += 1
        name, email = bot_git_identity(token, creds.app_id)
        print(f"{role}:")
        print(f"  app:          {token.app_slug}")
        print(f"  bot identity: {name} <{email}>")
        print(f"  token:        {token.token[:11]}… expires {token.expires_at}")
        print(f"  permissions:  {token.permissions}")
        forbidden = {"administration", "merge_queues"} & set(token.permissions)
        if forbidden:
            print(f"  ⚠ unexpected permissions present: {forbidden}")
    if failures:
        print(f"error: roles failed the check: {', '.join(failures)}", file=sys.stderr)
        return 1
    if not minted:
        print("no App configured (GITHUB_APP_ID / GITHUB_APP_PRIVATE_KEY_PATH unset)")
        return 2
    return 0


def _run_burst(args: argparse.Namespace) -> int:
    repo = args.repo.expanduser().resolve()
    if not repo.is_dir():
        print(f"error: --repo is not a directory: {repo}", file=sys.stderr)
        return 2
    prompt = args.prompt
    if args.prompt_file:
        prompt = args.prompt_file.read_text(encoding="utf-8")
    if args.mode == "run" and not prompt:
        print("error: a run burst needs --prompt or --prompt-file", file=sys.stderr)
        return 2

    slug = args.github or slug_from_origin(repo)
    if not slug:
        print(
            "error: pass --github owner/name (the clone's origin names no GitHub repo)",
            file=sys.stderr,
        )
        return 2

    init_telemetry()
    stores = PlatformStores.open(args.data_dir)
    refusal = _roles_executable(stores)
    if refusal:
        print(f"error: {refusal}", file=sys.stderr)
        return 2
    # Derived, not typed: the same id the scheduler would compute for this issue,
    # so a hand-run burst joins the same task rather than making a second one.
    task_id = task_id_for(slug, args.issue)
    task = stores.ledger.get_task(task_id) or TaskRecord(
        task_id=task_id, repository=slug, issue_number=args.issue
    )
    try:
        outcome = execute_burst(
            stores,
            task,
            repo,
            prompt,
            mode=args.mode,
            image=args.image,
            model=args.model,  # each None → the dev role row (D5)
            max_budget_usd=args.budget,
            max_turns=args.max_turns,
            fresh=args.fresh,
            with_github=True,  # the manual burst mirrors a dev burst
        )
    except (GitHubAuthError, RoleError, RegistryError, ArchiveError) as exc:
        # RegistryError: the hand-run verb met the registry wall — on the service
        # ledger a task names a registered product (#90). ArchiveError: the
        # session node the ledger named is gone or does not match (#87). Both are
        # refusals a person reads, not stacks they decipher.
        print(f"error: {exc}", file=sys.stderr)
        return 2
    trace.get_tracer_provider().shutdown()  # type: ignore[attr-defined]
    return outcome.exit_code


def _scheduler_policies(
    row: RepositoryRecord | None, environ: dict[str, str] | None = None
) -> tuple[ReviewPolicy, E2EPolicy]:
    """C3: a registered product's policies come from its row — the row is the
    entire truth about the declaration, and the env answers only what stays a
    machine question (credentials, model overrides). `from_env` is dev mode:
    an unregistered repo on the SQLite ledger still runs env-governed until
    C4 makes the scheduler registry-driven."""
    if row is None:
        return ReviewPolicy.from_env(environ), E2EPolicy.from_env(environ)
    return ReviewPolicy.from_product(row, environ), E2EPolicy.from_product(row, environ)


def _serve(engine, interval: int, once: bool) -> int:
    """The loop both modes share: tick, sleep, repeat — until this process is
    told to shut down. ``engine`` is a Scheduler (one product) or a Fleet (C4).

    SIGTERM/SIGINT set the engine's ``halt`` (E3): the live burst's heartbeat
    reads it and the runner kills the container, the tick completes, the loop
    exits. That flag is this process's own — a restart is not an org STOP, so
    nothing is written to the ledger. The org's STOP is the engine's business:
    a stopped org makes every tick a no-op and the loop keeps ticking, so
    `resume` takes effect here without anyone restarting anything.

    The sleep between ticks is a wait on the engine's ``wake`` flag (E5), so a
    webhook's wake — set by this process's endpoint or by the ledger's channel
    when a peer's took it — ends it early and the next tick polls the woken
    product now. `attach_wakes` wires the flag before this is called.
    """

    def _shutdown(signum, frame):  # noqa: ARG001 — signal handler shape
        engine.halt.set()
        engine.wake.set()  # cut the sleep, if that is where the loop is
        log.info("shutdown requested — the live burst is fenced by its heartbeat; exiting after this tick")

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    while True:
        try:
            engine.tick()
        except (GitHubApiError, GitHubAuthError) as exc:
            log.warning("github error — retrying next tick", error=str(exc))
        if engine.halt.is_set():
            log.info("shutting down")
            break
        if once:
            break
        # the sleep: the interval, or sooner — a wake (E5: a webhook reached this
        # replica's endpoint or a peer's) or a shutdown sets the flag
        if engine.wake.wait(interval):
            engine.wake.clear()
            # a wake settles before it is looked at (E6): GitHub delivered the
            # webhook before its read side shows the write, and a poll now would
            # spend the observation lease on the old answer for a whole interval.
            # The ledger reserved the observation for as long; `halt` cuts this too.
            engine.halt.wait(WAKE_SETTLE_S)
        if engine.halt.is_set():
            log.info("shutting down")
            break
    provider = trace.get_tracer_provider()
    if hasattr(provider, "shutdown"):  # a proxy provider (telemetry never initialized) has none
        provider.shutdown()
    return 0


def _task_trees(stores, clones_root: Path, slug: str, token_cache):
    """The scheduler's two sync callables for one product (H3): each burst gets
    its task's own tree, and every sync first sweeps the trees of tasks the
    ledger says are done — so a host keeps one tree per live task."""

    def finished(issue_number: int) -> bool:
        task = stores.ledger.get_task(task_id_for(slug, issue_number))
        return task is None or task.status == TaskStatus.DONE

    def sync_clone(task: TaskRecord) -> Path:
        prune_trees(clones_root, slug, finished)
        return prepare_clone(clones_root, slug, task.issue_number, token_cache())

    def sync_clone_ref(task: TaskRecord, ref: str) -> Path:
        prune_trees(clones_root, slug, finished)
        return prepare_clone(clones_root, slug, task.issue_number, token_cache(), ref=ref)

    return sync_clone, sync_clone_ref


def _run_fleet(
    args: argparse.Namespace,
    stores: PlatformStores,
    token_cache: InstallationTokenCache,
    copilot_token: str | None,
    rails: Rails,
    interval: int,
    clones_root: Path,
) -> int:
    """C4: serve every active product in the registry through one serial loop.

    Startup refuses if any active declaration can't be honored on this machine
    (the C3 honor-check, per row) — after that the Fleet re-reads the registry
    every tick, so register/pause/set take effect without a restart. One shared
    token cache: the org App has one installation here; per-org installations
    arrive with the TO-BE's org-wide Apps.
    """
    rows = [row for row in stores.ledger.list_repositories() if row.state == ProductState.ACTIVE]
    if not rows:
        print(
            "error: no active products registered — `control-plane product register` one, "
            "or pass --github owner/name for the env-governed dev mode",
            file=sys.stderr,
        )
        return 2
    policies: dict[str, tuple[ReviewPolicy, E2EPolicy]] = {}
    for row in rows:
        try:
            policies[row.repository] = _scheduler_policies(row)
        except (ReviewPolicyError, E2EPolicyError) as exc:
            print(f"error: {row.repository}: {exc}", file=sys.stderr)
            return 2
        review, testing = policies[row.repository]
        if testing.enabled and not review.claude:
            # preflighted at registration (C2); re-asserted because the funnel
            # (TESTER.md) is a startup invariant, not a registration artifact
            print(
                f"error: {row.repository}: e2e on requires the claude review provider "
                "(tests run after a clean assess)",
                file=sys.stderr,
            )
            return 2

    # Mint the reviewer App whenever it is configured — not only when a current
    # row declares claude — so a claude product registered mid-run finds it live.
    needs_claude = any(policies[row.repository][0].claude for row in rows)
    reviewer_cache = reviewer_login = None
    try:
        reviewer_creds = AppCredentials.from_env(prefix=REVIEWER_APP_PREFIX)
        if reviewer_creds is not None:
            minted = mint_installation_token(reviewer_creds)
            reviewer_login = minted.bot_login
            reviewer_cache = InstallationTokenCache(reviewer_creds)
    except GitHubAuthError as exc:
        if needs_claude:
            print(f"error: reviewer App: {exc}", file=sys.stderr)
            return 2
        log.warning("reviewer App not usable — claude reviews would degrade", error=str(exc))
    if needs_claude and reviewer_cache is None:
        print(
            "error: a product declares the claude provider but the reviewer App is not configured",
            file=sys.stderr,
        )
        return 2

    # Same for the tester App (D3): reports and auto issues post as its identity.
    # E2EPolicy already refuses e2e-on without the credentials; this catches
    # credentials that exist but cannot mint (revoked key, no installation).
    needs_tester = any(policies[row.repository][1].enabled for row in rows)
    tester_cache = tester_login = None
    try:
        tester_creds = AppCredentials.from_env(prefix=TESTER_APP_PREFIX)
        if tester_creds is not None:
            minted = mint_installation_token(tester_creds)
            tester_login = minted.bot_login
            tester_cache = InstallationTokenCache(tester_creds)
    except GitHubAuthError as exc:
        if needs_tester:
            print(f"error: tester App: {exc}", file=sys.stderr)
            return 2
        log.warning("tester App not usable — test reports would degrade", error=str(exc))
    if needs_tester and tester_cache is None:
        print(
            "error: a product declares e2e but the tester App is not configured",
            file=sys.stderr,
        )
        return 2

    runner = open_runner(stores.archive)  # where this replica's bursts run (E4) — one for the process

    def build_scheduler(row: RepositoryRecord) -> Scheduler:
        slug = row.repository
        review, testing = _scheduler_policies(row)  # honor-checked every build (C3)
        api = GitHubApi(slug, token_cache, copilot_token=copilot_token)
        reviewer_api = (
            GitHubApi(slug, reviewer_cache)
            if review.claude and reviewer_cache is not None
            else None
        )
        tester_api = (
            GitHubApi(slug, tester_cache) if testing.enabled and tester_cache is not None else None
        )

        sync_clone, sync_clone_ref = _task_trees(stores, clones_root, slug, token_cache)

        return Scheduler(
            stores,
            api,
            cache_dir(clones_root, slug),  # never mounted: every burst gets its task's tree
            args.owner or api.repo_owner(),
            # the row's quotas: its daily ceiling and, E2, its concurrency —
            # counted at admission across every replica of the fleet
            rails=replace(rails, daily_burst_ceiling=row.daily_bursts, concurrency=row.concurrency),
            review=review,
            testing=testing,
            reviewer_api=reviewer_api,
            reviewer_login=reviewer_login,
            tester_api=tester_api,
            tester_login=tester_login,
            image=args.image,
            sync_clone=sync_clone,
            sync_clone_ref=sync_clone_ref,
            runner=runner,
        )

    log.info(
        "fleet starting",
        products=[row.repository for row in rows],
        interval_s=interval,
        rails=str(rails),
    )
    for row in rows:
        review, testing = policies[row.repository]
        log.info(
            "switchboard",
            repo=row.repository,
            review=review.describe(),
            e2e=testing.describe(),
            tester=tester_login,
            daily_bursts=row.daily_bursts,
            policy_source="registry row",
        )
    fleet = Fleet(stores, build_scheduler)
    # WAKE_BIND: this replica's internal endpoint; the ledger's wake channel
    # regardless. A second ledger handle: the endpoint writes from its own thread
    attach_wakes(fleet, open_ledger(stores.data_dir))
    return _serve(fleet, interval, args.once)


def _run_scheduler(args: argparse.Namespace) -> int:
    slug = args.github
    if not slug and args.repo is not None:
        slug = slug_from_origin(args.repo.expanduser().resolve())
    try:
        creds = AppCredentials.from_env()
    except GitHubAuthError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if creds is None:
        print("error: the scheduler needs the GitHub App configured (it polls as the App)", file=sys.stderr)
        return 2
    init_telemetry()
    stores = PlatformStores.open(args.data_dir)
    # a stopped org is not a refusal to start (E3): the loop comes up and stands
    # down every tick until `resume` — a supervisor restarting replicas under a
    # STOP must not crash-loop them. The tick logs the stop in force.
    refusal = _roles_executable(stores)  # D5: the rows the engine spawns from
    if refusal:
        print(f"error: {refusal}", file=sys.stderr)
        return 2
    token_cache = InstallationTokenCache(creds)
    copilot_token = os.environ.get(COPILOT_TOKEN_ENV, "").strip() or None
    rails = Rails.from_env()
    interval = args.interval if args.interval is not None else rails.poll_interval_s
    # the observation lease (E2) runs one interval, so the rails carry the
    # interval the loop actually sleeps — `--interval` included
    rails = replace(rails, poll_interval_s=interval)
    clones_root = stores.data_dir / "clones"
    if slug is None:
        # C4: no --github names no product — the registry does
        return _run_fleet(args, stores, token_cache, copilot_token, rails, interval, clones_root)
    product_row = stores.ledger.get_repository(slug)
    if product_row is None:
        # C1: the service ledger refuses tasks for unregistered products (the
        # tasks→repositories FK). Until C4 makes the scheduler registry-driven,
        # this is a warning rather than a refusal — the SQLite variant carries no
        # FK, and the env policies (dev mode) still govern behavior.
        log.warning(
            "product is not registered — on the service ledger, claiming a task will fail; "
            "run `control-plane product register`",
            repo=slug,
        )
    try:
        # a declaration that can't be honored refuses to start — from the
        # registered row (C3) or, dev mode, from the env
        review, testing = _scheduler_policies(product_row)
    except (ReviewPolicyError, E2EPolicyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if testing.enabled and not review.claude:
        # the funnel (TESTER.md): tests run only after a clean assess — without
        # the claude review provider that trigger never fires
        print("error: e2e on requires the claude review provider (tests run after a clean assess)", file=sys.stderr)
        return 2

    api = GitHubApi(slug, token_cache, copilot_token=copilot_token)

    reviewer_api = reviewer_login = None
    if review.claude:  # the claude provider posts as a second App (REVIEWERS.md)
        try:
            reviewer_creds = AppCredentials.from_env(prefix=REVIEWER_APP_PREFIX)
            minted = mint_installation_token(reviewer_creds)
        except GitHubAuthError as exc:
            print(f"error: reviewer App: {exc}", file=sys.stderr)
            return 2
        reviewer_login = minted.bot_login
        reviewer_api = GitHubApi(slug, InstallationTokenCache(reviewer_creds))

    tester_api = tester_login = None
    if testing.enabled:  # reports post as the tester App's own identity (D3)
        try:
            tester_creds = AppCredentials.from_env(prefix=TESTER_APP_PREFIX)
            minted = mint_installation_token(tester_creds)
        except GitHubAuthError as exc:
            print(f"error: tester App: {exc}", file=sys.stderr)
            return 2
        tester_login = minted.bot_login
        tester_api = GitHubApi(slug, InstallationTokenCache(tester_creds))

    clones_root = stores.data_dir / "clones"

    sync_clone, sync_clone_ref = _task_trees(stores, clones_root, slug, token_cache)

    try:
        owner = args.owner or api.repo_owner()
        # bursts mount their task's tree of this cache, never the operator's copy (#4, H3)
        platform_clone = prepare_cache(clones_root, slug, token_cache())
    except (GitHubApiError, CloneError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    scheduler = Scheduler(
        stores,
        api,
        platform_clone,
        owner,
        rails=rails,
        review=review,
        testing=testing,
        reviewer_api=reviewer_api,
        reviewer_login=reviewer_login,
        tester_api=tester_api,
        tester_login=tester_login,
        image=args.image,
        sync_clone=sync_clone,
        sync_clone_ref=sync_clone_ref,
        runner=open_runner(stores.archive),
    )
    log.info(
        "scheduler starting",
        repo=slug,
        owner=owner,
        clone=str(platform_clone),
        interval_s=interval,
        rails=str(rails),
    )
    log.info(
        "switchboard",
        review=review.describe(),
        reviewer=reviewer_login,
        e2e=testing.describe(),
        tester=tester_login,
        gate=owner,
        policy_source="registry row" if product_row is not None else "env (dev mode)",
    )

    attach_wakes(scheduler, open_ledger(stores.data_dir))  # its own handle — see _run_fleet
    return _serve(scheduler, interval, args.once)


def _stop(args: argparse.Namespace) -> int:
    """The org's kill switch (E3): one audited row in the ledger, and nothing
    else — no file, no `docker kill` from this shell. Every replica reads the row
    at the top of its tick and before its spawns, and every live burst's
    heartbeat reads it on the clock; the runner holding the burst kills the
    container, on whatever host that is. So a `stop` issued here reaches a
    burst spawned elsewhere, which a local `docker kill` never could."""
    actor = args.actor or getpass.getuser()
    ledger = open_ledger(args.data_dir.expanduser().resolve())
    try:
        try:
            ledger.stop_org(actor, args.reason)
        except RegistryError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        print(f"org stopped: {args.reason}")
        live = ledger.running_bursts()
        if live:
            holders = []
            for burst in live:
                task = ledger.get_task(burst.task_id)
                holders.append(f"{burst.burst_id} ({burst.task_id}, held by {task.claimed_by if task else '?'})")
            print(f"{len(live)} live burst(s) — fenced by their replicas within {HEARTBEAT_POLL_S:g} s: " + ", ".join(holders))
        print("every replica stands down at its next tick and keeps ticking — `control-plane resume` lifts it")
        return 0
    finally:
        ledger.close()


def _resume(args: argparse.Namespace) -> int:
    actor = args.actor or getpass.getuser()
    ledger = open_ledger(args.data_dir.expanduser().resolve())
    try:
        try:
            ledger.resume_org(actor, args.reason)
        except RegistryError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        print("org resumed — the replicas pick up on their next tick")
        for entry in ledger.audit_trail(ORG_TARGET)[-2:]:
            reason = f" — {entry.reason}" if entry.reason else ""
            print(f"  #{entry.id} {entry.at} {entry.actor} {entry.action}{reason}")
        return 0
    finally:
        ledger.close()


def _migrate(args: argparse.Namespace) -> int:
    dsn = args.dsn or os.environ.get(LEDGER_DSN_ENV, "").strip()
    if not dsn:
        print(
            f"error: no Postgres DSN — pass --dsn or set {LEDGER_DSN_ENV}. "
            "Unset means the single-node SQLite ledger, which needs no migration.",
            file=sys.stderr,
        )
        return 2
    try:
        if args.check:
            applied, todo = status(dsn)
            for version in applied:
                print(f"applied  {version}")
            for migration in todo:
                print(f"pending  {migration}")
            return 1 if todo else 0
        applied_now = migrate(dsn)
    except (MigrationError, psycopg.Error) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if applied_now:
        log.info("schema migrated", applied=len(applied_now), head=str(applied_now[-1]))
    else:
        log.info("schema up to date — nothing to apply")
    return 0


def _replay(args: argparse.Namespace) -> int:
    """`control-plane replay` (G4). Writes no ledger row and hands the trials no
    GitHub credential — a replay is a measurement, never a burst of the task."""
    from control_plane import replay  # local: the harness is an operator's verb

    try:
        golden = replay.GoldenBurst.load(args.fixture)
    except replay.ReplayError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.node:
        burst = open_ledger(args.data_dir).get_burst(args.node)
        node = Snapshot.of(burst)
        if burst is None or node is None:
            print(f"error: burst {args.node} has no archived snapshot to replay from", file=sys.stderr)
            return 2
        golden = replace(
            golden, task_id=burst.task_id, node=node, seed=None,
            resume_session_id=burst.session_id or golden.resume_session_id,
        )
    archive = open_archive(args.data_dir)
    runner = open_runner(archive)
    work = args.data_dir / "replays" / golden.name
    trials = replay.replay(golden, args.runtime, runner, archive, work)
    table = replay.matrix(golden, trials)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "matrix.json").write_text(json.dumps(table, indent=2) + "\n", encoding="utf-8")
    markdown = replay.render(table)
    (args.out / "matrix.md").write_text(markdown, encoding="utf-8")
    print(markdown)
    # a breach fails the run whatever the flags say: the M4 target is zero
    if table["errors"] or table["breaches"] or (args.strict and not table["identical"]):
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    load_dotenv()  # the platform's .env; real env vars win — bursts still get an explicit env
    configure_logging()  # console on a TTY, logfmt everywhere else (dev_agent.log)
    args = build_parser().parse_args(argv)
    if args.command == "burst":
        return _run_burst(args)
    if args.command == "role":
        return _role(args)
    if args.command == "run":
        return _run_scheduler(args)
    if args.command == "stop":
        return _stop(args)
    if args.command == "resume":
        return _resume(args)
    if args.command == "migrate":
        return _migrate(args)
    if args.command == "backfill":
        return _backfill(args)
    if args.command == "replay":
        return _replay(args)
    if args.command == "github-check":
        return _github_check()
    if args.command == "product":
        return _product(args)
    if args.command == "subscription":
        return _subscription(args)
    return 2


if __name__ == "__main__":
    sys.exit(main())
