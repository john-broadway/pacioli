# Copyright (c) 2026, John Broadway and contributors
# License: Apache-2.0
"""The human mint route (``pacioli_guard.mint``) — the frappe glue around ``plan_consent_marker``.

Why this module exists: until now the only way to create a floor marker was an ad-hoc script run as
Administrator inside the container, which proved the mechanism and was not a route a human could
take (`docs/plans/2026-07-26-consent-ceremony-decision.md`, Option B's outstanding cost). A site with
`require_consent` on and no way to mint cannot complete its first governed write.

**The test that matters most here is the clock one.** ``expires_at`` is a frappe Datetime, stored
naive, and ``enforce._epoch`` resolves a naive value through the **SITE's** zone (since 0.10.0 —
before that it used the process's zone and every marker was born expired). A minter that writes the
*process* clock therefore hands the reader a different instant than it meant. The fake below runs a
UTC-ish process clock against a non-UTC site zone on purpose: with both in one zone the skew is
invisible, which is exactly the dimension the earlier doubles lacked.
"""
import contextlib
import datetime
import io
import json
import sys
import types
import unittest
import zoneinfo

sys.modules.setdefault("frappe", types.ModuleType("frappe"))

from pacioli_guard import enforce  # noqa: E402
from pacioli_guard import mint  # noqa: E402
from pacioli_guard.scope import consent_token_hash  # noqa: E402

SITE_TZ = "America/Chicago"  # deliberately NOT the process zone


class FakeDoc:
    def __init__(self, fields):
        self.fields = dict(fields)
        self.inserted = False
        self.ignore_permissions = None
        self.name = "fake-marker-name"

    def insert(self, ignore_permissions=False):
        self.inserted = True
        self.ignore_permissions = ignore_permissions


class FakeDB:
    def __init__(self, existing):
        self.existing = existing
        self.committed = False
        self.exists_calls = []

    def exists(self, doctype, name=None, cache=False, *, debug=False):
        # Signature and return semantics mirror frappe 16's `Database.exists`: it returns the
        # matching document NAME (a truthy string) or None, not a bool — the review flagged the
        # earlier bool-returning double as a fidelity gap. The `dt == dn` Single shortcut is
        # reproduced deliberately, because that is the real behaviour this module must defend
        # against (`frappe/database/database.py`: "single always exists (!)").
        self.exists_calls.append((doctype, name))
        if doctype != "DocType" and doctype == name:
            return name
        return name if (doctype, name) in self.existing else None

    def get_value(self, doctype, name=None, fieldname="name", **kw):
        # What `_document_exists` uses instead of `exists`, precisely BECAUSE this has no Single
        # shortcut: a real row lookup or nothing.
        self.exists_calls.append((doctype, name))
        return name if (doctype, name) in self.existing else None

    def commit(self):
        self.committed = True


class FakeUtils:
    """`now_datetime()` is frappe's site-zone naive clock — the convention for Datetime fields.

    `frozen` pins an AWARE instant so a test can sit on a real DST boundary instead of on "now".
    Without that, no test can exercise a transition, which is exactly why the DST defect shipped.
    """

    def __init__(self):
        self.frozen = None

    def get_system_timezone(self):
        return SITE_TZ

    def _aware_now(self):
        return self.frozen or datetime.datetime.now(zoneinfo.ZoneInfo(SITE_TZ))

    def now_datetime(self):
        return self._aware_now().astimezone(zoneinfo.ZoneInfo(SITE_TZ)).replace(tzinfo=None)


class FakeFrappe:
    def __init__(self, existing=(("Sales Invoice", "ACC-SINV-2026-00004"),), user="operator@x.com"):
        self.db = FakeDB(set(existing))
        self.session = types.SimpleNamespace(user=user)
        self.utils = FakeUtils()
        self.docs = []

    def get_doc(self, fields):
        doc = FakeDoc(fields)
        self.docs.append(doc)
        return doc


class MintBase(unittest.TestCase):
    def setUp(self):
        self._real_mint, self._real_enforce = mint.frappe, enforce.frappe
        self.fake = FakeFrappe()
        mint.frappe = self.fake
        enforce.frappe = self.fake

    def tearDown(self):
        mint.frappe, enforce.frappe = self._real_mint, self._real_enforce

    def mint_one(self, **over):
        kw = {"ref_doctype": "Sales Invoice", "ref_docname": "ACC-SINV-2026-00004",
              "ref_action": "submit", "ttl_seconds": 900}
        kw.update(over)
        return mint.mint_consent_marker(**kw)


