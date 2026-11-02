# Observability — what the platform emits, and where to look at it

`TO-BE.md` §12: **the platform ships capabilities, never instruments.** Four of them
— traces pushed over OTLP, structured logs on stdout with correlation in-band, the
§9 metric families as SQL views, and a violations feed with a documented alert
condition. No collector, log store, dashboard or alert router is part of the
shipment. This file is the other half of that bargain: if the platform ships no
instrument, it owes you the wiring.

The rig below is one wiring, dev-only and thrown away daily. It exists so a claim
about the whole chain can be *checked* — the same reason `tests/test_schema_walls.py`
provokes walls rather than describing them.

## The two endpoints

Two, because two processes in two network namespaces. Both are presence toggles:
unset, spans still exist (they mint the correlation ids, which cost nothing), and the
container is told nothing about telemetry at all.

| Variable | Set by | Who pushes | Rig value |
|---|---|---|---|
| `OTEL_EXPORTER_OTLP_ENDPOINT` | the control plane's own env | the control plane | `http://localhost:4318` |
| `DEV_AGENT_OTLP_ENDPOINT` | the control plane, injected per burst | each burst's worker and its `claude` CLI | `http://host.docker.internal:4318` |

The second string is the one that gets it wrong. `localhost` inside the box is the
box, and **the CLI drops export failures silently** — a burst pointed at nothing
looks exactly like a burst with telemetry off. `CLAUDE_CODE_OTEL_DIAG_STDERR=1` in
the container env makes it say why instead.

What a burst emits, and why each choice, is `control_plane.telemetry.agent_telemetry_env`
(B2): traces and log events yes, metrics no, tool **arguments** yes, tool and API
**bodies** no. `CONTROL_PLANE_TRACE_CONSOLE=1` prints the control plane's own spans
instead of pushing them; it is never passed into a container, where stdout is the
SDK's message channel.

## One join key

`trace_id` is the join, and it reaches every signal without a call site passing it:

- **The trace** is one turn (`ARCHITECTURE.md` §9): what the platform did for one
  task in one go, holding at most one burst. The control plane opens `turn`, a span
  per step and the `burst` span, and injects its `burst.run` step's `traceparent`
  into the box; the worker opens `agent.burst` under it, and the CLI nests its own
  tree under the worker's — three processes, one trace, one root. The ledger records
  the *burst* span's pair `(trace_id, span_id)`, because the link a *successor*
  burst makes names a span (B1).
- **Control-plane log lines** carry `trace_id` / `task_id` for the turn's lifetime
  (bound in `turn_span`) and `burst_id` as well for the burst's (`burst_span`), so
  every line written during a turn has them.
- **Agent log events** carry `trace_id` *and* `span_id` — the span they happened in:
  `claude_code.interaction` for most, the tool's span for a `tool_decision`, and
  `agent.burst` for the few the CLI writes before its interaction opens (measured on
  2.1.314; on 2.1.235 every event carried the span the CLI was handed). So a log
  record points at a span, not just a trace.
- **When there is no span there is still `burst.id`.** `OTEL_RESOURCE_ATTRIBUTES`
  stamps `task.id` / `burst.id` / `burst.role` / `burst.kind` on every signal the CLI
  and the worker emit. On 2.1.235 that is what made the quick haiku call attributable
  at all — a standalone `llm_request` (`llm_request.context = standalone`), outside
  the interaction tree, in a trace of its own; on 2.1.314 the same call was measured
  inside the interaction (`llm_request.context = interaction`).
- **The ledger** holds the same ids as columns, so "the trace for the burst that
  opened PR 41" is a query and not a search.

## The rig

Dev-only, never shipped, swappable by an edit to two files
(`docker/compose.telemetry.yaml` and `docker/telemetry/collector.yaml`). Nothing
above those files knows which viewer is running.

```bash
docker compose -f docker/compose.yaml up -d --wait                # 1. stores
uv run control-plane migrate                                      # 2. schema + views
docker compose -f docker/compose.yaml exec -T postgres \
  psql -U platform_admin -d platform \
  < docker/telemetry/dashboard-role.sql                           # 3. the reader
docker compose -f docker/compose.telemetry.yaml up -d --wait      # 4. the rig

export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
export DEV_AGENT_OTLP_ENDPOINT=http://host.docker.internal:4318
uv run control-plane burst --issue 1 --repo ../discord-bot --prompt "…" --budget 0.5
```

Step 3 is the one that looks out of place and is not: migration `0010` grants the
views to `platform_app` and stops, because how a *dashboard* authenticates is the
operator's business, not the schema's. It is also the step that makes the read
boundary real — see below.

