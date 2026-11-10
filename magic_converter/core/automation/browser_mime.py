"""Bounded browser/Windows drag adapters; no download or file publication here."""

import configparser
import struct

WINDOWS = 'application/x-qt-windows-mime;value="{}"'
MAX_DATA = 32 * 1024 * 1024


def raw(mime, kind, limit):
    value = bytes(mime.data(kind))
    if not value or len(value) > limit:
        raise ValueError("Invalid browser MIME size")
    return value


def decoded(value, *, wide=False):
    if value.startswith((b'\xff\xfe', b'\xfe\xff')):
        result = value.decode('utf-16')
    elif wide or b'\x00' in value[:128].rstrip(b'\x00'):
        result = value.decode('utf-16-le')
    else:
        result = value.decode('utf-8-sig')
    result = result.rstrip('\x00')
    if '\x00' in result:
        raise ValueError("Embedded NUL in browser MIME")
    return result


def image_selection(mime):
    """None: no HTML; empty: link-only; URL: one image; raise: body selection."""
    if not mime.hasHtml():
        return None
    from bs4 import BeautifulSoup
    html = mime.html()
    if len(html) > 1_000_000:
        raise ValueError("HTML drag is too large")
    soup = BeautifulSoup(html, 'html.parser')
    for node in soup(['script', 'style', 'head']):
        node.decompose()
    images = soup.find_all('img')
    visible = soup.get_text('', strip=True)
    if visible or len(images) != 1:
        anchors = soup.find_all('a')
        if not images and len(anchors) == 1 and visible == anchors[0].get_text('', strip=True):
            return ''  # Ordinary link drag, not selected body plus a picture.
        raise ValueError("Selected HTML is not a single image or link")
    return images[0].get('src', '')


def adapted(mime):
    """Return WebInputs or None when none of these explicit adapters applies."""
    from magic_converter.core.automation.web_inputs import WebInput, from_url, safe_name
    for logical, wide in (("text/x-moz-url", False), ("text/x-moz-url-data", False),
                          ('UniformResourceLocatorW', True), ('UniformResourceLocator', False)):
        for kind in (logical, WINDOWS.format(logical)):
            if mime.hasFormat(kind):
                lines = decoded(raw(mime, kind, 64_000), wide=wide).splitlines()
                urls = lines[::2] if logical == 'text/x-moz-url' else lines
                return tuple(from_url(url.strip()) for url in urls if url.strip())
    for logical in ('DownloadURL', 'application/x-moz-file-promise-url'):
        for kind in (logical, WINDOWS.format(logical)):
            if mime.hasFormat(kind):
                value = decoded(raw(mime, kind, 64_000))
                if logical == 'DownloadURL':
                    media, name, url = value.split(':', 2)
                    if not media or not name:
                        raise ValueError("Invalid DownloadURL")
                    return (WebInput(safe_name(name), url=url),)
                return (from_url(value),)
    kind = WINDOWS.format('FileGroupDescriptorW')
    if not mime.hasFormat(kind):
        return None
    descriptors = raw(mime, kind, 4 + 100 * 592)
    count = struct.unpack_from('<I', descriptors)[0] if len(descriptors) >= 4 else 0
    if not 1 <= count <= 100 or len(descriptors) != 4 + count * 592:
        raise ValueError("Invalid virtual file descriptor count")
    entries = []
    total = 0
    for index in range(count):
        record = descriptors[4 + index * 592:4 + (index + 1) * 592]
        flags, = struct.unpack_from('<I', record)
        attributes, = struct.unpack_from('<I', record, 36)
        high, low = struct.unpack_from('<II', record, 64)
        size = (high << 32) | low
        name = record[72:].decode('utf-16-le').split('\x00', 1)[0]
        total += size
        if (not flags & 0x40 or not 0 < size <= MAX_DATA or total > MAX_DATA
                or flags & 4 and attributes & 0x10 or name != safe_name(name)):
            raise ValueError("Unsafe or unbounded virtual file")
        entries.append((name, size))
    items = []
    for index, (name, size) in enumerate(entries):
        contents = WINDOWS.format('FileContents') + f';index={index}'
        if not mime.hasFormat(contents) and count == 1:
            contents = WINDOWS.format('FileContents')
        data = raw(mime, contents, size)
        if len(data) != size:
            raise ValueError("Virtual file size does not match descriptor")
        if name.lower().endswith('.url'):
            config = configparser.ConfigParser(interpolation=None)
            try:
                config.read_string(decoded(data))
            except configparser.Error as error:
                raise ValueError("Invalid InternetShortcut") from error
            items.append(from_url(config['InternetShortcut']['URL']))
        else:
            items.append(WebInput(name, data=data))
    return tuple(items)
