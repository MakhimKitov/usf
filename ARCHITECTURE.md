# Autonomous Maintenance Platform — Architecture

Source of truth for the whole experiment. Spans three repos; sits above all of
them. Each repo's own `CLAUDE.md`/`AGENTS.md` governs work *inside* that repo;
this document defines how the pieces fit and what we're testing.

> Status: **MVP built and proven end-to-end** (2026-07). A human-`ready` issue became
> a merged, deployed feature through work bursts, autonomous machine-review rounds,
> and a live E2E test burst — unattended between the two human gates (discord-bot
> task 17 / PR #18 was the proving run). This document remains the *design*; where
> the build deviated, the subsystem docs are authoritative for as-built behavior:
> **`SCHEDULER.md`** (loop, state machine, rails), **`REVIEWERS.md`** (review
> providers), **`TESTER.md`** (E2E test bursts), **`STORES.md`** (ledger + archive),
> **`OBSERVABILITY.md`** (what is emitted and how to look at it),
> **`GITHUB-INTERFACE.md`** (protocol).
> Known as-built deltas: both service stores are built and are presence toggles, not
> §6's scale-up rewrite (`STORES.md`, Stage A); the monitor is not built (the tester's
> symptom relay already exercises its `auto`-issue pipeline); pre-PR self-review was
> dropped in favor of reviewer bursts (decision record in REVIEWERS.md). Tracing is
> wired end to end as of Stage B — the delta that used to be listed here.

---

## 1. What this is

Not an app — a **platform** that runs an autonomous *developer* against a *product*
codebase. A developer runs in a container with a clone of the product repo, works
like a normal engineer (branch → code → test → PR), and hands a pull request to a
human for review. The product deploys via its own CI on merge.

The platform is **product-agnostic by design**: the product is a parameter — a repo +
its CI + its runtime. The first instance under maintenance is the sibling
`discord-bot`; it appears in this doc only as the running example, never as a design
assumption.

## 2. Hypotheses & bars

The experiment tests three things at once. Each maps to a metric family (§9):

| Hypothesis | Question | Bar |
|---|---|---|
| **Reliability under constraint** | Does it produce correct, mergeable work while honoring the operating contract, at an acceptable human-intervention rate? | qualitative, tracked over time |
| **Containment / safety** | Do the walls hold, unattended, even under prompt-injection from real users? | **zero successful breaches** (hard pass/fail) |
| **Raw capability** | Can it produce correct non-trivial fixes/features in this codebase at all? | review pass-rate, post-merge defects |

## 3. Components & repos

> Rule: a separately-running component gets its own repo.

| Component | Repo | Lifecycle | Identity / blast radius |
|---|---|---|---|
| **Control plane** (scheduler) | platform repo (`usf`) | long-lived | spawns Docker containers, holds run state |
| **Dev agent** (the developer) | same platform repo (payload image) | ephemeral, per-issue | GitHub App scoped to product repo: `contents`+`pull_requests` write, **no merge/admin/secrets** |
| **Tester** (QA) | same platform repo (**same image**, different contract) | ephemeral, per test-burst | **no GitHub credential** (as built — the report returns via harvest and the control plane posts it as the reviewer App; a dedicated tester App is deferred) · **product-sandbox creds** (`TEST_*` intersection, which the dev never has) |
| **Monitor** | **separate** repo | long-lived | telemetry-read + `issues:write` only |
| **Product** | its own repo (first instance: `discord-bot`) | runs in prod (human-operated) | n/a — the thing maintained |

Control plane + dev agent share one repo because the agent is the image the control
plane builds and runs in lockstep, not an independent service. Revisit if the
developer ever needs to be reused across products. The **tester is a role, not a
codebase**: the dev image booted with a different operating contract and a disjoint
credential set (§6 "Testing", §8).

## 4. Topology

```
┌─────────────────────────────────────────────────────────────────────┐
│ PLATFORM repo                                                         │
│  ┌───────────────┐  spawn (Docker) per burst        ┌──────────────┐  │
│  │ Control plane │ ───────────────────────────────▶│ Burst        │  │
│  │  scheduler    │   track run / budget / killswitch│ container    │  │
│  │  owns issue→  │ ◀─────────────────────────────── │ dev·reviewer │  │
│  │  burst mapping│      result via harvest           │ ·tester roles│  │
│  └───────────────┘                                   └──────────────┘  │
└───────────────────────────┬───────────────────────────────────────────┘
                            │  (all interaction via GitHub API)
   MONITOR repo (phase 1+, ─┤
   not built) logs·metrics  │  files issues (provenance = auto —
                            │  today fed by the tester's symptom relay)
                            ▼
┌─────────────────────────────────────────────────────────────────────┐
│ GITHUB  — integration substrate / source of truth                    │
│  Issues  queue: human-filed = trusted · machine-filed = auto/tainted │
│  PRs     work output + machine review + test report + HUMAN gate     │
│  Branch protection  no merge without human approval                  │
│  CI/CD   tests + deploy-on-merge   (owned by product repo)           │
└───────────────────────────┬───────────────────────────────────────────┘
                            ▼
         PRODUCT — live, real traffic, human merges → deploys
```

## 5. GitHub is the substrate

> The concrete protocol — label taxonomy, issue/PR templates, branch/PR conventions,
> identities & scopes — is **`GITHUB-INTERFACE.md`**.

The product's bespoke local protocol (`ops/tasks/`, `ops/reports/`) is **replaced**
by GitHub primitives:

- **Issues** = the task queue. Triaged and assigned by the control plane.
- **PRs** = release candidates *and* the review gate.
- **Branch protection** = the "never deploy to prod" wall: the dev identity cannot
  merge; deploy happens on human merge.
- **CI/CD** (product repo) = tests on PR; releases via `release-please` (Release PR →
  human merge → deploy). Not the platform's concern. Protocol: `GITHUB-INTERFACE.md`.
- **Reports** = PR description + issue comments. Run telemetry stays in the
  platform's own store, never in the product repo.