class TestTheGuardReadsBackTheLifetimeThatWasIntended(MintBase):
    """The regression test for a live defect, measured on the bench 2026-07-29.

    `deploy/bench/mint-consent-marker.py` wrote `datetime.now()` (the container's clock, UTC) while
    the site ran America/Chicago. `enforce._epoch` resolved that naive value through the SITE zone,
    so a marker minted for 900 seconds read as **17,980 seconds** of remaining life — roughly five
    hours. Fail-OPEN on lifetime: still document-bound, act-bound, single-use and minter-separated,
    so never an escape, but "short-lived by design" was off by a factor of twenty.
    """

    def test_the_process_clock_is_never_read(self):
        """ZONE-INDEPENDENT detection of the reverted bug, and the reason it is needed.

        🔴 The numeric test below is only decisive when the host's zone differs from `SITE_TZ`.
        An independent review reproduced the hole (2026-07-29): with the process zone forced to
        America/Chicago and `frappe.utils.now_datetime()` reverted to `datetime.datetime.now()`,
        the remaining lifetime computes to 900s and the numeric assertion PASSES with the bug fully
        reintroduced. CI zone is not pinned by anything in this repo, so that test alone is luck.

        This one asserts the CALL rather than the NUMBER: a bare `datetime.datetime.now()` reads the
        process clock and is always wrong here, whatever zone the host is in.
        """
        real = mint.datetime

        class _RefusesProcessClock(datetime.datetime):
            @classmethod
            def now(cls, tz=None):
                if tz is None:
                    raise AssertionError(
                        "mint read the PROCESS clock via datetime.datetime.now(); expires_at must "
                        "come from frappe.utils.now_datetime() (site-zone naive), which is what "
                        "enforce._epoch resolves a naive value through")
                return datetime.datetime.now(tz)

        mint.datetime = types.SimpleNamespace(
            datetime=_RefusesProcessClock,
            timedelta=datetime.timedelta,
            timezone=datetime.timezone,
        )
        try:
            result = self.mint_one(ttl_seconds=900)
        finally:
            mint.datetime = real
        self.assertTrue(result["ok"], result)

    def test_a_900_second_ttl_is_900_seconds_to_the_gate_that_reads_it(self):
        # PRECONDITION, not decoration: this assertion can only distinguish the site clock from the
        # process clock while the two actually differ. If a CI box is pinned to SITE_TZ they agree,
        # and a silent pass here would mean nothing. Fail loudly instead of passing blindly.
        process_offset = datetime.datetime.now().astimezone().utcoffset()
        site_offset = datetime.datetime.now(zoneinfo.ZoneInfo(SITE_TZ)).utcoffset()
        if process_offset == site_offset:
            self.skipTest(
                f"process zone offset equals {SITE_TZ}'s ({site_offset}); this numeric check cannot "
                f"tell the site clock from the process clock here. "
                f"test_the_process_clock_is_never_read covers it zone-independently.")
        result = self.mint_one(ttl_seconds=900)
        self.assertTrue(result["ok"], result)
        written = self.fake.docs[0].fields["expires_at"]
        # Coerce with the GUARD'S OWN reader, not a reimplementation of it. This is the number
        # `consent_verdict` compares against `time.time()`.
        as_the_gate_sees_it = enforce._epoch(written)
        remaining = as_the_gate_sees_it - datetime.datetime.now(
            datetime.timezone.utc).timestamp()
        self.assertAlmostEqual(remaining, 900, delta=30,
                              msg=f"the gate sees {remaining:.0f}s of life, not 900s — "
                                  f"the minter and the reader are in different clock domains")

    def test_the_expiry_is_written_naive_in_the_SITE_zone(self):
        # frappe's convention for a Datetime field, and the one `_epoch` assumes for a naive value.
        written = self.fake.docs[0].fields["expires_at"] if self.fake.docs else None
        if written is None:
            self.mint_one()
            written = self.fake.docs[0].fields["expires_at"]
        self.assertIsNone(getattr(written, "tzinfo", None),
                          "a Datetime field is stored naive; an aware value changes what _epoch does")
        site_now = datetime.datetime.now(zoneinfo.ZoneInfo(SITE_TZ)).replace(tzinfo=None)
        self.assertAlmostEqual((written - site_now).total_seconds(), 900, delta=30)


