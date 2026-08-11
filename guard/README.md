<img src="../assets/marks/device.svg" width="72" align="right" alt="The printer's device"/>

*Tractatus de Instrumentis — the guard.* ❧ *Nulla in libro sine contraria — no debit without a credit.*

# Pacioli Guard

**Least-privilege governance for ERPNext. Agents included.**

Frappe/ERPNext has no native way to scope an API credential. An API key authenticates *as a user*
and inherits that user's full role permissions across **every** REST endpoint — so a token you
minted so an integration (or an AI agent) could call one governed `/api/method/…` can equally
`POST /api/resource/<DocType>` directly, walking around whatever that method wrapped. OAuth2 scopes
don't close it either (they're consent claims, confirmed by Frappe's own OAuth author).

**Pacioli Guard binds a credential to an allowlist** — the methods (and, if you grant it, the
DocTypes) it may touch — and enforces it **at the credential layer**, deny-by-default. A leaked or
over-shared key becomes a blast-radius of *what you allowed*, not your whole ERP.

It governs **every api-key credential** on the site, agent or not: an integration, a Zapier/n8n
flow, a script, a vendor token, an MCP/A2A agent. You don't need to run AI to need this.

"Api-key" is the boundary, not a hedge. This gate runs at `auth_hooks`, where the only thing that
exists is *which credential authenticated*, so it sees requests carrying a `token` or `Basic`
Authorization header and nothing else. It does **not** see OAuth2 `Bearer` tokens, desk/cookie
sessions, background jobs, the scheduler, server scripts, or the bench console. The separate
document-layer gate below covers the *act* on those paths; see "What this does not govern".

This is the counting-house door, in Luca Pacioli's terms: **no one writes in the books but the
appointed hand — and only in the books appointed to them.**

## How it works — no core fork

Two of Frappe's own public hooks, because there are two questions and they are legible at two
different altitudes. Nothing in Frappe core is modified either way — this is a plain
`bench install-app`.

**`auth_hooks` — what may this credential call?** Runs **after** the api-key authenticates and
**before** the request dispatches. A scoped credential that steps outside its allowlist gets a
`403 PermissionError`; unscoped credentials (browser sessions, normal keys) are completely
untouched. "Which credential is this" only exists at authentication time, so this can only live
here, which also means it governs api-key requests and not OAuth `Bearer`, desk sessions, or
background jobs.

**`doc_events` `before_submit` / `before_cancel` — was this act consented?** Consent is a property
of a document, so it is checked on the document. Every path that **posts to the ledger** through the
ORM passes it, including the ones the credential hook never sees. It is inert for anyone without a
grant that sets `require_consent`, so installing this app does not change a site that has not opted
in.

**`doc_events` `before_gl_preview` / `before_sl_preview` — was this REHEARSAL consented?** (0.13.0.)
ERPNext previews a ledger by performing the posting and rolling the transaction back
(`controllers/stock_controller.py`), so a preview is a write that does not land. Previewing a submit
therefore requires the same marker the submit requires, and **does not spend it**: the real submit
still spends it, so single-use still means a single posting. The preview is gated rather than exempted
because every alternative means believing a caller's claim that a write will be rolled back, and a
document-layer gate sees documents, not transaction outcomes.

Two consequences worth knowing before you turn this on:

- **The floor marker must now exist BEFORE the plan**, not after it. A human authorises "submit this
  draft" and the projection is produced under that authority, so the projected GL becomes disclosure
  rather than a precondition. No chicken-and-egg: the marker binds to a document, not to a plan.
- **The broker half is `pacioli` 0.34.0.** Guard 0.13.0 alone gates the preview while a broker at
  0.33.3 has no way to present a marker, so `plan_submit` is refused. Fail-closed, and the two halves
  are only useful together.

**Minting a marker: `pacioli_guard.mint.mint_consent_marker`** (0.13.0). Until then this package
demanded a marker and shipped no way to make one, which bit hardest where it was least visible: a site
with `require_consent` on and no marker ever minted could not complete its first governed write.

```
bench --site <site> execute pacioli_guard.mint.mint_consent_marker \
  --kwargs '{"ref_doctype": "Sales Invoice", "ref_docname": "ACC-SINV-2026-00004",
             "ref_action": "submit", "ttl_seconds": 900}'
```

**Deliberately not whitelisted.** An HTTP mint endpoint would be reachable by exactly the api-key
credentials this floor exists to constrain, and a credential that can mint its own consent signs its
own permission slip. Books side only. It generates the token, prints it once, stores only the
SHA-256, and discloses the act so the operator sees what they are authorising from something other
than the agent's narration.

