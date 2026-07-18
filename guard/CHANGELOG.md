# Changelog — Pacioli Guard

Least-privilege API capability scoping for Frappe/ERPNext. Honest pre-1.0 semver.
Distribution name `pacioli-guard`; Frappe app / import module `pacioli_guard`.

## 0.6.3 — 2026-07-17 — cleaner wheel: test suite out, SPDX license

PATCH (packaging + metadata; no behavior change). The distributed wheel no longer ships the test
suite (`pacioli_guard.tests` excluded); the DocType schema and data model (`*.json`, `modules.txt`,
`hooks.py`) still travel with it, as a Frappe app requires. License metadata moves to the PEP 639
SPDX expression (`license = "Apache-2.0"`, deprecated classifier dropped, build requires
`setuptools>=77`).

## 0.6.2 — 2026-07-10 — container-DocType 2-hop: four broker-unneeded tool-DocTypes hard-denied

PATCH (a flagged residual, closed at John's ask + scope ruling; deny-more-only, no grant ever
widened; guard 297 + 50 subtests, +6/+19). NEEDS-BENCH-PIN for v16 exploit-reachability (P1–P4 in
the pin sheet, John's arm) — but the hard-deny is safe regardless, and no-regression is unit-proven.

- **`_UNGRANTABLE_METHOD_DOCTYPES` extended** from `{"Bulk Update"}` to also include `Data Import`,
  `Bank Statement Import`, `Unreconcile Payment`, `Repost Accounting Ledger` — tool-DocTypes whose
  granted controller method drives writes to OTHER doctypes named in the request body / child rows /
  saved record (the `Bulk Update` 2-hop shape), that the broker NEVER governs (verified: the broker
  references none of them). Caught by the existing accent+case-folded pre-grant hard-deny in
  `is_permitted`, exactly like `Bulk Update`; `Data Import.form_start_import` via the v2 two-segment
  route resolves to the right target and is denied even under an exact/`*` grant.
- **`Payment Reconciliation` deliberately NOT added** — the broker's own F-R2 reconcile needs it, and
  a malicious `run_doc_method` reconcile is byte-identical to the legitimate one, so it cannot be
  classifier-closed without breaking the broker (a per-credential carve-out would be inherited by a
  stolen broker credential — the threat). Stays a disclosed residual + operator rule; pinned by a
  test that `Payment Reconciliation.reconcile` remains grantable (no broker regression). The rejected
  shape-deny candidate and the reasoning: `../docs/plans/2026-07-10-container-doctype-2hop-hardening.md`
  (a workshop-internal run record; the public proof arc is `../SCOPED-TOKEN-PROOF.md`).
  README "Named residuals" updated: four moved from "un-audited" to HARD-DENIED, reconcile disclosed.

## 0.6.1 — 2026-07-10 — form_dict extraction seam: crash-to-deny + accent-insensitive hard-deny

PATCH (classifier robustness from the readiness redteam — all strictly-stricter/deny-biased, no
grant ever widened; the `scope.methods` fnmatch stays exact-case). Unit-proven (guard 291 + 31
subtests, +17/+20). The MariaDB-collation claims below are NEEDS-BENCH-PIN for the exact collation
the site ships, but folding here can only ever deny MORE than the raw-string comparison, so it is
the correct default posture without bench confirmation.

- **`cmd` type-guard.** A truthy non-string `cmd` in `form_dict` (a crafted JSON body's
  `"cmd": [...]`/`{...}`/int) crashed `cmd.strip()` unconditionally; now classified deny-biased
  (`other`) rather than crashing the hook. A falsy non-string still falls through to path unchanged.
- **Accent-AND-case-insensitive hard-deny (redteam, confirmed classifier bypass).** The
  "Bulk Update" 2-hop hard-deny first compared case-sensitively; a case-fold mirror then covered
  case but NOT diacritics — `Bülk Update` sailed through `is_permitted` as `True` under a wildcard
  grant, while MariaDB's default accent-insensitive collation on `tabDocType.name` plausibly still
  resolves it to the real DocType. The doctype-part is now NFKD-normalized + combining-marks-stripped
  + casefolded before the ungrantable-set lookup (`_fold_doctype`) — closing case and accent in one.
- **Unhashable `docstatus`/`action` → deny, not TypeError.** `is_docstatus_changing`'s POST branch
  and `body_scoped_target`'s bulk/savedocs action lookups crashed on a list/dict value; now
  isinstance-guarded and deny-biased.
- Flagged NEEDS-BENCH-PIN (NOT changed): `/api/method/<name>` v1-bare path is read raw (not
  percent-decoded) where the other routes decode — pinned with current behavior + a test.

## 0.6.0 — 2026-07-06

**BREAKING — the deny-unknown posture flip.** The 0.5.1 changelog below stages this as its own
increment; this is that increment. Three adversarial passes proved a pure request classifier cannot
enumerate every doctype-blind generic RPC — so 0.6.0 stops trying: a `methods` grant on a
`kind == "method"` call is now honored ONLY when the call is **doctype-RESOLVED** (the route itself
carried the doctype: v1 item-URL `run_method`, v2 path-carried doc-method, v2 two-segment
controller method, or a body-doctype rewrite) OR the bare name is one of THREE curated
**`SAFE_METHODS`** (exact names, no globs — `frappe.auth.get_logged_user`,
`frappe.desk.form.linked_with.get_submitted_linked_docs`,
`erpnext.controllers.stock_controller.show_accounting_ledger_preview`; each read-only, no
docstatus/data mutation). **Everything else is denied even if a grant pattern fnmatches it.** A new
frappe RPC is now denied-until-reviewed instead of open-until-enumerated. **NO escape hatch**, by
design: a new safe method is a reviewed, changelogged, version-bumped act — never a runtime knob.
SAFE_METHODS membership is necessary-not-sufficient (`scope.methods` must still grant the name).

### Breaking — what an existing grant loses on upgrade
- **Any bare/unresolved method grant stops matching** bare `/api/method/<name>`, `?cmd=`, and v2
  single-segment `/method/<name>` calls, however broad the pattern (`*` included), unless the exact
  name is in SAFE_METHODS. Per-doctype grants (`<DocType>.<method>`) keep working on every resolved
  route, unchanged.
- **`run_doc_method` now resolves EVERY inner method** to `"<DocType>.<method>"`, not just
  submit/cancel/discard — a grant of only `"Sales Invoice.get_pdf"` no longer reaches any other
  doctype's `get_pdf` (or any other method) through the bare RPC; a missing/non-string inner
  method deny-closes. (Closes the doctype-AND-method-blind hole 0.5.x documented as open.)
- **Draft creates via `frappe.client.save`/`.insert`/`.insert_many`/`.bulk_update` are now denied
  bare** (docstatus-0/absent bodies never rewrote per-doctype, and the bare names are unresolved) —
  the sanctioned path for a scoped credential to create drafts is `POST /api/resource/<DocType>`
  under `allow_resource` + `Allow Create` + `resource_doctypes`. Likewise `frappe.client.set_value`
  and `bulk_delete` no longer work as bare grants. The 0.5.x "grant those with care" residual class
  is simply no longer grantable-by-name.
- **`frappe.model.workflow.apply_workflow` re-grants per-doctype.** It calls `doc.submit()`/
  `.cancel()` internally — a doctype-blind submit/cancel — so it is deliberately NOT in
  SAFE_METHODS; `body_scoped_target` now resolves it to `"<DocType>.apply_workflow"` from the
  nested `doc` body param. A workflow-SoD credential granted the bare name must re-grant
  `"<DocType>.apply_workflow"` for each doctype it transitions.
- **`Bulk Update` is HARD-DENIED, ungrantable** (`_UNGRANTABLE_METHOD_DOCTYPES`): any method target
  whose doctype-part is `"Bulk Update"` is refused BEFORE the grant check, regardless of resolution.
  Its whitelisted instance method reads the victim doctype from the SAVED RECORD (the 2-hop
  laundering vector 0.5.1 named as the honest boundary) — no classifier can resolve it, so no grant
  can express it. Other container-DocType vectors of the same shape are un-audited — a post-Gate-10
  follow-up, stated not silently claimed closed.

### Added
- **Provenance signal** (`scope.py`): `classify()` internals refactored to `_classify_full` /
  `_classify_v2_full` returning `(kind, target, resolved)`; `classify()` stays a thin 2-tuple
  wrapper (its 48 existing tests untouched) and a new `method_target_resolved()` wrapper reads
  `resolved` off the SAME traversal — never inferred from the target's string shape, which cannot
  distinguish a route-supplied doctype from a caller-asserted one (`"Sales Invoice.submit"` via a
  bare `/api/method/` path is syntactically identical to the genuine per-doctype call).
  `is_permitted` gains a deny-biased `method_resolved=False` keyword; resource/other branches
  untouched. `enforce.py` counts a body-doctype rewrite as resolved and otherwise consults
  `method_target_resolved` on the same request fields.
- **Migrate audit** (`patches.txt` first entry —
  `pacioli_guard.patches.v0_6_0.warn_unresolved_method_grants`): at `bench migrate`, walks every
  API Key Scope's `methods` rows and logs a WARN per pattern that is neither an exact SAFE_METHODS
  member nor a plausible per-doctype grant (plus the now-ungrantable `Bulk Update` rows). LOG-ONLY
  and **best-effort by contract**: a static string heuristic (it cannot replay a live request), the
  whole `execute()` try/except-wrapped so a failed audit can never fail the migrate. Its live
  migrate behavior is a Gate 10 pin.
- Tests: a fixture-table test pinning `classify()` + `method_target_resolved()` together for every
  branch; deny-unknown and Bulk-Update-hard-deny test classes; bench-free tests for the migrate
  audit's pure heuristic. Four 0.5.x tests flip to their new outcomes by name (get_pdf, save-draft,
  apply_workflow ×2). Guard suite 232 → 263 passed.

