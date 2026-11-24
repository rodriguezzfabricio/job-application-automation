"""Employer transport + Tier-1 liveness tier tests (a1/a2, scar P1).

The employer transport is the P1 containment pin for the liveness fetch: it is
refused at construction for any URL that is not an HTTPS URL with a usable
host, fetches are GET-only and body-bounded, redirects can never widen the
transport to a new host, and liveness_tier composes the a2 Tier-1 checks over
the injected transport (the DI seam, so the tier runs against local fixtures).

RED-first protocol: each batch below was run and observed failing before its
production code existed (failures recorded per batch).
"""

import unittest
import urllib.error

from tests.fixtures import FixtureServer

from server.liveness_transport import (
    RESPONSE_BODY_CAP,
    EmployerRedirectHandler,
    EmployerTransport,
    EmployerTransportError,
    build_employer_transport,
    liveness_tier,
)


class EmployerTransportConstruction(unittest.TestCase):
    """build_employer_transport is the HTTPS-only gate: refusal happens at
    construction, before any socket opens (a1: two disjoint transports)."""

    def test_employer_transport_error_is_valueerror_subclass(self):
        self.assertTrue(issubclass(EmployerTransportError, ValueError))

    def test_https_careers_url_accepted(self):
        transport = build_employer_transport("https://www.notion.so/careers")
        self.assertIsInstance(transport, EmployerTransport)

    def test_http_careers_url_refused(self):
        with self.assertRaises(EmployerTransportError):
            build_employer_transport("http://www.notion.so/careers")

    def test_non_http_scheme_refused(self):
        with self.assertRaises(EmployerTransportError):
            build_employer_transport("ftp://www.notion.so/careers")

    def test_missing_host_refused(self):
        with self.assertRaises(EmployerTransportError):
            build_employer_transport("https:///careers")

    def test_empty_host_refused(self):
        with self.assertRaises(EmployerTransportError):
            build_employer_transport("https://")

    def test_whitespace_host_refused(self):
        with self.assertRaises(EmployerTransportError):
            build_employer_transport("https://exa mple.com/careers")

    def test_invalid_ipv6_host_refused(self):
        with self.assertRaises(EmployerTransportError):
            build_employer_transport("https://[bad")

    def test_invalid_port_refused(self):
        with self.assertRaises(EmployerTransportError):
            build_employer_transport("https://www.notion.so:notaport/careers")

    def test_feed_api_host_is_not_special_cased(self):
        # A careers_url on api.ashbyhq.com is still just an employer-page URL;
        # a1 forbids adding a special block beyond the https+host rules.
        transport = build_employer_transport(
            "https://api.ashbyhq.com/posting-api/job-board/notion"
        )
        self.assertIsInstance(transport, EmployerTransport)


class EmployerTransportFetch(unittest.TestCase):
    """fetch() over real sockets: (status, decoded text), UTF-8 with
    errors="replace", and the body read capped at RESPONSE_BODY_CAP — the
    buffer is never allowed to grow unbounded, even on HTTP errors."""

    def test_fetch_returns_status_and_decoded_text(self):
        server = FixtureServer([(200, {}, b"<html>we are hiring</html>")])
        server.start()
        self.addCleanup(server.stop)
        transport = EmployerTransport(server.url())
        self.assertEqual(transport.fetch(5), (200, "<html>we are hiring</html>"))
        self.assertEqual(server.hits, ["/"])

    def test_fetch_caps_body_at_256_kib(self):
        server = FixtureServer([(200, {}, b"x" * (RESPONSE_BODY_CAP + 4096))])
        server.start()
        self.addCleanup(server.stop)
        transport = EmployerTransport(server.url())
        status, text = transport.fetch(5)
        self.assertEqual(status, 200)
        self.assertEqual(len(text), RESPONSE_BODY_CAP)

    def test_http_error_returns_code_and_bounded_body(self):
        server = FixtureServer([(404, {}, b"z" * (RESPONSE_BODY_CAP + 64))])
        server.start()
        self.addCleanup(server.stop)
        transport = EmployerTransport(server.url())
        status, text = transport.fetch(5)
        self.assertEqual(status, 404)
        self.assertEqual(len(text), RESPONSE_BODY_CAP)

    def test_utf8_decode_errors_are_replaced(self):
        # b"\xc3\xa9" is é in UTF-8; b"\xff" is invalid and must become U+FFFD.
        server = FixtureServer([(200, {}, b"caf\xc3\xa9 \xff")])
        server.start()
        self.addCleanup(server.stop)
        transport = EmployerTransport(server.url())
        status, text = transport.fetch(5)
        self.assertEqual(status, 200)
        self.assertEqual(text, "caf\u00e9 \ufffd")


