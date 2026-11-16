//! Typed data interchange. Retain object order and exact JSON integer literals.

mod yaml;

use encoding_rs::{EUC_KR, UTF_16BE, UTF_16LE};
use roxmltree::{Document, Node, ParsingOptions};
use serde::de::{self, Deserializer, MapAccess, Visitor};
use serde_json::value::RawValue;
use serde_json::{Map, Value, json};
use std::collections::HashSet;
use std::fmt;
use std::fs::File;
use std::io::{self, ErrorKind, Read, Write};
use std::path::Path;

const MAX_FILE: u64 = 64 * 1024 * 1024;
const MAX_ITEMS: usize = 500_000;
const LIMIT: &str = "자료 중첩 깊이/항목 제한을 초과했습니다.";
const XML_LIMIT: &str = "XML 중첩 깊이/항목 제한을 초과했습니다.";
const UNSUPPORTED_TYPE: &str =
    "날짜·바이너리·집합 등 JSON에 없는 자료형은 명시적으로 문자열로 바꾼 뒤 변환하세요.";
type Check<'a> = &'a dyn Fn() -> io::Result<()>;

pub fn supports(source: &str, target: &str) -> bool {
    let direct = |format| {
        matches!(
            format,
            "json" | "jsonl" | "toml" | "xml" | "csv" | "tsv" | "yaml"
        )
    };
    direct(source) && direct(target)
}

fn invalid(message: impl Into<String>) -> io::Error {
    io::Error::new(ErrorKind::InvalidData, message.into())
}

struct Budget<'a> {
    check: Check<'a>,
    items: usize,
}

impl Budget<'_> {
    fn advance(&mut self, depth: usize) -> io::Result<()> {
        self.items += 1;
        if depth > 64 || self.items > MAX_ITEMS {
            return Err(invalid(LIMIT));
        }
        if self.items.is_multiple_of(1024) {
            (self.check)()?;
        }
        Ok(())
    }
}

fn validate_model(value: &Value, depth: usize, budget: &mut Budget<'_>) -> io::Result<()> {
    budget.advance(depth)?;
    match value {
        Value::Array(values) => {
            for value in values {
                validate_model(value, depth + 1, budget)?;
            }
        }
        Value::Object(values) => {
            for value in values.values() {
                validate_model(value, depth + 1, budget)?;
            }
        }
        _ => {}
    }
    Ok(())
}

pub(crate) fn bytes(path: &Path) -> io::Result<Vec<u8>> {
    let file = File::open(path)?;
    if file.metadata()?.len() > MAX_FILE {
        return Err(invalid("이 형식은 파일당 64 MiB까지 처리합니다."));
    }
    let mut bytes = Vec::new();
    file.take(MAX_FILE + 1).read_to_end(&mut bytes)?;
    if bytes.len() as u64 > MAX_FILE {
        return Err(invalid("입력 크기 제한을 초과했습니다."));
    }
    Ok(bytes)
}

pub(crate) fn text(input: &[u8]) -> io::Result<String> {
    let encoding_error = || invalid("텍스트 인코딩을 인식할 수 없습니다.");
    for (bom, encoding) in [(b"\xff\xfe", UTF_16LE), (b"\xfe\xff", UTF_16BE)] {
        if let Some(body) = input.strip_prefix(bom) {
            return encoding
                .decode_without_bom_handling_and_without_replacement(body)
                .map(|text| text.into_owned())
                .ok_or_else(encoding_error);
        }
    }
    if let Ok(text) = std::str::from_utf8(input.strip_prefix(b"\xef\xbb\xbf").unwrap_or(input)) {
        return Ok(text.to_owned());
    }
    EUC_KR
        .decode_without_bom_handling_and_without_replacement(input)
        .map(|text| text.into_owned())
        .ok_or_else(encoding_error)
}

pub(crate) fn parse_json(text: &str, check: Check<'_>) -> io::Result<Value> {
    json_value(
        serde_json::from_str(text)?,
        0,
        &mut Budget { check, items: 0 },
    )
}

