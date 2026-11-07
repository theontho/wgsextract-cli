//! Launches the Node WebUI backend and owns its lifetime.

use std::{
    env, fs,
    io::{Read, Write},
    net::TcpStream,
    path::PathBuf,
    process::{Child, Command, Stdio},
    sync::{Arc, Mutex},
    thread,
    time::{Duration, Instant},
};

#[cfg(windows)]
use std::os::windows::process::CommandExt;

use crate::{child_process_path, AppPaths};

#[cfg(windows)]
const CREATE_NO_WINDOW: u32 = 0x0800_0000;
const STARTUP_TIMEOUT: Duration = Duration::from_secs(15);
/// Node escalates its own process trees to SIGKILL after 3 s, so this leaves it time to finish.
const SHUTDOWN_TIMEOUT: Duration = Duration::from_secs(5);

pub(crate) struct BackendLaunch<'a> {
    pub paths: &'a AppPaths,
    pub picker_port: u16,
    pub application_name: &'a str,
    pub application_version: &'a str,
    pub api_token: &'a str,
}

struct BackendState {
    child: Child,
    port: u16,
    api_token: String,
    /// Every process the backend starts inherits this job; closing it (even if this app crashes) kills them all.
    #[cfg(windows)]
    job: job::KillOnCloseJob,
}

pub(crate) struct BackendProcess(Arc<Mutex<Option<BackendState>>>);

impl BackendProcess {
    pub(crate) fn new() -> Self {
        Self(Arc::new(Mutex::new(None)))
    }

    /// Starts Node on an OS-assigned port and waits until it reports that port.
    pub(crate) fn launch(
        &self,
        launch: BackendLaunch<'_>,
    ) -> Result<(u32, u16), Box<dyn std::error::Error>> {
        let port_file = env::temp_dir().join(format!(
            "gfc-backend-{}-{}.port",
            std::process::id(),
            crate::random_token(8)?
        ));
        let _ = fs::remove_file(&port_file);
        let mut command = Command::new(&launch.paths.node_path);
        command
            .current_dir(child_process_path(&launch.paths.repo_root))
            .arg(child_process_path(&launch.paths.server_script))
            .args(["--port", "0", "--host", "127.0.0.1", "--bundle"])
            .arg(child_process_path(&launch.paths.bundle_root))
            // Secrets and ports go through the environment, never argv (which other users can list).
            .env("GFC_API_TOKEN", launch.api_token)
            .env("GFC_PORT_FILE", &port_file)
            .env("GFC_PARENT_PID", std::process::id().to_string())
            .env(
                "GUI_FOR_CLI_APP_SUPPORT_NAME",
                &launch.paths.app_support_name,
            )
            .env("GUI_FOR_CLI_APPLICATION_NAME", launch.application_name)
            .env(
                "GUI_FOR_CLI_APPLICATION_VERSION",
                launch.application_version,
            )
            .env("GFC_NATIVE_PICKER_PORT", launch.picker_port.to_string())
            .stdin(Stdio::null());
        match backend_log(launch.paths.backend_log.as_deref()) {
            Some((stdout, stderr)) => command.stdout(stdout).stderr(stderr),
            None => command.stdout(Stdio::null()).stderr(Stdio::null()),
        };
        #[cfg(windows)]
        command.creation_flags(CREATE_NO_WINDOW);
        let mut child = command.spawn()?;
        #[cfg(windows)]
        let job = match job::KillOnCloseJob::containing(&child) {
            Ok(job) => job,
            Err(error) => {
                let _ = child.kill();
                return Err(format!("Could not place the backend in a job object: {error}").into());
            }
        };
        let port = match wait_for_port_file(&port_file, &mut child) {
            Ok(port) => port,
            Err(error) => {
                let _ = child.kill();
                let _ = child.wait();
                return Err(error);
            }
        };
        let pid = child.id();
        *self
            .0
            .lock()
            .map_err(|_| "Backend process lock was poisoned")? = Some(BackendState {
            child,
            port,
            api_token: launch.api_token.to_string(),
            #[cfg(windows)]
            job,
        });
        Ok((pid, port))
    }

