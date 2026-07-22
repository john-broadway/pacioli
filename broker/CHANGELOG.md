# Changelog — Pacioli (broker)

All notable changes to the `pacioli` MCP broker. Honest pre-1.0 semver: `0.x` = maturity/intent,
bumped deliberately; a public release is a separate act. Deploy identity = git commit SHA.

> References to `docs/plans/…` (and other build-record files: `GO-LIVE.md`, `docs/specs/…`, scout notes, redteam reports) are the workshop's internal run records — the day-books behind
> each entry. The public tree carries the proofs (`SCOPED-TOKEN-PROOF.md`) without the day-books.

## 0.31.0 — 2026-07-22 — the breadth roof: 51 governed doctypes, 265 tools, live-proven

MINOR (a large, backwards-compatible surface growth + one fix; no grant widened, deny-bias
unchanged everywhere). The breadth campaign, complete: every remaining GOVERN row landed —
**51 governed doctypes, 30 → 265 tools** — each one dossiered from v16 source, landed with its
own risk-flag disclosures, closed-books date pin, cascade shape, and tests (3,108 broker green).
The whole surface then **live-proven on a real ERPNext v16 bench as a guard-scoped seat**: 20+
full governed verticals (draft → plan → mint → submit → replay-refused → cancel), the armed-
Budget control-plane probe (belt disclosed at plan, bench refuses the PO, cancel disarms, same
PO passes), the Timesheet second-writer probe (a Sales Invoice's submit rewriting a submitted
Timesheet, disclosed before consent, confirmed live both directions), and a 3-node Asset
cascade under one consent. Run records: `docs/plans/2026-07-21-liveprove-batch-38.md` +
`docs/plans/2026-07-22-liveprove-batches-bcd.md` (internal day-books).

- **New governed doctypes: 47 since 0.30.x** (which shipped the founding four — Sales
  Invoice, Purchase Invoice, Journal Entry, Payment Entry — as 30 tools). The surface now
  spans the order stage (Sales/Purchase Order, Quotation, Supplier Quotation, Material
  Request, RFQ, Blanket Order…), the stock rows (Stock Entry, Delivery Note, Purchase
  Receipt, Stock Reconciliation, Pick List, Packing Slip, Landed Cost Voucher…), assets and
  maintenance (Asset and its movement/adjustment/repair/capitalization family, the
  maintenance rows), manufacturing and subcontracting (BOM, Work Order, Job Card, Production
  Plan, the Subcontracting trio), and the control-plane rows (Budget, Timesheet, Contract,
  Dunning…). The exact list and each row's findings live in `pacioli/erpnext.py`'s own
  SUPPORTED_DOCTYPES comment block — the source of truth this file deliberately doesn't
  restate.
- **FIX — the Asset cascade was structurally dead (live-prove find, closed same-arm):**
  Asset's own submit auto-creates a submitted Asset Depreciation Schedule sibling, and that
  unmodeled graph node — a doctype with ZERO date fields in its schema — hit the unmodeled
  `posting_date` default and the period-lock gate's non-ISO deny killed every Asset
  `cascade_cancel` at preflight. New `GRAPH_NODE_DATE_FIELDS` (the BOM declared-dateless pin
  extended to graph participants, source-verified three ways) unblocks it; deny-bias unchanged
  for every unpinned node. The unit fakes now refuse non-ISO dates exactly like the real
  client (nine tests reshaped to the live-true plan-stage refusal).
- **Debt pass — disclosures the earliest rows predated:** the four founding stock rows (Stock
  Entry, Stock Reconciliation, Delivery Note, Purchase Receipt) now disclose the Repost Item
  Valuation scheduler channel both directions, with the source-verified asymmetry (cancel arms
  it UNCONDITIONALLY; submit only for back-dated postings); Work Order now discloses its
  reserve_stock machinery (transfer-vs-create dispatch, the silent partial reservation, the
  live-state throws, the sibling Stock Reservation Entry cancels riding a cancel's consent).
- **Deploy kit hardened from the live-prove findings:** `govern.sh` gains g3b (full-verb seat
  DocPerms for every governed doctype + RBAC-transitive grants — `Item`/`Asset Depreciation
  Schedule`/`Asset Maintenance`, all three live-caught — with frappe's first-custom-row trap
  handled and documented in DEPLOY.md) and now GENERATES the per-doctype `.submit`/`.cancel`
  guard patterns from the doctypes list (the twice-live-confirmed stale-methods gap, closed
  structurally). New data files: `scope-graph-doctypes.list`, `seat-transitive-grants.list`.
- **Known limitation (disclosed, decision pending):** a Timesheet billed by a Sales Invoice
  links back to it, so the pair forms a cycle the cascade planner refuses and each leaf cancel
  names the other — no governed cancel path exists for that pair yet (ERPNext's own native SI
  cancel handles it; modeling the self-unlinking edge is a design decision, not a patch).

## 0.30.2 — 2026-07-17 — cleaner wheel: test suite out, SPDX license

PATCH (packaging + metadata; no API or behavior change). The distributed wheel no longer ships the
test suite — `pacioli.tests` is excluded from `packages.find`, so `pip install pacioli` is smaller
and carries no stray importable test namespace. License metadata moves to the PEP 639 SPDX
expression (`license = "Apache-2.0"`, the deprecated `License ::` classifier dropped, build requires
`setuptools>=77`); wheels now carry `License-Expression: Apache-2.0`. Verified end-to-end by
`scripts/install_smoke.sh` against a freshly-built wheel.

## 0.30.1 — 2026-07-17 — official MCP registry ownership marker

PATCH (metadata only — no code, no behavior change). Adds the marker the Official MCP Registry
requires to verify PyPI package ownership, so `pacioli` can be listed at
`io.github.john-broadway/pacioli` (and propagate to Glama / mcp.so / PulseMCP).

- **`mcp-name: io.github.john-broadway/pacioli`** added to the broker README — Pacioli's PyPI
  long-description, which the registry greps on the published artifact. Invisible HTML comment;
  no rendered change.
- `server.json` and the LobeHub manifest re-stamped 0.30.0 → 0.30.1. Tool surface unchanged (30).

## 0.30.0 — 2026-07-17 — amend seats the draft in the workflow (the F1 dogfood finding)

MINOR (new amend behavior + new deny-paths; no new grant, no new ERPNext read beyond the
Workflow read every governed path already requires). Found by the FIRST dogfood drive — Claude
driving the broker as a real MCP agent over the stdio door against the lab bench.

- **The finding (F1):** an amendment created under an ACTIVE workflow was born with no
  workflow state — the strip correctly drops the cancelled document's state, the bench's REST
  insert does not backfill one (live-observed), and `request_workflow_transition` (rightly)
  refuses a null state. Net: the broker's own amend → govern path was stuck; the draft could
  only be rescued from the desk UI.
- **The fix:** `amend_*` now seats the draft at the governing workflow's initial state —
  frappe's own convention (`states[0].state`, the seat `frappe/model/workflow.py` gives a new
  document), written to the workflow's own configured `workflow_state_field`, never a
  hardcoded name. The seat is disclosed in the result AND both receipts (`workflow_seat`), so
  the book shows the draft was born already standing in the workflow.
- **Deny-biased, gated BEFORE the intent receipt (a refusal never leaves an orphan):**
  ambiguous or malformed workflow config refuses with the same reasons as every other workflow
  gate; an unreadable Workflow list refuses (never reads as "no workflow"); a workflow that
  CANNOT seat a fresh draft (no `workflow_state_field`, no states, malformed first state, or a
  first state mapping to `doc_status` "1"/"2" — a draft must never wear a submitted state's
  name) refuses whole, naming why — creating the draft unseated would silently recreate the
  stuck shape.
- **No workflow configured = byte-identical behavior to 0.29.x** (no seat, no disclosure, no
  new call beyond the Workflow list read) — pinned by test.
