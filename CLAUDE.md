# usf — the platform

This repo is the **platform** in the autonomous-maintenance experiment: the **control
plane** (scheduler) plus the **dev-agent** image it runs. The dev agent maintains a
*product* repo — any codebase, passed as a parameter (the experiment's first instance
is the sibling `discord-bot`) — by working like an engineer — branch, code, test, open
a PR — through GitHub.

## Engine

[`claude-agent-sdk`](https://github.com/anthropics/claude-agent-sdk-python) — Claude
Code as an embeddable Python library (agent loop, tool suite, layered permission model,
sandbox mode, sessions/resume, `max_budget_usd`). We build on these primitives rather
than reinvent them. The SDK's platform wheel bundles the `claude` CLI it drives
(`claude_agent_sdk/_bundled/claude`, preferred over anything on PATH), so the SDK
version in `uv.lock` *is* the engine version. The dev-agent image installs the lock's
versions, hash-checked (`uv export --frozen` → `pip install --require-hashes`), carries
no second CLI, and CI fails the image if its SDK is not the lock's (#120);
`dev-agent check` reports `sdk_version` / `cli_version`. Bumping the engine is a lock
change: read the SDK's changelog for what a burst relies on (the options it builds, the
message types it reads, `max_budget_usd`, the PreToolUse hook).

## Setup

```bash
uv sync                      # create the venv, install deps
cp .env.example .env         # add CLAUDE_CODE_OAUTH_TOKEN (from `claude setup-token`)
```

## Layout

Six workspace packages behind the §12 seam — `packages/control-plane` (scheduler,
owns all persistence + telemetry), `packages/dev-agent` (the burst worker; ships
alone in the image and **must never import control-plane**), `packages/webhook-receiver`
(the public HMAC filter, E5; its own image, **must never import control-plane**
either — CI checks the image), `packages/burst-phases` (the pod-side init and
harvester, G2; its own image, same rule and same check) and two stdlib-only
packages the others share: `packages/secret-refs` (SecretResolver) and
`packages/session-volume` (what a burst leaves in its volume, and how it is
packed — the one shape the worker, the control plane and the harvester must
agree on). The env-var contract lives in `dev_agent.env`; control-plane imports
it, never the reverse.

## Run

```bash
# Build the dev-agent image
docker build -f docker/dev-agent/Dockerfile -t usf/dev-agent:latest .

# Boot smoke (no network, no key): container check in a clone
uv run control-plane burst --issue 1 --repo ../discord-bot --mode check --data-dir data

# One real burst (needs CLAUDE_CODE_OAUTH_TOKEN in the control-plane env)
uv run control-plane burst --issue 1 --repo ../discord-bot \
  --prompt "Summarize the repo layout" --budget 1.0

# The M2 scheduler (SCHEDULER.md): ready issue → PR → review rounds, unattended
uv run control-plane run --repo ../discord-bot     # loop (--once for a single tick)
uv run control-plane stop --reason "…"             # kill switch: the org's STOP row — every replica stands down, live bursts are killed by their runners
uv run control-plane resume                        # lift it; the loops pick up on their next tick, no restart

# Webhooks as wake-up (E5): WAKE_BIND on the replica, the receiver in front of it
WAKE_BIND=0.0.0.0:8081 uv run control-plane run …  # the internal wake endpoint (+ GET /healthz)
WEBHOOK_APPS="<app id>=env:DEV_WEBHOOK_SECRET" WAKE_URL=http://localhost:8081 uv run webhook-receiver serve

# The platform as images + compose (E5/E6): stores + migrate + 2 replicas + receiver (+ the dev-agent image)
# DEPLOYMENT.md is the runbook: PLATFORM_DATA/PLATFORM_SECRETS pinned in docker/.env, .env.receiver for the receiver
docker compose -f docker/compose.yaml --profile platform up -d --build --wait
```

## Stores

SQLite + a local directory by default; the service backends are presence toggles,
not modes (`STORES.md`). The dev rig supplies both:

```bash
docker compose -f docker/compose.yaml up -d --wait   # Postgres + SeaweedFS
export LEDGER_DSN='postgresql://platform_admin:platform@localhost:5432/platform?sslmode=disable'
export ARCHIVE_URL='s3://platform:platform-dev-secret@localhost:8333/platform-sessions?tls=false'
uv run control-plane migrate                         # numbered SQL; idempotent
uv run control-plane migrate --check                 # exit 1 if anything is pending
```

Two toggles, two stores, and they are independent — a deployment can buy one
capability before the other. `tls=false` is the archive's `sslmode=disable`; the
bucket is created on first write if it isn't there.

`sslmode=disable` is in the DSN so the same string works for psycopg and for Atlas,
which refuses to guess. `open_ledger` is the one place a backend is chosen; nothing
else may name one.

`migrate` is the only command a fresh database needs: it creates the `platform_app`
role if the cluster has none (passwordless — the schema owns what the role may do,
the operator owns how it logs in) and grants it what the runtime needs. Those
grants are walls: `violations`, `audit` and `events` are `SELECT, INSERT` only, and
nothing anywhere gets `DELETE`. Two more walls live outside the schema's reach —
the `roles_pipeline_guard` trigger and the grants themselves — so
`tests/test_schema_walls.py` provokes every wall and requires an error; that file
is the only thing standing behind those two.

### Changing the schema

`schema.sql` states what the schema should be; the numbered migrations are the path
that gets a database there. Never write ad-hoc DDL in the store layer, and never
edit an applied migration.

```bash
# 1. edit schema.sql — the desired state
# 2. ask Atlas what would have to change (read-only; ~30s, it spins its own dev db)
atlas schema diff --env local --from "$LEDGER_DSN" --to file://schema.sql
# 3. save that SQL as the next migrations/NNNN_slug.sql, then apply it
uv run control-plane migrate
# 4. re-run step 2 — "Schemas are synced" is what CI checks
# 5. and check what the DDL *does*, not just what it declares:
uv run squawk packages/control-plane/src/control_plane/migrations/*.sql
```

