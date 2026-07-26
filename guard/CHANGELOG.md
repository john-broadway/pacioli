# Changelog — Pacioli Guard

Least-privilege API capability scoping for Frappe/ERPNext. Honest pre-1.0 semver.
Distribution name `pacioli-guard`; Frappe app / import module `pacioli_guard`.

## 0.9.6 — 2026-07-26 — the code stops lying to the operator it just blocked

PATCH, and it changes no decision — every allow and every refusal is byte-identical. What changes is
what this software SAYS about itself, in the places a user actually reads.

John's standing law, stated this day: **"we dont lie ... our code must not lie."** A refusal is the
one message an operator is guaranteed to read, at the exact moment they are blocked and looking for
the way out. A false sentence there costs more than a false sentence in a README, because it sends
someone down a road that does not exist while their write is failing. Three of them shipped.

**1. A cancel refusal told the operator to get permission to SUBMIT.** `consent_verdict`'s
no-marker reason hardcoded "...before it can be submitted" for every act, though the act is a
parameter it already receives. Observed live on the public bench in this exact shape: *"requires
human consent to **cancel** a document, and no live consent marker ... before it can be
**submitted**."* It now names the act it was asked about.

**2. The remedy named a command this package does not ship.** The text said "a human mints a marker
with `pacioli mint`". That CLI lives in the SEPARATE `pacioli` broker distribution.
`pacioli-guard` installs no console script at all, and its documented deployment is a bare
`bench install-app` on a customer bench with no broker anywhere near it. Blocked operators were
being sent after a command their shell does not have. The text now names the desk UI first and
says which package ships the CLI.

**3. The denial claimed coverage this gate does not have.** It ended: *"This gate runs on every path
that reaches the document lifecycle, not only on api-key REST calls."* The second clause is true and
is the whole reason consent moved off `auth_hooks`. **The first is false** — and `act.py`'s own
docstring said so 180 lines above the sentence denying it. `run_before_save_methods` returns early on
`flags.ignore_validate` (frappe 16 `model/document.py:1399-1400`), and that return is BEFORE the
`_action == "submit"` branch at `:1405` that fires `before_submit`; ERPNext sets the flag and changes
docstatus anyway in at least two places, one of which cancels a consolidated Sales Invoice and
reverses GL. So a write can reach the document lifecycle and never reach this gate. The message now
states the coverage that is real AND names the two residuals that walk around it.

**Also fixed, on the public README.** It called Guard "independently useful for any credential on a
site." Credential scope sees **api-key** credentials only. This is the same overclaim caught in the
guard README on 2026-07-26 and fixed there while this instance survived another day — which is why
the fix this time is a TEST, not a note: `scripts/tests/test_copy_does_not_overclaim.py` scans every
public copy surface for the absolute phrasings that have actually shipped, and pins itself against
those real historical sentences so it cannot become a guard that never fires. It also pins that the
TRUE qualified phrasing ("every **api-key** path") keeps passing, so it pushes writers toward
accuracy rather than vagueness.

Tests **432 → 438** (guard) and **29 → 32** (repo). No behavior change: the gate decides exactly what
0.9.5 decided.

## 0.9.5 — 2026-07-26 — a cancel is not an insert, and 0.9.4 took UNDO out

PATCH. Closes a **fail-closed regression 0.9.4 introduced on the CANCEL path**, found by pointing a
third adversarial review at that fix. Nothing in the 0.9.x line was ever published.

0.9.4 required `flags.in_insert` before a cascaded act could ride the enclosing act's consent. That
is the right question for a submit and an **unanswerable** one for a cancel. `in_insert` is written
in exactly one place in frappe 16 — `Document.insert`, set at `model/document.py:478` and cleared at
`:482` (and again `:499`/`:507`). `Document._cancel` (`:1324-1326`) is `docstatus = 2` followed by
`save()` → `_save()`, which never sets it; and a document being cancelled has a name and is not
`__islocal`, so `_save` never delegates to `insert()` (`:571-572`). **The flag is therefore False at
every `before_cancel` frappe is capable of producing**, and the ride condition could not be satisfied
there by any input.

Effect: a cascaded cancel could only ever fall through to the marker check and be refused, for a
document name no human could have minted a marker against. Fails CLOSED, so never an escape.

