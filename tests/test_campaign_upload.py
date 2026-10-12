"""campaigns.py upload: .xlsx parsing, phone normalisation, row screening (docs/spec/campaigns-design.md §3)."""

from __future__ import annotations

import io
import unittest
import zipfile
from datetime import timedelta
from unittest import mock
from xml.sax.saxutils import escape

import campaigns
from tests.test_campaigns_store import NOW, SETTINGS, CampaignCase, add_campaign

_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


class Num(str):
    """A cell written as a raw number (<v>), the way Excel stores a phone typed as a number."""


def _column(i):
    letters = ""
    i += 1
    while i:
        i, rest = divmod(i - 1, 26)
        letters = chr(65 + rest) + letters
    return letters


def make_xlsx(rows):
    """A one-sheet .xlsx: str -> inline text, int/float/Num -> number cell, None -> no cell."""
    sheet_rows = []
    for r, row in enumerate(rows, start=1):
        cells = []
        for c, value in enumerate(row):
            ref = f"{_column(c)}{r}"
            if value is None:
                continue
            if isinstance(value, (Num, int, float)):
                cells.append(f'<c r="{ref}"><v>{value}</v></c>')
            else:
                cells.append(f'<c r="{ref}" t="inlineStr"><is><t>{escape(value)}</t></is></c>')
        sheet_rows.append(f'<row r="{r}">{"".join(cells)}</row>')
    sheet = f'<worksheet xmlns="{_MAIN}"><sheetData>{"".join(sheet_rows)}</sheetData></worksheet>'
    workbook = (f'<workbook xmlns="{_MAIN}" xmlns:r="{_REL}"><sheets>'
                f'<sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets></workbook>')
    rels = ('<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="worksheet" Target="worksheets/sheet1.xml"/></Relationships>')
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("xl/workbook.xml", workbook)
        z.writestr("xl/_rels/workbook.xml.rels", rels)
        z.writestr("xl/worksheets/sheet1.xml", sheet)
    return buffer.getvalue()


class TestNormalisePhone(unittest.TestCase):
    def test_normalise_phone(self):
        good = ["9876543210", "+919876543210", "919876543210", "09876543210", "+91 98765-43210",
                "(987) 654-3210", "98765 43210", "9.87654321E9", "9876543210.0", " 6000000000 "]
        for text in good:
            self.assertIn(campaigns.normalise_phone(text), ("9876543210", "6000000000"), text)
        bad = ["", "12345", "5876543210", "98765432101", "abcdefghij", "९८७६५४३२१०", "98765.4321",
               "+1 9876543210", "1e10", None, "1E5000", "1E999999999"]
        for text in bad:
            self.assertIsNone(campaigns.normalise_phone(text), text)


