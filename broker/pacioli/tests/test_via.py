# Copyright (c) 2026, John Broadway and contributors
# License: Apache-2.0
"""The principal in the ledger (F3, the transports ruling): with many transports, every intent
receipt must say WHICH door and WHO asked. One seam — the store stamps ``via`` into every
intent body when (and only when) a transport declared one; the serving transport's declaration always wins
over anything a body might carry (no caller can spoof its own door), and an undeclared store
is byte-identical to before (the in-process/CLI legacy). The principal is always a LABEL
(a token's reference like ``env:SERVE_T``, or ``local-spawn``) — never secret material."""
import tempfile
import unittest

from pacioli.runtime import open_store
from pacioli.tests.test_store import _store as _mem_store  # the standing in-memory fixture


class TestStoreVia(unittest.TestCase):
    def test_declared_via_is_stamped_into_every_intent(self):
        store = _mem_store()
        store.set_via({"transport": "http", "principal": "env:SERVE_T"})
        r = store.record_intent({"tool": "submit", "target": "t", "docname": "SI-1"})
        self.assertEqual(r.body["via"], {"transport": "http", "principal": "env:SERVE_T"})
        self.assertEqual(r.body["tool"], "submit")  # the rest of the body untouched

    def test_declared_via_overwrites_a_body_supplied_one(self):
        # No caller may declare its own door: the transport's stamp wins, always.
        store = _mem_store()
        store.set_via({"transport": "stdio", "principal": "local-spawn"})
        r = store.record_intent({"tool": "submit", "via": {"transport": "forged"}})
        self.assertEqual(r.body["via"], {"transport": "stdio", "principal": "local-spawn"})

    def test_undeclared_store_is_byte_identical(self):
        store = _mem_store()
        r = store.record_intent({"tool": "submit", "target": "t"})
        self.assertNotIn("via", r.body)

    def test_via_rides_the_hmac_chain(self):
        # The stamp is part of the receipt body, so it is covered by the chain — a later
        # edit of WHO asked is a chain break, not a quiet relabel.
        store = _mem_store()
        store.set_via({"transport": "http", "principal": "env:SERVE_T"})
        store.record_intent({"tool": "submit"})
        ok, reason, receipts = store.verify_snapshot()
        self.assertTrue(ok, reason)

    def test_non_dict_via_refused(self):
        store = _mem_store()
        with self.assertRaises(ValueError):
            store.set_via("http")


class TestOpenStoreVia(unittest.TestCase):
    def test_open_store_threads_via(self):
        d = tempfile.mkdtemp(prefix="via-")
        env = {"PACIOLI_STATE_DIR": d}
        store = open_store(env, "t", via={"transport": "stdio", "principal": "local-spawn"})
        r = store.record_intent({"tool": "submit"})
        self.assertEqual(r.body["via"]["transport"], "stdio")

    def test_open_store_without_via_unchanged(self):
        d = tempfile.mkdtemp(prefix="via-")
        store = open_store({"PACIOLI_STATE_DIR": d}, "t")
        r = store.record_intent({"tool": "submit"})
        self.assertNotIn("via", r.body)


class TestTransportViaConstants(unittest.TestCase):
    def test_stdio_transport_declares_local_spawn(self):
        from pacioli.server import STDIO_VIA
        self.assertEqual(STDIO_VIA, {"transport": "stdio", "principal": "local-spawn"})

    def test_http_transport_via_carries_the_reference_label_never_a_token(self):
        from pacioli.server import _http_via
        self.assertEqual(_http_via("env:SERVE_T"),
                         {"transport": "http", "principal": "env:SERVE_T"})
        self.assertEqual(_http_via(None),
                         {"transport": "http", "principal": "loopback"})


if __name__ == "__main__":
    unittest.main()
