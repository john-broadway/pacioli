# Copyright (c) 2026, John Broadway and contributors
# License: Apache-2.0
"""Pacioli Broker — RUNTIME: config assembly, the seal key, per-target state (glue, stdlib-only).

Turns environment/config into a wired :class:`pacioli.tools.PacioliBroker`. Everything that touches
the disk or the clock lives here so ``tools`` and the cores stay pure.

  * **Registry** — ``PACIOLI_REGISTRY`` (a TOML path; §4).
  * **State dir** — ``PACIOLI_STATE_DIR`` (one SQLite file per target = one set of books, one
    ledger; the mint CLI writes to the same file the server reads).
  * **Seal key** — ``PACIOLI_SEAL_KEY_FILE`` (default under the state dir). 32 random bytes,
    generated on first use, stored **0600**; a group/world-readable key is *refused* (the HMAC is
    only as good as the file mode). The key stays on-box — the honest limit PROVE discloses; the
    off-box head pin (``pacioli anchor``) bounds that exposure to since-the-last-pin, no further.
  * **Cascade cap** — ``PACIOLI_CASCADE_MAX`` (default ``25``). The max node count
    ``plan_cascade_cancel``/``cascade_cancel`` will discover+execute; a larger dependent graph is
    refused rather than unwound (see ``pacioli.cascade.build_cascade``).
"""
from __future__ import annotations

import os
import re
import secrets
from pathlib import Path

from pacioli.erpnext import ErpnextClient
from pacioli.registry import RegistryError, load_registry, resolve_auth
from pacioli.store import BrokerStore, refuse_if_torn
from pacioli.tools import PacioliBroker

_SEAL_KEY_BYTES = 32
_SAFE_NAME = re.compile(r"[^A-Za-z0-9_-]")  # no '.' — collapses any path-traversal attempt too


class RuntimeError_(Exception):
    """A misconfiguration the operator must fix — always a clear, actionable message."""


def _env(env, key, default=None, required=False):
    val = (env or {}).get(key, default)
    if required and not val:
        raise RuntimeError_(f"{key} is not set")
    return val


def state_db_path(state_dir, target_name):
    """The SQLite file for a target. The target name is filesystem-sanitised so a hostile registry
    key (``../../etc/x``) can never escape the state dir."""
    safe = _SAFE_NAME.sub("_", target_name) or "target"
    return Path(state_dir) / f"{safe}.db"


def load_or_create_seal_key(path):
    """Load the 0600 seal key at ``path``, or mint one on first use. Refuses a key that is too
    short or is group/world-readable (a leaked key voids every seal)."""
    path = Path(path)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        key = secrets.token_bytes(_SEAL_KEY_BYTES)
        # Create 0600 from the start — never a window where the fresh key is world-readable.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(key)
        return key
    mode = path.stat().st_mode & 0o777
    if mode & 0o077:
        raise RuntimeError_(
            f"seal key {path} has permissions {oct(mode)}; it must be 0600 (owner-only) — "
            f"run: chmod 600 {path}"
        )
    key = path.read_bytes()
    if len(key) < _SEAL_KEY_BYTES:
        raise RuntimeError_(
            f"seal key {path} is only {len(key)} bytes; expected >= {_SEAL_KEY_BYTES}. "
            "If this is corrupt, existing receipts can no longer be verified — investigate, "
            "do not just regenerate."
        )
    return key


def _seal_key_path(env):
    explicit = _env(env, "PACIOLI_SEAL_KEY_FILE")
    if explicit:
        return Path(explicit)
    state_dir = _env(env, "PACIOLI_STATE_DIR", required=True)
    return Path(state_dir) / "seal.key"


def open_store(env, target_name, *, with_key=True, via=None):
    """Open the :class:`BrokerStore` for a target. ``with_key=False`` opens it **keyless** — the
    mint CLI's least-exposure path (marker ops only, no seal key in reach). ``via`` is the
    door's stamp (:meth:`BrokerStore.set_via` — F3, the doors ruling); ``None`` (every CLI
    and legacy path) leaves recording byte-identical."""
    import sqlite3

    state_dir = _env(env, "PACIOLI_STATE_DIR", required=True)
    db = state_db_path(state_dir, target_name)
    db.parent.mkdir(parents=True, exist_ok=True)
    refuse_if_torn(db)  # must run BEFORE connect() -- connect() itself creates a 0-byte file
    key = load_or_create_seal_key(_seal_key_path(env)) if with_key else None
    store = BrokerStore(sqlite3.connect(str(db)), key=key)
    if via is not None:
        store.set_via(via)
    return store


def _load_registry_from_env(env):
    reg_path = _env(env, "PACIOLI_REGISTRY", required=True)
    if not Path(reg_path).exists():
        raise RuntimeError_(f"registry file not found: {reg_path} (set PACIOLI_REGISTRY)")
    try:
        return load_registry(path=reg_path)
    except RegistryError as exc:
        raise RuntimeError_(f"registry {reg_path}: {exc}") from exc


def assemble(env=None, *, via=None):
    """Build a fully-wired :class:`PacioliBroker` from the environment. The seal key and each
    target's store are created lazily/opened here; credential resolution is deferred to call time
    (nothing secret is cached). ``via`` is the serving door's stamp, threaded into every
    store this broker opens (F3, the doors ruling); ``None`` = undeclared, byte-identical."""
    env = os.environ if env is None else env
    registry = _load_registry_from_env(env)

    def store_provider(target_name):
        return open_store(env, target_name, with_key=True, via=via)

    def client_provider(target):
        key, secret = resolve_auth(target, env=env, read_file=_read_file)
        return ErpnextClient(base_url=target.base_url, api_key=key, api_secret=secret)

    cascade_max = int(_env(env, "PACIOLI_CASCADE_MAX", default="25") or "25")

    return PacioliBroker(registry=registry, store_provider=store_provider,
                         client_provider=client_provider, cascade_max=cascade_max)


def _read_file(path):
    return Path(path).read_text(encoding="utf-8")
