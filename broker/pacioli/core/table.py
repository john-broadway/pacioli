# Copyright (c) 2026, John Broadway and contributors
# License: AGPL-3.0-or-later
"""THE TABLE: the vendor's doctypes, read from the vendor, and the judgement that sits beside them.

Two files, two authors, one object:

* ``meta/doctypes.json.gz`` is WRITTEN BY A SCRIPT from the vendor's own DocType definitions
  (``scripts/vendor_meta.py fetch``): every DocType in frappe and erpnext with the facts the door
  needs (submittable, child table, single, system-created). Dated by its provenance block, never
  by memory. Nobody edits it by hand.
* ``doctypes.toml`` is WRITTEN BY A PERSON: for every submittable doctype, how the spine governs
  it (the party, date and amount fields a PLAN reads, the verb that submits it) and a sentence
  saying why, or the sentence that holds it. Editing that file IS the act of granting.

The address space is ``(doctype, verb)``. The vendor decides which verbs a doctype HAS (a child
table has none, a Single has only ``get``, a non-submittable has no lifecycle). The policy decides
which lifecycle acts the door SERVES. A submittable doctype with no row in the policy is refused
as unjudged: the door fails closed, and the coverage receipt names it.
"""
from __future__ import annotations

import gzip
import json
import tomllib
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
META_PATH = HERE / "meta" / "doctypes.json.gz"
POLICY_PATH = HERE / "doctypes.toml"

READS = ("get", "list")
ACTS = ("submit", "cancel", "amend")
VERBS = READS + ACTS
SUBMIT_VIAS = ("run_method", "client_rpc")


@dataclass(frozen=True)
class Doctype:
    name: str
    app: str
    module: str
    submittable: bool
    child: bool
    single: bool
    in_create: bool      # the vendor's "created by the system, not from the form" flag
    fields: tuple[str, ...] | None   # the vendor's field names; None = a snapshot that did not carry them


@dataclass(frozen=True)
class Served:
    party_field: str | None
    date_field: str | None
    amount_field: str | None
    submit_via: str
    why: str


@dataclass(frozen=True)
class Held:
    why: str
    ask: bool            # true = Claude's sentence, awaiting John's ruling
    source: str = ""     # where in the vendor's source the sentence's mechanism claim was read


@dataclass(frozen=True)
class Verdict:
    ok: bool
    reason: str = ""


class PolicyError(ValueError):
    """The policy file contradicts the vendor's table or itself. Refused at load, never served."""


def load_meta(path: Path = META_PATH) -> tuple[dict[str, Doctype], dict]:
    with gzip.open(path, "rt", encoding="utf-8") as f:
        raw = json.load(f)
    docs = {d["name"]: Doctype(d["name"], d["app"], d["module"], bool(d["submittable"]), bool(d["child"]),
                               bool(d["single"]), bool(d["in_create"]),
                               tuple(d["fields"]) if "fields" in d else None) for d in raw["doctypes"]}
    return docs, raw["provenance"]


def load_policy(path: Path = POLICY_PATH) -> tuple[dict[str, Served], dict[str, Held]]:
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    served, held = {}, {}
    for name, row in (raw.get("served") or {}).items():
        via = row.get("submit_via")
        if via not in SUBMIT_VIAS:
            raise PolicyError(f"served.{name!r}: submit_via must be one of {SUBMIT_VIAS}, got {via!r}")
        if not (row.get("why") or "").strip():
            raise PolicyError(f"served.{name!r}: a served row carries a why")
        served[name] = Served(row.get("party_field"), row.get("date_field"), row.get("amount_field"), via, row["why"])
    for name, row in (raw.get("held") or {}).items():
        if not (row.get("why") or "").strip():
            raise PolicyError(f"held.{name!r}: a held row carries a why")
        held[name] = Held(row["why"], bool(row.get("ask", False)), row.get("source", ""))
    both = sorted(set(served) & set(held))
    if both:
        raise PolicyError(f"served AND held: {both}")
    return served, held


