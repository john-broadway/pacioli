# Copyright (c) 2026, John Broadway and contributors
# License: Apache-2.0
"""Pacioli Broker — CASCADE: governed cancel of a document AND its submitted-dependent graph.

Two pure cores, no bench imports (I/O is injected, exactly like ``spine.py``):

  * :func:`build_cascade` — discover the transitive dependent closure, refuse a cycle or an
    over-cap graph, and return the nodes in CANCEL ORDER (every dependent before the document it
    depends on; the target LAST). Deny-biased: an unreadable node at any depth refuses the whole
    cascade — a graph you cannot fully read is never treated as small.
  * :func:`run_cascade` — the execution loop (see its own docstring): per-node
    fresh → closed-books → intent → cancel → outcome, fail-stop, one marker settled once.
"""
from __future__ import annotations

from types import SimpleNamespace

from pacioli.consent import commit, release, reserve
from pacioli.plan import check_fresh, check_red_line


def _node_key(n):
    return (n["doctype"], n["docname"])


def build_cascade(target, *, fetch_linked, node_meta, supported_doctypes, max_nodes):
    """Build the ordered cancel graph for ``target`` (see module docstring / node shape).

    ``fetch_linked(doctype, docname)`` returns the submitted dependents and may RAISE on an
    unreadable graph — any raise is a whole-cascade refusal. ``node_meta`` supplies per-node
    version/date/company/GL. Returns ``{"ok": True, "graph": [...]}`` or
    ``{"ok": False, "reason": ..., "stage": "plan"}``."""
    # 1. Walk the transitive closure (stack-based, depth-first traversal — the frontier is a LIFO
    #    list; the final cancel order comes from the topological sort in step 2 regardless, so the
    #    discovery order here doesn't matter), collecting edges dependent -> dependency ("A before B").
    edges = {}          # (dt,name) -> set of dependent (dt,name) that must precede it
    nodes = {}          # (dt,name) -> {"doctype","docname"}
    tkey = _node_key(target)
    nodes[tkey] = {"doctype": target["doctype"], "docname": target["docname"]}
    edges.setdefault(tkey, set())
    frontier = [tkey]
    while frontier:
        dt, name = frontier.pop()
        try:
            deps = fetch_linked(dt, name)
        except Exception as exc:  # noqa: BLE001 — unreadable graph is a refusal, never "empty"
            return {"ok": False, "stage": "plan",
                    "reason": f"could not read the dependent graph at {dt} {name}: {exc}"}
        for d in deps:
            dkey = _node_key(d)
            edges[(dt, name)].add(dkey)  # d must be cancelled before (dt,name)
            if dkey not in nodes:
                nodes[dkey] = {"doctype": d["doctype"], "docname": d["docname"]}
                edges.setdefault(dkey, set())
                frontier.append(dkey)
        if len(nodes) > max_nodes:
            return {"ok": False, "stage": "plan",
                    "reason": f"cascade graph has {len(nodes)} documents, over the cap of "
                              f"{max_nodes}; refuse rather than unwind an unexpectedly large graph"}

    # 2. Kahn topological sort on edges "dep -> node" (dependents emitted first, target last).
    #    indegree counts, for each node, how many dependents still precede it.
    order = []
    remaining = {k: set(v) for k, v in edges.items()}
    # a node is ready when it has no remaining unemitted dependents
    ready = sorted([k for k, deps in remaining.items() if not deps])
    emitted = set()
    while ready:
        k = ready.pop(0)
        order.append(k)
        emitted.add(k)
        for parent, deps in remaining.items():
            if k in deps:
                deps.discard(k)
                if not deps and parent not in emitted and parent not in ready:
                    ready.append(parent)
        ready.sort()
    if len(order) != len(nodes):
        stuck = sorted(f"{dt} {n}" for (dt, n) in nodes if (dt, n) not in emitted)
        return {"ok": False, "stage": "plan",
                "reason": f"cascade graph has a cycle among: {', '.join(stuck)}; refuse"}

    # 3. Attach per-node metadata + coverage label, in cancel order.
    graph = []
    for key in order:
        dt, name = key
        meta = node_meta(dt, name)
        graph.append({
            "doctype": dt, "docname": name,
            "doc_version": meta["doc_version"], "posting_date": meta["posting_date"],
            "company": meta["company"],
            "coverage": "modeled" if dt in supported_doctypes else "generic",
            "projected_gl": meta.get("projected_gl") or [],
        })
    return {"ok": True, "graph": graph}


