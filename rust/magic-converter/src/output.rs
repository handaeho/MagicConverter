use serde::Deserialize;
use std::fs::{self, File};
use std::io::{self, ErrorKind};
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};
use tempfile::{Builder, NamedTempFile, TempPath};

#[derive(Clone, Deserialize)]
#[serde(default)]
pub struct OutputRule {
    location: String,
    directory: PathBuf,
    template: String,
    collision: String,
}

impl Default for OutputRule {
    fn default() -> Self {
        Self {
            location: "source".to_owned(),
            directory: PathBuf::new(),
            template: "{stem}_{tag}".to_owned(),
            collision: "rename".to_owned(),
        }
    }
}

fn illegal(character: char) -> bool {
    character < ' ' || r#"<>:"/\|?*"#.contains(character)
}

fn validate(name: &str) -> io::Result<()> {
    let first = name.split('.').next().unwrap_or("").to_ascii_uppercase();
    let reserved = matches!(first.as_str(), "CON" | "PRN" | "AUX" | "NUL")
        || ["COM", "LPT"].iter().any(|prefix| {
            first
                .strip_prefix(prefix)
                .is_some_and(|number| number.len() == 1 && ("1"..="9").contains(&number))
        });
    if name.is_empty()
        || name.ends_with(['.', ' '])
        || name.chars().count() > 200
        || name.chars().any(illegal)
        || reserved
    {
        return Err(io::Error::new(
            ErrorKind::InvalidInput,
            "출력 이름은 Windows 파일 이름으로 사용할 수 있어야 합니다.",
        ));
    }
    Ok(())
}

fn expand(template: &str, stem: &str, tag: &str, preset: &str) -> io::Result<String> {
    let invalid = || {
        io::Error::new(
            ErrorKind::InvalidInput,
            "이름에는 {stem}, {tag}, {preset}만 사용할 수 있습니다.",
        )
    };
    let mut output = String::new();
    let mut characters = template.chars().peekable();
    while let Some(character) = characters.next() {
        match character {
            '{' | '}' if characters.peek() == Some(&character) => {
                characters.next();
                output.push(character);
            }
            '{' => {
                let mut field = String::new();
                loop {
                    let ch = characters.next().ok_or_else(&invalid)?;
                    if ch == '}' {
                        break;
                    }
                    field.push(ch);
                }
                output.push_str(match field.as_str() {
                    "stem" => stem,
                    "tag" => tag,
                    "preset" => preset,
                    _ => return Err(invalid()),
                });
            }
            '}' => return Err(invalid()),
            _ => output.push(character),
        }
    }
    validate(&output)?;
    Ok(output)
}

impl OutputRule {
    pub fn staging(directory: PathBuf, source: &Path) -> Self {
        let stem = source
            .file_stem()
            .and_then(|value| value.to_str())
            .unwrap_or("input");
        let escaped = stem.replace('{', "{{").replace('}', "}}");
        Self {
            location: "folder".to_owned(),
            directory,
            template: format!("{escaped}_{{tag}}"),
            collision: "error".to_owned(),
        }
    }

    pub fn validate_recipe(&self) -> io::Result<()> {
        let directory = self.directory.to_string_lossy();
        if !matches!(self.location.as_str(), "source" | "folder")
            || !matches!(self.collision.as_str(), "rename" | "error")
            || (self.location == "folder" && !self.directory.is_absolute())
            || directory.chars().count() > 2000
            || directory.chars().any(|ch| ch < ' ')
            || self.template.chars().count() > 160
            || self.template.chars().any(|ch| ch < ' ')
        {
            return Err(io::Error::new(
                ErrorKind::InvalidInput,
                "출력 설정이 올바르지 않습니다.",
            ));
        }
        expand(&self.template, "sample", "converted", "recipe")?;
        Ok(())
    }