🔴 **Upgrading to 0.13.0 needs `bench migrate` AND `bench clear-cache`, in that order.** The release
adds a field and two `doc_events` keys. Skip the migrate and **every** governed write refuses; skip
the cache clear and previews stay refused. Both fail closed, and both look like the upgrade did
nothing. `SECURITY.md` has the detail.

**What walks around both hooks, stated rather than discovered:**

1. Writes that skip the document lifecycle entirely: raw SQL, and `db_update`-style field writes,
   which ERPNext core does itself when reposting. No Frappe extension point observes those, so no
   gate built on Frappe extension points can claim them.
2. An actor who can break the *grant read* itself. The consent handler is registered on
   `doc_events["*"]`, so it runs on every document on the site; if reading the grant raises, it
   returns rather than throwing, because a `"*"` handler that crashes takes the whole site down
   with it. The trade is deliberate and it is a real residual, not a bug.
3. **Anything Frappe QUEUES cannot be consented at all.** A Journal Entry with more than 100 rows,
   and any DocType carrying `queue_in_background`, is submitted by a background worker. The worker
   runs as the enqueuing user so the gate still applies — but a marker travels in an HTTP header and
   a job has no request, so a gated principal's queued submit is refused *every time*, and Frappe
   surfaces it as a generic "Action Failed" rather than naming consent. Fail-closed, so nothing posts
   ungoverned; but do not gate a credential and then expect it to complete a queued submission.
4. `Document.discard` — whitelisted, and it sets `docstatus = 2` with `db_set` while firing
   `before_discard` rather than `before_cancel`. It refuses anything that is not a draft, so no
   ledger entry is ever reversed by it. Gating it wants its own act type rather than being folded
   into "cancel": consenting to reverse a posted entry and consenting to abandon a draft are
   different permissions.

```
POST /api/resource/SI/<name>?run_method=submit → 200  (granted "Sales Invoice.submit" — resolved)
POST /api/resource/JE/<name>?run_method=submit → 403  (doctype not granted)
GET  /api/method/<any-bare-rpc>                → 403  (deny-unknown: bare names denied unless
                                                       granted AND on the 4-entry SAFE_METHODS)
POST /api/resource/<DocType>                   → 403  (raw-CRUD bypass closed, unless allow_resource)
# unscoped credential → unchanged behaviour
```

## Install

```bash
# from your bench directory: wheel into the bench env, then install the app
env/bin/pip install pacioli-guard
bench --site <your-site> install-app pacioli_guard
```

## Scope a credential

Create an **API Key Scope** record (a first-class DocType, with child-table allowlists) for the
user whose API key you're scoping:

- `methods` — exact dotted names or `fnmatch` globs. A document method (item-URL `?run_method=submit`)
  matches as `"<DocType>.submit"`. **As of 0.6.0 (deny-unknown), grant per-doctype
  `"<DocType>.<method>"` patterns** — a bare RPC name only ever fires if it is one of the four
  curated `SAFE_METHODS` (see "Deny-unknown" below); any other bare/unresolved method is denied
  even if a pattern here matches it.
- `allow_resource` — defaults **false** (method-only; denies raw `/api/resource` CRUD).
- `resource_doctypes` — when `allow_resource` is true, the DocType allowlist. **Empty denies**
  (as of 0.8.0), matching `methods`: a grant that names nothing permits nothing. Everything granted
  here is still bounded by the user's own roles; scope narrows, never widens.
  > Before 0.8.0 an empty allowlist permitted **every** DocType. That was a security defect with a
  > published advisory. If you are on 0.6.3 or earlier, upgrade.
- `Allow All DocTypes` (`allow_all_doctypes`) — the **site-wide resource grant**, defaults **off**.
  Ticking it grants every DocType without listing them, for the verbs you have allowed.
  > **0.8.0 through 0.10.1 documented this as a literal `"*"` row in `resource_doctypes`, and that
  > gesture never worked.** `ref_doctype` is a validated `Link` to DocType, so frappe refused to
  > store `"*"` (`LinkValidationError`) and site-wide access was not actually expressible. It failed
  > *closed* — an unstorable row left the allowlist empty, which denies — so no grant was ever wider
  > than it looked, but the documentation described a capability that did not exist. Fixed in 0.11.0.
  > The `"*"` row is still honored for grants created programmatically, and is not the recipe.

  It widens exactly one axis. The check runs **after** the control-plane deny and **after** verb
  narrowing, so it grants breadth of *DocType* and never breadth of *verb*, and it never reaches
  `API Key Scope` / `Pacioli Consent Marker`. A request whose DocType cannot be resolved is still
  refused. Absence reads as off, so upgrading never switches it on.