### Corrected
- **0.5.1 below overstates "the broker is unaffected."** The broker's CODE is unchanged and its
  three bare safe-list methods are exactly the three SAFE_METHODS (still work), but the broker's
  CREDENTIAL is affected: its workflow leg must re-grant `"<DocType>.apply_workflow"` per-doctype
  (and drop any bare dangerous grants) at Gate 10 — see GO-LIVE.md Gate 10 step 2.

### Redteam-hardened before ship (fresh 3-lens pass on this increment — bypass / breakage / mechanical)
- **CRITICAL, fixed — v2 `run_doc_method` `dt`-decoy cross-doctype spoof.** frappe v2's
  `run_doc_method(method, document, kwargs)` has NO `dt` param and acts on `document`; a credential
  scoped to `Sales Invoice.*` could send `dt="Sales Invoice"` + `document={"doctype":"Journal
  Entry"}` and the dt-first guard would authorize `Sales Invoice.submit` while frappe submitted the
  JE (and the same decoy slid a `Bulk Update` `document` past the hard-deny). `_run_doc_method_doctype`
  is now **deny-biased on doctype-source disagreement**: it resolves only when every present source
  (`dt`/`docs`/`document`) names ONE doctype; a decoy that disagrees with the body doc fails closed.
  Version-robust (doesn't rely on tracking frappe's per-version arg precedence).
