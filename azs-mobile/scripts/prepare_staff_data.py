import argparse
import json
import math
import re
import zipfile
from datetime import date, datetime
from pathlib import Path
from xml.etree import ElementTree as ET


ROOT = Path(__file__).resolve().parents[2]
APP = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = APP / "data" / "staff"
OUT = APP / "data" / "staff_recommendations.json"

DAILY_SHEET = "По дням"
KSSS_COLUMN = "КССС"
DAY_NUMBER_COLUMN = "День"
DAY_LABEL_COLUMN = "День недели"
DAY_HOURS_COLUMN = "Совокупная сумма часов для дневной смены"
NIGHT_HOURS_COLUMN = "Совокупная сумма часов для ночной смены"
HOURS_PER_PERSON = 12

PERIOD_PATTERNS = [
    re.compile(r"(?P<month>0?[1-9]|1[0-2])[._-](?P<year>20\d{2})"),
    re.compile(r"(?P<year>20\d{2})[._-](?P<month>0?[1-9]|1[0-2])"),
]

MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"


def tag(namespace, name):
    return f"{{{namespace}}}{name}"


def clean(value):
    if value is None:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    text = str(value).strip()
    if text.lower() in {"nan", "none", "nat"}:
        return ""
    return text


def number(value):
    text = clean(value).replace(" ", "").replace(",", ".")
    if not text:
        return 0.0
    try:
        return float(text)
    except ValueError:
        return 0.0


def identifier(value):
    text = clean(value)
    if not text:
        return ""
    numeric = number(text)
    if re.fullmatch(r"\d+([.,]0+)?", text) and numeric.is_integer():
        return str(int(numeric))
    return text


def compact_number(value):
    rounded = round(float(value), 2)
    if rounded.is_integer():
        return int(rounded)
    return rounded


def people_from_hours(value):
    return compact_number(number(value) / HOURS_PER_PERSON)


def column_index(cell_ref):
    letters = re.match(r"[A-Z]+", cell_ref or "")
    if not letters:
        return None

    index = 0
    for char in letters.group(0):
        index = index * 26 + (ord(char) - ord("A") + 1)
    return index - 1


def parse_period(path):
    name = path.stem
    for pattern in PERIOD_PATTERNS:
        match = pattern.search(name)
        if match:
            year = int(match.group("year"))
            month = int(match.group("month"))
            return f"{year:04d}-{month:02d}", year, month
    return None


def excel_files(source_dir, recursive):
    pattern = "**/*.xlsx" if recursive else "*.xlsx"
    for path in sorted(source_dir.glob(pattern)):
        if path.name.startswith("~$"):
            continue
        if parse_period(path):
            yield path


def read_shared_strings(archive):
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []

    root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
    strings = []
    for item in root.findall(tag(MAIN_NS, "si")):
        parts = [node.text or "" for node in item.iter(tag(MAIN_NS, "t"))]
        strings.append("".join(parts))
    return strings


def sheet_path_for_name(archive, sheet_name):
    workbook = ET.fromstring(archive.read("xl/workbook.xml"))
    rels_root = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    rels = {
        rel.attrib["Id"]: rel.attrib["Target"]
        for rel in rels_root.findall(tag(PACKAGE_REL_NS, "Relationship"))
    }

    for sheet in workbook.findall(f".//{tag(MAIN_NS, 'sheet')}"):
        if sheet.attrib.get("name") != sheet_name:
            continue
        rel_id = sheet.attrib.get(tag(REL_NS, "id"))
        target = rels.get(rel_id)
        if not target:
            return None
        if target.startswith("/"):
            return target.lstrip("/")
        return str(Path("xl") / target)
    return None


def cell_text(cell, shared_strings):
    cell_type = cell.attrib.get("t")

    if cell_type == "inlineStr":
        parts = [node.text or "" for node in cell.iter(tag(MAIN_NS, "t"))]
        return "".join(parts)

    value_node = cell.find(tag(MAIN_NS, "v"))
    if value_node is None:
        return ""

    value = value_node.text or ""
    if cell_type == "s":
        try:
            return shared_strings[int(value)]
        except (ValueError, IndexError):
            return ""

    return value


def rows_from_sheet(archive, sheet_path, shared_strings):
    root = ET.fromstring(archive.read(sheet_path))
    for row in root.findall(f".//{tag(MAIN_NS, 'row')}"):
        values = {}
        for cell in row.findall(tag(MAIN_NS, "c")):
            index = column_index(cell.attrib.get("r", ""))
            if index is not None:
                values[index] = cell_text(cell, shared_strings)

        if values:
            max_index = max(values)
            yield [values.get(index, "") for index in range(max_index + 1)]