fn json_value(raw: &RawValue, depth: usize, budget: &mut Budget<'_>) -> io::Result<Value> {
    budget.advance(depth)?;
    let content = raw.get();
    if content.starts_with('{') {
        struct Object<'a, 'b> {
            depth: usize,
            budget: &'a mut Budget<'b>,
        }
        impl<'de> Visitor<'de> for Object<'_, '_> {
            type Value = io::Result<Value>;
            fn expecting(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
                formatter.write_str("a JSON object")
            }
            fn visit_map<A: MapAccess<'de>>(self, mut object: A) -> Result<Self::Value, A::Error> {
                let mut items = Map::new();
                while let Some((key, raw)) = object.next_entry::<String, &RawValue>()? {
                    if items.contains_key(&key) {
                        return Ok(Err(invalid(format!("중복 키를 덮어쓸 수 없습니다: {key}"))));
                    }
                    match json_value(raw, self.depth + 1, self.budget) {
                        Ok(value) => {
                            items.insert(key, value);
                        }
                        Err(error) => return Ok(Err(error)),
                    }
                }
                Ok(Ok(Value::Object(items)))
            }
        }
        let mut parser = serde_json::Deserializer::from_str(content);
        return parser
            .deserialize_map(Object { depth, budget })
            .map_err(io::Error::from)?;
    }
    if content.starts_with('[') {
        struct Array<'a, 'b> {
            depth: usize,
            budget: &'a mut Budget<'b>,
        }
        impl<'de> Visitor<'de> for Array<'_, '_> {
            type Value = io::Result<Value>;
            fn expecting(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
                formatter.write_str("a JSON array")
            }
            fn visit_seq<A: de::SeqAccess<'de>>(
                self,
                mut array: A,
            ) -> Result<Self::Value, A::Error> {
                let mut items = Vec::new();
                while let Some(raw) = array.next_element::<&RawValue>()? {
                    match json_value(raw, self.depth + 1, self.budget) {
                        Ok(value) => items.push(value),
                        Err(error) => return Ok(Err(error)),
                    }
                }
                Ok(Ok(Value::Array(items)))
            }
        }
        let mut parser = serde_json::Deserializer::from_str(content);
        return parser
            .deserialize_seq(Array { depth, budget })
            .map_err(io::Error::from)?;
    }
    let value: Value = serde_json::from_str(content)?;
    if value.is_number()
        && content.contains(['.', 'e', 'E'])
        && value.as_f64().is_none_or(|number| !number.is_finite())
    {
        return Err(invalid("NaN/무한대는 호환 데이터가 아닙니다."));
    }
    Ok(value)
}

fn toml_value(value: toml::Value, depth: usize, budget: &mut Budget<'_>) -> io::Result<Value> {
    budget.advance(depth)?;
    Ok(match value {
        toml::Value::String(value) => Value::String(value),
        toml::Value::Integer(value) => json!(value),
        toml::Value::Float(value) if value.is_finite() => json!(value),
        toml::Value::Float(_) => return Err(invalid("NaN/무한대는 호환 데이터가 아닙니다.")),
        toml::Value::Boolean(value) => Value::Bool(value),
        toml::Value::Datetime(_) => return Err(invalid(UNSUPPORTED_TYPE)),
        toml::Value::Array(items) => Value::Array(
            items
                .into_iter()
                .map(|item| toml_value(item, depth + 1, budget))
                .collect::<io::Result<_>>()?,
        ),
        toml::Value::Table(items) => Value::Object(
            items
                .into_iter()
                .map(|(key, item)| Ok((key, toml_value(item, depth + 1, budget)?)))
                .collect::<io::Result<_>>()?,
        ),
    })
}

