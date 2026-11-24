"""Curated archive annotations with verified posting and recorded stop evidence.

Only these complete negative-path records are exported. A tracker status alone cannot
establish eligibility, form field coverage, PDF, or a receipt for a confirmed replay.
"""
import hashlib
import re
import json
import sqlite3

from stage_archive import manifest_path, portal_bucket, stable_id, verify_episode

def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def reviewed_stops(root):
    path = root / 'reviewed-stops.json'
    if not path.is_file():
        return
    records = json.loads(path.read_text(encoding='utf-8'))
    with sqlite3.connect(f'{(root / "tracker.sqlite").as_uri()}?mode=ro', uri=True) as con:
        for case in records:
            opening_id, stem, event, blocks, quote = (case[k] for k in ('opening_id', 'posting_sha256', 'event', 'blocks', 'quote'))
            if not re.fullmatch(r'[a-fA-F0-9]{64}', stem):
                raise ValueError('invalid posting digest')
            posting_ref = f'codex-runtime/postings/{stem}.txt'
            posting = (root / posting_ref).read_bytes()
            record = con.execute('SELECT kind,key,detail FROM events WHERE id=?', (event,)).fetchone()
            if not record or opening_id.lower() not in record[1].lower() or quote not in record[2]:
                raise ValueError('the curated stop is missing its exact-opening evidence')
            company, title, link = con.execute('SELECT company,title,link FROM jobs WHERE job_key=?', (record[1],)).fetchone()
            snapshot = json.dumps(record, ensure_ascii=False).encode()
            opening = 'R-' + stable_id(record[1])[:9].upper()
            public_excerpt = 'Posting for ' + title.strip() + '. ' + quote
            if 'closed' not in blocks and quote not in posting.decode():
                raise ValueError('the decision quote must be present in the archived posting')
            for sentence in case.get('additional_quotes', []):
                if sentence not in posting.decode():
                    raise ValueError('quoted text is missing from the archived posting')
                public_excerpt += ' ' + sentence
            event_ref = {'file': 'tracker.sqlite', 'event': event, 'quote': quote, 'sha256': digest(snapshot)}
            posting_proof = {'file': posting_ref, 'sha256': digest(posting), 'quote': '' if 'closed' in blocks else quote}
            label = 'No longer accepting applications' if 'closed' in blocks else 'Application unavailable'
            item = {'id': portal_bucket(link) + '-' + opening[2:].lower(), 'portal': portal_bucket(link),
                    'opening_group': stable_id(opening_id), 'employer_group': stable_id(company),
                    'reviewer': 'archived source passages reviewed against the written eligibility rules',
                    'input': {'opening': opening, 'posting': public_excerpt, 'required_fields': [], 'facts': {},
                              'pdf_options': [], 'questions': {}, 'portal_state_before': 'Posting or gate review',
                              'attempt_status': 'No submit authorized',
                              'visual': ['Archived posting', title, opening, label, quote, 'Back to openings', 'blocked']},
                    'gold': {'blocks': list(blocks), 'eligible': False, 'requirements': [], 'observed_opening': opening,
                             'outcome': 'disqualified', 'answers': {}, 'claims': {}, 'unknown_fields': [],
                             'pdf': '', 'receipt': '', 'submit_count': 0, 'handoff_reason': '', 'action': 'stop'},
                    'sources': {'posting': posting_proof, 'decision': event_ref, 'terminal': event_ref}}
            yield verify_episode(root, item)


def export(root, persist):
    path = manifest_path(root)
    current = json.loads(path.read_text()) if path.is_file() else {'schema': 1, 'episodes': []}
    previous = {e['id']: e for e in current['episodes']}
    for episode in reviewed_stops(root):
        current_record = previous.get(episode['id'])
        if current_record is None or current_record.get('reviewer') == episode['reviewer']:
            previous[episode['id']] = episode
    current['episodes'] = sorted(previous.values(), key=lambda e: e['id'])
    persist(path, current)
    return len(current['episodes'])