class TestTheMintedRow(MintBase):
    def test_it_inserts_a_marker_bound_to_the_document_and_act(self):
        result = self.mint_one()
        self.assertTrue(result["ok"], result)
        doc = self.fake.docs[0]
        self.assertTrue(doc.inserted)
        self.assertEqual(doc.fields["doctype"], mint.MARKER_DOCTYPE)
        self.assertEqual(doc.fields["ref_doctype"], "Sales Invoice")
        self.assertEqual(doc.fields["ref_docname"], "ACC-SINV-2026-00004")
        self.assertEqual(doc.fields["ref_action"], "submit")
        self.assertEqual(doc.fields["burned"], 0)

    def _mint_with_known_token(self, token="a-known-fixture-token-not-a-secret", **over):
        """Pin `secrets` so the raw token is knowable without the function returning it.

        It stopped returning the token deliberately (see `TestFindingsFromTheIndependentReview`),
        which means a test can only learn it by fixing the generator or reading stdout. Fixing the
        generator lets us check the stored hash AND the printed disclosure in one go.
        """
        real = mint.secrets
        mint.secrets = types.SimpleNamespace(token_urlsafe=lambda n: token)
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                result = self.mint_one(**over)
        finally:
            mint.secrets = real
        return result, token, buf.getvalue()

    def test_it_stores_only_the_hash_of_the_token(self):
        result, token, _ = self._mint_with_known_token()
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.fake.docs[0].fields["token_hash"], consent_token_hash(token))
        # The row must not carry the secret anywhere.
        self.assertNotIn(token, repr(self.fake.docs[0].fields))

    def test_the_token_is_printed_exactly_once_INCLUDING_the_bench_execute_echo(self):
        """The real defect was `bench execute` echoing the RETURN VALUE, and this test used to miss it.

        🔴 It only counted in-process stdout, so putting `"token": token` back into the return value
        left it green — the actual catch lived in a different class
        (`test_the_raw_token_is_not_in_the_return_value`). The docstring named the bench-echo bug
        while the body could not see it. Caught by review.

        Now it simulates what `bench execute` really does — `frappe/commands/utils.py`:
        ``if ret: print(json.dumps(ret, default=json_handler))`` — so the count covers both prints.
        """
        result, token, out = self._mint_with_known_token()
        if result:  # exactly bench's own condition
            out += json.dumps(result, default=str)
        self.assertEqual(out.count(token), 1,
                         f"token appears {out.count(token)} times across the function's own print "
                         f"AND the bench-execute echo of its return value:\n{out}")

    def test_the_disclosure_names_the_act_being_authorised(self):
        """The operator's only independent view of what they are consenting to. Untested until the
        review pointed it out — a printout that shows only a token renders every act identically."""
        _, _, out = self._mint_with_known_token()
        self.assertIn("Sales Invoice", out)
        self.assertIn("ACC-SINV-2026-00004", out)
        self.assertIn("SUBMIT", out)
        self.assertIn("operator@x.com", out)          # who the server will record as minter
        self.assertIn("OUT OF BAND", out)

    def test_two_mints_never_share_a_token(self):
        # No fixture here: this is the one property that needs the REAL generator.
        first = self.fake.docs
        self.mint_one()
        self.mint_one()
        hashes = {d.fields["token_hash"] for d in self.fake.docs}
        self.assertEqual(len(hashes), 2, "two mints produced the same token hash")

    def test_it_does_not_send_minted_by(self):
        """`before_insert` binds it from the session and overwrites the caller. Sending it would
        imply this establishes separation; the server does, and floor audit F3 is why."""
        self.mint_one()
        self.assertNotIn("minted_by", self.fake.docs[0].fields)

    def test_it_commits_so_the_marker_survives_the_process(self):
        self.mint_one()
        self.assertTrue(self.fake.db.committed)


