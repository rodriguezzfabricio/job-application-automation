"""P1 RED test — the platform transport allowlist (scar P1, IRON_LAW §13).

A LinkedIn/Indeed URL is non-representable IN THE TRANSPORT: it is refused at
transport construction, not caught after a fetch attempt. A GREEN on this test
is itself a failure (anti-cheat guarantee).
"""

import unittest

from server.sources import SANCTIONED_FEED_HOSTS, AllowlistViolation, build_transport


class PlatformTransportAllowlistBlocksLinkedin(unittest.TestCase):
    def test_allowlist_is_frozen_and_sanctioned_only(self):
        self.assertEqual(
            SANCTIONED_FEED_HOSTS,
            frozenset(
                {"api.ashbyhq.com", "boards-api.greenhouse.io", "api.lever.co"}
            ),
        )

    def test_linkedin_url_refused_at_construction(self):
        with self.assertRaises(AllowlistViolation):
            build_transport(
                "https://www.linkedin.com/jobs/search/?keywords=software%20engineer"
            )

    def test_indeed_url_refused_at_construction(self):
        with self.assertRaises(AllowlistViolation):
            build_transport("https://www.indeed.com/jobs?q=software+engineer")

    def test_plain_linkedin_host_refused(self):
        with self.assertRaises(AllowlistViolation):
            build_transport("https://www.linkedin.com/")

    def test_sanctioned_hosts_construct(self):
        for url in (
            "https://api.ashbyhq.com/posting-api/job-board/example",
            "https://boards-api.greenhouse.io/v1/boards/example/jobs",
            "https://api.lever.co/v0/postings/example",
        ):
            transport = build_transport(url)
            self.assertIsNotNone(transport)

    def test_non_http_scheme_refused(self):
        with self.assertRaises(AllowlistViolation):
            build_transport("ftp://api.ashbyhq.com/whatever")


if __name__ == "__main__":
    unittest.main()
