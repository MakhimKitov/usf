# Deployment — the platform off the laptop

The T0 deployment (TO-BE §5–6): a **Docker host** running the compose `platform`
profile — the two stores, the migration, two stateless control-plane replicas and
the webhook receiver — with the dev-agent image present on the same host, because
bursts are `docker run` against its daemon. k8s is the reference shape for the
coordination tier (`examples/k8s/`) and gets bursts at Stage G. This document is
the operator's side: what the platform needs from the org, how it comes up, how it
is driven, and how the E6 gate is rehearsed. Everything the platform *is* stays in
`ARCHITECTURE.md`, `SCHEDULER.md`, `STORES.md`.

## What the operator provides

1. **A Docker host** — anything with a daemon and compose. The E6 rehearsal used the
   owner's laptop as the server; nothing below depends on the host being remote.
2. **One public HTTPS route** to the receiver's published port (`RECEIVER_PORT`,
   8080). TLS terminates *in front* — a load balancer, a reverse proxy, a tunnel —
   the platform never holds a certificate. The receiver is the only thing routed;
   the replicas' wake port (8081) stays on the compose network.
3. **The dev App's webhook**, once per App, in the App's settings page (there is no
   API for an App's event subscriptions): Webhook **Active**, URL = the route,
   content type `json`, secret = the value `.env.receiver` names; subscribe to
   **Issues, Pull request, Pull request review, Push**. The App installation is
   the subscription — registering a product configures no webhook. After the
   webhook exists, its URL *can* be moved by API, as the App
   (`PATCH /app/hook/config`), which is how a route that changes is followed.
   Do not add the reviewer/tester Apps' webhooks: every App that delivers is one
   more wake per event.
4. **Two environment files.** `.env` is the platform's (`.env.example`); it reaches
   the replicas and only them. `.env.receiver` is the receiver's
   (`.env.receiver.example`): the App id, its webhook-secret ref and the value the
   ref names — and nothing else, because what a public process can leak is what it
   is handed.
5. **Two directories, pinned in `docker/.env`** (compose's own interpolation file —
   pin them there, not in the shell: a later `up` from a shell without them
   recreates the replicas onto the defaults):
   ```
   PLATFORM_DATA=/srv/usf/data        # clones, sessions — same path on host and in the replica
   PLATFORM_SECRETS=/srv/usf/secrets  # the App private keys, read-only, same path
   ```
   Both are same-path mounts: bursts get bind mounts out of the data dir in the
   *host daemon's* namespace, and `.env` names the keys by path
   (`*_PRIVATE_KEY_PATH`, `file:` refs) — so a path has to mean the same file on the
   host and in the box. Key paths in `.env` are therefore absolute.

## Bring-up

```bash
docker compose -f docker/compose.yaml --profile platform up -d --build --wait
```

builds three images (control-plane, webhook-receiver, and dev-agent — a build-only
service, `replicas: 0`), applies pending migrations as the schema owner, and waits
for two healthy replicas and a healthy receiver. What healthy looks like, in logs:

```
control-plane   event="fleet starting" products=[…] interval_s=60 rails=…
control-plane   event="wake endpoint listening" host=0.0.0.0 port=8081
control-plane   event="listening for wakes" channel=wakes
webhook-receiver event=listening host=0.0.0.0 port=8080 apps=['<app id>'] wake_url=http://control-plane:8081
```

Then, through the route: `GET /healthz` → 200. GitHub's ping (sent when the webhook
is saved) shows up in the receiver's log as `event=ping` and in the App's *Recent
Deliveries* as green.

Replicas and the receiver run `restart: unless-stopped`: a replica that dies comes
back as a new replica, and the ledger's leases make that safe (E1/E2).

## Driving it

Every `control-plane` verb runs against the deployment's ledger through a
one-off container that shares the replicas' environment:

