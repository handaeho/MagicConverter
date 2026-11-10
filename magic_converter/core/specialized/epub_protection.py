"""Recognize IDPF font declarations without reading or recovering font bytes."""
from urllib.parse import urlsplit
from magic_converter.core.conversion.converter import ConversionError
from magic_converter.core.specialized.special_io import xml, relative_asset

CONTAINER = 'urn:oasis:names:tc:opendocument:xmlns:container'
ENC = 'http://www.w3.org/2001/04/xmlenc#'
IDPF = 'http://www.idpf.org/2008/embedding'
FONT_TYPES = {'font/ttf', 'font/otf', 'font/woff', 'font/woff2',
              'application/vnd.ms-opentype', 'application/font-sfnt', 'application/font-woff'}


def validate(package, opf_path, manifest, notice):
    if {'META-INF/sinf.xml', 'META-INF/rights.xml', 'META-INF/license.lcpl'} & package.names.keys():
        raise ConversionError('DRM 보호 표식이 있는 EPUB은 변환하지 않습니다. DRM을 해제하지 않습니다.')
    if 'META-INF/encryption.xml' not in package.names:
        return
    root = xml(package.read('META-INF/encryption.xml'))
    def refuse():
        raise ConversionError('지원하지 않거나 잘못된 EPUB 암호화/폰트 난독화 선언입니다. DRM을 해제하지 않습니다.')
    if root.tag != f'{{{CONTAINER}}}encryption' or not len(root):
        refuse()
    resources = {}
    for item in manifest.values():
        name = relative_asset(opf_path, item.get('href', ''))
        resources.setdefault(name, set()).add(item.get('media-type'))
    seen = set()
    for entry in root:
        method = entry.findall(f'{{{ENC}}}EncryptionMethod')
        data = entry.findall(f'{{{ENC}}}CipherData')
        if (entry.tag != f'{{{ENC}}}EncryptedData' or len(entry) != 2 or len(method) != 1 or len(data) != 1
                or method[0].get('Algorithm') != IDPF or len(method[0]) or len(data[0]) != 1):
            refuse()
        ref = data[0][0]
        uri = ref.get('URI', '')
        parsed = urlsplit(uri)
        if ref.tag != f'{{{ENC}}}CipherReference' or len(ref) or parsed.query or parsed.fragment:
            refuse()
        name = relative_asset('', uri)
        if (name in seen or name not in package.names or not resources.get(name)
                or not resources[name] <= FONT_TYPES or not package.names[name].file_size):
            refuse()
        seen.add(name)
    notice('IDPF 폰트 난독화 선언을 확인했습니다. 내장 폰트와 CSS는 생략하며 폰트 난독화를 해제하지 않습니다.')
