"""The receipt: a pasteable artifact the OPERATOR chooses to share.

The whole point of this module is the inversion of telemetry. Nothing is transmitted. `render` is
a pure function from doctor findings to text, and the human decides whether that text ever leaves
their machine. So the tests that matter most are not "does it format nicely" — they are:

  1. it CANNOT reach the network (no transport, no sockets, pure)
  2. it redacts the operator's infrastructure BEFORE rendering, because a receipt that leaks a
     hostname is worse than no receipt at all
  3. the findings survive redaction, or the artifact is useless to the person receiving it
"""

import pytest

from pacioli.receipt import WHERE, fingerprint, redact, render


# --- 1. redaction: the operator's infrastructure never survives ----------------------------------

@pytest.mark.parametrize("raw, must_not_contain", [
    ("base_url: https://erp.acme-corp.example:8443/", "acme-corp"),
    ("reached bench at 192.0.2.10", "192.0.2.10"),
    ("credential user agentready@bench.example", "agentready@bench.example"),
    ("secret file /opt/example/secrets/bench.secret", "/opt/example/secrets"),
    ("token 7f3a9c1e5b2d8a4f6c0e9b7d3a1f5c8e2b4d6a0f", "7f3a9c1e5b2d8a4f6c0e9b7d3a1f5c8e2b4d6a0f"),
])
def test_redact_removes_operator_infrastructure(raw, must_not_contain):
    assert must_not_contain not in redact(raw)


def test_redact_leaves_a_marker_not_a_hole():
    """A silently deleted host reads as 'there was nothing here'. Say something was removed."""
    out = redact("base_url: https://erp.acme-corp.example:8443/")
    assert "[redacted-" in out


def test_redaction_survives_into_the_rendered_receipt():
    """The end-to-end property. Redaction that only works on the helper is not redaction."""
    body = render(
        [("fail", "base_url: https://erp.acme-corp.example unreachable from 192.0.2.10")],
        version="0.33.3",
        generated_at="2026-07-27T03:00:00Z",
    )
    assert "acme-corp" not in body
    assert "192.0.2.10" not in body


# --- 2. the artifact stays USEFUL ----------------------------------------------------------------

def test_findings_survive_redaction():
    """Redacting the host must not redact the diagnosis. Otherwise nobody can act on it."""
    body = render(
        [("fail", "Sales Invoice submit refused: no consent marker")],
        version="0.33.3", generated_at="2026-07-27T03:00:00Z",
    )
    assert "no consent marker" in body
    assert "Sales Invoice" in body


def test_counts_are_stated_so_the_reader_sees_shape_first():
    body = render(
        [("ok", "a"), ("ok", "b"), ("warn", "c"), ("fail", "d")],
        version="0.33.3", generated_at="2026-07-27T03:00:00Z",
    )
    assert "1 fail" in body and "1 warn" in body and "2 ok" in body


def test_version_is_in_the_receipt():
    """A finding without the version it came from cannot be reproduced or fixed."""
    body = render([("ok", "x")], version="0.33.3", generated_at="2026-07-27T03:00:00Z")
    assert "0.33.3" in body


def test_receipt_says_the_human_chose_to_share_it():
    """The consent claim is made IN the artifact, so the receiver knows it was not harvested."""
    body = render([("ok", "x")], version="0.33.3", generated_at="2026-07-27T03:00:00Z")
    assert "chose to share" in body.lower() or "nothing was transmitted" in body.lower()


# --- 3. the fingerprint --------------------------------------------------------------------------

def test_fingerprint_is_stable_for_the_same_findings():
    f = [("fail", "x"), ("ok", "y")]
    assert fingerprint(f) == fingerprint(list(f))


def test_fingerprint_changes_when_a_finding_changes():
    assert fingerprint([("fail", "x")]) != fingerprint([("fail", "z")])


def test_fingerprint_is_computed_on_REDACTED_text():
    """Two operators with the same defect on different hosts must produce the SAME fingerprint,
    or the fingerprint leaks the host it was supposed to hide and cannot group reports."""
    a = fingerprint([("fail", "unreachable at 192.0.2.10")])
    b = fingerprint([("fail", "unreachable at 198.51.100.4")])
    assert a == b


# --- 4. it cannot phone home ---------------------------------------------------------------------

def test_module_imports_no_network_client():
    """Structural, not behavioural: the module must not even be ABLE to transmit. A comment
    promising it does not phone home is not a guarantee; an absent import is closer to one."""
    import pacioli.receipt as mod
    src = open(mod.__file__, encoding="utf-8").read()
    for banned in ("urllib", "socket", "http.client", "requests", "httpx", "subprocess"):
        assert banned not in src, f"receipt.py must not import {banned}"


