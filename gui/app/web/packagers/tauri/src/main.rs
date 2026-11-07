#![cfg_attr(
    all(not(debug_assertions), not(feature = "bench-console")),
    windows_subsystem = "windows"
)]

use std::{
    env, fs,
    net::TcpListener,
    path::{Path, PathBuf},
    sync::atomic::{AtomicBool, AtomicI32, Ordering},
    time::Instant,
};

use app_update::{
    check_for_updates, env_duration_seconds, env_flag, gfc_update_check, gfc_update_download,
    gfc_update_install, AUTO_ACCEPT_UPDATE_ENV, AUTO_UPDATE_ACTION_DELAY_SECONDS_ENV,
    AUTO_UPDATE_DELAY_SECONDS_ENV, AUTO_UPDATE_ENV, E2E_STEP_DELAY_SECONDS_ENV,
};
use backend::{BackendLaunch, BackendProcess};
use tauri::{ipc::CapabilityBuilder, Manager, WebviewUrl, WebviewWindowBuilder};

#[cfg(any(target_os = "macos", all(test, unix)))]
mod app_trash;
mod app_uninstall;
mod app_update;
mod backend;
mod menu;
mod native_picker;
mod update_e2e_overlay;

use menu::{
    app_menu, app_name, request_about, request_load_bundle, ABOUT_MENU_ID,
    CHECK_FOR_UPDATES_MENU_ID, LOAD_BUNDLE_MENU_ID,
};
use native_picker::start_native_picker_listener;

static NODE_PID: AtomicI32 = AtomicI32::new(0);
const PORT_FILE_ENV: &str = "GFC_PORT_FILE";

struct StartupClock {
    started_at: Instant,
    rendered: AtomicBool,
}

/// Reported once by the page's initialization script when the app has rendered (startup benchmarks).
#[tauri::command]
fn gfc_report_rendered(clock: tauri::State<'_, StartupClock>, page_ms: f64) {
    if clock.rendered.swap(true, Ordering::SeqCst) {
        return;
    }
    println!("metric webAppRenderedInPage_ms={page_ms:.1}");
    print_metric(clock.started_at, "webAppRendered");
}

