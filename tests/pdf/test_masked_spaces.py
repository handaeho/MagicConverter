import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from pptx import Presentation
from magic_converter.core.conversion.converter import pdf_to_pptx_editable
from tools.validation.verify_masked_text import fixture, CASES


class MaskedSpaceTests(unittest.TestCase):
    def test_empty_outlines_do_not_force_replay_and_visible_whitespace_keeps_its_ink(self):
        for name in CASES:
            with self.subTest(case=name), tempfile.TemporaryDirectory() as folder:
                source = Path(folder) / 'source.pdf'; fixture(source, name)
                target = source.with_suffix('.pptx'); pdf_to_pptx_editable(source, target)
                shapes = Presentation(target).slides[0].shapes
                groups = [s for s in shapes if s.name.startswith('PDF glyph outline region ')]
                if name in {'constant-space', 'visible-space'}:
                    self.assertFalse(any(s.shape_type == 13 for s in shapes))
                    self.assertEqual(len(groups), 1)
                    if name == 'visible-space':
                        self.assertEqual(len(groups[0].shapes[0]._element.xpath('.//a:moveTo')), 3)
                else:
                    self.assertEqual(sum(s.shape_type == 13 for s in shapes), 1)
                    self.assertFalse(groups)

    def test_spaces_still_count_toward_glyph_budget(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'source.pdf'; fixture(source, 'constant-space')
            with patch('magic_converter.core.pdf.pdf_pptx_glyphs.MAX_GLYPHS', 5):
                target = source.with_suffix('.pptx'); pdf_to_pptx_editable(source, target)
            self.assertEqual(sum(s.shape_type == 13 for s in Presentation(target).slides[0].shapes), 1)