**LATENT, not active — measured rather than assumed.** This entry first claimed the regression broke
UNDO on a live bench. **The bench said otherwise and the claim is withdrawn.** A governed Sales
Invoice CANCEL on 0.9.4, run against the public bench with a valid marker
(`deploy/bench/live-proof-095-cancel.py --red`, 2026-07-26), **succeeded**: docstatus 2, no refusal.
ERPNext reverses that ledger through `make_reverse_gl_entries` and `db_set`-style writes which skip
the document lifecycle entirely — the residual this app already publishes — so nothing reached
`before_cancel` on a second document and the dead ride had nothing to deny. The defect is real and
structural; it bites the first time any code, ERPNext's or an adopter's, cancels a second document
through the lifecycle inside a governed cancel. The driver reports LATENT vs ACTIVE on its own and
fails the run on an uninterpretable third outcome, so it cannot pass vacuously.

**The fix splits the predicate by act** (`_may_ride`) rather than loosening it. Submit still requires
`in_insert`, so 0.9.4's consent-amplification fix is fully intact and tested. Cancel restores the
0.9.3 predicate: an enclosing act that established its own consent licenses the cancels nested under
it.

**Stated residual, not a closed hole.** On the cancel path a cancel of a PRE-EXISTING document that a
caller could steer ERPNext into performing inside a governed act would ride. No such lever is known —
the amplification vector that motivated 0.9.4 reaches `get_doc(<name>).submit()`, not `.cancel()` —
but "none known" is not "none", and narrowing it needs a signal that a cascaded cancel is a
*consequence* of the enclosing document. Frappe's own link-cancel machinery is the candidate and has
not been walked, so nothing is asserted about it.

**A second wrong claim in this file's own comments is corrected.** 0.9.4's entry said it had fixed
the "`Document.flags` is never persisted and never crosses a request" claim; the corrected text was
written into the CHANGELOG but **never into `act.py`**, where the false sentence was still sitting
verbatim. Corrected now, on read bytes: `flags` is not in `UNPICKLABLE_KEYS`
(`model/base_document.py:1591` holds only `_parent_doc` and the cached properties) and
`get_cached_doc` pickles whole documents into redis for an hour (`model/document.py:2242-2260`). What
actually holds is (1) that cache is written immediately after a fresh `get_doc` on a miss, strictly
before any handler has run, and (2) — the load-bearing one — a stamp alone licenses nothing, because
riding also requires a LIVE `frappe.model.document` write frame holding that document on the stack,
and a flag that survived into redis cannot manufacture a frame.

**Why nothing caught the regression.** `test_cancel_cascades_the_same_way` drove the cascaded
document through the `insert` frame double with `in_insert` defaulted True, a combination frappe
cannot produce. The double could model the submit cascade faithfully and the cancel cascade only
unfaithfully, so it stayed green on a path that was broken. Sixth time in three days that a test
double's missing dimension was exactly where the defect lived. That test is now modelled as frappe
produces it, and all three cancel-path tests were proven RED against the 0.9.4 predicate.

Tests **427 → 432**.

**Proof status, stated exactly.** This fix is **unit-proven, not live-proven, and cannot be
live-proven** — a latent defect has no live failure to turn green. The three cancel-path tests were
each proven RED against the 0.9.4 predicate before being believed. 0.9.5 was then deployed to the
public bench and its version read back, and **three no-regression checks** ran against it: a cancel
still lands, a governed submit still lands with its cascade riding, and an unconsented cancel is
still refused. Those establish that 0.9.5 broke nothing. They do not establish that it fixed
anything. The honest phrase is **live-deployed, unit-proven, no regression observed** — an earlier
draft of this project's notes called it "live-verified", which was false and is withdrawn.

## 0.9.4 — 2026-07-26 — one marker licenses what the act CREATED, not what the caller named

PATCH. Closes **consent amplification** in 0.9.3, found by pointing a second adversarial review at
that fix. Neither 0.9.3 nor its predecessors were ever published.

Riding on "an enclosing governed act is in progress" licenses more than the human approved. The
failure that motivated the exception was a `Payment Ledger Entry` that **did not exist** until the
submit ran — which is exactly why no human could mint a marker for it. But the same predicate also
licensed **pre-existing drafts the caller named in the request body**, which a human could have been
asked to approve.

