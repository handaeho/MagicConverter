use std::ffi::OsString;
use std::fs::File;
use std::io::{self, ErrorKind};
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

use clap::ValueEnum;

#[derive(Clone, Copy, ValueEnum)]
pub enum Kind {
    Word,
    Excel,
    Powerpoint,
}

impl Kind {
    fn executable(self) -> &'static str {
        match self {
            Self::Word => "WINWORD.EXE",
            Self::Excel => "EXCEL.EXE",
            Self::Powerpoint => "POWERPNT.EXE",
        }
    }

    fn collection(self) -> &'static str {
        match self {
            Self::Word => "Documents",
            Self::Excel => "Workbooks",
            Self::Powerpoint => "Presentations",
        }
    }
}

fn format(edge: &crate::Edge) -> Option<(Kind, Option<&'static str>)> {
    if edge.action_key == "office:xlsx_print_pdf" {
        return Some((Kind::Excel, None));
    }
    if edge.action_key != "document:pdf" {
        return None;
    }
    match (edge.engine_id.as_str(), edge.source.as_str()) {
        ("documents", "docx") => Some((Kind::Word, None)),
        ("documents", "docm" | "dotx" | "dotm") => Some((Kind::Word, Some("docx"))),
        ("spreadsheets", "xlsx") => Some((Kind::Excel, None)),
        ("spreadsheets", "xlsm" | "xltx" | "xltm") => Some((Kind::Excel, Some("xlsx"))),
        ("presentations", "ppt" | "pptx") => Some((Kind::Powerpoint, None)),
        ("presentations", "pptm" | "potx" | "potm" | "ppsx" | "ppsm") => {
            Some((Kind::Powerpoint, Some("pptx")))
        }
        _ => None,
    }
}

pub fn supports(edge: &crate::Edge) -> bool {
    format(edge).is_some()
}

pub fn convert(
    source: &Path,
    destination: &Path,
    edge: &crate::Edge,
    check: &dyn Fn() -> io::Result<()>,
    warnings: &mut Vec<String>,
) -> io::Result<()> {
    let (kind, standard) =
        format(edge).ok_or_else(|| io::Error::other("지원하지 않는 Office 형식입니다."))?;
    let print_area = edge.action_key == "office:xlsx_print_pdf";
    let stage = tempfile::Builder::new().prefix("magic-office-").tempdir()?;
    let generated = stage.path().join("result.pdf");
    let normalized;
    let source = if let Some(standard) = standard {
        normalized = stage.path().join(format!("document.{standard}"));
        crate::office_package::convert(
            source,
            &normalized,
            crate::office_package::Action::Standardize,
            check,
        )?;
        warnings.push("Office 템플릿·매크로 문서를 표준 OOXML로 읽었습니다. 매크로·ActiveX·포함 개체는 결과에 포함되지 않습니다.".to_owned());
        normalized.as_path()
    } else {
        source
    };
    let mut arguments: Vec<OsString> = vec![
        "office-worker".into(),
        kind.to_possible_value()
            .expect("Office kind")
            .get_name()
            .into(),
        source.as_os_str().to_owned(),
        generated.as_os_str().to_owned(),
    ];
    if print_area {
        arguments.push("--print-area".into());
    }
    let result = crate::engine_process::run(
        &std::env::current_exe()?,
        &arguments,
        None,
        None,
        check,
        &|| Ok(()),
    )?;
    if result.code != 0 {
        return Err(io::Error::other(result.stderr.trim().to_owned()));
    }
    if print_area {
        warnings.push(
            "Excel 인쇄 영역과 반복 제목 및 페이지 설정을 적용해 PDF를 출력했습니다.".to_owned(),
        );
    }
    let mut incoming = File::open(generated)?;
    let length = incoming.metadata()?.len();
    if length == 0 {
        return Err(io::Error::new(
            ErrorKind::InvalidData,
            "Office PDF 출력이 비어 있습니다.",
        ));
    }
    crate::copy_stream(
        &mut incoming,
        &mut File::create(destination)?,
        length,
        check,
        |_| Ok(()),
    )
    .map(|_| ())
}

