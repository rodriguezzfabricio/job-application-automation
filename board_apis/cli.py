"""Search configured job-board sources and return normalized JSON."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys

from . import SOURCES, handshake, query


def _parse_sources(raw: str) -> tuple[str, ...]:
    if raw == "all":
        return tuple(SOURCES)
    picked = tuple(s.strip() for s in raw.split(",") if s.strip())
    unknown = [s for s in picked if s not in SOURCES]
    if unknown:
        raise SystemExit(f"unknown source(s): {', '.join(unknown)}; "
                         f"choose from {', '.join(SOURCES)}")
    return picked


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="board_apis", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", default="all",
                    help=f"comma separated or 'all': {', '.join(SOURCES)}")
    ap.add_argument("--keyword", "--query", dest="keyword", default="",
                    help="search keywords")
    ap.add_argument("--location", default="",
                    help="metro, state or city, e.g. 'Oregon' or 'Austin, TX'")
    ap.add_argument("--max-age-hours", type=float, default=None,
                    help="drop postings older than this (freshness gate)")
    ap.add_argument("--days", type=int, default=None,
                    help="hiring.cafe native look-back in days")
    ap.add_argument("--limit", type=int, default=25)
    ap.add_argument("--out", help="write JSON here instead of stdout")

    ap.add_argument("--handshake-status", action="store_true",
                    help="is a local Handshake export configured?")
    args = ap.parse_args(argv)

    if args.handshake_status:
        print(json.dumps(handshake.status(), indent=2))
        return 0
    q = query.Query(
        keyword=args.keyword,
        location=args.location,
        max_age_hours=args.max_age_hours,
        days=args.days,
        limit=args.limit,
        sources=_parse_sources(args.source),
    )
    jobs, errors = query.search(q)

    for source in q.sources:
        if source in errors:
            print(f"[{source}] skipped: {errors[source].splitlines()[0]}",
                  file=sys.stderr)
        else:
            print(f"[{source}] ok", file=sys.stderr)
    print(f"{len(jobs)} postings after filters", file=sys.stderr)

    payload = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "query": {"keyword": q.keyword, "location": q.location,
                  "max_age_hours": q.max_age_hours, "days": q.days,
                  "sources": list(q.sources)},
        "count": len(jobs),
        "jobs": [j.to_dict() for j in jobs],
        "errors": errors,
    }
    text = json.dumps(payload, indent=2, ensure_ascii=False)
    if args.out:
        with open(args.out, "w") as fh:
            fh.write(text + "\n")
        print(f"wrote {len(jobs)} jobs to {args.out}", file=sys.stderr)
    else:
        print(text)
    return 0 if jobs else 1


if __name__ == "__main__":
    raise SystemExit(main())