Reachable in the ordinary slice-one flow, not an exotic one. Submitting a Sales Invoice with
`update_stock` carries the item row's `serial_and_batch_bundle` LINK straight out of the request
body (`stock_controller.py:1069`), and `serial_batch_bundle.py:441-449` then does
`frappe.get_doc("Serial and Batch Bundle", <that name>)` and submits it. One marker for one invoice,
and stock-ledger-moving documents the human never saw reach docstatus 1. The same shape fires from
`POSInvoice.on_submit`.

**The fix uses frappe's own signal rather than new state.** `flags.in_insert` is set at
`model/document.py:478`, immediately before `run_before_save_methods()` at `:479` fires
`before_submit`, and cleared at `:482`. A document CREATED by the act carries it; a document loaded
by name and submitted does not. Verified on real bytes for the motivating case:
`create_payment_ledger_entry` builds the PLE with `frappe.get_doc(<dict>)`, so it has no name,
`_save` delegates to `insert()` (`:571-572`), and `in_insert` is true at its `before_submit`. Riding
now requires it in addition to the enclosing act's established consent.

**Three claims in this file's own comments were wrong and are corrected rather than deleted:**
- It cited `stock_reconciliation.py:227` as a live instance of an ungated `validate` submitting a
  bundle. That call sits behind `if save:`, `save` defaults False, and nothing in the shipped tree
  passes True — **it does not fire in ERPNext 16.** The class is real and the other two instances
  are verified; this one was written down without being checked.
- It claimed `Document.flags` is "never persisted and never crosses a request". Frappe gives no such
  guarantee: `flags` is not in `UNPICKLABLE_KEYS` and `get_cached_doc` pickles whole documents into
  redis. What actually holds is narrower and now stated: the cache is written immediately after a
  fresh `get_doc`, strictly before any stamp exists, and the document cache is cleared at the end of
  the act — so no flagged object is ever cached.
- It published two residuals when there are three. `run_before_save_methods` returns early on
  `flags.ignore_validate` (`:1399-1400`) BEFORE `before_submit` runs, and ERPNext sets it and changes
  docstatus anyway in at least two places, one of which cancels a consolidated Sales Invoice and
  reverses GL. Not remotely settable, so a coverage limit rather than an attacker lever — but real,
  and it was unpublished.

Tests **423 → 427**. The double gained `in_insert`, the dimension it lacked.

## 0.9.3 — 2026-07-26 — "a different document" was never the question; "already governed" is

PATCH, and it closes **three consent bypasses that 0.9.2 introduced**, found by pointing a second
adversarial review at the fix itself rather than at the code it fixed. Neither 0.9.2 nor 0.9.1 was
ever published. The suite was green at 415 with all three live, and the live bench proof passed,
because both only tested the two vectors already known.

**The defect was the PREDICATE, not the frame shape.** 0.9.2 asked "is any enclosing frame writing a
DIFFERENT document" and treated yes as "this act is a consequence of a governed one". Those are not
the same question, because **only `before_submit` and `before_cancel` are gated**. A draft `save()`,
and an insert of a non-submittable DocType, are never gated and never need a marker — yet each puts a
`frappe.model.document` write frame holding a different document on the stack. Anything submitted
beneath one rode for free, with no marker existing anywhere on the site and no human approving
anything at any level.

Three instances, all verified against ERPNext 16 source, all moving the ledger:

- **`Subscription`** carries no `is_submittable`, so inserting it is never gated. `Document.insert`
  runs `after_insert` inside its own frame (frappe `model/document.py:498`), and
  `subscription.py:529` submits a Sales Invoice for **every elapsed billing period**. One
  `POST /api/resource/Subscription` with a back-dated `start_date` and `submit_invoice` set yields N
  submitted invoices, N sets of GL entries, and a receivable — consent count zero.
- **`Item`** is not submittable either. `item.py:192` submits a Stock Entry from `after_insert`, at a
  caller-supplied `opening_stock` and `valuation_rate`: Stock Ledger Entries plus GL at a value the
  caller chose.
- A **`Stock Reconciliation` draft SAVE** submits a Serial and Batch Bundle
  (`stock_reconciliation.py:227`) from `validate` — an ungated action licensing a submit.

