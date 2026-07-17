# Copyright (c) 2026, John Broadway and contributors
# License: Apache-2.0
"""Bench-free tests for the off-box PROVE anchor — the pure record/compare core (pacioli.anchor)
and the `pacioli anchor write` / `pacioli anchor check` CLI glue.

Run: `python3 -m unittest pacioli.tests.test_anchor` from the broker app root. No frappe required.
"""
import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from pacioli.anchor import compare, make_anchor, parse_anchor, render_anchor
from pacioli.cli import cmd_anchor_check, cmd_anchor_write
from pacioli.prove import GENESIS, append
from pacioli.runtime import open_store

KEY = b"seal-key-lives-off-box"


def _chain(n, key=KEY):
    """A chain of n intent receipts."""
    receipts, prev = [], None
    for i in range(n):
        prev = append(key, prev, "intent", {"n": i}, ts=f"2026-07-02T00:00:{i:02d}Z")
        receipts.append(prev)
    return receipts


def _hmacs(receipts):
    return [r.hmac for r in receipts]


class TestRecordRoundTrip(unittest.TestCase):
    def test_make_render_parse_round_trips(self):
        chain = _chain(3)
        rec = make_anchor("prod", chain[-1].hmac, 3, "2026-07-02T12:00:00Z")
        self.assertEqual(parse_anchor(render_anchor(rec)), rec)

    def test_rendered_form_is_one_stable_json_line(self):
        # off-box copies get compared/committed verbatim — the bytes must be deterministic
        rec = make_anchor("prod", _chain(1)[-1].hmac, 1, "t")
        text = render_anchor(rec)
        self.assertTrue(text.endswith("\n"))
        self.assertEqual(text.count("\n"), 1)
        self.assertEqual(render_anchor(parse_anchor(text)), text)

    def test_empty_chain_anchors_genesis(self):
        rec = make_anchor("prod", GENESIS, 0, "t")
        self.assertEqual(rec["count"], 0)

    def test_make_validates_as_strictly_as_parse(self):
        # a bad pin must be refused at WRITE time, not discovered at check time
        with self.assertRaises(ValueError):
            make_anchor("prod", "not-a-head", 1, "t")


class TestParseRefusesMalformed(unittest.TestCase):
    """A malformed anchor must read as 'cannot check', never as 'ok'. Every shape violation
    raises with a reason; nothing is coerced."""

    def _good(self):
        return make_anchor("prod", _chain(1)[-1].hmac, 1, "t")

    def test_non_json_refused(self):
        with self.assertRaises(ValueError):
            parse_anchor("not json at all")

    def test_non_object_refused(self):
        with self.assertRaises(ValueError):
            parse_anchor("[1, 2, 3]")

    def test_missing_format_marker_refused(self):
        rec = dict(self._good())
        del rec["pacioli_anchor"]
        with self.assertRaises(ValueError):
            parse_anchor(json.dumps(rec))

    def test_wrong_format_version_refused(self):
        rec = dict(self._good(), pacioli_anchor=2)
        with self.assertRaises(ValueError):
            parse_anchor(json.dumps(rec))

    def test_missing_field_refused(self):
        rec = dict(self._good())
        del rec["count"]
        with self.assertRaises(ValueError):
            parse_anchor(json.dumps(rec))

    def test_unknown_field_refused(self):
        rec = dict(self._good(), extra="x")
        with self.assertRaises(ValueError):
            parse_anchor(json.dumps(rec))

    def test_negative_count_refused(self):
        rec = dict(self._good(), count=-1)
        with self.assertRaises(ValueError):
            parse_anchor(json.dumps(rec))

    def test_bool_count_refused(self):
        # bool is an int subclass Python would happily accept — the anchor must not
        rec = dict(self._good(), count=True)
        with self.assertRaises(ValueError):
            parse_anchor(json.dumps(rec))

    def test_non_hex_head_refused(self):
        rec = dict(self._good(), head="z" * 64)
        with self.assertRaises(ValueError):
            parse_anchor(json.dumps(rec))

    def test_short_head_refused(self):
        rec = dict(self._good(), head="ab12")
        with self.assertRaises(ValueError):
            parse_anchor(json.dumps(rec))

    def test_empty_target_refused(self):
        rec = dict(self._good(), target="")
        with self.assertRaises(ValueError):
            parse_anchor(json.dumps(rec))

    def test_count_zero_with_non_genesis_head_refused(self):
        rec = dict(self._good(), count=0)  # head is a real hmac
        with self.assertRaises(ValueError):
            parse_anchor(json.dumps(rec))

    def test_nonzero_count_with_genesis_head_refused(self):
        rec = dict(self._good(), head=GENESIS)  # count is 1
        with self.assertRaises(ValueError):
            parse_anchor(json.dumps(rec))

    def test_bool_version_refused(self):
        # F4 (correctness redteam 2026-07-15): bool is an int subclass, so JSON `true` parses to
        # Python `True`, and `True == 1` / `True in _SUPPORTED_VERSIONS` are both true — without
        # an explicit bool guard on the version marker (the module already has one for counts),
        # {"pacioli_anchor": true, ...} would silently pass as a v1 record.
        rec = dict(self._good(), pacioli_anchor=True)
        with self.assertRaises(ValueError):
            parse_anchor(json.dumps(rec))


