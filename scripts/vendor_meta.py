#!/usr/bin/env python3
"""vendor_meta.py: the vendor's own table of DocTypes, fetched from the vendor, dated by provenance.

    vendor_meta.py fetch <frappe-src> <erpnext-src> [--out PATH]   write broker/pacioli/core/meta/doctypes.json.gz
    vendor_meta.py coverage [--meta PATH] [--policy PATH]           the receipt; exit 1 if any submittable is unjudged

`fetch` reads every `**/doctype/*/*.json` whose top-level `doctype` is "DocType" under each app
source tree, and the app's `__version__`. It is the ONLY thing that writes the snapshot, the way
Proximo's `api_coverage.py fetch` is the only thing that writes its `apidoc/*.ops.json`. Re-fetch
when the vendor moves and commit the diff, so the table is dated by this file, not by memory.

`coverage` loads the snapshot and `doctypes.toml` through the same loader the door uses and
prints, per submittable doctype, served / held / unjudged. An unjudged row is a red receipt: the
door would refuse it, and the policy owes it a sentence.
"""
from __future__ import annotations

import argparse
import glob
import gzip
import json
import os
import re
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEFAULT_OUT = REPO / "broker" / "pacioli" / "core" / "meta" / "doctypes.json.gz"


def app_version(src: Path, app: str) -> str:
    text = (src / app / "__init__.py").read_text(encoding="utf-8")
    m = re.search(r'^__version__\s*=\s*"([^"]+)"', text, re.M)
    if not m:
        raise SystemExit(f"{app}: no __version__ in {src / app / '__init__.py'}")
    return m.group(1)


LAYOUT_FIELDTYPES = {"Section Break", "Column Break", "Tab Break", "Fold", "HTML", "Heading", "Button", "Image"}


def read_doctypes(src: Path, app: str) -> list[dict]:
    """Every DocType under the app, with the field names a policy row may point at. A vendor file
    that will not parse FAILS the fetch: a doctype that exists but was never read would leave the
    receipt green and the door refusing it as 'not in the table', which is a lie about the vendor."""
    rows, broken = [], []
    for f in sorted(glob.glob(str(src / app / "**" / "doctype" / "*" / "*.json"), recursive=True)):
        try:
            d = json.load(open(f, encoding="utf-8"))
        except (ValueError, OSError) as exc:
            broken.append(f"{f}: {type(exc).__name__}")
            continue
        if not isinstance(d, dict) or d.get("doctype") != "DocType":
            continue
        fields = sorted({x["fieldname"] for x in d.get("fields", [])
                         if x.get("fieldname") and x.get("fieldtype") not in LAYOUT_FIELDTYPES})
        rows.append({"name": d["name"], "app": app, "module": d.get("module") or "",
                     "submittable": int(bool(d.get("is_submittable"))), "child": int(bool(d.get("istable"))),
                     "single": int(bool(d.get("issingle"))), "in_create": int(bool(d.get("in_create"))),
                     "fields": fields})
    if broken:
        raise SystemExit(f"{app}: {len(broken)} DocType file(s) did not parse; the table would be incomplete:\n  "
                         + "\n  ".join(broken))
    if not rows:
        raise SystemExit(f"{app}: no DocType definitions under {src}; is this the app's source tree?")
    return rows


def fetch(frappe_src: Path, erpnext_src: Path, out: Path) -> int:
    apps = {}
    rows: list[dict] = []
    for src, app in ((frappe_src, "frappe"), (erpnext_src, "erpnext")):
        apps[app] = {"version": app_version(src, app), "doctypes": 0}
        got = read_doctypes(src, app)
        apps[app]["doctypes"] = len(got)
        rows.extend(got)
    names = [r["name"] for r in rows]
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        raise SystemExit(f"a DocType name appears in both apps: {dupes}")
    snapshot = {"provenance": {"fetched": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                               "writer": "scripts/vendor_meta.py fetch", "apps": apps},
                "doctypes": sorted(rows, key=lambda r: r["name"])}
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8", compresslevel=9) as f:
        json.dump(snapshot, f, separators=(",", ":"), sort_keys=True)
    os.replace(tmp, out)
    subs = sum(r["submittable"] for r in rows if not r["child"])
    print(f"wrote {out} ({out.stat().st_size} B): {len(rows)} doctypes, {subs} submittable (child tables "
          f"excluded), {sum(len(r['fields']) for r in rows)} field names; "
          + ", ".join(f"{a} {v['version']}" for a, v in apps.items()))
    return 0


def coverage(meta: Path | None, policy: Path | None) -> int:
    sys.path.insert(0, str(REPO / "broker"))
    from pacioli.core.table import META_PATH, POLICY_PATH, Table  # noqa: PLC0415

    t = Table.load(meta or META_PATH, policy or POLICY_PATH)
    c = t.coverage()
    print(f"vendor: {c['versions']} | doctypes {c['doctypes']} | submittable {c['submittable']} | "
          f"policy field names checked against the vendor: {'yes' if c['fields_checked'] else 'NO (snapshot has no fields)'}")
    print(f"served {len(c['served'])} | held {len(c['held'])} (ask {len(c['held_ask'])}) | unjudged {len(c['unjudged'])}")
    for n in c["held"]:
        h = t.held[n]
        print(f"  held     {n}{'  [ask]' if n in c['held_ask'] else ''}: {h.why}"
              + (f"\n           source: {h.source}" if h.source else "\n           source: NONE (unsourced sentence)"))
    for n in c["unjudged"]:
        print(f"  UNJUDGED {n}: no row in doctypes.toml")
    if c["unjudged"]:
        print("COVERAGE_RED: every submittable doctype needs a served or held row", file=sys.stderr)
        return 1
    print("COVERAGE_OK")
    return 0


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch")
    f.add_argument("frappe_src", type=Path)
    f.add_argument("erpnext_src", type=Path)
    f.add_argument("--out", type=Path, default=DEFAULT_OUT)
    c = sub.add_parser("coverage")
    c.add_argument("--meta", type=Path)
    c.add_argument("--policy", type=Path)
    a = p.parse_args(argv)
    if a.cmd == "fetch":
        return fetch(a.frappe_src, a.erpnext_src, a.out)
    return coverage(a.meta, a.policy)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