    fn location_and_name<'a>(
        &'a self,
        source: &'a Path,
        suffix: &str,
        tag: &str,
        preset: &str,
    ) -> io::Result<(&'a Path, String)> {
        let root = match self.location.as_str() {
            "source" => source.parent().expect("absolute input"),
            "folder" if self.directory.is_absolute() => &self.directory,
            _ => {
                return Err(io::Error::new(
                    ErrorKind::InvalidInput,
                    "출력 폴더는 절대 경로로 지정하세요.",
                ));
            }
        };
        let name = source
            .file_name()
            .and_then(|name| name.to_str())
            .ok_or_else(|| {
                io::Error::new(
                    ErrorKind::InvalidInput,
                    "원본 파일 이름을 읽을 수 없습니다.",
                )
            })?;
        // Python's ordinary Path.stem drops only the last suffix. Its original-copy
        // route explicitly drops the full extension for compound archives.
        let stem = if tag == "copy" && suffix[1..].contains('.') {
            &name[..name.len() - suffix.len()]
        } else {
            source
                .file_stem()
                .and_then(|stem| stem.to_str())
                .unwrap_or(name)
        };
        let preset: String = preset
            .chars()
            .map(|ch| if illegal(ch) { '_' } else { ch })
            .collect();
        let preset = match preset.trim_matches([' ', '.']) {
            "" => "converted",
            value => value,
        };
        let basename = expand(&self.template, stem, tag, preset)?;
        fs::create_dir_all(root)?;
        Ok((root, basename))
    }

    pub fn publish(
        &self,
        source: &Path,
        suffix: &str,
        tag: &str,
        preset: &str,
        check: impl Fn() -> io::Result<()>,
        produce: impl FnOnce(&mut File) -> io::Result<()>,
    ) -> io::Result<PathBuf> {
        self.publish_file(source, suffix, tag, preset, check, |mut staged| {
            produce(staged.as_file_mut())?;
            Ok(staged.into_temp_path())
        })
    }

    pub fn publish_external(
        &self,
        source: &Path,
        suffix: &str,
        preset: &str,
        check: impl Fn() -> io::Result<()>,
        produce: impl FnOnce(&Path) -> io::Result<()>,
    ) -> io::Result<PathBuf> {
        self.publish_path(source, suffix, preset, check, |path| {
            produce(path)?;
            if fs::metadata(path)?.len() == 0 {
                return Err(io::Error::new(
                    ErrorKind::InvalidData,
                    "변환 결과가 비어 있습니다.",
                ));
            }
            Ok(())
        })
    }

    pub fn publish_path(
        &self,
        source: &Path,
        suffix: &str,
        preset: &str,
        check: impl Fn() -> io::Result<()>,
        produce: impl FnOnce(&Path) -> io::Result<()>,
    ) -> io::Result<PathBuf> {
        self.publish_path_with_tag(source, suffix, "converted", preset, check, produce)
    }

    pub fn publish_path_with_tag(
        &self,
        source: &Path,
        suffix: &str,
        tag: &str,
        preset: &str,
        check: impl Fn() -> io::Result<()>,
        produce: impl FnOnce(&Path) -> io::Result<()>,
    ) -> io::Result<PathBuf> {
        self.publish_file(source, suffix, tag, preset, check, |staged| {
            let path = staged.into_temp_path();
            produce(&path)?;
            Ok(path)
        })
    }

    fn publish_file(
        &self,
        source: &Path,
        suffix: &str,
        tag: &str,
        preset: &str,
        check: impl Fn() -> io::Result<()>,
        produce: impl FnOnce(NamedTempFile) -> io::Result<TempPath>,
    ) -> io::Result<PathBuf> {
        check()?;
        let (root, basename) = self.location_and_name(source, suffix, tag, preset)?;
        let destination = |index| {
            root.join(if index == 0 {
                format!("{basename}{suffix}")
            } else {
                format!("{basename}({index}){suffix}")
            })
        };
        let collision = |path: &Path| {
            io::Error::new(
                ErrorKind::AlreadyExists,
                format!("같은 이름의 출력이 이미 있습니다: {}", path.display()),
            )
        };
        let mut index = 0;
        while occupied(&destination(index))? {
            check()?;
            if self.collision == "error" {
                return Err(collision(&destination(index)));
            }
            index += 1;
        }
        let staged = Builder::new()
            .prefix(".magic_converter-")
            .suffix(suffix)
            .tempfile_in(root)?;
        let mut staged = produce(staged)?;
        let started = Instant::now();
        loop {
            check()?;
            let path = destination(index);
            match staged.persist_noclobber(&path) {
                Ok(()) => return Ok(path),
                Err(error) => {
                    if error.error.kind() == ErrorKind::AlreadyExists || occupied(&path)? {
                        if self.collision == "error" {
                            return Err(collision(&path));
                        }
                        index += 1;
                    } else if !(cfg!(windows)
                        && matches!(error.error.raw_os_error(), Some(5 | 32 | 33))
                        && started.elapsed() < Duration::from_secs(1))
                    {
                        return Err(error.error);
                    } else {
                        std::thread::sleep(Duration::from_millis(20));
                    }
                    staged = error.path;
                }
            }
        }
    }

    pub fn publish_folder(
        &self,
        source: &Path,
        preset: &str,
        check: impl Fn() -> io::Result<()>,
        produce: impl FnOnce(&Path) -> io::Result<()>,
    ) -> io::Result<PathBuf> {
        check()?;
        let (root, basename) = self.location_and_name(source, "", "converted", preset)?;
        let staged = Builder::new()
            .prefix(".magic_converter-")
            .tempdir_in(root)?;
        produce(staged.path())?;
        let started = Instant::now();
        let mut index = 0;
        loop {
            check()?;
            let destination = root.join(if index == 0 {
                basename.clone()
            } else {
                format!("{basename}({index})")
            });
            match move_folder(staged.path(), &destination) {
                Ok(()) => {
                    let _ = staged.keep();
                    return Ok(destination);
                }
                Err(_) if occupied(&destination)? => {
                    if self.collision == "error" {
                        return Err(io::Error::new(
                            ErrorKind::AlreadyExists,
                            format!(
                                "같은 이름의 출력이 이미 있습니다: {}",
                                destination.display()
                            ),
                        ));
                    }
                    index += 1;
                }
                Err(error)
                    if cfg!(windows)
                        && matches!(error.raw_os_error(), Some(5 | 32 | 33))
                        && started.elapsed() < Duration::from_secs(1) =>
                {
                    std::thread::sleep(Duration::from_millis(20))
                }
                Err(error) => return Err(error),
            }
        }
    }
}

#[cfg(windows)]
pub(crate) fn move_folder(source: &Path, destination: &Path) -> io::Result<()> {
    use std::os::windows::ffi::OsStrExt;
    use windows_sys::Win32::Storage::FileSystem::MoveFileExW;
    let wide = |path: &Path| {
        path.as_os_str()
            .encode_wide()
            .chain([0])
            .collect::<Vec<_>>()
    };
    let source = wide(source);
    let destination = wide(destination);
    // Both buffers are NUL-terminated. Flags 0 exclude replacement and cross-volume copies.
    if unsafe { MoveFileExW(source.as_ptr(), destination.as_ptr(), 0) } == 0 {
        Err(io::Error::last_os_error())
    } else {
        Ok(())
    }
}

#[cfg(not(windows))]
pub(crate) fn move_folder(_: &Path, _: &Path) -> io::Result<()> {
    Err(io::Error::new(
        ErrorKind::Unsupported,
        "archive extraction requires Windows",
    ))
}

fn occupied(path: &Path) -> io::Result<bool> {
    match fs::symlink_metadata(path) {
        Ok(_) => Ok(true),
        Err(error) if error.kind() == ErrorKind::NotFound => Ok(false),
        Err(error) => Err(error),
    }
}
