# Copyright (c) 2026, John Broadway and contributors
# License: Apache-2.0
"""Pacioli Broker — REGISTRY: the TOML target registry, auth by reference (glue, stdlib-only).

Multi-site/company routing (SPEC §4): a ``pacioli_target=`` travels with each call, and this
registry is what it resolves against — so PLAN and submit always hit the same books, and the wrong
company's ledger is structurally unreachable. Each target maps to its **own least-privilege
credential** (a company *param* on a shared credential is not enforced by ERPNext; one scoped user
per (site, company) is).

Secrets are held **by reference, never inline**: ``api_secret`` must be ``env:VAR`` or
``file:/path`` — a literal is refused at load, and the refusal never echoes the literal back.
``api_key`` (an identifier, not a secret) may be inline or a reference. Resolution is injected
(``env``/``read_file``) so it is unit-testable and so nothing here ever logs a resolved value.

Deny-biased throughout: no targets, a malformed/plaintext-over-the-wire ``base_url``, an ambiguous
default, or an unknown target name are refusals with named alternatives — never a guess.
"""
from __future__ import annotations

import tomllib
from dataclasses import dataclass
from urllib.parse import urlparse

_REF_PREFIXES = ("env:", "file:")
_LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")


class RegistryError(ValueError):
    """A registry problem the caller must fix. Messages never contain secret material."""


@dataclass(frozen=True)
class Target:
    """One routed destination: a (site, company) with its own scoped credential.

    :param name: the registry key ``pacioli_target=`` selects.
    :param base_url: the bench URL, scheme-validated, no trailing slash.
    :param api_key: inline identifier or ``env:``/``file:`` reference.
    :param api_secret: ``env:``/``file:`` reference — never a literal.
    :param company: optional company pin, recorded into plans/receipts by the glue.
    :param seat_user: optional — the ERPNext username (``owner``) this credential authenticates as.
        Used ONLY by ``close --reconcile`` (the Close, Half 2) to corroborate that a governed
        voucher's GL rows were actually stamped by THIS seat, not merely that a voucher name
        matches. Purely tightening: a governed voucher whose rows carry a different ``owner``
        downgrades to second-generation (surfaced, never a false clean). Absent → owner
        corroboration is off (name+time match only).
    :param posture: optional — the operator's response posture for ``close --respond`` (the Close,
        Half 3): ``"mixed_door"`` (the default when absent — ungoverned movement is recorded, not
        alerted; real books have other legitimate doors) or ``"sole_door"`` (this credential is the
        only thing allowed near these books → ungoverned movement is raised to an alert). A non-string
        value is refused at load; a string typo passes through and ``build_response`` deny-biases it
        to ``sole_door`` with a flag — a policy typo must neither silently quiet the signal nor brick
        every operation.
    :param site_tz: optional — the IANA timezone the ERPNext SITE's wall clock runs in (e.g.
        ``"Asia/Kolkata"``). Declared, it makes ``close`` window bounds mean SITE time — the
        books' own calendar — converted once at the boundary to the store's UTC domain
        (``pacioli.clock``; ruling docs/plans/2026-07-16-clock-domain-ruling.md, T1). A
        non-string is refused at load; a string typo passes through and ``pacioli.clock``
        refuses it AT USE with the zone named. Absent → no conversion (both clock domains read
        the window string verbatim, the pre-0.24.0 behavior) and ``close --reconcile`` says so.
    :param default: is this the target a bare call routes to?
    """

    name: str
    base_url: str
    api_key: str
    api_secret: str
    company: str | None = None
    seat_user: str | None = None
    posture: str | None = None
    site_tz: str | None = None
    default: bool = False


class Registry:
    """The loaded target set. ``get(None)`` resolves the default, deny-biased."""

    def __init__(self, targets):
        self._targets = targets

    def names(self):
        return sorted(self._targets)

    def get(self, name):
        """Resolve a target by name, or the default for ``None``. Unknown/ambiguous → refusal."""
        if name is not None:
            if name not in self._targets:
                raise RegistryError(
                    f"unknown target {name!r}; configured targets: {', '.join(self.names())}"
                )
            return self._targets[name]
        defaults = [t for t in self._targets.values() if t.default]
        if len(defaults) == 1:
            return defaults[0]
        if not defaults and len(self._targets) == 1:
            return next(iter(self._targets.values()))
        raise RegistryError(
            "no unambiguous default target: pass pacioli_target= explicitly "
            f"(configured targets: {', '.join(self.names())})"
        )


def _is_reference(value):
    return isinstance(value, str) and value.startswith(_REF_PREFIXES)


