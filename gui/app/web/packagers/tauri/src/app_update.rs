use std::{
    env, fs,
    io::{self, Write},
    path::PathBuf,
    process, thread,
    time::{Duration, SystemTime, UNIX_EPOCH},
};

use serde::Serialize;
use tauri::{Emitter, Manager, Resource, ResourceId, Webview};
use tauri_plugin_dialog::{DialogExt, MessageDialogButtons, MessageDialogKind};
use tauri_plugin_updater::UpdaterExt;

use crate::{update_e2e_overlay, BackendProcess};

pub(crate) const AUTO_UPDATE_ENV: &str = "GFC_TAURI_AUTO_UPDATE";
pub(crate) const AUTO_ACCEPT_UPDATE_ENV: &str = "GFC_TAURI_AUTO_ACCEPT_UPDATE";
pub(crate) const AUTO_UPDATE_DELAY_SECONDS_ENV: &str = "GFC_TAURI_AUTO_UPDATE_DELAY_SECONDS";
pub(crate) const AUTO_UPDATE_ACTION_DELAY_SECONDS_ENV: &str =
    "GFC_TAURI_AUTO_UPDATE_ACTION_DELAY_SECONDS";
pub(crate) const E2E_STEP_DELAY_SECONDS_ENV: &str = "GFC_TAURI_E2E_STEP_DELAY_SECONDS";
const UPDATE_STATUS_FILE_ENV: &str = "GFC_TAURI_UPDATE_STATUS_FILE";

struct DownloadedUpdate {
    path: PathBuf,
}

impl Resource for DownloadedUpdate {}

impl Drop for DownloadedUpdate {
    fn drop(&mut self) {
        match fs::remove_file(&self.path) {
            Ok(()) => {}
            Err(error) if error.kind() == io::ErrorKind::NotFound => {}
            Err(error) => eprintln!(
                "Could not remove cached update file {}: {error}",
                self.path.display()
            ),
        }
    }
}

#[derive(Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub(crate) struct UpdateCheckResponse {
    current_version: String,
    available_version: Option<String>,
    update_rid: Option<ResourceId>,
    body: Option<String>,
}

#[derive(Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub(crate) struct UpdateDownloadResponse {
    bytes_rid: ResourceId,
}

#[derive(Clone, Serialize)]
#[serde(rename_all = "camelCase")]
struct UpdateProgressEvent {
    event: String,
    version: String,
    downloaded_bytes: u64,
    content_length: Option<u64>,
    percent: Option<f64>,
}

#[tauri::command]
pub(crate) async fn gfc_update_check<R: tauri::Runtime>(
    webview: Webview<R>,
    prior_update_rid: Option<ResourceId>,
    prior_bytes_rid: Option<ResourceId>,
) -> Result<UpdateCheckResponse, String> {
    let app = webview.app_handle().clone();
    let current_version = app.package_info().version.to_string();
    write_update_status("checking");
    update_e2e_overlay::update(
        &app,
        "Checking for updates",
        &format!("Installed version: {current_version}"),
    );

    let updater = webview
        .updater_builder()
        .on_before_exit(update_before_exit_hook(&app))
        .build()
        .map_err(|error| {
            write_update_status(&format!("not-configured:{error}"));
            format!("This build is not configured for updates: {error}")
        })?;
    let update = updater.check().await.map_err(|error| {
        write_update_status(&format!("check-failed:{error}"));
        format!("Could not check for updates: {error}")
    })?;

    close_prior_update_resources(&webview, prior_update_rid, prior_bytes_rid);

    let Some(update) = update else {
        write_update_status("none");
        update_e2e_overlay::update(
            &app,
            "No update available",
            &format!("Installed version: {current_version}"),
        );
        return Ok(UpdateCheckResponse {
            current_version,
            available_version: None,
            update_rid: None,
            body: None,
        });
    };

    write_update_status(&format!("available:{}", update.version));
    update_e2e_overlay::update(
        &app,
        "Update available",
        &format!(
            "Installed {current_version} -> available {}",
            update.version
        ),
    );
    let available_version = update.version.clone();
    let body = update.body.clone();
    let update_rid = webview.resources_table().add(update);
    Ok(UpdateCheckResponse {
        current_version,
        available_version: Some(available_version),
        update_rid: Some(update_rid),
        body,
    })
}

fn close_prior_update_resources<R: tauri::Runtime>(
    webview: &Webview<R>,
    prior_update_rid: Option<ResourceId>,
    prior_bytes_rid: Option<ResourceId>,
) {
    // Close any previously returned resources after the replacement check
    // succeeds so a failed check does not invalidate a retryable install.
    if let Some(rid) = prior_update_rid {
        let _ = webview.resources_table().close(rid);
    }
    if let Some(rid) = prior_bytes_rid {
        let _ = webview.resources_table().close(rid);
    }
}