class Table:
    def __init__(self, doctypes: dict[str, Doctype], served: dict[str, Served], held: dict[str, Held],
                 provenance: dict):
        """Refuse a policy that contradicts the vendor: a doctype the vendor lacks, a child table (its
        rows have no lifecycle of their own), a non-submittable judged, or a served row pointing at a
        field the doctype does not have. The field check needs a snapshot that carries field names;
        on one that does not, it is SKIPPED and `fields_checked` says so."""
        for name in list(served) + list(held):
            if name not in doctypes:
                raise PolicyError(f"doctypes.toml names {name!r}, which is not in the vendor's table")
            d = doctypes[name]
            if d.child:
                raise PolicyError(f"doctypes.toml judges {name!r}, a child table: its rows live inside a parent")
            if not d.submittable:
                raise PolicyError(f"doctypes.toml judges {name!r}, which the vendor says is not submittable")
        self.fields_checked = all(doctypes[n].fields is not None for n in served)
        for name, row in served.items():
            known = doctypes[name].fields
            if known is None:
                continue
            for attr in ("party_field", "date_field", "amount_field"):
                f = getattr(row, attr)
                if f and f not in known:
                    raise PolicyError(f"served.{name!r}: {attr} = {f!r} is not a field of {name} in the vendor's table")
        self.doctypes, self.served, self.held, self.provenance = doctypes, served, held, provenance

    @classmethod
    def load(cls, meta_path: Path = META_PATH, policy_path: Path = POLICY_PATH) -> "Table":
        docs, prov = load_meta(meta_path)
        served, held = load_policy(policy_path)
        return cls(docs, served, held, prov)

    def versions(self) -> str:
        apps = self.provenance.get("apps", {})
        return ", ".join(f"{a} {apps[a].get('version', '?')}" for a in sorted(apps))

    def verbs_for(self, doctype: str) -> tuple[str, ...]:
        d = self.doctypes.get(doctype)
        if d is None or d.child:
            return ()
        if d.single:
            return ("get",)
        return VERBS if d.submittable else READS

    def judge(self, doctype: str, verb: str) -> Verdict:
        if verb not in VERBS:
            return Verdict(False, f"{verb!r} is not a verb this door knows: {', '.join(VERBS)}")
        d = self.doctypes.get(doctype)
        if d is None:
            return Verdict(False, f"{doctype!r} is not in the vendor's table ({self.versions()}): check the "
                                  "spelling against the DocType name, singular, as frappe spells it")
        if d.child:
            return Verdict(False, f"{doctype} is a child table: its rows live inside a parent document; "
                                  "address the parent")
        if d.single and verb != "get":
            return Verdict(False, f"{doctype} is a Single: one record, no list and no lifecycle")
        if verb in ACTS:
            if not d.submittable:
                return Verdict(False, f"the vendor's meta says {doctype} is not submittable: it has no submit, "
                                      "cancel or amend")
            if doctype in self.held:
                h = self.held[doctype]
                return Verdict(False, f"{doctype} {verb} is held: {h.why}"
                                      + (" (Claude's sentence, awaiting a ruling)" if h.ask else ""))
            if doctype not in self.served:
                return Verdict(False, f"{doctype} {verb} is unjudged: no row in doctypes.toml says how it is "
                                      "governed, so the door fails closed")
        return Verdict(True)

    def find(self, query: str, limit: int = 10) -> list[dict]:
        """Words to addresses. Every term must match the doctype's name or module; results carry the
        policy's status so a model learns what is held before it asks."""
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must not be blank")
        terms = query.lower().split()
        verb_terms = [t for t in terms if t in VERBS]
        noun_terms = [t for t in terms if t not in VERBS]
        out = []
        for name in sorted(self.doctypes):
            d = self.doctypes[name]
            hay = f"{name} {d.module}".lower()
            if noun_terms and not all(t in hay for t in noun_terms):
                continue
            verbs = self.verbs_for(name)
            if not verbs:
                continue
            for verb in (verb_terms or verbs):
                if verb not in verbs:
                    continue
                if verb in READS:
                    # reads are not this policy's to judge, and must never wear a held sentence
                    status, why = "read", "reads are governed by the seat's scope on the floor, not by this policy"
                elif name in self.held:
                    status, why = "held", self.held[name].why
                elif name in self.served:
                    status, why = "served", self.served[name].why
                else:
                    status, why = "unjudged", "no row in doctypes.toml; the door refuses this act"
                out.append({"address": f"{name} {verb}", "status": status, "why": why[:140]})
        return out[: max(1, min(50, int(limit)))]

    def coverage(self) -> dict:
        """The receipt: every submittable doctype the vendor has, sorted by what the policy says."""
        subs = sorted(n for n, d in self.doctypes.items() if d.submittable and not d.child)
        served = [n for n in subs if n in self.served]
        held = [n for n in subs if n in self.held]
        unjudged = [n for n in subs if n not in self.served and n not in self.held]
        return {"versions": self.versions(), "doctypes": len(self.doctypes), "submittable": len(subs),
                "served": served, "held": held, "held_ask": [n for n in held if self.held[n].ask],
                "unjudged": unjudged, "fields_checked": self.fields_checked}
