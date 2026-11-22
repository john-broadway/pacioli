# Copyright (c) 2026, John Broadway and contributors
# License: AGPL-3.0-or-later
"""The core door: the vendor's table, the judgement beside it, and four tools that decide nothing.

What these hold: the table is the vendor's (every submittable doctype in the snapshot is judged
by the policy or the receipt is red); the policy cannot contradict the vendor or itself; a verb the
vendor's meta does not give a doctype is refused with the vendor's reason; a held doctype is refused
with its sentence and no client is touched; a served act routes to the SAME catalog tool that
governed it before, with the inner tool's own argument names; the 51 served rows equal the Python
table they were carried from, until that table goes.
"""
from __future__ import annotations

import gzip
import io
import json
import os
import re
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from pacioli.core import door
from pacioli.core.table import ACTS, META_PATH, POLICY_PATH, READS, PolicyError, Table, load_meta
from pacioli.doorway import served_tools
from pacioli.erpnext import SUPPORTED_DOCTYPES
from pacioli.registry import load_registry
from pacioli.store import BrokerStore
from pacioli.tools import DESCRIPTORS, READ_ONLY_TOOLS, TOOLS, PacioliBroker, _CORE_SLUGS

REG = ('[targets.prod]\nbase_url = "https://erp.example.com"\ncompany = "Example Corp"\n'
       'api_key = "k"\napi_secret = "env:S"\ndefault = true\n')
SLUGS = {d.doctype: (d.slug, d.plural) for d in DESCRIPTORS}
TABLE = Table.load()

# A held sentence's `source` is read against the vendor's tree, not matched for shape (lens
# 2026-10-16: a citation naming the wrong file passed the shape check). Citations are paths under
# the erpnext app, or under frappe when the word "frappe" precedes them; `:N` after a path is a line
# that must carry an identifier the prose names (or the creating call when the prose says "creates");
# a hook named after a path must be defined in it, "no <hook>" must not be; `in_create=1` is read
# out of the .json. The vendor tree is not in this repo: both roots come from the environment, and
# the check SKIPS LOUDLY without them (same law as the guard's wiring tests).
HOOKS = ("before_submit", "on_submit", "before_cancel", "on_cancel", "after_insert", "on_trash", "validate")
_CITE = re.compile(r"(?<![\w/])((?:\w+/)*\w+\.(?:py|json)|(?:\w+/){2,}\w+)(?![\w/.])")
_HOOK = re.compile(r"(no )?\b(" + "|".join(HOOKS) + r")\b")
_IDENT = re.compile(r"\b[a-z]\w*_\w+")


def citations(source):
    """(under_frappe, path, [lines], prose) for each citation in a held sentence's source."""
    ms = list(_CITE.finditer(source))
    for i, m in enumerate(ms):
        before = source[ms[i - 1].end() if i else 0:m.start()]
        after = source[m.end():ms[i + 1].start() if i + 1 < len(ms) else len(source)]
        yield "frappe" in before, m.group(1), [int(n) for n in re.findall(r":(\d+)", after)], after


def citation_problems(name, source, erpnext_root, frappe_root):
    """Every way a citation fails to say what the vendor's tree says; empty = the sentence was read."""
    out = []
    for under_frappe, path, lines, prose in citations(source):
        root = frappe_root if under_frappe else erpnext_root
        found = [f for f in ([root / path] if "/" in path else sorted(root.rglob(path))) if f.exists()]
        if len(found) != 1:
            out.append(f"{name}: {path} resolves to {len(found)} file(s) under {root.name}")
            continue
        target = found[0] / (found[0].name + ".py") if found[0].is_dir() else found[0]
        if not target.exists():
            out.append(f"{name}: {target.relative_to(root)} missing")
            continue
        text = target.read_text()
        creates = re.search(r"(?<!nothing )(?<!else )\bcreates? it\b", prose)   # "nothing (else) creates it" is not a creation claim
        if path.endswith(".json"):
            if "in_create=1" in prose and json.loads(text).get("in_create") != 1:
                out.append(f"{name}: {path} does not set in_create=1")
            continue
        for ln in lines:
            line = text.splitlines()[ln - 1] if ln <= text.count("\n") + 1 else ""
            wanted = _IDENT.findall(prose) + [name.lower().replace(" ", "_")]
            if creates:
                wanted += ["get_doc", "new_doc"]
            if not any(w in line for w in wanted):
                out.append(f"{name}: {path}:{ln} carries none of {sorted(set(wanted))}: {line.strip()[:80]!r}")
        if "no lifecycle hooks" in prose:
            present = [h for h in HOOKS if f"def {h}(" in text]
            if present:
                out.append(f"{name}: {path} defines {present}, the sentence says none")
        for negated, hook in _HOOK.findall(prose):
            if (f"def {hook}(" in text) == bool(negated):
                out.append(f"{name}: {path} {'defines' if negated else 'lacks'} {hook}")
        if not lines and creates and name not in text:
            out.append(f"{name}: {path} never names {name!r}, the sentence says it creates it")
    return out


