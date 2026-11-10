"""Font-only declarations pass without font recovery; DRM/malformed data fails."""
import hashlib
import tempfile
import unittest
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET
from magic_converter.core.specialized.ebook_converter import Book, Chapter, OPF, DC, read_book, write_epub
from magic_converter.core.specialized.epub_protection import IDPF, ENC, CONTAINER
from tests.conversion.test_special_formats import sample_font


def fixture(folder, *, algorithm=IDPF, uri='EPUB/font.ttf', media='font/ttf', marker=None):
    path = folder / 'font.epub'
    write_epub(Book('Font control', language='en', chapters=[Chapter('Body', '<p>Readable text</p>')]), path)
    with zipfile.ZipFile(path) as archive: contents = {i: archive.read(i) for i in archive.namelist()}
    opf = ET.fromstring(contents['EPUB/package.opf'])
    manifest = opf.find(f'{{{OPF}}}manifest')
    ET.SubElement(manifest, f'{{{OPF}}}item', {'id':'font', 'href':'font.ttf', 'media-type':media})
    # Obfuscation is fixture generation only; production never recovers font data.
    font_path = folder / 'font.ttf'; sample_font(font_path)
    font = bytearray(font_path.read_bytes())
    identity = opf.find(f'.//{{{DC}}}identifier').text
    key = hashlib.sha1(''.join(identity.split()).encode()).digest()
    for index in range(min(1040, len(font))): font[index] ^= key[index % 20]
    contents['EPUB/font.ttf'] = bytes(font)
    contents['EPUB/package.opf'] = ET.tostring(opf, encoding='utf-8', xml_declaration=True)
    contents['META-INF/encryption.xml'] = (f'<encryption xmlns="{CONTAINER}" xmlns:e="{ENC}"><e:EncryptedData>'
        f'<e:EncryptionMethod Algorithm="{algorithm}"/><e:CipherData><e:CipherReference URI="{uri}"/>'
        '</e:CipherData></e:EncryptedData></encryption>').encode()
    if marker: contents[marker] = b'protection marker'
    with zipfile.ZipFile(path, 'w') as archive:
        for name, data in contents.items():
            archive.writestr(name, data, compress_type=zipfile.ZIP_STORED if name == 'mimetype' else zipfile.ZIP_DEFLATED)
    return path


class EpubProtectionTests(unittest.TestCase):
    def test_font_only_and_plain_controls_preserve_text_and_omit_font(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder); source = fixture(folder); notices = []
            book = read_book(source, 'epub', notices.append)
            self.assertIn('Readable text', book.chapters[0].body)
            self.assertTrue(any('IDPF' in n for n in notices))
            output = folder / 'plain.epub'; write_epub(book, output)
            with zipfile.ZipFile(output) as archive:
                self.assertNotIn('META-INF/encryption.xml', archive.namelist())
                self.assertFalse(any(n.endswith('.ttf') for n in archive.namelist()))
            self.assertIn('Readable text', read_book(output, 'epub').chapters[0].body)

    def test_drm_nonfont_unknown_missing_and_unsafe_targets_refused(self):
        cases = [{'algorithm':'urn:unknown:encryption'}, {'uri':'EPUB/chapter0.xhtml'},
                 {'media':'application/xhtml+xml'}, {'uri':'EPUB/missing.ttf'},
                 {'uri':'../font.ttf'}, {'uri':'https://example.invalid/font.ttf'},
                 {'uri':'EPUB/font.ttf#fragment'},
                 *({'marker':m} for m in ('META-INF/sinf.xml','META-INF/rights.xml','META-INF/license.lcpl'))]
        with tempfile.TemporaryDirectory() as folder:
            for settings in cases:
                with self.subTest(settings=settings):
                    source = fixture(Path(folder), **settings)
                    with self.assertRaises(Exception): read_book(source, 'epub')


if __name__ == '__main__': unittest.main()
