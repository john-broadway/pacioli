"""The walk kit, driven against a fake site that behaves like a bench with pacioli-guard on it.

The kit is the shipped form of the 2026-09-26 lab walk. These tests hold what the lab run could
not: the exit codes (0 floor held / 3 fixture / 4 breached), the stop rule (the seat column ends at
the first door that submits the draft, and a stop is always a ROW), that --check performs no document
write, and the bench half driven through a fake `frappe`. The fake site answers with the floor's own
refusal sentences, so the gate classifier is driven on the words the wire carries.
"""
from __future__ import annotations

import io
import json
import sys
import types
import urllib.parse
from contextlib import redirect_stdout
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "verify"))
import walk_the_doors as W  # noqa: E402

CONSENT = ('{"exception":"frappe.exceptions.PermissionError: This credential requires human consent '
           'to submit a document, and no consent marker presented. Refused for Sales Invoice X."}')
SCOPE = ('{"exception":"frappe.exceptions.PermissionError: This credential is scoped and is not '
         'permitted to call this endpoint (resource: ...)."}')
RATE = '{"exception":"This credential is over its rate limit of 10 per minute."}'

SEAT_KEY, CTL_KEY, BEARER = "seatkey1:seatsecret-xyz", "ctlkey1:ctlsecret-xyz", "bearer-token-abc"
SEAT_SID, CTL_SID = "sid-seat-9f8e7d", "sid-ctl-1a2b3c"
CTL_DRAFTS = [f"CTL-{i}" for i in range(9)]


class FakeSite:
    """A bench with the floor on it. `breach_at` = index into W.HTTP_DOORS where the seat passes;
    `fixture_at` = index where the door answers 500 for the seat; `fail_draft_reads` = which GETs of
    the seat's draft (1-based count) answer 500; `dead_after_door` = the seat's door at that index
    answers 200 without moving the draft and every later GET of the draft answers 500."""

    def __init__(self, breach_at=None, fixture_at=None):
        self.ds = {"DRAFT-1": 0, **{d: 0 for d in CTL_DRAFTS}}
        self.gl = {}
        self.calls = []
        self.breach_at, self.fixture_at = breach_at, fixture_at
        self.fail_draft_reads, self.dead_after_door, self.draft_reads, self.dead = set(), None, 0, False

    def who(self, headers):
        a, c = headers.get("Authorization", ""), headers.get("Cookie", "")
        if a == "token " + SEAT_KEY or a == "Bearer " + BEARER or SEAT_SID in c:
            return "seat", "api-key" if a.startswith("token") else ("bearer" if a.startswith("Bearer") else "cookie")
        if a == "token " + CTL_KEY or CTL_SID in c:
            return "control", "api-key" if a else "cookie"
        return None, None

    @staticmethod
    def door_index(method, path):
        if method == "PUT" and path.startswith("/api/resource/"):
            return 1
        if method == "POST":
            return {"/api/method/run_doc_method": 0, "/api/method/frappe.client.submit": 3,
                    "/api/method/frappe.desk.form.save.savedocs": 4}.get(path, 2 if path.startswith("/api/v2/document/") else None)
        return None

    def __call__(self, method, url, headers, body):
        path = url.split("//site", 1)[1]
        self.calls.append((method, path))
        data = json.loads(body) if body else {}
        if path == "/api/method/ping":
            return 200, '{"message":"pong"}', []
        if path == "/api/method/login":
            if data == {"usr": "seat@x", "pwd": "pw-seat-123"}:
                return 200, '{"message":"Logged In"}', [f"sid={SEAT_SID}; Path=/", "system_user=yes; Path=/"]
            if data == {"usr": "ctl@x", "pwd": "pw-ctl-123"}:
                return 200, '{"message":"Logged In"}', [f"sid={CTL_SID}; Path=/"]
            return 401, '{"message":"Invalid login"}', []
        who, kind = self.who(headers)
        if who is None:
            return 401, '{"exc_type":"AuthenticationError"}', []
        if path == "/api/method/frappe.auth.get_logged_user":
            return (403, SCOPE, []) if (who == "seat" and kind == "api-key") else (200, '{"message":"u"}', [])
        if path.startswith("/api/resource/GL%20Entry"):
            if who != "control":
                return 403, SCOPE, []
            name = json.loads(urllib.parse.parse_qs(path.split("?", 1)[1])["filters"][0])[0][2]
            return 200, json.dumps({"data": [{"name": f"GL-{i}"} for i in range(self.gl.get(name, 0))]}), []
        if path.startswith("/api/resource/User"):
            return (403, SCOPE, []) if kind == "api-key" else (200, '{"data":[{"name":"u@x"}]}', [])
        if method == "GET" and path.startswith("/api/resource/Sales%20Invoice/"):
            name = path.rsplit("/", 1)[1]
            if name == "DRAFT-1":
                self.draft_reads += 1
                if self.draft_reads in self.fail_draft_reads or self.dead:
                    return 500, "Internal Server Error", []
            if name not in self.ds:
                return 404, '{"exc_type":"DoesNotExistError"}', []
            return 200, json.dumps({"data": {"name": name, "doctype": "Sales Invoice", "docstatus": self.ds[name]}}), []
        idx = self.door_index(method, path)
        if idx is None:
            return 404, "no route", []
        parts = urllib.parse.unquote(path).split("/")   # PUT: /api/resource/<dt>/<name>; v2: /api/v2/document/<dt>/<name>/method/submit/
        name = data.get("dn") or (json.loads(data["doc"])["name"] if "doc" in data else parts[5 if idx == 2 else 4])
        if who == "seat":
            if idx == self.fixture_at:
                return 500, "boom", []
            if idx == self.dead_after_door:
                self.dead = True
                return 200, '{"docs":[]}', []
            if idx == 1 and idx != self.breach_at:
                return 403, SCOPE, []
            if idx != self.breach_at:
                return 403, CONSENT, []
        self.ds[name], self.gl[name] = 1, 2
        return 200, json.dumps({"docs": [{"name": name, "docstatus": 1}]}), []


