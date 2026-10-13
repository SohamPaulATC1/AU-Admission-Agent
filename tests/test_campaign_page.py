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


class TestPlainWords(unittest.TestCase):
    """Operator 2026-10-13: the agent has one job (2027 admissions), so people see "Bulk calls" and
    "call list", never "campaign". URLs, APIs and tables keep their names."""

    @staticmethod
    def visible(html):
        # The text a person reads: the markup's text, plus the script's string literals that read as words
        # (a space or a capital first letter: "Call lists", "Stop this list?"), not keys like "campaigns".
        scripts = re.findall(r"<script\b[^>]*>(.*?)</script>", html, re.S)
        literals = [s for s in re.findall(r'"([^"\n]*)"', " ".join(scripts)) if " " in s or s[:1].isupper()]
        markup = re.sub(r"<script\b[^>]*>.*?</script>|<style\b[^>]*>.*?</style>", " ", html, flags=re.S)
        text = re.sub(r"<[^>]+>", " ", markup)
        return re.sub(r"/api/\S*|/campaigns\S*", " ", text + " " + " ".join(literals))

    def test_no_page_says_campaign(self):
        for name in ("campaigns.html", "index.html", "admin.html"):
            self.assertNotRegex(self.visible(read(name)), r"(?i)\bcampaigns?\b", name)

    def test_the_link_and_the_title(self):
        self.assertIn('<a href="/campaigns" class="nav-link">Bulk calls</a>', read("index.html"))
        html = read("campaigns.html")
        self.assertIn("<h1>Bulk calls</h1>", html)
        self.assertIn("Upload a call list", html)

    def test_the_list_name_is_optional_and_filled_from_the_file(self):
        html = read("campaigns.html")
        (field,) = re.findall(r'<input name="campaign_name"[^>]*>', html)
        self.assertNotIn("required", field)
        self.assertIn("name: listName(form.elements.campaign_name.value, file.name)", html)

    def test_the_settings_sit_under_advanced(self):
        html = read("campaigns.html")
        start = html.index('<form id="uploadForm"')
        form = html[start:html.index("</form>", start)]
        advanced = form[form.index('<details class="advanced">'):]
        for name in ("concurrency", "hours_start", "hours_end", "max_retries", "retry_gap_min"):
            self.assertIn(f'name="{name}"', advanced, name)
        self.assertIn('addEventListener("invalid"', html)   # a refused setting opens the section


class TestCallsPageLink(unittest.TestCase):
    def test_calls_page_links_to_campaigns(self):
        self.assertIn('href="/campaigns"', read("index.html"))


if __name__ == "__main__":
    unittest.main()
