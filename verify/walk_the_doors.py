#!/usr/bin/env python3
"""Walk the doors: drive a consent-gated seat through every door into the books, on YOUR site.

This is the proof behind one sentence in the README: the consent gate lives at the document
lifecycle, so it sees every door a write can reach the ledger by, not only the one the broker
opens. We first drove it on our own bench (2026-09-26, 42 rows, every door, no fixture rows). This
kit lets you drive it on yours and read the same table. If your rows differ from ours, that is the
useful outcome.

Two halves, one file:

  python3 verify/walk_the_doors.py --check     reach the site, authenticate, read the draft; WRITES NOTHING
  python3 verify/walk_the_doors.py             the HTTP half: every door reachable from outside the site
  <bench>/env/bin/python verify/walk_the_doors.py bench [--check]
                                               the bench half: the in-process doors, run ON the bench host
                                               as the bench user from <bench>/sites

Every input is an environment variable, never an argument, so no secret lands in a shell history or
a process list. SITE has no default: this kit sends your credential and performs a write probe.

  SITE            https://your-erpnext.example            required
  SEAT_KEY        api_key:api_secret of the CONSENT-GATED seat (API Key Scope, require_consent=1)   required
  DRAFT           the name of ONE draft (docstatus 0) of DOCTYPE the seat is allowed to act on     required for the walk
  DOCTYPE         default "Sales Invoice"
  SEAT_BEARER     an OAuth bearer token for the seat's user          optional: adds the bearer doors
  SEAT_USER/SEAT_PASS  the seat user's desk login                    optional: adds the cookie doors
  CONTROL_KEY, CONTROL_USER/CONTROL_PASS  a desk human with roles and NO grant   optional: the door-works control
  CONTROL_DRAFTS  comma-separated drafts, one per control row, consumed in order  optional
  READ_DOCTYPE    a doctype NOT in the seat's grant, default "User"   the scope-reach probe
  HOST_HEADER     a Host header, for a bench reached by address      optional
  OUT             the result table, default ./walk-result.tsv

The gated seat spends ONE draft for every door, because the floor holding means the draft stays a
draft. The moment any door submits it, the seat column stops at that row and the kit exits 4: the
floor was breached and the table says where. The control (a human with roles) is driven FIRST on
each door it has a draft for, so a refusal is never mistaken for a broken door.

Verdicts, per row: SEEN:CONSENT / SEEN:SCOPE / SEEN:RATE = the floor spoke in its own words and
docstatus stayed 0 · PASSED = docstatus 1 and GL rows posted · PASSED-NO-GL = docstatus 1 and no GL
(an impossible book) · PASSED (GL unread) = docstatus 1, GL not readable with the credentials given
· STOCK-PERM = frappe's own permission model refused · REACHED = a read outside the grant answered
200 · FIXTURE:<code> = something else broke and never counts as seen.

Exit: 0 every seat write row SEEN and no fixture rows · 3 fixture rows present · 4 the seat
submitted through some door · 2 usage or kit failure. --check: 0 ready · 1 not ready · 2 usage.

What this kit does NOT drive, and says so in its output: the server-script API door and the System
Console (both need site posture changes no outsider's kit should make), Data Import, the scheduler,
inbound integrations, backup restore. Our own lab drove the first two; the receipt is summarised in
VERIFY.md. What no hook can see: raw SQL and `db_set` below the lifecycle, which the bench half
drives on purpose so the table shows the impossible book they make, and which the reconciliation
(`pacioli close --reconcile`) is built to find.
"""
from __future__ import annotations

import http.cookies
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

CONSENT_WORDS = ("requires human consent", "consent (document layer)", "consent (preview)")
SCOPE_WORDS = ("this credential is scoped",)
RATE_WORDS = ("over its rate limit",)
TSV_COLS = ("door", "principal", "code", "gate", "docstatus", "gl", "verdict", "doc", "note")