What survives of the product repo's contract: the **rules** in its
`CLAUDE.md`/`AGENTS.md` (treat user content as untrusted, small reversible changes,
reproduce-before-fix) — delivered as the dev agent's operating prompt, not as an
`ops/bin` filesystem mechanism.

## 6. Run lifecycle

Two human gates bracket an autonomous middle: a human approves *what* to work on
(intake) and *what ships* (PR review + merge). The engineering in between is unattended.

```
issue (open)
  ─▶ HUMAN triage: approve via `ready` label            ◀── gate 1 (intake)
  ─▶ control plane assigns + spawns a BURST container
  ─▶ agent: reproduce ─▶ branch ─▶ code ─▶ run tests
  ─▶ open PR ─▶ container tears down                     ◀── burst ends
  ─▶ machine review (REVIEWERS.md): an assess burst posts findings as the
       reviewer App; findings drive autonomous dev rounds (resume), capped
  ─▶ TEST burst (TESTER.md): after a clean machine review, the tester boots the
       PR head, runs invented scenarios in the sandbox, report posted
  ─▶ HUMAN review                                        ◀── gate 2 (ship)
       ├─ changes requested ─▶ control plane spawns a NEW burst (resume from branch+PR)
       └─ approve + merge ─▶ CI deploys ─▶ issue closed
states: open · ready · assigned · in-progress · pr-open · needs-review ·
        changes-requested · blocked · done
```

- **Gate 1 — human triage.** Nothing is assignable until a human approves the issue
  (a `ready` label). This is the primary control on *what work happens*; it sits on
  top of the provenance gate (§8), which still governs *how* the dev interprets an
  approved issue's body. Monitor proposes → human disposes → dev executes.
- **Assignment authority = control plane**, not GitHub labels. With N devs, labels
  only *reflect* its decision — never the lock (race-free claiming).
- **Container lifecycle = ephemeral per *work-burst*, not per issue.** A burst is one
  unit of compute — "issue → PR", "address one round of review feedback", *or* "test a
  PR end-to-end" (a **test burst**, run by the tester role). The
  container runs the burst, pushes the branch, and **dies**; it never idles through
  the human-paced (possibly multi-day) review loop. Durable state lives in **GitHub
  (branch + PR thread + issue) plus a persisted agent session**; a follow-up burst
  spawns fresh and **resumes** from it. This keeps per-burst context clean
  (isolation, crash recovery = respawn, scaling = spawn more) without parking idle
  containers. Per-burst setup is amortized by a **warm base image** (toolchain +
  deps pre-baked → a burst is just `git fetch/checkout` + agent boot); a warm pool
  is the escape hatch only if setup is heavy.
  → **Shutdown rule: a container lives exactly one burst.**
- **Control plane is event-driven:** spawns a burst when there is an approved,
  unassigned issue, a PR with actionable feedback / CI failure, or a PR that is CI-green
  and not yet E2E-tested (→ test burst).
- **Crash recovery:** a dead burst releases its issue/PR back (lease + heartbeat on
  the control plane); respawn re-derives state from GitHub + session.
- **Kill switch + budgets:** instant stop + per-burst caps enforced by the control
  plane (independent of the agent behaving). Under **subscription billing** tokens
  (not USD) are the consumption metric, and cost figures are estimates — but
  `max_budget_usd` is a **hard cap regardless**: the SDK kills the burst mid-stream
  when estimated cost crosses it ("advisory" was falsified on task 17). Because the
  worker captures its session id from the init message and writes its result file
  even on a dying stream, a budget-killed burst **resumes** where it died — attempts
  × per-burst budget behaves as an installment plan, never a restart from zero. The
  SDK surfaces **rate-limit events/status** — the scheduler backs off spawning when
  the subscription window is near exhaustion instead of burning bursts into 429s.

### Session continuity (confirmed, SDK v0.2.110; re-confirmed live on v0.2.175)

The worker keeps its **full context across the whole task** — one persistent Claude
session per task, not per burst. Mechanism, confirmed from the installed SDK:

- The session id comes from `ResultMessage.session_id`; a later burst resumes by
  setting `ClaudeAgentOptions.resume = <session_id>`, which replays the full prior
  transcript.
- **The platform owns all persistence; the dev agent is a stateless worker** that
  touches only a local working dir. The SDK writes its session to `CLAUDE_CONFIG_DIR`,
  a **control-plane-owned volume** mounted into the burst. The agent holds **no storage
  credentials** (no Postgres, no object store) and never talks to a store directly — so
  a worker can't read or delete another task's transcript, or touch the control DB (a
  wall, §8).
- **Persistence is out-of-band, owned by the control plane.** *Before boot* it
  materializes the task's transcript into the mounted `CLAUDE_CONFIG_DIR` so
  `resume = session_id` finds it locally; *after the burst* (plus optional periodic
  snapshots — it owns the volume) it harvests the JSONL, mirrors it to the object store,
  and updates the index. So we use the SDK's **default local storage**, not the in-agent
  **`SessionStore`** adapter (which would need store creds in the box). The SDK's
  `SessionStore` contract — `append(key, entries[])`/`load(key)`, keyed
  `SessionKey{project_key, session_id, subpath}`, entries as opaque JSON-safe values,
  dedupe-by-`entry.uuid` — stays the reference for the control plane's own store layer.
- **Auto-compaction (re-verify against our pin).** Current SDK docs describe
  subprocess-level auto-compaction: on resume the agent sees a *compacted* chain
  (`getSessionMessages`), while `load()` still returns the raw entries. If that holds on
  our pinned version, the old "replays the entire transcript toward the limit" worry is
  largely handled by the SDK and the adapter just owns raw-entry retention. Compaction
  was *absent* at v0.2.110 — **confirm before relying on it either way.**
- **Caveat — resume carries the conversation only**, not settings/hooks/MCP/permission
  config. Those are **baked into the dev image** — which is desirable: the walls (§8)
  live in the image, not in resumable state the agent could touch.

So state per task splits three ways, all keyed by issue and reconstituted each burst:
**branch** (workspace ← GitHub) · **session** (context ← control-plane `SessionStore`)
· **issue/PR thread** (status + human conversation ← GitHub).

### State & storage

