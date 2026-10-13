"""admin.html: static checks (docs/spec/admin-panel-design.md §3). No browser here, so the page is checked as text."""

from __future__ import annotations

import os
import re
import unittest

from tests.harness.appctl import app
from tests.test_access_control import AuthTestCase

web_auth = app.web_auth
PAGE_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "admin.html")
with open(PAGE_PATH, encoding="utf-8") as handle:
    PAGE = handle.read()


class TestAdminPage(unittest.TestCase):
    def test_no_value_ever_becomes_markup(self):
        for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
            self.assertNotIn(banned, PAGE, banned)

    def test_every_element_the_script_looks_up_exists(self):
        ids = set(re.findall(r'\bid="([A-Za-z-]+)"', PAGE))
        wanted = set(re.findall(r'\$\("([A-Za-z-]+)"\)', PAGE))
        self.assertEqual(sorted(wanted - ids), [])

    def test_the_five_views_exist(self):
        self.assertIn('const VIEWS = ["overview", "calls", "call", "campaigns", "dnc"];', PAGE)
        for view in ("overview", "calls", "call", "campaigns", "dnc"):
            self.assertIn(f'id="view-{view}"', PAGE)

    def test_every_api_path_it_calls_is_an_admin_only_route(self):
        paths = set(re.findall(r'"(/api/admin/[^"?]*)', PAGE))
        self.assertTrue(paths)
        rules = {rule.rule: rule.endpoint for rule in app.app.url_map.iter_rules()}
        for path in paths:
            matches = [endpoint for rule, endpoint in rules.items()
                       if rule == path or (path.endswith("/") and rule.startswith(path))]
            self.assertTrue(matches, f"no route for {path}")
            for endpoint in matches:
                self.assertEqual(web_auth.ENDPOINT_ACCESS[endpoint], web_auth.ADMIN_API, endpoint)

    def test_the_export_links_point_at_real_csv_routes(self):
        for path in ("/api/admin/calls.csv", "/api/admin/campaigns.csv", "/api/admin/dnc.csv"):
            self.assertIn(path, PAGE)

    def test_the_rerun_poll_waits_past_pending(self):
        # The worker marks the row pending before it calls Haiku; stopping on any change said "Done." too early.
        self.assertIn('a.status !== "pending" && a.analyzed_at !== beforeAt', PAGE)

    def test_the_old_placeholder_is_gone(self):
        self.assertNotIn("will appear here", PAGE)


class TestAdminPageServing(AuthTestCase):
    def test_the_admin_gets_the_page_and_an_operator_does_not(self):
        status, _, body = self.get("admin", "/admin")
        self.assertEqual(status, 200)
        self.assertIn("Do-not-call", body)
        self.assertEqual(self.get("operator", "/admin")[:2], (302, "/"))
        self.assertEqual(self.get(None, "/admin")[:2], (302, "/login"))


if __name__ == "__main__":
    unittest.main()
