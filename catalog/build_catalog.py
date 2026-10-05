"""Build catalog/courses.json from the two AU admission Excel files.

Run by hand whenever either Excel file changes, then commit the new JSON:

    venv312/Scripts/python.exe catalog/build_catalog.py

Stdlib only (zipfile + ElementTree): requirements.txt is frozen, so no openpyxl.
Anything the join rules below do not explain fails the build with BuildError.
Design: docs/spec/course-catalog-tools-design.md sections 3 and 5.
"""
import collections
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "AU Admission Data")
PROGRAM_XLSX = os.path.join(
    DATA_DIR, "Academic Program Notification_2026-2027_Corrigendum_FINAL_06082026 (4) (1).xlsx"
)
FEE_XLSX = os.path.join(DATA_DIR, "National Course fee 2026.xlsx")
CATALOG_DIR = os.path.join(ROOT, "catalog")
ALIASES_JSON = os.path.join(CATALOG_DIR, "aliases.json")
COURSES_JSON = os.path.join(CATALOG_DIR, "courses.json")

SESSION = "2027"
ADMISSION_FEE_NOTE = "one-time, paid with the first semester fee; includes T-shirt and blazer"

# Operator-confirmed join special cases (2026-10-05). An entry that no longer
# matches the data is a build error, so this list cannot go stale silently.
SHARED_FEE_UIDS = {"121", "133"}
SPLIT_PROGRAMS = {"171": ["M.Pharm (Pharmaceutics)", "M.Pharm (Pharmacology)"]}
ADD_ON_FEE_ROWS = {
    "137": ("B.Tech (Computer Science and Engineering) SAP add-on- Certification",
            "SAP add-on certification"),
}
EXCLUDED = {"M. Tech (VLSI & Embedded Systems)", "Post-Graduate Diploma in Nuclear Medicine Technology"}

LEVEL_MAP = {"UG": "UG", "PG": "PG", "DIPLOMA": "Diploma", "Post-M.Sc. Diploma": "Diploma"}

# Checked in order against the name lowercased with spaces and dots removed, so
# "B. Sc. (Hons.)" and "B.Sc." both read "bsc". Longer prefixes come first.
DEGREE_PREFIXES = [
    ("bachelorofmedicallaboratory", "BMLS"),
    ("bachelorofoptometry", "B.Optom"),
    ("bacheloroffoodnutrition", "BFND"),
    ("post-msc", "Post-M.Sc Diploma"),
    ("ballb", "BA LL.B"),
    ("bballb", "BBA LL.B"),
    ("llm", "LL.M"),
    ("btech", "B.Tech"),
    ("mtech", "M.Tech"),
    ("bca", "BCA"),
    ("mca", "MCA"),
    ("bsc", "B.Sc"),
    ("msc", "M.Sc"),
    ("bcom", "B.Com"),
    ("bba", "BBA"),
    ("mba", "MBA"),
    ("bpharm", "B.Pharm"),
    ("mpharm", "M.Pharm"),
    ("dpharm", "D.Pharm"),
    ("bed", "B.Ed"),
    ("ba", "B.A"),
    ("ma", "M.A"),
]

FEE_COLUMN_PREFIXES = {
    "s1": "1st Sem", "s2": "2nd Sem", "s3": "3rd Sem", "s4": "4th Sem",
    "s56": "5th - 6th", "s710": "7th - 10th",
    "final": "Final Program", "admission": "Admission Fees", "total": "Total Fees",
}

_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
_REL_ID = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"


class BuildError(Exception):
    pass


def tidy(text):
    return " ".join(str(text).split())


def _column_index(ref):
    n = 0
    for ch in re.match(r"[A-Z]+", ref).group(0):
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def read_sheets(path):
    """Every sheet of an xlsx file as a list of rows (lists of cell strings)."""
    with zipfile.ZipFile(path) as z:
        shared = []
        if "xl/sharedStrings.xml" in z.namelist():
            for si in ET.fromstring(z.read("xl/sharedStrings.xml")).findall("m:si", _NS):
                shared.append("".join(t.text or "" for t in si.iter("{%s}t" % _NS["m"])))
        workbook = ET.fromstring(z.read("xl/workbook.xml"))
        rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
        targets = {r.get("Id"): r.get("Target") for r in rels}
        sheets = []
        for sheet in workbook.find("m:sheets", _NS):
            target = targets[sheet.get(_REL_ID)].lstrip("/")
            if not target.startswith("xl/"):
                target = "xl/" + target
            rows = []
            for row in ET.fromstring(z.read(target)).iter("{%s}row" % _NS["m"]):
                cells = {}
                for c in row.findall("m:c", _NS):
                    v = c.find("m:v", _NS)
                    kind = c.get("t")
                    if kind == "s" and v is not None:
                        value = shared[int(v.text)]
                    elif kind == "inlineStr":
                        value = "".join(x.text or "" for x in c.iter("{%s}t" % _NS["m"]))
                    else:
                        value = v.text if v is not None else ""
                    cells[_column_index(c.get("r"))] = value
                if cells:
                    rows.append([cells.get(i, "") for i in range(max(cells) + 1)])
            sheets.append(rows)
        return sheets


