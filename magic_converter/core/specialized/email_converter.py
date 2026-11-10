"""RFC5322/MIME mail and MHTML with inert HTML and exact attachment bytes."""

import html
import json
import re
import zipfile
from email import policy
from email.parser import BytesParser
from email.header import decode_header
from urllib.parse import unquote
from magic_converter.core.conversion.converter import ConversionError
from magic_converter.core.specialized.special_io import read_bytes
from magic_converter.core.specialized.safe_html import sanitize, page, image_uri
from magic_converter.core.jobs.job_control import check_cancelled


def decode_bytes(data, charset=None):
    for encoding in dict.fromkeys(filter(None, [charset, "utf-8", "cp949", "cp1252"])):
        try:
            return data.decode(encoding)
        except (UnicodeError, LookupError):
            pass
    raise ConversionError("메일 문자 인코딩을 해석할 수 없습니다.")


def header(value):
    if value is None:
        return ""
    result = []
    for fragment, charset in decode_header(value):
        if isinstance(fragment, bytes):
            result.append(decode_bytes(fragment, charset))
        else:
            try:
                result.append(decode_bytes(fragment.encode("ascii", "surrogateescape")))
            except UnicodeEncodeError:
                result.append(fragment)
    return "".join(result)


def payload(part):
    if part.get_content_type() == "message/rfc822":
        items = part.get_payload()
        data = (
            b"\r\n".join(p.as_bytes(policy=policy.SMTP) for p in items)
            if isinstance(items, list)
            else part.as_bytes()
        )
    else:
        data = part.get_payload(decode=True) or b""
    if part.defects:
        raise ConversionError(
            "MIME 데이터가 손상되었습니다: "
            + ", ".join(type(x).__name__ for x in part.defects)
        )
    return data


def load(path):
    raw = read_bytes(path)
    message = BytesParser(policy=policy.default).parsebytes(raw)
    if not list(message.keys()):
        raise ConversionError("메일/MHTML 헤더가 없습니다.")
    parts = list(message.walk())
    if len(parts) > 5000:
        raise ConversionError("MIME 파트가 너무 많습니다.")
    if any(p.defects for p in parts):
        raise ConversionError("메일/MHTML MIME 구조가 중단되었거나 잘못되었습니다.")
    # Encrypted content is not exported as an empty successful message.
    if any(
        p.get_content_type()
        in {"multipart/encrypted", "application/pkcs7-mime", "application/x-pkcs7-mime"}
        for p in parts
    ):
        raise ConversionError(
            "암호화된 메일은 해독 후 사용하세요. 서명/암호화 해제는 지원하지 않습니다."
        )
    return raw, message


def attachments(message):
    result = []

    def visit(part):
        if (
            part.get_content_disposition() == "attachment"
            or part.get_filename()
            or part.get_content_type() == "message/rfc822"
        ):
            result.append(
                (
                    part.get_filename()
                    or (
                        "attached.eml"
                        if part.get_content_type() == "message/rfc822"
                        else "attachment.bin"
                    ),
                    payload(part),
                    part.get_content_type(),
                    str(part.get("Content-ID", "")),
                )
            )
            return
        if part.is_multipart():
            for child in part.iter_parts():
                visit(child)
        elif part.get_content_maintype() == "image":
            result.append(
                (
                    "inline-image.bin",
                    payload(part),
                    part.get_content_type(),
                    str(part.get("Content-ID", "")),
                )
            )

    visit(message)
    return result


def mail_body(message, notice):
    part = message.get_body(preferencelist=("html", "plain"))
    if part is None:
        raise ConversionError("메일/MHTML에서 표시할 본문을 찾지 못했습니다.")
    body = decode_bytes(payload(part), part.get_content_charset())
    if part.get_content_type() == "text/plain":
        return "<pre>" + html.escape(body) + "</pre>"
    resources = {}
    for p in message.walk():
        if p.get_content_maintype() != "image":
            continue
        cid = str(p.get("Content-ID", "")).strip("<>")
        location = str(p.get("Content-Location", ""))
        if cid:
            resources["cid:" + cid.casefold()] = p
        if location:
            resources[location] = p
    cache = {}

    def resolver(src):
        if src[:4].lower() == "cid:":
            src = "cid:" + unquote(src[4:]).casefold()
        if src not in resources:
            return ""
        if src not in cache:
            cache[src] = image_uri(payload(resources[src]))
        return cache[src]

    notice(
        "메일 HTML의 스크립트·CSS·폼·외부 이미지/리소스를 제거했습니다. CID/Content-Location 내장 래스터 이미지는 포함합니다."
    )
    return sanitize(body, resolver)


def convert(source, destination, source_kind, target_kind, notice):
    raw, message = load(source)
    headers = []
    for key, value in message.raw_items():
        if key.lower() in {"from", "to", "cc", "bcc", "date", "subject"}:
            headers.append((key, header(value)))
    if target_kind == "zip":
        files = attachments(message)
        manifest = []
        with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("original.eml", raw)
            for i, (name, data, mime, cid) in enumerate(files):
                check_cancelled()
                clean = (
                    re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")[:120]
                    or "attachment.bin"
                )
                target = f"attachments/{i+1:04}_{clean}"
                z.writestr(target, data)
                import hashlib

                manifest.append(
                    {
                        "original_name": name,
                        "path": target,
                        "mime": mime,
                        "content_id": cid,
                        "bytes": len(data),
                        "sha256": hashlib.sha256(data).hexdigest(),
                    }
                )
            z.writestr(
                "manifest.json",
                json.dumps(
                    {"headers": headers, "attachments": manifest},
                    ensure_ascii=False,
                    indent=2,
                ).encode("utf-8"),
            )
        notice(
            "원본 EML과 첨부파일의 디코딩된 바이트를 ZIP에 보관했습니다. 서명은 검증하지 않습니다. 중첩 메일 첨부는 MIME로 다시 직렬화합니다."
        )
        return
    body = mail_body(message, notice)
    if source_kind == "eml":
        notice(
            "HTML/TXT에는 일반 첨부파일이 포함되지 않습니다. 메일 보관 ZIP을 사용하면 원본과 첨부를 함께 보존합니다. 메일 서명은 검증하지 않습니다."
        )
    if target_kind == "html":
        table = (
            "<table>"
            + "".join(
                f"<tr><th>{html.escape(k)}</th><td>{html.escape(v)}</td></tr>"
                for k, v in headers
            )
            + "</table><hr>"
        )
        destination.write_text(
            page(
                next((v for k, v in headers if k.lower() == "subject"), source.stem),
                table + body,
            ),
            encoding="utf-8",
        )
    else:
        from bs4 import BeautifulSoup

        result = (
            "\n".join(k + ": " + v for k, v in headers)
            + "\n\n"
            + BeautifulSoup(body, "html.parser").get_text("\n", strip=True)
        )
        destination.write_text(result, encoding="utf-8")
    check_cancelled()
