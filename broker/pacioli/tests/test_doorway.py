# Copyright (c) 2026, John Broadway and contributors
# License: Apache-2.0
"""The doorway (``pacioli.doorway`` + its three ``_tool_pacioli_*`` handlers + the doors'
served-surface selection) — ``docs/plans/2026-08-08-doorway-design.md``.

The seal-gate half of the doorway's proof (governed write through ``pacioli_call`` refused by
the INNER gate; reads and search surviving sealed/corrupt stores) lives in
``test_seal_gate.py`` beside the gate's own harness — this module covers the pure search/schema
core, the served-surface modes, the handler envelopes, and the lock-safety regression."""
from __future__ import annotations

import json
import os
import sqlite3
import unittest
from unittest import mock

from pacioli.doorway import (DOORWAY_NAMES, DOORWAY_TOOLS, RESIDENT_SPINE, search_tools,
                             served_tools, tool_schema)
from pacioli.registry import load_registry
from pacioli.server import dispatch_raw
from pacioli.store import BrokerStore
from pacioli.tools import TOOLS, PacioliBroker, tool_names

REG = ('[targets.prod]\nbase_url = "https://erp.example.com"\ncompany = "Example Corp"\n'
       'api_key = "k"\napi_secret = "env:S"\ndefault = true\n')


class _ReadClient:
    """Just ``get_document`` — enough for a read to flow through the doorway verbatim."""

    def get_document(self, doctype, name):
        return {"name": name, "doctype": doctype, "docstatus": 0}


def _refuse(*_a, **_k):
    raise AssertionError("the doorway's search/schema tools must touch no store and no client")


def make_broker(client=None, store_provider=None):
    return PacioliBroker(
        registry=load_registry(toml_text=REG),
        store_provider=store_provider
        or (lambda name: BrokerStore(sqlite3.connect(":memory:"), key=b"k" * 32)),
        client_provider=lambda target: client or _ReadClient(),
        now_epoch=lambda: 1_000.0,
        now_date=lambda: "2026-07-01",
    )


# --- the pure search core ----------------------------------------------------------------------

class TestSearch(unittest.TestCase):
    def test_preview_ranks_the_plan_spine_first_not_production_plan(self):
        # Design review finding 4, the measured defect: bare "plan"/"preview" put
        # amend/cancel/get_production_plan ahead of the PLAN spine. The plan_ synonym
        # (underscore included) must put the four plan_* tools in the top four positions.
        top4 = {r["name"] for r in search_tools(TOOLS, "preview")[:4]}
        self.assertEqual(top4, {"plan_submit", "plan_cancel",
                                "plan_cascade_cancel", "plan_reconcile"})

    def test_verify_ranks_prove_verify_above_prove_orphans(self):
        # Same finding: within a rank tier, a term matching AS TYPED in the name must beat a
        # synonym-only match — alphabetical tie-break alone put prove_orphans first.
        results = search_tools(TOOLS, "verify")
        self.assertEqual(results[0]["name"], "prove_verify")

    def test_post_sales_invoice_is_position_one(self):
        self.assertEqual(search_tools(TOOLS, "post sales invoice")[0]["name"],
                         "submit_sales_invoice")

    def test_post_invoice_reaches_submit_tools_only_at_the_top(self):
        top = search_tools(TOOLS, "post invoice")[:4]
        self.assertTrue(all(r["name"].startswith("submit_") for r in top), top)
        self.assertIn("submit_sales_invoice", {r["name"] for r in top})

    def test_and_semantics_not_or(self):
        for r in search_tools(TOOLS, "cancel payment", limit=50):
            text = (r["name"] + " " + r["summary"]).lower()
            self.assertIn("cancel", text)

    def test_blank_and_nonstring_queries_raise(self):
        for bad in ("", "   ", None, {"q": 1}, 7):
            with self.subTest(query=bad):
                with self.assertRaises(ValueError):
                    search_tools(TOOLS, bad)

    def test_limit_is_clamped_never_trusted(self):
        # No door validates inputSchema (2026-07-26 redteam class): 0, negatives, garbage and
        # oversize must all land inside [1, 50] — the declared bounds, actually enforced.
        self.assertEqual(len(search_tools(TOOLS, "get", limit=0)), 1)
        self.assertEqual(len(search_tools(TOOLS, "get", limit=-3)), 1)
        self.assertLessEqual(len(search_tools(TOOLS, "get", limit=10_000)), 50)
        self.assertLessEqual(len(search_tools(TOOLS, "get", limit="junk")), 10)

    def test_summaries_are_distinct_across_the_whole_catalog(self):
        # The doctype label is interpolated into sentence one of every mechanical description —
        # the property that makes a summaries-only result set usable. 265 tools, 265 summaries.
        from pacioli.doorway import _summary_of
        summaries = {t["name"]: _summary_of(t["description"]) for t in TOOLS}
        self.assertEqual(len(set(summaries.values())), len(TOOLS),
                         "duplicate summaries make search results indistinguishable")

    def test_doorway_tools_are_never_search_results(self):
        for query in ("tools", "search", "call", "schema", "pacioli"):
            for r in search_tools(TOOLS, query, limit=50):
                self.assertNotIn(r["name"], DOORWAY_NAMES)


