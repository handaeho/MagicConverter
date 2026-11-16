use super::{Budget, Check, UNSUPPORTED_TYPE, invalid, validate_model};
use num_bigint::BigInt;
use regex::Regex;
use serde_json::{Map, Value, json};
use std::collections::HashMap;
use std::io::{self, Write};
use std::sync::OnceLock;
use yaml_rust2::parser::{Event, Parser, Tag};
use yaml_rust2::scanner::TScalarStyle;

fn invalid_yaml() -> io::Error {
    invalid("YAML 문서 구조가 올바르지 않습니다.")
}

fn implicit(text: &str) -> &'static str {
    const RULES: &[(&str, &str)] = &[
        (
            "bool",
            r"^(?:yes|Yes|YES|no|No|NO|true|True|TRUE|false|False|FALSE|on|On|ON|off|Off|OFF)$",
        ),
        (
            "float",
            r"^(?:[-+]?(?:[0-9][0-9_]*)\.[0-9_]*(?:[eE][-+][0-9]+)?|\.[0-9][0-9_]*(?:[eE][-+][0-9]+)?|[-+]?[0-9][0-9_]*(?::[0-5]?[0-9])+\.[0-9_]*|[-+]?\.(?:inf|Inf|INF)|\.(?:nan|NaN|NAN))$",
        ),
        (
            "int",
            r"^(?:[-+]?0b[0-1_]+|[-+]?0[0-7_]+|[-+]?(?:0|[1-9][0-9_]*)|[-+]?0x[0-9a-fA-F_]+|[-+]?[1-9][0-9_]*(?::[0-5]?[0-9])+)$",
        ),
        ("merge", r"^<<$"),
        ("null", r"^(?:~|null|Null|NULL|)$"),
        (
            "timestamp",
            r"^(?:[0-9]{4}-[0-9]{2}-[0-9]{2}|[0-9]{4}-[0-9]{1,2}-[0-9]{1,2}(?:[Tt]|[ \t]+)[0-9]{1,2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]*)?(?:[ \t]*(?:Z|[-+][0-9]{1,2}(?::[0-9]{2})?))?)$",
        ),
    ];
    static COMPILED: OnceLock<Vec<(&'static str, Regex)>> = OnceLock::new();
    COMPILED
        .get_or_init(|| {
            RULES
                .iter()
                .map(|&(name, pattern)| (name, Regex::new(pattern).expect("static YAML resolver")))
                .collect()
        })
        .iter()
        .find(|(_, pattern)| pattern.is_match(text))
        .map_or("str", |(name, _)| *name)
}

fn tag(value: Option<&Tag>) -> io::Result<Option<&str>> {
    match value {
        Some(tag) if matches!(tag.handle.as_str(), "!!" | "tag:yaml.org,2002:") => {
            Ok(Some(&tag.suffix))
        }
        Some(tag) if tag.handle == "!" && tag.suffix.is_empty() => Ok(None),
        Some(_) => Err(invalid(UNSUPPORTED_TYPE)),
        None => Ok(None),
    }
}

fn yaml_integer(value: &str) -> io::Result<Value> {
    let value = value.replace('_', "");
    let digits = value.trim_start_matches(['-', '+']);
    let parse =
        |digits: &str, base| BigInt::parse_bytes(digits.as_bytes(), base).ok_or_else(invalid_yaml);
    let result = if let Some(binary) = digits.strip_prefix("0b") {
        parse(binary, 2)?
    } else if let Some(hex) = digits.strip_prefix("0x") {
        parse(hex, 16)?
    } else if digits.len() > 1 && digits.starts_with('0') {
        parse(&digits[1..], 8)?
    } else if digits.contains(':') {
        let mut result = BigInt::from(0);
        for part in digits.split(':') {
            result = result * 60 + parse(part, 10)?;
        }
        result
    } else {
        parse(digits, 10)?
    };
    let result = if value.starts_with('-') {
        -result
    } else {
        result
    };
    serde_json::from_str(&result.to_string()).map_err(io::Error::from)
}

fn yaml_float(value: &str) -> io::Result<Value> {
    let value = value.replace('_', "").to_ascii_lowercase();
    let digits = value.trim_start_matches(['-', '+']);
    if matches!(digits, ".inf" | ".nan") {
        return Err(invalid("NaN/무한대는 호환 데이터가 아닙니다."));
    }
    let mut result = 0.0_f64;
    for part in digits.split(':') {
        result = result * 60.0 + part.parse::<f64>().map_err(|_| invalid_yaml())?;
    }
    if value.starts_with('-') {
        result = -result;
    }
    if !result.is_finite() {
        return Err(invalid("NaN/무한대는 호환 데이터가 아닙니다."));
    }
    Ok(json!(result))
}

fn scalar(value: String, style: TScalarStyle, explicit: Option<&Tag>) -> io::Result<Value> {
    let kind = tag(explicit)?.unwrap_or_else(|| {
        if style == TScalarStyle::Plain {
            implicit(&value)
        } else {
            "str"
        }
    });
    match kind {
        "null" => Ok(Value::Null),
        "str" => Ok(Value::String(value)),
        "bool" => match value.to_ascii_lowercase().as_str() {
            "yes" | "true" | "on" => Ok(Value::Bool(true)),
            "no" | "false" | "off" => Ok(Value::Bool(false)),
            _ => Err(invalid_yaml()),
        },
        "int" => yaml_integer(&value),
        "float" => yaml_float(&value),
        _ => Err(invalid(UNSUPPORTED_TYPE)),
    }
}

