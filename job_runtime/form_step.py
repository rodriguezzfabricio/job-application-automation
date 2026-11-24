"""Plan one observed form step and check opening-specific application evidence."""
import argparse
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from .answers import plain_options, validate_answers
from .policy import canonical_url, timestamp
from .store import Denied

FORM_ANSWERS = 'form-answers.md'
PRIVATE = Path(__file__).resolve().parents[1] / 'stopgap-private'

# Only straightforward label matches to named, saved facts. Unknown and compound
# questions are left for review even when they offer a Yes/No choice.
LITERALS = {
    r'(legal |candidate )?(full |legal )?name': 'Legal name and full name',
    r'(legal )?first name|given name': 'Legal name and full name',
    r'(legal )?last name|family name|surname': 'Legal name and full name',
    r'preferred name': 'Preferred name',
    r'(confirm |re-?enter )?e-?mail( address)?( confirmation)?': 'Email',
    r'(mobile |primary |contact )?(phone|telephone)( number)?': 'Phone',
    r'linkedin( (profile|url|link)){0,2}': 'LinkedIn URL',
    r'github( (profile|url|link)){0,2}': 'GitHub URL',
    r'(personal |portfolio )?website( url)?|portfolio( url)?': 'Portfolio/website',
    r'(street )?address( line)?( 1)?': 'Street address',
    r'(?:current|most recent|last) (?:employer|company)(?: name)?|current or most recent employer': 'Current company / current or most recent employer',
    r'(?:earliest|available|availability|desired)(?: work| employment)? start date|date available to start|when can you start': 'Earliest exact work start date',
    r'(?:expected |anticipated )?graduation date|date of graduation': 'Exact expected graduation date',
    r'(current )?city|city of residence': 'Current location',
    r'state( ?/ ?province)?|state or province': 'Current location',
    r'(zip|postal)( code)?': 'Street address',
}

REVIEW = re.compile(
    r'password|passcode|one.time (code|password)|verification code|security question|token|credential|'
    r'terms|consent|agreement|attest|certif|acknowledg|arbitrat|privacy|waiv|agree|'
    r'signature|e-?sign|sign here|social security|passport|bank account', re.I)
RESUME = re.compile(r'\b(resume|résumé|cv|curriculum vitae)\b', re.I)
REQ_PARAM = re.compile(r'(?:job(?:[_.-]?id)?|requisition(?:[_.-]?id)?|req(?:[_.-]?id)?|gh_jid|posting(?:[_.-]?id)?)$', re.I)
REQ_TOKEN = re.compile(r'(?:^|[^A-Za-z0-9])((?:R|JR|REQ)[-_]?[0-9]+)(?=$|[^A-Za-z0-9])', re.I)
REQ_SEGMENT = re.compile(r'(?:[0-9]+|[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})$', re.I)


def normalize(value):
    return ' '.join(str(value or '').split()).casefold()


def private_root():
    return Path(os.environ.get('JOB_AUTOMATION_PRIVATE_ROOT') or PRIVATE)


def normalized_url(value):
    if not isinstance(value, str):
        return ''
    try:
        url = urlsplit(value)
        return '' if url.username or url.password else canonical_url(value)
    except ValueError:
        return ''


def requisition_id(url):
    if not normalized_url(url):
        return None
    parsed = urlsplit(url)
    query = {value for key, value in parse_qsl(parsed.query) if REQ_PARAM.fullmatch(key) and value}
    if len(query) == 1:
        return query.pop()
    if query:
        return None
    workday = (parsed.hostname or '').endswith(('.myworkdayjobs.com', '.myworkdaysite.com'))
    for path in (parsed.path, parsed.fragment):
        if workday:
            # A site such as External_Career_Site_1 is part of the tenant, never the opening.
            start = re.search(r'/job/', path, re.I)
            if not start:
                continue
            path = path[start.start():]
        segments = path.strip('/').split('/')
        prefixed = REQ_TOKEN.findall(path)
        numeric_or_uuid = [s for i, s in enumerate(segments) if REQ_SEGMENT.fullmatch(s) and (
            '-' in s or (i and segments[i - 1].casefold() in ('job', 'jobs', 'posting', 'requisitions')))]
        wd_numeric = re.findall(r'_((?:REF)?\d+[A-Z]?)(?:-[0-9]+)?(?:/|$)', path) if workday else []
        candidates = set(prefixed + numeric_or_uuid + wd_numeric)
        if len(candidates) == 1:
            return candidates.pop()
        if candidates:
            return None
    return None


