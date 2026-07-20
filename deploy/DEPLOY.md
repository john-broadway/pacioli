# pacioli deploy — blank Debian 13 host → governed ERP, `doctor: ready.`

The full product, as a paved road: **vanilla ERPNext, production-served, guarded from its
first credential, governed by the broker, instrumented and watched daily.** Extracted from
a real build that runs real books (run records: `broker/docs/plans/2026-07-17-*.md`);
every stage is staged/resumable, every claim gated on a positive readback.

ERPNext stays **vanilla** throughout — no fork, no patch. The product is the governed
deployment, which is exactly what makes it replaceable-by-design and worth trusting.

## Shape

Two hosts (one is allowed but not the posture this road paves):

- **the target** — the ERP box. Stages 1–3 run here as root.
- **the broker host** — holds the consent hand (mint), the receipt chain, and the cadence.
  Stage 4 runs here. Separation is the point: the box that keeps the books is not the box
  that consents to writes.

## The road

0. Copy `deploy.env.example` → `deploy.env`, fill it. Build a `pacioli-guard` wheel and
   set `GUARD_WHEEL`. Verify `scope-methods.list` against a reference install
   (`dump-live-scope.sh`) — scope is data, not folklore.
1. **`provision.sh`** *(target)* — deps → bench → site → ERPNext → TZ (positive readback) →
   **gunicorn + systemd unit split from birth** (the dev server never serves). Secrets are
   generated on-target, root-only 600, never echoed. Ends: API 200 by name AND by IP.
2. **`govern.sh`** *(target)* — guard installed **before any credential exists** → company
   (with the wizard-less fixture) → the tight seat (dedicated read-role, api keys, **no
   manager roles**) → deny-by-default scope from the data lists → the SoD workflow
   (masters first, self-approval OFF). The seat's secret lands frappe-owned 600; **carry
   it to the broker host, don't paste it anywhere.**
3. **`perimeter.sh`** *(target)* — nginx static front on :80 (Debian seams pre-fixed) +
   nftables contain (`:80` ← your TLS proxy only; `:8000/:9000/:22` ← the broker host).
   **TLS terminates at the proxy of YOUR house** — any door; this road doesn't pick one.
4. **`instruments.sh <name> <host:port> <api-key> <secret-file>`** *(broker host)* —
   registry (secret by `file:` reference; inline is refused by the parser), then the gate:
   **`pacioli doctor` must say `ready.`** → genesis anchor (carry a copy off-box) →
   baseline census → daily cadence timer (`ATTENTION` file exists ⇔ something failed).

Day-2 — **`upgrade.sh --snapshotted`** *(target)* — the recorded upgrade drill, codified.
Host-level snapshot first (the script **refuses** without the attestation — the snapshot
is the UNDO for a schema migration; bench has none). Then: discard build drift
(`bench build` dirties erpnext's `banking/yarn.lock` in-tree, which makes the next
`bench update` refuse the pull as "local changes"), stop the five serving units with
**redis kept up** (migrate needs it; workers must not see a mid-migration schema),
`bench update` under `~/.frappe_env` (node/yarn live in nvm), restart, 200 through
gunicorn AND the nginx front. The upgrade is not done until the **broker host's**
doctor says `ready.` again.

Day-2 — **`restore.sh <db.sql.gz> [public.tar] [private.tar]`** *(target)* — the recorded
restore drill, codified. Restores from files **you carry in** — deliberately not from
`sites/<site>/private/backups/`, because bench backups land on the box and a real
disaster takes them with it: **carry every backup off-box, sha-checked**. The script
stops the serving units (redis stays), restores with mariadb root **by reference** from
the provision-generated secrets env, **re-enables the scheduler** (`bench restore`
leaves it disabled — a silently-degraded site otherwise) with a positive readback,
migrates, restarts, 200 through both doors. Whole-guest loss is the hypervisor's
restore path (snapshot rollback / PBS) — drilled at the host layer, not this script's
job. The restore is not done until the **broker host's** doctor says `ready.` and the
chain still matches the off-box anchor.

## Definition of done

Not "the scripts ran" — the end-user proof: `doctor → ready.`, census statement balanced
with response findings 0, the desk answering through your TLS door, and the negative
probes holding (`:80` refused from anywhere but the proxy; `:8000` refused from anywhere
but the broker host). The kit itself is proven by **rebuilding a lab from blank using only
this road** before the word "product" goes on any public surface.

## What this road does NOT yet cover (honest boundaries)

- **Upgrades** — **drilled + codified 2026-07-17** (`upgrade.sh`; run record
  `broker/docs/plans/2026-07-17-upgrade-drill.md`). Honest note: the drill's version
  delta was zero — upstream `version-16` hadn't moved since install — so the machinery
  (refuse-on-dirty, pull, requirements, patch, build, restart, doctor gate) is proven,
  and the first nonzero jump rides the same road with the snapshot as UNDO.
- **Restore** — **drilled + codified 2026-07-17, both layers** (`restore.sh`; run record
  `broker/docs/plans/2026-07-17-restore-drill.md`). Host layer: snapshot rollback proven
  through the hypervisor (rolled-back guest booted to doctor `ready.`). Bench layer: the
  site database was DROPPED and on-box backups deleted, then restored from the off-box
  copy alone — every account back, doctor `ready.`, chain matching the off-box anchor.
  A trusted install is eventually a restored install; this one has been.
- **SMTP** and multi-company postures.
- **frappe_docker overlay — LAB-PROVEN 2026-07-20 from blank** (`frappe_docker/`; run record `docs/plans/2026-07-20-frappe-docker-labproof.md`):
  guard baked via pip-into-bench-venv + apps.txt stub (apps.json turned out git-only at the
  bench-source level, so the "apps.json custom image" plan was corrected); broker stays a
  separate host by design, never a co-located sidecar. Proven at the guard/site layer:
  from-blank image → site → guard enforcing (in-scope 200 / out-of-scope 403 / out-of-verb
  403). Still owed there: the broker `doctor: ready.` leg and the container upgrade drill —
  see `frappe_docker/README.md` Known unknowns.
