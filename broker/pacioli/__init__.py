# Copyright (c) 2026, John Broadway and contributors
# License: Apache-2.0
"""Pacioli Broker — the governed MCP broker for ERPNext.

The security-critical pillar logic lives in the pure cores (``prove``, ``consent``, ``plan``,
``spine``) with no frappe/network import, so it is unit-testable without a running bench. The frappe-
and MCP-facing glue (``erpnext``, ``server``, ``registry``) is thin and proven live. See ``SPEC.md``.
"""

__version__ = "0.31.1"