# The in-process doors the bench half drives. The HTTP half prints this list under "not driven
# here", so the two halves agree by construction (a test holds them to it).
BENCH_DOORS = ("bench python doc.submit", "background job enqueue_doc now",
               "raw SQL update docstatus", "db_set docstatus", "flags.ignore_validate + submit")
NOT_DRIVEN = ("server-script API (site posture)", "System Console (site posture)", "Data Import",
              "scheduler (the background-job row's path)", "inbound integrations", "backup restore")

SECRETS: list[str] = []     # every secret value this process holds; scrub() removes them from any text


def scrub(text) -> str:
    t = str(text if text is not None else "")
    for v in SECRETS:
        if v and len(v) >= 6:
            t = t.replace(v, "[redacted]")
    return t


def say(*a) -> None:
    print(*(scrub(x) for x in a), flush=True)


def gate_of(code: int, body: str) -> str:
    if 200 <= code < 400:
        return "NONE"        # a 2xx/3xx is never a refusal, whatever words ride in its body
    b = (body or "").lower()
    if any(w in b for w in CONSENT_WORDS):
        return "CONSENT"
    if any(w in b for w in SCOPE_WORDS):
        return "SCOPE"
    if any(w in b for w in RATE_WORDS):
        return "RATE"
    if code == 401:
        return "AUTH"
    if code == 403 or "permissionerror" in b or "not permitted" in b:
        return "FRAPPE-PERM"
    return "ERR"


def verdict_of(code: int, gate: str, ds: int, gl) -> str:
    if ds == 1 and gl is None:
        return "PASSED (GL unread)"
    if ds == 1 and gl > 0:
        return "PASSED"
    if ds == 1 and gl == 0:
        return "PASSED-NO-GL"
    if gate in ("CONSENT", "SCOPE", "RATE") and ds == 0:
        return "SEEN:" + gate
    if gate == "FRAPPE-PERM" and ds == 0:
        return "STOCK-PERM"
    return f"FIXTURE:{code}"


def write_tsv(path: str, rows: list[dict]) -> None:
    with open(path, "w") as f:
        f.write("\t".join(TSV_COLS) + "\n")
        for r in rows:
            f.write("\t".join(scrub(r[k]).replace("\n", " ").replace("\t", " ") for k in TSV_COLS) + "\n")


BREACH_UNKNOWN = "BREACH-UNKNOWN"   # the seat's door answered 2xx and the draft could not be read back


def summary(rows: list[dict], seat: str, residual: tuple = (), planned: int | None = None) -> tuple[str, int]:
    """One line and the exit code. 4 (breach) beats 3 (fixture) beats 0.

    A seat row that PASSED on a door named in `residual` (raw SQL, db_set: below every hook, by the
    floor's own documentation) is counted as the impossible book it is, not as a breach of a claim the
    floor never made. `planned` is how many seat write rows the plan held: a column shorter than its
    plan is a fixture failure, never a HELD (lens 2026-10-10: a readback hiccup before row one once
    produced exit 0 with zero seat rows). `seen` counts the seat's own write rows only."""
    seat_writes = [r for r in rows if r["principal"] == seat and r["doc"] != "-"]
    seen = sum(1 for r in seat_writes if r["verdict"].startswith("SEEN"))
    fixture = sum(1 for r in rows if r["verdict"].startswith("FIXTURE"))
    short = planned is not None and len(seat_writes) < planned
    passed_seat = [r for r in seat_writes if r["verdict"].startswith("PASSED") or r["verdict"] == BREACH_UNKNOWN]
    breached = [r for r in passed_seat if r["door"] not in residual]
    passed = sum(1 for r in rows if r["verdict"].startswith("PASSED"))
    code = 4 if breached else (3 if (fixture or short) else 0)
    line = (f"WALK_DONE rows={len(rows)} seat-rows={len(seat_writes)}"
            f"{'/' + str(planned) + ' SHORT' if short else ''} seat-seen={seen} passed={passed} "
            f"fixture={fixture} seat-breached={len(breached)} "
            f"residual-impossible-book={len(passed_seat) - len(breached)} -> exit {code}")
    return line, code


