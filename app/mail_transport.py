"""Low-level Gmail HTTPS capability, not packet/fact validation or an outbox.

Caller MUST validate trusted profile, current job, attachment hash/job binding and
claim fidelity before calling; durably claim the packet first. API acceptance is
not inbox delivery. Never automatically retry an unknown send outcome.
"""
import base64
from email import policy
from email.parser import BytesParser
import json
import os
from pathlib import Path
import re
import stat
from urllib import error, parse, request

TOKEN_URL = 'https://oauth2.googleapis.com/token'
SEND_URL = 'https://gmail.googleapis.com/gmail/v1/users/me/messages/send'


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs): return None


def _https(method, url, headers, body, timeout):
    # Fixed endpoints, TLS defaults, no redirects that could disclose credentials.
    opener = request.build_opener(request.ProxyHandler({}), _NoRedirect())
    req = request.Request(url, data=body, headers=headers, method=method)
    try:
        with opener.open(req, timeout=timeout) as response:
            return response.status, response.read(65537), response.headers.get('Retry-After')
    except error.HTTPError as exc:
        return exc.code, b'', exc.headers.get('Retry-After') if exc.headers else None  # Do not propagate provider bodies/secrets to logs.


def _reply(value):
    # Existing injected HTTP adapters may return two fields; live HTTPS adds only Retry-After.
    if not isinstance(value,tuple) or len(value) not in (2,3):raise ValueError('invalid HTTP reply')
    status,body=value[:2]
    if type(status) is not int or not 100<=status<=599 or not isinstance(body,bytes):raise ValueError('invalid HTTP reply')
    return status,body,value[2] if len(value)==3 else None


def _mailbox(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9._+!%\-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,63}', value):
        raise ValueError('one plain ASCII mailbox required')
    local, domain = value.rsplit('@', 1)
    return local + '@' + domain.lower()


def _credentials(path):
    path = Path(path)
    parent = path.parent.stat()
    if parent.st_uid != os.getuid() or parent.st_mode & 0o022:
        raise ValueError('credential directory must be owned and not writable by others')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_nlink != 1 or info.st_size > 16384:
            raise ValueError('protected regular credential file required')
        raw = source.read(16385)
    try: data = json.loads(raw)
    except (ValueError, UnicodeError): raise ValueError('invalid credential JSON') from None
    fields = {'client_id', 'client_secret', 'refresh_token'}
    if not isinstance(data, dict) or set(data) != fields or any(not isinstance(data[k], str) or not data[k] or len(data[k]) > 4096 or any(ord(c) < 32 for c in data[k]) for k in fields):
        raise ValueError('credential fields must be client_id, client_secret, refresh_token')
    return data