def table_rows(path):
    """Data rows of every sheet as dicts keyed by the sheet's "S.N." header row."""
    out = []
    for rows in read_sheets(path):
        header = None
        for row in rows:
            cells = [tidy(c) for c in row]
            if cells[0] == "S.N.":
                header = cells
                continue
            if header is None or not any(cells):
                continue
            cells += [""] * (len(header) - len(cells))
            out.append(dict(zip(header, cells)))
    return out


def find_column(row, prefix):
    matches = [key for key in row if key.startswith(prefix)]
    if len(matches) != 1:
        raise BuildError(f"expected one column starting {prefix!r}, found {matches}")
    return matches[0]


def amount(text, what):
    if text == "":
        return 0
    value = float(text.replace(",", ""))
    if value != int(value):
        raise BuildError(f"{what}: amount {text!r} is not a whole number of rupees")
    return int(value)


def fee_block(row, label):
    """Fee fields for one fee row; asserts the arithmetic in spec section 3."""
    cols = {key: find_column(row, prefix) for key, prefix in FEE_COLUMN_PREFIXES.items()}
    v = {key: amount(row[col], f"{label} {col}") for key, col in cols.items()}
    years = int(amount(row[find_column(row, "Duration")], f"{label} Duration"))
    sems = 2 * years
    firsts = [v["s1"], v["s2"], v["s3"], v["s4"]]
    per_semester = (firsts[:sems]
                    + [v["s56"]] * min(2, max(0, sems - 4))
                    + [v["s710"]] * max(0, sems - 6))
    unused = firsts[sems:] + ([v["s56"]] if sems <= 4 else []) + ([v["s710"]] if sems <= 6 else [])
    if any(unused):
        raise BuildError(f"{label}: fee columns beyond its {years}-year duration are not zero")
    if sum(per_semester) != v["final"]:
        raise BuildError(f"{label}: semesters sum to {sum(per_semester)}, Final Program Fees is {v['final']}")
    if v["final"] + v["admission"] != v["total"]:
        raise BuildError(f"{label}: final {v['final']} + admission {v['admission']} != total {v['total']}")
    return years, {
        "session": SESSION,
        "first_semester": per_semester[0],
        "admission_fee": v["admission"],
        "admission_fee_note": ADMISSION_FEE_NOTE,
        "due_at_admission": per_semester[0] + v["admission"],
        "per_year": [per_semester[i] + per_semester[i + 1] for i in range(0, sems, 2)],
        "per_semester": per_semester,
        "program_fee": v["final"],
        "total": v["total"],
    }


def degree_of(name):
    compact = name.lower().replace(" ", "").replace(".", "")
    for prefix, degree in DEGREE_PREFIXES:
        if compact.startswith(prefix):
            return degree
    raise BuildError(f"no degree pattern matches program {name!r}")


# Same degree-token joins as course_catalog.normalize, so "B. A", "B.A." and
# "BA" all slug to "ba".
_SLUG_JOINS = [
    (re.compile(r"\bb b a\b"), "bba"),
    (re.compile(r"\b([bmd]) (tech|sc|com|ed|pharm|optom|a)\b"), r"\1\2"),
    (re.compile(r"\bll ([bm])\b"), r"ll\1"),
]


def slug(name):
    text = re.sub(r"[^a-z0-9]+", " ", name.lower().replace("&", " and ")).strip()
    for pattern, replacement in _SLUG_JOINS:
        text = pattern.sub(replacement, text)
    return text.replace(" ", "-")


def school_of(text):
    m = re.match(r"^(.*?)\s*\((\w+)\)$", text)
    if not m:
        raise BuildError(f"school {text!r} has no (CODE) suffix")
    return {"name": m.group(1), "code": m.group(2)}


def eligibility_text(text):
    return re.sub(r"10\s*\+\s*2", "10+2", tidy(text))


