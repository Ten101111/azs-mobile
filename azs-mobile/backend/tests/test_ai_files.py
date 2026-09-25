"""Файлы пользователя и память папки (ИИ-07, ИИ-11; решения Р-5, Р-6).

Файлы собираются в тесте: XLSX — openpyxl, PDF — reportlab, DOCX и PPTX —
минимальный OOXML вручную. Журнал и диалоги — во временном файле.
"""
from __future__ import annotations

import io
import pathlib
import tempfile
import unittest
import zipfile

from backend.ai import dialogs as store
from backend.ai import file_parse, files, journal, quotas


def xlsx_bytes(sheets: dict[str, list[list]]) -> bytes:
    from openpyxl import Workbook

    book = Workbook()
    book.remove(book.active)
    for title, rows in sheets.items():
        sheet = book.create_sheet(title)
        for row in rows:
            sheet.append(row)
    out = io.BytesIO()
    book.save(out)
    return out.getvalue()


def ooxml(kind: str, body: str, extra: dict[str, bytes] | None = None) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
        if kind == "docx":
            archive.writestr("word/document.xml",
                             '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
                             + body + "</w:body></w:document>")
        else:
            archive.writestr("ppt/presentation.xml", "<p:presentation xmlns:p='p'/>")
            archive.writestr("ppt/slides/slide1.xml",
                             '<p:sld xmlns:p="p" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
                             + body + "</p:sld>")
        for name, data in (extra or {}).items():
            archive.writestr(name, data)
    return out.getvalue()


def docx_bytes(paragraphs: list[str], table: list[list[str]] | None = None) -> bytes:
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    if table:
        body += "<w:tbl>" + "".join(
            "<w:tr>" + "".join(f"<w:tc><w:p><w:r><w:t>{c}</w:t></w:r></w:p></w:tc>" for c in row) + "</w:tr>"
            for row in table) + "</w:tbl>"
    return ooxml("docx", body)


def pdf_bytes(lines: list[str], encrypt: bool = False) -> bytes:
    from reportlab.pdfgen import canvas

    out = io.BytesIO()
    page = canvas.Canvas(out)
    y = 800
    for line in lines:
        page.drawString(72, y, line)
        y -= 20
    page.showPage()
    page.save()
    data = out.getvalue()
    if encrypt:
        from pypdf import PdfReader, PdfWriter

        writer = PdfWriter()
        for p in PdfReader(io.BytesIO(data)).pages:
            writer.add_page(p)
        writer.encrypt("secret")
        buf = io.BytesIO()
        writer.write(buf)
        data = buf.getvalue()
    return data