class TestToolSchema(unittest.TestCase):
    def test_returns_the_full_catalog_entry(self):
        out = tool_schema(TOOLS, "submit_sales_invoice")
        self.assertEqual(out["name"], "submit_sales_invoice")
        self.assertIn("marker", out["inputSchema"]["properties"])

    def test_unknown_name_raises_with_near_miss_suggestions(self):
        with self.assertRaises(KeyError) as ctx:
            tool_schema(TOOLS, "submit_sales_invoce")
        self.assertIn("submit_sales_invoice", ctx.exception.args[0])

    def test_nonstring_name_raises_not_crashes(self):
        with self.assertRaises(KeyError):
            tool_schema(TOOLS, {"name": "x"})


# --- served surface ----------------------------------------------------------------------------

class TestServedTools(unittest.TestCase):
    def test_default_is_byte_identical_to_the_full_catalog(self):
        for env in ({}, {"PACIOLI_TOOLSETS": ""}, {"PACIOLI_TOOLSETS": "all"},
                    {"PACIOLI_TOOLSETS": " ALL "}):
            with self.subTest(env=env):
                self.assertEqual(served_tools(env), TOOLS)

    def test_dynamic_serves_exactly_the_nine(self):
        served = served_tools({"PACIOLI_TOOLSETS": "dynamic"})
        self.assertEqual([t["name"] for t in served],
                         [t["name"] for t in DOORWAY_TOOLS] + list(RESIDENT_SPINE))
        by_name = {t["name"]: t for t in TOOLS}
        for t in served:
            if t["name"] in by_name:   # the spine entries are the CATALOG dicts, never copies
                self.assertIs(t, by_name[t["name"]])

    def test_typo_refuses_rather_than_serving_a_surprise(self):
        with self.assertRaises(ValueError) as ctx:
            served_tools({"PACIOLI_TOOLSETS": "dyanmic"})
        self.assertIn("dyanmic", str(ctx.exception))

    def test_env_none_reads_os_environ_once(self):
        # assemble's exact rule (design review finding 9): `pacioli serve` under a real shell
        # must honor the var; explicit env dicts must NOT be shadowed by the shell.
        with mock.patch.dict(os.environ, {"PACIOLI_TOOLSETS": "dynamic"}):
            self.assertEqual(len(served_tools(None)), 9)
            self.assertEqual(served_tools({}), TOOLS)

    def test_resident_spine_names_exist_in_the_catalog(self):
        self.assertTrue(set(RESIDENT_SPINE) <= set(tool_names()))


class TestPayloadBudget(unittest.TestCase):
    """The doorway stays a doorway. Serialized exactly as the baseline was measured (the plain
    dict list — the same shape both sides of the changelog's 229,160 → dynamic comparison), the
    dynamic surface must stay under budget. Derived, not picked (review finding 8):
    7,544 (spine, measured 2026-08-08) + 3 doorway defs + margin for description growth."""

    BUDGET = 15_000

    def test_dynamic_payload_within_budget(self):
        payload = json.dumps([{"name": t["name"], "description": t["description"],
                               "inputSchema": t["inputSchema"]}
                              for t in served_tools({"PACIOLI_TOOLSETS": "dynamic"})])
        self.assertLessEqual(len(payload), self.BUDGET,
                             f"dynamic surface grew to {len(payload):,} bytes — either a "
                             "resident description got fatter (is the growth worth its rent at "
                             "every session start?) or something joined the resident set")

    def test_dynamic_is_under_five_percent_of_full(self):
        full = len(json.dumps(TOOLS))
        dyn = len(json.dumps(served_tools({"PACIOLI_TOOLSETS": "dynamic"})))
        self.assertLess(dyn, full * 0.05)


# --- the handlers, through dispatch ------------------------------------------------------------

