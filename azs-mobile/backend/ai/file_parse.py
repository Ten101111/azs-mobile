"""Разбор файлов пользователя для ИИ-аналитика (ИИ-07, решение Р-5).

Шесть форматов: PDF, DOCX, XLSX, CSV, TXT, PPTX, до 20 МБ. Файлы с макросами
(xlsm, docm, pptm, vbaProject внутри) и защищённые паролем не принимаются.
Расширение сверяется с сигнатурой: PDF — «%PDF-», OOXML — zip с нужными
частями; зашифрованный OOXML — это контейнер OLE, его сигнатура D0CF11E0.

Результат — части файла:
  * таблица: лист XLSX, CSV, таблица DOCX — колонки, строки и номер первой
    строки данных в исходнике (для ссылки «лист «Сентябрь», строки 2–40»);
  * текст: страница PDF, слайд PPTX, фрагмент DOCX или TXT с подписью места.

Разбор идёт в отдельном процессе (parse_isolated) без переменных окружения,
с пределами времени, процессора и памяти; внутри процесса — пределы на
размер распаковки OOXML (защита от zip-бомб), число страниц, листов и строк.
Содержимое файла — данные: ничего из него не исполняется.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

KINDS = ("pdf", "docx", "xlsx", "csv", "txt", "pptx")
KIND_TITLES = {"pdf": "PDF", "docx": "Word", "xlsx": "Excel", "csv": "CSV", "txt": "Текст", "pptx": "PowerPoint"}
MACRO_EXTENSIONS = {"xlsm", "xltm", "docm", "dotm", "pptm", "potm", "xlsb", "xls", "doc", "ppt"}
MAX_BYTES = 20 * 1024 * 1024                 # Р-5
MAX_UNZIPPED = 200 * 1024 * 1024             # распакованный OOXML
MAX_RATIO = 120                              # сжатие больше — похоже на zip-бомбу
MAX_PDF_PAGES = 300
MAX_SHEETS = 20
MAX_ROWS = 20_000                            # строк в одной таблице
MAX_COLUMNS = 200
MAX_TEXT = 2_000_000                         # знаков текста на файл
CHUNK = 1500                                 # знаков в текстовом фрагменте

PROJECT_ROOT = Path(__file__).resolve().parents[2]
TIMEOUT_S = float(os.environ.get("AI_FILE_PARSE_TIMEOUT", "60"))
CPU_S = 60
MEMORY_MB = 1536


class Rejected(Exception):
    """Файл не принят — причина понятна человеку."""


# --- формат ----------------------------------------------------------------------

def extension(name: str) -> str:
    return (Path(name or "").suffix or "").lower().lstrip(".")


def detect(name: str, data: bytes) -> str:
    """Вид файла по расширению, сверенный с содержимым."""
    ext = extension(name)
    if ext in MACRO_EXTENSIONS:
        raise Rejected("Файлы с макросами и старые форматы Office (xls, doc, ppt) не принимаются — "
                       "сохраните файл как xlsx, docx или pptx.")
    if ext not in KINDS:
        raise Rejected("Поддерживаются PDF, DOCX, XLSX, CSV, TXT и PPTX.")
    if not data:
        raise Rejected("Файл пустой.")
    if len(data) > MAX_BYTES:
        raise Rejected(f"Файл больше {MAX_BYTES // (1024 * 1024)} МБ.")
    if data[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        raise Rejected("Файл защищён паролем или сохранён в старом формате Office — снимите пароль и сохраните заново.")
    if ext == "pdf":
        if not data.lstrip()[:5] == b"%PDF-":
            raise Rejected("Это не PDF: содержимое не совпадает с расширением.")
    elif ext in ("docx", "xlsx", "pptx"):
        if data[:4] != b"PK\x03\x04":
            raise Rejected(f"Это не {ext.upper()}: содержимое не совпадает с расширением.")
        _check_ooxml(ext, data)
    else:
        if b"\x00" in data[:4096]:
            raise Rejected("Это не текстовый файл: внутри двоичные данные.")
    return ext


def _check_ooxml(ext: str, data: bytes) -> None:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as err:
        raise Rejected("Файл повреждён: архив OOXML не читается.") from err
    names = set(archive.namelist())
    main = {"docx": "word/document.xml", "xlsx": "xl/workbook.xml", "pptx": "ppt/presentation.xml"}[ext]
    if "[Content_Types].xml" not in names or main not in names:
        raise Rejected(f"Это не {ext.upper()}: внутри нет нужных частей.")
    if any(n.lower().endswith("vbaproject.bin") for n in names):
        raise Rejected("В файле есть макросы — такие файлы не принимаются.")
    total = 0
    for info in archive.infolist():
        total += info.file_size
        if info.compress_size and info.file_size / max(info.compress_size, 1) > MAX_RATIO and info.file_size > 10_000_000:
            raise Rejected("Файл подозрительно сильно сжат — разбирать его небезопасно.")
    if total > MAX_UNZIPPED:
        raise Rejected("Файл слишком большой в распакованном виде.")


# --- значения ----------------------------------------------------------------------

NUMBER_RE = re.compile(r"^[+-]?\d{1,3}(?:[   ]\d{3})*(?:[.,]\d+)?$|^[+-]?\d+(?:[.,]\d+)?$")
# Колонки-идентификаторы: номер АЗС, КССС, код, ИНН — цифры в них остаются текстом.
ID_COLUMN_RE = re.compile(r"азс|кссс|ksss|номер|код|\bid\b|инн|телефон", re.I)
LEADING_ZERO_RE = re.compile(r"^[+-]?0\d")


def cell(value: Any, numeric_text: bool = False) -> Any:
    """Значение ячейки для набора строк: числа — числами, даты — ISO, текст — без лишних пробелов.

    numeric_text — текст вида «1 234,5» превращается в число (CSV, таблицы DOCX); в XLSX
    числа уже числа, а текстовые ячейки остаются текстом. «0123» — всегда текст.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, (dt.datetime, dt.date)):
        if isinstance(value, dt.datetime) and value.time() == dt.time(0, 0):
            return value.date().isoformat()
        return value.isoformat()
    text = " ".join(str(value).split())
    if not text:
        return None
    if numeric_text and NUMBER_RE.match(text) and not LEADING_ZERO_RE.match(text):
        clean = re.sub(r"[   ]", "", text).replace(",", ".")
        try:
            number = float(clean)
            return int(number) if number.is_integer() and "." not in clean else number
        except ValueError:
            return text
    return text[:2000]