# ------------------------------------------------------------------ the HTTP half
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect is an answer, not a path: following one would forward the Authorization header to
    whatever host the site named and turn a POST into a GET."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _urllib_transport(method: str, url: str, headers: dict, body: bytes | None):
    """(code, text, set_cookie_headers). An HTTP error IS an answer; only a dead wire is 0."""
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with _OPENER.open(req, timeout=60) as r:  # noqa: S310 (operator-named site)
            return r.status, r.read().decode("utf-8", "replace"), r.headers.get_all("Set-Cookie") or []
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace"), e.headers.get_all("Set-Cookie") or []
    except Exception as e:  # noqa: BLE001  -- a dead wire, named, never a refusal
        return 0, f"http-error {type(e).__name__}: {e}", []


TRANSPORT = _urllib_transport


class Principal:
    """One credential set. `cookies` is the desk session once login() ran."""

    def __init__(self, name: str, key: str | None = None, bearer: str | None = None,
                 user: str | None = None, pw: str | None = None):
        self.name, self.key, self.bearer, self.user, self.pw = name, key, bearer, user, pw
        self.cookies: dict[str, str] | None = None
        for v in (key, bearer, pw):
            if v:
                SECRETS.append(v)
        if key and ":" in key:
            SECRETS.append(key.split(":", 1)[1])

    def auth(self, kind: str) -> dict:
        if kind == "api-key" and self.key:
            return {"Authorization": "token " + self.key}
        if kind == "bearer" and self.bearer:
            return {"Authorization": "Bearer " + self.bearer}
        if kind == "cookie" and self.cookies:
            return {"Cookie": "; ".join(f"{k}={v}" for k, v in self.cookies.items())}
        return {}

    def has(self, kind: str) -> bool:
        return bool(self.auth(kind))


class Site:
    def __init__(self, base: str, host_header: str | None = None):
        self.base = base.rstrip("/")
        self.host_header = host_header
        self.calls: list[tuple[str, str]] = []

    def call(self, method: str, path: str, auth: dict, data=None) -> tuple[int, str, list]:
        h = {"Accept": "application/json", **auth}
        if self.host_header:
            h["Host"] = self.host_header
        body = None
        if data is not None:
            h["Content-Type"] = "application/json"
            body = json.dumps(data).encode()
        self.calls.append((method, path))
        code, text, set_cookies = TRANSPORT(method, self.base + path, h, body)
        return code, text, set_cookies

    def login(self, p: Principal) -> bool:
        if not (p.user and p.pw):
            return False
        code, text, set_cookies = self.call("POST", "/api/method/login", {}, {"usr": p.user, "pwd": p.pw})
        if code != 200:
            say(f"  login {p.name} ({p.user}): HTTP {code} {text[:120]!r}")
            return False
        jar: dict[str, str] = {}
        for sc in set_cookies:
            c = http.cookies.SimpleCookie()
            c.load(sc)
            for k, m in c.items():
                jar[k] = m.value
        p.cookies = jar or None
        if p.cookies:
            SECRETS.extend(v for v in jar.values() if len(v) >= 6)
        return bool(p.cookies)

    def get_doc(self, dt: str, name: str, auth: dict):
        code, text, _ = self.call("GET", f"/api/resource/{_q(dt)}/{_q(name)}", auth)
        if code != 200:
            return None, code, text
        try:
            return json.loads(text)["data"], code, text
        except (ValueError, KeyError):
            return None, code, text

    def count_gl(self, name: str, auth: dict):
        filters = urllib.parse.quote(json.dumps([["voucher_no", "=", name], ["is_cancelled", "=", 0]]))
        code, text, _ = self.call("GET", f"/api/resource/GL%20Entry?fields=%5B%22name%22%5D"
                                  f"&limit_page_length=1000&filters={filters}", auth)
        if code != 200:
            return None
        try:
            return len(json.loads(text)["data"])
        except (ValueError, KeyError):
            return None