`squawk` catches DDL that is correct and still stalls a live replica. Anything
added to a live table needs the two-step `NOT VALID` (one migration) +
`VALIDATE CONSTRAINT` (the next), and a `UNIQUE` wall wants `CREATE UNIQUE INDEX`
rather than `ADD CONSTRAINT`; squawk says so if you forget. Rules live in
`.squawk.toml`, each exclusion carrying the claim that justifies it; the two
timeout rules are satisfied by the runner (`LOCK_TIMEOUT` / `STATEMENT_TIMEOUT` in
`migrate.py`), so no migration has to remember them. A single statement can be
excused with `-- squawk-ignore <rule>` on the line above it, which is the right
place for it — the reason sits next to the statement and the rule stays live for
the next one.

Atlas is a development instrument, never a runtime dependency: it diffs and it
lints, `control-plane migrate` applies. It cannot see triggers, grants, seeded
rows, views, or whether a constraint is still `NOT VALID` — so it will call a
database with a forgotten `VALIDATE CONSTRAINT` synced, and it *refuses outright* a
`schema.sql` containing a `CREATE VIEW` ("views are available to logged-in users
only"), which is why B3's reporting views live in their migration with
`tests/test_reporting_views.py` behind them. Walls and definitions of that kind are
tested by provoking them, not by diffing (`ARCHITECTURE.md` §12).

**A column lands in three places, sometimes four.** The numbered migration, then
`schema.sql` (the declared state Atlas diffs), then the SQLite `_SCHEMA` in
`store.py` — and if a file created before the column has to survive it,
`_ADDITIVE_COLUMNS` beside it, with a test that opens a pre-column file. The first
three are self-enforcing: both `record_burst` and `upsert_task` build their column
lists from the dataclass, so a field with no SQLite column fails the first insert
and CI's `test` job (which runs single-node) goes red. The fourth is not — CI only
ever creates fresh files, so it is the one to remember.

### The product registry

The tenancy unit is a registered product (TO-BE §2); the verbs are C1's:

```bash
uv run control-plane product register owner/name --team bots   # defaults materialized from .env
uv run control-plane product list
uv run control-plane product show owner/name                   # the row + its audit trail
uv run control-plane product pause owner/name --reason "..."   # the reason is required
uv run control-plane product resume owner/name
uv run control-plane product set owner/name --e2e on --test-env DISCORD_TOKEN=env:TEST_DISCORD_TOKEN
uv run control-plane product check owner/name --lint-issue    # re-run preflight; file the gaps (C2)
```

Three rules carry the design. **Defaults are materialized, not inherited**:
`register` fills every policy/quota column from the platform env (validated
through `ReviewPolicy.declared` — an incoherent declaration never becomes a row),
and a changed platform default never retro-propagates — changing a live product is
`product set`, audited. **Every mutation is one write**: the row change, its
`audit` row (actor, action, target, reason) and its outbox event commit together —
a CTE on Postgres, a transaction on SQLite. **A task names a registered product**:
`tasks.repository` is a foreign key (the registry wall), so on the service ledger
`run` and `backfill` need the product registered first — migration 0012 seeded
rows for repositories that already had tasks, with placeholder policies the audit
row tells you to `product set`. Test-env values never enter the ledger: `--test-env`
takes `NAME=env:VAR` / `NAME=file:PATH` / `NAME=helper:NAME` references, resolved
at spawn by the `SecretResolver` (D4).

**Registration preflights (C2)**: `register` — and `set`, when it touches a policy
column — runs `preflight.run_preflight` before the write. The *gate* refuses a
declaration the platform cannot execute: the org App unable to read the repo, a
review provider without its credentials on this machine (the old scheduler-startup
error, relocated), `e2e on` without the claude provider or without a parseable
`TESTING.md` whose declared env every ref covers *and resolves* (`SecretResolver.probe`:
env var set / file present / helper configured — values are never read). Everything
else is *socket lint* —
`CLAUDE.md`, issue forms, PR template, commitlint config — warned and recorded in
the registration's audit row, never a refusal. `product check` re-runs the same
preflight against the live repo (exit 1 = drift: the row is no longer executable);
`--lint-issue` on `register`/`check` files the gaps as a `human` issue in the
product repo. Quota/team changes and pause/resume never probe GitHub.

**Row-sourced policy (C3)**: at scheduler startup, a registered product's review
and e2e policies come from its row (`ReviewPolicy.from_product` /
`E2EPolicy.from_product`) — the row is the entire truth about the declaration, and
a machine-level `REVIEW=off` never silently degrades a registered product. The env
keeps answering only what stays a machine question: credentials (the honor-check
fires from the row exactly as it did from env — registration preflight and startup
share the constructor). The product row has no model column on purpose — which
model a role runs is the *role* row's (D5). `from_env` remains dev mode: an unregistered repo
on the SQLite ledger runs env-governed until C4 makes the scheduler
registry-driven.

**Registry-driven scheduler (C4)**: `run` without `--github` serves every active
registered product through one serial loop (`Fleet`). The registry is re-read
every tick (register/pause/set need no restart); each product's scheduler is
built from its row, with the row's `daily_bursts` as its ceiling. At most one
burst per tick — the least recently served product first
(`repositories.last_burst_at`, stamped via `touch_repository_burst`: operational
state, no audit row). A product's scheduler reads only its own tasks and bursts
(repository-scoped ledger queries), and one product's trouble quarantines that
product for the tick, never the fleet. `--github owner/name` still pins one repo
— dev mode when unregistered. SCHEDULER.md "The fleet" has the full contract.

**Fair share, and one question per tick (H1)**: the fleet orders products by
bursts-served-per-weight over `FAIR_SHARE_WINDOW_H` (24 h) — the numerator an
aggregate over `bursts` (`bursts_per_product_since`), the denominator the row's
`usage_share` — with C4's least-recently-served order as the tie-break, so equal
weights still alternate. And the pool is asked *once for the fleet*, before any
product's turn: an empty pool is empty for everybody, so the tick ends with one
`capacity_exhausted` event and nothing claimed, instead of one event per product
and a task's lease churned. The capacity is handed down to the burst that runs;
the single-product loop, having no fleet, asks for itself.

**Role-prefixed App credentials (D1)**: one App per pipeline role, addressed by
env prefix over the same three names — the dev App reads `GITHUB_APP_ID` /
`GITHUB_APP_PRIVATE_KEY_PATH` bare, the others behind `REVIEWER_`, `TESTER_`,
`MONITOR_` (`ROLE_PREFIXES` in `github_auth.py`; `MONITOR_` is reserved until
T4). App names are the adopter's choice — the prefix scheme is the contract.
`control-plane github-check` preflights every configured role: a declared App
that can't mint fails the check (exit 1), an unconfigured role is reported and
skipped.

**Burst-token downscoping (D2)**: two token tiers (TO-BE §4). The control plane
keeps the App's full grant, cached (`InstallationTokenCache`). A burst token is
minted fresh per spawn and *downscoped at mint* — `repositories=[the task's
repo]` + the role's permission subset (`BURST_PERMISSIONS` in `github_auth.py`;
dev = contents/issues/pull_requests write, and dev is the only role that holds
a burst credential at all). "A burst can't reach another product" (§13) is a
per-token fact GitHub enforces: product A's burst token is rejected on product
B by construction (403/404, proven live). `mint_burst_token` in `bursts.py` is
the one place the burst tier is minted; a credential request for a non-dev role
refuses loudly.