mod dispatch {
    use std::mem::ManuallyDrop;
    use std::ptr::null_mut;
    use windows::Win32::Foundation::{DISP_E_PARAMNOTFOUND, VARIANT_BOOL};
    use windows::Win32::System::Com::{
        DISPATCH_FLAGS, DISPATCH_METHOD, DISPATCH_PROPERTYGET, DISPATCH_PROPERTYPUT, DISPPARAMS,
        EXCEPINFO, IDispatch,
    };
    use windows::Win32::System::Variant::{
        VARENUM, VARIANT, VARIANT_0, VARIANT_0_0, VARIANT_0_0_0, VT_BOOL, VT_BSTR, VT_DISPATCH,
        VT_ERROR, VT_I2, VT_I4, VT_I8, VT_R4, VT_R8, VT_UI4, VariantClear, VariantCopy,
    };
    use windows_core::{BSTR, GUID, PCWSTR};

    #[repr(transparent)]
    #[derive(Default)]
    pub struct Value(VARIANT);

    impl Value {
        fn new(vt: VARENUM, data: VARIANT_0_0_0) -> Self {
            Self(VARIANT {
                Anonymous: VARIANT_0 {
                    Anonymous: ManuallyDrop::new(VARIANT_0_0 {
                        vt,
                        Anonymous: data,
                        ..Default::default()
                    }),
                },
            })
        }

        pub fn missing() -> Self {
            Self::new(
                VT_ERROR,
                VARIANT_0_0_0 {
                    scode: DISP_E_PARAMNOTFOUND.0,
                },
            )
        }

        pub fn number(number: i32) -> Self {
            Self::new(VT_I4, VARIANT_0_0_0 { lVal: number })
        }

        pub fn boolean(value: bool) -> Self {
            Self::new(
                VT_BOOL,
                VARIANT_0_0_0 {
                    boolVal: VARIANT_BOOL(if value { -1 } else { 0 }),
                },
            )
        }

        pub fn text(value: BSTR) -> Self {
            Self::new(
                VT_BSTR,
                VARIANT_0_0_0 {
                    bstrVal: ManuallyDrop::new(value),
                },
            )
        }

        pub fn as_number(&self) -> std::io::Result<i64> {
            // Read only the union member selected by the automation type tag.
            let value: &VARIANT_0_0 = unsafe { &self.0.Anonymous.Anonymous };
            unsafe {
                match value.vt {
                    VT_I2 => Ok(i64::from(value.Anonymous.iVal)),
                    VT_I4 => Ok(i64::from(value.Anonymous.lVal)),
                    VT_I8 => Ok(value.Anonymous.llVal),
                    VT_UI4 => Ok(i64::from(value.Anonymous.ulVal)),
                    VT_BOOL => Ok(i64::from(value.Anonymous.boolVal.0)),
                    _ => Err(std::io::Error::other(
                        "Office 속성이 정수를 반환하지 않았습니다.",
                    )),
                }
            }
        }

        pub fn as_string(&self) -> std::io::Result<String> {
            let value: &VARIANT_0_0 = unsafe { &self.0.Anonymous.Anonymous };
            if value.vt == VT_BSTR {
                return Ok(unsafe { value.Anonymous.bstrVal.to_string() });
            }
            Err(std::io::Error::other(
                "Office 속성이 문자열을 반환하지 않았습니다.",
            ))
        }

        pub fn as_object(&self) -> std::io::Result<IDispatch> {
            let value: &VARIANT_0_0 = unsafe { &self.0.Anonymous.Anonymous };
            if value.vt == VT_DISPATCH
                && let Some(object) = unsafe { value.Anonymous.pdispVal.as_ref() }
            {
                return Ok(object.clone());
            }
            Err(std::io::Error::other(
                "Office 속성이 개체를 반환하지 않았습니다.",
            ))
        }

        pub fn is_false(&self) -> bool {
            let value: &VARIANT_0_0 = unsafe { &self.0.Anonymous.Anonymous };
            value.vt == VT_BOOL && unsafe { value.Anonymous.boolVal.0 == 0 }
        }

        pub fn is_zero(&self) -> bool {
            let value: &VARIANT_0_0 = unsafe { &self.0.Anonymous.Anonymous };
            unsafe {
                match value.vt {
                    VT_R4 => value.Anonymous.fltVal == 0.0,
                    VT_R8 => value.Anonymous.dblVal == 0.0,
                    _ => self.as_number().is_ok_and(|number| number == 0),
                }
            }
        }

