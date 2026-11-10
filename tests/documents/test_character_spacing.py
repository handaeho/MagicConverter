import tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from magic_converter.core.documents.hwp_documents import DocumentData,Paragraph,TextRun,write_docx,read_docx,write_hwpx,read_hwpx


class CharacterSpacingTests(unittest.TestCase):
    def test_docx_fixed_spacing_is_retained_in_model_and_docx_output(self):
        with tempfile.TemporaryDirectory()as tmp:
            a=Path(tmp)/'source.docx';b=Path(tmp)/'returned.docx';doc=Document();run=doc.add_paragraph().add_run('가나다라')
            run.font.name='굴림';node=OxmlElement('w:spacing');node.set(qn('w:val'),'30');run._r.get_or_add_rPr().append(node);doc.save(a)
            model=read_docx(a);self.assertEqual(model.blocks[0].runs[0].spacing_pt,1.5)
            write_docx(model,b);self.assertEqual(read_docx(b).blocks[0].runs[0].spacing_pt,1.5)

    def test_verified_uniform_advance_tracking_roundtrip_preserves_spacing(self):
        from magic_converter.core.documents.document_audit import compare
        with tempfile.TemporaryDirectory()as tmp:
            root=Path(tmp)
            for percent in(-15,20):
                with self.subTest(percent=percent):
                    data=DocumentData([Paragraph([TextRun('가나다라',font='굴림',size=12,spacing_percent=percent)])])
                    p=root/f'{percent}.docx';h=p.with_suffix('.hwpx')
                    write_docx(data,p);model=read_docx(p)
                    self.assertAlmostEqual(model.blocks[0].runs[0].spacing_pt,12*percent/100)
                    self.assertFalse(any('자간'in w for w in compare(data,model)))
                    write_hwpx(model,h);self.assertEqual(read_hwpx(h).blocks[0].runs[0].spacing_percent,percent)

    def test_variable_width_missing_font_and_complex_script_keep_warning(self):
        for text,font,width in [('ABC','Arial',100),('IIII','Arial',100),('WWWW','Arial',100),('A'*40,'Arial',100),('AV','Arial',100),('가나다ABC','맑은 고딕',100),('가나다','NoSuchInstalledFont9281',100),('가나다','굴림',125),('fi','Arial',100),('a\u0301','Arial',100)]:
            with self.subTest(text=text,font=font),tempfile.TemporaryDirectory()as tmp:
                data=DocumentData([Paragraph([TextRun(text,font=font,spacing_percent=20,width_percent=width)])])
                p=Path(tmp)/'out.docx';write_docx(data,p)
                from magic_converter.core.documents.document_audit import compare
                self.assertTrue(any('자간'in w for w in compare(data,read_docx(p))))
                self.assertEqual(read_docx(p).blocks[0].runs[0].spacing_pt,0)

    def test_mixed_halfwidth_fullwidth_tracking_uses_each_glyph_advance(self):
        from magic_converter.core.documents.document_audit import compare
        for font, percent in (('굴림체', 20), ('DotumChe', -20)):
            with self.subTest(font=font), tempfile.TemporaryDirectory() as tmp:
                run = TextRun('A가 z나9', font=font, size=12, spacing_percent=percent)
                doc = DocumentData([Paragraph([run])])
                path = Path(tmp) / 'result.docx'
                write_docx(doc, path)
                result = read_docx(path)
                self.assertEqual(result.text(), run.text)
                expected = [percent * width / 100 for width in (6, 12, 6, 6, 12, 6)]
                actual = [r.spacing_pt for r in result.blocks[0].runs for _ in r.text]
                self.assertEqual(actual, expected)
                self.assertFalse(any('자간' in warning for warning in compare(doc, result)))

    def test_verified_proportional_paragraph_uses_variable_widths_and_keeps_unsafe_boundaries(self):
        from magic_converter.core.documents.character_spacing import glyph_advances
        from magic_converter.core.documents.document_audit import compare
        run = TextRun('number 20 WN', font='Tahoma', size=12, spacing_percent=20)
        advances = glyph_advances(run)
        if advances is None:
            self.skipTest('Verified Tahoma font revision is not installed')
        self.assertGreater(len(set(advances)), 2)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'result.docx'
            safe = DocumentData([Paragraph([run])])
            write_docx(safe, path)
            result = read_docx(path)
            self.assertEqual(result.text(), run.text)
            self.assertFalse(any('자간' in warning for warning in compare(safe, result)))
            actual = [r.spacing_pt for r in result.blocks[0].runs for _ in r.text]
            balance = 0
            for width, spacing in zip(advances, actual, strict=True):
                balance += spacing - width / 5
                self.assertLessEqual(abs(balance), .025001)
            unsafe = DocumentData([Paragraph([run, TextRun('.', font='Tahoma', size=12)])])
            write_docx(unsafe, path)
            result = read_docx(path)
            self.assertTrue(any('자간' in warning for warning in compare(unsafe, result)))
            self.assertTrue(all(r.spacing_pt == 0 for r in result.blocks[0].runs))
            kerned = DocumentData([Paragraph([TextRun('R', font='Tahoma', spacing_percent=20),
                                              TextRun('e', font='Tahoma')])])
            write_docx(kerned, path)
            result = read_docx(path)
            self.assertTrue(any('자간' in warning for warning in compare(kerned, result)))
            self.assertTrue(all(r.spacing_pt == 0 for r in result.blocks[0].runs))

    def test_fractional_twip_hangul_spacing_does_not_accumulate_rounding_loss(self):
        from magic_converter.core.documents.document_audit import compare
        from magic_converter.core.documents.character_spacing import glyph_advances
        for percent in (-17,13):
            with self.subTest(percent=percent),tempfile.TemporaryDirectory()as tmp:
                run=TextRun('가나다라'*20,font='굴림',size=12,spacing_percent=percent)
                source=DocumentData([Paragraph([run])]);p=Path(tmp)/'out.docx';write_docx(source,p)
                result=read_docx(p)
                self.assertFalse(any('자간'in w for w in compare(source,result)))
                target=sum(glyph_advances(run))*percent/100
                actual=sum(len(r.text)*r.spacing_pt for r in result.blocks[0].runs)
                self.assertLessEqual(abs(target-actual),.025001)