- `Allow Read` / `Allow Create` / `Allow Write` / `Allow Delete` — the **per-credential resource
  verbs** (all default on). A DocType allowlist alone admits *every* verb, so a credential meant to
  *read* invoices also POSTs/PUTs/DELETEs them. Untick the verbs you don't want and the same
  credential is genuinely read-only (or read+create, etc.) across its granted DocTypes. Leaving all
  four ticked is the pre-narrowing behavior (full CRUD); to deny resource access entirely, untick
  `allow_resource`. *Narrowing is per-credential in this version — per-DocType verb granularity is
  a future increment.*
- `enabled` — the **kill switch** (defaults on). See CONTAIN below.
- `rate_limit_per_minute` — the **speed limit** (defaults 0 = none). See CONTAIN below.
- `enforce_workflow` — the **Workflow-bypass gate** (defaults **off**, opt-in per credential). See
  "Workflow enforcement" below.

A deprecated one-version fallback still reads a JSON grant from a `api_scope` Long Text field on
**User** (`{ "methods": [...], "allow_resource": false }`) so pre-DocType setups keep enforcing
while they migrate. The enforcement seam (`pacioli_guard/scope.py`) is identical either way.

### Body-doctype scoping (submit/cancel) — the generic-RPC residual, closed for every submit/cancel RPC

