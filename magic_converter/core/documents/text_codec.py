"""Plain-text decoding shared by lightweight and rich document workers."""


def decode_text(data: bytes) -> str:
    encodings = ("utf-16", "utf-8-sig", "cp949") if data.startswith((b"\xff\xfe", b"\xfe\xff")) else ("utf-8-sig", "cp949", "utf-16")
    for encoding in encodings:
        try:
            return data.decode(encoding)
        except UnicodeError:
            continue
    from magic_converter.core.conversion.converter import ConversionError
    raise ConversionError("텍스트 인코딩을 인식할 수 없습니다.")