def _q(s: str) -> str:
    return urllib.parse.quote(s, safe="")


def door_rest_v1_run_doc_method(site, dt, name, auth):
    return site.call("POST", "/api/method/run_doc_method", auth, {"dt": dt, "dn": name, "method": "submit"})[:2]


def door_rest_v1_resource_put(site, dt, name, auth):
    return site.call("PUT", f"/api/resource/{_q(dt)}/{_q(name)}", auth, {"docstatus": 1})[:2]


def door_rest_v2_document_method(site, dt, name, auth):
    return site.call("POST", f"/api/v2/document/{_q(dt)}/{_q(name)}/method/submit/", auth, {})[:2]


def door_frappe_client_submit(site, dt, name, auth):
    doc, code, text = site.get_doc(dt, name, auth)
    if doc is None:
        return code, f"fixture: GET of the draft before frappe.client.submit answered {code}: {text[:160]}"
    return site.call("POST", "/api/method/frappe.client.submit", auth, {"doc": json.dumps(doc)})[:2]


def door_desk_savedocs(site, dt, name, auth):
    doc, code, text = site.get_doc(dt, name, auth)
    if doc is None:
        return code, f"fixture: GET of the draft before savedocs answered {code}: {text[:160]}"
    return site.call("POST", "/api/method/frappe.desk.form.save.savedocs", auth,
                     {"doc": json.dumps(doc), "action": "Submit"})[:2]


# (door label, function, credential kinds it is driven with)
HTTP_DOORS = (
    ("rest-v1 run_doc_method", door_rest_v1_run_doc_method, ("api-key", "bearer", "cookie")),
    ("rest-v1 resource PUT docstatus", door_rest_v1_resource_put, ("api-key",)),
    ("rest-v2 document/method/submit", door_rest_v2_document_method, ("api-key",)),
    ("frappe.client.submit", door_frappe_client_submit, ("api-key", "bearer", "cookie")),
    ("desk savedocs Submit", door_desk_savedocs, ("cookie",)),
)


def env(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name)
    return v if v not in (None, "") else default


def usage(msg: str) -> int:
    print(f"walk_the_doors: {msg}\n\n{__doc__.split('Two halves', 1)[0].strip()}\n\n"
          "usage: walk_the_doors.py [--check] | walk_the_doors.py bench [--check]", file=sys.stderr)
    return 2


def plan_rows(seat: Principal, control: Principal | None, control_drafts: list[str]):
    """The rows this run will drive, in order: control first on each door, then the seat.
    Returns (rows, skipped) where skipped names the (door, kind) pairs no credential covers."""
    rows, skipped, drafts = [], [], list(control_drafts)
    for door, fn, kinds in HTTP_DOORS:
        for kind in kinds:
            label = f"{door} [{kind}]"
            if control is not None and control.has(kind) and drafts:
                rows.append((label, fn, kind, control, drafts.pop(0)))
            if seat.has(kind):
                rows.append((label, fn, kind, seat, None))
            else:
                skipped.append(label)
    return rows, skipped


class Readback:
    """docstatus and GL count, read with the first credential that can see them."""

    def __init__(self, site: Site, dt: str, auths: list[dict]):
        self.site, self.dt, self.auths = site, dt, auths

    def docstatus(self, name: str) -> int:
        for a in self.auths:
            doc, code, _ = self.site.get_doc(self.dt, name, a)
            if doc is not None:
                return int(doc.get("docstatus", -1))
        return -1

    def gl(self, name: str):
        for a in self.auths:
            n = self.site.count_gl(name, a)
            if n is not None:
                return n
        return None