@pytest.fixture
def site(monkeypatch, tmp_path):
    fake = FakeSite()
    monkeypatch.setattr(W, "TRANSPORT", fake)
    monkeypatch.setattr(W, "SECRETS", [])
    monkeypatch.setenv("SITE", "http://site")
    monkeypatch.setenv("SEAT_KEY", SEAT_KEY)
    monkeypatch.setenv("DRAFT", "DRAFT-1")
    monkeypatch.setenv("OUT", str(tmp_path / "walk.tsv"))
    for k in ("SEAT_BEARER", "SEAT_USER", "SEAT_PASS", "CONTROL_KEY", "CONTROL_USER", "CONTROL_PASS",
              "CONTROL_DRAFTS", "HOST_HEADER", "READ_DOCTYPE", "DOCTYPE"):
        monkeypatch.delenv(k, raising=False)
    return fake


def full_env(monkeypatch):
    monkeypatch.setenv("SEAT_BEARER", BEARER)
    monkeypatch.setenv("SEAT_USER", "seat@x")
    monkeypatch.setenv("SEAT_PASS", "pw-seat-123")
    monkeypatch.setenv("CONTROL_KEY", CTL_KEY)
    monkeypatch.setenv("CONTROL_USER", "ctl@x")
    monkeypatch.setenv("CONTROL_PASS", "pw-ctl-123")
    monkeypatch.setenv("CONTROL_DRAFTS", ",".join(CTL_DRAFTS))


def run(argv):
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = W.main(argv)
    return rc, buf.getvalue()


def rows_of(path):
    lines = Path(path).read_text().splitlines()
    return [dict(zip(lines[0].split("\t"), ln.split("\t"))) for ln in lines[1:]]


def seat_rows(rows):
    return [r for r in rows if r["principal"] == "seat" and r["doc"] != "-"]


# ---------------------------------------------------------------- the pure parts
@pytest.mark.parametrize("code,body,gate", [
    (403, CONSENT, "CONSENT"), (403, SCOPE, "SCOPE"), (429, RATE, "RATE"), (401, "", "AUTH"),
    (403, '{"exception":"PermissionError: not permitted"}', "FRAPPE-PERM"), (200, "{}", "NONE"),
    (200, CONSENT, "NONE"),     # a 200 is never a refusal, whatever its body says (lens M4)
    (302, SCOPE, "NONE"),
    (500, "boom", "ERR"), (0, "http-error", "ERR"),
])
def test_gate_of_keys_on_the_floors_own_words_only_on_a_refusal(code, body, gate):
    assert W.gate_of(code, body) == gate


