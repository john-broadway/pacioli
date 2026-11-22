# Verify Pacioli. Do not trust it.

Pacioli stands between an AI agent and a company's books. That is a large ask, so the rule is the
opposite of "trust us": **every claim on this project should be checkable by you, against the
artifacts and against your own ERPNext, without our word for any of it.**

Each claim below is paired with the command that proves it. Run them. If one does not do what it
says, that is a bug: [open an issue](https://github.com/john-broadway/pacioli/issues).

The wider point outlives this project. A floor you can drive from outside, a receipt chain you can
verify, a wheel you can rebuild from its tag: you can demand all of it from any tool that touches
your ledger. Verifiability is the floor, not a favour.

> Most proofs here need only a clone of this repo and Python 3.12+. The ones marked **[your site]**
> need an ERPNext with `pacioli-guard` installed and a consent-gated seat on it. Nothing in this page
> needs our bench.

---

## 1. The consent gate sees every door into the books **[your site]**

**Claim.** With `require_consent=1` on a seat's API Key Scope, the floor refuses that seat's submit
on every door a write can reach the ledger by: REST v1 and v2, `frappe.client.submit`, OAuth bearer,
a desk cookie session, the desk's own save-and-submit (the HTTP half), bench python and a background
job (the bench half). The gate is
`doc_events["*"]` at the document lifecycle, so it keys on the executing user, not the HTTP header.

**Prove it.** One draft of the doctype, one gated seat, and the kit. On `--check` it writes no
document; if you give it a desk login, frappe records a session row and that user's `last_login`,
and the kit says so.

```bash
export SITE=https://your-erpnext.example
export SEAT_KEY=api_key:api_secret          # the consent-gated seat
export DRAFT=ACC-SINV-2026-00001            # one docstatus-0 document the seat may act on
python3 verify/walk_the_doors.py --check    # reach, authenticate, read the draft; exit 0 = ready
python3 verify/walk_the_doors.py            # the walk; the table lands in ./walk-result.tsv
```

Add `SEAT_BEARER`, `SEAT_USER`/`SEAT_PASS` for the OAuth and cookie doors, and a human's
`CONTROL_KEY` or `CONTROL_USER`/`CONTROL_PASS` with `CONTROL_DRAFTS` so each door is first shown to
work. The seat spends ONE draft for every door: the floor holding means the draft stays a draft. If
any door submits it, the seat column stops at that row and the kit exits 4.

What to expect, row by row: every seat write row `SEEN:CONSENT`, except `rest-v1 resource PUT
docstatus`, which the scope floor refuses first (`SEEN:SCOPE`). Every control row `PASSED` with GL
rows. Exit 0.

The in-process doors run on the bench host, as the bench user, from `<bench>/sites`:

```bash
WALK_SITE=your.site WALK_SEAT=seat@your.site WALK_TEMPLATE=ACC-SINV-2026-00001 \
  ../env/bin/python /path/to/verify/walk_the_doors.py bench --check
```

Expect `bench python doc.submit` and `background job enqueue_doc now` to read `SEEN:CONSENT` for
the seat and `PASSED` for Administrator, `flags.ignore_validate + submit` to read `SEEN:CONSENT`
(the kit records the verdict, not the mechanism; on our lab the refusal came from the consequence
ledger entry's own insert hook), and the two rows in section 3 to read `PASSED-NO-GL`.

**Our run, dated.** 2026-09-26 on our own bench (frappe 16.35.0 / erpnext 16.36.0 / guard 0.16.0):
42 rows, every door, 0 fixture rows in the final table. The gated seat read `SEEN:CONSENT` on all
nine lifecycle doors. A human with roles and no grant passed 10 of 10. The same seat with consent
OFF passed 8 and was refused twice, both by scope with the api key as the credential. Your numbers
are the ones that matter.

**Not driven by the kit, said plainly.** The server-script API door and the System Console need site
posture changes an outsider's kit should not make (our lab drove both: the API server script read
`SEEN:CONSENT` for the seat; the Console's Python type submitted freely as a System Manager, its SQL
type was refused by frappe's own read-only rule). Data Import, the scheduler, inbound integrations
and backup restore are named, not driven; the scheduler shares the background-job row's path.

## 2. The scope floor is api-key only, and that is a limit **[your site]**

**Claim.** The credential floor (`auth_hooks`) binds an api-key credential to an allowlist of
methods and doctypes. It does not see an OAuth bearer or a desk cookie: the same user through those
doors reads whatever their roles allow.

**Prove it.** The walk's last three rows read a doctype NOT in the seat's grant (`READ_DOCTYPE`,
default `User`) as the seat with each credential. Expect `SEEN:SCOPE` with the api key and
`REACHED` with a bearer and with a cookie. If your bearer row reads `SEEN:SCOPE`, the floor has
grown since this page was written; tell us.

## 3. Two doors make an impossible book, and the reconciliation finds them **[your site]**

**Claim.** Raw SQL and `db_set` sit below the document lifecycle, so no hook sees them. A seat that
reaches them produces docstatus 1 with no GL rows. The floor does not claim them. The Close does.

**Prove it.** The bench half drives both on purpose; expect `PASSED-NO-GL` on `raw SQL update
docstatus` and `db_set docstatus`, counted in the summary line as `residual-impossible-book`, not as
a breach. Then run the Close against the same period:

```bash
pacioli close --reconcile --target <your-target> --since <date> --until <date>
```

The two vouchers appear in the reconciliation's ungoverned bucket. `pacioli verify` checks the
receipt chain itself (`pacioli anchor check` against an off-box head pin for the strong guarantee).

## 4. The floor is still absent in frappe, on the vendor's newest code

**Claim.** An api key resolves to a user and the request gets everything that user can do. There
is no scope on the key, no method allowlist, no doctype limit, and `frappe.whitelist()` has no scope
parameter. The one seam before dispatch is `auth_hooks`.

**Prove it.** Read it, do not take it from us:

```bash
git clone --depth 1 https://github.com/frappe/frappe /tmp/frappe-develop
grep -n -A 30 "def validate_api_key_secret" /tmp/frappe-develop/frappe/auth.py      # key -> user -> set_user
grep -n -A 12 "^def whitelist" /tmp/frappe-develop/frappe/__init__.py                 # no scope parameter
grep -n "auth_hooks" /tmp/frappe-develop/frappe/auth.py                               # the seam the guard uses
```

Last read by us 2026-08-18 on `develop` (17.0.0-dev): the only change on that path since v16 was
the secret compare going constant-time. Read the current `develop` yourself before quoting this.

## 5. The published wheel is the tag's wheel

**Claim.** What PyPI serves for `pacioli-guard X.Y.Z` has the same contents as a wheel built from
the git tag `guard-vX.Y.Z` (archive hashes differ by build timestamps; the unpacked trees do not).

```bash
V=0.17.0
pip download "pacioli-guard==$V" --no-deps --only-binary :all: -d /tmp/pypi
git clone --branch "guard-v$V" --depth 1 https://github.com/john-broadway/pacioli /tmp/tag
python -m pip install build && (cd /tmp/tag/guard && python -m build --wheel -o /tmp/built)
mkdir /tmp/a /tmp/b && unzip -q /tmp/pypi/*.whl -d /tmp/a && unzip -q /tmp/built/*.whl -d /tmp/b
diff -r /tmp/a /tmp/b && echo "contents identical"
```

Every GitHub release also carries a CycloneDX SBOM and sigstore build provenance:
`gh attestation verify <asset> --owner john-broadway`.

## 6. The tool count is real, and the door can be small

```bash
cd broker && python -c "from pacioli.tools import TOOLS; print(len(TOOLS))"           # 265 at 0.40.1
PACIOLI_TOOLSETS=dynamic python -c "from pacioli.doorway import served_tools; print(len(served_tools()))"   # 9
PACIOLI_TOOLSETS=core python -c "from pacioli.doorway import served_tools; print(len(served_tools()))"      # 6
python ../scripts/vendor_meta.py coverage                                            # the vendor's table vs the policy
```

The second number is the lean door: search, schema, call, plus the PLAN and PROVE spine. The
third is the core door: four tools over the vendor's own table of doctypes plus the PROVE pair,
with every submittable doctype either served or held by a sentence in `pacioli/core/doctypes.toml`.
The coverage line goes red on any submittable doctype nobody judged. In every mode every catalog
tool stays callable; advertisement is a context choice, never authorisation.

## 7. A seat cannot mint its own consent

**Claim.** The marker's `minted_by` is set server-side from the session and overwrites whatever
the caller sends, so a stolen key cannot mint its own permission.

```bash
grep -n "minted_by" guard/pacioli_guard/scoping/doctype/pacioli_consent_marker/pacioli_consent_marker.py
cd guard && python -m pytest pacioli_guard/tests/test_consent_marker_controller.py -q -k TestBeforeInsertBindsTheMinter
```

## Residuals, stated plainly

- **Below the lifecycle:** raw SQL and `db_set` (section 3). ERPNext core itself writes this way when
  reposting. The reconciliation is the control, not a hook.
- **Scope is api-key only** (section 2). Consent is not: it keys on the user at the document hook.
- **`flags.ignore_validate`** skips `before_submit`. On ledger-posting doctypes the consequence
  document still met the gate at its own insert on our lab; the code's docstring states the wider
  claim and is due a narrowing.
- **The consent gate covers gated seats.** A System Manager erasing a consent-marker row is an open
  design question, recorded in the controller's docstring, not closed.
- **The kit's lab run predates the delete gate** (guard 0.17.0 added `on_trash`; driven on our lab
  2026-10-05, seven doors). The kit drives submit; a delete walk is the next cut.