        pub fn duplicate(&self) -> std::io::Result<Self> {
            let mut duplicate = Self::default();
            unsafe { VariantCopy(&mut duplicate.0, &self.0) }.map_err(std::io::Error::other)?;
            Ok(duplicate)
        }
    }

    impl Drop for Value {
        fn drop(&mut self) {
            let _ = unsafe { VariantClear(&mut self.0) };
        }
    }

    fn invoke(
        object: &IDispatch,
        name: &str,
        flags: DISPATCH_FLAGS,
        mut arguments: Vec<Value>,
    ) -> std::io::Result<Value> {
        let name_wide: Vec<_> = name.encode_utf16().chain([0]).collect();
        let iid = GUID::zeroed();
        let mut id = 0;
        let mut property_put = -3;
        let mut result = Value::default();
        let mut exception = EXCEPINFO::default();
        let property = flags == DISPATCH_PROPERTYPUT;
        arguments.reverse();
        let parameters = DISPPARAMS {
            rgvarg: if arguments.is_empty() {
                null_mut()
            } else {
                arguments.as_mut_ptr().cast()
            },
            cArgs: arguments.len() as u32,
            rgdispidNamedArgs: if property {
                &mut property_put
            } else {
                null_mut()
            },
            cNamedArgs: u32::from(property),
        };
        // These buffers remain live for the synchronous call and own all arguments.
        unsafe { object.GetIDsOfNames(&iid, &PCWSTR(name_wide.as_ptr()), 1, 0, &mut id) }
            .map_err(|error| std::io::Error::other(format!("Office {name}: {error}")))?;
        let status = unsafe {
            object.Invoke(
                id,
                &iid,
                0,
                flags,
                &parameters,
                Some(&mut result.0),
                Some(&mut exception),
                None,
            )
        };
        let description = unsafe { ManuallyDrop::take(&mut exception.bstrDescription) };
        drop(unsafe { ManuallyDrop::take(&mut exception.bstrSource) });
        drop(unsafe { ManuallyDrop::take(&mut exception.bstrHelpFile) });
        status.map_err(|error| {
            std::io::Error::other(format!("Office {name}: {error} {description}"))
        })?;
        Ok(result)
    }

    pub trait Automation {
        fn call(&self, name: &str, args: Vec<Value>) -> std::io::Result<Value>;
        fn property(&self, name: &str) -> std::io::Result<Value>;
        fn put(&self, name: &str, value: Value) -> std::io::Result<()>;
        fn object(&self, name: &str) -> std::io::Result<IDispatch>;
        fn integer(&self, name: &str) -> std::io::Result<i64>;
        fn text(&self, name: &str) -> std::io::Result<String>;
        fn item(&self, index: i64) -> std::io::Result<IDispatch>;
    }

    impl Automation for IDispatch {
        fn call(&self, name: &str, args: Vec<Value>) -> std::io::Result<Value> {
            invoke(self, name, DISPATCH_METHOD | DISPATCH_PROPERTYGET, args)
        }
        fn property(&self, name: &str) -> std::io::Result<Value> {
            invoke(self, name, DISPATCH_PROPERTYGET, vec![])
        }
        fn put(&self, name: &str, value: Value) -> std::io::Result<()> {
            invoke(self, name, DISPATCH_PROPERTYPUT, vec![value]).map(|_| ())
        }
        fn object(&self, name: &str) -> std::io::Result<IDispatch> {
            self.property(name)?.as_object()
        }
        fn integer(&self, name: &str) -> std::io::Result<i64> {
            self.call(name, vec![])?.as_number()
        }
        fn text(&self, name: &str) -> std::io::Result<String> {
            self.property(name)?.as_string()
        }
        fn item(&self, index: i64) -> std::io::Result<IDispatch> {
            self.call("Item", vec![Value::number(index as i32)])?
                .as_object()
        }
    }
}