class TestDispatchIntegration(unittest.TestCase):
    def test_find_and_schema_touch_no_store_and_no_client(self):
        broker = make_broker(client=None, store_provider=_refuse)
        broker._client = _refuse
        out = broker.dispatch("pacioli_find_tools", {"query": "submit invoice"})
        self.assertTrue(out["ok"])
        self.assertTrue(all(set(r) == {"name", "summary"} for r in out["results"]))
        out = broker.dispatch("pacioli_tool_schema", {"name": "submit_sales_invoice"})
        self.assertTrue(out["ok"])
        self.assertIn("inputSchema", out)

    def test_designed_refusals_use_the_house_envelope_not_the_backstop(self):
        # The backstop prefixes reasons with the exception type ("ValueError: ..."); a DESIGNED
        # refusal must not wear that noise (design review finding 6).
        broker = make_broker()
        for name, args in (("pacioli_find_tools", {}),
                           ("pacioli_find_tools", {"query": "   "}),
                           ("pacioli_tool_schema", {}),
                           ("pacioli_tool_schema", {"name": "no_such_tool"})):
            with self.subTest(tool=name, args=args):
                out = broker.dispatch(name, args)
                self.assertFalse(out["ok"])
                self.assertEqual(out["stage"], "request")
                self.assertFalse(out["reason"].startswith(("ValueError", "KeyError")), out)

    def test_schema_refusal_carries_suggestions(self):
        out = make_broker().dispatch("pacioli_tool_schema", {"name": "submit_sales_invoce"})
        self.assertFalse(out["ok"])
        self.assertIn("submit_sales_invoice", out["reason"])

    def test_call_delegates_and_returns_verbatim(self):
        broker = make_broker()
        direct = broker.dispatch("get_sales_invoice", {"name": "SI-1"})
        via = broker.dispatch("pacioli_call",
                              {"tool": "get_sales_invoice", "arguments": {"name": "SI-1"}})
        self.assertTrue(direct["ok"])
        self.assertEqual(direct, via)
        # refusals too — the doorway adds no second result shape
        self.assertEqual(broker.dispatch("no_such_tool", {}),
                         broker.dispatch("pacioli_call", {"tool": "no_such_tool"}))

    def test_call_refuses_the_doorway_itself(self):
        # Redteam F1 (mutation M6): the first version passed inner arguments that would refuse
        # ANYWAY, so deleting the self-dispatch guard left the suite green while nested doorway
        # calls succeeded. Inner args must be VALID — the refusal must come from the guard, and
        # the reason must say so.
        broker = make_broker()
        for inner, good_args in (("pacioli_find_tools", {"query": "invoice"}),
                                 ("pacioli_tool_schema", {"name": "get_sales_invoice"}),
                                 ("pacioli_call", {"tool": "get_sales_invoice",
                                                  "arguments": {"name": "SI-1"}})):
            with self.subTest(inner=inner):
                self.assertTrue(broker.dispatch(inner, good_args)["ok"],
                                "fixture rot: these args must succeed when dispatched directly")
                out = broker.dispatch("pacioli_call", {"tool": inner, "arguments": good_args})
                self.assertFalse(out["ok"])
                self.assertIn("does not dispatch itself", out["reason"])

    def test_schema_refuses_to_describe_the_doorway_with_the_designed_reason(self):
        # Redteam F4 (mutation M9): without this pin the designed message survives only by
        # accident of the KeyError fallback.
        broker = make_broker()
        for inner in sorted(DOORWAY_NAMES):
            out = broker.dispatch("pacioli_tool_schema", {"name": inner})
            self.assertFalse(out["ok"])
            self.assertIn("does not describe itself", out["reason"])

    def test_call_refuses_outer_keys_naming_pacioli_target(self):
        # Review finding 7: a silently-dropped outer pacioli_target retargets a read to the
        # DEFAULT books with ok:true — the refusal must name the key and say where it goes.
        out = make_broker().dispatch("pacioli_call", {
            "tool": "get_sales_invoice", "arguments": {"name": "SI-1"},
            "pacioli_target": "staging"})
        self.assertFalse(out["ok"])
        self.assertIn("pacioli_target", out["reason"])
        self.assertIn("arguments", out["reason"])

    def test_malformed_inputs_get_structured_denies_never_tracebacks(self):
        # The 07-26 redteam class: no door validates inputSchema, so schema-illegal-but-JSON-
        # legal values reach the handlers. Every one must land as the house envelope.
        broker = make_broker()
        cases = [
            ("pacioli_call", {}),                                      # tool missing
            ("pacioli_call", {"tool": ""}),                            # tool blank
            ("pacioli_call", {"tool": {"a": 1}}),                      # tool a dict
            ("pacioli_call", {"tool": "get_sales_invoice",
                              "arguments": "SI-1"}),                   # arguments a string
            ("pacioli_call", {"tool": "get_sales_invoice",
                              "arguments": ["SI-1"]}),                 # arguments a list
            ({"name": 1}, {}),                                         # dict-valued NAME (A2A
        ]                                                              # delivers raw JSON)
        for name, args in cases:
            with self.subTest(tool=name, args=args):
                out = broker.dispatch(name, args)
                self.assertFalse(out["ok"])
                self.assertIsInstance(out["reason"], str)

    def test_dispatch_raw_completes_through_the_doorway(self):
        # Review finding 2, the deadlock regression: the server doors hold the non-reentrant
        # _DISPATCH_LOCK while handlers run. pacioli_call must delegate via self.dispatch, so a
        # doorway call under the REAL locked entry point completes instead of deadlocking every
        # door forever. (A deadlock here hangs the test — that IS the detection.)
        broker = make_broker()
        out = dispatch_raw(broker, "pacioli_call",
                           {"tool": "get_sales_invoice", "arguments": {"name": "SI-1"}})
        self.assertTrue(out["ok"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