def walk_http(check_only: bool) -> int:
    base, seat_key, draft = env("SITE"), env("SEAT_KEY"), env("DRAFT")
    if not base or not base.startswith(("http://", "https://")):
        return usage("set SITE=https://your-erpnext.example (no default: this kit sends your credential)")
    if "@" in urllib.parse.urlsplit(base).netloc:
        return usage("SITE must not carry credentials in the URL; they would be printed")
    if not seat_key or ":" not in seat_key:
        return usage("set SEAT_KEY=api_key:api_secret of the consent-gated seat")
    if not draft and not check_only:
        return usage("set DRAFT=<name of one docstatus-0 document of DOCTYPE> for the walk")
    dt, read_dt, out = env("DOCTYPE", "Sales Invoice"), env("READ_DOCTYPE", "User"), env("OUT", "walk-result.tsv")
    control_drafts = [d.strip() for d in (env("CONTROL_DRAFTS") or "").split(",") if d.strip()]
    if draft and draft in control_drafts:
        return usage("CONTROL_DRAFTS must not name the seat's DRAFT: the control would submit it and the "
                     "seat column would stop with nothing measured")
    if len(set(control_drafts)) != len(control_drafts):
        return usage("CONTROL_DRAFTS names the same draft twice; one draft per control row")
    site = Site(base, env("HOST_HEADER"))
    seat = Principal("seat", key=seat_key, bearer=env("SEAT_BEARER"), user=env("SEAT_USER"), pw=env("SEAT_PASS"))
    control = None
    if env("CONTROL_KEY") or (env("CONTROL_USER") and env("CONTROL_PASS")):
        control = Principal("control", key=env("CONTROL_KEY"), user=env("CONTROL_USER"), pw=env("CONTROL_PASS"))

    say(f"== walk the doors on {base} ({'CHECK, no writes' if check_only else 'WALK'}) "
        f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} ==")
    code, text, _ = site.call("GET", "/api/method/ping", {})
    if code != 200:
        say(f"XX the site did not answer ping: HTTP {code} {text[:120]!r}")
        return 1 if check_only else 2
    for p in (seat, control):
        if p is None:
            continue
        for kind in ("api-key", "bearer"):
            if p.has(kind):
                c, t, _ = site.call("GET", "/api/method/frappe.auth.get_logged_user", p.auth(kind))
                g = gate_of(c, t)
                # a SCOPE refusal still proves the credential authenticated: scope runs after auth
                ok = c == 200 or g == "SCOPE"
                say(f"  {p.name} [{kind}]: {'authenticated' if ok else 'NOT accepted'} (HTTP {c}, gate {g})")
                if not ok and check_only:
                    return 1
        if p.user and p.pw:
            say(f"  {p.name} [cookie]: {'desk session open' if site.login(p) else 'LOGIN FAILED'}")
            if not p.cookies and check_only:
                return 1
    auths = [a for a in (control.auth("api-key") if control else {}, control.auth("cookie") if control else {},
                         seat.auth("api-key"), seat.auth("cookie"), seat.auth("bearer")) if a]
    rb = Readback(site, dt, auths)
    rows_plan, skipped = plan_rows(seat, control, control_drafts)
    if draft:
        ds = rb.docstatus(draft)
        say(f"  draft {dt} {draft}: docstatus {ds}" + ("" if ds == 0 else "  <- NOT a draft; the walk refuses"))
        if ds != 0:
            return 1 if check_only else 2
    say(f"  plan: {len(rows_plan)} rows ({sum(1 for r in rows_plan if r[3] is seat)} seat, "
        f"{sum(1 for r in rows_plan if r[3] is not seat)} control); skipped for want of a credential: "
        f"{', '.join(skipped) or 'none'}")
    say(f"  not driven here: {', '.join(BENCH_DOORS)} (the bench half); {', '.join(NOT_DRIVEN)}")
    if check_only:
        logged_in = any(p is not None and p.cookies for p in (seat, control))
        say("CHECK_OK: ready. No document was written."
            + (" The desk logins above are the one write a check makes: frappe records a session row and "
               "the user's last_login." if logged_in else ""))
        return 0

    rows: list[dict] = []
    planned = sum(1 for r in rows_plan if r[3] is seat)
    seat_open = True

    def stop(label: str, verdict: str, c: int, ds: int, why: str) -> None:
        nonlocal seat_open
        rows.append(dict(door=label, principal=seat.name, code=c, gate="-", docstatus=ds, gl=0, verdict=verdict,
                         doc=draft, note=why))
        say(f"  seat column STOPS at {label}: {why}")
        seat_open = False

    for label, fn, kind, p, ctl_draft in rows_plan:
        name = ctl_draft if p is not seat else draft
        if p is seat and not seat_open:
            continue
        if p is seat:
            ds0 = rb.docstatus(draft)
            if ds0 != 0:
                stop(label, "FIXTURE:STOP", 0, ds0, f"the draft reads docstatus {ds0} before this door was driven")
                continue
        try:
            c, body = fn(site, dt, name, p.auth(kind))
        except Exception as e:  # noqa: BLE001
            c, body = 0, f"driver-error {type(e).__name__}: {e}"
        ds = rb.docstatus(name)
        if p is seat and 200 <= c < 300 and ds == -1:
            stop(label, BREACH_UNKNOWN, c, ds, "the door answered 2xx and the draft could not be read back; "
                 "treat the draft as submitted until you have looked")
            continue
        gl = rb.gl(name) if ds == 1 else 0
        g = gate_of(c, body)
        v = verdict_of(c, g, ds, gl)
        rows.append(dict(door=label, principal=p.name, code=c, gate=g, docstatus=ds, gl="?" if gl is None else gl,
                         verdict=v, doc=name, note=scrub(body)[:160]))
        say(f"  {label:38} {p.name:8} code={c:<4} gate={g:12} ds={ds} gl={rows[-1]['gl']:<3} {v}")
        if p is seat and ds == 1:
            say(f"  seat column STOPS at {label}: the floor was breached, the draft is submitted")
            seat_open = False

    say("-- scope reach: a doctype NOT in the grant, read as the seat --")
    for kind in ("api-key", "bearer", "cookie"):
        if not seat.has(kind):
            continue
        c, t, _ = site.call("GET", f"/api/resource/{_q(read_dt)}?limit_page_length=1", seat.auth(kind))
        g = gate_of(c, t)
        v = "SEEN:SCOPE" if g == "SCOPE" else ("REACHED" if c == 200 else f"OTHER:{c}")
        rows.append(dict(door=f"read {read_dt} (not in grant) [{kind}]", principal=seat.name, code=c, gate=g,
                         docstatus=-1, gl=0, verdict=v, doc="-", note=scrub(t)[:120]))
        say(f"  {rows[-1]['door']:38} {seat.name:8} code={c:<4} gate={g:12} {v}")

    write_tsv(out, rows)
    line, rc = summary(rows, seat.name, planned=planned)
    say(f"results: {out}\n== {line} ==")
    return rc


