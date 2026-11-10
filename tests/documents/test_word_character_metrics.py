import tempfile
import unittest
from pathlib import Path

from docx import Document
from docx.enum.style import WD_STYLE_TYPE
from docx.oxml import OxmlElement
from docx.oxml.ns import qn

from magic_converter.core.documents.hwp_documents import read_docx, write_docx


def metric(properties, tag, value):
    node = OxmlElement('w:' + tag)
    node.set(qn('w:val'), str(value))
    properties.append(node)


class WordCharacterMetricTests(unittest.TestCase):
    def test_style_metrics_have_independent_precedence_and_explicit_zero(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'styles.docx'
            doc = Document()
            parent = doc.styles.add_style('ParentChar', WD_STYLE_TYPE.CHARACTER)
            metric(parent.element.get_or_add_rPr(), 'spacing', 24)
            metric(parent.element.get_or_add_rPr(), 'w', 120)
            child = doc.styles.add_style('ChildChar', WD_STYLE_TYPE.CHARACTER)
            child.base_style = parent
            paragraph_style = doc.styles.add_style('ParagraphMetrics', WD_STYLE_TYPE.PARAGRAPH)
            metric(paragraph_style.element.get_or_add_rPr(), 'spacing', 36)
            metric(paragraph_style.element.get_or_add_rPr(), 'w', 80)
            p = doc.add_paragraph(style=paragraph_style)
            a = p.add_run('Inherited'); a.style = child
            b = p.add_run('Zero'); b.style = child
            metric(b._r.get_or_add_rPr(), 'spacing', 0)
            c = p.add_run('Paragraph')
            doc.save(path)
            model = read_docx(path)
            self.assertEqual([(r.spacing_pt, r.width_percent) for r in model.blocks[0].runs],
                             [(1.2, 120), (0, 120), (1.8, 80)])
            returned = path.with_name('returned.docx')
            write_docx(model, returned)
            self.assertEqual([(r.spacing_pt, r.width_percent) for r in read_docx(returned).blocks[0].runs],
                             [(1.2, 120), (0, 120), (1.8, 80)])

    def test_defaults_apply_in_table_and_invalid_direct_value_blocks_inheritance(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'defaults.docx'
            doc = Document()
            defaults = doc.styles.element.find(qn('w:docDefaults')).find(qn('w:rPrDefault')).find(qn('w:rPr'))
            metric(defaults, 'spacing', 12)
            p = doc.add_table(rows=1, cols=1).cell(0, 0).paragraphs[0]
            p.add_run('Default')
            run = p.add_run('Invalid')
            metric(run._r.get_or_add_rPr(), 'spacing', 'bad')
            doc.save(path)
            data = read_docx(path)
            runs = data.blocks[0].cells[0].paragraphs[0].runs
            self.assertEqual([r.spacing_pt for r in runs], [.6, 0])
            self.assertTrue(any('고정 pt 자간' in warning for warning in data.warnings))

    def test_cyclic_character_style_is_bounded_and_warns(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'cycle.docx'
            doc = Document()
            a = doc.styles.add_style('CycleA', WD_STYLE_TYPE.CHARACTER)
            b = doc.styles.add_style('CycleB', WD_STYLE_TYPE.CHARACTER)
            a.base_style = b; b.base_style = a
            run = doc.add_paragraph().add_run('Bounded')
            run.style = a
            doc.save(path)
            data = read_docx(path)
            self.assertEqual(data.blocks[0].runs[0].text, 'Bounded')
            self.assertTrue(any('순환' in warning for warning in data.warnings))