@pytest.mark.parametrize("code,gate,ds,gl,verdict", [
    (200, "NONE", 1, 2, "PASSED"), (200, "NONE", 1, 0, "PASSED-NO-GL"), (200, "NONE", 1, None, "PASSED (GL unread)"),
    (403, "CONSENT", 0, 0, "SEEN:CONSENT"), (403, "SCOPE", 0, 0, "SEEN:SCOPE"), (429, "RATE", 0, 0, "SEEN:RATE"),
    (403, "FRAPPE-PERM", 0, 0, "STOCK-PERM"), (500, "ERR", 0, 0, "FIXTURE:500"),
    (403, "CONSENT", -1, 0, "FIXTURE:403"),   # a refusal with no readback is not a SEEN
    (200, "NONE", 0, 0, "FIXTURE:200"),       # 200 and the book did not move: the door did nothing
])
def test_verdict_matrix(code, gate, ds, gl, verdict):
    assert W.verdict_of(code, gate, ds, gl) == verdict


def test_scrub_removes_every_secret_including_the_pair(monkeypatch):
    monkeypatch.setattr(W, "SECRETS", [])
    W.Principal("seat", key=SEAT_KEY, bearer=BEARER, user="seat@x", pw="pw-seat-123")
    t = W.scrub(f"token {SEAT_KEY} / seatsecret-xyz / Bearer {BEARER} / pw-seat-123 / short:ab")
    assert "seatsecret" not in t and "bearer-token" not in t and "pw-seat" not in t
    assert "short:ab" in t   # values under 6 chars are not secrets and must not blank the text


def test_summary_exit_codes_the_residual_and_the_short_column():
    seat = "seat"
    base = dict(code=200, gate="NONE", doc="x", note="")
    seen = dict(base, door="d1", principal=seat, docstatus=0, gl=0, verdict="SEEN:CONSENT")
    sql = dict(base, door=W.BENCH_DOORS[2], principal=seat, docstatus=1, gl=0, verdict="PASSED-NO-GL")
    fix = dict(base, door="d2", principal=seat, docstatus=-1, gl=0, verdict="FIXTURE:500")
    breach = dict(base, door="d3", principal=seat, docstatus=1, gl=2, verdict="PASSED")
    unknown = dict(base, door="d4", principal=seat, docstatus=-1, gl=0, verdict=W.BREACH_UNKNOWN)
    ctl = dict(base, door="d1", principal="control", docstatus=1, gl=2, verdict="PASSED")
    reach = dict(base, door="read User", principal=seat, docstatus=-1, gl=0, verdict="SEEN:SCOPE", doc="-")
    assert W.summary([seen], seat)[1] == 0
    assert W.summary([seen, fix], seat)[1] == 3
    assert W.summary([seen, fix, breach], seat)[1] == 4
    assert W.summary([seen, unknown], seat)[1] == 4                            # a 2xx with no readback is a breach
    assert W.summary([seen, sql], seat)[1] == 4                                # unnamed, it is a breach
    assert W.summary([seen, sql], seat, residual=W.BENCH_DOORS[2:4])[1] == 0  # named, the impossible book
    assert W.summary([seen], seat, planned=1)[1] == 0
    assert W.summary([seen], seat, planned=2)[1] == 3                          # a column shorter than its plan
    assert "SHORT" in W.summary([seen], seat, planned=2)[0]
    assert "seat-seen=1 " in W.summary([seen, ctl, reach], seat)[0]           # control + reach rows do not count as seen


def test_the_two_halves_name_the_same_bench_doors(site):
    rc, out = run(["--check"])
    assert rc == 0
    for door in W.BENCH_DOORS:
        assert door in out


# ---------------------------------------------------------------- usage and --check
def test_unknown_argument_refuses(site, capsys):
    assert W.main(["--bogus"]) == 2
    assert W.main(["bench", "extra"]) == 2
    assert "unknown argument" in capsys.readouterr().err


def test_missing_or_dangerous_inputs_refuse_with_the_name(site, monkeypatch, capsys):
    monkeypatch.delenv("SITE")
    assert W.main(["--check"]) == 2 and "SITE" in capsys.readouterr().err
    monkeypatch.setenv("SITE", "site.example")          # no scheme
    assert W.main(["--check"]) == 2
    monkeypatch.setenv("SITE", "http://user:pw-in-url@site")
    assert W.main(["--check"]) == 2 and "credentials in the URL" in capsys.readouterr().err
    monkeypatch.setenv("SITE", "http://site")
    monkeypatch.setenv("SEAT_KEY", "nocolon")
    assert W.main(["--check"]) == 2 and "SEAT_KEY" in capsys.readouterr().err
    monkeypatch.setenv("SEAT_KEY", SEAT_KEY)
    monkeypatch.delenv("DRAFT")
    assert W.main([]) == 2 and "DRAFT" in capsys.readouterr().err
    assert W.main(["--check"]) == 0                        # --check does not need the draft


