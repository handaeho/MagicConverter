import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from lxml import etree
from magic_converter.core.documents.hwp_documents import DocumentData, Paragraph, TextRun, write_hwpx
from magic_converter.core.engines.rhwp_roman import normalize, available_replacements, H, P


class RomanFallbackTests(unittest.TestCase):
    def document(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'control.hwpx'
            write_hwpx(DocumentData([Paragraph([TextRun('ABC Ⅰ Ⅹ ⅪⅫ 가나다', font='굴림', size=14, bold=True)])]), source)
            with zipfile.ZipFile(source) as archive:
                return archive.read('Contents/header.xml'), {'Contents/section0.xml': archive.read('Contents/section0.xml')}

    def test_only_missing_roman_segments_change_font_and_style_metrics_remain(self):
        header, sections = self.document()
        changed, output, edits = normalize(header, sections, {'굴림': ('Segoe UI Symbol', set('ⅪⅫ'))})
        self.assertEqual(len(edits), 1)
        before, after = etree.fromstring(sections['Contents/section0.xml']), etree.fromstring(output['Contents/section0.xml'])
        self.assertEqual(''.join(before.itertext()), ''.join(after.itertext()))
        runs = list(after.iter(f'{{{P}}}run'))
        romans = next(run for run in runs if run[0].text == 'ⅪⅫ')
        unchanged = next(run for run in runs if run[0].text == 'ABC Ⅰ Ⅹ ')
        self.assertNotEqual(romans.get('charPrIDRef'), unchanged.get('charPrIDRef'))
        styles = {node.get('id'): node for node in etree.fromstring(changed).iter(f'{{{H}}}charPr')}
        a, b = (styles[run.get('charPrIDRef')] for run in (romans, unchanged))
        self.assertEqual({k:v for k,v in a.attrib.items() if k != 'id'}, {k:v for k,v in b.attrib.items() if k != 'id'})
        self.assertEqual([etree.tostring(n) for n in a if not n.tag.endswith('}fontRef')],
                         [etree.tostring(n) for n in b if not n.tag.endswith('}fontRef')])

    def test_no_available_replacement_or_uncovered_character_keeps_original_bytes(self):
        header, sections = self.document()
        for mapping in ({}, {'unrelated': ('Segoe UI Symbol', set('ⅪⅫ'))}, {'굴림': ('Segoe UI Symbol', {'x'})}):
            with self.subTest(mapping=mapping):
                self.assertEqual(normalize(header, sections, mapping), (header, sections, []))

    def test_control_children_and_mixed_font_slots_are_not_rewritten(self):
        header, sections = self.document()
        root = etree.fromstring(sections['Contents/section0.xml'])
        run = next(node for node in root.iter(f'{{{P}}}run') if 'Ⅺ' in ''.join(node.itertext()))
        etree.SubElement(run, f'{{{P}}}ctrl')
        mixed = {'Contents/section0.xml': etree.tostring(root)}
        self.assertFalse(normalize(header, mixed, {'굴림': ('Segoe UI Symbol', set('ⅪⅫ'))})[2])
        head = etree.fromstring(header)
        style_id = run.get('charPrIDRef')
        style = next(node for node in head.iter(f'{{{H}}}charPr') if node.get('id') == style_id)
        ref = style.find(f'{{{H}}}fontRef'); ref.set('symbol', '99999')
        self.assertFalse(normalize(etree.tostring(head), sections, {'굴림': ('Segoe UI Symbol', set('ⅪⅫ'))})[2])

    def test_installed_original_coverage_is_required_before_replacement(self):
        with patch('magic_converter.core.pdf.pdf_fonts.resolve_font', return_value=None):
            self.assertEqual(available_replacements(), {})

    def test_large_alternating_run_cannot_exceed_split_budget_or_emit_partial_text(self):
        header, sections = self.document()
        root = etree.fromstring(sections['Contents/section0.xml'])
        text = next(node for node in root.iter(f'{{{P}}}t') if 'Ⅺ' in (node.text or ''))
        text.text = 'AⅪ' * 3000
        large = {'Contents/section0.xml': etree.tostring(root)}
        self.assertEqual(normalize(header, large, {'굴림': ('Segoe UI Symbol', set('ⅪⅫ'))}), (header, large, []))


if __name__ == '__main__':
    unittest.main()
