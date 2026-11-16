use crate::Common;
use clap::{Args, ValueEnum};
use serde_json::{Value, json};
use std::env;
use std::io;
use std::path::PathBuf;
use std::ptr::{null, null_mut};
use windows_sys::Win32::System::Registry::{
    HKEY, HKEY_CURRENT_USER, KEY_SET_VALUE, REG_SZ, RRF_RT_REG_SZ, RegCloseKey, RegCreateKeyExW,
    RegDeleteKeyW, RegGetValueW, RegSetValueExW,
};

const KEY: &str = r"Software\Classes\*\shell\MagicConverter";

#[derive(Args)]
pub struct Shell {
    #[command(flatten)]
    pub common: Common,
    operation: Operation,
}

#[derive(Clone, ValueEnum)]
enum Operation {
    Install,
    Remove,
    Status,
}

struct Key(HKEY);

impl Key {
    fn open(path: &str) -> io::Result<Self> {
        let path = super::wide(path);
        let mut key = null_mut();
        ok(unsafe {
            RegCreateKeyExW(
                HKEY_CURRENT_USER,
                path.as_ptr(),
                0,
                null(),
                0,
                KEY_SET_VALUE,
                null(),
                &mut key,
                null_mut(),
            )
        })?;
        Ok(Self(key))
    }

    fn set(&self, name: &str, value: &str) -> io::Result<()> {
        let name = super::wide(name);
        let value = super::wide(value);
        ok(unsafe {
            RegSetValueExW(
                self.0,
                name.as_ptr(),
                0,
                REG_SZ,
                value.as_ptr().cast(),
                (value.len() * size_of::<u16>()) as u32,
            )
        })
    }
}

impl Drop for Key {
    fn drop(&mut self) {
        unsafe { RegCloseKey(self.0) };
    }
}

fn ok(code: u32) -> io::Result<()> {
    if code == 0 {
        Ok(())
    } else {
        Err(io::Error::from_raw_os_error(code as i32))
    }
}

fn executable() -> io::Result<PathBuf> {
    let own = env::current_exe()?;
    let sibling = own.with_file_name("magic_converter.exe");
    if sibling.is_file() {
        Ok(sibling)
    } else if own
        .file_stem()
        .is_some_and(|name| name == "magic_converter")
    {
        Ok(own)
    } else {
        Err(io::Error::other(
            "탐색기 메뉴를 등록하려면 같은 폴더에 magic_converter.exe가 필요합니다.",
        ))
    }
}

fn command() -> io::Result<String> {
    let key = super::wide(format!("{KEY}\\command"));
    let mut buffer = vec![0_u16; 4096];
    let mut length = (buffer.len() * size_of::<u16>()) as u32;
    let code = unsafe {
        RegGetValueW(
            HKEY_CURRENT_USER,
            key.as_ptr(),
            null(),
            RRF_RT_REG_SZ,
            null_mut(),
            buffer.as_mut_ptr().cast(),
            &mut length,
        )
    };
    if code == 2 {
        return Ok(String::new());
    }
    ok(code)?;
    let end = buffer
        .iter()
        .position(|&value| value == 0)
        .unwrap_or(buffer.len());
    Ok(String::from_utf16_lossy(&buffer[..end]))
}

pub fn run(input: &Shell) -> io::Result<Value> {
    match input.operation {
        Operation::Install => {
            let exe = executable()?;
            let key = Key::open(KEY)?;
            key.set("", "MagicConverter로 변환…")?;
            key.set("MultiSelectModel", "Document")?;
            Key::open(&format!("{KEY}\\command"))?
                .set("", &format!("\"{}\" --open \"%1\"", exe.display()))?;
        }
        Operation::Remove => {
            for key in [format!("{KEY}\\command"), KEY.to_owned()] {
                let key = super::wide(key);
                let result = unsafe { RegDeleteKeyW(HKEY_CURRENT_USER, key.as_ptr()) };
                if result != 2 {
                    ok(result)?;
                }
            }
        }
        Operation::Status => (),
    }
    Ok(json!({"ok": true, "command": command()?}))
}