**The fix.** Keep the frame walk (no pop point, no per-request state, fail-closed on rename). Change
what a matching frame must prove: the enclosing document must have **established consent for its own
act** — spent a marker, or itself legitimately ridden one. Stamped on the document object as
`pacioli_consent_established` (a `Document.flags` entry, per-object, never persisted, so it cannot
leak into a later act) and propagated when an act rides, which keeps the chain of custody across a
cascade of a cascade. Stamped only AFTER the marker is actually spent, so a refused act licenses
nothing. An ungoverned outer write now proves nothing, which is the property the docstring claimed
from the start.

**Also fixed: `other is not doc` was OBJECT identity.** `frappe.get_doc` returns a fresh instance on
every call and `load_doc_before_save` already builds a second object for the same document inside
`_save`, so a controller doing `frappe.get_doc(self.doctype, self.name).submit()` presented a
different object for the same document and read as a cascade. Now compared on `(doctype, name)`,
falling back to object identity when either name is missing, because two unsaved documents both
named `None` are not the same document.

**Tests 415 → 423.** Five of the eight new ones fail against 0.9.2; the other three are regression
guards that must pass either way. The double gained `flags`, the dimension it lacked — with no way to
express "this enclosing document never established consent", no test could have failed on it. Fourth
time in three days that the fake's missing dimension was exactly where the bug lived.

## 0.9.2 — 2026-07-26 — the cascade signal stops counting frames and starts naming the document

PATCH, and it closes a **consent bypass that 0.9.1 introduced**. Found by an adversarial review
before any of this reached PyPI, which is the whole reason the review runs before the push and not
after. 0.9.1 was never published; it was live on our own bench, so this is written up as a defect
rather than filed as an advisory.

**The defect.** 0.9.1 decided "is this act a consequence of another act" by COUNTING stack frames
named `_save` or `insert`, treating two or more as nested. A bare code name is ambiguous, and frappe
16 puts two write-named frames on the stack for two ordinary top-level paths:

- `frappe.client.insert` (`client.py:208`, whitelisted POST/PUT) calls `insert_doc` (`:511`) which
  calls `frappe.get_doc(doc).insert()` (`:527`), reaching `Document.insert`. **Two frames named
  `insert`**, and a body carrying `docstatus: 1` inserts a document that is already submitted.
- `Document._save` (`:571-572`) delegates to `self.insert()` for any document without a name, so
  submitting a **new** document is `_save` then `insert` for one document.

Both counted as depth 2, so `_require_consent` returned before checking anything: no marker demanded,
none burned, and the ledger moved. The first vector is reachable over HTTP with exactly the grant
this project tells an operator to give the broker, which made it the 2026-07-25 bypass reopened one
altitude down.

The published claim that the signal fails closed was true of only one failure mode. Rename-drift was
analysed; name-collision was not, and that one fails open.

**The fix.** The count was a proxy, and the proxy had collisions. The real question is not how many
write frames are on the stack but whether an enclosing frame is writing a **different document** —
which is what "my act is a consequence of another act" means. `_writing_document` recognises a frame
only when its code name is `_save`/`insert` **and** its module is `frappe.model.document`, then reads
the document out of that frame's `self`. Same document across two frames is one act. A same-named
function in another module is not a document write at all. Failure direction is unchanged and still
closed: if frappe renames or moves those internals, nothing is recognised, every act reads as
top-level, and cascades are refused loudly.

**Tests: 411 → 415**, and the four new ones failed against 0.9.1 before passing here. The double was
rebuilt to carry the two dimensions it lacked — the module a write frame belongs to, and which
document it is writing — because the old double could express neither, and therefore could not fail
on either. Ask what dimension a fake lacks; that is where the bug will be. Third time that lesson has
paid out in three days.

## 0.9.1 — 2026-07-26 — the framework's own cascade rides the act it is a consequence of

PATCH (0.9.0 as shipped could not complete a governed write; this makes the move actually work.
guard 411 + 51 subtests, +9). **Found by the live run, after 402 green unit tests.**

0.9.0 was deployed to the live bench and rolled back the same hour. What the run showed: with the
gate ON and a valid marker present, a governed submit still failed with HTTP 403. The Sales Invoice's
own consent check PASSED — and then ERPNext's own accounting machinery, inside that same submit,
created and submitted `Payment Ledger Entry` documents, and the `doc_events["*"]` handler demanded
consent for those too:

```
Refused for Payment Ledger Entry ruq5vig9c2
Refused for Payment Ledger Entry rum27r53ki
```

