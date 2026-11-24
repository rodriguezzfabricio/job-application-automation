"""Liveness predicate tests — a2 Tier 1 (knowledge/a2.md, Liveness check).

Contract under test: feed lists the job active ∧ careers page readable on
official_host ∧ ownership link to the registered board host with matching
board ∧ application page readable with no closed markers ∧ the rendered text
contains the job-title tokens (a generic 200 shell fails the positive check).

RED-first protocol: every batch below was run and observed failing (ImportError
— feature missing) before its production code existed.
"""

import unittest

from server.liveness import (
    CLOSED_MARKERS,
    application_page_readable,
    href_links,
    liveness_checks_pass,
    ownership_link_present,
    scan_closed_markers,
    title_tokens_present,
)


class ScanClosedMarkers(unittest.TestCase):
    def test_finds_casefolded_marker_and_returns_tuple_original(self):
        text = "Thank you for your interest — this position has been POSITION FILLED."
        self.assertEqual(scan_closed_markers(text), ["position filled"])

    def test_no_markers_returns_empty_list(self):
        text = "Senior Backend Engineer — apply today."
        self.assertEqual(scan_closed_markers(text), [])

    def test_custom_markers_parameter_is_honored(self):
        self.assertEqual(
            scan_closed_markers("This role is CLOSED NOW.", markers=("closed now",)),
            ["closed now"],
        )


class TitleTokensPresent(unittest.TestCase):
    def test_every_title_token_present_returns_true(self):
        self.assertTrue(
            title_tokens_present(
                "the senior backend engineer will own the API",
                "Senior Backend Engineer",
            )
        )

    def test_one_missing_token_in_generic_shell_returns_false(self):
        shell = "<html><body><h1>We're sorry — this page has moved.</h1></body></html>"
        self.assertFalse(title_tokens_present(shell, "Senior Backend Engineer"))

    def test_empty_title_returns_false(self):
        self.assertFalse(title_tokens_present("anything here", ""))

    def test_whitespace_only_title_returns_false(self):
        self.assertFalse(title_tokens_present("anything here", "   "))

    def test_punctuation_only_token_is_dropped(self):
        # "Engineer!" normalizes to "Engineer"; the rendered text only has "engineer".
        self.assertTrue(
            title_tokens_present("we need a senior engineer here", "Engineer!")
        )

    def test_casefolded_match(self):
        self.assertTrue(
            title_tokens_present("SENIOR BACKEND ENGINEER", "Senior Backend Engineer")
        )

    def test_substring_matching_is_deliberate(self):
        # a2 says the text must "contain the job-title tokens"; substring is the
        # conservative reading of that contract, so "backend" ⊂ "backendend" passes.
        self.assertTrue(title_tokens_present("the backendend framework", "backend"))


class HrefLinks(unittest.TestCase):
    def test_extracts_links_from_messy_html_in_document_order(self):
        html = (
            '<a HREF="https://boards.greenhouse.io/embed/job_app">first</a>'
            "<link href='https://example.com/css'>css</link>"
            '<img src="https://example.com/pic.png"'
            ' href="https://boards.greenhouse.io/other_board">'
        )
        self.assertEqual(
            href_links(html),
            [
                "https://boards.greenhouse.io/embed/job_app",
                "https://example.com/css",
                "https://boards.greenhouse.io/other_board",
            ],
        )

    def test_no_links_returns_empty_list(self):
        self.assertEqual(href_links("<p>plain text</p>"), [])


class OwnershipLinkPresent(unittest.TestCase):
    def test_host_and_board_token_match_returns_true(self):
        links = ["https://boards.greenhouse.io/embed/job_app?for=acme"]
        self.assertTrue(
            ownership_link_present(links, "boards.greenhouse.io", "job_app")
        )

    def test_wrong_board_token_on_right_host_returns_false(self):
        # Careers page on the registered host, but a DIFFERENT board → not ownership.
        links = ["https://boards.greenhouse.io/other_board"]
        self.assertFalse(
            ownership_link_present(links, "boards.greenhouse.io", "job_app")
        )

    def test_right_board_token_on_wrong_host_returns_false(self):
        links = ["https://jobs.lever.co/job_app"]
        self.assertFalse(
            ownership_link_present(links, "boards.greenhouse.io", "job_app")
        )

    def test_invalid_links_are_skipped_not_crashed(self):
        links = [
            "http://[bad",  # urlparse raises ValueError on this malformed host
            "not a url",  # parses to no hostname
            "https://boards.greenhouse.io/embed/job_app",
        ]
        self.assertTrue(
            ownership_link_present(links, "boards.greenhouse.io", "job_app")
        )


class ApplicationPageReadable(unittest.TestCase):
    def test_nonempty_marker_free_text_returns_true(self):
        self.assertTrue(application_page_readable("<html>Apply now</html>"))

    def test_empty_text_returns_false(self):
        self.assertFalse(application_page_readable(""))

    def test_closed_marker_present_returns_false(self):
        self.assertFalse(application_page_readable("This posting has closed."))


class LivenessChecksPass(unittest.TestCase):
    def test_true_only_when_all_three_checks_hold(self):
        text = "Senior Backend Engineer — apply today."
        links = ["https://boards.greenhouse.io/embed/job_app"]
        host, board_token = "boards.greenhouse.io", "job_app"
        title = "Senior Backend Engineer"
        self.assertTrue(
            liveness_checks_pass(text, links, host, board_token, title)
        )
        # Flip each check in turn: title tokens missing, then ownership, then markers.
        self.assertFalse(
            liveness_checks_pass(
                "DevOps Engineer — apply today.", links, host, board_token, title
            )
        )
        self.assertFalse(
            liveness_checks_pass(text, links, "other.example.com", board_token, title)
        )
        self.assertFalse(
            liveness_checks_pass(
                "Senior Backend Engineer — This posting has closed.",
                links,
                host,
                board_token,
                title,
            )
        )