class TestAnchorV2SealFields(unittest.TestCase):
    """Pin format v2 -- ``seal_head``/``seal_count`` riding alongside the receipt head, so one
    off-box pin covers both the PROVE chain and the seal history. ``parse_anchor`` must accept
    BOTH a v1 pin (no seal fields -- pre-0.21.0) and a v2 pin (seal fields required together);
    a v1 pin is never an error, and a malformed/partial seal-field pair is always refused loudly,
    never treated as v1."""

    SEAL_HEAD = "b" * 64

    def _v1(self):
        chain = _chain(1)
        return make_anchor("prod", chain[-1].hmac, 1, "t")

    def _v2(self):
        chain = _chain(1)
        return make_anchor("prod", chain[-1].hmac, 1, "t",
                            seal_head=self.SEAL_HEAD, seal_count=3)

    def test_v1_pin_has_no_seal_fields(self):
        rec = self._v1()
        self.assertEqual(rec["pacioli_anchor"], 1)
        self.assertNotIn("seal_head", rec)
        self.assertNotIn("seal_count", rec)

    def test_v2_pin_carries_seal_head_and_count(self):
        rec = self._v2()
        self.assertEqual(rec["pacioli_anchor"], 2)
        self.assertEqual(rec["seal_head"], self.SEAL_HEAD)
        self.assertEqual(rec["seal_count"], 3)

    def test_v2_round_trips(self):
        rec = self._v2()
        self.assertEqual(parse_anchor(render_anchor(rec)), rec)

    def test_v1_round_trips_still_has_no_seal_fields(self):
        rec = self._v1()
        rt = parse_anchor(render_anchor(rec))
        self.assertEqual(rt, rec)
        self.assertNotIn("seal_head", rt)
        self.assertNotIn("seal_count", rt)

    def test_seal_head_without_seal_count_refused(self):
        with self.assertRaises(ValueError):
            make_anchor("prod", _chain(1)[-1].hmac, 1, "t", seal_head=self.SEAL_HEAD)

    def test_seal_count_without_seal_head_refused(self):
        with self.assertRaises(ValueError):
            make_anchor("prod", _chain(1)[-1].hmac, 1, "t", seal_count=3)

    def test_seal_count_zero_with_non_genesis_seal_head_refused(self):
        with self.assertRaises(ValueError):
            make_anchor("prod", _chain(1)[-1].hmac, 1, "t",
                        seal_head=self.SEAL_HEAD, seal_count=0)

    def test_seal_count_zero_with_genesis_seal_head_ok(self):
        rec = make_anchor("prod", _chain(1)[-1].hmac, 1, "t", seal_head=GENESIS, seal_count=0)
        self.assertEqual((rec["seal_head"], rec["seal_count"]), (GENESIS, 0))

    def test_nonzero_seal_count_with_genesis_seal_head_refused(self):
        with self.assertRaises(ValueError):
            make_anchor("prod", _chain(1)[-1].hmac, 1, "t", seal_head=GENESIS, seal_count=1)

    def test_non_hex_seal_head_refused(self):
        with self.assertRaises(ValueError):
            make_anchor("prod", _chain(1)[-1].hmac, 1, "t",
                        seal_head="not-hex-at-all", seal_count=1)

    def test_negative_seal_count_refused(self):
        with self.assertRaises(ValueError):
            make_anchor("prod", _chain(1)[-1].hmac, 1, "t",
                        seal_head=self.SEAL_HEAD, seal_count=-1)

    def test_bool_seal_count_refused(self):
        with self.assertRaises(ValueError):
            make_anchor("prod", _chain(1)[-1].hmac, 1, "t",
                        seal_head=self.SEAL_HEAD, seal_count=True)

    def test_v2_labeled_pin_missing_seal_count_field_refused(self):
        rec = dict(self._v2())
        del rec["seal_count"]
        with self.assertRaises(ValueError):
            parse_anchor(json.dumps(rec))

    def test_v2_labeled_pin_missing_seal_head_field_refused(self):
        rec = dict(self._v2())
        del rec["seal_head"]
        with self.assertRaises(ValueError):
            parse_anchor(json.dumps(rec))

    def test_v1_labeled_pin_with_stray_seal_field_refused(self):
        # A record claiming v1 but carrying a seal field must never be silently accepted as v1.
        rec = dict(self._v1(), seal_head=self.SEAL_HEAD)
        with self.assertRaises(ValueError):
            parse_anchor(json.dumps(rec))

    def test_unsupported_version_refused(self):
        # 3 became a supported version with the count-anchor slice (2026-07-16); the
        # first UNsupported version is now 4.
        rec = dict(self._v2(), pacioli_anchor=4)
        with self.assertRaises(ValueError):
            parse_anchor(json.dumps(rec))




