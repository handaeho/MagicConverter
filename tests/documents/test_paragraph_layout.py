import tempfile
import unittest
from pathlib import Path

from docx import Document
from docx.shared import Pt
from magic_converter.core.documents.hwp_documents import (
    DocumentData, Paragraph, TextRun, read_docx, read_hwpx, write_docx, write_hwpx)


class ParagraphLayoutTests(unittest.TestCase):
    def test_authored_indent_and_spacing_survive_docx_hwpx_roundtrip(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);source=root/'source.docx'
            doc=Document();p=doc.add_paragraph('Authored origin and spacing')
            f=p.paragraph_format
            f.left_indent=Pt(18);f.right_indent=Pt(9);f.first_line_indent=Pt(-6)
            f.space_before=Pt(12);f.space_after=Pt(8)
            doc.save(source)
            data=read_docx(source);middle=root/'middle.hwpx';write_hwpx(data,middle)
            back=read_hwpx(middle);target=root/'returned.docx';write_docx(back,target)
            f=Document(target).paragraphs[0].paragraph_format
            for field,value in [('left_indent',18),('right_indent',9),('first_line_indent',-6),('space_before',12),('space_after',8)]:
                with self.subTest(field=field):
                    self.assertIsNotNone(getattr(f,field));self.assertAlmostEqual(getattr(f,field).pt,value,places=2)
            self.assertEqual(back.text(),'Authored origin and spacing')

    def test_inherited_word_layout_is_kept_and_direct_zero_overrides_style(self):
        with tempfile.TemporaryDirectory() as folder:
            source=Path(folder)/'inherited.docx';doc=Document()
            style=doc.styles['Normal'].paragraph_format
            style.left_indent=Pt(24);style.space_after=Pt(7)
            doc.add_paragraph('Inherited');p=doc.add_paragraph('Direct zero');p.paragraph_format.left_indent=Pt(0)
            doc.save(source);data=read_docx(source)
            self.assertEqual(data.blocks[0].left_indent,24)
            self.assertEqual(data.blocks[0].space_after,7)
            self.assertEqual(data.blocks[1].left_indent,0)

    def test_default_model_stays_compatible_and_audit_detects_layout_loss(self):
        from magic_converter.core.documents.document_audit import compare
        a=DocumentData([Paragraph([TextRun('Control')])])
        b=DocumentData([Paragraph([TextRun('Control')])])
        self.assertEqual(compare(a,b),[])
        a.blocks[0].left_indent=12
        self.assertTrue(any('문단' in warning for warning in compare(a,b)))

    def test_invalid_hwpx_optional_unit_keeps_text_and_reports_loss(self):
        import zipfile
        from xml.etree import ElementTree as ET
        from magic_converter.core.documents.hwp_documents import q
        with tempfile.TemporaryDirectory()as folder:
            source=Path(folder)/'bad-unit.hwpx'
            write_hwpx(DocumentData([Paragraph([TextRun('Readable')],left_indent=18)]),source)
            with zipfile.ZipFile(source)as z:files={n:z.read(n)for n in z.namelist()}
            header=ET.fromstring(files['Contents/header.xml'])
            for node in header.iter(q('hc','left')):node.set('unit','UNKNOWN')
            files['Contents/header.xml']=ET.tostring(header)
            with zipfile.ZipFile(source,'w')as z:
                for name,data in files.items():z.writestr(name,data)
            result=read_hwpx(source)
            self.assertEqual(result.text(),'Readable')
            self.assertEqual(result.blocks[0].left_indent,0)
            self.assertTrue(any('문단' in w for w in result.warnings))
