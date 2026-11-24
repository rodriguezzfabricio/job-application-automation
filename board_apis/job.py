"""Normalized job record shared by every board adapter."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Optional


@dataclass
class Job:
    source: str                      # "hiringcafe" | "ziprecruiter" | "wellfound" | "handshake"
    title: str
    company: str = ""
    location: str = ""
    url: str = ""                    # canonical page on the board
    apply_url: str = ""              # direct employer/ATS link when the board exposes one
    posted_at: Optional[str] = None  # ISO-8601 UTC, None when the board does not say
    salary: str = ""
    seniority: str = ""
    description: str = ""
    external_id: str = ""
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    # --- scoring helpers the rubric in SOURCES_AND_SCORING.md needs ---------
    @property
    def dedup_key(self) -> str:
        """Stable identity across runs. Prefers the employer link."""
        return self.apply_url or self.url or f"{self.source}:{self.external_id}"

    @property
    def has_employer_apply(self) -> bool:
        """G3: a real ATS page rather than an aggregator page."""
        if not self.apply_url:
            return False
        host = self.apply_url.lower()
        ats = ("greenhouse.io", "lever.co", "ashbyhq.com", "workday",
               "myworkdayjobs", "smartrecruiters", "workable.com", "jobvite",
               "icims.com", "taleo", "selectminds", "bamboohr", "recruitee",
               "teamtailor", "personio", "rippling", "applytojob", "saashr",
               "paylocity", "ultipro", "successfactors")
        return any(a in host for a in ats)

    @property
    def age_hours(self) -> Optional[float]:
        """G2: posting age, when the board gave a timestamp."""
        if not self.posted_at:
            return None
        try:
            dt = datetime.fromisoformat(self.posted_at.replace("Z", "+00:00"))
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds() / 3600.0

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("raw", None)
        d["dedup_key"] = self.dedup_key
        d["has_employer_apply"] = self.has_employer_apply
        d["age_hours"] = None if self.age_hours is None else round(self.age_hours, 2)
        return d