class TestAnchorV3CloseFields(unittest.TestCase):
    """Pin format v3 -- ``close_head``/``close_count`` riding alongside the receipt head and the
    seal pair, so ONE off-box pin covers all three chains (the count-anchor slice,
    docs/plans/2026-07-16-close-count-anchor.md). ``parse_anchor`` must accept v1, v2, AND v3;
    older pins are never an error; a malformed/partial close-field pair is always refused
    loudly, never treated as v2; close fields REQUIRE the seal fields (v3 is a superset of v2 --
    ``anchor write`` always has both pairs in reach, so close-without-seal is a shape violation,
    not a real pin)."""

    SEAL_HEAD = "b" * 64
    CLOSE_HEAD = "c" * 64

    def _v2(self):
        chain = _chain(1)
        return make_anchor("prod", chain[-1].hmac, 1, "t",
                            seal_head=self.SEAL_HEAD, seal_count=3)

    def _v3(self):
        chain = _chain(1)
        return make_anchor("prod", chain[-1].hmac, 1, "t",
                            seal_head=self.SEAL_HEAD, seal_count=3,
                            close_head=self.CLOSE_HEAD, close_count=2)

    def test_v3_pin_carries_close_head_and_count(self):
        rec = self._v3()
        self.assertEqual(rec["pacioli_anchor"], 3)
        self.assertEqual(rec["close_head"], self.CLOSE_HEAD)
        self.assertEqual(rec["close_count"], 2)

    def test_v3_round_trips(self):
        rec = self._v3()
        self.assertEqual(parse_anchor(render_anchor(rec)), rec)

    def test_v2_emission_byte_identical_without_close_fields(self):
        # Global constraint 2: callers that never pass close fields emit exactly the bytes they
        # emitted before v3 existed.
        rec = self._v2()
        self.assertEqual(rec["pacioli_anchor"], 2)
        self.assertNotIn("close_head", rec)
        self.assertNotIn("close_count", rec)

    def test_close_fields_without_seal_fields_refused(self):
        with self.assertRaises(ValueError):
            make_anchor("prod", _chain(1)[-1].hmac, 1, "t",
                        close_head=self.CLOSE_HEAD, close_count=2)

    def test_close_head_without_close_count_refused(self):
        with self.assertRaises(ValueError):
            make_anchor("prod", _chain(1)[-1].hmac, 1, "t",
                        seal_head=self.SEAL_HEAD, seal_count=3, close_head=self.CLOSE_HEAD)

    def test_close_count_without_close_head_refused(self):
        with self.assertRaises(ValueError):
            make_anchor("prod", _chain(1)[-1].hmac, 1, "t",
                        seal_head=self.SEAL_HEAD, seal_count=3, close_count=2)

    def test_close_count_zero_with_genesis_close_head_ok(self):
        # the empty close table is a legitimate genesis state ("no period has ever closed") --
        # the pin records it as (GENESIS, 0), same shape rule the other pairs follow.
        rec = make_anchor("prod", _chain(1)[-1].hmac, 1, "t",
                          seal_head=self.SEAL_HEAD, seal_count=3,
                          close_head=GENESIS, close_count=0)
        self.assertEqual((rec["close_head"], rec["close_count"]), (GENESIS, 0))

    def test_close_count_zero_with_non_genesis_close_head_refused(self):
        with self.assertRaises(ValueError):
            make_anchor("prod", _chain(1)[-1].hmac, 1, "t",
                        seal_head=self.SEAL_HEAD, seal_count=3,
                        close_head=self.CLOSE_HEAD, close_count=0)

    def test_nonzero_close_count_with_genesis_close_head_refused(self):
        with self.assertRaises(ValueError):
            make_anchor("prod", _chain(1)[-1].hmac, 1, "t",
                        seal_head=self.SEAL_HEAD, seal_count=3,
                        close_head=GENESIS, close_count=1)

    def test_non_hex_close_head_refused(self):
        with self.assertRaises(ValueError):
            make_anchor("prod", _chain(1)[-1].hmac, 1, "t",
                        seal_head=self.SEAL_HEAD, seal_count=3,
                        close_head="not-hex", close_count=1)

    def test_negative_close_count_refused(self):
        with self.assertRaises(ValueError):
            make_anchor("prod", _chain(1)[-1].hmac, 1, "t",
                        seal_head=self.SEAL_HEAD, seal_count=3,
                        close_head=self.CLOSE_HEAD, close_count=-1)

    def test_bool_close_count_refused(self):
        with self.assertRaises(ValueError):
            make_anchor("prod", _chain(1)[-1].hmac, 1, "t",
                        seal_head=self.SEAL_HEAD, seal_count=3,
                        close_head=self.CLOSE_HEAD, close_count=True)

    def test_v3_labeled_pin_missing_close_count_refused(self):
        rec = dict(self._v3())
        del rec["close_count"]
        with self.assertRaises(ValueError):
            parse_anchor(json.dumps(rec))

    def test_v3_labeled_pin_missing_close_head_refused(self):
        rec = dict(self._v3())
        del rec["close_head"]
        with self.assertRaises(ValueError):
            parse_anchor(json.dumps(rec))

    def test_v3_labeled_pin_missing_seal_fields_refused(self):
        # v3 is a superset of v2 -- a v3 record without the seal pair is malformed, never
        # downgraded.
        rec = dict(self._v3())
        del rec["seal_head"]
        del rec["seal_count"]
        with self.assertRaises(ValueError):
            parse_anchor(json.dumps(rec))

    def test_v2_labeled_pin_with_stray_close_field_refused(self):
        rec = dict(self._v2(), close_head=self.CLOSE_HEAD)
        with self.assertRaises(ValueError):
            parse_anchor(json.dumps(rec))

    def test_v1_labeled_pin_with_stray_close_field_refused(self):
        chain = _chain(1)
        rec = dict(make_anchor("prod", chain[-1].hmac, 1, "t"), close_count=2)
        with self.assertRaises(ValueError):
            parse_anchor(json.dumps(rec))

    def test_compare_ignores_close_fields(self):
        # compare() is the receipt-chain half only -- a v3 record's close fields never change
        # its verdict (the close comparison is close_gate_state's kwargs, run separately).
        chain = _chain(3)
        rec = make_anchor("prod", chain[-1].hmac, 3, "t",
                          seal_head=self.SEAL_HEAD, seal_count=3,
                          close_head=self.CLOSE_HEAD, close_count=2)
        ok, reason = compare(rec, "prod", [r.hmac for r in chain])
        self.assertTrue(ok, reason)