Three UIs, one per capability, each the native one for it:

| Where | What it holds | The question it answers |
|---|---|---|
| `http://localhost:6006` — Phoenix | the burst trace, both processes' spans | what did this burst *do*, in what order, how long |
| `http://localhost:9428/select/vmui` — VictoriaLogs | the agent's log events | what did the CLI say — prompts, tool decisions, API requests |
| `http://localhost:3000` — Grafana | the reporting views | how is the platform doing across tasks |

A LogsQL query for one burst, which is the whole correlation story in one line:

```
trace_id:"7caf15929cf77e014997da6bf8395979" | sort by (_time)
```

### What a burst looks like in Phoenix

Everything lands in the `default` project — nothing sets a Phoenix project name — and
each turn is one trace, rooted at the control plane's own `turn` span. A hand-run
burst on CLI 2.1.314 (`control-plane burst`, one Bash call and one read the wall
denied), as Phoenix holds it:

```
turn                                 9.9s   ← the control plane: one task, one go
  github.request                   512ms   listing installations       ← the burst's GitHub
  github.request                   593ms   minting installation token  ← token, downscoped (D2)
  burst                              8.7s   ← the burst: the span the ledger records
    burst.materialize                0ms   ← the volume (a first burst: nothing to restore)
    burst.run                        8.7s   ← the container's lifetime; the box is handed its context
      agent.burst                    6.9s   ← the worker in the box: how the run ended, wall.denied
        claude_code.interaction      4.6s   ← the CLI, under the worker
          claude_code.llm_request  855ms   LLM   claude-haiku-5-5
          claude_code.llm_request    3.1s   LLM   claude-sonnet-5
          claude_code.tool          51ms   TOOL  tool_name=Bash, full_command=…, tool_use_id
            claude_code.tool.blocked_on_user     8ms
            claude_code.tool.execution          43ms
          claude_code.tool           1ms   TOOL  tool_name=Read — the call the wall denied
            claude_code.tool.blocked_on_user     1ms
          claude_code.llm_request    1.3s   LLM
    burst.collect                   21ms   ← what the burst left, out of the volume
    burst.fold                       1ms   ← into ledger rows: burst.violations=1
```

A scheduled turn holds more of the platform: `clone.sync` (`clone.fresh` says
whether it was the task's cold first sync, H3) and the GitHub calls that build the
prompt before the burst and act on its result after it — the PR lookup, the review
requests. A tick that only polls GitHub is not traced, and a GitHub call is a span
only inside a turn: a trace a minute of nothing would bury the turns.

Five things worth knowing when you look:

- **Every gap has a name now.** Before `burst`, the turn's own steps (1.2s here,
  the token mint). Inside `burst.run`: 1.3s before `agent.burst` is the container
  starting, 1.3s before `claude_code.interaction` is the SDK starting the CLI, 0.9s
  after it is the SDK closing the CLI, and 0.5s after the worker is the container
  going away. The CLI tree alone used to leave all of that as one unexplained gap.
- **A wall shows twice and joins once.** The denial is a `wall.denied` event on
  `agent.burst` — tool, reason, `tool.use_id` — and the CLI's `claude_code.tool`
  span for the same call carries the same id as `tool_use_id`. The violations
  ledger stays the record (`burst.fold` counts what it folded); the event is how a
  reader finds the call in the tree.
- **Tool arguments are there** (`full_command`, `file_path`), because tool visibility
  *is* the experiment's dataset (§9). Tool and API *bodies* are not.