class EmployerTransportRedirectContainment(unittest.TestCase):
    """P1: a redirect must never widen the transport. The registered careers
    endpoint is (hostname, effective port); a redirect to any other endpoint
    raises EmployerTransportError before the redirected request opens."""

    def test_cross_endpoint_redirect_refused(self):
        server_b = FixtureServer([(200, {}, b"target body")])
        server_b.start()
        self.addCleanup(server_b.stop)
        server_a = FixtureServer([(302, {"Location": server_b.url() + "/target"}, b"")])
        server_a.start()
        self.addCleanup(server_a.stop)
        transport = EmployerTransport(server_a.url())
        with self.assertRaises(EmployerTransportError):
            transport.fetch(5)
        self.assertEqual(server_b.hits, [])  # never opened: zero sockets on B

    def test_same_endpoint_redirect_followed(self):
        server = FixtureServer(
            [
                (302, {"Location": "/final"}, b""),
                (200, {}, b"final body"),
            ]
        )
        server.start()
        self.addCleanup(server.stop)
        transport = EmployerTransport(server.url())
        self.assertEqual(transport.fetch(5), (200, "final body"))
        self.assertEqual(server.hits, ["/", "/final"])

    def test_redirect_handler_installed_with_careers_endpoint(self):
        # The pin must actually be installed on the transport, not merely
        # defined somewhere unused.
        transport = build_employer_transport("https://www.notion.so/careers")
        handlers = [
            h
            for h in transport._opener.handlers
            if isinstance(h, EmployerRedirectHandler)
        ]
        self.assertEqual(len(handlers), 1)
        self.assertEqual(handlers[0]._endpoint, ("www.notion.so", 443))


NOW = 1750000000


def careers_html(base_url, board="notion"):
    """Careers page whose title tokens (staff/software/engineer) are all
    present and whose ownership link targets the careers host itself with the
    board token in the path — the a2 Tier-1 all-pass fixture."""
    return (
        "<html><body>"
        "<h1>Staff Software Engineer</h1>"
        "<p>We are hiring a staff software engineer to build the future.</p>"
        f"<a href='{base_url}/posting-api/job-board/{board}'>Open roles</a>"
        "</body></html>"
    )


class _RaisingTransport:
    def fetch(self, timeout_s):
        raise urllib.error.URLError("connection refused (fixture)")


class _RecordingTransport:
    def __init__(self):
        self.calls = []

    def fetch(self, timeout_s):
        self.calls.append(timeout_s)
        raise AssertionError(
            "liveness_tier must not fetch when the careers_url is missing/empty"
        )


