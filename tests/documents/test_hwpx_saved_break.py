"""Stored bottom-page blank tails must not pull the next inline heading up."""
import unittest
from lxml import etree as E
from magic_converter.core.engines.rhwp_hwpx import NS, normalize_section


def fixture():
    root=E.Element('section',nsmap={'p':NS['p']})
    def n(parent,tag,**attrs):return E.SubElement(parent,'{'+NS['p']+'}'+tag,**attrs)
    config=n(root,'config');page=n(config,'pagePr',height='80000')
    n(page,'margin',top='5000',bottom='5000',header='2500',footer='2500')
    n(config,'colPr',colCount='1')
    def para(y):
        p=n(root,'p',pageBreak='0',columnBreak='0')
        run=n(p,'run');n(n(p,'linesegarray'),'lineseg',vertpos=str(y),vertsize='1200')
        return p,run
    _,run=para(5000);table=n(run,'tbl',rowCnt='12',pageBreak='TABLE')
    n(table,'pos',treatAsChar='0');n(table,'sz',height='50000')
    para(58000);para(61000)
    p,run=para(300);table=n(run,'tbl',rowCnt='1',textWrap='TOP_AND_BOTTOM')
    n(table,'pos',treatAsChar='1')
    return root,p


class SavedBreakTests(unittest.TestCase):
    def test_saved_break_is_private_idempotent_and_keeps_lines_and_text(self):
        root,p=fixture();before=E.tostring(root)
        data,changes=normalize_section(before,set())
        self.assertEqual(changes,[{'paragraph':3,'operation':'stored_tail_page_break'}])
        self.assertEqual(p.get('pageBreak'),'0')
        after=E.fromstring(data)
        self.assertEqual(after.findall('p:p',NS)[-1].get('pageBreak'),'1')
        self.assertEqual([E.tostring(n) for n in root.findall('.//p:lineseg',NS)],
                         [E.tostring(n) for n in after.findall('.//p:lineseg',NS)])
        self.assertEqual(normalize_section(data,set()),(data,[]))

    def test_mid_page_multicolumn_fields_bad_cache_and_existing_break_stay_untouched(self):
        for kind in ('mid_page','multicolumn','field','rewind','not_reset','existing','no_page','text','inline_anchor'):
            with self.subTest(kind=kind):
                root,p=fixture();paragraphs=root.findall('p:p',NS)
                if kind=='mid_page':paragraphs[2].find('p:linesegarray/p:lineseg',NS).set('vertpos','40000')
                if kind=='multicolumn':root.find('.//p:colPr',NS).set('colCount','2')
                if kind=='field':E.SubElement(paragraphs[2].find('p:run',NS),'{'+NS['p']+'}fieldBegin')
                if kind=='rewind':paragraphs[1].find('p:linesegarray/p:lineseg',NS).set('vertpos','62000')
                if kind=='not_reset':p.find('p:linesegarray/p:lineseg',NS).set('vertpos','10000')
                if kind=='existing':p.set('pageBreak','1')
                if kind=='no_page':root.remove(root[0])
                if kind=='text':E.SubElement(paragraphs[2].find('p:run',NS),'{'+NS['p']+'}t').text='Authored'
                if kind=='inline_anchor':root.find('.//p:pos',NS).set('treatAsChar','1')
                before=E.tostring(root)
                self.assertEqual(normalize_section(before,set()),(before,[]))


if __name__=='__main__':unittest.main()