**Tester provenance (D3)**: test reports and relayed `auto` issues post under the
tester App (`Scheduler.tester_api`), never the reviewer's — the finding's own
identity. The tester's `REQUEST_CHANGES` (PR-caused defects) joins
`machine_reviewers` via `tester_login`, so defect returns ride the same round
machinery. `E2E=on` without the tester App refuses at startup and at
registration preflight (`E2EPolicy.from_env` carries the honor-check, like the
claude provider's) — declared-but-unhonorable is never a silent degrade. The
tester *burst* still holds no GitHub credential at all.

**SecretResolver (D4)**: the platform never speaks to a secret manager (TO-BE §4).
Everywhere it needs a secret it holds a *ref* — a name, never a value, never a
command — and `secrets.SecretResolver` reads it at the moment of use. Three
schemes: `env:VAR`, `file:PATH`, `helper:NAME` — the last invokes one helper
binary configured out-of-band (`SECRET_HELPER`, the git-credential-helper shape:
`<helper> get NAME` → value on stdout), which is how anything vault-shaped plugs
in without the platform learning its name. Two things resolve through it: the
App keys (`*_GITHUB_APP_PRIVATE_KEY_PATH` takes a plain path as before, or any
ref) and the tester's sandbox credentials — a registered product's row
declares them (`test_env_refs`, `E2EPolicy.env_refs`), an unregistered
env-governed run keeps the `TEST_` prefix as `X ← env:TEST_X`, and
`resolve_test_env` injects the intersection of the manifest's ask and that
declaration. Preflight *probes* refs (points at something, never read);
the scheduler *resolves* at spawn; a helper failure is a skipped test with the
ref named, never a crash. `grep -ri vault packages/` stays empty by design.

**Roles as rows (D5)**: the four pipeline roles are rows in `roles` (seeded by
0002 on Postgres, by `_SCHEMA` on SQLite — the same values), and `execute_burst`
reads the role's row at *every* spawn: `model`, `max_budget_usd`, `max_turns`,
`prompt_preamble` (composed ahead of the task prompt, *after* the image's own
role preamble — the ceiling text stays code) and `mount_mode` come from it, so
`control-plane role set dev --model … --budget … --turns … --preamble …` governs
the next burst with no restart, audited (`role.set` + a `role_updated` event, one
write). `role list` / `role show` read. Ceilings are code (`roles.py`): a row's
`tools` must be `["*"]` or ⊆ `TOOL_ALLOWLIST` (checked at load; a narrowed row
refuses to spawn until custom-role execution lands at T4 — running it wide would
be a silent widening); reviewer/tester/monitor mounts are read-only whatever the
row says (a row narrows the dev to `ro`, never widens a reader). The wall-bearing
fields — `role`, `kind`, `identity`, `sink`, `tools` — are refused by name in the
store (`RoleError: frozen`) and by the schema's trigger beneath it (Postgres
`roles_pipeline_guard`, its SQLite twin); `role set` has no flag for them.
Migration 0014 gives `platform_app` `UPDATE` on `roles` and nothing else. The env
knobs the row replaced — `DEV_AGENT_MODEL`, `REVIEWER_MODEL`, `TESTER_MODEL`,
`SCHEDULER_BURST_BUDGET_USD`, `SCHEDULER_BURST_MAX_TURNS` — are retired: still
set, they make `run`/`burst` refuse at startup naming the `role set` that
replaces each (one source, or "editing the row changes the next burst" is false).
The hand-run `burst` verb's `--model/--budget/--max-turns` remain as explicit
per-burst overrides; `run --model` is gone.

**Leases + CAS (E1)**: the scheduler's ledger writes are the four single-statement
shapes of `STORES.md` and nothing else — no transaction outlives a store call, and
`tests/test_transactions.py` asserts it at every I/O seam of a tick on both
backends (`in_transaction` is the hook). A task is *claimed* by `claim_next`: one
`UPDATE … WHERE task_id = (SELECT … FOR UPDATE SKIP LOCKED LIMIT 1) RETURNING *`
that stamps `claimed_by`/`claimed_until` (0015) on the oldest eligible row — the
failure cooldown is its WHERE clause, the daily ceiling its fence (Postgres: one
try-only advisory lock around the aggregate and the claim; a peer holding it
means idle this tick). The ceiling counts *admitted* work — bursts today plus
live leases with no burst row yet — so two replicas cannot both see "one under".
Every other move is `Scheduler._move` → `transition_task`: write only the named
columns, only if the row still reads the (status, holder) this replica saw;
rowcount decides, a loser writes nothing and posts nothing. The lease is renewed
by a heartbeat the runner asks on the clock (`HEARTBEAT_POLL_S`, output or none)
while the container runs, and a lost lease kills the container. Reconcile touches
only *orphaned* rows — no live lease — so a peer's burst on another host is never
judged by this host's runner (`Runner.alive`, E4); a lapsed lease is presumed dead
and absorbed.
`upsert_task` remains for the verb/backfill/seed paths; `execute_burst` creates
the row if absent and lands the session id by CAS. `SCHEDULER_LEASE_S` (90) is
the one new rail.

