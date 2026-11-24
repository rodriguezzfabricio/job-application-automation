"""Test fixtures: a canned-response HTTP server over real sockets (no mocks).

The production FeedTransport refuses non-sanctioned hosts at construction (P1),
so politeness/API tests inject a transport factory that points at this local
server instead. The factory seam is test-only; the production default is the
allowlisted transport (sources.build_transport), never caller-supplied.
"""

import contextlib
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from server.sources import parse_retry_after


class FixtureHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.serve_hit(self)

    def log_message(self, *args):  # quiet
        pass


class FixtureServer:
    """Serves canned (status, headers, body) responses round-robin; the last
    response repeats. `hits` records every request path served."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.hits = []
        self._httpd = None
        self._thread = None

    def start(self):
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
        self._httpd.serve_hit = self._serve_hit
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def _serve_hit(self, handler):
        idx = min(len(self.hits), len(self.responses) - 1)
        self.hits.append(handler.path)
        status, headers, body = self.responses[idx]
        handler.send_response(status)
        for key, value in headers.items():
            handler.send_header(key, value)
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    def url(self):
        return f"http://127.0.0.1:{self._httpd.server_address[1]}"

    def stop(self):
        self._httpd.shutdown()
        self._httpd.server_close()


class FixtureTransport:
    """Test stand-in for sources.FeedTransport: same envelope shape
    (status, body, retry_after), Retry-After parsed by the production helper."""

    def __init__(self, url):
        self.url = url

    def fetch(self, timeout_s):
        import urllib.error

        try:
            resp = urllib.request.urlopen(self.url, timeout=timeout_s)
        except urllib.error.HTTPError as exc:
            with contextlib.closing(exc):
                return exc.code, exc.read(), parse_retry_after(exc.headers)
        with contextlib.closing(resp):
            return resp.status, resp.read(), parse_retry_after(resp.headers)


def fixture_transport_factory(base_url):
    """Transport factory that serves every requested URL from the fixture."""

    def factory(url):
        return FixtureTransport(base_url)

    return factory
