# Copyright (c) 2026, John Broadway and contributors
# License: Apache-2.0
"""Pacioli Consent Marker — a single-use, document-bound proof that a human agreed.

The DECISION lives in the pure core (``pacioli_guard.scope.consent_verdict``) so it stays
unit-testable without a bench, and the frappe glue that spends a marker lives in
``pacioli_guard.enforce``. The one thing that must happen here is establishing ``minted_by``,
because it is the only field the server can vouch for and the separation property depends on it.
"""
import frappe
from frappe.model.document import Document


class PacioliConsentMarker(Document):
    def before_insert(self):
        """Bind ``minted_by`` to the authenticated session, overwriting whatever the caller sent.

        The property that closes the 2026-07-25 direct-submit bypass is that the marker was minted
        by a DIFFERENT principal than the credential presenting it — ``consent_verdict`` refuses a
        self-minted marker. That check is only ever as good as the field it reads, and until now
        nothing established that field. ``minted_by`` is marked ``read_only`` on the DocType, but
        ``read_only`` is a FORM property and does not wall off an API write, and this controller had
        no logic at all, so the stored value was simply whatever the creator supplied. Our own mint
        script passes ``minted_by="Administrator"`` as a parameter — true in our estate, but true by
        convention rather than because the server established it.

        OVERWRITE, never fill-when-blank. A fill-when-blank would let a credential name any other
        principal as its minter and satisfy the separation check with a string it chose itself,
        which is the same hole wearing a different hat. Floor audit F3, 2026-07-26.
        """
        self.minted_by = frappe.session.user
