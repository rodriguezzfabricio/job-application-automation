"""Scoring & eligibility layer (IRON_LAW §1.1 role buckets, §9 score+reasons, scar P5/P8).

Pure functions only: no db, no network, no config imports.
"""

# HEURISTICS: small lowercase substring sets, deliberately not exhaustive
# vocabularies. Unknown stays unknown; callers route it to review, never guess.
CITIZENSHIP_MARKERS = frozenset({
    "us citizen",
    "us citizenship",
    "united states citizen",
    "united states citizenship",
    "citizenship required",
})

CLEARANCE_MARKERS = frozenset({
    "security clearance",
    "top secret clearance",
    "secret clearance",
    "active clearance",
    "ts/sci",
})

ML_EXCLUDE_MARKERS = frozenset({
    "machine learning engineer",
    "ml engineer",
    "model training",
})

AI_BUCKET_MARKERS = frozenset({
    "ai engineer",
    "ai software engineer",
    "llm",
    "agent",
    "tool calling",
    "mcp",
})

ENTRY_LEVEL_MARKERS = frozenset({
    "entry level",
    "new grad",
    "new graduate",
    "junior",
})

SEVERITIES = frozenset({"deal_breaker", "reject", "review", "met"})


class SchemaError(ValueError):
    """A verdict or reason violates the closed schema (P5: unexplained = invalid)."""


def _normalize(text):
    # Hyphens collapse to spaces so "entry-level" matches "entry level".
    return text.casefold().replace("-", " ")


def build_reason(rule_id, severity, evidence_refs, text):
    if severity not in SEVERITIES:
        raise SchemaError(
            f"invalid severity {severity!r}; expected one of {sorted(SEVERITIES)}")
    return {
        "rule_id": rule_id,
        "severity": severity,
        "evidence_refs": list(evidence_refs),
        "text": text,
    }


def analyze_dealbreakers(text):
    """Layer-1 gates: citizenship/clearance markers -> deal_breaker reasons."""
    norm = _normalize(text)
    reasons = []
    for marker in CITIZENSHIP_MARKERS:
        if marker in norm:
            reasons.append(build_reason(
                "rule.citizenship", "deal_breaker", [f"marker:{marker}"],
                "role requires US citizenship"))
            break
    for marker in CLEARANCE_MARKERS:
        if marker in norm:
            reasons.append(build_reason(
                "rule.clearance", "deal_breaker", [f"marker:{marker}"],
                "role requires security clearance"))
            break
    return reasons


def classify_bucket(text):
    """One of new_grad_swe / entry_ai / excluded_ml, or None for unknown."""
    norm = _normalize(text)
    if any(marker in norm for marker in ML_EXCLUDE_MARKERS):
        return "excluded_ml"
    if any(marker in norm for marker in AI_BUCKET_MARKERS):
        return "entry_ai"
    if any(marker in norm for marker in ENTRY_LEVEL_MARKERS):
        return "new_grad_swe"
    return None


def work_auth_verdict(profile_work_auth, text):
    """P8 ternary: eligible / reject-on-explicit-language / review. Never guesses."""
    norm = _normalize(text)
    if any(marker in norm for marker in CITIZENSHIP_MARKERS | CLEARANCE_MARKERS):
        return "reject-on-explicit-language"
    if profile_work_auth == "eligible":
        return "eligible"
    # Anything but a pinned eligible profile fact fails closed to review;
    # this covers sponsorship-ambiguous language and absent markers alike.
    return "review"


def _build_result(score_value, reasons, bucket, work_auth):
    # Fail-closed: a verdict with no reasons is schema-invalid (P5).
    if not reasons:
        raise SchemaError("verdict requires at least one reason")
    severities = {reason["severity"] for reason in reasons}
    verdict = "eligible"
    for candidate in ("deal_breaker", "reject", "review"):
        if candidate in severities:
            verdict = candidate
            break
    return {
        "score": round(score_value, 1),
        "reasons": reasons,
        "bucket": bucket,
        "work_auth": work_auth,
        "verdict": verdict,
    }


def score_job(job_text, profile):
    """70/30 score + reasons for one posting; never a bare yes/no."""
    skills = profile.get("skills") or []
    role_bucket = profile.get("role_bucket")
    # Missing work-auth fact fails closed to review; it never becomes eligible.
    profile_work_auth = profile.get("work_auth", "needs-review")

    dealbreakers = analyze_dealbreakers(job_text)
    if dealbreakers:
        # Top-level gates evaluate before any scoring (§9).
        return _build_result(0.0, dealbreakers, classify_bucket(job_text),
                             work_auth_verdict(profile_work_auth, job_text))

    reasons = []
    bucket = classify_bucket(job_text)
    if bucket == "excluded_ml":
        reasons.append(build_reason(
            "rule.role_bucket", "reject", [f"bucket:{bucket}"],
            "machine-learning-engineer / model-training roles are excluded"))
    elif bucket is None:
        reasons.append(build_reason(
            "rule.role_bucket", "review", ["bucket:unknown"],
            "role bucket could not be classified"))

    work_auth = work_auth_verdict(profile_work_auth, job_text)
    if work_auth == "reject-on-explicit-language":
        reasons.append(build_reason(
            "rule.work_auth", "reject", ["work_auth:explicit-reject-language"],
            "posting explicitly requires citizenship or clearance"))
    elif work_auth == "review":
        reasons.append(build_reason(
            "rule.work_auth", "review", ["work_auth:needs-review"],
            "work authorization not confirmed eligible"))

    norm = _normalize(job_text)
    found = sum(1 for skill in skills if _normalize(skill) in norm)
    skill_overlap = found / len(skills) if skills else 0.0
    role_fit = 1.0 if bucket is not None and bucket == role_bucket else 0.3
    total = 70 * skill_overlap + 30 * role_fit

    if not reasons:
        # An eligible verdict must still carry a positive explanation.
        reasons.append(build_reason(
            "rule.eligible", "met", [f"bucket:{bucket}", f"work_auth:{work_auth}"],
            f"no exclusions; bucket {bucket}, work auth {work_auth}"))
    return _build_result(total, reasons, bucket, work_auth)