use dispatch::{Automation, Value};
use std::os::windows::ffi::OsStrExt;
use std::os::windows::io::{FromRawHandle, OwnedHandle};
use windows::Win32::System::Com::{
    CLSIDFromProgID, COINIT_APARTMENTTHREADED, CoInitializeEx, CoUninitialize, CreateFileMoniker,
    GetRunningObjectTable, IDispatch,
};
use windows::Win32::System::Ole::GetActiveObject;
use windows::Win32::UI::Accessibility::AccessibleObjectFromWindow;
use windows_core::{BSTR, Interface, PCWSTR, w};
use windows_sys::Win32::System::Diagnostics::ToolHelp::{
    CreateToolhelp32Snapshot, PROCESSENTRY32W, Process32FirstW, Process32NextW, TH32CS_SNAPPROCESS,
};
use windows_sys::Win32::UI::WindowsAndMessaging::{
    EnumChildWindows, EnumWindows, GetClassNameW, GetWindowThreadProcessId, OBJID_NATIVEOM,
};

struct Apartment;
impl Drop for Apartment {
    fn drop(&mut self) {
        unsafe { CoUninitialize() };
    }
}

fn executable(kind: Kind) -> io::Result<PathBuf> {
    for variable in ["ProgramFiles", "ProgramFiles(x86)"] {
        if let Some(root) = std::env::var_os(variable) {
            for relative in ["root/Office16", "Office16", "Office15"] {
                let path = PathBuf::from(&root)
                    .join("Microsoft Office")
                    .join(relative)
                    .join(kind.executable());
                if path.is_file() {
                    return Ok(path);
                }
            }
        }
    }
    Err(io::Error::new(
        ErrorKind::NotFound,
        format!("{} 설치 경로를 찾지 못했습니다.", kind.executable()),
    ))
}

fn powerpoint_processes() -> io::Result<Vec<u32>> {
    use std::os::windows::io::AsRawHandle;
    let handle = unsafe { CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0) };
    if handle == windows_sys::Win32::Foundation::INVALID_HANDLE_VALUE {
        return Err(io::Error::last_os_error());
    }
    let handle = unsafe { OwnedHandle::from_raw_handle(handle) };
    let mut entry = PROCESSENTRY32W {
        dwSize: std::mem::size_of::<PROCESSENTRY32W>() as u32,
        ..Default::default()
    };
    let mut processes = Vec::new();
    let mut more = unsafe { Process32FirstW(handle.as_raw_handle(), &mut entry) };
    if more == 0 {
        return Err(io::Error::last_os_error());
    }
    while more != 0 {
        let name = String::from_utf16_lossy(
            &entry.szExeFile[..entry
                .szExeFile
                .iter()
                .position(|ch| *ch == 0)
                .unwrap_or(entry.szExeFile.len())],
        );
        if name.eq_ignore_ascii_case("POWERPNT.EXE") {
            processes.push(entry.th32ProcessID);
        }
        more = unsafe { Process32NextW(handle.as_raw_handle(), &mut entry) };
    }
    Ok(processes)
}

fn powerpoint_window(pid: u32) -> bool {
    use windows_sys::Win32::Foundation::{HWND, LPARAM};
    struct State {
        pid: u32,
        found: bool,
    }
    unsafe extern "system" fn visit(window: HWND, data: LPARAM) -> i32 {
        let state = unsafe { &mut *(data as *mut State) };
        let mut owner = 0;
        unsafe { GetWindowThreadProcessId(window, &mut owner) };
        if owner == state.pid {
            let mut class = [0; 64];
            let size = unsafe { GetClassNameW(window, class.as_mut_ptr(), class.len() as i32) };
            state.found =
                size > 0 && String::from_utf16_lossy(&class[..size as usize]) == "PPTFrameClass";
        }
        i32::from(!state.found)
    }
    let mut state = State { pid, found: false };
    unsafe { EnumWindows(Some(visit), &mut state as *mut State as isize) };
    state.found
}

