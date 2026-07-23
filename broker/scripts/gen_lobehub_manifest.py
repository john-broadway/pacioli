#!/usr/bin/env python3
"""Regenerate lhm.plugin.json — the LobeHub Marketplace manifest — for Pacioli.

LobeHub scores a listing partly on a non-empty `tools` array (satisfies the
"Includes At Least One Skill" score item; an empty extract → grade F). Their
crawler extracts tools by cold-starting the server and calling `tools/list`.

**Pacioli-specific:** unlike a homelab tool, the Pacioli broker refuses to boot
without a live ERPNext credential (`assemble()` fail-closes) — so a credential-less
crawl extracts NOTHING and would grade F. We therefore ship an OWNER-DECLARED tools
array (authoritative, survives re-crawls), read straight from the server module's
`TOOLS` table — no cold-start, no ERPNext, no secrets. Run in the broker venv, then
`npx -y @lobehub/market-cli plugin publish --dir <repo-root>`.
"""

from __future__ import annotations

import json
import sys
import tomllib
from pathlib import Path

BROKER = Path(__file__).resolve().parent.parent          # .../pacioli/broker
REPO_ROOT = BROKER.parent                                 # .../pacioli
MANIFEST = REPO_ROOT / "lhm.plugin.json"

# Static listing fields. identifier follows the house pattern john-broadway-<product>
# (LobeHub may reassign at first listing — keep name/description in sync with the
# server.json title). "ERPNext" MUST appear in the NAME: LobeHub/Glama keyword search
# matches the name field and "Pacioli" contains neither "erpnext" nor "accounting"
# (the Proximo/Proxmox 2026-07-10 lesson — invisible in the domain search otherwise).
BASE = {
    "identifier": "john-broadway-pacioli",
    "name": "Pacioli — the ERPNext broker you can hand the books",
    "description": (
        "Governed agent access to your ERPNext books — 51 doctypes across accounts, "
        "stock, assets, and manufacturing: every write PLANNED, CONSENTED, PROVEN, and "
        "UNDOable. No debit without a credit. MCP + A2A doors, one spine."
    ),
}


def broker_version() -> str:
    data = tomllib.loads((BROKER / "pyproject.toml").read_text())
    return data["project"]["version"]


def main() -> int:
    try:
        from pacioli.server import TOOLS
    except ImportError as exc:  # pragma: no cover - dev/release helper
        raise SystemExit(
            f"cannot import pacioli.server ({exc}); run this in the broker venv "
            "(.venv/bin/python) with the [server] extra installed"
        ) from exc
    if not TOOLS:
        raise SystemExit("refusing to write an empty tools array")
    manifest = {
        **BASE,
        "version": broker_version(),
        "tools": [
            {"name": t["name"], "description": t["description"],
             "inputSchema": t["inputSchema"]}
            for t in TOOLS
        ],
    }
    MANIFEST.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {MANIFEST} — {len(TOOLS)} tools, v{manifest['version']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
