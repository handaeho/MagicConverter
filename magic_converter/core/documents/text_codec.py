"""Plain-text decoding shared by lightweight and rich document workers."""


def decode_text(data: bytes) -> str:
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        encodings = ("utf-16",)
    elif b"\x00" in data:
        # ASCII text in UTF-16 has a NUL in one byte lane. UTF-8 and CP949
        # accept the same bytes as literal NULs, which would corrupt the text.
        sample = data[:8192]
        even_nulls = sample[::2].count(0)
        odd_nulls = sample[1::2].count(0)
        if len(data) % 2 == 0 and max(even_nulls, odd_nulls) > 3 * min(even_nulls, odd_nulls):
            encodings = ("utf-16-le",) if odd_nulls > even_nulls else ("utf-16-be",)
        else:
            encodings = ()
    else:
        encodings = ("utf-8-sig", "cp949", "utf-16")
    for encoding in encodings:
        try:
            text = data.decode(encoding)
            if "\x00" not in text:
                return text
        except UnicodeError:
            continue
    from magic_converter.core.conversion.converter import ConversionError
    raise ConversionError("텍스트 인코딩을 인식할 수 없습니다.")