class HeldSentenceReadTest(unittest.TestCase):
    """The 25 held sentences, read against the vendor's own tree (skips loudly without it)."""

    def setUp(self):
        erp, fr = os.environ.get("PACIOLI_ERPNEXT_REFS", ""), os.environ.get("PACIOLI_FRAPPE_REFS", "")
        if not (erp and fr):
            self.skipTest("PACIOLI_ERPNEXT_REFS / PACIOLI_FRAPPE_REFS unset — the held sentences' citations "
                          "are UNVERIFIED in this environment. Point them at erpnext and frappe checkouts.")
        self.erp, self.fr = Path(erp) / "erpnext", Path(fr) / "frappe"
        for root in (self.erp, self.fr):
            if not (root / "hooks.py").exists():
                self.skipTest(f"no hooks.py under {root} — the citations are UNVERIFIED in this environment.")

    def problems(self, name, source):
        return citation_problems(name, source, self.erp, self.fr)

    def test_every_held_citation_resolves_and_says_what_the_line_says(self):
        for name, h in TABLE.held.items():
            self.assertEqual(self.problems(name, h.source), [], name)
            self.assertTrue(any(True for _ in citations(h.source)), f"{name}: no citation at all")

    def test_the_lens_wrong_file_and_three_planted_lies_go_red(self):
        # the 2026-10-16 lens misread: right line number, wrong file — the shape check passed it
        wrong = "accounts/doctype/process_period_closing_voucher/process_period_closing_voucher.py:138 creates it"
        self.assertEqual(len(self.problems("Process Period Closing Voucher", wrong)), 1)
        self.assertIn("carries none of", self.problems("Process Period Closing Voucher", wrong)[0])
        self.assertIn("resolves to 0", self.problems("X", "accounts/doctype/no_such/no_such.py (on_submit)")[0])
        self.assertIn("lacks on_submit", self.problems("Cashier Closing", "accounts/doctype/cashier_closing/cashier_closing.py (on_submit)")[0])
        self.assertIn("defines validate", self.problems("Cashier Closing", "accounts/doctype/cashier_closing/cashier_closing.py (no validate)")[0])
        self.assertIn("never names", self.problems("Mystery Voucher", "accounts/utils.py creates it")[0])
        self.assertIn("in_create", self.problems("Sales Invoice", "sales_invoice.json in_create=1")[0])
        self.assertEqual(self.problems("DuckDB Sync", "frappe: core/doctype/duckdb_sync/duckdb_sync.py:45 on_submit"), [])


class _Client:
    """Records every call; answers a read."""

    def __init__(self):
        self.calls = []

    def get_document(self, doctype, name):
        self.calls.append(("get_document", doctype, name))
        return {"name": name, "doctype": doctype, "docstatus": 0}

    def __getattr__(self, item):
        def rec(*a, **k):
            self.calls.append((item, a, k))
            raise AssertionError(f"unexpected client call {item}")
        return rec


def make_broker(client):
    return PacioliBroker(registry=load_registry(toml_text=REG),
                         store_provider=lambda name: BrokerStore(sqlite3.connect(":memory:"), key=b"k" * 32),
                         client_provider=lambda target: client,
                         now_epoch=lambda: 1_000.0, now_date=lambda: "2026-07-01")


def write_policy(text: str) -> Path:
    f = tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False)
    f.write(text)
    f.close()
    return Path(f.name)


def write_meta(rows: list[dict]) -> Path:
    f = tempfile.NamedTemporaryFile("wb", suffix=".json.gz", delete=False)
    f.close()
    with gzip.open(f.name, "wt") as g:
        json.dump({"provenance": {"fetched": "t", "apps": {"erpnext": {"version": "x"}}}, "doctypes": rows}, g)
    return Path(f.name)


def row(name, **kw):
    base = dict(name=name, app="erpnext", module="Accounts", submittable=1, child=0, single=0, in_create=0)
    base.update(kw)
    return base


