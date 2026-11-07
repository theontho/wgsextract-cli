fn main() {
    tauri_build::try_build(tauri_build::Attributes::new().app_manifest(
        tauri_build::AppManifest::new().commands(&[
            "gfc_report_rendered",
            "gfc_update_check",
            "gfc_update_download",
            "gfc_update_install",
        ]),
    ))
    .expect("failed to generate Tauri app manifest permissions for IPC commands")
}
