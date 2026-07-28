"""The receipt — the inverse of telemetry.

Pacioli's whole argument is that an agent should not be able to act without leaving a record a human
can read. This module points the same idea at the vendor: it turns a `doctor` run into one pasteable
artifact, and then it stops. **Nothing here transmits.** `render` is a pure function from findings to
text; whether that text ever leaves the operator's machine is the operator's decision, made by hand,
after reading it.

That constraint is enforced structurally rather than promised in prose: this module imports no
network client and `render` takes no transport, and there is a test pinning both. A comment saying
"we don't phone home" is worth nothing; an import that does not exist is worth something.

WHY THIS EXISTS. On 2026-07-27 an external evaluator hit three 403s on a credential our own records
described as proven. The seat's scope listed five methods and the enforcement honored zero, and it
had been that way since the day it was minted. Nothing was broken that a diagnostic could not have
shown on day one — but the refusals happened in a room nobody was watching, and the only reason we
found out is that one generous stranger took the trouble to write. This is the channel that should
not have depended on his generosity.

REDACTION IS THE PRECONDITION, NOT A FEATURE. An operator will paste this into a forum thread or an
email. A receipt that carries their hostname, their bench IP, their seat's email address or a path
into their credential store is worse than no receipt, because it converts a helpful act into a
disclosure. So redaction runs BEFORE rendering, on every line, and the fingerprint is computed on
the redacted text — otherwise two operators hitting the identical defect on different hosts would
produce different fingerprints, and the identifier meant to group reports would instead leak the
thing it was hiding.
"""
import hashlib
import re

__all__ = ["redact", "fingerprint", "render", "WHERE"]

# The destination is IN the artifact on purpose. A diagnostic that does not say where it can go is
# not a loop, it is a dead end with good formatting — which is exactly what the first version of
# this module was. Naming the destination is also the honest half of the consent claim above: the
# operator is told what sharing would mean before deciding, rather than after.
WHERE = "https://github.com/john-broadway/pacioli/issues/new?template=receipt.yml"

# Order matters: the most specific shapes first, so a credential path is not half-eaten by the
# host rule and an email is not shredded into a hostname. Each pattern leaves a MARKER — a silently
# deleted value reads as "there was nothing here", which is its own small lie.
_RULES = (
    # A long hex run is a token or a key. Do this first: it is the highest-cost leak and the
    # least likely to be load-bearing for the diagnosis.
    (re.compile(r"\b[0-9a-fA-F]{24,}\b"), "[redacted-secret]"),
    # Anything under a credentials/secrets directory, path and filename both.
    (re.compile(r"(?:/[\w.\-]*(?:credential|secret|\.ssh)[\w.\-]*)+(?:/[\w.\-]+)*"),
     "[redacted-credential-path]"),
    (re.compile(r"\b[\w.+\-]+@[\w\-]+(?:\.[\w\-]+)+"), "[redacted-user]"),
    # ANY absolute path, not only credential-shaped ones. Added 2026-07-28 after the same module
    # written for Proximo emitted a CA-bundle path whose filename carried the operator's hardware
    # model: a path names the operator's layout whether or not the word "secret" appears in it.
    (re.compile(r"(?<![\w.])/(?:etc|root|home|var|opt|srv|mnt|usr|tmp)(?:/[\w.@%+\-]+)+"),
     "[redacted-path]"),
    # The WHOLE url, BEFORE the address rule. Ordering is load-bearing: with the address rule first,
    # an ip-based base_url became `https://[redacted-ip]:8080/api` and this rule then matched only
    # the fragment up to the bracket, emitting `https://[redacted-host]]:8080/api` — port and path
    # still leaking, with a doubled bracket announcing the bug.
    (re.compile(r"\bhttps?://[^\s,;)\]'\"]+"), "[redacted-url]"),
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}(?:/\d{1,2})?\b"), "[redacted-ip]"),
    # A bare dotted hostname that survived the URL rule.
    (re.compile(r"\b(?:[\w\-]+\.)+(?:com|net|org|io|dev|lan|local|internal|example)\b"),
     "[redacted-host]"),
)


def redact(text):
    """Strip the operator's infrastructure out of one line, leaving a marker in its place."""
    out = str(text)
    for pattern, marker in _RULES:
        out = pattern.sub(marker, out)
    return out


def fingerprint(findings):
    """A short stable id for a set of findings, computed on the REDACTED text.

    Same defect on two different benches => same fingerprint. That is the point: it lets two
    operators discover they are hitting the same thing without either of them revealing where.
    """
    joined = "\n".join(f"{level}:{redact(message)}" for level, message in findings)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:12]


_ORDER = ("fail", "warn", "ok")
_GLYPH = {"fail": "XX", "warn": "!!", "ok": "ok"}


def render(findings, *, version, generated_at, target_count=1):
    """Render findings into the pasteable artifact. Pure: no I/O, no clock, no transport.

    `generated_at` and `version` are passed IN rather than read here, so the same findings always
    render identically and the caller owns every fact the receipt asserts.
    """
    findings = [(lvl, redact(msg)) for lvl, msg in findings]
    counts = {lvl: sum(1 for f_lvl, _ in findings if f_lvl == lvl) for lvl in _ORDER}
    fp = fingerprint(findings)

    head = [
        "pacioli receipt",
        f"  version      {version}",
        f"  generated    {generated_at}",
        f"  targets      {target_count}",
        f"  fingerprint  {fp}",
        f"  summary      {counts['fail']} fail · {counts['warn']} warn · {counts['ok']} ok",
        "",
        "Hostnames, addresses, URLs, user identifiers and filesystem paths were removed before",
        "this text was written. Nothing was transmitted: the operator ran a local, read-only check",
        "and chose to share the result. The fingerprint is computed on the redacted text, so an",
        "identical defect on a different system produces an identical fingerprint.",
        "",
    ]

    body = []
    for level in _ORDER:
        rows = [msg for lvl, msg in findings if lvl == level]
        if not rows:
            continue
        body.append(f"{level.upper()} ({len(rows)})")
        body.extend(f"  {_GLYPH[level]}  {msg}" for msg in rows)
        body.append("")

    foot = [
        "",
        "-" * 78,
        "Where this can go, if you want it to:",
        f"  {WHERE}",
        "Paste it whole. The fingerprint above lets an identical defect on someone else's system",
        "be recognised as the same one, which is the only reason it exists.",
    ]
    return "\n".join(head + body + foot).rstrip() + "\n"