- **MEDIUM, fixed — `Bulk Update` hard-deny doctype-part extraction hardened.** It used
  `rsplit(".",1)` so a dotted method name (`Bulk Update.x.bulk_update`) yielded doctype-part
  `Bulk Update.x` and dodged the ungrantable set; now `split(".",1)` + `.strip()` (a DocType carries
  no dot, so the first segment IS the doctype), closing the dotted-method slide and a whitespace-
  padded (`Bulk Update .…`) URL-path evasion. The migrate audit uses the same extraction.
- **LOW, fixed — migrate-audit false-negatives.** A whitespace-padded grant row is dead at
  enforcement (patterns are matched EXACTLY, not stripped) — the audit now flags it instead of
  strip-and-approving it; and a Title-cased RPC module path (`MyApp.api.do_thing`) is now warned (a
  dot before the method = module path, not `<DocType>.method`).

### Honest residuals — named, unchanged by this flip
- **R3 — safe-listed reads bypass `resource_doctypes` narrowing**: `get_submitted_linked_docs` and
  `show_accounting_ledger_preview` carry a doctype in the body that is not checked against the
  resource allowlist — DISCLOSURE-only (no mutation), pre-existing, now load-bearing enough to name.
- **v2 two-segment `/method/<dt>/<method>` grants the whole controller module.** Counts as resolved
  (doctype is path-carried, unspoofable) but frappe runs a module-level whitelisted function, so a
  broad `<DocType>.*` grant reaches every whitelisted function in that controller — grant explicit
  `<DocType>.<method>` patterns, not `<DocType>.*`. Bounded (`load_doctype_module` raises on a
  non-doctype).
- **v2 collection-mutation routes classify as `create` (NEW this pass, STAGED for Gate 10, not
  closed).** `POST /api/v2/document/<Dt>/bulk_update`|`bulk_delete` classify as a resource `create`
  but frappe writes/submits/deletes through them, and `enforce_workflow` reads only a top-level
  `docstatus` (misses the per-`docs`-item one). Same-doctype, requires `allow_resource`; documented
  and staged as a Gate-10 falsification pin rather than closed by an unverified v2-route change.
- **Other container-DocType 2-hop vectors un-audited** (the `Bulk Update` shape generalizes; the
  scouts flagged this class has recurred three rounds) — post-Gate-10 follow-up pass.
- **The deny-unknown behaviors are LIVE-PROVEN** on the real Frappe v16 bench (2026-07-06, GO-LIVE
  Gate 10): the migrate audit runs+warns without breaking migrate; SAFE_METHODS fire bare; the bare
  `apply_workflow` grant is denied and rewrites per-doctype (403 `Sales Invoice.apply_workflow`); the
  **v2 `run_doc_method` dt-decoy spoof is denied** (403 `other: None`); a granted *resolved* call is
  permitted (not over-blocked); and `Bulk Update.bulk_update` is denied even when explicitly granted.
  The v2 `/document/<Dt>/bulk_update` collection route was found NOT to exist on v16 (the residual
  reappears only against v17-dev). Still knowledge-pinned (pending a focused bench window): JE/SI/PI/PE
  document-submit **end-to-end** and the apply_workflow positive case (both need balanced draft docs).
  *(Update 2026-07-07: that window ran — all of it held. JE 0→1→2 through the body-doctype rewrite,
  SI/PI/PE regression clean, apply_workflow positive under the per-doctype re-grant. Broker-side
  record: `SCOPED-TOKEN-PROOF.md` PHASE M.)*

## 0.5.1 — 2026-07-06