fn main() {
    #[cfg(unix)]
    unsafe {
        libc::signal(
            libc::SIGTERM,
            handle_signal as *const () as libc::sighandler_t,
        );
        libc::signal(
            libc::SIGINT,
            handle_signal as *const () as libc::sighandler_t,
        );
    }

    let started_at = Instant::now();

    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_updater::Builder::new().build())
        .invoke_handler(tauri::generate_handler![
            gfc_report_rendered,
            gfc_update_check,
            gfc_update_download,
            gfc_update_install
        ])
        .menu(app_menu)
        .on_menu_event(|app, event| {
            match event.id().as_ref() {
                ABOUT_MENU_ID => request_about(app),
                CHECK_FOR_UPDATES_MENU_ID => check_for_updates(app.clone()),
                LOAD_BUNDLE_MENU_ID => request_load_bundle(app),
                app_uninstall::UNINSTALL_MENU_ID => app_uninstall::request_uninstall(app),
                _ => {}
            }
        })
        .manage(BackendProcess::new())
        .manage(StartupClock {
            started_at,
            rendered: AtomicBool::new(false),
        })
        .setup(move |app| {
            print_metric(started_at, "appSetupStarted");
            let paths = AppPaths::resolve(app)?;
            app.manage(paths.clone());
            let application_name = app_name(app);
            let application_version = app_version(app);
            let api_token = random_token(32)?;
            let picker_listener = TcpListener::bind(("127.0.0.1", 0))?;
            let picker_port = picker_listener.local_addr()?.port();
            start_native_picker_listener(picker_listener, app.handle().clone(), api_token.clone());
            let (node_pid, port) = app.state::<BackendProcess>().launch(BackendLaunch {
                paths: &paths,
                picker_port,
                application_name: &application_name,
                application_version: &application_version,
                api_token: &api_token,
            })?;
            NODE_PID.store(node_pid as i32, Ordering::SeqCst);
            println!("node_pid={node_pid}");
            print_metric(started_at, "nodeProcessStarted");
            print_metric(started_at, "serverRootReady");
            write_test_harness_files(port, &api_token)?;

            let origin = format!("http://127.0.0.1:{port}");
            // IPC is granted to the exact backend origin only, and only what the page uses.
            app.add_capability(
                CapabilityBuilder::new("backend-webui")
                    .remote(origin.clone())
                    .window("main")
                    .permission("core:event:allow-listen")
                    .permission("core:event:allow-unlisten")
                    .permission("allow-gfc-report-rendered")
                    .permission("allow-gfc-update-check")
                    .permission("allow-gfc-update-download")
                    .permission("allow-gfc-update-install"),
            )?;
            let overlay_script = update_e2e_overlay::script(&application_name, &application_version);
            let auto_update = env_flag(AUTO_UPDATE_ENV);
            let auto_accept_update = env_flag(AUTO_ACCEPT_UPDATE_ENV);
            let auto_update_delay_seconds =
                env_duration_seconds(AUTO_UPDATE_DELAY_SECONDS_ENV).as_secs();
            let auto_update_action_delay_seconds =
                if env::var_os(AUTO_UPDATE_ACTION_DELAY_SECONDS_ENV).is_some() {
                    env_duration_seconds(AUTO_UPDATE_ACTION_DELAY_SECONDS_ENV)
                } else {
                    env_duration_seconds(E2E_STEP_DELAY_SECONDS_ENV)
                }
                .as_secs();
            let init_script = format!(
                r##"
                (() => {{
                  if (location.origin !== {origin:?}) {{
                    return;
                  }}
                  window.__GUI_FOR_CLI_TAURI__ = true;
                  Object.defineProperty(window, "__GFC_API_TOKEN__", {{ value: {api_token:?}, writable: false, configurable: false }});
                  window.GUI_FOR_CLI_APPLICATION_NAME = {application_name:?};
                  window.GUI_FOR_CLI_APPLICATION_VERSION = {application_version:?};
                  window.GUI_FOR_CLI_AUTO_UPDATE = {auto_update:?};
                  window.GUI_FOR_CLI_AUTO_ACCEPT_UPDATE = {auto_accept_update:?};
                  window.GUI_FOR_CLI_AUTO_UPDATE_DELAY_SECONDS = {auto_update_delay_seconds};
                  window.GUI_FOR_CLI_AUTO_UPDATE_ACTION_DELAY_SECONDS = {auto_update_action_delay_seconds};
                  const started = performance.now();
                  let reported = false;
                  const notify = () => {{
                    if (reported) {{
                      return;
                    }}
                    const app = document.querySelector("#app");
                    if (app && app.dataset.state === "ready" && document.title) {{
                      reported = true;
                      observer.disconnect();
                      window.__TAURI_INTERNALS__.invoke("gfc_report_rendered", {{ pageMs: performance.now() - started }}).catch(() => {{}});
                    }}
                  }};
                  const observer = new MutationObserver(notify);
                  // `document`, not `document.documentElement`: WebView2 runs this
                  // script before the root element exists, and observing null throws.
                  observer.observe(document, {{
                    attributes: true,
                    childList: true,
                    subtree: true,
                    attributeFilter: ["data-state"]
                  }});
                  window.addEventListener("gui-for-cli-rendered", notify);
                  document.addEventListener("DOMContentLoaded", notify);
                  window.addEventListener("load", notify);
                }})();
                {overlay_script}
                "##
            );
            let url = format!("http://127.0.0.1:{port}/")
                .parse()
                .map_err(|error| format!("Invalid WebUI URL: {error}"))?;
            let window = WebviewWindowBuilder::new(app, "main", WebviewUrl::External(url))
                .title(window_title(&application_name, &application_version))
                .inner_size(1200.0, 800.0)
                .initialization_script(&init_script)
                .on_document_title_changed(|window, title| {
                    let _ = window.set_title(&title);
                })
                .on_page_load(move |_window, _payload| {
                    print_metric(started_at, "webNavigationDidFinish");
                })
                .build()?;
            window.show()?;
            if std::env::var("GFC_BENCHMARK_PRESERVE_FOCUS").as_deref() != Ok("1") {
                window.set_focus()?;
            }
            print_metric(started_at, "windowShown");
            if cfg!(not(windows)) && env_flag(app_uninstall::AUTO_RUN_UNINSTALL_ENV) {
                app_uninstall::auto_run_uninstall(app.handle());
            }
            update_e2e_overlay::update(
                app.handle(),
                "App launched",
                &format!("Installed version: {application_version}"),
            );
            Ok(())
        })
        .on_window_event(|window, event| {
            if matches!(event, tauri::WindowEvent::CloseRequested { .. }) {
                let app = window.app_handle();
                app.state::<BackendProcess>().terminate();
                app.exit(0);
            }
        })
        .run(tauri::generate_context!())
        .expect("failed to run GUI for CLI Tauri app");
}

/// Test harnesses (the Windows lifecycle/update scripts) set GFC_PORT_FILE to learn the backend port.
/// They also need the per-launch API token, so it is written next to it as `<port file>.token`,
/// readable only by the current user, and only when the harness asked for the port file. The token
/// file is written first, so a port file's presence means both are ready.
fn write_test_harness_files(port: u16, api_token: &str) -> Result<(), Box<dyn std::error::Error>> {
    let Some(path) = env::var_os(PORT_FILE_ENV).map(PathBuf::from) else {
        return Ok(());
    };
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }
    let mut token_path = path.clone().into_os_string();
    token_path.push(".token");
    write_private_file(Path::new(&token_path), api_token)?;
    fs::write(path, format!("{port}\n"))?;
    Ok(())
}