def _header(values: list[Any], width: int) -> list[str]:
    names, seen = [], {}
    for index in range(width):
        raw = values[index] if index < len(values) else None
        name = " ".join(str(raw).split()) if raw not in (None, "") else f"Колонка {index + 1}"
        count = seen.get(name, 0)
        seen[name] = count + 1
        names.append(name if not count else f"{name} ({count + 1})")
    return names


def _table(label: str, raw_rows: list[list[Any]], first_row_number: int, numeric_text: bool = False) -> dict | None:
    """Первая непустая строка — шапка, дальше — данные; пустые хвосты обрезаются."""
    rows = [[cell(v) for v in row[:MAX_COLUMNS]] for row in raw_rows]
    start = next((i for i, row in enumerate(rows) if any(v not in (None, "") for v in row)), None)
    if start is None:
        return None
    width = max((max((i + 1 for i, v in enumerate(row) if v not in (None, "")), default=0) for row in rows), default=0)
    if not width:
        return None
    header = _header(rows[start], width)
    body = [(row + [None] * width)[:width] for row in rows[start + 1:]]
    while body and not any(v not in (None, "") for v in body[-1]):
        body.pop()
    if numeric_text:
        for index, name in enumerate(header):
            if ID_COLUMN_RE.search(name):
                continue
            for row in body:
                row[index] = cell(row[index], numeric_text=True)
    truncated = len(body) > MAX_ROWS
    return {"kind": "table", "label": label, "columns": header, "rows": body[:MAX_ROWS],
            "firstRow": first_row_number + start + 2, "truncated": truncated}


def _chunks(label_of, pieces: list[tuple[int, str]]) -> list[dict]:
    """Склеить куски текста в фрагменты ~CHUNK знаков с подписью диапазона."""
    parts, buf, first, last, total = [], [], None, None, 0
    for number, text in pieces:
        text = text.strip()
        if not text:
            continue
        if buf and sum(len(t) for t in buf) + len(text) > CHUNK:
            parts.append({"kind": "text", "label": label_of(first, last), "text": "\n".join(buf)})
            buf, first = [], None
        if first is None:
            first = number
        last = number
        buf.append(text[:MAX_TEXT])
        total += len(text)
        if total > MAX_TEXT:
            break
    if buf:
        parts.append({"kind": "text", "label": label_of(first, last), "text": "\n".join(buf)})
    return parts