**Redteam hardening of 0.5.0, before it ever left internal.** An independent guard-bypass lens
found the 0.5.0 body-doctype allowlist was incomplete: two more frappe RPCs call `doc.submit()`/
`doc.cancel()` directly (the override-doctype-capable shape 0.5.0 exists to make safe) and slipped
through to the doctype-blind literal-method check.

- **CRITICAL — bulk submit/cancel closed.** `frappe.desk.doctype.bulk_update.bulk_update
  .submit_cancel_or_update_docs(doctype, docnames, action)` bulk-submits/cancels up to 500 docs via
  `doc.submit()`/`doc.cancel()`. It was unrecognized, so a credential holding that method's literal
  name (e.g. an operator enabling Desk "Bulk Edit → Submit" for Sales Invoice) could bulk-submit or
  bulk-cancel **any** doctype, doctype-blind. Now resolved per-doctype from the `doctype` body param
  with the verb from `action`; `action="update"` (arbitrary field write) and any unknown/missing
  action **fail closed**.
- **HIGH — Desk cancel endpoint closed.** `frappe.desk.form.save.cancel(doctype, name)` (the Desk
  UI's own cancel button, distinct from `frappe.client.cancel`) had the identical plain-sibling-param
  shape and was unrecognized. Now resolved per-doctype.
- **HIGH — `enforce_workflow` now covers body-doctype submit/cancel.** The Workflow-bypass gate ran
  on classify()'s pre-rewrite target, so a body-doctype submit yielded doctype `"frappe.client"` →
  no workflow found → the gate silently no-op'd. Journal Entry submits/cancels EXCLUSIVELY via
  `frappe.client.submit`/`.cancel`, so it was the one doctype with zero workflow protection. The gate
  now judges the body-rewritten target; a workflow-governed JE submit via `frappe.client.submit` is
  caught, same as the URL-path shape.
- **LOW** — `_doctype_from_doc_param` now strips the extracted doctype (was returned unstripped;
  deny-biased before, consistent now).

Then a **completeness audit against frappe 17 source** (a second adversarial pass, before ship)
found the name-allowlist was still incomplete and — more important — the wrong SHAPE. Closed by
recognising the docstatus change **by body content**, not by chasing method names:
- **`frappe.desk.form.save.submit`** — a bare module-level alias of `savedocs` (`submit = savedocs`),
  independently whitelisted and the endpoint the Desk UI actually hits for every Submit-button click.
  A literal match on `savedocs` missed it; now handled identically (action-driven).
- **The docstatus-by-body class.** `frappe.client.save`/`.insert`/`.insert_many`/`.bulk_update`
  submit or cancel a document whenever its body carries `docstatus` 1/2 — `Document.save()` detects
  the 0→1 / 1→2 transition and runs the real submit/cancel hooks. (The 0.5.0 docs wrongly called
  `bulk_update` "save-only, no docstatus move" — corrected.) A docstatus-1/2 body now rewrites to
  the per-doctype `"<DocType>.submit"`/`".cancel"` target; a **draft** body (docstatus 0/absent)
  stays the unchanged doctype-blind CREATE residual.
- **`frappe.desk.form.linked_with.cancel_all_linked_docs`** and any multi-doc save batch
  (`insert_many`/`client.bulk_update`) **deny-close** the moment they carry a docstatus-changing item
  — a mixed-doctype batch cannot be authorised by one per-doctype grant (a scoped credential cancels
  a graph through the broker's per-node-marker cascade instead).
- Recognition is now **content-based**, so the next alias frappe adds to this class is caught by the
  docstatus check rather than needing a new name in the list.

A THIRD adversarial pass closed three more single-request vectors: **`frappe.desk.form.save.discard`**
(draft 0→2, scoped as `<DocType>.discard`), **`run_doc_method` `method="discard"`** and its fully
dotted `frappe.handler.run_doc_method` spelling, and frappe's **separate v2 `/api/v2/method/bulk_update`**
(a distinct undecorated function that classifies to the bare name `bulk_update`, now in the multi-doc
deny-close set). That same pass proved the honest boundary: **a pure I/O-free classifier cannot be
provably complete** — a 2-hop laundering vector (a `Bulk Update` DocType record driving a submit via
its own fields, target doctype nowhere in the request) needs a DB read to close. So this is now a
**named residual**, and the complete answer — a **"deny-unknown"** posture flip (allowlist per-doctype
patterns + curated safe methods; deny any unrecognised generic-RPC method) — is **staged as its own
increment** (real breakage risk → fresh redteam + Gate 10 bench, not folded in blind). The broker's own
credential is already scoped that way, so the broker is unaffected. See README "Known residual".
- Regression tests pin every case (`test_scope.py::TestBodyScopedTargetRedteamGaps` /
  `TestBodyScopedTargetCompletenessAudit`, `test_enforce.py::TestBodyDoctypeScoping` /
  `TestWorkflowEnforcement`). Still knowledge-pinned, live re-prove is Gate 10.

## 0.5.0 — 2026-07-06

**Closes the body-doctype residual for submit/cancel — the guard's own scope gate now enforces
per-doctype on `frappe.client.submit`/`.cancel`, `run_doc_method`, and Desk `savedocs`
Submit/Update/Cancel, not just the URL-path `run_method` vector.** Driven by the Journal Entry
submit/cancel blocker (`SCOPED-TOKEN-PROOF.md` PHASE L): ERPNext's `JournalEntry` overrides
`submit()`/`cancel()` without `@frappe.whitelist()` (background-queues >100-row entries), so
frappe's REST handler 403s the item-URL `run_method=submit` shape the guard could already scope
per-doctype. The only frappe-accepted alternatives (`frappe.client.submit`/`.cancel`, `savedocs`)
carry their target doctype in the request BODY — exactly the guard's own pre-existing, disclosed
"generic-RPC footgun" residual (a `methods` grant matches by name only, doctype-blind). This closes
that residual for the submit/cancel shapes specifically, so those RPCs become safe to grant
per-doctype and Journal Entry (and any other override-doctype) can be unblocked on the broker side
without reopening the bypass the residual describes.

BUILD only — no bench available in this worktree. Every frappe request-body shape this reads
(`frappe.client.submit`'s `doc` param, `.cancel`'s plain `doctype` param, `run_doc_method`'s
`dt`/`dn`/`docs` (v1) and `document` (v2) params, `savedocs`' `doc`/`action`) was read from frappe
source (`frappe/client.py`, `frappe/handler.py`, `frappe/api/v2.py`, `frappe/desk/form/save.py`),
not exercised against a live bench. Knowledge-pinned, not live-verified — a future bench gate (Gate
10) closes PHASE L's `⛔ BLOCKED` status.

### Added
- **Pure core** (`scope.py`, bench-free, unit-tested in `test_scope.py`): `body_scoped_target(kind,
  target, http_method, form)` — resolves a body-doctype submit/cancel RPC to the SAME per-doctype
  `("method", "<DocType>.submit"/"cancel")` shape `classify()`'s URL-path `run_method` vector
  already produces, so `is_permitted`'s existing `kind == "method"` branch (a `scope.methods`
  fnmatch) enforces it identically — no new enforcement branch, no `resource_doctypes` involvement.
  Recognises `frappe.client.submit` (doctype from the `doc` body param, JSON string or dict — same
  shape `_doctype_from_doc_param` already reads for `savedocs`), `frappe.client.cancel` (doctype
  from a **plain sibling** `doctype` param — NOT nested in `doc`, unlike submit/save/savedocs),
  `run_doc_method` (v1: `dt` directly when present, else the `docs` param; v2: the `document`
  param — both routed through the same doc-param helper), and `frappe.desk.form.save.savedocs`
  with `action` `Submit`/`Update` (→ `.submit`) or `Cancel` (→ `.cancel`). **Fails CLOSED**: a
  recognised submit/cancel RPC whose doctype can't be extracted (malformed/missing body, or an
  unrecognised/missing `savedocs` `action`) rewrites to `("other", None)` — it never falls back to
  the original doctype-blind target, which would silently reopen the bypass for any credential
  holding a blind grant. **Deliberately narrow**: `frappe.client.save`/`.insert` and any
  `run_doc_method` call naming a non-submit/cancel controller method return `None` (untouched,
  doctype-blind) — a documented, still-open residual, not silently folded in.
- **Frappe glue** (`enforce.py`): `check_scope` calls `body_scoped_target` on the already-classified
  `(kind, target)` and feeds the result (or the original pair, unchanged, if it returns `None`) into
  **new** `perm_kind`/`perm_target` variables passed to `is_permitted`. The **original** `kind`/
  `target` continue unchanged into the (separate, opt-in) `enforce_workflow` gate's
  `is_docstatus_changing`/`docstatus_target_doctype` calls — that gate's own pre-existing
  generic-RPC residual (a bare `frappe.client.submit` matches its shape check by name but derives
  the nonsense "doctype" `"frappe.client"`) is untouched by this change, on purpose: composing the
  two gates by rewriting the SAME variable would have coupled two independently-tested,
  independently-residualed mechanisms.

### Changed
- **Behavior, strictly stronger, not backward compatible for these four RPC names specifically.** A
  credential granted only the bare method name (`"frappe.client.submit"`, `"frappe.client.cancel"`,
  `"run_doc_method"`, or `"frappe.desk.form.save.savedocs"`) can no longer submit/cancel an
  arbitrary doctype through it — the credential must ALSO hold `"<DocType>.submit"`/`".cancel"` for
  the specific doctype the call targets, exactly as the URL-path `run_method` vector already
  required. A pre-existing grant that relied on the bare name to cover submit/cancel across
  doctypes will start seeing scope denials on upgrade; the fix is to add the per-doctype method
  pattern(s) the credential actually needs (the same pattern already required for the URL-path
  vector). `savedocs`' plain draft `action=Save` is UNCHANGED (still matched by the bare method
  name only — it never rewrites, since a draft save is not a docstatus move).