class TestParseUpload(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.multiple(campaigns, MAX_FILE_BYTES=2 * 1024 * 1024, MAX_UNPACKED_BYTES=50 * 1024 * 1024,
                                      MAX_ROWS=5000, MAX_EXTRAS=20, MAX_CELL_CHARS=200)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_header_variants_and_extras(self):
        data = make_xlsx([["Student  Name", "Mobile Number", "City", "Course Interest"],
                          ["অনিতা দাস", "9876543210", "Kolkata", "B.Tech"]])
        rows, extras, dropped = campaigns.parse_upload(data)
        self.assertEqual(extras, ["City", "Course Interest"])
        self.assertEqual(dropped, 0)
        self.assertEqual(rows, [{"sheet_row": 2, "name": "অনিতা দাস", "phone_text": "9876543210",
                                 "extra": {"City": "Kolkata", "Course Interest": "B.Tech"}}])

    def test_every_accepted_header(self):
        for name in campaigns.NAME_HEADERS:
            for phone in campaigns.PHONE_HEADERS:
                rows, _, _ = campaigns.parse_upload(make_xlsx([[name.upper(), phone.title()], ["A", "9876543210"]]))
                self.assertEqual(rows[0]["phone_text"], "9876543210", (name, phone))

    def test_numeric_and_text_cells(self):
        data = make_xlsx([["Name", "Phone"], ["A", 9876543210], ["B", Num("9.87654321E9")], ["C", "+91 98765 43210"]])
        rows, _, _ = campaigns.parse_upload(data)
        self.assertEqual([campaigns.normalise_phone(r["phone_text"]) for r in rows], ["9876543210"] * 3)

    def test_blank_rows_are_skipped(self):
        data = make_xlsx([[None], ["Name", "Phone"], [None, None], ["A", "9876543210"], [], ["B", "9876543211"]])
        rows, _, _ = campaigns.parse_upload(data)
        self.assertEqual([(r["sheet_row"], r["name"]) for r in rows], [(2, "A"), (3, "B")])

    def test_short_rows_and_missing_cells(self):
        rows, _, _ = campaigns.parse_upload(make_xlsx([["Name", "Phone", "City"], ["A"], [None, "9876543210"]]))
        self.assertEqual(rows[0], {"sheet_row": 2, "name": "A", "phone_text": "", "extra": {"City": ""}})
        self.assertEqual(rows[1]["name"], "")

    def test_cells_are_cut(self):
        rows, _, _ = campaigns.parse_upload(make_xlsx([["Name", "Phone", "Note"], ["x" * 300, "9876543210", "y" * 300]]))
        self.assertEqual((len(rows[0]["name"]), len(rows[0]["extra"]["Note"])), (200, 200))

    def test_extra_columns_capped_and_duplicates_dropped(self):
        header = ["Name", "Phone"] + [f"Col {i}" for i in range(25)] + ["col 0"]
        rows, extras, dropped = campaigns.parse_upload(make_xlsx([header, ["A", "9876543210"]]))
        self.assertEqual(len(extras), 20)
        self.assertEqual(dropped, 5)
        self.assertEqual(extras[0], "Col 0")

    def test_missing_columns(self):
        for header in (["Name", "City"], ["Phone", "City"], ["Naam", "Number"]):
            with self.assertRaises(campaigns.CampaignError):
                campaigns.parse_upload(make_xlsx([header, ["A", "9876543210"]]))

    def test_empty_sheet(self):
        with self.assertRaises(campaigns.CampaignError):
            campaigns.parse_upload(make_xlsx([]))

    def test_not_an_xlsx(self):
        for data in (b"", b"name,phone\nA,9876543210\n", b"PK\x03\x04garbage"):
            with self.assertRaises(campaigns.CampaignError):
                campaigns.parse_upload(data)
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as z:
            z.writestr("hello.txt", "not a workbook")
        with self.assertRaises(campaigns.CampaignError):
            campaigns.parse_upload(buffer.getvalue())

    def test_far_column_reference_is_refused(self):
        # A few hundred bytes with r="ZZZZ1" would make rows millions of cells wide in read_sheets.
        data = make_xlsx([["Name", "Phone"], ["A", "9876543210"]])
        buffer = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(data)) as src, zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as dst:
            for info in src.infolist():
                body = src.read(info)
                if info.filename.endswith("sheet1.xml"):
                    self.assertIn(b'r="B2"', body)
                    body = body.replace(b'r="B2"', b'r="ZZZZ2"')
                dst.writestr(info.filename, body)
        with self.assertRaises(campaigns.CampaignError) as caught:
            campaigns.parse_upload(buffer.getvalue())
        self.assertIn("columns", str(caught.exception))

    def _with_sheet(self, sheet_xml, target="worksheets/sheet1.xml"):
        """make_xlsx's workbook with this sheet XML at this rels target."""
        rels = ('<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                f'<Relationship Id="rId1" Type="worksheet" Target="{target}"/></Relationships>')
        buffer = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(make_xlsx([]))) as src, \
                zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as dst:
            for info in src.infolist():
                if info.filename == "xl/workbook.xml":
                    dst.writestr(info.filename, src.read(info))
            dst.writestr("xl/_rels/workbook.xml.rels", rels)
            dst.writestr("xl/" + target, sheet_xml)
        return buffer.getvalue()

    def test_column_check_cannot_be_bypassed(self):
        head = f'<worksheet xmlns="{_MAIN}"><sheetData>'
        tail = '</sheetData></worksheet>'
        header = '<row r="1"><c r="A1" t="inlineStr"><is><t>Name</t></is></c><c r="B1" t="inlineStr"><is><t>Phone</t></is></c></row>'
        for far, target in (("<c r='AMK2'><v>1</v></c>", "worksheets/sheet1.xml"),            # single quotes
                            ('<c r="&#65;MK2"><v>1</v></c>', "worksheets/sheet1.xml"),        # character reference
                            ('<c r="AMK2"><v>1</v></c>', "elsewhere/sheet1.xml")):          # outside worksheets/
            data = self._with_sheet(head + header + f'<row r="2">{far}</row>' + tail, target)
            with self.assertRaises(campaigns.CampaignError, msg=far) as caught:
                campaigns.parse_upload(data)
            self.assertIn("columns", str(caught.exception))

    def test_many_wide_rows_are_refused_without_building_them(self):
        # 20,000 rows each padded to column AMJ (1,024 wide): a 3 KB zip that read_sheets turned into ~180 MB.
        import tracemalloc
        body = "".join(f'<row r="{i}"><c r="AMJ{i}"><v>1</v></c></row>' for i in range(2, 20002))
        data = self._with_sheet(f'<worksheet xmlns="{_MAIN}"><sheetData>'
                                '<row r="1"><c r="A1" t="inlineStr"><is><t>Name</t></is></c>'
                                '<c r="B1" t="inlineStr"><is><t>Phone</t></is></c></row>'
                                f'{body}</sheetData></worksheet>')
        tracemalloc.start()
        try:
            with self.assertRaises(campaigns.CampaignError) as caught:
                campaigns.parse_upload(data)
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        self.assertIn("rows", str(caught.exception))
        # At most MAX_ROWS rows of 1,024 cells are ever built (~45 MB of lists under tracemalloc), not 20,000.
        self.assertLess(peak, 80 * 1024 * 1024)

    def test_shared_string_cells(self):
        # Real Excel files store text in xl/sharedStrings.xml (t="s"), not inline.
        sheet = (f'<worksheet xmlns="{_MAIN}"><sheetData>'
                 '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c></row>'
                 '<row r="2"><c r="A2" t="s"><v>2</v></c><c r="B2"><v>9876543210</v></c></row>'
                 '</sheetData></worksheet>')
        shared = (f'<sst xmlns="{_MAIN}"><si><t>Name</t></si><si><t>Phone</t></si><si><t>অনিতা</t></si></sst>')
        buffer = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(make_xlsx([]))) as src, zipfile.ZipFile(buffer, "w") as dst:
            for info in src.infolist():
                body = sheet.encode() if info.filename.endswith("sheet1.xml") else src.read(info)
                dst.writestr(info.filename, body)
            dst.writestr("xl/sharedStrings.xml", shared)
        rows, _, _ = campaigns.parse_upload(buffer.getvalue())
        self.assertEqual((rows[0]["name"], rows[0]["phone_text"]), ("অনিতা", "9876543210"))

    def test_long_headers_are_cut(self):
        _, extras, _ = campaigns.parse_upload(make_xlsx([["Name", "Phone", "H" * 300], ["A", "9876543210"]]))
        self.assertEqual(len(extras[0]), 200)

    def test_file_size_limit(self):
        data = make_xlsx([["Name", "Phone"], ["A", "9876543210"]])
        with mock.patch.object(campaigns, "MAX_FILE_BYTES", len(data) - 1):
            with self.assertRaises(campaigns.CampaignError):
                campaigns.parse_upload(data)

    def test_unpacked_size_limit(self):
        data = make_xlsx([["Name", "Phone"]] + [["A" * 100, "9876543210"]] * 50)
        with mock.patch.object(campaigns, "MAX_UNPACKED_BYTES", 1000):
            with self.assertRaises(campaigns.CampaignError) as caught:
                campaigns.parse_upload(data)
        self.assertIn("50 MB", str(caught.exception))

    def test_row_limit(self):
        data = make_xlsx([["Name", "Phone"]] + [["A", f"98765432{i:02d}"] for i in range(4)])
        with mock.patch.object(campaigns, "MAX_ROWS", 3):
            with self.assertRaises(campaigns.CampaignError):
                campaigns.parse_upload(data)