def test_render_signature_takes_no_transport():
    """The doctor takes a transport. The receipt must not — that is the whole design."""
    import inspect
    assert "transport" not in inspect.signature(render).parameters


# --- 6. it is a LOOP, not a dead end -------------------------------------------------------------

def test_receipt_names_where_it_can_go():
    """The first version of this module emitted an artifact with nowhere to send it, which is a
    dead end with good formatting rather than a loop. The destination is part of the artifact."""
    body = render([("ok", "x")], version="0.33.3", generated_at="2026-07-27T03:00:00Z")
    assert WHERE in body


def test_the_destination_survives_redaction():
    """Our own URL contains a hostname. If the redactor eats it, the loop silently dies and the
    receipt goes back to being a dead end — with every test above still passing."""
    body = render([("ok", "x")], version="0.33.3", generated_at="2026-07-27T03:00:00Z")
    assert "github.com" in body, "the redactor ate the destination"
    assert "receipt.yml" in body


def test_the_issue_template_the_receipt_points_at_reaches_the_PUBLIC_repo():
    """A destination that 404s is worse than none: it spends the operator's goodwill and returns
    nothing.

    The first version of this test checked the file existed ON DISK — and passed while the template
    was untracked and therefore absent from the curated public tree, so the URL in every receipt
    would have 404'd. Existing locally is a STAND-IN for being published; they are not the same
    property, and the stand-in is the one that lies. This checks the real thing: that the path is
    tracked by git AND that the leak-audit partition keeps it rather than stripping it.
    """
    import subprocess
    from pathlib import Path

    repo = Path(__file__).resolve().parents[3]
    rel = ".github/ISSUE_TEMPLATE/receipt.yml"
    assert "template=" + Path(rel).name in WHERE

    tracked = subprocess.run(["git", "ls-files", "--error-unmatch", rel],
                             cwd=repo, capture_output=True, text=True)
    assert tracked.returncode == 0, f"{rel} is not tracked by git — it cannot reach the mirror"

    import sys
    sys.path.insert(0, str(repo / "scripts"))
    from release_leak_audit import partition_paths
    kept, stripped = partition_paths([rel])
    assert kept == [rel] and not stripped, f"{rel} would be stripped from the public tree"


# --- 5. the mirror property ----------------------------------------------------------------------

def test_findings_out_captures_every_line_doctor_prints():
    """The receipt must be a FAITHFUL mirror of the report, not a subset.

    Caught for real on 2026-07-27: the `base_url:` line was appended straight to `lines` and
    bypassed the structured channel, so the receipt was 66 findings against doctor's 67. It looked
    clean — but clean by OMISSION, because that one skipped line is the only one carrying the
    operator's hostname. A redactor that is never handed the secret has not been proven to redact
    it. This test fails if any future line takes the same shortcut.
    """
    from pacioli.doctor import _PREFIX, run_doctor

    out = []
    _code, lines = run_doctor({"PACIOLI_REGISTRY": "/nonexistent/registry.toml"},
                              offline=True, findings_out=out)
    printed = [ln for ln in lines if any(ln.startswith(p) for p in _PREFIX.values())]
    assert len(printed) == len(out), (
        f"doctor printed {len(printed)} findings but only {len(out)} reached findings_out — "
        "a line is bypassing the structured channel"
    )


# --- 7. ported back from Proximo, where the same design failed on real data -----------------------

def test_any_absolute_path_is_redacted_not_only_credential_shaped_ones():
    """The original rule only matched paths containing credential/secret/.ssh. A CA-bundle or
    state-db path under the operator's home is just as much a map of their machine, and the
    Proximo build of this module emitted exactly that off a live cluster on 2026-07-28."""
    assert "/home/op/.config/app" not in redact("ca_bundle: /home/op/.config/app/leaf.pem")
    assert "[redacted-path]" in redact("state db /var/lib/pacioli/state.db")


def test_an_ip_based_url_is_redacted_WHOLE_not_mangled():
    """Ordering regression, found in Proximo and present here unnoticed: with the address rule
    before the url rule, an ip-based base_url produced `https://[redacted-host]]:8080/api` — the
    port and path surviving, with a doubled bracket announcing it. Our own end-user run missed it
    because that bench's base_url is a hostname, not an address. One dataset is not real data."""
    out = redact("base_url: https://192.0.2.10:8080/api/resource/X")
    assert "192.0.2.10" not in out
    assert "8080" not in out, "port and path must go with the host"
    assert "/api/resource/X" not in out
    assert "]]" not in out, "rule ordering produced a mangled marker"