#[tauri::command]
pub(crate) async fn gfc_update_download<R: tauri::Runtime>(
    webview: Webview<R>,
    update_rid: ResourceId,
    accepted_by: Option<String>,
) -> Result<UpdateDownloadResponse, String> {
    let app = webview.app_handle().clone();
    let update = webview
        .resources_table()
        .get::<tauri_plugin_updater::Update>(update_rid)
        .map_err(|error| format!("Could not find the pending update: {error}"))?;
    let update = (*update).clone();
    let version = update.version.clone();
    let accepted_by = accepted_by.unwrap_or_else(|| "user".to_string());
    write_update_status(&format!("accepted:{accepted_by}"));
    write_update_status("downloading");
    update_e2e_overlay::pause_for_review();
    update_e2e_overlay::update(
        &app,
        "Downloading update",
        &format!("Downloading version {version}"),
    );

    let progress_app = app.clone();
    let progress_version = version.clone();
    let mut visible_progress_steps = 0_u8;
    let mut downloaded_bytes = 0_u64;
    let bytes = update
        .download(
            move |chunk_length, content_length| {
                downloaded_bytes = downloaded_bytes.saturating_add(chunk_length as u64);
                let percent = content_length.filter(|length| *length > 0).map(|length| {
                    ((downloaded_bytes as f64 / length as f64) * 100.0).clamp(0.0, 100.0)
                });
                emit_update_progress(
                    &progress_app,
                    UpdateProgressEvent {
                        event: "progress".into(),
                        version: progress_version.clone(),
                        downloaded_bytes,
                        content_length,
                        percent,
                    },
                );
                update_e2e_overlay::update_with_progress(
                    &progress_app,
                    "Downloading update",
                    &format!("Downloading version {progress_version}"),
                    percent,
                );
                if update_e2e_overlay::enabled() && visible_progress_steps < 12 {
                    visible_progress_steps += 1;
                    thread::sleep(Duration::from_millis(500));
                }
            },
            {
                let app = app.clone();
                let version = version.clone();
                move || {
                    write_update_status("downloaded");
                    emit_update_progress(
                        &app,
                        UpdateProgressEvent {
                            event: "finished".into(),
                            version: version.clone(),
                            downloaded_bytes: 0,
                            content_length: None,
                            percent: Some(100.0),
                        },
                    );
                    update_e2e_overlay::update(
                        &app,
                        "Download complete",
                        &format!("Ready to install version {version}"),
                    );
                    update_e2e_overlay::pause_for_review();
                }
            },
        )
        .await
        .map_err(|error| {
            write_update_status(&format!("download-failed:{error}"));
            format!("Could not download the update: {error}")
        })?;

    let downloaded_update = write_downloaded_update(&bytes).map_err(|error| {
        write_update_status(&format!("download-failed:{error}"));
        error
    })?;

    Ok(UpdateDownloadResponse {
        bytes_rid: webview.resources_table().add(downloaded_update),
    })
}

#[tauri::command]
pub(crate) async fn gfc_update_install<R: tauri::Runtime>(
    webview: Webview<R>,
    update_rid: ResourceId,
    bytes_rid: ResourceId,
) -> Result<(), String> {
    let app = webview.app_handle().clone();
    let update = webview
        .resources_table()
        .get::<tauri_plugin_updater::Update>(update_rid)
        .map_err(|error| format!("Could not find the pending update: {error}"))?;
    let downloaded_update = webview
        .resources_table()
        .get::<DownloadedUpdate>(bytes_rid)
        .map_err(|error| format!("Could not find the downloaded update: {error}"))?;
    let downloaded_update_path = downloaded_update.path.clone();
    drop(downloaded_update);
    let bytes = fs::read(&downloaded_update_path).map_err(|error| {
        let message = format!("Could not read the downloaded update: {error}");
        write_update_status(&format!("install-failed:{message}"));
        message
    })?;
    write_update_status("installing");
    update_e2e_overlay::update(
        &app,
        "Starting installer",
        &format!("Installing version {}", update.version),
    );
    update_e2e_overlay::pause_for_review();
    update.install(&bytes).map_err(|error| {
        write_update_status(&format!("install-failed:{error}"));
        format!("Could not install the update: {error}")
    })?;
    let _ = webview.resources_table().close(update_rid);
    let _ = webview.resources_table().close(bytes_rid);
    write_update_status("installed:requesting-restart");
    app.request_restart();
    Ok(())
}

