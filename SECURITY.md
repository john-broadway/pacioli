# Security

Pacioli governs access to accounting books. A defect here is a defect in someone's ledger, so this
file states plainly what is supported, what is covered, and what is not.

## Supported versions

| Package | PyPI | Supported | Notes |
|---|---|---|---|
| `pacioli-guard` | `pacioli-guard` | **0.10.1 and later** | Two published *security* defects, both fixed by upgrading. In 0.6.3 and earlier an empty resource allowlist returned permitted, granting every DocType (incorrect authorization). In 0.9.6 the consent gate let a nested cancel ride a marker that authorised a *submit*, so a caller-named pre-existing document could be cancelled with no marker of its own — fixed in 0.10.0. Separately, 0.10.0 read a marker's expiry through the process clock instead of the site's, so on any site whose `time_zone` differs from the container's every marker was born expired and no governed write could proceed — a functional defect, not a vulnerability, fixed in 0.10.1. Eight versions were ever published: 0.6.2, 0.6.3, 0.9.6, 0.10.0, 0.10.1, 0.11.0, 0.12.0 and 0.13.0. **0.9.0–0.9.5 were never published** and are not supported. 0.11.0 carries no security fix — it adds the site-wide `allow_all_doctypes` grant and corrects documentation that described a `"*"` row the grant document could never store. That mis-documentation failed *closed* (an unstorable row leaves the allowlist empty, which denies), so it is not a vulnerability and has no advisory. 0.12.0 carries no security fix and changes no enforcement path (`enforce.py`, `act.py`, `scope.py` untouched) — it adds `gate_registered` and `consent_enforced` to `consent_status` so an operator can see whether the consent gate is actually loaded. **It surfaces a deployment condition worth checking on your own install:** the consent gate rides `doc_events`, and a site whose hooks cache predates its consent support can hold `require_consent = 1` while those handlers are not registered, in which case acts are not consented even though the grant says they are. Scope rides `auth_hooks` and is unaffected, which is what makes it hard to notice. No version of guard mis-decided anything and the packaged artifact was correct on disk, so this is not a vulnerability and has no advisory — but if you rely on `require_consent`, read `consent_enforced`, and if it is false while `require_consent` is true, clear your site cache and confirm with an actual refusal. **0.13.0 is the recommended version. It carries no fix for a credential-escape defect, so 0.12.0 is not vulnerable in the sense 0.9.6 was, and it does carry four things worth knowing.** (a) The consent gate now covers ERPNext's ledger PREVIEW, which previews a posting by performing it and rolling the transaction back. Through 0.12.0 that call sat in `SAFE_METHODS` described as read-only. It was mislabelled rather than exploitable: the cascade it triggers was refused by the consent gate and the transaction rolled back, so nothing posted. The label is now accurate and the preview requires the same marker as the submit it previews. (b) A minted marker is immutable except for `burned`. Before this, anything holding write permission on `Pacioli Consent Marker` could extend a marker's expiry, repoint it at a different document, or re-stamp `minted_by` and so defeat the minter-separation check with a value it chose. That requires System Manager and is therefore out of scope by the policy below. It is recorded anyway, because those are the exact fields every other consent guarantee reads. **Deleting** a marker is still permitted at the same permission level and is not blocked. (c) The expiry is now stored as epoch seconds in `expires_at_epoch` rather than as a site-naive Datetime re-interpreted at spend time. Two ways the old representation could read a live marker as lasting LONGER than its TTL: changing `System Settings.time_zone` re-timed every marker already minted, and the UTC fallback in `_as_instant` errs later rather than earlier for a site east of UTC, while its own docstring claimed it could only ever err earlier. That fallback fires only when reading the site zone RAISES, not when the zone is merely unset, because frappe returns `Asia/Kolkata` for an unset zone. So it is narrow. In every case the direction is a longer life for a marker a human really did mint, never a marker they did not: document binding, act binding, single use and minter separation are untouched. If you minted markers with a script of your own that wrote a process-clock `datetime.now()`, measure the lifetime you are actually getting; on a site whose zone differs from the container's, ours read 17,980 seconds for a 900-second marker. (d) `_epoch` now refuses a non-finite expiry, which previously would have produced a marker that never expired. Latent only, since a `DATETIME(6)` column cannot store one. **0.13.0 has a required upgrade order, and skipping either step fails closed in a way that looks like the upgrade did nothing. See "Upgrading" below.** |
| `pacioli` (broker) | `pacioli` | **0.33.3 and later** | 0.33.2 and earlier used urllib's default opener, which re-sends `Authorization` when following a 3xx and permits plain `http`, so a redirecting endpoint could take the broker's credential to a host the registry never pinned. Fixed in 0.33.3; upgrade. **0.34.0 carries no security fix.** It adds the broker half of guard 0.13.0's preview gate: `plan_submit` accepts a `consent_token` and forwards it to the ledger preview, which is what makes a governed write completable at all on a site with `require_consent` on. With no token the request is byte-identical to 0.33.3. **Pair it with guard 0.13.0.** Guard 0.13.0 alone gates the preview while a broker at 0.33.3 has no way to present a marker, so `plan_submit` is refused and PLAN cannot complete. That is fail-closed and it is not a regression, since a consent-enforced site on guard 0.12.0 refused the preview's cascade instead, but the two halves are only useful together. **0.34.1 carries no security fix and changes no enforcement path, but it corrects what a human is shown at the consent moment, so it is worth knowing about if you approve markers by reading `pacioli mint`.** From 0.33.2 through 0.34.0 the projected-GL summary printed `debits 0.00 / credits 0.00` for every plan a real bench produced: `ledger_preview` returns its rows as lists, and the helper that summed them only understood dicts. Two effects. The size of the act was understated as zero to the person approving it. And the "no debit without a credit" check sitting beside it compared `0.0 - 0.0`, so `projected entry DOES NOT BALANCE` could never fire — an alarm that was present, documented, and unreachable. Nothing was mis-authorized: consent was still required, markers were still document- and act-bound, and the floor still enforced, which is why this is not a vulnerability and has no advisory. It is a disclosure defect, and disclosure is what a consent ceremony is for. 0.34.1 also makes an unreadable row say so (`totals unavailable ... no balance check was made`) rather than silently score zero. |