struct Reader<'a> {
    parser: Parser<std::str::Chars<'a>>,
    anchors: HashMap<usize, Value>,
    alias_count: usize,
    anchor_budget: Budget<'a>,
}

impl Reader<'_> {
    fn next(&mut self) -> io::Result<Event> {
        self.parser
            .next_token()
            .map(|(event, _)| event)
            .map_err(io::Error::other)
    }

    fn peek(&mut self) -> io::Result<&Event> {
        self.parser
            .peek()
            .map(|(event, _)| event)
            .map_err(io::Error::other)
    }

    fn node(&mut self, depth: usize, is_value: bool, budget: &mut Budget<'_>) -> io::Result<Value> {
        if is_value {
            budget.advance(depth)?;
        } else if !matches!(self.peek()?, Event::Scalar(..) | Event::Alias(_)) {
            return Err(invalid("객체 키는 문자열이어야 합니다."));
        }
        let (anchor, value) = match self.next()? {
            Event::Alias(anchor) => {
                self.alias_count += 1;
                if self.alias_count > 100 {
                    return Err(invalid("YAML 별칭이 너무 많습니다."));
                }
                let value = self
                    .anchors
                    .get(&anchor)
                    .ok_or_else(|| invalid("순환 YAML 별칭은 변환할 수 없습니다."))?;
                if is_value {
                    budget.items -= 1;
                    validate_model(value, depth, budget)?;
                }
                return Ok(value.clone());
            }
            Event::Scalar(value, style, anchor, explicit) => {
                (anchor, scalar(value, style, explicit.as_ref())?)
            }
            Event::SequenceStart(anchor, explicit) => {
                if tag(explicit.as_ref())?.is_some_and(|tag| tag != "seq") {
                    return Err(invalid(UNSUPPORTED_TYPE));
                }
                let mut values = Vec::new();
                while self.peek()? != &Event::SequenceEnd {
                    values.push(self.node(depth + 1, true, budget)?);
                }
                self.next()?;
                (anchor, Value::Array(values))
            }
            Event::MappingStart(anchor, explicit) => {
                if tag(explicit.as_ref())?.is_some_and(|tag| tag != "map") {
                    return Err(invalid(UNSUPPORTED_TYPE));
                }
                let mut values = Map::new();
                while self.peek()? != &Event::MappingEnd {
                    let Value::String(key) = self.node(depth + 1, false, budget)? else {
                        return Err(invalid("객체 키는 문자열이어야 합니다."));
                    };
                    if values.contains_key(&key) {
                        return Err(invalid(format!("중복 키를 덮어쓸 수 없습니다: {key}")));
                    }
                    values.insert(key, self.node(depth + 1, true, budget)?);
                }
                self.next()?;
                (anchor, Value::Object(values))
            }
            _ => return Err(invalid_yaml()),
        };
        if anchor != 0 {
            validate_model(&value, 0, &mut self.anchor_budget)?;
            self.anchors.insert(anchor, value.clone());
        }
        Ok(value)
    }
}

pub fn read(source: &str, budget: &mut Budget<'_>) -> io::Result<Value> {
    let mut reader = Reader {
        parser: Parser::new_from_str(source),
        anchors: HashMap::new(),
        alias_count: 0,
        anchor_budget: Budget {
            check: budget.check,
            items: 0,
        },
    };
    if reader.next()? != Event::StreamStart {
        return Err(invalid_yaml());
    }
    match reader.next()? {
        Event::StreamEnd => Ok(Value::Null),
        Event::DocumentStart => {
            let value = reader.node(0, true, budget)?;
            if reader.next()? != Event::DocumentEnd || reader.next()? != Event::StreamEnd {
                return Err(invalid("YAML 입력에는 문서 하나만 있어야 합니다."));
            }
            Ok(value)
        }
        _ => Err(invalid_yaml()),
    }
}

// YAML flow syntax keeps strings explicitly typed. Adjust floating exponents
// to satisfy the original YAML 1.1 resolver and preserve exact integer tokens.
pub fn write(value: &Value, output: &mut impl Write, check: Check<'_>) -> io::Result<()> {
    check()?;
    match value {
        Value::Object(values) => {
            output.write_all(b"{")?;
            for (index, (key, value)) in values.iter().enumerate() {
                if index > 0 {
                    output.write_all(b", ")?;
                }
                serde_json::to_writer(&mut *output, key)?;
                output.write_all(b": ")?;
                write(value, output, check)?;
            }
            output.write_all(b"}")
        }
        Value::Array(values) => {
            output.write_all(b"[")?;
            for (index, value) in values.iter().enumerate() {
                if index > 0 {
                    output.write_all(b", ")?;
                }
                write(value, output, check)?;
            }
            output.write_all(b"]")
        }
        Value::Number(number) => {
            let spelling = number.to_string();
            if let Some((mantissa, exponent)) = spelling.split_once(['e', 'E']) {
                let fraction = if mantissa.contains('.') { "" } else { ".0" };
                let sign = if exponent.starts_with(['-', '+']) {
                    ""
                } else {
                    "+"
                };
                write!(output, "{mantissa}{fraction}e{sign}{exponent}")
            } else {
                output.write_all(spelling.as_bytes())
            }
        }
        _ => serde_json::to_writer(output, value).map_err(io::Error::from),
    }
}