class TestCompare(unittest.TestCase):
    def _anchor(self, chain, target="prod"):
        head = chain[-1].hmac if chain else GENESIS
        return make_anchor(target, head, len(chain), "t")

    def test_unchanged_chain_matches(self):
        chain = _chain(3)
        self.assertEqual(compare(self._anchor(chain), "prod", _hmacs(chain)), (True, None))

    def test_grown_chain_with_pinned_head_still_on_chain_matches(self):
        chain = _chain(5)
        anchor = self._anchor(chain[:3])
        self.assertEqual(compare(anchor, "prod", _hmacs(chain)), (True, None))

    def test_count_regression_is_tampering(self):
        # the truncated list is still internally self-consistent; only the pin catches it
        chain = _chain(3)
        anchor = self._anchor(chain)
        ok, reason = compare(anchor, "prod", _hmacs(chain[:2]))
        self.assertFalse(ok)
        self.assertIn("regress", reason)

    def test_full_wipe_is_tampering(self):
        anchor = self._anchor(_chain(2))
        ok, _ = compare(anchor, "prod", [])
        self.assertFalse(ok)

    def test_head_mismatch_at_same_count_is_tampering(self):
        anchor = self._anchor(_chain(2))
        rewritten = _chain(2, key=b"another-key")  # same length, different history
        ok, reason = compare(anchor, "prod", _hmacs(rewritten))
        self.assertFalse(ok)
        self.assertIn("rewritten", reason)

    def test_grown_chain_with_rewritten_prefix_is_tampering(self):
        # count went UP, but the pinned head no longer sits at its position: pre-pin history
        # was rewritten and rebuilt longer. Deny.
        anchor = self._anchor(_chain(2))
        rebuilt = _chain(4, key=b"another-key")
        ok, reason = compare(anchor, "prod", _hmacs(rebuilt))
        self.assertFalse(ok)
        self.assertIn("rewritten", reason)

    def test_cross_target_check_refused(self):
        chain = _chain(2)
        anchor = self._anchor(chain, target="staging")
        ok, reason = compare(anchor, "prod", _hmacs(chain))
        self.assertFalse(ok)
        self.assertIn("staging", reason)

    def test_empty_anchor_matches_empty_and_grown_chain(self):
        anchor = make_anchor("prod", GENESIS, 0, "t")
        self.assertEqual(compare(anchor, "prod", []), (True, None))
        self.assertEqual(compare(anchor, "prod", _hmacs(_chain(2))), (True, None))

    def test_garbage_record_fails_closed(self):
        ok, _ = compare({"whatever": 1}, "prod", _hmacs(_chain(1)))
        self.assertFalse(ok)


class TestAnchorCarriesNoSecret(unittest.TestCase):
    def test_seal_key_material_never_in_the_record(self):
        chain = _chain(2)
        text = render_anchor(make_anchor("prod", chain[-1].hmac, 2, "t"))
        self.assertNotIn(KEY.decode(), text)
        self.assertNotIn(KEY.hex(), text)


REG = '[targets.prod]\nbase_url = "https://erp.example.com"\n' \
      'api_key = "env:K"\napi_secret = "env:S"\ndefault = true\n'