# --- the table ---------------------------------------------------------------------------------
class TestTable(unittest.TestCase):
    def test_the_snapshot_is_the_vendors_and_dated(self):
        docs, prov = load_meta(META_PATH)
        self.assertEqual(set(prov["apps"]), {"frappe", "erpnext"})
        self.assertTrue(prov["apps"]["frappe"]["version"].startswith("16."))
        self.assertEqual(prov["writer"], "scripts/vendor_meta.py fetch")
        self.assertIn("Sales Invoice", docs)
        self.assertTrue(docs["Sales Invoice"].submittable)
        self.assertTrue(docs["Sales Invoice Item"].child)
        self.assertTrue(docs["Accounts Settings"].single)
        self.assertTrue(docs["Payment Request"].in_create)

    def test_every_submittable_doctype_is_judged(self):
        c = TABLE.coverage()
        self.assertEqual(c["unjudged"], [], c["unjudged"])
        self.assertEqual(len(c["served"]), 51)
        self.assertEqual(len(c["served"]) + len(c["held"]), c["submittable"])
        self.assertEqual(c["held"], c["held_ask"])          # every held sentence is Claude's, awaiting a ruling

    def test_the_served_rows_equal_the_python_table_they_were_carried_from(self):
        self.assertEqual(set(TABLE.served), set(SUPPORTED_DOCTYPES))
        for name, cfg in SUPPORTED_DOCTYPES.items():
            s = TABLE.served[name]
            self.assertEqual(s.party_field or None, cfg.get("party_field"), name)
            self.assertEqual(s.date_field or None, cfg.get("date_field"), name)
            self.assertEqual(s.amount_field or None, cfg.get("amount_field"), name)
            self.assertEqual(s.submit_via, cfg["submit_via"], name)

    def test_verbs_follow_the_vendors_meta(self):
        self.assertEqual(TABLE.verbs_for("Sales Invoice"), READS + ACTS)
        self.assertEqual(TABLE.verbs_for("Customer"), READS)             # not submittable
        self.assertEqual(TABLE.verbs_for("Sales Invoice Item"), ())      # child table
        self.assertEqual(TABLE.verbs_for("Accounts Settings"), ("get",))  # single
        self.assertEqual(TABLE.verbs_for("No Such Thing"), ())

    def test_judge_speaks_the_vendors_reason_then_the_policys(self):
        self.assertTrue(TABLE.judge("Sales Invoice", "submit").ok)
        self.assertTrue(TABLE.judge("Customer", "get").ok)
        self.assertIn("not submittable", TABLE.judge("Customer", "submit").reason)
        self.assertIn("child table", TABLE.judge("Sales Invoice Item", "get").reason)
        self.assertIn("Single", TABLE.judge("Accounts Settings", "list").reason)
        self.assertIn("not in the vendor's table", TABLE.judge("Sales Invoce", "get").reason)
        self.assertIn("frappe 16.", TABLE.judge("Sales Invoce", "get").reason)
        held = TABLE.judge("Transaction Deletion Record", "submit")
        self.assertFalse(held.ok)
        self.assertIn("held", held.reason)
        self.assertIn("awaiting a ruling", held.reason)
        self.assertTrue(TABLE.judge("Transaction Deletion Record", "get").ok)   # reads are the floor's to judge
        self.assertIn("not a verb", TABLE.judge("Sales Invoice", "delete").reason)

    def test_an_unjudged_submittable_is_refused_and_named_by_the_receipt(self):
        meta = write_meta([row("Sales Invoice"), row("Mystery Voucher")])
        policy = write_policy('[served."Sales Invoice"]\nsubmit_via = "run_method"\nwhy = "x"\n')
        t = Table.load(meta, policy)
        self.assertEqual(t.coverage()["unjudged"], ["Mystery Voucher"])
        self.assertIn("unjudged", t.judge("Mystery Voucher", "submit").reason)
        self.assertTrue(t.judge("Mystery Voucher", "list").ok)

    def test_the_policy_may_not_contradict_the_vendor_or_itself(self):
        meta = write_meta([row("Sales Invoice", fields=["customer", "posting_date"]), row("Customer", submittable=0),
                           row("Sales Invoice Item", child=1)])
        for text, words in (
            ('[served."Sales Invoice"]\nsubmit_via = "run_method"\nwhy = "x"\n[held."Sales Invoice"]\nwhy = "y"\n', "served AND held"),
            ('[held."Nope"]\nwhy = "y"\n', "not in the vendor's table"),
            ('[held."Customer"]\nwhy = "y"\n', "not submittable"),
            ('[held."Sales Invoice Item"]\nwhy = "y"\n', "child table"),
            ('[served."Sales Invoice"]\nsubmit_via = "magic"\nwhy = "x"\n', "submit_via"),
            ('[served."Sales Invoice"]\nsubmit_via = "run_method"\nwhy = " "\n', "carries a why"),
            ('[served."Sales Invoice"]\nsubmit_via = "run_method"\ndate_field = "posting_dt"\nwhy = "x"\n',
             "not a field of Sales Invoice"),
        ):
            with self.assertRaises(PolicyError, msg=words) as ctx:
                Table.load(meta, write_policy(text))
            self.assertIn(words, str(ctx.exception))

    def test_every_committed_held_sentence_names_its_source_in_the_vendor(self):
        # A sentence John will rule on says where its mechanism claim was READ, never recalled
        # (lens 2026-10-10: one recalled sentence named a child table as a CRM document).
        for name, h in TABLE.held.items():
            self.assertTrue(h.ask, name)
            self.assertRegex(h.source, r"\.py|\.json|hooks\.py|doctype/", name)   # the floor; HeldSentenceReadTest reads it

    def test_the_committed_policy_points_only_at_fields_the_vendor_has(self):
        self.assertTrue(TABLE.fields_checked)
        self.assertTrue(TABLE.coverage()["fields_checked"])
        self.assertIn("posting_date", TABLE.doctypes["Sales Invoice"].fields)
        self.assertNotIn("Contract Fulfilment Checklist", TABLE.held)   # a child table: nothing to judge

    def test_a_snapshot_without_fields_skips_the_field_check_and_says_so(self):
        meta = write_meta([row("Sales Invoice")])    # no "fields" key at all
        t = Table.load(meta, write_policy('[served."Sales Invoice"]\nsubmit_via = "run_method"\ndate_field = "zzz"\nwhy = "x"\n'))
        self.assertFalse(t.fields_checked)
        self.assertFalse(t.coverage()["fields_checked"])

    def test_find_is_words_to_addresses_with_status(self):
        rows = TABLE.find("sales invoice submit")
        self.assertEqual(rows[0], {"address": "Sales Invoice submit", "status": "served",
                                   "why": TABLE.served["Sales Invoice"].why[:140]})
        rows = {r["address"]: r for r in TABLE.find("period closing")}
        self.assertEqual(rows["Period Closing Voucher submit"]["status"], "held")
        # a READ of a held doctype is not this policy's to judge and must never wear the held sentence
        self.assertEqual(rows["Period Closing Voucher get"]["status"], "read")
        self.assertNotIn("human ceremony", rows["Period Closing Voucher get"]["why"])
        self.assertIn("scope", rows["Period Closing Voucher get"]["why"])
        self.assertEqual(TABLE.find("zzz-no-such-doctype"), [])
        with self.assertRaises(ValueError):
            TABLE.find("  ")
        self.assertEqual(len(TABLE.find("stock", limit=3)), 3)