**A human cannot mint a marker for that document: its name does not exist until the submit is already
running.** Consent binds to the act a human authorised, and the framework's consequences of that act
are part of it. So consent is now required for the OUTERMOST act only; writes nested inside it ride it.

**The mechanism is nesting depth, and the obvious alternative was rejected on purpose.** A per-request
stack with an explicit close needs a pop point, and the only candidate is `on_submit` — which is
exactly where ERPNext creates its ledger entries. Hook ordering inside one event would decide whether
the pop ran before or after that cascade, and a pop that ran too early would let a BULK submit's
documents 2..N ride document 1's marker. That is fail-OPEN in the one place this app cannot afford it.
Depth needs no pairing, keeps no per-request state that can leak into the next act, and ignores hook
order. Its own failure direction is stated in the code: if frappe renames those internals, every act
reads as top-level and cascaded documents are REFUSED — loud, not silently permissive.

**One claim per act, not per cascaded document** — the marker is spent once for the whole cascade,
which a test now locks alongside the bulk-submit case.

**What the live run also proved, and it is worth keeping.** On 0.7.0 the gated seat submitted
`ACC-SINV-2026-00010` with no request, no api-key header and no marker, both GL rows in its own name
(1310 debit 1450.00 / 4110 credit 1450.00). On 0.9.0 the identical submit was refused, and receipt 4
over REST was refused too, now at the document layer. The atomic claim burned inside the transaction
and un-burned when the transaction rolled back. The altitude move is right; it was incomplete.

**Why the unit tests could not catch it, stated so it is not repeated.** `FakeDoc` is one document.
Nothing modelled "submitting A causes the framework to submit B and C". 402 green, and the defect sat
in the one dimension the double did not have — the same shape as the 2026-07-25 bypass, which lived at
a join rather than inside a component.

## 0.9.0 — 2026-07-26 — consent moves to the document, where the act is legible

MINOR (deny-more: the consent gate now covers paths it previously could not reach; no grant widened;
guard 402 + 51 subtests, +28 net after deleting 17 tests whose properties moved). **A door admits; it
does not decide.**

**The move.** Consent is a property of an ACT ON A DOCUMENT. It was enforced in `auth_hooks`, which
is the wrong altitude twice over. *Coverage:* `check_scope` only fires for a credential carrying an
api-key `Authorization` header, so OAuth `Bearer`, desk/cookie sessions, background jobs, the
scheduler, server scripts and the bench console reached the ledger without meeting it — consent
placed there inherited that boundary exactly, which means the fix for the 2026-07-25 transport escape
was itself sitting at the transport layer. *Legibility:* at that altitude "is this a submit, and of
what document" has to be inferred from a request shape, which is why the classifier carries `?cmd=`
dominance, five body-carrying RPC rewrites, a `savedocs` action map, three REST mounts and a
disclosed residual on raw `docstatus` writes. On the document it is `doc.doctype`, `doc.name` and the
event name. No inference, so no residual.

Now registered as `doc_events["*"]` → `before_submit` / `before_cancel` (`pacioli_guard/act.py`).
Every path that reaches `docstatus` 1/2 through the ORM passes it: REST, `run_doc_method`,
`frappe.client.submit`, the desk Save/Submit endpoint, bulk submit, a raw docstatus field write
followed by a save, a background job, a server script, the bench console.

**Verified against frappe 16 source before writing it, not after** (`model/document.py`): `_submit()`
sets `docstatus = 1` then `save()` → `_save()` (`:552`) → `run_before_save_methods()` (`:587`) →
`run_method("before_submit")` (`:1407`); `Document.hook` (`:1606`) composes each app's handler and the
composed runner uses `try/finally`, **not** `try/except`, so a `frappe.throw` aborts the save.
`before_submit` has exactly two call sites, `:479` (insert) and `:587` (save), and `set_new_name` runs
at insert+44 before `run_before_save_methods` at insert+49, so `doc.name` exists even for an
insert-as-submitted.

**There is deliberately no consent check left in `auth_hooks`.** Two gates cannot both hold a
single-use marker: whichever ran first would spend it and the second would correctly refuse a burned
one. One act, one claim, one place. A test asserts by source inspection that `check_scope` no longer
references `consent_verdict` or `_claim_consent`, so re-adding one fails loudly.