class _AnchorCliFixture(unittest.TestCase):
    """Shared fixture for the anchor-CLI test classes below: a real on-disk store + registry,
    and the write/check/append/seal helpers. Holds NO tests of its own — extracted (verify pass
    2026-07-16, Item D) so ``TestAnchorCliCloseFields`` can reuse the fixture WITHOUT inheriting
    and re-running ``TestAnchorCli``'s whole suite as duplicate executions (inflated-looking
    coverage; the parent run already proves those behaviors on the same v3-emitting code)."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        d = Path(self.dir.name)
        (d / "targets.toml").write_text(REG)
        self.env = {"PACIOLI_REGISTRY": str(d / "targets.toml"),
                    "PACIOLI_STATE_DIR": str(d), "K": "kk", "S": "ss"}

    def tearDown(self):
        self.dir.cleanup()

    def _append(self, n=1):
        store = open_store(self.env, "prod")
        for i in range(n):
            store.record_intent({"doc": f"SINV-{i}"})

    def _write(self, out=None):
        o, e = io.StringIO(), io.StringIO()
        with redirect_stdout(o), redirect_stderr(e):
            rc = cmd_anchor_write(self.env, target=None, out=out)
        return rc, o.getvalue(), e.getvalue()

    def _check(self, infile):
        o, e = io.StringIO(), io.StringIO()
        with redirect_stdout(o), redirect_stderr(e):
            rc = cmd_anchor_check(self.env, target=None, infile=infile)
        return rc, o.getvalue() + e.getvalue()

    def _db(self):
        return sqlite3.connect(str(Path(self.dir.name) / "prod.db"))

    def _seal(self, reason="closing"):
        return open_store(self.env, "prod").seal(reason)


class TestAnchorCli(_AnchorCliFixture):
    """The operator's write/check loop, against a real on-disk store."""

    def test_write_emits_a_valid_record_on_stdout(self):
        self._append(2)
        rc, out, err = self._write()
        self.assertEqual(rc, 0)
        rec = parse_anchor(out)
        self.assertEqual(rec["target"], "prod")
        self.assertEqual(rec["count"], 2)
        self.assertEqual(rec["head"], open_store(self.env, "prod").head())
        self.assertIn("off", err.lower())  # the not-off-box-until-YOU-move-it reminder

    def test_write_to_file(self):
        self._append(1)
        path = Path(self.dir.name) / "pin.json"
        rc, out, _ = self._write(out=str(path))
        self.assertEqual(rc, 0)
        self.assertEqual(out, "")  # nothing on stdout when a file was asked for
        self.assertEqual(parse_anchor(path.read_text())["count"], 1)

    def test_write_then_check_round_trips(self):
        self._append(2)
        _, out, _ = self._write()
        path = Path(self.dir.name) / "pin.json"
        path.write_text(out)
        rc, msg = self._check(str(path))
        self.assertEqual(rc, 0, msg)
        self.assertIn("ok", msg.lower())

    def test_check_reads_stdin(self):
        self._append(1)
        _, out, _ = self._write()
        with mock.patch("sys.stdin", io.StringIO(out)):
            rc, msg = self._check("-")
        self.assertEqual(rc, 0, msg)

    def test_check_detects_truncation_since_the_pin(self):
        self._append(3)
        _, out, _ = self._write()
        path = Path(self.dir.name) / "pin.json"
        path.write_text(out)
        with self._db() as conn:  # host-level tampering: drop the newest receipt
            conn.execute("DELETE FROM receipts WHERE seq = (SELECT MAX(seq) FROM receipts)")
        rc, msg = self._check(str(path))
        self.assertEqual(rc, 1)
        self.assertIn("regress", msg)

    def test_check_passes_when_the_chain_only_grew(self):
        self._append(1)
        _, out, _ = self._write()
        path = Path(self.dir.name) / "pin.json"
        path.write_text(out)
        self._append(2)
        rc, msg = self._check(str(path))
        self.assertEqual(rc, 0, msg)
        self.assertIn("not yet covered", msg)  # the rotate nudge for the unpinned suffix

    def test_check_refuses_a_malformed_anchor(self):
        self._append(1)
        path = Path(self.dir.name) / "pin.json"
        path.write_text("{}")
        rc, msg = self._check(str(path))
        self.assertEqual(rc, 1)
        self.assertIn("FAILED", msg)

    def test_check_refuses_a_cross_target_anchor(self):
        self._append(1)
        head = open_store(self.env, "prod").head()
        path = Path(self.dir.name) / "pin.json"
        path.write_text(render_anchor(make_anchor("staging", head, 1, "t")))
        rc, msg = self._check(str(path))
        self.assertEqual(rc, 1)
        self.assertIn("staging", msg)

    def test_check_fails_when_the_keyed_verify_fails(self):
        # compare() alone never sees seals — the CLI must run the keyed verify too
        self._append(2)
        _, out, _ = self._write()
        path = Path(self.dir.name) / "pin.json"
        path.write_text(out)
        with self._db() as conn:
            conn.execute("UPDATE receipts SET body='{\"doc\":\"FORGED\"}' WHERE seq=0")
        rc, msg = self._check(str(path))
        self.assertEqual(rc, 1)
        self.assertIn("FAILED", msg)

    def test_write_refuses_to_pin_a_tampered_chain(self):
        # pinning a corrupt chain would launder it as truth
        self._append(2)
        with self._db() as conn:
            conn.execute("UPDATE receipts SET body='{\"doc\":\"FORGED\"}' WHERE seq=0")
        rc, out, err = self._write()
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")  # no record emitted
        self.assertIn("refusing", err)

    def test_write_on_an_empty_chain_pins_genesis(self):
        rc, out, _ = self._write()
        self.assertEqual(rc, 0)
        rec = parse_anchor(out)
        self.assertEqual((rec["count"], rec["head"]), (0, GENESIS))

    def test_anchor_record_never_contains_seal_key_material(self):
        self._append(1)
        rc, out, err = self._write()
        self.assertEqual(rc, 0)
        key = (Path(self.dir.name) / "seal.key").read_bytes()
        for surface in (out, err):
            self.assertNotIn(key.hex(), surface)
            self.assertNotIn(str(key), surface)

    # --- Task 2: the seal head rides alongside the receipt head -------------------

    def test_write_emits_v3_with_seal_head_and_count(self):
        # v2 with the seal slice, v3 with the count-anchor slice (2026-07-16) -- a live write
        # always emits the current format with every pair it has in reach.
        self._append(1)
        self._seal("closing")
        rc, out, _ = self._write()
        self.assertEqual(rc, 0)
        rec = parse_anchor(out)
        store = open_store(self.env, "prod")
        self.assertEqual(rec["pacioli_anchor"], 3)
        self.assertEqual(rec["seal_head"], store.seal_head())
        self.assertEqual(rec["seal_count"], store.seal_count())

    def test_write_then_check_round_trips_v2(self):
        self._append(2)
        self._seal("closing")
        _, out, _ = self._write()
        path = Path(self.dir.name) / "pin.json"
        path.write_text(out)
        rc, msg = self._check(str(path))
        self.assertEqual(rc, 0, msg)

    def test_check_detects_seal_tail_truncation_while_receipts_still_verify(self):
        # The seal-side analogue of test_check_detects_truncation_since_the_pin: a keyless
        # attacker with DB-file write access deletes the newest seal_events row. The receipt
        # chain is untouched and must still verify on its own -- only the seal pin catches this.
        self._append(2)
        self._seal("closing")
        _, out, _ = self._write()
        path = Path(self.dir.name) / "pin.json"
        path.write_text(out)
        with self._db() as conn:
            conn.execute("DELETE FROM seal_events WHERE seq = (SELECT MAX(seq) FROM seal_events)")

        control_ok, control_reason, _ = open_store(self.env, "prod").verify_snapshot()
        self.assertTrue(control_ok, control_reason)  # receipts alone still verify

        rc, msg = self._check(str(path))
        self.assertEqual(rc, 1)
        self.assertIn("seal", msg.lower())
        self.assertIn("off-box anchor", msg)

    def test_check_detects_seal_divergence(self):
        self._append(1)
        self._seal("closing")
        _, out, _ = self._write()
        path = Path(self.dir.name) / "pin.json"
        path.write_text(out)
        from pacioli.store import _seal_event_hmac
        with self._db() as conn:
            row = conn.execute(
                "SELECT seq, ts, source FROM seal_events ORDER BY seq DESC LIMIT 1"
            ).fetchone()
            seq, ts, source = row
            key = (Path(self.dir.name) / "seal.key").read_bytes()
            new_mac = _seal_event_hmac(key, seq, ts, "unseal", "forged reopen", source)
            conn.execute(
                "UPDATE seal_events SET action=?, reason=?, hmac=? WHERE seq=?",
                ("unseal", "forged reopen", new_mac, seq),
            )
        rc, msg = self._check(str(path))
        self.assertEqual(rc, 1)
        self.assertIn("diverges", msg)

    def test_check_v1_pin_warns_and_does_not_falsely_cover_the_seal(self):
        self._append(1)
        chain_head = open_store(self.env, "prod").head()
        v1_pin = render_anchor(make_anchor("prod", chain_head, 1, "t"))
        path = Path(self.dir.name) / "pin.json"
        path.write_text(v1_pin)
        self._seal("closing")
        with self._db() as conn:  # tail-truncate the seal history AFTER the v1 pin was taken
            conn.execute("DELETE FROM seal_events WHERE seq = (SELECT MAX(seq) FROM seal_events)")
        rc, msg = self._check(str(path))
        self.assertEqual(rc, 0, msg)  # receipts alone still verify -- v1 pin doesn't cover the seal
        self.assertIn("predates seal anchoring", msg)
        self.assertIn("v1", msg)
        self.assertIn("NOT covered", msg)

    def test_check_refuses_partial_seal_fields(self):
        self._append(1)
        chain_head = open_store(self.env, "prod").head()
        rec = dict(make_anchor("prod", chain_head, 1, "t", seal_head="a" * 64, seal_count=1))
        del rec["seal_count"]  # malformed/partial pair
        path = Path(self.dir.name) / "pin.json"
        path.write_text(json.dumps(rec))
        rc, msg = self._check(str(path))
        self.assertEqual(rc, 1)
        self.assertIn("FAILED", msg)

    def test_check_refuses_non_hex_seal_head(self):
        self._append(1)
        chain_head = open_store(self.env, "prod").head()
        rec = dict(make_anchor("prod", chain_head, 1, "t", seal_head="a" * 64, seal_count=1))
        rec["seal_head"] = "not-hex"
        path = Path(self.dir.name) / "pin.json"
        path.write_text(json.dumps(rec))
        rc, msg = self._check(str(path))
        self.assertEqual(rc, 1)
        self.assertIn("FAILED", msg)

    def test_write_refuses_when_seal_history_is_unverifiable(self):
        # A gapped seal history (an interior row deleted) must never be witnessed as a pin --
        # that would launder a broken history as a trustworthy anchor.
        self._append(1)
        self._seal("closing")
        self._seal("closing again")
        with self._db() as conn:
            conn.execute("DELETE FROM seal_events WHERE seq = 2")
        rc, out, err = self._write()
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")
        self.assertIn("seal", err.lower())

    def test_write_still_pins_a_legitimately_sealed_broker(self):
        # A genuinely, verifiably sealed state (cause=None) is not "broken" -- refusing to pin
        # it would make anchoring impossible for every operator who has ever sealed.
        self._append(1)
        self._seal("closing")
        rc, out, _ = self._write()
        self.assertEqual(rc, 0)
        rec = parse_anchor(out)
        self.assertEqual(rec["seal_count"], 2)  # genesis + the seal event

    # --- Task 2 review (Critical): `cmd_anchor_check` must fail on ANY fail-closed
    # `seal_state()` cause, not only the two "off-box" pin-mismatch causes it used to
    # substring-match for. `_check_seal_pin` can agree (the pinned POSITION is untouched) while
    # the derivation's own content-only checks still catch a different kind of tamper -- that
    # `cause` must never be thrown away. ------------------------------------------------------

    def test_check_fails_on_interior_gap_with_agreeing_pin_position(self):
        # Attack: delete an INTERIOR seal_events row, then append a compensating row so the row
        # COUNT matches the pin again. The pinned POSITION (seq == pinned seal_count) is never
        # touched, so `_check_seal_pin` agrees -- only the derivation's seq-contiguity check
        # catches this, via cause="seal history gap (rollback?)", which does not contain
        # "off-box". The receipt chain is untouched and verifies cleanly; only the seal side is
        # attacked.
        self._append(1)
        self._seal("closing")
        self._seal("closing again")  # seal_events: seq1=genesis, seq2=seal, seq3=seal (count 3)
        _, out, _ = self._write()
        path = Path(self.dir.name) / "pin.json"
        path.write_text(out)

        from pacioli.store import _seal_event_hmac
        key = (Path(self.dir.name) / "seal.key").read_bytes()
        with self._db() as conn:
            conn.execute("DELETE FROM seal_events WHERE seq = 2")  # interior row gone
            ts = "2026-07-02T00:00:01Z"
            mac = _seal_event_hmac(key, 4, ts, "seal", "compensating", "operator")
            conn.execute(
                "INSERT INTO seal_events(seq, ts, action, reason, source, hmac) "
                "VALUES(4,?,?,?,?,?)",
                (ts, "seal", "compensating", "operator", mac),
            )  # restores the row COUNT to 3 -- seq3 (the pinned position) is untouched

        control_ok, control_reason, _ = open_store(self.env, "prod").verify_snapshot()
        self.assertTrue(control_ok, control_reason)  # receipts alone still verify

        rc, msg = self._check(str(path))
        self.assertEqual(rc, 1, msg)
        self.assertIn("gap", msg.lower())

    def test_check_fails_on_keyless_tail_injection_with_agreeing_pin_position(self):
        # Attack: a keyless attacker (DB-file write access, no HMAC key) appends a garbage-hmac
        # tail row. The pinned POSITION is untouched and the row count only grows (>= pinned
        # count), so `_check_seal_pin` agrees -- only the derivation's latest-row HMAC check
        # (keyed open) catches this, via cause="unverifiable", which does not contain "off-box".
        self._append(1)
        self._seal("closing")  # seal_events: seq1=genesis, seq2=seal (count 2)
        _, out, _ = self._write()
        path = Path(self.dir.name) / "pin.json"
        path.write_text(out)

        with self._db() as conn:
            conn.execute(
                "INSERT INTO seal_events(ts, action, reason, source, hmac) VALUES(?,?,?,?,?)",
                ("2026-07-02T00:00:01Z", "unseal", "forged reopen", "operator",
                 "deadbeef" * 8),
            )

        control_ok, control_reason, _ = open_store(self.env, "prod").verify_snapshot()
        self.assertTrue(control_ok, control_reason)  # receipts alone still verify

        rc, msg = self._check(str(path))
        self.assertEqual(rc, 1, msg)
        self.assertIn("unverifiable", msg.lower())

    # --- F3 (correctness redteam 2026-07-15): cmd_anchor_write must build the seal pin from ONE
    # consistent snapshot, never from three separate reads. -----------------------------------

    def test_anchor_write_uses_one_seal_snapshot_not_three_separate_reads(self):
        # F3 fix, proven structurally: the OLD `cmd_anchor_write` called `store.seal_state()`,
        # `store.seal_head()`, and `store.seal_count()` as three separate reads — a concurrent
        # seal/unseal landing between any two of them could pair a stale derivation with a fresh
        # head/count (test_store_seal.py's TestSealStateSnapshot::
        # test_RED_three_separate_reads_can_yield_a_self_inconsistent_triple reproduces the race
        # directly). The fix is `BrokerStore.seal_state_snapshot()` — ONE `SELECT`. Spy on all
        # four methods (the three vulnerable ones, plus the new single-snapshot one) and assert
        # the vulnerable three are NEVER called by a real `anchor write` run — this fails (RED)
        # against the pre-fix code, which calls all three.
        self._append(1)
        self._seal("closing")
        calls = []
        from pacioli.store import BrokerStore as _BS
        originals = {
            name: getattr(_BS, name)
            for name in ("seal_state", "seal_head", "seal_count", "seal_state_snapshot")
        }

        def _spy(name):
            orig = originals[name]

            def _inner(self, *a, **k):
                calls.append(name)
                return orig(self, *a, **k)
            return _inner

        patches = [mock.patch.object(_BS, name, _spy(name)) for name in originals]
        for p in patches:
            p.start()
        try:
            rc, out, err = self._write()
        finally:
            for p in patches:
                p.stop()

        self.assertEqual(rc, 0, err)
        self.assertNotIn("seal_state", calls)
        self.assertNotIn("seal_head", calls)
        self.assertNotIn("seal_count", calls)
        self.assertIn("seal_state_snapshot", calls)

    def test_anchor_write_pin_is_self_consistent_when_a_writer_races_the_seal_snapshot(self):
        # GREEN: end-to-end, against the real CLI command (not just the store method in
        # isolation) — a writer landing between what USED to be three separate reads no longer
        # has anywhere to land, because `cmd_anchor_write` now reads the seal half once. The
        # emitted pin, whichever side of the race it landed on, must check clean against the
        # store's OWN (untouched) history.
        self._append(1)
        rc, out, _ = self._write()
        self.assertEqual(rc, 0)
        path = Path(self.dir.name) / "pin.json"
        path.write_text(out)
        rc, msg = self._check(str(path))
        self.assertEqual(rc, 0, msg)




