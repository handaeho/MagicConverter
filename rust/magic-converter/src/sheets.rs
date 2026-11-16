use calamine::{Data, ExcelDateTime, Reader, Xls, Xlsx, open_workbook};
use chrono::NaiveDate;
use roxmltree::{Document, Node, ParsingOptions};
use rust_xlsxwriter::{Format, Formula, Workbook};
use std::collections::{BTreeMap, HashSet};
use std::fmt::Write as _;
use std::fs::File;
use std::io::{self, ErrorKind, Read, Write};
use std::path::Path;
use std::sync::LazyLock;
use zip::write::SimpleFileOptions;

const ROWS: u32 = 1_048_576;
const COLS: u32 = 16_384;
const BUDGET: usize = 12_000_000;
const OFFICE: &str = "urn:oasis:names:tc:opendocument:xmlns:office:1.0";
const TABLE: &str = "urn:oasis:names:tc:opendocument:xmlns:table:1.0";
const TEXT: &str = "urn:oasis:names:tc:opendocument:xmlns:text:1.0";
const OPENFORMULA: &str = "urn:oasis:names:tc:opendocument:xmlns:of:1.2";
const MIME: &str = "application/vnd.oasis.opendocument.spreadsheet";
type Check<'a> = &'a dyn Fn() -> io::Result<()>;

#[derive(Clone)]
enum Cell {
    Text(String),
    Integer(i64),
    Float(f64),
    Bool(bool),
    Date(ExcelDateTime),
    Formula {
        original: String,
        excel: Option<String>,
        ods: Option<String>,
        cached: Option<Box<Cell>>,
    },
}

struct Sheet {
    name: String,
    height: u32,
    width: Option<u32>,
    row_widths: BTreeMap<u32, u32>,
    cells: BTreeMap<(u32, u32), Cell>,
}

impl Sheet {
    fn new(name: String) -> Self {
        Self {
            name,
            height: 0,
            width: None,
            row_widths: BTreeMap::new(),
            cells: BTreeMap::new(),
        }
    }

    fn set(&mut self, row: u32, col: u32, value: Cell) -> io::Result<()> {
        if row >= ROWS || col >= COLS || self.cells.len() >= BUDGET {
            return Err(invalid("스프레드시트의 행·열·셀 한도를 넘습니다."));
        }
        self.height = self.height.max(row + 1);
        self.cells.insert((row, col), value);
        Ok(())
    }

    fn rows(&self) -> impl Iterator<Item = (u32, u32)> {
        (0..self.height).map(|row| {
            (
                row,
                self.width
                    .unwrap_or_else(|| self.row_widths.get(&row).copied().unwrap_or(0)),
            )
        })
    }
}

fn invalid(message: &str) -> io::Error {
    io::Error::new(ErrorKind::InvalidData, message)
}

fn python_float(value: f64) -> String {
    let raw = format!("{value:?}");
    if let Some((digits, exponent)) = raw.split_once('e')
        && let Ok(exponent) = exponent.parse::<i32>()
    {
        return format!("{digits}e{exponent:+03}");
    }
    raw
}

fn date_display(value: ExcelDateTime, iso: bool) -> io::Result<String> {
    if value.is_duration() {
        let millis = value
            .as_duration()
            .ok_or_else(|| invalid("날짜 범위를 벗어났습니다."))?
            .num_milliseconds();
        let days = millis.div_euclid(86_400_000);
        let remaining = millis.rem_euclid(86_400_000);
        let mut clock = format!(
            "{}:{:02}:{:02}",
            remaining / 3_600_000,
            remaining / 60_000 % 60,
            remaining / 1000 % 60
        );
        if remaining % 1000 > 0 {
            clock.push_str(&format!(".{:06}", remaining % 1000 * 1000));
        }
        if days != 0 {
            clock = format!(
                "{days} day{}, {clock}",
                if days.abs() == 1 { "" } else { "s" }
            );
        }
        return Ok(clock);
    }
    let dt = value
        .as_datetime()
        .ok_or_else(|| invalid("날짜 범위를 벗어났습니다."))?;
    let pattern = if iso {
        "%Y-%m-%dT%H:%M:%S"
    } else if (0.0..1.0).contains(&value.as_f64()) {
        "%H:%M:%S"
    } else {
        "%Y-%m-%d %H:%M:%S"
    };
    let mut text = dt.format(pattern).to_string();
    if dt.and_utc().timestamp_subsec_micros() != 0 {
        text.push_str(&format!(".{:06}", dt.and_utc().timestamp_subsec_micros()));
    }
    Ok(text)
}

impl Cell {
    fn display(&self) -> io::Result<String> {
        Ok(match self {
            Self::Text(value)
            | Self::Formula {
                original: value, ..
            } => value.clone(),
            Self::Integer(value) => value.to_string(),
            Self::Float(value) => python_float(*value),
            Self::Bool(true) => "True".to_owned(),
            Self::Bool(false) => "False".to_owned(),
            Self::Date(value) => date_display(*value, false)?,
        })
    }
}