def _range(word: str):
    return lambda a, b: f"{word} {a}" if a == b else f"{word} {a}–{b}"


# --- форматы ------------------------------------------------------------------------

def parse_xlsx(data: bytes) -> dict:
    from openpyxl import load_workbook

    book = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    parts, notes = [], []
    for index, sheet in enumerate(book.worksheets):
        if index >= MAX_SHEETS:
            notes.append(f"прочитаны первые {MAX_SHEETS} листов")
            break
        raw = []
        for row in sheet.iter_rows(values_only=True):
            raw.append(list(row))
            if len(raw) > MAX_ROWS + 50:
                break
        table = _table(f"лист «{sheet.title}»", raw, 0)
        if table:
            if table["truncated"]:
                notes.append(f"лист «{sheet.title}»: прочитаны первые {MAX_ROWS} строк")
            parts.append(table)
    book.close()
    return {"parts": parts, "meta": {"sheets": len(book.sheetnames)}, "notes": notes}


def _decode(data: bytes) -> str:
    for encoding in ("utf-8-sig", "cp1251"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def parse_csv(data: bytes) -> dict:
    text = _decode(data)
    sample = text[:20_000]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=";,\t|")
        delimiter = dialect.delimiter
    except csv.Error:
        delimiter = ";" if sample.count(";") >= sample.count(",") else ","
    raw = []
    for row in csv.reader(io.StringIO(text), delimiter=delimiter):
        raw.append(row)
        if len(raw) > MAX_ROWS + 50:
            break
    table = _table("таблица", raw, 0, numeric_text=True)
    notes = [f"прочитаны первые {MAX_ROWS} строк"] if table and table["truncated"] else []
    return {"parts": [table] if table else [], "meta": {"delimiter": delimiter}, "notes": notes}


def parse_txt(data: bytes) -> dict:
    lines = _decode(data).splitlines()
    return {"parts": _chunks(_range("строки"), list(enumerate(lines, start=1))), "meta": {"lines": len(lines)}, "notes": []}


W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"


def _xml(archive: zipfile.ZipFile, name: str) -> ET.Element:
    return ET.fromstring(archive.read(name))


def parse_docx(data: bytes) -> dict:
    archive = zipfile.ZipFile(io.BytesIO(data))
    body = _xml(archive, "word/document.xml").find(f"{W}body")
    pieces, parts, number, tables = [], [], 0, 0
    for block in list(body) if body is not None else []:
        if block.tag == f"{W}p":
            number += 1
            pieces.append((number, "".join(t.text or "" for t in block.iter(f"{W}t"))))
        elif block.tag == f"{W}tbl":
            tables += 1
            rows = [["".join(t.text or "" for t in tc.iter(f"{W}t")) for tc in tr.iter(f"{W}tc")]
                    for tr in block.iter(f"{W}tr")]
            table = _table(f"таблица {tables}", rows, 0, numeric_text=True)
            if table:
                parts.append(table)
    return {"parts": _chunks(_range("абзацы"), pieces) + parts, "meta": {"paragraphs": number, "tables": tables},
            "notes": []}


def parse_pptx(data: bytes) -> dict:
    archive = zipfile.ZipFile(io.BytesIO(data))
    slides = sorted((n for n in archive.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)),
                    key=lambda n: int(re.search(r"(\d+)\.xml$", n).group(1)))
    parts = []
    for index, name in enumerate(slides, start=1):
        root = _xml(archive, name)
        lines = []
        for paragraph in root.iter(f"{A}p"):
            line = "".join(t.text or "" for t in paragraph.iter(f"{A}t")).strip()
            if line:
                lines.append(line)
        if lines:
            parts.append({"kind": "text", "label": f"слайд {index}", "text": "\n".join(lines)[:MAX_TEXT]})
    return {"parts": parts, "meta": {"slides": len(slides)}, "notes": []}