fn to_toml(value: Value) -> io::Result<toml::Value> {
    Ok(match value {
        Value::String(value) => toml::Value::String(value),
        Value::Number(value) => {
            let spelling = value.to_string();
            if spelling.contains(['.', 'e', 'E']) {
                toml::Value::Float(
                    value
                        .as_f64()
                        .ok_or_else(|| invalid("NaN/무한대는 호환 데이터가 아닙니다."))?,
                )
            } else {
                toml::Value::Integer(
                    value
                        .as_i64()
                        .ok_or_else(|| invalid("TOML 정수는 부호 있는 64비트 범위여야 합니다."))?,
                )
            }
        }
        Value::Bool(value) => toml::Value::Boolean(value),
        Value::Null => {
            return Err(invalid(
                "TOML로 표현할 수 없는 자료형입니다. null 값은 자동으로 바꾸지 않습니다.",
            ));
        }
        Value::Array(items) => {
            toml::Value::Array(items.into_iter().map(to_toml).collect::<io::Result<_>>()?)
        }
        Value::Object(items) => toml::Value::Table(
            items
                .into_iter()
                .map(|(key, item)| Ok((key, to_toml(item)?)))
                .collect::<io::Result<_>>()?,
        ),
    })
}

fn csv_read(source: &str, delimiter: u8, budget: &mut Budget<'_>) -> io::Result<Value> {
    let mut reader = csv::ReaderBuilder::new()
        .delimiter(delimiter)
        .has_headers(false)
        .flexible(true)
        .from_reader(source.as_bytes());
    let mut rows = reader.records();
    let header = rows
        .next()
        .transpose()
        .map_err(io::Error::other)?
        .unwrap_or_default();
    let distinct: HashSet<_> = header.iter().collect();
    if header.is_empty() || distinct.len() != header.len() || distinct.contains("") {
        return Err(invalid(
            "CSV/TSV에 비어 있지 않은 고유한 열 이름이 필요합니다.",
        ));
    }
    budget.advance(0)?;
    let mut result = Vec::new();
    for row in rows {
        let row = row.map_err(io::Error::other)?;
        if row.len() != header.len() {
            return Err(invalid("CSV/TSV의 열 개수가 일치하지 않습니다."));
        }
        budget.advance(1)?;
        let mut values = Map::new();
        for (key, value) in header.iter().zip(row.iter()) {
            budget.advance(2)?;
            values.insert(key.to_owned(), Value::String(value.to_owned()));
        }
        result.push(Value::Object(values));
    }
    Ok(Value::Array(result))
}

fn csv_write(value: Value, delimiter: u8, output: &mut File, check: Check<'_>) -> io::Result<()> {
    let rows = value
        .as_array()
        .filter(|rows| !rows.is_empty() && rows.iter().all(Value::is_object))
        .ok_or_else(|| invalid("CSV/TSV에는 비어 있지 않은 객체 배열이 필요합니다."))?;
    let keys: Vec<_> = rows[0].as_object().expect("CSV rows").keys().collect();
    if keys.is_empty()
        || keys.iter().any(|key| key.is_empty())
        || rows.iter().any(|row| {
            row.as_object().is_none_or(|values| {
                values.len() != keys.len()
                    || keys.iter().any(|key| !values.contains_key(key.as_str()))
            })
        })
    {
        return Err(invalid(
            "각 행은 동일한 열을 가져야 합니다. 누락된 열은 자동으로 채우지 않습니다.",
        ));
    }
    output.write_all(b"\xef\xbb\xbf")?;
    let mut writer = csv::WriterBuilder::new()
        .delimiter(delimiter)
        .terminator(csv::Terminator::CRLF)
        .from_writer(output);
    writer.write_record(&keys).map_err(io::Error::other)?;
    for row in rows {
        check()?;
        let mut cells = Vec::new();
        for key in &keys {
            let cell = match &row[key.as_str()] {
                Value::String(text) => text.to_owned(),
                Value::Null => String::new(),
                Value::Bool(_) | Value::Number(_) => row[key.as_str()].to_string(),
                _ => return Err(invalid("중첩 객체/배열은 CSV/TSV로 평탄화하지 않습니다.")),
            };
            cells.push(cell);
        }
        writer.write_record(cells).map_err(io::Error::other)?;
    }
    writer.flush()
}