class TestMintRefusals(MintBase):
    def test_it_refuses_a_document_that_does_not_exist(self):
        # A marker for a docname nobody can spend is consent-shaped clutter, and a typo here means
        # the operator thinks they authorised something they did not.
        result = self.mint_one(ref_docname="ACC-SINV-DOES-NOT-EXIST")
        self.assertFalse(result["ok"])
        self.assertIn("does not exist", result["reason"].lower())
        self.assertEqual(self.fake.docs, [], "nothing may be inserted on a refusal")

    def test_it_refuses_an_unknown_act(self):
        result = self.mint_one(ref_action="delete")
        self.assertFalse(result["ok"])
        self.assertEqual(self.fake.docs, [])

    def test_it_refuses_a_ttl_outside_the_short_lived_range(self):
        result = self.mint_one(ttl_seconds=999_999)
        self.assertFalse(result["ok"])
        self.assertEqual(self.fake.docs, [])

    def test_a_refusal_never_commits(self):
        self.mint_one(ref_action="delete")
        self.assertFalse(self.fake.db.committed)


class TestFindingsFromTheIndependentReview(MintBase):
    """Three real defects an adversarial review found in this module (2026-07-29), each verified
    against frappe 16 source rather than against our own comments."""

    def test_the_raw_token_is_not_in_the_return_value(self):
        """`bench execute` ECHOES the return value, so returning the token prints it TWICE.

        `frappe/commands/utils.py`: `if ret: print(json.dumps(ret, default=json_handler)...)`. The
        module docstring claimed the token is "printed once"; it was printed once by us and again by
        bench, in an unlabelled JSON blob that a "redact lines containing `token:`" filter misses.
        Observed for real: a live token landed twice in a session transcript on first use, because
        `bench execute` here runs over ssh into captured stdout. The human already has it from the
        explicit print, so the return value must not carry it.
        """
        result = self.mint_one()
        self.assertTrue(result["ok"], result)
        self.assertNotIn("token", result)
        # The useful, non-secret facts still come back for scripting.
        self.assertEqual(result["name"], "fake-marker-name")
        self.assertIn("expires_at", result)

    def test_a_docname_equal_to_the_doctype_is_refused(self):
        """`frappe.db.exists(dt, dn)` returns TRUTHY when `dt == dn`, with no document present.

        `frappe/database/database.py`: `if dt != "DocType" and dt == dn: return dn  # single always
        exists (!)`. That shortcut is for Single doctypes but is applied unconditionally, so
        `exists("Sales Invoice", "Sales Invoice")` is truthy. Our existence check therefore passed
        and we minted a marker for a document that does not exist, breaking this module's own stated
        invariant ("refusing to mint a marker nothing could spend"). Any typo where the docname
        equals the doctype triggers it.
        """
        result = self.mint_one(ref_docname="Sales Invoice")
        self.assertFalse(result["ok"], "a docname equal to the doctype must not pass as existing")
        self.assertEqual(self.fake.docs, [], "nothing may be inserted")

    def test_a_nonstring_docname_never_reaches_the_database(self):
        """`frappe.db.exists` also accepts a FILTER DICT as its second argument, so an unvalidated
        `ref_docname` reached a live query that asks "does ANY row match" instead of checking one
        document. Refused downstream by the pure planner today, but the query ran first. Validate,
        then query."""
        self.fake.db.exists_calls = []
        result = self.mint_one(ref_docname={"customer": "ACME"})
        self.assertFalse(result["ok"])
        self.assertEqual(self.fake.db.exists_calls, [],
                         "the DB must not be queried with an unvalidated docname")


class TestTheBenchExecuteBoundary(MintBase):
    """`bench execute --kwargs` hands every value in as a STRING. The glue is where the world's
    messiness is absorbed; the pure planner stays strict and refuses a string ttl."""

    def test_a_string_ttl_is_accepted_from_the_command_line(self):
        result = self.mint_one(ttl_seconds="900")
        self.assertTrue(result["ok"], result)

    def test_a_nonsense_ttl_string_is_still_refused(self):
        result = self.mint_one(ttl_seconds="fifteen minutes")
        self.assertFalse(result["ok"])
        self.assertEqual(self.fake.docs, [])