**Two replicas over one ledger (E2)**: `tests/test_replicas.py` is the race, run
in the stores CI job — two `tests/replica_harness.py` processes (the real
`_serve` → `Fleet` → `Scheduler` → `execute_burst` → `PostgresLedger`, GitHub
and docker in fakes) on one Postgres, one SIGKILLed mid-burst; the survivor
absorbs the orphan after its lease lapses, and the ledger shows one `succeeded`
burst per task, one `killed` row, one failed attempt. What fans out with the
second replica: the fleet takes each product's **observation turn** through a
lease on its registry row (`claim_observation`, one poll interval long — N
replicas poll GitHub once, not N times; `Scheduler.tick`, the `--github` loop,
observes every tick without it, since dev mode may have no row); the row's
**`concurrency`** is counted at admission in `claim_next` (tasks in flight — a
live lease or a running burst — against the cap, default 1: a second replica is
a hot standby unless the row says otherwise; `Rails.concurrency`, copied from
the row by the fleet like `daily_bursts`, no env knob); and the heartbeat
**fences itself** when the lease has gone unrenewed for two thirds of its span —
the holder's only proof of life is the row, so it stops before a peer may
lawfully absorb. `cli.py` copies the loop's actual `--interval` into the rails
so the observation lease matches the cadence.

**The kill switch is a row (E3)**: `org_state`, one row for the whole ledger
(migration 0016; `Ledger.org_stop` / `stop_org` / `resume_org`), replaces the
MVP's STOP file and its local `docker kill`. `control-plane stop --reason …` sets
it by CAS with its audit row (`org.stop`, target `org`) and `org_stopped` event
in one statement; `resume` clears it. Every loop reads it at the top of every
tick and before every spawn (`standing_down` in `scheduler.py`) and keeps
ticking while stopped, so `resume` needs no restart and `run` no longer refuses
to start under a STOP. Every live burst's heartbeat reads it on **every beat**
and answers False, on which the runner holding the burst kills its container —
that is how a `stop` on one host reaches a burst on another. A fenced burst is
recorded `killed`, not `failed` (`execute_burst` remembers the heartbeat's
answer; the exit code is 137 either way), and its task rolls back with one more
attempt. SIGTERM/SIGINT set the engine's `halt` flag instead — the same fence,
process-local, nothing written: a restarted replica is not a stopped org. The
second test in `tests/test_replicas.py` runs the done-when: a `stop` from the
operator's CLI process kills a burst one replica holds, and `resume` finishes the
work with nothing restarted.