### Honest residuals — updated
- **The body-doctype residual (README "generic-RPC footgun" / CHANGELOG 0.4.0) is now CLOSED for
  submit/cancel, knowledge-pinned until a live bench gate (Gate 10) proves it.** Still open,
  unchanged: `frappe.client.save`/`.insert`/`.set_value`, non-submit/cancel `run_doc_method` calls,
  and the top-level `bulk_update`/`bulk_delete` remain doctype-blind — grant those with care. The
  `enforce_workflow` gate's own copy of this residual (documented in its 0.4.0 entry above) is
  ALSO unchanged — it still runs on the original, un-rewritten classification, by design (see
  "Added" above).

## 0.4.0 — 2026-07-03

**Bench-side Workflow enforcement — closes "governs the agent's path only".** The Pacioli broker's
own `pacioli/workflow.py` gate refuses a workflow-governed submit on the agent's own path, but
frappe does NOT enforce Workflow on a direct `docstatus` change (`validate_workflow` only fires
when `workflow_state` itself changes on save — see that module's "Honest limit #1"). That meant the
SAME scoped credential could submit around a configured approval chain via a raw REST call that
never touches the broker at all. This closes that gap **at the credential layer**, upgrading
"governs the agent's path" to "governs every **api-key** path through this credential" — belt
(broker) and suspenders (guard).

BUILD only — no bench available in this worktree. Every frappe shape this relies on is
knowledge-pinned (see below); nothing here is claimed "live-proven". That proof is a future bench
gate, the same way `pacioli/workflow.py`'s own Honest limit #2 states for its own shapes.

### Added
- **`enforce_workflow`** — a new Check field on *API Key Scope* (default `0`, **opt-in, OFF**).
  When on, `check_scope` runs a new gate AFTER the existing scope allowlist: a docstatus-changing
  call — `submit`/`cancel` by method name (covers the v1 item-url `run_method`, the v2 path-carried
  doc-method, and the legacy `?cmd=` route alike, since `classify` already funnels all three into
  the identical `("method", "<DocType>.submit")` shape), or a raw `PUT`/`PATCH` to
  `/api/resource/<dt>/<name>` (v1) or `/api/v2/document/<dt>/<name>` (v2) whose body carries a
  `docstatus` key; a `POST` **create** carrying a *submitting* `docstatus` 1/2 (insert-as-submitted;
  a draft `docstatus: 0` still passes); and the Desk UI's `frappe.desk.form.save.savedocs` with an
  `action` other than a plain `"Save"` (its doctype read from the `doc` body param) — each against a
  doctype with an active frappe Workflow is refused unless the call IS
  `frappe.model.workflow.apply_workflow`. Off by default per-credential, same reasoning as every
  CONTAIN field before it: a gate that can newly deny previously-passing calls the instant it's on
  must never flip site-wide-default-on, or a live credential breaks on upgrade with no warning.
- **Pure core** (`scope.py`, bench-free, unit-tested in `test_scope.py`):
  `is_docstatus_changing(kind, target, http_method, form)` — the shape check above, including the
  `apply_workflow` allowlist exception (never flagged, regardless of what shape it would otherwise
  match) — and `docstatus_target_doctype(kind, target, form)`, extracting the doctype name a flagged
  target names (from the method name, the resource tuple, or — for `savedocs` — the `doc` body
  param), for the workflow-existence lookup. `ApiScope` gains `enforce_workflow: bool = False`
  threaded through `from_dict`/`from_grant` exactly like `enabled`/`rate_limit_per_minute`/
  `resource_verbs` before it: `None`/absent reads as off, so a doc loaded before `bench migrate`
  adds the column (no attribute at all) behaves as off, not a crash.
- **Frappe glue** (`enforce.py`): `_scope_from_doctype` reads `getattr(doc, "enforce_workflow",
  None)`, same backward-compatible pattern as the CONTAIN pair. A new `_active_workflow_name(doctype)`
  calls `frappe.model.workflow.get_workflow_name(doctype)` **directly** — this hook runs as
  frappe-internal code (inside `validate_auth`, after the api-key already authenticated), so it
  reads Workflow existence with NO System-Manager REST wall (unlike the broker, which has to go
  through a permissioned API call) and NO recursion risk (auth_hooks fire once per request; this is
  an internal ORM/cache call, not another request). Deny-biased on error: if the lookup itself
  raises, that is treated the same as "this doctype has a workflow" for a call that already looks
  docstatus-changing — an unverifiable answer is never read as "no workflow".

### Honest residuals — stated, not silently left uncovered
- **Generic-RPC footgun (the guard's own pre-existing disclosed limit, now shared by this gate
  too).** `frappe.client.submit`/`cancel`/`set_value`, v2 `run_doc_method`, and the top-level
  `bulk_update`/`bulk_delete` carry their REAL target doctype in the request body, not their method
  name. They slip two different ways: `frappe.client.submit`/`cancel` DO match the name-suffix
  check, but the "doctype" derived from the name is `"frappe.client"` — never a real doctype — so
  the workflow lookup finds nothing and the call passes; `set_value`/`run_doc_method`/`bulk_*` never
  match the suffix check at all. Either way a whitelisted method that flips docstatus on a
  body-named doctype stays invisible to this gate, exactly as it already was invisible to the scope
  gate. (`frappe.desk.form.save.savedocs` was in this list in the first draft — it is now covered
  specifically, reading its doctype from the `doc` body param, because it is the Desk UI's own
  high-traffic path; extending the same per-method body-parsing to every generic RPC is open-ended
  and deliberately not attempted.)
- **Credential-layer boundary (unchanged from the rest of this hook).** Fires only for
  api-key/`Basic` requests (`_scope_for_request`'s Authorization gate) — NOT OAuth Bearer,
  desk/cookie sessions, background jobs, the bench console, or script/report calls. This closes the
  gap for *other scoped api callers bypassing the broker*, not an ERPNext-wide Workflow patch — only
  frappe core touching `validate_workflow` itself could do that. Stated as plainly as
  `pacioli/workflow.py`'s "Honest limit #1" states the broker's own equivalent boundary.
- **Ambiguous/malformed workflow config — NOT mirrored from the broker's sentinels.** The broker's
  own pure core (`find_active`) explicitly detects and refuses on more than one active Workflow for
  a doctype, naming an `Ambiguous`/`Malformed` sentinel. `frappe.model.workflow.get_workflow_name`
  is not known to expose that distinction at all — knowledge-pinned, not verified against a live
  bench — so if a site somehow carries more than one active Workflow for one doctype, this gate has
  no way to detect or flag it; it silently governs by whichever workflow frappe's own lookup
  happened to return. Do not assume this gate's ambiguity handling matches the broker's.
- **Knowledge-pinned, not live-verified (mirrors `pacioli/workflow.py`'s own "Honest limit #2").**
  Two shapes this gate depends on have not been exercised against a live bench: (1)
  `get_workflow_name`'s import path and its cached/falsy-for-none-else-name-string return contract;
  (2) whether a raw `PUT`/`PATCH` request's JSON body genuinely surfaces its `docstatus` key through
  `frappe.form_dict` the way the fake test harness models it. On (2), `broker/pacioli/erpnext.py`
  already flags the general shape of this uncertainty for a DIFFERENT call (`erpnext.py:17-18`:
  submit sends `run_method` in the **query string**, not the form body, specifically "so the
  classifier's `form_dict` read always sees it, whatever the body encoding") — this gate leans on
  the same `frappe.form_dict` read, but for a plain JSON request body it did not choose the
  encoding of, so that guarantee does not obviously transfer. Live falsification of both is a
  future bench gate, not implied by anything here.

## 0.3.0 — 2026-07-03 (unreleased)

**CONTAIN — the credential floor grows the sixth pillar** (agent danger is velocity under
hijack). **Live-proven on the bench (2026-07-02, GO-LIVE Gate 4):** the app was reinstalled under
`pacioli_guard` and migrated (the two fields landed); unticking `enabled` flipped the credential
from 200 → 403 on its very next call and re-ticking restored it (no restart); a
`rate_limit_per_minute` of 3 let exactly three calls through in a window then denied the rest
naming the limit; and every denial (kill / rate / out-of-scope) left a `Pacioli Guard denied a
request (<reason>)` row in the Error Log. Alpha dropped.

### Added
- **Kill switch** — `enabled` Check on *API Key Scope* (default 1). Unticked = every request
  from that credential denied at the chokepoint, effective on the next request (no restart).
  Decision in the pure core (`ApiScope.enabled`; `is_permitted` refuses a disabled scope —
  defense in depth) with one-edge coercion: the field's ABSENCE (pre-CONTAIN grant) reads as
  enabled — absence is not a kill; any present value coerces deny-biased (an ambiguous `""`
  kills).
- **Per-credential rate limit** — `rate_limit_per_minute` Int (0 = no limit). Pure
  `is_rate_allowed` decision; counting via `frappe.cache()` INCR+EXPIRE on fixed one-minute
  windows (boundary burst ~2x nominal, stated). Order kill → rate → scope; every request burns
  budget (total velocity is what's contained). A cache failure with a limit set fails CLOSED for
  that credential only — opting into a limit is opting into containment; no-limit grants never
  touch the counter.
- **Denied-call audit trail** — every denial (out-of-scope / kill / rate) writes a row via
  `frappe.log_error`, wrapped so a failure to LOG can never suppress the DENY. Chose the stock
  Error Log over a bespoke DocType: zero new schema/permission surface, and denial logs are
  diagnostics — the tamper-evident ledger is the broker's PROVE leg, not duplicated here.

### Added — per-credential resource-verb scoping (closes the redteam's one design gap)
- **`Allow Read` / `Allow Create` / `Allow Write` / `Allow Delete`** on *API Key Scope* (all default
  on). Before this, a `resource_doctypes` allowlist admitted **every** CRUD verb — a credential
  meant to *read* Sales Invoices silently also POSTed/PUT/DELETEd them, undercutting the
  least-privilege promise. Now a credential can be locked to e.g. read-only (or read+create, the
  broker's own posture) across its granted DocTypes. Pure core: new `ApiScope.resource_verbs`
  (empty = all verbs, backward compatible) + `_clean_verbs`; `is_permitted` checks the verb from
  `classify` (which it computed all along but never consulted for resource CRUD). Migrate adds the
  four Check fields defaulting to 1, so an existing grant is unchanged. Narrowing is
  per-credential; per-DocType verb granularity is a stated future increment.

### Hardened (independent fresh-eyes redteam of this release, before it ships)
- `check_scope` now `return`s explicitly after each `_deny()` — the kill and rate denials no longer
  *rely* on `frappe.throw` always raising to stop control flow (the scope deny has `is_permitted`
  as a downstream backstop; the kill/rate denies did not). Defense in depth, no behavior change.
- README documents the revoke footgun the redteam surfaced: **untick `enabled` to revoke, don't
  delete the scope doc** — on a site still carrying a legacy `User.api_scope` grant, deletion can
  fall through to that older (pre-`enabled`) grant and silently reopen the credential.

## 0.2.0 — 2026-07-02 (unreleased)

### Changed
- **License: MIT → Apache-2.0** (pre-any-release, sole author — no downstream affected). Matches
  the family (Proximo, Maude) + Apache's express patent grant; `hooks.py` `app_license` updated.

### Changed (breaking — done deliberately BEFORE any public install exists)
- **Frappe app_name / import module renamed `guard` → `pacioli_guard`** (hooks `app_name`, the
  `auth_hooks` path, the module directory, all imports, packaging includes). A Frappe bench has a
  **flat app namespace** (`apps/<name>`, `installed_apps`, `import <name>`), and `guard` is about
  the most generic name a security app could squat — a collision on a customer bench is a hard
  install block in both directions, plus the same squat at the Python top-level. Renaming now costs
  one commit; renaming after installs means migrating `installed_apps` and DocType module rows in
  every customer database. One name now runs the whole chain: PyPI `pacioli-guard` → app/module
  `pacioli_guard`.
- No behavior change: the decision core, enforcement, DocTypes, and tests are byte-identical apart
  from the import path. 233 tests green (74 guard + 159 broker).
- Historical note: proof records made before this date (`SCOPED-TOKEN-PROOF.md` PHASES B/C) show
  `_exc_source: "guard (app)"` — that was this same app under its old name; the records stand as
  observed.

## 0.1.1 — 2026-07-01

Security fixes from a fresh-eyes redteam that read Frappe's real dispatcher, **verified live**.
On the sealed bench, both bypasses returned 200 + leaked data on 0.1.0 and **403 on 0.1.1** with
the identical scoped credential, while every legitimate call still passed (`SCOPED-TOKEN-PROOF.md`
PHASE C).

### Security
- **Closed the legacy `?cmd=` RPC bypass** (was: total bypass). Frappe routes on `frappe.form_dict.cmd`
  *before* the URL path, so a credential with one allowlisted method could smuggle any whitelisted
  call via `?cmd=`. `classify` now treats a present `cmd` as the real target; `enforce` passes it.
- **Closed the `run_method` CRUD misclassification** (was: raw-CRUD bypass reopened). Frappe honours
  `?run_method` only on an **item** URL (read_doc GET / execute_doc_method POST); on a collection
  (create/list) or item PUT/PATCH/DELETE it ignores it and does real CRUD. `classify` now honours
  `run_method` only where Frappe does, and classifies the rest by their true verb.

### Packaging
- DocType schema (`api_key_scope*.json`), `modules.txt`, and `patches.txt` now ship in the built
  sdist/wheel (`MANIFEST.in` + `include-package-data`) — a built distribution no longer installs an
  app whose auth-hook fires against a never-migrated DocType.
- Distribution renamed `guard` → `pacioli-guard` (the bare `guard` is taken/generic on PyPI); the
  Frappe app_name / import module stays `guard`.

### Honest scope (unchanged, now stated in the README)
- Scopes `token`/`Basic` REST credentials across `/api` v1+v2. **OAuth2 `Bearer` is NOT scoped.**
  Internal `frappe.client` RPC and background jobs are out of band. A `methods` entry matches by
  name only (does not constrain a generic RPC's body-supplied target).

## 0.1.0 — 2026-07-01

- Live-proven credential-scope boundary on a real Frappe v16 bench (see `../SCOPED-TOKEN-PROOF.md`):
  a scoped credential denied on both `/api/method` and `/api/resource`, Frappe attributing the 403
  to this app, with no core fork (public `auth_hooks`).
- First-class **API Key Scope** DocType (child-table allowlists); deprecated JSON-field fallback.
- Earlier hardening: Basic-auth scheme parity, `/api/v1`+`/api/v2` classification, v2 slash-named
  doc-method fix, `Frappe-Authorization-Source` alt-source fail-open fix.
