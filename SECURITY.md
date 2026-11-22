# Security

Pacioli governs access to accounting books. A defect here is a defect in someone's ledger, so this
file states plainly what is supported, what is covered, and what is not.

## Supported versions

| Package | PyPI | Supported | Notes |
|---|---|---|---|
| `pacioli-guard` | `pacioli-guard` | **0.18.0 and later** | Two published *security* defects, both fixed by upgrading. In 0.6.3 and earlier an empty resource allowlist returned permitted, granting every DocType (incorrect authorization). In 0.9.6 the consent gate let a nested cancel ride a marker that authorised a *submit*, so a caller-named pre-existing document could be cancelled with no marker of its own — fixed in 0.10.0. Separately, 0.10.0 read a marker's expiry through the process clock instead of the site's, so on any site whose `time_zone` differs from the container's every marker was born expired and no governed write could proceed — a functional defect, not a vulnerability, fixed in 0.10.1. Ten versions were published through 0.15.0: 0.6.2, 0.6.3, 0.9.6, 0.10.0, 0.10.1, 0.11.0, 0.12.0, 0.13.0, 0.14.0 and 0.15.0 (0.16.0 is the eleventh, below). (**Corrected 2026-08-11:** this sentence said "eight" and stopped at 0.13.0 while the same cell went on to discuss 0.14.0 at length. The 0.14.0 edit did not sweep it.) **0.9.0–0.9.5 were never published** and are not supported. 0.11.0 carries no security fix — it adds the site-wide `allow_all_doctypes` grant and corrects documentation that described a `"*"` row the grant document could never store. That mis-documentation failed *closed* (an unstorable row leaves the allowlist empty, which denies), so it is not a vulnerability and has no advisory. 0.12.0 carries no security fix and changes no enforcement path (`enforce.py`, `act.py`, `scope.py` untouched) — it adds `gate_registered` and `consent_enforced` to `consent_status` so an operator can see whether the consent gate is actually loaded. **It surfaces a deployment condition worth checking on your own install:** the consent gate rides `doc_events`, and a site whose hooks cache predates its consent support can hold `require_consent = 1` while those handlers are not registered, in which case acts are not consented even though the grant says they are. Scope rides `auth_hooks` and is unaffected, which is what makes it hard to notice. No version of guard mis-decided anything and the packaged artifact was correct on disk, so this is not a vulnerability and has no advisory — but if you rely on `require_consent`, read `consent_enforced`, and if it is false while `require_consent` is true, clear your site cache and confirm with an actual refusal. **0.13.0 is the recommended version. It carries no fix for a credential-escape defect, so 0.12.0 is not vulnerable in the sense 0.9.6 was, and it does carry four things worth knowing.** (a) The consent gate now covers ERPNext's ledger PREVIEW, which previews a posting by performing it and rolling the transaction back. Through 0.12.0 that call sat in `SAFE_METHODS` described as read-only. It was mislabelled rather than exploitable: the cascade it triggers was refused by the consent gate and the transaction rolled back, so nothing posted. The label is now accurate and the preview requires the same marker as the submit it previews. (b) A minted marker is immutable except for `burned`. Before this, anything holding write permission on `Pacioli Consent Marker` could extend a marker's expiry, repoint it at a different document, or re-stamp `minted_by` and so defeat the minter-separation check with a value it chose. That requires System Manager and is therefore out of scope by the policy below. It is recorded anyway, because those are the exact fields every other consent guarantee reads. **Deleting** a marker is still permitted at the same permission level and is not blocked. (c) The expiry is now stored as epoch seconds in `expires_at_epoch` rather than as a site-naive Datetime re-interpreted at spend time. Two ways the old representation could read a live marker as lasting LONGER than its TTL: changing `System Settings.time_zone` re-timed every marker already minted, and the UTC fallback in `_as_instant` errs later rather than earlier for a site east of UTC, while its own docstring claimed it could only ever err earlier. That fallback fires only when reading the site zone RAISES, not when the zone is merely unset, because frappe returns `Asia/Kolkata` for an unset zone. So it is narrow. In every case the direction is a longer life for a marker a human really did mint, never a marker they did not: document binding, act binding, single use and minter separation are untouched. If you minted markers with a script of your own that wrote a process-clock `datetime.now()`, measure the lifetime you are actually getting; on a site whose zone differs from the container's, ours read 17,980 seconds for a 900-second marker. (d) `_epoch` now refuses a non-finite expiry, which previously would have produced a marker that never expired. Latent only, since a `DATETIME(6)` column cannot store one. **0.13.0 has a required upgrade order, and skipping either step fails closed in a way that looks like the upgrade did nothing. See "Upgrading" below.** **0.14.0 is now the recommended version. It carries no fix for a credential-escape defect and changes no allow/deny decision on the scope path, but it corrects two things the app REPORTS about itself, and one of them will change what your monitoring sees.** (a) `consent_status`'s `gate_registered`/`consent_enforced` checked for two of the four `doc_events` handlers that can deny; the two preview gates shipped in 0.13.0 and they refuse. So the receipt reported the gate loaded on a site carrying only the pre-0.13.0 pair — the exact stale-hooks-cache condition the 0.12.0 note above tells you to check for, one release later. ⚠️ **A readiness check that returned true may now return false with no change to your site.** That is the probe working: run `bench migrate` then `bench clear-cache` so the 0.13.0 handlers are registered, and re-check. (b) `consent_status`'s resource posture reported `allow_resource=1, allow_all_doctypes=1` — the widest grant this app can express — as `denies_all`, the narrowest of its four states, because it never read the `allow_all_doctypes` flag that replaced the retired `"*"` sentinel row. A new value, `all_doctypes`, is now returned; anything switching on that string needs a branch for it. Neither defect mis-decided anything — enforcement never consulted either report — so neither is a vulnerability and neither has an advisory. They are disclosure defects, and both erred toward telling an operator their posture was SAFER or NARROWER than it was, which is the direction worth publishing. 0.14.0 also makes an unreadable multi-doc request body deny-close instead of falling through to the doctype-blind create residual; that was not exploitable (frappe's own parser rejects the same bodies) and no working call changes. **0.15.0 raised the Python floor to 3.12 (from 3.10) and bounds the build backend's major; it carries no security fix and changes no allow/deny decision. A site on 3.10 or 3.11 stays on 0.14.0, which this table still supports.** **0.16.0 is now the recommended version. It carries no security fix and changes no allow/deny decision on the scope path: it adds one read-only endpoint, `pacioli_guard.api.my_roles`, to `SAFE_METHODS` so `pacioli doctor` can read the calling seat's own roles after frappe 16.33.0 removed `User.get_roles`. The endpoint takes no arguments and reports only on the session user; a seat that still holds the old `User.get_roles` grant keeps that function's `?uid=` enumeration residual until the grant is removed (the road no longer grants it). Eleven versions were ever published: the ten above and 0.16.0.** 0.17.0 (2026-10-05) added the delete gate and carries no fix for an earlier published defect. **0.18.0 is the only supported version, and the one to install. It fixes four security defects reported privately, which affect every earlier release:** GHSA-7267-5929-54c6 (a resource write moving `docstatus` without the submit or cancel grant), GHSA-wc4v-5w73-74w4 with GHSA-9h6v-f656-p77h (a consent-gated seat editing a submitted document with no marker), GHSA-mqmm-jvxm-423w (the same edit reached through a child-row update's parent re-save) and GHSA-7p4h-gwc5-w282 (the marker lookup not asking for the act); and more found by our own reviews (the CHANGELOG has each). GHSA-c7c5-4994-6xx3 and GHSA-5wf4-v3f5-jqg6 are stated residuals, below. Everything above that recommends an earlier version, or calls one still supported, is superseded by this sentence: a site on Python 3.10 or 3.11 has no fixed version (0.18.0 needs 3.12). Thirteen versions were ever published: the eleven above, 0.17.0 and 0.18.0. |
| `pacioli` (broker) | `pacioli` | **0.33.3 and later** | 0.33.2 and earlier used urllib's default opener, which re-sends `Authorization` when following a 3xx and permits plain `http`, so a redirecting endpoint could take the broker's credential to a host the registry never pinned. Fixed in 0.33.3; upgrade. **0.34.0 carries no security fix.** It adds the broker half of guard 0.13.0's preview gate: `plan_submit` accepts a `consent_token` and forwards it to the ledger preview, which is what makes a governed write completable at all on a site with `require_consent` on. With no token the request is byte-identical to 0.33.3. **Pair it with guard 0.13.0.** Guard 0.13.0 alone gates the preview while a broker at 0.33.3 has no way to present a marker, so `plan_submit` is refused and PLAN cannot complete. That is fail-closed and it is not a regression, since a consent-enforced site on guard 0.12.0 refused the preview's cascade instead, but the two halves are only useful together. **0.34.1 carries no security fix and changes no enforcement path, but it corrects what a human is shown at the consent moment, so it is worth knowing about if you approve markers by reading `pacioli mint`.** From 0.33.2 through 0.34.0 the projected-GL summary printed `debits 0.00 / credits 0.00` for every **submit-direction** plan a real bench produced: `ledger_preview` returns its rows as lists, and the helper that summed them only understood dicts. Two effects. The size of the act was understated as zero to the person approving it. And the "no debit without a credit" check sitting beside it compared `0.0 - 0.0`, so `projected entry DOES NOT BALANCE` could never fire on one — an alarm that was present, documented, and unreachable. **Correction, 2026-07-31:** through 0.34.2 this paragraph said "every plan a real bench produced" and "could never fire on any plan a real bench produced". Both were too broad and are corrected here rather than quietly dropped. `plan_cancel` and `plan_cascade_cancel` build their rows from `get_gl_entries`, which really does return dicts, so cancel-direction plans summed correctly and their alarm was reachable throughout. The defect was submit-direction only. Nothing was mis-authorized: consent was still required, markers were still document- and act-bound, and the floor still enforced, which is why this is not a vulnerability and has no advisory. It is a disclosure defect, and disclosure is what a consent ceremony is for. 0.34.1 also makes an unreadable row say so (`totals unavailable ... no balance check was made`) rather than silently score zero. **0.34.2 carries no security fix either, and corrects a regression 0.34.1 introduced in that same disclosure.** 0.34.1 let a single unreadable row blank the totals and skip the balance check for the whole entry. **The evidence first given for that was overstated and is corrected here:** the two scenarios cited ("an imbalance 0.34.0 would have flagged went silent", and "a cascade, which concatenates every node's rows, lost the disclosure for all of them") were demonstrated with hand-built rows, not observed on a bench. 0.34.0 could only score dict rows, and the cancel path that carries dicts validates every amount at the `get_gl_entries` seam, so a cascade row cannot be unreadable and neither scenario is reachable on a stock bench. The regression is real against 0.34.1's own intent and the fix stands on that; the claim that a bench had lost a real alarm did not. 0.34.2 degrades per row instead: it totals what it can read, names how many rows it could not, and declines to assert imbalance from a partial read, since an unread row may be the balancing side. It also pins the expected GL row length exactly rather than as a minimum, which matters because an inserted numeric column would otherwise shift both sides together, leaving the entry netting to zero with the alarm quiet and the magnitude wrong; and it applies `math.isfinite` and a bool guard to the money columns, which `get_gl_entries` already did on the cancel path. If you read `pacioli mint` before approving, 0.34.2 is the version to be on. **0.37.0 carries no security fix and changes no enforcement path, but it is the one broker release so far that can REFUSE a call your client makes today, so read this before upgrading.** An argument a tool's schema does not declare is now refused (`stage: "request"`, before anything is claimed or spent) instead of being silently dropped. Every served schema declares `additionalProperties: false` and the broker also enforces it in dispatch, because a declaration is only as good as whoever checks it: the A2A door validates nothing at all, and the `mcp` pin (`>=1.0` at the time) permits 1.x SDKs that do not validate either. (**Updated 2026-08-11:** the pin is `mcp>=1.10,<3`, so the SDK may or may not validate: 1.10+ does, 2.x removed schema validation entirely. Enforcement lives in `dispatch` because two of the three ways in never reach the SDK's check at all. The A2A door validates nothing, and `pacioli_call`'s inner arguments and dynamic by-name calls do not pass through it. The outcome is the same on every path; only the message differs.) The defect it closes is a wrong answer wearing the shape of a right one: passing `doctype` instead of `pacioli_doctype` was ignored, the resolver fell back to its default, and the reply named a doctype the caller never asked for about a document that exists under the one they meant. Nothing was ever mis-authorized — consent, document binding and the floor were untouched — so this is not a vulnerability and has no advisory. **If your client sends keys outside a tool's schema, they will now be refused.** On mcp 1.x both layers refuse and the outcome is the same: with the schemas closed the MCP SDK (1.28.x) validates before the handler and returns `Input validation error`. On mcp 2.x only the broker's own refusal answers, with the same outcome: it names every accepted key and the correction, and answers on the A2A door, on `pacioli_call`'s inner arguments, and on any SDK that does not validate. 0.37.0 also corrects two worst-state messages that told an operator "the consent marker is spent" when a failed ledger write meant it was not (the row still read `reserved` — not spendable, but not spent, and needing different cleanup), and adds the dispatch-layer guard for envelope E1 that had never existed. **0.37.1 carries no security fix, changes no enforcement path, and changes nothing a served call does. It is one dependency bound, and it is the version to install.** Through 0.37.0 the `server` extra declared `mcp>=1.0` with no upper bound. `mcp` 2.0.0, published 2026-07-28, removed the decorator registration API (`Server.list_tools`, `Server.call_tool`) the MCP door is built on. From that date every FRESH `pip install 'pacioli[server]'` resolved an SDK the door cannot start against, and raised `AttributeError: 'Server' object has no attribute 'list_tools'` before it served a single tool. **Every published version 0.30.0 through 0.37.0 is affected, on a new install only.** An environment already holding an mcp 1.x was never affected and nothing about it degrades. This is availability, not authorization: the failure is at handler registration, before the door accepts anything, so no call was ever mis-handled, no marker was ever spent under a half-started door, and consent, document binding and the floor are untouched. It is not a vulnerability and has no advisory. It is published here because "the door will not start" is exactly what an operator needs to recognise quickly, and because the cause is worth naming plainly: an unbounded major in published metadata is a promise that every future major of a dependency will keep working, and no package can keep it. 0.37.1 bounds `mcp` and `a2a-sdk` at their next major and `uvicorn` at the 1.0 line. **0.37.2 carries no security fix either, and it is the version to install. It exists because an independent review of 0.37.1 found that the fix was half a fix and two more requirements were wrong in the same way.** Three things, all packaging, none of them changing a served call. (a) **`pip install 'pacioli[a2a]'` on its own could not build the A2A door.** `a2a.py` imports `a2a.server.routes.*`, which needs `starlette` and `sse-starlette`, and the `a2a-sdk[signing]` extra does not carry them; they arrived only because `[server]`'s `mcp` happens to depend on them. Anyone installing the A2A door alone got `ModuleNotFoundError: No module named 'sse_starlette'` instead of a door, and it reached that error *after* assembling the broker, past the availability probe that was supposed to refuse cleanly. Now `a2a-sdk[signing,http-server]`. (b) **The `mcp` FLOOR was wrong and always had been.** `>=1.0` promised versions this package cannot run on: `mcp.server.streamable_http_manager` does not exist before 1.8.0, so `serve --http` could not start anywhere in 1.0 through 1.7 while the extra advertised it, and this package's own door suite is red below 1.10.0. The floor is now `>=1.10`, which is the oldest mcp actually tested. In the same place, `uvicorn` is now **declared** by the `server` extra rather than inherited as an undeclared transitive of `mcp`, because `serve_http` imports it directly. (c) `cryptography` is now capped at `<51` rather than left open, matching the rule this project follows elsewhere: bound the major, then widen promptly when an advisory lands. The floor stays `>=50` for PYSEC-2026-3552 and has its own test. All three are availability and packaging only. No enforcement path, no allow/deny decision, and no served behaviour changes, so this is not a vulnerability and has no advisory. Worth knowing if you install either extra on its own, or if you pin `mcp` below 1.10. **0.38.0 carries no security fix and changes no enforcement path, and it is now the recommended version.** It widens the `mcp` pin to `>=1.10,<3` so the door runs on mcp 1.10 through 2.x from one build. Tool registration moved from decorator-based to constructor-callback registration. Nothing about a 1.x install changes; it behaves exactly as it did in 0.37.2. mcp 2.x removed the SDK's own schema validation entirely, so on that major the broker's own `dispatch` enforcement is the only refusal. That is not a new gap: the A2A door and `pacioli_call`'s inner arguments already relied on `dispatch` and never on the SDK. The outcome, refused and never silently dropped, is identical on both majors; only the message differs. CI now runs both majors explicitly rather than trusting the resolver. **0.39.0 CARRIES A REAL SECURITY FIX — the first since 0.33.3 — and it is the version to install. It affects you only if you EMBED `pacioli.a2a.build_app` yourself; the shipped `pacioli serve --a2a` was never affected.** Through 0.38.0 `build_app` decided whether a door needed authentication from the ADVERTISED host in `rpc_url`, and its only runtime protection was the perimeter's Host allowlist. Both are claims, not facts: the advertised URL is what an operator typed, and a `Host` header is set freely by any direct client. Host allowlisting only ever defeated a BROWSER's DNS-rebinding, never a direct caller. So `build_app(rpc_url="http://127.0.0.1/", token=None)` passed every check the function made, and an integrator who then served that app on a public interface (for example `uvicorn.run(host="0.0.0.0")`) had a governed door answering unauthenticated JSON-RPC to anyone who could reach the port. **Measured, not theorised: on 2026-08-24 that configuration answered `200` from the A2A dispatcher to an unauthenticated POST arriving on a non-local socket carrying a forged `Host: 127.0.0.1`.** This is an authorization bypass, so unlike most entries above it IS a security defect and is named as one. **Affected: 0.31.0 (where the A2A door first shipped) through 0.38.0, embedder path only.** `serve_a2a` refuses a non-loopback bind without auth, so bind, served and advertised always agreed there, and no `serve --a2a` deployment was ever exposed by this. 0.39.0 adds a REQUEST-TIME refusal on every network door, keyed on `scope["server"]` — the address the connection actually landed on, which the server reports and a client cannot forge — so a door with no bearer token answering on a non-local socket is refused before dispatch. The equivalent gaps in the MCP HTTP and REST doors were found in the same pass and never reached a published version: `build_http_asgi` and the REST door are both new in this release. Separately, and NOT a vulnerability: several runtime sites treated an empty-string token as a token, which built a door whose bearer check then refused every request including a correct one. That failed closed — a silent lock, not an open door — and is now consistent across all doors. **0.39.1 carries no security fix and changes nothing inside the installed package — the only in-package diff is the version string.** What it ships is the verification rail around the artifact: the `[a2a]` extra now has its own install-and-serve gate leg (installed ALONE, driven over a real socket, with a real governed call that must complete through the spine), run on every release and daily against the published artifact. Not a vulnerability, no advisory; named here so the version table stays complete. **0.40.0 carries no security fix and changes no enforcement path. It raises the Python floor to 3.12 (from 3.11); an adopter on 3.11 stays on 0.39.1, which this table still supports. Not a vulnerability, no advisory.** 0.40.1 carries no security fix: `doctor` reads the seat's roles from `pacioli_guard.api.my_roles` (guard >= 0.16.0) with frappe's `User.get_roles` as the fallback, because frappe 16.33.0 removed the latter and the doctor could no longer certify a seat on a current v16. |

Pre-1.0 means the surface can still move. Security fixes land on the current minor; there is no
long-term support branch and no backports to 0.6.x.

### The deploy road (`deploy/`), which is not a package

The road is published here and run by hand on a fresh host. **From 2026-07-17 until this release (broker 0.40.1), the published
`provision.sh` wrote the site's DB root and Administrator passwords into a step file
at mode 644 under a home it makes `o+x` for nginx, so `www-data` or any local account on that host
could read both, while the file's header claimed root-only 600.** Local read only; nothing a
network caller could reach, and neither package is involved, so there is no advisory. It is
recorded here because a password readable by the web server's account is the kind of thing an
operator should hear from us. If you built a host from the road before this release: `chmod 600
/home/frappe/s*_*.sh /home/frappe/.frappe_env`, then rotate both passwords. The road now sets 600
at write time, and its stage runner no longer passes through IPython, which journaled every input
line (passwords included) to a log under that same home.

## Upgrading

`pip install --upgrade` is not the whole step for a Frappe app, and for `pacioli-guard` 0.13.0 the
remaining two are mandatory and ordered:

```
bench --site <your-site> migrate       # 0.13.0 adds Pacioli Consent Marker.expires_at_epoch
bench --site <your-site> clear-cache   # 0.13.0 adds two doc_events keys
```

Both failures are silent in the sense that nothing logs "you skipped a step", and both fail closed.
They present differently, so knowing which you are looking at saves the debugging:

- **Migrate skipped: every governed write refuses**, with "no live consent marker for this document
  and act", whether or not you minted one. The gate asks for the new column without frappe's
  `ignore` flag, frappe re-raises the missing-column error, and the deny-biased handler around it
  reads any error as "no marker". Nothing is under-governed. Everything is stopped.
- **Cache cleared but migrate skipped**: same as above. Order does not rescue it.
- **Cache clear skipped: ledger previews stay refused** and your broker's PLAN step keeps failing
  exactly as it did before the upgrade. Frappe caches the app-hook registry, and an upgrade in place
  preserves the hook entries already cached without picking up new ones. Confirm with
  `get_hooks("doc_events")["*"]["before_gl_preview"]`, which must not be empty.

`consent_status.gate_registered` counts every denying handler — the two preview hooks since 0.14.0,
`on_trash` since 0.17.0, and `before_update_after_submit`, `on_change` and `before_discard` since
0.18.0 — so a site whose cache predates the upgrade reports `false`. (Until
0.14.0 it checked `before_submit` and `before_cancel` only, and this file kept saying so after that
stopped being true.)

For **0.17.0** the same two steps apply and the failure shape inverts: an in-place pip upgrade with
NEITHER step leaves `on_trash` out of frappe's cached hook registry (`get_hooks` reads redis's
`app_hooks`, which a worker restart does not rebuild) and **deletes stay ungated — fail open**, the
only upgrade failure in this app that does not refuse. `bench migrate` clears that cache itself and
also syncs the `delete` act into the marker's Select; skipped on its own, minting a `delete` marker
is refused by frappe's Select validation, fail closed. Read `gate_registered` after the upgrade; it
is the receipt.

For **0.18.0** it is the same again, for the edit gate. With NEITHER step the cached registry has
none of `before_update_after_submit`, `on_change` and `before_discard`, frappe never calls those
handlers, and **a gated seat's edit of a submitted document stays ungated, and a submitted document
can be cancelled through `discard`: fail open**. `bench migrate` clears the cache and syncs the
`update` act into the marker's Select. `gate_registered` counts the three new handlers.

0.18.0 also narrows what a scoped api-key credential may send, and a client that relied on the old
behaviour is refused until its grant or its request changes: a resource write whose body carries
`docstatus` 1 or 2 needs the `<DocType>.submit` or `<DocType>.cancel` method grant as well as the
verb (so a whole submitted document PUT back with its `docstatus` reads as a submit; send the
changed fields only); a create or an update whose body carries `_action` is refused, and so is any
method call whose document carries it (`frappe.client.submit`/`insert`/`save`, `savedocs`,
`run_doc_method`), because a preset "discard" switches off frappe's transition check and every
hook of that save; for every principal, a `discard` of a document whose stored docstatus is not 0
is refused (frappe's `discard()` checked the docstatus the request described, and a body that also
preset `_action` reached it); and, for a scoped api-key credential again, `frappe.client.set_value`
naming `_action` as its field is refused; and an update
whose body carries any key starting with `__`, or a `name` other than the document in the URL, is
refused.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting on this repository (Security tab, "Report a
vulnerability"). That opens a private thread visible only to the maintainer.

Please do not open a public issue for a suspected vulnerability. If private reporting is
unavailable to you, open a public issue that says only "security report, please make contact" with
no detail, and a private channel will be opened.

What helps: the version, the call you made, what you expected to be refused, and what actually
happened. A reproduction against your own bench is worth more than a description.

## What this software claims, and what it does not

The honest boundary matters more than the promise, so it is stated up front rather than discovered
later. Coverage is a **composition of two enforcement points at two altitudes**, not a single wall:

- **Credential scope** runs at `auth_hooks`. It governs **api-key** credentials, meaning requests
  authenticated with a `token` or `Basic` Authorization header. It does **not** see OAuth2 `Bearer`
  tokens, desk/cookie sessions, background jobs, the scheduler, server scripts, or the bench
  console. "Which credential is this" only exists at authentication time, which is why that gate can
  only live there.
- **Consent** runs at `doc_events` on `before_submit` and `before_cancel`, since 0.13.0 on
  `before_gl_preview` and `before_sl_preview`, and since 0.17.0 on `on_trash` (a gated seat's delete
  needs its own `delete` marker; a submit or cancel marker never spends on it), and since 0.18.0 on
  `before_update_after_submit` (a gated seat's edit of an already-submitted document needs its own
  `update` marker; an edit ERPNext itself makes inside a governed submit or cancel rides that act,
  and one submit marker therefore also covers the submitted Payment Entry or Journal Entry that the
  invoice's advances table names; a consented edit can itself cause a ledger repost, because
  ERPNext creates and submits a `Repost Accounting Ledger` when an account field on a posted
  invoice, payment or journal changes; and an edit also rides a ledger preview of a consented
  submit, which ERPNext rolls back), and since 0.18.0 on `on_change`, the one hook frappe still runs
  when a document arrives with `_action` preset to "discard": such a save by a consent-gated seat
  is refused there. The row is already written when that hook runs, so the handler rolls the
  transaction back itself before refusing (a caller such as `frappe.client.bulk_update` catches the
  error and commits), past the lock frappe puts on transaction control inside a hook; whatever the
  same transaction wrote ahead of that save is rolled back with it. If that rollback cannot run,
  the transaction's next commit is refused instead (once), which surfaces as a bare 500. A
  document's own `on_change` runs before this app's and may commit; a row committed there is not
  undone. It is a property of an act on a document, so it
  is enforced on the document, and it therefore covers paths the credential hook cannot see. The two
  preview handlers gate a rehearsal rather than a posting: ERPNext previews a ledger by performing it
  and rolling back, so previewing a submit needs the same marker as the submit and does not spend it.

**The residual, stated:** writes that skip the document lifecycle entirely. Raw SQL, and
`db_update`-style direct field writes, which ERPNext core itself performs in places (for example
when reposting a landed cost voucher). No single Frappe extension point observes those, so no gate
built on Frappe extension points can claim to. Anything asserting total coverage of a Frappe site is
claiming something the platform does not offer.

**Credential scope judges the request, and eight request shapes are stated rather than closed.**
(1) A resource update may name, in a child table, a row that belongs to ANOTHER document of the
same child DocType; frappe moves that row onto the document being updated. The gate cannot refuse
named rows, because an ordinary whole-document update sends them. (2) After a child-row update
frappe re-saves the row's parent, so a grant on a child DocType reaches its parent's save; when
that parent is submitted and the principal is consent-gated, the edit gate now asks for an
`update` marker. (3) Frappe 16.56.0 and later serve an MCP endpoint at `/api/v2/mcp`, on by
default (`System Settings.enable_mcp`). A scoped api-key credential is refused there outright,
because the call is in a JSON-RPC body the classifier does not read. An OAuth `Bearer` caller is
not scoped there, exactly as everywhere else in this section: if your agents reach the site over
OAuth, this app's credential scope does not apply to them, on that endpoint or any other.
(4) The `_action` scan reads a request's top-level keys and the documents directly under them. No
frappe core method loads a document from deeper than that, but an app's own whitelisted method that
takes one nested in its arguments (`{"args": {"doc": {...}}}`) passes the scan; for a
consent-gated principal the document-layer `on_change` check stands behind it, for a scoped
principal that is not gated nothing does. (5) On a Single DocType frappe skips its transition check
for every save, so any preset `_action` there, not only "discard", skips that save's before and on
hooks. Singles are not submittable, so no consent act is reached this way. Since frappe 16.50.0
frappe itself drops a client's `_action`, so (4) and (5) apply to earlier 16.x releases. (6) On frappe
before 16.50.0 only (stock 16.60.2 refused it in our lab), a preset `_action: "discard"` with a
`docstatus` reaches a save on several routes: a v1 resource `PUT`, `frappe.client.save`,
`frappe.client.bulk_update`, and `frappe.client.set_value` with `fieldname` as JSON. A user with
write permission and no grant sets docstatus 1 or 2 that way with no submit or cancel permission
check and no `on_submit`/`on_cancel`, so a posted document can be cancelled with its ledger left
in place. That is frappe's own; a scoped api-key credential is refused by the scan and a
consent-gated seat by `on_change`, and nothing here stops anyone else. Upgrade frappe to 16.50.0
or later. (7) A child row can be added to a SUBMITTED document without a save of that document,
so no edit gate fires: `frappe.client.bulk_update` moving a draft document's row onto it by setting
the row's `parent`, and a v1 resource `POST` of a new row on the child DocType naming it as
`parent`. Driven on our lab (frappe 16.60.2, ERPNext 16.60.0, Journal Entry): a consent-gated seat
on a desk session and, in stock frappe, any user with write on the parent DocType each turned a
posted entry's 3 rows into 4 with its 3 ledger rows unchanged. A scoped api-key credential was
refused both ways (bare `bulk_update` resolves to no grant, and the child DocType is outside its
allowlist). A fix is being designed for a later release: three attempts at listing the routes
each missed one or refused a legitimate ERPNext flow, so it will judge a row's parent at the
document instead. (8) A
v1 or v2 resource update re-saves whatever `parenttype`/`parent` its body names, on any DocType,
so a scoped credential with write on one DocType can cause a no-change save of a document of
another DocType that frappe lets it write, outside its own allowlist. For a submitted target and a
consent-gated principal the edit gate asks for an `update` marker.

**The new 0.18.0 handlers share the gating question's posture.** `before_update_after_submit` and
`on_change` read the grant the same way the submit gate does, so an unreadable grant reads as "not
gated" there too (the stated residual in `act.py`'s `_gated`). The `on_change` belt recognises
frappe's own save by the names of frappe's `_save` and `insert` frames; a frappe that renamed them
would leave the belt passing everything, where the submit gate's frame check fails closed.

**A consent-gated principal cannot submit anything Frappe QUEUES.** Some submits are handed to a
background worker rather than run inline — a Journal Entry with more than 100 rows is the common
case, and any DocType with `queue_in_background` behaves the same way. The worker runs *as the
enqueuing user*, so the gate still applies to it, but a marker is presented in an HTTP header and a
background job has no request to carry one. The act is therefore refused, always, and Frappe reports
it as a generic "Action Failed" rather than naming consent. This is fail-closed, so nothing posts
ungoverned — but if you gate a credential, do not expect it to complete a queued submission. Carrying
consent into a job is a design increment, not a setting.

**A consent marker binds a document, an act and a minter — not the document's CONTENT.** It is
minted for `(doctype, name, submit|cancel|delete|update)` by a principal other than the caller, and it is spent
once. Draft saves are not gated, so between minting and spending, the draft's amounts can change and
the same marker still authorises the submit. An operator approving a specific figure should mint the
marker *after* the draft is final, and treat the marker as approval of the document rather than of a
number. Closing this properly means pinning the document's state into the marker, which is a design
increment rather than a patch, and it is stated here rather than discovered later.

If you find a path to `docstatus` 1 or 2, to deleting a document, or to editing a submitted one,
that is **not** in the residuals above and is **not** refused for a consent-gated principal, that
is a vulnerability and we want to hear about it.

## Out of scope

- Misconfiguration that the software correctly reports. `doctor` names an open bypass out loud when
  `require_consent` is off; acting on that warning is the operator's job.
- Frappe and ERPNext defects themselves. Report those upstream. We will disclose one here when it
  changes what this software can promise, and several are already disclosed in the docs.
- Anything requiring System Manager, or requiring the attacker to already hold the credential being
  protected, unless it lets that credential exceed its own grant. Exceeding the grant is exactly the
  thing this software exists to prevent, so those reports are in scope.

## Disclosure practice

Advisories are published on this repository. Fixes ship with a CHANGELOG entry that names the defect
plainly, including defects found in our own code by our own audits, and including the ones nobody
would have noticed. A security tool that hides its own history is asking for trust it has not
earned.