def _validate(raw, recipient, sender):
    if not isinstance(raw, bytes) or len(raw) > 5 * 1024 * 1024:
        raise ValueError('bounded RFC822 bytes required')
    message = BytesParser(policy=policy.default).parsebytes(raw)
    allowed = {'from', 'to', 'subject', 'message-id', 'date', 'mime-version', 'content-type'}
    names = [name.lower() for name in message.keys()]
    if len(names) != len(set(names)) or set(names) - allowed:
        raise ValueError('unexpected or duplicate message header')
    for name in ('From', 'To', 'Subject', 'Message-ID'):
        if len(message.get_all(name, [])) != 1 or not str(message[name]).strip():
            raise ValueError('required header missing')
    if _mailbox(str(message['To'])) != recipient or _mailbox(str(message['From'])) != sender:
        raise ValueError('mailbox differs from protected configuration')
    if not re.fullmatch(r'<[^<>\s]+@[^<>\s]+>', str(message['Message-ID'])):
        raise ValueError('stable Message-ID required')
    if message.get_content_type() != 'multipart/mixed': raise ValueError('mixed MIME required')
    parts = list(message.iter_parts())
    if len(parts) != 2 or parts[0].get_content_type() != 'text/plain' or parts[0].get_content_disposition() is not None or parts[1].get_content_type() != 'application/pdf' or parts[1].get_content_disposition() != 'attachment':
        raise ValueError('one plain body and one PDF attachment required')
    for part in message.walk():
        if part.defects or any(getattr(value, 'defects', ()) for value in part.values()):
            raise ValueError('malformed MIME')
        if part is not message:
            names = [name.lower() for name in part.keys()]
            if part.is_multipart() or len(names) != len(set(names)) or set(names) - {'content-type', 'content-transfer-encoding', 'content-disposition', 'mime-version'}:
                raise ValueError('unexpected nested header or MIME')
    decoded = []
    for part in parts:
        encoding = str(part.get('Content-Transfer-Encoding', '7bit')).lower().strip()
        if encoding not in {'7bit', '8bit', 'base64', 'quoted-printable'}:
            raise ValueError('unsupported transfer encoding')
        payload = part.get_payload()
        if not isinstance(payload, str): raise ValueError('scalar MIME payload required')
        if encoding == 'base64':
            try: base64.b64decode(re.sub(r'[\r\n\t ]', '', payload), validate=True)
            except (ValueError, UnicodeError): raise ValueError('invalid base64 MIME') from None
        if encoding == 'quoted-printable' and re.search(r'=(?![A-Fa-f0-9]{2}|\r?\n)', payload):
            raise ValueError('invalid quoted-printable MIME')
        value = part.get_payload(decode=True)
        if part.defects or not isinstance(value, bytes) or (encoding == '7bit' and any(b > 127 for b in value)):
            raise ValueError('malformed decoded MIME')
        decoded.append(value)
    pdf = decoded[1]
    if not pdf or not pdf.startswith(b'%PDF-') or not (parts[1].get_filename() or '').lower().endswith('.pdf'):
        raise ValueError('PDF attachment required; caller must verify contents')


class GmailTransport:
    def __init__(self, credential_path, *, recipient, sender, http=None,send_allowed=None):
        self.credential_path = credential_path
        self.recipient, self.sender = _mailbox(recipient), _mailbox(sender)
        self.http = http or _https
        if send_allowed is not None and not callable(send_allowed):raise ValueError('trusted send policy callback required')
        self.send_allowed=send_allowed

    def preflight(self,raw):
        _validate(raw,self.recipient,self.sender)
        _credentials(self.credential_path)
        return True

    def send(self, raw):
        _validate(raw, self.recipient, self.sender)
        credentials = _credentials(self.credential_path)
        result = {'state': 'not_attempted', 'delivery_verified': False, 'provider_message_id': None}
        form = dict(credentials, grant_type='refresh_token')
        try:
            status, body, _ = _reply(self.http('POST', TOKEN_URL, {'Content-Type': 'application/x-www-form-urlencoded'}, parse.urlencode(form).encode(), 15))
            token = json.loads(body).get('access_token') if status == 200 and len(body) <= 65536 else None
            if not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9._~+/\-]+=*', token): return result
        except Exception: return result  # No send attempted; no credentials/provider text logged.
        payload = json.dumps({'raw': base64.urlsafe_b64encode(raw).decode('ascii')}).encode()
        try:
            if self.send_allowed is not None and self.send_allowed() is not True:return result
        except Exception:return result
        result['state'] = 'unknown'
        try:
            status, body, retry_after = _reply(self.http('POST', SEND_URL, {'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'}, payload, 15))
            result['http_status']=status
            if status==429:result['retry_after']=retry_after
            if 400 <= status < 500: result['state'] = 'rejected'
            elif status == 200 and len(body) <= 65536:
                provider_id = json.loads(body).get('id')
                if isinstance(provider_id, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,256}', provider_id):
                    result.update(state='api_accepted', provider_message_id=provider_id)
        except Exception: pass  # Timeout/reset/malformed success: acceptance uncertain, never retry here.
        return result