fn document(source: &str) -> io::Result<Document<'_>> {
    let mut remainder = source;
    while let Some(start) = remainder.find('<') {
        remainder = &remainder[start..];
        if remainder.starts_with("<!DOCTYPE") {
            return Err(invalid("XML DTD는 허용되지 않습니다."));
        }
        let special = [("<!--", "-->"), ("<![CDATA[", "]]>"), ("<?", "?>")];
        if let Some((opening, closing)) = special
            .iter()
            .find(|(opening, _)| remainder.starts_with(opening))
        {
            remainder = remainder[opening.len()..]
                .split_once(closing)
                .map_or("", |(_, rest)| rest);
        } else {
            remainder = &remainder[1..];
        }
    }
    Document::parse_with_options(
        source,
        ParsingOptions {
            allow_dtd: false,
            nodes_limit: 1_500_000,
            ..ParsingOptions::default()
        },
    )
    .map_err(|error| invalid(error.to_string()))
}

fn clark(namespace: Option<&str>, name: &str) -> String {
    namespace.map_or_else(
        || name.to_owned(),
        |namespace| format!("{{{namespace}}}{name}"),
    )
}

fn mixed_text<'a, 'input: 'a>(nodes: impl Iterator<Item = Node<'a, 'input>>) -> Option<String> {
    let value = nodes
        .take_while(|node| !node.is_element())
        .filter(Node::is_text)
        .filter_map(|node| node.text())
        .collect::<String>();
    (!value.is_empty()).then_some(value)
}

fn xml_node(element: Node<'_, '_>, depth: usize, budget: &mut Budget<'_>) -> io::Result<Value> {
    budget.advance(depth).map_err(|error| {
        if error.to_string() == LIMIT {
            invalid(XML_LIMIT)
        } else {
            error
        }
    })?;
    let tag = clark(element.tag_name().namespace(), element.tag_name().name());
    let attributes: Map<_, _> = element
        .attributes()
        .map(|attribute| {
            (
                clark(attribute.namespace(), attribute.name()),
                json!(attribute.value()),
            )
        })
        .collect();
    let children = element
        .children()
        .filter(Node::is_element)
        .map(|child| xml_node(child, depth + 1, budget))
        .collect::<io::Result<Vec<_>>>()?;
    let tail = element
        .parent()
        .filter(Node::is_element)
        .and_then(|_| mixed_text(element.next_siblings().skip(1)));
    Ok(
        json!({"tag": tag, "attributes": attributes, "text": mixed_text(element.children()),
        "tail": tail, "children": children}),
    )
}

fn xml_read(source: &str, budget: &mut Budget<'_>, notices: &mut Vec<String>) -> io::Result<Value> {
    let doc = document(source)?;
    let root = doc.root_element();
    if root.tag_name().namespace().is_none()
        && root.tag_name().name() == "magic-data"
        && root.attributes().len() == 1
        && root.attribute("version") == Some("1")
        && !root.children().any(|node| node.is_element())
        && let Some(content) =
            mixed_text(root.children()).filter(|content| !content.trim().is_empty())
    {
        if let Ok(value) = serde_json::from_str::<&RawValue>(&content) {
            return json_value(value, 0, budget);
        }
        notices.push("JSON 컨테이너가 아닌 XML을 $xml 문서 모델로 보존합니다.".to_owned());
    } else {
        notices.push("XML은 속성·확장 네임스페이스·혼합 텍스트·자식 순서를 $xml 모델로 보존합니다. 주석·처리 지시문·원래 접두사는 보존하지 않습니다.".to_owned());
    }
    Ok(json!({"$xml": xml_node(root, 0, budget)?}))
}

fn escape(value: &str, attribute: bool) -> String {
    let mut output = String::new();
    for character in value.chars() {
        match character {
            '&' => output.push_str("&amp;"),
            '<' => output.push_str("&lt;"),
            '>' => output.push_str("&gt;"),
            '"' if attribute => output.push_str("&quot;"),
            '\r' if attribute => output.push_str("&#13;"),
            '\n' if attribute => output.push_str("&#10;"),
            '\t' if attribute => output.push_str("&#9;"),
            _ => output.push(character),
        }
    }
    output
}