def saved_lines(root):
    path = root / FORM_ANSWERS
    lines = {}
    if path.is_file():
        for line in path.read_text().splitlines():
            m = re.fullmatch(r'\s*-?\s*([^:]+):\s*(.+)', line)
            if m:
                lines.setdefault(m[1], []).append((m[2], line))
    return {key: entries[0] for key, entries in lines.items() if len(entries) == 1}


def literal_candidate(label, lines):
    for pattern, source in LITERALS.items():
        if not re.fullmatch(pattern, label) or source not in lines:
            continue
        value, quote = lines[source]
        value = value.strip().strip('`')
        if source == 'Current company / current or most recent employer':
            # The remainder of this saved line states title and when employment ended.
            value = value.partition('. Current title:')[0].rstrip('.')
            if not value or len(value)>120: return None
        if source in ('Earliest exact work start date', 'Exact expected graduation date'):
            match = re.match(r'^(\d{4}-\d{2}-\d{2})(?:\b|$)', value)
            if not match: return None
            value = match[1]
        if source == 'Legal name and full name':
            parts = value.split()
            if len(parts) != 2:
                return None
            if label in ('first name', 'legal first name', 'given name'):
                value = parts[0]
            elif label in ('last name', 'legal last name', 'family name', 'surname'):
                value = parts[1]
        if source == 'Preferred name':
            preferred = re.fullmatch(r"([A-Za-z][\w'-]*)(?:\s+\([^)]*\))?\.?", value)
            if not preferred:
                return None
            value = preferred[1]
        if source == 'Current location':
            parts = [v.strip() for v in value.split(',')]
            if len(parts) != 2 or not re.fullmatch(r'[A-Za-z .-]+', parts[0]) or not re.fullmatch(r'[A-Z]{2}', parts[1]):
                return None
            value = parts[0] if 'city' in label else parts[1]
        if source == 'Street address':
            if re.fullmatch(r'(zip|postal)( code)?', label):
                zips = re.findall(r'\b[0-9]{5}(?:-[0-9]{4})?\b', value)
                if len(zips) != 1:
                    return None
                value = zips[0]
            else:
                value = value.split(',', 1)[0]
                if not re.fullmatch(r'[0-9]+[A-Za-z0-9 .#-]+', value):
                    return None
        if source in ('LinkedIn URL', 'GitHub URL', 'Portfolio/website') and not normalized_url(value):
            return None
        if source == 'Email' and not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', value):
            return None
        if source == 'Phone' and not re.fullmatch(r'[+()0-9 .-]{7,24}', value):
            return None
        return {'kind': 'literal', 'source': FORM_ANSWERS, 'quote': quote, 'value': value}
    return None


def option_for(value, field):
    options = [o for o in field.get('options', []) if isinstance(o, dict) and o.get('value') not in ('', None)]
    exact = [o for o in options if normalize(o.get('label')) == normalize(value)]
    if not exact and normalize(value) in ('yes', 'no'):
        exact = [o for o in options if re.match(r'^' + re.escape(value) + r'(?=\b|$)', normalize(o.get('label')), re.I)]
    return exact[0] if len(exact) == 1 else None


def field_label(field):
    return normalize(field.get('label')).strip(' *?:').replace('\u2019', "'")


def navigation(controls, has_fields, blocked):
    groups = {}
    for control in controls:
        if control.get('disabled') or not control.get('key'):
            continue
        label = normalize(control.get('label'))
        if label in ('submit application', 'submit my application', 'send application') or (
                has_fields and label in ('submit', 'send', 'apply now')):
            kind = 'final_review'
        elif label in ('next', 'next step', 'next page', 'continue', 'save and continue', 'review', 'review application'):
            kind = 'advance'
        elif not has_fields and label in ('apply', 'apply now', 'apply for this job', 'apply for this position',
                                           'start application', 'start your application', 'begin application'):
            kind = 'open_application'
        elif re.fullmatch(r'create (?:an? )?(?:account|profile)|register|sign[ -]?(?:up|in)|log[ -]?in', label):
            kind = 'account'
        else:
            continue
        groups.setdefault(kind, []).append(control['key'])
    if blocked or len(groups) != 1:
        return None
    kind, keys = next(iter(groups.items()))
    return {'kind': kind, 'key': keys[0]} if len(keys) == 1 else None