# --- the route ---------------------------------------------------------------------------------
class TestRoute(unittest.TestCase):
    def route(self, tool, **args):
        return door.route(TABLE, tool, args, SLUGS)

    def test_served_acts_route_to_the_catalog_tool_with_its_own_argument_names(self):
        self.assertEqual(self.route("pacioli_plan", doctype="Sales Invoice", act="submit", name="SI-1", consent_token="c"),
                         ("plan_submit", {"name": "SI-1", "pacioli_doctype": "Sales Invoice", "consent_token": "c"}, None))
        self.assertEqual(self.route("pacioli_plan", doctype="Sales Invoice", act="cascade_cancel", name="SI-1")[0],
                         "plan_cascade_cancel")
        self.assertEqual(self.route("pacioli_read", doctype="Sales Invoice", name="SI-1", pacioli_target="prod"),
                         ("get_sales_invoice", {"name": "SI-1", "pacioli_target": "prod"}, None))
        self.assertEqual(self.route("pacioli_read", doctype="Journal Entry", filters={"a": 1}, limit=5),
                         ("list_journal_entries", {"filters": {"a": 1}, "limit": 5}, None))
        self.assertEqual(self.route("pacioli_act", doctype="Payment Entry", act="cancel", name="PE-1", plan_id="p", marker="m"),
                         ("cancel_payment_entry", {"name": "PE-1", "plan_id": "p", "marker": "m"}, None))
        self.assertEqual(self.route("pacioli_act", doctype="Payment Entry", act="amend", name="PE-1"),
                         ("amend_payment_entry", {"name": "PE-1"}, None))

    def test_refusals_carry_the_true_reason(self):
        self.assertIn("held", self.route("pacioli_act", doctype="Repost Accounting Ledger", act="submit",
                                         name="x", plan_id="p", marker="m")[2])
        self.assertIn("not submittable", self.route("pacioli_plan", doctype="Customer", act="submit", name="x")[2])
        self.assertIn("not in the vendor's table", self.route("pacioli_read", doctype="Sales Invoce", name="x")[2])
        self.assertIn("plan_id", self.route("pacioli_act", doctype="Sales Invoice", act="submit", name="x")[2])
        self.assertIn("act must be one of", self.route("pacioli_act", doctype="Sales Invoice", act="delete", name="x")[2])
        self.assertIn("act must be one of", self.route("pacioli_plan", doctype="Sales Invoice", act="amend", name="x")[2])
        self.assertIn("does not take", self.route("pacioli_read", doctype="Sales Invoice", pacioli_doctype="x")[2])
        self.assertIn("not both", self.route("pacioli_read", doctype="Sales Invoice", name="x", filters={})[2])
        self.assertIn("non-empty", self.route("pacioli_read", doctype="Sales Invoice", name="")[2])
        self.assertIn("requires 'doctype'", self.route("pacioli_read", name="x")[2])
        self.assertIn("requires 'name'", self.route("pacioli_plan", doctype="Sales Invoice", act="submit")[2])
        self.assertIn("takes an object", door.route(TABLE, "pacioli_read", ["x"], SLUGS)[2])

    def test_a_served_but_unwired_doctype_refuses_on_wiring_not_policy(self):
        meta = write_meta([row("Future Voucher")])
        policy = write_policy('[served."Future Voucher"]\nsubmit_via = "run_method"\nwhy = "x"\n')
        t = Table.load(meta, policy)
        _, _, why = door.route(t, "pacioli_read", {"doctype": "Future Voucher"}, SLUGS)
        self.assertIn("no list tool wired", why)
        self.assertIn("not a refusal of the act", why)

    def test_reads_of_a_held_doctype_route_when_the_catalog_has_them(self):
        # Period Closing Voucher is held for acts; the catalog has no read tool for it either, so
        # the refusal is the WIRING one, and a held doctype WITH a read tool would route.
        _, _, why = self.route("pacioli_read", doctype="Period Closing Voucher", name="x")
        self.assertIn("no get tool wired", why)


