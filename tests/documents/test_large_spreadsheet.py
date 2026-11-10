import hashlib
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import pymupdf as fitz
from openpyxl import Workbook

from magic_converter.core.documents.large_spreadsheet import large_workbook,stream_workbook_pdf
from magic_converter.core.jobs.job_control import JobCancelled


class LargeSpreadsheetTests(unittest.TestCase):
    def test_dense_shaped_pages_keep_long_values_across_lanes_and_continuations(self):
        from tools.validation.verify_large_workbook import source_contents,pdf_contents
        with tempfile.TemporaryDirectory()as folder:
            source=Path(folder)/'dense.xlsx';book=Workbook();book.active.title='Dense'
            for row in range(145):
                book.active.append(['مُناسب للقيادة؛ שלום עולם']*12+[str(row)])
            book.active.append(['M'*600,'हिन्दी भाषा '*100,'العربية '*100])
            book.save(source);book.close();target=source.with_suffix('.pdf')
            stream_workbook_pdf(source,target)
            expected=source_contents(source);actual=pdf_contents(target,Path(folder))
            self.assertGreater(actual['pages'],3)
            self.assertEqual(actual['cells'],expected['cells'])
            self.assertEqual(actual['content_sha256'],expected['content_sha256'])

    def test_adaptive_columns_reduce_short_cell_pages_without_content_loss(self):
        from tools.validation.verify_large_workbook import source_contents, pdf_contents
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / 'short.xlsx'
            book = Workbook()
            for row in range(256):
                book.active.append([row * 13 + column for column in range(13)])
            book.save(source)
            book.close()
            expected = source_contents(source)
            pages = []
            for columns in (3, 6):
                target = root / f'columns-{columns}.pdf'
                with patch('magic_converter.core.documents.large_spreadsheet.COLUMNS_PER_PANEL', columns):
                    result = stream_workbook_pdf(source, target)
                actual = pdf_contents(target, root)
                self.assertEqual(actual['cells'], expected['cells'])
                self.assertEqual(actual['content_sha256'], expected['content_sha256'])
                pages.append(result['pages'])
            self.assertLessEqual(pages[1], (pages[0] + 1) // 2)

    def test_shaped_pages_follow_the_requested_column_count(self):
        from tools.validation.verify_large_workbook import source_contents, pdf_contents
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / 'shaped-columns.xlsx'
            book = Workbook()
            for row in range(160):
                book.active.append(['مُناسب للقيادة', 'שלום עולם', 'हिन्दी भाषा', f'한글{row}'])
            book.save(source)
            book.close()
            expected = source_contents(source)
            for columns in (1, 2, 4, 6):
                with self.subTest(columns=columns):
                    target = root / f'shaped-{columns}.pdf'
                    with patch('magic_converter.core.documents.large_spreadsheet.COLUMNS_PER_PANEL', columns):
                        stream_workbook_pdf(source, target)
                    actual = pdf_contents(target, root)
                    self.assertEqual(actual['content_sha256'], expected['content_sha256'])
                    self.assertEqual(actual['cells'], expected['cells'])
                    self.assertEqual(actual['actualtext_pages'], actual['pages'])

    def test_shaped_scripts_keep_exact_logical_unicode_after_subset_and_chunk_merge(self):
        from tools.validation.verify_large_workbook import source_contents,pdf_contents
        with tempfile.TemporaryDirectory()as folder:
            source=Path(folder)/'scripts.xlsx';book=Workbook();book.active.title='بيانات'
            values=['الفولاذ الكرومولي','فناجين من سبيكة الألومنيوم؛ ذات محور دوران كبير القطر.',
                    'مُناسب للقيادة، سريع وآمن.','שלום עולם','हिन्दी भाषा','ไทย ภาษา','Cafe\u0301',
                    'Mixed العربية English 123 שלום','<script>& original text']
            for row in range(130):book.active.append([values[row%len(values)],f'Row {row+1}'])
            book.save(source);book.close();target=source.with_suffix('.pdf')
            with patch('magic_converter.core.documents.large_spreadsheet.ROWS_PER_BATCH',1):
                result=stream_workbook_pdf(source,target)
            expected=source_contents(source);actual=pdf_contents(target,Path(folder))
            self.assertEqual(result['rows'],130)
            self.assertEqual(actual['cells'],expected['cells'])
            self.assertEqual(actual['content_sha256'],expected['content_sha256'])
            self.assertEqual(actual['actualtext_pages'],130)

    def test_shaped_page_requires_complete_valid_actualtext_coverage(self):
        from tools.validation.verify_large_workbook import pdf_contents
        with tempfile.TemporaryDirectory()as folder:
            source=Path(folder)/'scripts.xlsx';book=Workbook();book.active.append(['العربية'])
            book.save(source);book.close();target=source.with_suffix('.pdf')
            stream_workbook_pdf(source,target)
            changed=Path(folder)/'partial.pdf'
            with fitz.open(target)as pdf:
                page=pdf[0];page.insert_text((30,590),'Outside replacement span')
                pdf.save(changed)
            with self.assertRaisesRegex(ValueError,'partially covered'):
                pdf_contents(changed,Path(folder))

    def source(self,folder):
        source=Path(folder)/'source.xlsx'
        book=Workbook();book.active.title='첫째'
        for sheet in (book.active,book.create_sheet('Second')):
            for row in range(1,8):
                sheet.append([f'{sheet.title}R{row}C{col}'for col in range(1,5)])
        book.worksheets[-1].cell(8,1,'=SUM(1,2)')
        book.save(source);book.close();return source

    def test_all_sheets_last_rows_columns_unicode_and_formula_survive_bounded_batches(self):
        with tempfile.TemporaryDirectory()as folder:
            source=self.source(folder);before=hashlib.sha256(source.read_bytes()).digest()
            target=source.with_suffix('.pdf')
            with patch('magic_converter.core.documents.large_spreadsheet.ROWS_PER_BATCH',3),patch(
                    'magic_converter.core.documents.large_spreadsheet.COLUMNS_PER_PANEL',2):
                result=stream_workbook_pdf(source,target)
            self.assertEqual((result['sheets'],result['rows'],result['cells'],result['formulas']),(2,15,57,1))
            with fitz.open(target)as pdf:
                text=''.join(''.join(page.get_text().split())for page in pdf)
                self.assertEqual(len(pdf),result['pages'])
            for sheet in ('첫째','Second'):
                for row in range(1,8):
                    for col in range(1,5):self.assertIn(f'{sheet}R{row}C{col}',text)
            self.assertIn('=SUM(1,2)',text)
            self.assertEqual(hashlib.sha256(source.read_bytes()).digest(),before)

    def test_cancellation_after_written_pages_removes_incomplete_pdf(self):
        with tempfile.TemporaryDirectory()as folder:
            source=self.source(folder);target=source.with_suffix('.pdf')
            with patch('magic_converter.core.documents.large_spreadsheet.ROWS_PER_BATCH',1),patch(
                    'magic_converter.core.documents.large_spreadsheet.report_stage',side_effect=JobCancelled()):
                with self.assertRaises(JobCancelled):stream_workbook_pdf(source,target)
            self.assertFalse(target.exists());self.assertTrue(source.exists())

    def test_unbroken_long_cells_continue_without_off_page_text_or_truncation(self):
        with tempfile.TemporaryDirectory()as folder:
            source=Path(folder)/'source.xlsx';book=Workbook()
            book.active.append(['M'*600,'한글'*200]);book.save(source);book.close()
            target=source.with_suffix('.pdf');stream_workbook_pdf(source,target)
            with fitz.open(target)as pdf:
                text=''.join(''.join(page.get_text().split())for page in pdf)
                self.assertEqual(text.count('M'),600)
                self.assertEqual(text.count('한'),200)
                self.assertEqual(text.count('글'),200)
                for page in pdf:
                    for block in page.get_text('blocks'):
                        self.assertLessEqual(block[2],page.rect.width)
            target.replace(Path(folder)/'published.pdf')

    def test_page_limit_failure_removes_incomplete_pdf(self):
        with tempfile.TemporaryDirectory()as folder:
            source=self.source(folder);target=source.with_suffix('.pdf')
            with patch('magic_converter.core.documents.large_spreadsheet.ROWS_PER_BATCH',1),patch(
                    'magic_converter.core.documents.large_spreadsheet.MAX_PAGES',1):
                with self.assertRaises(ValueError):stream_workbook_pdf(source,target)
            self.assertFalse(target.exists())

    def test_incremental_chunk_merge_keeps_all_page_text_and_releases_files(self):
        with tempfile.TemporaryDirectory()as folder:
            source=Path(folder)/'source.xlsx';book=Workbook()
            for row in range(1,131):book.active.append([f'Row{row}'])
            book.save(source);book.close();target=source.with_suffix('.pdf')
            with (
                patch('magic_converter.core.documents.large_spreadsheet.ROWS_PER_BATCH', 1),
                patch('magic_converter.core.documents.large_spreadsheet.PAGES_PER_CHUNK', 16),
            ):
                result=stream_workbook_pdf(source,target)
            with fitz.open(target)as pdf:
                self.assertEqual(len(pdf),130)
                self.assertIn('Row130',pdf[-1].get_text())
            self.assertEqual(result['rows'],130)
            target.replace(Path(folder)/'published.pdf')
            self.assertFalse(any(p.is_dir()for p in Path(folder).iterdir()))

    def test_incremental_merge_failure_closes_the_combined_pdf_before_cleanup(self):
        with tempfile.TemporaryDirectory() as folder:
            source = self.source(folder)
            target = source.with_suffix('.pdf')
            with (
                patch('magic_converter.core.documents.large_spreadsheet.ROWS_PER_BATCH', 1),
                patch('magic_converter.core.documents.large_spreadsheet.PAGES_PER_CHUNK', 1),
                patch.object(fitz.Document, 'saveIncr', side_effect=OSError('failed increment')),
                self.assertRaisesRegex(OSError, 'failed increment'),
            ):
                stream_workbook_pdf(source, target)
            self.assertFalse(target.exists())
            self.assertFalse(any(p.is_dir() for p in Path(folder).iterdir()))

    def test_compact_sparse_workbook_uses_large_route_and_wrong_dimension_never_hides_cells(self):
        with tempfile.TemporaryDirectory()as folder:
            source=Path(folder)/'sparse.xlsx';book=Workbook()
            book.active['A1']='First';book.active['T100001']='마지막Final'
            book.save(source);book.close()
            self.assertTrue(large_workbook(source))
            changed=source.with_name('wrong-dimension.xlsx')
            import re
            with zipfile.ZipFile(source)as original,zipfile.ZipFile(changed,'w')as output:
                for item in original.infolist():
                    data=original.read(item)
                    if item.filename=='xl/worksheets/sheet1.xml':
                        data=re.sub(rb'<dimension ref="[^"]+"/>',b'<dimension ref="A1"/>',data)
                    output.writestr(item,data)
            result=stream_workbook_pdf(changed,changed.with_suffix('.pdf'))
            self.assertEqual((result['rows'],result['cells']),(2,2))
            with fitz.open(changed.with_suffix('.pdf'))as pdf:
                text=''.join(page.get_text()for page in pdf)
                self.assertIn('T100001',text);self.assertIn('마지막Final',text)
