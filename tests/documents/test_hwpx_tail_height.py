"""Only declared, empty floating table tail slack may be recovered."""
from pathlib import Path
import tempfile
import unittest
import zipfile
from lxml import etree

from magic_converter.core.engines.rhwp_hwpx import NS, normalize_section, prepare


def section():
    root=etree.Element('section',nsmap={'p':NS['p']})
    def node(parent,name,**attrs):return etree.SubElement(parent,'{'+NS['p']+'}'+name,**attrs)
    paragraph=node(root,'p');run=node(paragraph,'run')
    table=node(run,'tbl',id='1',rowCnt='2',colCnt='1',cellSpacing='0',noAdjust='0',textWrap='SQUARE')
    node(table,'sz',height='7800',width='10000');node(table,'pos',treatAsChar='0')
    node(table,'inMargin',top='141',bottom='141')
    for row in range(2):
        cell=node(node(table,'tr'),'tc',hasMargin='0')
        sub=node(cell,'subList');p=node(sub,'p',paraPrIDRef='0')
        node(node(p,'run'),'t').text='Source text'
        node(node(p,'linesegarray'),'lineseg',vertpos='0',vertsize='1000',textheight='1000',spacing='300')
        node(cell,'cellAddr',rowAddr=str(row),colAddr='0');node(cell,'cellSpan',rowSpan='1',colSpan='1')
        node(cell,'cellSz',height='4000',width='10000')
    return root,table


class TailHeightTests(unittest.TestCase):
    def test_small_declared_tail_slack_keeps_text_and_prior_row(self):
        root,table=section();before=etree.tostring(root)
        data,edits=normalize_section(before,{'0'})
        self.assertEqual(edits[0]['recovered_hwp_units'],200)
        after=etree.fromstring(data)
        self.assertEqual([c.get('height') for c in after.findall('.//p:cellSz',NS)],['4000','3800'])
        self.assertEqual([t.text for t in root.findall('.//p:t',NS)],[t.text for t in after.findall('.//p:t',NS)])
        self.assertEqual(normalize_section(data,{'0'}),(data,[]))

    def test_consistent_large_conflicts_content_floor_and_ambiguous_geometry_are_untouched(self):
        for kind in ('consistent','large','content','inline','rowspan','host_text','style','nested'):
            with self.subTest(kind=kind):
                root,table=section();styles={'0'}
                if kind=='consistent':table.find('p:sz',NS).set('height','8000')
                if kind=='large':table.find('p:sz',NS).set('height','6000')
                if kind=='content':table.findall('.//p:lineseg',NS)[-1].set('textheight','3900')
                if kind=='inline':table.find('p:pos',NS).set('treatAsChar','1')
                if kind=='rowspan':table.find('.//p:cellSpan',NS).set('rowSpan','2')
                if kind=='host_text':etree.SubElement(root.find('p:p/p:run',NS),'{'+NS['p']+'}t').text='Anchor'
                if kind=='style':styles=set()
                if kind=='nested':etree.SubElement(table.findall('.//p:run',NS)[-1],'{'+NS['p']+'}tbl')
                before=etree.tostring(root);after,edits=normalize_section(before,styles)
                self.assertEqual(after,before);self.assertEqual(edits,[])

    def test_private_archive_preserves_original_and_every_unmodified_member(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);source=root/'source.hwpx';private=root/'private';private.mkdir()
            data=etree.tostring(section()[0])
            header=b'<head xmlns="http://www.hancom.co.kr/hwpml/2011/head"><paraPr id="0"><margin><prev value="0" unit="HWPUNIT"/><next value="0" unit="HWPUNIT"/></margin></paraPr></head>'
            with zipfile.ZipFile(source,'w') as z:
                z.writestr('mimetype',b'application/hwp+zip');z.writestr('Contents/header.xml',header)
                z.writestr('Contents/section0.xml',data);z.writestr('BinData/image.bin',b'image bytes')
            original=source.read_bytes();result,edits=prepare(source,private)
            self.assertEqual(source.read_bytes(),original);self.assertEqual(len(edits),1)
            self.assertTrue(result.is_relative_to(private))
            with zipfile.ZipFile(source) as before,zipfile.ZipFile(result) as after:
                self.assertEqual(before.namelist(),after.namelist())
                for name in before.namelist():
                    if name!='Contents/section0.xml':self.assertEqual(before.read(name),after.read(name))


if __name__=='__main__':unittest.main()
