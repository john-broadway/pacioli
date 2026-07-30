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

from pacioli_guard.scope import immutable_marker_violations


class PacioliConsentMarker(Document):
    def validate(self):
        """A minted marker is immutable except for ``burned``. THE DECISION IS IN THE PURE CORE
        (:func:`pacioli_guard.scope.immutable_marker_violations`); this is glue.

        **Why an UPDATE guard and not just ``before_insert``.** ``before_insert`` fires only on
        create, so the ``minted_by`` binding below did not survive an UPDATE, and ``expires_at_epoch``
        — the authoritative expiry since 0.13.0 — had no guard at all. Anything holding doctype write
        permission could extend a marker's life indefinitely, re-stamp its minter to defeat the
        separation check with a self-chosen string, or repoint it at another document. ``read_only``
        is a form property and stops no API write; no ``permlevel`` is declared. Found by an
        independent review, 2026-07-29.

        **This cannot block spending a marker.** The spend is a raw
        ``UPDATE ... WHERE burned = 0`` in :func:`pacioli_guard.enforce._claim_consent`, which skips
        the document lifecycle entirely, so ``validate`` never runs for it. ``burned`` is also
        explicitly outside the immutable set, so a spend through the ORM would pass too.
        """
        violations = immutable_marker_violations(self.get_doc_before_save(), self)
        if not violations:
            return
        frappe.throw(
            f"A consent marker cannot be changed after it is minted. Refused an edit to: "
            f"{', '.join(violations)}. Consent is a record of what a human authorised at one moment "
            f"for one document and one act — editing it afterwards is minting a different consent "
            f"while keeping the original's audit trail. Cancel this marker and mint a fresh one "
            f"instead. Only `burned` may change, and only by the floor spending it.",
            frappe.PermissionError,
        )

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