**Inert for anyone who has not opted in.** A `"*"` handler runs on every submit on the site, including
a human clicking Submit and ERPNext's own internal submits, so it exits after one grant lookup for
any actor without `require_consent` — no marker read, no claim. Registered on the wildcard rather than
a doctype list on purpose: the gate is keyed on the acting principal's grant, so a governed seat
cannot slip through by touching a doctype nobody enumerated.

**STATED RESIDUAL — the GATING question fails open, and only that question.** If the grant read
raises, the actor reads as not-gated. A `"*"` hook that failed closed on a transient DB error would
refuse every submit on a site that never opted into this app. Once gating is established, every
consent failure denies. Consequence said plainly: an actor who can break the grant read can skip this
gate, and on api-key paths the credential floor still applies to them.

**`enforce_workflow` has the identical misplacement and did NOT move.** It cannot yet:
`frappe.model.workflow.apply_workflow` (`model/workflow.py:120`) sets no flag on the document that a
handler could read, so the document layer cannot tell a sanctioned workflow transition from a direct
submit — which is the whole distinction that gate makes. Named so nobody concludes it was covered.

**Tests: 17 deleted, and every property they proved re-proven at the new layer first.** Three existed
only in the old location (a forged token, a marker with no recorded minter, an unidentifiable
document) and two were backward-compatibility properties (a grant predating the `require_consent`
column must read as OFF; acts that are not docstatus moves are never gated — now structural, since
only two events are registered). All written into `tests/test_act.py` **before** the old ones were
removed. One of the deleted tests would have passed against the removed gate for the wrong reason,
which is exactly why they were mapped one by one instead of deleted as a block.

**Copy corrected across every surface, not one.** `README.md` still carried "governs **every**
credential on the site" at a SECOND location after the first was fixed earlier the same day — the
lesson being that one grep is not a sweep. README, `guard/README.md` and `pacioli/CLAUDE.md` now state
the two-hook split, and the broker's `doctor` says the stronger guarantee out loud.

## 0.8.0 — 2026-07-26 — the floor stops trusting its own grant, and stops overstating its reach

MINOR (deny-more-only; one semantic changed, six findings closed; guard 390 + 51 subtests, +36).
Found by auditing our own floor for the *shape* of the 2026-07-25 bypass rather than for bugs: every
finding sits at a **join between two components each confident from its own side**, which is where
that bypass hid too. Full audit, including what is NOT fixed here: `docs/plans/2026-07-26-floor-audit.md`.

**F1 — an empty resource allowlist granted every DocType on the site.** The two child tables read as
parallel allowlists and behaved oppositely: an empty `methods` grants nothing (`any()` over an empty
set), an empty `resource_doctypes` granted *everything*, verb-narrowed only. Since `allow_resource`
defaults to 0 and our own scripts document the gesture as "tick the master Check or every resource
call refuses", an operator who ticked it and saved before filling the table held full resource CRUD
across the site, including over this app's own control DocTypes. **Empty now denies.** Site-wide
access stays expressible because it is a legitimate thing to want, but it must be *said*: one
literal `"*"` row (`RESOURCE_DOCTYPE_WILDCARD`), visible in the grant doc where an auditor sees it.
Spelling borrowed from frappe's own `doc_events["*"]` rather than a bespoke sentinel. No seat in our
estate was affected — every provisioning script appends explicit rows — so this is deny-more only.

**F2 — the guard did not defend its own control plane.** `_UNGRANTABLE_METHOD_DOCTYPES` was
consulted only on the `method` branch; the `resource` branch had no hard-deny at all. So
`API Key Scope` and `Pacioli Consent Marker` were reachable, and worse, *grantable* — an operator
could hand them out by listing them. A credential that can write its own grant is not scoped: it can
widen `methods`, clear the allowlist, or simply untick `require_consent`, and the grant doc is
trivially addressable because `API Key Scope` autonames `field:user`. A credential that can insert a
consent marker mints its own permission. Frappe's role permissions already restrict both to System
Manager, but that is a role wall, and our own bench notes record the standing pressure to hand an
agent seat a manager role to make something else green. Both DocTypes and both child tables are now
**ungrantable on both branches**, checked before the grant, case- and accent-folded, and the `"*"`
row does not reach them.