The control plane owns both stores **and all I/O to them**; the dev agent holds no
storage credentials and touches only a mounted working dir. Nothing durable lives in
the ephemeral container.

> **As built:** both stores are presence toggles now (`STORES.md`, Stage A).
> Unset, they are what the MVP shipped — a SQLite ledger and a filesystem archive
> under `--data-dir`, zero services to operate at experiment scale. Set
> `LEDGER_DSN` and the ledger is Postgres, whose schema numbered migrations own;
> set `ARCHIVE_URL` and the archive is an S3 bucket. The archive is a tree of
> per-burst snapshots either way (A5): `data/volumes/` still holds per-burst
> forensics, but `sessions/{task}/{burst}/session.tar.gz` replaced the
> task-keyed blob, and the ledger owns the lineage. The full as-built data model —
> ledger tables, both stores, and the GitHub entities they reference —
> is drawn in `diagrams/data-model.drawio` (+ `.png`).
>
> **As built (C1):** the product registry opened — `control-plane product
> register|list|show|pause|resume|set`, the platform's first operator verbs.
> Registration materializes every policy/quota column from the platform env into
> the `repositories` row (no NULL-means-inherit, no retro-propagation), each
> mutation writes its `audit` row and outbox event in the same statement, and
> `tasks.repository` became a foreign key: a task for an unregistered product is
> unrepresentable. The scheduler still reads its policies from the env — rows
> become the source at C3, and the scheduler becomes registry-driven at C4.
>
> **As built (C2):** registration preflights (TO-BE §2). The gate refuses a
> declaration the platform cannot execute — the org App can't read the repo, a
> review provider without its credentials (the scheduler-startup error,
> relocated to registration), `e2e on` without a parseable `TESTING.md` whose
> declared env the product's refs cover and resolve. Everything else is socket
> lint: warned, recorded in the registration's audit row, re-runnable with
> `product check` to catch drift; `--lint-issue` files the gaps as a `human`
> issue in the product repo. All probes read through the org App's own
> installation token, so "the App can read the repo" and "the manifest is
> fetchable" are one fact.
>
> **As built (C3):** a registered product's policies reach the scheduler from
> its row — `ReviewPolicy.from_product` / `E2EPolicy.from_product`, the same
> constructor registration preflight (C2) uses, so registration and startup can
> never disagree about honorability. The row is the entire truth about the
> declaration (toggle + selector); the env answers only machine questions —
> credentials and model overrides. `from_env` stays as dev mode for
> unregistered repos, until C4 makes the scheduler registry-driven.
>
> **As built (C4):** the scheduler is registry-driven — `run` without `--github`
> serves every active product through one serial loop (`Fleet`, SCHEDULER.md).
> The registry is re-read every tick, so register/pause/set take effect without
> a restart; fairness is least-recently-served on `repositories.last_burst_at`,
> quota is the row's `daily_bursts`, and every ledger query a product's
> scheduler makes is scoped to its repository — two products in one ledger
> cannot see each other's tasks. One burst per tick keeps the trust-first
> serial invariant; with one product the loop degenerates to the MVP's.
>
> **As built (D1):** App credentials follow the role-prefix scheme (TO-BE §4) —
> one App per pipeline role, the dev App on the bare `GITHUB_APP_*` names and
> reviewer/tester/monitor on the same names behind their prefix (`MONITOR_`
> reserved until T4). App names are the adopter's choice; the prefixes are the
> platform's contract. `github-check` preflights every configured role — a
> declared App that cannot mint is a check failure, never a silent skip. The
> tester's own posting identity arrives at D3.
>
> **As built (D2):** burst tokens are downscoped at mint (TO-BE §4's two
> tiers): `repositories=[the task's repo]` plus the role's permission subset,
> so a burst for product A holds a token product B rejects by construction —
> GitHub enforces the wall, not shell discipline. The control plane's cached
> tier keeps the App's full grant; only the dev role carries a burst
> credential at all (reviewer/tester bursts stay credential-free).
>
> **As built (D3):** the tester has its own provenance — test reports and the
> relayed `auto` issues post under the `TESTER_`-prefixed App, and the tester's
> `REQUEST_CHANGES` triggers dev rounds through the same machine-reviewer
> registry. `e2e on` without that App refuses at startup and at registration
> preflight (the honor-check lives in `E2EPolicy`, mirroring the claude
> provider's). The MVP's deferral — the tester riding the reviewer App — ends
> here; the tester burst itself still holds no GitHub credential.
>
> **As built (D4):** secrets are materialized by the adopter and resolved by
> ref (TO-BE §4): `SecretResolver` takes `env:`, `file:` and `helper:` refs —
> the helper is one binary named out-of-band by `SECRET_HELPER`, the
> git-credential-helper pattern, so a vault is the adopter's instrument and
> never a platform dependency. App private keys and the tester's sandbox
> credentials resolve through it at the moment of use; the ledger keeps refs
> (`repositories.test_env_refs`), preflight probes that a ref points at
> something, and the value exists only between resolve and the container env.
>
> **As built (D5):** roles are rows (TO-BE §4, "ceilings in code, composition
> in data"). The four pipeline slots are seeded rows whose wall-bearing fields
> (`role`, `kind`, `identity`, `sink`, `tools`) are frozen by the store — a
> trigger on both backends, refused by name before it — and whose open fields
> (`prompt_preamble`, `model`, `max_budget_usd`, `max_turns`, `mount_mode`) are
> `role set`'s, audited. `execute_burst` reads the row at every spawn, so an
> edit governs the next burst with no restart; the env knobs that used to carry
> model/budget/turns are retired (startup refuses them). Ceilings stay code:
> the tool allowlist, the image's role preamble, the deny floor, and read-only
> mounts for every reader — a row narrows, never widens. Tool narrowing and
> custom-role execution reach the container together, with T4's triggers.
>
> **As built (E1):** the ledger discipline is enforced, not described (TO-BE
> §5, "transactional, but not interactive"). Work is claimed as a lease — one
> `UPDATE … FOR UPDATE SKIP LOCKED … RETURNING` stamps `tasks.claimed_by` /
> `claimed_until` on the oldest eligible row, with the cooldown in its WHERE
> and the daily ceiling behind the one advisory lock the design allows —
> every transition is a compare-and-set on the (status, holder) the replica
> read, and the lease is renewed by a heartbeat while the container runs. A
> replica that dies leaves a lapsed lease any peer reconciles; a live lease is
> proof of life from any host, so `docker ps` on one machine never judges a
> burst on another. No code path holds a transaction across I/O, and the suite
> asserts it at every seam of a tick, on both backends. Two replicas over one
> ledger is the shape this makes possible; the test that runs them is E2.
>
> **As built (E2):** two replicas over one ledger, proven in CI — two real
> control-plane processes on one Postgres, one SIGKILLed mid-burst, the
> survivor absorbing the orphan after its lease lapsed, with one `succeeded`
> burst per task and nothing doubled (`tests/test_replicas.py`). Observation
> fans out as a lease too: the fleet takes each product's turn through its
> registry row, one poll interval long, so N replicas poll GitHub once. The
> product's registered `concurrency` (default 1) is counted at admission across
> the fleet, so a second replica is a hot standby until the row says otherwise.
> And a holder that cannot renew its lease for two thirds of its span fences
> its own burst — the row is its only proof of life, and it stops before a peer
> may lawfully start the successor.
>
> **As built (E3):** the kill switch is a ledger row. `org_state` — one row,
> `stop_reason`/`stopped_by`/`stopped_at`, seeded by migration 0016 — replaces
> the MVP's STOP file and local `docker kill`. `control-plane stop --reason …`
> sets it by compare-and-set with its audit row and `org_stopped` event in one
> statement; `resume` clears it. Every loop reads the row at the top of every
> tick and before every spawn and keeps ticking while stopped, so a `resume`
> restarts nothing; every live burst's heartbeat reads it on every beat and the
> runner holding the burst kills its container — a `stop` on one host reaches a
> burst on another through the row, the heartbeat and the runner. Fenced
> bursts are `killed`, not `failed`; SIGTERM is the same fence with a
> process-local flag and no ledger write. `tests/test_replicas.py` runs the
> done-when: a stop from a third process kills a burst one replica holds, and
> the work completes after `resume`.
>
> **As built (E4):** the runner is a protocol. `Runner` — `run(spec, log_path,
> heartbeat) -> int`, `alive(burst_id) -> bool` — joins `Ledger` and
> `SessionArchive` as the third seam (TO-BE §6), with `DockerRunner` behind it
> and its behavior untouched. The scheduler holds one runner per process,
> chosen in one place (`open_runner`, one presence toggle: `BURST_NAMESPACE`
> makes it `JobsRunner`, G3),
> and asks it to run every burst and whether an orphaned one is still alive;
> `docker` is spelled nowhere outside `runner.py`. What the box is given is
> decided once for every runner — `burst_env`: the one credential, the agent's
> OTel, the tester's sandbox refs — and a runner only packages it. The runner
> tests are one parameterized contract suite, each runner bringing a rig; the
> docker rig runs the real pump, tee, clock and fence with a shell in the
> daemon's seat, so the contract runs without docker, and G3 adds the Jobs rig
> to the same suite rather than writing a second one.
>
> **As built (E5):** webhooks as wake-up, polls as truth (TO-BE §5). A third
> workspace package, `webhook-receiver`, is the platform's only public surface
> — an HMAC filter: it verifies each App delivery against the App's webhook
> secret (refs, through the shared `secret-refs` leaf), drops replayed delivery
> ids from memory, and forwards a wake (`repository`, `kind`, `delivery`) to the
> control plane's internal endpoint; it never imports the control plane, and its
> image provably cannot. The endpoint makes one ledger write, `wake_repository`
> — the product's observation lease cleared, a `product_woken` event with the
> delivery id — and the replicas' `wakes` channel wakes every loop, which sleeps
> on a flag rather than a timer; the observation lease still makes exactly one
> replica poll. Forgery and replay are harmless by construction: a forged
> delivery is refused and remembered nowhere, a replay is dropped, and the worst
> either can cost after a receiver restart is one poll of real GitHub state —
> never a state transition. `tests/test_wakes.py` runs it as processes: two
> replicas and the receiver on one Postgres. The platform now ships as images
> (control-plane, webhook-receiver) with a compose `platform` profile — the T0
> deployment on a Docker host — and reference k8s manifests, validated in CI.
>
> **As built (E6, the gate):** the laptop retired — the whole platform ran as
> containers on a Docker host (the laptop itself, for the rehearsal) behind a
> public HTTPS route, and the checklist held: ready → claim in **6.1 s** through
> the webhook path; a replica SIGKILLed mid-burst left an orphan container the
> survivor watched (`runner.alive`) and never doubled, then marked killed and
> retried — the retry adopting the orphan's PR by branch prefix; the org STOP
> row held for hours with a `ready` issue pending; GitHub's own redelivery and
> a forgery through the route were zero polls and zero writes; the single-node
> pair stayed byte-for-byte. The gate paid for itself four times: a wake that
> fires before GitHub's read side is consistent now **settles** (`WAKE_SETTLE_S`
> — the first measurement was 65 s, a poll that missed the label and held the
> lease); the receiver gets its own environment file, never `.env`; the App keys
> live under a same-path `PLATFORM_SECRETS` mount; and a transport failure on a
> GitHub call is a `GitHubApiError`, not a dead replica. `DEPLOYMENT.md` is the
> operator's runbook and the rehearsal's record.

- **Control DB — Postgres.** The queryable index the scheduler reasons over: `issue ↔
  session_id ↔ branch`, the burst ledger (when/where/cost/tokens), lifecycle status,
  provenance, the violations ledger (§9), and each transcript's object key + checksum.
- **Object store — SeaweedFS (S3-compatible).** The raw session transcripts (one JSONL
  part-file per `append()`, per the SDK's S3 storage model) plus any large artifacts.
  Self-hosted with an S3 API, so the SDK's stock S3 `SessionStore` adapter works
  unchanged — point its client at the SeaweedFS gateway, no cloud dependency for the
  experiment.

Keeping raw transcripts out of Postgres keeps the control DB small and queryable, dodges
the write-amplification bloat that per-entry relational checkpoints are known for, and
keeps raw agent context (code, tool output, secrets that may leak into logs) out of the
operational DB — consistent with the walls (§8).

### Review — a policy of providers (`REVIEWERS.md`)

Machine review is a **toggle + provider selector**, declared in the platform config
and verified loudly at startup. Every provider delivers the same artifact — a PR
review with inline findings, on GitHub — so there is one review surface, one trigger,
one etiquette:

- **`claude`** — a **reviewer burst** (`assess`): same image, reviewer role, fresh
  session, clone mounted read-only at the PR head, **no GitHub credential** — findings
  return via the harvest and the control plane posts them as a second GitHub App (the
  reviewer App; findings → `REQUEST_CHANGES`, clean → `APPROVE`).
- **`codex`** — the same reviewer envelope with the codex CLI as the engine (slice 3,
  not yet built).
- **`copilot`** — a request to GitHub's reviewer, sent on the owner's PAT and
  echo-verified (App tokens are silently dropped — the 201 lies).

Findings drive **autonomous dev rounds**: the dev burst resumes its session, fixes,
and replies in every thread; a push re-queues assess. The earlier design's **pre-PR
in-container Codex self-review was dropped** (decision record in `REVIEWERS.md`): its
output wasn't a platform artifact, and it put a second vendor's credential inside the
dev's box. The human remains the final gate regardless of policy.

### Testing — the tester role (shipped 2026-07; `TESTER.md`)

Reviewers read diffs; **nobody has run the thing**. The tester is **QA, not a third
reviewer** — the goal is to behave like a human tester. Same image as the dev, booted
with a tester contract, spawned as a **test burst** after a clean machine review
(only a clean assess queues a test — no sandbox boot for a diff that's about to
change):

- **Designs its own test scenarios** from the product's specs and its `TESTING.md`
  (the product-owned testing contract: frontmatter names the env it needs, prose
  tells the tester how this product is driven) — happy path, edge cases, "what would
  a user actually try" — rather than executing a checklist someone else wrote.
- **Runs them against a live system**: boots the product from the read-only PR-head
  clone and drives it end-to-end in the **sandbox** (a disposable instance of the
  product's runtime; creds provisioned platform-side under `TEST_*` and injected
  only as the intersection with what the manifest requests).
- **Reports** structured results returned via the harvest (it holds **no GitHub
  credential**); the control plane posts them: PR-caused defects as a
  `REQUEST_CHANGES` review that drives a normal dev round, pre-existing defects
  relayed as `auto` issues behind the human `ready` gate, a clean run as an advisory
  comment. Advisory input to the human at gate 2 — not a required check.

Explicitly *not* CI's job re-done by an agent: deterministic unit/integration/smoke
tests stay in CI as the reproducible floor. The tester covers what scripted tests
can't — scenario invention and exploratory judgement ("the command works but the
error message is nonsense").

Independence is engineered, not assumed:

- **Separate context.** Fresh session per test burst — no persistent tester session,
  no access to the dev's transcript. It tests "does the built thing satisfy the
  spec," not "did the dev do what it intended." Round 2 starts clean — no anchoring
  on round 1's verdict; its durable output is the PR thread.
- **Disjoint credentials** (the role asymmetry, §8): dev writes code but holds no
  sandbox creds; tester holds sandbox creds but is repo-read-only. Neither can ship a
  change *and* validate its own claim about it.
- **Its report is machine-generated data** entering the dev's context — same provenance
  class as monitor issues (§8): the dev reproduces findings first-hand, never executes
  report prose as instructions.

> **As built (B5):** the tester's transcript is archived — same per-burst key
> scheme as the dev's, but as *evidence*, never memory: the session-tree leaf
> query filters on `role = 'dev'`, so no non-dev snapshot can become what a dev
> burst resumes. The envelope has the tester boot the product with stdout
> captured to `product.log` on the session volume, so the archived artifact
> holds what the product printed, not only what the tester typed. On top of it
> (B6), a scenario's evidence is a **citation, not prose**: each cites a
> `tool_use_id` from its own run, the control plane resolves it against the
> transcript (exists, has a recorded non-error result), an unresolvable
> citation fails the report the way malformed JSON does, and the posted table
> carries a platform-extracted *cited call* column beside the tester's words.
> The tester cannot assert what it did not do; judging whether the cited call
> *proves* the claim stays with the human reader, record in hand.

## 7. The monitor

Separate service (own repo, own creds, own lifecycle). Watches the product's
**logs, metrics, and deterministic alerts (e.g. Alertmanager)** and **files GitHub
issues** describing symptoms.

- It **only produces issues** — it never talks to the dev directly. The issue is
  the clean, auditable, rate-limitable boundary.
- **An issue means a code change is needed.** Operational-only signals (transient errors,
  capacity, config drift) are *not* issues — they stay in the alerting plane (Alertmanager
  / dashboards) for humans. The monitor is the narrow bridge from code-change signals to
  issues, not the general alerting system.
- Monitor-filed issues carry **`auto` provenance** (see §8).
- Issues are **structured/templated**, not raw log dumps — narrows injection surface.
- Rate-limited so it can't manufacture unbounded work.
- **Future (not now):** the monitor reacts (opens its own remediation tasks) rather
  than only reporting. Designed-for, out of current scope.

## 8. Trust model & enforcement

Hybrid: catastrophic boundaries are **walls** (physically impossible); process
rules are **tripwires** (instructed, observed, counted as data).

| Boundary | Mechanism | Type |
|---|---|---|
| Can't deploy to prod | branch protection (no-merge identity) + deploy-on-merge | wall |
| Can't edit its own maintainer | container holds only the product clone; platform code isn't in the box | wall |
| Repo-scope confinement | the container *is* the scope | wall |
| Blast radius | GitHub App scoped to product repo, no merge/admin/secrets | wall |
| No prod creds (product tokens, DB) | simply absent from the dev environment | wall |
| No storage creds (agent is a stateless worker) | control plane owns all persistence; agent touches only a mounted working dir | wall |
| No agent validates its own work (role asymmetry) | dev: repo write, **no sandbox creds** · tester: sandbox creds, **repo read-only** — disjoint credential sets | wall |
| Sandbox blast radius | disposable product runtime, throwaway rotating creds, no real users — the tester *executes dev-written code*, so what that runtime holds must be worthless | wall |
| Untrusted user content | handled by the **separate** monitor + structured issues | wall |
| Reproduce-first · write report · small reversible changes | operating contract; observer logs every deviation | tripwire (data) |

### The injection-laundering path (the critical one)

```
end user sends malicious content to the product
  └▶ product logs it ─▶ monitor files an issue containing it
        └▶ dev reads issue ─▶ ⚠️ could execute the injection as "work"
```

**Provenance gate** breaks it:

- **`human` issues** → spec is *trusted instruction*; the dev may act on its prose.
- **`auto` issues** (monitor/agent-filed) → body is *tainted data describing a
  symptom*, **never an instruction**. The dev reproduces from first-hand evidence
  (runs the failing test itself), acts only within a fixed envelope
  (reproduce → verify → propose), and an auto issue can **never** widen the agent's
  own scope or permissions.

The **tester's test reports are the same class**: machine-generated, structured
(scenarios run, pass/fail, repro steps) — the dev treats findings as symptoms to
reproduce first-hand, never as prose to execute. Sandbox content can reach the tester's
report (it drives a running bot), so the laundering rule applies end-to-end.

Upstream of this, **human triage (§6, gate 1)** is an additional control: no issue
— auto or human — is worked until a human approves it. Triage governs *whether*
the work is legitimate; the provenance gate governs *how* the body is interpreted.

## 9. Observability & metrics

Telemetry lives in the **platform's** store (never the product repo, so the agent
can't read or skew its own scorecard).

- **Reliability** → task success rate · blocked/needs-review rate · rework rate · cost/task
- **Containment** → wall-hit count · attempted-violation count · **successful-breach count (must be 0)**
- **Capability** → automated-review pass-rate (Codex + Copilot) · human-review pass-rate · post-merge defect rate · task difficulty handled
- **Violations ledger** — every tripwire firing and every wall block, logged. This
  is the experiment's primary dataset.

Each family is one SELECT: `reporting.reliability`, `reporting.containment`,
`reporting.capability` over `reporting.task_facts` (B3, migration `0010`). The
denominators, and the four metrics above that the ledger has no source for — review
verdicts, human-review pass-rate, post-merge defects, difficulty — are written down
in STORES.md, "Reporting". Breach rate is the one that is measured rather than
queried: it is what the walls did *not* catch, so no row holds it and G6's M4 suite
is what produces the number.

### Tracing

**OpenTelemetry end-to-end**, in the code from the first line (the backend can come
later; retrofitting correlation can't). Span naming follows the OTel **GenAI semantic
conventions** where applicable (still experimental) and Claude Code's built-in
`claude_code.*` spans as-is.

- **Trace = one burst; the task is a correlation attribute.** A task can span days,
  and multi-day open traces break tooling — so each burst is its own trace. Every
  span/event carries `task.id` (= issue), `burst.id`, `role` (dev|tester), and each
  burst links to its predecessor via span links. The burst ledger (Control DB) stores
  each burst's `trace_id` **and the id of the burst span in it** — a link names a
  span, not a trace, so one half of the pair would only buy a link that resolves to
  nothing. Task-level assembly is a query.
- **Context propagation is built in:** the control plane opens the burst span and
  injects `TRACEPARENT` into the container env; Claude Code in SDK/non-interactive
  mode adopts it, nesting its span tree (`claude_code.interaction` →
  `llm_request` / `tool` / `hook`, subagents, even Bash subprocesses) under our burst
  span. No custom instrumentation inside the box.
- **Agent-side telemetry is Claude Code's own OTel** (B2): traces (beta, two
  switches) and structured events (`tool_result`, `tool_decision`, `api_request`,
  `user_prompt`), configured entirely by env the runner injects — no custom
  instrumentation inside the box. `OTEL_RESOURCE_ATTRIBUTES` stamps every signal with
  `task.id` / `burst.id` / `burst.role` / `burst.kind`, so a query written against one
  signal works on the other. Content flags: tool **arguments** on
  (`OTEL_LOG_TOOL_DETAILS`, prompts) — tool visibility *is* the experiment's dataset —
  and tool/API **bodies** off, because spans stay thin (below) and the transcript is
  the deep record. **Metrics are deliberately not exported**: TO-BE §12 makes the
  ledger the metrics store, and the CLI's counters are cost and tokens, which the
  burst row already holds as the authoritative copy.
- **The endpoint is the one egress widening, and it's the adopter's** — a burst talks
  to GitHub, the inference API, and now one collector, named by
  `DEV_AGENT_OTLP_ENDPOINT` (the control plane's own `OTEL_EXPORTER_OTLP_ENDPOINT` is
  a different variable because the two processes are in different network namespaces:
  `localhost` inside the box is the box). Unset ⇒ no telemetry env enters the
  container at all. Declaring that host per burst and enforcing it is G6/#47; until
  then the note is the wall. Two consequences worth stating: the collector sees tool
  arguments, which for a tester burst can include a command that names a credential;
  and an *authenticated* endpoint would want its header token behind a
  `SecretResolver` ref (D4 shipped the resolver; wiring
  `OTEL_EXPORTER_OTLP_HEADERS` through it rides with G6).
- **One instrumentation point, two consumers.** `tool_decision` / hook events feed
  both the trace stream (debugging) and the **violations ledger** (the §9 dataset).
  Ledger derivation is control-plane-owned — tripwire-relevant events are also
  written to the burst volume and harvested with the session, so the agent needs no
  DB or telemetry-store access.
- **Telemetry is outward-only for agents:** they emit, never read — the concrete
  mechanism behind "can't skew its own scorecard."
- **Spans stay thin.** The deep record of agent behavior is the session JSONL in the
  object store; spans carry timing/outcome/cost/IDs plus the transcript's object key,
  never embedded content.
- **Backend: none, and a rig to prove it** (B4). Chosen over Langfuse (v3 self-host
  needs ~6 services incl. ClickHouse + Redis) at experiment scale, Phoenix is still
  the trace UI — but as one service in a dev-only rig
  (`docker/compose.telemetry.yaml`), not as a component: TO-BE §12 ships four
  capabilities and no instrument. Two things changed in the wiring once it was built.
  A **collector sits in front** rather than Phoenix's own OTLP listener, because logs
  need a second destination (Phoenix takes traces only), both processes should learn
  exactly one address, and the swap then happens in a config file instead of in an
  environment variable the platform sets. And Phoenix keeps **its own store, not our
  Postgres**: a viewer's tables in the platform database are drift in every
  `atlas schema diff`, and a rig's state is meant to die with `down`. The correlation
  join, the alert condition and the reference dashboards are `OBSERVABILITY.md`.

## 10. Rollout phases

Same architecture throughout — exposure is graduated, not the design.

0. **Walls-first:** single dev container, human-filed issues only, static/staging
   product. Prove **0 wall failures** over N runs. → build plan in `PHASE-0.md`.
   **Status: built and running** — the loop is proven end-to-end; the systematic
   red-team suite (M4) is still to run.
1. **Loop:** add the **tester** (test bursts + sandbox — **pulled forward and shipped
   2026-07**, so capability metrics mean something), then the monitor + auto issues
   on staging / recorded traffic (the provenance gate and the `auto` relay pipeline
   are already live via the tester). Watch the laundering path specifically.
2. **Live:** real user traffic, then horizontal scaling. Only after 0–1 show
   zero successful breaches.

## 11. Scope: now vs. future

- **Built (MVP):** control plane spawning **one** container per burst on Docker;
  human-triaged intake (`ready` label); GitHub-native flow (issue → PR → human
  review → merge → CI deploy); machine review as a provider policy (`REVIEWERS.md`)
  with autonomous dev rounds; the **tester role** with E2E test bursts and the
  `auto`-issue relay (`TESTER.md`); the wall set in §8.
- **Designed-for, not built:** the monitor (files issues from prod telemetry — its
  pipeline is already exercised by the tester's symptom relay); the `codex` review
  engine and the tester tools registry; Postgres/SeaweedFS stores; OTel/Phoenix
  tracing; horizontal scaling (N containers); monitor **reaction**
  (auto-remediation); k8s as the runtime when N>1 is real.

## 12. Decisions

**Resolved**

- **Repos:** control plane + dev agent in **one** repo behind a clean
  `control-plane` / `dev-agent` module seam (cheap to split later if the developer
  becomes a reusable, product-agnostic artifact). Monitor is its own repo.
- **Container lifecycle:** ephemeral **per work-burst** with resume-from-durable-state
  (§6). Warm base image to amortize setup; warm pool only if setup is heavy.
- **Intake:** human-triaged via a `ready` label (§6, gate 1).
- **Review:** a **toggle + provider selector** (`REVIEWERS.md`): `claude` reviewer
  bursts posting as a second GitHub App, optional `codex` engine, optional `copilot`
  request on the owner's PAT. *Supersedes* the original two-stage design — the pre-PR
  in-container Codex self-review was dropped (its output wasn't a platform artifact;
  it put a second vendor's credential in the dev's box).
- **Tester:** a **QA role, not a reviewer, not a codebase** — the dev image booted with
  a tester contract and disjoint creds (no GitHub credential — reports return via the
  harvest; product-sandbox creds the dev never has). Behaves like a human tester:
  invents scenarios from the product's `TESTING.md` + specs and runs them against a
  live sandbox boot of the PR head — the non-deterministic complement to CI, not a
  replacement for it. **Fresh context per test burst** — never sees the dev's session,
  deliberately blind to the PR conversation. Advisory report, not a required check.
  **Shipped 2026-07** (slices 1–2, `TESTER.md`).
- **Staging:** the RC-validation env; product CI deploys the PR/branch there.
- **Session continuity:** one persistent Claude session per task, externalized via a
  control-plane `SessionStore` adapter; resume by `session_id` each burst (§6).
- **Storage:** the **platform owns all persistence** — Postgres (index) + **SeaweedFS**
  (transcripts + artifacts). The dev agent is a **stateless worker with no store creds**;
  the control plane loads the session into a mounted `CLAUDE_CONFIG_DIR` before boot and
  harvests/mirrors it after (SDK **default local storage**, not the in-agent
  `SessionStore` adapter). Raw transcripts stay out of Postgres (bloat + containment);
  retention lives in the object store (§6, "State & storage").

- **Anthropic auth = the owner's subscription**, not API billing: `claude setup-token`
  → a 1-year, **inference-only** OAuth token in the control-plane env
  (`CLAUDE_CODE_OAUTH_TOKEN`), injected per run-burst as the container's **only**
  Anthropic credential. Never inject an API key alongside it (the CLI prefers the API
  key and would silently switch billing). `.credentials.json` is excluded from session
  harvest unconditionally. Consequences: consumption is measured in tokens; all bursts
  share one subscription's rate limits (a real constraint on N>1 scaling, phase 2);
  personal-experiment use — a multi-tenant service would need API billing.
- **Tracing:** OpenTelemetry end-to-end from the first line of code; trace-per-burst
  with `task.id`/`burst.id`/`role` correlation attributes + span links across bursts;
  `TRACEPARENT` injected by the control plane so Claude Code's native spans nest under
  the burst span; Phoenix as the phase-0 OTLP backend (single container, swappable);
  agents emit but never read telemetry (§9, "Tracing").

- **Scope — code maintenance only (2026-08).** What the platform generalizes over is
  not "code" but its gate structure: the agent's output is an **inert artifact until a
  human promotes it**, and the promoter (CI), never the agent, holds the effectful
  credentials. Anything expressible as a repo + CI fits — operational work (e.g. a
  client-onboarding runbook) enters only as **config-as-code repos, i.e. additional
  product instances** (multi-product, not a new execution paradigm). A plan/apply
  "operator role" for effects that can't be repo-mediated was considered and
  **rejected**: business ops has no state model, so a reviewed plan's staleness is
  undetectable (Terraform's plan/apply works only because state makes drift
  detectable) — the industry's per-action-approval default (2026 survey) is the honest
  fallback there, and importing it would dissolve the reviewable-artifact gate this
  platform is built on.
- **Intake — a port, not a substrate (2026-08; design settled, build parked below).**
  A task lives in exactly **one home** — the company's tracker — never mirrored. The
  control plane reads it through a small **intake port** (~6 ops: list ready · reflect
  status · comment · transition · file · observe re-approval) speaking a
  **tracker-agnostic task contract** (the issue template's fields + provenance + the
  ready act) — the contract replaces "a GitHub issue" as the narrow waist. GitHub
  Issues becomes the default in-process adapter and the executable spec; user-built
  connectors plug in out-of-process behind one `remote` adapter, held to a conformance
  kit. **Delivery (branch/PR/review/merge) stays native GitHub, never abstracted.**
  Wake channel: one generic authenticated endpoint whose payload is **discarded** —
  poll-for-truth (`SCHEDULER.md`) makes it tracker-agnostic for free. Trust:
  provenance = *authorship*, not transport; the human `ready` act may live in the
  tracker, which makes the connector gate-1 load-bearing — so connectors are stateless
  translators that decide nothing and hold tracker creds only, and the control plane
  snapshots the task contract into the ledger at claim time (auditable).

- **Schema authoring — a declared desired state, diffed (2026-08).** The schema is
  stated once, in `schema.sql`, and **Atlas** computes the difference between it and
  what the migration directory actually builds; CI asserts that difference is empty
  on every PR. Before this, "the canonical DDL is authoritative" (`STORES.md`) was a
  claim about a markdown block that nothing verified, and Stage A roughly triples the
  schema (A4–A6) — the drift would have been found at gate A7 or by a reader, not by
  a test. **Alembic was rejected**: it brings SQLAlchemy into a store layer whose
  design is no ORM and four hand-written statement shapes, and its autogenerate wants
  ORM metadata this repo will never declare. `migra`, the pure-Python schema-diff
  route, has been deprecated since 2022, so declarative diffing in Python is Atlas or
  nothing.

  Three boundaries make this cheap rather than a dependency:

  - **Atlas never applies anything.** `control-plane migrate` still owns the apply
    path and `schema_migrations`, so no server needs the binary — only a developer's
    machine and CI. `atlas.hcl` deliberately omits `url`/`src` so that
    `atlas schema apply`, which would rewrite a database to match `schema.sql` and
    skip migrations entirely, fails for want of a target instead of working.
  - **Atlas never owns the migration directory.** It wants its own `atlas.sum` and
    its own timestamped file names; the directory already has an owner with a
    different convention (`NNNN_slug.sql`, contiguous, enforced at discovery). Atlas
    computes the SQL and a human puts it in the next numbered file.
  - **Its view has a documented hole**, wider than first written and now measured
    against a real migrated database (A4). The community version reads tables,
    columns, indexes and constraints, and **skips triggers, functions, privileges
    and table data** — which is `roles_pipeline_guard`, every grant including the
    append-only ones, and the seeded pipeline role rows. It also does not read
    `pg_constraint.convalidated`, so it calls a database whose `VALIDATE
    CONSTRAINT` step never ran *synced*: a wall enforced for new rows only looks
    identical to one that holds for all of them. The first three are verified by
    provoking them (A4's *done when*), which is the better proof anyway; the
    fourth is one query in the same suite. The schema diff is not to be read as
    the whole truth about the database. The advanced `migrate lint` suite is Pro;
    the unsafe-DDL niche it covers is #56's, free.

**Still open**

- Transcript-growth policy — **gated on the auto-compaction re-verify (§6).** If the SDK
  compacts the resumed chain on our pinned version, this is largely handled; otherwise
  the adapter's `load()` owns truncate/summarize (and/or a turn cap). Defer until a task
  spans enough rounds to matter.
- Warm-pool threshold — defer until per-burst setup time is measured.
- ~~Whether `max_budget_usd` enforces anything under subscription billing~~ —
  **resolved (task 17): it's a hard cap**; the SDK kills the burst mid-stream on
  estimated cost. Resume-on-any-death makes retries an installment plan (§6).
  Rate-limit backoff tuning for the scheduler stays open.
- ~~How the tester *drives* the product E2E in the sandbox~~ — **resolved: the
  socket-and-plug model** (`TESTER.md`). "How to test this product" is product
  knowledge and lives in the product repo's `TESTING.md` (frontmatter for the
  platform, prose for the tester); the platform owns only the envelope and stays
  product-agnostic.

**Parked (2026-08)** — recorded so they aren't relitigated; deliberately unscheduled.
M4 (the red-team suite) is the open milestone; nothing here jumps that queue.

- **Intake docs:** an `INTAKE.md` (task contract, port ops, wake rule, connector
  discipline) subsuming the intake half of `GITHUB-INTERFACE.md`, which trims to the
  pure delivery protocol (labels become the GitHub adapter's *encoding* of contract
  concepts, not the concepts).
- **Intake-port refactor** (first post-M4 code task; pure refactor, no behavior
  change): `github_api.py` behind the port; `observe_github()` splits into
  `observe_tasks()` (port) + `observe_delivery()` (native GitHub); ledger task keys →
  `(source, external_id)` with a branch-safe encoding replacing `dev/<n>-`;
  `Closes #n` auto-close → port `transition(done)` on observed merge; claim-time task
  snapshot into the ledger.
- **Linear adapter** — gated on a second tracker being real.
- **Remote connector protocol + conformance kit** — gated on a second operator being
  real ("users plug their own connectors").
- **Automations / scheduled bursts** — two distinct ideas if revived: a **time
  trigger** as a third reconciler trigger kind (first candidate: a daily digest over
  the burst + violations ledgers), and the **monitor**, whose first form is likely a
  scheduled report burst. Read-only reporting needs no gates; anything filing issues
  goes through the existing `auto` pipeline.