def _validate_base_url(name, raw, allow_http):
    if not isinstance(raw, str) or not raw.strip():
        raise RegistryError(f"target {name!r}: base_url is required")
    url = raw.strip().rstrip("/")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise RegistryError(f"target {name!r}: base_url {raw!r} must be an http(s):// URL")
    if parsed.scheme == "http" and parsed.hostname not in _LOCAL_HOSTS and not allow_http:
        raise RegistryError(
            f"target {name!r}: plain http to a non-local host sends the credential in cleartext; "
            "use https, or set allow_http = true if you accept that"
        )
    return url


def _build_target(name, section):
    if not isinstance(section, dict):
        raise RegistryError(f"target {name!r}: must be a table")
    allow_http = bool(section.get("allow_http"))
    base_url = _validate_base_url(name, section.get("base_url"), allow_http)
    api_key = section.get("api_key")
    api_secret = section.get("api_secret")
    if not isinstance(api_key, str) or not api_key.strip():
        raise RegistryError(f"target {name!r}: api_key is required")
    if not isinstance(api_secret, str) or not api_secret.strip():
        raise RegistryError(f"target {name!r}: api_secret is required")
    if not _is_reference(api_secret):
        # Never echo the offending literal — it is (or was about to become) a secret.
        raise RegistryError(
            f"target {name!r}: api_secret must be held by reference (env:VAR or file:/path), "
            "never inline in the registry"
        )
    company = section.get("company")
    seat_user = section.get("seat_user")
    posture = section.get("posture")
    if posture is not None and not isinstance(posture, str):
        # A non-string is an unambiguous type error — refuse loudly. A string typo is NOT refused
        # here; build_response is the single posture validator and deny-biases an unknown one.
        raise RegistryError(
            f"target {name!r}: posture must be a string ('mixed_door' or 'sole_door') or omitted")
    site_tz = section.get("site_tz")
    if site_tz is not None and not isinstance(site_tz, str):
        # Same split as posture: type errors refuse at load; a string typo passes through and
        # pacioli.clock is the single zone validator, refusing AT USE with the zone named (a
        # typo must not brick operations that never touch a window).
        raise RegistryError(
            f"target {name!r}: site_tz must be an IANA zone name string "
            "(e.g. 'Asia/Kolkata') or omitted")
    return Target(
        name=name,
        base_url=base_url,
        api_key=api_key.strip(),
        api_secret=api_secret.strip(),
        company=company if isinstance(company, str) and company.strip() else None,
        seat_user=seat_user.strip() if isinstance(seat_user, str) and seat_user.strip() else None,
        posture=posture.strip() if isinstance(posture, str) and posture.strip() else None,
        site_tz=site_tz.strip() if isinstance(site_tz, str) and site_tz.strip() else None,
        default=bool(section.get("default")),
    )


def load_registry(toml_text=None, path=None):
    """Parse and validate a registry from TOML text or a file path. Returns a :class:`Registry`."""
    if toml_text is None:
        if path is None:
            raise RegistryError("no registry given (need toml_text or path)")
        with open(path, "rb") as f:
            data = tomllib.load(f)
    else:
        data = tomllib.loads(toml_text)
    sections = data.get("targets")
    if not isinstance(sections, dict) or not sections:
        raise RegistryError("registry has no [targets.<name>] tables")
    targets = {name: _build_target(name, section) for name, section in sections.items()}
    if sum(1 for t in targets.values() if t.default) > 1:
        raise RegistryError("more than one target is marked default = true; exactly one may be")
    return Registry(targets)


def _resolve_ref(value, what, env, read_file):
    """Resolve one ``env:``/``file:`` reference (or pass an inline value through). The error path
    names the reference, never a value."""
    if value.startswith("env:"):
        var = value[len("env:"):]
        resolved = (env or {}).get(var)
        if resolved is None or not resolved.strip():
            raise RegistryError(f"{what}: environment variable {var} is not set (or empty)")
        return resolved.strip()
    if value.startswith("file:"):
        p = value[len("file:"):]
        try:
            resolved = read_file(p)
        except OSError as exc:
            raise RegistryError(f"{what}: cannot read secret file {p}: {exc}") from exc
        if not resolved or not resolved.strip():
            raise RegistryError(f"{what}: secret file {p} is empty")
        return resolved.strip()
    return value


def resolve_auth(target, *, env, read_file):
    """Resolve a target's credential to ``(api_key, api_secret)`` at call time.

    ``env`` (a mapping) and ``read_file`` (``path -> str``) are injected; nothing is cached and
    nothing resolved is ever put in an error message.
    """
    key = _resolve_ref(target.api_key, f"target {target.name!r} api_key", env, read_file)
    secret = _resolve_ref(target.api_secret, f"target {target.name!r} api_secret", env, read_file)
    if not key or not secret:
        raise RegistryError(f"target {target.name!r}: resolved credential is empty")
    return key, secret
