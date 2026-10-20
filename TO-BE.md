# Autonomous Maintenance Platform — TO-BE architecture

Target architecture for the platform as an **internal Fermata product**: an org-wide
service that maintains **dozens of repos** on behalf of their owning teams. This
document is the design target; `ARCHITECTURE.md` + the subsystem docs describe the
proven MVP it grows from. Nothing here is speculative capability — every mechanism
generalizes something the MVP already proved end-to-end (discord-bot task 17: issue →
work bursts → autonomous review rounds → live E2E test → human merge).

> Status: **draft for team review** (2026-07). Decisions marked *(DR)* are proposed
> decision records; open questions are collected in §15.

---

## 1. What changes and what doesn't

**The MVP proved the shape.** These invariants carry over untouched:

- **GitHub is the only work substrate.** Issues = queue, PRs = delivery + review,
  branch protection = the ship gate. The platform never grows its own task tracker,
  and the console (§8) never becomes a GitHub replacement.
- **Two human gates bracket an autonomous middle** — `ready` (intake) and merge
  (ship). Teams keep owning both, in GitHub, exactly as today.
- **Subscription-powered, not API-billed** *(DR)*. The whole experiment runs on
  Claude subscriptions and the TO-BE keeps that: every team member has one, and the
  platform's compute is the team's **pooled subscription capacity** (§4). The open
  design axis is the *mechanism* (token pool vs member-hosted runners), never the
  billing model.
- **Bursts are the unit of compute**: ephemeral container + durable session, one
  container per burst, resume-on-any-death (installment-plan budgeting).
- **Provenance gate + laundering defense**: `human` = trusted spec, `auto` = tainted
  symptom data; evidence blocks untrusted always.
- **Socket and plug**: product knowledge lives in the product repo (`CLAUDE.md`,
  `TESTING.md`); the platform owns only envelopes and stays product-agnostic. This is
  what makes "dozens of repos" a registration problem, not an integration project.
- **Walls before tripwires**; agents hold no storage credentials; labels reflect,
  the ledger locks.
- **Config taste**: minimal orthogonal controls — toggles and selectors, no tiers,
  no modes, no per-product platform code.

**The MVP also measured its own ceilings.** The TO-BE exists to remove exactly these:

