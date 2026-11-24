"""Freshness & novelty predicates for canonical job records (a2; a1 raw-id unseen)."""

BOOTSTRAP_WINDOW_S = 3600
WATERMARK_AGE_S = 30 * 86400


def normalize_title(text):
    """Casefold + collapse internal whitespace + strip; the shared key normalizer."""
    return " ".join(text.casefold().split())


def first_published_age_s(record, now):
    """Age of the posting's own first-published-class signal (a2), or None.

    Never the fetched_at time: posting age renders from the posting's own field.
    """
    signal = record["first_published_epoch_s"]
    if signal is None:
        return None
    return now - signal


def is_future(record, now):
    """True only when the first-published signal is present and after now."""
    signal = record["first_published_epoch_s"]
    return signal is not None and signal > now


def within_bootstrap_window(record, now, window_s=BOOTSTRAP_WINDOW_S):
    """Signal present, not future, and age within the bootstrap window (a2)."""
    age = first_published_age_s(record, now)
    return age is not None and age >= 0 and age <= window_s


def is_new(record, seen_raw_ids, now):
    """Unseen raw id AND within the bootstrap window (a1 unseen gate + a2 window).

    Covers only first-seen + window; liveness-verified is a separate upstream
    gate, and "new" never claims original-publication proof.
    """
    return record["raw_id"] not in seen_raw_ids and within_bootstrap_window(record, now)


def possible_stale(record, now, watermark_s=WATERMARK_AGE_S):
    """Signal present, not future, older than the 30-day watermark (a2).

    This is a REVIEW flag, never an alert: a possibly-stale posting is
    operator-visible, never emailed as new.
    """
    age = first_published_age_s(record, now)
    return age is not None and age >= 0 and age > watermark_s


def republish_candidate(record, seen_titles, now, horizon_s=WATERMARK_AGE_S):
    """Identical normalized title+employer seen within the horizon (a2).

    A new-UUID republish heuristic: REVIEW flag, never auto-alert. seen_titles
    maps the normalized "title|employer" key to its last-seen epoch.
    """
    key = f"{normalize_title(record['title'])}|{normalize_title(record['employer'])}"
    last_seen = seen_titles.get(key)
    return last_seen is not None and 0 <= now - last_seen <= horizon_s