class TestAnchorCliCloseFields(_AnchorCliFixture):
    """``anchor write``/``anchor check`` cover the close chain (format v3) -- the count-anchor
    slice. Shares the fixture, NOT TestAnchorCli's test suite: the parent class's own run
    already exercises every receipt/seal behavior against the v3-emitting write (the write is
    unconditional), so re-running those 25 bodies here would be duplicate execution counted as
    close-slice coverage. These are the close-specific tests only."""

    def _close(self, until, since=None, expect=None, gapped=False):
        store = open_store(self.env, "prod", with_key=True)
        store.record_close(period_since=since, period_until=until,
                           attested_head="h-" + until, gapped=gapped,
                           expected_last_close_seq=expect)

    def test_write_emits_v3_with_close_head_and_count(self):
        self._append(1)
        self._close("2026-01-31")
        rc, out, _ = self._write()
        self.assertEqual(rc, 0)
        rec = parse_anchor(out)
        store = open_store(self.env, "prod")
        self.assertEqual(rec["pacioli_anchor"], 3)
        self.assertEqual(rec["close_head"], store.close_head())
        self.assertEqual(rec["close_count"], 1)

    def test_write_empty_close_table_pins_genesis_zero(self):
        # "no period has ever closed yet" is a claim worth pinning -- GENESIS sentinel, count 0.
        self._append(1)
        rc, out, _ = self._write()
        self.assertEqual(rc, 0)
        rec = parse_anchor(out)
        self.assertEqual((rec["close_head"], rec["close_count"]), (GENESIS, 0))

    def test_write_refuses_a_gapped_close_history(self):
        # an INTERIOR deletion (integrity cause) must never be witnessed as a pin.
        self._append(1)
        self._close("2026-01-31")
        self._close("2026-02-28", since="2026-01-31", expect=1)
        with self._db() as conn:
            conn.execute("DELETE FROM close_records WHERE seq = 1")
        rc, out, err = self._write()
        self.assertEqual(rc, 1)
        self.assertIn("close", err.lower())

    def test_write_pins_normally_while_gapped_awaiting_attestation(self):
        # a workflow latch on a fully verified, contiguous history is not "broken" -- exactly as
        # a genuinely SEALED broker still pins normally.
        self._append(1)
        self._close("2026-01-31", gapped=True)
        rc, out, _ = self._write()
        self.assertEqual(rc, 0)
        self.assertEqual(parse_anchor(out)["close_count"], 1)

    def test_check_detects_close_tail_deletion_since_the_pin(self):
        self._append(1)
        self._close("2026-01-31")
        self._close("2026-02-28", since="2026-01-31", expect=1)
        _, out, _ = self._write()
        path = Path(self.dir.name) / "pin.json"
        path.write_text(out)
        with self._db() as conn:  # the silent cursor rollback
            conn.execute(
                "DELETE FROM close_records WHERE seq = (SELECT MAX(seq) FROM close_records)")
        rc, msg = self._check(str(path))
        self.assertEqual(rc, 1)
        self.assertIn("behind the off-box anchor", msg)

    def test_check_passes_when_close_history_only_grew(self):
        self._append(1)
        self._close("2026-01-31")
        _, out, _ = self._write()
        path = Path(self.dir.name) / "pin.json"
        path.write_text(out)
        self._close("2026-02-28", since="2026-01-31", expect=1)
        rc, msg = self._check(str(path))
        self.assertEqual(rc, 0, msg)

    def test_check_v2_pin_warns_close_not_covered(self):
        # a v2 pin (pre-0.26.0) still checks receipts + seal exactly as before, but must WARN
        # that a close-record rollback is not covered -- never silently treated as fine.
        self._append(1)
        store = open_store(self.env, "prod", with_key=True)
        v2 = render_anchor(make_anchor("prod", store.head(), 1, "t",
                                       seal_head=store.seal_head(),
                                       seal_count=store.seal_count()))
        path = Path(self.dir.name) / "pin.json"
        path.write_text(v2)
        rc, msg = self._check(str(path))
        self.assertEqual(rc, 0, msg)
        self.assertIn("close", msg.lower())
        self.assertIn("NOT covered", msg)

    def test_check_v1_pin_warning_names_both_uncovered_tables(self):
        self._append(1)
        chain_head = open_store(self.env, "prod").head()
        v1 = render_anchor(make_anchor("prod", chain_head, 1, "t"))
        path = Path(self.dir.name) / "pin.json"
        path.write_text(v1)
        rc, msg = self._check(str(path))
        self.assertEqual(rc, 0, msg)
        self.assertIn("seal", msg.lower())
        self.assertIn("close", msg.lower())
        self.assertIn("NOT covered", msg)

    def test_check_passes_while_gapped_awaiting_attestation(self):
        # the workflow latch is a legitimate, verified state -- an agreeing pin over it is an
        # anchor SUCCESS (only integrity/anchor causes fail the check). Uses the store contract's
        # stable cause tag, never prose-matching.
        self._append(1)
        self._close("2026-01-31", gapped=True)
        _, out, _ = self._write()
        path = Path(self.dir.name) / "pin.json"
        path.write_text(out)
        rc, msg = self._check(str(path))
        self.assertEqual(rc, 0, msg)

    def test_check_v2_pin_does_not_falsely_cover_a_close_rollback(self):
        # the v2 warning is honest: a close tail-deletion AFTER a v2 pin passes the check
        # (rc 0) -- the pin simply cannot see it, and says so.
        self._append(1)
        self._close("2026-01-31")
        store = open_store(self.env, "prod", with_key=True)
        v2 = render_anchor(make_anchor("prod", store.head(), 1, "t",
                                       seal_head=store.seal_head(),
                                       seal_count=store.seal_count()))
        path = Path(self.dir.name) / "pin.json"
        path.write_text(v2)
        with self._db() as conn:
            conn.execute("DELETE FROM close_records WHERE seq = 1")
        rc, msg = self._check(str(path))
        self.assertEqual(rc, 0, msg)  # honestly not covered
        self.assertIn("NOT covered", msg)


class TestAnchorRepinReminder(unittest.TestCase):
    """F2 (security redteam 2026-07-15): "re-pin after every seal/unseal" used to live only in an
    abstract limits paragraph — `pacioli anchor write`'s own trailing guidance mentioned rotation
    but never named this. `pacioli seal`/`pacioli unseal`'s reminder is pinned in
    test_seal_cli.py; this pins `anchor write`'s side of it."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        d = Path(self.dir.name)
        (d / "targets.toml").write_text(REG)
        self.env = {"PACIOLI_REGISTRY": str(d / "targets.toml"),
                    "PACIOLI_STATE_DIR": str(d), "K": "kk", "S": "ss"}

    def tearDown(self):
        self.dir.cleanup()

    def test_anchor_write_trailing_guidance_names_repin_discipline(self):
        o, e = io.StringIO(), io.StringIO()
        with redirect_stdout(o), redirect_stderr(e):
            rc = cmd_anchor_write(self.env, target=None, out=None)
        self.assertEqual(rc, 0, e.getvalue())
        err = e.getvalue().lower()
        self.assertIn("seal", err)
        self.assertIn("unseal", err)


if __name__ == "__main__":
    unittest.main()
