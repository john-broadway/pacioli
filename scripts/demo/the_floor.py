#!/usr/bin/env python3
"""Pacioli demo driver - "The floor." A narrated, RECORDABLE walk through the credential floor
and the receipt chain.

The whole point of this demo is that it is REAL: every decision below is made by Pacioli's own
shipped code, and every seal is a real HMAC. Nothing is faked, and that is the pitch.

Two modes:
  --local   (default)  Self-contained. Two pillars, no ERP, no network:
                         1. THE FLOOR  - pacioli_guard.scope decides allow/deny on the SAME call for
                            an unscoped credential (frappe's default: everything the user can do) vs
                            a floor-scoped one (deny-by-default). Real `is_permitted`, real `classify`.
                         2. THE RECORD - pacioli.prove seals three receipts into a keyed chain, then
                            a tampered row and a truncated tail are both caught. Real HMAC linkage.
                       Needs only `pip install pacioli pacioli-guard`. Runs anywhere, for anyone.
  --live               Prints the preconditions for running the four governed receipts (floor
                       refuses, borrowed key, governed write through PLAN->CONSENT->PROVE, bypass
                       refused) against a real ERPNext bench you have configured. Does not run them.

Pacing (for recording):
  default     pause for <Enter> between beats - you drive the tempo while recording.
  --auto N    no prompts; sleep ~N seconds between beats, unevenly, so a cast reads at human speed.

Examples:
  python scripts/demo/the_floor.py                 # local, you press Enter to advance
  python scripts/demo/the_floor.py --auto 3        # local, hands-free, ~3s/beat (asciinema)
"""
from __future__ import annotations

import argparse
import os
import random  # noqa: S311 - pacing jitter for recording, not crypto
import sys
import time

# --- tiny terminal theater ------------------------------------------------------------------------
_C = sys.stdout.isatty()


def _c(s: str, code: str) -> str:
    return f"\033[{code}m{s}\033[0m" if _C else s


def dim(s): return _c(s, "2")
def bold(s): return _c(s, "1")
def red(s): return _c(s, "1;31")
def grn(s): return _c(s, "1;32")
def cyn(s): return _c(s, "1;36")
def yel(s): return _c(s, "1;33")


_AUTO: float | None = None


def _breathe(secs: float) -> None:
    # human-uneven pacing so a recorded cast does not read like a machine metronome
    if _AUTO is not None:
        time.sleep(secs * random.uniform(0.6, 1.5))  # noqa: S311


def beat(title: str) -> None:
    print("\n" + cyn("=" * 66))
    print(cyn(f"  {title}"))
    print(cyn("=" * 66))
    _breathe(0.8)


def caption(s: str) -> None:   # the voiceover / on-screen caption
    print(bold(f"\n  > {s}"))
    _breathe(1.2)


def agent(s: str) -> None:     # "the AI agent asks"
    print(f"\n  {yel('AI agent >')} {s}")
    _breathe(1.0)


def verdict(ok: bool, label: str, detail: str = "") -> None:
    tag = grn("ALLOW") if ok else red("REFUSE")
    print(f"    pacioli > {tag}  {label}   {dim(detail)}")
    _breathe(0.5)


def line(label: str, value: str) -> None:
    print(dim(f"    {label:<22}") + value)
    _breathe(0.15)


def pause() -> None:
    if _AUTO is not None:
        time.sleep(_AUTO)
    else:
        try:
            input(dim("\n    [Enter] >"))
        except EOFError:
            pass


