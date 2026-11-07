use std::{env, thread, time::Duration};

use tauri::Manager;

const ENABLE_ENV: &str = "GFC_TAURI_E2E_UPDATE_OVERLAY";
const STEP_DELAY_SECONDS_ENV: &str = "GFC_TAURI_E2E_STEP_DELAY_SECONDS";

pub fn enabled() -> bool {
    env_flag(ENABLE_ENV)
}

pub fn step_delay() -> Duration {
    env_duration_seconds(STEP_DELAY_SECONDS_ENV)
}

pub fn pause_for_review() {
    if enabled() {
        thread::sleep(step_delay());
    }
}

pub fn script(application_name: &str, application_version: &str) -> String {
    if !enabled() {
        return String::new();
    }
    let application_name = js_string_literal(application_name);
    let application_version = js_string_literal(application_version);
    format!(
        r##"
                (() => {{
                  const appName = {application_name};
                  const appVersion = {application_version};
                  let lastStatus = "App launched";
                  let lastDetail = `Installed version: ${{appVersion}}`;
                  const ensurePanel = () => {{
                    if (!document.body) {{
                      return null;
                    }}
                    let panel = document.getElementById("gui-for-cli-update-e2e-overlay");
                    if (panel) {{
                      return panel;
                    }}
                    panel = document.createElement("section");
                    panel.id = "gui-for-cli-update-e2e-overlay";
                    panel.setAttribute("aria-label", "Update end-to-end test status");
                    panel.style.cssText = [
                      "position: fixed",
                      "z-index: 2147483647",
                      "top: 16px",
                      "left: 16px",
                      "width: 360px",
                      "padding: 14px 16px",
                      "border-radius: 12px",
                      "border: 2px solid #60a5fa",
                      "background: rgba(15, 23, 42, 0.94)",
                      "color: white",
                      "font: 600 16px/1.35 system-ui, -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif",
                      "box-shadow: 0 12px 32px rgba(0, 0, 0, 0.35)",
                      "pointer-events: none"
                    ].join(";");
                    panel.innerHTML = [
                      "<div style=\"font-size: 13px; opacity: 0.78; text-transform: uppercase; letter-spacing: 0.08em\">Windows updater E2E</div>",
                      "<div data-role=\"app\" style=\"margin-top: 4px; font-size: 20px\"></div>",
                      "<div data-role=\"status\" style=\"margin-top: 10px; color: #bfdbfe\"></div>",
                      "<div data-role=\"detail\" style=\"margin-top: 4px; font-weight: 500\"></div>",
                      "<div data-role=\"progress-track\" style=\"display: none; margin-top: 12px; height: 12px; overflow: hidden; border-radius: 999px; background: rgba(255, 255, 255, 0.18)\">",
                      "  <div data-role=\"progress-bar\" style=\"width: 0%; height: 100%; border-radius: inherit; background: linear-gradient(90deg, #60a5fa, #34d399)\"></div>",
                      "</div>"
                    ].join("");
                    document.body.appendChild(panel);
                    return panel;
                  }};
                  const render = (status, detail, progress) => {{
                    lastStatus = String(status || "");
                    lastDetail = String(detail || "");
                    const panel = ensurePanel();
                    if (!panel) {{
                      return;
                    }}
                    panel.querySelector("[data-role='app']").textContent = `${{appName}} version ${{appVersion}}`;
                    panel.querySelector("[data-role='status']").textContent = lastStatus;
                    panel.querySelector("[data-role='detail']").textContent = lastDetail;
                    const progressTrack = panel.querySelector("[data-role='progress-track']");
                    const progressBar = panel.querySelector("[data-role='progress-bar']");
                    if (Number.isFinite(progress)) {{
                      progressTrack.style.display = "block";
                      progressBar.style.width = `${{Math.max(0, Math.min(100, progress))}}%`;
                    }} else {{
                      progressTrack.style.display = "none";
                      progressBar.style.width = "0%";
                    }}
                  }};
                  window.__guiForCliSetUpdateE2EStatus = render;
                  document.addEventListener("DOMContentLoaded", () => render(lastStatus, lastDetail));
                  window.addEventListener("load", () => render(lastStatus, lastDetail));
                  render(lastStatus, lastDetail);
                }})();
"##
    )
}

pub fn update<R: tauri::Runtime>(app: &tauri::AppHandle<R>, status: &str, detail: &str) {
    update_with_progress(app, status, detail, None);
}

pub fn update_with_progress<R: tauri::Runtime>(
    app: &tauri::AppHandle<R>,
    status: &str,
    detail: &str,
    progress_percent: Option<f64>,
) {
    if !enabled() {
        return;
    }
    let Some(window) = app.get_webview_window("main") else {
        return;
    };
    let progress = progress_percent
        .map(|value| format!("{value:.2}"))
        .unwrap_or_else(|| "null".to_string());
    let script = format!(
        "window.__guiForCliSetUpdateE2EStatus && window.__guiForCliSetUpdateE2EStatus({}, {}, {});",
        js_string_literal(status),
        js_string_literal(detail),
        progress
    );
    if let Err(error) = window.eval(script) {
        eprintln!("Could not update E2E update overlay: {error}");
    }
}

fn env_flag(name: &str) -> bool {
    matches!(
        env::var(name)
            .unwrap_or_default()
            .trim()
            .to_ascii_lowercase()
            .as_str(),
        "1" | "true" | "yes" | "on"
    )
}

fn env_duration_seconds(name: &str) -> Duration {
    env::var(name)
        .ok()
        .and_then(|value| value.trim().parse::<u64>().ok())
        .map(Duration::from_secs)
        .unwrap_or(Duration::ZERO)
}

fn js_string_literal(value: &str) -> String {
    let mut escaped = String::with_capacity(value.len() + 2);
    escaped.push('"');
    for character in value.chars() {
        match character {
            '"' => escaped.push_str("\\\""),
            '\\' => escaped.push_str("\\\\"),
            '\n' => escaped.push_str("\\n"),
            '\r' => escaped.push_str("\\r"),
            '\t' => escaped.push_str("\\t"),
            character if character.is_control() => {
                escaped.push_str(&format!("\\u{:04x}", character as u32));
            }
            character => escaped.push(character),
        }
    }
    escaped.push('"');
    escaped
}