fn excel_workbook(pid: u32) -> Option<IDispatch> {
    use windows_sys::Win32::Foundation::{HWND, LPARAM};
    struct Search {
        pid: u32,
        window: HWND,
    }
    unsafe extern "system" fn child(window: HWND, data: LPARAM) -> i32 {
        let search = unsafe { &mut *(data as *mut Search) };
        let mut class = [0; 32];
        let length = unsafe { GetClassNameW(window, class.as_mut_ptr(), class.len() as i32) };
        if length == 6 && class[..6] == [69, 88, 67, 69, 76, 55] {
            let mut pid = 0;
            unsafe { GetWindowThreadProcessId(window, &mut pid) };
            if pid == search.pid {
                search.window = window;
                return 0;
            }
        }
        1
    }
    unsafe extern "system" fn top(window: HWND, data: LPARAM) -> i32 {
        let mut owner = 0;
        unsafe { GetWindowThreadProcessId(window, &mut owner) };
        if owner == unsafe { (*(data as *const Search)).pid } {
            unsafe { EnumChildWindows(window, Some(child), data) };
        }
        i32::from(unsafe { (*(data as *const Search)).window.is_null() })
    }
    let mut search = Search {
        pid,
        window: std::ptr::null_mut(),
    };
    unsafe { EnumWindows(Some(top), &mut search as *mut Search as isize) };
    if search.window.is_null() {
        return None;
    }
    let mut object = std::ptr::null_mut();
    unsafe {
        AccessibleObjectFromWindow(
            windows::Win32::Foundation::HWND(search.window),
            OBJID_NATIVEOM as u32,
            &IDispatch::IID,
            &mut object,
        )
    }
    .ok()?;
    if object.is_null() {
        return None;
    }
    let window = unsafe { IDispatch::from_raw(object) };
    window.object("Parent").ok()
}

fn path_value(path: &Path) -> Value {
    Value::text(BSTR::from_wide(
        &path.as_os_str().encode_wide().collect::<Vec<_>>(),
    ))
}

fn remove_recent(app: &IDispatch, path: &Path) -> io::Result<()> {
    let recent = app.object("RecentFiles")?;
    for index in (1..=recent.integer("Count")?.min(200)).rev() {
        let item = recent.item(index)?;
        let parent = PathBuf::from(item.text("Path")?);
        let name = item.text("Name")?;
        if parent == path || parent.join(name) == path {
            item.call("Delete", vec![])?;
            break;
        }
    }
    Ok(())
}

fn attach(
    kind: Kind,
    path: Option<&Path>,
    process: &crate::engine_process::Process,
) -> io::Result<IDispatch> {
    let start = Instant::now();
    if let Some(path) = path {
        let wide: Vec<_> = path.as_os_str().encode_wide().chain([0]).collect();
        let table = unsafe { GetRunningObjectTable(0) }.map_err(io::Error::other)?;
        let moniker =
            unsafe { CreateFileMoniker(PCWSTR(wide.as_ptr())) }.map_err(io::Error::other)?;
        while process.wait(Duration::from_millis(50))?.is_none()
            && start.elapsed() < Duration::from_secs(25)
        {
            let document: Option<IDispatch> = unsafe { table.GetObject(&moniker) }
                .ok()
                .and_then(|object| object.cast().ok())
                .or_else(|| {
                    if matches!(kind, Kind::Excel) {
                        excel_workbook(process.id())
                    } else {
                        None
                    }
                });
            if let Some(document) = document {
                let full_name = document.text("FullName")?;
                if !full_name.eq_ignore_ascii_case(&path.to_string_lossy()) {
                    return Err(io::Error::other(
                        "Office 시작 문서 경로가 일치하지 않습니다.",
                    ));
                }
                let app = document.object("Application")?;
                let window = if matches!(kind, Kind::Word) {
                    document.object("Windows")?.item(1)?.integer("Hwnd")?
                } else {
                    app.integer("Hwnd")?
                };
                let mut pid = 0;
                unsafe { GetWindowThreadProcessId(window as _, &mut pid) };
                if pid != process.id() || pid == 0 {
                    return Err(io::Error::other(
                        "실행한 Office 서버의 소유권을 확인하지 못했습니다.",
                    ));
                }
                remove_recent(&app, path)?;
                app.put("Visible", Value::boolean(false))?;
                document.call("Close", vec![Value::boolean(false)])?;
                return Ok(app);
            }
        }
    } else {
        let clsid =
            unsafe { CLSIDFromProgID(w!("PowerPoint.Application")) }.map_err(io::Error::other)?;
        while process.wait(Duration::from_millis(50))?.is_none()
            && start.elapsed() < Duration::from_secs(25)
        {
            let mut running = None;
            if unsafe { GetActiveObject(&clsid, None, &mut running) }.is_ok()
                && let Some(running) = running
                && powerpoint_window(process.id())
                && powerpoint_processes()? == [process.id()]
            {
                return running.cast().map_err(io::Error::other);
            }
        }
    }
    Err(io::Error::new(
        ErrorKind::TimedOut,
        "전용 Office 서버 연결 시간이 초과됐거나 서버가 종료됐습니다.",
    ))
}

