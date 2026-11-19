#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]
mod host_process;
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::time::Duration;
use tauri::Manager;
struct Host {
    child: Mutex<Option<Child>>,
    connection: Mutex<Option<Result<serde_json::Value, String>>>,
    stopping: std::sync::atomic::AtomicBool,
}
impl Host {
    fn connect(&self) -> Result<serde_json::Value, String> {
        let mut saved = self.connection.lock().map_err(|_| "Host connection lock failed.")?;
        if let Some(value) = saved.as_ref() { return value.clone(); }
        if self.stopping.load(std::sync::atomic::Ordering::SeqCst) { return Err("Desktop is closing.".into()); }
        let (mut child, result) = start_host();
        let mut slot = self.child.lock().map_err(|_| "Host process lock failed.")?;
        if self.stopping.load(std::sync::atomic::Ordering::SeqCst) {
            if let Some(ref mut process) = child { host_process::stop(process, Duration::from_secs(5)); }
            return Err("Desktop is closing.".into());
        }
        *slot = child;
        *saved = Some(result.clone());
        result
    }
    fn stop(&self) {
        self.stopping.store(true, std::sync::atomic::Ordering::SeqCst);
        if let Ok(mut slot) = self.child.lock() {
            if let Some(mut child) = slot.take() { host_process::stop(&mut child, Duration::from_secs(5)); }
        }
    }
}
impl Drop for Host { fn drop(&mut self) { self.stop(); } }
#[tauri::command]
async fn connection(app: tauri::AppHandle) -> Result<serde_json::Value, String> {
    // Keep the desktop responsive while Python starts or waits for macOS access.
    tauri::async_runtime::spawn_blocking(move || app.state::<Host>().connect())
        .await.map_err(|_| "Host startup worker failed.".to_string())?
}
fn start_host() -> (Option<Child>, Result<serde_json::Value, String>) {
    let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../../..");
    let log_dir = root.join(".runtime");
    let _ = std::fs::create_dir_all(&log_dir);
    let log_path = log_dir.join("desktop-host-startup.log");
    let mut diagnostics = std::fs::OpenOptions::new();
    diagnostics.create(true).write(true).truncate(true);
    #[cfg(unix)] {
        use std::os::unix::fs::OpenOptionsExt;
        diagnostics.mode(0o600);
    }
    let mut log = diagnostics.open(&log_path).ok();
    let mut connection_result = Err("Cannot start the local host. Run uv sync in the project folder, then restart the app.".into());
    let mut owned_child = None;
    // A cold post-build launch can time out on this Mac. Retry only after the
    // failed owned host has been stopped and reaped; never attach to other hosts.
    for attempt in 1..=2 {
        if let Some(ref mut file) = log {
            use std::io::Write;
            let _ = writeln!(file, "LocalOffice host startup attempt {}", attempt);
        }
        let stderr = log.as_ref().and_then(|f| f.try_clone().ok()).map(Stdio::from).unwrap_or_else(Stdio::inherit);
        let spawned = Command::new(root.join(".venv/bin/python"))
            .args(["-u", "-c", "import sys,faulthandler; sys.stderr.write('Python entered; importing host\\n'); faulthandler.dump_traceback_later(10); from localoffice.server import main; faulthandler.cancel_dump_traceback_later(); sys.stderr.write('Host imports ready\\n'); main()"])
            .current_dir(&root)
            .env("HF_HUB_OFFLINE", "1").env("TRANSFORMERS_OFFLINE", "1")
            .stdin(Stdio::null()).stdout(Stdio::piped()).stderr(stderr).spawn();
        match spawned {
            Ok(mut child) => {
                connection_result = host_process::handshake(&mut child, Duration::from_secs(20));
                if connection_result.is_ok() { owned_child = Some(child); break; }
                if let Some(ref mut file) = log {
                    use std::io::Write;
                    let _ = writeln!(file, "Attempt {} failed: {}", attempt, connection_result.as_ref().unwrap_err());
                }
                // handshake already stops/reaps failed children.
            }
            Err(_) => break,
        }
    }
    if connection_result.is_err() {
        connection_result = Err(format!("Local host unavailable after bounded startup attempts. If macOS asks for Documents-folder access, allow it for this project, then restart. Check System Settings > Privacy & Security > Files and Folders > LocalOffice if the prompt is hidden. Diagnostics: {}. Run uv sync only if dependencies are missing.", log_path.display()));
    }
    (owned_child, connection_result)
}
fn main() {
    let host = Host {
        child: Mutex::new(None), connection: Mutex::new(None),
        stopping: std::sync::atomic::AtomicBool::new(false),
    };
    let app = tauri::Builder::default().manage(host)
        .invoke_handler(tauri::generate_handler![connection]).build(tauri::generate_context!()).expect("Desktop launch failed");
    app.run(|app,event| { if let tauri::RunEvent::Exit = event { app.state::<Host>().stop(); }});
}
