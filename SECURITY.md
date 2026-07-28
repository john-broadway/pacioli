# Security

Pacioli governs access to accounting books. A defect here is a defect in someone's ledger, so this
file states plainly what is supported, what is covered, and what is not.

## Supported versions

| Package | PyPI | Supported | Notes |
|---|---|---|---|
| `pacioli-guard` | `pacioli-guard` | **0.10.1 and later** | Two published *security* defects, both fixed by upgrading. In 0.6.3 and earlier an empty resource allowlist returned permitted, granting every DocType (incorrect authorization). In 0.9.6 the consent gate let a nested cancel ride a marker that authorised a *submit*, so a caller-named pre-existing document could be cancelled with no marker of its own — fixed in 0.10.0. Separately, 0.10.0 read a marker's expiry through the process clock instead of the site's, so on any site whose `time_zone` differs from the container's every marker was born expired and no governed write could proceed — a functional defect, not a vulnerability, fixed in 0.10.1. Five versions were ever published: 0.6.2, 0.6.3, 0.9.6, 0.10.0 and 0.10.1. **0.9.0–0.9.5 were never published** and are not supported. |
| `pacioli` (broker) | `pacioli` | **0.33.3 and later** | 0.33.2 and earlier used urllib's default opener, which re-sends `Authorization` when following a 3xx and permits plain `http`, so a redirecting endpoint could take the broker's credential to a host the registry never pinned. Fixed in 0.33.3; upgrade. |

Pre-1.0 means the surface can still move. Security fixes land on the current minor; there is no
long-term support branch and no backports to 0.6.x.

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
- **Consent** runs at `doc_events` on `before_submit` and `before_cancel`. It is a property of an
  act on a document, so it is enforced on the document, and it therefore covers paths the credential
  hook cannot see.

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
