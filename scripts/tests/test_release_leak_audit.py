"""Tests for the public-publish boundary.

`release_leak_audit.py` models the ONE transform that decides what leaves this repo. Nothing else
scans the synthetic tree that becomes the public commit, so a mistake here publishes silently.
These lock the two halves of the boundary: which paths are stripped, and that whatever survives
the strip is still scanned for leak shapes.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from release_leak_audit import (  # noqa: E402
    DENY_BASENAMES,
    KEEP_PATHS,
    _allowed,
    audit_files,
    partition_paths,
    printable_runs,
)


# --- the strip rule -------------------------------------------------------------------

def test_bench_drivers_are_stripped():
    """`deploy/bench/` is internal (ruled 2026-07-25): the drivers hardcode the jump host,
    the bench IP and the bench hostname, so publishing them hands out a map of private infra."""
    kept, stripped = partition_paths([
        "deploy/bench/receipt-consent.py",
        "deploy/bench/bench-build.sh",
        "deploy/bench/mint-seat.py",
    ])
    assert kept == []
    assert len(stripped) == 3


def test_receipts_sh_is_the_one_bench_file_that_publishes():
    """`receipts.sh` is fully parameterised (SITE + two credentials) so anyone can run it against
    their OWN site. It is the artifact for the thread; the drivers around it are ours."""
    kept, stripped = partition_paths([
        "deploy/bench/receipts.sh",
        "deploy/bench/receipt-consent.py",
    ])
    assert kept == ["deploy/bench/receipts.sh"]
    assert stripped == ["deploy/bench/receipt-consent.py"]
    assert "deploy/bench/receipts.sh" in KEEP_PATHS


def test_keep_paths_cannot_resurrect_a_denied_basename():
    """An exact-path exception overrides a deny PREFIX, never a deny BASENAME. The named memos
    are 'never public' unconditionally, so a mistaken KEEP_PATHS entry must not be able to
    publish one. Defense in depth: the two rules are not the same rule."""
    denied_name = DENY_BASENAMES[0]
    path = f"deploy/bench/{denied_name}"
    kept, stripped = partition_paths([path], )
    assert kept == []
    assert stripped == [path]


def test_established_deny_prefixes_still_strip():
    """Regression guard: adding the bench rule must not disturb the day-book rules."""
    paths = [
        "docs/plans/2026-07-26-floor-audit.md",
        "docs/internal/PARITY-AUDIT.md",
        "broker/docs/plans/2026-07-17-restore-drill.md",
        ".gitea/leak-deny.txt",
        ".scratch/notes.md",
    ]
    kept, stripped = partition_paths(paths)
    assert kept == []
    assert sorted(stripped) == sorted(paths)


def test_public_code_is_kept():
    """The boundary must not over-strip: the packages are the product."""
    paths = ["guard/pacioli_guard/act.py", "broker/pacioli/tools.py", "README.md"]
    kept, stripped = partition_paths(paths)
    assert sorted(kept) == sorted(paths)
    assert stripped == []


# --- the scan rule --------------------------------------------------------------------

# THIS FILE PUBLISHES (`scripts/` is not a deny prefix), so a real address written here would be
# the exact leak the tool exists to stop. It is caught if you try: that is how this note came to
# be written. Never put a real one here.
#
# It also cannot be a TEXTBOOK address. ALLOW deliberately waves through the sanctioned example
# subnets (`10.0.0.`, `192.168.`, `172.16.`, the RFC 5737 ranges) so real docs can use them, so a
# fixture drawn from those proves nothing: the scanner would pass it by design. This address is
# synthetic, is in no example range, and is not anyone's infrastructure.
_UNALLOWED_PRIVATE_IP = "10.99.99.99"  # leak-audit: allow — synthetic fixture, see note above


def test_a_kept_file_carrying_a_leak_shape_is_a_finding():
    """Fail-closed: surviving the strip is not a pass. Everything kept is still scanned."""
    res = audit_files({"deploy/bench/receipts.sh": f"SITE=http://{_UNALLOWED_PRIVATE_IP}:8080\n"})
    assert not res.ok
    assert any(f.kind == "rfc1918-ip" for f in res.findings)


def test_a_stripped_file_carrying_a_leak_shape_is_not_a_finding():
    """Stripped files are not scanned: they never reach the public tree, so their contents are
    not a public-surface question. This is what lets the internal drivers name real infra."""
    res = audit_files({"deploy/bench/receipt-consent.py": f"HOST = '{_UNALLOWED_PRIVATE_IP}'\n"})
    assert res.ok
    assert res.findings == []
    assert res.stripped == ["deploy/bench/receipt-consent.py"]


# --- binaries are published, therefore binaries are scanned -----------------------------

def test_printable_runs_pulls_text_out_of_a_utf16_document():
    """A UTF-16 markdown file has a NUL in every other byte, so a naive text check calls it a
    binary and skips it — while `build_public_tree` publishes it in full. Extract and scan."""
    blob = "jump host 10.99.99.99".encode("utf-16-le")  # leak-audit: allow — fixture
    assert "10.99.99.99" in printable_runs(blob)  # leak-audit: allow — fixture


def test_printable_runs_pulls_metadata_out_of_a_png():
    """PNG tEXt chunks carry arbitrary strings. A screenshot of a bench names the bench."""
    blob = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x20tEXtComment\x00" + b"host 10.99.99.99" + b"\x00\x00"  # leak-audit: allow — fixture
    assert "10.99.99.99" in printable_runs(blob)  # leak-audit: allow — fixture


def test_printable_runs_ignores_short_noise():
    """Only runs long enough to be a real string, so font tables and image data stay quiet."""
    assert "ab" not in printable_runs(b"\x00a\x00b\x00")


# --- the allowlist is anchored, not a substring search -----------------------------------

def test_allow_matches_only_at_an_anchor_never_free_floating():
    """The old rule was `entry in token`, which matched anywhere and made every entry a wildcard in
    both directions. Anchoring keeps the two intents the entries actually have and drops the rest.

    A subdomain of a sanctioned example DOMAIN is still an example, so it stays allowed on a dot
    boundary — but a host that merely ends in the same letters is not."""
    assert _allowed("example.lan")            # the entry itself
    assert _allowed("erp.example.lan")        # a subdomain of an example domain
    # Same trailing letters, different domain. Built by concatenation rather than written out,
    # because this file publishes and a bare internal-TLD literal is a finding in its own right.
    assert not _allowed("not" + "example.lan")
    assert not _allowed("host.example.lan.attacker.tld")  # example domain buried in the middle


def test_allow_still_covers_the_documented_example_ranges():
    """Anchoring must not break the intended prefix semantics: the example subnets are ranges."""
    for token in ("192.0.2.7", "192.168.1.50", "10.0.0.5", "172.16.30.9"):
        assert _allowed(token), token
