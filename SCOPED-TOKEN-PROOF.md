# Scoped-API-token patch — LIVE PROOF (2026-07-01)

> References to `docs/plans/…` (and other build-record files: `GO-LIVE.md`, `docs/specs/…`, scout notes, redteam reports) are the workshop's internal run records — the day-books behind
> each phase. This document is the public proof arc; the day-books stay in the workshop.

> **v0.1.1 fixes RE-PROVEN LIVE (2026-07-01).** A fresh-eyes review (reading Frappe's real
> dispatcher) found two fail-open shapes the 0.1.0 classifier missed — a legacy `?cmd=` RPC that
> routes before the path, and `run_method` on collection/PUT/DELETE URLs that Frappe ignores. Both
> were fixed, unit-tested, **and then verified end-to-end on the live bench** (PHASE C below): on
> 0.1.0 they returned **200 and leaked data** (the User list via `?cmd=`, a ToDo list via
> `?run_method=`); on 0.1.1 the identical requests return **403** (guard's own PermissionError),
> while every legitimate call still passes. The bypasses were found by source-reading and *confirmed
> by running them* — this project's discipline: trust checked, not tidy.

The `scoped-api-tokens` Frappe patch is **live-proven** against a real Frappe v16 bench.
This closes the "live integration needs a running bench to verify" caveat the patch's own
commit message called out — the security-critical decision logic already had 15 bench-free
unit tests; this proves the *wiring* (auth → scope-load → enforce chokepoint) in a real bench.

## ⭐ SHIPPABLE FORM — installable Frappe app, NO core fork (PROVEN 2026-07-01)

**The decisive result.** The same boundary was re-proven with the **core patch fully reverted to
stock Frappe 16.25.0**, enforced instead by an **installed Frappe app** (`pacioli_guard`) that
registers frappe's public **`auth_hooks`** extension point. Frappe's `validate_auth()` runs
`auth_hooks` *after* the api-key authenticates and *without a try/except* around the loop — so an
app-registered hook both sees the credential (re-read from the `Authorization` header) and can
`frappe.throw(PermissionError)` into a real 403, classifying method-vs-resource from the request path.

Same 4-leg matrix, core reverted, app-only:

| # | User | Call | Got | Source |
|---|------|------|-----|--------|
| 1 | scoped | `GET /api/method/frappe.auth.get_logged_user` | **200** | — |
| 2 | scoped | `GET /api/method/frappe.client.get_list` | **403** | `_exc_source: pacioli_guard` |
| 3 | scoped | `POST /api/resource/ToDo` | **403** | `_exc_source: pacioli_guard` |
| 4 | unscoped | `POST /api/resource/ToDo` | **200** | — |

Frappe stamps `_exc_source: pacioli_guard` on the 403s — it attributes the denial to the **app**, not
core. **So the credential-scope boundary ships as a `bench install-app` on stock ERPNext — no fork,
nothing for a customer to patch.** This is what makes Pacioli's trust-by-construction story deployable.

**Honest gaps in the app form (not chased):** classifies from the URL path (not the resolved
endpoint), so frappe's full routing surface (`/api/v2/*`, legacy `?cmd=`, `run_doc_method` variants,
encoded DocType names) needs coverage; only `token key:secret` auth is handled (session=unscoped by
design is correct; OAuth-bearer→scope lookup is a TODO); positive resource grant (`allow_resource:true`
+ doctype allowlist) and `execute_doc_method` are coded but untested; a real customer install is
`bench install-app` from a published repo (here `add_to_installed_apps` was used to skip asset-build
in the sealed lab); `api_scope` is still prototype JSON-on-User, prod = an "API Key Scope" DocType.

## What the patch does
A Frappe API key authenticates *as a user* and inherits that user's full role permissions on
**every** endpoint. So a token minted so an agent can call one governed `/api/method` endpoint
can equally `POST /api/resource/<DocType>` directly, sidestepping whatever that method wrapped.
The patch binds a credential to an allowlist (`api_scope` JSON on the credential source) and
enforces it once, centrally, in `frappe/api/handle()` between route-resolve and dispatch —
closing the bypass for both `/api/method/*` and `/api/resource/*`. Unscoped credentials
(sessions, normal keys) are untouched (`is_permitted(None, …)` is always True).

- Patch: workshop-internal frappe branch `scoped-api-tokens` @ `cc298cffcd` (+288L)
  - `frappe/api/scope.py` — pure `classify` + `is_permitted` + lazy-frappe `enforce`
  - `frappe/api/__init__.py` — `enforce(endpoint, arguments)` at the dispatch chokepoint
  - `frappe/auth.py` — `_load_api_scope()` after `set_user` (api_key + OAuth paths)

