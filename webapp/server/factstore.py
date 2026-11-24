"""Fact-store model for scar P4 (tailor phase) — pure model, no db/network.

Every résumé/cover-letter claim binds to an evidence span in the operator's
fact store; unbound claims are non-representable; claim text is literal reuse
of evidence — no free rewording (a1 design decision). Only the operator adds
evidence, and no LLM output ever becomes résumé text directly: both are
enforced at the API layer, which is the only caller of add_fact. Persistence
and the operator-only write API land later (not here).

The RED scar test `test_resume_claim_without_source_evidence_refused` is
wired by the integrator against compose_claim / verify_claim below.
"""


class FactError(ValueError):
    """A fact or claim violates the fact-store model."""


# closed vocabulary: the operator states it, or it came from an artifact
_AUTHORITIES = ("operator-stated", "artifact")


class Fact:
    """One evidence span: verbatim text, its source, and its authority."""

    def __init__(self, fact_id, content, source, authority):
        if authority not in _AUTHORITIES:
            raise FactError(f"invalid authority: {authority!r}")
        self.fact_id = fact_id
        self.content = content
        self.source = source
        self.authority = authority


class FactStore:
    """In-memory operator fact store with deterministic ids (fact-1, ...)."""

    def __init__(self):
        self._facts = {}
        self._next = 1

    def add_fact(self, content, source, authority):
        fact = Fact(f"fact-{self._next}", content, source, authority)
        self._next += 1
        self._facts[fact.fact_id] = fact
        return fact.fact_id

    def get(self, fact_id):
        return self._facts.get(fact_id)


def _normalize(text):
    """Collapse whitespace runs to single spaces and strip both sides."""
    return " ".join(text.split())


def verify_claim(store, claim):
    """True iff the claim satisfies every composition rule.

    A claim is bound iff it cites at least one known fact whose verbatim
    content (whitespace-normalized) contains the claim text
    (whitespace-normalized) as a literal substring — no free rewording.
    """
    fact_ids = claim.get("fact_ids")
    if not isinstance(fact_ids, (list, tuple)):
        return False
    text = claim.get("text")
    if not isinstance(text, str):
        return False
    normalized = _normalize(text)
    if not normalized:
        return False
    for fact_id in fact_ids:
        fact = store.get(fact_id)
        if fact is not None and normalized in _normalize(fact.content):
            return True
    return False


def compose_claim(store, claim_text, fact_ids):
    """Compose a claim dict, or raise FactError if it cannot bind."""
    if not fact_ids:
        raise FactError("at least one fact id required")
    for fact_id in fact_ids:
        if store.get(fact_id) is None:
            raise FactError(f"unknown fact id: {fact_id}")
    claim = {"text": claim_text, "fact_ids": list(fact_ids)}
    if not verify_claim(store, claim):
        raise FactError("unbound claim text")
    return claim