# --- the door on the broker --------------------------------------------------------------------
class TestDoorOnBroker(unittest.TestCase):
    def test_served_tools_core_is_six_and_the_other_modes_are_unchanged(self):
        core = served_tools({"PACIOLI_TOOLSETS": "core"})
        self.assertEqual([t["name"] for t in core],
                         ["pacioli_find", "pacioli_plan", "pacioli_read", "pacioli_act", "prove_verify", "prove_orphans"])
        self.assertLess(len(json.dumps(core)), 6_000)
        self.assertEqual(len(served_tools({"PACIOLI_TOOLSETS": "dynamic"})), 9)
        self.assertEqual(len(served_tools({"PACIOLI_TOOLSETS": ""})), len(TOOLS))
        with self.assertRaises(ValueError):
            served_tools({"PACIOLI_TOOLSETS": "cor"})

    def test_find_and_read_are_read_only_plan_and_act_are_not(self):
        self.assertIn("pacioli_find", READ_ONLY_TOOLS)
        self.assertIn("pacioli_read", READ_ONLY_TOOLS)
        self.assertNotIn("pacioli_plan", READ_ONLY_TOOLS)
        self.assertNotIn("pacioli_act", READ_ONLY_TOOLS)
        self.assertEqual(set(_CORE_SLUGS), set(SUPPORTED_DOCTYPES))

    def test_a_read_flows_through_the_catalog_tool(self):
        client = _Client()
        out = make_broker(client).dispatch("pacioli_read", {"doctype": "Sales Invoice", "name": "SI-1"})
        self.assertTrue(out.get("ok"), out)
        self.assertEqual(client.calls, [("get_document", "Sales Invoice", "SI-1")])

    def test_a_held_act_is_refused_before_any_client_call(self):
        client = _Client()
        out = make_broker(client).dispatch("pacioli_act", {"doctype": "Transaction Deletion Record", "act": "submit",
                                                            "name": "TDR-1", "plan_id": "p", "marker": "m"})
        self.assertFalse(out.get("ok"))
        self.assertIn("held", json.dumps(out))
        self.assertEqual(client.calls, [])

    def test_an_unknown_key_is_refused_by_the_dispatcher_with_the_core_tools_declared_args(self):
        client = _Client()
        out = make_broker(client).dispatch("pacioli_read", {"doctype": "Sales Invoice", "pacioli_doctype": "x"})
        self.assertFalse(out.get("ok"))
        self.assertEqual(client.calls, [])

    def test_the_core_doors_own_keys_do_not_become_inner_arg_hints_on_pacioli_call(self):
        # The 2026-07-30 pair: inner tools spell it `pacioli_doctype`. A bare `doctype` sent to
        # pacioli_call must NOT be told "this belongs inside arguments" just because the core door
        # declares a `doctype` of its own; `pacioli_doctype` (a true inner arg) still is.
        broker = make_broker(_Client())
        wrong = broker.dispatch("pacioli_call", {"tool": "get_sales_invoice", "arguments": {"name": "x"}, "doctype": "Sales Invoice"})
        inner = broker.dispatch("pacioli_call", {"tool": "get_sales_invoice", "arguments": {"name": "x"}, "pacioli_doctype": "Sales Invoice"})
        self.assertFalse(wrong["ok"])
        self.assertFalse(inner["ok"])
        self.assertIn("everything the inner tool accepts", json.dumps(inner))
        self.assertNotIn("everything the inner tool accepts", json.dumps(wrong))

    def test_find_answers_in_process(self):
        client = _Client()
        out = make_broker(client).dispatch("pacioli_find", {"query": "journal"})
        self.assertTrue(out["ok"])
        self.assertIn({"address": "Journal Entry submit", "status": "served", "why": TABLE.served["Journal Entry"].why[:140]},
                      out["results"])
        self.assertEqual(client.calls, [])
        self.assertFalse(make_broker(client).dispatch("pacioli_find", {"query": " "})["ok"])