    pub(crate) fn terminate(&self) {
        let Ok(mut state) = self.0.lock() else {
            return;
        };
        let Some(mut state) = state.take() else {
            return;
        };
        let _ = request_backend_shutdown(state.port, &state.api_token);
        let deadline = Instant::now() + SHUTDOWN_TIMEOUT;
        while Instant::now() < deadline {
            match state.child.try_wait() {
                Ok(Some(_)) => break,
                Ok(None) => thread::sleep(Duration::from_millis(50)),
                Err(_) => break,
            }
        }
        #[cfg(windows)]
        state.job.terminate();
        if matches!(state.child.try_wait(), Ok(None)) {
            let _ = state.child.kill();
        }
        let _ = state.child.wait();
    }
}

impl Drop for BackendProcess {
    fn drop(&mut self) {
        self.terminate();
    }
}

/// A new log each launch, holding the backend's stdout and stderr.
fn backend_log(path: Option<&std::path::Path>) -> Option<(Stdio, Stdio)> {
    let path = path?;
    fs::create_dir_all(path.parent()?).ok()?;
    let file = fs::File::create(path).ok()?;
    let stderr = file.try_clone().ok()?;
    Some((Stdio::from(file), Stdio::from(stderr)))
}

fn wait_for_port_file(
    port_file: &PathBuf,
    child: &mut Child,
) -> Result<u16, Box<dyn std::error::Error>> {
    let deadline = Instant::now() + STARTUP_TIMEOUT;
    while Instant::now() < deadline {
        if let Some(status) = child.try_wait()? {
            return Err(format!("WebUI backend exited during startup ({status})").into());
        }
        if let Ok(contents) = fs::read_to_string(port_file) {
            if let Ok(port) = contents.trim().parse::<u16>() {
                let _ = fs::remove_file(port_file);
                return Ok(port);
            }
        }
        thread::sleep(Duration::from_millis(25));
    }
    Err("Timed out waiting for the WebUI backend to report its port".into())
}

fn request_backend_shutdown(port: u16, api_token: &str) -> bool {
    let Ok(mut stream) = TcpStream::connect(("127.0.0.1", port)) else {
        return false;
    };
    let _ = stream.set_read_timeout(Some(Duration::from_millis(750)));
    let body = "{}";
    let request = format!(
        "POST /api/shutdown HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nX-GFC-Token: {api_token}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
        body.len()
    );
    if stream.write_all(request.as_bytes()).is_err() {
        return false;
    }
    let mut response = [0; 64];
    let Ok(count) = stream.read(&mut response) else {
        return false;
    };
    response[..count].starts_with(b"HTTP/1.1 200")
}

#[cfg(windows)]
mod job {
    use std::{io, mem, os::windows::io::AsRawHandle, process::Child, ptr};

    use windows_sys::Win32::{
        Foundation::{CloseHandle, HANDLE},
        System::JobObjects::{
            AssignProcessToJobObject, CreateJobObjectW, JobObjectExtendedLimitInformation,
            SetInformationJobObject, TerminateJobObject, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
            JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
        },
    };

    pub(crate) struct KillOnCloseJob(HANDLE);

    // The handle is only used through thread-safe Win32 calls.
    unsafe impl Send for KillOnCloseJob {}

    impl KillOnCloseJob {
        pub(crate) fn containing(child: &Child) -> io::Result<Self> {
            // SAFETY: plain Win32 calls on handles we own; `info` outlives the call that reads it.
            unsafe {
                let handle = CreateJobObjectW(ptr::null(), ptr::null());
                if handle.is_null() {
                    return Err(io::Error::last_os_error());
                }
                let job = Self(handle);
                let mut info: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = mem::zeroed();
                info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
                if SetInformationJobObject(
                    handle,
                    JobObjectExtendedLimitInformation,
                    &info as *const JOBOBJECT_EXTENDED_LIMIT_INFORMATION as *const _,
                    mem::size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
                ) == 0
                {
                    return Err(io::Error::last_os_error());
                }
                if AssignProcessToJobObject(handle, child.as_raw_handle() as HANDLE) == 0 {
                    return Err(io::Error::last_os_error());
                }
                Ok(job)
            }
        }

        pub(crate) fn terminate(&self) {
            // SAFETY: `self.0` is a valid job handle until drop.
            unsafe {
                TerminateJobObject(self.0, 1);
            }
        }
    }

    impl Drop for KillOnCloseJob {
        fn drop(&mut self) {
            // SAFETY: closing our own handle; with KILL_ON_JOB_CLOSE this ends every process in the job.
            unsafe {
                CloseHandle(self.0);
            }
        }
    }
}
