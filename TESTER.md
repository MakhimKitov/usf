# Tester — E2E test bursts (phase 1+)

QA, not review: the tester boots the product in a disposable sandbox, invents scenarios
from the spec, executes them, and reports. The reviewer judges the diff by reading;
the tester judges the running product by using it. Advisory to the human gate.

## The principle: socket and plug

"How to test this product" is irreducibly product knowledge — that a Discord bot needs
a throwaway app on an empty guild and that bots cannot invoke another bot's slash
commands, or that a website just needs a browser pointed at `npm run dev`. None of that
may enter the platform. This is the third instance of the platform's one boundary trick:

| Knowledge | Lives in | Platform sees |
|---|---|---|
| how the dev behaves | product `CLAUDE.md` (system prompt) | an opaque contract |
| which reviewers exist | platform `.env` review policy | its own declaration |
| how the product is tested | product `TESTING.md` | a declared plug for its socket |

The platform owns the **envelope** (when a test burst runs, its walls, credential
injection, the report contract, posting, rails) and stays product-agnostic forever.
The product owns the **plug**: a testing contract in its own repo.

## The plug: `TESTING.md` in the product repo

One file, two audiences. YAML frontmatter for the control plane — parsed *before* the
burst, because injection happens at spawn — and prose for the tester agent, baked into
its prompt the way `CLAUDE.md` is baked for the dev:

```markdown
---
env: [DISCORD_TOKEN, GUILD_ID]   # names only — values live platform-side
tools: []                         # from the platform's registry; [] = base image
egress: [discord.com, gateway.discord.gg]  # the sandbox's hosts: the burst's egress declaration (G6)
---
# E2E testing contract
Boot: `uv run python -m bot`; online when the log says "commands synced …".
Drive: bots cannot invoke another bot's slash commands — invoke handlers
in-process; verify registration via Discord REST. Scenarios: from the PRD,
focused on the PR's diff.
```

`egress` is optional and names hosts, never patterns or addresses: the tester burst's
egress declaration (`control_plane/egress.py`, the `usf/egress` annotation on
its Job) carries them beside the platform's own, and an adopter's CNI policy is built
from that declaration. A dev or reviewer burst never declares them.

A product with no `TESTING.md` is not testable — that is a product property, not an
error: the scheduler skips the test stage and logs it. A frontmatter that doesn't parse
is a failed test burst (soft, like everything else here).

## Credentials: two-sided declaration, intersected

The manifest is **untrusted** — it lives in a repo the dev agent writes to. So neither
side alone decides what enters the container:

- The operator declares each sandbox credential as a **secret ref** on the product's
  registry row: `product set owner/name --test-env DISCORD_TOKEN=env:TEST_DISCORD_TOKEN
  --test-env GUILD_ID=file:/run/secrets/guild`. A ref is a name — `env:VAR`,
  `file:PATH` or `helper:NAME` (the `SECRET_HELPER` binary, D4) — never a value; the
  ledger stores refs only. An unregistered, env-governed run has no row: there the
  `TEST_` prefix is the declaration, `X ← env:TEST_X`, the MVP's allowlist boundary
  spelled as a default ref.
- The manifest names what it needs: `DISCORD_TOKEN`, `GUILD_ID`.
- The control plane injects exactly the **intersection**: the manifest asks for `X` ∧
  the declaration has a ref for `X` → resolved at spawn, injected as `X`. A hostile
  manifest requesting `CLAUDE_CODE_OAUTH_TOKEN` gets nothing — no row declares it, and
  `TEST_CLAUDE_CODE_OAUTH_TOKEN` does not exist.

Declared by the manifest but undeclared or unresolvable platform-side → the test stage
is skipped with an issue comment naming the gap and its ref (the operator's move, not
a blocked task); registration preflight (C2) already probes every declared ref, so the
skip is the drift case — a manifest that grew a name after registration. Sandbox
values enter **tester bursts only** — never dev, never reviewer — and exist only
between resolve and the container env. Sandbox credentials must be disposable by
construction: the blast radius of a rogue test run is one empty Discord guild, by the
operator's own provisioning choice.

## Tools: a curated registry (slice 2)

Same shape as review providers: the platform image ships a curated set —
`playwright` = MCP server + browser baked into the image — and the manifest picks from
`RECOGNIZED` names only; unknown names refuse. `tools: []` (the base image: python,
node, shell) covers the discord-bot case and ships first.

## The burst envelope

The reviewer's envelope, reused — with the execution posture inverted:

- Kind `test`, role `tester` (both already reserved in `dev_agent.env`), **fresh
  session** — no shared context with the dev or reviewer.
- **No GitHub credential.** The report returns through the harvest as the final
  message: `{"verdict": str, "scenarios": [{"name", "result": "pass|fail",
  "tool_use_id", "evidence"}], "defects": [{"summary", "repro", "preexisting":
  bool}], "issues": [{"title", "body"}]}`. The control plane posts it. The
  `issues` are the complete symptom reports the tester **authors** for
  pre-existing defects — the platform relays them verbatim: authorship stays
  with the agent, the credential stays outside the box.