def plan(data, root=None):
    root = Path(root) if root is not None else private_root()
    job_key = data.get('job_key')
    fields, controls = data.get('fields', []), data.get('controls', [])
    if not isinstance(job_key, str) or not job_key.strip():
        raise ValueError('job_key required')
    if not isinstance(fields, list) or not isinstance(controls, list) or len(fields) > 100 or len(controls) > 60:
        raise ValueError('form observation must contain at most 100 fields and 60 controls')
    lines = saved_lines(root)
    ops, answers, blocked = [], [], []
    refs = data.get('refs')
    if not isinstance(refs, list) or not all(isinstance(ref, str) for ref in refs):
        refs = []
    if data.get('captcha') or data.get('errors'):
        reason = 'captcha' if data.get('captcha') else 'validation errors'
        return {'job_key': job_key, 'ops': [], 'answers': [], 'blocked': [{'reason': reason}], 'navigation': None}
    fields = [f for f in fields if not f.get('disabled') and f.get('visible') is not False and f.get('type') != 'hidden']
    keys = {}
    for field in fields:
        key = field.get('key')
        keys[key] = keys.get(key, 0) + 1
    for field in fields:
        key = field.get('key')
        label = field_label(field)
        seen = {'key': key, 'label': '[credential field]' if field.get('type') == 'password' else field.get('label', '')}
        kind = field.get('kind') or field.get('type')
        if not key or keys[key] != 1:
            if field.get('required'):
                blocked.append(dict(seen, reason='ambiguous field reference'))
            continue
        if kind=='checkbox':
            from .terms import saved_term
            candidate=saved_term(root,field.get('label'),data.get('opening'),field.get('terms_sha256'))
            if candidate:
                answer=dict(candidate, key=key, field=field['label'], refs=field.get('refs') or refs or [str(key)])
                validate_answers(root,[answer],data.get('opening'))
                if field.get('checked'): answer['observed_value']='checked'
                answers.append(answer)
                if not field.get('checked'):
                    ops.append({'key':key,'mode':'check','value':True})
            elif field.get('required'):
                blocked.append(dict(seen,reason='checkbox has no matching approval for this opening'))
            continue
        if field.get('type') in ('password', 'hidden') or kind=='password' or REVIEW.search(label):
            if field.get('required'):
                blocked.append(dict(seen, reason='owner review required'))
            continue
        if kind == 'file':
            if RESUME.search(label) and not field.get('attachment_verified'):
                blocked.append(dict(seen, reason='verify selected PDF attachment'))
            elif field.get('required') and not field.get('attachment_verified'):
                blocked.append(dict(seen, reason='required attachment needs review'))
            continue
        if (field.get('filled') or field.get('checked')) and field.get('observed_value') is None:
            if field.get('required'):
                blocked.append(dict(seen, reason='preserve unverified prefilled answer'))
            continue
        if kind == 'textbox':
            kind = 'text'
        candidate = None
        for binding in plain_options(root):
            if field_label({'label':binding['field']}) == label:
                candidate = {'kind': 'plain', 'meaning': binding['meaning'], 'value': binding['value']}
                break
        candidate = candidate or literal_candidate(label, lines)
        option = None
        if candidate and kind in ('select', 'radio', 'combobox'):
            option = option_for(candidate['value'], field)
            if option:
                candidate['value'] = option['label']
            else:
                candidate = None
        if candidate and kind in ('text', 'date', 'email', 'tel', 'url', 'textarea', 'select', 'radio', 'combobox'):
            answer = dict(candidate, key=key, field=field['label'], refs=field.get('refs') or refs or [str(key)])
            try:
                validate_answers(root, [answer])
            except Denied:
                candidate = None
            else:
                mode = kind if kind in ('select', 'radio', 'combobox') else 'text'
                if field.get('filled') and normalize(field.get('observed_value'))==normalize(candidate['value']):
                    answer['observed_value']=field['observed_value']
                elif field.get('filled'):
                    blocked.append(dict(seen, reason='preserve conflicting prefilled answer'))
                    continue
                else:
                    ops.append({'key': key, 'mode': mode, 'value': option['value'] if option else candidate['value']})
                answers.append(answer)
        elif candidate:
            candidate = None
        if not candidate and field.get('required'):
            blocked.append(dict(seen, reason='no supported saved answer for visible field'))
    # Re-observe and verify operations before advancing.
    return {'job_key': job_key, 'ops': ops, 'answers': answers, 'blocked': blocked,
            'navigation': navigation(controls, bool(fields), blocked or ops)}


