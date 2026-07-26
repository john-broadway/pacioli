"""The copy this project publishes must not claim coverage it does not have.

John, 2026-07-26: **"we dont lie ... our code myst not lie."**

WHY THIS IS A TEST AND NOT A NOTE. The same overclaim has now been introduced twice. It was caught
in the guard README on 2026-07-26 and fixed THERE — and the root README, which is the first page
anyone reads on GitHub, kept saying Guard was "independently useful for any credential on a site"
for another day. A note in CLAUDE.md did not stop the second instance, because a note only fires if
someone happens to read it while writing the sentence. A test fires every run.

WHAT IS ACTUALLY TRUE, and what these patterns defend:

  * ``auth_hooks`` / ``check_scope`` governs **api-key** credentials — a request carrying a ``token``
    or ``Basic`` Authorization header. It does NOT see OAuth2 ``Bearer``, desk/cookie sessions,
    background jobs, the scheduler, server scripts, or the bench console. "Which credential is this"
    only exists at authentication time.
  * ``doc_events`` / ``act.py`` consent runs at the document layer and DOES cover those paths — but
    only for a principal whose grant carries ``require_consent``, and it never sees a write that
    sets ``flags.ignore_validate`` or one that skips the lifecycle entirely (raw SQL, ``db_update``).

So an unqualified "every/any credential" is false about the first, and an unqualified "every path"
is false about the second. Either one hands a reader a threat model this software does not support.

These patterns are deliberately narrow: they match the ABSOLUTE phrasings that have actually shipped
or nearly shipped, not every use of the word "every". A qualified claim — "every **api-key** path" —
is true and must keep passing, and there is a test below that pins exactly that.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# Every file here is PUBLIC — it ships in the curated GitHub tree.
PUBLIC_COPY = (
    "README.md",
    "SECURITY.md",
    "guard/README.md",
    "broker/README.md",
)

# (pattern, why it is false). Case-insensitive. Each one has actually appeared in this repo.
BANNED = (
    (r"governs every credential",
     "check_scope sees api-key credentials only — not OAuth Bearer, desk sessions, jobs, "
     "the scheduler, server scripts, or the bench console"),
    (r"(useful|works|governs)[^.\n]{0,40}\bany credential\b(?![^.\n]{0,60}api-key)",
     "same claim in the other direction — qualify it with api-key, or name the two altitudes"),
    (r"every credential on (the|a) site",
     "the phrasing CLAUDE.md records as a live overclaim caught 2026-07-26"),
    (r"every path that reaches the document lifecycle",
     "run_before_save_methods returns early on flags.ignore_validate "
     "(frappe model/document.py:1399-1400), BEFORE before_submit runs at :1405"),
    (r"cannot be bypassed|impossible to bypass|no bypass is possible",
     "coverage is a composition of two hooks with published residuals; no single frappe "
     "extension point is a floor"),
)


class TestPublicCopyDoesNotOverclaimCoverage(unittest.TestCase):
    def test_no_banned_absolute_claim_ships(self):
        found = []
        for rel in PUBLIC_COPY:
            path = ROOT / rel
            if not path.exists():
                continue
            text = path.read_text(encoding="utf-8")
            for lineno, line in enumerate(text.splitlines(), 1):
                for pattern, why in BANNED:
                    if re.search(pattern, line, re.IGNORECASE):
                        found.append(f"{rel}:{lineno}\n    {line.strip()[:160]}\n    WHY FALSE: {why}")
        self.assertEqual(found, [], "public copy overclaims coverage:\n\n" + "\n\n".join(found))

    def test_the_patterns_actually_match_the_sentences_that_shipped(self):
        """A guard that cannot fire is not a guard — pin it against the real historical text."""
        shipped = [
            "This is composition, not coupling: Guard is independently useful for any credential "
            "on a site, agent or otherwise;",
            "This gate runs on every path that reaches the document lifecycle, not only on "
            "api-key REST calls.",
            "pacioli-guard governs every credential on the site",
        ]
        for sentence in shipped:
            self.assertTrue(
                any(re.search(p, sentence, re.IGNORECASE) for p, _ in BANNED),
                f"no pattern catches a sentence that really shipped: {sentence!r}")

    def test_a_QUALIFIED_claim_is_not_flagged(self):
        """The true, qualified phrasing must keep passing, or the guard would push writers toward
        vagueness instead of accuracy."""
        ok = [
            'It turns "governs the agent\'s path" into "governs every api-key path through '
            'this credential."',
            "Binds any api-key credential to an allowlist of methods/DocTypes, deny-by-default.",
            "credential scope binds any **api-key** credential (agent or otherwise)",
        ]
        for sentence in ok:
            hits = [p for p, _ in BANNED if re.search(p, sentence, re.IGNORECASE)]
            self.assertEqual(hits, [], f"a TRUE qualified claim was flagged: {sentence!r}")


if __name__ == "__main__":
    unittest.main()