A `methods` entry ordinarily matches by NAME only — it does not by itself constrain the doctype a
generic RPC touches, since `frappe.client.*`/`run_doc_method` take their real target from the
request BODY, not the URL. **As of 0.5.0 (extended in 0.5.1)** the guard reads the doctype out of the
body and enforces the SAME `"<DocType>.submit"`/`"<DocType>.cancel"` pattern the item-URL
`?run_method=submit` vector already required, for **every frappe RPC that calls `doc.submit()`/
`doc.cancel()` directly** (the shape that bypasses frappe's own `is_whitelisted` on an override-doctype
like Journal Entry):
`frappe.client.submit`, `frappe.client.cancel`, **`frappe.desk.form.save.cancel`** (the Desk UI's own
cancel endpoint), **`frappe.desk.doctype.bulk_update.bulk_update.submit_cancel_or_update_docs`** (bulk
submit/cancel of up to 500 docs), `run_doc_method` (v1 `dt`/`dn`/`docs` and v2 `document`/`method`), and
`frappe.desk.form.save.savedocs` with a Submit/Update/Cancel `action`. So granting a credential
`"Sales Invoice.submit"` lets it submit a Sales Invoice through *any* of these transports, but **not** a
Journal Entry or any other doctype through them, even though the credential never named the generic RPC.
A malformed/missing doctype — or the bulk RPC's non-submit/cancel `action="update"` — fails **closed**
(denied), never falls back to doctype-blind. The two Desk/bulk names and the fail-closed on bulk-update
were added in **0.5.1** after a guard-bypass redteam found them missing from 0.5.0 (a CRITICAL bulk
bypass + the Desk cancel), and the Desk Submit alias (`frappe.desk.form.save.submit`) plus the
**docstatus-by-body class** were added by a completeness audit against frappe 17 source: because
`frappe.client.save`/`.insert`/`.insert_many`/`.bulk_update` submit or cancel a doc whenever its
body carries `docstatus` 1/2 (via `Document.save()`'s transition detection), those are recognised
too — a docstatus-1/2 body rewrites to `"<DocType>.submit"`/`".cancel"`, a mixed-doctype batch or
`cancel_all_linked_docs` **deny-closes**, all before 0.5.0 ever left internal.
As of **0.6.0** nothing "stays doctype-blind — grant with care" anymore: the shapes that used to
(the draft-create path of `save`/`insert`, `set_value`, `bulk_delete`, bare read/query RPCs) are
**denied as bare grants** under deny-unknown below — a scoped credential creates drafts via
`POST /api/resource/<DocType>` (`allow_resource` + `Allow Create`), and `run_doc_method` now
resolves EVERY inner method (not just submit/cancel/discard) to `"<DocType>.<method>"`.
**Live-proven on the real Frappe v16 bench** at the Gate 10 close-out (2026-07-07): Journal Entry
submit/cancel drove through this body-doctype rewrite (`frappe.client.submit`/`.cancel` verbatim in
the bench request log, 0→1→2), SI/PI/PE stayed clean on the URL-path shape, and the per-doctype
`apply_workflow` re-grant proved positive — `../SCOPED-TOKEN-PROOF.md` **PHASE M** (PHASE L is the
pre-fix historical record). See CHANGELOG 0.5.0–0.6.0.

### Deny-unknown (0.6.0) — the posture flip, SHIPPED

Three adversarial passes against frappe 17 source drove the body-doctype coverage above — and the
third proved the honest boundary: a pure, I/O-free request classifier **cannot** be provably
complete (a *2-hop laundering* vector carries the target doctype **nowhere in the request** — see
the `Bulk Update` hard-deny below). Whack-a-mole on method names does not converge, so **0.6.0
inverts the posture**: a `methods` grant on a method call is honored ONLY when the call is
**doctype-RESOLVED** — the route itself carried the doctype (item-URL `?run_method`, v2
path-carried doc-method, v2 two-segment controller method, or a body-doctype rewrite above) — OR
the bare name is one of exactly **four curated `SAFE_METHODS`** (exact names, no globs, each
read-only with no docstatus/data mutation):

- `frappe.auth.get_logged_user` — identity probe (`pacioli doctor`)
- `frappe.desk.form.linked_with.get_submitted_linked_docs` — linked-doc lookup (the UNDO graph)
- `erpnext.controllers.stock_controller.show_accounting_ledger_preview` — PLAN preview
- `pacioli_guard.api.consent_status` — this app's own self-report (`pacioli doctor`), added 0.7.0.
  It passes the same admission test: no arguments, writes nothing, reports only on
  `frappe.session.user`, so it cannot be pointed at another credential or another doctype and has
  no body to launder a target through. It exists so an operator can be told `require_consent` is
  off, because a silent insecure default is how a bypass hides.

  Since **0.12.0** it also reports **`gate_registered`** and **`consent_enforced`**, because
  `require_consent` being *on* turned out not to be enough either. It is a flag on a grant — it
  records what was asked for. The consent gate itself rides `doc_events`, and a site whose hooks
  cache predates its consent support can carry that flag while the handlers are not registered at
  all. Scope rides `auth_hooks`, so scope keeps refusing perfectly and the floor looks present while
  no act is being consented. `consent_enforced` is the conjunction, and it is the field to act on.
  It is deny-biased: a half-loaded gate, another app's handler, or a probe that cannot look all read
  as not registered. It proves the handlers are *registered*, not that they *function* — only
  watching a refusal proves that.

  🔴 **0.14.0 made it stricter, and that can flip an existing answer.** Through 0.13.x it looked for
  two handlers, `before_submit` and `before_cancel`. The two preview gates shipped in 0.13.0 and
  **they deny**, so the receipt reported the gate loaded on a site carrying only the pre-0.13.0
  pair — precisely the stale-hooks-cache condition this field exists to catch, one release later.
  It now requires all four. **If `consent_enforced` was true and is now false, nothing about your
  site changed except that it is being measured properly:** run `bench --site <site> migrate` then
  `bench --site <site> clear-cache`, in that order, and re-check. `after_insert` is deliberately not
  required — `hooks.py` records that it decides and refuses nothing, and a receipt stricter than the
  gate is its own kind of lie.

  🔴 **0.14.0 also fixed the resource posture, which inverted the widest grant.** A grant with
  `allow_resource` and `allow_all_doctypes` both on reported `denies_all` — the narrowest of the
  four states — because the report never read the `allow_all_doctypes` flag that replaced the
  retired `"*"` row. A new value, **`all_doctypes`**, is returned; anything switching on that string
  needs a branch for it. Nothing was mis-decided: enforcement never consulted this report. But it
  told operators their grant was narrower than it was, and the reason to publish it is that
  direction.

**Anything else is denied even if a grant pattern fnmatches it** — a new frappe RPC is denied until
reviewed, instead of open until enumerated. SAFE_METHODS membership is necessary-not-sufficient
(the grant must still name it), and the **admission criterion** is strict: read-only, no
docstatus/data mutation, reviewed on entry. **There is NO escape hatch** — no site config, no
per-credential flag — because an escape hatch is exactly the open-until-enumerated posture this
flip removes; a new safe method is a reviewed, changelogged, version-bumped act.

Consequences worth knowing before you upgrade (full list: CHANGELOG 0.6.0):

- `frappe.model.workflow.apply_workflow` is deliberately **not** safe-listed (it submits/cancels
  internally); it resolves per-doctype from its `doc` body param — re-grant it as
  `"<DocType>.apply_workflow"`.
- `run_doc_method` resolves **every** inner method to `"<DocType>.<method>"` (a missing/non-string
  method deny-closes) — bare it never fires.
- **`Bulk Update` is hard-denied, ungrantable**: its whitelisted instance method reads the victim
  doctype from the SAVED RECORD (`document_type` + `field=docstatus` on a `Bulk Update` row), so no
  request classifier can resolve it and no grant can express it. Denied before the grant check.
- The `bench migrate` for 0.6.0 runs a **log-only audit** of existing grants and WARNs per pattern
  the new posture treats differently (best-effort static heuristic; it cannot break the migrate).

**Named residuals — the honest boundary of 0.6.0** (stated, not silently claimed closed):

- **R3 — safe-listed reads bypass `resource_doctypes`.** The two safe-listed *read* methods carry a
  doctype in their body that is **not** checked against `resource_doctypes` — a disclosure-only
  bypass of the read-narrowing axis (no mutation), pre-existing.
- **v2 two-segment `/method/<dt>/<method>` grants the whole controller module.** It counts as
  doctype-resolved (the doctype IS in the URL path, so it can't be spoofed), but frappe runs it as a
  **module-level** whitelisted function in that doctype's controller — so a broad `<DocType>.*` grant
  regains *every* whitelisted function in that module, not just document methods. Bounded
  (`load_doctype_module` raises on a non-doctype, so `frappe.client.*` can't be laundered this way;
  most ERPNext controller functions are unsaved mappers) but real: **grant explicit
  `<DocType>.<method>` patterns, not `<DocType>.*`,** for a credential that shouldn't reach a
  controller's whole surface.
- **v2 collection-mutation routes classify as `create` (NEW — found by the 0.6.0 redteam, staged for
  Gate 10, NOT yet closed).** `POST /api/v2/document/<DocType>/bulk_update` and `…/bulk_delete` are
  classified as a `resource` **create**, but frappe writes/submits (docstatus carried per-item in the
  `docs` list) and *deletes* through them — so a credential narrowed to the `create` verb can write/
  submit/delete existing docs of a granted doctype, and the `enforce_workflow` gate (which reads only
  a top-level `docstatus`) misses the per-item one. Same doctype (not a cross-doctype spoof) and it
  requires `allow_resource`. **Live-checked on the v16 bench (2026-07-06, Gate 10): this route does
  NOT exist on Frappe v16** — `/api/v2/document/<Dt>/bulk_update` resolves to a document-name lookup
  (same `DoesNotExistError` as any garbage trailing segment), so there is no mutating handler behind
  it. The residual reappears only against v17-dev source, so it stays documented (not closed) for
  forward-compat, but it is not a live hole on v16.
- **Container/tool-DocType 2-hop vectors — four now HARD-DENIED, reconcile a disclosed residual
  (readiness audit + hardening, 2026-07-10).** The same 2-hop shape as `Bulk Update` — a whitelisted
  method on a "container" doctype that reads its *victim* doctype from a **saved record** or a
  **nested child-table row**, not from the request the classifier sees — recurs across ERPNext's
  accounting module. The broker governs a small fixed set of doctypes; every tool-DocType of this
  shape it does **not** use is now added to the same ungrantable hard-deny set (deny-more-only,
  zero-regression):
  - **`Data Import` / `Bank Statement Import` `form_start_import`** (v2 `/method/<DocType>/…`, resolves
    to `("method", "Data Import.form_start_import")`) — a granted import-controller method would drive
    insert+submit of whatever `reference_doctype` the target record names. **HARD-DENIED (ungrantable).**
  - **`Unreconcile Payment` / `Repost Accounting Ledger`** — same child-table-of-references shape
    (plausible, not traced to full depth; a hard-deny is safe regardless since the broker never uses
    them). **HARD-DENIED (ungrantable).**
  - **`Payment Reconciliation.reconcile`** (`run_doc_method`; `document.allocation[*].reference_*`) —
    the allocation child rows name arbitrary Payment Entry / Sales Invoice / Journal Entry docs, and
    ERPNext relinks them with `ignore_permissions=True`, reaching those doctypes with their **own role
    permissions bypassed**. This one **cannot** be classifier-closed: the broker's own F-R2 write
    *needs* `reconcile`, and a malicious call is byte-identical to the legitimate one (same
    `run_doc_method`, same `Payment Reconciliation` doc, same nested refs), so a hard-deny would break
    the broker and any per-credential carve-out is inherited by a stolen broker credential — the exact
    threat. It stays a **disclosed residual**, closed by posture. **Operator rule:** grant
    `Payment Reconciliation.reconcile` ONLY to the broker's own credential (which owns and constructs
    the payload from a pinned plan — an agent cannot inject an allocation list); never to a general
    agent credential, and never grant `<DocType>.*` on a container/tool doctype. Design + the rejected
    shape-deny candidate: `../docs/plans/2026-07-10-container-doctype-2hop-hardening.md`.

The deny-unknown enforcement above is **live-proven on a real Frappe v16 bench** (2026-07-06, Gate 10
— bare-deny, the v2 dt-decoy spoof denied, SAFE_METHODS fire, apply_workflow per-doctype re-grant,
Bulk Update ungrantable, granted-resolved not over-blocked, migrate audit runs without breaking
migrate). Document-submit end-to-end (JE/SI/PI/PE) closed 2026-07-07: JE submit/cancel live through
the body-doctype rewrite (`frappe.client.submit` verbatim in the bench request log, 0→1→2), SI/PI/PE
regression clean on the URL-path shape, and the per-doctype `apply_workflow` re-grant proven positive
(a real Draft → Pending Approval transition) — broker-side record `SCOPED-TOKEN-PROOF.md` PHASE M.

## CONTAIN — the kill switch and the speed limit

The danger of an agent credential isn't just *what* it can call — it's **velocity under hijack**: a
compromised or misbehaving agent does allowed things, fast, until someone notices. Guard already
sits on every request the credential makes, so containment lives here too. Two fields on the same
**API Key Scope** record:

- **`enabled` (kill switch).** Untick it and **every** request from that credential is denied at
  the chokepoint — effective on the *next request*, no restart, no cache to wait out (the grant is
  read per request). It dominates the allowlists: a killed credential can't call anything, not even
  what it was granted. A grant created before this field existed reads as enabled — the field's
  absence is not a kill.
- **`rate_limit_per_minute` (speed limit).** `0` (default) = no limit. When set, requests are
  counted per fixed one-minute window and the request over the limit gets a `403` naming the limit.
  **Every** request the credential makes burns budget — permitted or denied — because what's being
  contained is the credential's total velocity, not its success rate.

What CONTAIN stops: runaway velocity (a hijacked or looping agent gets damped to N calls/minute)
and gives you an instant, no-deploy revoke (untick one box).

What it does **NOT** do — honestly:

- The rate counter lives in the site cache (`frappe.cache()`), so it is **best-effort velocity
  damping, not tamper-evident accounting** — a cache flush (or Redis restart) resets the window.
  It is a damper, not a ledger; don't cite its counts as an audit record.
- Fixed windows have the classic **boundary burst**: a credential can spend a full budget in the
  last second of one window and another in the first second of the next — briefly ~2× the nominal
  rate. Acceptable for damping; set the limit with that in mind.
- If the cache is unusable while a limit is set, that credential **fails closed** (denied) — whoever
  set a limit opted into containment, and an uncountable window can't honestly be called under it.
  Credentials with no limit never touch the counter.
- **To revoke, untick `enabled` — do not delete the scope doc.** The kill switch is the sanctioned
  revoke, and it always wins. Deleting the *API Key Scope* row is not the same thing: if a site
  still carries a legacy `User.api_scope` JSON grant from before the DocType existed (the deprecated
  one-version fallback), deleting the row makes the credential fall through to that older grant —
  which predates `enabled` and so reads as *enabled*. During a migration window, delete-to-revoke
  can silently reopen a credential; untick-to-revoke never can. (Once a site has fully migrated off
  the legacy field, this footgun is gone.)

Every guard denial — out-of-scope, kill switch, or rate — also writes a row to Frappe's stock
**Error Log** (`frappe.log_error`, title prefixed `Pacioli Guard denied a request`), so denials are
visible in the desk without a new DocType. The log write is fail-open by design: a failure to log
can never suppress the deny.

## Workflow enforcement — the belt-and-suspenders gate

Pacioli's **broker** (the MCP tool front door) already refuses to `submit` a workflow-governed
document on the agent's own path — that's `pacioli/workflow.py`. But Frappe itself does **not**
enforce Workflow on a direct `docstatus` change (`validate_workflow` only fires when the document's
`workflow_state` field itself changes on save), so the SAME scoped credential that's denied by the
broker can submit around a configured approval chain with a raw REST call that never touches the
broker at all. `enforce_workflow` closes that at the **credential layer** — it turns "governs the
agent's path" into "governs every api-key path through this credential."

Turn it on per credential (**off by default** — like every CONTAIN field before it, a gate that can
newly deny a previously-passing call must never flip on site-wide by surprise):

```
enforce_workflow = 1
```

With it on, `check_scope` refuses — AFTER the existing scope allowlist, on a call the credential
would otherwise be permitted to make — any of:

- A `submit`/`cancel` **method call** on a doctype with an active Workflow (covers the v1 item-URL
  `run_method`, the v2 path-carried doc-method, and the legacy `?cmd=` route alike — they all
  classify to the same shape).
- A raw `PUT`/`PATCH` to `/api/resource/<DocType>/<name>` (v1) or `/api/v2/document/<DocType>/<name>`
  (v2) whose body carries a `docstatus` key at all, against a workflow-governed doctype — the sneaky
  raw-REST update path. The mere *presence* of the key is the signal; this does not read/compare the
  document's current `docstatus` (a fragile extra DB read).
- A `POST` **create** carrying a *submitting* `docstatus` (1 or 2) — insert-as-submitted. Here
  presence alone is not the signal (a draft legitimately posts `docstatus: 0`); only a submitting
  value is.
- The Desk UI's own **`frappe.desk.form.save.savedocs`** endpoint with an `action` other than a
  plain draft `"Save"` — its method name never ends in `submit`/`cancel`, so it is recognised
  specifically (the doctype is read from its `doc` body param); a missing/unknown action is treated
  as docstatus-moving (deny-biased).

...unless the call **is** `frappe.model.workflow.apply_workflow` — the sanctioned transition path,
exempt from *this* gate (it still clears the kill switch, the rate limit, and the base scope
allowlist like any other call; "exempt" means never treated as a workflow *bypass*, not "waved
through the guard entirely").

### What it does **NOT** do — honestly

- **Body-doctype submit/cancel ARE covered here now (0.5.1).** This gate judges the SAME
  body-doctype-rewritten target the base scope gate resolves — `frappe.client.submit`/`.cancel`,
  the Desk cancel, bulk submit/cancel, `run_doc_method`(submit/cancel), and `savedocs`
  Submit/Update/Cancel all resolve to `"<DocType>.submit"`/`".cancel"`, so a workflow-governed
  submit/cancel on a doctype named only in the request body is caught, not just the URL-path
  `run_method` shape. This closed a real gap (0.5.0 ran on the un-rewritten target → a body-doctype
  submit yielded doctype `"frappe.client"` → no workflow found → silent no-op); it mattered most for
  **Journal Entry**, which submits/cancels EXCLUSIVELY via `frappe.client.submit`/`.cancel` (its
  overridden submit/cancel aren't whitelisted) and would otherwise have had zero workflow protection.
  Found by the same guard-bypass redteam, before 0.5.0 shipped. The remaining theoretical residual is
  narrow: a *non*-submit/cancel method that flips `docstatus` by raw field write (`frappe.client.set_value`
  on the `docstatus` field) is not rewritten — mitigated because the base scope grants such blind
  methods only with care, and the raw-REST `PUT …?docstatus=` path is caught separately (below).
- **Credential-layer boundary, same as every other gate in this hook.** Fires only for
  api-key/`Basic` requests. NOT OAuth Bearer, desk/cookie sessions, background jobs, the bench
  console, or script/report calls. This closes the gap for *other scoped api callers bypassing the
  broker* — it is not an ERPNext-wide Workflow patch; only frappe core touching `validate_workflow`
  itself could do that.
- **Ambiguous/malformed Workflow config is not detected the way the broker detects it.** The
  broker's own pure core explicitly refuses when more than one active Workflow governs a doctype
  (naming every one it found). This gate relies on frappe's internal `get_workflow_name`, which is
  not known to expose that ambiguity at all — if a site somehow carries more than one active
  Workflow for a doctype, this gate silently governs by whichever one frappe's own lookup picked. Do
  not assume this mirrors the broker's ambiguity handling.
- **Deny-biased on lookup failure, not deny-biased on ambiguity it can't see.** If the internal
  Workflow lookup itself raises for a call that already looks docstatus-changing, that is treated as
  "assume governed" and refused — an unverifiable answer is never read as "no workflow."
- **Live-verified (Gate 7, 2026-07-03, real ERPNext v16 bench).** Both knowledge-pins held under
  live falsification: `frappe.model.workflow.get_workflow_name` exists at the pinned import path and
  returns the active workflow name (`'SI Approval (Gate5)'`) for a governed doctype and a falsy value
  for an ungoverned one; and a raw JSON `PUT` `docstatus` body **does** surface through
  `frappe.form_dict` (leg refused 403), as does the Desk `savedocs` `doc`/`action` (leg refused 403).
  All four `is_docstatus_changing` detection branches were caught with `enforce_workflow=1` — item-URL
  `run_method=submit` (the governed SI stayed docstatus 0), `savedocs` Submit, raw `PUT {docstatus:1}`,
  and POST-create `docstatus:1` — while the sanctioned `apply_workflow` was not guard-blocked and an
  ungoverned doctype was not over-blocked. Default-off was A/B-proven on the same vector
  (`enforce_workflow=0` → 200, the SI submitted). The generic-RPC residual below was confirmed live as
  documented (see the footgun note): `/api/method/run_doc_method` with the doctype in its body is not
  classified as docstatus-changing, so a credential that is *also* granted that broad method can walk
  around this gate — which is exactly why the default tight scope denies such methods outright.

## Honest scope

- Governs **programmatic** REST access (`/api/method` + `/api/resource` v1/v2, incl. item-URL
  `run_method` doc-methods and the legacy `?cmd=` RPC). Frappe's own roles already govern
  desk/browser users, correctly — this doesn't change their experience.
- **Credential schemes it scopes:** `token <key>:<secret>` and `Basic base64(key:secret)`
  (`curl -u`). **OAuth2 `Bearer` tokens are NOT scoped** — they carry Frappe's own OAuth scopes and
  authenticate as a real user, but this gate deliberately does not touch them (governing OAuth is a
  separate layer, stated not silently skipped). If your agent authenticates via OAuth Bearer, this
  credential floor does not yet constrain it.
- Internal `frappe.client` RPC and background jobs run in a non-credential context (no `Authorization`
  header) and are not this hook's surface; tracking them is production hardening, tracked separately.
- A `methods` entry is enforced **deny-unknown as of 0.6.0**: it fires only on a doctype-RESOLVED
  call (item-URL `run_method`, v2 path/two-segment doc-methods, or a body-doctype rewrite — the
  submit/cancel/apply_workflow RPCs and every `run_doc_method` inner method resolve from the
  request body per 0.5.0–0.6.0) or on an exact `SAFE_METHODS` name (four read-only utilities).
  A bare/unresolved generic RPC — a draft `save`/`insert`, `set_value`, `bulk_delete`, any
  unrecognised name — is **denied even if granted** (see "Deny-unknown" above; live-proven
  2026-07-06: a *granted* bare `frappe.model.workflow.apply_workflow` and a *granted*
  `Bulk Update.bulk_update` both 403'd on the real bench).
- Scope **narrows** a credential; it never widens one (a scoped user still can't exceed its roles).
  One honest exception: a granted method that ERPNext itself runs with `ignore_permissions=True`
  (notably `Payment Reconciliation.reconcile`) reaches its referenced docs with *their* roles
  bypassed — see the container/tool-DocType 2-hop residual under "Named residuals" and grant such
  methods only to the broker's own credential.
- Workflow-governed docstatus changes are a SEPARATE opt-in gate (`enforce_workflow`, off by
  default) — see "Workflow enforcement" above for its own honest limits (generic-RPC footgun,
  credential-layer boundary, ambiguity handling, knowledge-pinned shapes).

## Design & proof

The security-critical decision logic (`pacioli_guard/scope.py`) is a **pure module** with no Frappe
import, unit-tested bench-free (`pacioli_guard/tests/test_scope.py`). The live end-to-end proof
(scoped credential denied on both `/api/method` and `/api/resource` against a real Frappe v16 bench,
frappe attributing the 403 to this app) is written up in `../SCOPED-TOKEN-PROOF.md`; the market/novelty
verdict is recorded in the workshop's internal research verdict (a 73-agent adversarial review; not shipped in this tree).

Part of **Pacioli** — least-privilege governance for ERPNext. Pacioli Guard is the foundation;
the Pacioli MCP broker (planned/consented/proven/reversible tool calls) is the agent front door built on top.

License: Apache-2.0.


---

<sub>VENETIIS MCDXCIV · IN THE WORKSHOP OF JOHN BROADWAY · MMXXVI</sub>
