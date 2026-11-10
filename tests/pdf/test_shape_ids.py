"""Large PDF paths must not rescan every ID or collide with group children."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import pymupdf as fitz
from pptx import Presentation
from pptx.oxml.shapes.groupshape import CT_GroupShape
from magic_converter.core.conversion.converter import pdf_to_pptx_editable


class ShapeIdTests(unittest.TestCase):
    def test_many_paths_and_interleaved_glyph_groups_keep_unique_ids_with_bounded_scans(self):
        with tempfile.TemporaryDirectory() as directory:
            source=Path(directory)/'many-paths.pdf'
            with fitz.open() as pdf:
                page=pdf.new_page(width=400,height=320)
                for index in range(200):
                    page.draw_line((10,10+index*.3),(180,10+index*.3),width=.1)
                origin=fitz.Point(30,110)
                page.insert_text(origin,'Stretched group',fontsize=18,morph=(origin,fitz.Matrix(.7,1)))
                for index in range(200):
                    page.draw_line((210+index*.3,130),(210+index*.3,230),width=.1)
                page.insert_text((30,270),'Fill stroke group',fontsize=18,render_mode=2)
                page.insert_text((30,305),'Editable after both groups',fontsize=12)
                pdf.save(source)
            original=CT_GroupShape.max_shape_id.fget;scans=[]
            def counted(tree):
                scans.append(1)
                return original(tree)
            with patch.object(CT_GroupShape,'max_shape_id',property(counted)):
                pdf_to_pptx_editable(source,source.with_suffix('.pptx'))
            slide=Presentation(source.with_suffix('.pptx')).slides[0]
            ids=[int(node.get('id')) for node in slide._element.xpath('.//p:cNvPr')]
            self.assertEqual(len(ids),len(set(ids)))
            self.assertGreater(len(ids),400)
            self.assertEqual(sum(s.name.startswith('PDF glyph outline region ') for s in slide.shapes),2)
            self.assertIn('Editable after both groups',[s.text for s in slide.shapes if s.has_text_frame])
            # Detect quadratic ID allocation without a machine-speed timeout.
            self.assertLess(len(scans),20)