# --- the receipt script ------------------------------------------------------------------------
class TestCoverageScript(unittest.TestCase):
    def run_cov(self, *argv):
        sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
        import vendor_meta  # noqa: PLC0415
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = vendor_meta.main(["coverage", *argv])
        return rc, out.getvalue(), err.getvalue()

    def test_the_committed_table_is_green(self):
        rc, out, _ = self.run_cov("--meta", str(META_PATH), "--policy", str(POLICY_PATH))
        self.assertEqual(rc, 0, out)
        self.assertIn("COVERAGE_OK", out)
        self.assertIn("served 51", out)

    def test_fetch_refuses_a_vendor_file_that_will_not_parse(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
        import vendor_meta  # noqa: PLC0415
        src = Path(tempfile.mkdtemp())
        for app in ("frappe", "erpnext"):
            d = src / app / "mod" / "doctype" / "thing"
            d.mkdir(parents=True)
            (src / app / "__init__.py").write_text('__version__ = "1.0.0"\n')
            (d / "thing.json").write_text(json.dumps({"doctype": "DocType", "name": f"{app} Thing", "module": "Mod",
                                                      "fields": [{"fieldname": "posting_date", "fieldtype": "Date"},
                                                                 {"fieldname": "sb", "fieldtype": "Section Break"}]}))
        out = Path(tempfile.mkdtemp()) / "m.json.gz"
        with redirect_stdout(io.StringIO()):
            self.assertEqual(vendor_meta.main(["fetch", str(src), str(src), "--out", str(out)]), 0)
        docs, _ = load_meta(out)
        self.assertEqual(docs["erpnext Thing"].fields, ("posting_date",))   # layout fieldtypes dropped
        (src / "erpnext" / "mod" / "doctype" / "thing" / "broken.json").write_text("{not json")
        with self.assertRaises(SystemExit) as ctx, redirect_stdout(io.StringIO()):
            vendor_meta.main(["fetch", str(src), str(src), "--out", str(out)])
        self.assertIn("did not parse", str(ctx.exception))

    def test_a_planted_unjudged_doctype_reds_the_receipt(self):
        meta = write_meta([row("Sales Invoice"), row("Mystery Voucher")])
        policy = write_policy('[served."Sales Invoice"]\nsubmit_via = "run_method"\nwhy = "x"\n')
        rc, out, err = self.run_cov("--meta", str(meta), "--policy", str(policy))
        self.assertEqual(rc, 1)
        self.assertIn("UNJUDGED Mystery Voucher", out)
        self.assertIn("COVERAGE_RED", err)


if __name__ == "__main__":
    unittest.main()