Pre-1.0 means the surface can still move. Security fixes land on the current minor; there is no
long-term support branch and no backports to 0.6.x.

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

`consent_status.gate_registered` checks `before_submit` and `before_cancel` only, so it reports true
while the preview hooks are still missing. That is not a false statement, and it will not warn you
about this either.

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
- **Consent** runs at `doc_events` on `before_submit` and `before_cancel`, and since 0.13.0 on
  `before_gl_preview` and `before_sl_preview` as well. It is a property of an act on a document, so it
  is enforced on the document, and it therefore covers paths the credential hook cannot see. The two
  preview handlers gate a rehearsal rather than a posting: ERPNext previews a ledger by performing it
  and rolling back, so previewing a submit needs the same marker as the submit and does not spend it.

**The residual, stated:** writes that skip the document lifecycle entirely. Raw SQL, and
`db_update`-style direct field writes, which ERPNext core itself performs in places (for example
when reposting a landed cost voucher). No single Frappe extension point observes those, so no gate
built on Frappe extension points can claim to. Anything asserting total coverage of a Frappe site is
claiming something the platform does not offer.

**A consent-gated principal cannot submit anything Frappe QUEUES.** Some submits are handed to a
background worker rather than run inline — a Journal Entry with more than 100 rows is the common
case, and any DocType with `queue_in_background` behaves the same way. The worker runs *as the
enqueuing user*, so the gate still applies to it, but a marker is presented in an HTTP header and a
background job has no request to carry one. The act is therefore refused, always, and Frappe reports
it as a generic "Action Failed" rather than naming consent. This is fail-closed, so nothing posts
ungoverned — but if you gate a credential, do not expect it to complete a queued submission. Carrying
consent into a job is a design increment, not a setting.

**A consent marker binds a document, an act and a minter — not the document's CONTENT.** It is
minted for `(doctype, name, submit|cancel)` by a principal other than the caller, and it is spent
once. Draft saves are not gated, so between minting and spending, the draft's amounts can change and
the same marker still authorises the submit. An operator approving a specific figure should mint the
marker *after* the draft is final, and treat the marker as approval of the document rather than of a
number. Closing this properly means pinning the document's state into the marker, which is a design
increment rather than a patch, and it is stated here rather than discovered later.

If you find a path to `docstatus` 1 or 2 that is **not** in the residual above and is **not**
refused, that is a vulnerability and we want to hear about it.

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