fn write_private_file(path: &Path, contents: &str) -> std::io::Result<()> {
    use std::io::Write;
    let _ = fs::remove_file(path);
    let mut options = fs::OpenOptions::new();
    options.write(true).create_new(true);
    #[cfg(unix)]
    std::os::unix::fs::OpenOptionsExt::mode(&mut options, 0o600);
    // On Windows the file inherits the harness-chosen directory's ACL (a per-user log directory).
    options.open(path)?.write_all(contents.as_bytes())
}

#[cfg(unix)]
extern "C" fn handle_signal(_signal: libc::c_int) {
    let pid = NODE_PID.load(Ordering::SeqCst);
    if pid > 0 {
        unsafe {
            libc::kill(pid, libc::SIGTERM);
        }
    }
    std::process::exit(0);
}

#[derive(Clone)]
pub(crate) struct AppPaths {
    pub(crate) repo_root: PathBuf,
    pub(crate) node_path: String,
    pub(crate) server_script: PathBuf,
    pub(crate) bundle_root: PathBuf,
    pub(crate) app_support_name: String,
    /// The backend's output, so a failure that ends it can be diagnosed (in the app's log folder).
    pub(crate) backend_log: Option<PathBuf>,
}

impl AppPaths {
    fn resolve(app: &tauri::App) -> Result<Self, Box<dyn std::error::Error>> {
        let resource_root = app.path().resource_dir()?;
        let repo_root = repo_root(resource_root)?;
        let node_path = node_path(&repo_root)?;
        let server_script = repo_root.join("app/dist/web/src/server/main.js");
        let bundle_root = bundle_root(&repo_root);
        let app_support_name = app.config().identifier.clone();
        let backend_log = app
            .path()
            .app_log_dir()
            .ok()
            .map(|dir| dir.join("backend.log"));

        if !server_script.exists() {
            return Err(
                format!("WebUI server script not found: {}", server_script.display()).into(),
            );
        }
        if !bundle_root.exists() {
            return Err(format!("Bundle root not found: {}", bundle_root.display()).into());
        }

        Ok(Self {
            repo_root,
            node_path,
            server_script,
            bundle_root,
            app_support_name,
            backend_log,
        })
    }
}

fn repo_root(resource_root: PathBuf) -> Result<PathBuf, Box<dyn std::error::Error>> {
    if cfg!(debug_assertions) {
        if let Some(path) = env::var_os("GUI_FOR_CLI_REPO_ROOT").map(PathBuf::from) {
            return Ok(path);
        }
        return Ok(Path::new(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .and_then(Path::parent)
            .and_then(Path::parent)
            .and_then(Path::parent)
            .ok_or("Could not resolve source repository root")?
            .to_path_buf());
    }
    Ok(resource_root)
}

fn node_path(repo_root: &Path) -> Result<String, Box<dyn std::error::Error>> {
    let bundled = repo_root.join(if cfg!(windows) {
        "node/node.exe"
    } else {
        "node/bin/node"
    });
    if bundled.exists() {
        return Ok(child_process_path(&bundled));
    }
    if !cfg!(debug_assertions) {
        return Err(format!("Bundled Node runtime not found: {}", bundled.display()).into());
    }
    if let Ok(path) = env::var("GUI_FOR_CLI_NODE_PATH") {
        return Ok(path);
    }
    if cfg!(unix) {
        for path in [
            "/opt/homebrew/bin/node",
            "/usr/local/bin/node",
            "/usr/bin/node",
        ] {
            if Path::new(path).exists() {
                return Ok(path.to_string());
            }
        }
    }
    Ok("node".to_string())
}

fn bundle_root(repo_root: &Path) -> PathBuf {
    if cfg!(debug_assertions) {
        if let Some(path) = env::var_os("GUI_FOR_CLI_BUNDLE").map(PathBuf::from) {
            return path;
        }
    }
    let embedded_bundle = repo_root.join("examples/EmbeddedBundle");
    if embedded_bundle.exists() {
        return embedded_bundle;
    }
    repo_root.join("examples/WGSExtract")
}

fn app_version(app: &tauri::App) -> String {
    app.package_info().version.to_string()
}

fn window_title(application_name: &str, application_version: &str) -> String {
    format!("{application_name} {application_version}")
}

pub(crate) fn child_process_path(path: &Path) -> String {
    let value = path.to_string_lossy();
    if cfg!(windows) {
        value.strip_prefix(r"\\?\").unwrap_or(&value).to_string()
    } else {
        value.into_owned()
    }
}

/// A hex-encoded secret from the OS CSPRNG.
pub(crate) fn random_token(bytes: usize) -> Result<String, getrandom::Error> {
    let mut buffer = vec![0u8; bytes];
    getrandom::fill(&mut buffer)?;
    Ok(buffer.iter().map(|byte| format!("{byte:02x}")).collect())
}

fn print_metric(started_at: Instant, name: &str) {
    println!(
        "metric {name}_ms={:.1}",
        started_at.elapsed().as_secs_f64() * 1_000.0
    );
}