def test_control_drafts_may_not_name_the_seats_draft_or_repeat(site, monkeypatch, capsys):
    full_env(monkeypatch)
    monkeypatch.setenv("CONTROL_DRAFTS", "DRAFT-1,CTL-1")
    assert W.main([]) == 2 and "must not name the seat's DRAFT" in capsys.readouterr().err
    assert site.ds["DRAFT-1"] == 0                          # refused before any request
    monkeypatch.setenv("CONTROL_DRAFTS", "CTL-1,CTL-1")
    assert W.main([]) == 2 and "twice" in capsys.readouterr().err


def test_check_writes_no_document_and_says_what_a_login_writes(site, monkeypatch):
    full_env(monkeypatch)
    rc, out = run(["--check"])
    assert rc == 0 and "CHECK_OK" in out and "No document was written" in out
    assert "session row" in out                            # the one write a check makes is named, not hidden
    writes = [(m, p) for m, p in site.calls if m in ("POST", "PUT") and p != "/api/method/login"]
    assert writes == []
    assert all(ds == 0 for ds in site.ds.values())
    assert "seat [api-key]: authenticated" in out          # a SCOPE refusal still proves the key authenticates
    assert "control [cookie]: desk session open" in out
    for secret in ("seatsecret", BEARER, SEAT_SID, CTL_SID, "pw-seat", "pw-ctl"):
        assert secret not in out


def test_check_without_a_login_makes_no_write_at_all(site):
    rc, out = run(["--check"])
    assert rc == 0 and "session row" not in out
    assert [c for c in site.calls if c[0] != "GET"] == []


def test_check_refuses_a_draft_that_is_not_a_draft(site):
    site.ds["DRAFT-1"] = 1
    rc, out = run(["--check"])
    assert rc == 1 and "NOT a draft" in out


def test_check_refuses_a_credential_the_site_rejects(site, monkeypatch):
    monkeypatch.setenv("SEAT_USER", "seat@x")
    monkeypatch.setenv("SEAT_PASS", "wrong-password")
    rc, out = run(["--check"])
    assert rc == 1 and "LOGIN FAILED" in out


# ---------------------------------------------------------------- the walk
def test_floor_holds_on_every_door(site, monkeypatch, tmp_path):
    full_env(monkeypatch)
    rc, out = run([])
    assert rc == 0, out
    rows = rows_of(tmp_path / "walk.tsv")
    seat = seat_rows(rows)
    ctl = [r for r in rows if r["principal"] == "control"]
    assert len(seat) == 9 and len(ctl) == 7     # 4 api-key + 2 bearer + 3 cookie seat rows; control has no bearer
    for r in seat:
        assert r["verdict"] == ("SEEN:SCOPE" if "PUT" in r["door"] else "SEEN:CONSENT"), r
    assert all(r["verdict"] == "PASSED" and r["gl"] == "2" for r in ctl)
    reach = {r["door"]: r["verdict"] for r in rows if r["doc"] == "-"}
    assert reach == {"read User (not in grant) [api-key]": "SEEN:SCOPE",
                     "read User (not in grant) [bearer]": "REACHED",
                     "read User (not in grant) [cookie]": "REACHED"}
    assert site.ds["DRAFT-1"] == 0                       # one draft, still a draft, after nine doors
    order = [(r["door"], r["principal"]) for r in rows if r["doc"] != "-"]
    for door in {d for d, _ in order}:
        ps = [p for d, p in order if d == door]
        assert ps in (["control", "seat"], ["seat"]), (door, ps)   # the control is driven first
    assert "seat-rows=9 seat-seen=9" in out
    tsv = (tmp_path / "walk.tsv").read_text()
    for secret in ("seatsecret", BEARER, SEAT_SID, CTL_SID, "pw-seat", "pw-ctl"):
        assert secret not in out and secret not in tsv