# ------------------------------------------------------------------ the bench half
def walk_bench(check_only: bool) -> int:
    """Runs ON the bench host as the bench user from <bench>/sites. frappe is imported here, never
    at module level, so the HTTP half and the tests need no bench."""
    site_name, seat_user, template = env("WALK_SITE"), env("WALK_SEAT"), env("WALK_TEMPLATE")
    if not (site_name and seat_user and template):
        return usage("bench half: set WALK_SITE=<site> WALK_SEAT=<the gated seat's user> "
                     "WALK_TEMPLATE=<a docstatus-0 document to copy per row>")
    dt, out = env("DOCTYPE", "Sales Invoice"), env("OUT", "walk-bench-result.tsv")
    import frappe  # noqa: PLC0415

    frappe.init(site=site_name)
    frappe.connect()
    frappe.set_user("Administrator")
    say(f"== walk the doors, bench half, on {site_name} frappe {frappe.__version__} "
        f"({'CHECK, no writes' if check_only else 'WALK'}) ==")
    scope = frappe.db.get_value("API Key Scope", {"user": seat_user}, ["name", "require_consent"], as_dict=True)
    say(f"  seat {seat_user}: {'API Key Scope ' + scope.name + ', require_consent=' + str(scope.require_consent) if scope else 'NO API Key Scope row'}")
    tds = frappe.db.get_value(dt, template, "docstatus")
    say(f"  template {dt} {template}: docstatus {tds}")
    if not scope or int(scope.require_consent or 0) != 1 or tds != 0:
        say("XX not ready: the seat must carry require_consent=1 and the template must be a draft")
        frappe.destroy()
        return 1 if check_only else 2
    say(f"  plan: {', '.join(BENCH_DOORS)} (Administrator control on the first two, then the seat on all)")
    if check_only:
        say("CHECK_OK: ready. Nothing was written.")
        frappe.destroy()
        return 0

    def fresh():
        frappe.db.commit()

    def draft() -> str:
        frappe.set_user("Administrator")
        d = frappe.copy_doc(frappe.get_doc(dt, template))
        d.insert(ignore_permissions=True)
        fresh()
        return d.name

    def readback(name):
        fresh()
        ds = frappe.db.get_value(dt, name, "docstatus")
        return int(ds if ds is not None else -1), int(frappe.db.count("GL Entry", {"voucher_no": name, "is_cancelled": 0}))

    def run(fn, user, name):
        frappe.set_user(user)
        try:
            fn(name)
            frappe.db.commit()
            return 200, "ok"
        except Exception as e:  # noqa: BLE001
            frappe.db.rollback()
            return (403 if isinstance(e, frappe.PermissionError) else 500), f"{type(e).__name__}: {str(e)[:300]}"
        finally:
            frappe.set_user("Administrator")

    def f_bench_python(name):
        frappe.get_doc(dt, name).submit()

    def f_background_job(name):
        frappe.enqueue_doc(dt, name, "submit", now=True)

    def f_raw_sql(name):
        frappe.db.sql(f"update `tab{dt}` set docstatus=1 where name=%s", (name,))

    def f_db_set(name):
        frappe.get_doc(dt, name).db_set("docstatus", 1)

    def f_ignore_validate(name):
        d = frappe.get_doc(dt, name)
        d.flags.ignore_validate = True
        d.submit()

    fns = dict(zip(BENCH_DOORS, (f_bench_python, f_background_job, f_raw_sql, f_db_set, f_ignore_validate)))
    rows: list[dict] = []
    for door in BENCH_DOORS:
        principals = ("Administrator", seat_user) if door in BENCH_DOORS[:2] else (seat_user,)
        for user in principals:
            name = draft()
            c, body = run(fns[door], user, name)
            ds, gl = readback(name)
            g = gate_of(c, body)
            v = verdict_of(c, g, ds, gl)
            rows.append(dict(door=door, principal=user, code=c, gate=g, docstatus=ds, gl=gl, verdict=v, doc=name,
                             note=body[:160]))
            say(f"  {door:38} {user:28} code={c:<4} gate={g:12} ds={ds} gl={gl:<3} {v}")
    write_tsv(out, rows)
    line, rc = summary(rows, seat_user, residual=BENCH_DOORS[2:4])
    say(f"results: {out}\n== {line} ==")
    frappe.destroy()
    return rc


def main(argv: list[str]) -> int:
    args = list(argv)
    bench = bool(args) and args[0] == "bench"
    if bench:
        args.pop(0)
    check = "--check" in args
    if check:
        args.remove("--check")
    if args:
        return usage(f"unknown argument {args[0]!r}")
    try:
        return walk_bench(check) if bench else walk_http(check)
    except Exception as e:  # noqa: BLE001  -- the documented exit, with the cause scrubbed, never a traceback
        print(scrub(f"WALK_FAILED {type(e).__name__}: {e}"), file=sys.stderr, flush=True)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