def parsed(*phones):
    return [{"sheet_row": i + 2, "name": f"N{i}", "phone_text": p, "extra": {}} for i, p in enumerate(phones)]


class TestScreenRows(CampaignCase):
    def test_each_reason(self):
        self.conn.execute("INSERT INTO do_not_call (phone, added_at) VALUES ('9000000005', '2026-10-01 10:00:00')")
        self.conn.execute("INSERT INTO calls (call_uuid, phone, created_at) VALUES ('u-1', '9000000006', ?)",
                          (campaigns._stamp(NOW - timedelta(days=3)),))
        contacts, rejected = campaigns.screen_rows(self.conn, parsed(
            "9000000001", "", "12345", "+91 90000 00001", "9000000005", "9000000006"), NOW)
        self.assertEqual([c["phone"] for c in contacts], ["9000000001"])
        self.assertEqual([(r["sheet_row"], r["reason"]) for r in rejected],
                         [(3, "missing_number"), (4, "bad_number"), (5, "duplicate_in_file"),
                          (6, "on_do_not_call"), (7, "called_recently")])
        self.assertEqual(rejected[2]["phone"], "+91 90000 00001")   # as written, for the operator

    def test_recent_window(self):
        self.conn.execute("INSERT INTO calls (call_uuid, phone, created_at) VALUES ('u-1', '+919000000001', ?)",
                          (campaigns._stamp(NOW - timedelta(days=40)),))
        contacts, rejected = campaigns.screen_rows(self.conn, parsed("9000000001"), NOW)
        self.assertEqual((len(contacts), rejected), (1, []))

    def test_other_campaigns_count_but_not_their_cancelled_contacts(self):
        add_campaign(self.conn, ["9000000001", "9000000002"])
        self.conn.execute("UPDATE campaign_contacts SET status='cancelled' WHERE phone='9000000002'")
        contacts, rejected = campaigns.screen_rows(self.conn, parsed("9000000001", "9000000002"), NOW)
        self.assertEqual([c["phone"] for c in contacts], ["9000000002"])
        self.assertEqual(rejected[0]["reason"], "called_recently")

    def test_create_from_upload(self):
        result = campaigns.create_from_upload(self.conn, "Oct", SETTINGS, parsed("9000000001", "123"),
                                              created_by="operator", file_name="list.xlsx", now=NOW)
        self.assertEqual((result["accepted"], [r["reason"] for r in result["rejected"]]), (1, ["bad_number"]))
        state, rejected_count = self.conn.execute("SELECT state, rejected_count FROM campaigns WHERE id=?",
                                                  (result["id"],)).fetchone()
        self.assertEqual((state, rejected_count), ("draft", 1))

    def test_all_rejected_stores_nothing(self):
        with self.assertRaises(campaigns.NothingToCall) as caught:
            campaigns.create_from_upload(self.conn, "Oct", SETTINGS, parsed("123", ""), created_by="operator",
                                         file_name="list.xlsx", now=NOW)
        self.assertEqual(len(caught.exception.rejected), 2)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM campaigns").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