pub fn worker(kind: Kind, source: &Path, destination: &Path, print_area: bool) -> io::Result<()> {
    if print_area && !matches!(kind, Kind::Excel) {
        return Err(io::Error::new(
            ErrorKind::InvalidInput,
            "Excel 인쇄 PDF는 XLSX만 지원합니다.",
        ));
    }
    unsafe { CoInitializeEx(None, COINIT_APARTMENTTHREADED) }
        .ok()
        .map_err(io::Error::other)?;
    let _apartment = Apartment;
    let mut arguments: Vec<OsString> = match kind {
        Kind::Word => ["/a", "/q", "/n"].into_iter().map(Into::into).collect(),
        Kind::Excel => ["/x", "/e", "/automation"]
            .into_iter()
            .map(Into::into)
            .collect(),
        Kind::Powerpoint => vec!["/EMBEDDING".into()],
    };
    let directory = tempfile::Builder::new()
        .prefix("magic-office-bootstrap-")
        .tempdir()?;
    let bootstrap = match kind {
        Kind::Word => {
            let path = directory.path().join("bootstrap.docx");
            crate::text_documents::write_docx(&path, "<p></p>", &|| Ok(()))?;
            Some(path)
        }
        Kind::Excel => {
            let path = directory.path().join("bootstrap.xlsx");
            let mut book = rust_xlsxwriter::Workbook::new();
            book.add_worksheet();
            book.save(&path).map_err(io::Error::other)?;
            Some(path)
        }
        Kind::Powerpoint => {
            if !powerpoint_processes()?.is_empty() {
                return Err(io::Error::other(
                    "기존 Office 서버(PowerPoint)를 재사용할 수 없습니다.",
                ));
            }
            None
        }
    };
    if let Some(path) = &bootstrap {
        arguments.push(path.as_os_str().to_owned());
    }
    let executable = executable(kind)?;
    let log = tempfile::tempfile()?;
    let process = crate::engine_process::Process::start_limited(
        &executable,
        &arguments,
        &log,
        &log,
        Some(2 * 1024 * 1024 * 1024),
    )?;
    let app = attach(kind, bootstrap.as_deref(), &process)?;
    let collection = app.object(kind.collection())?;
    if collection.integer("Count")? != 0 {
        return Err(io::Error::other(
            "Office 서버에 이미 열린 문서가 있어 입력을 거부했습니다.",
        ));
    }
    app.put("AutomationSecurity", Value::number(3))?;
    app.put(
        "DisplayAlerts",
        if matches!(kind, Kind::Excel) {
            Value::boolean(false)
        } else {
            Value::number(i32::from(matches!(kind, Kind::Powerpoint)))
        },
    )?;
    let result = export(kind, &app, &collection, source, destination, print_area);
    let _ = app.call("Quit", vec![]);
    result
}

fn export(
    kind: Kind,
    app: &IDispatch,
    collection: &IDispatch,
    source: &Path,
    target: &Path,
    print_area: bool,
) -> io::Result<()> {
    let missing = Value::missing;
    let no = || Value::boolean(false);
    let yes = || Value::boolean(true);
    let empty = || Value::text(BSTR::new());
    let args = match kind {
        Kind::Word => vec![
            path_value(source),
            no(),
            yes(),
            no(),
            empty(),
            missing(),
            no(),
            empty(),
            missing(),
            missing(),
            missing(),
            no(),
            no(),
        ],
        Kind::Excel => vec![
            path_value(source),
            Value::number(0),
            yes(),
            missing(),
            empty(),
            empty(),
            yes(),
        ],
        Kind::Powerpoint => vec![
            path_value(source),
            Value::number(-1),
            Value::number(0),
            Value::number(0),
        ],
    };
    let document = collection.call("Open", args)?.as_object()?;
    let result = (|| match kind {
        Kind::Word => document.call(
            "ExportAsFixedFormat",
            vec![path_value(target), Value::number(17), no()],
        ),
        Kind::Powerpoint => document.call("SaveAs", vec![path_value(target), Value::number(32)]),
        Kind::Excel => {
            if print_area {
                if let Some(printer) = std::env::var_os("MAGIC_CONVERTER_EXCEL_VALIDATION_PRINTER")
                {
                    app.put("ActivePrinter", path_value(Path::new(&printer)))?;
                }
                verify_print_areas(&document)?;
            }
            document.call(
                "ExportAsFixedFormat",
                vec![
                    Value::number(0),
                    path_value(target),
                    missing(),
                    missing(),
                    no(),
                    missing(),
                    missing(),
                    no(),
                ],
            )
        }
    })();
    let close = document.call(
        "Close",
        if matches!(kind, Kind::Powerpoint) {
            vec![]
        } else {
            vec![no()]
        },
    );
    result.and(close)?;
    Ok(())
}