- New pure core `pacioli.workflow.initial_seat`; `amend_payload(source_doc, seat=None)` applies
  the seat AFTER the strip (also overwriting a CUSTOM state field's stale copied value).
- **The pre-merge review (high-effort, adversarially verified) caught 3 real ones, fixed
  test-first:** (1) the seat field had NO reserved-key guard — a `workflow_state_field`
  (mis)configured as `amended_from`/`docstatus`/a child table would overwrite the very keys the
  strip-list protects; now `seat_conflict` (shared by both layers) refuses every strip-list key
  except `workflow_state` itself, underscore fields, and child tables. (2) asymmetric
  whitespace handling — the state was returned unstripped (a `"Draft "` seat matches no
  transition row: stuck again, with receipts asserting the seat) and the readers used the raw
  configured field name; both halves of the seat are now stripped and both reader sites
  normalise the field the same way. (3) `workflow_seat` was disclosed from the REQUEST — it is
  now `confirmed` against the created document (the E1 rule): a bench that silently drops the
  field yields `confirmed: false` in the result and the outcome receipt, never a book asserting
  a seat that didn't land. Plus: the thrice-repeated find_active+sentinel-deny idiom extracted
  to `_resolve_active_workflow` (one place for the refusal taxonomy).
  Suite 1720 → 1747.
- **Residual CLOSED same night (the armed lab window ran):** the e2e pin held live, first
  contact — amend under active Gate5 seated the draft (`workflow_seat.confirmed: true` from the
  bench's own answer: the REST insert persists the field), the draft was born at `Draft` with a
  legal transition, `Request Approval` ran, the bench state moved to `Pending Approval`, chain
  18 → 22 receipts verified, orphans unchanged. Full record:
  `docs/plans/2026-07-17-amend-seat-pin.md`. The seat is live-proven.

## 0.29.1 — 2026-07-17 — webguard: a replayed body no longer fakes a client disconnect

PATCH (bug fix to shipped 0.28.0 behavior; no new capability, no new grant). Found LIVE by the
doors lab-pin driver's arm-free smoke — the first time a real MCP client drove the HTTP door
through the webguard over an actual socket.

- **The bug:** `guard_asgi` buffers a protected POST body and replays it to the app — and the
  replay answered every post-body `receive()` with a synthetic `http.disconnect`. The MCP
  streamable-HTTP session manager polls `receive()` *while streaming its SSE response* to watch
  for a real client hangup, saw the fake disconnect immediately, and aborted the stream before
  the result was sent: **every HTTP-door MCP call under the guard returned an empty SSE stream**
  (200, headers, no events) and real clients hung. The 0.28.0 end-user drive exercised the
  guard's refusal paths (Host/CORS/413/408), never a full call through a real MCP client — the
  gap that let this ship. The A2A door was unaffected (its SDK app never re-polls receive
  mid-response).
- **The fix:** after delivering the buffered body once, the replay DELEGATES to the real
  `receive` — a live client blocks there, a real disconnect surfaces from it. Only a disconnect
  the guard itself saw during the body read is answered directly (never re-poll a channel known
  dead). Three new tests pin the contract (connected client never reported disconnected · a real
  disconnect still reaches the app · a mid-body disconnect is reported, not swallowed), plus all
  three doors re-driven end-to-end over their real transports (stdio/http/a2a).

## 0.29.0 — 2026-07-17 — the signed agent card: ES256, key in the operator's tier

MINOR (a new opt-in capability; inert unless a signing key is configured; `pacioli_guard`
byte-untouched, no new grant, no new ERPNext read). The LAST of the two follow-on slices the A2A
door's redteam scoped (webguard-parity shipped 0.28.0; this closes card signing). Design +
key-custody ruling: `docs/plans/2026-07-17-a2a-card-signing.md`. Mechanism mirrors Proximo's
SIGNET; **composition-not-coupling — Pacioli's own, zero shared code.**

The A2A agent card can now carry a cryptographic proof it came from the operator who runs these
books, closing the MITM-substitutes-a-forged-card gap the 0.27.0 unsigned card disclosed.

- **Signing helpers (`pacioli/a2a.py`, lazy crypto imports):** `load_signing_key` (PEM EC P-256,
  refuse-if-group/world-readable — the seal-key discipline — refuse-not-P256/missing/non-PEM,
  RFC 7638 thumbprint `kid`); `sign_card` **ES256-PINNED** (never the a2a-sdk's HS256 default —
  the JWT algorithm-confusion class closed at the source); `public_jwk`/`jwks` (public point
  only, never the private scalar `d`); `verifier_for_jwk` (ES256-only, **jku-ignoring** pinned
  verifier — a MITM can't substitute their key by pointing `jku` at an attacker JWKS; the
  client-safe pattern, shipped so the broker's own tests prove the seal is verifiable and
  downgrade-proof).
- **Key custody — the operator's tier (John delegated the ruling).** The key lives with the seal
  key + the consent marker: a PEM held by reference at `PACIOLI_A2A_SIGNING_KEY_FILE`, 0600,
  refuse-if-exposed. **Opt-in, never auto-minted** (unlike the seal key — a published identity is
  not minted silently): unset → unsigned card (0.27.0 behavior); set → sign, or **FAIL LOUD** on
  a missing/exposed/non-P256 key (never serve unsigned when signing was intended). Honest ceiling
  stated plainly: a signing key only proves authorship if the agent can't REWRITE it, so it must
  live outside the agent's own write reach.
- **Wiring:** `pacioli serve --a2a` with the key configured serves an ES256-signed card + `GET
  /.well-known/jwks.json` (public key, pre-auth like the card, still Host-checked). `pacioli
  a2a-keygen [--out PATH]` mints an EC P-256 key (0600, `O_CREAT|O_EXCL`, refuse-overwrite) and
  prints the operator-vault reminder. `[a2a]` extra → `a2a-sdk[signing]` + `cryptography`.
- **Proven live:** minted a key, served `--a2a` with it, fetched the card + `/.well-known/jwks.json`
  (public-only, no private `d`), verified the card's ES256 seal against the served JWKS, and
  confirmed a wrong-key verifier is REFUSED (`InvalidSignaturesError`).
- **Adversarial review (security-mandatory — trust-assertion surface; sonnet security + haiku
  correctness):** the crypto core came back CLEAN — algorithm confusion (hand-crafted HS256 and
  `alg:none` forgeries all refused), jku substitution (a live two-server probe: the pinned
  verifier ignores the card's `jku`, no network fetch), private-scalar leakage (never in the
  JWKS/card/logs/errors), the permission boundary (a full `0o0xx` matrix + symlink-follows-target).
  It caught a real MAJOR — mine — fixed test-first: `PACIOLI_A2A_SIGNING_KEY_FILE=""` (a broken
  env interpolation resolving an unset upstream var to empty) was falsy and served UNSIGNED
  silently, breaking the fail-loud contract; a present-but-empty value now FAILS LOUD, matching
  the empty-token refusal elsewhere (proven live). Plus three clean-UX Minors: a directory/FIFO
  at the key path (raw `OSError` → `is_file()` refusal), a dangling-symlink keygen (raw
  `FileExistsError` → wrapped), and an auto-created parent dir that could be world-writable under
  a permissive umask (`mkdir(mode=0o700)`).
- **Tests:** broker 1717 (+24 signing): load/refuse (incl. non-file, present-but-empty, absent),
  sign+verify roundtrip, HS256-downgrade refused, cross-key refused, jwks-public-only,
  served-card-verifies, JWKS-pre-auth, serve fail-loud on every bad-key path, keygen
  0600/refuse-overwrite/dangling-symlink/parent-perms. TDD.

## 0.28.0 — 2026-07-16 — the in-door perimeter: Host + cross-origin + body cap, both doors

MINOR (a new default-on transport-perimeter a legitimate client passes transparently;
`pacioli_guard` byte-untouched, no new grant, no new ERPNext read). The first of the two
follow-on slices the A2A door's security redteam scoped
(`docs/plans/2026-07-16-webguard-parity.md`). Stops delegating the network perimeter to "the
reverse proxy" and builds it in-door across BOTH network doors (HTTP 0.25.0 + A2A 0.27.0) —
closing the two disclosed Minors (no CORS guard, no body cap) and the Host-header divergence the
redteam found. Template was Proximo's `webguard.py`; **composition-not-coupling — Pacioli's own,
zero shared code.**

- **`pacioli/webguard.py` — one raw-ASGI wrapper** both doors mount OUTSIDE their bearer gate.
  Raw-ASGI, not Starlette middleware (the one divergence from Proximo, whose faces are both
  Starlette): Pacioli's HTTP door is a raw-ASGI wrapper around the MCP session manager, its A2A
  door is Starlette — a scope-level wrapper spans both. Three fail-closed checks: (1) **Host
  allowlist / DNS-rebind** — every request, a Host off the allowlist → 400 (default = bind host +
  loopback; `["*"]` disables + warns loudly); (2) **cross-origin guard** on protected POSTs —
  `Sec-Fetch-Site: cross-site`/`same-site` → 403, a cross `Origin` → 403, a non-`application/json`
  body → 415; (3) **body-size cap** (128 KiB) — **stronger than Proximo's Content-Length check**:
  it buffers up to cap+1 bytes and answers 413 ITSELF before calling the app, then replays the
  buffered body, so a chunked request that drops `Content-Length` cannot evade the floor (proven
  live). The bearer 401 stays per-door.
- **Wired into both doors** (`a2a.build_app` protects the RPC path so the card GET stays open;
  `server.serve_http` protects every POST — the MCP endpoint has no discovery route) + a
  `serve --allowed-hosts` flag threading the allowlist to whichever door runs.
- **Proven end-user over a live socket:** bad Host → 400, cross-origin POST → 403, non-JSON →
  415, declared-oversize → 413, **chunked no-Content-Length oversize → 413** (the stronger case;
  the first cut raised inside `receive` and the a2a-sdk swallowed it into a 200 — the live drive
  caught that, the buffer-and-check fix earned its place), and a legitimate governed
  `prove_verify` still completes.
- **Honest note updated:** the reverse proxy's remaining job is now **TLS only** — Host/CORS/size
  are in-door for both doors. Card signing stays the OTHER follow-on slice (key custody is John's
  ruling).
- **Tests:** broker 1658 → 1691 (+33): 21 webguard units (host/cross-origin/body-cap incl. the
  off-by-one and chunked-evasion pins), door-level perimeter proofs over the real `build_app`
  (ASGITransport) and the HTTP `_asgi_app` composition, CLI `--allowed-hosts` parse+thread. TDD.

## 0.27.0 — 2026-07-16 — the A2A door: the first non-MCP door

MINOR (a new opt-in serving surface; stdio and HTTP doors byte-identical; `pacioli_guard`
byte-untouched, no new grant, no new ERPNext read). F6 of the doors ruling, fired same day
(`docs/plans/2026-07-16-a2a-door.md`). The proof this buys: `dispatch()` is protocol-blind —
stdio and streamable-HTTP are both MCP; A2A (v1.0, Linux Foundation — agent→agent delegation,
JSON-RPC over HTTP, agent card discovery) is the first protocol the spine has never seen, and
the same governed refusals hold over it.

- **`dispatch_raw` extracted** (server.py): the ONE locked dispatch core every door's rendering
  wraps — MCP doors render MCP content, the A2A executor a DataPart artifact. The F5
  serialization + worker-thread offload is inherited, never re-implemented (lock-state probed
  by test).
- **`pacioli serve --a2a`** (`pacioli/a2a.py`, lazy `pacioli[a2a]` extra — `a2a-sdk>=1.1` +
  uvicorn): executor parses the house wire shape `{"tool": "<name>", "params": {...}}`
  (`"skill"` alias; the same convention Proximo's A2A door speaks — shape, not coupling);
  agent card = the FULL 30-tool governed surface, one AgentSkill per tool, readable pre-auth,
  bearer scheme declared when secured; startup refusal table IDENTICAL to the HTTP door via
  the same tested primitives (non-loopback sans token refuses to start; token by reference
  only; empty resolution refuses; unclassifiable bind reads as exposed); RPC route answers
  401 (as a JSON-RPC error envelope) before the SDK sees the request. `via.transport: "a2a"`
  + the token's reference label (or `loopback`) into every intent receipt through the same
  F3 seam.
- **Proven with the OFFICIAL a2a-sdk client end-to-end** (in-process ASGI, full JSON-RPC
  stack): card resolves with all 30 skills · `prove_verify` completes over A2A against a real
  store · **a forged submit is refused at `stage: plan` over A2A** — the door claim, the same
  structured deny an MCP client gets · an unknown tool is a structured `stage: request` deny
  (a PROVE-visible answer, not an invisible transport error) · a message with no tool part
  fails the task cleanly (one terminal Task event — probed live: the SDK's non-streaming
  snapshot races a SUBMITTED-then-failed pair) · glue-level exceptions name the exception
  TYPE only, never a traceback or params.
- **Adversarial review (pre-merge, tiered Haiku finders / Sonnet SECURITY lens — mandatory
  for a new listening surface):** model-fidelity + correctness clean. Security caught one
  Major, fixed test-first: `build_app` (public API an embedder / `uvicorn --factory` can call
  directly) had no bind/token check of its own — only `serve_a2a` refused a public bind, so a
  direct `build_app(rpc_url=public, token=None)` constructed a fully-open governed door. Now
  `build_app` re-checks the advertised host itself and refuses (the exact defense-in-depth
  Proximo's `build_app` carries). Two Minors judged and DISCLOSED rather than half-built (both
  the reverse proxy's job, both pre-existing in the HTTP door, neither currently exploitable):
  no in-process CORS/Origin guard (a browser cross-origin call is blocked only incidentally by
  the SDK's mandatory `A2A-Version` header — front with a strict proxy CORS policy) and no
  request-body size cap.
- **Honest notes, disclosed not half-fixed:** the agent card is UNSIGNED this slice (key
  custody is its own ruling); an A2A door is a standing listener by the protocol's nature
  (acceptable only because doors are opt-in); the reverse proxy is the perimeter (TLS +
  Host-header allowlist + CORS + body-cap — a first-class in-door perimeter matching Proximo's
  webguard across both network doors is tracked as its own slice); the live-wire
  `via`-in-committed-receipt remains the SAME lab pin 0.25.0 staged (the seam is pinned by
  test; an act's receipt needs a reachable bench).
- **Dead-word scrub:** ten `mouth` survivors from the 0.25.0 vocabulary rename found in
  `store.py`/`runtime.py` docstrings — now DOORS vocabulary throughout.
- **Tests:** broker 1614 → 1658 (+44): +4 `dispatch_raw`, +40 A2A (incl. the security-lens defense-in-depth pins)
  (pure parser/via, card, refusal table, perimeter over ASGI, sdk-client e2e, CLI surface
  incl. `--http`/`--a2a` mutual exclusion and per-door port defaults). TDD.

## 0.26.0 — 2026-07-16 — the count-anchor: one pin, all three chains

MINOR (a new anchor format version + new store read surface; `pacioli_guard` byte-untouched, no
new grant, no new ERPNext read). Design: `docs/plans/2026-07-16-close-count-anchor.md` — the
slice the doors ruling queued AHEAD of the A2A door. John's go, same day.

The last chain gets its off-box pin. `close_records` (0.23.0) is per-row HMAC'd + seq-contiguous
— interior deletions and content edits are caught — but a TAIL-ROW DELETION left survivors
contiguous and verifying, so the gate read clean and the period CURSOR SILENTLY ROLLED BACK: the
exact hole the receipt chain (v1 pin) and the seal history (v2 pin, 0.21.0) already had closed.
Its own docstring said it plainly: "there is no count anchor for `close_records` yet." The "yet"
is out. Parity, not a new trust model.

- **Store:** `close_head()`/`close_count()` — the pin pair, plain keyless reads mirroring
  `seal_head()`/`seal_count()`; `close_gate_state(expected_close_head=…, expected_close_count=…)`
  keyword-only kwargs via a pure `_check_close_pin` (never raises; `(None, 0)` native-empty
  accepted; lookup by `seq` VALUE never list position; count 0 vouches for nothing). Pin causes
  are stable machine tags per this table's own contract — `anchor_behind` (tail truncated),
  `anchor_diverged` (pinned position rewritten — catches a KEY HOLDER's in-place edit of that
  row), `anchor_malformed` (folded, never raised) — and a pin failure outranks the plain
  derivation and NULLS `cursor`/`last_close_seq` (rollback evidence: row content not
  trustworthy). No pin supplied = byte-identical to 0.25.0 (regression-pinned by test). Honest
  ceiling restated, not oversold: per-row HMAC, not prefix-chained — the pin fixes the one row it
  names plus the count; a key holder rewriting any OTHER row is still not caught (pinned by
  test, same as the seal's).
- **Anchor format v3:** `close_head`+`close_count` ride the same record beside the receipt and
  seal pairs — one off-box pin covers all three chains. v3 is a strict superset of v2
  (close-without-seal refused); both-or-neither per pair; same shape rules
  (`_validate_head_count_pair`, label `"close"`); v1/v2 records still parse and v1/v2 EMISSION is
  byte-identical for callers that never pass the new fields. The legitimately-empty close table
  ("no period has ever closed yet" — the cursor's honest genesis, unlike the seal's seeded
  genesis row) pins as (`GENESIS`, 0): the COUNT is the claim, and a count-0 pin vouches for
  nothing on replay.
- **CLI:** `anchor write` reads the close state+rows from ONE `close_records_snapshot()` (the
  seal slice's F3 self-inconsistent-pin race closed here by construction) and refuses to witness
  a history carrying any cause EXCEPT the workflow latch — `gapped_awaiting_attestation` (fully
  verified rows, gate up awaiting attest) pins normally, exactly as a genuinely SEALED broker
  does. `anchor check` replays the close pair through the store contract's cause tags (never
  prose-matching — the seal slice's Task-2 Critical, inherited as a rule); a pre-v3 pin WARNS per
  uncovered table (v1: seal AND close; v2: close), never silently "fine". The off-box reminder
  now names `close --advance`/`attest` in the re-pin discipline.
- **Tests:** broker 1614 (+49 real, no double-counting): store pin suite mirrors
  `TestSealAnchorPin` (the control test reproduces the silent cursor rollback, then catches it
  with the pin; honest-ceiling key-holder-rewrite-before-the-pin pinned as documented
  behavior), anchor v3 shape suite mirrors v2's, CLI close-fields suite shares
  `TestAnchorCli`'s fixture via a mixin WITHOUT re-running its 25 tests as apparent
  close-slice coverage (the verify pass caught that inflation — the parent run already
  exercises the v3-emitting write). TDD throughout (red → green).
- **Adversarial review (pre-merge, tiered Haiku finders / Sonnet verify; model-fidelity,
  ledger-integrity, correctness):** ledger + correctness lenses clean (tail-refill,
  seq-manipulation, HMAC type traps, every `make_anchor` combination, every caller of the
  touched surfaces). One finding tightened test-first: `_check_close_pin`'s count-0 branch now
  accepts only the two legitimate empty-table sentinels (`None` — the store's own native pair —
  or `GENESIS` — the record-level pin) and refuses any other head as `anchor_malformed`;
  stricter than the seal's count-0 branch ON PURPOSE (the close count-0 pin is CLI-reachable,
  the seal's is not; `BrokerStore` is public API and not every future caller routes through
  `anchor.py`'s record validation). Plus two comment-accuracy fixes the verify pass caught
  (store/record sentinel distinction; "three reads" → five in the API-surface pin).

## 0.25.0 — 2026-07-16 — any door, one spine (F1–F5)

MINOR (a new opt-in serving surface + a new receipt field; `pacioli serve` stdio is
byte-identical; `pacioli_guard` byte-untouched, no new grant, no new ERPNext read; broker
1537 → 1565, +28). John's ruling, same day (`docs/plans/2026-07-16-any-transport-one-spine.md`,
"cover it all go"): *a door admits; it never decides* — the spine's guarantees (guard
credential floor, out-of-band marker consent, PLAN→CONSENT→PROVE, seal, gate) never read the
wire, so a stdio-only door was a posture, not a proof.

- **The stdio wire RE-PROVEN at 0.24.0 first** (the standing claim, owed since Gate 2):
  a real MCP client against the installed `pacioli serve` subprocess — initialize
  (proto 2025-11-25), all 30 governed tools listed over the wire, unknown tool → structured
  deny (`stage: request`), unreachable bench → structured refusal (never a raw traceback),
  forged submit → refused at `stage: plan`. Bonus: the SDK's inputSchema validation refuses
  malformed arguments before dispatch — an outer belt the wire adds for free.
- **`pacioli serve --http` — the HTTP door (F1, F2, F4):** OFF by default,
  deliberate opt-in. Deny-biased start-up: binds `127.0.0.1` by default; a non-loopback bind
  REFUSES TO START without `--auth`; the token is held by reference only (`env:VAR` /
  `file:/path` — the registry's secrets law; inline literals refused without being echoed);
  an empty resolution refuses (an empty token would admit everyone); an unclassifiable bind
  reads as exposed. With a token, every request needs exactly `Authorization: Bearer <token>`
  (constant-time) or is answered 401 before the MCP layer sees it. TLS stays the perimeter's
  job (reverse proxy), said honestly rather than half-built in-process. Driven end-user with
  curl: refuse-to-start proven, 401 both ways, initialize, 30 tools, governed refusal over
  HTTP. Zero new dependencies (rides `pacioli[server]`'s own SDK + uvicorn).
- **Dispatch serialized (F5) — off the event loop:** both doors funnel through one
  process-wide lock IN A WORKER THREAD (`anyio.to_thread`); the loop stays live for other
  connections while governed acts run strictly one-at-a-time. Concurrent dispatch is its own
  future slice with its own redteam.
- **Adversarial review (pre-merge, tiered Haiku finders / Sonnet verify) caught 4, fixed
  test-first:** (1) the sync dispatch lock originally ran ON the event loop inside the async
  handlers — accidental serialization, deliberate loop-blocking → worker-thread offload with
  a loop-heartbeat test proving both properties; (2) a dying lifespan channel could crash the
  ASGI handler mid-shutdown → exits quietly now; (3) the token-file reader trusted the locale
  → encoding pinned utf-8; (4) the stdio and HTTP doors carried duplicate tool-handler
  registrations → ONE `_register_tool_handlers`, and the ASGI app extracted (`_asgi_app`) so
  the 401/lifespan paths are unit-testable. Both wires re-driven end-user after the fixes
  (stdio W1-W5; HTTP 401/tools/refusal).
- **The principal in the ledger (F3):** every intent receipt now records `via` — which door
  (`stdio`/`http`) and what principal (the token's REFERENCE label like `env:SERVE_T`, or
  `local-spawn`/`loopback`) — stamped at ONE seam (`BrokerStore.set_via`), where the door's own
  declaration always wins over anything a body carries (no caller declares its own door) and
  rides the HMAC chain (relabeling WHO asked is a chain break, not an edit). Undeclared
  stores (every CLI path) record byte-identically to before. DISCLOSED RESIDUAL: proven at
  the store seam and the transport-stamp constants; a live-bench committed receipt carrying `via` is a
  staged pin for the next lab window.

## 0.24.0 — 2026-07-16 — the clock domain: one window, one meaning (T1)

MINOR (a new opt-in registry capability; existing behavior unchanged without it EXCEPT one
deliberate output addition — a bounded `close --reconcile` with no declared `site_tz` now
carries an honest clock note (rendered `clock:` line / JSON `clock` block) saying the window
crossed two clock domains unconverted; every other undeclared path is byte-identical.
`pacioli_guard` byte-untouched, no new grant, no new ERPNext read; broker 1494 → 1537, +43).
Found LIVE by the 0.23.0 bench pins (G3): the bench site ran IST-flavored stamps against the
store's honest UTC clock, and one `--since/--until` string was being applied verbatim to BOTH —
receipt `.ts` (store, UTC) and GL `creation` (site wall clock). Same string, two different
instants: every window boundary skewed by the site's UTC offset between the statement side and
the reconcile side, and an operator's site-local "now" refused as "future" by the cursor-poison
guard. Ruling: `docs/plans/2026-07-16-clock-domain-ruling.md` (John chose T1, 2026-07-16).

- **New registry field `site_tz`** (optional, per-target, the `seat_user`/`posture` pattern):
  the IANA zone the ERPNext site's wall clock runs in. Declared, `close` window bounds mean
  **SITE time** — the books' own calendar — converted once at the CLI boundary
  (`pacioli.clock`, a new pure module) to the store's UTC domain: the statement filter, the
  future-`--until` compare, and the close-record cursor all see store-clock instants, while
  the GL/repost sweeps keep the operator's original site-domain strings (their `creation`
  axis IS the site clock; `--reconcile`'s both-bounds requirement guarantees originals always
  exist, so no back-conversion path exists). Bare-date `--until` expands to end-of-day IN THE
  SITE DOMAIN before converting (the F3 semantic, where the books live).
- **Deny-biased at every edge:** a non-string `site_tz` refuses at registry load; a zone typo
  refuses AT USE with the zone named, only when a window is actually being converted (an
  unwindowed close is never bricked by it); an unparseable bound refuses rather than passes
  unconverted — a silently skipped conversion IS the original defect. An unreadable registry
  under a bounded close warns on stderr that a declared `site_tz` (if any) was not applied.
- **The clock is disclosed, never silent:** with `site_tz` active, the render carries a
  `clock:` line naming the zone and the JSON combined doc a `clock` block with both faces of
  the window (site + store); `close --reconcile` WITHOUT a declared `site_tz` now says
  honestly that the window crossed two clock domains unconverted.
- **G3 ergonomics fixed for free:** a site-local "now" on a UTC-ahead site converts to a
  valid past instant — the future-`--until` cursor-poison guard keeps refusing real poison
  and stops refusing honest operators. (Conversion also normalizes the bound to the store
  stamp shape, removing a lexicographic wobble where a space-separated same-day bound
  compared below a `T`-separated store stamp. KNOWN RESIDUAL, disclosed: targets WITHOUT
  `site_tz` keep that pre-existing wobble — a same-day space-separated future `--until` can
  slip under the naive string compare; declare `site_tz` to close it.)
- **Docs:** bench lab lesson 2 recorded — `--advance` with an `--envelope` naming a
  reconciliation-side class but WITHOUT `--reconcile` records a clean close (no finding
  exists to escalate); correct by construction, now said out loud in the `close` docstring.
- **Adversarial review (pre-merge, 21 agents) caught 4 real correctness defects in the first
  cut, all fixed test-first:** (1) CRASH — an `--advance`-materialized `until` / cursor-
  defaulted `since` has no site-domain original, and the sweep raised `_to_frappe_clock(None)`;
  those bounds now derive their site form from the store canonical (`store_to_site`, which the
  review had simultaneously flagged as dead code — it wasn't dead, it was load-bearing).
  (2) The advance reversed-window guard ran BEFORE conversion, falsely refusing valid
  mixed-separator windows; it now compares converted bounds and reports the operator's own
  strings. (3) Plain bounded `close --json` silently dropped the clock disclosure; the JSON
  now carries the `clock` block wherever the render carries the `clock:` line. (4) A readable
  registry whose target merely failed to RESOLVE was mis-reported as "registry unreadable";
  the two failures are now kept apart. Plus: a Z-suffixed (store-shape) bound under site_tz
  is refused with the domain conflict NAMED, and the F3 end-of-day expansion has one
  implementation.

## 0.23.0 — 2026-07-15 — the close-record: the attestation gate gets teeth (Fork A1)

MINOR (a new operator capability — the period-close record + attestation gate, plus the CLI surface
that reaches it; existing behavior byte-identical without the new flag; `pacioli_guard`
byte-untouched, no new grant, no new ERPNext read; broker 1354 → 1494, +140). `gate_required` was a
decision `response.py` (0.18.0) computed and nothing acted on since Slice 1 — exactly the state
`seal_required` was in before the seal (0.20.0) gave it teeth. This release gives the
attestation-gate its own state: after this build, a period that closed over an
`attestation_gate`-level gap blocks the NEXT period from opening until an operator freshly attests
the gap was reviewed. Fork A1 ruled 2026-07-14
(`docs/plans/2026-07-11-close-half3-response-to-gap.md`); built as its own slice, design + pin
sheet `docs/plans/2026-07-15-close-half3-close-record.md`.

- **New store table `close_records`** (append-only, HMAC'd, domain `close:` — same
  domain-separation discipline as `seal_events`'s `seal:`, so no receipt or seal-event HMAC can
  ever be replayed here, or vice versa, even under the same key): `seq`/`ts`/`action`
  (`close`|`attest`)/`period_since`/`period_until`/`attested_head`/`gapped`/`reason`/`source`/
  `hmac`. The three nullable fields are passed through to the canonical bytes as-is — `None` ("no
  bound" / an `attest` row) and an explicit `""` never collide.
- **`BrokerStore.record_close(...)`** — appends a `close` row inside its own `BEGIN IMMEDIATE`.
  Refuses (`CloseGateLatchedError`) when the gate, derived FRESH inside that same transaction, is
  LATCHED — no TOCTOU between the check and the write. **Compare-and-append**
  (`expected_last_close_seq`, a required keyword — a Task 2 builder finding): the caller's planned
  last-close-seq must still match the store's current one at write time, else
  `CloseRecordStaleError` — two concurrent advances can no longer double-write overlapping periods;
  the loser's write is refused (exit 1) naming the fix (re-run against the moved cursor). Ordering
  is pinned by test: the latch refusal fires before the staleness check when both apply.
- **`BrokerStore.attest(reason)`** — mirrors `unseal` exactly: always `source="operator"`, appends,
  never edits. Refuses (`NoAttestationPendingError`) unless the gate's cause is precisely
  `gapped_awaiting_attestation` — an attest with nothing pending refuses, and so does an attest
  against a gate LATCHED for an INTEGRITY reason instead (attesting cannot repair a corrupt
  history, only confess to a real, reviewed gap).
- **`BrokerStore.close_cursor()` / `close_gate_state()` / `close_records_snapshot()`** — the
  deny-biased derivation (`_derive_close_gate_state`), with one deliberate divergence from the
  seal: **zero rows reads OPEN, not sealed** — absence of close-record history is the honest
  genesis state for a *cursor* ("no period has ever closed"), not a fail-closed case, so the first
  `--advance` against an empty table is legitimate. Past that it is exactly as conservative as the
  seal: a `seq` gap, an unverifiable HMAC (every row recomputed and checked, not only the latest's),
  or a keyless open all latch the gate. The **cursor is verified, never read raw off a row** (Task
  1 review, adversarial finding 1) — a tampered history can never serve a forged cursor even while
  `close_gate_state` on the same rows correctly latches. A bare `None` is ambiguous between "no
  close ever happened" and "the latest close was legitimately open-ended" (finding 2) —
  `close_gate_state()`'s `last_close_seq`/`cursor` pair is the one documented, verified way to tell
  them apart.
- **`pacioli close ... --advance`** — everything `--respond` does, plus: after the render, writes
  the close row (period, chain head, this close's own `gapped`). Requires `--respond` (usage error,
  exit 2, without it — parsed before any I/O, same boundary discipline as `--envelope`). `--since`
  defaults from the verified cursor when omitted (unless no close has ever been recorded, then
  genesis stands) — an explicit `--since` is never overridden. Refuses the WRITE (exit 1; the
  read/render never refuses — reads are never gated) on a PRE-EXISTING latch, naming the stuck
  period/seq and the `attest` command, or the integrity failure with no repair claim. A gapped
  close still records itself — that recording is what latches the NEXT advance. Independent of the
  same-run auto-seal: a CONTAIN reached this run does not stop the close record from landing (one
  stops the pen, the other stops the page-turn). Plain `close`/`close --respond` (no `--advance`)
  render no cursor/gate vocabulary and write nothing to `close_records` — pinned structurally (a
  vocabulary sweep, since there is no pre-Task-2 binary to diff against byte-for-byte).
- **`pacioli attest --target NAME --reason <why>`** — the operator ceremony, wired to
  `BrokerStore.attest`. Exit 2 on `NoAttestationPendingError`, wording the two refusal shapes
  differently: a real pending gap says so plainly; an integrity-caused latch names the failure and
  explicitly does not claim attesting can repair it.
- **`pacioli close-status [--target NAME] [--json]`** — read-only, mirrors `seal-status`'s shape
  and exit contract: cursor rendered in words (genesis / open-ended / integrity-failure, never a
  bare `None`), gate state, history tail. Exit 2 LATCHED (any cause), 0 OPEN. Never gated — renders
  keyless, gapped, or integrity-failed states alike, the same way `seal-status` renders a
  fail-closed seal rather than crashing on it.
- **Composition-not-coupling, unchanged.** The close-record lives only in the broker's own store —
  no `pacioli_guard` write/import, no credential mutation, no new ERPNext read. Reconciliation stays
  optional and bench-dependent exactly as before this release.
- **Docs brought current (Task 4).** `broker/README.md` gains "The close-record + attestation gate"
  section with the scope stated explicitly — the gate stops exactly one thing, writing the next
  close record, never dispatch, `mint`, reads, or governed writes (that is the seal's job) — plus a
  short advance → gapped → latched → attest → advance example.
  `docs/plans/2026-07-11-close-half3-response-to-gap.md`'s Fork A ruling is marked BUILT; the Half 1
  deferral this repays (`docs/plans/2026-07-09-the-close.md`) carries a dated note pointing here.
- **Three-lens adversarial redteam (model-fidelity · deny-bias · honesty), all findings fixed
  test-first before merge.** The catches, so the record confesses them: a future-dated `--until`
  used to poison the cursor permanently (every later default-`--since` advance silently saw zero
  acts — a real orphan invisible forever; **an advance now refuses to close a period that has not
  happened yet**, measured against the store's own clock, and a legacy-poisoned cursor refuses
  loudly instead of reporting empty); a reversed window (`--since` after `--until`) was accepted and
  recorded — now a usage error, and a cursor already past the requested `--until` refuses with the
  reason; two consecutive open-ended advances used to both close `genesis..now` (full overlap,
  double-count) — the effective `--until` is now **materialized from the store clock at close time**,
  so the recorded window is exactly the examined window and cursors are always concrete
  (`record_close` refuses `period_until=None`; historical open-ended rows are tolerated, read-only);
  `attest` gained its own compare-and-append (`expected_seq`, `AttestStaleError`, optional `--seq`) —
  an operator can no longer clear a *different* gap than the one they reviewed; the stale-cursor
  prose no longer swears "nothing is broken" (the check cannot distinguish a concurrent advance from
  a deleted tail row — an **honest-tamper-ceiling** note in `close_gate_state` now states plainly
  what the derivation does NOT detect, and the ceiling is pinned behaviorally by test); "gate: OPEN"
  renders are now derived from the freshly-returned post-write/post-attest state, never assumed; and
  an `--advance` under a pre-existing SEAL now confesses it ("advancing the cursor does not clear
  containment") instead of turning the page silently.
- **Tests:** `test_store_close_record.py` (Task 1 — genesis-empty, seq-gap, bad-HMAC,
  latch/attest/latch-again lifecycle, keyless-open, compare-and-append staleness),
  `test_close_advance.py` (Task 2 — usage errors, `--since` cursor defaulting, both LATCHED causes,
  the happy-path write, JSON shape, byte-identical-without-the-flag — upgraded post-redteam to two
  LITERAL golden outputs), `test_attest_cli.py` (Task 3 — attest happy path/refusals, close-status
  in every state, a full advance→gapped→latch→attest→advance lifecycle pin), plus the redteam wave's
  35 (`TestHonestTamperCeiling`, future-until, reversed-window, materialization, attest staleness).
  TDD throughout (red → green). Broker 1354 → 1494, +140, zero regressions.

## 0.22.0 — 2026-07-15 — `chain_broken`: the first default-CONTAIN finding class

MINOR (a new response finding class — `build_response` now emits `chain_broken` at floor `contain`
when Pacioli's own local receipt ledger fails to verify; `pacioli_guard` byte-untouched, no new
grant, no new ERPNext read). Design: `docs/plans/2026-07-15-chain-broken-finding.md`. Built same
day, then narrowed by a same-day redteam fix wave (D1, C1-C4 below) before merge — this entry
describes the FINAL shipped shape, not the intermediate build.

**The honest boundary — read this before anything else in this entry:** this release makes
`seal_required` REACHABLE on a compromised local chain. It does **not** change how the seal is
performed — that is still `cmd_close`'s pre-existing 0.20.0 auto-seal path (`store.seal(...,
source="response")`), unmodified. Before this release, a broken chain produced no `response.py`
finding at all, so a plain `close --respond` never reached `contain` on it (plain `close`/
`close --respond` without an envelope still exited 1 via the separate, pre-existing `balanced`
check — nothing silently passed, but the auto-seal path was unreachable by this cause). After this
release, `close --respond` on a broken chain reaches `contain` and auto-seals **without needing any
`--envelope` escalation** — this is the one and only class where reaching `contain` is not the
operator's explicit opt-in.

- **`response.py`: `_FLOOR["chain_broken"] = 3` (`contain`).** Fires when `statement["chain"]` is
  present and `verified is False` (the keyed PROVE-chain verify actively failed — tampered/corrupt
  receipts) — **and that signal alone.** **The false-seal guard (the highest-severity property in
  this slice):** `verified is None` (verify not run — a synthetic/non-`cmd_close` statement) emits
  NOTHING; the check is an identity check (`is False`), never falsiness, so `None` can never be
  mistaken for a failure. An ordinary, verify-clean close is byte-identical to before this
  release — it still emits no `chain_broken` finding and does not seal. The envelope can escalate
  `chain_broken` (already at its ceiling, so a no-op) but per the pre-existing floor-not-override
  law can never silence it below `contain` — a broken chain cannot be configured away.
- **D1 (John ruled 2026-07-15, redteam fix wave): the anchor branch was built, then DROPPED.** The
  as-built version of this release also fired `chain_broken` on `anchor_matches is False` (an
  off-box anchor supplied and the live head disagreeing with it). The model-fidelity redteam found
  this load-bearing-broken and John ruled to drop it same day: `close.py`'s `anchor_matches` is a
  NAIVE `head == anchor` equality that false-seals a legitimately-grown chain fed a stale
  `--expected-head` — a default-CONTAIN auto-seal firing on a perfectly untampered ledger. The real,
  count-aware rollback detection already lives in `pacioli anchor check` / `seal-status --anchor`
  (the 0.21.0 seal-anchor tooling), which correctly reads a grown chain as clean — that tooling is
  unaffected and remains the receipt-rollback answer. `anchor_matches` keeps feeding
  `build_statement`'s own `balanced` check and the human render (`close.py` untouched) — it simply
  no longer drives this default-CONTAIN floor. New pin: `verified=True, anchor_matches=False`
  (a grown chain against a stale pin) now emits NOTHING and does NOT reach `contain` —
  `test_response.py::TestChainBroken::test_anchor_mismatch_alone_does_NOT_emit_chain_broken`.
- **Invariant 1, REVISED, then restated to the real axis (D2, same-day fix wave).** Through 0.21.0:
  "CONTAIN is never a default... a property of the data." The as-built 0.22.0 revision framed the
  exception as "own ledger vs. another party's movement" — WRONG axis: `orphan`/`unconfirmed` are
  ALSO Pacioli's own writes and correctly stay at `alert`, so "own" cannot be what earns CONTAIN.
  **The real, restated axis: CONTAIN is never a default for a working ledger honestly recording a
  known uncertainty.** `chain_broken` is CONTAIN because the attestation apparatus is **provably
  broken** (the HMAC chain cannot verify itself — no record in it can be trusted); every other
  class, including Pacioli's own `orphan`/`unconfirmed`/`second_generation`, stays at `alert` or
  below because the ledger **verifiably records a known uncertainty** — the apparatus works and
  honestly confesses an open item. No other class may ever gain a `contain` floor.
- **C1 (Medium, redteam fix wave): the seal reason no longer claims "escalated".** `cli.py`'s
  `_seal_reason` wrote `"close --respond escalated {names} to CONTAIN ..."` into the PERMANENT
  `seal_events` record — a lie for `chain_broken`, which reaches `contain` by its unconditional
  default floor, never an operator escalation (it is not a valid `--envelope` CLASS at all). Reworded
  to neutral, truthful phrasing: `"close --respond reached CONTAIN via {names} for the period
  {period}."` TDD: a `chain_broken`-only seal (no `--envelope`) → the seal_events reason does NOT
  contain "escalated" and truthfully names `chain_broken`.
- **C2 (Low): a non-dict `chain` is now flagged, deny-biased (mirrors the sibling `acts` check).**
  Still emits nothing (safe), but is no longer silent — appends "statement 'chain' is not a dict —
  chain integrity not evaluated (deny-biased)" to `flags`.
- **C3 (Low): `verify_reason` falsiness.** The "omit the parenthetical" guard checked
  `verify_reason is None`; an empty string `""` still produced "failed to verify ()". Guarded on
  falsiness (`if verify_reason:`) instead, so `""` also omits it.
- **C4 (coverage): a real end-to-end pin of the marquee path.** A new test tampers a REAL receipt
  row on disk (`UPDATE receipts SET body = ...`), runs `cmd_close(respond=True)` with NO
  `--envelope`, and asserts it reaches `contain`, exits non-zero, and produces a REAL sealed store
  (reopened, `seal_state` sealed, `source="response"`) whose reason names `chain_broken` and does
  NOT say "escalated" — this pins the shipped capability end-to-end, not just synthetic
  `build_response` dicts.
- **Render polish (two Minors from the Task 1 review, TDD):** `_CLASS_WORD`/`_finding_line` now
  render `chain_broken` as a descriptive phrase ("chain broken (the receipt chain failed to verify —
  the attestation apparatus itself is broken, not a verdict on a party)") instead of falling back to
  the bare, unhelpful `chain_broken: chain_broken`. And the emitted detail no longer reads "the
  receipt chain failed to verify (None)" (or, after C3, "()" for an empty-string reason) when there
  is no reason string to show — the parenthetical is omitted entirely.
- **Closed the stale disclosed-gap docstring.** `build_response`'s docstring, and the two README
  paragraphs it mirrored, described this exact gap as still-open ("a `chain_broken` finding class
  is a staged next increment") — now rewritten to describe the shipped behavior. `README.md`'s
  seal/CONTAIN section names `chain_broken`, its default-contain rationale, and the false-seal
  guard directly.
- **Reconciled every "never a default"/"CONTAIN is never"/"staged next increment"/"anchor_matches"/
  "escalated" reference found by grep across `broker/` and `docs/`** against the final, narrowed
  shape: current, load-bearing prose (`response.py`, `README.md`, `cli.py`'s `--envelope` help text
  and `cmd_close` docstring) is rewritten to the honest, current claim; historical CHANGELOG/plan
  entries that were true when written are left standing with a dated "closed/shipped" annotation
  appended, not rewritten — keep-the-record discipline, same pattern already used for the 0.21.0
  anchor entry above.
- **Tests:** `test_response.py`'s `TestChainBroken` (base build, 9 tests) plus Task 2 render tests,
  narrowed/added by the redteam fix wave: the anchor-mismatch-fires test replaced by
  `test_anchor_mismatch_alone_does_NOT_emit_chain_broken` (D1's pin), a same-`chain_broken`-with-
  both-signals test now asserts the detail names only the verify failure, a `TestChainNonDictFlagged`
  class (C2), an empty-string `verify_reason` test (C3), and `TestCmdCloseChainBrokenAutoSeal` in
  `test_close_envelope.py` — a real-store, no-envelope, tamper-to-seal end-to-end pin (C4). See
  `.superpowers/sdd/redteam-fix-report-chainbroken.md` for the full before/after evidence. TDD
  throughout (red → green). Net count reported at merge — the anchor-branch tests removed/replaced
  are outweighed by the new pins added.

## 0.21.0 — 2026-07-15 — the seal anchor: off-box tail-rollback DETECTION

MINOR (a new operator capability — the seal history can now be pinned off-box, closing the
seal-side blind spot 0.20.0 disclosed and deferred; existing behavior byte-identical with no pin
supplied; `pacioli_guard` byte-untouched, no new grant, no new ERPNext read; broker 1263 → 1322,
+59). Closes the one CONFIRMED HIGH the 0.20.0 seal redteam found and staged as its own next
increment (`docs/plans/2026-07-14-close-half3-seal-slice.md`'s "Off-box seal-head anchor" bullet):
a keyless attacker with DB-file write access can roll back the seal by deleting the newest
`seal_events` row(s) — seq-contiguity is blind to tail truncation, so a genuine, untouched earlier
row becomes "latest" and the store reads UNSEALED. Scope A only (John ruled 2026-07-15): **audit-time
DETECTION against an off-box pin, exactly the parity the receipt chain's own `pacioli anchor`
already had — NOT real-time prevention.** Nothing *on-box* stops a principal who can write the DB
file — this pin does not change that, does not run on every `seal`/`unseal` call, and blocks no
write, ever. What it buys: a rollback stops being silent to an operator who pinned the seal head
off-box and checks it — the detection window is that operator's own check cadence, not continuous.
Design: `docs/plans/2026-07-15-seal-anchor-slice.md`.

- **`BrokerStore.seal_head()` / `seal_count()`** — the latest `seal_events` row's `hmac` (`None`
  on an empty table) and the row count, mirroring `head()` on the receipt side exactly: plain
  accessors, no key needed, available on the same least-exposure keyless path `seal_state()`/
  `seal_events()` already are.
- **`seal_state(*, expected_seal_head=None, expected_seal_count=None)`** — both keyword-only,
  default `None` on both (a regression pin proves this is byte-identical to the pre-anchor
  derivation when neither is supplied). When both are supplied: live count < the pinned count →
  `sealed=True, cause="seal history behind the off-box anchor (tail truncated?)"` — the headline
  catch, the exact gap the redteam found. Live count at-or-past the pinned count but the row
  actually AT the pinned position (found by `seq`, not list index) disagrees →
  `cause="seal history diverges from the off-box anchor"` — the belt beyond the latest-row-only
  HMAC check: rewriting history while preserving the exact pinned hmac needs an HMAC-SHA256
  collision, not just key possession. History that only grew since the pin, with the pinned
  position still agreeing, reports the current state normally — new legitimate events since a
  pin are never held against the caller. Every comparison is `hmac.compare_digest`; the method
  never raises on a malformed pin pair (folded into the fail-closed answer, the same as every
  other branch), only a genuine `sqlite3` error still propagates.
- **Anchor pin format v2** (`pacioli.anchor`, `FORMAT_VERSION` 1→2) — adds `seal_head`/
  `seal_count` alongside the existing receipt fields, both required together (never one without
  the other), same shape rules as the receipt pair (64 lowercase hex, non-negative non-bool int,
  `(count == 0) == (head == GENESIS)`). A v1 pin (no seal fields) still parses without error — it
  is simply a pin that predates seal anchoring, never coerced into v2 or refused.
- **`pacioli anchor write`** now also reads `seal_head()`/`seal_count()` and emits them, and
  additionally refuses to pin a seal history that is not itself fully verifiable (a fail-closed
  `seal_state()` — zero rows, a gap, an unverifiable latest row) — witnessing an already-broken
  seal history as a trustworthy off-box pin would be recording a lie, not a fact. A genuinely,
  verifiably SEALED broker (a real operator seal, `cause is None`) is not "broken" in this sense
  and is pinned normally.
- **`pacioli anchor check`** verifies the receipt chain exactly as before, and — when the pin
  carries seal fields — additionally runs `seal_state(expected_seal_head=..., expected_seal_
  count=...)` and FAILS (exit 1) on ANY fail-closed cause it returns, naming it. A v1 pin (no
  seal fields) is not an error: the receipts still verify, and a WARNING is printed to stderr that
  the seal is NOT covered by it — never silently treated as "the seal is fine" just because
  nothing contradicted it.
- **`pacioli seal-status --anchor <pinfile>`** (new flag, optional) — reads a recorded pin (a
  path, or `-` for stdin), threads its `seal_head`/`seal_count` into `seal_state(...)` the same
  way `anchor check` does, and renders the rollback-detected state (SEALED, cause naming the
  anchor mismatch) with exit 2 — so an operator gets the finding from the everyday status command
  too, not only from a dedicated `anchor check` run. Without `--anchor`: 0.20.0-identical. A v1
  pin passed to `--anchor` WARNS (stderr) that it doesn't cover the seal and renders the on-box
  state, same as no `--anchor` at all. Deny-biased: an anchor recorded for a different target, or
  one that is unreadable or malformed (bad JSON, a shape violation, a partial seal-field pair) is
  refused loudly (exit 1, nothing rendered) — never silently rendered as if unanchored, which
  would let a real rollback hide behind a broken pin path.
- **Corrected in-branch (Task 2 review, Critical — caught by review before this branch ever
  reached `main`, fixed same-day):** the first cut of `cmd_anchor_check`'s v2 branch only flagged
  `anchor check` as FAILED when `seal_state()`'s `cause` string happened to contain the substring
  `"off-box"` — the two pin-mismatch causes above. Every OTHER fail-closed cause the same call can
  return (a `seq` gap from an interior-row deletion, an unverifiable latest row from an in-place
  edit) does **not** contain that substring, so a seal history broken by a DIFFERENT attack — one
  whose pinned POSITION was never disturbed, only the middle or the very tail of the surviving
  history — would sail past `anchor check` as `ok`, a fail-OPEN in code whose entire purpose is
  failing closed. Fixed same-review by testing `seal_cause is not None` (the method's own
  documented contract: `cause` is `None` iff the history is genuinely verified/contiguous with an
  agreeing pin, and set on every other branch) instead of pattern-matching the cause text. Two
  redteam-style regression tests pin both attack shapes (`test_check_fails_on_interior_gap_with_
  agreeing_pin_position`, `test_check_fails_on_keyless_tail_injection_with_agreeing_pin_position`,
  `test_anchor.py`). Recorded here as honest history, not smoothed over.
- **Honest ceiling, unchanged by this release:** anyone holding this store's HMAC key can still
  forge a fully self-consistent seal-event history after the pinned position — key possession is
  authorship, on-box, until the key itself is anchored off-box; a pin only vouches for history up
  to and including its own count. The window between pins is unprotected. Keeping the pin
  off-box (another machine, a git remote, paper) is operator discipline, not code — the tool
  cannot do that part for you. The `chain_broken` finding-class gap (`response.py` never
  inspecting `chain.verified`/`anchor_matches`, so a broken PROVE chain can't trigger auto-seal)
  is still the next increment, not touched here (John ruled 2026-07-14).
  **Closed in 0.22.0 (see above):** `chain_broken` now fires at floor `contain`, the first
  deny-biased default-CONTAIN in the model (own-ledger-integrity, not a party verdict) — the state
  described in this bullet was true when written, before that shipped.
- **Corrected in-branch (2-lens redteam wave — security + correctness, same-day, before this
  branch ever reached `main`):** four fail-safe hardenings/reliability/honesty fixes, no model
  change. Full evidence: `.superpowers/sdd/redteam-fix-report-anchor.md`.
  - **F1(a), security (Medium):** `seal_state()`'s keyed derivation verified only the LATEST
    row's HMAC — a keyless attacker with DB-file write access could rewrite an INTERIOR row's
    content (flip a past `seal`→`unseal`, launder a reason) and, once that row was no longer
    latest, both `anchor check` and `seal-status --anchor` read clean. Fixed: every surviving
    row's HMAC is now recomputed and checked, not only the latest's — a keyless edit of ANY row
    now fails closed (`cause="unverifiable"`). Does not defend against a KEY-holder's
    self-consistent rewrite of a row other than the exact pinned position — see F1(b).
  - **F1(b), security honesty (paired with F1(a)):** `seal_state`'s docstring, the module
    docstring, and this README's "Honest ceiling" overclaimed the pin's coverage — "catches a
    rewrite at-or-before the pinned position" and an "identical [ceiling] to receipts" — both
    false: `seal_events` has no prefix-chaining (unlike the receipt chain's `prev_hash`), so the
    pin fixes only the exact row it names by position (plus the count), never the whole pre-pin
    history; a key holder can still forge a self-consistent rewrite of any OTHER row, including
    one earlier than the pin. Corrected to the honest claim throughout store.py and this README;
    `anchor.py` was audited and already disclaimed prefix-chaining correctly.
  - **F2, security (Medium):** "re-pin after every seal/unseal" is the linchpin of the off-box
    seal anchor but lived only in an abstract limits paragraph — an operator who pinned once at
    install and never again got ZERO seal-rollback protection with no warning. `pacioli seal` and
    `pacioli unseal` now print a reminder after every append, `pacioli anchor write`'s trailing
    guidance names the same discipline, and the README states it explicitly.
  - **F3, correctness (Medium-High):** `cmd_anchor_write` built the seal half of a v2 pin from
    THREE unsynced reads (`seal_state()` / `seal_head()` / `seal_count()`) — a concurrent
    seal/unseal (another CLI invocation, or an auto-CONTAIN `close --respond`) landing between
    them could pair a stale derivation with a fresh head/count, later false-alarming "diverges
    from the off-box anchor" against untouched history (fails SAFE — deny, never silent-clear —
    but still a false alarm). Fixed: new `BrokerStore.seal_state_snapshot()` (one `SELECT`,
    mirrors `verify_snapshot()`) — `cmd_anchor_write` now builds the pin from one consistent
    triple; the code comment that falsely claimed single-snapshot behavior is corrected too.
  - **F4, correctness (Low):** `anchor.py`'s version check accepted JSON `true` as a format
    version (`True == 1`, `True in (1, 2)`) — the module's own docstring already called out
    bool-exclusion for the head/count pairs but missed the version marker. Fixed: a bool version
    is now refused explicitly (`isinstance(version, bool)` guard, same shape as the existing
    count guards).
  - All four are fail-safe (deny-biased on failure, never silently pass) and none defeats the
    headline tail-rollback detection this release ships; recorded here as honest history, same
    discipline as the Task 2 review correction above.

## 0.20.0 — 2026-07-14 — the seal ships: CONTAIN's teeth (Fork B1)

MINOR (a new operator capability — sealing/unsealing the broker, plus the CLI and `close
--respond` surface that reach it; existing behavior byte-identical when unsealed; `pacioli_guard`
byte-untouched, no new grant, no new ERPNext read; broker 1149 → 1257, +108). `seal_required` was
a decision `response.py` (0.18.0) computed and nothing acted on. This release gives it teeth:
after this build a sealed broker refuses every governed write, an operator can seal/unseal from
the CLI, and `close --respond --envelope` can escalate a gap-class to CONTAIN and actually seal
the store. Fork B1 ruled 2026-07-14 (broker-local seal; guard revocation stays a human runbook
step — see README's "The seal (CONTAIN)" section); Fork A (the close-record / attestation-gate)
is ruled A1 and is the next slice, not this one.

- **New store table `seal_events`** (append-only, HMAC'd) — `pacioli.store.BrokerStore` gains
  `seal(reason, source="operator")`, `unseal(reason)`, `seal_state()`, and `seal_events()`. Each
  event's HMAC is computed with the SAME store key that already seals PROVE receipts, over the
  canonical tuple `(seq, ts, action, reason, source)`, under a distinct domain-separation prefix
  (`b"seal:"` vs the receipt chain's own prefix) — a receipt HMAC can never be replayed as a
  seal-event HMAC, or vice versa, even holding the key. **Genesis migration:** on the first KEYED
  open of any store (fresh or pre-0.20.0), a `genesis` event is seeded once — double-checked
  locking (a lock-free `COUNT(*)` read first, the write lock only taken when it might actually
  need to seed, re-checked once inside the lock) so this runs on every dispatch's store-open
  without holding the write lock hostage in steady state. Keyless opens never seed (no key to
  HMAC with) — a store that has only ever been opened keyless reads as SEALED until its first
  keyed open, which is the correct fail-closed answer, not a special case.
- **Fail-closed by construction.** `seal_state()` never raises on malformed row content: zero rows
  → sealed ("no seal history"); a gap in the `seq` sequence → sealed ("seal history gap
  (rollback?)" — `seq` is `AUTOINCREMENT`, never reused even across deletes, so a missing number
  can only mean a row deleted from the MIDDLE of the history, which is exactly the INTERIOR
  tamper this catches — see the corrected note below for the tail-truncation gap it does NOT
  catch); a keyed open whose latest event's HMAC does not verify → sealed ("unverifiable"). A
  keyless open cannot check HMACs at all and trusts row content, still enforcing the
  zero-rows/gap checks — documented as defense-in-depth, not an authoritative verdict, in
  `seal_state`'s own docstring.
- **The dispatch gate (`tools.py`) — every non-read tool is seal-gated.** A new exported
  `READ_ONLY_TOOLS` frozenset names exactly the read surface (every `get_*`/`list_*`,
  `workflow_status`, `prove_verify`, `prove_orphans`); every OTHER dispatched tool — every
  `plan_*`, every `submit_*`/`cancel_*`/`amend_*`, `cascade_cancel`, `reconcile`,
  `request_workflow_transition` — is a governed surface checked against `seal_state()` in
  `PacioliBroker.dispatch` BEFORE its handler is even looked up: sealed → refused at
  `stage="seal"`, the handler never runs, nothing is claimed, no marker is spent (the F-C2
  invariant — consent is spent by commitment, never by refusal — extended to the seal: a marker
  survives a sealed refusal and still commits once unsealed). New tools are **born gated** — the
  classification is an allowlist of reads, not of writes, so an unclassified new tool is
  seal-gated with zero further code change; a test walks every `_tool_*` handler and asserts each
  is either read-only or covered, so an unclassified tool fails loudly.
- **Gate-entry semantics, stated as a contract, not left implicit:** target/store *resolution*
  failures (an unknown/ambiguous `pacioli_target`, a torn/corrupt store file) keep their
  PRE-EXISTING stages (`"request"`/`"store"`) — resolution precedes seal knowledge, so an unknown
  target is never reported as "the seal did this." Only once the store resolves cleanly does a
  genuine failure reading `seal_state()` itself deny at `stage="seal"`, deny-biased the same as a
  real seal. A write already past the gate completes; the gate and the write each open the store
  independently, so a seal landing in that window is not re-checked at write time — this is a
  recorded, intentional gate-entry semantics (a future tightening would re-check at execute time),
  not an oversight.
- **`pacioli seal --reason <text> [--target NAME]`** — operator CONTAIN: appends a `seal` event
  (`source="operator"`), prints the resulting state, exit 0. **`pacioli unseal --reason <text>
  [--target NAME]`** — appends an `unseal` event, exit 0 when the resulting state reads unsealed,
  exit 1 (with the state still printed) if a pre-existing fail-closed cause survives the append
  (an unseal cannot heal a rollback gap in the history). **`pacioli seal-status [--target NAME]
  [--json]`** — read-only, renders `seal_state()` plus a tail of the event history with each
  event's verified flag; **exit 2 when sealed, 0 when unsealed** (deliberately distinct from the
  generic error exit 1, so a script can tell "sealed" from "could not even check"). All three
  require `--reason` (seal/unseal) at the argparse boundary — a confession without a reason is not
  an entry.
- **`close --respond --envelope CLASS=LEVEL`** (repeatable) escalates a finding class above its
  deny-biased floor (`pacioli.response`'s floor-not-override law, unchanged — CONTAIN is never a
  default, only ever reached because the operator wrote `...=contain`). Parsed deny-biased at the
  CLI boundary, before any I/O: an unknown class or level, `--envelope` given without `--respond`,
  or the SAME class given twice (even in agreement) is a usage error, exit 2 — close does not run;
  an operator who asked for an escalation must never silently get a weaker or ambiguous one. When
  the resulting response's `seal_required` is True, `cmd_close` seals the already-open keyed store
  itself (`source="response"`) — this is CONTAIN's teeth. Success renders a `seal` block in both
  the human render and `--json`, and names how to clear it (`pacioli unseal --reason <why>`).
  **Fail-closed on the seal write itself:** if the append raises, the failure is printed loudly to
  stderr (manual `pacioli seal` required) and is never folded into a success shape — CONTAIN was
  decided but the broker is not confirmed sealed. A plain `close` or `close --respond` with every
  finding at or below its floor makes ZERO writes to `seal_events` — the close path stays
  read-only except for this one explicit, operator-escalated case.
- **Mint's keyless pre-check (`cmd_mint`) is defense-in-depth, not authoritative.** Before minting,
  `cmd_mint` opens its store keyless (as it always has) and checks `seal_state()` on it; sealed →
  the same refusal text as the dispatch gate, no marker minted. This check cannot verify HMACs (no
  key on a keyless open) — the keyed dispatch gate at execute time is the authoritative check;
  this one exists so a sealed broker refuses as early and as legibly as possible, not as the sole
  guarantee.
- **Honest residuals, disclosed, not new:** anyone holding this store's HMAC key (the same key
  that already seals every PROVE receipt) can forge a fully self-consistent seal-event history —
  the same ceiling the receipt chain has always had (SPEC §5); key possession is authorship,
  on-box, until the key is anchored off-box. Off-box anchoring of the seal history (the way
  `pacioli anchor` already anchors the receipt chain head) is a noted refinement, not shipped
  here. The gate→handler TOCTOU window above is a stated contract, not a gap discovered later.
  The mint keyless pre-check's HMAC-unverified status is disclosed inline, not implied to be
  stronger than it is. **Corrected 2026-07-14 (security redteam, same slice):** `seal_state()`'s
  seq-contiguity check is a FORWARD fail-closed control, not a rollback-resistant one — it is
  structurally blind to a KEYLESS attacker deleting the NEWEST `seal_events` row(s) (tail
  truncation): survivors stay contiguous, the surviving `genesis`/`unseal` row's HMAC still
  verifies (it was never touched), and the store reads UNSEALED. This is the SAME on-box limit
  the receipt chain has always disclosed above, closed there by the off-box `pacioli anchor`
  pin — this table had no such anchor yet at this release (staged as the next increment, not this
  slice; see `docs/plans/2026-07-14-close-half3-seal-slice.md`'s staged-next-increments note).
  **Closed in 0.21.0 (see above):** the seal head now shares the receipt chain's off-box anchor
  discipline — `pacioli anchor write`/`check` and `seal-status --anchor` pin/verify a
  `(seal_head, seal_count)` pair alongside the receipt head, detectable against the off-box
  anchor since 0.21.0 (audit-time; the on-box limit described in this entry is unchanged — a
  key-holder can still forge post-pin history). Earlier drafts of this entry and `store.py`'s own
  docstrings overstated this as "no off-box anchor needed for this table" — corrected here and in
  `store.py`, not left standing.

## 0.19.0 — 2026-07-11 — response posture is configurable per target (`posture` registry field)

MINOR (a new opt-in registry field; behavior byte-identical when absent; broker 1144 → 1149, +5).
Completes `close --respond`'s reachability (0.18.0): the accounting-vs-police switch is now the
operator's to set per target the right way — a persistent registry property, not a flag to remember.

- **New optional `posture` on a registry target** — `"mixed_door"` (the default when absent —
  ungoverned movement is recorded, not alerted) or `"sole_door"` (this credential is the only thing
  allowed near these books → ungoverned movement is raised to an alert). `close --respond` reads it and
  threads it to `build_response`. Absent → `mixed_door` (unchanged from 0.18.0).
- **Deny-biased, single-validator.** A non-string value is refused at load (an unambiguous type error).
  A string typo is NOT refused at load — it passes through and `build_response` deny-biases it to
  `sole_door` with a visible flag, so a policy typo neither silently quiets the signal nor bricks every
  operation. An unreadable registry at respond-time is deny-biased to `sole_door`.
- **Still staged (arm-free next, then bench):** the escalating response envelope (opt-in CONTAIN) pairs
  with the broker-local fail-closed **seal** that makes `seal_required` bite — the next slice.
  `sole_door` only changes reconciliation-side findings, so its live effect shows under `--reconcile`
  (bench); statement-only, it is proven wired (the response names the posture).

## 0.18.0 — 2026-07-11 — the Close, Half 3 reachable: response-to-gap (`close --respond`)

MINOR (a new opt-in operator capability; plain `close` and `close --reconcile` are unchanged,
`pacioli_guard` is byte-untouched, no new grant or ERPNext read; broker 1113 → 1144, +31). Halves 1–2
DETECT (attest, then partition against the real GL); Half 3 is the response — it turns a confessed gap
into a reaction. **The model is load-bearing** (design + open forks A–E:
`docs/plans/2026-07-11-close-half3-response-to-gap.md`): accounting-not-police is preserved (the
response renders no verdict of its own — ungoverned movement is *recorded, not accused*, and raised to
an alert ONLY under an operator-declared `sole_door` posture), the envelope is a *floor not an override*
(an operator can escalate a finding but never silence one below its deny-biased floor; and because no
floor is CONTAIN, CONTAIN can never appear unless the operator opts in), and it is deny-biased throughout.

- **New pure core `pacioli.response`** — `build_response(statement, reconciliation, *, target, posture,
  envelope)` + `render_response`. A pure consumer of the two dicts Halves 1–2 already return (no store,
  clock, seal, or key). It extracts findings — `orphan` / `unconfirmed` (Statement),
  `ungoverned` / `second_generation` / `blind_read` / `adverse_posture` (Reconciliation) — applies the
  posture and the floor-not-override envelope, and emits the per-finding and aggregate reaction
  (`record` / `alert` / `attestation_gate` / `contain`, plus `gate_required` / `seal_required` for the
  caller to act on).
- **New `close --respond` flag** — applies the response to the Close's findings and renders the
  reaction. Over a Statement alone (arm-free) it responds to the statement-side findings at the default
  `mixed_door` posture; adding `--reconcile` also weighs the reconciliation-side findings. Exit is
  non-zero when the aggregate response rises above `record` — which includes a second-generation
  voucher the balanced/complete checks alone would miss. Available in `--json` (a `response` block
  alongside the statement) and human-legible.
- **`recorded-open` split to match `close.py`'s own model** — a `failed` act is a known non-event and is
  NOT a finding; only an `unconfirmed` one (no answer — may have posted, the suspense item the Statement
  already refuses to call balanced) is, at the `alert` floor. The two halves now agree on what a clean
  period is.
- **Staged, not yet built (arm-free next, then bench):** the broker-local fail-closed **seal** that
  makes `seal_required` bite (CONTAIN's teeth), the **attestation-gate** period cursor, and a per-target
  **registry posture field** (so `sole_door` and an escalating envelope are configured, not just
  defaulted). `close --respond` today responds at `record`/`alert`; the deny-biased `sole_door` and
  CONTAIN paths are proven in the core's tests but not yet reachable through the CLI.

## 0.17.0 — 2026-07-10 — the Reconciliation gains owner corroboration (`seat_user`)

MINOR (a new opt-in registry field; purely tightening; behavior byte-identical when unset; broker
1113, +4). Closes the `seat_owner=None` residual disclosed in 0.16.0 — the Close's Half 2 now
corroborates that a governed voucher's GL rows were actually *stamped by the seat*, not merely that
a voucher name matches.

- **New optional `seat_user` on a registry target** — the ERPNext username (`owner`) that target's
  credential authenticates as. When set, `close --reconcile` requires a governed voucher's GL rows
  to also carry that `owner`; a voucher whose rows were stamped by a **different** user (e.g. an
  admin's ledger *repost* that rewrote a governed voucher's rows) downgrades to **second-generation**
  — surfaced, never passed by the name+time match alone. This is the Fork-III owner corroboration
  ruled 2026-07-09 and deferred in the shipping slice (the seat's username is not carried in any
  Pacioli receipt, so the operator supplies it once in the registry).
- **Purely tightening / safe-direction.** `seat_user` can only move a voucher governed →
  second-generation, never the reverse — it can never make ungoverned movement read as governed.
  Absent/blank → `None` → corroboration off, name+time match only (unchanged from 0.16.0). A target
  without the field is unaffected.

## 0.16.0 — 2026-07-10 — the Close, Half 2: the Reconciliation (`close --reconcile`)

MINOR (a new read-only operator capability; the governed runtime and `pacioli_guard` are
byte-untouched, plain `close` is unchanged — offline, store-only; broker 1109, +52). Half 1 (the
Statement, 0.15.0) attests to what passed *through* Pacioli; Half 2 closes that gap the only way a
self-referential ledger honestly can — it joins Pacioli's governed acts against ERPNext's actual
General Ledger movement for the period and partitions every voucher. **Accounting, not police**
(the model John locked 2026-07-09): it presents a partitioned account and passes NO verdict on the
ungoverned bucket — a desk posting is normal, not an accusation.

- **`pacioli close --reconcile [--since --until]`** (a flag on the existing `close`, not a new
  verb — distinct from the F-R2 `reconcile` MCP *write* tool). Requires a company-pinned target and
  a bounded window. Renders the Statement, then the Reconciliation: every GL voucher sorted into
  **governed** (its rows line up with a governed act, time-corroborated against the act's server
  `modified` stamp), **ungoverned** (no governed act references it — "did not pass through Pacioli",
  stated never accused), or **second-generation** (a governed voucher carrying rows that don't line
  up — most often an ERPNext ledger *repost*, attributed to the naming Repost Accounting Ledger doc
  when readable). Exit 0 iff the Statement balances AND the reconciliation is complete; the mere
  presence of ungoverned movement never flips the code.
- **New pure core `reconciliation.py`** (`build_reconciliation`/`render_reconciliation`) — no I/O, no
  clock, no key, same discipline as `close.py`/`prove.py`. **New read methods** `sweep_gl_entries`
  (a `creation`-window GL sweep — the axis that catches a posting backdated into a closed period)
  and `get_reposts`, both page-pinned (`limit_page_length: "0"`) and deny-biased on a malformed body,
  exactly like `get_gl_entries`. **New doctor probes** `probe_gl_entry_read` (required-read, 403 =
  FAIL — closes the standing "GL Entry has no probe" gap) and `probe_repost_read` (the deliberate
  WARN-on-403 — an unreadable repost source degrades the reconciliation to a flag, it does not
  refuse it).
- **NEW GRANT: Repost Accounting Ledger read.** The repost attribution reads the Repost Accounting
  Ledger DocType, which may be System-Manager-gated on a bench. It is *not* required — `close
  --reconcile` still runs and completes without it, losing only the ability to name *which* repost
  caused a second generation (`probe_repost_read` WARNs, does not FAIL). GL Entry and Accounts
  Settings reads ride the existing `Accounts User`-based seat.
- **The reconciliation cannot close two ceilings, carried IN its `scope_note` so no render drops
  them:** (a) *governs-vs-detects* — it sees a voucher did not come through Pacioli but cannot see
  why, and renders no verdict; (b) *tamper* — an actor with ERPNext server-side code execution can
  forge the `creation`/`owner`/`modified` stamps this join reads and erase GL rows with no
  ERPNext-side trail. Half 2 checks ground truth; it does not defeat a code-execution adversary.
- **Disclosed residuals (all safe-direction — never a false "governed"):**
  - `seat_owner` is passed `None` this slice — the seat's ERPNext username is not carried in any
    Pacioli receipt, so owner-corroboration of the governed bucket is off (a future enhancement).
  - System-generated side documents (ERPNext's Exchange Gain-Or-Loss JE, auto-authored inside a
    submit/reconcile transaction) are NOT 2-hop-attributed to their parent act (the Fork-III
    `voucher_subtype` / JE-Account-reference join was not built); their GL rows surface in the
    *ungoverned* bucket — over-reported, never falsely governed.
  - The `creation`-vs-`modified` match is a ±120s tolerance window: a repost landing within that
    window of the governing act reads governed (the gate is time-bounded, not absolute).
  - A committed act whose outcome carries no server `modified` stamp (the cascade readback-confirmed
    path) governs its voucher *structurally* — the time gate cannot apply, so a second generation of
    those rows cannot be distinguished; a per-run `flags` entry says so.
  - `sweep_gl_entries`/`get_reposts` are knowledge-pinned to ERPNext's documented REST surface, NOT
    yet live-verified against a bench (the same SPEC §7 status as the rest of the client) — the
    live falsification is a future bench window (John's arm).
- **Redteamed 3 lenses (security / correctness / model-fidelity), all SHIP-WITH-FIXES**; no
  false-clean path found. The fix wave landed test-first: reconcile acts no longer govern GL rows (a
  reconcile writes the Payment Ledger, not GL — leaving it in would have read a desk-submitted
  invoice as "governed" because Pacioli settled it); posture normalized in the core against ERPNext's
  int `0`/`1` checkbox wire form (a mutable-ledger posture now always raises its tamper flag); a
  non-dict Accounts-Settings body refuses instead of crashing; a bare-date `--until` expands to
  end-of-day (it no longer drops the last day's movement); a `(None,None)`-identity row can never
  read governed.

## 0.15.4 — 2026-07-10 — markerless amend/workflow: a store-write failure confesses, never crashes or over-claims

PATCH (a flagged residual, closed at John's ask; deny-biased, no happy-path change; broker 971, +6
— incl. workflow intent-failure + double-fault coverage a redteam flagged as the missing amend/workflow test symmetry).

- **`_amend_document` / `_tool_request_workflow_transition` — the three direct store writes are
  wrapped.** These two flows record intent/outcome to the store DIRECTLY (markerless — no consent
  grant to settle), and those three calls per flow were unwrapped: a store-write exception crashed
  past `dispatch()` as a raw traceback (only partly covered by the new `dispatch` `StoreCorruptError`
  /`OperationalError` catches; a `sqlite3.DatabaseError` or other unexpected error still escaped).
  Now: (1) a pre-wire `record_intent` failure is a structured deny — nothing was sent to the bench.
  (2) A double-fault (the bench call fails AND recording that failure fails) is a structured deny,
  never a crash; the intent is left an orphan. (3) The **landed-but-unrecorded** case — the bench
  act (amend draft / workflow move) SUCCEEDED but its `"committed"` receipt cannot be written — is
  John's ruling (2026-07-10): a new `_record_committed_or_confess` helper retries once, then returns
  `ok:False, stage:store`, NAMES the landed doc, tells the caller NOT to retry (it succeeded — a
  repeat is refused by `find_amendments` / frappe's own state machine anyway), and leaves the intent
  an orphan for `prove.orphans` hand-reconciliation. The PROVE chain is the source of truth, so the
  broker never attests a clean success the receipts don't back — the same principle as the marker
  cores' `_settle`, adapted to the markerless (no-`final_marker`) shape.

## 0.15.3 — 2026-07-10 — JE balance check: refuse an unreadable or non-finite total, never sum as zero

PATCH (a flagged rigor gap, closed at John's ask + a same-class residual a redteam found in the same
function, both closed here; deny-biased, no happy-path change; broker 965, +4).

- **`_journal_entry_balance_check` — a malformed debit/credit refuses, never reads as balanced.**
  The independent "no debit without a credit" gate silently SKIPPED any debit/credit that wasn't a
  non-bool int/float (summing it as 0), so an unbalanced JE whose amounts the check couldn't read
  passed as "balanced"; worse, a `NaN` amount IS a float, was summed, and then defeated the
  `abs(total_debit - total_credit) > epsilon` comparison entirely (`nan > epsilon` is `False`) — the
  same NaN-defeats-comparison class WG-2a / `get_gl_entries` / `check_allocation` already close. Now:
  an absent/`None` side stays the legitimate 0 (an ERPNext row carries only one side), but a PRESENT
  value that is not a finite non-bool number is a structured `_deny(stage="plan")` — the balance is
  refused rather than blessed from an amount that could not be independently verified. Enforced at
  both `plan_submit` and the governed `submit_journal_entry` write, as before.
- **…and the accumulated-total companion (redteam).** Every per-row value can be finite yet the
  SUM overflow float64 to `inf` (many large amounts on both sides), and a non-finite TOTAL defeats
  the same `abs()>epsilon` check (`abs(inf - inf)` is `NaN`). A post-loop `math.isfinite` guard on
  both totals now refuses that too — low-reachability but the same class, closed so the claim holds.

## 0.15.2 — 2026-07-10 — readiness follow-up: post-claim ledger robustness + torn-store floor + GL read validation

PATCH (safety hardening from the same readiness discipline — fanned-out builder agents + head-chef
integration + a rule-of-three adversarial floor redteam whose findings are folded in below). No
happy-path behavior change: every code change is a strictly-stricter refusal, a fail-closed on a
previously-uncaught crash, or a reads-as-empty close. Unit-proven (broker 961, +61). Not yet
bench-re-proven — the new refusals are structural (a torn/sub-header store file, a malformed GL
row, a store-write exception mid-outcome); the live legs are marked where they matter.

- **POST-CLAIM LEDGER ROBUSTNESS (WG-2b — general case, all three cores).** `governed_submit`
  (spine), the cascade loop, and `run_reconcile` recorded their intent/outcome with a NARROW
  `except` around only the wire call — an unexpected exception in `record_intent`/`record_outcome`
  (e.g. `prove.append`'s JSON-native/non-finite guard rejecting a value that slipped every upstream
  check) crashed PAST `dispatch()`'s structured deny as a raw traceback, stranding the marker with
  an UNRECORDED outcome. Now `record_intent` is wrapped (pre-wire → structured deny, marker left
  claimed-not-spendable, nothing sent to the bench) and a `_settle` helper (identical in all three
  cores) wraps `record_outcome`: on a store-write failure it retries once with a sanitized body,
  **preserving `failed` (pre-wire truth) but forcing every post-wire status to `unconfirmed`** (per
  the b9cd3ed close-model ruling: `unconfirmed` blocks a clean close, `failed` does not — a
  possibly-landed act must never be recorded as clean-refused). Callers downgrade their own
  `ok:True`/`"done"` when the ledger degraded, so a result never claims a success the receipts
  don't back. `reconcile.py` was the ORIGINAL locus of the concrete WG-2a NaN crash — `check_allocation`
  closed that one trigger, this closes the general class in all three paths. The retry's atomicity
  (poisoned first attempt rolls back, exactly one receipt lands, marker settles once) is now proven
  against a real `:memory:` store, not only mocks. (spine/cascade +14, reconcile +5, real-store +1.)
- **STORE — a torn or sub-header ledger refuses, never reads-as-empty (redteam, critical).** The
  first guard checked `size == 0` exactly; a file truncated to **1 byte** escaped both that AND
  SQLite's own corruption detection (2+ bytes → `DatabaseError`, but 1 byte opens SILENTLY as an
  empty db), and the reopen's schema script then destroyed the ledger with a clean `verify()` — the
  exact reads-as-empty class (TH-1/TH-2) the guard exists to close. `refuse_if_torn` now refuses any
  existing file below the 100-byte SQLite header (the mechanism, not the exact byte), and its error
  no longer suggests `rm` (a data-loss footgun on the narrow first-open TOCTOU, now documented as a
  known residual). A real mid-`record_outcome` crash (SIGKILL before COMMIT) was already safe via
  the rollback journal — pinned with a real-crash test and `synchronous=FULL` made explicit. (+10.)
- **STORE — a damaged ledger confesses structurally, at open AND at read (redteam + verify pass).**
  Two triggers, one structured handling. (1) *At open:* `cli.py` catches `StoreCorruptError` at all
  six `open_store` sites (clean CLI error, exit 2) and `dispatch()` catches it too, so a torn file
  on the agent-facing server path lands as the house deny (`ok:False, stage:store`), not the MCP
  SDK's generic error channel. (2) *At read:* a store tail-truncated so SQLite still opens but an
  individual receipt body no longer parses used to crash `prove_verify` with a raw
  `json.JSONDecodeError` (the same torn-store class, found by the verify pass). `_row_to_receipt`
  now raises `StoreCorruptError` on an unparseable body — flowing through the same dispatch/CLI
  catches — and `verify()`/`verify_snapshot()` report it as `(False, reason)` (the integrity checker
  never crashes on the corruption it exists to detect); `cli.py verify`/`orphans` degrade cleanly.
  (+1 cli-open, +1 dispatch, +5 read-time.)
- **RECONCILE — a total outcome-record failure is reported, not silently clean (verify pass).** When
  a `failed` (answered-refusal) reconcile's outcome write failed on BOTH the original and the
  sanitized retry, `ok` stayed correctly `False` and the marker's release rolled back with the write
  (leaving it reserved/dead), but the `reason` read like a normal clean refusal. It now flags that
  no outcome was durably recorded and the marker is uncertain/unspendable — mirroring spine's
  not-recorded path. (+1.)
- **GL ENTRIES — malformed rows refuse, never coerce to zero/blank.** `get_gl_entries` (the
  projected-reversal disclosure a human consents to) validates the load-bearing fields: non-list
  body / non-dict row → deny; `account` a non-blank string; `debit`/`credit` finite non-bool numbers
  (`math.isfinite`). The six nullable disclosure fields stay tolerated. Same house pattern as
  `get_settling_references`/`get_period_locks`. (+22.)
- **Honest-scope (doc-only).** `list_documents` returns a page with no has-more signal; amend
  copies site-added custom `Password`-fieldtype fields — both were documented only in source, now
  in `README#honest-scope`.

## 0.15.1 — 2026-07-10 — readiness audit: reconcile safety belts + reads-as-empty fixes

PATCH (safety hardening + honesty fixes surfaced by an adversarial readiness audit of our own work;
found by fanned-out agents + head-chef review, all arm-free). No happy-path behavior change — every
code change is a strictly-stricter refusal, a corrected internal baseline, or a schema/doc that
stopped overstating. Unit-proven (broker 893, +9). **Bench re-proof 2026-07-10** (sealed lab, v16 —
`docs/plans/2026-07-10-reconcile-belts-bench-reproof.md`): the WG-1 wrong-company belt is
**LIVE-PROVEN** (a `db_set(update_modified=False)` company swap, freshness-invisible, was refused
`stage: plan … wrong books`; the clean settle still committed; the marker survived the refusal).
WG-2a (NaN) has no live leg by design (refused at plan). WG-3 is unit-proven only and honestly so —
a reconcile bumps the invoice's `modified`, so the freshness gate pre-empts its false-`unconfirmed`
scenario in practice; the fix stays as docstring-consistency + defense-in-depth.

- **RECONCILE — execute-time wrong-company belt (was missing, now closed).** `run_reconcile`
  preflighted the closed-books/lock checks against the PLAN-time company only; a company swapped
  under a live plan via `db_set(update_modified=False)` leaves `modified` untouched (freshness
  passes) and could land a settle against a *different* company's closed books. Now re-reads each
  invoice's and payment's **live** company at execute and refuses any drift (stage `plan`,
  deny-biased — an unreadable/missing company refuses too). Mirrors `_governed_write`'s F-C2 /
  `_cascade_books_gate`'s C1 belt, which already closed this exact class for submit/cancel/cascade;
  reconcile (0.13.0, newer) never got it.
- **RECONCILE — the over-allocation ceiling is no longer defeated by a `NaN`.** `check_allocation`
  gained `math.isfinite` guards (the pattern `consent.py`/`prove.py` already use). A `NaN`/`inf`
  `allocated_amount` (reachable via a literal `"nan"` `plan_reconcile` arg) used to slip past the
  positivity check and *both* ceiling comparisons (every comparison against `NaN` is `False`) and
  then crash past the structured-deny layer, stranding the consent marker with no receipt trail.
  Now refused cleanly at preflight, marker untouched.
- **RECONCILE — the success baseline uses the FRESH outstanding, not the stale plan-time snapshot.**
  The readback that decides committed-vs-unconfirmed compared against the plan-time
  `invoice_outstanding`; if outstanding legitimately moved between plan and execute, a
  correctly-bounded settle was false-flagged `unconfirmed`. Now judged against the fresh preflight
  read (the same source the ceiling uses). The wire still carries the pinned snapshot by design
  (the payment-echo TOCTOU belt — unchanged).
- **THE CLOSE — an `unconfirmed` act now blocks a clean close (John's ruling, this session).**
  `pacioli close` reported a period "balanced — closes clean" (exit 0) even when it held an
  `unconfirmed` act — a write that got no answer and MAY have posted server-side, the exact suspense
  item `prove`'s own orphan sweep refuses to clear. Two attestation surfaces of the same system
  disagreed on the most safety-critical status, in the tool whose thesis is "balance or confess a
  gap." Now `balanced` requires no orphans AND no `unconfirmed` acts, and the confession lists them;
  a `failed` act (the bench answered and refused — a known non-event) still does NOT block, so
  governance correctly refusing a write never reads as an unbalanced period. **Observable change:** a
  period containing an `unconfirmed` act now exits 1 and confesses it (was exit 0), and the JSON
  gains `summary.unconfirmed`. This restores the tool's own documented "exit 0 only when the period
  closes clean" contract.
- **UNDO — the cancel blast-radius no longer reads an unreadable graph as a leaf.**
  `get_submitted_linked_docs` resolved a malformed 200 body (`{"message": {"docs": null}}`, or a
  missing `docs` key even alongside a non-zero `count`) to `[]` via `list(x or [])` — silently
  no-opping the non-leaf-cancel refusal. Now a dict message without a real `docs` list refuses
  (deny-biased), honoring the method's own docstring. `find_amendments` carried the same shape (a
  `null` `data` read as "no amendments", letting a duplicate amended draft slip past) — same fix.
- **`plan_reconcile` tool schema stopped advertising Journal-Entry payments.** The agent-facing
  description + `payment_type`/`payment_no` fields listed "Journal Entry" as a reconcilable
  payment, but the code unconditionally refuses it (JE-payments are a deferred increment). The
  schema now advertises exactly what it delivers (the legitimate "system Journal Entry side-effect"
  disclosure stays).
- **Docs truth-sized** (no code): the F-R2 grant precondition (was still "NOT live-proven" after
  PHASE X live-proved it), the JE-grant box ("the Gate 10 precondition" after Gate 10 ran), the
  tool count (28 → **30**), the README headline (the certified tight seat is proven on the Sales
  Invoice vertical + doctor's read surface, *not* "the whole surface"), and the E5 proof-record
  verdict ("structurally unreachable" softened to ordinary deny-by-default — `Payment
  Reconciliation.reconcile` is a grantable method the F-R2 seat now holds, not a Bulk-Update-style
  ungrantable block).

## 0.15.0 — 2026-07-09 — the Close: the period Statement (Half 1)

MINOR (new operator capability — `pacioli close`; governed runtime + guard byte-untouched, no new
grant). Design + the honest loop-analysis: `docs/plans/2026-07-09-the-close.md`. Double-entry's
power in 1494 was the *close* — at period end the books balance or confess. PROVE records every
governed act; the Close is the trial balance for a period.

- **`pacioli close [--target] [--since ISO] [--until ISO] [--expected-head HASH] [--json]`** — a
  read-only period attestation built from Pacioli's OWN receipt store: every governed act,
  classified **committed** (balanced), **recorded-open** (a terminal failed/unconfirmed outcome —
  accounted for but didn't land clean), or **orphan** (intent with no committed outcome — the
  confession). Summaries by class / tool / target / doctype, the chain head + verify result + any
  off-box anchor comparison, and — carried IN the statement, never a droppable footnote — the
  honest scope line: this attests only to what passed *through Pacioli*, not the whole ERPNext
  ledger. Renders a human-legible statement — the proof made readable — or `--json`.
- **Balanced ⇔ closes clean:** exit 0 only when there are no orphans AND the chain verifies AND
  (if `--expected-head` given) the live head matches the off-box pin — the same
  it-doesn't-balance-so-it's-not-done discipline as `verify`/`orphans`. A no-timestamp receipt is
  included and flagged, never silently dropped from a windowed attestation (deny-biased).
- **Pure core** `close.py` (`build_statement`/`render_statement`) — no seal, no I/O, no clock, the
  `prove.py` discipline; the glue supplies receipts + the verify result + head + anchor. 22 tests.
- **Honest scope of this half** (in the design doc + the statement's own words): the Statement is
  Pacioli attesting to Pacioli — necessary, but self-referential. The loop only closes when the
  **Reconciliation** (Half 2, designed, next bench window) checks the Statement against ERPNext's
  actual books, and truly bites its tail at **response-to-gap** (Half 3, on the map). Half 1 must
  not be mistaken for the closed loop.

## 0.14.0 — 2026-07-09 — `probe_belt_exemptions`: watch the belts' role escape hatches

MINOR (new `doctor` capability; governed runtime byte-untouched; **no new grant** — every read
rides grants the broker already requires). Design: `docs/plans/2026-07-09-probe-belt-exemptions.md`
(the recorded next increment from the tight-role sheet). PHASE X's T-P5 verified the exemption
fields blank on the proof bench; this probe is what keeps that true over time.

- **The three stock escape hatches, watched:** `Accounting Period.exempted_role` (source-verified
  to have **no anti-Administrator carve-out** — once non-blank, Administrator bypasses that belt
  too), `Company.role_allowed_for_frozen_entries` (v16), and legacy
  `Accounts Settings.frozen_accounts_modifier` (≤v15). Each, once set, silently disables ERPNext's
  closed-period / frozen-books belt for every seat holding the named role — with zero warning
  anywhere until now.
- **Verdict, cross-referenced against the seat's own roles** (same `User.get_roles` read as
  `probe_roles`): exemption held by this seat → **FAIL** (that belt does not fire for this seat's
  postings; the broker's own closed-books refusal is the only remaining gate). The cross-ref set
  deliberately keeps the frappe-auto roles — `exempted_role = "All"` exempts every authenticated
  seat and fails loudly. Set-but-not-held → **WARN**. Nothing set → OK.
- **Deny-biased end-to-end:** any unreadable source (roles 403 → the `User.get_roles` grant
  remedy; Company/Accounts Settings/Accounting Period list or item failures; a nameless list row;
  a non-string field value; a mid-probe transport exception, cause carried into the message) is a
  FAIL — an unauditable belt cannot be certified.
- **Version-safe + volume-safe by construction:** every field is read off a FULL document with
  `.get()` (absent = the other major-version's shape = blank — the F-C1 lesson: never select a
  possibly-version-missing column in LIST `fields`); every LIST carries `limit_page_length: "0"`
  (the F-V1 lesson), pinned by test.
- Honest residuals (pin sheet): drift *detection*, not prevention — a write after doctor ran is
  invisible until the next run; custom belt-bypass mechanisms (server scripts) are out of scope.
  Bench pins B1–B6 staged for the next arm window.

## 0.13.1 — 2026-07-09 — the reconcile wire shape, corrected against the live bench (P7)

PATCH (bug fix of 0.13.0's known-incomplete transport; no API change, no new grant). The first
live run of the governed reconcile (bench window, sealed lab, Frappe v16) refused exactly as
0.13.0's CHANGELOG predicted — and answered both BENCH-PENDING questions by reproduction:

- **The `invoices[]` pool child-table is REQUIRED.** ERPNext's `validate_allocation` builds its
  per-invoice outstanding map from `self.get("invoices")`; with the pool absent the ceiling check
  TypeErrors (`float - NoneType`, HTTP 500 — the exact live refusal). The broker now sends one pool
  row per unique invoice (`{invoice_type, invoice_number, outstanding_amount}`). A `payments[]`
  pool is NOT read on this write path and is deliberately not sent.
- **`amount` and `unreconciled_amount` are BOTH the payment's unallocated — 0.13.0 had the
  semantics swapped** (it sent the invoice's outstanding as `unreconciled_amount`).
  `check_if_advance_entry_modified` compares `unreconciled_amount` to the PE's **live**
  `unallocated_amount` (`utils.py:645-647`); the swapped value was refused live ("Payment Entry
  has been modified after you pulled it"). Entries are processed grouped per voucher with every
  check before the group's single save, so every row carries the plain pre-write value.
- **Semantic row keys at the core/glue seam.** `run_reconcile`'s rows now carry
  `payment_unallocated`/`invoice_outstanding` (semantic) instead of wire-ish names; the ONLY
  semantic→wire translation lives in `erpnext.py reconcile()` — the swapped-semantics bug class
  now has exactly one place to exist, and it is pinned by live-verified tests.
- **Pinned-not-live echo values, deliberately:** the wire echoes are the plan-time values the
  human saw disclosed, so ERPNext's own anti-tamper doubles as a second TOCTOU belt — a doc that
  drifted past `check_fresh` (e.g. `db_set` with `update_modified=False`) gets an ANSWERED
  pre-write refusal from the bench itself (marker released, nothing landed) instead of silently
  landing a drifted act.

## 0.13.0 — 2026-07-09 — F-R2: govern Payment Reconciliation

Design + source citations: `docs/plans/2026-07-09-fr2-govern-reconciliation.md` (2 sonnet scouts read
frappe/erpnext v16). MINOR (new governed operation + a new required grant). Code-complete +
unit-proven; **the reconcile transport payload is known-incomplete against a live bench** — static
analysis shows wire-shape gaps (the `invoices[]`/`payments[]` pool child-tables; the `amount` field's
exact semantics) that would be refused by ERPNext's own validation until corrected in the first bench
window. **NOT live-proven** — bench pins P1–P8 staged. Two adversarial redteam passes fixed a real
safety bug pre-ship — the over-allocation ceiling was checked per-row, now **cumulative per invoice
AND per payment** across the whole allocation set — plus honesty corrections (the per-account-freeze
overclaim, the JE-payment deferral).

- **New governed operation: `plan_reconcile` → marker → `reconcile`.** An agent settles specific
  payments against specific invoices through PLAN → CONSENT → PROVE. Modeled on cascade (preflight
  the whole allocation set, one marker, readback-driven outcome) — NOT the single-doc submit shape,
  because `Payment Reconciliation` is a stateless single, not submittable (no docstatus to confirm).
- **Why the broker owns the safety, not the grant** (the load-bearing finding): ERPNext's own
  `reconcile()` trusts the caller's declared `outstanding_amount` (never re-fetched) and writes with
  `ignore_permissions=True`, and the guard — once it grants the doctype-method pair — is blind to the
  allocation *content*. So the broker (a) **constructs the reconcile payload itself** from data it
  read fresh during PLAN and never forwards an agent-supplied allocation (the agent proposes at plan
  time; `reconcile` takes only `plan_id`+`marker`); (b) enforces its **own** over-allocation ceiling
  from a fresh read, **cumulative per invoice and per payment across the whole allocation set** (not
  per row — two rows against one invoice cannot together exceed its outstanding); (c) supplies the
  **freshness** ERPNext lacks (the doctype's `check_if_latest` is structurally inert — `load_from_db`
  hardcodes `modified=None`); (d) **enforces the CLOSED-BOOKS belt ERPNext bypasses** — the relink
  write never reaches the company period-freeze check, so the broker runs its own closed-Accounting-
  Period / PCV / company-frozen-till refusal on both the invoice and payment dates. The broker ends
  up *stricter than ERPNext*, deliberately. (The **per-account** `Account.freeze_account` check
  ERPNext also skips via `adv_adj=1` is **DISCLOSED** in the plan but **not yet independently
  enforced** — a recorded next increment, needing an Account read grant.)
- **New confirmation discipline (readback, no docstatus):** the outcome is decided by re-reading each
  invoice's real post-write `outstanding_amount`, never the call's result body. Deliberately stricter
  than `spine`'s `post_failure_readback`: `committed` requires BOTH a clean return AND a readback
  confirm — a raised call (even one the readback later shows landed) degrades to `unconfirmed`,
  because reconcile's readback is a weaker signal than a docstatus transition (side-effect JEs
  unverified; confoundable by a concurrent change). Marker released only on an answered refusal the
  readback proves changed nothing; spent in every partial/ambiguous case.
- **New required grant (config-only, no guard code change): `Payment Reconciliation.reconcile`** —
  rides the guard's existing doctype-method scoping (`body_scoped_target` already resolves
  `run_doc_method` on the body doctype; locked by `guard` `TestRolesProbeGrantContract`-style
  reasoning). No `doctor` probe: a read-grant probe is safe to attempt, but a reconcile is a *write* —
  it can't be probed without performing it, so the grant is README-documented, not probe-enforced.
- **Deferred, recorded (not pretended-covered):** **Journal-Entry payments** (the build restricts
  payments to Payment Entry — a JE's available amount is not a simple `unallocated_amount` field);
  **per-account `Account.freeze_account` enforcement** (disclosed, not enforced — above); UNDO
  (`Unreconcile Payment` — a wider raw-SQL blast radius straight to GL Entry, with no clean
  `on_cancel` reversal); and `Process Payment Reconciliation` (the queued batch). Each its own
  future increment.

## 0.12.0 — 2026-07-09

**Added — `doctor` refuses an over-privileged seat (the tight-role seat), plus a NEW doctor-only
grant.** Design + source citations: `docs/plans/2026-07-09-tight-role-seat.md` (scout-verified
against frappe/erpnext v16).

- **`pacioli doctor` gains a roles probe (`probe_roles`).** It reads the broker seat's own roles
  (`GET /api/v2/method/User/get_roles`) and **FAILs an install whose seat carries `System Manager`**
  (or the literal Administrator) — extending doctor's existing "Administrator is a failure" doctrine
  from the username to the administrative *role*. Such a seat can administer the governance away over
  the REST surface (write Custom DocPerm rows, mint API keys, run arbitrary code via the System
  Console if server scripts are enabled), even though frappe grants the *role* no runtime
  permission-bypass — only the literal `Administrator` username gets that
  (`frappe/permissions.py:107,304,544`). An `Accounts Manager` seat draws a WARN (over-broad, not
  spine-voiding). Deny-biased: a 403, an unparseable body, or an empty role list is a FAIL — an
  un-auditable seat cannot be certified least-privilege.
- **NEW required grant — `User.get_roles` (doctor only; the governed runtime is byte-unchanged).**
  Chosen via the v2 doctype-resolved route (`/api/v2/method/User/get_roles`) so it rides a
  config-only `User.get_roles` methods-grant with **no guard code change** (the bare dotted method
  would need a reviewed `SAFE_METHODS` addition — a guard release). A cross-package lock test
  (`guard` `TestRolesProbeGrantContract`) pins this. **Existing installs:** add the grant (API Key
  Scope → `methods` → `User.get_roles`), else the roles probe reports a 403 FAIL with the remedy.
  Honest caveat: `get_roles` honors a `?uid=` param with no permission check, so this grant also
  permits reading *any* user's roles (read-only recon, not escalation); doctor never sends `uid`. A
  guard-side `uid` fence is a recorded follow-up.
- **The minimal seat, documented (README recipe):** `Accounts User` + a custom "Pacioli Seat" role
  granting only Sales Invoice cancel + Accounts Settings / Period Closing Voucher / Workflow read.
- **Doc ripple:** DESIGN.md / SPEC.md / README.md's "System Manager bypasses every permission"
  meta-finding is corrected to the source-verified version (only the literal `Administrator`
  *username* is a runtime bypass; `System Manager` is an administrative/blast-radius risk that this
  probe now refuses).
- **Redteam** (3 lenses — bypass/correctness, honesty/regression, mechanical): no live bypass found;
  the minimal role was independently re-derived to cover every broker op. Caught + fixed pre-ship:
  the refusal message overclaimed app-install (bench-CLI-only, not REST-reachable) and understated
  System Console code-exec; the `User.get_roles` `uid` breadth; two edge cases (whitespace-padded
  role name, blank-only role list) hardened with strip+drop-blanks. **762 broker + 274 guard green.**

**Changed (internal) — doctype-descriptor spine (behavior-preserving refactor)**

Design: `docs/plans/2026-07-08-doctype-descriptor-spine.md`. Carries no bump *on its own* (capability
and maturity unchanged; the enabling scaffold, no new doctype) — folded into 0.12.0, which the roles
probe bumps. Recorded because one agent-facing surface changes.

- **The 20 mechanical tools (`get`/`list`/`submit`/`cancel`/`amend` × 4 doctypes) now generate from
  a `DoctypeDescriptor` table** instead of 20 hand-written tool dicts + 20 one-line wrapper methods
  (net −237 lines in `tools.py`). Adding a supported doctype becomes one descriptor row (+ its own
  hazard walk in the disclosure layer, which is deliberately NOT generated). The 8 generic
  spine tools and every governed-write helper (`_governed_write`, `_<verb>_document`) are untouched
  — verified by an AST diff of all 51 functions against the pre-refactor file (byte-identical).
- **Agent-facing change (the reason this is logged):** the 15 non-Sales-Invoice mechanical tools'
  top-level `description` text is now templated from a per-verb template (was hand-written with
  cross-references like "same generic handler as get_sales_invoice"). Wording only; every tool's
  **inputSchema is byte-identical** to before (sub-property hint text included — submit still says
  "Draft document name."/"From plan_submit.", cancel "Submitted document name."/"From plan_cancel.").
- **New tests** (`tests/test_tool_surface.py`, 8): pin the exact 28-tool name-set, each mechanical
  tool's full inputSchema, dispatch routing (the late-binding-closure guard), and a
  `DESCRIPTORS`↔`SUPPORTED_DOCTYPES` consistency belt (a doctype in one table but not the other now
  fails loudly). Independent-literal expectations, redteam-confirmed non-vacuous.

## 0.11.0 — 2026-07-07

**Added — the settling-PE disclosure on cancel (F-R1), plus a NEW required grant.** Pin sheet
`docs/plans/2026-07-07-fr1-settling-pe-disclosure.md`. E5/PHASE R's recorded finding: cancelling a
document that a Payment Entry (or any other `auto_cancel_exempted_doctypes` voucher) has settled
silently unlinks that settlement — the payment stays posted but its allocation against this
document is severed and its unallocated amount goes back up — and the broker's existing blast-
radius check (`get_submitted_linked_docs`) structurally never surfaces it, because PE is removed
from ERPNext's own linked-docs traversal at two points (frappe's exempt-list handling). The human
consented to a cancel without being told a settlement link was about to be severed, on every
doctype except Journal Entry (which already carried a narrower, JE-only version of this warning).

- **New client read, `get_settling_references(doctype, name)`** (`pacioli/erpnext.py`): a
  doctype-blind GET against **Payment Ledger Entry** (the settlement ledger since ERPNext v14),
  filtered to `against_voucher_type`/`against_voucher_no` = this document, `delinked=0`, and
  `voucher_no != name` (excludes the document's own self-referencing rows). GL-entries-shaped:
  explicit fields (`voucher_type`/`voucher_no`/`amount`/`account_currency`), `limit_page_length:
  "0"` (F-V1 law), and the same structured-deny-on-non-list-body house pattern
  `get_period_locks` already applies to its own Accounting Period LIST read.
- **`plan_cancel` and `plan_cascade_cancel` disclosure, widened to EVERY supported doctype** (not
  just Journal Entry): the Accounts Settings `unlink_payment_on_cancellation_of_invoice` read is
  now made for every doctype (memoized ONCE per plan/graph — generalized from the prior JE-only
  `je_settings` memo variable), and every settling voucher this new read surfaces gets its own
  flag, in one of two exact voices depending on the setting:
  - **ON** — "cancelling will SILENTLY UNLINK `<voucher_type>` `<voucher_no>`'s allocation of
    `<amount>` against this document — the payment stays posted but the settlement link is
    severed and its unallocated amount increases (`auto_cancel_exempted_doctypes`)".
  - **OFF** — "ERPNext will REFUSE this cancel (LinkExistsError) while `<voucher_no>` references
    it".
  No settling rows = no new flags (the control case — an unsettled document gets no noise). An
  **unreadable Payment Ledger Entry read OR an unreadable Accounts Settings read refuses the WHOLE
  plan** (deny-biased — the standing "an unreadable graph refuses too" law), never just a missing
  flag. The prior Journal-Entry-specific EG-auto-cancel note and unlink flag
  (`_journal_entry_cancel_flags_for_settings`) are **unchanged, byte-for-byte** — this disclosure
  is additional, not a replacement. Cascade parity: the Payment Ledger Entry read happens PER NODE
  (each node has its own settlement blast radius), docname-prefixed, same convention as every
  other per-node cascade flag; the Accounts Settings read stays memoized ONCE for the whole graph.
- **Named test casualties (both flip deliberately, not silent regressions):**
  (1) `test_non_je_plan_cancel_never_reads_accounts_settings` pinned that a non-Journal-Entry
  `plan_cancel` NEVER read Accounts Settings — the exact opposite of the new, correct behavior;
  renamed to `test_plan_cancel_reads_accounts_settings_for_every_doctype`. (2) `test_403_fails_
  naming_the_je_cancel_scope` (doctor) pinned remedy text claiming "other doctypes' cancels are
  unaffected" — which this widening made false; renamed to
  `test_403_fails_naming_the_widened_f_r1_scope`, asserting that stale claim is now ABSENT.
  Each carries an in-file comment naming the flip.
- **Recorded residual (the 0.9.6 precedent, generalized — plan-time disclosure can go stale):**
  the settling-reference list AND the unlink setting are read at PLAN time and never re-verified
  at execute — a settlement that lands (or a setting flip) between plan and marker-mint makes the
  disclosed rationale stale relative to the live cancel. `check_fresh` covers only the target
  document's own `modified`. Nothing is bypassed — ERPNext's own execute-time behavior (the
  unlink, or its `LinkExistsError` refusal) is the real enforcement either way; the disclosure is
  the consent story, best-effort by construction. Same class as 0.9.6's JE-only note, now carried
  forward for the widened read rather than silently dropped.

**BREAKING — a NEW required grant: `Payment Ledger Entry` read.** Every doctype's `plan_cancel`/
`plan_cascade_cancel` now reads Payment Ledger Entry to compute the settling-PE disclosure above,
and an unreadable read **refuses the whole plan** (deny-biased, the same house rule as every other
lock-adjacent read) — **an existing broker credential scoped before this release will have EVERY
plan_cancel/plan_cascade_cancel denied until the grant is added**, whether or not the target
document is actually settled by anything. The migration step: Role Permission Manager → `Payment
Ledger Entry` → read permission for the broker's role. `pacioli doctor` gains
`probe_payment_ledger_read` (registered in `run_doctor`, named in the `--offline` skip message),
mirroring the Company/PCV/Accounting-Period-read probes' deliberate 403-is-FAIL inversion (readable-
but-empty still passes).

**Honest limit, unchanged in kind from every other breadth increment in this file:** the Payment
Ledger Entry filter shape, and the doctor probe's inversion, are knowledge-pinned from ERPNext v14+
source and the pin sheet's ground-truth read (Scout A = frappe `9a8daf3` + erpnext `d1d3b24`, Scout
B = broker 0.10.4 audit) — **not yet live-verified against a bench.** Five bench pins are staged
for the next armed window (`docs/plans/2026-07-07-fr1-settling-pe-disclosure.md`, "Pins to
falsify"): R1 the disclosure names the settling PE + amount pre-consent and the live unlink matches
after a governed cancel; R2 the OFF-voice names a real `LinkExistsError` refusal; R3 an unsettled
document gets zero flags; R4 an unscoped credential's doctor probe FAILs naming the grant, a
widened one PASSes; R5 receipts exact, zero new orphans. Version bumped `0.10.4 → 0.11.0` (MINOR —
a new disclosure surface, a new client read, and a NEW required grant; not a hardening-only patch)
— builder-drafted, head-chef confirmed MINOR. Full suite: 737 (was 706; +1 = the redteam's
malformed-PLE-row structured-deny pin, fixed pre-ship alongside the two doc-honesty catches).

## 0.10.4 — 2026-07-07

**Fixed — the recorded residual since 0.9.3 is closed: an exception from the mutating call itself
no longer releases the consent marker on a never-verified "no progress" assumption (transport
taxonomy, `docs/plans/2026-07-07-transport-taxonomy.md`; scout pass same day — Scout A read frappe
`9a8daf3`/erpnext `d1d3b24` source for rollback/status semantics, Scout B audited the broker's own
exception landing sites).** 0.9.3's CRITICAL fix covered the *readback-throwing* half of this gap
(cascade); the other half — a raw exception from `submit_document`/`cancel_document` itself, where
the broker had no way to tell "the bench said no" from "no answer arrived" — was recorded and
deferred as its own increment in both the CHANGELOG and `docs/plans/2026-07-07-envelope-e3-eg-
cascade.md`'s out-of-scope list. This closes it, single-op and cascade alike.

- **The taxonomy** (`ErpnextError` grows `answered: bool = False`):

  | Class | Evidence | Marker | Outcome |
  |---|---|---|---|
  | Answered refusal/error | int status + frappe's OWN error envelope in the JSON body (`exc_type` / `_server_messages`) | `release()` (byte-identical) | `"failed"` + error |
  | Rejected-before-processing | int status ∈ {429, 413}, any body | `release()` | `"failed"` + error |
  | Ambiguous answer | int status, body non-JSON OR JSON *without* frappe's envelope keys (a JSON-speaking proxy's error page) | **spend** + readback | per readback |
  | No answer | `status=None`, or ANY unconverted exception | **spend** + readback | per readback |

  Readback resolution (single-op and cascade node alike, the existing `_Effects.cancel`,
  tools.py:1744-1779, readback pattern): confirmed end-docstatus → `"committed"` +
  `confirmed_via: "post_failure_readback"`; mismatch → `"unconfirmed"`; readback itself fails →
  `"unconfirmed"` + `readback_error` (never release-in-flight either way).

- **THE deliberate behavior flip, deny-biased:** a generic/unconverted exception from the mutating
  call (the exact shape of the pre-fix residual — a raw `OSError` that escaped `default_transport`
  unconverted, or any other unclassified raise) used to `release()` the marker, assuming no
  progress. It now **spends** the marker and resolves via a governed readback instead — the
  redteam property held throughout this fix: **every reclassification moves release→spend, never
  the other direction; no release path was added.** `test_spine.py`'s
  `test_execute_raises_records_failed_and_releases_marker` (a bare `RuntimeError`) is the direct
  casualty — renamed/reworked to `test_unanswered_exception_spends_marker_and_resolves_via_readback`
  (now asserts commit+readback, not release), with a new `AnsweredError`-carrying sibling,
  `test_answered_error_records_failed_and_releases_marker`, pinning the unchanged answered-refusal
  branch byte-for-byte. `test_cascade.py`'s `test_first_node_fail_marker_released` (same
  `RuntimeError` shape) got the identical treatment →
  `test_no_answer_exception_first_node_now_commits_not_released`, with `test_answered_error_first_
  node_releases` as its answered-branch sibling.
- **Source-verified premises this closure rests on** (Scout A, one line each): frappe rolls back
  the whole request on ANY exception, unconditionally, for the broker's call shapes
  (`frappe.client.submit/cancel`, `run_method=submit|cancel` — `app.py:144-150`, success-path-only
  commit) — an exception genuinely means nothing landed, *when the bench is the one raising it*;
  429/413 are always pre-handler (rate limiter in `init_request` before dispatch; body-size check
  during request parsing) — guaranteed no progress, safe to release wherever emitted; a proxy
  502/503/504 is disambiguated from a frappe-emitted error by frappe's OWN envelope keys —
  `report_error` stamps `exc_type` unconditionally on every V1 error response
  (frappe/utils/response.py), while proxy error bodies (HTML, text, or generic JSON like
  `{"error": "Bad Gateway"}`) never carry `exc_type`/`_server_messages`. "Parsed JSON" alone is
  NOT positive proof — modern proxies speak JSON too (the redteam reproduced exactly that
  mis-release; see the redteam paragraph below).
- **Transport layer** (`erpnext.py`): `ErpnextError.answered` set by a new `_answered(status,
  payload)` helper at both `_call` raise sites (the non-2xx branch and the non-JSON-body branch);
  `default_transport`'s connection-failure catch broadened from `(URLError, TimeoutError)` to
  `OSError` (both are already OSError subclasses, so this collapses cleanly and additionally
  catches raw connection-level failures — `ConnectionResetError` etc. — that used to escape
  unconverted) — `HTTPError` (itself an OSError subclass) is still handled first and separately, so
  an answered HTTP error is never swallowed by the broadened catch.
- **Readback seam, single-op** (`store.SubmitEffects` grows an optional `readback()`; wired in
  `tools.py`'s `_governed_write` to `client.get_document(doctype, name).get("docstatus")`, the same
  read path as everywhere else in this module) **and cascade** (`_Effects` in `_tool_cascade_cancel`
  grows the same `readback(dt, dn)`) — never raises past the pure core, which owns degrading any
  readback failure to `readback_error`.
- **Cascade design choice, spelled out in code comments:** when the no-answer readback DOES confirm
  a node cancelled, the run still fail-stops there rather than resuming the loop — the exception
  interrupted normal control flow, and resuming past an exceptional path isn't the same guarantee
  the ordinary per-node continue carries. The confirmed node is recorded honestly in `cancelled`
  with `confirmed_via` on its outcome; later nodes are never attempted.
- Pinned by 5 new/reworked pure-core tests each in `test_spine.py`/`test_cascade.py`, a new
  `TestTransportTaxonomy`/`TestDefaultTransportConnectionFailures` in `test_erpnext.py` (12 tests,
  including a monkeypatched-`urlopen` pin that `HTTPError` is never swallowed by the broadened
  `OSError` catch), and glue-wiring tests in `test_tools.py`
  (`TestGovernedSubmitNoAnswerReadback`/`TestGovernedCancelNoAnswerReadback`/
  `TestCascadeCancelNoAnswerReadback`) proving the real `client.get_document` readback end to end.
  Pre-existing tests that simulated a bench-answered refusal via a directly-constructed
  `ErpnextError(status=...)` (`FakeClient`/`CascadeClient` in `test_tools.py` — `fail_submit`,
  `fail_cancel`, `fail_locks`, `fail_linked`, `fail_amend`, `fail_workflows`, `fail_workflow_state`,
  `fail_apply_workflow`, `fail_accounts_settings`, the 404 read) now construct it with
  `answered=True` explicitly, since those fakes stand in for the whole `ErpnextClient` (bypassing
  `_call`'s own classification) and were always meant to simulate a bench that actually answered —
  their pre-existing release-marker assertions are unaffected, not flipped.
- **Redteam catches, fixed pre-ship** (consent-policy bypass lens + regression/honesty lens +
  mechanical, all three on the diff): (1) 🥇 **the JSON-proxy hole, reproduced live against the
  client** — the first-cut `_answered` accepted ANY dict payload as proof of a frappe answer, so a
  JSON-speaking proxy's 502 (`{"error": "Bad Gateway"}`) would have released the marker on unknown
  progress, reopening the exact residual this fix closes for that transport shape; fixed by
  requiring frappe's envelope keys (`_FRAPPE_ENVELOPE_KEYS`), pinned by 3 new tests including the
  reproduced input verbatim. (2) the committed-via-readback receipt recorded `confirmed_via` but
  not WHAT failed — the durable receipt now carries the original `error` string on all three
  readback resolutions, asserted in tests (an auditor reads the book without the code).
- **Bench proof is STAGED, not run:** pins T1–T5 in the taxonomy pin sheet await the next arm
  window (fold into PHASE W). Only the *pre-fix* residual behavior was ever observed live; the
  flip itself is unit-proven only, and is stated as such everywhere.
- **Honest residual (recorded, out of broker reach):** the "answered ⇒ rolled back ⇒ safe to
  release" premise is source-verified for frappe core + the erpnext mainline submit/cancel flows —
  but a CUSTOM doctype hook calling `frappe.db.commit()` mid-flow before a later failure would
  break it (partial state committed, error response still sent). Not present in any mainline path
  the broker calls; unverifiable for arbitrary third-party apps from here. Recorded in README
  honest-scope alongside this fix.
- **706 green** (broker, up from 673) + 270 (guard, untouched — no guard-side surface touched).
- **Semver classification (deliberate, not incidental): PATCH — flagged for head-chef confirmation.**
  This is hardening that closes a recorded gap in an existing security gate (the marker-release
  decision on a mutating-call exception) — no new tool, no new argument, no user-facing surface
  added. The new `confirmed_via`/`readback_error` result fields are additive receipt detail on the
  existing `"committed"`/`"unconfirmed"` vocabulary, not a new outcome vocabulary. The one item that
  could argue otherwise: this DOES change observable behavior for a real caller (a marker that used
  to come back `live`/reusable after a connection failure now comes back spent) — a deliberate,
  deny-biased tightening of an existing gate, the same class of change as F-C1/F-C2 (both classed
  PATCH; F-S1 — the one *loosening* — went MINOR, and this is its opposite direction) — but
  flagged here explicitly since it changes what a caller can DO after the call
  (re-plan-and-retry with the same marker is no longer possible), which is a sharper edge than a
  read-path hardening.

## 0.10.3 — 2026-07-07

**F-V1: the Accounting Period LIST is the one gate-feeding read that sent no `limit_page_length`
at all — source-found by the E8 volume-and-caps scout pass
(`docs/plans/2026-07-07-envelope-e8-volume-caps.md` §F-V1).** `get_period_locks`'s Accounting
Period LIST (`erpnext.py:665-670`) decides closed-books allow/deny — it feeds `check_red_line`
directly. Frappe's v1 REST (`api/v1.py:19-21`) defaults an omitted `limit_page_length` to **20**
rows and carries **no truncation signal** of any kind: no `has_more`, no total count, nothing that
would tell a caller a result was cut. Every sibling gate-feeding read already pins a limit
explicitly — `find_amendments` (`:445`) and `get_active_workflows` (`:485`) pin `"0"`
(unbounded), the GL-entries read (`:422`) pins `"0"`, the Period Closing Voucher read (`:646`)
deliberately pins `"1"` (latest-only, by design) — the Accounting Period LIST was the one
exception, silently riding frappe's default instead of naming its own limit.

- **Deny-bias reasoning, spelled out:** a bench with more than 20 `Accounting Period` rows whose
  `start_date`/`end_date` range contains the posting date would have this LIST inspect only page
  1 — an enabled, closing period sitting on page 2 would never be read, `check_red_line` would
  never see it, and a posting into genuinely closed books would be **allowed**. That is the
  dangerous direction: allow-where-should-refuse, not merely an over-refusal. `limit_page_length:
  "0"` provably disables the LIMIT clause outright (frappe `db_query.py:185` — a falsy value
  emits no `LIMIT` at all), matching the unbounded siblings rather than inventing a new pattern.
- **The F-C1 interaction (0.10.2, same day) raised the stakes of this gap.** F-C1 correctly
  dropped the v16-only `disabled=0` filter from this same LIST (a v15-breaking unknown-column
  filter) so that a v15 bench's periods would be read at all — but doing so widened the LIST's
  row set: disabled periods that were previously filtered out server-side now flow through this
  read too, on both v15 and v16. A bench carrying many disabled periods alongside one enabled,
  closing period is exactly the shape that pushes the real match past row 20. F-C1 was the right
  fix for v15 compatibility; it also made the missing page-length pin a live-reachable gap rather
  than a theoretical one.
- **The fix**: `"limit_page_length": "0"` added to the Accounting Period LIST params, matching
  the exact style of `find_amendments` (`:445`) and `get_active_workflows` (`:485`) — no other
  param on this call changed; `filters`/`fields` are byte-identical to 0.10.2.
- Pinned by a new sibling test in `test_erpnext.py`
  (`test_accounting_period_list_pins_unbounded_limit_page_length`, beside the existing
  `test_accounting_period_list_filters_by_company_and_range_never_disabled`, which stays
  unweakened) asserting the LIST call's recorded params carry `limit_page_length == "0"`; confirmed
  failing first (`KeyError: 'limit_page_length'` — the param was simply absent) before the fix
  landed. A second gap named by the same scout pass — `get_active_workflows`'s own LIST already
  sent `"0"` in code but had no test pinning it — gained one assertion line in the existing
  `test_lists_then_fetches_each_full_workflow_doc` (no code change; that pin was already true).
- **Cascade's cap boundary, test-only, no code change:** the same scout pass named a gap on the
  *other* named-cap read in this envelope — `cascade.build_cascade`'s node cap
  (`cascade.py:56-59`) had an over-cap refusal test but no exactly-at-cap proof. Added
  `test_cap_exactly_met_succeeds` (`test_cascade.py`): a 3-node graph against `max_nodes=3`
  succeeds (`ok: True`, all 3 nodes returned, target still last in cancel order) — the cap refuses
  only when the count *exceeds* it, never when it merely meets it. This test passed on first run;
  `cascade.py` was not touched.
- **Semver classification (deliberate, not incidental): PATCH.** This closes a gap in an existing
  security gate (the closed-books read `check_red_line` already depends on) — hardening, not a new
  capability or a change in what the gate matches. No new tool, argument, or user-facing surface;
  the only behavior change is that a closing period sitting past row 20 of the Accounting Period
  LIST can no longer be silently missed. Classed patch per the workspace doctrine (patch =
  fix/hardening), the same class as F-C1 (0.10.2) and F-C2 (0.10.1).
- **673 green** (broker, up from 671) + 270 (guard, untouched — no guard-side surface touched;
  this is a broker-only request-shape hardening).
- Source pointers: `erpnext.py` `get_period_locks` (Accounting Period LIST params), pin sheet
  `docs/plans/2026-07-07-envelope-e8-volume-caps.md` §F-V1, the F-C1 entry above (0.10.2) for the
  interaction this fix closes.

## 0.10.2 — 2026-07-07

**F-C1: restoring v15 compatibility that F-S1 (0.10.0, same day) silently broke — the
`disabled=0` LIST filter on `Accounting Period` names a v16-only column, and frappe has no filter
sanitizer** (source-found by the E7 config-diversity scout pass,
`docs/plans/2026-07-07-envelope-e7-config-diversity.md` §F-C1). `get_period_locks`'s Accounting
Period LIST call added a `["disabled", "=", 0]` filter in F-S1. `Accounting Period` carries no
`disabled` column on a v15 bench (`accounting_period.json` v15 has no `disabled`, no
`exempted_role` — Scout A). Frappe builds filter SQL directly from the fieldname with no
meta-validation and no upstream sanitizer (`frappe/model/db_query.py::build_filter_conditions` ->
`prepare_filter_condition`, both branches; no `get_valid_columns` gate exists) — an unknown-column
filter is a MariaDB error, not "no match", so the LIST itself fails non-2xx and the broker's
`_call` raises. Because `get_period_locks` sits on the critical path of every plan/submit/cancel,
**the broker refused EVERY governed operation against a v15 bench** — fail-safe (no wrong write
could happen), but non-functional, not "same behavior." Before F-S1 the read carried no `disabled`
filter and worked on v15.

- **The fix**: the `disabled` check moves from the LIST filter to the per-hit item GET that
  `get_period_locks` already performs to read `closed_documents` (the list endpoint never expands
  child tables, so a full-document read was already happening for every hit). The LIST now filters
  only `company` + the date range (`start_date`/`end_date` — confirmed present on both v15 and v16,
  Scout A). Each hit's full document is read for `disabled` before its `closed_documents` rows are
  ever inspected: a truthy `disabled` skips the period outright (a disabled period locks nothing —
  the same F-S1/PHASE-T semantics, unchanged, just read from a different response); an *absent*
  `disabled` (the v15 shape — the field simply doesn't exist there) is treated as enabled, which is
  the correct v15 reading, not a fallback guess (v15 has no period-disable concept at all).
- **F-S1's v16 semantics are byte-preserved.** A v16 bench's disabled period is still allowed
  (PHASE-T's pin, now proven at the item-GET layer rather than the LIST-filter layer); an enabled,
  closing period still refuses, exact-boundary both ends; multi-period behavior is unchanged
  (a disabled period among several is skipped, an enabled closing sibling still locks). Nothing
  about F-S1's doctype- and date-range-aware matching changed — only *where* `disabled` is read
  from.
- **Judgment call, flagged for redteam:** a `disabled` value that is present but not a clean
  `0`/`1`/bool/`None` now **raises** (deny), rather than being coerced toward enabled or disabled.
  Coercing toward "enabled" would be the safe direction on its own, but doing so on an
  unparseable value is still a guess dressed as a default — and the pin sheet's instruction is
  explicit that a malformed value must never silently flip a period from locked to unlocked. Raising
  keeps this field under the exact same "can't verify, refuse" discipline every other field on this
  row already gets (`closed`, `document_type`, `start_date`/`end_date`). This case has never been
  observed on a real bench; it is the same class of defensive-but-untested deny path as the
  pre-existing malformed-`closed`/malformed-child-row checks beside it.
- **The v15 bench proof itself stays deferred** (egress-gated per the pin sheet — a fresh v15
  bench build needs package egress out of the sealed lab; standing that up is a network-scope
  change, John's hand). What ships here is unit-proven arm-free: a fake transport carrying the
  exact v15 shape (a full document with no `disabled` key at all) is exercised end to end —
  `get_period_locks` does not raise, and `closed_documents` is still evaluated correctly on that
  shape (locks when it closes the doctype, doesn't when it doesn't) — plus a companion test that
  pins the LIST's *sent* filters and asserts `disabled` is never among them, so a regression that
  re-adds the filter fails here rather than only being discovered against a live v15 bench (E7 P2).
- Pinned by 4 new tests (`test_erpnext.py`): the v15 arm-free proof (locks correctly, allows
  correctly, both off the no-`disabled`-key shape) in a new
  `TestPeriodLocksAccountingPeriodFC1V15Compat` class; a disabled-among-multiple-periods case
  proving the skip is per-hit, not a single disabled-anywhere shortcut
  (`TestPeriodLocksAccountingPeriodF3MultiPeriod`); the malformed-`disabled` deny
  (`TestPeriodLocksAccountingPeriodF4DenyBias`). Three existing F-S1 tests were updated (mock
  queues only, no assertion weakened): `test_disabled_period_is_now_allowed` now sends the period
  through the LIST (it's no longer excluded there) and pins the skip firing at the item-GET layer
  even though the fixture's `closed_documents` would otherwise lock — a strictly stronger version
  of the original test, which just described emptiness; the two F2 "still refused" boundary tests
  gained an explicit `disabled: 0` in their item-GET fixture for v16 realism; the filter-shape test
  was renamed and now asserts the LIST filters do NOT contain `disabled` (previously asserted the
  opposite). **671 green** (broker, up from 667) + 270 (guard, untouched — no guard-side surface
  touched; `pacioli_guard`'s scope classification for Accounting Period list/item reads is
  unaffected by which filters the broker happens to send).
- **Semver classification (deliberate, not incidental):** PATCH. This restores previously-working
  behavior (v15 compatibility existed before F-S1, same day) that a hardening change regressed —
  a compatibility/bug fix, not a new capability, and not a loosening of what F-S1 itself decided to
  refuse (every v16 refusal F-S1 introduced still refuses, byte-for-byte).
- **New failure mode, recorded (over-refusal, safe direction):** because the LIST no longer
  pre-filters `disabled`, a disabled period that the server would previously have withheld is now
  fetched via item-GET like any other hit — so a disabled period whose *own document* is
  unreadable/malformed now raises (deny) where before F-C1 it was never looked up at all. This can
  only make a v16 bench refuse an op it previously allowed (a corrupt disabled period's doc), never
  the reverse — strictly stricter, never a loosening. Surfaced by the gate-preservation redteam;
  noted here rather than left as an undocumented behavior change.
- **Redteam (gate-preservation lens + mechanical): clean, no loosening.** The catastrophic case —
  an enabled (`disabled=0`) period that closes the doctype must STILL lock — was hand-traced and is
  pinned by two tests (`test_disabled_period_among_multiple_is_skipped_enabled_still_locks`, the F2
  exact-boundary pair); a flipped skip-polarity fails them. Malformed `disabled` raises (never
  silently skips a would-lock period); v15 shape (no `disabled` key) tested in BOTH lock and allow
  directions. No input allows a posting into a genuinely-closed enabled period that 0.10.1 refused.
- Source pointers: `erpnext.py` `get_period_locks` (LIST + per-hit item-GET loop), pin sheet
  `docs/plans/2026-07-07-envelope-e7-config-diversity.md` §F-C1, the F-S1 sheet it modifies
  (`docs/plans/2026-07-07-fs1-doctype-aware-period-lock.md`) for the semantics held constant.

## 0.10.1 — 2026-07-07

**F-C2: the single-op wrong-books TOCTOU belt the cascade path already had, mirrored into
`_governed_write` — a new refusal, deny-biased, source-found by the E7 config-diversity scout
pass (`docs/plans/2026-07-07-envelope-e7-config-diversity.md` §F-C2).** `_governed_write` (the
real submit/cancel EXECUTE path) reads the freshly-fetched doc's own `company` for the period-lock
read, but never re-compared it to the pinned target's `company` at execute — the plan-time gates
(`_tool_plan_submit`/`_tool_plan_cancel`) already made that comparison, but nothing re-ran it at
the write itself. The only thing standing between "plan built for Company A" and "execute lands on
a doc now showing Company B" was `check_fresh`'s `modified`-equality — an IMPLICIT protection that
holds only if changing a doc's `company` always bumps `modified`, which a
`db_set(update_modified=False)`/raw-SQL/patch does not. **The codebase had already fixed this exact
class for cascade cancel** (`tools.py` ~1653, `_cascade_books_gate` — landed with cascade cancel
itself, 0.7.0/2026-07-03, closed in review before that merge per this file's own 0.7.0 entry, and
covered by `test_cascade_execute_refuses_wrong_books_dependent_toctou`), but the belt was never
mirrored into the single-doc spine, and no test drove it — an asymmetric gap between two paths the
same module already knew how to close.

- **The fix**: `_governed_write` now re-checks `target.company` against the freshly-fetched doc's
  own `company` immediately after the doc read, for both submit and cancel — the identical
  snapshot every other execute-time gate in this function already validates against, never a new
  read and never the plan's stale company. Placed FIRST among the execute-time belts (ahead of the
  Journal Entry voucher-type/balance checks and the closed-books/`red_line` check inside
  `governed_submit`): a document posting to the wrong company's books is refused on that fact
  alone, before any doctype-specific or date-range belt gets a chance to characterize the same
  write as merely "unbalanced" or "locked." Same `stage="plan"` label and wording as the plan-time
  check and `_cascade_books_gate` — the violation is "this write is not what was planned," not a
  tier of its own.
- **No pin, no check**: `target.company` unset (the documented unpinned posture, `registry.py`)
  still accepts any company's document at execute, exactly as it does at plan — this closes a gap
  in the pinned path, it does not narrow the unpinned one.
- **Deny does not spend the marker.** The belt sits before `store.get_marker`/`governed_submit`
  even run (same tier as `check_docname`/`check_op`/`check_doctype` above it) — a refusal here
  never reserves or claims the marker, so it stays exactly as minted (`live`), the same
  never-touched posture those pre-flight gates already have. This is distinct from the
  execute-*failure* release path (a failed `submit_document`/`cancel_document` call explicitly
  releases a marker it had already claimed) — this belt fires earlier, before a claim is ever
  attempted, so there is nothing to release.
- **Version-independent**: no ERPNext version-specific behavior is touched (compare F-C1, the same
  scout pass's other finding, which is v15/v16-sensitive) — provable now, no bench version pin
  needed. **Live-proven same evening (`SCOPED-TOKEN-PROOF.md` PHASE U):** a Company-A draft planned
  + minted, drifted to Company B via `db_set(update_modified=False)` (`modified` untouched — the
  shape `check_fresh` can't see), submit under the stale plan+marker **refused** `stage: plan —
  … wrong books`; docstatus stayed 0; the marker survived `live` and committed 0→1 once the company
  was restored (deny does not spend consent). Chain 82→84, zero new orphans.
- Pinned by 8 new tests (`test_tools.py`, `TestGovernedSubmit`/`TestGovernedCancel`): the TOCTOU
  headline for both submit and cancel (plan while same-company, mutate company before execute,
  `modified` untouched — refused, marker stays `live`, nothing written), the ordinary same-company
  path proceeding unaffected (regression), the unpinned-target posture unaffected, and the
  deny-biased ordering against `red_line` (a simultaneous company mismatch and closed-period lock
  refuses wrong-books first). **667 green** (broker) + 270 (guard, untouched — this is a
  spine/security change scoped entirely to the broker's own execute path, no guard-side surface
  touched).
- **Semver classification (deliberate, not incidental):** PATCH. This closes a gap in an existing
  security gate to match a sibling that already had it — hardening, not a new capability (contrast
  F-S1/0.10.0, a minor, which changed *what* the closed-books gate matches). No new tool, argument,
  or user-facing capability; the only behavior change is that a wrong-books drift that should never
  have been permitted is now refused. Classed patch per the workspace doctrine (patch = fix/
  hardening). Redteam raised the question; recorded here as the answer.
- Redteam pass (correctness/bypass lens + mechanical): **clean** — belt reads the fresh doc not the
  plan, `None`/absent company denies (deny-biased), unpinned-target is a pure skip of only the
  company check (all other belts still apply), marker verified `live`-after-deny against the actual
  store code, symmetric with `_cascade_books_gate`. No fail-to-refuse or bypass found.

## 0.10.0 — 2026-07-07

**F-S1: the Accounting Period lock is now doctype- and date-range-aware — a deliberate
GATE-LOOSENING, done as its own increment with its own redteam.** 0.9.8 named the E6 over-refusal
without fixing it (the Accounting Period read was doctype-blind: it refused every doctype past the
latest period's `end_date`, for every doctype, regardless of what that period actually closed).
This increment closes that gap so the broker matches ERPNext's own enforcement instead of
over-refusing past it — the pin sheet (`docs/plans/2026-07-07-fs1-doctype-aware-period-lock.md`)
is the design contract; the two-scout source read behind it confirmed ERPNext enforces the
Accounting Period lock in TWO places (`validate_accounting_period_on_doc_save` on submit,
`general_ledger.make_reverse_gl_entries:718` on **cancel** — the doc-level hook never fires on
cancel, but the GL-level check re-runs there), and that overlap between two enabled periods in one
company is impossible (`validate_overlap`).

- **`get_period_locks(company)` → `get_period_locks(company, doctype, posting_date)` — both new
  params REQUIRED, no default.** A default would silently reintroduce doctype-blindness; a call
  missing either is a `TypeError` at build time, never a doctype-blind read at run time (F5).
  Frozen (`Company`/`Accounts Settings`) and PCV reads are **byte-for-byte unchanged**.
- **The Accounting Period read becomes a two-step, matching ERPNext's own rule exactly**: (1) LIST
  periods for `company`, `disabled=0`, and a range that CONTAINS `posting_date`
  (`start_date <= posting_date <= end_date`, both ends inclusive); (2) for EACH hit — never just
  the first, equal-or-stricter than ERPNext's own unordered first-row read — a full-document GET
  (the list endpoint never expands child tables, the same two-step `get_active_workflows` already
  uses) reads `closed_documents`. Any row with `closed=1` and `document_type` equal to `doctype`
  (verbatim) sets `closed_period_until` to that period's `end_date`; otherwise the key is absent.
  **Newly allowed**: a posting dated before a containing period's start, a doctype the period
  doesn't close, and a `disabled=1` period — all three previously refused past the latest period's
  end date regardless. **Still refused, exact-boundary on both ends**: a posting inside an enabled
  period that closes its doctype (`== start_date` and `== end_date` both refuse, one day outside
  either end allows — matches ERPNext's inclusive `BETWEEN`).
- **Deny-bias extended, not relaxed.** An unreadable LIST *or* item GET raises (never "assume
  open"). Malformed period/child data — a non-ISO `start_date`/`end_date` on a hit, a
  `closed_documents` row missing `document_type`, or a `closed` value that isn't a clean
  `0`/`1`/bool (including a missing key) — raises too, the same class as unreadable, never
  skip-and-allow. `posting_date` itself is validated ISO **before** any network call (refuses a
  malformed date without spending a round-trip). A match found partway through the periods/rows
  never short-circuits validation of the rest — a later malformed row still denies.
- **Deliberately NOT modeled: `exempted_role`** (a per-period role that lets a raw ERPNext seat
  holding it bypass the lock entirely). This broker does not inherit that bypass — a seat holding
  the exempted role could act directly against the bench and succeed where this broker refuses.
  Equal-or-stricter, disclosed in `get_period_locks`'s own docstring, not a bug. No `frappe.flags`
  bypass exists to model; amend gets no exemption; `from_repost` is still checked by ERPNext's own
  path.
- **Cancel parity**: `_governed_write` threads the identical (company, doctype, posting_date)
  triple through for both `submit` and `cancel` (posting_date comes from the already-fetched
  `doc`, never a new network read) — matching ERPNext's own cancel-path enforcement
  (`general_ledger.make_reverse_gl_entries:718`), not exceeding it. `cascade.run_cascade`'s
  per-node `locks_for` call grew the same two params, reading them off the same node dict its
  `check_fresh`/`check_red_line` calls already use. `plan_submit`/`plan_cancel`'s disclosure
  (`_plan_closed_books_risk`) now calls `get_period_locks` with the SAME triple the execute path
  will use — "never drifts" is calling the identical function with identical arguments, not merely
  reusing `check_red_line`.
- **Guard scope: NO new grant.** List and item GET of `Accounting Period` both classify to the
  same `("resource", ("Accounting Period", "read"))` target (`pacioli_guard/scope.py`
  `_classify_full`) — an existing credential already scoped for the (previously single-call) list
  read covers the new item GET too.
- **Doctor**: `probe_accounting_period_read` now exercises the item-GET leg too, when at least one
  period exists (readability of the row the lock actually reads) — a 403 there gets its own remedy
  naming the period it tried to read. Empty-bench (zero periods) still PASSES with the original
  single finding.
- Pinned by 29 new tests (`test_erpnext.py`'s doctype-aware `TestPeriodLocks*` classes — F1 newly-
  allowed, F2 exact-boundary, F3 multi-period, F4 deny-bias, F5 no-silent-blindness; `test_tools.py`
  F6/F7 thread-through + disclosure-parity; `test_doctor.py`'s item-GET probe). **659 green**
  (broker) + 270 (guard, untouched).
- **Gate-loosening redteam ran pre-ship (3 lenses), 2 real catches fixed:** (MEDIUM) the cascade
  test fake never recorded `locks_for`'s call arguments — an argument-order swap at the call site
  (three plain strings) would have been a silent no-match → allow with the whole suite green; the
  fake now records the `(company, doctype, posting_date)` triple and the multi-node test asserts
  it per node. (LOW) a `{"data": null}` Accounting Period LIST body crashed with a bare
  `TypeError` instead of the structured deny — now an explicit `ErpnextError` ("cannot verify the
  closed-period lock, refusing"), same class as unreadable. The hunt's headline came back clean:
  no ERPNext-refuses-broker-allows input was found (NULL period dates can't exist in saved data —
  both `reqd:1`; GL `voucher_type` ≡ doctype by ERPNext construction; same-company overlap
  impossible even for disabled periods). **Bench proof: PHASE T ran same day
  (`SCOPED-TOKEN-PROOF.md` PHASE T) — the flip live**: the exact PHASE S P3 fixture the 0.9.8
  broker refused planned clean and committed 0→1 under 0.10.0; still-refused held on BOTH
  boundary days in the TOCTOU shape; a disabled-period posting committed; doctor's item-GET
  probe green naming the period it read; chain 76→82, zero new orphans.

## 0.9.8 — 2026-07-07

**Period boundaries made honest and disclosed early (envelope E6 — the core inclusivity holds;
the Accounting Period read's over-refusal is now named, not hidden).** ERPNext is inclusive on
every closing boundary (`posting_date <= frozen`, `<= pcv_end`, `start <= date <= end`); the
broker already matches it with a uniform `<=` refuse-on-tie for the frozen-till and PCV dates
(`plan.py:185`). This ships the safe half of E6; the doctype-aware Accounting Period correctness
fix is a gate-loosening redesign tracked separately as F-S1.

- **Docstring honesty:** `get_period_locks` no longer claims a "closed" Accounting Period filter
  it never applied. It now states plainly that it reads the latest period `end_date` doctype-blind
  — so it OVER-refuses relative to ERPNext (which blocks only inside a specific period's range,
  when that period is `disabled=0` and *that doctype* is checked `closed=1`). Fail-safe (never
  writes into a closed book), but it can refuse legitimate postings; the correct doctype-aware
  fix is F-S1. The same lie in the module docstring was corrected too.
- **Exact-boundary tests:** `posting_date == boundary` → refuse, one per lock source (frozen,
  PCV, Accounting Period), plus one-day-after allowed — the core inclusivity the whole leg
  asserts had no exact-tie test before.
- **Plan-time closed-books disclosure:** `plan_submit`/`plan_cancel` now read the locks and flag a
  posting already inside a locked period BEFORE consent is minted — reusing the SAME
  `check_red_line` the execute gate runs, so the warning can never drift from the refusal. The
  memorandum warns instead of letting the human discover the refusal at submit. Disclosure only;
  the execute-time gate still enforces, TOCTOU-fresh. (A source that is unreadable now denies the
  plan too — a new refusal path, but only for a source that was already deny-on-unreadable at
  execute; never a loosening.)
- **Doctor probes:** Period Closing Voucher + Accounting Period readability (required-read, 403 =
  FAIL + remedy) — both reads `get_period_locks` needs, previously undiagnosed. Both URLs
  percent-encode the doctype name from the start (the 0.9.7 raw-space class does not recur).
- Pinned by 30 new tests. **630 green** (broker) + 270 (guard). Bench proof: **E6 PHASE S ran
  2026-07-07, all 6 pins held live** (`SCOPED-TOKEN-PROOF.md` PHASE S) — refuse-on-tie proven in
  the TOCTOU shape for frozen + PCV, draft PCV non-locking, the AP over-refusal documented live
  with a same-seat control (F-S1 evidence), the plan-time flag verbatim pre-consent in the AP
  case. Window finding recorded for F-S1: frozen/PCV dates 417 ERPNext's own ledger preview, so
  the plan-time flag's live-reachable surface is exactly the doctype-blind AP case.

## 0.9.7 — 2026-07-07

**Fixed — the Accounts Settings probe never worked against a real bench (envelope E5, found live
in the PHASE R window, its FIRST doctor call).** The 0.9.6 probe built its URL by raw f-string —
`/api/resource/Accounts Settings/Accounts Settings` — with the space unencoded. `urllib` rejects
that ("URL can't contain control characters") before the request even leaves the box. Company and
Workflow never hit it (no space in the name), and the unit test's fake transport never exercised
a real URL — so a probe that could not possibly succeed shipped green.

- The path segment is now percent-encoded (`urllib.parse.quote(..., safe="")`, the same way the
  erpnext client already quotes doc names). The test now asserts the URL contains
  `Accounts%20Settings` and NO raw space — a fake transport that ignores URL validity can't mask
  this class again.
- The exact "a wrong result fixes the code, never the assertion" loop, this time on the campaign's
  own tooling: the bench caught a broker defect the unit tests structurally could not. **600 green.**

## 0.9.6 — 2026-07-07

**Added — `pacioli doctor` now probes Accounts Settings readability (envelope E5, found from
source).** A Journal Entry `plan_cancel`/`plan_cascade_cancel` reads
`unlink_payment_on_cancellation_of_invoice` for its blast-radius disclosure, and an unreadable
settings doc RAISES — refusing the whole plan. This was the same required-read class doctor
already probes for Company (frozen-books) and Workflow (SoD gate), but it had no probe: an
operator would only discover the gap live, mid-plan.

- New `probe_accounts_settings` (the required-read inversion: 403 = FAIL with remedy), narrower
  blast radius than Company/Workflow — it names that only Journal Entry *cancels* refuse, other
  doctypes are unaffected. Readable reports the live unlink state (ON/OFF). Pinned by 8 tests
  (`TestProbeAccountsSettings` + a not-ready end-to-end). **600 green.**
- **Recorded residual (not a false-allow, advisory only):** the unlink setting is read at PLAN
  time, never re-checked at execute — if an operator flips it between `plan_cancel` and minting
  the marker, the disclosed rationale can go stale relative to the live cancel. A re-read at
  execute is a heavier change; deferred with this named rationale rather than bloating the flag.

## 0.9.5 — 2026-07-07

**Fixed — the memorandum now says whether a credit note actually settles its original (envelope
E4, found LIVE in the PHASE Q window).** Both mapper-built returns on the bench carried
`update_outstanding_for_self=1` — ERPNext's return mapper sets it by default — so the return's
receivable rows posted against the return ITSELF: the return held its own −100 credit and **the
original invoice's outstanding never moved** (100, Unpaid) until a separate payment
reconciliation would allocate them. Consent to "a credit note against X" is not consent to "X is
settled," and 0.9.4's memorandum never said which shape it was.

- `plan_submit`/`plan_cancel`/cascade-per-node: a return WITH `return_against` now discloses the
  settlement shape from the doc's own field — either "does NOT settle <original>: …the credit
  sits on this return until a separate payment reconciliation allocates them" or "posts against
  <original> — the original's outstanding is reduced by this reversal."
- Found, fixed test-first, and re-proven live in the same bench window (the E1 pattern): the flag
  fired verbatim on the stock-return leg under the rebuilt wheel.
- Pinned by 3 new tests (`TestReturnDisclosure`). **592 green.** The reconciliation-allocation
  leg itself (settling a self-outstanding return) is E5 territory, recorded in the campaign doc.

## 0.9.4 — 2026-07-07

**Fixed + disclosed — returns, credit notes, and the POS till (envelope campaign E4, found from
source before the bench opened).** The broker had zero return/POS awareness; the generic spine
mostly carries them (the projected GL is ERPNext's own preview, stored opaque — a credit note's
column-swapped rows and a POS doc's inline cash legs flow through untouched), but one disclosure
was backwards and three ERPNext traps went unnamed.

- **Fixed: the stock disclosure pointed the wrong way on a return.** The cancel flag said items
  "return to their warehouses" — backwards for cancelling a return, where stock that came home
  goes back out. Both branches are now sign-aware: a negative-qty movement is named as inbound
  (a return receipt) on submit, and the cancel flag names the reversal's real direction.
  Positive-qty wording is byte-identical to before (pinned by exact-equality tests).
- **New: return disclosures** (`is_return`, doctype-agnostic, read from the draft's own fields —
  no new bench read): every credit note named as a reversal; a **free-standing credit note**
  (no `return_against`) flagged with exactly which ERPNext consistency checks will NOT run for
  it (over-return protection, exchange-rate match, receivable-account match, posting-date
  ordering — all gated on `return_against` in `sales_and_purchase_return.py`); **mixed-sign
  item rows** flagged (only negative rows receive ERPNext's return checks).
- **New: POS disclosures** (`is_pos`): payments rows summarized; the coming ERPNext refusal
  disclosed when payments are empty on a positive total; the zero-total waiver named; and the
  **partial-payment gap** flagged — ERPNext enforces full payment only for `is_created_using_pos`
  docs, so a bare `is_pos=1` doc can post with a shortfall left outstanding (tolerance 0.005 so
  float dust never reads as a shortfall — the JE balance-check precedent). Cancel notes the
  inline payment GL legs reverse too.
- All three run on plan_submit, plan_cancel, AND per-node in the cascade memorandum
  (docname-prefixed). Disclosures only — never gates.
- Pinned by 25 new tests (`TestUpdateStockDisclosure` sign cases, `TestReturnDisclosure`,
  `TestPosDisclosure`). **589 green.** Bench proof staged as envelope E4 PHASE Q
  (`docs/plans/2026-07-07-envelope-e4-returns-pos.md`) — headline pin: does a submitted credit
  note guard its original from a quiet cancel (`return_against` in the blast radius).

## 0.9.3 — 2026-07-07

**Fixed — the cascade never learned E1's lesson, and its memorandum was quieter than the
single-op one (envelope campaign E3, found from source before the bench opened).** Three gaps,
all in the cascade path, all fixed test-first; a fresh redteam pass on the fix then caught a
fourth (a CRITICAL in the fix itself) pre-ship.

- **Per-node transition confirm (the E1 rule, ported):** `run_cascade` recorded every node
  `committed` on the cancel call returning — no docstatus readback at all, the exact
  claim-more-than-reality shape 0.9.1 fixed for the single-op spine. Now each node's response
  must show docstatus 2; anything else records **`unconfirmed`** (never `committed`),
  **fail-stops** the run, and **spends** the marker even on a first node — an unconfirmed
  response means the act may already be in motion server-side, and one grant must never initiate
  two acts. When the response carries no docstatus, the glue reads the document back through the
  existing read path — never a new surface.
- **Redteam catch (CRITICAL, empirically reproduced): a throwing readback released the marker
  for an act in flight.** If the cancel succeeded but the confirmatory readback threw (timeout,
  transient 5xx), the exception fell into the generic failure path, which on a no-progress run
  releases the marker. The readback now degrades instead of raising: outcome `unconfirmed`,
  marker spent, the receipt and the stop reason carry the readback error so a reconciler sees
  "real state unknown, readback failed" — not the queued-write guess. (Recorded residual, its
  own increment: an exception from the *mutating call itself* is the same ambiguity and still
  releases on a no-progress run, single-op and cascade alike; the honest fix is a
  refusal-vs-transport-error taxonomy in the client layer. **Closed in 0.10.4** — see that entry's
  transport taxonomy.)
- **Per-node disclosure parity:** `plan_cascade_cancel` per-node flags were only future-date +
  no-live-GL — a Journal Entry reached as a cascade node got none of the disclosures single-op
  `plan_cancel` always gives. Every node now gets its doctype-appropriate flags, docname-prefixed:
  the EG auto-cancel note + the unlink setting for JE nodes (the Accounts Settings read happens
  once per graph, not once per node), the physical-stock reversal for `update_stock` docs, and a
  Payment Entry node's settled references (which invoices revert to unpaid).
- **The flags are now actually visible:** the cascade plan response never included a
  `risk_flags` key at all — computed, stored, and returned to no one. It does now, matching
  `plan_submit`/`plan_cancel`.
- Pinned by 20 new tests (`RunCascadeUnconfirmedTest`, `TestCascadeCancelConfirmation`,
  `TestCascadePlanRiskFlags`). **564 green.** Bench proof of the new behavior is staged as
  envelope E3 P7 (`docs/plans/2026-07-07-envelope-e3-eg-cascade.md`).

## 0.9.2 — 2026-07-07

**Fixed — the memorandum told half the story on stock-touching documents (envelope campaign E2,
found live).** A Sales/Purchase Invoice with `update_stock` set moves **physical stock** on
submit: the stock ledger is written alongside the GL. On the perpetual-inventory bench the
projected GL did include the valuation rows (COGS/Stock In Hand) — the money story was complete —
but nothing disclosed the *movement itself* (items, quantities, warehouses), and with perpetual
inventory disabled the movement leaves **no trace in the GL preview at all**: consent could be
minted blind to an inventory decrement.

- `plan_submit` and `plan_cancel` now disclose the physical movement whenever the doc itself
  carries a truthy `update_stock`: item rows summarized (first 5 + honest "and N more"), read from
  the draft's OWN items rows — never a new bench read (the JE-balance source discipline), no new
  credential surface. The flag names why the GL preview alone can be blind to it. Cancel discloses
  the reversal.
- Doctype-agnostic by construction: JE/PE and plain billing invoices carry no `update_stock` —
  no-op, never a branch on doctype shape.
- Pinned by `test_tools.TestUpdateStockDisclosure` (4 tests). 544 green.
- Deferred (recorded in the campaign doc): valuation-level stock preview via ERPNext's native
  `show_stock_ledger_preview` would need a new curated bare-method grant in the guard —
  its own increment if wanted.

## 0.9.1 — 2026-07-07

**Fixed — the book could claim more than reality (envelope campaign E1, found live).** ERPNext
queues a **>100-row Journal Entry** submit/cancel to a background worker
(`JournalEntry.submit`/`.cancel` override the base method past 100 accounts rows); frappe answers
200 with the doc still at its **pre-transition docstatus**, and the spine recorded a `committed`
outcome for a transition the response never showed (the worker made it true ~28s later on the
bench — a failed worker would have left a committed receipt for a write that never happened).

- `spine.governed_submit` now **confirms the transition from the execute response**: the returned
  `docstatus` must equal the transition's end state (`0->1` → 1, `1->2` → 2; a response without a
  docstatus cannot confirm anything — deny-biased). On mismatch the outcome is recorded
  **`unconfirmed`** (a new status, not `committed`, not `failed`), the tool returns
  `ok: false, stage: "unconfirmed"` with the queue cause named, and — because only a `committed`
  outcome finalizes an intent (`prove.orphans`, unchanged) — the write **stays in the reconcile
  sweep** until checked against the document's real docstatus.
- **The marker is SPENT on `unconfirmed`** (committed, not released): consent initiated an
  irreversible act that is in motion server-side; releasing the grant would let one marker
  initiate a second act.
- `plan_submit` on a >100-row Journal Entry now **discloses the queue upfront** (a risk flag on
  the memorandum, before any consent is minted).
- Pinned by `test_spine.TestUnconfirmedOutcome` (5 tests: mismatch → unconfirmed; marker spent;
  missing docstatus deny-biased; cancel checks its own end state; matching docstatus still `done`)
  and two `test_tools` flag tests. 540 green.

## 0.9.0 — 2026-07-06

> **Update 2026-07-07:** the Gate 10 bench proof this entry was pending on ran and held — JE
> submit 0→1 / cancel 1→2 live via `frappe.client.submit`/`.cancel` (transport verbatim in the
> bench request log), SI/PI/PE regression clean, cascade-with-JE completes 2/2. Record:
> `SCOPED-TOKEN-PROOF.md` PHASE M.

**Journal Entry submit/cancel: BLOCKED → built (knowledge-pinned, pending Gate 10 bench proof).**
Closes the PHASE L blocker (0.8.1): `JournalEntry` overrides `submit()`/`cancel()` without
`@frappe.whitelist()`, 403ing the broker's one *original* guard-scopeable submit shape
(`run_method=submit` on the URL-path doc-method surface). Journal Entry now submits/cancels via
`frappe.client.submit`/`.cancel` instead — the generic, doctype-in-body RPC surface — which is
only safe to enable because `pacioli_guard` 0.5.0 closed its own matching residual: it now parses
the doctype out of that RPC's body (`scope.body_scoped_target`) and enforces the credential's
per-doctype `<DocType>.submit`/`.cancel` grant on it exactly as strictly as the URL-path shape.
**Hard precondition, not optional:** this broker path is unsafe to run against a credential still
on guard <0.5.0 (or with `body_scoped_target` disabled) — it would submit/cancel Journal Entry
through a doctype-BLIND method grant. Sales Invoice, Purchase Invoice, and Payment Entry are
**unchanged** — they stay on the proven `run_method` transport (neither overrides
`Document.submit`/`.cancel`), selected per-doctype via a new `SUPPORTED_DOCTYPES[doctype]
["submit_via"]` flag (`"run_method"` vs `"client_rpc"`).

- `erpnext.py`: `submit_document(doctype, name, doc=None)` grows the `client_rpc` branch —
  `POST /api/method/frappe.client.submit` with `{"doc": doc}`. `doc` is REQUIRED on this branch
  (raises, fails closed, never silently falls back to the 403ing `run_method` shape) because
  `frappe.client.submit` reconstructs the document from the body server-side
  (`frappe.get_doc(doc); doc.submit()`) rather than re-reading it from the DB.
  `cancel_document(doctype, name)` grows the same branch for `frappe.client.cancel` — no doc body
  needed there (`frappe.get_doc(doctype, name); wrapper.cancel()` loads fresh from the DB;
  `doctype`/`name` are plain sibling params, not nested under `doc`).
- `tools.py`'s `_governed_write` passes the **same already-fetched `doc`** (the one
  `current_doc_version`/the closed-books check/the JE balance and Exchange-Gain-Or-Loss checks already
  validated) into `submit_document` — never a fresh re-fetch, which would reopen a TOCTOU gap
  between the freshness check and the actual write. All of the JE-specific gates that were already
  live-proven (PHASE L) — the independent debit==credit balance check, the
  Exchange-Gain-Or-Loss/system-reserved-voucher-type refusal, the Workflow-SoD gate, freshness,
  the closed-books check, the marker/consent chain — run identically and BEFORE this write; nothing about
  them changed.
- Nothing about SI/PI/PE's request shape, tests, or proven behavior changed — pinned by
  `test_erpnext.py::TestSupportedDoctypesConfig::test_only_journal_entry_uses_client_rpc` and the
  unmodified `TestSubmit` SI/PI submit-shape tests asserting they stay on `run_method`.
- **Knowledge-pinned, NOT live-verified.** `frappe.client.submit`/`.cancel`'s param shapes and
  `message`-envelope return contract are read from frappe source
  (`frappe/client.py`), matching guard's own pin for the same RPCs. Live falsification — JE submit
  0→1 through the scoped credential; guard denying an out-of-scope body-doctype submit; SI/PI/PE
  behavior unchanged — is **Gate 10**, staged in `GO-LIVE.md`, next armed bench window.
- **Hard precondition on the guard side:** requires `pacioli_guard` ≥ 0.5.0 (body-doctype scoping).

## 0.8.1 — 2026-07-06

**Bench-hardening from the Gate 8 / Gate 9 / PHASE J live run (real ERPNext v16 bench). Payment
Entry is now LIVE-PROVEN; Journal Entry submit/cancel hit a real ERPNext incompatibility and stays
knowledge-pinned with the blocker documented.**

**Fixed — cascade `fetch_linked` shape mismatch (found live).** ERPNext's real
`get_submitted_linked_docs` returns each dependent in frappe's native shape
(`{"doctype", "name", …}`), but `build_cascade` keys every node on `docname` — so the first real
dependent raised `KeyError: 'docname'`. The pure-core cascade tests fed fakes already in `docname`
shape, so this client-adapter seam had no live-shape coverage (the same class as the Gate 4
server.py adapter gap). A new `_cascade_fetch_linked` normalizes `name → docname` at both the plan
and execute-time re-discovery seams; `test_tools.py` pins the real frappe wire shape.

**Live-proven — Payment Entry (Gate 8), the whole vertical.** `plan_submit` → real projected GL
(party-ledger leg naming the settled invoice) → `submit_payment_entry` docstatus 0→1 with the
**referenced invoice's own `outstanding_amount` moving 100→0** (the Payment Ledger cascade, not the
PE doc alone) → cross-doctype refusal both directions → `plan_cancel` blast-radius disclosure
(references + `against_voucher` per GL row) → `cancel_payment_entry` 1→2 with the invoice
outstanding reverting 0→100 and all 4 GL rows `is_cancelled` → `prove_verify` 4 receipts, zero
orphans. README flipped to live-proven for Payment Entry.

**KNOWN LIMITATION — Journal Entry submit/cancel not reachable via the guard-scopeable shape.**
ERPNext's `JournalEntry` overrides `submit()`/`cancel()` (to background-queue >100-row entries) and
the overrides drop frappe's `@frappe.whitelist()` decorator. The broker's one submit shape —
`POST /api/resource/<doctype>/<name>?run_method=submit`, the *only* form `pacioli_guard` can scope
per-doctype — is rejected by frappe for Journal Entry (`… .submit is not whitelisted`). Sales
Invoice, Purchase Invoice, and Payment Entry use the whitelisted base `Document.submit`, so they
are unaffected. Journal Entry's **governance legs are live-proven** (plan_submit projected GL; the
Exchange-Gain-Or-Loss refusal; the independent debit==credit balance check — both fired live), but
its **submit/cancel stay knowledge-pinned** pending a design decision on the submit transport for
override-doctypes (which necessarily trades against per-doctype guard-scopeability — John's call,
tracked in `GO-LIVE.md`). Do not call Journal Entry submit/cancel live-proven.

## 0.8.0 — 2026-07-06

**Breadth: Payment Entry + Journal Entry — built, NOT live-verified. Plus one correctness fix
that reaches every doctype already shipped, not just the new ones.** **18 → 28 tools.**

**Fixed — the v16 frozen-books read source.** `get_period_locks` read only the legacy
`Accounts Settings.acc_frozen_upto` for the frozen-till-date boundary. ERPNext v16 migrated that
field onto `Company.accounts_frozen_till_date`
(`erpnext/patches/v16_0/migrate_account_freezing_settings_to_company.py`; the field is absent
from `accounts_settings.json` on a v16 bench) — the real enforcement
(`general_ledger.check_freezing_date`) reads `Company`, not `Accounts Settings`, so this lock was
silently always-absent against a v16 bench for **every** doctype already shipped (Sales Invoice,
Purchase Invoice), not just the doctypes landing in this release. `get_period_locks` now reads
**both** the Company doc (the v16 source) and Accounts Settings (the legacy v15 field, kept for
an unmigrated bench) and honors whichever date is **later** when both carry a value.
**BREAKING for existing scopes:** reading `Company` is a new scope requirement — an existing
broker credential scoped before this fix will have `get_period_locks` raise (deny-biased, never a
silent "no lock") on every plan/submit/cancel until Role Permission Manager grants `Company` read
to the broker's role. `pacioli doctor` gains `probe_company_read`, mirroring the existing
workflow-read probe's deliberate 403-is-FAIL inversion.

**Added — Payment Entry breadth (18 → 23 tools).** Five new siblings
(`get`/`list`/`submit`/`cancel`/`amend_payment_entry`) wrap the same generic handlers pinned to
Payment Entry (`party_field="party"`; an Internal Transfer payment carries no party at all,
surfaced as an absent field like any other doctype's missing value) — no duplicated logic, the
same recipe §3b already proved for Purchase Invoice. Two advisory disclosures, both read from the
draft's own cached `references` child rows (no extra bench call): `plan_submit` flags any
reference row with a nonzero `exchange_gain_loss` (ERPNext's own ledger-preview call creates AND
submits a real, separate Exchange Gain/Loss Journal Entry mid-preview, rolled back only at the
very end, and the projection never shows that JE's own GL rows) and any reference already at
zero/negative `outstanding_amount` (ERPNext itself only warns — `frappe.msgprint`, HTTP 200, no
exception); `plan_cancel` discloses the blast radius a single Payment Entry cancel carries — one
voucher can revert `outstanding_amount` on N invoices at once, listed by doctype/name/allocated
amount. `get_gl_entries`'s field list gained `against_voucher_type`/`against_voucher`
(doctype-agnostic, benefits Sales/Purchase Invoice reads too).

**Added — Journal Entry breadth (23 → 28 tools).** Five new siblings wrap the same generic
handlers pinned to Journal Entry — the first doctype with **no header-level party field at all**
(only per-line party in its `accounts` child table): `party_field=None`, and `list_journal_entries`
carries no `party`, no `status` (Journal Entry has none; `docstatus` is its only status signal),
and no `grand_total` (`total_debit`/`total_credit` stand in). **Journal Entry is also the first
doctype to earn its own gate, not just ride the existing five pillars unchanged** — its ERPNext
controller carves out a real, source-confirmed bypass of Pacioli's founding law ("no debit
without a credit") that Sales/Purchase Invoice and Payment Entry never could:
`voucher_type == "Exchange Gain Or Loss"` is **refused outright** at both `plan_submit` and
`submit_journal_entry` (two independent ERPNext gates skip the debit==credit check for exactly
this value, since it's meant to be produced only by ERPNext's own FX-revaluation tooling — not
refused at cancel, since ERPNext's own machinery routinely auto-cancels these as a side effect of
cancelling whatever they reference), and an **independent balance check** sums the draft's own
`accounts` child-row debit/credit fields itself — never trusting ERPNext's cached
`total_debit`/`total_credit` — at both `plan_submit` (before any marker can even be minted) and
`submit_journal_entry` (belt-and-suspenders). Two more advisory disclosures: a standing note that
Journal Entry's `on_submit`-only checks (cheque info, credit limit, invoice-discounting status)
are invisible to the native preview, plus a conditional missing-`cheque_no`/`cheque_date` flag for
Bank Entry (ERPNext-enforced) and Cash Entry (the broker's own precaution, worded honestly as
such); `plan_cancel` reads `Accounts Settings.unlink_payment_on_cancellation_of_invoice` (a new,
deny-biased read) and flags cancel's blast radius accordingly, plus a standing flag that
cancelling a Journal Entry auto-cancels any system-generated Exchange Gain Or Loss Journal Entry
that references it, with no separate consent.

**Cascade coverage.** Adding Payment Entry and Journal Entry to `SUPPORTED_DOCTYPES` automatically
flips any cascade node of either doctype from `"generic"` to `"modeled"` — `cascade.py`'s coverage
label is driven entirely by `SUPPORTED_DOCTYPES` membership, zero code change. The suite's own
placeholder examples of "not yet modeled"/"unsupported" doctypes (Journal Entry as the
unsupported-doctype exemplar, Payment Entry as the cascade `"generic"` exemplar) were repointed to
Delivery Note, which remains genuinely unmodeled.

**Honest limit, unchanged in kind from Purchase Invoice's own breadth entry:** every Payment Entry
and Journal Entry request shape here — including the Exchange-Gain-Or-Loss refusal and the
independent balance check — is knowledge-pinned from ERPNext v16 source reading, **not verified
against a live bench**. Live falsification of both paths is a distinct future bench gate (staged
in `docs/plans/2026-07-06-gates-8-9-phase-j.md`), not implied by anything in this entry. Version
bumped `0.7.0 → 0.8.0` (a minor bump — new features, no breaking removal — landed once,
deliberately, in this final integration commit for the increment). Full suite: 526 (was 446).

## 0.7.0 — 2026-07-03

**Cascade cancel — governing a whole submitted-dependent graph, built (NOT yet live-verified).**
Until now the broker governed a **leaf** cancel only: `plan_cancel` refused the moment any submitted
document linked to the target (ERPNext itself won't cancel a document with submitted dependents).
Two new tools now govern the cancel of a document **and** its dependents in one consent:
`plan_cascade_cancel` walks the full transitive dependent graph, topologically orders it (dependents
first, target last, any doctype, each node labeled `modeled` for Sales/Purchase Invoice vs `generic`),
refuses a cycle or a graph over `PACIOLI_CASCADE_MAX` (default 25), and records the whole ordered graph
as one plan; **one** human-minted marker authorizes exactly that frozen set. `cascade_cancel` re-checks
the graph is unchanged since planning, then executes: **preflight every node** (freshness + period-lock
closed-books check + the Workflow-SoD gate) BEFORE any cancel, then cancel in order and **fail-stop** on the first
failure — the result names exactly what was cancelled and where it stopped (`ok` is true only if ALL
cancelled). The single marker is spent iff at least one document was cancelled. It is non-atomic by
nature (each cancel commits individually; there is no rollback) and honest about it.

**The Workflow-SoD gate applies to every node.** A cascade runs
`workflow.check_submit_gate(..., "cancel")` for each node's doctype, so a document whose cancel the
single-op path refuses (a company's active Workflow governs it) can never be laundered through a
cascade — the whole cascade refuses, naming the node, workflow, and approving role, at both plan and
execute time. (Closed in review before merge: the first cut of the cascade path omitted this gate.)
The **wrong-books/company pin** is applied the same way: every node's `company` must match the pinned
target's, so a cross-company dependent can never be cancelled under a target's marker (also caught in
review — the first cut pinned only the target node; checked at plan and, against live bench data, at
execute for TOCTOU).

**Plan model / store.** `pacioli.plan.Plan` gained a `graph` field (the ordered node list; empty for
single-op plans); the `plans` table gained a `graph` column with a migration mirroring the `op`/`doctype`
migrations. New pure core `pacioli.cascade` (`build_cascade` discovery + `run_cascade` execution) — no
bench imports, fully unit-tested. **16 → 18 tools.** The `plan_cancel` blast-radius refusal — which
used to tell the agent the linked-graph case was out of scope — now points at the new tools. Bench
live-proof is a future gate — nothing is claimed "live-proven" yet.

## 0.6.0 — 2026-07-03

**Breadth: Purchase Invoice — built, NOT live-verified.** The tool surface now covers a second
doctype, riding every existing gate rather than adding new ones. **11 → 16 tools**: the five
`*_sales_invoice` tools keep their name, schema, and behaviour (every pre-existing Sales Invoice
test still holds — the only change is one clause in the `submit`/`cancel` *descriptions* noting the
new cross-doctype refusal), and five new `*_purchase_invoice` siblings (`get`/`list`/`submit`/
`cancel`/`amend`) wrap the SAME generic handlers pinned to Purchase Invoice — no duplicated logic. The four
generically-named doc-scoped tools (`plan_submit`, `plan_cancel`, `workflow_status`,
`request_workflow_transition`) gained an optional `pacioli_doctype` argument (default `"Sales
Invoice"`, unchanged behaviour when omitted); an unsupported doctype is refused before any network
call, naming the two that are: `pacioli.erpnext.SUPPORTED_DOCTYPES`.

**The security-critical addition — the plan is now bound to its doctype.** `pacioli.plan.Plan`
gained a `doctype` field (default `"Sales Invoice"`, back-compat) and `check_doctype(plan,
doctype)`, wired into `_governed_write` alongside the existing `check_docname`/`check_op` guards,
before the Workflow-SoD gate and the spine. A plan built for Sales Invoice can never authorize a
Purchase Invoice submit/cancel, or vice versa — proven in both directions (submit AND cancel) in
`test_tools.py`. The `plans` table gained a `doctype` column with a migration mirroring the
existing `op`-column migration (pre-breadth history backfills honestly as `"Sales Invoice"`).

**Genuine per-doctype differences, threaded explicitly:** `list_documents` takes a `party_field`
(`customer` for Sales Invoice, `supplier` for Purchase Invoice); `get_gl_entries` now filters on
`voucher_type` as well as `voucher_no` (closes a latent cross-doctype GL-read gap once two
doctypes can share one `GL Entry` table); `pacioli doctor`'s workflow-read probe now loops over
every supported doctype independently (a company's Role Permission Manager grant can differ per
doctype).

`pacioli.erpnext.ErpnextClient`'s five Sales-Invoice-baked methods were generalized to explicit
`doctype`-parameterized methods (`get_document`, `list_documents`, `submit_document`,
`cancel_document`, `get_doc_for_amend`, `find_amendments`, `create_amended_draft`); every call
site (including `pacioli.doctor`) was updated in lockstep. `ledger_preview`,
`get_submitted_linked_docs`, `get_period_locks`, `get_active_workflows`, `get_workflow_state`, and
`apply_workflow` were already doctype-generic and are unchanged. `guard/`,
`pacioli.consent`/`prove`/`workflow`/`amend`/`registry`/`runtime`/`server` are untouched — confirmed
doctype-agnostic. `pacioli.spine` got a **single one-line addition**: its intent receipt now records
`plan.doctype` (see below), so the ledger is self-describing per doctype on both intent and outcome.

**Honest limit:** every Purchase Invoice request shape is knowledge-pinned from ERPNext's
documented REST conventions (the same generic surface Sales Invoice already rode before Gate 5
confirmed it against a real bench) — it has **not** been verified against a live bench. Live
falsification of the Purchase Invoice path is a distinct future bench gate. Every receipt (submit,
cancel, amend, workflow-transition) carries `doctype` on both intent and outcome; the doctype is
*also* recoverable via `plan_id` from the doctype-columned plan store. None of this is the gate:
`check_doctype` enforces independently of what any receipt records.

## 0.5.0 — 2026-07-03

**CONSENT's second gate: Workflow-SoD** — riding a company's own ERPNext Workflow as
separation-of-duties law, never inventing new rules on top of it. Built from frappe source reading
(frappe/frappe branch version-15) and then **live-proven end-to-end against a real ERPNext v16
bench (2026-07-02, Gate 5)** — the version-15 shapes were introspected on the live v16 bench and
all held (`apply_workflow(doc, action)`, `has_approval_access`, `allow_self_approval` default
`"1"`, the Workflow/Transition/Document-State fieldnames, and `Workflow` being System-Manager-read
by default). Eight legs green: `workflow_status`, the `plan_submit` risk flag, **a valid minted
marker still refused at the workflow stage** (CONSENT is two gates), a broker-performed
non-approving transition, an approving transition refused broker-side, frappe's own self-approval
block firing on a direct `apply_workflow` (HTTP 417), cancel *not* over-blocked when the workflow
doesn't govern it (docstatus 1→2), and the receipt chain verifying with zero orphans. `pacioli
doctor`'s workflow-read probe was proven FAIL (403) → PASS across granting the required custom
read permission.

### Added
- **`pacioli/workflow.py`** (pure core, no frappe/I/O): `find_active` (0/1/>1 active workflows —
  ambiguous config is a named-refusal sentinel, never "pick the first"), `classify_transition`
  (a transition is `"non_approving"` iff its `next_state` maps to a `doc_status "0"` state row;
  anything else — unknown state, missing `doc_status`, `"1"`/`"2"` — is `"approving"`,
  deny-biased), `governs_op` (any active workflow governs `submit` outright; `cancel` only when
  the company's own config maps a state to `doc_status "2"`), `check_submit_gate` (the
  caller-side refusal, naming the workflow and the approving role(s)), `check_transition`
  (validates a requested action from the doc's current state, refusing an approving match and
  naming the human's role, or an undefined action naming the legal ones), `sod_report` (honest
  risk text when an approving transition permits self-approval — frappe's own
  `allow_self_approval` defaults to on; CONSENT surfaces this, it does not refuse it).
- **`workflow_status(name)`** — a new read-only tool: whether an active workflow governs
  this doctype, the document's current state, the legal transitions from here (each flagged
  approving/role/self-approval), and an honest SoD note. No workflow = `workflow_active: false`,
  not an error. Ambiguous config denies, naming every workflow found.
- **`request_workflow_transition(name, action)`** — a new write tool, the non-approving half:
  amend's no-marker precedent applied to a workflow move (reversible — a workflow-state change,
  never a `docstatus` change — so no consent marker; the intent+outcome receipt pair is still
  written durably around the call). Refuses any approving match, and any action undefined from
  the current state, naming the alternatives. Honest residual: no CAS on this path — two
  concurrent requests may both pass validation and both call `apply_workflow`; frappe's own state
  machine refuses the second once the document has moved off `current_state`, so the blast radius
  is a duplicate-attempt race, never a double-posting.
- **`ErpnextClient.get_active_workflows/get_workflow_state/apply_workflow`** — the transport:
  a name-then-full-doc read for active workflows (the list endpoint doesn't expand the
  `states`/`transitions` child tables), a configurable-state-field read (never hardcodes
  `"workflow_state"`), and `POST /api/method/frappe.model.workflow.apply_workflow` sending only
  `{"doctype", "name"}` in the doc body (the pinned server contract discards everything else).
  All three raise `ErpnextError` on any unreadable source — deny, never read as "no workflow".
- **`_governed_write`'s new caller-side gate** (`tools.py`): fetches the active workflow after
  `check_op`, before the doc/lock reads, and refuses per `check_submit_gate` for both `submit`
  and `cancel`. No workflow configured on the doctype = passes silently; both directions of the
  duality stay marker-governed exactly as before this change (regression-tested).
- **`plan_submit`** adds a `workflow-governed` risk flag (naming the approving role) plus a
  `workflow` info block in the response whenever an active workflow exists; a self-approvable
  approving transition gets its own risk-flag line. Ambiguous config is flagged, not refused —
  planning is a read; only the actual write is gated.
- Required scoping grant documented (README preconditions): the `Workflow` DocType is
  System-Manager-read-only by default, so the broker's role needs a custom Role Permission
  Manager grant to read it — until then every workflow-aware call raises (deny), never reads as
  "no workflow configured", the same house rule as the period-lock reads.
- **`pacioli doctor` gains a workflow-read probe** (online path; skipped by `--offline` like the
  method probe): GETs the active-workflow list for Sales Invoice. Readable — even empty — is a
  PASS; **403 is a FAIL** with the exact remedy (grant custom read permission on Workflow for
  the broker's role). This is the deliberate inversion of the method probe's 403-is-PASS rule,
  and the probe's own text says so: that probe checks a method the broker doesn't need (scoped
  tighter = the prescribed posture); this one checks a read the gate REQUIRES.

### Changed — ⚠️ upgrade note (breaking for existing scoped credentials)
- **Every governed `submit` and `cancel` now reads the `Workflow` DocType first** (the
  workflow-SoD gate source), and an unreadable gate source refuses — deny-biased, never read as
  "no workflow". Consequence, stated plainly: **an existing broker credential scoped before this
  build, WITHOUT the new custom read grant on `Workflow`, will have ALL governed writes denied
  after upgrading — whether or not any workflow is configured.** The migration step is the
  grant: Role Permission Manager → `Workflow` → read for the broker's role. `pacioli doctor`
  now probes exactly this and prints the remedy (see Added).

### Fixed (second independent redteam pass, on this increment before merge)
- **CRITICAL — a malformed single workflow body silently disabled the gate.** A full-doc read
  that came back null/empty flowed through `find_active`'s single-element branch unchecked, and
  both `check_submit_gate` (`is None`) and `governs_op` (falsy) then read it as "no workflow" —
  a submit with a valid plan+marker PROCEEDED past the gate. A truthy non-dict body instead
  crashed four tools with an uncaught AttributeError, not a structured deny. Fixed at both
  layers: `get_active_workflows` now raises on any full-doc body that is not a non-empty dict
  with a non-blank name (honouring its own deny contract), and the pure core gained a
  `Malformed` sentinel — `find_active` returns it for any non-workflow element (checked BEFORE
  Ambiguous, so garbage in a multi list is never masked as merely ambiguous) and every consumer
  (`check_submit_gate`, `workflow_status`, `request_workflow_transition`, `plan_submit`'s risk
  pass) refuses/flags it by name. `check_submit_gate` also gained a default-deny floor: only an
  affirmative `None` (no workflow configured) passes without a workflow; any other non-dict or
  empty input refuses.
- **MEDIUM — a transition lacking `allow_self_approval` read as "no self-approval risk".**
  frappe's field default is `"1"` (ON), so a missing key or a None value is the RISKY direction
  — plain truthy-normalisation inverted frappe's documented default. New
  `self_approval_allowed(transition)` maps missing/None to the frappe default; used by
  `sod_report` and `workflow_status`'s per-transition field. `truthy` stays generic and its
  docstring now says it carries no default.
- **HIGH — the two new tools accepted a missing required arg.** `workflow_status` with no
  `name` could return `ok: true` (a silent success on a schema-required argument). Both new
  tools now refuse a missing/blank `name` (and `action`) with a structured `stage: "request"`
  deny BEFORE any network call. The nine pre-existing tools are deliberately untouched.
- **LOW — `doc_status` comparisons normalised through one helper** (`workflow.doc_status`):
  stripped string, None/missing → `""` (never the string `"None"`), ints normalised (`0` reads
  as `"0"`); used by `classify_transition`, `governs_op`, and `workflow_status`'s
  current-state lookup. A provably-dead transition walk in `governs_op`'s cancel branch (it
  re-derived what the state scan directly before it already answered) was removed and the
  docstring corrected to match.
- The tool-layer test fake now honors `workflow_state_field` end-to-end: a workflow configured
  with a custom state field (e.g. `approval_state`) is proven read AND returned under the
  configured key by both new tools, never a hardcoded `workflow_state`.

### Honest limits (stated in `pacioli/workflow.py`'s module docstring and carried into the docs)
1. **Frappe does not enforce Workflow on a direct `docstatus` change** — `validate_workflow` only
   fires when the document's `workflow_state` field itself changes on save. This gate is the
   only thing stopping *this broker* from submitting around a company's configured approval
   chain; it cannot make ERPNext enforce the workflow against any other calling path (bench
   console, a script, a report button). Bench-side enforcement against every calling path is a
   named future increment, not implied here.
2. **Self-approval is not blocked by frappe by default** (`allow_self_approval` defaults to
   `"1"`). This gate does not refuse a self-approvable configured workflow — CONSENT enforces the
   company's own rules, it does not invent stricter ones — `workflow_status`/`plan_submit`
   surface the risk honestly instead.
3. Every frappe shape here — fieldnames, `apply_workflow`'s contract, the HTTP 417 error mapping
   — is knowledge-pinned from source-reading, not live-verified. No "live-proven" claim is made
   anywhere in this changelog entry.

`len(TOOLS)` pin test updated 9 → 11. 103 new tests (46 pure-core, 14 client-shape, 34
tool-layer — one of them a new schema-shape pin on `request_workflow_transition`'s no-marker
surface — and 9 doctor); the broker suite runs 273 → 376, all green.

## 0.4.0 — 2026-07-03 (unreleased)

Two features, one review pass: the **off-box PROVE anchor** (closes PROVE's biggest asterisk) and
**amend** (completes UNDO's arc). **Both live-proven on the bench (2026-07-02, GO-LIVE Gate 4 /
`SCOPED-TOKEN-PROOF.md` PHASE F):** amend created the corrected re-draft of the cancelled Gate-3
invoice (`amended_from` set, docstatus 0), a second amend refused naming the existing one, and the
amendment re-posted through the full plan → mint → submit — the whole submit → cancel → amend →
resubmit arc on one ledger (8 receipts, verifies, zero orphans). The anchor pinned the live head,
`anchor check` passed green, and a truncated copy of the ledger was caught (`count regressed`,
exit 1). Alpha dropped.

### Added — off-box PROVE anchor
- **`pacioli/anchor.py`** (pure core): a versioned JSON anchor record (`pacioli_anchor: 1`,
  target, head hmac, receipt count, ts); strict fail-closed `parse_anchor` (wrong marker,
  missing/extra/mistyped fields, non-hex head, bool-as-count, count/head inconsistency all
  refused); byte-stable `render_anchor`; deny-biased `compare` — a count that went DOWN or a
  head that moved is tampering, and on a grown chain the pinned hmac must still sit at its
  pinned position (prefix-commitment fixes all pre-pin history, even against a seal-key holder).
  No I/O, no clock, never sees the seal key.
- **`pacioli anchor write [--target] [--out PATH|-]`** — emits the pin, stdout by default (the
  operator carries it off-box — the tool says plainly it cannot do that part); refuses to pin a
  chain that does not verify. **`pacioli anchor check --in PATH|-`** — keyed chain verify + the
  anchor comparison, exit 0 only when both hold; nudges to rotate when an unpinned suffix exists.
- Honest claim, stated exactly (README PROVE row + Precondition 3): with a disciplined off-box
  pin, PROVE is tamper-evident against host-level truncation or rewrite **since the last pin** —
  the pin window stays unprotected, and the seal key stays on-box.

### Added — amend (UNDO's second half)
- **`amend_sales_invoice(name)`** — the ninth tool: the corrected re-draft after a governed
  cancel. A new DRAFT copied from the cancelled document with `amended_from` SET (one hop back)
  and docstatus forced 0, via the documented client-side flow (ERPNext has no native server
  `amend()`).
- **`pacioli/amend.py`** (pure core): `amend_payload` with the **strip-list documented as the
  security surface** — identity, audit stamps, state, settlement residue, every `_`-prefixed
  runtime key by rule, per-row child identity; child-table data survives. Honest limit stated:
  a fixed name/rule list, not a walk of per-field `no_copy` meta — the bench's validate is the
  backstop for an uncovered field.
- **Amend takes no marker, deliberately**: it creates a reversible draft (deleting it undoes it);
  the irreversible act stays `submit`, behind its own plan + human-minted marker — consent for a
  reversible act would dilute what the marker means. It DOES write the intent+outcome receipt
  pair (op `amend`, transition `2->0(draft)`), so one ledger shows the whole cancel → amend →
  submit arc. Gated: refuses an uncancelled source, an existing amendment (any docstatus, named),
  and a wrong-books company; a failed insert leaves an orphan intent, never a silent draft.
- Guard implication stated honestly (README preconditions): the amendment insert is a resource
  CREATE on Sales Invoice; guard resource grants are not verb-granular, so the existing grant
  admits it — the added surface is a reversible draft only.

### Fixed (independent fresh-eyes redteam of this release, before it ships)
- **CRITICAL — the live MCP server dispatched to an unbound name.** `server.py`'s `call_tool`
  closure called `pacioli.dispatch(...)` (a name never imported) instead of `broker.dispatch(...)`,
  so `pacioli serve` — the only agent-facing entrypoint — raised `NameError` on the first tool
  call. It hid because the whole suite drives `PacioliBroker.dispatch` directly and nothing
  exercised the MCP adapter. Fixed; the dispatch step is extracted to a testable `dispatch_tool`
  with a new `test_server.py` (the coverage that was missing).
- **HIGH — the off-box anchor verified and compared two separate reads.** `anchor write`/`anchor
  check` called `store.verify()` then `store.receipts()` as unguarded, independent reads, so
  `compare()` (and the emitted pin) could run against a snapshot the keyed verify never covered —
  a concurrent host-level writer (the anchor's own threat model) could slip a chain past both. New
  `BrokerStore.verify_snapshot()` returns the exact receipts it verified, from one read; both
  commands now use it. The pure crypto in `anchor.py`/`prove.py` was sound — the gap was purely the
  CLI wiring.
- Amend's missing-marker race is now disclosed as an honest residual in `amend.py` (a concurrent
  double-amend yields a duplicate *reversible* draft, reconciled by hand — never a double-posting).

### Changed
- Broker README: CONTAIN paragraph now points at the guard floor (kill switch + rate limit)
  instead of claiming CONTAIN is unbuilt; stale "increment 2 (pending)" anchor wording swept
  from prove/store/runtime docstrings; unused import dropped in `cli.py`.

## 0.3.0 — 2026-07-02 (unreleased)

**UNDO — the governed cancel, LIVE-PROVEN the same day** (`SCOPED-TOKEN-PROOF.md` PHASE E): the
full `plan_cancel → pacioli mint → cancel_sales_invoice` vertical ran on the real bench against
the very invoice the Gate-2 submit posted — the plan showed the exact GL rows to unwind, the
**cross-op refusal fired live** (the cancel marker presented to `submit` was refused, grant
untouched), cancel took **docstatus 1 → 2**, and the bench DB shows ERPNext's literal
equal-and-opposite reversing GL rows (all `is_cancelled`). Replay refused; the receipt chain
verified with exactly the submit pair + the cancel pair; zero orphans. The knowledge-pinned
linked-docs REST shape was falsified against the live bench and held.

### Added
- **`plan_cancel`** — PLAN for the unwind: requires docstatus 1, reads the posting's live GL rows
  (`GL Entry`, uncancelled only) as the projected reversal, and **refuses a non-leaf cancel** —
  if submitted documents link to this one, the deny names them; cascade cancel is deliberately
  not governed in this slice. An unreadable blast radius refuses (never reads as empty).
- **`cancel_sales_invoice`** — the second state-changing tool (docstatus 1 → 2), through the same
  gates as submit by construction (one shared spine path): wrong-books, docname, freshness,
  closed-books check (a freeze that closed over the original posting date blocks the unwind too), marker
  CAS, intent+outcome receipts. `run_method=cancel` on the item URL — the same guard-scopeable
  shape (`"Sales Invoice.cancel"`); never `adv_adj`, never a rewritten `posting_date`.
- **Plans are op-bound** (`op` column, migrated on open with pre-UNDO history backfilled as
  `submit` — which it all was): a consent marker minted for a cancel can never authorize a
  submit, or vice versa. Consent does not transfer across the duality.
- Credential scope for UNDO (README preconditions updated): + `Sales Invoice.cancel`,
  + `frappe.desk.form.linked_with.get_submitted_linked_docs`, + `GL Entry` resource read.

## 0.2.0 — 2026-07-02 (unreleased)

### Changed
- **License: MIT → Apache-2.0** (pre-any-release, sole author — no downstream affected). Matches
  the rest of the family (Proximo, Maude) and carries Apache's express patent grant, which is the
  right posture for a governance product businesses deploy on their books.

### Added
- **`pacioli doctor`** — read-only config & readiness checks (new CLI command, human-side only;
  deliberately NOT an MCP tool). Came straight out of running the fresh-install walkthrough as an
  end-user: credential resolution is deferred to call time, so the purely-local commands succeed
  on a half-configured install and the first bench call was where a missing secret surfaced.
  Doctor closes that gap up front. Checks: registry loads (targets + default named), credential
  references resolve (**values never shown**), state dir / per-target db / seal-key file mode
  (creating **none** of them — read-only, no side effects), and a one-GET live bench probe with
  `--offline` to skip it. Deny-biased with one deliberate inversion: a **403 on the probe is a
  PASS** (reachable + authenticated + scoped tighter than the probe = the prescribed posture),
  while **authenticating as Administrator is a FAILURE** — a superuser broker credential voids
  the trust spine (SPEC §2), and doctor refuses to call that install ready. Exit 0 only with zero
  failures.

## 0.1.0 — 2026-07-02 (unreleased)

**Live-proven.** The full governed vertical ran end-to-end against a real ERPNext bench
(Frappe 16 / ERPNext 16, sealed lab), authenticated as a scoped non-Administrator user under
`pacioli-guard` 0.1.1 — the exact deployment posture the SPEC prescribes. This is the proof the
`a1` was waiting on; nothing in the package changed except the version and the status wording.

### Proven live (each leg observed, HTTP against the bench, GL verified in the bench DB)
- `plan_submit` on a real draft Sales Invoice returned the **real projected GL** from ERPNext's
  native `show_accounting_ledger_preview` (balanced debit/credit pair), recorded bound to the
  doc's `modified` version. Nothing posted.
- A **bogus marker was refused** (structured deny, `stage: consent`) before any state changed.
- `pacioli mint` (the human side, out of band) minted a single-use marker for the plan_id;
  only its hash landed in the store.
- `submit_sales_invoice` under the marker took the draft **docstatus 0 → 1**; the bench DB shows
  the real, balanced, uncancelled GL Entry rows.
- **Replay refused**: the same plan+marker resubmitted was denied at the freshness stage (the
  document changed after planning — the deny-biased ordering fires before the marker check, so
  the GL a human consented to can never silently drift). Planning the now-submitted doc was
  refused (`not a draft`).
- `prove_verify` — the sealed receipt chain verified with exactly the **intent + outcome pair**;
  `prove_orphans` — none.
- Guard boundary re-confirmed on the same credential: in-scope reads (Sales Invoice, the three
  period-lock sources) and the preview 200; off-scope DocTypes 403.

## 0.1.0a1 — 2026-07-01 (unreleased)

First assembled vertical: governed Sales Invoice submit, end to end, bench-free.

### Added
- **Glue layer over the pure cores**: `registry` (TOML targets, auth-by-reference — secrets only as
  `env:`/`file:`, never inline), `erpnext` (shape-pinned REST client over an injected transport),
  `tools` (the SDK-free MCP surface + dispatcher), `runtime` (config/seal-key/state assembly),
  `cli` (the human side — `pacioli mint`/`verify`/`orphans`), `server` (thin MCP stdio adapter).
- **Plan persistence** (`plans` table) so `plan_submit` and `submit` survive across separate MCP
  tool calls, with the projected GL the human consented to landing in the durable record.
- **Human marker-mint CLI** (`pacioli mint <plan_id>`): generates a high-entropy token itself,
  stores only its hash (keyless store — no seal key in reach), prints the token once. Minting is
  deliberately **not** an MCP tool (consent cannot be self-granted).
- Seal key: 32 random bytes, `0600`, auto-created; refused if group/world-readable.
- Console script `pacioli`; `pip install pacioli`, MCP server via `pip install 'pacioli[server]'`.

### Security (fix-as-found, from a fresh-eyes redteam of the cores)
- closed-books check no longer skips a **present-but-falsy** period-lock boundary (`""`/`0`) — it
  validates it and refuses, matching the module's deny-bias everywhere else.
- PROVE refuses **non-finite floats** (`NaN`/`Infinity`) into a sealed receipt (invalid JSON; a
  corrupt amount in a financial ledger).
- A SQLite write-lock timeout is now a structured, fail-closed denial, never a traceback out of a
  tool call.
- `submit` binds to the plan's **document name and target**, not just its version (two drafts can
  share a `modified`); a mismatch is refused.
- One live marker per plan (DB constraint); mint TTL bounded to 1..86400s.

### Not yet done (honest scope — see SPEC §5 / README)
- Live end-to-end proof against a real ERPNext bench is **pending** (the reason for the `a1`).
- PROVE is on-box only — **not** tamper-evident against a host-level actor (off-box anchor = a
  later increment). UNDO is **not shipped**. CONSENT is the out-of-band marker, **not** ERPNext
  Workflow segregation-of-duties.
