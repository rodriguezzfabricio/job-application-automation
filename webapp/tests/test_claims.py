"""Claims registry RED tests (P6: registry-only emission path, greppable).

Pins the rebuild design:
- every quantitative claim renders through the registry only, annotated
- bare strings are non-representable in the renderer
- the module has exactly one emission format site (greppable)

A GREEN on any of these before the registry existed is itself a failure.
"""

import inspect
import unittest

from server import claims


class ClaimsRegistry(unittest.TestCase):
    def test_add_get_roundtrip(self):
        registry = claims.Registry()
        stored = registry.add(
            "latency",
            "<=25 min detection to send-accepted",
            "IRON_LAW §12",
            "design-target",
            "2026-10-20",
        )
        got = registry.get("latency")
        self.assertEqual(got, stored)
        self.assertEqual(got["claim_id"], "latency")
        self.assertEqual(got["text"], "<=25 min detection to send-accepted")
        self.assertEqual(got["source"], "IRON_LAW §12")
        self.assertEqual(got["strength"], "design-target")
        self.assertEqual(got["date"], "2026-10-20")
        self.assertIsNone(got["ref"])

    def test_duplicate_claim_id_refused(self):
        registry = claims.Registry()
        registry.add("latency", "text", "source", "measured", "2026-10-20")
        with self.assertRaises(claims.ClaimError):
            registry.add("latency", "other", "source", "measured", "2026-10-20")

    def test_bad_strength_refused(self):
        registry = claims.Registry()
        with self.assertRaises(claims.ClaimError):
            registry.add("latency", "text", "source", "made-up", "2026-10-20")

    def test_render_includes_provenance_tag(self):
        registry = claims.Registry()
        registry.add(
            "latency",
            "<=25 min detection to send-accepted",
            "IRON_LAW §12",
            "design-target",
            "2026-10-20",
        )
        rendered = claims.render(registry, "latency")
        self.assertEqual(
            rendered,
            "<=25 min detection to send-accepted"
            " [design-target — IRON_LAW §12, 2026-10-20]",
        )

    def test_render_unknown_id_refused(self):
        registry = claims.Registry()
        with self.assertRaises(claims.ClaimError):
            claims.render(registry, "missing")

    def test_render_bare_always_refused(self):
        # A bare quantitative string — even one equal to a registered id —
        # cannot reach the UI through this module (P6).
        registry = claims.Registry()
        registry.add("latency", "text", "source", "measured", "2026-10-20")
        for bare in ("25 minutes", "99% uptime", "latency"):
            with self.subTest(bare=bare):
                with self.assertRaises(claims.ClaimError):
                    claims.render_bare(bare)

    def test_emission_format_is_greppable_single_site(self):
        # P6 acceptance (a9): the renderer formats rendered text in exactly
        # one place; grep for the tag template finds it exactly once.
        tag = "[{claim['strength']} — {claim['source']}, {claim['date']}]"
        self.assertEqual(inspect.getsource(claims).count(tag), 1)

    def test_claim_strengths_vocabulary_is_closed(self):
        # Golden pin (P6): the four epistemic strengths are the closed set.
        self.assertEqual(
            claims.CLAIM_STRENGTHS,
            ("measured", "design-target", "heuristic", "operator-stated"),
        )
        for strength in claims.CLAIM_STRENGTHS:
            with self.subTest(strength=strength):
                registry = claims.Registry()
                registry.add("c", "text", "source", strength, "2026-10-20")
                self.assertEqual(registry.get("c")["strength"], strength)

    def test_is_registered(self):
        registry = claims.Registry()
        registry.add("latency", "text", "source", "measured", "2026-10-20")
        self.assertTrue(claims.is_registered(registry, "latency"))
        self.assertFalse(claims.is_registered(registry, "missing"))


if __name__ == "__main__":
    unittest.main()