def run_cascade(*, plan, marker, token, now_epoch, now_date, effects):
    """Execute a governed cascade cancel. See module docstring for the ordering guarantees.

    Order is PREFLIGHT-ALL → CONSENT → EXECUTE (fail-stop), the design's approved shape:

      1. **Preflight every node** (freshness + closed-books check) BEFORE consent and BEFORE any
         irreversible action. A gate failure records nothing and leaves the marker untouched — a
         clean no-op (mirrors the single-op spine: gates precede consent). This is what stops a
         locked node #2 from leaving node #1 stranded.
      2. **Consent** — reserve + CAS-claim the ONE marker, only once the whole graph preflights clean.
      3. **Execute** in order: durable intent, then the irreversible cancel, then the outcome —
         which the response alone does NOT prove: each node's returned ``docstatus`` must confirm
         the ``1->2`` transition (the exact E1 discipline ``spine.governed_submit`` carries for the
         single-op path), or the node is recorded ``unconfirmed`` (never ``committed``). On the
         FIRST cancel failure OR unconfirmed node the run STOPS (later nodes untouched). The
         marker is settled ONCE: ``committed`` iff ≥1 node was cancelled OR the stopping node came
         back unconfirmed (an act may already be in motion server-side), ``released`` only when a
         clean exception stopped the very first node. A failed/unconfirmed node leaves its intent
         an orphan for the reconciliation sweep.
    """
    graph = list(plan.graph or [])
    total = len(graph)
    if total == 0:
        return {"ok": False, "stage": "plan", "cancelled": [], "stopped_at": None, "total": 0,
                "reason": "cascade plan has no graph"}

    # 1. PREFLIGHT every node before ANY irreversible action; records nothing, marker untouched.
    for i, node in enumerate(graph):
        dt, name = node["doctype"], node["docname"]
        ok, reason = check_fresh(SimpleNamespace(doc_version=node["doc_version"]),
                                 effects.current_version(dt, name))
        if not ok:
            return {"ok": False, "stage": "fresh", "cancelled": [], "total": total,
                    "stopped_at": {"doctype": dt, "docname": name, "seq": i, "reason": reason}}
        # F-S1: locks_for is now doctype- and posting_date-aware — pass the SAME node fields this
        # very check_red_line call already uses, never a second read of either.
        ok, reason = check_red_line(
            node["posting_date"], now_date,
            effects.locks_for(node["company"], node["doctype"], node["posting_date"]))
        if not ok:
            return {"ok": False, "stage": "red_line", "cancelled": [], "total": total,
                    "stopped_at": {"doctype": dt, "docname": name, "seq": i, "reason": reason}}

    # 2. CONSENT: reserve + CAS-claim the ONE marker now that the whole graph preflights clean.
    ok, reason, reserved = reserve(marker, token, plan.plan_id, now_epoch)
    if not ok:
        return {"ok": False, "stage": "consent", "cancelled": [], "stopped_at": None,
                "total": total, "reason": reason}
    if not effects.claim_marker(reserved):
        return {"ok": False, "stage": "consent", "cancelled": [], "stopped_at": None,
                "total": total, "reason": "marker is already in use (concurrent cascade)"}

    # 3. EXECUTE in order; durable intent before each cancel; fail-stop; settle the marker once.
    cancelled = []
    degraded_nodes = []  # WG-2b: docnames whose outcome had to degrade to a sanitized retry
    for i, node in enumerate(graph):
        dt, name = node["doctype"], node["docname"]
        try:
            intent = effects.record_intent({"tool": "cascade_cancel", "plan_id": plan.plan_id,
                                            "cascade_id": plan.plan_id, "seq": i, "doctype": dt,
                                            "docname": name, "coverage": node["coverage"],
                                            "transition": "1->2"})
        except Exception as exc:  # noqa: BLE001 — WG-2b: post-claim, pre-wire for THIS node.
            # effects.cancel is never reached, so nothing was sent to the bench for this node —
            # but there is no intent receipt to link an outcome to (store.record_outcome requires
            # one), so none can be recorded here. Earlier nodes (if any) already have their own
            # durable outcomes from prior loop iterations; the marker is left exactly as it stood
            # (claimed and, if any prior node landed, already committed via that node's own
            # settle) — never released, never silently re-opened. This is now a structured
            # stop, never a raw exception past dispatch()'s narrow catch.
            return {"ok": False, "stage": "execute", "cancelled": cancelled, "total": total,
                    "stopped_at": {"doctype": dt, "docname": name, "seq": i,
                                   "reason": f"could not durably record the intent for this "
                                             f"node ({exc}); nothing was sent to the bench for "
                                             "it; the consent marker remains claimed (not "
                                             "spendable) for manual review"}}
        try:
            result = effects.cancel(dt, name)
        except Exception as exc:  # noqa: BLE001 — record ANY cancel failure, then fail-stop
            # Transport taxonomy (docs/plans/2026-07-07-transport-taxonomy.md): an ANSWERED
            # refusal (the bench definitely saw and refused this cancel — `ErpnextError.answered`,
            # duck-typed via getattr so this pure core never imports the glue's exception type)
            # keeps today's byte-identical behavior: commit iff progress already happened,
            # otherwise release. Everything else ("no answer" — a raw/unconverted exception, a
            # connection failure, a proxy-shaped ambiguous response) is resolved below.
            if getattr(exc, "answered", False):
                final = commit(reserved) if cancelled else release(reserved)  # ≥1 progress -> commit
                recorded, oexc = _settle(effects, intent, "failed",
                                         {"error": str(exc), "doctype": dt}, final)
                if not recorded:
                    return {"ok": False, "stage": "execute", "cancelled": cancelled,
                            "total": total,
                            "stopped_at": {"doctype": dt, "docname": name, "seq": i,
                                           "reason": f"cancel of {dt} {name} was refused ({exc}) "
                                                     f"and the outcome could not be durably "
                                                     f"recorded either ({oexc}); the consent "
                                                     "marker's real state is uncertain — treat "
                                                     "it as unspendable and inspect manually"}}
                return {"ok": False, "stage": "execute", "cancelled": cancelled, "total": total,
                        "stopped_at": {"doctype": dt, "docname": name, "seq": i,
                                       "reason": str(exc)}}
            # No-answer/ambiguous: the cancel may already be in motion server-side regardless of
            # prior-node progress — ALWAYS commit here (THE FLIP: this used to release on a first
            # node, the never-verified "no progress" assumption; it no longer does), then resolve
            # via a governed readback of the node's real docstatus — never raise past this point.
            committed_marker = commit(reserved)
            try:
                got = effects.readback(dt, name)
            except Exception as rexc:  # noqa: BLE001 — the readback must never raise past here
                result = {"error": str(exc), "doctype": dt, "readback_error": str(rexc)}
                recorded, oexc = _settle(effects, intent, "unconfirmed", result, committed_marker)
                if not recorded:
                    return {"ok": False, "stage": "execute", "cancelled": cancelled,
                            "total": total,
                            "stopped_at": {"doctype": dt, "docname": name, "seq": i,
                                           "reason": f"cancel of {dt} {name} raised ({exc}) with "
                                                     f"no answer, the readback also failed "
                                                     f"({rexc}), and the outcome could not be "
                                                     f"durably recorded either ({oexc}); the "
                                                     "consent marker's real state is uncertain — "
                                                     "treat it as unspendable and inspect "
                                                     "manually"}}
                reason = (f"cancel of {dt} {name} raised ({exc}) with no answer from the bench, "
                         f"and the confirmatory readback itself failed ({rexc}); the document's "
                         "real state is unknown. The consent marker is spent; the intent receipt "
                         "stays open until reconciled.")
                return {"ok": False, "stage": "execute", "cancelled": cancelled, "total": total,
                        "stopped_at": {"doctype": dt, "docname": name, "seq": i, "reason": reason}}
            if got == 2:
                # The readback CONFIRMS this node actually cancelled, even though the call raised.
                # DESIGN CHOICE: the run still FAIL-STOPS here rather than resuming the loop — the
                # exception interrupted normal control flow, and continuing past an exceptional
                # path is not the same guarantee the ordinary per-node "try the next one" carries
                # (later nodes' preflight ran against the PLANNED graph, not against "the previous
                # node's cancel raised but might have actually worked"). Record the confirmed node
                # honestly in `cancelled` and stop, rather than pretend the exception never
                # happened and resume as if this were the normal success path.
                cancelled.append({"doctype": dt, "docname": name, "seq": i})
                result = {"error": str(exc), "docstatus": got, "doctype": dt,
                          "confirmed_via": "post_failure_readback"}
                recorded, oexc = _settle(effects, intent, "committed", result, committed_marker)
                if not recorded:
                    return {"ok": False, "stage": "execute", "cancelled": cancelled,
                            "total": total,
                            "stopped_at": {"doctype": dt, "docname": name, "seq": i,
                                           "reason": f"cancel of {dt} {name} raised ({exc}) but a "
                                                     f"readback confirms it DID cancel, yet the "
                                                     f"outcome could not be durably recorded "
                                                     f"({oexc}); the consent marker's real state "
                                                     "is uncertain — treat it as unspendable and "
                                                     "inspect manually"}}
                reason = (f"cancel of {dt} {name} raised ({exc}) but a readback confirms it DID "
                         "cancel (docstatus 2); stopping here rather than continuing past an "
                         "exceptional path — later nodes were never attempted.")
                return {"ok": False, "stage": "execute", "cancelled": cancelled, "total": total,
                        "stopped_at": {"doctype": dt, "docname": name, "seq": i, "reason": reason}}
            result = {"error": str(exc), "doctype": dt, "docstatus": got}
            recorded, oexc = _settle(effects, intent, "unconfirmed", result, committed_marker)
            if not recorded:
                return {"ok": False, "stage": "execute", "cancelled": cancelled, "total": total,
                        "stopped_at": {"doctype": dt, "docname": name, "seq": i,
                                       "reason": f"cancel of {dt} {name} raised ({exc}) with no "
                                                 f"answer, and the outcome could not be durably "
                                                 f"recorded either ({oexc}); the consent marker's "
                                                 "real state is uncertain — treat it as "
                                                 "unspendable and inspect manually"}}
            reason = (f"cancel of {dt} {name} raised ({exc}) with no answer from the bench; the "
                     f"outcome is recorded 'unconfirmed' (not 'committed') because a readback "
                     f"shows docstatus {got!r}, not confirmed as cancelled. The consent marker is "
                     "spent; the intent receipt stays open until reconciled.")
            return {"ok": False, "stage": "execute", "cancelled": cancelled, "total": total,
                    "stopped_at": {"doctype": dt, "docname": name, "seq": i, "reason": reason}}
        # `effects.cancel` returning is NOT proof the transition happened — confirm it, the exact
        # E1 discipline the single-op spine carries (spine.py's `governed_submit`, CHANGELOG
        # 0.9.1): ERPNext can queue a cancel to a background worker and answer 200 with the doc
        # still at its pre-transition docstatus. A node that doesn't confirm docstatus 2 is
        # recorded `unconfirmed`, never `committed`, and fail-stops the run exactly like an
        # exception does — but the marker is ALWAYS spent here (never released): unlike the
        # exception case above (where a clean bench refusal means nothing happened), an
        # unconfirmed response means the act may already be in motion server-side, even if this
        # is the very first node — releasing the grant would let it initiate a second act.
        got = result.get("docstatus") if isinstance(result, dict) else None
        if got != 2:
            recorded, oexc = _settle(effects, intent, "unconfirmed", {**result, "doctype": dt},
                                     commit(reserved))
            if not recorded:
                return {"ok": False, "stage": "execute", "cancelled": cancelled, "total": total,
                        "stopped_at": {"doctype": dt, "docname": name, "seq": i,
                                       "reason": f"cancel of {dt} {name} was accepted but NOT "
                                                 f"confirmed, and the outcome could not be "
                                                 f"durably recorded either ({oexc}); the consent "
                                                 "marker's real state is uncertain — treat it as "
                                                 "unspendable and inspect manually"}}
            # A `readback_error` on the result means the glue's confirmatory readback itself
            # failed (redteam, critical) — name that cause instead of the queue guess, so a human
            # reconciler knows the real docstatus is UNKNOWN, not shown-stale. Either way the
            # settle is the same: marker spent, fail-stop.
            readback_error = result.get("readback_error") if isinstance(result, dict) else None
            if readback_error:
                cause = ("the response carried no docstatus and the confirmatory readback itself "
                         f"failed ({readback_error}), so the document's real state is unknown")
            else:
                cause = (f"the response showed docstatus {got!r}, expected 2. ERPNext queues some "
                         "cancels to a background worker — the write may still land after this "
                         "reply")
            reason = (f"cancel of {dt} {name} was accepted but NOT confirmed (outcome recorded "
                      f"'unconfirmed', not 'committed'): {cause}. The consent marker is spent; "
                      "the intent receipt stays open until reconciled against the document's real "
                      "docstatus (fetch the document, or sweep prove_orphans).")
            return {"ok": False, "stage": "execute", "cancelled": cancelled, "total": total,
                    "stopped_at": {"doctype": dt, "docname": name, "seq": i, "reason": reason}}
        cancelled.append({"doctype": dt, "docname": name, "seq": i})
        if i == total - 1:  # terminal success → settle the marker committed on the last outcome
            recorded, oexc = _settle(effects, intent, "committed", {**result, "doctype": dt},
                                     commit(reserved))
        else:
            recorded, oexc = _settle(effects, intent, "committed", {**result, "doctype": dt}, None)
        if not recorded:
            return {"ok": False, "stage": "execute", "cancelled": cancelled, "total": total,
                    "stopped_at": {"doctype": dt, "docname": name, "seq": i,
                                   "reason": f"cancel of {dt} {name} succeeded (docstatus "
                                             f"confirmed) but the outcome could not be durably "
                                             f"recorded ({oexc}); the consent marker's real "
                                             "state is uncertain — treat it as unspendable and "
                                             "inspect manually"}}
        if oexc is not None:
            # The cancel genuinely confirmed, but the FIRST attempt to record "committed" failed
            # and had to degrade to a sanitized "unconfirmed" retry (see _settle) — track it so
            # the run never reports a clean "done" through a ledger that had to degrade, even
            # though every node's own cancel fully succeeded at the bench.
            degraded_nodes.append(name)

    if degraded_nodes:
        return {"ok": False, "stage": "execute", "cancelled": cancelled, "total": total,
                "stopped_at": None,
                "reason": f"every node cancelled and confirmed, but the outcome record for "
                          f"{len(degraded_nodes)} node(s) ({', '.join(degraded_nodes)}) could "
                          "not be durably written as originally intended and was degraded to "
                          "'unconfirmed'; reconcile against each document's real docstatus "
                          "before treating this cascade as fully closed"}
    return {"ok": True, "cancelled": cancelled, "stopped_at": None, "total": total, "stage": "done"}