fn typed(data: Data, legacy: bool) -> io::Result<Option<Cell>> {
    Ok(Some(match data {
        Data::Empty => return Ok(None),
        Data::String(value) | Data::DurationIso(value) => Cell::Text(value),
        Data::DateTimeIso(value) => Cell::Text(value.replace('T', " ")),
        Data::Float(value)
            if !legacy && value.fract() == 0.0 && value.abs() < (1_u64 << 53) as f64 =>
        {
            Cell::Integer(value as i64)
        }
        Data::Float(value) => Cell::Float(value),
        Data::Int(value) if legacy => Cell::Float(value as f64),
        Data::Int(value) => Cell::Integer(value),
        Data::Bool(value) => Cell::Bool(value),
        Data::DateTime(value) if legacy => Cell::Text(date_display(value, true)?),
        Data::DateTime(value) => Cell::Date(value),
        Data::Error(value) if legacy => Cell::Integer(match value {
            calamine::CellErrorType::Null => 0,
            calamine::CellErrorType::Div0 => 7,
            calamine::CellErrorType::Value => 15,
            calamine::CellErrorType::Ref => 23,
            calamine::CellErrorType::Name => 29,
            calamine::CellErrorType::Num => 36,
            calamine::CellErrorType::NA => 42,
            _ => 255,
        }),
        Data::Error(value) => Cell::Text(value.to_string()),
    }))
}

pub(crate) fn stream_cells(
    path: &Path,
    check: Check<'_>,
    mut begin: impl FnMut(&str, Option<(u32, u32, String, bool)>) -> io::Result<()>,
) -> io::Result<()> {
    package_size(path)?;
    let mut workbook: Xlsx<_> = open_workbook(path).map_err(io::Error::other)?;
    let mut formulas: Xlsx<_> = open_workbook(path).map_err(io::Error::other)?;
    for name in workbook.sheet_names() {
        check()?;
        begin(&name, None)?;
        let mut values = workbook
            .worksheet_cells_reader(&name)
            .map_err(io::Error::other)?;
        let mut expressions = formulas
            .worksheet_cells_reader(&name)
            .map_err(io::Error::other)?;
        let mut count = 0;
        loop {
            let raw = values.next_cell().map_err(io::Error::other)?;
            let formula = expressions.next_formula().map_err(io::Error::other)?;
            let (Some(raw), Some(formula)) = (raw, formula) else {
                break;
            };
            count += 1;
            if count % 1024 == 0 {
                check()?;
            }
            let (row, col) = raw.get_position();
            if row >= ROWS || col >= COLS || (row, col) != formula.get_position() {
                return Err(invalid("스프레드시트의 셀 좌표를 확인할 수 없습니다."));
            }
            let expression = formula.get_value();
            if !expression.is_empty() {
                begin(
                    &name,
                    Some((row + 1, col + 1, format!("={expression}"), true)),
                )?;
            } else if let Some(value) = typed(Data::from(raw.get_value().clone()), false)? {
                begin(&name, Some((row + 1, col + 1, value.display()?, false)))?;
            }
        }
    }
    check()
}

fn read_xlsx(path: &Path, check: Check<'_>) -> io::Result<Vec<Sheet>> {
    package_size(path)?;
    let mut workbook: Xlsx<_> = open_workbook(path).map_err(io::Error::other)?;
    let mut sheets = Vec::new();
    let mut processed = 0;
    for name in workbook.sheet_names() {
        check()?;
        let mut sheet = Sheet::new(name);
        let mut cells = workbook
            .worksheet_cells_reader(&sheet.name)
            .map_err(io::Error::other)?;
        let (mut rows, mut cols) = cells.dimensions().end;
        while let Some(cell) = cells.next_cell().map_err(io::Error::other)? {
            processed += 1;
            if processed > BUDGET {
                return Err(invalid("스프레드시트의 셀 한도를 넘습니다."));
            }
            if processed % 1024 == 0 {
                check()?;
            }
            let (row, col) = cell.get_position();
            rows = rows.max(row);
            cols = cols.max(col);
            if let Some(value) = typed(Data::from(cell.get_value().clone()), false)? {
                sheet.set(row, col, value)?;
            }
        }
        drop(cells);
        let mut formulas = workbook
            .worksheet_cells_reader(&sheet.name)
            .map_err(io::Error::other)?;
        while let Some(cell) = formulas.next_formula().map_err(io::Error::other)? {
            let value = cell.get_value();
            if value.is_empty() {
                continue;
            }
            check()?;
            let (row, col) = cell.get_position();
            let formula = format!("={value}");
            sheet.set(
                row,
                col,
                Cell::Formula {
                    original: formula.clone(),
                    excel: Some(formula),
                    ods: None,
                    cached: None,
                },
            )?;
        }
        if rows >= ROWS || cols >= COLS {
            return Err(invalid("스프레드시트의 행·열 한도를 넘습니다."));
        }
        sheet.height = rows + 1;
        sheet.width = Some(cols + 1);
        sheets.push(sheet);
    }
    Ok(sheets)
}