| MVP ceiling | Consequence at org scale | TO-BE answer |
|---|---|---|
| Serial loop, one burst globally | a day of wall-clock per task, mostly queueing | N runners, per-task serialization (§6) |
| Laptop process | no availability story, no webhooks | control plane on k8s (§5) |
| SQLite + filesystem | single-operator stores | Postgres + S3 object store (§5) |
| One owner's subscription | one person's rate limit = org throughput | the team subscription pool (§4) |
| Ledger surgery as operator UX | SQL against prod state (finding #9) | operator API + live console with audit (§8) |
| Memory dies with the task | every burst relearns the product's gotchas | platform memory (§9) |
| Review is sampling (finding #7) | ~1.6 of ~7 defects found per round; QA loop ≈ 85 % of task cost | parallel lens-scoped review + verify round (§10) |
| Nobody watches prod (finding #11) | integration breakage waits for a human to notice | the monitor, per-product (§11) |

## 2. Product model — the tenancy unit

Single-tenant platform (one Fermata org), **multi-product**. A *product* is a
registered entity in the control plane:

```
product:
  repo            github.com/fermata/<name>
  team            owning team (gate authority, Slack channel)
  contracts       CLAUDE.md · TESTING.md — live in the repo, never here
  review policy   toggle + provider selector (per product, as today)
  e2e             on|off + TEST_* sandbox credentials (per product)
  quotas          concurrency (default 1) · daily bursts · usage share
  state           active | paused
```

Registration is an operator action (console/CLI): install the org Apps on the repo,
declare quotas, provision sandbox creds. **No platform code changes per product** —
if a product needs platform code, the socket-and-plug boundary was violated and the
design review should catch it.

Registration mechanics *(DR)*:

- **Defaults are materialized, not inherited.** `product register` fills every
  policy/quota column from `.env`-shaped platform defaults unless overridden —
  the row is the entire truth about a product; no NULL-means-inherit, no
  resolution logic. A changed platform default never retro-propagates: changing
  a live product is an operator action against that product, audited.
- **Preflight = operability gate + socket lint.** Hard refusals only for
  declarations the platform cannot execute: the App not installed / no repo
  access; `e2e on` with `TESTING.md` absent, unparseable, or its declared env
  unsatisfiable by the provided `TEST_*` set; a review provider declared
  without its credentials (today's startup error, relocated to registration).
  Everything else — `CLAUDE.md`, issue/PR templates, commitlint — is **socket
  lint**: checked, warned, recorded in the registration's audit row, and
  re-runnable on demand (`product check`) to catch drift. A socket-poor repo
  registers and works conservatively; the socket improves while the platform
  runs. Optional `--lint-issue` files the gaps as a `human` issue in the
  product repo — the socket becomes the product's own first backlog.
- **The audit log starts here.** `product register|pause|resume|set` are the
  platform's first operator mutations and write the first audit rows (actor,
  action, target, reason — pause requires one). The console (§8) inherits a
  working audit trail, never introduces one.

Teams own their products' gates and PRs through GitHub, exactly like the MVP's owner.
The platform team owns the service: quotas, capacity, incidents, the fleet view.

## 3. Components

| Component | Runs as | Identity | New vs MVP |
|---|---|---|---|
| **Control plane** | k8s Deployment (leader-elected) | org GitHub Apps (mints per-repo tokens) | deployed service; subscription-pool scheduler |
| **Webhook receiver** | k8s Deployment behind the ingress — **the platform's only public surface** | App webhook secrets (HMAC verify only) — no other credential | **new** (§5) |
| **Burst runners** | k8s **Jobs** in a microVM runtime (§6 DR: Kata / e2b / gVisor) — or, as a *deployment variant*, the same runner on a member's machine (§4, mechanism B) | dev App token (dev role only) · none (reviewer/tester) | hardware-virtualized isolation; same walls |
| **Console** | web GUI + the existing CLI | Fermata SSO; roles: owner / operator / viewer | **new**; live fleet state + streaming burst logs |
| **Monitor** | k8s Deployment, per-product watchers | monitor App (`issues:write` only) | **new** (pipeline already proven by the tester relay) |
| **Ledger** | Postgres (managed) | — | replaces SQLite behind the existing `Ledger` protocol |
| **Session archive** | S3-compatible object store | — | replaces the filesystem behind the existing `SessionArchive` protocol |
| **Platform memory** | Postgres + object store (§9) | written only via harvest; read only via prompt assembly | **new — core subsystem** |
| **Observability** | the adopter's stack, behind an OTLP endpoint + ledger reporting views | agents emit, never read | capabilities shipped, instruments not (§12) |

## 4. Identities & the subscription pool *(DR)*

- **GitHub: four org-level Apps, one per pipeline role** — dev, reviewer,
  tester, monitor — installed org-wide. **App names are the adopter's choice;
  the platform's contract is the env-prefix scheme** (`GITHUB_APP_ID`,
  `REVIEWER_…`, `TESTER_…`, `MONITOR_…` — the monitor prefix is reserved now,
  its App created at T4). This replaces per-product App pairs (2 Apps × N
  products → 4 Apps total) and finally gives the tester its own provenance
  (deferred in the MVP, where it rode the reviewer App). Scopes as in
  `GITHUB-INTERFACE.md`; the dev App still cannot merge; no App satisfies the
  CODEOWNERS gate.

- **Two token tiers** *(DR)*: **control-plane tokens** (polling, posting
  reviews/reports) are minted broad and cached; the private key and broad
  tokens never cross the container boundary. **Burst tokens** are minted fresh
  per spawn and **downscoped below the App's grant at mint**
  (`repositories: [the task's repo]` + a per-role `permissions` subset) — so
  §13's "a burst can't reach another product" becomes a per-token fact,
  live-tested: a dev token for product A is rejected on product B by
  construction. Reviewer and tester bursts still hold no GitHub credential at
  all; the control plane posts their findings under the matching App identity.
  The Copilot PAT stays the documented oddity (only a seated user's token may
  request Copilot review).

- **Anthropic: the team subscription pool.** Every team member has a Claude
  subscription; the platform runs on that pooled capacity. Two mechanisms, not
  mutually exclusive:

  **A — registered token pool (the backbone).** A member opts in with
  `claude setup-token` (a 1-year, **inference-only** OAuth token),
  **materializes it themselves** per the adopter's runbook, and registers
  **only a ref** (`subscription register --member x --ref env:… --cap N`) —
  **the platform never holds a token value** *(DR)*: the ledger stores a
  name, resolution happens at spawn, the token lives in the container env and
  dies with it. Custody never leaves the member's materialization, and
  deleting the secret at its source is a revocation lever the platform cannot
  override. Mechanics *(DR)*:
  - **Usage is derived, backoff is state**: per-subscription consumption is a
    rolling-window SQL aggregate over `bursts` (never a mirrored counter —
    §12's ledger-as-metrics rule); the only new pool state is
    `subscriptions.backoff_until`, set by CAS when a burst reports rate
    limiting, cleared by time. Caps are **tokens per rolling window** — one
    number per subscription; `max_budget_usd` keeps its runaway-hard-stop
    role untouched.
  - **The pick happens inside admission** (§5's fenced aggregate step):
    eligibility is a pure predicate (active ∧ unexpired ∧ not backing off ∧
    headroom > 0), pick is max-headroom-first, `subscription_id` stamped at
    `record_burst`. No eligible token → idle + `capacity_exhausted` event
    (adopters route it — capacity stays a people question). A rate-limited
    burst → backoff by CAS, and the attempt rail retries on another token
    (sessions are local transcripts; any valid token resumes them). The pick
    outputs `(subscription_id, affinity)`; affinity is `any` until mechanism
    B, whose runners are just claimers filtered to their owner's bursts.
  - **Lifecycle is events + CAS, no daemons**: `subscription_expiring` events
    through the final N days (renewal = re-materialize at the same ref + one
    CLI verb updating `expires_at`); an auth-failed burst marks `expired` by
    CAS; `revoke` is CAS + audit + event — immediate ineligibility, in-flight
    bursts finish. Every verb audited. Contribution ("your subscription ran N
    bursts / M tokens this week") is a `reporting.*` view (§12).

  **B — member-hosted runners (the extension).** Not a separate component — the
  **same burst runner, deployed on a member's own machine** (a deployment-diagram
  concern, not a container): the daemon picks up bursts assigned to *their*
  subscription and executes them locally (Docker); the OAuth token **never leaves
  their device**. Strongest personal-credential story and free compute, at the cost
  of availability variance (laptops sleep) and heterogeneous environments. The
  dispatch protocol treats a member-hosted runner exactly like a k8s runner that
  only accepts its owner's bursts; harvest ships results to the platform stores
  over a per-burst scoped channel.

  Consequences, either mechanism: **consumption is measured in tokens, not USD** —
  accounting (§8) reports usage per subscription, per task, per product;
  `max_budget_usd` keeps its proven role as the runaway hard-stop (the SDK kills on
  estimated cost regardless of billing model — falsified into shape on task 17);
  org throughput = the sum of contributed windows, so capacity planning is a
  people-question ("who opts in") rather than a procurement one.

- **Humans: Fermata SSO** on the console. Three roles, deliberately few: **owner**
  (their products: gates visibility, force-test, re-ready), **operator** (fleet:
  quotas, pause, overrides, capacity), **viewer**. Every mutating console/CLI action
  lands in an **audit log** (the ledger-surgery lesson: the override path must be
  first-class and attributable, finding #9).

- **Secrets — materialized by the adopter, resolved by ref** *(DR)*: the
  platform never speaks to a secret manager. It ships a `SecretResolver`
  protocol over opaque refs — `env:NAME`, `file:PATH`, plus one helper binary
  configured out-of-band (the git-credential-helper pattern) for anything
  else. How secrets reach env/files is the adopter's instrument (§12:
  capabilities, not instruments). Refs stored in the ledger
  (`subscriptions.secret_ref`, per-product `TEST_*`) are names, never
  commands. Injected at spawn, never in images, never in the archive (the
  MVP's unconditional credential-exclusion rule survives). The pool's runtime
  write path is decided with the pool itself: helper store verb vs
  registration through the adopter's materialization.

### Roles — ceilings in code, composition in data *(DR)*

Roles live in the ledger (`roles` table) as declarative execution contracts —
prompt preamble, tool selection, model, budget/turn defaults, mount mode,
identity binding, result sink — creatable and editable at runtime (CLI, later
console), every mutation audited. The safety model is the database's own:
grants are runtime data, the engine is code, and no row composes past the
engine's ceilings.

- **Ceilings (code):** a role's tools are a subset of a code-defined
  allowlist, and the `walls.py` deny-hook floor runs beneath whatever the row
  says — narrowing is data, widening is impossible. Identity binding accepts
  `none` or the dedicated **automation App** only; the pipeline Apps are not
  values the column accepts. Sinks come from an enumerated,
  platform-implemented set (post issue/comment · ledger artifact · emitted
  event for external systems).
- **The funnel's trust slots stay closed:** the four pipeline roles are seeded
  rows whose wall-bearing fields are frozen (CHECK + grants); no runtime role
  can occupy the assess slot or review platform-authored PRs. The
  author/reviewer separation and the pipeline's credential asymmetry are code,
  not policy.
- **Custom roles** automate everything outside the funnel — business-process
  bursts with a `schedule` or `event` trigger (trigger machinery arrives with
  T4's monitor), same container walls as every burst, delivering through
  their sink. New automation = new row, zero deploys; a new *trust position*
  in the pipeline = a platform release, deliberately.
- **§10's review lenses are this mechanism:** a lens is a custom-role row
  constrained to the reviewer's identity-less execution profile — one
  mechanism, not two.

## 5. Control plane as a service

The **reconciler model survives unchanged** — observe GitHub, derive events, advance
per-task state machines, run bursts. What changes is where and how much at once.

- **Deployment — stateless peers, no leader** *(DR)*: the control plane ships as
  an OCI image + 12-factor env and runs as N identical replicas on any
  substrate (reference k8s manifests and a compose file are examples, not
  dependencies — no orchestrator client in platform code). There is no
  election and no failover window: per-row work (observe a product, advance a
  task, harvest a burst) is **claimed through the ledger as a lease** — one
  atomic `UPDATE … WHERE id IN (SELECT … FOR UPDATE SKIP LOCKED) RETURNING`,
  lease = visible data with expiry, reclaimed by anyone when it lapses. The
  only serialized section is **admission** (rails + spawn allocation — an
  aggregate computation, later fair share × token availability): a short
  advisory lock fencing in-memory math and single-statement writes, never
  held across I/O. Schema walls (§5-of-block-1: one running burst per task,
  finalize-once) backstop any claiming bug as a constraint violation, never a
  doubled burst.

- **Ledger discipline — transactional, but not interactive** *(DR)*: no
  transaction ever spans application logic, network I/O, or think-time. Every
  ledger interaction is a single atomic statement: claims are leases (above),
  state transitions are compare-and-set (`UPDATE … WHERE status='expected'`,
  rowcount decides), mutation + outbox ride one data-modifying CTE, and work
  always runs with no transaction open. Locks live for microseconds inside
  statements; replica death leaves expired leases (debuggable data), never
  held locks.
- **Stores**: Postgres implements the existing `Ledger` protocol (tasks, bursts,
  violations — plus products, quotas, subscriptions, memory index, audit); the
  object store implements `SessionArchive` (task-keyed transcript blobs,
  checksummed). The MVP's protocols were designed for exactly this swap; callers
  don't change.
- **Webhooks as wake-up, polls as truth** — the MVP's standing decision activates
  now that a receiver is deployable. Mechanics *(DR)*:
  - **Delivery rides the org Apps.** A GitHub App carries its own webhook: every
    repo an App is installed on delivers `issues`, `pull_request`,
    `pull_request_review`, and `push` events to the App's webhook URL
    automatically. Registering product #30 configures **zero** webhooks — the App
    installation *is* the subscription.
  - **A dedicated receiver, deliberately dumb — and credential-free** *(DR)*.
    The platform's **only public surface** is a small stateless deployment
    (the third workspace package, `packages/webhook-receiver`; never imports
    control-plane) that does exactly three things: verify the HMAC signature
    against the App's webhook secret (a `SecretResolver` ref — its only
    secret), dedup delivery ids in an in-memory LRU, and forward a *wake
    event* (`repo`, `kind`) to the control plane over an **internal,
    unauthenticated endpoint**. No DB credential, no GitHub credential, no
    state: because forgery is harmless by construction (below), the wake path
    needs no trust anywhere — the public process degrades to a stateless HMAC
    filter. Receiver restart loses dedup (one duplicate poll); control plane
    down loses the wake (the poll catches up). The MVP skipped webhooks
    precisely because a public endpoint next to the App private key was a bad
    trade on a laptop; the receiver is how that objection is retired.
  - **Forgery is harmless by construction.** Because polls remain the only source
    of truth, the worst a forged/replayed webhook can cause is one extra poll of
    real GitHub state — never a state transition. The wake path needs availability,
    not trust; missed deliveries cost latency, never correctness.
- **Scheduling** *(DR)*: per-task serialization is a hard invariant (one running
  burst per task — sessions are single-writer). Across tasks: **weighted fair share
  across products** with per-product concurrency caps (default 1) under a global
  runner ceiling — now intersected with **token availability** from the
  subscription pool (a burst needs both a runner slot and an eligible token). No
  priority knobs until fair-share demonstrably fails (config-taste: add controls
  when a real scheduling failure demands them). Fair share's trivial predecessor
  ships first: the registry-driven serial loop iterates active products
  **least-recently-served** (`last_burst_at`, nulls first) — zero knobs, and
  byte-for-byte MVP behavior when one product is registered.
- **Rails, now three-level**: per-burst (hard budget cap, max_turns, wall-clock —
  plus a default **per-tool-call timeout** inside the burst; an 18-minute hung Bash
  call burned a round in the MVP, finding #6) · per-task (attempts, review rounds —
  the installment plan) · per-product (daily bursts, usage share) · per-subscription
  (the member's cap) · org ceiling. All `.env`-shaped defaults, overridable per
  product at registration.
- **Kill switches**: burst, task, product (pause), and org STOP — all through the
  operator API with audit, all converging on the same mechanics the MVP proved
  (mark killed, roll back task state, re-derive on resume). **The STOP flag
  moves from the filesystem into the ledger** *(DR)*: an org-scoped state row,
  checked at the top of every tick and before every spawn — identical
  semantics from any replica, any host; the console later writes the same row.

## 6. Burst execution

- **The runner is a protocol** *(DR)*: `Runner` joins `Ledger`/`SessionArchive`
  as a seam — `DockerRunner` (as built) and `JobsRunner` (T1, after the
  isolation bake-off). This resolves T0's "where do bursts run": the control
  plane deploys **wherever Docker is available** — a VM with the image and the
  compose file is a first-class T0 deployment; k8s arrives at T1 *with* the
  runners, when it pays for itself. **k8s Jobs** then replace local Docker:
  one Job per burst, `activeDeadlineSeconds` = the wall-clock rail, no retries
  at the k8s layer (retries are the scheduler's decision, informed by the
  result file).
- **The Job is a three-container pod** *(DR)*: **init** (platform image)
  materializes the session snapshot + clone into an `emptyDir`; **agent** (the
  dev-agent image, unchanged) runs against it — dev holds its downscoped
  GitHub token, reviewer/tester hold nothing, as ever; **harvester** (native
  sidecar) uploads result + violations (+ the dev session snapshot) after
  agent exit. Under `DockerRunner` the same three phases are
  prepare/run/harvest in-process; mechanism B's member-hosted runner reuses
  the pattern verbatim.
- **Zero standing storage credentials in the pod** *(DR)*: init and harvester
  hold **presigned URLs minted at spawn** — a GET for the one snapshot key,
  PUTs for the exact result keys, time-boxed to the wall-clock rail. A
  capability for one object, never a credential for a store; works on any
  S3-compatible backend. Clone sync for credential-less roles rides a
  `contents:read`, single-repo token confined to init — the reviewer/tester
  agent process still never sees a GitHub credential.
- **The k8s client lives in `JobsRunner` and nowhere else**: its service
  account is scoped to Jobs-in-one-namespace; the burst pod mounts **no
  service-account token at all** (`automountServiceAccountToken: false`) —
  "no k8s API access" becomes an absence-of-credential fact, not a
  NetworkPolicy promise.
- **Isolation runtime** *(DR — bake-off in wave T1)*: bursts execute arbitrary
  code an agent wrote seconds earlier; container-grade isolation is the floor, not
  the target. Candidates:
  - **Kata Containers** — microVMs as a k8s `RuntimeClass`: OCI-compatible, so the
    Jobs design, warm images, and volume choreography carry over unchanged. The
    conservative default.
  - **e2b/infra** — Firecracker-based sandbox infrastructure purpose-built for AI
    agents; attractive ergonomics (snapshot/restore could make warm boots instant),
    but its own orchestration next to k8s.
  - **gVisor** — syscall-interception; lighter than microVMs, weaker isolation
    story. The fallback if microVM overhead hurts.
  Bake-off criteria: boot latency on warm images · bind-mount support for the
  session volume choreography · egress policy enforcement · snapshot/restore ·
  ops burden on our cluster. Decision wanted before T1 exit; the Jobs abstraction
  isolates the choice. **Method** *(DR)*: a **golden-replay harness** — the
  same archived session node + task replayed as an identical burst under each
  `RuntimeClass` (the session tree's fixtures), emitting the criteria matrix;
  the decision lands as a DR with data attached, and the M4 red-team suite
  replays through the same harness. *Built (G4):* `control-plane replay`
  (`control_plane/replay.py`); CI replays a golden `check` burst under the
  default runtime and gVisor on kind and uploads the matrix.
- **Session choreography — sessions become a tree *(DR)***: the archive keeps
  **one immutable snapshot per burst** (`sessions/<task>/<burst>`) instead of one
  mutable blob per task, and `bursts.resumed_from_burst_id` records which snapshot
  a burst grew from. Session history is a tree whose nodes are burst boundaries;
  the engine stays opaque — branching is the SDK's `fork_session` from an archived
  snapshot, never transcript surgery. What the tree buys:
  - **retry ≠ branch** — an environment flake re-runs the same node unchanged; an
    approach failure branches from the last good node, with a short **branch
    summary** of the abandoned attempt injected as auto-class data and proposed
    to product memory (§9). Retries keep the lesson, not the poisoned tail
    (finding #6's hung-call transcript never haunts attempt N+1 again).
  - **exploration** — fork one explored node into N strategy branches on wide
    tasks (judge-panel; opt-in per task, costs N×; no cache reuse across bursts).
  - **golden replay** — archived nodes are immutable fixtures: replay a node
    through a prompt/model/wall change and diff outcomes; the M4 red-team suite
    replays attack scenarios from pinned nodes and measures breach rate.
  - **forensics** — the console timeline (§8) renders true lineage; every burst's
    exact input context is a named, retained object in the archive.
  Everything else carries over: the control plane materializes the chosen
  snapshot into the Job's volume before boot, harvests result + violations +
  session (+ memory proposals, §9) after; reviewer volumes are never seeded and
  never archived, tester volumes are never seeded but archived as *evidence*
  (B5 — leafhood is the dev's, enforced in the leaf query, so a tester snapshot
  is never resumable); `burst-result.json` in `finally` + session id from
  the init message — resume survives any death, including budget kills.
- **Warm images per product family**: a base agent image + product toolchain layers
  (uv/python, node, …), built by the platform's CI, pinned per product. A burst is
  `git fetch/checkout` + agent boot, as designed in the MVP.
- **Network walls — egress declared as data** *(DR)*: the platform emits a
  per-burst egress declaration as structured Job metadata — GitHub, the
  inference API, the adopter's OTLP endpoint (§12), and (tester bursts only)
  the product's declared sandbox endpoints from its `TESTING.md` manifest.
  Reference FQDN policies ship as examples; enforcement belongs to the
  adopter's CNI (capabilities, not instruments), and the M4 suite validates
  it on *their* cluster: a request to an undeclared host must fail there,
  not in our docs. *Built (G6):* the `usf/egress` annotation on
  every Job and pod (`control_plane/egress.py`), a reference policy in
  `examples/egress/`, and `dev-agent walls` — the M4 probes, replayed through
  the G4 harness on Cilium in CI; a breach fails the build.
- **The clone** becomes a per-runner cache keyed by repo, synced before every mount —
  same invariant ("whatever a burst leaves behind is erased before the next look"),
  now safe under concurrency because a task's bursts are serialized and caches are
  per-runner. *Built (H3):* the cache seeds one tree per task, so two bursts on one
  runner never share a working tree (SCHEDULER.md, "The clone").

## 7. Task lifecycle at scale

Unchanged from `SCHEDULER.md` — the state machine, the funnel
(work → PR → assess → dev rounds → clean assess → test → human gate), soft/hard
round caps, defect routing (PR-caused → `REQUEST_CHANGES`; pre-existing → relayed
`auto` issues behind the `ready` gate). The TO-BE adds only *where it runs* (Jobs)
and *how many at once* (fair share × token availability). Multi-repo means the
funnel runs concurrently across products, never concurrently within a task.

## 8. Human surfaces — the console *(GUI, live)*

GitHub stays the surface for **work** (issues, PRs, reviews, gates). The console is
the surface for **operations** — a web GUI whose defining property is **liveness**:
it shows the platform *now*, not a report about it.

- **Live fleet view**: products × task states streaming as they change (SSE/WS over
  the ledger's event feed); what's queued, working, awaiting whom; per-product
  health (rails tripped, assess failures, test skips); pool status (which
  subscriptions are contributing, window headroom per token).
- **Live burst view**: a running burst's structured log stream in real time (the
  structlog events are already shaped for this), current tool call, elapsed
  time per step, cost ticking against the cap. (This page is the productization of
  every "check artifacts / why is it slow" investigation from the MVP cycle —
  including catching the next 18-minute hung tool call *while it hangs*.)
- **Task timeline**: bursts with durations, per-step timing, cost, session lineage,
  links to PR/issue; the full history that today lives in tarballs.
- **Usage accounting**: tokens per burst → task → product → subscription → week;
  contribution view per member; the ~85 % QA-iteration cost structure becomes a
  visible, optimizable number per product.
- **Violations & containment feed**: the tripwire dataset as a first-class live
  page — the experiment's primary dataset becomes the platform's security
  telemetry.
- **Memory browser** (§9): per-product memory entries with provenance — inspect,
  edit, retire; owner-gated for their product.
- **Operator actions, audited**: stage override (force test / re-assess — finding
  #9 as porcelain), reset rails, re-`ready`, pause product, kill burst/task, org
  STOP, pool management (register/cap/revoke tokens). Every action = who/when/why
  in the audit log.
- **Notifications: Slack** per owning team — the ball-is-with-you nudges (PR
  awaiting owner review, task blocked, budget threshold, monitor filed an issue).
  Deep links into GitHub/console; never actionable from Slack (gates live in
  GitHub, full stop).
- The **CLI keeps parity** for operators; the console adds visibility, not a second
  control path with different semantics.

## 9. Platform memory *(new core subsystem)*

A long-running platform must not relearn its products from zero every burst. The
MVP already has three memory layers in embryo — the TO-BE makes them explicit:

| Tier | Scope | Lives in | MVP ancestor |
|---|---|---|---|
| **Session** | one task | session archive | proven resume — untouched |
| **Product memory** | cross-task, per product | platform memory store | living docs + the acknowledged-defects idea (#10) |
| **Platform memory** | cross-product, operational | platform memory store | the human-curated retrospective (findings #6–#11) |

- **An entry is a datum, not an instruction**: `{claim, evidence, scope, provenance
  (task/burst/date), status}`. Memory is auto-class content under the provenance
  gate — injected as *data the agent may verify*, never as contract. The contract
  channel for durable product knowledge remains the repo's living docs
  (human-reviewable, versioned); memory holds what doesn't belong in a contract:
  operational patterns, failure history, environment quirks ("YouTube bot-walls
  this sandbox's egress IP — use a direct audio URL for the voice path and report
  the wall"), review heuristics ("watch-URL-with-list-param was a real defect
  here").
- **Write path — through the harvest, like everything else.** The burst result
  contract gains a `memories` field: the agent *proposes* entries; the control
  plane stores them with provenance. Nothing writes to memory from inside the box,
  and no proposal is auto-trusted into high-weight recall.
- **Read path — through prompt assembly.** At spawn, the control plane injects a
  size-capped, scope-filtered selection (product memory for that product; role
  memory for that role) into the burst prompt, explicitly framed as recalled data.
  Dev bursts get product gotchas; **reviewer lenses get the product's defect
  history** (memory is what turns finding #7's parallel lenses from generic into
  product-tuned); the tester gets environment knowledge that today has to be
  re-discovered per run.
- **Curation — memory rots.** Entries age; recall-verify applies (an entry naming
  a file/flag is checked against the repo head before high-weight injection where
  cheap); a periodic **gardener pass** consolidates duplicates and retires
  contradicted entries; the console's memory browser gives humans direct
  edit/retire. Provenance makes every entry traceable to the burst that proposed
  it.
- **Containment**: the memory store is platform-owned (agents hold no store
  creds); scope-filtering means product A's bursts never see product B's memory;
  injected memory is auto-class data, so the laundering defense covers a poisoned
  memory entry exactly as it covers a poisoned issue body.

## 10. Review & QA at scale *(roadmap wave 3)*

- **Parallel lens-scoped review** (finding #7): on each assess trigger, fan out
  reviewer bursts by lens — correctness, security, tests/evidence,
  spec-compliance — dedup findings, post as **one** review by the reviewer App.
  Serial rounds proved to be sampling (~1.6 of ~7 present defects per pass); lenses
  attack coverage, and the round machinery (cursor, caps, threads) is unchanged —
  it just receives better rounds. A **verify round** after fixes stays irreducible
  (fix-induced defects were real in the MVP). Lenses read product memory (§9)
  and are runtime role rows (§4 — ceilings in code, composition in data).
- **Tester tools registry** (TESTER.md slice 3): `playwright` and friends baked in
  the image, enabled per product manifest. The plug declares needs; the platform
  ships capabilities.
- **Acknowledged-defects channel** (finding #10) folds into product memory:
  human-labeled known-defect issues become memory entries, so known pre-existing
  failures stop re-headlining every report — sourced only from human-labeled
  issues, never PR text (no laundering).
- **Feasibility discipline** (the yt-dlp lesson) is already contractual — spec-time
  feasibility section, dev live round-trip rule, tester defect-grade headline —
  and ships with each product's contracts, not the platform.

## 11. The monitor *(roadmap wave 4)*

As designed in `ARCHITECTURE.md` §7, now with a proven receiving pipeline (the
tester's symptom relay exercised `auto` end-to-end): per-product watchers over the
org's existing telemetry (logs/metrics/Alertmanager), filing structured `auto`
issues behind the `ready` gate, rate-limited, dedup-capped. Operational-only signals
stay in the alerting plane. First detector class: **external-integration breakage**
(the arms-race feed — yt-dlp-style dependencies break on the vendor's schedule, and
finding #11 showed that's recurring maintenance demand, i.e. the platform's business
case). Monitor findings that repeat become product memory (§9).

## 12. Observability

**The platform ships capabilities, never instruments** *(DR)*: no collector,
log store, dashboard, or alerting component is part of the shipment — adopters
plug the capabilities into whatever monitoring stack they already run. Four
capabilities:

- **Traces** — OTLP push to a configurable endpoint, trace-per-burst with
  `task.id`/`burst.id`/`role` attributes and `TRACEPARENT` into the Job so the
  agent's native spans nest. Both gaps this section used to name are closed:
  the burst span links to its predecessor (B1 — a link needs `trace_id` *and*
  the span id, so the ledger records the pair), and the agent's own OTel is
  configured per burst with `OTEL_RESOURCE_ATTRIBUTES` stamping the correlation
  ids (B2). Two endpoints, because two network namespaces:
  `OTEL_EXPORTER_OTLP_ENDPOINT` is the control plane's own,
  `DEV_AGENT_OTLP_ENDPOINT` is where a burst pushes. The burst egress wall
  widens by exactly one adopter-configured endpoint (§13).
- **Logs** — structured logfmt on stdout + the per-burst teed files, with
  `trace_id`/`burst_id`/`task_id` bound into every line. That is the whole
  contract: 12-factor out, correlation in-band, consumable by any log
  pipeline; no shipper is shipped or assumed. (The console's live streams (§8)
  read the same structlog events over the ledger's event feed, not through a
  log backend.)
- **Metrics** — the ledger *is* the metrics store: the §9-of-ARCHITECTURE
  families (reliability / containment / capability) ship as documented
  read-only SQL **reporting views** over `tasks`/`bursts`/`violations`. Any
  dashboard tool with a Postgres driver renders them; reference dashboards are
  examples, not components, and are exactly that — `examples/dashboards/` (B4),
  a portable `.sql` file plus one importable dashboard that embeds it, read by a
  role holding nothing but SELECT on the views. No emitted-metrics pipeline —
  that would mirror ledger facts and drift (labels reflect, the ledger locks). Emitted OTel
  metrics stay reserved for process-local facts (tick latency, queue depth)
  when a real operational question demands them.
- **The alert signal** — the violations feed itself (append-only table + its
  `events` outbox row) plus the documented condition: any new violations row
  is a page. Adopters wire it into their own alerting; the platform emits,
  never routes. Written down in three forms at B4 (`OBSERVABILITY.md`, "The alert
  condition") because they are not equivalent: the pull query a least-privileged
  dashboard role can run pages at group grain, and only the outbox — or the rows
  themselves — is exact per violation.

> **Testing rig** — built at B4: `docker/compose.telemetry.yaml`, dev-only, never
> shipped, swappable in that file plus `docker/telemetry/collector.yaml`. OTel
> collector in front (logs need a second destination and Phoenix takes traces only),
> VictoriaLogs for logs, Phoenix for traces, Grafana for the views; `lnav` over the
> logfmt files as the no-server default for laptop work. Two measured caveats. The
> CLI reports usage under its own attribute names, so a viewer's accounting reads zero
> until something renames them — the rig's collector does, in the OTel GenAI
> convention, which is the shape of every viewer fix that belongs in a rig rather than
> in the platform: **translate names, never invent numbers.** Tokens then reconcile
> with the burst row exactly; Phoenix's *cost* does not, because it prices tokens off
> its own table (0.64× measured), and the ledger stays the authority for money. And
> Phoenix has no span-links field, so B1's lineage link is dropped and
> `burst.resumed_from` is what a viewer reads.

## 13. Containment at org scale

Everything in `ARCHITECTURE.md` §8 survives. New surface, new walls:

| Boundary | Mechanism | Type |
|---|---|---|
| Burst escapes its box | microVM isolation (§6 DR) — hardware virtualization, not just namespaces | wall |
| The public webhook endpoint | receiver verifies HMAC + dedups, holds no creds, emits wake events only — a forged webhook can at worst trigger one poll of real state | wall |
| Burst can't touch the cluster | no SA token mounted (`automountServiceAccountToken: false`); k8s client only in `JobsRunner`, scoped to Jobs-in-one-namespace; declared-egress allowlist | wall |
| Burst can't reach the stores | presigned per-object URLs minted at spawn, time-boxed to the rail; no store credential in any burst container | wall |
| Burst can't reach another product | per-repo-scoped App token minted per burst; sandbox creds injected per product; memory scope-filtered | wall |
| A member's subscription is protected | per-token usage cap + one-click revoke + full attribution; token B never leaves its owner's machine (mechanism B) | wall |
| Memory poisoning | write only via harvest with provenance; injected as auto-class data; gardener + human browser | tripwire (data) |
| Operator actions are attributable | audit log on every mutating API call | tripwire (data) |
| Console can't bypass gates | console holds no merge identity; gates only in GitHub | wall |
| Secrets never persist | secret-manager injection at spawn; archive exclusion unconditional | wall |
| Platform CI red-team | the M4 suite runs as a scheduled job against a canary product | tripwire (data) |

## 14. Roadmap — AS-IS → TO-BE

Waves, not phases (rollout phases 0–2 in `ARCHITECTURE.md` stay the *exposure*
story; waves are the *build* story). Each wave ships alone and is useful alone.
The PR-sized decomposition of these waves — steps, gates, dependency spine —
lives in **`PHASE-1.md`**.

- **T0 — the service** *(ops hardening)*: Postgres + object store behind the
  existing protocols; control plane shipped as an OCI image, run as stateless
  peer replicas on any Docker-capable host (leases + CAS through the ledger;
  k8s waits for T1's runners); the webhook receiver
  (App-delivered events, HMAC-verified, credential-free, wake-only); org Apps (4 pipeline roles,
  downscoped per-burst tokens); the role registry seeded; secrets resolved by
  ref, materialized by the adopter; **subscription pool v1** (ref-only
  registry — the platform never holds token values; per-token caps,
  headroom-ordered pick in admission, attribution); **the session
  tree's storage layer** (snapshot-per-burst archive + `resumed_from_burst_id`
  lineage in the ledger); **the product registry** (materialized defaults,
  operability gate + socket lint, audit-first operator verbs, registry-driven
  least-recently-served scheduling).
  Exit: the laptop is retired; two products registered; MVP behavior byte-for-byte
  on pooled tokens.
- **T1 — concurrency + isolation**: k8s Jobs runners (three-container pods;
  presigned materialize/harvest); **isolation runtime bake-off decided**
  (Kata / e2b / gVisor, via the golden-replay harness) and adopted; per-task
  serialization + weighted
  fair share × token availability + quotas; per-tool-call timeout; clone caches;
  **fork rails** (retry-vs-branch from archived nodes; branch summaries at
  harvest); **memory store + write path** (harvest-side, entries accumulate with
  provenance).
  Exit: 5+ products active, ≥5 concurrent bursts, zero cross-task interference,
  bursts running under the chosen runtime.
- **T2 — human surfaces**: the live console (fleet, live burst logs, task
  timelines, usage accounting, violations, memory browser, pool management),
  operator actions with audit, Slack notifications, CLI parity; **memory read
  path** for tester + reviewer prompts. Exit: an operator who didn't build the
  platform runs it for a week without SQL; a hung tool call is spotted live.
- **T3 — review & QA depth**: parallel lens review + verify round (memory-tuned);
  tester tools registry; acknowledged-defects → memory; WIP-branch push rule
  (finding #8); member-hosted runners (mechanism B) if the pool wants the
  capacity. Exit: measured defect yield per round beats the MVP's ~1.6 on the same
  benchmark class.
- **T4 — the monitor**: integration-breakage detectors on 2–3 products, `auto`
  issues flowing behind the gate, repeat findings feeding memory. Exit: first
  monitor-originated fix merged by a human gate, no human having noticed the
  breakage first.

## 15. Open questions (for team review)

- **Subscription pooling posture**: resolved (§4 mechanism A) — registration
  is **ref-only**; the platform never holds a token value, custody stays with
  the member's own materialization, and source-side deletion is a revocation
  lever the platform can't override. Members wanting device-local custody
  graduate to mechanism B at T3.
- **Token lifecycle ops**: resolved (§4 mechanism A) — expiry warnings and
  renewals are events + one CLI verb; mid-task expiry falls to the attempt
  rail on another token; the revocation drill is: delete the secret at its
  source, then `subscription revoke` (CAS + audit + event).
- **Isolation bake-off owner**: who runs the Kata vs e2b vs gVisor evaluation, and
  on which cluster.
- **Object store**: SeaweedFS self-hosted vs the org's existing S3-compatible
  storage — pure ops preference, the protocol doesn't care.
- **Observability backend**: resolved (§12) — no backend is shipped. The
  platform emits OTLP + logfmt + SQL reporting views; backends are the
  adopter's instruments. VictoriaLogs/Phoenix are testing-rig choices only.
- **Transcript + memory retention**: transcripts contain code and tool output;
  retention and access policy is a security/compliance call (proposal: transcripts
  hot 90 days then delete; memory entries and the violations ledger retained —
  they're the platform's long-term value).
- **Console stack**: whatever the org's internal-tools default is; the console is
  deliberately thin (read models + event stream over the ledger, one operator API).
- **Which repos first**: two low-stakes-but-real products for T0; candidates are a
  team decision.
