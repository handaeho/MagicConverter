import tempfile
import unittest
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from magic_converter.core.documents.hwp_documents import (DocumentData, Paragraph, TextRun,
    write_hwpx, read_hwpx, write_docx, read_docx, q)


class HwpxMetricsTests(unittest.TestCase):
    def test_hwpx_model_roundtrip_retains_positive_negative_tracking_and_width(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/'source.hwpx';result=root/'result.hwpx'
            expected=[TextRun('가나다',spacing_percent=-15,width_percent=85),
                      TextRun('ABC',spacing_percent=20,width_percent=130),TextRun('기본')]
            write_hwpx(DocumentData([Paragraph(expected)]),source)
            data=read_hwpx(source);write_hwpx(data,result);actual=read_hwpx(result)
            self.assertEqual(actual.blocks[0].runs,expected)
            self.assertEqual(actual.margins,(72,72,72,72))

    def test_same_font_different_language_metrics_are_not_merged_and_space_is_latin(self):
        with tempfile.TemporaryDirectory() as tmp:
            source=Path(tmp)/'source.hwpx';write_hwpx(DocumentData([Paragraph([TextRun('가 A漢あ')])]),source)
            with zipfile.ZipFile(source) as archive:files={n:archive.read(n) for n in archive.namelist()}
            header=ET.fromstring(files['Contents/header.xml'])
            for style in header.iter(q('hh','charPr')):
                for kind,values in [('spacing',{'hangul':'-10','latin':'20','hanja':'5','japanese':'7'}),
                                    ('ratio',{'hangul':'90','latin':'120','hanja':'110','japanese':'80'})]:
                    node=style.find(q('hh',kind))
                    for key,value in values.items():node.set(key,value)
            files['Contents/header.xml']=ET.tostring(header)
            with zipfile.ZipFile(source,'w') as archive:
                for name,data in files.items():archive.writestr(name,data)
            runs=read_hwpx(source).blocks[0].runs
            self.assertEqual([(r.text,r.spacing_percent,r.width_percent)for r in runs],
                             [('가',-10,90),(' A',20,120),('漢',5,110),('あ',7,80)])

    def test_docx_width_is_preserved_and_tracking_mismatch_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            source=Path(tmp)/'source.docx'
            data=DocumentData([Paragraph([TextRun('가나다 ABC',spacing_percent=20,width_percent=125)])])
            write_docx(data,source)
            actual=read_docx(source)
            self.assertEqual(actual.blocks[0].runs[0].width_percent,125)
            from magic_converter.core.documents.document_audit import compare
            self.assertTrue(any('자간' in w for w in compare(data,actual)))
            self.assertEqual(actual.blocks[0].runs[0].spacing_percent,0)

    def test_invalid_optional_metrics_leave_valid_text_readable_and_warn(self):
        with tempfile.TemporaryDirectory() as tmp:
            source=Path(tmp)/'source.hwpx';write_hwpx(DocumentData([Paragraph([TextRun('가나다')])]),source)
            with zipfile.ZipFile(source) as archive:files={n:archive.read(n)for n in archive.namelist()}
            # Change only spacing, keeping style/font identities valid.
            head=ET.fromstring(files['Contents/header.xml'])
            for node in head.iter(q('hh','spacing')):node.set('hangul','999')
            with zipfile.ZipFile(source,'w') as archive:
                files['Contents/header.xml']=ET.tostring(head)
                for name,data in files.items():archive.writestr(name,data)
            data=read_hwpx(source)
            self.assertEqual(data.text(),'가나다')
            self.assertTrue(any('자간' in w for w in data.warnings))

    def test_audit_accepts_equivalent_run_splits_and_defaults(self):
        from magic_converter.core.documents.document_audit import compare
        a=DocumentData([Paragraph([TextRun('AB',width_percent=120)])])
        b=DocumentData([Paragraph([TextRun('A',width_percent=120),TextRun('B',width_percent=120)])])
        self.assertEqual(compare(a,b),[])
        self.assertEqual(compare(DocumentData([Paragraph([TextRun('AB')])]),
                                 DocumentData([Paragraph([TextRun('A'),TextRun('B')])])),[])
