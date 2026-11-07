//! macOS: after Uninstall removes what setup installed, the running app moves itself to the Trash,
//! as dragging it there would, so nothing of the app is left behind.
// Built on other Unix platforms only for its tests.
#![cfg_attr(not(target_os = "macos"), allow(dead_code))]

use std::{
    fs,
    path::{Path, PathBuf},
    process::{Command, Stdio},
};

/// WebKit and AppKit keep these per-app folders under ~/Library, outside the app's data folder.
const LIBRARY_DATA_FOLDERS: &[&str] = &["WebKit", "HTTPStorages", "Saved Application State"];

/// The per-app folders WebKit and AppKit keep under ~/Library. None for an identifier that is
/// empty or not a single path component, which would name the shared folders themselves.
pub(crate) fn library_data_dirs(identifier: &str) -> Vec<PathBuf> {
    let usable = !identifier.is_empty()
        && identifier != "."
        && identifier != ".."
        && !identifier.contains(['/', '\\']);
    let Some(library) = home_dir()
        .filter(|_| usable)
        .map(|home| home.join("Library"))
    else {
        return Vec::new();
    };
    LIBRARY_DATA_FOLDERS
        .iter()
        .map(|folder| {
            let name = if *folder == "Saved Application State" {
                format!("{identifier}.savedState")
            } else {
                identifier.to_string()
            };
            library.join(folder).join(name)
        })
        .collect()
}

/// Removes `dirs` once this process has exited, from a detached shell that outlives it.
pub(crate) fn remove_after_exit(dirs: &[PathBuf]) -> Result<(), String> {
    if dirs.is_empty() {
        return Ok(());
    }
    Command::new("/bin/sh")
        .args([
            "-c",
            r#"pid=$1; shift; while kill -0 "$pid" 2>/dev/null; do sleep 0.1; done; rm -rf -- "$@""#,
            "gui-for-cli-uninstall",
        ])
        .arg(std::process::id().to_string())
        .args(dirs)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .map(drop)
        .map_err(|error| format!("could not schedule the final cleanup: {error}"))
}

/// Moves the running `.app` into ~/.Trash and returns where it went. Fails when the app is not on
/// the home folder's volume (a mounted DMG, a translocated or external copy): the user drags it.
pub(crate) fn move_running_app_to_trash() -> Result<PathBuf, String> {
    let executable = std::env::current_exe()
        .map_err(|error| format!("could not find the running app: {error}"))?;
    let app = app_bundle(&executable)
        .ok_or_else(|| format!("{} is not inside an app bundle", executable.display()))?;
    let trash = home_dir()
        .map(|home| home.join(".Trash"))
        .ok_or("could not find the home folder")?;
    fs::create_dir_all(&trash)
        .map_err(|error| format!("could not open {}: {error}", trash.display()))?;
    let destination = free_trash_path(&trash, &app);
    fs::rename(&app, &destination).map_err(|error| {
        format!(
            "could not move {} to the Trash ({error}); drag it to the Trash instead",
            app.display()
        )
    })?;
    Ok(destination)
}

/// `.../Name.app/Contents/MacOS/binary` -> `.../Name.app`.
fn app_bundle(executable: &Path) -> Option<PathBuf> {
    let app = executable.parent()?.parent()?.parent()?;
    (app.extension()? == "app").then(|| app.to_path_buf())
}

/// `Name.app`, or `Name 2.app`, `Name 3.app`, ... when the Trash already holds one.
fn free_trash_path(trash: &Path, app: &Path) -> PathBuf {
    let stem = app
        .file_stem()
        .map(|stem| stem.to_string_lossy().into_owned())
        .unwrap_or_default();
    let mut candidate = trash.join(format!("{stem}.app"));
    let mut copy = 2;
    while candidate.exists() {
        candidate = trash.join(format!("{stem} {copy}.app"));
        copy += 1;
    }
    candidate
}

fn home_dir() -> Option<PathBuf> {
    std::env::var_os("HOME")
        .filter(|home| !home.is_empty())
        .map(PathBuf::from)
}

#[cfg(test)]
mod tests {
    use super::{app_bundle, free_trash_path, library_data_dirs, remove_after_exit};
    use std::path::Path;

    #[test]
    fn finds_the_app_bundle_of_the_executable() {
        assert_eq!(
            app_bundle(Path::new(
                "/Applications/WGSExtract.app/Contents/MacOS/wgsextract"
            )),
            Some(Path::new("/Applications/WGSExtract.app").to_path_buf())
        );
        assert_eq!(app_bundle(Path::new("/usr/local/bin/tool/x/y")), None);
    }

    #[test]
    fn lists_webkit_and_saved_state_folders() {
        let dirs = library_data_dirs("dev.example.app");
        assert!(dirs
            .iter()
            .any(|dir| dir.ends_with("Library/WebKit/dev.example.app")));
        assert!(dirs
            .iter()
            .any(|dir| dir.ends_with("Saved Application State/dev.example.app.savedState")));
    }

    #[test]
    fn rejects_identifiers_that_name_shared_folders() {
        for identifier in ["", ".", "..", "a/b", "a\\b"] {
            assert!(library_data_dirs(identifier).is_empty(), "{identifier:?}");
        }
    }

    #[test]
    fn schedules_removal_for_after_exit() {
        let dir = std::env::temp_dir().join(format!("gfc-after-exit-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        remove_after_exit(std::slice::from_ref(&dir)).unwrap();
        std::thread::sleep(std::time::Duration::from_millis(300));
        assert!(dir.exists(), "removed while this process is still running");
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn picks_a_free_name_in_the_trash() {
        let trash = std::env::temp_dir().join(format!("gfc-trash-test-{}", std::process::id()));
        std::fs::create_dir_all(trash.join("Tool.app")).unwrap();
        let app = Path::new("/Applications/Tool.app");
        assert_eq!(free_trash_path(&trash, app), trash.join("Tool 2.app"));
        std::fs::remove_dir_all(&trash).unwrap();
    }
}