fn bounds(range: &IDispatch) -> io::Result<[i64; 4]> {
    let row = range.integer("Row")?;
    let col = range.integer("Column")?;
    Ok([
        row,
        col,
        row + range.object("Rows")?.integer("Count")? - 1,
        col + range.object("Columns")?.integer("Count")? - 1,
    ])
}

fn automatic_bounds(sheet: &IDispatch, setup: &IDispatch) -> io::Result<i64> {
    let used = sheet.object("UsedRange")?;
    let expected = bounds(&used)?;
    if expected[2] * expected[3] > 30_000 {
        return Err(io::Error::other(
            "자동 인쇄 영역의 합계는 30,000셀까지 지원합니다.",
        ));
    }
    let cells = used.object("Cells")?;
    let first = cells.call("Item", vec![Value::number(1), Value::number(1)])?;
    let last = cells.call(
        "Item",
        vec![
            Value::number((expected[2] - expected[0] + 1) as i32),
            Value::number((expected[3] - expected[1] + 1) as i32),
        ],
    )?;
    let find = |after: &Value, order, direction, axis| -> io::Result<i64> {
        let value = used.call(
            "Find",
            vec![
                Value::text(BSTR::from("*")),
                after.duplicate()?,
                Value::number(-4123),
                Value::number(2),
                Value::number(order),
                Value::number(direction),
                Value::boolean(false),
                Value::boolean(false),
                Value::boolean(false),
            ],
        )?;
        value
            .as_object()
            .map_err(|_| io::Error::other("자동 인쇄 영역을 정할 값이나 수식이 없습니다."))?
            .integer(axis)
    };
    let candidate = [
        find(&last, 1, 1, "Row")?,
        find(&last, 2, 1, "Column")?,
        find(&first, 1, 2, "Row")?,
        find(&first, 2, 2, "Column")?,
    ];
    let shapes = sheet.object("Shapes")?;
    let shape_count = shapes.integer("Count")?;
    if candidate != expected
        || !used.property("MergeCells")?.is_false()
        || shape_count > 1
        || setup.integer("PrintComments")? != -4142
    {
        return Err(io::Error::other(
            "자동 인쇄 영역을 정할 수 없습니다. 직사각형 인쇄 영역을 저장하세요.",
        ));
    }
    if shape_count == 1 {
        let shape = shapes.item(1)?;
        if shape.integer("Type")? != 1
            || shape.integer("AutoShapeType")? != 1
            || shape.integer("Visible")? != -1
            || !shape.property("Rotation")?.is_zero()
        {
            return Err(io::Error::other(
                "자동 인쇄 영역은 회전하지 않은 표시 직사각형 도형 한 개만 지원합니다.",
            ));
        }
        let last = shape.object("BottomRightCell")?;
        return Ok(
            candidate[2].max(last.integer("Row")?) * candidate[3].max(last.integer("Column")?)
        );
    }
    Ok(candidate[2] * candidate[3])
}

