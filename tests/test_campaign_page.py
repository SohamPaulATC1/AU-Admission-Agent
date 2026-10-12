"""campaigns.html and the calls-page link (docs/spec/campaigns-design.md §4)."""

from __future__ import annotations

import os
import re
import unittest

from tests.harness.appctl import APP_PY
from tests.test_access_control import AuthTestCase

ROOT = os.path.dirname(os.path.abspath(APP_PY))


def read(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as f:
        return f.read()


class TestCampaignPage(AuthTestCase):
    def test_page_is_served_to_both_roles(self):
        for role in ("operator", "admin"):
            status, _, body = self.get(role, "/campaigns")
            self.assertEqual(status, 200, role)
            for element in ('id="uploadForm"', 'id="campaignRows"', 'id="detail"', 'id="contactRows"'):
                self.assertIn(element, body)


class TestCampaignPageSource(unittest.TestCase):
    def setUp(self):
        self.html = read("campaigns.html")

    def test_page_never_builds_html_from_data(self):
        # Names and extra columns come from an uploaded Excel: text only, never markup.
        for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
            self.assertNotIn(sink, self.html)

    def test_page_shows_no_cost(self):
        self.assertNotIn("cost", self.html.lower())

    def test_page_uses_the_api_polls_and_goes_to_login(self):
        self.assertIn("/api/campaigns", self.html)
        self.assertIn("POLL_MS = 5000", self.html)
        self.assertIn('window.location.href = "/login"', self.html)

    def test_csv_export_defuses_formulas(self):
        self.assertRegex(self.html, re.escape("/^[=+\\-@\\t\\r]/"))   # leading tab / CR count too

    def test_stop_asks_first(self):
        self.assertIn("confirm(", self.html)


class TestCallsPageLink(unittest.TestCase):
    def test_calls_page_links_to_campaigns(self):
        self.assertIn('href="/campaigns"', read("index.html"))


if __name__ == "__main__":
    unittest.main()