## Environment
- A sealed, network-isolated ERPNext test bench (private lab; exact host in the internal runbook).
- ERPNext v16.26.0 / **Frappe v16.25.0**, bare-metal, Python 3.14, systemd `frappe-bench`, `erpnext.localhost`.
- Patch applied by **anchor-verified idempotent insertion** (not `git apply` — the live 16.25.0
  `handle()`/OAuth context differs from the patch's base; a blind `git apply` would have rejected).
  Both edited files compile-checked before the bench restart.
- Two users, **identical except the scope** (both `System Manager`, so a scoped 403 can only be
  the enforcement, never a missing role permission):
  - scoped   → `api_scope = {"methods": ["frappe.auth.get_logged_user"], "allow_resource": false}`
  - unscoped → no `api_scope`
- Pre-flight gate (verified before curling): `frappe.db.get_value("User", scoped, "api_scope")`
  returned the JSON — the scope really persisted (the patch fails *open* on a missing field, so
  this rules out a 403 that's actually "silently unscoped").

## The proof (real HTTP codes, curl'd inside the CT with `-H 'Host: erpnext.localhost'`)

| # | User     | Call                                          | Expect | Got  | Evidence |
|---|----------|-----------------------------------------------|--------|------|----------|
| 1 | scoped   | `GET /api/method/frappe.auth.get_logged_user` | 200    | **200** | `{"message":"scoped@proof.local"}` |
| 2 | scoped   | `GET /api/method/frappe.client.get_list`      | 403    | **403** | `PermissionError … (method: frappe.client.get_list)` |
| 3 | scoped   | `POST /api/resource/ToDo`                     | 403    | **403** | `PermissionError … (resource: ('ToDo', 'create'))` |
| 4 | unscoped | `POST /api/resource/ToDo`                     | 200    | **200** | created ToDo `stbq15j18b` |

**Why this is conclusive**
- **T1 (200) + T2 (403) together** prove the scope is loaded and the method path enforces in
  *both* directions — an allowlisted method passes, a non-allowlisted one is denied. A silently-
  unscoped credential would have returned 200 on both.
- **T3 (403)** proves the raw-CRUD bypass is closed (`allow_resource` defaults False); the classify
  output `('ToDo', 'create')` confirms resource classification is correct.
- **T4 (200)** proves backward compatibility — the *same* System-Manager role, unscoped, creates
  the resource fine. So T3's 403 is the scope, not the role.

## Relevance to Pacioli
This is the frappe-layer foundation for Pacioli's least-privilege / CONSENT story: a per-credential
capability boundary that holds at the REST chokepoint, below any MCP layer. A production version
models `api_scope` as its own "API Key Scope" DocType; the enforcement seam is identical.

---

## PHASE B — RE-PROOF RUNBOOK (planned; pending a live bench + John's operator arm)

> Everything below the original 4-leg proof (2026-07-01) has since been hardened but is proven ONLY
> by bench-free unit tests (58 green). The seam moved from prototype JSON-on-User → **"API Key Scope"
> DocType**, gained **/api/v1 + /api/v2** classification, and closed a **CRITICAL Basic-auth bypass**
> + the **Frappe-Authorization-Source alt-source fail-open**. Unit-green ≠ wired-green — this runbook
> is the end-to-end re-proof. Bring the sealed bench up, run the matrix inside it, then tear it down.

**Pre-flight**
1. Bring up the sealed ERPNext test bench (private lab; steps in the internal runbook) and
   wait for it to be reachable.
3. **DocType install path** — the prototype used `add_to_installed_apps` (skips asset build). Verify
   the **"API Key Scope" DocType** actually migrates/discovers in the sealed CT (3-level module path
   `guard/guard/scoping/doctype/`). If `bench migrate` can't build assets offline, confirm the
   DocType + child tables exist in the DB another way before trusting a 403.
4. Pre-flight gate (rule out "silently unscoped"): confirm the scoped user's **API Key Scope** grant
   persisted (query the DocType), exactly as the original proof gated on the JSON field.

**Curl matrix** (all with `-H 'Host: erpnext.localhost'`, inside the CT). Expands the original 4 legs:

| # | Scheme | User | Call | Expect | Proves |
|---|--------|------|------|--------|--------|
| 1 | `token` | scoped | `GET /api/method/frappe.auth.get_logged_user` | 200 | allowlisted method passes |
| 2 | `token` | scoped | `GET /api/method/frappe.client.get_list` | 403 | off-list method denied |
| 3 | `token` | scoped | `POST /api/resource/ToDo` | 403 | raw-CRUD bypass closed |
| 4 | `token` | unscoped | `POST /api/resource/ToDo` | 200 | backward compat (role ≠ scope) |
| 5 | **`Basic` (`curl -u key:secret`)** | scoped | `GET /api/method/frappe.client.get_list` | **403** | **CRITICAL Basic-auth bypass fix** — same key via Basic is still scoped |
| 6 | `Basic` | scoped | `GET /api/method/frappe.auth.get_logged_user` | 200 | Basic scheme resolves the grant (not a blanket block) |
| 7 | **`token` + `Frappe-Authorization-Source`** | scoped (bound to a non-User doctype linking to the scoped user) | `POST /api/resource/ToDo` | **403** | **alt-source fail-open fix** — credential scoped to its owning user |
| 8 | `token` | scoped | `POST /api/v2/document/ToDo` (v2 CRUD) | 403 | v2 `/document/` CRUD classified |
| 9 | `token` | scoped | v2 doc-method on a slash-named doc `.../method/<m>` | per-grant | v2 path-carried doc-method (the slash-name CRITICAL fix) |

Leg 5 is the one that would have passed (bypassed) before the Basic-auth fix; leg 7 the one that would
have passed before the alt-source fix. Both are the point of this re-proof.

---

## PHASE B — RESULTS ✅ PROVEN (2026-07-01, live)

Re-proven end-to-end on the **sealed ERPNext test bench**, **Frappe 16.25.0 /
ERPNext 16.26.0 / Python 3.14.6**, The **`guard` app 0.1.0** (the app's name at proof time — renamed
`pacioli_guard` on 2026-07-02, pre-any-public-install; these records stand as observed) (current
repo `f653e37`) installed on stock Frappe with **no core fork**: `.pth` + `apps.txt` + `add_to_installed_apps`
+ `bench migrate` — the migrate created the **API Key Scope** DocType + two child tables **offline (no asset
build)**, closing the flagged "does the DocType bench-migrate path work in the sealed CT" gate. The old
prototype **`pacioli_guard` was stood down** (`remove_from_installed_apps`; `installed_apps` = frappe/erpnext/guard)
so **guard is the sole enforcer** — every 403 below carries `_exc_source: "guard (app)"` (v1) or a
`apps/guard/guard/enforce.py` traceback frame (v2). Scoped user + unscoped user both **System Manager**
(so a 403 is the scope, never a missing role); scoped grant = `{methods:["frappe.auth.get_logged_user"], allow_resource:0}`, stored as the **API Key Scope DocType** (not the legacy JSON field).

| # | Scheme | User | Call | Expect | Got | Enforcer |
|---|--------|------|------|--------|-----|----------|
| T1 | token | scoped | `GET /api/method/frappe.auth.get_logged_user` | 200 | **200** | — (allowlisted) |
| T2 | token | scoped | `GET /api/method/frappe.client.get_list` | 403 | **403** | guard (method: frappe.client.get_list) |
| T3 | token | scoped | `POST /api/resource/ToDo` | 403 | **403** | guard (resource: ('ToDo','create')) |
| T4 | token | unscoped | `POST /api/resource/ToDo` | 200 | **200** | — (backward compat) |
| **T5** | **Basic (`curl -u`)** | scoped | `GET /api/method/frappe.client.get_list` | 403 | **403** | **guard — CRITICAL Basic-auth fix** |
| T6 | Basic | scoped | `GET /api/method/frappe.auth.get_logged_user` | 200 | **200** | — (Basic resolves the grant) |
| T7 | token | scoped | `GET /api/v1/method/frappe.auth.get_logged_user` | 200 | **200** | — (v1 alias) |
| T8 | token | scoped | `GET /api/v2/document/ToDo` | 403 | **403** | guard (v2 `/document/` classify) |
| **T9** | token (multi-colon) | — | `Authorization: token x:secret:extra` | 401 | **401** | **frappe 401 — regression fix (was 500 risk)** |

**Live before/after nugget:** in the FIRST pass (pacioli_guard *still* installed), T2/T3 (token) were caught
by the **old** `pacioli_guard`, but **T5 (Basic) fell PAST it to guard** — the old code's Basic-auth bypass,
observed live. Standing pacioli_guard down and re-running attributed all legs to guard.

**Alt-source (original runbook leg 7):** the design changed under redteam review — Pacioli now scopes
`frappe.session.user` (the executing principal), and **frappe itself** resolves a `Frappe-Authorization-Source`
credential into `session.user` *before* the hook runs. So the alt-source credential is covered by the same
`session.user` path proven in T1–T9; frappe's source-doctype→user resolution is frappe's own (verified against
`auth.py`), not Pacioli code, so it was **not separately re-staged** with a synthetic doctype. If a real
alt-source doctype is ever added to a deployment, T7-style coverage applies unchanged.

**Verdict:** the credential-scope boundary — method allow/deny, raw-CRUD deny, unscoped untouched, across
**token + Basic + v1 + v2**, with the multi-colon path failing **closed (401, not 500)** — holds end-to-end
on stock Frappe as an installable app. The bench-free unit suite (63 green) proves the logic; this proves the
wiring.

---

## PHASE C — v0.1.1 bypass fixes, LIVE before/after ✅ PROVEN (2026-07-01)

A fresh-eyes redteam (reading Frappe's real `app.py`/`api/v1.py` dispatcher) found two fail-open
shapes 0.1.0 missed. Both were reproduced live on the bench against deployed 0.1.0, then re-run
against the patched 0.1.1 — **same scoped user, same requests**. Scoped grant for this pass:
`{methods:["frappe.auth.get_logged_user","ToDo.*","erpnext...show_accounting_ledger_preview"], allow_resource:0}`,
both users System Manager (so a 403 is purely the scope).

| # | Request (scoped credential) | 0.1.0 | 0.1.1 | What it proves |
|---|---|---|---|---|
| C1 | `GET /api/method/frappe.auth.get_logged_user` (allowlisted) | 200 | **200** | legit method access preserved |
| C2 | `GET /api/method/frappe.client.get_list` (off-list) | 403 | **403** | baseline enforcement intact |
| C3 | `POST /api/resource/ToDo` (allow_resource=0) | 403 | **403** | raw-CRUD still denied |
| **C4** | `GET /api/method/<allowed>?cmd=frappe.client.get_list&doctype=User` | **200 + leaked the User list** | **403** | **`?cmd=` legacy-RPC total bypass — CLOSED** |
| **C5** | `GET /api/resource/ToDo?run_method=x` (collection list) | **200 + leaked the ToDo list** | **403** | **`run_method` CRUD misclassification — CLOSED** |
| C6 | `GET /api/resource/User?run_method=x` (User ∉ `ToDo.*`) | 403 | **403** | the bypass was scope-bounded, not a blanket pass (control) |
| C7 | `GET /api/resource/ToDo/<name>?run_method=get_x` (legit item doc-method) | method-classified | **404 (frappe), not a guard 403** | the legitimate doc-method-over-GET path still passes |

On 0.1.1 every C4/C5 403 carries guard's own `PermissionError: This credential is scoped and is
not permitted to call this endpoint` — i.e. **guard** blocked it, not a downstream accident.

**Broker PLAN endpoint (SPEC §7 #1 estimate-blower), partial:** `show_accounting_ledger_preview`
is `@frappe.whitelist()` and **reachable over REST**, and **guard correctly passes it when it is in
the credential's method allowlist** (a call to it returned a PermissionError whose `_exc_source` was
`"erpnext (app)"`, **not guard** — guard allowed it; ERPNext raised on the bogus company/doc).
The full `plan_submit → mint → submit` against a *real* draft Sales Invoice was the remaining live
step for the broker — completed as PHASE D below (the bench was made healthy 2026-07-02: `bench
migrate` + ERPNext `after_install` + base fixtures + a real company/customer/item/draft invoice).

## PHASE D — broker Gate 2: the full governed vertical, LIVE E2E ✅ PROVEN (2026-07-02)

The **real `pacioli` 0.1.0 package** (sha256-verified transfer into the sealed bench) ran the whole
PLAN → CONSENT → execute → PROVE vertical against a real draft Sales Invoice (`grand_total` 100.00,
docstatus 0), authenticated as the **scoped non-Administrator broker user under guard 0.1.1** — the
deployment posture SPEC §5 prescribes. Registry target: the bench site over loopback,
`api_secret = "env:…"` (by reference), company-pinned.

**Scoped grant for this pass** (guard's API Key Scope DocType, the narrowing layer over a
role-broad user): `methods = {frappe.auth.get_logged_user, erpnext…show_accounting_ledger_preview,
Sales Invoice.submit}`, `allow_resource = 1`, `resource_doctypes = {Sales Invoice, Accounts
Settings, Period Closing Voucher, Accounting Period}` — the last three because `get_period_locks`
reads them before every submit and an unreadable lock source is a deny.

| # | Leg (all through the broker's dispatch, over HTTP) | Result | What it proves |
|---|---|---|---|
| D0 | Guard boundary curl matrix on the new grant | in-scope reads + preview **200**; ToDo/User **403** | guard narrows exactly to the Gate-2 grant |
| D1 | `get_sales_invoice` on the draft | **200**, docstatus 0 | scoped read tier works |
| D2 | `plan_submit` | **ok** — real projected GL (Debtors 100.00 D / Sales 100.00 C), no risk flags, plan bound to `modified` | PLAN = ERPNext's native preview, recorded durably |
| D3 | `submit` with a **bogus marker** | **deny, `stage: consent`** | no marker → no write, structured refusal |
| D4 | `pacioli mint <plan_id>` (human side, out of band) | marker minted, TTL 900s, hash-only stored | consent cannot be self-granted by the agent |
| D5 | `submit_sales_invoice` with the live marker | **ok — docstatus 0 → 1**; balanced, uncancelled GL Entry rows verified in the bench DB | the one governed write, real books moved |
| D6 | **Replay**: same plan + same marker resubmitted | **deny, `stage: fresh`** (doc changed after planning) | deny-biased ordering — a consented plan can never silently drift; replay dead |
| D7 | `plan_submit` on the now-submitted doc | **deny** (`not a draft`) | can't re-plan a posted document |
| D8 | `prove_verify` | **ok** — chain verifies, exactly 2 receipts (intent + outcome) | PROVE sealed the write it governed |
| D9 | `prove_orphans` | **none** | no intent without a proven outcome |

Every deny above is a structured `{ok: false, stage, reason}` — never a traceback. This closes
**GO-LIVE Gate 2**; `pacioli` dropped `a1` → **0.1.0** with no code change (version + status
wording only).

## PHASE E — UNDO Gate 3: the governed cancel, LIVE E2E ✅ PROVEN (2026-07-02, same day as PHASE D)

The **real `pacioli` 0.3.0 package** (sha256-verified transfer) unwound the very posting PHASE D
made — the same invoice, the same scoped non-Administrator credential, the guard grant widened for
UNDO by exactly three lines: `methods += {Sales Invoice.cancel,
frappe.desk.form.linked_with.get_submitted_linked_docs}`, `resource_doctypes += {GL Entry}`.

| # | Leg (all through the broker's dispatch, over HTTP) | Result | What it proves |
|---|---|---|---|
| E1 | `plan_cancel` on the submitted invoice | **ok** — projected reversal = the posting's live GL rows (Debtors 100.00 D / Sales 100.00 C), plan bound to `modified`, `op=cancel` | PLAN works in the unwind direction; the human sees exactly what a cancel undoes |
| E2 | — (inside E1) linked-submitted-docs read | empty graph (leaf) | the knowledge-pinned `frappe.desk.form.linked_with.get_submitted_linked_docs` REST shape **live-falsified and held** |
| E3 | `submit_sales_invoice` presented the **cancel marker** | **deny, `stage: plan`** — "authorizes 'cancel', not 'submit'; a consent grant does not transfer between operations"; grant untouched | **cross-op refusal, live** — consent is op-bound |
| E4 | `pacioli mint` for the cancel plan (human side) | marker minted, TTL 900s, hash-only | same consent ceremony, opposite direction |
| E5 | `cancel_sales_invoice` with the live marker | **ok — docstatus 1 → 2** | the governed unwind, real books |
| E6 | Bench DB check | **ERPNext's literal reversing GL rows present** — Debtors 100 D + 100 C, Sales 100 C + 100 D, all `is_cancelled=1`; docstatus 2 | the equal-and-opposite entries are rows, not metaphor |
| E7 | Replay: same plan+marker re-presented | **deny, `stage: fresh`** | deny-biased ordering holds in the unwind direction too |
| E8 | `plan_cancel` on the now-cancelled doc | **deny** (`not a submitted document`) | can't re-plan a dead posting |
| E9 | `prove_verify` / `prove_orphans` | **ok** — exactly 4 receipts (submit pair + cancel pair), zero orphans | one book, both directions, fully receipted |

Every deny is a structured `{ok: false, stage, reason}`. This closes **GO-LIVE Gate 3**;
`pacioli` dropped the alpha → **0.3.0** with no code change. Deferred honestly: cascade cancel
(a non-leaf graph refuses, never cascades silently) and amend — future increments.

## PHASE F — Gate 4: amend + off-box anchor + CONTAIN, LIVE E2E ✅ PROVEN (2026-07-02)

The worktree-cook trio (broker 0.4.0, guard 0.3.0), run against the sealed bench in one pass.

**Pre — the guard reinstall under the new name.** The bench still had the app installed under its
old flat name `guard`. Swapped to **`pacioli_guard`** (apps.txt + `add_to_installed_apps` /
`remove_from_installed_apps`) and `bench migrate`d offline: the two CONTAIN fields
(`enabled`, `rate_limit_per_minute`) were created on *API Key Scope*, and
`frappe.get_hooks('auth_hooks')` became `['pacioli_guard.enforce.check_scope']`. A bench restart
cycled the new enforcer into the web workers; the scoped credential's grant backfilled
`enabled = 1` (absence-is-not-a-kill) / `rate = 0`.

### (a) amend — the corrected re-draft + the full arc

| # | Leg | Result | Proves |
|---|---|---|---|
| F-a1 | `amend_sales_invoice` on the cancelled `ACC-SINV-2026-00001` (docstatus 2) | **ok** — new draft `ACC-SINV-2026-00001-1`, docstatus 0, `amended_from` = source | the re-draft is built, one hop back |
| F-a2 | `amend_sales_invoice` again on the same source | **deny** (`stage: amend`) — "already has 1 amendment(s): ACC-SINV-2026-00001-1" | a second amendment is refused, named |
| F-a3 | `plan_submit` → `pacioli mint` → `submit_sales_invoice` on the amendment | **ok** — docstatus 0 → 1 | the corrected entry re-posts through the same governed gate (its own plan + marker) |
| F-a4 | `prove_verify` / `prove_orphans` | **ok** — **8 receipts**, verifies, zero orphans | the full submit → cancel → amend → resubmit arc on one ledger |

Amend took **no marker** (it created a reversible draft; the irreversible re-post demanded its own),
yet still wrote its intent+outcome pair — so the ledger reads the whole story end to end.

### (b) off-box PROVE anchor

| # | Leg | Result | Proves |
|---|---|---|---|
| F-b1 | `pacioli anchor write --target bench` | pin emitted (`count 8`, head hmac, ts) + the stderr reminder that the tool cannot make it off-box | the pin is producible; honesty about what "off-box" means is in the tool |
| F-b2 | `pacioli anchor check` against the live chain | **ok, exit 0** — "chain matches the anchor (pinned 8, live 8, verifies)" | a good chain passes |
| F-b3 | `anchor check` against a **tail-truncated copy** of the state db | **FAILED, exit 1** — "receipt count regressed: pinned 8, live 7 — tail truncated or wiped since the pin" | the on-box blind spot is now caught by the off-box pin |

### (c) CONTAIN (the credential floor)

| # | Leg | Result | Proves |
|---|---|---|---|
| F-c1 | untick `enabled`, then call an in-scope method | **200 → 403** (no restart), re-tick → **200** | the kill switch is instant, per-request |
| F-c2 | `rate_limit_per_minute = 3`, fire 5 calls in a window | **200, 200, 200, 403, 403** | exactly N pass, then denied naming the limit |
| F-c3 | Error Log after the denials | rows: `Pacioli Guard denied a request (rate limit / kill switch / out of scope)` | every denial leaves an audit trail |

Every leg green; both packages dropped their alpha — **broker 0.4.0, guard 0.3.0**. Remaining
honest gaps (unchanged, deferred): cascade cancel, Workflow-engine SoD CONSENT, and the seal key
is still on-box (the anchor bounds a key-holder to forging only *post*-pin receipts).

## PHASE G — Gate 5: Workflow-SoD CONSENT, LIVE E2E ✅ PROVEN (2026-07-02)

Broker **0.5.0** (CONSENT's second gate), run against the sealed **frappe/erpnext version-16**
bench. The gate was built from a frappe **v15** source read; the first act
here was to introspect those shapes on the live **v16** bench — **every pin held**:
`apply_workflow(doc, action)` signature, `has_approval_access` source (self-approval allowed unless
a transition unchecks it AND the actor is the creator), `allow_self_approval` default `"1"`, the
`Workflow`/`Workflow Document State`/`Workflow Transition` fieldnames, and `Workflow` being
**System-Manager-read by default**.

**Pre.** Guard `bench migrate`; a `Draft(0) → Pending Approval(0) → Approved(1)` Workflow on Sales
Invoice — "Request Approval" (Draft→Pending, non-approving) and "Approve" (Pending→Approved,
`allow_self_approval` **OFF** = real SoD); broker credential (`scoped@proof.local`, guard scope)
extended with **`Workflow` read** + the **`frappe.model.workflow.apply_workflow`** method; a fresh
broker-owned draft `ACC-SINV-2026-00004`.

### The required-grant proof — `pacioli doctor` FAIL → PASS

| # | Leg | Result | Proves |
|---|---|---|---|
| G-d1 | `pacioli doctor` **before** the Workflow read grant | `XX workflow read: the credential cannot read the Workflow DocType (403) — grant custom read permission … without it every submit/cancel denies` → **NOT ready** | the upgrade break is caught at readiness time |
| G-d2 | `pacioli doctor` **after** the grant | `ok workflow read: Workflow DocType is readable — 1 active workflow(s) on Sales Invoice` → **ready.** | the grant is exactly the fix; 403-here-is-FAIL (opposite of the method probe) |

### The eight legs

| # | Leg | Result | Proves |
|---|---|---|---|
| G-a | `workflow_status(ACC-SINV-2026-00004)` | **ok** — active, `current_state` Draft (doc_status 0), the one legal transition flagged `approving:false` role `Accounts Manager`, **`sod:true`** | the read surface reports the gate honestly |
| G-b | `plan_submit` | **ok** — risk flag `workflow-governed: submission requires human approval via Workflow 'SI Approval (Gate5)' (role Accounts Manager)` | PLAN surfaces the gate; planning stays a read |
| G-c | `submit_sales_invoice` with a **valid, freshly minted 32-char marker** | **deny** (`stage: workflow`) — "submit is governed by Workflow … request the workflow transition instead of a direct submit" | **the headline: a valid marker is not enough; CONSENT is two gates, the workflow gate precedes consent** |
| G-d | `request_workflow_transition("Request Approval")` | **ok** — `ACC-SINV-2026-00004` → workflow_state **Pending Approval**, docstatus stays **0**, intent+outcome receipts | the broker MAY perform a non-approving move (no marker; reversible) |
| G-e | `request_workflow_transition("Approve")` | **deny** (`stage: workflow`) — "'Approve' from 'Pending Approval' is an approving transition (belongs to role 'Accounts Manager'); request the human perform it" | **the belt**: the broker refuses the approving transition itself |
| G-f | raw `POST …/apply_workflow` "Approve" as the broker credential (owner==broker, self-approval OFF) | **HTTP 417** `frappe.exceptions.ValidationError: Self approval is not allowed` | **the suspenders**: frappe's own `has_approval_access` blocks the creator — belt AND suspenders hold |
| G-g | governed cancel of the submitted `ACC-SINV-2026-00001-1` (this workflow maps no state to doc_status 2) | **ok** — `plan_cancel` ok, `cancel_sales_invoice(marker)` → **docstatus 1 → 2**, `stage: done` | **non-overreach**: `governs_op(cancel)=False`, so the gate does NOT block a cancel it doesn't govern |
| G-h | `prove_verify` / `prove_orphans` | **ok** — **count 14**, head hash, **zero orphans** | the two transition receipts (G-d) + the cancel pair (G-g) join the chain; refused legs wrote nothing |

**broker 0.5.0a1 → 0.5.0.** Honest note: a frappe **v16 `locale.py` bug** (`get_locale_value`
leaves `value` unbound when `frappe.local.lang` is unset) surfaced only while creating the draft
fixture *in the console*; it does not touch the broker's REST path (where `lang` comes from the
request) and was worked around with `frappe.local.lang='en'`. The test Workflow and the
`ACC-SINV-2026-0000x` fixtures remain on the (dark) lab bench. Remaining honest gaps (unchanged):
cascade cancel, guard-side (bench-side) Workflow enforcement against every calling path, and the
seal key still on-box.

## PHASE H — Gate 6: Purchase Invoice breadth, LIVE E2E ✅ PROVEN (2026-07-03)

Broker **0.6.0**, run **from the workspace box** (`pacioli` source on `PYTHONPATH`, stdlib-only)
against **the bench REST** (sealed lab, same /24; registry `allow_http=true` for the
sealed LAN; `api_secret` a `file:` ref pulled CT→box over the LAN, never in transcript).
Scoped non-Administrator credential `scoped@proof.local` under `pacioli_guard` (scope extended for
Purchase Invoice: +`Purchase Invoice` resource, +`Purchase Invoice.submit`/`.cancel`). Frappe-layer
preflight confirmed the user has read/create/submit/cancel on PI (Accounts Manager role covers both
doctypes). Fixtures on company **"Pacioli Test"**: item `SVC-1`, cost center `Main - PT`.

| # | Leg | Result | Proves |
|---|---|---|---|
| H-a | `plan_submit(ACC-PINV-2026-00001, pacioli_doctype="Purchase Invoice")` | **ok** — projected GL: DR `Cost of Goods Sold - PT` 100 / CR `Creditors - PT` 100, from the native `show_accounting_ledger_preview` | PLAN reads the real payable posting for a second doctype through the same handler |
| H-c1 | a PI plan → `submit_sales_invoice` (bogus marker) | **deny** `stage:plan` — "different document type ('Purchase Invoice', not 'Sales Invoice') … a consent grant does not transfer between document types" | **the breadth security headline**: a plan is doctype-bound |
| H-c2 | an SI plan → `submit_purchase_invoice` (bogus marker) | **deny** `stage:plan` — mirror refusal, other direction; both plans stay live | doctype-binding holds both ways, `check_doctype` before marker |
| H-b | `submit_purchase_invoice` with a valid minted marker | **ok** — `ACC-PINV-2026-00001` docstatus **0→1** | governed PI submit works, same spine as SI |
| H-d | `plan_cancel` + `cancel_purchase_invoice(marker)` | **ok** — docstatus **1→2**; bench GL Entry shows **4 rows all `is_cancelled=1`** (2 original + 2 equal-and-opposite reversal) | governed UNDO truly unwinds the PI ledger |
| H-e | `prove_verify` / `prove_orphans` | **ok** — **count 4**, zero orphans; last 4 receipts self-describe `doctype=Purchase Invoice` | the chain carries doctype on every receipt |

**Honest note.** On the first arc, `plan_cancel`'s *projected_reversal* preview returned **0 rows**
(correctly risk-flagged "no live GL rows found — nothing visible to unwind"), even though the cancel
itself reversed the ledger correctly. Reproduction on a fresh submit (`ACC-PINV-2026-00003`) showed
`plan_cancel` preview = **2 rows** immediately and after a 4 s wait — so the PI cancel-preview works;
the single 0 was a **non-reproduced one-off**. The cancel is governed independently of the preview,
so the arc was safe either way. Not overclaimed. **broker 0.6.0 README flipped to live-proven for PI.**

## PHASE I — Gate 7: guard-side Workflow enforcement, LIVE E2E ✅ PROVEN (2026-07-03)

Guard **0.4.0** (`pacioli_guard`) upgraded in-bench from 0.3.0a1 (editable-app source sync +
`bench migrate` added the `enforce_workflow` Check + werkzeug dev-serve reload). Legs run as the
scoped credential via **raw curl** to the bench (NOT through the broker), governed doctype =
Sales Invoice (Gate-5 Workflow `SI Approval (Gate5)`, Draft→Pending→Approved, self-approval off).

**Knowledge-pins falsified live — both HELD:**
- `frappe.model.workflow.get_workflow_name("Sales Invoice")` → `'SI Approval (Gate5)'` (str);
  `("Purchase Invoice")` → `None`. Import path + return contract exactly as pinned.
- `frappe.form_dict` **does** surface a raw JSON `PUT` `docstatus` key (leg I-b refused) and the
  Desk `savedocs` `doc`/`action` params (leg I-c refused).

**With `enforce_workflow=1` — all four `is_docstatus_changing` branches caught (403 "workflow bypass"):**

| # | Vector | Result | Proves |
|---|---|---|---|
| I-a | `POST /api/resource/Sales Invoice/ACC-SINV-2026-00006?run_method=submit` | **403** — "workflow gate refused … method 'Sales Invoice.submit' … active Workflow governs it"; SI stayed **docstatus 0** | branch 1 (method-name suffix), the item-URL run_method vector — workflow NOT bypassed |
| I-b | raw `PUT {"docstatus":1}` | **403** workflow | branch 3 (raw-REST update) — form_dict surfaces JSON docstatus |
| I-c | `savedocs` `action=Submit` | **403** workflow | branch 2 (the redteam-CRITICAL Desk vector) |
| I-d | POST-create `docstatus:1` | **403** workflow | branch 4 (insert-as-submitted) |
| I-e | `apply_workflow` (sanctioned) | **not guard-blocked** (417 frappe self-approval) | the one sanctioned path is funneled through, never blocked |
| I-g | ungoverned PI submit | **not workflow-blocked** | no over-reach on a doctype with no workflow |
| I-f | same I-a vector with `enforce_workflow=0` | **200**, SI submitted (0→1) | **default-off A/B**: the gate is genuinely opt-in; the ON 403s were the guard's |

**Layering (defense in depth).** Legs I-a/I-c only reach the workflow gate because the credential was
*temporarily* granted `run_doc_method`/`savedocs` to isolate it; with the **default tight scope** those
methods are denied at the **scope gate** BEFORE the workflow gate runs (first ON run returned scope-403s
for them). Credential restored to tight scope afterward.

**Documented residual, confirmed live.** `/api/method/run_doc_method` with the doctype in its body
classifies as `("method","run_doc_method")` — no dotted suffix → `is_docstatus_changing=False` → **not
caught** (returned 200, submitted a governed SI). This matches the guard's own honest residual list
(generic RPC / `frappe.client.*` / doctype-in-body). The mitigation is the tight default scope, which
never grants such broad methods. **guard 0.4.0 README residual line flipped to live-verified.**

## PHASE K — Gate 8: Payment Entry vertical, LIVE E2E ✅ PROVEN (2026-07-06)

Broker **0.8.1**, run **from the workspace box** (`pacioli` wheel in a fresh venv, end-user style)
against the bench REST (sealed lab, Frappe v16, reached over the permanent lab NIC:
workspace box → bench `:8000`). Scoped non-Administrator `scoped@proof.local` under
`pacioli_guard`, re-scoped for this run: **+`Company` read** (the v16 frozen-books fix), **+`Payment
Entry` read/submit/cancel**, **+`Journal Entry` read/submit/cancel**. Company **"Pacioli Test"**.

**Frozen-books v16 fix — doctor FAIL→PASS proven.** Before the `Company` grant, `pacioli doctor`
reported `XX Company read (403) … required to reconstruct the v16 frozen-books red-line
(Company.accounts_frozen_till_date)`; after the grant, `ok Company read … `, `ready.` Pins confirmed
live: `acc_frozen_upto` is **absent** from the v16 `Accounts Settings` meta; `accounts_frozen_till_date`
is **present** on the `Company` meta — the fix reads the correct v16 source.

| # | Leg | Result | Proves |
|---|---|---|---|
| K-a | `plan_submit(ACC-PAY-2026-00001, "Payment Entry")` | **ok** — projected GL DR `Cash - PT` 100 / CR `Debtors - PT` 100, party leg carries `against_voucher = ACC-SINV-2026-00007` | PLAN reads a real PE posting through the same handler |
| K-b | `submit_payment_entry(plan, marker)` | **ok** — docstatus **0→1**; **referenced SI outstanding 100→0** (Payment Ledger cascade, verified in bench DB) | governed PE submit moves the *invoice*, not just the PE doc |
| K-c1 | PE plan+marker → `submit_sales_invoice` | **deny** `check_doctype` — "different document type ('Payment Entry', not 'Sales Invoice')" | plan is doctype-bound |
| K-c2 | SI plan+marker → `submit_payment_entry` | **deny** `check_doctype` — mirror refusal, other direction | doctype-binding both ways |
| K-d | `plan_cancel` + `cancel_payment_entry(marker)` | **ok** — `references` disclosure lists SI + allocated 100; each reversal GL row carries `against_voucher`; docstatus **1→2**; **SI outstanding reverted 0→100**; 4 GL rows all `is_cancelled=1` | governed UNDO unwinds the PE *and* the invoice; blast radius disclosed |
| K-e | `prove_verify` / `prove_orphans` | **ok** — 4 receipts, zero orphans | chain carries doctype per receipt |

**Falsification pins closed.** (1) The zero/negative-outstanding case is **harder than "a warning"**:
ERPNext **throws at insert** (`ValidationError: … has already been fully paid`) when a PE allocates
against a zero-outstanding invoice — so the broker's advisory zero-outstanding flag is defense-in-depth
behind a stronger ERPNext block, not the primary guard. (2) `acc_frozen_upto` absent / `Company` field
present, confirmed above. **broker README flipped to live-proven for Payment Entry.**

## PHASE L — Gate 9: Journal Entry, governance legs LIVE ✅ / submit+cancel BUILT, KNOWLEDGE-PINNED ◐ (2026-07-06)

> **Update (2026-07-06, broker 0.9.0 / guard 0.5.0).** The BLOCKED status below is closed **in
> code**, not yet on a live bench. `pacioli_guard` 0.5.0 added `scope.body_scoped_target`, parsing
> the doctype out of `frappe.client.submit`/`.cancel`'s request body and enforcing the credential's
> per-doctype grant on it (`"<DocType>.submit"`/`".cancel"`) exactly as strictly as the URL-path
> `run_method` shape it already enforced for SI/PI/PE. The broker (`erpnext.py`) now sends Journal
> Entry submit/cancel through that now-scopeable RPC surface instead of the 403ing `run_method`
> shape (`SUPPORTED_DOCTYPES["Journal Entry"]["submit_via"] = "client_rpc"`) — SI/PI/PE unchanged.
> **Update 2 (2026-07-07): LIVE-PROVEN — see PHASE M** (JE submit 0→1 landing verbatim as
> `frappe.client.submit` in the bench request log; cancel 1→2; out-of-scope body-doctype submit
> 403'd pre-dispatch; SI/PI/PE regression clean). The narrative below (as of 2026-07-06, PHASE K/L bench
> run) is left UNCHANGED as the historical record of the blocker as found; do not read it as still
> current for submit/cancel reachability.

Same box + bench + credential as PHASE K.

| # | Leg | Result | Proves |
|---|---|---|---|
| L-1 | `plan_submit(balanced JE, "Journal Entry")` | **ok** — 3-row projected GL + standing fidelity risk flag (on_submit-only checks invisible to the preview) | PLAN reads every `accounts` row for a 3rd governed doctype |
| L-2 | `plan_submit` on `voucher_type="Exchange Gain Or Loss"` | **deny** `plan` — names both ERPNext gates that skip debit==credit for this value; refuses rather than trust either | **the founding law, live** — the lone unbalanced entry is what the system refuses |
| L-3 | `plan_submit` on a genuinely-unbalanced JE (debit 100 ≠ credit 60) | **deny** `plan` — "total_debit (100.0) != total_credit (60.0), summed independently from the draft's own accounts rows" | the independent balance check fires before any marker can mint |
| L-⛔ | `submit_journal_entry` / `cancel_journal_entry` | **BLOCKED** — frappe **403 "erpnext…journal_entry.submit is not whitelisted"** | see below |

**Honest test note (L-3).** The first unbalanced fixture was a *false negative*: it set
`debit_in_account_currency` but not the company-currency `debit`/`credit` fields (and bypassed
`validate`, which populates them), so both the broker and ERPNext saw `0 == 0`. The bench caught the
bad test; the fixture was rebuilt to carry real `debit=100 / credit=60`, and the refusal fired. The
check reads the company-currency fields GL posts from — the correct fields.

**⛔ KNOWN LIMITATION — Journal Entry submit/cancel not reachable via the guard-scopeable shape.**
Source-confirmed (`erpnext/accounts/doctype/journal_entry/journal_entry.py:186,195`): `JournalEntry`
**overrides** `submit()`/`cancel()` to background-queue >100-row entries, and the overrides drop
frappe's `@frappe.whitelist()` decorator. Frappe's REST handler
(`frappe/handler.py`: `is_whitelisted(getattr(doc, method).__func__)`) therefore rejects the broker's
only guard-scopeable submit shape — `POST /api/resource/Journal Entry/<name>?run_method=submit` — with
a 403. Sales Invoice, Purchase Invoice, and Payment Entry override neither and use the whitelisted base
`Document.submit`, so they are unaffected (proven live in this run and PHASE H). **Journal Entry's
governance legs are live-proven (above); its submit/cancel stay knowledge-pinned.** The fix is a design
decision on the submit transport for override-doctypes — every alternative (`frappe.client.submit`,
`savedocs` action=Submit) puts the doctype in the request body and **cannot be scoped per-doctype by
`pacioli_guard`**, which is the exact property the run_method shape exists to preserve. **John's call**
(tracked in `GO-LIVE.md`). Journal Entry is **not** flipped to live-proven.

## PHASE J — cascade cancel, bench proof ✅ (discovery + fail-stop + happy path) (2026-07-06)

Owed since 0.7.0. Broker **0.8.1** (the run that also fixed the bug below), same box/bench/credential.

**Bug found live, fixed, re-proven.** `plan_cascade_cancel` crashed `KeyError: 'docname'` on the first
real dependent: ERPNext's `get_submitted_linked_docs` returns each dependent in frappe's native shape
(`{"doctype","name",…}`), but the cascade core keys every node on `docname`. The pure-core tests fed
fakes already in `docname` shape, so this client-adapter seam had no live coverage (same class as the
Gate 4 server.py adapter gap). Fixed by `_cascade_fetch_linked` (normalizes `name → docname` at both
the plan and execute-time re-discovery seams) + a `test_tools.py` regression pinning the real frappe
shape. Wheel rebuilt 0.8.0→0.8.1, reinstalled, re-run:

| # | Leg | Result | Proves |
|---|---|---|---|
| J-1 | `plan_cascade_cancel(ACC-SINV-2026-00015)` | **ok** — graph `[Journal Entry ACC-JV-2026-00011 (modeled), Sales Invoice (modeled)]`, dependents first, target last | discovery works on the real linked-docs shape (the fix, live) |
| J-2 | `cascade_cancel(marker)` on that graph | **fail-stop** — `cancelled=[]`, `total=2`, `stopped_at` = JE seq 0 with the exact JE-cancel-blocked reason; **target SI untouched (docstatus still 1)** | no partial cascade; fail-stop names where and why |
| J-3 | `cascade_cancel` on a standalone submitted SI (`ACC-SINV-2026-00019`) | **ok** — `cancelled=[Sales Invoice]`, docstatus **1→2** | the cascade success path end-to-end |
| J-4 | marker semantics | failed cascade (0 cancelled) **released** its marker (reusable — consent not consumed); successful cascade's marker **single-use-dead** (reuse → `stage:fresh`) | commit-iff-≥1-cancelled, released-iff-0 |
| J-5 | `prove_verify` / `prove_orphans` | 12 receipts, chain intact; **prove_orphans surfaces the 2 blocked JE-cancel intents as orphans** | the failed cancels are honestly recorded (intent without outcome = "attempted, didn't finish") |

**Load-bearing finding.** On ERPNext, a Sales Invoice's submittable dependents surface (via
`get_submitted_linked_docs`) as **Journal Entries** — a *paying* Payment Entry does **not** surface as
a linked dependent of the invoice. So real-world cascade graphs routinely contain JE nodes, and the JE
submit/cancel limitation (PHASE L) therefore **gates cascade's practical usefulness**, not just an edge
case. Cascade's mechanism (discovery, ordering, preflight, fail-stop, marker-once) is proven; its
real-world reach waits on the same submit-transport decision. **README/CHANGELOG say exactly this.**

## PHASE M — Gate 10 close-out: JE submit/cancel LIVE via the body-doctype path + all positive legs ✅ (2026-07-07)

Same bench + credential as PHASE K/L (the sealed-lab bench `erpnext-test`, Frappe 16.25.0 / ERPNext 16.26.0,
`scoped@proof.local`). Broker **0.9.0** (wheel built from `main c10afa6`, installed into the driver
venv, driven end-user style from this box); guard **0.6.0** already on the bench from the Gate-10
window. This closes every "◑ STILL PENDING" Gate-10 leg: **Journal Entry submit/cancel flips
knowledge-pinned → LIVE-PROVEN**, and the PHASE J cascade-JE caveat closes.

| # | Leg | Result | Proves |
|---|---|---|---|
| M-1 | `plan_submit(ACC-JV-2026-00014, "Journal Entry")` — balanced 20/20 draft | **ok** — 2-row projected GL (Cash - PT 20 dr / Debtors - PT 20 cr) | PLAN unchanged on the new transport |
| M-2 | TOCTOU belt: marker minted, then bench-side `db.set_value(…, update_modified=False)` unbalanced the JE (debit 20→50; `modified` byte-identical to the plan's `doc_version`) → `submit_journal_entry` | **deny** `plan` — "total_debit (50.0) != total_credit (20.0), summed independently from the draft's own accounts rows … refusing" | the independent balance check fires **at the write**, with valid consent in hand and freshness green — the belt is real, not plan-stage-only |
| M-3 | restore 20/20 (`modified` still untouched) → same plan + **same marker** → `submit_journal_entry` | **ok — docstatus 0→1**; bench request log verbatim: `POST /api/method/frappe.client.submit HTTP/1.1" 200`; 2 GL rows posted uncancelled | **the Gate-9 blocker is CLOSED LIVE** — JE submit lands via `frappe.client.submit`, not `run_method`; a refused write does NOT consume the marker |
| M-4 | replay the spent marker | **deny** `fresh` — "document changed after planning" | single-use consent holds on the new path |
| M-5 | `plan_cancel` (real 2-row projected reversal + the unlink-on-cancel risk flag) → mint → `cancel_journal_entry` | **ok — docstatus 1→2** via `frappe.client.cancel` (request log verbatim); all 4 GL rows `is_cancelled=1` | JE UNDO end-to-end on the new transport |
| M-6 | fresh draft with `voucher_type="Exchange Gain Or Loss"` → `plan_submit` | **deny** `plan` — names both ERPNext gates that skip debit==credit for this value | the founding-law refusal still runs BEFORE the now-reachable write |
| M-7 | raw `frappe.client.submit` naming `{"doctype":"Purchase Order"}` (never granted) | **403** `(method: Purchase Order.submit)`, `_exc_source: pacioli_guard`, thrown in `validate_auth` before frappe touched any doc | `body_scoped_target` enforces per-doctype on the live wire — deny before dispatch, no doc lookup needed |
| M-8 | SI/PI/PE regression, Gate 2/3/6/8 shapes unmodified: PI `ACC-PINV-2026-00004` 0→1→2 · PE `ACC-PAY-2026-00019` 0→1→2 · SI `ACC-SINV-2026-00020` 0→1→2 | **all ok** via the URL-path `run_method` shape | deny-unknown + the body-doctype rewrite did not regress the proven path |
| M-9 | SI submit with a VALID marker while `SI Approval (Gate5)` is active | **deny** `workflow` — "the approving transition belongs to role 'Accounts Manager'" | the Workflow-SoD CONSENT gate is intact under broker 0.9.0 (workflow deactivated ONLY for M-8's SI transport legs, reactivated after — fixture management, recorded) |
| M-10 | `request_workflow_transition("Request Approval")` with ONLY the old bare `frappe.model.workflow.apply_workflow` grant | **deny** — HTTP 403 `(method: Sales Invoice.apply_workflow)` | the BREAKING re-grant is real, broker-driven (complements the Gate-10 raw-curl negative) |
| M-11 | same call after re-granting per-doctype `Sales Invoice.apply_workflow` and DROPPING the bare grant (credential now in the exact deny-unknown final shape) | **ok** — `workflow_state Draft → Pending Approval`, docstatus 0 | **the positive half: the re-grant shape WORKS** |
| M-12 | `plan_cascade_cancel(ACC-SINV-2026-00015)` — the exact PHASE J-2 graph (dependent JE `ACC-JV-2026-00011`) → mint → `cascade_cancel` | **ok — cancelled 2/2** (JE seq 0, target SI seq 1), `stopped_at: null`; DB: both docstatus 2, 8 GL rows `is_cancelled=1` | **the PHASE J caveat closes** — cascade's real-world reach (JE dependents) is live |
| M-13 | `pacioli verify` / `pacioli orphans` | chain ok (40 receipts); 6 orphans, each mapping 1:1 to a known refused/failed attempt (2× the J-2 fail-stops of 2026-07-06, 1× stale-PE 417, 2× refused workflow attempts) | PROVE stays honest: every intent without an outcome is a named, explained refusal |

**Notes (verbatim honesty):**
- The Gate-8-era PE draft `ACC-PAY-2026-00013` failed at execute — HTTP 417 "Sales Invoice
  ACC-SINV-2026-00014 has already been fully paid" (its referenced invoice was settled after the
  draft was made). An ERPNext business rule surfaced honestly at the execute stage; the leg was
  re-run on a fresh reference-free PE. Not a regression.
- **Deny-does-not-consume, proven twice:** M-2's balance deny and M-9's workflow deny both left
  their markers live, and the SAME marker then carried the permitted write. Consent is spent by
  commitment, not by refusal.
- Fixture quirks (recorded for the next window): v16 console still needs `frappe.local.lang = "en"`
  (known pin); copied PI/SI fixtures needed `payment_schedule` cleared and dates re-set to today.
- `submit_journal_entry`/`cancel_journal_entry`/`cascade_cancel` all require the `name` arg alongside
  `plan_id`+`marker`; calling without it denies `stage: plan` ("no document name") — fail-closed on a
  malformed call, at the cost of one burned-but-unspent mint (a live marker blocks re-mint until TTL;
  re-plan → fresh plan_id is the recovery).

## PHASE N — envelope E1: the >100-row Journal Entry (queued writes) — BUG FOUND LIVE, FIXED, RE-PROVEN ✅ (2026-07-07)

First leg of the envelope campaign (`docs/plans/2026-07-07-envelope-campaign.md`): accounting's
edge cases are rules, and rules are constructible — this one staged a balanced **102-row JE**
(`ACC-JV-2026-00016`, 51.0/51.0) against the source-confirmed queue threshold
(`JournalEntry.submit`/`.cancel` background-queue past 100 accounts rows; bench runs 4 workers).
Same bench + credential as PHASE M; broker driven end-user style from the built wheel.

**The pin, and what it caught (broker 0.9.0):**

| # | Leg | Result | What it proved |
|---|---|---|---|
| N-1 | `plan_submit` on the 102-row JE | **ok** — 102 projected GL rows; **NO queue disclosure** | the memorandum scales; the disclosure gap is real |
| N-2 | mint → `submit_journal_entry` | **`ok: true, stage: done` with `docstatus: 0`** — ERPNext queued the submit and answered 200 with the doc unsubmitted; **PROVE recorded a `committed` 0→1 outcome the response never showed** | 🔴 **THE BUG**: `spine.governed_submit` treated "execute didn't throw" as proof of the transition. The worker happened to land it ~28s later (docstatus 1, 102 GL rows) — a failed worker would have left the book carrying a committed receipt for a write that never happened. The exact inversion of the journal's purpose. |

**The fix (0.9.0 → 0.9.1, test-first — `test_spine.TestUnconfirmedOutcome`, 5 tests + 2 flag
tests, broker suite 533 → 540 green):** the execute response must **confirm** the transition
(returned docstatus == the transition's end state; a response without a docstatus confirms
nothing — deny-biased). On mismatch: outcome recorded **`unconfirmed`** (never `committed`), the
tool answers `ok: false, stage: "unconfirmed"` naming the queue cause, the **marker is SPENT**
(consent initiated an irreversible act now in motion; releasing it would let one grant initiate a
second act), and — since only `committed` finalizes an intent (`prove.orphans`, unchanged) — the
write **stays in the reconcile sweep** until checked against the real docstatus. Plus upfront
disclosure: `plan_submit` flags a >100-row JE's queue before any consent exists.

**Re-proof under 0.9.1 (wheel rebuilt + reinstalled, all live):**

| # | Leg | Result |
|---|---|---|
| N-3 | queued **cancel** of `ACC-JV-2026-00016` (submitted in N-2) | **`ok: false, stage: unconfirmed`** — "the response shows docstatus 1, expected 2 … the write may still land after this reply. The consent marker is spent; the intent receipt stays open until reconciled" |
| N-4 | fresh 102-row JE `ACC-JV-2026-00017` → `plan_submit` | **queue flag present on the memorandum** ("more than 100 accounts rows — ERPNext queues its submit … the broker will report 'unconfirmed'") — disclosed before consent |
| N-5 | mint → `submit_journal_entry` | **`unconfirmed`**, honest reason verbatim |
| N-6 | replay the marker | **deny `consent` — "marker not available (state: consumed)"** — spent exactly as designed |
| N-7 | reconcile (the documented step): governed `get_journal_entry` after the worker window | `ACC-JV-2026-00017` **docstatus 1**, `ACC-JV-2026-00016` **docstatus 2** — both queued writes landed; the open receipts now have their real-world answer |
| N-8 | `pacioli verify` / `orphans` | chain ok (46 receipts); orphans 6 → **8** — the two `unconfirmed` intents correctly stay in the sweep (only `committed` finalizes; reconciliation is an act against the real docstatus, N-7) |

**The campaign's first lesson, plainly:** a customer seat would have hit this in month three with
real money on the rows; the bench hit it in an hour because the rule was written down where it
could be staged. One dimension walked, seven to go (E2–E8).

## PHASE O — envelope E2: the update_stock dual ledger — DISCLOSURE GAP FOUND, FIXED, RE-PROVEN ✅ (2026-07-07)

Staged the stock world on the bench (perpetual inventory ON with an inventory account — the
strongest form of the pin): stock item `G10-WIDGET`, Material Receipt of 10 Nos @ 5.00 into
`Stores - PT`, draft SI `ACC-SINV-2026-00021` selling 2 @ 10 with `update_stock=1`.

| # | Leg | Result | What it proved |
|---|---|---|---|
| O-1 | `plan_submit` (broker 0.9.1) | **ok** — projected GL COMPLETE for money (COGS 10 dr / Stock In Hand 10 cr valuation rows present alongside Sales/Debtors 20) but **ZERO disclosure of the physical movement** (items/qty/warehouse) | 🔶 **THE GAP**: the quantity story was absent. With perpetual inventory disabled even the valuation rows vanish — consent could be minted blind to an inventory decrement |
| O-2 | fix 0.9.1 → **0.9.2** (test-first, `TestUpdateStockDisclosure`, 4 tests, suite 540 → 544) | `plan_submit`/`plan_cancel` disclose the movement whenever the doc itself carries truthy `update_stock` — items summarized (first 5 + honest "and N more") from the draft's OWN rows, no new bench read, no new credential surface, doctype-agnostic by construction | the memorandum tells the whole story: money AND matter |
| O-3 | re-plan under 0.9.2 | flag live: **"moves PHYSICAL STOCK on submit … 2.0 Nos of G10-WIDGET @ Stores - PT … can be invisible in the GL preview"** | disclosed before any consent exists |
| O-4 | mint → `submit_sales_invoice` (workflow deactivated for the leg, as PHASE M) | **docstatus 0→1**; bench DB: SLE `actual_qty -2`, `qty_after_transaction 8`, **Bin 10→8**, 4 GL rows exactly matching the memorandum row-for-row | both ledgers moved exactly as disclosed |
| O-5 | `plan_cancel` | reversal flag live: "REVERSES its physical stock movement … return to their warehouses" | the cancel discloses matter too |
| O-6 | mint → `cancel_sales_invoice` | **docstatus 1→2**; reversal SLE +2 posted, both SLE rows `is_cancelled=1`, **Bin back to 10**, zero live GL rows; workflow reactivated after | equal-and-opposite across BOTH ledgers |

**Deferred, recorded:** valuation-level stock preview (`show_stock_ledger_preview`, ERPNext's
native sibling of the GL preview) would give the memorandum per-row valuation for stock moves —
it needs a new curated bare-method grant in the guard's SAFE_METHODS, so it is its own increment
if wanted, not a silent scope-widening here.

## PHASE P — envelope E3: the journal ERPNext writes with its own hand (system EG in cascade) ✅ (2026-07-07)

**Pin sheet pre-registered** (`docs/plans/2026-07-07-envelope-e3-eg-cascade.md`, two-scout source
read: erpnext `version-16` @ `d1d3b241` + frappe @ `9a8daf34`); **broker 0.9.3 shipped pre-window**
(`7c80d88` — per-node confirm, per-node disclosure, `risk_flags` response key, the redteam's
readback CRITICAL; 564+270 green). Bench: the sealed lab (frappe v16, site erpnext.localhost), broker
0.9.3 **wheel** in the e2e venv (site-packages verified, an editable-resolution drift in the venv
was caught and fixed before the window), scoped credential `scoped@proof.local`, Gate-6 pattern.
Fixtures: company `Pacioli Test` (**USD** company currency → the script's defensive flip picked
**EUR** as the foreign side), `E3 Receivable EUR - PT`, `E3 FX Customer`, SI `ACC-SINV-2026-00022`
(100 EUR @ 80, submitted console-side), PE draft `ACC-PAY-2026-00020` (@ 75, allocating in full —
`references[0].exchange_gain_loss = -500.0`, the 100×(75−80) prediction exact). One fixture catch:
Item needed `stock_uom`/`uom` (MandatoryError live, fixed in place, rerun clean).

| Leg | Act | Verbatim result | Pin |
|---|---|---|---|
| P-1 | `plan_submit` (Payment Entry) | ok — projected GL 7500/7500; **risk_flags: "reference 'Sales Invoice ACC-SINV-2026-00022': nonzero exchange_gain_loss (-500.0) — ERPNext's own preview creates AND SUBMITS a real Exchange Gain/Loss Journal Entry mid-preview … projection-incomplete, disclosed"** | **P3 ✅** — the side-effect named BEFORE consent |
| P-2 | mint → `submit_payment_entry` | ok, **docstatus 0→1** under the scoped credential; no permission throw | **P1 ✅ / P2 ⚠️ see below** |
| P-3 | bench DB read | **`ACC-JV-2026-00018`: voucher_type "Exchange Gain Or Loss", `is_system_generated=1`, `multi_currency=1`, `owner=scoped@proof.local`, docstatus 1, exactly 2 rows** (EUR receivable cr 500 → ref SI; Exchange Gain/Loss dr 500 → ref PE), balanced live GL | **P1 ✅** — created synchronously, in the scoped seat's own hand, exactly the source-read shape |
| P-4 | `plan_cancel` (PE, single-op) | **REFUSED, stage plan: "1 submitted document(s) link to ACC-PAY-2026-00020: Journal Entry ACC-JV-2026-00018 — … use plan_cascade_cancel"** | **P4 ✅** — discovery surfaces the system journal; the blast radius forces the honest path; the "invisible side-cancel" is structurally unreachable through the broker |
| P-5 | `plan_cascade_cancel` | graph dependents-first [JE seq0, PE seq1], both `modeled`, per-node projected GL; **`risk_flags` in the RESPONSE** (0.9.3), docname-prefixed: JE EG-auto-cancel note ✅, unlink flag ✅ (setting ON on this bench), **PE settled-references flag naming the SI + allocated 100.0** ✅ | **P7(plan) ✅** — cascade consent now as informed as single-op |
| P-6 | mint → `cascade_cancel` | **ok — cancelled 2/2, stopped_at null** (JE seq 0 then PE seq 1); every `committed` passed the 0.9.3 per-node docstatus confirm | **P5 ✅** — dependents-first defused the side-cancel: by the time `cancel_exchange_gain_loss_journal` ran on the PE, the EG journal was already docstatus 2 and its docstatus=1 filter found nothing. No collision, no partial state |
| P-7 | bench DB sweep | cancelled-this-window = **exactly** {ACC-JV-2026-00018, ACC-PAY-2026-00020}; SI outstanding **restored 0→100**; EG live GL rows = 0; PE docstatus 2 | **P6 ✅** |
| P-8 | `prove_verify` / `prove_orphans` | chain ok, **50→56 receipts (exactly this window's 3 intent+outcome pairs)**; 10 orphans all pre-window (known named refusals), **zero new** | **P6 ✅** — no entry in the ledger without an entry in the journal |

**P2 — the honest finding (not the success theater):** the EG journal is created inline by
`create_gain_loss_journal` (`save()`+`submit()` **without** `ignore_permissions`) as
`frappe.session.user` — and it succeeded here, but `scoped@proof.local` carries **System Manager +
Accounts Manager** roles, so the frappe-role envelope was never actually stressed. What this proves:
the guard's HTTP-boundary scoping and the role system are independent layers, and the broker's
governed PE submit implicitly requires JE-authorship *roles* on its seat. What it does NOT prove: a
minimal-role seat works. **Open follow-up (recorded):** re-run the PE leg under a tight-role user
(Accounts User, no SysMgr) — the refusal, if it comes, must be honest end-to-end and `doctor`
should probe for it.

**Unconfirmed-path note:** 0.9.3's `unconfirmed`/readback-failure branches are pinned by 10 unit
tests + the E1 live precedent (PHASE N proved the queued-write shape live on single-op); this
window's cascade nodes all confirmed synchronously (2-row EG journals cancel inline — as the
source read predicted). A live cascade-unconfirmed run would need an artificial >100-row
dependent; recorded as covered-by-construction, not re-proven live.

**Window ops notes:** bench brought up via one clean `bench start` after a triple-starter port
collision (the CT autostarts bench on boot AND a pending root `su` from the window's first attempt
revived — both raced mine; killed all, one clean runuser start; watch's npm asset build is the
boot-time IO hog). Fixture transfer = sha-gated chunked ct_exec (2 chunks, end-to-end sha
`625110ef…` exact); the lab-NIC HTTP hop was classifier-blocked this session — the chunked recipe
remains the sanctioned fallback.

## PHASE Q — envelope E4: returns, credit notes, POS — ALL PINS HELD + A LIVE FINDING FIXED IN-WINDOW ✅ (2026-07-07)

**Pin sheet** `docs/plans/2026-07-07-envelope-e4-returns-pos.md`; **broker 0.9.4 pre-staged**
(`d03c924`), **0.9.5 cut mid-window** (the E1 pattern — found live, fixed test-first, re-proven
same window). Bench: the sealed lab, wheels verified site-packages both versions, scoped credential,
Gate-6 pattern. Fixture catches on the way in (each fixed in place): console `exec` needs an
explicit globals dict once the script defines functions; the bench had no Currency Exchange
record (EUR→USD @80 created — the mapper re-derives rates) and no Mode of Payment (Cash created
with the company account row). Fixture note: the "plain" SI inherited the customer's EUR default
currency — all E4 documents are EUR-denominated @80; changes no pin, recorded honestly. Gate-5 SI
workflow deactivated for the window, REACTIVATED at close (verified).

| Leg | Act | Verbatim result | Pin |
|---|---|---|---|
| Q-A | `plan_submit` on the mapper return draft (−10 of the qty-10 SI) | projected GL **column-swapped positive magnitudes** (Sales dr 8000 / receivable cr 8000 — the mirror of a sale, no negative debits); return flag on the memorandum | **P1 ✅ P9 ✅** |
| Q-B | `plan_cancel` on the ORIGINAL after the return posted | **REFUSED: "1 submitted document(s) link to ACC-SINV-2026-00023: Sales Invoice ACC-SINV-2026-00024"** | **P2 ✅ — the headline pin: a submitted credit note guards its original; `return_against` IS in the blast radius** |
| Q-C | second full return, bench-side insert | **refused at DRAFT INSERT**: `StockOverReturnError: Cannot return more than 0.0 for Item E3-FX-SERVICE` — upstream of the broker; a governable over-return draft cannot exist | **P3 ✅** (the refusal lives at draft-validation — recorded as-is, not claimed for our gate) |
| Q-D | free-standing credit note: plan → mint → submit | FREE-STANDING flag verbatim pre-consent (names the four skipped checks); **submits clean 0→1** — ERPNext takes it | **P4 ✅** |
| Q-E | EUR return against E3's `ACC-SINV-2026-00022` | mapper copied rate **80.0** exactly; submits 0→1 — **but the original's outstanding DID NOT MOVE (100.0, Unpaid)** | **P5 ◐ → THE FINDING** (below) |
| Q-F/G/H | POS full / partial / none | full: **cash legs on the memorandum** (Cash dr 8000 alongside the sale rows), posts 0→1, GL matches; partial: **PARTIAL-PAYMENT flag verbatim**, posts 0→1, **outstanding 50 exactly as disclosed**; none: refusal disclosed on the plan, then **honest deny at execute** (`HTTP 417: At least one mode of payment is required`) — intent receipt stays open as the window's one named orphan | **P6 ✅ P7 ✅** |
| Q-J | `plan_submit {pacioli_doctype: "POS Invoice"}` | **refused at request**: "unsupported pacioli_doctype… refused here even if a credential's own resource grant would otherwise allow it" — the queued-consolidation half-post window stays outside the broker's write path, stated not silent | **P8 ✅** |
| Q-K | stock return under **0.9.5**: plan → submit → plan_cancel | sign-aware inbound flag verbatim ("**stock comes IN to the named warehouse(s) on submit, not out**"); NEW settlement flag verbatim; submit 0→1, **Bin 8→10**; cancel plan **no longer says "return to their warehouses"** — names the reversal direction-honestly | **P9 ✅ + the fix live** |
| Q-L | trial balance | chain ok, **56→70 receipts (this window's 7 intent/outcome pairs exactly)**; orphans = 8 pre-window + the one named POS refusal | **✅** |

**THE FINDING (P5 → broker 0.9.5, fixed and re-proven in-window):** both mapper-built returns
carried **`update_outstanding_for_self=1`** (ERPNext's return mapper sets it by default) — the
return's receivable rows post `against_voucher=<the return itself>`, the return holds its own
**−100** credit, and **the original's outstanding does not move** until a separate payment
reconciliation allocates them. Consent to "a credit note against X" is NOT consent to "X is
settled," and 0.9.4's memorandum never said which shape it was. 0.9.5 adds the settlement flag
(both directions, from the doc's own field, no new read): live on Q-K verbatim — "this credit
note does NOT settle ACC-SINV-2026-00030…". Suites 589→592. The reconciliation-allocation leg
itself (settling a self-outstanding return against its original) is **E5 territory**, recorded.

**Window ops:** the CT's own bench autostart won cleanly this time (last window's collision was
our premature start — patience was the fix). Lab STOPPED at close; workflow reactivation verified
before shutdown.

## PHASE R — envelope E5: reconciliation, allocation, and the laundering door — DOOR HELD SHUT + 2 findings ✅ (2026-07-07)

**Pin sheet** `docs/plans/2026-07-07-envelope-e5-reconciliation.md`; **broker 0.9.6 pre-staged**
(`f54cd51`, the doctor Accounts-Settings probe), **0.9.7 cut in-window** (the probe's own live bug,
below). Bench: the sealed lab (v16), scoped credential, wheels verified. Fixtures: fresh EUR SI
`ACC-SINV-2026-00032` (outstanding 10 @80); unlink setting confirmed **ON** (v16 default, as the
source scout corrected).

| Leg | Act | Verbatim result | Pin |
|---|---|---|---|
| P5 | `pacioli doctor` (0.9.6) | 🐛**FAIL live**: `Accounts Settings read probe: bench unreachable: URL can't contain control characters` — the space in "Accounts Settings" was never URL-encoded (Company/Workflow have no space; the unit test's fake transport never exercised a real URL). **The bench caught a probe that never worked — the campaign thesis on our own code.** Fixed **0.9.6→0.9.7** (percent-encode the path segment, test asserts `%20` + no raw space), rebuilt, re-run: **"Accounts Settings read: readable … unlink … is ON"** | **P5 ✅ (after the fix)** |
| P1 | partial-allocation PE (paid 4 of outstanding 10): plan → mint → `submit_payment_entry` | plan clean; submit **0→1**; **SI-32 outstanding 10→6 exactly** | **P1 ✅** |
| P2 | over-allocation PE (allocated 20 vs live 6), bench insert | **refused at DRAFT INSERT**: `ValidationError: Row #1: Allocated Amount cannot be greater than outstanding amount` — ERPNext's floor, upstream of the broker; a governable over-allocation draft cannot exist (same shape as E4's over-return). The governed-**execute** clean-refuse for the stale-outstanding case was already proven live at Gate 10 M (the stale-PE 417, marker released) — same spine rail | **P2 ✅** (floor-at-insert; execute-path = Gate-10 M) |
| **P3** | **the laundering door**: scoped credential fires ERPNext's Payment Reconciliation `reconcile` via 3 RPC shapes (`e5_recon_probe.py`) | 🔒**ALL 3 DENIED 403 by the guard** — bare dotted → `PermissionError … not permitted (method: …payment_reconciliation.reconcile)`; `run_doc_method` (client-built doc) → guard resolved the body-doctype and denied `Payment Reconciliation.reconcile`; v2 `run_doc_method` → denied pre-dispatch at `validate_auth`. **Control** (`get_logged_user`, a SAFE_METHOD) → **200 scoped@proof.local**, proving the guard is ACTIVELY enforcing, not blanket-down | **P3 ✅ — the headline: deny-unknown (Gate 10) denies ERPNext's `ignore_permissions` reconciliation door on every shape a credential never granted `Payment Reconciliation.reconcile` can send; no coarse or bare grant launders into it. Ordinary deny-by-default, NOT a Bulk-Update-style ungrantable block — reaching `reconcile` needs an explicit `Payment Reconciliation.reconcile` grant like any other per-doctype method (the broker's own F-R2 seat now holds exactly that grant, by design)** |
| P4 | unlink OFF → bench-side invoice cancel under a live PE | **`LinkExistsError` fired** ("Cannot delete or cancel because Sales Invoice…") — the class the Gate-8 scouts predicted and NEVER saw (bench always ran ON before); restored ON after, verified | **P4 ✅ — the never-seen class, live** |
| P7 | trial balance | chain ok, **70→72 receipts** (the partial allocation's pair), zero new orphans | **✅** |

**🔴 FINDING F-R1 (recorded, its own increment — NOT rushed in-window):** the broker's `plan_cancel`
blast radius **ALLOWED** cancelling the settled SI-32, because `get_submitted_linked_docs(Sales
Invoice)` returned **`[]`** — ERPNext's `auto_cancel_exempted_doctypes` excludes Payment Entry from
an invoice's linked docs (the same E3/Gate-8 asymmetry). So under unlink ON, a human could mint a
marker and the broker would cancel a settled invoice, silently triggering ERPNext's **raw-SQL
unlink** of the submitted PE — a side effect the memorandum discloses nothing about; under unlink
OFF the broker would proceed to execute and hit `LinkExistsError` (clean spine refuse, marker
released). Unlike E4's settlement flag (read from the doc's OWN field), disclosing this needs a
**new reverse-reference read** (which PEs reference this invoice) + a guard grant — its own
increment (read surface + word-model + guard scope), recorded not patched.

**🔴 FINDING F-R2 (the honest non-coverage, by design):** governing Payment Reconciliation to
actually SETTLE the E4 self-outstanding pair (via ERPNext's system "Credit Note" JE) is a named
future increment — it rides the same `ignore_permissions` write path P3 just proved is denied, and
touches raw in-place writes to submitted docs. E5 proves the door is shut and the ungoverned path
refused; it does NOT pretend we govern reconciliation. The E4 pair stays on the bench as the
standing proof-of-need.

**Window ops:** the doctor probe fixed a bug in itself mid-window (0.9.6→0.9.7) — the exact
"a wrong result fixes the code, never the assertion" loop. Lab STOPPED at close; unlink setting
restored ON + verified before shutdown.

## PHASE S — envelope E6: period boundaries, exactly on the line ✅ (2026-07-07)

Broker **0.9.8** (wheel built from `cdfe688`, installed end-user style into the driver venv;
630 unit green re-confirmed pre-window). Bench: the sealed lab (Frappe v16, site `erpnext.localhost`),
company **Pacioli Test** (USD), baseline verified lock-free (no frozen date, zero PCVs, zero
Accounting Periods — the E6RECON read). Gate5 SI workflow deactivated for the window, restored
after. Every refusal below is the broker's execute-time gate (`stage: red_line`), proven in the
TOCTOU shape — plan + mint while the books were open, the lock landed bench-side *between consent
and execute*, and the fresh execute-time read caught it.

| Leg | Act | Verbatim result | Pin |
|---|---|---|---|
| P5 | `pacioli doctor` (0.9.8) | **both new probes live+green**: `Period Closing Voucher read: readable — required for the PCV boundary of the closed books (get_period_locks)` + `Accounting Period read: readable — required for the closed-period boundary` | **P5 ✅** |
| P1 | frozen line: `accounts_frozen_till_date=2026-06-15`; SI-33 dated **exactly 2026-06-15**, plan+mint open-books, freeze, execute | **refused** `stage: red_line — posting_date 2026-06-15 is within a locked period (frozen_until 2026-06-15)`; SI-34 dated 2026-06-16 (frozen still set): full vertical → **committed, docstatus 0→1 confirmed readback** | **P1 ✅ — refuse-on-tie, allow-day-after; the `<=` matches ERPNext's** |
| P2 | PCV line: draft PCV `ACC-PCV-2026-00001` (period ends 2026-05-31) — **draft did NOT lock** (plan on SI-36 clean, the `docstatus=1` filter proven live); mint, then PCV **submitted** bench-side, execute | **refused** `stage: red_line — posting_date 2026-05-31 is within a locked period (pcv_until 2026-05-31)`; SI-37 dated 2026-06-01: full vertical → **committed 0→1** | **P2 ✅ — refuse-on-tie ON the PCV date; draft PCV invisible by construction** |
| **P3** | **the AP over-refusal, live**: Accounting Period "E6 April JE-only - PT" (2026-04-01→30, `disabled=0`, closes **Journal Entry only**, SI explicitly `closed=0`); SI-35 dated **2026-03-15** (before the period, a doctype it never closes) | broker **refused** `stage: red_line — posting_date 2026-03-15 is within a locked period (closed_period_until 2026-04-30)`. **Control: the SAME scoped seat, the broker's exact transport (`POST …?run_method=submit`), bypassing the broker → HTTP 200, docstatus 0→1.** ERPNext allowed the posting the broker refused | **P3 ✅ — F-S1 documented live: doctype-blind read over-refuses; fail-safe, not fail-correct** |
| P4 | plan-time closed-books disclosure (0.9.8) | AP case: plan **ok:True** carrying verbatim `closed-books: posting_date 2026-03-15 is within a locked period (closed_period_until 2026-04-30) — this posting will be refused at execute unless it changes before then` — **before any marker existed**, and ERPNext's preview passed happily (the one case where this flag is the ONLY pre-consent warning) | **P4 ✅ (AP case; frozen/PCV = finding below)** |
| P6 | trial balance | chain ok, **72→76 receipts** (exactly the two commits' intent+outcome pairs); **zero new orphans** — all 3 red_line refusals were clean pre-act denials (no intent journaled, marker released) | **P6 ✅** |

**🔎 WINDOW FINDING (recorded, no code change): the plan-time closed-books flag is live-reachable
only where ERPNext's own preview survives the lock.** For a **frozen-books** date the memorandum
never forms — ERPNext 417s the ledger preview itself (`You are not authorized to add or update
entries before 2026-06-15`) and `plan_submit` refuses at the request stage; PCV dates ride the same
`make_gl_entries` path. The human is still warned **pre-consent** (earlier, and deny-biased), so
nothing is unsafe — but the 0.9.8 disclosure's unit-test shape (preview passes, flag appended)
occurs live only for locks ERPNext's preview doesn't itself enforce, i.e. exactly the doctype-blind
Accounting Period case (F-S1 territory). Recorded for the F-S1 increment: whatever the doctype-aware
fix does, the disclosure's reachable surface moves with it.

**🔎 BONUS (live, unplanned): ERPNext's frozen-books error message is off-by-one against its own
behavior** — it blocked 2026-06-15 exactly (inclusive `<=`, per `general_ledger.py:809`) while
saying *"…before 2026-06-15"*. The broker's wording ("within a locked period (frozen_until …)")
states the tie honestly. Cosmetic, upstream, noted.

**Window ops:** fixtures sha-gated through `ct_exec` (the known v16 console quirks re-confirmed:
`frappe.local.lang = "en"` required; `acc_frozen_upto` ABSENT on v16 Accounts Settings). Bench
restored: PCV cancelled (docstatus 2), AP deleted, control SI-35 cancelled, frozen cleared, Gate5
workflow reactivated — verified by readback (`E6RESTORE`). Lab STOPPED before disarm.

## PHASE T — F-S1 live: the doctype-aware period lock, the flip proven ✅ (2026-07-07)

Broker **0.10.0** (`2066d97`, wheel end-user style into the driver venv; 659+270 green pre-window).
Same bench, same company, same-day round trip: PHASE S banked the over-refusal evidence in the
morning; F-S1 shipped scout→TDD→redteam in the afternoon; this window proves the flip live at
night. Gate5 SI workflow deactivated for the window, restored after.

| Leg | Act | Verbatim result | Pin |
|---|---|---|---|
| **THE FLIP** | the exact PHASE S P3 fixture rebuilt (Accounting Period "T April JE-only - PT", 2026-04-01→30, closes **JE only**, SI `closed=0`); SI dated **2026-03-15** through the full governed vertical | plan **ok, `risk_flags: []`** (no false flag — the plan-time disclosure went quiet exactly when the lock stopped applying) → mint → submit **committed, docstatus 0→1**. The posting broker 0.9.8 refused this morning at `red_line` is a governed commit under 0.10.0 — and only because ERPNext itself allows it | **✅ the over-refusal is retired** |
| Still refused, both ends | TOCTOU shape: SIs dated **2026-05-01 (== start)** and **2026-05-31 (== end)** planned+minted while May was open; then "T May SI-closed - PT" (closes Sales Invoice) created bench-side; execute both | both **refused** `stage: red_line — posting_date … is within a locked period (closed_period_until 2026-05-31)` — the doctype-aware read still catches its own doctype, inclusive on BOTH boundaries, TOCTOU-fresh | **✅ refuse-on-tie survives the loosening** |
| Day after end | SI dated 2026-06-01, full vertical | **committed 0→1** | **✅** |
| Disabled period | "T May SI-closed - PT" flipped `disabled=1` bench-side; SI dated **2026-05-15** (inside the range, doctype closed) full vertical | plan clean → **committed 0→1** — a disabled period locks nothing, matching ERPNext | **✅** |
| Doctor | `pacioli doctor` with a period present | new item-GET leg fired live: `ok Accounting Period item read (T April JE-only - PT): readable — required for get_period_locks to read closed_documents (the doctype-aware period-close check, F-S1)` | **✅** |
| Trial balance | prove sweep | chain **76→82** (exactly the 3 commits' intent+outcome pairs); **zero new orphans** (still 9, all prior-window; both red_line refusals journaled nothing, markers released) | **✅** |

**Window ops:** bench restored + readback-verified (both fixture periods deleted, `ap_count: 0`,
Gate5 workflow reactivated); lab STOPPED before disarm. The full F-S1 arc — found (E6 PHASE S),
evidenced (P3 same-seat control), fixed (0.10.0, scout-first + TDD + 3-lens redteam), and
live-proven (this phase) — closed inside one day.

## PHASE U — F-C2 live: the single-op wrong-books TOCTOU belt fires ✅ (2026-07-07)

Broker **0.10.1** (`0f057df`, wheel end-user style into the driver venv; 667+270 green + a
correctness/bypass redteam clean pre-window). Same v16 bench, target pinned to **Pacioli Test**
(confirmed via doctor). A second company **Pacioli Test Two** created as the drift destination
(kept on the bench as staged fixture for the future E7 second-company pins P5/P6). Gate5 SI
workflow deactivated for the window (its SoD gate runs ahead of F-C2 and would otherwise mask the
belt under test), restored after.

| Leg | Act | Verbatim result | Pin |
|---|---|---|---|
| Setup | draft SI in **Company A** (Pacioli Test) → `plan_submit` → `mint` (plan + marker recorded while company matched the pin) | plan ok; marker minted | — |
| **The drift** | Administrator `db_set("Sales Invoice", …, "company", "Pacioli Test Two", update_modified=False)` — company **A→B**, `modified` **UNCHANGED** (`2026-07-08 00:10:35.968620` before and after) — the exact evasive shape `check_fresh` cannot see | drift confirmed, `modified_unchanged: true` | — |
| **P4 (headline)** | `submit_sales_invoice` under the stale plan + marker | **REFUSED** `stage: plan — document belongs to company 'Pacioli Test Two' but target 'bench' is pinned to 'Pacioli Test' — wrong books`. Pre-0.10.1 this write would have committed (the finding); the belt now catches it on company alone, ahead of every doctype/date belt | **✅ the gap is closed live** |
| Write untouched | readback after the deny | docstatus **still 0** — the refusal wrote nothing | **✅** |
| **Marker not spent** | restore company **B→A** (`db_set`, `modified` still untouched) → re-`submit_sales_invoice` with the **SAME** marker | **committed 0→1** — the marker survived the wrong-books deny `live` and carried the now-legitimate act. **Consent is spent by commitment, not refusal** — the campaign's core invariant, holding for F-C2 | **✅** |
| Trial balance | prove sweep | chain **82→84** (SI-43's intent + the eventual outcome); **zero new orphans** (the wrong-books deny journals nothing — a pre-act refusal, marker released) | **✅** |

**The window in one line:** the wrong-books drift that 0.10.0 would have committed is refused by
0.10.1 on company alone, writes nothing, and does not burn the marker — found by the E7 scout pass
this evening, fixed + redteamed + proven the same evening. **F-C1 (the v15 half of E7) remains
bench-gated on a provisioned v15 bench (lab egress = John's hand); its fix is developed +
unit-tested arm-free, its bench proof deferred.**

**Window ops:** Gate5 workflow reactivated + verified; Company "Pacioli Test Two" left as staged
fixture (P5/P6); SI-43 committed (a real submitted invoice, like every prior window's). Lab STOPPED
before disarm.

## PHASE V — E7 v16 config pins (P5, P7, P8) — proof-only window ✅ (2026-07-07)

Broker **0.10.2** (`882b831`). Same v16 bench. A **proof-only** window by design (context-bounded):
run the pins, document what each shows, stage any finding rather than build it. An `unpinned`
registry target (same base_url/creds, no `company` key) was added to the driver registry for P5.

| Pin | Act | Verbatim result | Verdict |
|---|---|---|---|
| **P5 (unpinned target skips the belt)** | SI-44 (Company A) drifted to Company B via `db_set(update_modified=False)`, then `plan_submit` via the **pinned** `bench` target vs the **unpinned** target | pinned → `ok:false stage: plan — document belongs to company 'Pacioli Test Two' but target 'bench' is pinned to 'Pacioli Test'`; unpinned → `ok:false stage: request — HTTP 417 (ERPNext's own account-mismatch validation)`, **NOT** the broker's wrong-books refusal. The stage difference is the signal: pinned = broker refuses on company before touching ERPNext; unpinned = broker forwards, the belt correctly skipped. Also confirms the `bench` target IS company-pinned (so P4/PHASE U proved a real pin) | **✅ HOLD** (unpinned posture is by design — `registry.py`) |
| **P8 (custom fields invisible)** | two custom fields added to Sales Invoice (`custom_note` plain, `custom_serial` `no_copy=1`); SI-45 carries both; `plan_submit`+mint+`submit` via bench | plan `ok, risk_flags: []` (no spurious flag from the custom fields); submit **committed 0→1** — the broker's named-`.get()` reads never see custom fields | **✅ HOLD** |
| **P7 (amend copies a `no_copy` field — the residual)** | SI-45 cancelled (governed, 1→2) → `amend_sales_invoice` → new draft `ACC-SINV-2026-00045-1`; read its custom fields | the amended draft carries **`custom_serial = "NOCOPY-SERIAL-P7"`** — the `no_copy=1` field **COPIED** into the amendment, where — this window *asserted, without a control leg* — ERPNext's own desk-side amend would blank it. The broker's amend payload (`amend.py`) is a deny-LIST, not `no_copy`-meta-aware, and copies it with no disclosure | **✅ HOLD as NATIVE PARITY** (was 🔴 FINDING F-C3 — **REFUTED** 2026-07-07, see below) |
| **P6 (cascade second-company)** | — | **Not run live this window.** Confirmation-only of already-fixed behavior: `_cascade_books_gate` is the *original* instance of the wrong-books belt (F-C2 mirrored it), is unit-tested (`test_cascade_execute_refuses_wrong_books_dependent_toctou`), and its identical single-op logic was just live-proven at PHASE U/P5. A dedicated cross-company cascade fixture is disproportionate context for the lowest-value confirmation of the set; deferred, not skipped silently | ◑ deferred (covered-by-construction + PHASE U) |

**🔴 FINDING F-C3 (staged — its own increment, NOT built this window):** the broker's amend copies a
`no_copy=1` custom field into the amended draft (fidelity gap vs ERPNext's native amend, which
respects `no_copy`), and does so with no disclosure. `amend.py`'s strip-list is a fixed name/rule
list, not a walk of the target's per-field `no_copy` meta — the module's own honest-limit docstring
predicted exactly this. Not a wrong-books/consent breach; a fidelity + disclosure gap (a copied
serial/barcode/"already-invoiced" marker could duplicate a unique value). Disposition (fix vs
accept-and-disclose) is John's call; the fix (read the doctype's field meta and blank/omit `no_copy`
fields, or at minimum disclose them in the amend risk_flags) is F-C3, staged for a fresh session
with full context.

**❌ F-C3 REFUTED (2026-07-07, the staged session — source-verified, closed on John's call):** the
finding's premise ("ERPNext's native amend blanks `no_copy` fields") is **false in the frappe
source**, v15 and v16 verbatim-identical (`frappe/public/js/frappe/model/create_new.js:281-320`,
version-16 @ `9a8daf3`; version-15 fetched and diffed same day). Native amend is a **client-side**
flow (`Form.amend_doc` → `frappe.model.copy_doc(doc, from_amend)`; no server-side amend exists —
`amended_from` is set in JS, form.js:1145-1152) and its strip condition is
`is_no_copy = !from_amend && cint(df.no_copy) == 1` — **on amend the no_copy strip is explicitly
DISABLED**; only `name`/`amended_from`/`amendment_date`/`cancel_reason`, `Password`-type fields,
and `__`-internal keys are withheld. It is **"Duplicate"** (from_amend falsy), not amend, that
strips `no_copy`. Server-side `frappe.copy_doc` defaults `ignore_no_copy=True` ("No_copy fields
also get copied" — its own docstring) and is not part of the amend flow anyway. P7's live
observation **stands** — the broker copied the field — but that is **native parity, not a fidelity
gap**; the "ERPNext would blank it" half of the P7 verdict was asserted, never run as a control.
The broker's strip-list is a strict **superset** of native amend's withheld set for all four
supported doctypes (none define `amendment_date`/`cancel_reason`/`Password` fields — grepped the
v16 doctype JSONs). Honest residual (recorded, not built): a site-added *custom* field of fieldtype
`Password` would be withheld by native amend but copied by the broker's amend. **Disposition:
CLOSED as refuted; docs-only truth-sizing, no code change, no new grant** (`amend.py` docstring
corrected same commit). Anti-drift lesson, banked: a live proof of OUR half plus an *asserted*
native half is not a confirmed divergence — run the control leg or read the source before filing
a gap.

**Window ops:** proof-only, per the session boundary (build nothing against the context ceiling).
Bench restored + readback-verified: SI-44 company→A, both P7/P8 custom fields deleted, Gate5
workflow reactivated. Company "Pacioli Test Two" + the `unpinned` scratch registry target kept as
fixtures. Chain 84→90, zero new orphans. Lab STOPPED before disarm.

## PHASE W — E8 volume/caps · transport taxonomy (T) · F-R1 settling-PE, ALL LIVE ✅ (2026-07-08)

Broker **0.11.0** (`3efbc3c`). Same v16 sealed-lab bench. The largest single window of the campaign:
**16 pins across three staged sheets** run end-to-end under the scoped credential through the broker,
bench-side fixtures dogfooded through Proximo `ct_exec` (John's standing "dogfood everything we dev").
A **0.10.2 control venv** (git worktree `882b831`) was stood up beside 0.11.0 to prove F-V1 both ways.

### R (F-R1) — the settling-PE cancel disclosure, BREAKING grant proven live
The doctor's new **Payment Ledger Entry** read probe FAILED first (`XX … 403 … without it every
plan_cancel/plan_cascade_cancel on this target refuses`, both targets) → the seat's *API Key Scope*
was widened by one `resource_doctypes` row (Administrator, bench console) → doctor **ready**. That is
R4 proven both directions: the BREAKING grant is really required and really gates the tool.

| Pin | Act | Verbatim result | Verdict |
|---|---|---|---|
| **R1 (ON voice + live unlink)** | SI settled by a PE (`ACC-SINV-2026-00046` ← `ACC-PAY-2026-00022`, 100), `plan_cancel` then governed cancel | flag pre-consent: *"cancelling will SILENTLY UNLINK Payment Entry ACC-PAY-2026-00022's allocation of -100.0 … the payment stays posted but the settlement link is severed and its unallocated amount increases (auto_cancel_exempted_doctypes)"*; cancel → docstatus 2; **readback**: PE reference row DELETED, `unallocated_amount` 0→100, PE GL untouched, PLE against the cancelled SI empty | **✅ HOLD** |
| **R2 (OFF voice + live refusal)** | unlink setting OFF, `plan_cancel` on a PE-settled SI (`ACC-SINV-2026-00047` ← `ACC-PAY-2026-00023`) then execute | flag: *"ERPNext will REFUSE this cancel (LinkExistsError) while ACC-PAY-2026-00023 references it"*; execute → `ok:false stage: execute — HTTP 417: LinkExistsError … at Row: 1`; SI stayed docstatus 1. Setting restored ON | **✅ HOLD** |
| **R3 (control)** | `plan_cancel` on an unsettled SI | `risk_flags: []` — zero settling noise | **✅ HOLD** |

Bonus F-R1 proof surfaced at P2: a 1-node cascade over an SI with 24 settling PEs produced 24
docname-prefixed silent-unlink flags — **settling PEs are invisible to `get_submitted_linked_docs`
by construction**, exactly the blindness F-R1 exists to voice.

### PHASE W (E8) — volume & caps

| Pin | Act | Verbatim result | Verdict |
|---|---|---|---|
| **P1 (F-V1 both directions, TOCTOU)** | 25 Accounting Periods drifted onto 2026-06 (enabled SI-closer at page-2 position 25) via `db_set`; both venvs planned+minted books-open, trap drifted in, then executed | **0.10.2 control** → `stage: execute — HTTP 417 ClosedAccountingPeriod` (broker's OWN red_line NEVER fired — the page-1 miss; consent spent, stopped only by ERPNext's native check). **0.11.0** → `stage: red_line — posting_date 2026-06-15 is within a locked period (closed_period_until 2026-06-30)` (broker's own gate, pre-forward) | **✅ FIX HOLDS** |
| **P2 (cascade exactly at the cap)** | 25-node graph (SI + 24 JEs) planned; live-grown to 26, executed; shrunk to 25, re-executed; re-planned + executed | 25-plan `total:25` dependents-first; at 26 → `stage: plan — cascade graph has 26 documents, over the cap of 25; refuse rather than unwind` (nothing partial); shrunk-to-25 same marker → `stage: fresh — plan is stale: the document changed after planning` (freshness caught the drift, zero acts); fresh plan → **cancelled 25/25** dependents-first | **✅ HOLD (TOCTOU-safe)** |
| **P3 (100-item memorandum)** | 100-row SI, `plan_submit` | memorandum GL rows = **2**, ERPNext's own `show_accounting_ledger_preview` = **2** (same-account grouping is ERPNext's, upstream) — **exact parity, broker truncates nothing**; 694-byte response; submit 0→1 | **✅ HOLD** |
| **P4 (burst/429)** | site_config `rate_limit` on (processing-time budget), driven to exhaustion | verbatim `HTTP/1.1 429 TOO MANY REQUESTS` + `Retry-After: 50` + `X-RateLimit-Reset: 50`; `plan_submit` → `stage: request — HTTP 429: TooManyRequestsError` (structured, pre-consent) | **✅ HOLD** |
| **P5 (queue_in_background beyond JE)** | DocType `queue_in_background=1` on Sales Invoice, governed submit | submit ran **inline** (docstatus 1 in response and DB). Source: v16 honors the flag **only** on the desk savedocs path (`desk/form/save.py:38`); the broker's `run_method=submit` transport never reaches it → the stale-snapshot trap is **unreachable by call-shape** on v16 | **✅ HOLD (immune by construction)** |
| **P6 (chain prove)** | `prove_verify` + `prove_orphans` both targets | bench chain `ok, count 168`; t1proxy chain `ok, count 8`; every new open intent reconciled against real docstatus (see below) | **✅ HOLD** |

### T (transport taxonomy) — the 0.10.4 law proven live end-to-end
Instrument: a local filtering proxy (registry target `t1proxy`) that forwarded every read but
killed or mangled **only** the mutating `run_method` POST.

| Pin | Act | Verbatim result | Verdict |
|---|---|---|---|
| **T1 (the flip)** | mutating POST severed mid-flight, live marker | `stage: unconfirmed — submit raised (… Remote end closed connection without response) with no answer from the bench; a readback shows the document at docstatus 0 … The consent marker is spent`; doc stayed 0; replay → `marker not available (state: consumed)` | **✅ SPEND, no release-in-flight** |
| **T2 (answered refusal releases)** | 417 LinkExistsError (answered), same marker twice | both executions reached ERPNext (identical 417) → marker **released** (re-usable). *Also banked:* a db-drifted unbalanced JE hit the broker's own `total_debit != total_credit` preflight twice on one marker — broker-side denies don't spend | **✅ HOLD** |
| **T4 (proxy-shaped ambiguity)** | mutating POST answered nginx-style HTML 502 (no frappe envelope) | `stage: unconfirmed — submit raised (HTTP 502: bench refused the call) with no answer …`, marker consumed (the 0.10.4 "generic body ≠ answered" redteam fix holding live) | **✅ SPEND + readback** |
| **T5 (success-then-severed), LIVE — not unit-only** | proxy forwarded the POST (bench COMMITTED) then severed the response | receipt `ok:true stage: done` with `{"error": "… Remote end closed connection …", "docstatus": 1, "confirmed_via": "post_failure_readback"}` — original error + confirmed commit in one receipt; bench docstatus 1 verified; marker spent | **✅ HOLD** |
| **T3 (429 releases)** | 429 mid-window on a live marker, then window reset | 429 landed on execute's first read → `stage: request — HTTP 429`; after reset the **same marker committed** (SI `ACC-SINV-2026-00058` 0→1) — a marker that ate two 429s posted cleanly = 429 releases, never spends | **✅ HOLD** |

**Orphan reconciliation (P6/R5):** the two chains carry a handful of open intents from this window;
every one is an **honest** open receipt, not a lost mutation — reconciled live against real
docstatus: the taxonomy's `unconfirmed`/`failed` outcomes (T1/T4 severed, F-V1 control's spent-then-
417, the double-executed R2 cancel) deliberately do **not** finalize the intent (only `committed`
does), so `prove_orphans` keeps them visible for exactly this reconciliation. SI-00033/00055/00056 =
docstatus 0 (their mutations never landed — correct); SI-00047 = docstatus 1 (the OFF-voice cancel
ERPNext refused — correct). **Zero silent gaps.**

**Findings (docs-only truth-sizing, no code change):**
- **F-V1 severity, sized down honestly.** The E8 sheet projected "posting into closed books is
  ALLOWED"; live on a healthy v16, the net outcome of the pre-fix control was **fail-safe** —
  ERPNext's own unbounded `ClosedAccountingPeriod` check caught it. The real defect grade is
  *belt-and-suspenders broken + wasted consent* (the broker's own gate went silent and the posting
  depended on a single native check), **not** a books breach. The 0.11.0 fix restores the broker's
  independent gate. Recorded, not re-opened.
- **P5 closes stronger than pinned:** immune-by-call-shape, not merely honest-readback. E1's JE
  honesty law stays the belt if frappe ever widens queueing to the REST path.

**Window ops:** bench fully restored + readback-verified — Gate5 workflow reactivated,
`allow_multiple_items` OFF, unlink ON, all 25 APs deleted, `rate_limit` removed from site_config
(bench restarted twice this window for site_config reload — announced), `queue_in_background` OFF,
credit-limit cleared, both P7-era custom fields already gone. Filtering proxy killed, nft table
removed. Closing **doctor: ready**. Lab **STOPPED** before disarm. Push + disarm = John's `!`.

## PHASE X — F-R2 reconcile LIVE (P1–P8, wire corrected in-window) · the tight-role seat (T-P1–P5) ✅ (2026-07-09)

Broker **0.13.0 → 0.13.1** (the wire fix built mid-window, TDD, 841 green). Same v16 bench
(the sealed lab). Two staged sheets ran end-to-end in one arm: `docs/plans/2026-07-09-fr2-govern-
reconciliation.md` P1–P8 + `docs/plans/2026-07-09-tight-role-seat.md` P1–P5.

### The P7 story — 0.13.0's honest "NOT live-proven" flag, cashed in
0.13.0's first live governed reconcile **refused exactly as its own CHANGELOG predicted**
(HTTP 500 TypeError; broker degraded honestly: `unconfirmed` row, readback proved nothing landed,
receipt froze the bad wire in the chain at seq 168). Bench-side reproduction (rollback-only)
answered both BENCH-PENDING questions:
1. **`invoices[]` pool REQUIRED** — `validate_allocation` builds its outstanding map from
   `self.get("invoices")`; absent → `flt(row.allocated_amount) - None` TypeErrors
   (`payment_reconciliation.py:735`). A `payments[]` pool is NOT read on this path — not sent.
2. **`amount` AND `unreconciled_amount` are BOTH the payment's unallocated** — 0.13.0 had the
   semantics swapped (invoice outstanding in `unreconciled_amount`); the live anti-tamper
   (`check_if_advance_entry_modified`, `utils.py:645-647`) compares it to the PE's live
   `unallocated_amount` and refused: *"Payment Entry has been modified after you pulled it."*
   Entries are processed grouped per voucher, every check before the group's single save
   (`reconcile_against_document`) → plain pre-write values, no running decrement.

**Fix 0.13.1 (PATCH):** semantic row keys (`payment_unallocated`/`invoice_outstanding`) at the
core/glue seam; the ONLY semantic→wire translation lives in `erpnext.py reconcile()`; echo values
stay PINNED (what the human saw disclosed) so ERPNext's anti-tamper doubles as a second TOCTOU
belt — drift past `check_fresh` gets an answered pre-write refusal, marker released.

### F-R2 pins

| Pin | Act | Verbatim result | Verdict |
|---|---|---|---|
| **P1+P7 (the landing)** | SI-00061 (100) ← PE-00049 (60 unallocated), plan→mint→reconcile under 0.13.1 | plan disclosed `100.0 -> 40.0` + all 3 standing flags; **committed**, readback outstanding 40.0; bench truth: PE unallocated 60→0, NEW reference row allocated 60, PLE settlement −60 | **✅ HOLD** |
| **P2 (over-allocation)** | propose 50 vs live outstanding 40 | plan disclosed `-10.0` projection + *"will be refused at execute"* flag; execute → `stage: allocation — allocated 50.0 exceeds the invoice's live outstanding 40.0`; **marker stayed live** (broker-side deny never spends) | **✅ HOLD** |
| **P3 (the headline: closed-books belt)** | AP "FR2 P3 Lock - PT" closes SI+PE for the doc date; broker vs raw ERPNext | plan flagged the lock on BOTH doc dates pre-consent; execute → `stage: red_line — posting_date 2026-07-10 is within a locked period`. **CONTROL: the identical raw `reconcile()` into the identical closed period SUCCEEDED** (outstanding 100→70 pre-rollback, rolled back) — ERPNext's period belt never fires for reconciliation; the broker is the only lock on that door. *(Control ran as Administrator in-process; the bypass is structural — the relink write never reaches `save_entries` — so seat role is irrelevant to it.)* | **✅ HOLD, bypass PROVEN live** |
| **P4 (TOCTOU, both shapes)** | (a) real edit between mint and execute; (b) `db_set outstanding=20, update_modified=False` | (a) `stage: fresh — plan is stale`; (b) freshness-invisible drift caught by the live ceiling: `stage: allocation — allocated 30.0 exceeds the invoice's live outstanding 20.0` | **✅ HOLD** |
| **P5 (EG side-effect)** | SI EUR@1.10 ← PE EUR@1.05, 100 EUR | plan named the system-JE class pre-consent; committed; bench authored **ACC-JV-2026-00044**: `is_system_generated=1`, **owner=scoped@proof.local**, exactly 5.00 USD, balanced rows naming BOTH SI and PE. ERPNext computed the gain/loss from its own reads — no rate fields needed on the wire | **✅ HOLD** |
| **P6 (laundering door)** | grant dropped; agent-supplied `docs` body via bare `run_doc_method` | guard **403 pre-dispatch** (`validate_auth`, before frappe routes): `not permitted … (method: Payment Reconciliation.reconcile)`; grant restored after | **✅ SHUT** |
| **P8 (trial balance + cross-company)** | chain + both cross-company layers | chain `ok, count 174`; the ONE new orphan = the 0.13.0 failed attempt (honest open, bad wire frozen in the receipt); both committed reconciles paired. Cross-company: (a) pinned target → `wrong books` verbatim; (b) unpinned target, B-company SI-00064 → `belongs to company 'Pacioli Test Two' … cross-company allocation refused` | **✅ HOLD** |

### The tight-role seat (T-P1–P5)

The minimal seat **built live and certified**: Role **"Pacioli Seat"** (Custom DocPerm: SI `cancel`
+ `read` on Accounts Settings / Period Closing Voucher / Workflow) + user `tight@proof.local`
(**Accounts User + Pacioli Seat only**), guard scope mirrored from the proof seat, registry target
`tight`.

| Pin | Verbatim result | Verdict |
|---|---|---|
| **T-P1** | `doctor --target tight` → **ready**, every read probe green, `ok roles: seat carries Accounts User, Pacioli Seat — no spine-voiding role` — the 4-grant recipe covers the whole broker surface live | **✅** |
| **T-P2** | full governed vertical under the tight seat: SI-00065 plan→mint→submit **0→1**, plan→mint→cancel **1→2** (the cancel = Pacioli Seat's write-gap grant working; Accounts User alone cannot cancel an SI). tight chain `ok, count 4`, **0 orphans** | **✅** |
| **T-P3** | + System Manager → `XX roles probe: the seat carries System Manager — … dismantle the governance it operates under` verbatim; removed after | **✅ FAIL fires** |
| **T-P4** | `User.get_roles` grant dropped → `XX roles probe (403): … grant the method 'User.get_roles' … cannot certify` (deny-biased); restored after. *(Also observed pre-staging on the legacy seat — the probe fired 403 the moment 0.12.0 hit this window, before any grant existed.)* | **✅ FAIL fires** |
| **T-P5 (verification gap)** | live reads: `Company.role_allowed_for_frozen_entries` **null** (BOTH companies), `accounts_frozen_till_date` null, zero APs remain, AP meta default for `exempted_role` **blank**, and THIS window's proof period read `exempted_role: null` at creation while its refusal fired. Historical periods are deleted — retro-read impossible; no fixture script ever set the field. Closed as far as reality allows, honestly | **✅ (bounded)** |

**Closing doctors tell the story:** `tight` → **ready**; legacy `bench` seat → honestly **NOT
ready** (`probe_roles` flags its System Manager) — the shipped probe retiring the very seat that
proved the first nine phases. **The tight seat is the reference seat for future windows.**

**Window ops:** fixtures kept (settled SI/PE pairs + EG JE + the B-company SI-00064 + the tight
seat itself = future-window fixtures; drift remarks/outstanding restored + readback-verified;
Gate5 workflow reactivated after the T-P2 vertical — deactivation announced in-window). New grants
on the proof seat: `User.get_roles` + `Payment Reconciliation.reconcile` (both config-only, zero
guard code). Lab **STOPPED** before disarm. Push + disarm = John's `!`.

## PHASE Y — the doors lab pins: a committed receipt carrying `via`, over each REAL wire ✅ (2026-07-17)

**The claim closed:** the `via` stamp (WHICH door + WHICH principal a governed act came through)
had been proven at the store seam by test, never on a real committed receipt written by a real
governed write over an actual transport. This window drove the SAME governed vertical
(`plan_submit` → out-of-band `pacioli mint` → `submit` → readback → replay-refuse →
`plan_cancel`/`cancel` restore) through all three doors as real socket/stdio clients — the mcp
SDK client (stdio + streamable HTTP) and the official a2a-sdk client — against the live v16
bench, one draft SI per door. Driver: `broker/docs/plans/2026-07-17-doors-lab-pins-driver.py`;
run env broker **0.29.1**, guard 0.6.0 on-bench, seat `scoped@proof.local` (fresh cred, by
reference).

| Pin | Result |
|---|---|
| **P-stdio** (ACC-SINV-2026-00066) | docstatus 0→1 on the bench; committed intent **seq 4** `via={transport: stdio, principal: local-spawn}`; replay refused (`stage: fresh`); cancel-restore intent **seq 6** carries the same via | **✅ HELD** |
| **P-http** (ACC-SINV-2026-00056) | served `--http --auth env:LABPIN_HTTP_T` (bearer honored on loopback); 0→1; intent **seq 8** `via={transport: http, principal: env:LABPIN_HTTP_T}` — the token's REFERENCE label, never material; replay refused; cancel seq 10 same via | **✅ HELD** |
| **P-a2a** (ACC-SINV-2026-00055) | card resolved, JSON-RPC DataPart convention; 0→1; intent **seq 12** `via={transport: a2a, principal: loopback}`; replay refused; cancel seq 14 same via | **✅ HELD** |
| **Trial balance** | `pacioli verify` → chain ok, 16 receipts, head `80df52ae…0719`; `orphans` → exactly the 2 HONEST orphans = the two 417-refused fixture-stale submit attempts (intents recorded durably pre-wire, nothing landed, each naming plan/doc — and each already carrying `via: stdio`) | **✅ accounted** |

**What the window ALSO earned (arm-free leg, before the arm):** the pre-window door smoke — the
first real-MCP-client drive of the guarded HTTP door over a socket — caught the 0.28.0 webguard
synthetic-disconnect bug (every HTTP-door SSE response aborted before its result; fixed TDD as
**0.29.1**, 3 contract pins, suite 1720 green). Refusal-path coverage is not door coverage.

**In-window refusals, all honest:** (1) first submit refused `stage: workflow` — the bench's
Gate5 SI-approval workflow governs direct submits; the broker refused and named the transition
path (governance working, not a bug); workflow deactivated for the window, **reactivated after,
readback-verified `is_active=1`**. (2) two submits refused `stage: execute` HTTP 417 — the
draft fixtures' due dates (doc + Payment Schedule child rows) predated the auto-reset posting
date; fixture maintenance (dates → 2026-08-31), both attempts left as the 2 disclosed orphans.

**Window ops:** every bench mutation through Proximo `ct_exec` under John's arm; bench-execute
gotcha reconfirmed (falsy returns print NOTHING — gate on a positive readback, e.g. SELECT).
Fixture state after: the 3 SIs are docstatus 2 (submitted→cancelled — future windows amend or
mint new drafts). Box-side: labpin registry/cred/state persist. Lab stop + disarm = John's hand
at window close.
