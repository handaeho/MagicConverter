import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from magic_converter.core.conversion.builtin_converters import create_catalog
from magic_converter.core.conversion.file_router import slots_for
from magic_converter.core.engines.office_com import _automatic_print_bounds, _print_area_cells, _verify_print_areas


def region(row, column, rows, columns):
    return SimpleNamespace(Row=row, Column=column, Rows=SimpleNamespace(Count=rows),
                           Columns=SimpleNamespace(Count=columns), CountLarge=rows * columns)


class ExcelPrintRouteTests(unittest.TestCase):
    def test_explicit_xlsx_route_uses_office_gate_and_default_pdf_keeps_its_action(self):
        catalog = create_catalog()
        route = catalog.assess('test.xlsx', 'office:xlsx_print_pdf').route
        self.assertTrue(route.uses_office_queue)
        self.assertEqual([step.action_key for step in route.steps], ['office:xlsx_print_pdf'])
        self.assertIsNone(catalog.assess('test.ods', 'office:xlsx_print_pdf').route)
        self.assertEqual(catalog.assess('test.xlsx', 'document:pdf').route.steps[0].action_key, 'document:pdf')
        self.assertIn('office:xlsx_print_pdf', {slot.action.key for slot in slots_for(['test.xlsx'], 'convert') if slot.enabled})
        self.assertNotIn('office:xlsx_print_pdf', {slot.action.key for slot in slots_for(['test.xlsx', 'test.ods'], 'convert')})

    def test_native_guard_requires_a_bounded_rectangular_area_on_each_visible_sheet(self):
        book = MagicMock()
        book.Sheets.Count = book.Worksheets.Count = 2
        book.Charts.Count = 0
        visible, hidden = MagicMock(), MagicMock()
        visible.Visible, hidden.Visible = -1, 0
        book.Worksheets.Item.side_effect = [visible, hidden]
        visible.PageSetup.PrintArea = '$A$1:$M$48'
        visible.PageSetup.PrintTitleRows = visible.PageSetup.PrintTitleColumns = ''
        visible.Range.return_value = region(1, 1, 48, 13)
        _verify_print_areas(book)
        hidden.Range.assert_not_called()
        book.Worksheets.Item.side_effect = None
        book.Worksheets.Item.return_value = visible
        book.Sheets.Count = book.Worksheets.Count = 1
        for area in ('$A$1:$B$4,$A$7:$B$10,$A$12:$B$14,$A$16:$B$18', 'A:A'):
            with self.subTest(area=area):
                visible.PageSetup.PrintArea = area
                with self.assertRaisesRegex(ValueError, '인쇄 영역'):
                    _verify_print_areas(book)
        visible.PageSetup.PrintArea = '$A$1:$K$3000'
        visible.Range.return_value = region(1, 1, 3000, 11)
        with self.assertRaisesRegex(ValueError, '30,000'):
            _verify_print_areas(book)
        visible.PageSetup.PrintArea = '$A$2:$F$20'
        visible.PageSetup.PrintTitleRows = '$21:$21'
        visible.Range.side_effect = {'$A$2:$F$20': region(2, 1, 19, 6), '$21:$21': region(21, 1, 1, 16384)}.__getitem__
        with self.assertRaisesRegex(ValueError, '반복 인쇄'):
            _verify_print_areas(book)
        book.Sheets.Count = 2
        with self.assertRaisesRegex(ValueError, '워크시트와 차트 시트'):
            _verify_print_areas(book)
        book.Sheets.Count = book.Worksheets.Count = 21
        with self.assertRaisesRegex(ValueError, '최대 20개'):
            _verify_print_areas(book)

    def test_disjoint_regions_and_shared_title_axes(self):
        book, sheet = MagicMock(), MagicMock()
        book.Sheets.Count = book.Worksheets.Count = 1
        book.Charts.Count = 0
        book.Worksheets.Item.return_value = sheet
        sheet.Visible = -1
        page = sheet.PageSetup
        page.PrintArea = '$B$2:$K$1001,$B$1100:$K$2099,$B$2200:$K$3199'
        page.PrintTitleRows = page.PrintTitleColumns = ''
        regions = {'$B$2:$K$1001': region(2, 2, 1000, 10), '$B$1100:$K$2099': region(1100, 2, 1000, 10),
                   '$B$1001:$K$2000': region(1001, 2, 1000, 10), '$B$1100:$K$2100': region(1100, 2, 1001, 10),
                   '$B$2200:$K$3199': region(2200, 2, 1000, 10),
                   '$2:$3': region(2, 1, 2, 16384), '$B:$C': region(1, 2, 1048576, 2),
                   '$A:$C': region(1, 1, 1048576, 3)}
        sheet.Range.side_effect = regions.__getitem__
        _verify_print_areas(book)
        page.PrintTitleRows = '$2:$3'
        with self.assertRaisesRegex(ValueError, '30,000'):
            _verify_print_areas(book)
        page.PrintTitleRows = ''
        page.PrintArea = '$B$2:$K$1001,$B$1100:$K$2100,$B$2200:$K$3199'
        with self.assertRaisesRegex(ValueError, '30,000'):
            _verify_print_areas(book)
        page.PrintArea = '$B$2:$K$1001,$B$1001:$K$2000'
        with self.assertRaisesRegex(ValueError, '겹칠'):
            _verify_print_areas(book)
        page.PrintArea = '$B$2:$K$1001'
        page.PrintTitleRows = '$2:$3'
        page.PrintTitleColumns = '$A:$C'
        _verify_print_areas(book)
        self.assertEqual(_print_area_cells((3, 2, 12, 8), ((1, 2), (1, 1))), 96)
        self.assertEqual(_print_area_cells((3, 2, 12, 8), ((2, 4), (1, 3))), 88)

    def test_automatic_bounds_must_cover_native_used_range(self):
        sheet, setup = MagicMock(), MagicMock()
        used = sheet.UsedRange
        used.Row, used.Column = 2, 2
        used.Rows.Count, used.Columns.Count = 4, 3
        used.MergeCells = False
        sheet.Shapes.Count = 0
        setup.PrintComments = -4142
        points = [SimpleNamespace(Row=2), SimpleNamespace(Row=5),
                  SimpleNamespace(Column=2), SimpleNamespace(Column=4)]
        used.Find.side_effect = points
        self.assertEqual(_automatic_print_bounds(sheet, setup), (2, 2, 5, 4))
        used.Find.side_effect = [None]
        with self.assertRaisesRegex(ValueError, '값이나 수식'):
            _automatic_print_bounds(sheet, setup)
        used.Find.side_effect = points
        used.Columns.Count = 6
        with self.assertRaisesRegex(ValueError, '직사각형 인쇄 영역'):
            _automatic_print_bounds(sheet, setup)
        used.Columns.Count = 16384
        with self.assertRaisesRegex(ValueError, '30,000'):
            _automatic_print_bounds(sheet, setup)
