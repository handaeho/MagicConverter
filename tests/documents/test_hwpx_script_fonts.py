"""A HWPX style's Latin/Hanja/Japanese faces must survive model conversion."""
import tempfile,unittest,zipfile
from pathlib import Path
from xml.etree import ElementTree as ET
from magic_converter.core.documents.hwp_documents import DocumentData,Paragraph,TextRun,read_hwpx,write_hwpx,write_docx,read_docx,q
from magic_converter.core.documents.rich_hwp import _hwpx_text_runs


class HwpxScriptFontTests(unittest.TestCase):
    def test_distinct_declared_script_faces_survive_hwpx_docx_roundtrip(self):
        with tempfile.TemporaryDirectory()as folder:
            root=Path(folder);source=root/'source.hwpx';seed=root/'seed.hwpx'
            write_hwpx(DocumentData([Paragraph([TextRun('Ab가中あ·Ⅺ',font='Gulim')])]),seed)
            with zipfile.ZipFile(seed)as archive:
                files={n:archive.read(n)for n in archive.namelist()}
            header=ET.fromstring(files['Contents/header.xml'])
            faces={'LATIN':'Arial','HANJA':'Batang','JAPANESE':'Meiryo'}
            for face in header.iter(q('hh','fontface')):
                if face.get('lang')in faces:
                    for font in face:
                        if font.get('face')=='Gulim':font.set('face',faces[face.get('lang')])
            files['Contents/header.xml']=ET.tostring(header,encoding='utf-8')
            with zipfile.ZipFile(source,'w')as archive:
                for name,data in files.items():archive.writestr(name,data)
            document=read_hwpx(source)
            expected=[('Ab','Arial'),('가','Gulim'),('中','Batang'),('あ','Meiryo'),('·Ⅺ','Gulim')]
            self.assertEqual([(r.text,r.font)for r in document.blocks[0].runs],expected)
            for extension,writer,reader in [('hwpx',write_hwpx,read_hwpx),('docx',write_docx,read_docx)]:
                target=root/('roundtrip.'+extension);writer(document,target)
                self.assertEqual([(r.text,r.font)for r in reader(target).blocks[0].runs],expected)

    def test_missing_refs_and_ambiguous_symbols_keep_default_face(self):
        style={'font':'Gulim','size':12,'_script_fonts':{'latin':'Arial'}}
        self.assertEqual([(r.text,r.font)for r in _hwpx_text_runs('A가·Ⅺ中あ',style)],
                         [('A','Arial'),('가·Ⅺ中あ','Gulim')])
        self.assertEqual(_hwpx_text_runs('',style)[0].text,'')

    def test_same_face_control_keeps_one_run_and_all_formatting(self):
        style={'font':'Gulim','size':12,'bold':True,'_script_fonts':{'latin':'Gulim','hanja':'Gulim','japanese':'Gulim'}}
        runs=_hwpx_text_runs('Ab가中あ·Ⅺ',style)
        self.assertEqual(len(runs),1);self.assertTrue(runs[0].bold);self.assertEqual(runs[0].size,12)


if __name__=='__main__':unittest.main()