def parse_pdf(data: bytes) -> dict:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    if reader.is_encrypted:
        raise Rejected("PDF защищён паролем — снимите защиту и загрузите снова.")
    pages = len(reader.pages)
    parts, notes, total = [], [], 0
    for index, page in enumerate(reader.pages, start=1):
        if index > MAX_PDF_PAGES:
            notes.append(f"прочитаны первые {MAX_PDF_PAGES} страниц")
            break
        text = (page.extract_text() or "").strip()
        if text:
            parts.append({"kind": "text", "label": f"стр. {index}", "text": text[:MAX_TEXT]})
            total += len(text)
            if total > MAX_TEXT:
                notes.append("текст обрезан по пределу объёма")
                break
    if not parts:
        raise Rejected("В PDF нет текстового слоя (похоже на скан) — распознавание текста пока не поддерживается.")
    return {"parts": parts, "meta": {"pages": pages}, "notes": notes}


PARSERS = {"xlsx": parse_xlsx, "csv": parse_csv, "txt": parse_txt, "docx": parse_docx,
           "pptx": parse_pptx, "pdf": parse_pdf}


def parse_bytes(name: str, data: bytes) -> dict:
    """Разбор в текущем процессе: {"kind", "parts", "meta", "notes"} или Rejected."""
    kind = detect(name, data)
    try:
        result = PARSERS[kind](data)
    except Rejected:
        raise
    except Exception as err:  # noqa: BLE001 - повреждённый файл: причина человеку, без трассировки
        raise Rejected(f"Файл не удалось прочитать: {type(err).__name__}.") from err
    if not result["parts"]:
        raise Rejected("В файле не нашлось ни текста, ни таблиц.")
    result["kind"] = kind
    return result


# --- изолированный разбор -------------------------------------------------------------

RUNNER = r'''
import json, sys
root, src, dst, cpu, mem = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4]), int(sys.argv[5])
try:
    import resource
    resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
    try:
        resource.setrlimit(resource.RLIMIT_AS, (mem * 1024 * 1024, mem * 1024 * 1024))
    except (ValueError, OSError):
        pass
except ImportError:
    pass
sys.path.insert(0, root)
from backend.ai import file_parse
payload = json.load(open(src, encoding="utf-8"))
data = open(payload["path"], "rb").read()
try:
    out = {"ok": True, "result": file_parse.parse_bytes(payload["name"], data)}
except file_parse.Rejected as err:
    out = {"ok": False, "error": str(err)}
with open(dst, "w", encoding="utf-8") as fh:
    json.dump(out, fh, ensure_ascii=False, default=str)
'''


def parse_isolated(name: str, data: bytes, timeout_s: float | None = None) -> dict:
    """Разбор в отдельном процессе без сети и окружения. Rejected — если файл не принят."""
    detect(name, data)                      # быстрый отказ до запуска процесса
    with tempfile.TemporaryDirectory(prefix="ai-file-") as tmp:
        folder = Path(tmp)
        (folder / "runner.py").write_text(RUNNER, encoding="utf-8")
        (folder / "file.bin").write_bytes(data)
        (folder / "in.json").write_text(json.dumps({"name": name, "path": str(folder / "file.bin")}), encoding="utf-8")
        out = folder / "out.json"
        env = {"PATH": os.environ.get("PATH", ""), "LANG": "C.UTF-8", "PYTHONIOENCODING": "utf-8",
               "HOME": tmp, "TMPDIR": tmp}
        started = time.monotonic()
        try:
            proc = subprocess.run([sys.executable, "-I", str(folder / "runner.py"), str(PROJECT_ROOT),
                                   str(folder / "in.json"), str(out), str(CPU_S), str(MEMORY_MB)],
                                  capture_output=True, text=True, timeout=timeout_s or TIMEOUT_S, env=env, cwd=tmp)
        except subprocess.TimeoutExpired as err:
            raise Rejected("Файл разбирался слишком долго — возможно, он слишком большой или сложный.") from err
        if not out.exists():
            raise Rejected("Файл не удалось прочитать: разбор завершился с ошибкой.")
        payload = json.loads(out.read_text(encoding="utf-8"))
    if not payload.get("ok"):
        raise Rejected(payload.get("error") or "Файл не удалось прочитать.")
    result = payload["result"]
    result["elapsedMs"] = int((time.monotonic() - started) * 1000)
    return result