class TestItIsNotAnHttpSurface(unittest.TestCase):
    """THE SECURITY PROPERTY OF THIS MODULE. A whitelisted mint endpoint would be reachable by the
    very api-key credentials the floor exists to constrain — a credential that can mint its own
    consent is signing its own permission slip. `consent_verdict` refuses a self-minted marker at
    spend time, but that backstop is not a reason to open the door. This runs from the books side
    only (`bench execute`), which is Option B's whole point.

    🔴 **THE FIRST VERSION OF THIS TEST ASSERTED NOTHING** (caught by an independent review,
    2026-07-29). It read `getattr(fn, "whitelisted", False)` and a `__wrapped__` class name. But
    `frappe.whitelist()` adds the function to a module-level **set** (`frappe/__init__.py`, checked
    by `is_whitelisted`) and sets **no attribute on the function at all**, and
    `getattr(fn, "__wrapped__", "")` returns `""` whose class name is `"str"`. Both assertions
    therefore pass for a genuinely whitelisted function — proven by reimplementing the real
    decorator. A test for the module's central security property was pure decoration.

    The check below is structural: parse the source and look for the decorator itself. That needs no
    frappe import (these suites run bench-free by design) and goes red if anyone adds one.

    ⚠️ **STATED RESIDUAL — this check is blind to RUNTIME registration, and that is not fixable by
    static analysis.** A second review (2026-07-29) demonstrated two genuine evasions:
    ``mint_consent_marker = frappe.whitelist()(mint_consent_marker)`` after the ``def``, and
    ``frappe.whitelisted.add(mint_consent_marker)``. Both really do land the function in frappe's
    ``whitelisted`` set while leaving no decorator node to find. Closing that needs an assertion
    against the live ``frappe.whitelisted`` set, which requires a real frappe import and therefore a
    bench-integration test, not this file. **Do not read a green here as proof of unreachability** —
    it proves only that nobody added a decorator. The mint-reachability argument itself rests on
    ``is_whitelisted`` being unconditional set membership with no Administrator bypass, verified in
    frappe source and recorded in the CHANGELOG.
    """

    def _decorator_names(self, source=None):
        """Every decorator name on every function in ``source`` (default: the real `mint.py`).

        ``source`` is a parameter so the guard-the-guard test can drive THIS function instead of
        reimplementing it. The first version of that test walked its own inline copy of this logic,
        which meant stubbing this method to return ``[]`` left both tests green — the exact failure
        it claimed to prevent. Caught by review; do not inline it again.

        Alias-aware: ``from frappe import whitelist as wl`` then ``@wl()`` used to slip through,
        because only the literal names were compared. Any local name bound to ``frappe.whitelist``
        by an import is now reported as ``"whitelist"``.
        """
        import ast
        import inspect

        tree = ast.parse(inspect.getsource(mint) if source is None else source)

        aliases = {"whitelist"}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "frappe":
                for alias in node.names:
                    if alias.name == "whitelist":
                        aliases.add(alias.asname or alias.name)

        names = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for dec in node.decorator_list:
                # `@frappe.whitelist()` is a Call whose func is an Attribute; `@whitelist` is a Name.
                target = dec.func if isinstance(dec, ast.Call) else dec
                if isinstance(target, ast.Attribute):
                    names.append(target.attr)
                elif isinstance(target, ast.Name):
                    # Normalise an alias back to the thing it refers to.
                    names.append("whitelist" if target.id in aliases else target.id)
        return names

    def test_no_function_in_the_mint_module_is_whitelisted(self):
        self.assertNotIn("whitelist", self._decorator_names())

    def test_the_check_can_actually_see_a_decorator(self):
        """Guards the guard, by driving the REAL detector rather than a copy of it.

        If `_decorator_names` ever returns nothing — a stubbed body, a broken walk, an ast API
        change — the security test above would pass forever. This calls the same method on sources
        it must flag, so breaking the detector breaks this too.
        """
        for label, src in (
            ("attribute form", "import frappe\n@frappe.whitelist()\ndef f():\n    pass\n"),
            ("bare name", "from frappe import whitelist\n@whitelist()\ndef f():\n    pass\n"),
            ("aliased import", "from frappe import whitelist as wl\n@wl()\ndef f():\n    pass\n"),
            ("undecorated call", "import frappe\n@frappe.whitelist\ndef f():\n    pass\n"),
        ):
            with self.subTest(label):
                self.assertIn("whitelist", self._decorator_names(src),
                              f"the detector missed the {label}")

    def test_the_detector_does_not_cry_wolf(self):
        """It must not flag an ordinary decorator, or the security test becomes noise people mute."""
        src = "import functools\n@functools.cache\ndef f():\n    pass\n"
        self.assertNotIn("whitelist", self._decorator_names(src))


