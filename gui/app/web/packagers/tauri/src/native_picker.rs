//! A loopback endpoint the Node backend calls to show native file dialogs. Only requests that carry
//! the per-launch API token (which only the backend knows) are served.

use std::{
    collections::HashMap,
    io::{Read, Write},
    net::{TcpListener, TcpStream},
    thread,
};

use tauri_plugin_dialog::DialogExt;

use crate::child_process_path;

const MAX_REQUEST_BYTES: usize = 16 * 1024;
/// What the `bundle` picker offers: a bundle's manifest.json or a bundle archive.
/// Keep in sync with BUNDLE_FILE_EXTENSIONS in bundle-archive.ts.
const BUNDLE_EXTENSIONS: &[&str] = &["json", "zip", "tar", "tgz", "gz", "gzip"];

pub(crate) fn start_native_picker_listener<R: tauri::Runtime>(
    listener: TcpListener,
    app: tauri::AppHandle<R>,
    api_token: String,
) {
    thread::spawn(move || {
        for stream in listener.incoming().flatten() {
            let app = app.clone();
            let api_token = api_token.clone();
            thread::spawn(move || handle_native_picker_request(stream, &app, &api_token));
        }
    });
}

fn handle_native_picker_request<R: tauri::Runtime>(
    mut stream: TcpStream,
    app: &tauri::AppHandle<R>,
    api_token: &str,
) {
    let Some(request) = read_request_head(&mut stream) else {
        write_json_response(
            &mut stream,
            400,
            serde_json::json!({"error": "Invalid request"}),
        );
        return;
    };
    let mut lines = request.lines();
    let target = lines
        .next()
        .and_then(|line| line.split_whitespace().nth(1))
        .unwrap_or("");
    let token = lines
        .filter_map(|line| line.split_once(':'))
        .find(|(name, _)| name.trim().eq_ignore_ascii_case("x-gfc-token"))
        .map(|(_, value)| value.trim())
        .unwrap_or("");
    if !constant_time_eq(token.as_bytes(), api_token.as_bytes()) {
        write_json_response(
            &mut stream,
            401,
            serde_json::json!({"error": "Missing or invalid API token"}),
        );
        return;
    }
    let Some(query) = target
        .strip_prefix("/pick?")
        .or_else(|| (target == "/pick").then_some(""))
    else {
        write_json_response(&mut stream, 404, serde_json::json!({"error": "Not found"}));
        return;
    };
    let values = parse_query(query);
    let kind = values.get("kind").map(String::as_str).unwrap_or("file");
    let title = values
        .get("title")
        .map(String::as_str)
        .unwrap_or(if kind == "directory" {
            "Choose directory"
        } else {
            "Choose file"
        });
    let default_path = values.get("defaultPath").map(String::as_str).unwrap_or("");
    let mut dialog = app.dialog().file().set_title(title);
    if !default_path.is_empty() {
        dialog = dialog.set_directory(default_path);
    }
    // Blocking picks are fine here: this runs on a per-request thread, never the main thread.
    let picked = match kind {
        "directory" | "folder" => dialog.blocking_pick_folder(),
        "file" => dialog.blocking_pick_file(),
        "bundle" => dialog
            .add_filter("Bundle", BUNDLE_EXTENSIONS)
            .blocking_pick_file(),
        _ => {
            write_json_response(
                &mut stream,
                400,
                serde_json::json!({"error": "Path picker kind must be file, directory, or bundle."}),
            );
            return;
        }
    };
    match picked.map(|path| path.into_path()) {
        Some(Ok(path)) => write_json_response(
            &mut stream,
            200,
            serde_json::json!({"path": child_process_path(&path), "kind": kind, "cancelled": false}),
        ),
        Some(Err(error)) => write_json_response(
            &mut stream,
            400,
            serde_json::json!({"error": error.to_string()}),
        ),
        None => write_json_response(
            &mut stream,
            200,
            serde_json::json!({"kind": kind, "cancelled": true}),
        ),
    }
}

fn read_request_head(stream: &mut TcpStream) -> Option<String> {
    let mut request = Vec::new();
    let mut buffer = [0; 4096];
    while !request.windows(4).any(|window| window == b"\r\n\r\n") {
        let count = stream.read(&mut buffer).ok()?;
        if count == 0 || request.len() + count > MAX_REQUEST_BYTES {
            return None;
        }
        request.extend_from_slice(&buffer[..count]);
    }
    String::from_utf8(request).ok()
}

fn constant_time_eq(left: &[u8], right: &[u8]) -> bool {
    left.len() == right.len()
        && left
            .iter()
            .zip(right)
            .fold(0, |difference, (a, b)| difference | (a ^ b))
            == 0
}

fn parse_query(query: &str) -> HashMap<String, String> {
    query
        .split('&')
        .filter(|part| !part.is_empty())
        .filter_map(|part| {
            let (key, value) = part.split_once('=').unwrap_or((part, ""));
            Some((percent_decode(key)?, percent_decode(value)?))
        })
        .collect()
}

fn percent_decode(value: &str) -> Option<String> {
    let bytes = value.as_bytes();
    let mut output = Vec::with_capacity(bytes.len());
    let mut index = 0;
    while index < bytes.len() {
        match bytes[index] {
            b'+' => {
                output.push(b' ');
                index += 1;
            }
            b'%' if index + 2 < bytes.len() => {
                let hex = std::str::from_utf8(&bytes[index + 1..index + 3]).ok()?;
                output.push(u8::from_str_radix(hex, 16).ok()?);
                index += 3;
            }
            byte => {
                output.push(byte);
                index += 1;
            }
        }
    }
    String::from_utf8(output).ok()
}

fn write_json_response(stream: &mut TcpStream, status: u16, body: serde_json::Value) {
    let reason = match status {
        200 => "OK",
        400 => "Bad Request",
        401 => "Unauthorized",
        404 => "Not Found",
        _ => "Internal Server Error",
    };
    let payload = format!("{body}\n");
    let response = format!(
        "HTTP/1.1 {status} {reason}\r\nContent-Type: application/json; charset=utf-8\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{payload}",
        payload.len()
    );
    let _ = stream.write_all(response.as_bytes());
}

#[cfg(test)]
mod tests {
    use super::{constant_time_eq, percent_decode};

    #[test]
    fn decodes_query_components() {
        assert_eq!(
            percent_decode("C%3A%5CUsers+me").as_deref(),
            Some("C:\\Users me")
        );
        assert_eq!(percent_decode("100%25").as_deref(), Some("100%"));
        assert_eq!(percent_decode("bad%zz"), None);
    }

    #[test]
    fn compares_tokens_by_value() {
        assert!(constant_time_eq(b"secret", b"secret"));
        assert!(!constant_time_eq(b"secret", b"secreT"));
        assert!(!constant_time_eq(b"secret", b"secret2"));
    }
}
