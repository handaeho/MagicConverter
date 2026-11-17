use crate::Common;
use rusqlite::{Connection, OpenFlags};
use std::env;
use std::fs::{self, File};
use std::io::{self, ErrorKind};
use std::path::{Path, PathBuf};
use std::time::Duration;

pub(crate) fn directory(common: &Common) -> io::Result<PathBuf> {
    if let Some(directory) = common
        .data_dir
        .clone()
        .or_else(|| env::var_os("MAGIC_CONVERTER_PROFILE_DIR").map(PathBuf::from))
    {
        return Ok(directory);
    }
    #[cfg(target_os = "macos")]
    {
        let home = env::var_os("HOME").ok_or_else(|| {
            io::Error::new(ErrorKind::NotFound, "macOS 홈 폴더를 찾을 수 없습니다.")
        })?;
        Ok(PathBuf::from(home).join("Library/Application Support/MagicConverter/3.0/profile"))
    }
    #[cfg(not(target_os = "macos"))]
    {
        let Some(local) = env::var_os("LOCALAPPDATA").map(PathBuf::from) else {
            return Ok(PathBuf::from("magic-converter-profile"));
        };
        let legacy = local.join("magic_converter").join("magic_converter");
        let current = local.join("MagicConverter").join("3.0").join("profile");
        migrate(&legacy, &current)?;
        Ok(current)
    }
}

fn regular_file(path: &Path) -> io::Result<bool> {
    match fs::symlink_metadata(path) {
        Ok(metadata) => {
            if !metadata.is_file() {
                return Err(io::Error::new(
                    ErrorKind::InvalidData,
                    "설정 데이터가 일반 파일이 아닙니다.",
                ));
            }
            #[cfg(windows)]
            {
                use std::os::windows::fs::MetadataExt;
                if metadata.file_attributes() & 0x400 != 0 {
                    return Err(io::Error::new(
                        ErrorKind::InvalidData,
                        "설정 데이터에 재분석 지점이 있습니다.",
                    ));
                }
            }
            Ok(true)
        }
        Err(error) if error.kind() == ErrorKind::NotFound => Ok(false),
        Err(error) => Err(error),
    }
}

fn snapshot(source: &Path, target: &Path, watch: bool) -> io::Result<()> {
    let path = target.to_str().ok_or_else(|| {
        io::Error::new(
            ErrorKind::InvalidInput,
            "설정 데이터 경로를 읽을 수 없습니다.",
        )
    })?;
    let copy = || -> rusqlite::Result<()> {
        let database = Connection::open_with_flags(source, OpenFlags::SQLITE_OPEN_READ_ONLY)?;
        database.busy_timeout(Duration::from_secs(5))?;
        database.execute("VACUUM INTO ?1", [path])?;
        if watch {
            // The old staging paths belong to the 2.x profile. Pending inputs
            // are discovered again by the first scan in 3.0.
            let journal = Connection::open(target)?;
            journal.execute(
                "DELETE FROM events WHERE state IN ('queued', 'publishing')",
                [],
            )?;
        }
        Ok(())
    };
    copy().map_err(io::Error::other)
}

fn migrate(legacy: &Path, current: &Path) -> io::Result<()> {
    match fs::metadata(current) {
        Ok(metadata) if metadata.is_dir() => return Ok(()),
        Ok(_) => {
            return Err(io::Error::new(
                ErrorKind::AlreadyExists,
                "3.0 설정 경로가 폴더가 아닙니다.",
            ));
        }
        Err(error) if error.kind() == ErrorKind::NotFound => {}
        Err(error) => return Err(error),
    }
    let parent = current.parent().expect("profile has a parent");
    fs::create_dir_all(parent)?;
    let staged = tempfile::Builder::new()
        .prefix("profile-import-")
        .tempdir_in(parent)?;
    for name in [
        "settings.json",
        "settings.json.bak",
        "history.json",
        "history.json.bak",
        "automation.json",
        "automation.json.bak",
    ] {
        let source = legacy.join(name);
        if regular_file(&source)? {
            let mut target = File::create(staged.path().join(name))?;
            io::copy(&mut File::open(source)?, &mut target)?;
            target.sync_all()?;
        }
    }
    for (name, watch) in [
        ("watch-journal.sqlite3", true),
        ("ipc-receipts.sqlite3", false),
    ] {
        let source = legacy.join(name);
        if regular_file(&source)? {
            snapshot(&source, &staged.path().join(name), watch)?;
        }
    }
    match fs::rename(staged.path(), current) {
        Ok(()) => Ok(()),
        Err(_) if current.is_dir() => Ok(()),
        Err(error) => Err(error),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn upgrades_existing_profile_without_reusing_old_paths() -> io::Result<()> {
        let temp = tempfile::tempdir()?;
        let old = temp.path().join("old");
        let new = temp.path().join("new").join("profile");
        fs::create_dir(&old)?;
        fs::write(old.join("settings.json"), "settings")?;
        fs::write(old.join("history.json"), "history")?;
        fs::write(old.join("automation.json"), "automation")?;
        fs::write(old.join("magic_converter.lock"), "old runtime lock")?;
        let journal =
            Connection::open(old.join("watch-journal.sqlite3")).map_err(io::Error::other)?;
        journal
            .execute_batch(
                "PRAGMA journal_mode=WAL;
             CREATE TABLE events (state TEXT);
             INSERT INTO events VALUES ('done'), ('queued'), ('publishing');",
            )
            .map_err(io::Error::other)?;
        migrate(&old, &new)?;
        assert_eq!(fs::read_to_string(new.join("settings.json"))?, "settings");
        assert_eq!(fs::read_to_string(new.join("history.json"))?, "history");
        assert_eq!(
            fs::read_to_string(new.join("automation.json"))?,
            "automation"
        );
        assert!(!new.join("magic_converter.lock").exists());
        let imported =
            Connection::open(new.join("watch-journal.sqlite3")).map_err(io::Error::other)?;
        let states = |database: &Connection| {
            database.query_row("SELECT group_concat(state) FROM events", [], |row| {
                row.get::<_, String>(0)
            })
        };
        assert_eq!(states(&imported).map_err(io::Error::other)?, "done");
        assert_eq!(
            states(&journal).map_err(io::Error::other)?,
            "done,queued,publishing"
        );
        fs::write(new.join("settings.json"), "edited")?;
        migrate(&old, &new)?;
        assert_eq!(fs::read_to_string(new.join("settings.json"))?, "edited");
        Ok(())
    }
}