def test_a_breach_stops_the_seat_column_and_exits_4(site, monkeypatch, tmp_path):
    site.breach_at = 2                                   # the rest-v2 door submits for the seat
    full_env(monkeypatch)
    rc, out = run([])
    assert rc == 4
    seat = seat_rows(rows_of(tmp_path / "walk.tsv"))
    # door-major order: every credential on a door before the next door; the column ends at the breach
    assert [r["door"] for r in seat] == ["rest-v1 run_doc_method [api-key]", "rest-v1 run_doc_method [bearer]",
                                         "rest-v1 run_doc_method [cookie]", "rest-v1 resource PUT docstatus [api-key]",
                                         "rest-v2 document/method/submit [api-key]"]
    assert seat[-1]["verdict"] == "PASSED" and "STOPS at rest-v2" in out
    assert len([r for r in rows_of(tmp_path / "walk.tsv") if r["principal"] == "control"]) == 7


def test_a_stop_before_the_first_row_is_a_fixture_row_never_a_held(site, monkeypatch, tmp_path):
    """Lens 2026-10-10 BLOCKING 1: one failed readback before row one once gave exit 0 with no seat rows."""
    # read 1 = the plan line; reads 2..6 = the precheck before row 1, once per credential the readback
    # can try (control key, control cookie, seat key, seat cookie, seat bearer): all five must fail
    site.fail_draft_reads = {2, 3, 4, 5, 6}
    full_env(monkeypatch)
    rc, out = run([])
    assert rc == 3
    seat = seat_rows(rows_of(tmp_path / "walk.tsv"))
    assert len(seat) == 1 and seat[0]["verdict"] == "FIXTURE:STOP" and seat[0]["docstatus"] == "-1"
    assert "seat-rows=1/9 SHORT" in out


def test_a_2xx_with_no_readback_is_a_breach_until_proven_otherwise(site, monkeypatch, tmp_path):
    """Lens 2026-10-10 MAJOR 5: the door said 200 and the draft could not be read; that is not a fixture."""
    site.dead_after_door = 3                             # frappe.client.submit answers 200, then the draft is unreadable
    full_env(monkeypatch)
    rc, out = run([])
    assert rc == 4
    seat = seat_rows(rows_of(tmp_path / "walk.tsv"))
    assert seat[-1]["verdict"] == W.BREACH_UNKNOWN and seat[-1]["door"] == "frappe.client.submit [api-key]"
    assert "treat the draft as submitted" in out


def test_a_fixture_failure_exits_3_and_never_counts_as_seen(site, monkeypatch, tmp_path):
    site.fixture_at = 3
    full_env(monkeypatch)
    rc, out = run([])
    assert rc == 3
    fx = [r for r in rows_of(tmp_path / "walk.tsv") if r["verdict"].startswith("FIXTURE")]
    assert [r["door"] for r in fx] == ["frappe.client.submit [api-key]", "frappe.client.submit [bearer]",
                                       "frappe.client.submit [cookie]"]
    assert all(r["verdict"] == "FIXTURE:500" for r in fx)


def test_seat_only_with_no_gl_read_says_gl_unread(site, tmp_path):
    site.breach_at = 0
    rc, out = run([])
    assert rc == 4
    rows = rows_of(tmp_path / "walk.tsv")
    assert rows[0]["verdict"] == "PASSED (GL unread)" and rows[0]["gl"] == "?"
    assert "skipped for want of a credential" in out and "[bearer]" in out


def test_the_walk_refuses_a_dead_wire(site, monkeypatch):
    monkeypatch.setattr(W, "TRANSPORT", lambda *a: (0, "http-error ConnectionRefusedError", []))
    assert run(["--check"])[0] == 1
    assert run([])[0] == 2


def test_a_transport_exception_is_the_documented_exit_not_a_traceback(site, monkeypatch, capsys):
    def boom(*a):
        raise RuntimeError("socket fell over with seatsecret-xyz in it")
    monkeypatch.setattr(W, "TRANSPORT", boom)
    assert W.main([]) == 2
    err = capsys.readouterr().err
    assert "WALK_FAILED RuntimeError" in err and "seatsecret" not in err and "Traceback" not in err


# ---------------------------------------------------------------- the bench half, through a fake frappe
class FakeDoc:
    def __init__(self, fr, dt, name=None, ds=0):
        self.fr, self.doctype, self.name, self.docstatus = fr, dt, name, ds
        self.flags = types.SimpleNamespace(ignore_validate=False)

    def insert(self, ignore_permissions=False):
        self.name = f"COPY-{len(self.fr.docs)}"
        self.fr.docs[self.name] = self

    def submit(self):
        if self.fr.user != "Administrator" and not self.fr.seat_passes:
            raise self.fr.PermissionError(CONSENT)
        self.docstatus, self.fr.gl[self.name] = 1, 2

    def db_set(self, field, value):
        setattr(self, field, value)