pub(crate) fn check_for_updates<R: tauri::Runtime>(app: tauri::AppHandle<R>) {
    tauri::async_runtime::spawn(async move {
        let current_version = app.package_info().version.to_string();
        write_update_status("checking");
        update_e2e_overlay::update(
            &app,
            "Checking for updates",
            &format!("Installed version: {current_version}"),
        );
        let updater = match app
            .updater_builder()
            .on_before_exit(update_before_exit_hook(&app))
            .build()
        {
            Ok(updater) => updater,
            Err(error) => {
                write_update_status(&format!("not-configured:{error}"));
                show_update_message(
                    &app,
                    "Updates Not Configured",
                    &format!("This build is not configured for updates: {error}"),
                    MessageDialogKind::Warning,
                );
                return;
            }
        };
        let update = match updater.check().await {
            Ok(update) => update,
            Err(error) => {
                write_update_status(&format!("check-failed:{error}"));
                show_update_message(
                    &app,
                    "Update Check Failed",
                    &format!("Could not check for updates: {error}"),
                    MessageDialogKind::Error,
                );
                return;
            }
        };

        let Some(update) = update else {
            write_update_status("none");
            update_e2e_overlay::update(
                &app,
                "No update available",
                &format!("Installed version: {current_version}"),
            );
            show_update_message(
                &app,
                "No Updates Available",
                "You are already running the latest version.",
                MessageDialogKind::Info,
            );
            return;
        };

        write_update_status(&format!("available:{}", update.version));
        update_e2e_overlay::update(
            &app,
            "Update available",
            &format!(
                "Installed {current_version} -> available {}",
                update.version
            ),
        );
        let prompt = format!(
            "You are running version {current_version}.\n\nVersion {} is available.\n\nDownload, install, and restart now?",
            update.version
        );
        let accepted = if env_flag(AUTO_ACCEPT_UPDATE_ENV) {
            write_update_status("accepted:auto");
            update_e2e_overlay::update(
                &app,
                "Update accepted",
                &format!("Downloading version {}", update.version),
            );
            true
        } else {
            let prompt_app = app.clone();
            match tauri::async_runtime::spawn_blocking(move || {
                prompt_app
                    .dialog()
                    .message(prompt)
                    .title("Update Available")
                    .kind(MessageDialogKind::Info)
                    .buttons(MessageDialogButtons::OkCancelCustom(
                        "Install and Restart".into(),
                        "Not Now".into(),
                    ))
                    .blocking_show()
            })
            .await
            {
                Ok(accepted) => {
                    write_update_status(if accepted {
                        "accepted:user"
                    } else {
                        "declined:user"
                    });
                    let overlay_detail = if accepted {
                        format!("Downloading version {}", update.version)
                    } else {
                        format!("Installed version: {current_version}")
                    };
                    update_e2e_overlay::update(
                        &app,
                        if accepted {
                            "Update accepted"
                        } else {
                            "Update declined"
                        },
                        &overlay_detail,
                    );
                    accepted
                }
                Err(error) => {
                    write_update_status(&format!("prompt-failed:{error}"));
                    show_update_message(
                        &app,
                        "Update Check Failed",
                        &format!("Could not show the update prompt: {error}"),
                        MessageDialogKind::Error,
                    );
                    return;
                }
            }
        };
        if !accepted {
            return;
        }

        update_e2e_overlay::pause_for_review();
        write_update_status("downloading");
        update_e2e_overlay::update(
            &app,
            "Downloading update",
            &format!("Downloading version {}", update.version),
        );
        let progress_app = app.clone();
        let progress_version = update.version.clone();
        let download_complete_app = app.clone();
        let download_complete_version = update.version.clone();
        let mut visible_progress_steps = 0_u8;
        let mut downloaded_bytes = 0_usize;
        match update
            .download_and_install(
                move |chunk_length, content_length| {
                    downloaded_bytes = downloaded_bytes.saturating_add(chunk_length);
                    let total = content_length
                        .map(|length| format!(" of {}", human_bytes(length)))
                        .unwrap_or_default();
                    let progress_percent = content_length
                        .filter(|length| *length > 0)
                        .map(|length| {
                            ((downloaded_bytes as f64 / length as f64) * 100.0).clamp(0.0, 100.0)
                        });
                    update_e2e_overlay::update_with_progress(
                        &progress_app,
                        "Downloading update",
                        &format!(
                            "Downloading version {progress_version}: {}{total}",
                            human_bytes(downloaded_bytes as u64)
                        ),
                        progress_percent,
                    );
                    if update_e2e_overlay::enabled() && visible_progress_steps < 12 {
                        visible_progress_steps += 1;
                        thread::sleep(Duration::from_millis(500));
                    }
                },
                move || {
                    write_update_status("downloaded");
                    update_e2e_overlay::update(
                        &download_complete_app,
                        "Download complete",
                        &format!(
                            "Starting installer for version {download_complete_version}; the old app will quit."
                        ),
                    );
                    update_e2e_overlay::pause_for_review();
                },
            )
            .await
        {
            Ok(()) => {
                write_update_status("installed:requesting-restart");
                app.request_restart();
            }
            Err(error) => {
                write_update_status(&format!("install-failed:{error}"));
                show_update_message(
                    &app,
                    "Update Failed",
                    &format!("Could not install the update: {error}"),
                    MessageDialogKind::Error,
                );
            }
        }
    });
}