fn qname(name: &str, namespaces: &mut Vec<String>) -> io::Result<String> {
    let invalid_name = || invalid("올바른 XML 노드가 아닙니다.");
    let valid = |part: &str| {
        let start = |ch: char| {
            ch == '_'
                || matches!(ch, 'A'..='Z' | 'a'..='z' | '\u{c0}'..='\u{d6}'
            | '\u{d8}'..='\u{f6}' | '\u{f8}'..='\u{2ff}' | '\u{370}'..='\u{37d}' | '\u{37f}'..='\u{1fff}'
            | '\u{200c}'..='\u{200d}' | '\u{2070}'..='\u{218f}' | '\u{2c00}'..='\u{2fef}'
            | '\u{3001}'..='\u{d7ff}' | '\u{f900}'..='\u{fdcf}' | '\u{fdf0}'..='\u{fffd}' | '\u{10000}'..='\u{effff}')
        };
        let mut characters = part.chars();
        characters.next().is_some_and(start)
            && characters.all(|ch| {
                start(ch)
                    || matches!(ch,
            '-' | '.' | '0'..='9' | '\u{b7}' | '\u{300}'..='\u{36f}' | '\u{203f}'..='\u{2040}')
            })
    };
    let Some(clark) = name.strip_prefix('{') else {
        let mut parts = name.split(':');
        return if parts.next().is_some_and(valid)
            && parts.next().is_none_or(valid)
            && parts.next().is_none()
        {
            Ok(name.to_owned())
        } else {
            Err(invalid_name())
        };
    };
    let (namespace, local) = clark
        .split_once('}')
        .ok_or_else(|| invalid("올바른 XML 노드가 아닙니다."))?;
    if !valid(local) || namespace.is_empty() {
        return Err(invalid_name());
    }
    if namespace == "http://www.w3.org/XML/1998/namespace" {
        return Ok(format!("xml:{local}"));
    }
    let index = match namespaces.iter().position(|uri| uri == namespace) {
        Some(index) => index,
        None => {
            let index = namespaces.len();
            namespaces.push(namespace.to_owned());
            index
        }
    };
    Ok(format!("ns{index}:{local}"))
}

fn render_xml(
    node: &Value,
    depth: usize,
    namespaces: &mut Vec<String>,
    output: &mut String,
    check: Check<'_>,
    root_attributes: &mut usize,
) -> io::Result<()> {
    check()?;
    let keys = ["tag", "attributes", "text", "tail", "children"];
    if depth > 64
        || node.as_object().is_none_or(|object| {
            object.len() != keys.len() || keys.iter().any(|key| !object.contains_key(*key))
        })
    {
        return Err(invalid("올바른 $xml 문서 모델이 아닙니다."));
    }
    let invalid_node = || invalid("올바른 XML 노드가 아닙니다.");
    let invalid_text = || invalid("XML 속성과 텍스트는 문자열이어야 합니다.");
    let name = qname(node["tag"].as_str().ok_or_else(invalid_node)?, namespaces)?;
    let attributes = node["attributes"].as_object().ok_or_else(invalid_node)?;
    let children = node["children"].as_array().ok_or_else(invalid_node)?;
    let text = |key: &str| {
        if node[key].is_null() {
            Ok("")
        } else {
            node[key].as_str().ok_or_else(invalid_text)
        }
    };
    output.push('<');
    output.push_str(&name);
    if depth == 0 {
        *root_attributes = output.len();
    }
    for (key, value) in attributes {
        output.push(' ');
        output.push_str(&qname(key, namespaces)?);
        output.push_str("=\"");
        output.push_str(&escape(value.as_str().ok_or_else(invalid_text)?, true));
        output.push('"');
    }
    output.push('>');
    output.push_str(&escape(text("text")?, false));
    for child in children {
        render_xml(child, depth + 1, namespaces, output, check, root_attributes)?;
    }
    output.push_str("</");
    output.push_str(&name);
    output.push('>');
    output.push_str(&escape(text("tail")?, false));
    if output.len() as u64 > MAX_FILE {
        return Err(invalid("XML 크기 제한을 초과했습니다."));
    }
    Ok(())
}

