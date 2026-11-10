"""Real Qt MIME containers, browser formats and selection/refusal controls."""
import struct
import unittest
from PySide6.QtCore import QMimeData, QUrl
from magic_converter.core.automation.web_inputs import from_mime
from magic_converter.core.automation.browser_mime import WINDOWS


def virtual(names_data):
    mime = QMimeData()
    descriptor = bytearray(struct.pack('<I', len(names_data)))
    for index, (name, data) in enumerate(names_data):
        record = bytearray(592)
        struct.pack_into('<I', record, 0, 0x40)
        struct.pack_into('<II', record, 64, 0, len(data))
        encoded = (name + '\0').encode('utf-16-le')
        record[72:72 + len(encoded)] = encoded
        descriptor.extend(record)
        mime.setData(WINDOWS.format('FileContents') + f';index={index}', data)
    mime.setData(WINDOWS.format('FileGroupDescriptorW'), descriptor)
    return mime


class BrowserMimeTests(unittest.TestCase):
    def test_single_plain_http_link_preserves_the_complete_url(self):
        for url in ('http://127.0.0.1:50377/token/image.png',
                    'https://example.invalid/a%20b.png?x=a:b&next=%2Ffile#part',
                    'https://[::1]:8443/image.png'):
            with self.subTest(url=url):
                mime = QMimeData(); mime.setText('  ' + url + '\r\n')
                paths, items = from_mime(mime)
                self.assertTrue(items)
                self.assertEqual(items[0].url, url)
                self.assertEqual(paths, (items[0].placeholder,))

    def test_plain_text_body_multiple_urls_and_unsafe_addresses_are_refused(self):
        for value in ('Article https://example.invalid/a.png',
                      'https://example.invalid/a.png\nhttps://example.invalid/b.png',
                      'https://example.invalid/a.png https://example.invalid/b.png',
                      'file:///C:/Windows/a.png', 'javascript:alert(1)',
                      'data:image/png;base64,aA==', 'https://user:pass@example.invalid/a',
                      'https://example.invalid:bad/a', 'https://example.invalid/a\x00b',
                      'https://example.invalid/a\x7fb', 'https://example.invalid/a\u00a0b',
                      'https://example.invalid/' + 'x' * 16000, 'example.invalid/a.png', ''):
            with self.subTest(value=value[:80]):
                mime = QMimeData(); mime.setText(value)
                self.assertEqual(from_mime(mime), ((), ()))

    def test_plain_link_never_bypasses_html_or_malformed_explicit_formats(self):
        url = 'https://example.invalid/image.png'
        cases = []
        for html in ('<p>Article</p><img src="' + url + '">',
                     '<img src="' + url + '"><img src="' + url + '">',
                     '<a href="' + url + '">Selected link without explicit URL MIME</a>'):
            mime = QMimeData(); mime.setText(url); mime.setHtml(html); cases.append(mime)
        for kind, data in [('DownloadURL', b'broken'),
                           (WINDOWS.format('FileGroupDescriptorW'), struct.pack('<I', 101)),
                           ('text/uri-list', b'ftp://example.invalid/a.png\r\n')]:
            mime = QMimeData(); mime.setText(url); mime.setData(kind, data); cases.append(mime)
        for mime in cases:
            self.assertEqual(from_mime(mime), ((), ()))

    def test_explicit_browser_url_formats_preserve_url_and_named_download(self):
        url = 'https://example.invalid/image.png?key=a:b'
        cases = [('text/x-moz-url', (url + '\nImage title').encode('utf-16-le')),
                 ('text/x-moz-url-data', url.encode()),
                 (WINDOWS.format('UniformResourceLocatorW'), (url + '\0').encode('utf-16-le')),
                 (WINDOWS.format('UniformResourceLocator'), (url + '\0').encode()),
                 ('DownloadURL', ('image/png:renamed.png:' + url).encode())]
        cases += [(WINDOWS.format('text/x-moz-url'), (url + '\nImage title').encode('utf-16-le')),
                  (WINDOWS.format('DownloadURL'), ('image/png:renamed.png:' + url).encode())]
        for kind, data in cases:
            with self.subTest(kind=kind):
                mime = QMimeData(); mime.setData(kind, data)
                paths, items = from_mime(mime)
                self.assertEqual(items[0].url, url)
                self.assertEqual(paths, (items[0].placeholder,))
                self.assertEqual(items[0].name, 'renamed.png' if 'DownloadURL' in kind else 'image.png')

    def test_virtual_files_shortcuts_and_multiple_content_indices(self):
        mime = virtual([('one.txt', b'hello'), ('two.csv', b'a,b\n1,2')])
        self.assertEqual([i.data for i in from_mime(mime)[1]], [b'hello', b'a,b\n1,2'])
        mime = virtual([('link.url', b'[InternetShortcut]\r\nURL=https://example.invalid/a.png\r\n')])
        self.assertEqual(from_mime(mime)[1][0].url, 'https://example.invalid/a.png')

    def test_malformed_unsafe_and_unbounded_adapters_refuse_all_items(self):
        cases = [('text/x-moz-url', b'file:///C:/Windows/a\nTitle'),
                 ('DownloadURL', b'image/png:a.png:https://user:pass@example.org/a'),
                 ('DownloadURL', b'broken'),
                 (WINDOWS.format('UniformResourceLocatorW'), b'odd'),
                 (WINDOWS.format('FileGroupDescriptorW'), struct.pack('<I', 101))]
        for kind, data in cases:
            mime = QMimeData(); mime.setData(kind, data)
            self.assertEqual(from_mime(mime), ((), ()), kind)
        for entries in ([('../bad.txt', b'content')], [('bad.url', b'not an ini')],
                        [('bad.url', b'[InternetShortcut]\nURL=file:///C:/Windows/a')]):
            self.assertEqual(from_mime(virtual(entries)), ((), ()))
        mime = virtual([('a.txt', b'123')]); mime.setData(WINDOWS.format('FileContents') + ';index=0', b'12')
        self.assertEqual(from_mime(mime), ((), ()))
        mime = virtual([('a.txt', b'123')])
        descriptor = bytearray(mime.data(WINDOWS.format('FileGroupDescriptorW')))
        struct.pack_into('<I', descriptor, 4 + 68, 33 * 1024 * 1024)
        mime.setData(WINDOWS.format('FileGroupDescriptorW'), descriptor)
        self.assertEqual(from_mime(mime), ((), ()))

    def test_article_selection_never_uses_url_image_or_explicit_fallback(self):
        from PySide6.QtGui import QImage
        image = QImage(3, 3, QImage.Format.Format_RGB32); image.fill(0xff00ff00)
        for html in ('<p>Article</p><img src="https://example.invalid/tracker.png">',
                     '<img src="https://example.invalid/a.png"><img src="https://example.invalid/b.png">',
                     '<p>Only selected body</p>'):
            mime = QMimeData(); mime.setHtml(html)
            mime.setImageData(image)
            mime.setUrls([QUrl('https://example.invalid/tracker.png')])
            mime.setData('DownloadURL', b'image/png:a.png:https://example.invalid/a.png')
            self.assertEqual(from_mime(mime), ((), ()))
        mime = QMimeData(); mime.setHtml('<a href="https://example.invalid/page"><img alt="title" src="https://example.invalid/image.png"></a>')
        mime.setUrls([QUrl('https://example.invalid/page')])
        self.assertEqual(from_mime(mime)[1][0].url, 'https://example.invalid/image.png')
        mime.setHtml('<a href="https://example.invalid/page">Link title</a>')
        self.assertEqual(from_mime(mime)[1][0].url, 'https://example.invalid/page')
        mime = QMimeData(); mime.setImageData(image)
        mime.setHtml('<img src="blob:https://example.invalid/image">')
        self.assertTrue(from_mime(mime)[1][0].data.startswith(b'\x89PNG'))
        mime.setData('text/x-moz-url', b'blob:https://example.invalid/image\nTitle')
        self.assertTrue(from_mime(mime)[1][0].data.startswith(b'\x89PNG'))
        # An HTTP GIF must remain a URL to retain all original frames, rather
        # than becoming the browser's static thumbnail pixels.
        mime = QMimeData(); mime.setImageData(image)
        mime.setHtml('<img src="https://example.invalid/animation.gif">')
        self.assertEqual(from_mime(mime)[1][0].url, 'https://example.invalid/animation.gif')
        mime.setHtml('<p>Article</p><img src="blob:https://example.invalid/image">')
        mime.setData('text/x-moz-url', b'blob:https://example.invalid/image\nTitle')
        self.assertEqual(from_mime(mime), ((), ()))
        mime = virtual([('a.txt', b'123')]); mime.setImageData(image)
        mime.setHtml('<img src="blob:https://example.invalid/image">')
        mime.setData(WINDOWS.format('FileContents') + ';index=0', b'12')
        self.assertEqual(from_mime(mime), ((), ()))


if __name__ == '__main__': unittest.main()
