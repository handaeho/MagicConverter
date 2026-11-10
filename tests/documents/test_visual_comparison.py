import tempfile
import unittest
from pathlib import Path
import pymupdf
from magic_converter.core.diagnostics.visual_comparison import compare


class VisualComparisonTests(unittest.TestCase):
    def test_equal_versus_pixels_geometry_and_missing_pages(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            def pdf(name, *, width=180, pages=2, text='Reference'):
                path = folder / name
                with pymupdf.open() as doc:
                    for _ in range(pages): doc.new_page(width=width, height=120).insert_text((10,30), text)
                    doc.save(path)
                return path
            original = pdf('reference.pdf')
            self.assertEqual(compare(original, pdf('same.pdf'))['status'], 'matched')
            for name, settings in [('text', {'text':'Changed'}), ('geometry',{'width':200}), ('missing', {'pages':1})]:
                result = compare(original, pdf(name+'.pdf', **settings), artifacts=folder/name)
                self.assertEqual(result['status'], 'different', name)
            self.assertTrue(list((folder/'text').glob('*-pair.png')))
            bad = folder/'bad.pdf'; bad.write_bytes(b'not pdf')
            with self.assertRaises(Exception): compare(original, bad)
            for settings in ({'dpi':0}, {'changed_limit':float('nan')}, {'mae_limit':-1}):
                with self.assertRaises(ValueError): compare(original, original, **settings)


if __name__ == '__main__': unittest.main()