def build():
    programs = table_rows(PROGRAM_XLSX)
    fees = table_rows(FEE_XLSX)
    with open(ALIASES_JSON, encoding="utf-8") as f:
        hand_aliases = json.load(f)["records"]

    excluded = []
    prog_by_uid = collections.OrderedDict()
    for row in programs:
        if row["Program"] in EXCLUDED:
            excluded.append(row["Program"])
            continue
        if not row["UID"]:
            raise BuildError(f"program {row['Program']!r} has a blank UID")
        prog_by_uid.setdefault(row["UID"], []).append(row)
    fee_by_uid = collections.defaultdict(list)
    for row in fees:
        if row["Program."] in EXCLUDED:
            excluded.append(row["Program."])
            continue
        if not row["UID"]:
            raise BuildError(f"fee row {row['Program.']!r} has a blank UID")
        fee_by_uid[row["UID"]].append(row)

    if set(excluded) != EXCLUDED:
        raise BuildError(f"EXCLUDED entries not found in the data: {sorted(EXCLUDED - set(excluded))}")
    for uid in sorted(set(fee_by_uid) - set(prog_by_uid)):
        raise BuildError(f"fee UID {uid} has no program row")
    for table in (SHARED_FEE_UIDS, SPLIT_PROGRAMS, ADD_ON_FEE_ROWS):
        for uid in table:
            if uid not in prog_by_uid:
                raise BuildError(f"special-case UID {uid} is not in the program file")

    records = []
    for uid, prog_rows in prog_by_uid.items():
        fee_rows = list(fee_by_uid.get(uid, []))
        if not fee_rows:
            raise BuildError(f"program UID {uid} has no fee row")
        add_ons = []
        if uid in ADD_ON_FEE_ROWS:
            source_name, add_on_name = ADD_ON_FEE_ROWS[uid]
            row = next((r for r in fee_rows if r["Program."] == source_name), None)
            if row is None:
                raise BuildError(f"add-on fee row {source_name!r} not found under UID {uid}")
            fee_rows.remove(row)
            years, block = fee_block(row, f"UID {uid} add-on")
            add_ons.append({"name": add_on_name, "duration_years": years, "fee": block["total"]})

        if uid in SPLIT_PROGRAMS:
            names = SPLIT_PROGRAMS[uid]
            by_name = {r["Program."]: r for r in fee_rows}
            if len(prog_rows) != 1 or sorted(by_name) != sorted(names):
                raise BuildError(f"UID {uid} no longer matches its SPLIT_PROGRAMS entry")
            pairs = [(prog_rows[0], name, by_name[name]) for name in names]
        elif len(fee_rows) == 1:
            if (len(prog_rows) > 1) != (uid in SHARED_FEE_UIDS):
                raise BuildError(f"UID {uid}: {len(prog_rows)} program rows, SHARED_FEE_UIDS disagrees")
            pairs = [(row, row["Program"], fee_rows[0]) for row in prog_rows]
        else:
            raise BuildError(f"UID {uid}: {len(prog_rows)} program rows and {len(fee_rows)} fee rows")

        for prog, name, fee_row in pairs:
            fee_years, fee = fee_block(fee_row, f"UID {uid} {name}")
            duration_text = prog["Program Duration (Years)"]
            if duration_text.isdigit():
                duration_years, duration_note = int(duration_text), None
                if duration_years != fee_years:
                    raise BuildError(f"{name}: duration {duration_years} vs fee-file duration {fee_years}")
            else:
                duration_years, duration_note = None, duration_text
            if prog["Level"] not in LEVEL_MAP:
                raise BuildError(f"{name}: unknown level {prog['Level']!r}")
            aliases = []
            fee_name = fee_row["Program."]
            if uid not in SHARED_FEE_UIDS and fee_name.lower() != name.lower():
                aliases.append(fee_name)
            record = {
                "id": slug(name),
                "uid": uid,
                "name": name,
                "aliases": aliases,
                "degree": degree_of(name),
                "level": LEVEL_MAP[prog["Level"]],
                "school": school_of(prog["School"]),
                "department": prog["Department"],
                "duration_years": duration_years,
                "duration_note": duration_note,
                "seats": int(prog["Intake"]),
                "eligibility": eligibility_text(prog["Eligibility Criteria"]),
                "fees": fee,
                "add_ons": add_ons,
            }
            if uid in SPLIT_PROGRAMS:
                others = [n for n in SPLIT_PROGRAMS[uid] if n != name]
                record["seats_note"] = "intake shared with " + ", ".join(others)
            records.append(record)

    ids = collections.Counter(r["id"] for r in records)
    clashes = sorted(i for i, n in ids.items() if n > 1)
    if clashes:
        raise BuildError(f"duplicate record ids: {clashes}")
    for record_id, extra in hand_aliases.items():
        if record_id not in ids:
            raise BuildError(f"aliases.json names unknown record id {record_id!r}")
    for record in records:
        for alias in hand_aliases.get(record["id"], []):
            if alias not in record["aliases"]:
                record["aliases"].append(alias)

    return {
        "session": SESSION,
        "source": [os.path.basename(PROGRAM_XLSX), os.path.basename(FEE_XLSX)],
        "excluded": sorted(excluded),
        "records": records,
    }


def dumps(catalog):
    return json.dumps(catalog, indent=1, ensure_ascii=False) + "\n"


def main():
    catalog = build()
    with open(COURSES_JSON, "w", encoding="utf-8", newline="\n") as f:
        f.write(dumps(catalog))
    records = catalog["records"]
    print(f"wrote {COURSES_JSON}: {len(records)} records")
    for code, count in sorted(collections.Counter(r["school"]["code"] for r in records).items()):
        print(f"  {code}: {count}")
    print("excluded (incomplete data):", ", ".join(catalog["excluded"]))
    print("records with aliases:", sum(1 for r in records if r["aliases"]))


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    main()