pub(crate) fn document_html(
    path: &Path,
    check: Check<'_>,
    warnings: &mut Vec<String>,
) -> io::Result<String> {
    use crate::{hwp_model, safe_html::escape, word_model};
    const REL: &str = "http://schemas.openxmlformats.org/officeDocument/2006/relationships";
    let mut sheets = read_xlsx(path, check)?;
    let mut archive = word_model::package(path, check)?;
    let mut workbook: Xlsx<_> = open_workbook(path).map_err(io::Error::other)?;
    let part = "xl/workbook.xml";
    let root = word_model::read_part(&mut archive, part)?;
    let root = Document::parse(std::str::from_utf8(&root).map_err(io::Error::other)?)
        .map_err(io::Error::other)?;
    let relationships = word_model::relationships(&mut archive, part)?;
    let styles = if archive.index_for_name("xl/styles.xml").is_some() {
        word_model::read_part(&mut archive, "xl/styles.xml")?
    } else {
        b"<styleSheet/>".to_vec()
    };
    let styles = Document::parse(std::str::from_utf8(&styles).map_err(io::Error::other)?)
        .map_err(io::Error::other)?;
    let group = |key| {
        styles
            .root_element()
            .children()
            .find(|item| item.tag_name().name() == key)
            .into_iter()
            .flat_map(|item| item.children().filter(Node::is_element))
            .collect::<Vec<_>>()
    };
    let (fonts, fills, styles) = (group("fonts"), group("fills"), group("cellXfs"));
    let mut output = String::new();
    let mut chart = false;
    for sheet in &mut sheets {
        check()?;
        let merged = workbook
            .merge_cells_by_sheet_name(&sheet.name)
            .map_err(io::Error::other)?;
        let mut width = sheet.width.unwrap_or(1).max(1);
        for bounds in &merged {
            sheet.height = sheet.height.max(bounds.end.0.saturating_add(1));
            width = width.max(bounds.end.1.saturating_add(1));
        }
        if u64::from(sheet.height) * u64::from(width) > 100_000 {
            return Err(invalid("문서 모델 변환은 시트당 100,000셀까지 지원합니다."));
        }
        let mut spans = BTreeMap::new();
        let mut covered = HashSet::new();
        for bounds in merged {
            let (r, c) = bounds.start;
            let (last_r, last_c) = bounds.end;
            if last_r < r || last_c < c {
                return Err(invalid("Excel 셀 병합 범위가 잘못되었습니다."));
            }
            spans.insert((r, c), (last_r - r + 1, last_c - c + 1));
            for row in r..=last_r {
                for col in c..=last_c {
                    if !covered.insert((row, col)) {
                        return Err(invalid("Excel 셀 병합 범위가 겹칩니다."));
                    }
                }
            }
        }
        let entry = root
            .descendants()
            .find(|item| {
                item.tag_name().name() == "sheet"
                    && item.attribute("name") == Some(sheet.name.as_str())
            })
            .and_then(|item| item.attribute((REL, "id")))
            .and_then(|id| relationships.get(id))
            .ok_or_else(|| invalid("Excel 시트의 연결 항목을 찾을 수 없습니다."))?;
        let xml = word_model::read_part(&mut archive, entry)?;
        let xml = Document::parse(std::str::from_utf8(&xml).map_err(io::Error::other)?)
            .map_err(io::Error::other)?;
        let coordinates = |address: &str| -> Option<(u32, u32)> {
            let split = address.find(|ch: char| ch.is_ascii_digit())?;
            let col = address[..split]
                .bytes()
                .try_fold(0_u32, |col, ch| {
                    let digit = ch.checked_sub(b'A')?;
                    (digit < 26).then_some(())?;
                    col.checked_mul(26)?.checked_add(u32::from(digit) + 1)
                })?
                .checked_sub(1)?;
            let row = address[split..].parse::<u32>().ok()?.checked_sub(1)?;
            Some((row, col))
        };
        let cell_styles: BTreeMap<_, _> = xml
            .descendants()
            .filter(|node| node.tag_name().name() == "c")
            .filter_map(|cell| {
                Some((
                    coordinates(cell.attribute("r")?)?,
                    cell.attribute("s")?.parse::<usize>().ok()?,
                ))
            })
            .collect();
        write!(
            output,
            r#"<p><span data-size="14" data-bold="1">{}</span></p><table>"#,
            escape(&sheet.name)
        )
        .unwrap();
        for r in 0..sheet.height {
            check()?;
            output.push_str("<tr>");
            for c in 0..width {
                let (rs, cs) = if let Some(span) = spans.get(&(r, c)) {
                    *span
                } else if covered.contains(&(r, c)) {
                    continue;
                } else {
                    (1, 1)
                };
                let style = styles.get(*cell_styles.get(&(r, c)).unwrap_or(&0));
                let index = |key| {
                    style
                        .and_then(|item| item.attribute(key))
                        .and_then(|value| value.parse::<usize>().ok())
                        .unwrap_or_default()
                };
                let font = fonts.get(index("fontId"));
                let child = |key| {
                    font.and_then(|item| item.children().find(|node| node.tag_name().name() == key))
                };
                let font = child("name")
                    .and_then(|item| item.attribute("val"))
                    .unwrap_or("맑은 고딕");
                let size = child("sz")
                    .and_then(|item| item.attribute("val"))
                    .and_then(|v| v.parse::<f64>().ok())
                    .unwrap_or(10.0);
                let active = |key| {
                    child(key).is_some_and(|item| {
                        !matches!(item.attribute("val"), Some("0" | "false" | "off"))
                    })
                };
                let fill = fills
                    .get(index("fillId"))
                    .and_then(|fill| {
                        fill.children().find(|node| {
                            node.tag_name().name() == "patternFill"
                                && node.attribute("patternType") == Some("solid")
                        })
                    })
                    .and_then(|node| {
                        node.children()
                            .find(|item| item.tag_name().name() == "fgColor")
                    })
                    .and_then(|node| node.attribute("rgb"))
                    .and_then(|value| value.get(value.len().saturating_sub(6)..))
                    .and_then(hwp_model::color)
                    .map_or_else(String::new, |fill| format!(r##" bgcolor="#{fill}""##));
                let text = sheet
                    .cells
                    .get(&(r, c))
                    .map(Cell::display)
                    .transpose()?
                    .unwrap_or_default();
                write!(output, r#"<td data-col="{c}" rowspan="{rs}" colspan="{cs}"{fill}><p><span data-font="{}" data-size="{size}" data-bold="{}" data-italic="{}">{}</span></p></td>"#,
                    escape(font), u8::from(active("b")), u8::from(active("i")), escape(&text)).unwrap();
            }
            output.push_str("</tr>");
            if output.len() > 128 * 1024 * 1024 {
                return Err(invalid("Excel 문서 모델이 크기 한도를 초과했습니다."));
            }
        }
        output.push_str("</table>");
        let parts = word_model::relationships(&mut archive, entry)?;
        for drawing in xml
            .descendants()
            .filter(|node| node.tag_name().name() == "drawing")
        {
            let Some(part) = drawing.attribute((REL, "id")).and_then(|id| parts.get(id)) else {
                continue;
            };
            let bytes = word_model::read_part(&mut archive, part)?;
            let rels = word_model::relationships(&mut archive, part)?;
            let document = Document::parse(std::str::from_utf8(&bytes).map_err(io::Error::other)?)
                .map_err(io::Error::other)?;
            chart |= document
                .descendants()
                .any(|node| node.tag_name().name() == "chart");
            for item in document
                .descendants()
                .filter(|node| node.tag_name().name() == "blip")
            {
                check()?;
                let reference = item
                    .attribute((REL, "embed"))
                    .and_then(|id| rels.get(id))
                    .ok_or_else(|| invalid("Excel 그림의 연결 항목을 찾을 수 없습니다."))?;
                let picture = word_model::read_part(&mut archive, reference)?;
                write!(
                    output,
                    "<p>{}</p>",
                    hwp_model::image_html(&picture, None, None, [0; 4])?
                )
                .unwrap();
                if output.len() > 128 * 1024 * 1024 {
                    return Err(invalid("Excel 문서 모델이 크기 한도를 초과했습니다."));
                }
            }
        }
    }
    if chart {
        warnings.push("Excel 차트는 문서 모델 출력에 포함되지 않습니다.".to_owned());
    }
    warnings.push("통합 문서의 셀 값·수식 문자열·그림을 문서로 옮겼습니다. 셀 서식과 인쇄 배치는 달라질 수 있습니다.".to_owned());
    Ok(output)
}

fn read_xls(path: &Path, check: Check<'_>) -> io::Result<Vec<Sheet>> {
    if std::fs::metadata(path)?.len() > 128 * 1024 * 1024 {
        return Err(invalid("XLS 입력 파일이 너무 큽니다."));
    }
    let mut workbook: Xls<_> = open_workbook(path).map_err(io::Error::other)?;
    let mut sheets = Vec::new();
    for name in workbook.sheet_names() {
        check()?;
        let mut sheet = Sheet::new(name);
        let cells = workbook
            .worksheet_range(&sheet.name)
            .map_err(io::Error::other)?;
        if let Some((row, col)) = cells.end() {
            sheet.height = row + 1;
            sheet.width = Some(col + 1);
        }
        let (offset_row, offset_col) = cells.start().unwrap_or_default();
        for (row, col, value) in cells.used_cells() {
            check()?;
            if let Some(value) = typed(value.clone(), true)? {
                sheet.set(offset_row + row as u32, offset_col + col as u32, value)?;
            }
        }
        sheets.push(sheet);
    }
    Ok(sheets)
}

fn package_size(path: &Path) -> io::Result<()> {
    let mut archive = zip::ZipArchive::new(File::open(path)?)?;
    let mut total = 0_u64;
    for index in 0..archive.len() {
        total = total.saturating_add(archive.by_index(index)?.size());
    }
    if total > 1024 * 1024 * 1024 {
        return Err(invalid("스프레드시트 패키지 압축 해제 한도를 넘습니다."));
    }
    Ok(())
}

fn read_delimited(path: &Path, delimiter: u8, check: Check<'_>) -> io::Result<Vec<Sheet>> {
    let text = crate::data::text(&crate::data::bytes(path)?)?;
    let mut sheet = Sheet::new(
        path.file_stem()
            .unwrap_or_default()
            .to_string_lossy()
            .into_owned(),
    );
    let mut input = text.bytes().peekable();
    let mut field = Vec::new();
    let mut columns = 0;
    let mut started = false;
    let mut quoted = false;
    let put = |sheet: &mut Sheet, columns: &mut u32, field: &mut Vec<u8>| -> io::Result<()> {
        let row = sheet.height;
        let text = String::from_utf8(std::mem::take(field)).map_err(io::Error::other)?;
        sheet.set(row, *columns, Cell::Text(text))?;
        sheet.height = row;
        *columns += 1;
        Ok(())
    };
    while let Some(byte) = input.next() {
        if quoted {
            if byte != b'"' || input.next_if_eq(&b'"').is_some() {
                field.push(byte);
            } else {
                quoted = false;
            }
        } else if byte == delimiter {
            put(&mut sheet, &mut columns, &mut field)?;
            started = false;
        } else if matches!(byte, b'\r' | b'\n') {
            if byte == b'\r' {
                input.next_if_eq(&b'\n');
            }
            if started || columns != 0 {
                put(&mut sheet, &mut columns, &mut field)?;
                sheet.row_widths.insert(sheet.height, columns);
            }
            sheet.height += 1;
            if sheet.height > ROWS {
                return Err(invalid("스프레드시트의 행 한도를 넘습니다."));
            }
            check()?;
            columns = 0;
            started = false;
        } else if byte == b'"' && !started {
            quoted = true;
            started = true;
        } else {
            started = true;
            field.push(byte);
        }
    }
    if started || columns != 0 {
        put(&mut sheet, &mut columns, &mut field)?;
        sheet.row_widths.insert(sheet.height, columns);
        sheet.height += 1;
    }
    Ok(vec![sheet])
}

static ARITHMETIC_TOKEN: LazyLock<regex::Regex> = LazyLock::new(|| {
    regex::Regex::new(
    r"(?i)^(?:\s+|(?P<reference>\$?[a-z]{1,3}\$?[1-9][0-9]{0,6})|(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:e[+-]?[0-9]+)?|[()+*/-])").unwrap()
});
static ODS_REFERENCE: LazyLock<regex::Regex> =
    LazyLock::new(|| regex::Regex::new(r"\[\.(\$?[a-zA-Z]{1,3}\$?[1-9][0-9]{0,6})\]").unwrap());

fn to_ods_formula(formula: &str) -> Option<String> {
    let mut raw = formula.strip_prefix('=')?;
    let mut converted = "of:=".to_owned();
    while !raw.is_empty() {
        let capture = ARITHMETIC_TOKEN.captures(raw)?;
        let token = capture.get(0)?.as_str();
        if capture.name("reference").is_some() {
            converted.push_str("[.");
            converted.push_str(token);
            converted.push(']');
        } else if !token.trim().is_empty() {
            converted.push_str(token);
        } else {
            return None;
        }
        raw = &raw[token.len()..];
    }
    (converted.len() > 4).then_some(converted)
}

fn ods_cell(cell: Node<'_, '_>) -> io::Result<Option<Cell>> {
    let field = |name| cell.attribute((OFFICE, name));
    let value = match field("value-type") {
        Some("float" | "currency" | "percentage") => {
            let value: f64 = field("value")
                .unwrap_or("0")
                .parse()
                .map_err(io::Error::other)?;
            Some(Cell::Float(value))
        }
        Some("boolean") => Some(Cell::Bool(field("boolean-value") == Some("true"))),
        kind => {
            let body = cell
                .children()
                .filter(|node| node.has_tag_name((TEXT, "p")))
                .map(|node| {
                    node.descendants()
                        .filter(Node::is_text)
                        .filter_map(|node| node.text())
                        .collect::<String>()
                })
                .collect::<Vec<_>>()
                .join("\n");
            if !body.is_empty() {
                Some(Cell::Text(body))
            } else {
                field("string-value")
                    .filter(|v| !v.is_empty())
                    .or_else(|| field("date-value").filter(|v| !v.is_empty()))
                    .or_else(|| field("time-value"))
                    .or((kind == Some("string")).then_some(""))
                    .map(|value| Cell::Text(value.to_owned()))
            }
        }
    };
    let Some(formula) = cell.attribute((TABLE, "formula")) else {
        return Ok(value);
    };
    let mut excel = None;
    let mut ods = None;
    if let Some((prefix, expression)) = formula.split_once(':')
        && cell.lookup_namespace_uri(Some(prefix)) == Some(OPENFORMULA)
        && expression.starts_with('=')
    {
        let translated = ODS_REFERENCE.replace_all(expression, "${1}").into_owned();
        let canonical = format!("of:{expression}");
        if to_ods_formula(&translated).as_deref() == Some(canonical.as_str()) {
            excel = Some(translated);
        }
        ods = Some(canonical);
    }
    Ok(Some(Cell::Formula {
        original: formula.to_owned(),
        excel,
        ods,
        cached: value.map(Box::new),
    }))
}

fn repeats(node: Node<'_, '_>, attr: &str) -> io::Result<u32> {
    node.attribute((TABLE, attr))
        .unwrap_or("1")
        .parse::<u32>()
        .ok()
        .filter(|count| *count > 0)
        .ok_or_else(|| invalid("ODS의 행·열 반복 수가 올바르지 않습니다."))
}

fn ods_table(table: Node<'_, '_>, check: Check<'_>) -> io::Result<Sheet> {
    let mut sheet = Sheet::new(
        table
            .attribute((TABLE, "name"))
            .unwrap_or("Sheet")
            .to_owned(),
    );
    let mut row = 0_u32;
    for node in table
        .descendants()
        .filter(|node| node.has_tag_name((TABLE, "table-row")))
    {
        if !node
            .ancestors()
            .skip(1)
            .take_while(|parent| *parent != table)
            .all(|parent| {
                parent.tag_name().namespace() == Some(TABLE)
                    && matches!(
                        parent.tag_name().name(),
                        "table-rows" | "table-header-rows" | "table-row-group"
                    )
            })
        {
            continue;
        }
        check()?;
        let count = repeats(node, "number-rows-repeated")?;
        let mut col = 0_u32;
        let mut width = 0;
        let mut values = Vec::new();
        for item in node.children().filter(|cell| {
            cell.has_tag_name((TABLE, "table-cell"))
                || cell.has_tag_name((TABLE, "covered-table-cell"))
        }) {
            let repeat = repeats(item, "number-columns-repeated")?;
            let end = col.saturating_add(repeat);
            if let Some(mut cell) = ods_cell(item)? {
                if end > COLS {
                    return Err(invalid("ODS 열 반복 수가 Excel 한도를 넘습니다."));
                }
                if (repeat > 1 || count > 1)
                    && let Cell::Formula {
                        excel, ods, cached, ..
                    } = &mut cell
                {
                    *excel = None;
                    *ods = None;
                    *cached = None;
                }
                values.push((col, end, cell));
                width = end;
            }
            col = end;
        }
        let end = row.saturating_add(count);
        if width > 0 {
            if end > ROWS {
                return Err(invalid("ODS 행 반복 수가 Excel 한도를 넘습니다."));
            }
            for index in row..end {
                check()?;
                sheet.row_widths.insert(index, width);
                for (start, end, cell) in &values {
                    for col in *start..*end {
                        sheet.set(index, col, cell.clone())?;
                    }
                }
            }
        }
        row = end;
    }
    Ok(sheet)
}

fn read_ods(path: &Path, check: Check<'_>) -> io::Result<Vec<Sheet>> {
    let mut archive = zip::ZipArchive::new(File::open(path)?)?;
    let mut entry = archive.by_name("content.xml")?;
    if entry.size() > 128 * 1024 * 1024 {
        return Err(invalid("ODS 내용 XML이 너무 큽니다."));
    }
    let mut bytes = Vec::new();
    entry.read_to_end(&mut bytes)?;
    let text = crate::data::text(&bytes)?;
    let doc = Document::parse_with_options(
        &text,
        ParsingOptions {
            nodes_limit: BUDGET as u32,
            ..Default::default()
        },
    )
    .map_err(io::Error::other)?;
    let Some(body) = doc
        .root_element()
        .children()
        .find(|node| node.has_tag_name((OFFICE, "body")))
        .and_then(|body| {
            body.children()
                .find(|node| node.has_tag_name((OFFICE, "spreadsheet")))
        })
    else {
        return Ok(Vec::new());
    };
    body.children()
        .filter(|node| node.has_tag_name((TABLE, "table")))
        .map(|table| ods_table(table, check))
        .collect()
}

fn safe_name(name: &str, bad: &str, limit: usize, fallback: &str) -> String {
    let name: String = name
        .chars()
        .take(limit)
        .map(|ch| if bad.contains(ch) { '_' } else { ch })
        .collect();
    if name.is_empty() {
        fallback.to_owned()
    } else {
        name
    }
}

fn address(name: &str, row: u32, col: u32) -> String {
    let mut label = Vec::new();
    let mut col = col + 1;
    while col > 0 {
        col -= 1;
        label.push(char::from(b'A' + (col % 26) as u8));
        col /= 26;
    }
    format!(
        "{name}!{}{}",
        label.iter().rev().collect::<String>(),
        row + 1
    )
}

fn formula_warning(warnings: &mut Vec<String>, sheet: &Sheet, row: u32, col: u32) {
    if warnings.len() < 8 {
        warnings.push(address(&sheet.name, row, col));
    } else if warnings.len() == 8 {
        warnings.push("…".to_owned());
    }
}

fn write_xlsx(
    path: &Path,
    sheets: &[Sheet],
    check: Check<'_>,
    warnings: &mut Vec<String>,
) -> io::Result<()> {
    let mut book = Workbook::new();
    let mut names = HashSet::new();
    let placeholder = format!("MagicConverter-{}-empty", uuid::Uuid::new_v4());
    let mut rewrite = false;
    for sheet in sheets {
        check()?;
        let base = safe_name(&sheet.name, "\\/*?:[]", 31, "Sheet");
        let mut name = base.clone();
        for counter in 1.. {
            if names.insert(name.to_lowercase()) {
                break;
            }
            let suffix = counter.to_string();
            name = base.chars().take(31 - suffix.len()).collect::<String>() + &suffix;
        }
        let output = book
            .add_worksheet()
            .set_name(name)
            .map_err(io::Error::other)?;
        for (&(row, col), cell) in &sheet.cells {
            if col == 0 {
                check()?;
            }
            let col = col as u16;
            match cell {
                Cell::Integer(value) => output.write_number(row, col, *value as f64),
                Cell::Float(value) => output.write_number(row, col, *value),
                Cell::Bool(value) => output.write_boolean(row, col, *value),
                Cell::Date(value) => {
                    let format = Format::new().set_num_format(if value.is_duration() {
                        "[h]:mm:ss"
                    } else if (0.0..1.0).contains(&value.as_f64()) {
                        "hh:mm:ss"
                    } else {
                        "yyyy-mm-dd hh:mm:ss"
                    });
                    let mut serial = value.as_f64();
                    if !value.is_duration()
                        && let Some(dt) = value.as_datetime()
                        && serial >= 1.0
                    {
                        let base = NaiveDate::from_ymd_opt(1899, 12, 30)
                            .unwrap()
                            .and_hms_opt(0, 0, 0)
                            .unwrap();
                        serial = (dt - base).num_milliseconds() as f64 / 86_400_000.0;
                        if (1.0..=60.0).contains(&serial) {
                            serial -= 1.0;
                        }
                    }
                    output.write_number_with_format(row, col, serial, &format)
                }
                Cell::Formula {
                    excel: Some(formula),
                    ..
                } => output.write_formula(row, col, Formula::new(formula).set_result("")),
                _ => {
                    if matches!(cell, Cell::Formula { .. }) {
                        formula_warning(warnings, sheet, row, u32::from(col));
                    }
                    let value = cell.display()?;
                    if value.chars().count() > 32_767 {
                        return Err(io::Error::other(format!(
                            "{} 텍스트가 Excel 셀 한도를 넘습니다.",
                            address(&sheet.name, row, u32::from(col))
                        )));
                    }
                    if value.is_empty() || value.contains('\r') {
                        rewrite = true;
                    }
                    output.write_string(
                        row,
                        col,
                        if value.is_empty() {
                            placeholder.clone()
                        } else {
                            value
                        },
                    )
                }
            }
            .map_err(io::Error::other)?;
        }
    }
    check()?;
    if !rewrite {
        return book.save(path).map_err(io::Error::other);
    }
    let temporary = tempfile::Builder::new().suffix(".xlsx").tempfile()?;
    book.save(temporary.path()).map_err(io::Error::other)?;
    let mut archive = zip::ZipArchive::new(temporary.reopen()?)?;
    let mut strings = String::new();
    archive
        .by_name("xl/sharedStrings.xml")?
        .read_to_string(&mut strings)?;
    let table = Document::parse(&strings).map_err(io::Error::other)?;
    let empty_index = table
        .root_element()
        .children()
        .filter(Node::is_element)
        .position(|node| {
            node.children()
                .find(|child| child.is_element())
                .and_then(|child| child.text())
                == Some(&placeholder)
        });
    let empty_cell = empty_index.map(|index| {
        regex::Regex::new(&format!(r#"<c r="([^"]+)" t="s"><v>{index}</v></c>"#)).unwrap()
    });
    let mut output = zip::ZipWriter::new(File::create(path)?);
    let options = SimpleFileOptions::default().compression_method(zip::CompressionMethod::Deflated);
    for index in 0..archive.len() {
        check()?;
        let mut entry = archive.by_index(index)?;
        let name = entry.name()?.into_owned();
        if name == "xl/sharedStrings.xml" {
            output.start_file(name, options)?;
            output.write_all(strings.replace("_x000D_", "&#13;").as_bytes())?;
        } else if let Some(empty_cell) = &empty_cell
            && name.starts_with("xl/worksheets/sheet")
            && name.ends_with(".xml")
        {
            output.start_file(name, options)?;
            let mut xml = String::new();
            entry.read_to_string(&mut xml)?;
            output.write_all(
                empty_cell
                    .replace_all(&xml, r#"<c r="${1}" t="inlineStr"></c>"#)
                    .as_bytes(),
            )?;
        } else {
            output.raw_copy_file(entry)?;
        }
    }
    output.finish()?.sync_all()?;
    Ok(())
}

fn ods_attribute(value: &str) -> std::borrow::Cow<'_, str> {
    html_escape::encode_double_quoted_attribute(value)
}

fn write_ods(
    path: &Path,
    sheets: &[Sheet],
    check: Check<'_>,
    warnings: &mut Vec<String>,
) -> io::Result<()> {
    let mut package = zip::ZipWriter::new(File::create(path)?);
    package.start_file(
        "mimetype",
        SimpleFileOptions::default().compression_method(zip::CompressionMethod::Stored),
    )?;
    package.write_all(MIME.as_bytes())?;
    let options = SimpleFileOptions::default().compression_method(zip::CompressionMethod::Deflated);
    package.start_file("content.xml", options)?;
    write!(
        package,
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?><office:document-content xmlns:office=\"{OFFICE}\" xmlns:table=\"{TABLE}\" xmlns:text=\"{TEXT}\" xmlns:of=\"{OPENFORMULA}\" office:version=\"1.2\"><office:body><office:spreadsheet>"
    )?;
    for sheet in sheets {
        write!(
            package,
            "<table:table table:name=\"{}\">",
            ods_attribute(&sheet.name)
        )?;
        for (row, width) in sheet.rows() {
            check()?;
            write!(package, "<table:table-row>")?;
            for col in 0..width {
                let value = sheet.cells.get(&(row, col));
                write!(package, "<table:table-cell")?;
                let value = if let Some(Cell::Formula {
                    excel, ods, cached, ..
                }) = value
                {
                    if let Some(formula) = ods
                        .clone()
                        .or_else(|| excel.as_deref().and_then(to_ods_formula))
                    {
                        write!(package, " table:formula=\"{}\"", ods_attribute(&formula))?;
                        cached.as_deref()
                    } else {
                        formula_warning(warnings, sheet, row, col);
                        value
                    }
                } else {
                    value
                };
                if let Some(value) = value {
                    let content = value.display()?;
                    match value {
                        Cell::Bool(flag) => write!(
                            package,
                            " office:value-type=\"boolean\" office:boolean-value=\"{flag}\""
                        )?,
                        Cell::Integer(_) | Cell::Float(_) => write!(
                            package,
                            " office:value-type=\"float\" office:value=\"{content}\""
                        )?,
                        Cell::Date(_) => {}
                        _ => write!(package, " office:value-type=\"string\"")?,
                    }
                    write!(
                        package,
                        "><text:p>{}</text:p></table:table-cell>",
                        html_escape::encode_text(&content)
                    )?;
                } else {
                    write!(package, "/>")?;
                }
            }
            write!(package, "</table:table-row>")?;
        }
        write!(package, "</table:table>")?;
    }
    write!(
        package,
        "</office:spreadsheet></office:body></office:document-content>"
    )?;
    package.start_file("META-INF/manifest.xml", options)?;
    write!(
        package,
        "<manifest:manifest xmlns:manifest=\"urn:oasis:names:tc:opendocument:xmlns:manifest:1.0\" manifest:version=\"1.2\"><manifest:file-entry manifest:full-path=\"/\" manifest:media-type=\"{MIME}\"/><manifest:file-entry manifest:full-path=\"content.xml\" manifest:media-type=\"text/xml\"/></manifest:manifest>"
    )?;
    package.finish()?;
    Ok(())
}

fn write_delimited(
    output: &mut File,
    sheet: Option<&Sheet>,
    delimiter: u8,
    check: Check<'_>,
) -> io::Result<()> {
    if let Some(sheet) = sheet.filter(|sheet| sheet.height > 0) {
        output.write_all(b"\xef\xbb\xbf")?;
        let mut output = io::BufWriter::new(output);
        for (row, width) in sheet.rows() {
            check()?;
            for col in 0..width {
                if col != 0 {
                    output.write_all(&[delimiter])?;
                }
                let value = sheet
                    .cells
                    .get(&(row, col))
                    .map(Cell::display)
                    .transpose()?
                    .unwrap_or_default();
                if width == 1 && value.is_empty()
                    || value
                        .bytes()
                        .any(|byte| matches!(byte, b'"' | b'\r' | b'\n') || byte == delimiter)
                {
                    write!(output, "\"{}\"", value.replace('"', "\"\""))?;
                } else {
                    output.write_all(value.as_bytes())?;
                }
            }
            output.write_all(b"\r\n")?;
        }
        output.flush()?;
    }
    Ok(())
}

pub fn supports(source: &str, target: &str) -> bool {
    matches!(
        source,
        "xls" | "xlsx" | "xlsm" | "xltx" | "xltm" | "ods" | "csv" | "tsv"
    ) && matches!(target, "xlsx" | "ods" | "csv" | "tsv" | "pdf")
}

pub fn convert(
    source: &Path,
    edge: &crate::Edge,
    selection: &crate::Selection<'_>,
    check: Check<'_>,
    warnings: &mut Vec<String>,
) -> io::Result<std::path::PathBuf> {
    let variant = matches!(edge.source.as_str(), "xlsm" | "xltx" | "xltm");
    if variant {
        warnings.push("Office 템플릿·매크로 문서를 표준 OOXML로 읽었습니다. 매크로·ActiveX·포함 개체는 결과에 포함되지 않습니다.".to_owned());
    }
    if (variant || edge.source == "xlsx") && edge.target == "xlsx" {
        return selection.output.publish_external(
            source,
            ".xlsx",
            &selection.name,
            check,
            |target| {
                crate::office_package::convert(
                    source,
                    target,
                    crate::office_package::Action::RewriteWorkbook,
                    check,
                )
            },
        );
    }
    let temporary;
    let sheets = match edge.source.as_str() {
        "xlsx" => read_xlsx(source, check)?,
        _ if variant => {
            temporary = tempfile::Builder::new().suffix(".xlsx").tempfile()?;
            crate::office_package::convert(
                source,
                temporary.path(),
                crate::office_package::Action::Standardize,
                check,
            )?;
            read_xlsx(temporary.path(), check)?
        }
        "xls" => read_xls(source, check)?,
        "ods" => read_ods(source, check)?,
        "csv" => read_delimited(source, b',', check)?,
        "tsv" => read_delimited(source, b'\t', check)?,
        _ => return Err(invalid("지원하지 않는 표 입력입니다.")),
    };
    if edge.target == "pdf" {
        return selection.output.publish_external(
            source,
            ".pdf",
            &selection.name,
            check,
            |target| print_tables(&sheets, target, check),
        );
    }
    let mut unconverted = Vec::new();
    let suffix = format!(".{}", edge.target);
    let delimiter = if edge.target == "tsv" { b'\t' } else { b',' };
    let output = if matches!(edge.target.as_str(), "csv" | "tsv") && sheets.len() > 1 {
        selection
            .output
            .publish_folder(source, &selection.name, check, |folder| {
                for (i, sheet) in sheets.iter().enumerate() {
                    let safe = safe_name(&sheet.name, "<>:\"/\\|?*", usize::MAX, "")
                        .trim_end_matches([' ', '.'])
                        .to_owned();
                    let name = if safe.is_empty() {
                        format!("Sheet{}", i + 1)
                    } else {
                        safe
                    };
                    write_delimited(
                        &mut File::create(folder.join(format!("{:03}_{name}{suffix}", i + 1)))?,
                        Some(sheet),
                        delimiter,
                        check,
                    )?;
                }
                Ok(())
            })?
    } else if matches!(edge.target.as_str(), "csv" | "tsv") {
        selection.output.publish(
            source,
            &suffix,
            "converted",
            &selection.name,
            check,
            |target| write_delimited(target, sheets.first(), delimiter, check),
        )?
    } else {
        selection
            .output
            .publish_external(source, &suffix, &selection.name, check, |target| match edge
                .target
                .as_str()
            {
                "xlsx" => write_xlsx(target, &sheets, check, &mut unconverted),
                "ods" => write_ods(target, &sheets, check, &mut unconverted),
                _ => unreachable!(),
            })?
    };
    if !unconverted.is_empty() {
        warnings.push(format!(
            "수식 문법을 그대로 옮길 수 없어 원래 수식을 텍스트로 저장했습니다. {}",
            unconverted.join(", ")
        ));
    }
    Ok(output)
}

fn print_tables(sheets: &[Sheet], target: &Path, check: Check<'_>) -> io::Result<()> {
    use std::fmt::Write as _;
    let mut html = String::from("<html><body>");
    let mut cells = 0_usize;
    for sheet in sheets {
        write!(
            html,
            "<h2>{}</h2><table>",
            crate::safe_html::escape(&sheet.name)
        )
        .unwrap();
        for (row, width) in sheet.rows() {
            check()?;
            cells = cells.saturating_add(width as usize);
            if cells > BUDGET {
                return Err(invalid("표 PDF의 셀 한도를 초과했습니다."));
            }
            html.push_str("<tr>");
            for col in 0..width {
                let value = sheet
                    .cells
                    .get(&(row, col))
                    .map(Cell::display)
                    .transpose()?
                    .unwrap_or_default();
                write!(html, "<td>{}</td>", crate::safe_html::escape(&value)).unwrap();
                if html.len() > 128 * 1024 * 1024 {
                    return Err(invalid("표 PDF의 본문 용량 한도를 초과했습니다."));
                }
            }
            html.push_str("</tr>");
        }
        html.push_str("</table>");
    }
    html.push_str("</body></html>");
    crate::qt_render::html_pdf(&html, target, true, check)
}