class LivenessTier(unittest.TestCase):
    """a2 Tier-1 liveness via the INJECTED transport (DI seam). The tier runs
    against the real EmployerTransport pointed at local fixtures; fail-closed
    paths inject stub transports."""

    def _run(self, record, source_row, transport):
        return liveness_tier(record, source_row, transport, NOW)

    def _ok_record(self):
        return {"title": "Staff Software Engineer"}

    def _ok_source_row(self, url):
        # No "host" key on purpose: the tier derives official_host from the
        # careers_url itself and must not depend on the feed host.
        return {"id": "ashby:notion", "careers_url": url}

    def _careers_server(self, html_factory):
        # The HTML embeds the server's own URL, whose port only exists after
        # start() — so responses[0] is filled in once the port is known.
        server = FixtureServer([(200, {}, b"")])
        server.start()
        self.addCleanup(server.stop)
        server.responses[0] = (200, {}, html_factory(server.url()).encode())
        return server

    def test_all_passing_fixture_returns_liveness_ok_true(self):
        server = self._careers_server(careers_html)
        result = self._run(
            self._ok_record(),
            self._ok_source_row(server.url()),
            EmployerTransport(server.url()),
        )
        self.assertTrue(result["liveness_ok"])
        self.assertEqual(result["evidence_level"], "reduced")
        self.assertTrue(all(result["checks"].values()))
        self.assertEqual(result["observed_at"], NOW)

    def test_return_shape_exact_match(self):
        server = self._careers_server(careers_html)
        result = self._run(
            self._ok_record(),
            self._ok_source_row(server.url()),
            EmployerTransport(server.url()),
        )
        self.assertEqual(
            result,
            {
                "liveness_ok": True,
                "evidence_level": "reduced",
                "checks": {
                    "careers_page_readable": True,
                    "ownership_link": True,
                    "application_page_readable": True,
                    "title_tokens": True,
                },
                "observed_at": NOW,
            },
        )

    def test_title_token_mismatch_fails(self):
        server = self._careers_server(careers_html)
        result = self._run(
            {"title": "DevOps Engineer"},
            self._ok_source_row(server.url()),
            EmployerTransport(server.url()),
        )
        self.assertFalse(result["liveness_ok"])
        self.assertTrue(result["checks"]["careers_page_readable"])
        self.assertTrue(result["checks"]["ownership_link"])
        self.assertTrue(result["checks"]["application_page_readable"])
        self.assertFalse(result["checks"]["title_tokens"])

    def test_ownership_link_on_feed_host_does_not_count(self):
        # The parent correction pin: official_host is the CAREERS URL's host,
        # so a link to the feed API host (api.ashbyhq.com) is not ownership.
        html = (
            "<html><body><h1>Staff Software Engineer</h1>"
            "<p>We are hiring a staff software engineer.</p>"
            "<a href='https://api.ashbyhq.com/posting-api/job-board/notion'>"
            "feed</a></body></html>"
        )
        server = self._careers_server(lambda url: html)
        result = self._run(
            self._ok_record(),
            self._ok_source_row(server.url()),
            EmployerTransport(server.url()),
        )
        self.assertFalse(result["liveness_ok"])
        self.assertTrue(result["checks"]["careers_page_readable"])
        self.assertFalse(result["checks"]["ownership_link"])
        self.assertTrue(result["checks"]["title_tokens"])

    def test_ownership_link_wrong_board_token_fails(self):
        server = self._careers_server(lambda url: careers_html(url, board="other"))
        result = self._run(
            self._ok_record(),
            self._ok_source_row(server.url()),
            EmployerTransport(server.url()),
        )
        self.assertFalse(result["liveness_ok"])
        self.assertTrue(result["checks"]["careers_page_readable"])
        self.assertFalse(result["checks"]["ownership_link"])
        self.assertTrue(result["checks"]["title_tokens"])

    def test_transport_exception_fails_closed(self):
        result = self._run(
            self._ok_record(),
            self._ok_source_row("https://x.example/c"),
            _RaisingTransport(),
        )
        self.assertEqual(
            result,
            {
                "liveness_ok": False,
                "evidence_level": "reduced",
                "checks": {
                    "careers_page_readable": False,
                    "ownership_link": False,
                    "application_page_readable": False,
                    "title_tokens": False,
                },
                "observed_at": NOW,
            },
        )

    def test_non_2xx_fails_closed(self):
        server = FixtureServer([(503, {}, b"down for maintenance")])
        server.start()
        self.addCleanup(server.stop)
        result = self._run(
            self._ok_record(),
            self._ok_source_row(server.url()),
            EmployerTransport(server.url()),
        )
        self.assertFalse(result["liveness_ok"])
        self.assertTrue(all(not v for v in result["checks"].values()))

    def test_200_empty_body_fails_closed(self):
        # "2xx AND non-empty": an empty 200 is not a readable careers page.
        server = FixtureServer([(200, {}, b"")])
        server.start()
        self.addCleanup(server.stop)
        result = self._run(
            self._ok_record(),
            self._ok_source_row(server.url()),
            EmployerTransport(server.url()),
        )
        self.assertFalse(result["liveness_ok"])
        self.assertTrue(all(not v for v in result["checks"].values()))

    def test_empty_careers_url_fails_closed_without_fetch(self):
        transport = _RecordingTransport()
        result = self._run(
            self._ok_record(), {"id": "ashby:notion", "careers_url": ""}, transport
        )
        self.assertFalse(result["liveness_ok"])
        self.assertTrue(all(not v for v in result["checks"].values()))
        self.assertEqual(transport.calls, [])

    def test_missing_careers_url_fails_closed_without_fetch(self):
        transport = _RecordingTransport()
        result = self._run(self._ok_record(), {"id": "ashby:notion"}, transport)
        self.assertFalse(result["liveness_ok"])
        self.assertTrue(all(not v for v in result["checks"].values()))
        self.assertEqual(transport.calls, [])

    def test_malformed_careers_url_fails_closed_without_fetch(self):
        transport = _RecordingTransport()
        result = self._run(
            self._ok_record(),
            {"id": "ashby:notion", "careers_url": "https://[bad"},
            transport,
        )
        self.assertFalse(result["liveness_ok"])
        self.assertTrue(all(not v for v in result["checks"].values()))
        self.assertEqual(transport.calls, [])


if __name__ == "__main__":
    unittest.main()
