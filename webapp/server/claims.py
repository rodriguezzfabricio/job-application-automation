"""Claims registry model — pure, in-memory; persistence comes later (P6).

Every quantitative string rendered anywhere must be a registered claim;
bare strings are non-representable in this module.
"""

CLAIM_STRENGTHS = ("measured", "design-target", "heuristic", "operator-stated")


class ClaimError(ValueError):
    """A claim that cannot be represented in this registry."""


class Registry:
    """In-memory claim store. Pure model; persistence comes later."""

    def __init__(self):
        self._claims = {}

    def add(self, claim_id, text, source, strength, date, ref=None):
        if strength not in CLAIM_STRENGTHS:
            raise ClaimError(f"unknown strength: {strength!r}")
        if claim_id in self._claims:
            raise ClaimError(f"duplicate claim id: {claim_id!r}")
        claim = {
            "claim_id": claim_id,
            "text": text,
            "source": source,
            "strength": strength,
            "date": date,
            "ref": ref,
        }
        self._claims[claim_id] = claim
        return claim

    def get(self, claim_id):
        return self._claims[claim_id]


def is_registered(registry, claim_id):
    return claim_id in registry._claims


def render(registry, claim_id):
    """The only emission path: registered claim text + provenance tag (P6)."""
    if not is_registered(registry, claim_id):
        raise ClaimError(f"unknown claim id: {claim_id!r}")
    claim = registry.get(claim_id)
    return f"{claim['text']} [{claim['strength']} — {claim['source']}, {claim['date']}]"


def render_bare(text):
    """Bare strings are non-representable (P6): refuse unconditionally."""
    raise ClaimError(f"bare string is non-representable: {text!r}")