```bash
cp() { docker compose -f docker/compose.yaml --profile platform run --rm --no-deps -T control-plane "$@"; }
cp product register owner/product --team owners      # the registry is the source of what is served
cp product list
cp stop --reason "…"                                 # the org's STOP row: every replica stands down at its next tick
cp resume
cp role set dev --budget 4.0
cp subscription list                                 # who is contributing capacity (F1)
```

**A member contributing their subscription** is two steps, in this order,
because the platform stores only the name of the second one:

1. the member materializes the token where the *control plane's* processes can
   read it — for this compose deployment that is a line in `.env` (both replicas
   read it; the receiver's `.env.receiver` never sees it) or a file under
   `PLATFORM_SECRETS`, which is already mounted read-only at the same path in
   the box as on the host;
2. `cp subscription register --member <handle> --ref env:<NAME> --cap <tokens per window> --expires <YYYY-MM-DD>`.

The value never reaches the ledger, so nothing the platform writes can leak it —
and deleting it at its source revokes the contribution whether or not anyone runs
`subscription revoke`. Rotating a token is step 1 again at the same ref, with no
verb at all; moving it is `subscription set --ref`.

A new image rolls under STOP: `cp stop --reason "rolling"`, then
`up -d --build --wait control-plane` (compose recreates both replicas), then
`cp resume`. Nothing in flight is lost that a lease would not have recovered
anyway — but STOP keeps the roll from racing a spawn.

Logs are the containers' (`docker compose … logs -f control-plane`). A webhook's
path reads, in order: receiver `event="delivery forwarded" delivery=<id>` → one
replica `event="product woken" … delivery=<id>` → every replica
`event="wake from a peer — looking now"` → the settle → one replica polls. The
delivery id is the same one in GitHub's *Recent Deliveries* and in the
`product_woken` event's payload, so the three can be lined up.

## Bursts on a cluster (G3)

`BURST_NAMESPACE` is the whole switch. Set it on the replicas and `open_runner`
returns `JobsRunner`: one k8s Job per burst, three containers (init → agent →
harvest), no docker socket anywhere. Leave it unset and nothing changes — the
compose deployment above runs bursts on the host's docker, which is the T0 shape
and stays supported.

What the cluster has to have:

- `examples/k8s/burst-runner.yaml` applied — the namespace and the account whose
  grant is the four verbs the runner speaks. The control plane reaches the API
  the way `kubectl` does: in-cluster it is that service account, off-cluster it
  is the operator's kubeconfig.
- `kubectl` on the control-plane image's PATH, and **`ARCHIVE_URL` set**. A pod
  moves its own bytes through presigned URLs, so a deployment that runs bursts on
  a cluster needs an object store; `open_runner` refuses at startup rather than
  handing a pod a directory it cannot see.
- the two images reachable from the nodes: the dev-agent image (what the burst
  is) and the phases image (`BURST_PHASES_IMAGE`, default
  `usf/burst-phases:latest`). Both are pulled `IfNotPresent`, so a
  node that already holds them never goes looking for a registry.
- the archive reachable **from the pods, at the same address the control plane
  signs with**. A presigned URL is signed over its Host header: one name, both
  sides. In a cluster that is ordinary service DNS; on a laptop it is the host's
  LAN address, which is what `scripts/burst-cluster.sh` builds.

One command gets a throwaway one (kind + the archive + the phases image), and it
is what the runner contract suite runs against:

```bash
./scripts/burst-cluster.sh                 # prints the three exports
uv run pytest tests/test_runner_contract.py tests/test_jobs_runner.py
./scripts/burst-cluster.sh --delete
```

Two rails are the Job's own. `activeDeadlineSeconds` is the wall-clock ceiling
and is the same number the pod's capabilities expire on — a burst cannot outlive
its own URLs. `backoffLimit: 0` means k8s never retries: a retry is the
scheduler's decision, made with the result file in hand.

## The rehearsal (gate E6, #37)

Manual, about thirty minutes, with the whole platform in containers and **no
platform process on the host** (`pgrep -f control-plane` is empty). Each item names
its pass criterion.

1. **Two replicas + receiver up** — `docker compose … ps`: two healthy replicas,
   one healthy receiver; the route answers `/healthz`.
2. **`ready` → claim in seconds.** Label an open issue `ready`, note the time, and
   watch the ledger for its task row (`select created_at from tasks where task_id =
   'owner/product#N'`). Passes when the row appears within single-digit seconds
   and a `product_woken` event with the delivery's id precedes it; the replicas'
   logs show the wake and the settle, not a poll at the interval.
3. **Kill a replica mid-burst.** With a `burst-*` container running, `docker kill`
   the replica whose log spawned it. Passes when: the burst container outlives the
   replica; the survivor leaves it alone while `docker ps` still shows it (`runner.
   alive`); at the first tick after it exits, `burst died without a result — marked
   killed` and `attempt failed — will retry`; at no moment are there two `burst-*`
   containers for the task. If the orphan pushed a branch and opened a PR, the
   retry's harvest adopts it by branch prefix — one PR, not two.
4. **STOP.** `cp stop --reason …` with a `ready` issue pending: every tick logs
   `org STOP in force — standing down`, no task row appears, wakes are still
   recorded (`product_woken`) but change nothing. `cp resume` → the next tick claims.
5. **Replayed and forged webhooks, through the route.** Redeliver a delivery the
   receiver already took (*Recent Deliveries → Redeliver*, or
   `POST /app/hook/deliveries/{id}/attempts`): receiver `delivery replayed —
   dropped`, 200, no forward. POST the same body with a wrong signature: 401,
   `delivery forged`. Passes when neither adds a row to `events` nor a grant to the
   product's observation lease (`claimed_until` on its `repositories` row does not
   move) — zero polls, zero writes.
