import tempfile,unittest
from pathlib import Path
from pptx import Presentation
from magic_converter.core.conversion.converter import pdf_to_pptx_editable
from magic_converter.core.diagnostics.pdf_fixtures import single_line_cells_pdf

class SingleLineCellTests(unittest.TestCase):
    def test_tight_single_line_preserves_baseline_outside_native_cell_paragraph(self):
        self.check(14,False,4)

    def test_loose_table_and_empty_grid_controls(self):
        self.check(60,False,0)
        self.check(14,True,0)

    def check(self,height,empty,positioned):
        with tempfile.TemporaryDirectory()as folder:
            source=Path(folder)/'source.pdf';single_line_cells_pdf(source,row_height=height,empty=empty)
            target=source.with_suffix('.pptx');pdf_to_pptx_editable(source,target)
            shapes=Presentation(target).slides[0].shapes
            tables=[s.table for s in shapes if s.has_table]
            self.assertEqual(len(tables),0 if empty else 1)
            if empty:
                self.assertEqual(sum(s.name.startswith('PDF shape ')for s in shapes),6)
                self.assertFalse(any(s.shape_type==13 for s in shapes))
            textboxes=[s for s in shapes if s.name.startswith('PDF cell R')]
            self.assertEqual(len(textboxes),positioned)
            self.assertIn('Independent title',[s.text for s in shapes if s.has_text_frame])
            if positioned:
                self.assertEqual({s.text.replace('\xa0',' ') for s in textboxes},{f'Cell {row}{col} gjpq'for row in(1,2)for col in(1,2)})
                self.assertTrue(all(not cell.text for cell in tables[0].iter_cells()))
                self.assertTrue(all(cell.text_frame.paragraphs[0].font.size.pt==1 for cell in tables[0].iter_cells()))

if __name__=='__main__':unittest.main()