- **`tool_use_id` is on both tool spans**, which is the handle B6 (#68) resolves a
  test report's citation against — visible here before anything reads it.
- **Span links are not shown, at all.** Phoenix's span model has no links field, so
  B1's link to the predecessor burst is exported and then dropped. Lineage is
  readable anyway: `burst.resumed_from` is an attribute on the `burst` span, which
  is the reason B1 recorded it as one as well as as a link. The authority is the
  ledger's `resumed_from_burst_id`.

On CLI 2.1.235 one trace looked wrong and was not: a lone `claude_code.llm_request`
with no parent and `llm_request.context = standalone` — the CLI's quick side-call,
outside the interaction tree and in a trace of its own (B2), attributable only by
its `burst.id`. On 2.1.314 that call was measured inside the interaction; if it
reappears on its own, that is what it is.

### What a burst looks like in VictoriaLogs

The content lives here, not in the spans. Eight event types, measured on a 3-tool
burst — a work burst on a real issue is the same shapes, tens of times over:

| Event | Per | Carries |
|---|---|---|
| `user_prompt` | burst | the **full prompt text** — for a scheduled burst, what the platform composed and actually said |
| `tool_decision` | tool call | `decision`, `source`, `tool_name`, **`tool_parameters`** (the arguments), `tool_use_id` |
| `tool_result` | tool call | `success`, `duration_ms`, `tool_input`, and *sizes* rather than bodies (`tool_result_size_bytes`) |
| `hook_execution_start` / `_complete` | tool call | `hook_event=PreToolUse`, `hook_name`, `num_hooks`, `num_blocking`, `total_duration_ms` — the **wall's own execution** |
| `api_request` | model call | **`cost_usd`, `input_tokens`, `output_tokens`, `cache_read_tokens`**, `duration_ms`, `model`, `effort`, `speed` |
| `assistant_response` | turn | the full response text |
| `plugin_loaded` | burst | the toolkit plugin the image installs |

Three consequences worth carrying around:

- **The per-request cost and token breakdown is here**, in `api_request` — so what
  Phoenix declines to count is an accounting gap in one viewer, not missing data. The
  ledger's burst row remains the authority for the total.
- **`num_blocking` on the hook events is the wall reporting itself**, once per tool
  call: `0` means it let the call through. The deny path should read `1` with
  `decision=deny`, which is stated rather than measured — the burst that was supposed
  to trip a wall declined to call the tool at all.
- **Tool arguments include the `git` and `gh` commands**, so a PR's title and body
  text arrive here as tool parameters. That is the intended dataset (§9 — tool
  visibility *is* the experiment) and it is also the concrete answer to what a burst
  puts in front of an adopter's collector.

**A collector in front of both, on purpose.** `ARCHITECTURE.md` §9 originally paired
Phoenix's own OTLP listener directly with the emitters. The rig puts a collector
there instead for three reasons that outlived the shortcut: logs need a second
destination and Phoenix takes traces only; both processes should learn exactly one
address; and the swap TO-BE promises then happens in a config file rather than in an
environment variable the platform sets.

**No metrics pipeline, deliberately.** The ledger is the metrics store, so an
exported metric would be a second copy of a ledger fact. The collector has nowhere
to put one, which means it refuses the export rather than quietly keeping it — if
this rig ever starts rejecting metrics you did not know were being sent, that is the
wall working.

**No volumes.** `down` leaves nothing and the next `up` starts empty. A rig that
accumulates history is a store, and the store this platform has is the ledger.

### When nothing arrives

In order, cheapest first:

1. `docker compose -f docker/compose.telemetry.yaml logs otel-collector` — the
   `debug` exporter prints per-batch counts (`resource spans`, `log records`). If
   they are there, the emitters are fine and the problem is downstream.
2. The same log, for export failures. A destination that is down shows up here as a
   retry loop, and it is the *only* place it shows up: the CLI is silent by design
   and the control plane's exporter is not much louder.
3. `docker compose -f docker/compose.telemetry.yaml ps -a` — with `-a`, because a
   dead container disappears from the default listing and its DNS name goes with it.
   A destination that crashed reads upstream as `lookup victorialogs … no such
   host`, which points at the network and means the process.
4. Only then the endpoint variables, and the `host.docker.internal` trap above.

Three things measured on the rig that are worth knowing before they cost you an hour:

- **A config the collector never read looks like a working rig.** Each distribution
  defaults to its own path — `/etc/otelcol/config.yaml` for core,
  `/etc/otelcol-contrib/…` for contrib — and mounting to the wrong one is not an
  error: the collector boots its **built-in default**, accepts every push, and drops
  it. Everything reports healthy and nothing arrives anywhere. The tell is in the
  `debug` output: internal counters like `otelcol_receiver_failed_metric_points`
  mean you are watching the default config, because ours has no metrics pipeline.
  `docker/compose.telemetry.yaml` passes `--config` explicitly so this cannot recur.

- **Do not make a correlation id a VictoriaLogs stream field.** The CLI sends
  `task.id` / `burst.id` / `burst.role` / `burst.kind` on the resource *and* on every
  log record, so each name matches twice, and VictoriaLogs answers a duplicate
  stream tag with a FATAL panic that takes down the process — after which the
  symptom is the DNS error above, two containers from the cause. The rig streams on
  `service.name` alone; every other attribute is still an ordinary searchable field,
  which is all correlation needs (`docker/telemetry/collector.yaml`).
- **Tokens are exact, cost is an estimate.** The CLI reports usage under its own
  names (`input_tokens`, `cache_read_tokens`), so out of the box every viewer's
  accounting reads zero next to attributes full of numbers. The rig's collector
  renames them to the OTel GenAI convention, and Phoenix's own translation takes it
  from there — token counts then reconcile with the burst row exactly. Its **cost
  does not**: Phoenix multiplies tokens by its own price table rather than reading
  the CLI's figure, which measured **0.64×** the burst row on a warm session. The
  ledger stays the authority for money (§9); Phoenix's number is for *shape* — which
  request was the expensive one. Content panes are empty by our own choice: bodies
  off, the transcript is the deep record.

## The alert condition

`TO-BE.md` §12: **any new `violations` row is a page.** The platform emits and never
routes, so the condition is written down here in the three forms an adopter's tooling
can consume — with what each costs, because they are not equivalent.

A wall hit is not a crash. It is an agent trying something the walls stopped, which
is the experiment's primary dataset (§9) — and a *new* `reason` means the walls met
something they had not met before.

**1. Pull, through the read boundary.** What the reference dashboard uses, and the
only form a least-privilege dashboard role can run:

```sql
SELECT repository, tool, reason, hits, last_seen
  FROM reporting.containment
 WHERE last_seen > now() - interval '5 minutes';
```

Any row means page. The window must be longer than your poll interval or a hit lands
between two polls and is never seen. Fidelity is group grain — `(repository, tool,
reason)` — so a second identical hit inside the window keeps one alert firing rather
than raising a new one. For paging that is usually right: somebody is already
looking.

**2. The outbox, by cursor.** Exact, one notification per row, and the form that
matches the sentence in TO-BE:

```sql
SELECT id, kind, payload FROM events
 WHERE kind = 'violation_recorded' AND id > $cursor ORDER BY id;
```

The event and the `violations` row are written in one statement — both or neither
(`STORES.md`, "The events outbox") — so a consumer that keeps its own cursor cannot
miss a violation. `payload` carries `violation_id`, `task_id`, `burst_id`, `tool`,
`reason`: enough to route on, and the row it names is there for the rest. `events` is
a base table, so this needs the runtime's role or one like it, not the dashboard's.

**3. The rows themselves**, `SELECT … FROM violations WHERE id > $cursor` — the same
privilege cost as form 2 and no outbox consumer to write, at the price of the
`detail` column being in front of whoever gets paged.

One caveat on both cursors: an id watermark is safe while one process writes, which
is the case today. When E2 (#33) puts two replicas behind one ledger, a row can
commit *after* the cursor has passed its id, and the fix is to overlap — re-read a
few seconds behind the watermark, or page on the window of form 1, which cannot skip.

Alerting on anything else the platform emits is the adopter's to define; this is the
one condition the platform states, because it is the one where the platform knows
what the number means.

## Reference dashboards

`examples/dashboards/` — examples, not components:

- `reporting.sql` — every panel as a statement any Postgres driver can run. No tool
  macros and nothing outside `reporting.*`, so the file is portable and stays inside
  the boundary.
- `grafana-reporting.json` — the same statements as a dashboard, provisioned into the
  rig's Grafana straight from `examples/` (so the rig renders the committed file, not
  a copy of it).

`tests/test_dashboards.py` runs every one of those statements against the views **as
a role with nothing but the two grants**, and holds the JSON's copy of the SQL to the
`.sql` file's. A panel that reaches for a base table fails there rather than in
front of somebody.

**The reader is a role, and it is the operator's.** Views execute with their owner's
privileges, so USAGE on `reporting` plus SELECT on its views buys every aggregate the
platform publishes and no row of `tasks`, `bursts` or `violations`:

```sql
GRANT USAGE ON SCHEMA reporting TO <dashboard_role>;
GRANT SELECT ON ALL TABLES IN SCHEMA reporting TO <dashboard_role>;
```

`docker/telemetry/dashboard-role.sql` is those two statements plus a rig password and
a default-privileges line, and the rig's Grafana connects as that role — so the
reference dashboard cannot quietly come to depend on a base table. Point it at
`platform_app` instead and the boundary is gone, silently, with everything still
working. That is why it is written down.

## No server

The laptop default. Control-plane logs are logfmt on stdout and teed per burst to
`data/logs/{burst_id}.log`, correlation ids in-band, so the whole capability works
with no rig at all:

```bash
lnav data/logs/*.log                                   # one pane, timestamps merged
grep 757ffedcafce55e098216f977d14f51c data/logs/*.log   # one burst, by trace id
```

That is the entire logs contract — 12-factor out, correlation in-band, consumable by
any pipeline. No shipper is shipped or assumed; the rig ingests what the platform
*pushes*, which is the agent's log events, and the control plane's own lines stay on
stdout where the operator's own stack collects them.