class TestTheExpiryDoesNotDependOnAMutableGlobal(MintBase):
    """The root-cause fix for three separate findings (2026-07-29).

    A naive site-zone `expires_at` means whatever `System Settings.time_zone` says **at spend time**,
    which produced three defects with one cause:

    1. **DST.** `site_now + timedelta` is naive wall-clock arithmetic, so a TTL spanning a transition
       is off by exactly the DST shift — fail-OPEN across the doubled fall-back hour.
    2. **The UTC fallback direction.** If the site zone cannot be read, a naive value resolved as UTC
       expires LATER than intended for any site east of UTC (+5:30 for `Asia/Kolkata`, frappe's own
       default for an unset zone).
    3. **A site timezone change** silently re-times every live marker, either direction.

    The marker now carries `expires_at_epoch`: the true instant, computed once at mint. Epoch
    seconds have no timezone to disagree about, so all three collapse. The naive Datetime stays for
    the desk UI and for pre-0.13.0 rows.
    """

    def test_the_epoch_instant_is_stored_alongside_the_readable_datetime(self):
        result = self.mint_one(ttl_seconds=900)
        self.assertTrue(result["ok"], result)
        fields = self.fake.docs[0].fields
        self.assertIn("expires_at_epoch", fields)
        # ~900s from true now, in real epoch seconds — no zone arithmetic involved.
        self.assertAlmostEqual(
            fields["expires_at_epoch"] - datetime.datetime.now(datetime.timezone.utc).timestamp(),
            900, delta=30)

    def test_a_ttl_spanning_a_DST_transition_keeps_its_true_length(self):
        """The DST case, pinned at a real 2026 boundary rather than 'now'.

        America/Chicago springs forward 2026-03-08 02:00->03:00. A 4500s TTL minted at 01:55 local
        crosses the gap. Naive arithmetic lands on 03:10 and loses the hour; epoch arithmetic does
        not, because it never leaves the instant domain.
        """
        boundary = datetime.datetime(2026, 3, 8, 1, 55, tzinfo=zoneinfo.ZoneInfo(SITE_TZ))
        real_time = mint.time
        self.fake.utils.frozen = boundary
        mint.time = types.SimpleNamespace(time=lambda: boundary.timestamp())
        try:
            result = self.mint_one(ttl_seconds=4500)
        finally:
            mint.time = real_time
            self.fake.utils.frozen = None
        self.assertTrue(result["ok"], result)

        stored_epoch = self.fake.docs[0].fields["expires_at_epoch"]
        self.assertAlmostEqual(stored_epoch - boundary.timestamp(), 4500, delta=1,
                               msg="the stored instant lost or gained the DST hour")

        # The readable Datetime is DERIVED from that instant rather than computed by naive
        # arithmetic, so it now names the same moment even across the gap: 01:55 + 4500s is 04:10
        # local, not the 03:10 naive addition produces. Round-tripping it must land back on the
        # true instant.
        naive = self.fake.docs[0].fields["expires_at"]
        round_tripped = naive.replace(tzinfo=zoneinfo.ZoneInfo(SITE_TZ)).timestamp()
        self.assertAlmostEqual(round_tripped, stored_epoch, delta=1,
                               msg=f"the readable value {naive} is not the same moment as the epoch")
        self.assertEqual((naive.hour, naive.minute), (4, 10),
                         "naive arithmetic would have produced 03:10 and silently eaten the hour")


    def test_a_ttl_spanning_the_DOUBLED_fall_back_hour_does_not_gain_an_hour(self):
        """The fail-OPEN direction, which is the one that mattered.

        America/Chicago falls back 2026-11-01 02:00 CDT -> 01:00 CST, so 01:00-02:00 happens twice.
        A 5400s TTL minted at 00:50 crosses it. Naive arithmetic lands on 02:20 and the gate read
        that as an hour LATER than granted — the marker outlived its TTL. The authoritative instant
        is now epoch, so there is no ambiguous wall clock in the decision at all.
        """
        boundary = datetime.datetime(2026, 11, 1, 0, 50, tzinfo=zoneinfo.ZoneInfo(SITE_TZ))
        real_time = mint.time
        self.fake.utils.frozen = boundary
        mint.time = types.SimpleNamespace(time=lambda: boundary.timestamp())
        try:
            result = self.mint_one(ttl_seconds=5400)
        finally:
            mint.time = real_time
            self.fake.utils.frozen = None
        self.assertTrue(result["ok"], result)
        stored_epoch = self.fake.docs[0].fields["expires_at_epoch"]
        self.assertAlmostEqual(stored_epoch - boundary.timestamp(), 5400, delta=1,
                               msg="the marker gained or lost the fall-back hour")


