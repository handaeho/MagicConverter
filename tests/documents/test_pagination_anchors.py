import unittest
from tools.validation.verify_hwpx_pagination import anchors


class PaginationAnchorTests(unittest.TestCase):
    def test_matching_and_shifted_pages_are_distinct_measurements(self):
        paragraphs=['Unique first paragraph long enough','Unique final paragraph long enough']
        self.assertEqual([r['page_delta'] for r in anchors(paragraphs,paragraphs,paragraphs)['matched']],[0,0])
        shifted=anchors(paragraphs,paragraphs,[paragraphs[0],'new preceding content',paragraphs[1]])
        self.assertEqual([r['page_delta'] for r in shifted['matched']],[0,1])

    def test_missing_and_ambiguous_anchors_are_never_matches(self):
        text='Repeated paragraph long enough'
        self.assertFalse(anchors([text],[text,text],[text])['matched'])
        self.assertFalse(anchors([text],[text],['Missing anchor'])['matched'])
        self.assertFalse(anchors([text,text],[text],[text])['matched'])


if __name__=='__main__': unittest.main()