fn update_before_exit_hook<R: tauri::Runtime>(
    app: &tauri::AppHandle<R>,
) -> impl Fn() + Send + Sync + 'static {
    let app = app.clone();
    move || {
        write_update_status("installer-launched:exiting");
        update_e2e_overlay::update(
            &app,
            "Installer launched",
            "The old app is quitting so Windows can replace it.",
        );
        app.state::<BackendProcess>().terminate();
        app.cleanup_before_exit();
    }
}

fn emit_update_progress<R: tauri::Runtime>(app: &tauri::AppHandle<R>, event: UpdateProgressEvent) {
    if let Err(error) = app.emit("gfc-update-progress", event) {
        eprintln!("Could not emit update progress event: {error}");
    }
}

fn write_downloaded_update(bytes: &[u8]) -> Result<DownloadedUpdate, String> {
    let directory = env::temp_dir().join("gui-for-cli-updates");
    fs::create_dir_all(&directory)
        .map_err(|error| format!("Could not create update download cache: {error}"))?;
    let timestamp = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_nanos())
        .unwrap_or(0);

    for attempt in 0..100 {
        let path = directory.join(format!(
            "update-{}-{timestamp}-{attempt}.bin",
            process::id()
        ));
        match fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&path)
        {
            Ok(mut file) => {
                if let Err(error) = file.write_all(bytes) {
                    let _ = fs::remove_file(&path);
                    return Err(format!("Could not write downloaded update: {error}"));
                }
                return Ok(DownloadedUpdate { path });
            }
            Err(error) if error.kind() == io::ErrorKind::AlreadyExists => {}
            Err(error) => return Err(format!("Could not create downloaded update file: {error}")),
        }
    }

    Err("Could not create a unique downloaded update file.".into())
}

pub(crate) fn env_flag(name: &str) -> bool {
    matches!(
        env::var(name)
            .unwrap_or_default()
            .trim()
            .to_ascii_lowercase()
            .as_str(),
        "1" | "true" | "yes" | "on"
    )
}

pub(crate) fn env_duration_seconds(name: &str) -> Duration {
    env::var(name)
        .ok()
        .and_then(|value| value.trim().parse::<u64>().ok())
        .map(Duration::from_secs)
        .unwrap_or(Duration::ZERO)
}

fn write_update_status(event: &str) {
    let Some(path) = env::var_os(UPDATE_STATUS_FILE_ENV).map(PathBuf::from) else {
        return;
    };
    if let Some(parent) = path.parent() {
        if let Err(error) = fs::create_dir_all(parent) {
            eprintln!("Could not create update status directory: {error}");
            return;
        }
    }
    match fs::OpenOptions::new().create(true).append(true).open(&path) {
        Ok(mut file) => {
            if let Err(error) = writeln!(file, "{event}") {
                eprintln!(
                    "Could not write update status to {}: {error}",
                    path.display()
                );
            }
        }
        Err(error) => eprintln!(
            "Could not open update status file {}: {error}",
            path.display()
        ),
    }
}

fn show_update_message<R: tauri::Runtime>(
    app: &tauri::AppHandle<R>,
    title: &str,
    message: &str,
    kind: MessageDialogKind,
) {
    app.dialog()
        .message(message)
        .title(title)
        .kind(kind)
        .buttons(MessageDialogButtons::Ok)
        .show(|_| {});
}

fn human_bytes(bytes: u64) -> String {
    const MIB: u64 = 1024 * 1024;
    const KIB: u64 = 1024;
    if bytes >= MIB {
        format!("{:.1} MiB", bytes as f64 / MIB as f64)
    } else if bytes >= KIB {
        format!("{:.1} KiB", bytes as f64 / KIB as f64)
    } else {
        format!("{bytes} B")
    }
}