def read_xlsx_sheet(path, sheet_name):
    try:
        with zipfile.ZipFile(path) as archive:
            sheet_path = sheet_path_for_name(archive, sheet_name)
            if not sheet_path:
                return None

            shared_strings = read_shared_strings(archive)
            rows = list(rows_from_sheet(archive, sheet_path, shared_strings))
    except (KeyError, ET.ParseError, zipfile.BadZipFile):
        return None

    header = None
    result = []
    for row in rows:
        if header is None:
            if not any(clean(value) for value in row):
                continue
            header = [clean(value) for value in row]
            continue

        if not any(clean(value) for value in row):
            continue

        result.append(
            {
                column: row[index] if index < len(row) else ""
                for index, column in enumerate(header)
                if column
            }
        )
    return result


def read_daily_sheet(path):
    return read_xlsx_sheet(path, DAILY_SHEET)


def build_staff_database(source_dir, recursive=False):
    periods = {}
    files = []
    skipped = []
    overwritten_rows = 0

    required_columns = {
        KSSS_COLUMN,
        DAY_NUMBER_COLUMN,
        DAY_HOURS_COLUMN,
        NIGHT_HOURS_COLUMN,
    }

    for path in excel_files(source_dir, recursive):
        parsed = parse_period(path)
        if not parsed:
            continue

        period, year, month = parsed
        df = read_daily_sheet(path)
        if df is None:
            skipped.append({"file": str(path), "reason": f"Нет листа '{DAILY_SHEET}'"})
            continue

        missing = sorted(required_columns - set(df[0].keys() if df else []))
        if missing:
            skipped.append({"file": str(path), "reason": f"Нет колонок: {', '.join(missing)}"})
            continue

        period_bucket = periods.setdefault(period, {"sourceFiles": [], "stations": {}})
        period_bucket["sourceFiles"].append(path.name)

        imported_rows = 0
        for row in df:
            ksss = identifier(row.get(KSSS_COLUMN))
            if not ksss:
                continue

            day_number = int(number(row.get(DAY_NUMBER_COLUMN)))
            if day_number < 1:
                continue

            try:
                work_date = date(year, month, day_number).isoformat()
            except ValueError:
                continue

            day_hours = compact_number(number(row.get(DAY_HOURS_COLUMN)))
            night_hours = compact_number(number(row.get(NIGHT_HOURS_COLUMN)))
            station = period_bucket["stations"].setdefault(ksss, {"ksss": ksss, "days": {}})

            if work_date in station["days"]:
                overwritten_rows += 1

            station["days"][work_date] = {
                "date": work_date,
                "label": clean(row.get(DAY_LABEL_COLUMN)),
                "day": people_from_hours(day_hours),
                "night": people_from_hours(night_hours),
                "dayHours": day_hours,
                "nightHours": night_hours,
            }
            imported_rows += 1

        files.append({"file": str(path), "period": period, "rows": imported_rows})

    for period_data in periods.values():
        for station in period_data["stations"].values():
            days = sorted(station["days"].values(), key=lambda item: item["date"])
            station["days"] = days
            station["staffTotal"] = compact_number(
                max((number(item["day"]) + number(item["night"]) for item in days), default=0)
            )

        period_data["stationsCount"] = len(period_data["stations"])
        period_data["daysCount"] = sum(len(station["days"]) for station in period_data["stations"].values())

    return {
        "meta": {
            "sourceDir": str(source_dir),
            "generatedAt": datetime.now().isoformat(timespec="seconds"),
            "files": files,
            "skipped": skipped,
            "overwrittenRows": overwritten_rows,
        },
        "periods": dict(sorted(periods.items())),
    }


def main():
    parser = argparse.ArgumentParser(description="Build staff recommendations JSON from monthly Excel files.")
    parser.add_argument("source_dir", nargs="?", default=str(DEFAULT_SOURCE), help="Folder with monthly .xlsx files.")
    parser.add_argument("--recursive", action="store_true", help="Search for .xlsx files recursively.")
    parser.add_argument("--out", default=str(OUT), help="Output JSON path.")
    args = parser.parse_args()

    source_dir = Path(args.source_dir).expanduser().resolve()
    out = Path(args.out).expanduser().resolve()
    payload = build_staff_database(source_dir, recursive=args.recursive)

    if not payload["meta"]["files"]:
        print(f"No matching monthly .xlsx files found in {source_dir}. Output was not changed.")
        raise SystemExit(1)

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    periods_count = len(payload["periods"])
    stations_count = sum(period["stationsCount"] for period in payload["periods"].values())
    days_count = sum(period["daysCount"] for period in payload["periods"].values())
    print(f"Wrote {periods_count} periods, {stations_count} station-periods, {days_count} days to {out}")
    if payload["meta"]["skipped"]:
        print(f"Skipped {len(payload['meta']['skipped'])} files without expected structure")


if __name__ == "__main__":
    main()