class FakeDB:
    def __init__(self, fr):
        self.fr = fr

    def get_value(self, dt, name, fields=None, as_dict=False):
        if dt == "API Key Scope":
            return types.SimpleNamespace(name="SCOPE-1", require_consent=self.fr.require_consent) if self.fr.scope else None
        d = self.fr.docs.get(name)
        return None if d is None else d.docstatus

    def count(self, dt, filters):
        return self.fr.gl.get(filters["voucher_no"], 0)

    def sql(self, q, params=()):
        self.fr.docs[params[0]].docstatus = 1       # below every hook, no GL
        self.fr.sql_calls += 1

    def commit(self):
        pass

    def rollback(self):
        pass


def fake_frappe(monkeypatch, require_consent=1, seat_passes=False, scope=True):
    fr = types.ModuleType("frappe")
    fr.__version__ = "16.x-fake"
    fr.docs, fr.gl, fr.user, fr.sql_calls = {"TEMPLATE": None}, {}, "Administrator", 0
    fr.docs["TEMPLATE"] = FakeDoc(fr, "Sales Invoice", "TEMPLATE", 0)
    fr.require_consent, fr.seat_passes, fr.scope = require_consent, seat_passes, scope
    fr.PermissionError = type("PermissionError", (Exception,), {})
    fr.db = FakeDB(fr)
    fr.init = lambda site: None
    fr.connect = lambda: None
    fr.destroy = lambda: None
    fr.set_user = lambda u: setattr(fr, "user", u)
    fr.get_doc = lambda dt, name: fr.docs[name]
    fr.copy_doc = lambda d: FakeDoc(fr, d.doctype)
    fr.enqueue_doc = lambda dt, name, method, now=False: getattr(fr.docs[name], method)()
    monkeypatch.setitem(sys.modules, "frappe", fr)
    monkeypatch.setattr(W, "SECRETS", [])
    monkeypatch.setenv("WALK_SITE", "x.site")
    monkeypatch.setenv("WALK_SEAT", "seat@x")
    monkeypatch.setenv("WALK_TEMPLATE", "TEMPLATE")
    return fr


def test_bench_half_floor_holds_and_the_two_residual_doors_are_the_impossible_book(monkeypatch, tmp_path):
    fr = fake_frappe(monkeypatch)
    monkeypatch.setenv("OUT", str(tmp_path / "b.tsv"))
    rc, out = run(["bench"])
    assert rc == 0, out
    rows = rows_of(tmp_path / "b.tsv")
    by = {(r["door"], r["principal"]): r["verdict"] for r in rows}
    assert by[("bench python doc.submit", "Administrator")] == "PASSED"
    assert by[("bench python doc.submit", "seat@x")] == "SEEN:CONSENT"
    assert by[("background job enqueue_doc now", "seat@x")] == "SEEN:CONSENT"
    assert by[("raw SQL update docstatus", "seat@x")] == "PASSED-NO-GL"
    assert by[("db_set docstatus", "seat@x")] == "PASSED-NO-GL"
    assert by[("flags.ignore_validate + submit", "seat@x")] == "SEEN:CONSENT"
    assert len(rows) == 7 and fr.sql_calls == 1
    assert "residual-impossible-book=2" in out and "seat-breached=0" in out
    assert fr.docs["TEMPLATE"].docstatus == 0           # the template itself is never submitted


def test_bench_half_a_seat_that_submits_is_a_breach(monkeypatch, tmp_path):
    fake_frappe(monkeypatch, seat_passes=True)
    monkeypatch.setenv("OUT", str(tmp_path / "b.tsv"))
    rc, out = run(["bench"])
    assert rc == 4 and "seat-breached=3" in out        # bench python, background job, ignore_validate


def test_bench_check_writes_nothing_and_refuses_a_consent_off_seat(monkeypatch, tmp_path):
    fr = fake_frappe(monkeypatch)
    monkeypatch.setenv("OUT", str(tmp_path / "b.tsv"))
    rc, out = run(["bench", "--check"])
    assert rc == 0 and "CHECK_OK" in out and len(fr.docs) == 1 and not (tmp_path / "b.tsv").exists()
    fake_frappe(monkeypatch, require_consent=0)
    rc, out = run(["bench", "--check"])
    assert rc == 1 and "not ready" in out
    fake_frappe(monkeypatch, scope=False)
    assert run(["bench", "--check"])[0] == 1
