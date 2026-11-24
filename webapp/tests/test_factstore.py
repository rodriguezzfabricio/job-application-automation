"""RED tests — fact-store model (scar P4, tailor phase).

Cardinal sin S9 (IRON_LAW §7.1): fabricated or over-claimed résumé content.
Structural prevention: every claim binds to an evidence span in the
operator's fact store; unbound claims are non-representable; only the
operator adds evidence; no LLM output ever becomes résumé text directly;
literal claim-text reuse — no free rewording (a1).

What these tests guarantee (one line each):
- test_add_and_get_roundtrip: facts store and return their verbatim text,
  source and authority; unknown ids come back as None.
- test_id_determinism: ids are fact-1, fact-2, ... per store, reproducible
  across stores (no randomness, no shared global counter).
- test_authority_vocabulary_enforced: only "operator-stated" and "artifact"
  are representable authorities.
- test_exact_literal_substring_claim_composes: a claim that is a verbatim
  substring of a cited fact composes.
- test_whitespace_normalized_reuse_composes: different internal spacing does
  not change meaning — the claim still matches the evidence.
- test_reworded_claim_refused: same meaning in different words is refused
  ("unbound claim text") — no free rewording.
- test_claim_with_no_facts_refused: a claim citing no facts cannot compose.
- test_unknown_fact_id_refused: a claim citing a missing fact cannot compose.
- test_verify_claim_mirrors_compose: verify_claim re-runs compose's rules as
  a boolean, true only for claims compose would accept.
- test_empty_claim_refused: empty/whitespace/non-text claim text is refused,
  never bound.
"""

import unittest

from server import factstore
from server.factstore import FactError


class FactStoreModel(unittest.TestCase):
    def test_add_and_get_roundtrip(self):
        store = factstore.FactStore()
        fact_id = store.add_fact(
            "Led build of the 15-minute job-alert pipeline", "resume.pdf",
            "artifact",
        )
        fact = store.get(fact_id)
        self.assertEqual(fact.fact_id, fact_id)
        self.assertEqual(fact.content, "Led build of the 15-minute job-alert pipeline")
        self.assertEqual(fact.source, "resume.pdf")
        self.assertEqual(fact.authority, "artifact")
        self.assertIsNone(store.get("fact-99"))

    def test_id_determinism(self):
        first = factstore.FactStore()
        second = factstore.FactStore()
        self.assertEqual(first.add_fact("a", "s", "operator-stated"), "fact-1")
        self.assertEqual(first.add_fact("b", "s", "operator-stated"), "fact-2")
        self.assertEqual(second.add_fact("c", "s", "operator-stated"), "fact-1")

    def test_authority_vocabulary_enforced(self):
        store = factstore.FactStore()
        with self.assertRaises(FactError):
            store.add_fact("text", "resume.pdf", "llm")
        # both vocabulary members remain representable
        store.add_fact("text", "resume.pdf", "operator-stated")
        store.add_fact("text", "resume.pdf", "artifact")

    def _store_with(self, content="Shipped a 15-minute job-alert pipeline in Python"):
        store = factstore.FactStore()
        fact_id = store.add_fact(content, "resume.pdf", "artifact")
        return store, fact_id

    def test_exact_literal_substring_claim_composes(self):
        store, fact_id = self._store_with()
        claim = factstore.compose_claim(
            store, "15-minute job-alert pipeline", [fact_id]
        )
        self.assertEqual(
            claim, {"text": "15-minute job-alert pipeline", "fact_ids": [fact_id]}
        )

    def test_whitespace_normalized_reuse_composes(self):
        store, fact_id = self._store_with(
            "Built  custom   MCP\nservers\tfor agents"
        )
        claim = factstore.compose_claim(
            store, "  Built custom MCP servers for   agents  ", [fact_id]
        )
        self.assertEqual(claim["text"], "  Built custom MCP servers for   agents  ")

    def test_reworded_claim_refused(self):
        store, fact_id = self._store_with("Led a team of four engineers")
        with self.assertRaises(FactError) as ctx:
            factstore.compose_claim(
                store, "Managed a group of engineers", [fact_id]
            )
        self.assertEqual(str(ctx.exception), "unbound claim text")

    def test_claim_with_no_facts_refused(self):
        store, _ = self._store_with()
        with self.assertRaises(FactError):
            factstore.compose_claim(store, "any claim text", [])

    def test_unknown_fact_id_refused(self):
        store, fact_id = self._store_with("Evidence text")
        with self.assertRaises(FactError):
            factstore.compose_claim(
                store, "Evidence text", [fact_id, "fact-999"]
            )

    def test_verify_claim_mirrors_compose(self):
        store, fact_id = self._store_with()
        good = factstore.compose_claim(
            store, "15-minute job-alert pipeline", [fact_id]
        )
        self.assertTrue(factstore.verify_claim(store, good))
        self.assertFalse(
            factstore.verify_claim(
                store, {"text": "Shipped a pipeline in Go", "fact_ids": [fact_id]}
            )
        )
        self.assertFalse(
            factstore.verify_claim(
                store, {"text": "15-minute job-alert pipeline", "fact_ids": []}
            )
        )
        self.assertFalse(
            factstore.verify_claim(
                store,
                {"text": "15-minute job-alert pipeline", "fact_ids": ["fact-999"]},
            )
        )

    def test_empty_claim_refused(self):
        store, fact_id = self._store_with("Some evidence")
        with self.assertRaises(FactError) as ctx:
            factstore.compose_claim(store, "   ", [fact_id])
        self.assertEqual(str(ctx.exception), "unbound claim text")
        self.assertFalse(
            factstore.verify_claim(store, {"text": "", "fact_ids": [fact_id]})
        )
        self.assertFalse(
            factstore.verify_claim(store, {"text": None, "fact_ids": [fact_id]})
        )


if __name__ == "__main__":
    unittest.main()
