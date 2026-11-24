"""Final answer provenance checks; unavailable facts never become defaults, except the owner's
recorded rule-based answers (kind 'rule'), which are a closed list with hard limits."""
import re
import json
from pathlib import Path
from .store import Denied
from .terms import RECRUITING_CONSENT

FILES={'facts.md','form-answers.md','answer-bank.md'}

# Optional rules require matching evidence in the private answer bank.
RULES={
    'recruiting_consent': ('form-answers.md', RECRUITING_CONSENT),
    'default_yes':     ('form-answers.md','[allow] ordinary non-sensitive yes/no questions'),
    'sms_consent_yes': ('form-answers.md','[allow] application text messages'),
    'legal_ack':       ('form-answers.md','[allow] standard legal acknowledgement'),
    'years_rule':      ('form-answers.md','Years of full-time professional experience:'),
    'salary_default':  ('form-answers.md','Salary with no posted range:'),
    'rating_rule':     ('stage:one-click.md','Rating / proficiency scales'),
    'choice_rule':     ('stage:one-click.md','Multiple choice with no saved answer'),
    'free_text_rule':  ('stage:one-click.md','Short free-text'),
    'computed_date':   ('stage:one-click.md','Dates you can compute from the files'),
}
# A rule never answers these: identity, legal status, signatures, credentials, payment. They need a
# saved literal (or a user handoff), exactly the "STILL STOP" list in one-click.md.
FORBIDDEN=re.compile(r'citizen|clearance|criminal|felony|convict|arrest|date of birth|birth ?date|\bdob\b|\bage\b|'
                     r'\bssn\b|social security|passport|national id|government id|sponsor|visa\b|work authori|'
                     r'veteran|disabilit|gender|\brace\b|ethnic|e-?sign|signature|sign here|password|credential|'
                     r'bank|routing|credit card|payment|export|sanction|embargo|government official|debar',re.I)
MAX_FREE_TEXT_WORDS=150

def plain_options(private_root):
    """Read per-applicant option bindings from the private configuration directory."""
    path = Path(private_root) / 'form-options.json'
    if not path.is_file():
        return []
    try:
        rows = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise Denied('invalid private form-options.json') from exc
    if not isinstance(rows, list) or any(not isinstance(row, dict) or
        not all(isinstance(row.get(k), str) and row[k].strip() for k in ('meaning', 'field', 'value', 'quote')) for row in rows):
        raise Denied('form options require a meaning, exact field, value and source quote')
    return rows


def _plain(private_root, item):
    saved = _text(private_root, 'form-answers.md').splitlines()
    for row in plain_options(private_root):
        if (row['meaning'] == item.get('meaning') and row['field'].strip().casefold() == str(item['field']).strip().casefold()
                and row['value'].strip().casefold() == str(item['value']).strip().casefold() and row['quote'] in saved):
            return
    raise Denied('option is not supported by the saved field, value and source quote')


def _text(private_root,source):
    path=Path(private_root)/'stages'/source[6:] if source.startswith('stage:') else Path(private_root)/source
    return path.read_text() if path.exists() else ''


def _rule(private_root,item):
    rule=item.get('rule')
    if rule not in RULES:raise Denied('unsupported answer rule; use a saved literal or user handoff')
    source,marker=RULES[rule]
    if marker not in _text(private_root,source):raise Denied('answer rule is not on record in '+source)
    label=str(item.get('field',''))
    if FORBIDDEN.search(label):raise Denied('identity, legal, signature or credential field needs a saved literal or user handoff')
    value=str(item['value'])
    if rule in ('default_yes', 'choice_rule') and re.search(r'consent|agreement|attest|certif|terms|privacy|arbitrat|acknowledg|no ai|not use|non-compete',label,re.I):
        raise Denied('a displayed term needs its own scoped approval')
    if rule in ('years_rule', 'salary_default'):
        source_text = _text(private_root, source)
        match = re.search(re.escape(marker) + r'[ \t]*([$]?[\d,]+(?:\.\d+)?)(?:[.\s]|$)', source_text)
        if not match or re.sub(r'[$,]', '', match[1]) != re.sub(r'[$,]', '', value.strip()):
            raise Denied('numeric answer differs from the saved rule')
    if rule=='free_text_rule':
        if len(value.split())>MAX_FREE_TEXT_WORDS:raise Denied('free text over 150 words needs a user handoff')
        facts=_text(private_root,'facts.md')
        if not item.get('claims_checked') or not item.get('claim_quotes') or any(q not in facts for q in item['claim_quotes']):
            raise Denied('written answer requires claims quoted from facts.md')


def validate_answers(private_root,answers,job=None):
    if not isinstance(answers,list) or not answers:raise Denied('field-level saved-answer evidence required')
    for item in answers:
        if not item.get('field') or not item.get('refs') or 'value' not in item:raise Denied('answer field, value and observation references required')
        if re.search(r'password|passcode|one[- ]time (?:code|password)|verification code|security question|social security number',str(item['field']),re.I):
            raise Denied('private account or identity field cannot enter application evidence')
        kind=item.get('kind');value=str(item['value'])
        from .terms import saved_term
        if kind=='scoped_term':
            term=saved_term(private_root,item['field'],job,item.get('terms_sha256'))
            if not term or any(item.get(k)!=v for k,v in term.items()):
                raise Denied('application-specific approval does not cover this opening and wording')
            continue
        if kind=='rule' and item.get('rule') in ('legal_ack','sms_consent_yes','recruiting_consent'):
            term=saved_term(private_root,item['field'],job)
            if not term or term['rule']!=item['rule'] or value.lower() not in ('checked','yes','true'):
                raise Denied('displayed term outside recorded approval')
        if kind=='prefilled':
            if value!=str(item.get('observed_value')) or not item.get('preserved'):raise Denied('prefilled answer must be observed and preserved')
            continue
        if kind=='rule':
            _rule(private_root,item)
            continue
        if kind=='plain':
            _plain(private_root,item)
            continue
        source=item.get('source');quote=item.get('quote','')
        if source not in FILES or not quote:raise Denied('saved answer source and quote required')
        path=Path(private_root)/source
        if not path.exists() or quote not in path.read_text():raise Denied('answer evidence quote not found in saved facts')
        if kind=='literal':
            if not re.search(r'(?<!\w)'+re.escape(value)+r'(?!\w)',quote,re.I):raise Denied('literal answer is not supported by its quote')
        elif kind=='normalized_phone':
            if re.sub(r'\D','',value)!=re.sub(r'\D','',quote):raise Denied('phone must match saved digits')
        elif kind=='factual_writing':
            if not item.get('claims_checked') or not item.get('claim_quotes'):raise Denied('written answer requires checked factual claims')
            if any(q not in path.read_text() for q in item['claim_quotes']):raise Denied('written claim lacks saved evidence')
        else:raise Denied('unsupported answer derivation; use a saved literal or user handoff')
    return True


def check_required_coverage(required, answers):
    """Verify each observed required field against its own readback at final review."""
    if not isinstance(required,list) or not isinstance(answers,list):
        raise Denied('grouped form inventory and answer readback required')
    evidence={item.get('field_ref'):item for item in answers if isinstance(item,dict) and item.get('field_ref')}
    for row in required:
        item=evidence.get(row['ref'])
        if not item or item.get('field')!=row['field'] or 'observed_value' not in item:
            raise Denied('a required field lacks its own readback')
        expected=' '.join(str(item['value']).casefold().split())
        seen=' '.join(str(item['observed_value']).casefold().split())
        if not ((expected=='checked' and seen in ('true','checked')) or seen==expected):
            raise Denied('required answer differs from browser readback')