- **Evidence is a citation, not prose** (B6). Each scenario's `tool_use_id` names
  one tool call from the burst's own run; on harvest the control plane resolves it
  against the archived transcript (B5) and requires the id to exist with a recorded,
  non-error result. A citation that doesn't resolve fails the report exactly like
  JSON that doesn't parse — the same retry rail, then "machine test could not run",
  never a blocked task. The posted table carries a platform-filled *cited call*
  column (the call and what it ran) beside the tester's own words, so a reader sees
  whether the prose matches the record. No grading of prose, no second model — a
  citation resolves or it doesn't. The tester lost exactly one ability: asserting
  something it did not do. Claims that are already someone else's artifact stay
  theirs — the unit suite's status is CI's check run, never a tester scenario.
- Clone mounted at the PR head, read-only; the tester copies to scratch and boots
  there. (A product too heavy for copy-boot gets a platform-prepared throwaway copy —
  decide when such a product exists.)
- Unlike the reviewer, the tester **must execute** — that is its whole job. The
  read-don't-execute rule does not apply; the sandbox credentials and the empty guild
  are the containment, not abstinence.
- Session volume not seeded — but **archived**, under the burst's own key like a
  dev's (B5): the transcript is the strongest evidence the platform holds — the
  model chooses what its report *says*, not what the CLI recorded it *did*. It is
  evidence, never memory: `latest_archived_burst` filters on `role = 'dev'`, so a
  tester's snapshot cannot become the session-tree leaf and no burst can resume it.
- The product's own stdout is part of that evidence: the envelope prompt has the
  tester boot the product with output captured to `product.log` on the session
  volume, so the archive holds what the bot printed and not only what the tester
  typed. That file is authored by the product process — nobody in the box writes it.

## Where it sits in the loop

A cheap-to-expensive funnel in the serial loop:

```
work ok, PR open ──► assess ──findings──► dev round ──push──► assess (again)
                        │
                     APPROVE ──► test ──report posted──► awaiting_review (human)
```

- **Only a clean assess queues a test** — no sandbox boot for a diff that's about to
  change. A pushed dev round re-queues assess; only its APPROVE re-queues the test.
- **Defects route by cause** — the tester classifies each (`preexisting`, judged
  against the diff):
  - **PR-caused** → the report posts as a `REQUEST_CHANGES` review, and the *existing*
    round machinery returns it to the developer — no new states, the rounds rail and
    soft caps apply, and the loop closes naturally: push → assess (its APPROVE clears
    the chip) → re-test.
  - **Pre-existing** → the tester authors a complete symptom issue (the product's
    `symptom.yml` template shape); the control plane relays it verbatim under the
    **`auto` label, never `ready`** — the human gate authorizes all work, machine-filed
    included. Rails: dedup against open `auto` issues by title, ≤3 filed per report.
    This is the monitor's pipeline arriving early: the `auto` provenance framing
    (reproduce-first, body-is-tainted-data) gets its first real feeder.
  - Neither, or a clean run → the report is a plain PR comment, advisory.
- Rails mirror assess: retry with attempts/cooldown; at cap → "machine test could not
  run" comment, task proceeds to `awaiting_review`, never blocked.

## Posting

Everything posts as the **tester App** (`TESTER_`-prefixed credentials — D3) with a
`#### 🧪 Machine test report` header: the report (comment, or `REQUEST_CHANGES`
review when PR-caused defects exist) and the relayed `auto` issues — which require
**Issues: read/write** on the App, alongside its pull-request permission. `E2E=on`
without that App is a startup/preflight refusal, never a silent degrade. (Slice 1
rode the reviewer App as a deliberate deferral; D3 gave the tester its own
provenance.)

## Config

One knob, matching everything else — a toggle:

```
E2E=on|off            # ships off; a registered product reads its row instead (C3)
```

The tester's model, effort, budget and turns are the `tester` role row's (D5):
`control-plane role set tester --model … --effort … --budget … --turns …`, audited,
governing the next burst. An unset effort is the CLI's default for the model.
Plus the sandbox credential refs described above. No tiers, no per-product platform
config — everything product-specific rides in the product's `TESTING.md`.

## Slices

1. **Envelope** — frontmatter parsing, env intersection, test burst after clean assess,
   report posted by the reviewer App. Ships with discord-bot's `TESTING.md`
   (`tools: []` — no registry needed yet). *Shipped 2026-07-04.*
2. **Defect routing** — PR-caused defects return to the developer via a
   `REQUEST_CHANGES` review (the existing round machinery); pre-existing defects
   become tester-authored `auto` issues, relayed by the control plane behind the
   human `ready` gate. *Shipped 2026-07-04.*
3. **Tools registry** — `playwright` baked into the image, enabled per manifest.

## Deliberately out

Product-specific logic in the platform (ever) · driving Discord as a human user
(ToS-violating automation — the product contract defines what E2E means within the
rules) · performance/load testing · testing unmerged *platform* changes (the tester
tests the product).