class ParseTests(unittest.TestCase):
    def test_xlsx_sheets_become_tables_with_row_numbers(self):
        data = xlsx_bytes({"Сентябрь": [["АЗС", "План НТУ, ₽"], ["77551", 1250000], ["50425", 980000.5]],
                           "Пустой": []})
        result = file_parse.parse_bytes("план.xlsx", data)
        self.assertEqual(result["kind"], "xlsx")
        table = result["parts"][0]
        self.assertEqual(table["label"], "лист «Сентябрь»")
        self.assertEqual(table["columns"], ["АЗС", "План НТУ, ₽"])
        self.assertEqual(table["rows"][0], ["77551", 1250000])
        self.assertEqual(table["firstRow"], 2)
        self.assertEqual(len(result["parts"]), 1)                       # пустой лист пропущен

    def test_csv_in_cp1251_with_russian_numbers(self):
        data = "АЗС;Выручка\n77551;1 234,5\n50425;980\n".encode("cp1251")
        table = file_parse.parse_bytes("выгрузка.csv", data)["parts"][0]
        self.assertEqual(table["rows"], [["77551", 1234.5], ["50425", 980]])

    def test_docx_text_and_tables(self):
        data = docx_bytes(["План мероприятий", "Пункт первый"], [["АЗС", "Срок"], ["77551", "сентябрь"]])
        parts = file_parse.parse_bytes("план.docx", data)["parts"]
        self.assertEqual(parts[0]["label"], "абзацы 1–2")
        self.assertIn("Пункт первый", parts[0]["text"])
        self.assertEqual(parts[1]["columns"], ["АЗС", "Срок"])

    def test_pptx_slides(self):
        data = ooxml("pptx", "<a:p><a:r><a:t>Итоги квартала</a:t></a:r></a:p>")
        parts = file_parse.parse_bytes("итоги.pptx", data)["parts"]
        self.assertEqual((parts[0]["label"], parts[0]["text"]), ("слайд 1", "Итоги квартала"))

    def test_pdf_pages_and_rejections(self):
        parts = file_parse.parse_bytes("report.pdf", pdf_bytes(["Plan 2026", "Revenue 1250"]))["parts"]
        self.assertEqual(parts[0]["label"], "стр. 1")
        self.assertIn("1250", parts[0]["text"])
        with self.assertRaisesRegex(file_parse.Rejected, "паролем"):
            file_parse.parse_bytes("secret.pdf", pdf_bytes(["x"], encrypt=True))

    def test_signature_macros_password_and_formats(self):
        with self.assertRaisesRegex(file_parse.Rejected, "не совпадает"):
            file_parse.detect("fake.pdf", b"PK\x03\x04 not a pdf")
        with self.assertRaisesRegex(file_parse.Rejected, "макрос"):
            file_parse.detect("book.xlsm", b"PK\x03\x04")
        with self.assertRaisesRegex(file_parse.Rejected, "макрос"):
            file_parse.detect("plan.docx", ooxml("docx", "<w:p/>", {"word/vbaProject.bin": b"x"}))
        with self.assertRaisesRegex(file_parse.Rejected, "паролем"):
            file_parse.detect("locked.xlsx", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\0" * 100)
        with self.assertRaisesRegex(file_parse.Rejected, "Поддерживаются"):
            file_parse.detect("photo.png", b"\x89PNG")
        with self.assertRaisesRegex(file_parse.Rejected, "20 МБ"):
            file_parse.detect("big.txt", b"a" * (file_parse.MAX_BYTES + 1))

    def test_zip_bomb_is_rejected(self):
        with self.assertRaisesRegex(file_parse.Rejected, "сжат"):
            file_parse.detect("bomb.docx", ooxml("docx", "<w:p/>", {"word/media/zero.bin": b"\0" * 12_000_000}))

    def test_isolated_process(self):
        result = file_parse.parse_isolated("t.csv", "a;b\n1;2\n".encode())
        self.assertEqual(result["parts"][0]["rows"], [[1, 2]])
        with self.assertRaises(file_parse.Rejected):
            file_parse.parse_isolated("scan.pdf", pdf_bytes([]))        # нет текстового слоя


class StoreCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._saved = (journal.JOURNAL_DB, store.JOURNAL_DB)
        path = pathlib.Path(self._tmp.name) / "journal.sqlite3"
        journal.JOURNAL_DB = path
        store.JOURNAL_DB = path
        quotas.set_overrides({})

    def tearDown(self):
        journal.JOURNAL_DB, store.JOURNAL_DB = self._saved
        quotas.set_overrides({})
        self._tmp.cleanup()

    @staticmethod
    def csv(n: int = 1) -> bytes:
        return f"АЗС;Выручка\n7755{n};{n * 100}\n".encode()

    def add(self, user=1, name="t.csv", data=None, **where):
        return files.add(user, "regional_manager", name, data or self.csv(), parse=file_parse.parse_bytes, **where)


class FileStoreTests(StoreCase):
    def test_dialog_files_limit_duplicates_and_owner(self):
        dialog = store.create_dialog(1, "Тест", role="regional_manager")["id"]
        first = self.add(dialog_id=dialog)
        self.assertEqual(first["summary"], "1 строка")
        self.assertTrue(self.add(dialog_id=dialog)["duplicate"])            # тот же файл — не дубль
        for n in range(2, 6):
            self.add(data=self.csv(n), dialog_id=dialog)
        with self.assertRaises(store.Limit) as caught:
            self.add(data=self.csv(9), dialog_id=dialog)
        self.assertEqual(caught.exception.param, "files_per_folder")
        with self.assertRaises(store.NotFound):
            self.add(user=2, data=self.csv(7), dialog_id=dialog)            # чужой диалог

    def test_user_volume_limit(self):
        quotas.set_overrides({("ru", "user_files_mb"): 0})
        with self.assertRaises(store.Limit) as caught:
            self.add()
        self.assertEqual(caught.exception.param, "user_files_mb")

    def test_drafts_link_to_new_dialog_and_leave_with_it(self):
        draft = self.add()
        self.assertTrue(draft["draft"])
        dialog = store.create_dialog(1, "Новый", role="regional_manager")["id"]
        self.assertEqual(files.link_drafts(1, [draft["id"]], dialog), 1)
        self.assertEqual([f["id"] for f in files.list_files(1, dialog_id=dialog)], [draft["id"]])
        store.delete_dialog(dialog, 1)
        self.assertEqual(files.list_files(1, ids=[draft["id"]]), [])
        conn = store._connect()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM ai_file_parts").fetchone()[0], 0)
        conn.close()

    def test_question_sees_dialog_folder_and_draft_files(self):
        folder = store.create_folder(1, "Совещание", role="regional_manager")["id"]
        dialog = store.create_dialog(1, "Вопрос", role="regional_manager")["id"]
        store.move_dialog(dialog, 1, folder)
        in_folder = self.add(data=self.csv(1), folder_id=folder)
        in_dialog = self.add(data=self.csv(2), dialog_id=dialog)
        draft = self.add(data=self.csv(3))
        found = files.for_question(1, dialog, [draft["id"]])
        self.assertEqual({f["id"] for f in found}, {in_folder["id"], in_dialog["id"], draft["id"]})
        self.assertEqual(found[0]["parts"][0]["kind"], "table")
        self.assertEqual(files.for_journal(found)[0].keys(), {"name", "kind", "size", "sha256", "place"})
        store.delete_folder(folder, 1, dialogs="move")
        self.assertEqual(files.list_files(1, folder_id=folder), [])

    def test_old_drafts_are_cleaned(self):
        draft = self.add()
        self.assertEqual(files.cleanup_drafts(now=draft["createdAt"] + files.DRAFT_TTL_S + 1), 1)


class FolderMemoryTests(StoreCase):
    def test_memory_history_limit_and_folder_delete(self):
        folder = store.create_folder(1, "НТУ", role="regional_manager")["id"]
        store.set_folder_memory(folder, 1, "Моё управление, сравнивай с прошлым годом", role="regional_manager")
        memory = store.set_folder_memory(folder, 1, "Фокус на НТУ", role="regional_manager")
        self.assertEqual(memory["text"], "Фокус на НТУ")
        self.assertEqual([h["text"] for h in memory["history"]], ["Фокус на НТУ", "Моё управление, сравнивай с прошлым годом"])
        self.assertEqual(memory["limit"], 2000)
        with self.assertRaises(store.Limit):
            store.set_folder_memory(folder, 1, "x" * 2001, role="regional_manager")
        with self.assertRaises(store.NotFound):
            store.folder_memory(folder, 2)
        dialog = store.create_dialog(1, "В папке", role="regional_manager")["id"]
        store.move_dialog(dialog, 1, folder)
        self.assertEqual(store.dialog_folder(dialog, 1)["memory"], "Фокус на НТУ")
        store.delete_folder(folder, 1)
        conn = store._connect()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM ai_folder_memory_log").fetchone()[0], 0)
        conn.close()


if __name__ == "__main__":
    unittest.main()
