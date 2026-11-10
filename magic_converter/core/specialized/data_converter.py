"""Typed data interchange; XML uses an explicit ordered document envelope."""

import csv
import io
import json
import math
import tomllib
from xml.etree import ElementTree as ET
import tomli_w
import yaml
from magic_converter.core.conversion.converter import ConversionError
from magic_converter.core.jobs.job_control import check_cancelled
from magic_converter.core.jobs.raw_api import bounded_raw
from magic_converter.core.specialized.special_io import read_bytes, text, xml

MAX_ITEMS = 500000

def unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ConversionError(f"중복 키를 덮어쓸 수 없습니다: {key}")
        result[key] = value
    return result


class Loader(yaml.SafeLoader):
    pass


def mapping(loader, node):
    # Merge keys and aliases that would silently overwrite keys are rejected too.
    return unique(
        (loader.construct_object(k, deep=True), loader.construct_object(v, deep=True))
        for k, v in node.value
    )


Loader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, mapping)


def validate(value, depth=0, stack=None, budget=None):
    stack = set() if stack is None else stack
    budget = [0] if budget is None else budget
    budget[0] += 1
    if depth > 64 or budget[0] > MAX_ITEMS:
        raise ConversionError("자료 중첩 깊이/항목 제한을 초과했습니다.")
    if budget[0] % 1024 == 0:
        check_cancelled()
    if type(value) in (dict, list):
        identity = id(value)
        if identity in stack:
            raise ConversionError("순환 YAML 별칭은 변환할 수 없습니다.")
        stack.add(identity)
        if isinstance(value, dict) and any(type(k) is not str for k in value):
            raise ConversionError("객체 키는 문자열이어야 합니다.")
        for child in value.values() if isinstance(value, dict) else value:
            validate(child, depth + 1, stack, budget)
        stack.remove(identity)
    elif type(value) not in (str, int, float, bool, type(None)):
        raise ConversionError(
            "날짜·바이너리·집합 등 JSON에 없는 자료형은 명시적으로 문자열로 바꾼 뒤 변환하세요."
        )
    elif isinstance(value, float) and not math.isfinite(value):
        raise ConversionError("NaN/무한대는 호환 데이터가 아닙니다.")


def _element(e, depth=0):
    if depth > 64:
        raise ConversionError("XML 중첩 깊이 제한을 초과했습니다.")
    return {
        "tag": e.tag,
        "attributes": dict(e.attrib),
        "text": e.text,
        "tail": e.tail,
        "children": [_element(c, depth + 1) for c in e],
    }


def _restore(node, depth=0):
    if (
        depth > 64
        or not isinstance(node, dict)
        or set(node) != {"tag", "attributes", "text", "tail", "children"}
    ):
        raise ConversionError("올바른 $xml 문서 모델이 아닙니다.")
    if (
        not isinstance(node["tag"], str)
        or not isinstance(node["attributes"], dict)
        or not isinstance(node["children"], list)
    ):
        raise ConversionError("올바른 XML 노드가 아닙니다.")
    if any(not isinstance(v, str) for v in node["attributes"].values()) or any(
        node[k] is not None and not isinstance(node[k], str) for k in ("text", "tail")
    ):
        raise ConversionError("XML 속성과 텍스트는 문자열이어야 합니다.")
    e = ET.Element(node["tag"], node["attributes"])
    e.text, e.tail = node["text"], node["tail"]
    e.extend(_restore(c, depth + 1) for c in node["children"])
    return e


@bounded_raw
def read_data(path, kind, notice=lambda _: None):
    if kind == "json":
        result = json.loads(
            text(path),
            object_pairs_hook=unique,
            parse_constant=lambda x: (_ for _ in ()).throw(
                ConversionError(f"잘못된 숫자: {x}")
            ),
        )
    elif kind == "jsonl":
        result = [
            json.loads(line, object_pairs_hook=unique)
            for line in text(path).splitlines()
            if line.strip()
        ]
    elif kind == "yaml":
        source = text(path)
        aliases = 0
        for index, event in enumerate(yaml.parse(source, Loader=Loader)):
            if index % 1024 == 0:
                check_cancelled()
            aliases += isinstance(event, yaml.AliasEvent)
            if aliases > 100:
                raise ConversionError("YAML 별칭이 너무 많습니다.")
        result = yaml.load(source, Loader=Loader)
    elif kind == "toml":
        result = tomllib.loads(text(path))
    elif kind == "xml":
        root = xml(read_bytes(path))
        if (
            root.tag == "magic-data"
            and root.attrib == {"version": "1"}
            and not len(root)
            and root.text
            and root.text.strip()
        ):
            # Only the complete shape emitted by our writer is a typed
            # container. A similarly named ordinary XML tree must survive.
            try:
                result = json.loads(root.text, object_pairs_hook=unique)
            except json.JSONDecodeError:
                result = {"$xml": _element(root)}
                notice("JSON 컨테이너가 아닌 XML을 $xml 문서 모델로 보존합니다.")
        else:
            result = {"$xml": _element(root)}
            notice(
                "XML은 속성·확장 네임스페이스·혼합 텍스트·자식 순서를 $xml 모델로 보존합니다. 주석·처리 지시문·원래 접두사는 보존하지 않습니다."
            )
    elif kind in {"csv", "tsv"}:
        rows = csv.reader(
            io.StringIO(text(path), newline=""),
            delimiter="\t" if kind == "tsv" else ",",
        )
        header = next(rows, None)
        if (
            not header
            or len(set(header)) != len(header)
            or any(not k for k in header)
        ):
            raise ConversionError(
                "CSV/TSV에 비어 있지 않은 고유한 열 이름이 필요합니다."
            )
        result = []
        for index, row in enumerate(rows):
            if index % 1024 == 0:
                check_cancelled()
            if len(row) != len(header):
                raise ConversionError("CSV/TSV의 열 개수가 일치하지 않습니다.")
            # validate() counts the outer list, each row dictionary and each
            # cell value. Enforce the same existing budget before allocating
            # millions of lists/dicts which will inevitably be rejected.
            if 1 + (index + 1) * (1 + len(header)) > MAX_ITEMS:
                raise ConversionError("자료 중첩 깊이/항목 제한을 초과했습니다.")
            result.append(dict(zip(header, row)))
        notice(
            "CSV/TSV 셀은 문자열로 읽습니다. 숫자·날짜·수식으로 자동 해석하지 않습니다."
        )
    else:
        raise ConversionError("지원하지 않는 데이터 형식입니다.")
    validate(result)
    return result