6. **Single-node parity (#18, unchanged).** Fresh stores on other ports next to
   the live rig — a compose override remapping `5432/8333/9333`, another project
   name so the volumes are new — `migrate`, `product register`, then
   `control-plane burst --issue N --repo ../product --mode check` with
   `LEDGER_DSN`+`ARCHIVE_URL` set, and the same with both unset. Passes when both
   ledgers hold one open task and one `succeeded` burst finalized once, both
   archives hold `sessions/<task>/<burst>/session.tar.gz` with no credential-looking
   entry, and nothing else differs. `down -v` the twin afterwards.

### Record — 2026-09-06, the laptop as the server

- Route: a `trycloudflare.com` quick tunnel (no account; the URL changes on every
  restart and the tunnel dies when the laptop sleeps — followed with
  `PATCH /app/hook/config`). Fine for a rehearsal, not a deployment.
- **Ready → claim: 6.1 s** (`gh issue edit --add-label ready` sent → task row written;
  GitHub delivered 0.75 s after the label landed, the wake settled 2 s, the tick
  claimed 3.6 s after the wake). The first measurement, before the settle
  existed, was **65 s**: the wake fired before GitHub's read side showed the
  label, the poll missed it and held the observation lease for the whole
  interval — `WAKE_SETTLE_S` is that finding.
- Kill mid-burst: replica SIGKILLed 62 s into a 160 s burst; the orphan finished
  and opened its PR; the survivor marked the burst killed 35 s after the container
  exited (the next tick) and the retry adopted the PR. Never two bursts.
- STOP held three and a half hours with a `ready` issue pending (the operator was
  away); zero claims; `resume` claimed at the next tick.
- Replay via GitHub's redelivery and a forgery via the route: 0 polls, 0 writes.
- Parity: identical rows and snapshot shape on Postgres+SeaweedFS and SQLite+dir.
- Found and fixed on the way: the receiver was handed the whole `.env` (now its own
  file); relative key paths did not resolve in the box (`PLATFORM_SECRETS`); a
  replica died of a raw `requests.ConnectionError` during a DNS blip (transport
  failures are now `GitHubApiError`/`GitHubAuthError`, retried next tick); no
  restart policy (now `unless-stopped`). Filed, not fixed: a burst that closes its
  issue as already-resolved is scored "no PR — retry" and burns the remaining
  attempts.
