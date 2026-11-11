"""Always-available pinned HWP/HWPX controls; user corpus remains optional."""
import hashlib,json,tempfile,unittest
from pathlib import Path
from magic_converter.core.documents.hwp_documents import read_hwp,read_hwpx,read_docx,write_hwpx,write_docx
from magic_converter.core.documents.rich_hwp import object_counts,walk_blocks,Picture

ROOT=Path(__file__).resolve().parents[2]


class FixedHwpCorpusTests(unittest.TestCase):
    def test_pinned_documents_preserve_objects_and_picture_bytes(self):
        manifest=json.loads((ROOT/'tests/fixtures/hwp/manifest.json').read_text(encoding='utf-8'))
        self.assertEqual(manifest['schema_version'],1)
        with tempfile.TemporaryDirectory()as folder:
            for entry in manifest['fixtures']:
                source=ROOT/entry['path'];before=source.read_bytes()
                self.assertEqual(len(before),entry['bytes'])
                self.assertEqual(hashlib.sha256(before).hexdigest(),entry['sha256'])
                data=(read_hwp if source.suffix=='.hwp'else read_hwpx)(source)
                self.assertEqual(object_counts(data),entry['expected_counts'])
                def images(document):
                    return sorted(hashlib.sha256(b.data).hexdigest()for b in walk_blocks(
                        [*document.headers,*document.blocks,*document.footers])if isinstance(b,Picture))
                for extension,writer,reader in [('hwpx',write_hwpx,read_hwpx),('docx',write_docx,read_docx)]:
                    target=Path(folder)/('roundtrip.'+extension);writer(data,target);restored=reader(target)
                    self.assertEqual(object_counts(restored),entry['expected_counts'])
                    self.assertEqual(images(data),images(restored))
                self.assertEqual(source.read_bytes(),before)

    def test_hwpx_spacing_and_declared_faces_are_fixed_inputs(self):
        document=read_hwpx(ROOT/'tests/fixtures/hwp/table-picture-spacing.hwpx')
        self.assertEqual(document.margins,(36,48,60,72))
        self.assertEqual(document.blocks[0].text,'Origin control · Ⅺ Mixed size')
        self.assertEqual([r.font for r in document.blocks[0].runs],['Gulim','Batang'])


if __name__=='__main__':unittest.main()
