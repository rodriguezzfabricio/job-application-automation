"""Match a displayed application checkbox to an approval in the local answer bank."""
import re
import hashlib
import json
import unicodedata
from pathlib import Path


# Full label matches avoid using the first clause of a compound new agreement.
RECRUITING_CONSENT = '[allow] recruiting terms and application contact'
SCREENING_APPROVAL = 'Conditional screening after application review: Yes.'
TERMS = r'terms (?:of (?:use|service)|(?:and|&) conditions)'
PRIVACY = r'(?:applicant |candidate )?privacy (?:notice|policy)'
STANDARD = (
    ('recruiting_consent', rf'(?:i (?:accept|agree to|acknowledge) (?:the )?)?{TERMS}(?:(?:,? and |, | & )(?:the )?{PRIVACY})?'),
    ('recruiting_consent', rf'(?:by )?applying to this job[,]? you are creating an account, agreeing to (?:the )?{TERMS}, (?:the )?{PRIVACY}\s*and permitting text/calls about your application(?: \(message/data rate[s]? may apply\))?'),
    ('recruiting_consent', r'i (?:agree|consent) to (?:receive|be contacted by) (?:text messages?|texts?|sms)(?: and | or |/)(?:phone )?calls? (?:about|regarding) (?:this|my|your) application'),
    ('sms_consent_yes', r'(?:i (?:agree|consent) to (?:receive|be contacted by)|can (?:we|you) (?:send me|contact me by)|may we contact you by) (?:sms|texts?|text messages?)(?: (?:about|regarding) (?:this|my|your) application)?'),
    ('sms_consent_yes', r'(?:may|can) we text you about (?:this|your) application'),
    ('legal_ack', r'(?:i (?:acknowledge(?: that)? |confirm(?: that)? )?(?:have read|read|agree to|accept|acknowledge) (?:the |this )?)?(?:candidate |applicant |application )?privacy (?:notice|policy)'),
    ('legal_ack', r'i (?:agree to|acknowledge|accept) (?:the |this )?(?:applicant |application )?arbitration agreement'),
    ('legal_ack', r'i acknowledge (?:the |this )?candidate ai responsible use policy'),
    ('legal_ack', r'(?:i (?:certify|confirm)(?: that)? )?all application materials are my own work'),
    ('legal_ack', r'i (?:certify|confirm|attest)(?: that)? (?:my answers|the information (?:provided|i (?:have )?provided)(?: in this application)?) (?:are|is) (?:true|accurate)(?: and (?:complete|correct|accurate))?'),
)
MARKERS = {'sms_consent_yes': '[allow] application text messages', 'legal_ack': '[allow] standard legal acknowledgement',
           'recruiting_consent': RECRUITING_CONSENT}
EXCLUDE = re.compile(r'not (?:use|used)|no (?:ai|artificial intelligence)|without (?:ai|artificial intelligence)|'
                     r'non[- ]?compete|signed offer|paid service|authorize.*payment', re.I)


def clean(text):
    return ' '.join(str(text or '').casefold().split()).rstrip('.?* ')


def wording_hash(text):
    normalized = ' '.join(unicodedata.normalize('NFKC', text).split())
    return hashlib.sha256(normalized.encode()).hexdigest()


def archived_wording(root, company, opening_id, label):
    path = Path(root) / 'approved-terms.json'
    if not path.is_file():
        return None
    try:
        entries = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    if not isinstance(entries, list) or not opening_id:
        return None
    for row in entries:
        if (isinstance(row, dict) and clean(row.get('company')) == company
                and str(row.get('opening_id', '')).casefold() == opening_id.casefold()
                and clean(row.get('label')) == label and isinstance(row.get('wording'), str)):
            return row['wording']
    return None


def saved_term(root, field, job=None, terms_sha256=None):
    label = clean(field)
    if not label or EXCLUDE.search(label):
        return None
    p = Path(root) / 'form-answers.md'
    if not p.is_file():
        return None
    lines = p.read_text().splitlines()
    for rule, pattern in STANDARD:
        if not terms_sha256 and re.fullmatch(pattern, label):
            quote = next((line for line in lines if MARKERS[rule] in line), None)
            if quote:
                return {'kind': 'rule', 'rule': rule, 'source':'form-answers.md', 'quote':quote, 'value':'checked'}
    if not job:
        return None
    # A named approval is valid only for its recorded opening and its displayed terms.
    from .form_step import requisition_id
    identifier = str(job.get('requisition_id') or requisition_id(job.get('link','')) or '')
    company = clean(job.get('company'))
    if not company:
        return None
    if terms_sha256:
        original=archived_wording(root,company,identifier,label)
        quote=next((line for line in lines if clean(line.lstrip('- ')).startswith(company+' ') and ': Yes.' in line
                    and ('terms' in line.casefold()) and identifier and identifier.casefold() in line.casefold()), None)
        if quote and original and wording_hash(original)==terms_sha256:
            return {'kind':'scoped_term','source':'form-answers.md','quote':quote,'value':'checked',
                    'job_key':job['job_key'],'terms_sha256':terms_sha256}
        return None
    if not identifier:
        return None
    for line in lines:
        statement, yes, _ = line.lstrip('- ').partition(': Yes.')
        if not yes or not clean(statement).startswith(company + ' '):
            continue
        match = re.search(r'(?<![a-z0-9])' + re.escape(identifier) + r'(?![a-z0-9])', statement, re.I)
        if not match:
            continue
        terms = statement[match.end():].strip(' ,')
        terms = re.split(r', (?:for|to continue) this application\b', terms, flags=re.I)[0]
        terms = re.sub(r'^displayed ', '', terms, flags=re.I)
        if terms and clean(terms) in (label, re.sub(r'^i (agree to|accept|acknowledge) (the |these )?', '', label)):
            return {'kind':'scoped_term', 'source':'form-answers.md', 'quote':line, 'value':'checked',
                    'job_key':job['job_key']}
    return None