**The runner is a protocol (E4)**: `Runner` in `runner.py` — `run(spec,
log_path, heartbeat) -> int` and `alive(burst_id) -> bool` — is the third seam
beside `Ledger` and `SessionArchive` (TO-BE §6); `DockerRunner` sits behind it
with its behavior untouched, and `JobsRunner` beside it since G3. The scheduler
holds one runner per process (`open_runner()`, chosen in `cli.py` like the two
stores) and asks it to `run` every burst and, in `reconcile_running`, whether an
orphaned burst is still `alive` — the old `docker ps` in the scheduler is gone;
nothing outside `runner.py` spells `docker`. `burst_env(spec)` is the box's
contract, factored out of the docker command line: the environment every
runner hands the container verbatim (the one credential, the agent's OTel, the
tester's sandbox refs), so a second runner packages the rules rather than
re-deriving them. `tests/test_runner_contract.py` is the parameterized contract
(the E4 done-when, reused by G3): each runner brings a rig — the docker rig is
in-process, a `DockerRunner` with a shell in the daemon's seat — and the suite
holds every runner to the exit code, the tee, the heartbeat on the clock, the
fence, `alive` and `burst_env` unchanged. The suite's fakes in docker's seat
(`SleepingRunner`, `ProbingRunner`, …) are handed in through the seam
(`Scheduler(runner=…)`, `execute_burst(runner=…)`), never monkeypatched in.

**Three phases behind the runner (G1)**: a burst is `materialize` → `run` →
`collect`, and all three are the `Runner`'s. The first and the third move bytes —
the parent snapshot into the volume, the result file and violations feed back out
— and *where* those bytes live is exactly what a substrate decides: a directory
on this host (`HostPhases`, which `DockerRunner` and the suite's fakes inherit),
an init container and a harvester talking to presigned URLs on a cluster (G2/G3).
Turning what came back into ledger rows is **not** a phase: `fold_artifacts` runs
in the control plane, because nothing that runs beside a burst holds ledger
credentials. `open_runner(archive)` hands the runner the archive for the same
reason the ledger and archive seams are chosen in one place.
`tests/test_runner_contract.py` holds every runner to the phases as it already
holds them to the exit code, the tee, the heartbeat and the fence.

**Capabilities, not credentials, beside a burst (G2)**: `packages/burst-phases`
is the pod's init and harvester — `burst-init` fills the volume (the parent
snapshot through a presigned GET, checksum checked against what the *ledger*
recorded; then the clone, with a `contents:read` single-repo token that is
init's alone) and `burst-harvest` ships what the burst left (the snapshot to its
key, and a small record — result + violations + the snapshot's checksum — that
the control plane reads and folds). `control_plane.phases` is the other half:
`mint_phase_env` hands out one URL per object, time-boxed (`PHASE_URL_TTL_S`,
the wall-clock rail's stand-in until Stage H gives bursts one), and
`read_artifacts` turns the record back into the same `BurstArtifacts` a host
phase produces — so `fold_artifacts` cannot tell where the burst ran. The image
(`docker/burst-phases/Dockerfile`) carries no control-plane code, no ledger
client and no cloud SDK: CI checks it the way it checks the receiver's, because
what is not installed cannot be imported in a pod. `LocalSessionArchive` refuses
to presign — a directory has no URLs to hand out — which is the message a
deployment meets if it tries to run bursts off the control plane's own
filesystem without `ARCHIVE_URL`. `DockerRunner` stays on the host phases (G1):
it shares that filesystem, so its burst container is handed nothing at all, and
the zero-standing-credentials property holds on both paths for the same reason.

**Bursts as Jobs on a cluster (G3)**: `BURST_NAMESPACE` is the toggle — set, and
`open_runner` returns `JobsRunner` (`jobs.py`) instead of `DockerRunner`; unset,
nothing changes. One burst is one Job is one pod of three containers: **init**
(the phases image) fills an `emptyDir` from its presigned GET and clones the
product into a second one, **agent** (the dev-agent image, unchanged) runs
against both, **harvest** waits for the exit code the agent writes to a third,
tiny volume and ships what is on the session volume. A pod has no "after the
main container" hook, which is why that sentinel exists and why it is *not* on
the session volume — that one is archived and materialized into the next burst.
The walls are absences: `automountServiceAccountToken: false` (a burst holds no
k8s credential at all, so "a burst can't touch the cluster" is a fact about the
pod), no storage credential (capabilities, G2), no GitHub credential in a
reviewer's or tester's agent (the `contents:read` clone token is the init
container's alone, `mint_clone_token`), `backoffLimit: 0` + `restartPolicy:
Never` (a retry is the scheduler's decision, made with the result file in hand).
`activeDeadlineSeconds` is `PHASE_URL_TTL_S` — how long a burst may run and how
long its URLs are good for are one number, because two would drift into a pod
outliving its capabilities. **kubectl, not a client library**, for the reason
`DockerRunner` shells out to docker: the streaming half of `run` — the pump, the
heartbeat on the clock, the fence — is subtle and already written, and against a
subprocess it is literally the same `_pump`, so the two runners cannot drift on
the guarantee that matters most.

Three things the second substrate changed on this side of the seam.
`BurstSpec.archive_as` — *where* a burst's snapshot goes is settled at spawn
rather than asked for at `collect`, because a pod's PUT has to be minted before
it starts; `collect(spec)` lost its argument. `BurstSpec.clone` /
`clone_token` — a pod inherits no mount, so the burst is told what to fetch
(`pinned_origin`: origin, and the branch or commit the platform's own clone is
sitting on, so the tree in the pod is the tree `repo_mount` would have been), and
`Runner.pod_side` is the one bit of substrate the caller reads, buying exactly
that decision. And a G2 bug the second substrate exposed: the harvester's record
key was derived from the *parent's* snapshot key, so two bursts resuming one
parent would have filed their records in the same place — it is
`artifacts_key(session_key(task_id, burst_id))` now, this burst's own, which is
also what lets a reviewer report without archiving anything.

`tests/test_runner_contract.py` runs on both runners, and getting a cluster to
run it against is one command (`scripts/burst-cluster.sh`, a throwaway kind
cluster + the archive + the phases image). The suite had to become
substrate-neutral to do it: every test is now materialize → run → collect, and
reads the volume only through what the burst *wrote*, because on a cluster the
volume is inside the pod and there is no moment at which a test could look at
it. `tests/test_jobs_runner.py` is what the contract cannot ask — the pod spec's
walls, read off the manifest without a cluster, plus one live burst that goes
looking for the service-account token and finds nothing.

**Webhooks as wake-up, polls as truth (E5)**: three parts and one wall (TO-BE §5).
`packages/webhook-receiver` is the platform's only public surface — verify
`X-Hub-Signature-256` against the App's webhook secret (`WEBHOOK_APPS="<app id>=
<secret ref>,…"`, refs resolved once at start), drop replayed delivery ids (an
in-memory LRU; a forged delivery is 401 and *not* remembered, so it cannot poison
the dedup), POST a wake (`repository`, `kind`, `delivery`) to `WAKE_URL`. It never
imports the control plane, and its image (`docker/webhook-receiver/Dockerfile`)
cannot import `control_plane`/`psycopg`/`boto3` and has no git or docker — CI
checks. The replica's internal endpoint (`WAKE_BIND`, `wake.py`; unauthenticated by
design — the worst a caller can do is one poll of real GitHub state) turns a wake
into one ledger statement, `wake_repository`: the product's observation lease is
cleared (a rate, not a wall — clearing it is exactly one extra poll) and a
`product_woken` event with the delivery id is written; the `wakes` channel is
notified. Every loop sleeps on its engine's `wake` flag instead of a timer
(`_serve`); the endpoint sets it locally and `Ledger.subscribe_wakes` (Postgres
LISTEN, own thread and connection, reconnecting) sets it on every replica — so a
webhook that reached one replica has all of them looking within a second, and the
observation lease still makes exactly one poll. The endpoint holds its own ledger
handle (`open_ledger` twice): it writes from the server's threads, the loop from
its own. `tests/test_wakes.py` runs the done-when as processes on Postgres: two
replicas + the real receiver; a signed delivery is one poll within seconds, a
replay and two forgeries are none — each named in the receiver's log by delivery
id — and a second delivery is exactly one more. The images
(`docker/control-plane/Dockerfile` — the CLI plus git and the docker *CLI*, the
host's socket mounted; the receiver's, slim) and the compose `platform` profile
(migrate + 2 replicas + receiver over the stores) are the T0 deployment shape;
`examples/k8s/` is the reference cluster shape, kubeconform-validated, with the
caveat that bursts on a cluster are Stage G.

**The gate (E6) and what it changed.** `DEPLOYMENT.md` — the operator's side and the
rehearsal record. Four things the rehearsal forced: **a wake settles** — GitHub's
read side lags its own webhook (a `ready` label listed 0.75 s after it landed), so
`wake_repository` *reserves* the observation lease (`claimed_by = NULL, claimed_until
= now() + WAKE_SETTLE_S`, 2 s; a wake inside a pending window joins it) and the
woken loop sleeps the settle before its tick — without it the woken poll missed the
label and the claim came at the interval (65 s; with it 6.1 s). **The receiver's
environment is its own file** (`.env.receiver`), never `.env` — compose had handed
the public process every platform credential. **`PLATFORM_SECRETS`**: the App keys
are a same-path read-only mount like `PLATFORM_DATA`, and `.env` key paths are
absolute — a relative path resolves nowhere in the box. **Transport failures on
GitHub calls are `GitHubApiError`/`GitHubAuthError`** (`_call`, `_check`): a DNS
blip surfaced as a raw `requests.ConnectionError` and took a replica down; the loop
already retries those errors next tick. Replicas and receiver restart
`unless-stopped`; the dev-agent image is a build-only compose service
(`replicas: 0`) so `up --build` produces all three.

### The subscription pool

The platform's compute is the team's pooled subscription capacity (TO-BE §4
mechanism A); the verbs are F1's:

```bash
uv run control-plane subscription register --member maksim \
    --ref env:MAKSIM_CLAUDE_TOKEN --cap 12000000 --expires 2027-09-01
uv run control-plane subscription list
uv run control-plane subscription show maksim            # the row + its audit trail
uv run control-plane subscription set maksim --cap 20000000 --ref file:/run/secrets/maksim
uv run control-plane subscription renew maksim --expires 2028-09-01   # the way back from expired/revoked
uv run control-plane subscription revoke maksim --reason "..."   # the reason is required
```

**The platform never holds a token.** A member runs `claude setup-token`,
materializes the value themselves — env var, file, whatever the adopter's tooling
writes — and registers a *ref*: `env:NAME`, `file:PATH`, `helper:NAME`. Custody
never moves, so deleting the secret at its source is a revocation the platform
cannot override, and there is nothing for a ledger dump, a log line or an archive
to leak. The wall is three deep and each layer is tested: `subscriptions.check_ref`
refuses a value at the verb, both backends call it before their statement, and
`subscriptions_secret_ref_check` (0017) refuses whatever reaches the table by any
other path. Refusals are deliberately mute about their input — Postgres puts the
failing row in a CHECK violation's DETAIL, so that message is never passed
through. `tests/test_subscription_pool.py` runs the done-when: a token pushed at
every entrance that takes a ref, then the ledger file, every audit row, every
event payload, stdout, stderr and the log stream greppable for it and empty.

**The pick, at admission (F2).** Before a burst is synced, minted or spawned, the
scheduler asks the pool who pays: two reads — the rows, and one rolling-window
aggregate over `bursts` (`subscription_usage`; `USAGE_WINDOW_H` = 5, the span a
`cap_tokens` is declared against) — then the eligible subscription with the most
headroom, ties broken on the id so every replica reads the same order. Eligible
is a pure predicate in `subscriptions.py`: active ∧ unexpired ∧ not backing off ∧
headroom > 0. The ref resolves at that moment and the token goes into the box
(`BurstSpec.oauth_token`, preferred over the machine's own credential — a burst
stamped with a member's subscription has to actually run on it); the ledger keeps
only `bursts.subscription_id` (0018/0019). A ref that no longer resolves is the
member's own revocation, so it is logged and skipped rather than fallen back
from. Nothing eligible is **not a failed attempt** — the task keeps its attempts,
gives back its lease and waits, and `capacity_exhausted` goes to the outbox. An
empty pool is `MACHINE_CREDENTIAL`: the deployment behaves exactly as it did
before F2, which is what every laptop run still does.

**A spent token is not a failed task (F3).** What comes back from a rate-limited
burst is text, not a status code — the SDK hands the control plane a result file
— so `rate_limited_until` reads the provider's own words and, when the message
carries a reset stamp, uses it verbatim (a stamp already past or more than a day
out falls back to `RATE_LIMIT_BACKOFF_H` = 1). `execute_burst` then benches the
subscription that paid, by a forward-only CAS on `backoff_until` — two bursts
limited in the same second cannot shorten each other's answer — and reports it on
the outcome. The scheduler's rail reads that and charges the task *nothing*: no
attempt, no cooldown, no comment on the issue, the lease back. The next tick
picks another member (the benched row is ineligible until its time) and resumes
the session the limited burst had already written, because the session is the
task's and not the token's. A misread costs one member an hour; a missed read is
the ordinary failure it was before F3.

**The year, and the way back (F4).** A `claude setup-token` lasts a year and
renewing it is a person's errand, so the platform says so: one
`subscription_expiring` event per subscription per day through the final
`EXPIRY_NOTICE_DAYS` (14), emitted from the ordinary tick — no daemon, and no new
column to remember it by, because the outbox itself is asked whether it already
said this today (`events_of_kind_since`). A burst whose credential is *refused*
— the year ran out, or the member revoked it at the provider — marks the row
`expired` by CAS with an audit row, because there the platform changed a member's
standing on its own authority. `subscription renew` is the one way back to
`active` from either ending: the member re-materializes the value at the ref, an
operator moves the date. It leaves `backoff_until` alone — a provider limit has
nothing to do with the token's year. And `reporting.contribution` (0020) is what
each member's subscription actually paid for, by week: bursts, tokens, products,
with the row's own declaration beside them, and one row with a NULL week for a
contribution not yet spent.

**One member, one contribution**, and the id is derived from the handle
(`--member Maksim` → `maksim`), so `subscription revoke maksim` is typeable by
whoever registered it. Registering twice is a refusal that names the first row.
The endings are CAS with the registry's discipline — `revoke` requires a reason,
a no-op leaves no audit row and no event — and there is no transition back to
`active`: a revoked or expired row stays (the bursts it paid for still name it),
and coming back is renewal at F4. `set` touches the declaration only —
`cap_tokens`, `expires_at`, `secret_ref`; `status` is `revoke`'s and
`backoff_until` is the attempt rail's (F3). Usage is never mirrored into a
counter: consumption is a rolling-window aggregate over `bursts`, which is what
the pick reads.

**No single command hangs a burst (H2)**: `burst_env` sets the CLI's own per-call
ceiling — `BASH_DEFAULT_TIMEOUT_MS` (2 min, what a call gets when the model asks
for nothing) and `BASH_MAX_TIMEOUT_MS` (10 min, the most it may ask for). A
ceiling in code (`runner.py`), not a row: how long one command may run is a
platform-safety question, and a product that needs longer says so per call rather
than raising a floor for everyone. Enforcement is the engine's, and what it does at the
ceiling was measured on a live burst (#104): it **backgrounds** the call and
hands control back rather than killing it. The agent is free at two minutes,
which is the whole of finding #6 — 18 minutes lost to one stuck command, with
the budget draining and the heartbeat renewing a lease on work that had stopped
— while the process itself runs until the burst's container ends, so the thing
that bounds it is the wall-clock rail (`activeDeadlineSeconds` on a Job). A rail,
not a wall: the burst continues on its own terms.

### Task identity

A task id is derived, never typed: `{owner}/{repo}#{issue_number}` (`task_id_for`).
Both places that mint one — the scheduler's intake and the `burst` verb — build it
from the repository slug, so a hand-run burst joins the task the scheduler would
have created rather than making a second one. `UNIQUE (repository, issue_number)`
makes a duplicate unrepresentable, and `tasks.repository` holds the slug: the
platform's clone path is a fact about one machine and lives in no row.

MVP-era data — bare-number ids, a clone path in `repo` — moves with one verb:

```bash
uv run control-plane backfill --repository owner/name --data-dir data --dry-run
```

It reads the single-node pair read-only and writes to whatever the toggles name:
rows re-keyed, each task's one blob re-keyed as its last dev burst's snapshot,
manual-era rows (`t1` and friends — ids that were never issues) dropped and
counted. It refuses a second run rather than colliding on every derived id.
Two consequences worth knowing: `migrate` **fails on purpose** at `0008` while any
task lacks an `issue_number` (the repair is the verb, and the state after the
failure is a prefix), and `SqliteLedger` refuses to open a pre-A6 file for the same
reason instead of half-repairing it.

### Platform memory — the write path

A burst may propose what it learned; the platform stores it with provenance
(TO-BE §9, H4). The channel is the result contract, like everything else that
leaves a burst: the work prompt invites an optional fenced
`{"memories": [{"claim", "evidence"}]}` block at the end of the final message,
`dev_agent.burst.proposed_memories` parses it (optional and never fatal — the
deliverable is the PR, so a missing block is the ordinary case and a malformed
one is ignored), and the harvest folds it into `memory_entries`.

Three rules, each a wall rather than a preference. **Evidence is required** —
memory is injected as data an agent may verify, and an entry without evidence is
an instruction wearing a datum's clothes. **The scope is derived** — a product
entry names the burst's *own* product, taken from its task id, so one product's
burst cannot write into another's memory even by asking to. **The status is
`proposed`, always** — nothing a burst writes promotes itself into recall.
`check_proposal` holds both backends to all three; a refused proposal is a log
line, never a failed burst.

There is no read path yet: injecting recalled data into prompts is scope *and*
weight, and weight is T2's. `memory_entries(scope=…, repository=…)` is the read
the console and the gardener will use.

### The session tree

One immutable snapshot per burst, keyed `sessions/{task_id}/{burst_id}/session.tar.gz`,
and the ledger owns the lineage: `resumed_from_burst_id` names the parent a volume
was materialized from, `session_key`/`session_checksum` name what the burst left.
`latest_archived_burst(task_id)` is the leaf — the newest **dev** snapshot: a
tester's volume is archived too (B5), but as evidence the role filter keeps out of
the tree — and `prepare_volume(..., parent=None)`
is how an amnesiac burst is expressed — there is deliberately no way to ask for a
volume that resumes nothing but still holds the session. Snapshots are write-once:
a retry is a new burst, so a new key, and branching is a ledger write.

**Retry ≠ branch (H5)**: a node can be *abandoned* — `bursts.branch_summary`
(0021) is both the mark and its content, set once by CAS (`abandon_burst`), and
the leaf query steps over it, so the next burst grows from the abandoned node's
parent. The scheduler abandons only the approach's failures — the engine's own
`error_max_turns` / `error_max_budget_usd`, or a clean finish with no PR
(`BurstOutcome.spent_its_allowance`, `abandon_attempt`); a kill, a spent token, a
refused credential or a dead stream retries the same node as before. A branch
resumes its node's own session with `AGENT_FORK_SESSION=1` (the SDK's
`fork_session`), is shown the node's abandoned attempts as data
(`abandoned_branches`, `prompts.abandoned_attempts`), and each summary is proposed
to product memory with the burst that made it. The summary is structural — the row,
the engine's `subtype` (now in the result file), the rail's reason — because the
platform does not read transcripts. `tests/test_fork_rails.py` is the done-when.

**One tree per task (H3)**: the platform clone is a bare cache per repository
(`clones/cache/<owner>__<name>.git`) seeding one tree per task
(`clones/tasks/<owner>__<name>/<issue>`), because replicas on one host share the
data dir and a product's `concurrency` can put two of its bursts there at once.
`sync_clone(task)` / `sync_clone_ref(task, ref)` take the task; cli's
`_task_trees` sweeps the trees of `done` tasks before every sync (`prune_trees`). A
tree is a plain local clone (hardlinks, no alternates, no worktree link) — the box
mounts it at `/workspace`, where a pointer back to a host path would break git. The
`.venv` stays warm across one task's bursts; a task's first burst syncs it cold.
`tests/test_clone.py` is the done-when.

**Golden replay (G4)**: `control_plane/replay.py` runs one archived node + task as
an identical burst under each runtime and emits the criteria matrix
(`matrix.json` + `matrix.md`); `control-plane replay FIXTURE --runtime …` is the
verb, `--node <burst id>` replays a ledger node. The one knob is
`BurstSpec.runtime_class` — `runtimeClassName` on the pod, `--runtime` under
docker, None (the default) everywhere the scheduler spawns. `JobsRunner.timings`
reads boot/init/agent/harvest off the pod's own stamps (whole seconds). A replay
writes no ledger row and holds no GitHub credential. `dev-agent check` now also
reports `resume_transcript` when told to resume, which is how the restore row is
measured without the engine. The CI `cluster` job builds kind + gVisor
(`scripts/burst-cluster.sh --gvisor`, release pinned and sha512-checked), runs
the runner contract and `tests/test_replay.py` there, replays
`examples/golden/check.json` under `default` and `gvisor`, and uploads the matrix.
Kata is not on that list because a kind node has no nested virtualization; the
bake-off proper is G5's, on hardware.

**Egress as data, M4 as a number (G6)**: every Job and its pod carry
`usf/egress` — JSON entries `{host, port, why}` from
`control_plane/egress.py`: GitHub; on `run` the inference API and the uv
toolchain's index; the archive's host (from the minted URLs); the OTLP endpoint;
and for a tester only, `TESTING.md`'s new `egress:` hosts (`BurstSpec.egress`).
The platform enforces none of it — `examples/egress/cilium-burst-egress.yaml` is
the reference policy, and `test_egress.py` keeps it equal to the declaration's
fixed hosts. `dev-agent walls` is the M4 probe set: attacks that must fail (each
failure is a violations-feed entry, so on a real burst it reaches the ledger via
`fold_artifacts`) and one control that must succeed (a box that reaches nothing
proves no allowlist). Replayed through the G4 harness, the matrix gets a
`breaches` row and a per-probe table, and any breach makes `control-plane replay`
exit 1. The rig is `scripts/burst-cluster.sh --gvisor --cilium` (Cilium pinned,
operator at one replica on a one-node kind); without the policy the probes find
2/7 (the undeclared host, the cluster API), with it 0/7 on both runtimes.

With `LEDGER_DSN` and/or `ARCHIVE_URL` set, `uv run pytest` additionally runs that
store's contract suite (`test_ledger_contract.py`, `test_archive_contract.py`)
against the service backend as well as the single-node one. Everything else keeps
its own scratch ledger and archive regardless — see `tests/conftest.py` for why
that has to be forced.

### Parity, at a gate

The contract suites hold the backends to one answer per method; a `gate` issue asks
it of a whole scenario. Run the same commands twice — toggles set, then unset, into
two data dirs — and compare with the instruments A7 built:

```bash
uv run python scripts/ledger-parity.py  data-single/ledger.sqlite3 "$LEDGER_DSN"
uv run python scripts/archive-parity.py data-single "$LEDGER_DSN" "$ARCHIVE_URL"
```

Both exit non-zero on a difference. What they can and can't say — the three
buckets, and the asymmetries that are by design — is `STORES.md`, "Parity, and
what it can mean". They are by-hand instruments (a live rig, a real burst), so
`tests/test_gate_instruments.py` is what keeps a rename from silently breaking
them between gates.

## Telemetry

Two OTLP endpoints, because two processes in two network namespaces. Both are
presence toggles; unset means spans exist only to mint correlation ids, and the
container is told nothing about telemetry at all.

```bash
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318          # the control plane's own
export DEV_AGENT_OTLP_ENDPOINT=http://host.docker.internal:4318   # where a *burst* pushes
```

The second string is the one that gets it wrong: `localhost` inside the box is the
box, and the CLI drops export failures silently — a burst with an unreachable
collector looks exactly like one with none. Point both at the same collector by two
different names, and if nothing arrives, `CLAUDE_CODE_OTEL_DIAG_STDERR=1` in the
container env makes the CLI say why.

What a burst emits is decided in `control_plane.telemetry.agent_telemetry_env`, with
the reason for each choice next to it: traces and log events yes, metrics no (the
ledger is the metrics store — TO-BE §12), tool arguments yes, tool and API bodies no.
`CONTROL_PLANE_TRACE_CONSOLE=1` prints the control plane's own spans instead of
pushing them; it is never passed into a container, where stdout is the SDK's message
channel.

The rig that receives all this is `docker/compose.telemetry.yaml` (B4) — collector,
VictoriaLogs, Phoenix, Grafana, no volumes, four commands to boot. **`OBSERVABILITY.md`
is the read**: the correlation join, the three UIs, what to check when nothing
arrives, the alert condition in the three forms an adopter can consume, and the
reference dashboards in `examples/dashboards/`. The traps it records because they cost
real time: a correlation id used as a VictoriaLogs stream field kills the process, a
collector config mounted at the other distribution's path is silently ignored while
everything reports healthy, and Phoenix's cost is its own estimate (~0.64× measured)
because it prices tokens rather than reading the CLI's figure — the burst row stays
the authority. The rig's Grafana connects as a role that can read the views and
nothing else, so a panel reaching for a base table fails rather than widening the
boundary.

## Test

```bash
uv run pytest                # structural tests, no network
```

## Conventions

- Python 3.12, `uv` for env/deps, type hints throughout.
- `async` core with a thin sync wrapper for the CLI.
- Keep the seam to the SDK in one place; layer guardrails as composable hooks.
- Logs are structlog, slog-shaped (event phrase + `key=value`), configured once per
  CLI entry via `dev_agent.log`: colorized console on a TTY, logfmt everywhere else.
  `print()` is reserved for payload — command output, agent prose, container streams.