fn xml_write(value: &Value, notices: &mut Vec<String>, check: Check<'_>) -> io::Result<String> {
    let mut xml = "<?xml version='1.0' encoding='utf-8'?>\n".to_owned();
    if value
        .as_object()
        .is_some_and(|value| value.len() == 1 && value.contains_key("$xml"))
    {
        let mut namespaces = Vec::new();
        let mut insertion = 0;
        render_xml(
            &value["$xml"],
            0,
            &mut namespaces,
            &mut xml,
            check,
            &mut insertion,
        )?;
        let declarations: String = namespaces
            .iter()
            .enumerate()
            .map(|(index, namespace)| format!(" xmlns:ns{index}=\"{}\"", escape(namespace, true)))
            .collect();
        xml.insert_str(insertion, &declarations);
    } else {
        xml.push_str("<magic-data version=\"1\">");
        xml.push_str(&escape(&serde_json::to_string(value)?, false));
        xml.push_str("</magic-data>");
        notices.push("자료형 보존을 위해 magic-data XML 컨테이너에 JSON을 저장했습니다. 일반 업무 XML 스키마 변환은 아닙니다.".to_owned());
    }
    if xml.len() as u64 > MAX_FILE {
        return Err(invalid("XML 크기 제한을 초과했습니다."));
    }
    document(&xml)?;
    Ok(xml)
}

pub fn convert(
    path: &Path,
    input: &str,
    target: &str,
    output: &mut File,
    check: Check<'_>,
    notices: &mut Vec<String>,
) -> io::Result<()> {
    let source = text(&bytes(path)?)?;
    check()?;
    let mut budget = Budget { check, items: 0 };
    let data = match input {
        "json" => json_value(serde_json::from_str(&source)?, 0, &mut budget)?,
        "jsonl" => {
            budget.advance(0)?;
            Value::Array(
                source
                    .lines()
                    .filter(|line| !line.trim().is_empty())
                    .map(|line| json_value(serde_json::from_str(line)?, 1, &mut budget))
                    .collect::<io::Result<_>>()?,
            )
        }
        "toml" => toml_value(
            toml::from_str(&source).map_err(io::Error::other)?,
            0,
            &mut budget,
        )?,
        "yaml" => yaml::read(&source, &mut budget)?,
        "xml" => xml_read(&source, &mut budget, notices)?,
        "csv" | "tsv" => {
            let data = csv_read(
                &source,
                if input == "csv" { b',' } else { b'\t' },
                &mut budget,
            )?;
            notices.push(
                "CSV/TSV 셀은 문자열로 읽습니다. 숫자·날짜·수식으로 자동 해석하지 않습니다."
                    .to_owned(),
            );
            data
        }
        _ => return Err(invalid("지원하지 않는 데이터 형식입니다.")),
    };
    if input == "xml" {
        validate_model(&data, 0, &mut Budget { check, items: 0 })?;
    }
    check()?;
    match target {
        "json" => {
            serde_json::to_writer_pretty(&mut *output, &data)?;
            output.write_all(b"\n")?;
        }
        "jsonl" => {
            let rows = data
                .as_array()
                .ok_or_else(|| invalid("JSON Lines 출력에는 최상위 배열이 필요합니다."))?;
            for row in rows {
                check()?;
                serde_json::to_writer(&mut *output, row)?;
                output.write_all(b"\n")?;
            }
        }
        "toml" => {
            if !data.is_object() {
                return Err(invalid(
                    "TOML에는 최상위 객체가 필요합니다. null 값도 저장할 수 없습니다.",
                ));
            }
            output.write_all(
                toml::to_string(&to_toml(data)?)
                    .map_err(io::Error::other)?
                    .as_bytes(),
            )?;
        }
        "yaml" => {
            yaml::write(&data, output, check)?;
            output.write_all(b"\n")?;
        }
        "xml" => output.write_all(xml_write(&data, notices, check)?.as_bytes())?,
        "csv" | "tsv" => {
            csv_write(
                data,
                if target == "csv" { b',' } else { b'\t' },
                output,
                check,
            )?;
            notices.push("CSV/TSV에는 자료형이 없습니다. null은 빈 셀, 논리값·숫자는 문자열이 됩니다. 수식처럼 보이는 원문도 그대로 보존합니다.".to_owned());
        }
        _ => return Err(invalid("지원하지 않는 데이터 출력입니다.")),
    }
    if output.metadata()?.len() > 256 * 1024 * 1024 {
        return Err(invalid("작업 결과 파일 크기가 제한을 초과했습니다."));
    }
    check()
}