**F3 — `minted_by` was self-reported, so minter-separation was conventional.** `consent_verdict`
refuses a self-minted marker, which is the property that closed the 0.7.0 bypass — but nothing
established the field it reads. `minted_by` is `read_only` on the DocType, which is a *form*
property and not a server-side wall, and the controller was `pass`, so the stored value was whatever
the creator supplied (our own mint script passes `minted_by="Administrator"` as a parameter: true in
our estate, true by convention rather than because the server established it). `before_insert` now
binds it to `frappe.session.user`, **overwriting** anything supplied — never fill-when-blank, which
would let a credential name any other principal as its minter and satisfy the separation check with
a string it chose. The property is now mechanical instead of asserted.

**Coverage stated, not implied (README + broker/SPEC.md).** The README said this app "guards every
credential on the site." It guards every **api-key** credential. OAuth2 `Bearer`, desk/cookie
sessions, background jobs, the scheduler, server scripts and the bench console reach the books
without passing it, and consent inherits that same boundary because it sits at the same gate.
Neither instrument sees a raw SQL write or a `db_update` that skips the document lifecycle, which
ERPNext core itself does when reposting (`landed_cost_voucher.py`). No single frappe extension point
covers all of it, so the README now publishes coverage as a composition with a stated residual.

**F4 — single-use consent was a read-then-write, so it raced.** `consent_verdict` READ `burned`, and
a separate `set_value` wrote it. Two requests presenting one marker in the same instant could both
pass the verdict before either write landed, and single-use is the entire anti-replay property. The
spend is now the check: `_claim_consent` issues a conditional
`UPDATE ... SET burned = 1 WHERE name = %s AND burned = 0` and the database is the arbiter, since the
row is addressed by primary key and exactly one statement can match it while unburned. **Losing the
claim denies**, and it **fails closed**: if the statement raises, or the driver reports no
affected-row count we can read, the claim is lost and the request is refused. `frappe.db._cursor.rowcount`
is the only affected-row signal frappe exposes (no public accessor; `database/database.py:149` in
frappe 16), so it is read through `getattr` chains where any surprise reads as a lost claim. The cost
is stated plainly: a driver that never reports `rowcount` refuses every governed write rather than
silently permitting replays, which is the correct direction to fail in.

**F6 — a fail-open inside a fail-closed file.** `check_scope` used to `return` when
`frappe.local.request` was absent, skipping the scope allowlist, the workflow gate and the consent
gate — after the kill switch and rate limit had already run and passed. Almost certainly unreachable,
since a scope only exists there because an Authorization header was readable. It is now a denial with
its own audit reason rather than an inference, and tests lock the ordering (the kill switch still wins)
and the no-op path for unscoped credentials (which must NOT become a 403 for every ordinary key).

**F5 — the `check_scope` docstring understated its own deny-list**, claiming `Bulk Update` was the
only hard-denied doctype-part with the rest "un-audited", when the 2026-07-10 ruling closed four more.
A stale security docstring is how a reviewer concludes a gate is thinner or thicker than it is. It now
names both sets, both branches, the fold, the resource-branch semantics, and the one *disclosed*
residual (`Payment Reconciliation` stays grantable because the broker's reconcile needs it and its
hostile use is byte-identical to the legitimate one).

**The floor now reports its own width.** `pacioli_guard.api.consent_status` gained `resource_posture`
(`off` / `denies_all` / `all_doctypes` / `narrow` / `unknown`) so the broker's `doctor` warns when a
grant carries a wildcard row, and when `allow_resource` is on with an empty allowlist (which now
denies, and usually means a half-finished grant). **Posture, never contents** — no DocType names are
returned, because one seat's allowlist is not another seat's business. Deny-biased and isolated: an
unreadable posture reports `unknown`, never "fine", and cannot break the consent report it rides with.
That endpoint also gained its first behaviour tests, including one asserting it never leaks a name.

**Still open, and named rather than implied.** The act-level gates (consent, and `enforce_workflow`,
which has the identical misplacement) still live at `auth_hooks`, so they govern api-key transport
only. Moving them to `doc_events` on `before_submit`/`before_cancel` is the standing next move and the
reasoning, verified against frappe 16 call sites, is in the audit doc. `_claim_consent` relocates into
that design unchanged — it is the same conditional statement, run inside the document's transaction.

