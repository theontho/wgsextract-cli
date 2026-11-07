//! The app's own Uninstall, on macOS and Linux. Dragging the app to the Trash or removing its
//! package never removes what the app's setup put in the user's home (the bundle's runtime, its
//! Pixi environments, a reference library), so the app removes those itself: the bundle's
//! uninstall steps (bundle-uninstall-cli.js, the Windows uninstaller's too), then the app's data.
//! On macOS the app then moves itself to the Trash; on Linux the package manager removes the
//! package. Windows does all of this in its NSIS uninstaller.

use std::{fs, path::Path, process::Command, thread};

use tauri::{AppHandle, Manager, Runtime};
use tauri_plugin_dialog::{DialogExt, MessageDialogButtons, MessageDialogKind};

use crate::{backend::BackendProcess, child_process_path, menu::app_name, AppPaths};

pub(crate) const UNINSTALL_MENU_ID: &str = "uninstall-app";
/// Runs the uninstall at launch without asking, then quits (for automated lifecycle tests).
pub(crate) const AUTO_RUN_UNINSTALL_ENV: &str = "GUI_FOR_CLI_AUTO_RUN_UNINSTALL";

pub(crate) fn request_uninstall<R: Runtime>(app: &AppHandle<R>) {
    let app = app.clone();
    thread::spawn(move || {
        let name = app_name(&app);
        let confirmed = app
            .dialog()
            .message(format!(
                "This runs {name}'s uninstall steps and deletes everything {name} keeps in your \
                 home folder, including downloaded reference genomes and the settings in its \
                 workspace. Files you saved elsewhere are kept.\n\n{}",
                app_removal_note(&name)
            ))
            .title(format!("Uninstall {name}"))
            .kind(MessageDialogKind::Warning)
            .buttons(MessageDialogButtons::OkCancelCustom(
                "Uninstall".into(),
                "Cancel".into(),
            ))
            .blocking_show();
        if confirmed {
            run_uninstall(&app, true);
        }
    });
}

pub(crate) fn auto_run_uninstall<R: Runtime>(app: &AppHandle<R>) {
    let app = app.clone();
    thread::spawn(move || run_uninstall(&app, false));
}

fn run_uninstall<R: Runtime>(app: &AppHandle<R>, interactive: bool) {
    // Nothing of the bundle may run while its runtime is removed.
    app.state::<BackendProcess>().terminate();
    let outcome = remove_setup_and_data(app);
    if interactive {
        let name = app_name(app);
        let (kind, message) = match &outcome {
            Ok(()) => (
                MessageDialogKind::Info,
                format!(
                    "{name}'s data and what its setup installed were removed. {}",
                    app_removed_note(&name)
                ),
            ),
            Err(error) => (
                MessageDialogKind::Error,
                format!("{name} could not be fully uninstalled: {error}."),
            ),
        };
        app.dialog()
            .message(message)
            .title(format!("Uninstall {name}"))
            .kind(kind)
            .buttons(MessageDialogButtons::Ok)
            .blocking_show();
    }
    if let Err(error) = &outcome {
        eprintln!("Uninstall failed: {error}");
    }
    app.exit(if outcome.is_ok() { 0 } else { 1 });
}

fn remove_setup_and_data<R: Runtime>(app: &AppHandle<R>) -> Result<(), String> {
    let paths = app.state::<AppPaths>();
    let script = paths
        .repo_root
        .join("app/dist/web/src/server/bundle-uninstall-cli.js");
    let status = Command::new(&paths.node_path)
        .arg(child_process_path(&script))
        .args([
            "--app-support-name",
            &paths.app_support_name,
            "--remove-app-data",
        ])
        .arg(child_process_path(&paths.bundle_root))
        .status()
        .map_err(|error| format!("could not run the uninstall steps: {error}"))?;
    if !status.success() {
        return Err(format!("the uninstall steps failed ({status})"));
    }
    // WebKit's caches and the backend log, outside the data folder the steps removed.
    for dir in [app.path().app_cache_dir(), app.path().app_log_dir()]
        .into_iter()
        .flatten()
    {
        remove_dir_if_present(&dir)?;
    }
    remove_app(app)
}

pub(crate) fn remove_dir_if_present(path: &Path) -> Result<(), String> {
    match fs::remove_dir_all(path) {
        Ok(()) => Ok(()),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(error) => Err(format!("could not remove {}: {error}", path.display())),
    }
}

#[cfg(target_os = "macos")]
fn remove_app<R: Runtime>(app: &AppHandle<R>) -> Result<(), String> {
    let library_data = crate::app_trash::library_data_dirs(&app.config().identifier);
    for dir in &library_data {
        remove_dir_if_present(dir)?;
    }
    // The open webview can write its storage again before the app exits; sweep it once more after.
    crate::app_trash::remove_after_exit(&library_data)?;
    let trashed = crate::app_trash::move_running_app_to_trash()?;
    eprintln!("[uninstall] Moved the app to {}", trashed.display());
    Ok(())
}

/// The package manager removes a Linux app's files.
#[cfg(not(target_os = "macos"))]
fn remove_app<R: Runtime>(_app: &AppHandle<R>) -> Result<(), String> {
    Ok(())
}

#[cfg(target_os = "macos")]
fn app_removal_note(name: &str) -> String {
    format!("{name} is then moved to the Trash.")
}

#[cfg(not(target_os = "macos"))]
fn app_removal_note(name: &str) -> String {
    format!(
        "Afterwards, remove the {name} package with your package manager (or delete its AppImage)."
    )
}

#[cfg(target_os = "macos")]
fn app_removed_note(name: &str) -> String {
    format!("{name} was moved to the Trash.")
}

#[cfg(not(target_os = "macos"))]
fn app_removed_note(name: &str) -> String {
    format!(
        "To finish, remove the {name} package with your package manager (or delete its AppImage)."
    )
}
