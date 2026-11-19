use std::io::{BufRead, BufReader, Read};
use std::process::{Child, Command, Stdio};
use std::sync::mpsc;
use std::time::{Duration, Instant};

pub fn stop(child: &mut Child, grace: Duration) {
    if matches!(child.try_wait(), Ok(Some(_))) { return; }
    let _ = Command::new("/bin/kill").args(["-TERM", &child.id().to_string()])
        .stdout(Stdio::null()).stderr(Stdio::null()).status();
    let deadline = Instant::now() + grace;
    while Instant::now() < deadline {
        if matches!(child.try_wait(), Ok(Some(_))) { return; }
        std::thread::sleep(Duration::from_millis(25));
    }
    let _ = child.kill();
    let _ = child.wait();
}

pub fn handshake(child: &mut Child, timeout: Duration) -> Result<serde_json::Value, String> {
    let output = child.stdout.take().ok_or("Local host has no startup pipe.")?;
    let (sender, receiver) = mpsc::channel();
    std::thread::spawn(move || {
        let mut reader = BufReader::new(output);
        let mut bytes = Vec::new();
        let result = (&mut reader).take(65537).read_until(b'\n', &mut bytes)
            .map_err(|_| "Cannot read local host startup response.".to_string())
            .and_then(|_| {
                if bytes.len() > 65536 || !bytes.ends_with(b"\n") {
                    return Err("Local host returned an incomplete or oversized startup response.".into());
                }
                let value: serde_json::Value = serde_json::from_slice(&bytes)
                    .map_err(|_| "Local host returned invalid startup data.".to_string())?;
                let url = value["url"].as_str().unwrap_or("");
                let port = url.strip_prefix("http://127.0.0.1:")
                    .and_then(|p| p.parse::<u16>().ok()).unwrap_or(0);
                if port == 0 || value["token"].as_str().unwrap_or("").is_empty() {
                    return Err("Local host returned invalid connection settings.".into());
                }
                Ok(value)
            });
        let valid = result.is_ok();
        let _ = sender.send(result);
        if valid { let _ = std::io::copy(&mut reader, &mut std::io::sink()); }
    });
    let result = receiver.recv_timeout(timeout)
        .unwrap_or_else(|_| Err("Local host startup timed out. Restart the app; inspect terminal logs if this repeats.".into()));
    if result.is_err() { stop(child, Duration::from_millis(500)); }
    result
}

#[cfg(test)]
mod tests {
    use super::*;
    fn fixture(code: &str) -> Child {
        Command::new(std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../../../.venv/bin/python"))
            .args(["-u", "-c", code]).stdout(Stdio::piped()).spawn().unwrap()
    }
    #[test]
    fn valid_host_exits_cleanly() {
        let mut child = fixture("print('{\"url\":\"http://127.0.0.1:12345\",\"token\":\"fixture\"}'); import time; time.sleep(30)");
        assert!(handshake(&mut child, Duration::from_secs(3)).is_ok());
        stop(&mut child, Duration::from_secs(1));
        assert!(child.try_wait().unwrap().is_some());
    }
    #[test]
    fn malformed_or_early_exit_never_exposes_raw_data() {
        for code in ["print('private-invalid-payload')", "pass", "print('{}')"] {
            let mut child = fixture(code);
            let error = handshake(&mut child, Duration::from_secs(3)).unwrap_err();
            assert!(!error.contains("private-invalid-payload"));
            assert!(child.try_wait().unwrap().is_some());
        }
    }
    #[test]
    fn oversized_handshake_is_rejected() {
        let mut child = fixture("print('x'*70000); import time; time.sleep(30)");
        assert!(handshake(&mut child, Duration::from_secs(3)).is_err());
        assert!(child.try_wait().unwrap().is_some());
    }
    #[test]
    fn silent_startup_is_bounded() {
        let mut child = fixture("import time; time.sleep(30)");
        let start = Instant::now();
        assert!(handshake(&mut child, Duration::from_millis(100)).is_err());
        assert!(start.elapsed() < Duration::from_secs(3));
        assert!(child.try_wait().unwrap().is_some());
    }
    #[test]
    fn unresponsive_host_is_force_stopped() {
        let mut child = fixture("import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); print('{\"url\":\"http://127.0.0.1:12345\",\"token\":\"fixture\"}'); time.sleep(30)");
        assert!(handshake(&mut child, Duration::from_secs(3)).is_ok());
        let start = Instant::now();
        stop(&mut child, Duration::from_millis(100));
        assert!(start.elapsed() < Duration::from_secs(3));
        assert!(child.try_wait().unwrap().is_some());
    }
}
