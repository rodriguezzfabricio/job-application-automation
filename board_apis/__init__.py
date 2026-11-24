"""Read job board pages and locally exported postings."""
from .job import Job
from . import handshake, hiringcafe, query, wellfound, ziprecruiter

SOURCES = ("hiringcafe", "ziprecruiter", "wellfound", "handshake")

__all__ = ["Job", "hiringcafe", "ziprecruiter", "wellfound", "handshake",
           "query", "SOURCES"]