def answer_revision(root):
    import hashlib
    h=hashlib.sha256()
    for name in ('form-answers.md','facts.md','answer-bank.md'):
        path=Path(root)/name
        h.update(name.encode())
        h.update(hashlib.sha256(path.read_bytes() if path.is_file() else b'').digest())
    return h.hexdigest()


def technical_hold(questions, retry):
    """A retry tag must describe every outstanding question before it can fund another run."""
    from .learning import classify
    if retry not in ('private_autofill','attachment_evidence') or not questions:
        return False
    return all(classify('question', question)[0] == retry for question in questions)


def park_question(store,q):
    import json
    from .store import Denied
    job=store.job(q['job_key'])
    incoming = q.get('questions') or [q.get('question')]
    if not isinstance(incoming, list) or not all(isinstance(v, str) and v.strip() for v in incoming) or not q.get('refs'):
        raise Denied('observed field labels and source required')
    revision=answer_revision(store.path.parent)
    with store.tx():
        store.check(q['owner'],q['token'])
        prior=store.con.execute("SELECT payload,state FROM jr_backlog WHERE provider='application_handoff' AND item_key=?",(job['job_key'],)).fetchone()
        old=json.loads(prior['payload']) if prior and prior['state']!='resolved' else {}
        if old.get('answer_revision')!=revision: old={}
        questions=list(dict.fromkeys([*(old.get('questions') or ([old['question']] if old.get('question') else [])),
                                     *(' '.join(v.split())[:300] for v in incoming)]))
        payload={'question':questions[0],'questions':questions,'refs':list(dict.fromkeys([*old.get('refs',[]),*q['refs']])),
                 'job_key':job['job_key'],'answer_revision':revision}
        retry = q.get('retry_on')
        if technical_hold(questions,retry) and q.get('run_id') and (not old or old.get('retry_on') == retry):
            payload.update(retry_on=retry,blocked_run=q['run_id'])
        store.con.execute("""INSERT INTO jr_backlog(provider,item_key,payload,first_seen,state) VALUES('application_handoff',?,?,?,'pending')
            ON CONFLICT(provider,item_key) DO UPDATE SET payload=excluded.payload,state='pending'""",
            (job['job_key'],json.dumps(payload),store.now()))
        from . import learning
        for question in incoming:
            learning.observe(store,q.get('run_id'),job['job_key'],'question',question,payload.get('retry_on'))
    return {'saved':True,'job_key':job['job_key'],'questions':len(questions)}