def encode_data(value, kind, notice=lambda _: None):
    validate(value)
    if kind == "json":
        return json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    if kind == "jsonl":
        if not isinstance(value, list):
            raise ConversionError("JSON Lines 출력에는 최상위 배열이 필요합니다.")
        return "".join(
            json.dumps(v, ensure_ascii=False, allow_nan=False) + "\n" for v in value
        )
    if kind == "yaml":
        return yaml.safe_dump(value, allow_unicode=True, sort_keys=False)
    if kind == "toml":
        if not isinstance(value, dict):
            raise ConversionError(
                "TOML에는 최상위 객체가 필요합니다. null 값도 저장할 수 없습니다."
            )

        def check_toml(node):
            if type(node) is int and not -(2**63) <= node < 2**63:
                raise ConversionError("TOML 정수는 부호 있는 64비트 범위여야 합니다.")
            if isinstance(node, (dict, list)):
                for child in node.values() if isinstance(node, dict) else node:
                    check_toml(child)

        check_toml(value)
        try:
            return tomli_w.dumps(value)
        except (TypeError, ValueError) as exc:
            raise ConversionError(
                "TOML로 표현할 수 없는 자료형입니다. null 값은 자동으로 바꾸지 않습니다."
            ) from exc
    if kind == "xml":
        if isinstance(value, dict) and set(value) == {"$xml"}:
            root = _restore(value["$xml"])
        else:
            root = ET.Element("magic-data", version="1")
            root.text = json.dumps(value, ensure_ascii=False, allow_nan=False)
            notice(
                "자료형 보존을 위해 magic-data XML 컨테이너에 JSON을 저장했습니다. 일반 업무 XML 스키마 변환은 아닙니다."
            )
        data = ET.tostring(root, encoding="utf-8", xml_declaration=True)
        xml(data)  # Reject invalid names/characters before publication.
        return data.decode("utf-8")
    if kind in {"csv", "tsv"}:
        if (
            not isinstance(value, list)
            or not value
            or not all(isinstance(row, dict) for row in value)
        ):
            raise ConversionError("CSV/TSV에는 비어 있지 않은 객체 배열이 필요합니다.")
        keys = list(value[0])
        if (
            not keys
            or any(not k for k in keys)
            or any(set(row) != set(keys) for row in value)
        ):
            raise ConversionError(
                "각 행은 동일한 열을 가져야 합니다. 누락된 열은 자동으로 채우지 않습니다."
            )
        if any(isinstance(v, (dict, list)) for row in value for v in row.values()):
            raise ConversionError("중첩 객체/배열은 CSV/TSV로 평탄화하지 않습니다.")
        notice(
            "CSV/TSV에는 자료형이 없습니다. null은 빈 셀, 논리값·숫자는 문자열이 됩니다. 수식처럼 보이는 원문도 그대로 보존합니다."
        )
        output = io.StringIO(newline="")
        writer = csv.writer(output, delimiter="\t" if kind == "tsv" else ",")
        writer.writerow(keys)
        writer.writerows(
            [
                (
                    ""
                    if row[k] is None
                    else (
                        str(row[k]).lower() if isinstance(row[k], bool) else str(row[k])
                    )
                )
                for k in keys
            ]
            for row in value
        )
        return "\ufeff" + output.getvalue()
    raise ConversionError("지원하지 않는 데이터 출력입니다.")


def convert(source, destination, source_kind, target_kind, notice):
    value = read_data(source, source_kind, notice)
    check_cancelled()
    destination.write_text(
        encode_data(value, target_kind, notice), encoding="utf-8", newline=""
    )