## 0.7.0 — 2026-07-25 — consent at the floor: possession of the key stops being permission to post

MINOR (a new opt-in gate; deny-more-only, off by default, no existing grant widened or narrowed;
guard 351 + 50 subtests, +54). Found by running the demo against the live public bench, not by
reading the code — and then two further holes found by a second lens reading the fix, same day,
before any of it shipped.

**The gap.** Pacioli's README carried a hard precondition: scope the broker's own ERPNext
credential to exactly the calls it makes, or anything holding that credential bypasses
PLAN/CONSENT/PROVE. The bench proved that scoping it correctly is not sufficient. The broker must
be allowed to submit invoices, so its credential is allowed to submit invoices, so a direct
`run_doc_method` call with that credential submitted a Sales Invoice with no plan, no marker and no
receipt. Verified against the ledger: docstatus 1, two real GL entries. The guard behaved exactly
as designed and the allowlist was exactly right — no allowlist can close this, because the call
being made is the call the broker is for. Possession of the key was permission to post.

**The gate.** `API Key Scope.require_consent` (Check, default 0). When on, a docstatus-changing
call is refused unless it carries a live, unburned `Pacioli Consent Marker` minted for that exact
document, presented in the `X-Pacioli-Consent` header — and the marker is spent on use.

- **`Pacioli Consent Marker` DocType** — `ref_doctype` + `ref_docname` (the one document consented
  to), `ref_action` (the one act), `token_hash` (SHA-256; the token itself is shown once to a human
  on the broker host and never stored here), `expires_at`, `burned`, `minted_by`.
- **Consent binds to the ACT, not only the document.** `ref_action` is required (`submit` or
  `cancel`) and `docstatus_action` resolves which move a call is attempting, reusing the same
  constants as `is_docstatus_changing` so the two cannot drift. Cancel reverses GL entries: a human
  who consented to posting an invoice did not consent to reversing it, and both are docstatus moves
  on the same document — so the document binding alone would have let a submit marker spend on a
  cancel. An act that cannot be read binds to nothing and refuses.
- **A credential cannot mint its own consent — enforced, not merely documented.** `minted_by` was
  being recorded and never read, which left the separation the whole gate rests on standing on two
  accidents: the marker DocType requiring System Manager to create, and the broker's seat happening
  not to hold that role. Any site running its broker credential as System Manager (common) would
  have had a gate that authorised itself. The floor now compares the marker's minter against the
  acting principal and refuses a match, refuses a marker with no recorded minter, and refuses when
  the caller cannot be identified. Unprovable separation is not separation.
- **`consent_verdict` / `consent_token_hash` / `docstatus_target_docname`** in the pure core —
  unit-testable with no bench, like every other security decision in this app. Deny-biased
  throughout: binding and liveness are judged BEFORE the token is compared, the comparison is
  constant-time, and an unreadable expiry reads as expired.
- **`docstatus_target_docname` is deny-biased on ambiguity**, mirroring `_run_doc_method_doctype`:
  it collects the document name from every source (path, `dn`, `name`, doc body) and resolves only
  when they AGREE. Naming two documents is the spoof shape — a marker minted for a harmless
  invoice presented alongside a body naming a different one.
- **Off by default, and absence reads as off.** A grant loaded before `bench migrate` added the
  column has no attribute at all; the pure core reads that as off. A gate that switched itself on
  during an upgrade would start refusing every submit a live broker makes — the lesson `enabled`,
  `rate_limit_per_minute` and `enforce_workflow` each learned before it.
- **CONTAIN order is now kill → rate → scope → workflow → consent.**

**Honest limits, named rather than papered over.** (1) Burning a marker is a read-then-write, not
an atomic compare-and-swap, so two requests racing the same marker in the same instant could both
pass before either burn lands; the marker is already bound to one document and one short TTL, but
the window is real and wants a conditional `UPDATE ... WHERE burned = 0`. (2) The gate is only as
good as who can mint. The floor now *enforces* that the minter is a different principal than the
credential it authorises, but it cannot vouch for who that other principal is — a site that lets
the same human hand both mint and drive the agent has a procedural gap the software cannot see.
(3) Consent state now
lives on the books box, which the broker host previously held alone; the bench only ever verifies
a hash it cannot forge into a token, and the decision still happens off-box, but it is a genuine
change in where state sits.

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