# --- LOCAL: two pillars, self-contained -----------------------------------------------------------
def demo_local() -> int:
    try:
        from pacioli_guard.scope import ApiScope, classify, is_permitted, method_target_resolved
    except ImportError:
        print(red("\n  needs pacioli-guard:  pip install pacioli-guard\n"))
        return 2
    try:
        from pacioli import prove
    except ImportError:
        print(red("\n  needs pacioli:  pip install pacioli\n"))
        return 2

    beat("the setup: one AI agent, one api key, one erpnext")
    caption("an api key in frappe maps to a user. hold the key, you get everything that user can do.")
    caption("no scope on the key. no method limit. no doctype limit. that is the floor that isn't there.")
    caption("pacioli puts one in. same call, watch what changes.")
    pause()

    # The call under test: submit a Sales Invoice (moves the ledger - two GL entries).
    path, http_method, run_method = "/api/method/run_doc_method", "POST", "submit"
    kind, target = classify(path, http_method, run_method=run_method)
    resolved = method_target_resolved(path, http_method, run_method=run_method)
    mres = resolved is not None

    beat("beat 1 - the floor decides")
    agent("submit Sales Invoice ACC-SINV-2026-00007  (posts to the ledger)")
    line("frappe sees:", f"{http_method} {path}  method={run_method}")
    line("pacioli reads it as:", f"{kind} -> {target or resolved}")
    _breathe(0.6)

    # Unscoped credential = frappe's default. is_permitted(None, ...) is True by construction.
    caption("with a normal api key - no floor - this is what frappe allows:")
    verdict(is_permitted(None, kind, target, method_resolved=mres),
            "raw api key, no scope", "everything the user can do")
    _breathe(0.4)

    # A read-only seat: the exact shape of the bench's discovery seat.
    read_seat = ApiScope.from_dict({
        "allow_resource": True,
        "resource_doctypes": ["Sales Invoice", "Customer"],
        "resource_verbs": ["read"],
        "methods": [],
    })
    caption("now the SAME call, same key, but pacioli-guard scoped it to read-only:")
    verdict(is_permitted(read_seat, kind, target, method_resolved=mres),
            "floor-scoped seat (read-only)", "deny-by-default: submit is not granted")
    _breathe(0.4)

    # Prove it is not a blanket no: a granted read on the same seat passes.
    r_kind, r_target = classify("/api/resource/Sales Invoice/ACC-SINV-2026-00007", "GET")
    caption("not a blanket no - a granted read on that same seat still works:")
    verdict(is_permitted(read_seat, r_kind, r_target),
            "read Sales Invoice", f"{r_kind} -> {r_target}")
    caption("the key did not change. the floor did. that is authorization the credential can't talk past.")
    pause()

    # --- Pillar 2: the record --------------------------------------------------------------------
    beat("beat 2 - the record, and why it can't be quietly rewritten")
    caption("pacioli means luca pacioli, 1494, double-entry. no debit without a credit.")
    caption("every governed act gets a sealed receipt. the seals chain. here are three:")
    key = os.urandom(32)  # a real per-book HMAC key; in production it lives 0600 off the books
    ts = "2026-08-25T15:40:00Z"
    receipts = []
    prev = None
    for k, body in [
        ("intent", {"actor": "agent-7", "tool": "submit_sales_invoice",
                    "target": "ACC-SINV-2026-00007", "plan": "PLN-0042"}),
        ("outcome", {"finalizes": 0, "status": "submitted", "docstatus": 1}),
        ("intent", {"actor": "agent-7", "tool": "submit_sales_invoice",
                    "target": "ACC-SINV-2026-00008", "plan": "PLN-0043"}),
    ]:
        r = prove.append(key, prev, k, body, ts)
        receipts.append(r)
        prev = r
        line(f"receipt #{r.seq} {k}", f"{r.hmac[:16]}...  <- links {r.prev_hash[:12]}...")
    ok, why = prove.verify_chain(key, receipts)
    verdict(ok, "verify the chain", why or "all seals check, linkage intact")
    pause()

    beat("beat 3 - try to rewrite the past")
    caption("someone edits receipt #1 to hide that the invoice was submitted. one field.")
    from dataclasses import replace
    tampered = list(receipts)
    tampered[1] = replace(tampered[1], body={"finalizes": 0, "status": "draft", "docstatus": 0})
    line("changed:", "receipt #1 status  submitted -> draft")
    ok, why = prove.verify_chain(key, tampered)
    verdict(ok, "verify the chain again", why)
    caption("the seal is over the contents. change the contents, the seal no longer matches. caught at the exact line.")
    pause()

    beat("beat 4 - try to erase the tail instead")
    caption("so don't edit - just drop the last receipt and hope nobody kept a copy of the head.")
    truncated = receipts[:1]
    off_box_head = receipts[-1].hmac  # the head, pinned off the books before the wipe
    line("dropped:", "receipts #1 and #2")
    ok_naive, _ = prove.verify_chain(key, truncated)
    line("a naive check:", (grn("passes") if ok_naive else red("fails")) + dim("  (a short chain is still self-consistent)"))
    ok, why = prove.verify_chain(key, truncated, expected_head=off_box_head)
    verdict(ok, "verify against the off-box head", why)
    caption("pin the head off the books and the wipe can't hide. a book that doesn't balance confesses.")
    pause()

    beat("that's the floor")
    caption("the credential can't do what it wasn't scoped for. the record can't be rewritten or erased.")
    caption("plan it. consent it. prove it. the floor lives erp-side, so the key alone can't walk around it.")
    print(f"\n  {bold('github.com/john-broadway/pacioli')}\n")
    return 0


# --- LIVE: the four receipts against the real bench -----------------------------------------------
def demo_live() -> int:
    """Print the preconditions for a live receipt run. It does not run anything.

    The four governed receipts (floor refuses, borrowed key, governed write through
    PLAN->CONSENT->PROVE, bypass refused) run against a real ERPNext bench you configure and
    drive yourself. This mode only states what that bench has to satisfy, so a run is never
    half-real.
    """
    print(cyn("\n  LIVE receipts run against a real ERPNext bench you configure - this mode only\n"
              "  states the preconditions; it runs nothing.\n"))
    print("  A faithful run needs, on that bench:")
    print("   - pacioli-guard installed and current enough to ENFORCE consent (>= 0.13.0)")
    print("   - a floor-scoped seat credential, and a broker seat with require_consent on")
    print("   - permission to create and submit a synthetic invoice")
    print(dim("\n  Record from a host that can reach the bench, and read every cast back before you\n"
              "  post it. A real refusal (a 403 you predicted) is the product working, not a failure.\n"))
    print(yel("  Not auto-run: the live path mutates a real ERPNext. Drive the receipt suite\n"
              "  yourself once the preconditions above are green.\n"))
    return 0


def main() -> int:
    global _AUTO
    ap = argparse.ArgumentParser(description="Pacioli demo - the credential floor and the receipt chain.")
    ap.add_argument("--live", action="store_true", help="print the preconditions for a live receipt run")
    ap.add_argument("--auto", nargs="?", const=3.0, type=float, default=None,
                    help="hands-free pacing, ~N seconds/beat (humanised). default: Enter-paced.")
    args = ap.parse_args()
    _AUTO = args.auto
    try:
        return demo_live() if args.live else demo_local()
    except KeyboardInterrupt:
        print()
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
