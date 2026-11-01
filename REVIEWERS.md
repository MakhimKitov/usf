# Review providers — one role, selectable brains, everything lands on GitHub

Machine reviews are **additional content, not load-bearing structure**. The platform's
only hard dependency is the Claude subscription that powers the dev itself; the human
merge gate (CODEOWNERS + branch protection, `GITHUB-INTERFACE.md`) is the only required
review. Everything between is optional enrichment tied to subscriptions the operator may
not have. Every declared reviewer is verified at startup, loudly — never silently
pretend a reviewer exists (the 201 lesson: two cycles believed Copilot was requested
while GitHub dropped it; falsified 2026-07-03, PR #6).

**The model — a toggle and a selector, nothing else.** Machine review is **on or off**;
when on, *reviewer* is a burst **role** and the **provider selector** names who fills
it. Every provider delivers the same artifact — **a PR review with inline findings, on
GitHub** — so there is exactly one review surface, one trigger, one etiquette,
regardless of whose brain produced the findings.

**Delivery rule:** one dev-agent image, one control plane. Every provider's mechanism
ships universally and lies dormant; the declared policy activates it. Enabling a
provider is "name it in the selector, supply its credential" — never "rebuild the
image".

## No pre-PR review stage (decision record)

M0–M2 ran a Codex self-review *inside* the dev burst, pre-PR. Dropped, deliberately:

- **Its output wasn't a platform artifact.** The verdict survived only as a quote the
  dev pasted into the PR body — unverifiable, unthreaded, invisible to the round
  machinery (no fix/reply/defer, no cursor, no caps). Everything the experiment wants
  auditable happened off the record, inside the context that wrote the code.
- **It put a second vendor's credentials in the dev's box.** Three containment issues
  (#1 cred-scrubbing shim, #2 bypass-flag walls, #3 bubblewrap/seccomp) existed only to
  make that safe. As a review *provider*, Codex runs in the reviewer container instead —
  which holds **no GitHub credential**, so the blast radius shrinks on both axes.
- **What survives:** tests + CI still gate before the PR opens, and the contract keeps
  "re-read your full diff before opening the PR" — the discipline is free; only the
  subsystem is gone. The cost is honest: garbage is caught one visible round later,
  on the record.

## Providers

Two driver kinds behind one interface: **spawn** (a reviewer-role burst whose engine
produces findings) and **request** (an external service reviews on GitHub's side).
`claude` is the natural pick when in doubt — it needs no extra subscription, so the
round machinery runs on what the platform already requires; `codex` and `copilot` add
cross-vendor eyes (different vendors, different blind spots). And review **off** is
still a complete pipeline: the contract's real invariants (tests, evidence, small
reversible changes) gate in CI, and the human gate holds regardless.

| Provider | Driver | Credentials (verified at startup) | When not selected |
|---|---|---|---|
| `claude` | spawn — reviewer burst, Claude engine | `REVIEWER_GITHUB_APP_ID` + `REVIEWER_GITHUB_APP_PRIVATE_KEY_PATH` | no reviewer bursts |
| `codex` | spawn — reviewer burst, codex engine (auth mounted into the *reviewer* container only) | reviewer App creds **and** operator's `~/.codex` auth | codex binaries idle in the image |
| `copilot` | request — PAT call, echo-verified (App tokens are silently dropped) | `GITHUB_COPILOT_REVIEW_TOKEN` | never requested; `copilot-instructions.md` idles |

The owner is not a provider — the merge gate is unconditional and stays out of this file.
Spawn providers post through the **reviewer App** (`<product>-ai-reviewer`, Pull requests
read/write only): GitHub forbids a PR's author from reviewing it, and the dev App
authored the PR. The reviewer App is the same shape as the phase-1 tester App.

## Policy — a toggle and a selector (`ReviewPolicy.from_env()`, beside `Rails`)

```bash
REVIEW=off                       # the toggle — on: machine review; off: tests/CI and
                                 # the human gate stand alone
REVIEW_PROVIDERS=                # the selector — comma-list of claude,codex,copilot;
                                 # must name at least one provider when REVIEW=on
```

The reviewer's model, effort, budget and turns are the `reviewer` role row's (D5):
`control-plane role set reviewer --model … --effort … --budget … --turns …`, audited,
governing the next burst. An unset effort is the CLI's default for the model. A different model than the dev's offsets same-family bias.

- **The toggle owns *whether*, the selector owns *who*.** `REVIEW=off` ignores the
  selector entirely — a curated provider list survives being switched off.
- **Everything is declared, nothing is inferred.** No credential sniffing, no implicit
  defaults: the platform reviews with exactly the providers named, and ships with the
  toggle off — turning review on is the operator's explicit act, same as naming who
  does it.
- **A declaration that can't be honored refuses to start**: `REVIEW=on` with an empty
  selector, or `copilot` named without its PAT, is a startup error, not a silent
  degrade — a declaration is a promise the platform must not quietly break.
- The scheduler prints the resolved switchboard at startup, next to the rails:

```
[scheduler] review: on · providers=claude, copilot · gate=owner
```

## The reviewer burst

Same infrastructure as every burst — same image, same walls, same harvest, same ledger —
launched with a different envelope (`bursts.role = reviewer`; the column has existed
since M0 for exactly this split):

| | Dev burst | Reviewer burst |
|---|---|---|
| Session | resumes the per-task session | **always fresh** — deliberately amnesiac |
| GitHub credential | App installation token | **none** |
| Engine | Claude | Claude (`claude`) or codex CLI (`codex`) per provider |
| System framing | the product's operating contract | a reviewer contract: adversarial, verdict + line-anchored findings |
| Model | the `dev` role row | the `reviewer` role row (`role set reviewer --model`, D5) |

- **No GitHub credential in the container.** The burst reads the mounted clone (synced
  to the PR head) and returns **structured findings** through the harvest:
  `{verdict, findings: [{path, line, body}]}`. The control plane posts them as the
  reviewer App — one REST call, `POST /pulls/<n>/reviews`, findings as inline comments,
  the overview styled by the control plane (header + resolved model + reviewed commit —
  the sha is what keeps a multi-round PR's review history legible).
  The **event is the verdict**: findings → `REQUEST_CHANGES`, clean → `APPROVE` (legal
  because the reviewer App is never the PR author — that's why it's a second App). A
  later review by the same App supersedes, so the red chip clears when a re-assess comes
  back clean. Caveat, deliberate: no required-approving-reviews branch protection — a
  bot approval must stay *signal*, never satisfy the human merge gate. A reviewer that
  *cannot* write anywhere gives hostile diff content nothing to hijack.
- **Mounts lifecycle — isolation by construction, not instruction.** The clone mounts
  **read-only** into the reviewer (`:ro`). The prompt has it judge by reading — CI owns
  test execution; a small self-check snippet goes to `/tmp`, never the mount. Its
  session volume is **not seeded** from the task's archive (the dev's transcripts
  must not even be readable from inside) and is **not archived** back — the reviewer
  reads and never executes, so there is nothing to prove; the tester, whose whole job
  is execution, is the role whose transcript is archived as evidence (B5, TESTER.md).
  The reviewer's transcript survives in `data/volumes/<burst_id>/` for the length of
  the run. The shared clone stays safe because the loop is serial and every burst
  path re-syncs before mounting.
- **No findings → an APPROVE review**, which — like a clean Copilot pass — deliberately
  triggers nothing. A dev round fires on a machine `CHANGES_REQUESTED` *or* inline
  findings: the reviewer App carries both, but its body-only 422 fallback keeps only the
  state, and Copilot only ever produces findings (COMMENTED by design).
- The `codex` engine runs inside the same credential-free envelope; its output is
  constrained to the findings schema (a thin harness prompt, not the interactive
  `review` subcommand). The sandbox walls (shim, seccomp) move with it and out of the
  dev's box.

## Scheduler deltas

Small generalizations of M2 (`SCHEDULER.md` stays authoritative for the state machine):

- `COPILOT_REVIEWER` becomes a **reviewer registry**: the set of logins whose unseen
  inline findings trigger a dev review round (Copilot's bot + the reviewer App's bot).
  Trigger logic, `last_review_id` cursor, soft cap — unchanged.
- **On PR open and after round pushes, fire every active provider**: spawn providers
  queue a reviewer burst (serial slot, no request API, nothing to drop silently);
  request providers call GitHub with the echo check. Findings from any provider advance
  the same round counter; the round prompt already carries all open threads.
- Reviewer bursts **count against the daily ceiling** (they spend the same subscription)
  and **never block the task** — reviews are additive, so the failure mode is "no
  machine review this round", logged, task proceeds to the human gate.

## Contract change (product repo)

The product `CLAUDE.md` stops naming vendors and drops the self-review subsystem — a
repo maintained by an operator with every subscription must not carry a contract that
lies for an operator with none. Review section becomes: *"re-read your full diff before
opening the PR; the platform arranges machine review on the PR and re-requests after
your pushes; triage every finding in its thread (fix / reply / defer); the human owner
approves and merges."* The codex-review skill leaves the toolkit's instructions; the
binaries stay in the image (delivery rule) for the reviewer role.

## Requirements vs capabilities (noted, not built)

A product repo could someday *require* "≥ 1 machine review before the human" while the
operator's config *provides* capabilities; the scheduler would warn when requirements
exceed capabilities. Phase 0 keeps exactly one requirement — the human gate — so this
stays a note.

## Implementation slices

1. **Policy + registry + honest degradation** — `ReviewPolicy.from_env()`, startup
   matrix, fail-loud declarations, `copilot` as the first (request) provider behind the
   registry. Mostly re-plumbing what exists; no new capability. **Shipped 2026-07-03**
   (`review.py`; scheduler triggers/requests policy-gated).
2. **The reviewer burst** — reviewer App, `reviewer`-role burst with the `claude`
   engine, findings harvest + posting path, contract slimming (self-review subsystem
   out of the dev burst and the product contract). The autonomous review cycle becomes
   self-contained. **Shipped 2026-07-03** (`assess` burst kind; findings contract =
   the burst's final message, parsed by `review.parse_findings`; posting via
   `post_review` with a body-only 422 fallback; reviewer App creds under
   `REVIEWER_GITHUB_APP_*`, bot login learned at startup).
3. **`codex` engine** — the findings-schema harness around the codex CLI inside the
   reviewer envelope. Optional; a second vendor's eyes, nothing structural.

## Deliberately out

`platform.toml` (env vars hold until a second consumer of config exists) · review
quality scoring / reviewer ensembles · per-provider round caps (the shared rail is
enough at this scale) · tester role and E2E runs (TESTER.md) · monitor-filed
issues (provenance gate ships regardless).