def _settle(effects, intent, status, result, final_marker):
    """Record the outcome; if the store itself refuses the write (e.g. a non-finite float or any
    other unexpected value that slipped past every upstream check — belt-and-suspenders for
    ``prove.append``'s JSON-native guard), degrade to a minimal, always-safe body rather than let
    the raw exception crash past ``dispatch()``'s structured deny (WG-2b: the residual this
    closes — a post-claim exception stranding the marker with an UNRECORDED outcome and an
    unstructured crash; mirrors ``spine._settle``, the single-op sibling of this loop). A
    ``"failed"`` status (a known-clean answered refusal — nothing landed) is preserved on retry;
    every other status is POST-WIRE and is recorded on the degraded retry as ``"unconfirmed"`` and
    ONLY ``"unconfirmed"`` — deny-biased: never let a recording failure make a possibly-in-motion
    act look like a clean "failed", and never claim "committed" through a body the store itself
    just refused. ``final_marker`` is reused as-is on the retry: the first attempt's transaction
    rolls back cleanly on any exception (``BrokerStore._immediate``), so nothing was partially
    written and retrying is safe.

    Returns ``(recorded, retry_exc)``: ``recorded`` is ``True`` iff some outcome (the original or
    the degraded retry) is now durably persisted; ``retry_exc`` is ``None`` on a clean first-try
    success, else the exception the first attempt raised (whether or not the retry then
    recovered) — the caller uses it to tell a clean success from a degraded one."""
    try:
        effects.record_outcome(intent, status, result, final_marker)
        return True, None
    except Exception as exc:  # noqa: BLE001 — the outcome write itself must never crash past here
        safe_status = status if status == "failed" else "unconfirmed"
        safe_result = {"error": f"the original outcome could not be durably recorded ({exc}); "
                                 "this is a degraded, sanitized record", "attempted_status": status}
        try:
            effects.record_outcome(intent, safe_status, safe_result, final_marker)
            return True, exc
        except Exception as exc2:  # noqa: BLE001 — genuinely unrecoverable; never raise past here
            return False, exc2