class TestTheGateReadsTheEpochInPreference(unittest.TestCase):
    """`_consent_record` must trust the instant, not the wall clock — and must still read markers
    minted before the field existed."""

    def _record(self, **row):
        base = {"name": "m1", "token_hash": "h", "ref_doctype": "Sales Invoice",
                "ref_docname": "SI-1", "ref_action": "submit", "burned": 0,
                "minted_by": "operator@x.com", "expires_at": None, "expires_at_epoch": None}
        base.update(row)

        class _DB:
            def get_value(self, *a, **kw):
                return dict(base)

        fake = types.SimpleNamespace(db=_DB(), utils=FakeUtils())
        real = enforce.frappe
        enforce.frappe = fake
        try:
            return enforce._consent_record("Sales Invoice", "SI-1")
        finally:
            enforce.frappe = real

    def test_the_epoch_field_wins_when_present(self):
        # A deliberately disagreeing pair: the naive value says one thing, the epoch another.
        rec = self._record(expires_at=datetime.datetime(2030, 1, 1), expires_at_epoch=1785380000.0)
        self.assertEqual(rec["expires_at"], 1785380000.0)

    def test_a_pre_0_13_0_marker_still_reads_from_the_datetime(self):
        rec = self._record(expires_at=datetime.datetime(2026, 7, 29, 22, 0), expires_at_epoch=None)
        self.assertIsNotNone(rec["expires_at"], "markers minted before the field existed must work")

    def test_the_REAL_post_migration_value_is_zero_not_none(self):
        """Caught by running the migration on a live bench rather than trusting the double.

        A frappe `Float` column defaults to **0**, not NULL, so after `bench migrate` every
        pre-existing marker reads `expires_at_epoch = 0.0` — not `None` as this test file first
        assumed. The fallback happened to work because `0.0` is falsy, which is luck rather than
        design, so `_expiry_instant` now treats 0 as UNSET deliberately. A real expiry of 0 would be
        1970, i.e. long expired, so nothing legitimate is lost by that reading.
        """
        naive = datetime.datetime(2026, 7, 29, 22, 0)
        rec = self._record(expires_at=naive, expires_at_epoch=0.0)
        # 🔴 `assertIsNotNone` was the whole assertion here, and it passes whether the guard works OR
        # returns 0.0 — which is also not None. Caught by review. Assert the VALUE: the fallback must
        # yield the instant the Datetime names, resolved through the site zone.
        self.assertEqual(rec["expires_at"],
                         naive.replace(tzinfo=zoneinfo.ZoneInfo(SITE_TZ)).timestamp(),
                         "a migrated marker must fall back to its Datetime, not read as epoch 0")
        self.assertNotEqual(rec["expires_at"], 0.0)

    def test_a_zero_epoch_with_no_datetime_still_denies(self):
        rec = self._record(expires_at=None, expires_at_epoch=0.0)
        self.assertIsNone(rec["expires_at"])

    def test_an_unusable_epoch_falls_back_rather_than_authorising(self):
        for bad in (float("inf"), float("nan"), "not a number"):
            rec = self._record(expires_at=None, expires_at_epoch=bad)
            self.assertIsNone(rec["expires_at"],
                              f"epoch={bad!r} with no readable datetime must deny, not authorise")