def check_evidence(data):
    # Import here because receipt validation shares this module's URL parser.
    from .receipts import validate

    opening, after, before = (data.get(name) or {} for name in ('opening', 'after', 'before'))
    job_url = normalized_url(opening.get('job_url'))
    after_url = normalized_url(after.get('url'))
    kind = after.get('kind')
    quote = after.get('quote') or ''
    observed = timestamp(after.get('observed_at'))
    expected_req = opening.get('requisition_id') or requisition_id(job_url)

    def pending(reason):
        return {'outcome': 'unknown', 'reason': reason}

    if not (opening.get('job_key') and job_url and normalize(opening.get('title'))):
        return pending('exact opening identity required')
    if kind not in ('receipt', 'history', 'email') or not after_url or not observed:
        return pending('application receipt or history observation required')
    evidence = {
        'proof': 'confirmation_email' if kind == 'email' else 'portal_confirmation',
        'job_key': opening['job_key'], 'job_url': job_url, 'requisition_id': expected_req,
        'refs': after.get('refs'), 'quotes': [quote], 'observed_at': after.get('observed_at'),
        'receipt_kind': kind, 'receipt_url': after_url, 'receipt_title': after.get('title'),
        'receipt_message_id':after.get('message_id'),
        'receipt_requisition_id': after.get('requisition_id'), 'receipt_job_url': after.get('job_url'),
    }
    clicked = timestamp(data.get('clicked_at'))
    if clicked and clicked <= observed and data.get('action')=='submit_application':
        evidence['clicked_at'] = clicked.isoformat()
    if kind == 'receipt':
        prior_time = timestamp(before.get('observed_at'))
        if data.get('action') != 'submit_application' or not prior_time or prior_time >= observed:
            return pending('a new receipt after the final application action is required')
        if normalize(before.get('quote')) == normalize(quote) or (
                normalized_url(before.get('url')) == after_url and before.get('kind') == kind):
            return pending('observation does not show a new receipt')
        evidence['transition'] = {
            'action': 'submit_application', 'before_job_url': before.get('job_url'),
            'before_tab': before.get('tab'), 'after_tab': after.get('tab'),
            'before_quote': before.get('quote'), 'after_url': after_url,
        }
    job = dict(opening, link=job_url, requisition_id=expected_req)
    try:
        validate(job, evidence)
    except ValueError as exc:
        return pending(str(exc))
    return {'outcome': 'confirmed', 'evidence': evidence}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('plan', 'evidence'))
    args = parser.parse_args()
    try:
        raw = sys.stdin.read(128 * 1024 + 1)
        if len(raw) > 128 * 1024:
            raise ValueError('form observation exceeds 128 KiB')
        data = json.loads(raw)
        result = plan(data) if args.command == 'plan' else check_evidence(data)
    except (AttributeError, KeyError, TypeError, ValueError, OSError) as exc:
        print(json.dumps({'error': 'invalid form observation', 'detail': str(exc) if type(exc) is ValueError else type(exc).__name__}))
        return 2
    print(json.dumps(result, ensure_ascii=False, separators=(',', ':')))
    return 0



def inventory_key(run_id, job_key):
    from .policy import digest
    return 'form-fields:' + digest([run_id, job_key])


def inventory(store, run_id, job_key):
    row=store.con.execute('SELECT value FROM jr_meta WHERE key=?',(inventory_key(run_id,job_key),)).fetchone()
    return json.loads(row[0]) if row else None


def worker_plan(store, q):
    from .policy import digest
    from .store import Denied
    job=store.job(q['job_key'])
    with store.tx():
        store.check(q['owner'],q['token'])
        result=plan(dict(q,job_key=job['job_key'],opening=job),store.path.parent)
        if not q.get('run_id') or not q.get('page_ref'):
            return result
        run=store.con.execute('SELECT owner,token,status FROM jr_runs WHERE id=?',(q['run_id'],)).fetchone()
        if not run or tuple(run)!=(q['owner'],q['token'],'running'):
            raise Denied('active run required to save the observed fields')
        page=str(q['page_ref'])
        pages=inventory(store,q['run_id'],job['job_key']) or {}
        required=[]
        for f in q['fields']:
            if (not f.get('required') or f.get('disabled') or f.get('visible') is False
                    or f.get('type') in ('file','hidden') or f.get('kind')=='file'):
                continue
            key,label=f.get('key'),f.get('label')
            if not key or not label:
                raise Denied('visible required field lacks stable identity')
            ref=digest([job['job_key'],page,key])
            required.append({'key':key,'field':label,'ref':ref})
        if len({v['ref'] for v in required})!=len(required):
            raise Denied('duplicate required field references')
        pages[page]=required
        store.con.execute('INSERT OR REPLACE INTO jr_meta VALUES(?,?)',(inventory_key(q['run_id'],job['job_key']),json.dumps(pages)))
        for ans in result['answers']:
            match=[f for f in required if f['field']==ans['field'] and (
                   ans.get('key')==f['key'] if ans.get('key') is not None else True)]
            if len(match)==1:
                ans['field_ref']=match[0]['ref']
        return dict(result,required_fields=required)


if __name__ == '__main__':
    sys.exit(main())