fn verify_print_areas(workbook: &IDispatch) -> io::Result<()> {
    let sheets = workbook.object("Worksheets")?;
    let charts = workbook.object("Charts")?;
    let count = sheets.integer("Count")?;
    let chart_count = charts.integer("Count")?;
    if workbook.object("Sheets")?.integer("Count")? != count + chart_count {
        return Err(io::Error::other(
            "Excel 인쇄 영역 모드는 워크시트와 차트 시트만 지원합니다.",
        ));
    }
    if count + chart_count > 20 {
        return Err(io::Error::other(
            "Excel 인쇄 영역 모드는 시트 최대 20개를 지원합니다.",
        ));
    }
    let mut visible_charts = 0;
    for index in 1..=chart_count {
        visible_charts += i64::from(charts.item(index)?.integer("Visible")? == -1);
    }
    if visible_charts > 2 {
        return Err(io::Error::other("표시된 차트 시트는 두 개까지 지원합니다."));
    }
    let cell = r"\$?[A-Z]{1,3}\$?[1-9][0-9]{0,6}";
    let address = regex::Regex::new(&format!(r"^{cell}(?::{cell})?$")).expect("Excel print range");
    let title = [
        r"^\$?[1-9][0-9]{0,6}:\$?[1-9][0-9]{0,6}$",
        r"^\$?[A-Z]{1,3}:\$?[A-Z]{1,3}$",
    ]
    .map(|pattern| regex::Regex::new(pattern).expect("Excel print titles"));
    let mut total = 0;
    for index in 1..=count {
        let sheet = sheets.item(index)?;
        if sheet.integer("Visible")? != -1 {
            continue;
        }
        let setup = sheet.object("PageSetup")?;
        let area = setup.text("PrintArea")?;
        let titles = [
            setup.text("PrintTitleRows")?,
            setup.text("PrintTitleColumns")?,
        ];
        if area.is_empty() {
            if titles.iter().any(|title| !title.is_empty()) {
                return Err(io::Error::other(
                    "반복 인쇄 제목을 사용하려면 인쇄 영역을 저장하세요.",
                ));
            }
            total += automatic_bounds(&sheet, &setup)?;
        } else {
            let range = |address: &str| -> io::Result<[i64; 4]> {
                bounds(
                    &sheet
                        .call("Range", vec![Value::text(BSTR::from(address))])?
                        .as_object()?,
                )
            };
            let parts: Vec<_> = area.split(',').collect();
            if parts.len() > 3 || parts.iter().any(|part| !address.is_match(part)) {
                return Err(io::Error::other(
                    "각 표시 워크시트에 직사각형 인쇄 영역을 세 개까지 저장하세요.",
                ));
            }
            let mut headers = [None; 2];
            for (axis, value) in titles
                .iter()
                .enumerate()
                .filter(|(_, value)| !value.is_empty())
            {
                if !title[axis].is_match(value) {
                    return Err(io::Error::other(
                        "반복 인쇄 제목은 연속된 행 또는 열로 지정하세요.",
                    ));
                }
                headers[axis] = Some(range(value)?);
            }
            let mut regions = Vec::<[i64; 4]>::new();
            for part in parts {
                let region = range(part)?;
                if regions.iter().any(|other| {
                    region[0] <= other[2]
                        && other[0] <= region[2]
                        && region[1] <= other[3]
                        && other[1] <= region[3]
                }) {
                    return Err(io::Error::other("인쇄 영역은 서로 겹칠 수 없습니다."));
                }
                regions.push(region);
                let mut cells = 1;
                for axis in 0..2 {
                    let (start, end) = (region[axis], region[axis + 2]);
                    let mut extent = end - start + 1;
                    if let Some(header) = headers[axis] {
                        let (header_start, header_end) = (header[axis], header[axis + 2]);
                        if header_end > end {
                            return Err(io::Error::other(
                                "반복 인쇄 제목은 인쇄 영역 안이나 위쪽·왼쪽에 있어야 합니다.",
                            ));
                        }
                        extent += (header_end.min(start - 1) - header_start + 1).max(0);
                    }
                    cells *= extent;
                }
                total += cells;
            }
        }
        if total > 30_000 {
            return Err(io::Error::other(
                "Excel 인쇄 영역과 반복 제목의 합계는 30,000셀까지 지원합니다.",
            ));
        }
    }
    if total == 0 && visible_charts == 0 {
        return Err(io::Error::other(
            "표시 워크시트의 인쇄 영역이나 표시 차트가 필요합니다.",
        ));
    }
    Ok(())
}
