"""Verify application status and opening identity from the observed receipt."""
import re
from urllib.parse import urlsplit

from .policy import history_identity, timestamp
from .form_step import normalized_url, requisition_id


SUCCESS = re.compile(
    r"\b(?:thank(?:s| you) for (?:applying|submitting your application)|"
    r"(?:we(?:'ve| have)? |you(?:'ve| have)? )?(?:received|submitted) your application|"
    r"(?:your |the )?application (?:was |has been |is )?(?:successfully )?(?:submitted|received)|"
    r"you(?: have|'ve) (?:successfully )?applied)\b", re.I)
NEGATIVE = re.compile(
    r"\b(?:not|never|unable to|failed to|couldn.t|could not|unsuccessful|no longer|"
    r"how to|before|once|if|when|has yet to|will be|will receive|preview|draft)\b", re.I)
ACCOUNT = re.compile(r"\b(?:account (?:created|registration|verification)|(?:creating|verifying) (?:your|an) account|"
                     r"thank you for (?:registering|signing up))\b", re.I)
ACCOUNT_PAGE = re.compile(r"(?:^|[/#?=&])(?:register|registration|sign[-_]?up|log[-_]?in|sign[-_]?in|create[-_]?account)(?:$|[/#?=&])", re.I)
NOT_SENT = re.compile(r"\bwe (?:have |did )?not (?:receive|received) your application\b|"
                      r"\b(?:application (?:was |has |has been |is )?not (?:been )?(?:submitted|received)|"
                      r"(?:failed|unable) to submit (?:your |the )?application|we (?:could not|couldn.t) submit your application)\b", re.I)
HISTORY_STATES = {'submitted', 'received', 'applied', 'under review', 'application under review', 'in review'}


def normalized(text):
    return ' '.join(str(text or '').split()).casefold()


def trusted_origin(job_url, receipt_url):
    a, b = urlsplit(job_url), urlsplit(receipt_url)
    if a.scheme != 'https' or b.scheme != 'https' or a.netloc != b.netloc:
        return False
    tenants = {'jobs.lever.co', 'jobs.ashbyhq.com', 'apply.workable.com', 'jobs.smartrecruiters.com',
               'boards.greenhouse.io', 'job-boards.greenhouse.io'}
    if a.hostname in tenants:
        return a.path.strip('/').split('/')[0] == b.path.strip('/').split('/')[0]
    from . import accounts
    if accounts.account_site(job_url):
        from .account_flow import tenant
        scope = tenant(job_url)
        return bool(scope and scope == tenant(receipt_url))
    return True


def validate(job, evidence, *, not_submitted=False):
    """Raise ValueError on ambiguous or contradictory evidence. No tracker writes."""
    def require(ok, reason):
        if not ok:
            raise ValueError(reason)

    proof = evidence.get('proof')
    if not_submitted:
        require(proof == 'explicit_portal_non_submission', 'explicit portal non-submission required')
    else:
        require(proof in {'portal_confirmation', 'confirmation_email', 'portal_application_submitted'},
                'application receipt required')
    refs, quotes = evidence.get('refs'), evidence.get('quotes')
    require(all(isinstance(x, list) and x and all(isinstance(v, str) and v.strip() for v in x)
                for x in (refs, quotes)) and timestamp(evidence.get('observed_at')), 'observed receipt references and quotes required')
    posting = normalized_url(job.get('link'))
    require(posting and evidence.get('job_key', job['job_key']) == job['job_key'], 'wrong opening key')
    if evidence.get('receipt_company'):
        require(normalized(evidence['receipt_company']) == normalized(job.get('company')), 'receipt employer does not match the opening')
    typed_history = proof == 'portal_application_submitted' and not evidence.get('receipt_kind')
    kind = evidence.get('receipt_kind') or ('history' if typed_history else None)
    require(kind in ('receipt', 'history', 'email') and (kind == 'email') == (proof == 'confirmation_email'),
            'an observed application receipt, history entry or email is required')
    source = normalized_url(evidence.get('receipt_url'))
    observed_link = normalized_url(evidence.get('receipt_job_url'))
    if observed_link and history_identity(observed_link) == history_identity(posting):
        observed_link = posting
    if evidence.get('job_url'):
        require(normalized_url(evidence['job_url']) == posting, 'wrong requested posting URL')
    if observed_link:
        require(observed_link == posting, 'receipt identifies a different opening')
    require(source and (kind == 'email' or trusted_origin(posting, source)), 'unverified receipt origin')
    require(kind == 'email' or not ACCOUNT_PAGE.search(urlsplit(source).path + '#' + urlsplit(source).fragment), 'account page')
    expected = str(job.get('requisition_id') or requisition_id(posting) or '')
    observed = str(evidence.get('receipt_requisition_id') or '')
    if expected and observed:
        require(observed == expected, 'receipt identifies another requisition')
    source_id = requisition_id(source) if kind != 'email' else None
    if expected and source_id:
        require(source_id == expected, 'observed receipt URL identifies another requisition')
    title = normalized(evidence.get('receipt_title'))
    require(title and title == normalized(job.get('title')), 'receipt title does not match the opening')
    tenant_path = urlsplit(posting).path.strip('/').split('/')[0]
    id_matches = bool(expected and observed == expected and trusted_origin(posting, source)
                      and urlsplit(source).path.strip('/').split('/')[0] == tenant_path)
    change = evidence.get('transition') or {}
    transitioned = bool(kind == 'receipt' and change.get('action') == 'submit_application'
                        and change.get('before_tab') and change.get('before_tab') == change.get('after_tab')
                        and normalized_url(change.get('before_job_url')) == posting
                        and normalized_url(change.get('after_url')) == source and trusted_origin(posting, source)
                        and change.get('before_quote') and not SUCCESS.search(change['before_quote']))
    require(observed_link == posting or id_matches or transitioned, 'receipt lacks observed identity for the exact opening')
    if kind == 'email':
        mailbox=urlsplit(source)
        require(mailbox.hostname=='mail.google.com' and mailbox.path.startswith('/mail/')
                and isinstance(evidence.get('receipt_message_id'),str) and evidence['receipt_message_id'].strip()
                and observed_link==posting, 'confirmation email requires its mailbox message ID and exact posting link')
    status_quotes = [q.strip() for q in quotes if len(q) <= 1000]
    positive = any(not NEGATIVE.search(q) and (SUCCESS.search(q) or (kind == 'history' and normalized(q) in HISTORY_STATES)) for q in status_quotes)
    if not_submitted:
        require(not positive and kind != 'email' and any(NOT_SENT.search(q) for q in status_quotes), 'explicit application failure required')
    else:
        require(not any(ACCOUNT.search(q) or NOT_SENT.search(q) for q in status_quotes)
                and positive, 'no affirmative application receipt')
