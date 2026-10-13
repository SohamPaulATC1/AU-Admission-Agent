"""admin_data.py: the range, the overview and its costs (docs/spec/admin-panel-design.md §1)."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from datetime import date

import admin_data
import call_store
import campaigns
from tests.test_call_store import CALLS_TABLE

TODAY = date(2026, 10, 13)


def new_db():
    conn = sqlite3.connect(":memory:")
    conn.execute(CALLS_TABLE)
    call_store.upgrade(conn)
    campaigns.upgrade(conn)
    admin_data.upgrade(conn)
    return conn


def add_call(conn, call_uuid, created="2026-10-12 10:00:00", *, name="Asha Das", phone="9876543210",
             connected=True, gemini=None, stt=None, nlp=None, plivo=None, plivo_status=None, cost_text=None,
             transcript_path=None):
    conn.execute(
        "INSERT INTO calls (call_uuid, name, phone, status, created_at, updated_at, connected_at, cost, "
        "gemini_cost_usd, stt_cost_usd, nlp_cost_usd, plivo_cost_usd, plivo_cost_status, transcript_path) "
        "VALUES (?, ?, ?, 'ended', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (call_uuid, name, phone, created, created, created if connected else None, cost_text, gemini, stt, nlp,
         plivo, plivo_status, transcript_path))


def add_analysis(conn, call_uuid, status="ok", **fields):
    call_store._upsert(conn, call_uuid, {"status": status, **fields})


class DbCase(unittest.TestCase):
    def setUp(self):
        self.conn = new_db()
        self.addCleanup(self.conn.close)


class TestParseRange(unittest.TestCase):
    def test_no_dates_is_the_last_seven_days(self):
        self.assertEqual(admin_data.parse_range(None, None, False, TODAY), ("2026-10-07", "2026-10-13"))
        self.assertEqual(admin_data.parse_range("", "", False, TODAY), ("2026-10-07", "2026-10-13"))

    def test_all_time_has_no_bounds(self):
        self.assertEqual(admin_data.parse_range(None, None, True, TODAY), (None, None))

    def test_custom_range(self):
        self.assertEqual(admin_data.parse_range("2026-10-01", "2026-10-13", False, TODAY),
                         ("2026-10-01", "2026-10-13"))
        self.assertEqual(admin_data.parse_range("2026-10-05", "2026-10-05", False, TODAY),
                         ("2026-10-05", "2026-10-05"))

    def test_bad_dates_are_refused(self):
        for start, end in (("2026-13-01", "2026-13-02"), ("20261013", "20261013"), ("yesterday", "today"),
                           ("2026-10-01", None), (None, "2026-10-01"), ("2026-10-1", "2026-10-13"),
                           ("2026-10-01 ", "2026-10-13")):
            with self.assertRaises(admin_data.AdminError, msg=(start, end)):
                admin_data.parse_range(start, end, False, TODAY)

    def test_from_after_to_is_refused(self):
        with self.assertRaises(admin_data.AdminError):
            admin_data.parse_range("2026-10-13", "2026-10-01", False, TODAY)

    def test_the_last_possible_date_is_refused_not_a_crash(self):
        with self.assertRaises(admin_data.AdminError):
            admin_data.parse_range("2026-10-01", "9999-12-31", False, TODAY)


class TestRangeEdges(DbCase):
    def test_whole_days_are_included_at_both_ends(self):
        add_call(self.conn, "before", "2026-10-06 23:59:59")
        add_call(self.conn, "first", "2026-10-07 00:00:00")
        add_call(self.conn, "last", "2026-10-13 23:59:59")
        add_call(self.conn, "after", "2026-10-14 00:00:00")
        self.assertEqual(admin_data.overview(self.conn, "2026-10-07", "2026-10-13")["calls"], 2)

    def test_all_time_counts_everything(self):
        add_call(self.conn, "old", "2020-01-01 00:00:00")
        add_call(self.conn, "new", "2026-10-13 09:00:00")
        self.assertEqual(admin_data.overview(self.conn, None, None)["calls"], 2)


class TestOverview(DbCase):
    def test_an_empty_database(self):
        result = admin_data.overview(self.conn, None, None)
        self.assertEqual((result["calls"], result["connected"], result["hot"]), (0, 0, 0))
        self.assertEqual(result["cost"]["total"], 0)
        self.assertIsNone(result["cost_per_call"])
        self.assertIsNone(result["cost_per_hot_lead"])

    def test_counts(self):
        add_call(self.conn, "a", gemini=0.1, stt=0.01, nlp=0.002, plivo=0.02)
        add_call(self.conn, "b", gemini=0.2, stt=0.02, nlp=0.002, plivo=0.02)
        add_call(self.conn, "c", connected=False)
        add_call(self.conn, "d", gemini=0.05)
        add_analysis(self.conn, "a", lead_status="hot", campus_visit_agreed=1)
        add_analysis(self.conn, "b", lead_status="warm", campus_visit_agreed=0)
        add_analysis(self.conn, "d", lead_status="hot", campus_visit_agreed=0)
        result = admin_data.overview(self.conn, None, None)
        self.assertEqual((result["calls"], result["connected"]), (4, 3))
        self.assertEqual((result["hot"], result["warm"], result["visits"]), (2, 1, 1))

    def test_cost_parts_total_and_ratios(self):
        add_call(self.conn, "a", gemini=0.1, stt=0.01, nlp=0.002, plivo=0.02)
        add_call(self.conn, "b", gemini=0.2, stt=0.02, nlp=0.002, plivo=0.02)
        add_analysis(self.conn, "a", lead_status="hot")
        cost = admin_data.overview(self.conn, None, None)
        self.assertAlmostEqual(cost["cost"]["gemini"], 0.3)
        self.assertAlmostEqual(cost["cost"]["stt"], 0.03)
        self.assertAlmostEqual(cost["cost"]["haiku"], 0.004)
        self.assertAlmostEqual(cost["cost"]["plivo"], 0.04)
        self.assertAlmostEqual(cost["cost"]["total"], 0.374)
        self.assertAlmostEqual(cost["cost_per_call"], 0.187)
        self.assertAlmostEqual(cost["cost_per_hot_lead"], 0.374)

    def test_earlier_calls_use_the_cost_text_and_stay_out_of_the_four_parts(self):
        add_call(self.conn, "old", cost_text="$0.1404")
        add_call(self.conn, "new", gemini=0.1)
        cost = admin_data.overview(self.conn, None, None)["cost"]
        self.assertAlmostEqual(cost["earlier"], 0.1404)
        self.assertAlmostEqual(cost["gemini"], 0.1)
        self.assertAlmostEqual(cost["total"], 0.2404)

    def test_an_earlier_call_that_later_got_analysis_and_plivo_cost_counts_all_of_it(self):
        add_call(self.conn, "old", cost_text="$0.1404", nlp=0.0013, plivo=0.0166)
        cost = admin_data.overview(self.conn, None, None)["cost"]
        self.assertAlmostEqual(cost["earlier"], 0.1404)
        self.assertAlmostEqual(cost["haiku"], 0.0013)
        self.assertAlmostEqual(cost["plivo"], 0.0166)
        self.assertAlmostEqual(cost["total"], 0.1583)

    def test_a_step_2_call_counts_its_gemini_cost_once(self):
        # add_leg writes gemini_cost_usd and the cost text together: the text must not count again.
        add_call(self.conn, "new", gemini=0.1, cost_text="$0.1000")
        result = admin_data.overview(self.conn, None, None)
        self.assertAlmostEqual(result["cost"]["total"], 0.1)
        self.assertEqual(result["cost"]["earlier"], 0)
        self.assertIsNone(result["cost_per_hot_lead"])
        self.assertAlmostEqual(result["cost_per_call"], 0.1)

    def test_a_range_with_no_calls(self):
        add_call(self.conn, "a", "2026-10-12 10:00:00", gemini=0.1)
        result = admin_data.overview(self.conn, "2026-09-01", "2026-09-30")
        self.assertEqual((result["calls"], result["cost"]["total"], result["cost_per_call"]), (0, 0, None))

    def test_an_odd_cost_text_counts_as_zero(self):
        add_call(self.conn, "x", cost_text="n/a")
        add_call(self.conn, "y", cost_text="")
        add_call(self.conn, "z")
        self.assertEqual(admin_data.overview(self.conn, None, None)["cost"]["total"], 0)

    def test_notes_count_pending_and_failed_analyses_and_missing_plivo_costs(self):
        add_call(self.conn, "a", gemini=0.1, plivo_status="ok", plivo=0.01)
        add_call(self.conn, "b", gemini=0.1, plivo_status="pending")
        add_call(self.conn, "c", gemini=0.1)
        add_call(self.conn, "d", connected=False)
        add_analysis(self.conn, "a", "pending")
        add_analysis(self.conn, "b", "failed")
        add_analysis(self.conn, "c", "failed")
        notes = admin_data.overview(self.conn, None, None)["notes"]
        self.assertEqual(notes, {"analysis_pending": 1, "analysis_failed": 2, "plivo_pending": 2})


class TestUpgrade(DbCase):
    def test_the_index_exists_and_upgrade_is_repeatable(self):
        admin_data.upgrade(self.conn)
        names = {r[1] for r in self.conn.execute("PRAGMA index_list(calls)")}
        self.assertIn("calls_created_at", names)


def add_campaign_row(conn, name="October list", created="2026-10-12 09:00:00", contacts=()):
    """A campaign with (phone, call_uuid, dialed_at) contacts, written straight into the tables."""
    campaign_id = conn.execute("INSERT INTO campaigns (name, state, created_at) VALUES (?, 'done', ?)",
                               (name, created)).lastrowid
    for phone, call_uuid, dialed_at in contacts:
        conn.execute("INSERT INTO campaign_contacts (campaign_id, phone, status, call_uuid, dialed_at) "
                     "VALUES (?, ?, 'done', ?, ?)", (campaign_id, phone, call_uuid, dialed_at))
    return campaign_id


NO_FILTERS = admin_data.check_filters({})


def filters(**given):
    return admin_data.check_filters(given)


class TestCheckFilters(unittest.TestCase):
    def test_nothing_given_is_all_none(self):
        self.assertEqual(NO_FILTERS, {"lead": None, "analysis": None, "visit": None, "program": None,
                                      "school": None, "campaign": None, "q": None})

    def test_good_values_pass(self):
        got = filters(lead="hot", analysis="failed", visit="yes", program="B.Tech CSE", school="SoET",
                      campaign="12", q=" asha ")
        self.assertEqual((got["lead"], got["analysis"], got["visit"], got["campaign"], got["q"]),
                         ("hot", "failed", "yes", 12, "asha"))

    def test_values_outside_the_lists_are_refused(self):
        for bad in ({"lead": "boiling"}, {"analysis": "maybe"}, {"visit": "perhaps"}, {"campaign": "x"},
                    {"campaign": "-1"}, {"campaign": "1" * 10}, {"campaign": "²"}, {"q": "x" * 101},
                    {"program": "x" * 201}):
            with self.assertRaises(admin_data.AdminError, msg=bad):
                admin_data.check_filters(bad)


class TestListCalls(DbCase):
    def rows(self, **given):
        return admin_data.list_calls(self.conn, None, None, filters(**given), 1)

    def test_newest_first_in_pages_of_fifty(self):
        for i in range(120):
            add_call(self.conn, f"u{i:03d}", f"2026-10-12 10:{i // 60:02d}:{i % 60:02d}")
        first = admin_data.list_calls(self.conn, None, None, NO_FILTERS, 1)
        third = admin_data.list_calls(self.conn, None, None, NO_FILTERS, 3)
        self.assertEqual((first["total"], len(first["rows"]), len(third["rows"])), (120, 50, 20))
        self.assertEqual(first["rows"][0]["call_uuid"], "u119")
        self.assertEqual(third["rows"][-1]["call_uuid"], "u000")
        self.assertEqual(first["page_size"], 50)

    def test_a_row_carries_the_table_columns_and_the_call_cost(self):
        add_call(self.conn, "a", gemini=0.1, stt=0.01, nlp=0.002, plivo=0.02)
        add_analysis(self.conn, "a", lead_status="hot", primary_program="B.Tech CSE", school_name="SoET",
                     campus_visit_day="Saturday", interest_score=8)
        row = self.rows()["rows"][0]
        self.assertEqual(tuple(row), admin_data.CALL_ROW_KEYS)
        self.assertEqual((row["lead_status"], row["primary_program"], row["school_name"], row["campus_visit_day"],
                          row["interest_score"], row["analysis_status"]),
                         ("hot", "B.Tech CSE", "SoET", "Saturday", 8, "ok"))
        self.assertAlmostEqual(row["cost_usd"], 0.132)

    def test_a_call_without_analysis_still_lists(self):
        add_call(self.conn, "a")
        row = self.rows()["rows"][0]
        self.assertIsNone(row["lead_status"])
        self.assertIsNone(row["analysis_status"])

    def test_the_range_applies(self):
        add_call(self.conn, "in", "2026-10-12 10:00:00")
        add_call(self.conn, "out", "2026-09-01 10:00:00")
        got = admin_data.list_calls(self.conn, "2026-10-07", "2026-10-13", NO_FILTERS, 1)
        self.assertEqual([r["call_uuid"] for r in got["rows"]], ["in"])

    def test_each_filter(self):
        add_call(self.conn, "a", name="Asha", phone="9876543210")
        add_call(self.conn, "b", name="Bimal", phone="9000000002")
        add_call(self.conn, "c", name="Chitra", phone="9000000003")
        add_analysis(self.conn, "a", lead_status="hot", campus_visit_agreed=1, primary_program="CSE",
                     school_name="SoET")
        add_analysis(self.conn, "b", "failed", lead_status="cold", campus_visit_agreed=0, primary_program="BBA",
                     school_name="SoM")
        campaign_id = add_campaign_row(self.conn, contacts=[("9000000003", "c", "2026-10-12 10:00:00")])

        def ids(**given):
            return sorted(r["call_uuid"] for r in self.rows(**given)["rows"])

        self.assertEqual(ids(lead="hot"), ["a"])
        self.assertEqual(ids(analysis="failed"), ["b"])
        self.assertEqual(ids(visit="yes"), ["a"])
        self.assertEqual(ids(visit="no"), ["b", "c"])
        self.assertEqual(ids(program="BBA"), ["b"])
        self.assertEqual(ids(school="SoET"), ["a"])
        self.assertEqual(ids(campaign=str(campaign_id)), ["c"])
        self.assertEqual(ids(q="asha"), ["a"])
        self.assertEqual(ids(q="0000003"), ["c"])
        self.assertEqual(ids(lead="hot", school="SoM"), [])

    def test_search_wildcards_are_literal(self):
        add_call(self.conn, "a", name="Asha")
        add_call(self.conn, "b", name="50% off")
        add_call(self.conn, "c", name="a_b")
        add_call(self.conn, "d", name="Wow!")

        def ids(q):
            return sorted(r["call_uuid"] for r in self.rows(q=q)["rows"])

        self.assertEqual(ids("%"), ["b"])
        self.assertEqual(ids("_"), ["c"])
        self.assertEqual(ids("!"), ["d"])
        self.assertEqual(ids("\\"), [])

    def test_bengali_names_are_searchable(self):
        add_call(self.conn, "a", name="আশা দাস")
        self.assertEqual([r["call_uuid"] for r in self.rows(q="দাস")["rows"]], ["a"])

    def test_dropdown_values_and_the_failed_total_ignore_the_range_and_filters(self):
        add_call(self.conn, "a", "2020-01-01 10:00:00")
        add_analysis(self.conn, "a", "failed", primary_program="CSE", school_name="SoET")
        add_call(self.conn, "b")
        add_analysis(self.conn, "b", primary_program="BBA", school_name="SoM")
        add_campaign_row(self.conn, name="October list")
        got = admin_data.list_calls(self.conn, "2026-10-07", "2026-10-13", filters(lead="hot"), 1)
        self.assertEqual((got["programs"], got["schools"]), (["BBA", "CSE"], ["SoET", "SoM"]))
        self.assertEqual([c["name"] for c in got["campaigns"]], ["October list"])
        self.assertEqual(got["failed_total"], 1)

    def test_export_returns_one_row_past_the_cap(self):
        for i in range(5):
            add_call(self.conn, f"u{i}")
        original = admin_data.CSV_MAX_ROWS
        admin_data.CSV_MAX_ROWS = 3
        self.addCleanup(setattr, admin_data, "CSV_MAX_ROWS", original)
        self.assertEqual(len(admin_data.export_calls(self.conn, None, None, NO_FILTERS)), 4)


class TestCallDetail(DbCase):
    def setUp(self):
        super().setUp()
        self.folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.folder, ignore_errors=True)

    def detail(self, call_uuid="a"):
        return admin_data.call_detail(self.conn, call_uuid, self.folder)

    def test_an_unknown_call_is_none(self):
        self.assertIsNone(self.detail("nope"))
        self.assertFalse(admin_data.call_exists(self.conn, "nope"))

    def test_costs_lists_and_analysis_are_decoded(self):
        add_call(self.conn, "a", gemini=0.1, stt=0.01, nlp=0.002, plivo=0.02, plivo_status="ok")
        self.conn.execute("UPDATE calls SET programs_looked_up = ?, plivo_b_legs = ? WHERE call_uuid = 'a'",
                          (json.dumps(["B.Tech CSE"]), json.dumps(["leg-1"])))
        add_analysis(self.conn, "a", lead_status="hot", objections=json.dumps(["fee"]),
                     campus_visit_agreed=1, scholarship_discussed=0, summary="Visit fixed.", raw_json="{}")
        got = self.detail()
        self.assertTrue(admin_data.call_exists(self.conn, "a"))
        self.assertEqual(got["cost"], {"gemini": 0.1, "stt": 0.01, "haiku": 0.002, "plivo": 0.02, "earlier": None,
                                       "total": 0.132, "plivo_status": "ok"})
        self.assertEqual(got["call"]["programs_looked_up"], ["B.Tech CSE"])
        self.assertEqual(got["call"]["plivo_b_legs"], ["leg-1"])
        self.assertEqual(got["analysis"]["objections"], ["fee"])
        self.assertEqual(got["analysis"]["subjects"], [])
        self.assertIs(got["analysis"]["campus_visit_agreed"], True)
        self.assertIs(got["analysis"]["scholarship_discussed"], False)
        self.assertIsNone(got["analysis"]["callback_requested"])
        self.assertNotIn("raw_json", got["analysis"])
        self.assertNotIn("transcript_path", got["call"])

    def test_an_unanswered_new_call_has_no_earlier_cost(self):
        add_call(self.conn, "missed", connected=False)
        cost = admin_data.call_detail(self.conn, "missed", self.folder if hasattr(self, "folder") else ".")["cost"]
        self.assertEqual((cost["earlier"], cost["total"]), (None, 0))

    def test_an_earlier_call_shows_its_text_cost_as_earlier(self):
        add_call(self.conn, "a", cost_text="$0.1404")
        cost = self.detail()["cost"]
        self.assertEqual((cost["gemini"], cost["earlier"], cost["total"]), (None, 0.1404, 0.1404))

    def test_no_analysis_and_no_campaign(self):
        add_call(self.conn, "a")
        got = self.detail()
        self.assertIsNone(got["analysis"])
        self.assertIsNone(got["campaign"])
        self.assertIsNone(got["transcript"])

    def test_the_campaign_it_belongs_to(self):
        add_call(self.conn, "a")
        campaign_id = add_campaign_row(self.conn, name="October list", contacts=[("9876543210", "a", None)])
        self.assertEqual(self.detail()["campaign"], {"id": campaign_id, "name": "October list"})

    def test_the_transcript_is_read_from_inside_the_folder(self):
        path = os.path.join(self.folder, "a.txt")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("Neha: Hello\nUser: হ্যাঁ\n")
        add_call(self.conn, "a", transcript_path=path)
        self.assertEqual(self.detail()["transcript"], "Neha: Hello\nUser: হ্যাঁ\n")


class TestReadTranscript(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
        self.root = os.path.join(self.base, "TRANSCRIPTS")
        os.mkdir(self.root)
        self.evil = os.path.join(self.base, "TRANSCRIPTS_evil")
        os.mkdir(self.evil)
        for folder in (self.root, self.evil):
            with open(os.path.join(folder, "x.txt"), "w", encoding="utf-8") as handle:
                handle.write("secret" if folder == self.evil else "fine")

    def test_inside_the_folder(self):
        self.assertEqual(admin_data.read_transcript(os.path.join(self.root, "x.txt"), self.root), "fine")

    def test_dot_dot_out_of_the_folder_is_refused(self):
        self.assertIsNone(admin_data.read_transcript(os.path.join(self.root, "..", "TRANSCRIPTS_evil", "x.txt"),
                                                     self.root))

    def test_a_sibling_folder_with_the_same_prefix_is_refused(self):
        self.assertIsNone(admin_data.read_transcript(os.path.join(self.evil, "x.txt"), self.root))

    def test_an_absolute_path_elsewhere_is_refused(self):
        self.assertIsNone(admin_data.read_transcript(os.path.abspath(__file__), self.root))

    def test_missing_empty_and_none(self):
        self.assertIsNone(admin_data.read_transcript(os.path.join(self.root, "gone.txt"), self.root))
        self.assertIsNone(admin_data.read_transcript("", self.root))
        self.assertIsNone(admin_data.read_transcript(None, self.root))

    def test_a_long_file_is_cut(self):
        path = os.path.join(self.root, "big.txt")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("x" * (admin_data.TRANSCRIPT_MAX_CHARS + 10))
        self.assertEqual(len(admin_data.read_transcript(path, self.root)), admin_data.TRANSCRIPT_MAX_CHARS)


class TestCampaignReports(DbCase):
    def test_funnel_and_cost(self):
        add_call(self.conn, "c1", gemini=0.1, stt=0.01, nlp=0.002, plivo=0.02)
        add_call(self.conn, "c2", gemini=0.2)
        add_call(self.conn, "c3", connected=False)
        add_analysis(self.conn, "c1", lead_status="hot", campus_visit_agreed=1)
        add_analysis(self.conn, "c2", lead_status="warm", campus_visit_agreed=0)
        campaign_id = add_campaign_row(self.conn, contacts=[
            ("9000000001", "c1", "2026-10-12 10:00:00"), ("9000000002", "c2", "2026-10-12 10:01:00"),
            ("9000000003", "c3", "2026-10-12 10:02:00"), ("9000000004", None, None)])
        (row,) = admin_data.campaign_reports(self.conn, None, None)
        self.assertEqual((row["id"], row["name"], row["state"]), (campaign_id, "October list", "done"))
        self.assertEqual((row["contacts"], row["dialed"], row["reached"], row["hot"], row["visits"]), (4, 3, 2, 1, 1))
        self.assertAlmostEqual(row["cost_usd"], 0.332)
        self.assertAlmostEqual(row["cost_per_hot_lead"], 0.332)

    def test_no_hot_lead_means_no_cost_per_hot_lead(self):
        add_call(self.conn, "c1", gemini=0.1)
        add_campaign_row(self.conn, contacts=[("9000000001", "c1", "2026-10-12 10:00:00")])
        self.assertIsNone(admin_data.campaign_reports(self.conn, None, None)[0]["cost_per_hot_lead"])

    def test_an_empty_campaign_lists_with_zeros(self):
        add_campaign_row(self.conn)
        (row,) = admin_data.campaign_reports(self.conn, None, None)
        self.assertEqual((row["contacts"], row["dialed"], row["reached"], row["hot"], row["cost_usd"]), (0, 0, 0, 0, 0))

    def test_the_range_is_the_campaign_creation_day_and_newest_comes_first(self):
        add_campaign_row(self.conn, "old", "2026-09-01 09:00:00")
        add_campaign_row(self.conn, "mid", "2026-10-08 09:00:00")
        add_campaign_row(self.conn, "new", "2026-10-12 09:00:00")
        got = admin_data.campaign_reports(self.conn, "2026-10-07", "2026-10-13")
        self.assertEqual([r["name"] for r in got], ["new", "mid"])


class TestDoNotCall(DbCase):
    def numbers(self, q=None):
        return [r["phone"] for r in admin_data.list_dnc(self.conn, q)]

    def test_add_normalises_and_lists_newest_first(self):
        self.assertEqual(admin_data.add_dnc(self.conn, "+91 98765-43210", "asked us to stop", "2026-10-12 10:00:00"),
                         "9876543210")
        admin_data.add_dnc(self.conn, 9000000001, "", "2026-10-13 10:00:00")
        self.assertEqual(self.numbers(), ["9000000001", "9876543210"])
        self.assertEqual(admin_data.list_dnc(self.conn)[1]["note"], "asked us to stop")

    def test_a_bad_number_or_long_note_is_refused(self):
        for bad in ("12345", "", None, "abcdefghij"):
            with self.assertRaises(admin_data.AdminError, msg=bad):
                admin_data.add_dnc(self.conn, bad, "")
        with self.assertRaises(admin_data.AdminError):
            admin_data.add_dnc(self.conn, "9876543210", "x" * 201)
        with self.assertRaises(admin_data.AdminError):
            admin_data.add_dnc(self.conn, "9876543210", {"a": 1})
        self.assertEqual(self.numbers(), [])

    def test_adding_again_keeps_the_first_row_and_a_given_note_replaces_the_note(self):
        admin_data.add_dnc(self.conn, "9876543210", "first", "2026-10-12 10:00:00")
        admin_data.add_dnc(self.conn, "9876543210", "", "2026-10-13 10:00:00")
        self.assertEqual(admin_data.list_dnc(self.conn)[0]["note"], "first")
        admin_data.add_dnc(self.conn, "09876543210", "second", "2026-10-13 11:00:00")
        (row,) = admin_data.list_dnc(self.conn)
        self.assertEqual((row["note"], row["added_at"]), ("second", "2026-10-12 10:00:00"))

    def test_a_number_added_by_hand_in_another_format_is_matched(self):
        self.conn.execute("INSERT INTO do_not_call (phone, added_at) VALUES ('+91 90000 00001', '2026-10-01 10:00:00')")
        admin_data.add_dnc(self.conn, "9000000001", "dup")
        self.assertEqual(len(admin_data.list_dnc(self.conn)), 1)
        self.assertEqual(admin_data.remove_dnc(self.conn, "9000000001"), "9000000001")
        self.assertEqual(self.numbers(), [])

    def test_remove(self):
        admin_data.add_dnc(self.conn, "9876543210", "")
        admin_data.remove_dnc(self.conn, "98765 43210")
        self.assertEqual(self.numbers(), [])
        with self.assertRaises(admin_data.NotFound):
            admin_data.remove_dnc(self.conn, "9876543210")
        with self.assertRaises(admin_data.AdminError):
            admin_data.remove_dnc(self.conn, "junk")

    def test_search_matches_number_or_note_literally(self):
        admin_data.add_dnc(self.conn, "9876543210", "parent called")
        admin_data.add_dnc(self.conn, "9000000001", "100% sure")
        self.assertEqual(self.numbers("43210"), ["9876543210"])
        self.assertEqual(self.numbers("parent"), ["9876543210"])
        self.assertEqual(self.numbers("%"), ["9000000001"])
        self.assertEqual(self.numbers("zzz"), [])

    def test_search_text_is_checked(self):
        self.assertIsNone(admin_data.check_search({}))
        self.assertEqual(admin_data.check_search({"q": " 98 "}), "98")
        with self.assertRaises(admin_data.AdminError):
            admin_data.check_search({"q": "x" * 101})


class TestCsvCell(unittest.TestCase):
    def test_formula_starts_get_a_leading_quote(self):
        for text in ("=1+1", "+1+1", "-5+2", "@SUM(A1)", "\tcmd", "\rcmd", "=", "+", "-", "+91 98765"):
            self.assertEqual(admin_data.csv_cell(text), "'" + text, text)

    def test_plain_text_numbers_and_phones_pass_through(self):
        for value, shown in (("Asha", "Asha"), ("a=b", "a=b"), ("", ""), (None, ""), (12, "12"), (0.132, "0.132"),
                             ("9876543210", "9876543210"), ("+919876543210", "+919876543210"),
                             ("আশা", "আশা"), (True, "True")):
            self.assertEqual(admin_data.csv_cell(value), shown, value)

    def test_export_calls_csv_queries_and_writes_in_one_step(self):
        conn = new_db()
        self.addCleanup(conn.close)
        add_call(conn, "a", name="Asha")
        body = admin_data.export_calls_csv(conn, None, None, NO_FILTERS)
        rows = body.decode("utf-8-sig").split("\r\n")
        self.assertEqual((rows[0].split(",")[0], rows[1].split(",")[:3]), ("Call ID", ["a", "2026-10-12 10:00:00", "Asha"]))

    def test_the_file_has_a_bom_crlf_lines_and_quotes_commas(self):
        body = admin_data.csv_bytes(["A", "B"], [["x,y", 'he said "hi"'], ["line\nbreak", None]], note="cut")
        self.assertTrue(body.startswith(b"\xef\xbb\xbf"))
        text = body.decode("utf-8-sig")
        self.assertEqual(text, 'A,B\r\n"x,y","he said ""hi"""\r\n"line\nbreak",\r\ncut\r\n')


if __name__ == "__main__":
    unittest.main()
