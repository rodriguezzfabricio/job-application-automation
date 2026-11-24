"""Fetch public job-board pages. Surface access refusals to the caller."""
from contextlib import contextmanager
from dataclasses import dataclass
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler


class Blocked(RuntimeError):
    """A board did not return an accessible listing."""


@dataclass
class Page:
    text: str


class SameHostRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urlsplit(newurl).scheme != 'https' or urlsplit(newurl).hostname != urlsplit(req.full_url).hostname:
            raise Blocked('Job board redirected off its public host')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def http_get(url, *, host, referer=None, timeout=30, accept='text/html'):
    target = urlsplit(url)
    if target.scheme != 'https' or target.hostname != host or target.username or target.password:
        raise Blocked('Expected an HTTPS URL on the board host')
    headers = {'User-Agent':'job-application-automation/1.0', 'Accept':accept}
    if referer:
        headers['Referer'] = referer
    try:
        with build_opener(SameHostRedirects()).open(Request(url, headers=headers), timeout=timeout) as response:
            data = response.read(8 * 1024 * 1024 + 1)
            if len(data) > 8 * 1024 * 1024:
                raise Blocked('Job board response exceeded the size limit')
            body = data.decode(response.headers.get_content_charset() or 'utf-8', errors='replace')
    except (URLError, TimeoutError) as exc:
        raise Blocked(f'Job board unavailable ({getattr(exc, "code", "network error")})') from exc
    if any(marker in body[:12000].casefold() for marker in ('verify you are human', 'just a moment...', 'cf-chl-')):
        raise Blocked('Job board requested human verification')
    return Page(body)


class BrowserSession:
    """Compatibility wrapper for public page readers."""
    def fetch(self, host, path, *, accept='text/html'):
        return http_get(f'https://{host}{path}', host=host, accept=accept).text


@contextmanager
def browser():
    yield BrowserSession()
