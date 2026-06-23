//! Review-ui sidecar process management.

use reqwest::blocking::Client;
use serde::{Deserialize, Serialize};
use std::path::Path;
use std::sync::Mutex;
use std::time::{Duration, Instant};
use tauri::AppHandle;
use tauri_plugin_shell::process::{CommandChild, CommandEvent};
use tauri_plugin_shell::ShellExt;

pub const DEFAULT_HOST: &str = "127.0.0.1";
pub const DEFAULT_PORT: u16 = 7780;
const STARTUP_TIMEOUT: Duration = Duration::from_secs(30);
const POLL_INTERVAL: Duration = Duration::from_millis(150);

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct HealthzResponse {
    pub ok: bool,
    pub kb: String,
    pub kb_label: Option<String>,
    pub pending: i64,
    pub auth: bool,
    pub clients: i64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SidecarStatus {
    pub running: bool,
    pub base_url: Option<String>,
    pub project_root: Option<String>,
    pub kb_label: Option<String>,
    pub pid: Option<u32>,
}

pub struct SidecarManager {
    child: Mutex<Option<CommandChild>>,
    base_url: Mutex<Option<String>>,
    project_root: Mutex<Option<String>>,
    kb_label: Mutex<Option<String>>,
    port: Mutex<u16>,
}

impl Default for SidecarManager {
    fn default() -> Self {
        Self {
            child: Mutex::new(None),
            base_url: Mutex::new(None),
            project_root: Mutex::new(None),
            kb_label: Mutex::new(None),
            port: Mutex::new(DEFAULT_PORT),
        }
    }
}

impl SidecarManager {
    pub fn status(&self) -> SidecarStatus {
        SidecarStatus {
            running: self.child.lock().ok().and_then(|g| g.as_ref().map(|_| true)).unwrap_or(false),
            base_url: self.base_url.lock().ok().and_then(|g| g.clone()),
            project_root: self.project_root.lock().ok().and_then(|g| g.clone()),
            kb_label: self.kb_label.lock().ok().and_then(|g| g.clone()),
            pid: None,
        }
    }

    pub async fn stop(&self) -> Result<(), String> {
        let mut guard = self.child.lock().map_err(|e| e.to_string())?;
        if let Some(child) = guard.take() {
            child.kill().map_err(|e| e.to_string())?;
        }
        if let Ok(mut url) = self.base_url.lock() {
            *url = None;
        }
        Ok(())
    }

    fn health_url(base: &str) -> String {
        format!("{base}/healthz")
    }

    fn wait_for_health(base: &str, expected_root: &str) -> Result<HealthzResponse, String> {
        let client = Client::builder()
            .timeout(Duration::from_secs(2))
            .build()
            .map_err(|e| e.to_string())?;
        let deadline = Instant::now() + STARTUP_TIMEOUT;
        let mut last_err = "sidecar did not become healthy".to_string();
        while Instant::now() < deadline {
            match client.get(Self::health_url(base)).send() {
                Ok(resp) => {
                    if let Ok(body) = resp.json::<HealthzResponse>() {
                        if body.ok {
                            let kb = Path::new(&body.kb);
                            let expected = Path::new(expected_root);
                            if kb.canonicalize().ok() == expected.canonicalize().ok() {
                                return Ok(body);
                            }
                            last_err = format!("kb mismatch: {} != {}", body.kb, expected_root);
                        }
                    }
                }
                Err(e) => last_err = e.to_string(),
            }
            std::thread::sleep(POLL_INTERVAL);
        }
        Err(last_err)
    }

    pub async fn start(&self, app: &AppHandle, project_root: &str) -> Result<(String, String), String> {
        self.stop().await?;

        let port = {
            let mut guard = self.port.lock().map_err(|e| e.to_string())?;
            *guard = DEFAULT_PORT;
            *guard
        };
        let bind = format!("{DEFAULT_HOST}:{port}");
        let base_url = format!("http://{DEFAULT_HOST}:{port}");

        let shell = app.shell();
        let sidecar = shell
            .sidecar("vouch")
            .or_else(|_| shell.command("vouch"))
            .map_err(|e| e.to_string())?;

        let (mut rx, child) = sidecar
            .args([
                "review-ui",
                "--bind",
                &bind,
                "--kb",
                project_root,
                "--no-open-browser",
                "--reviewer",
                "desktop-reviewer",
            ])
            .spawn()
            .map_err(|e| e.to_string())?;

        {
            let mut guard = self.child.lock().map_err(|e| e.to_string())?;
            *guard = Some(child);
        }

        // Drain events in background so the pipe doesn't block the child.
        tauri::async_runtime::spawn(async move {
            while let Some(event) = rx.recv().await {
                if matches!(event, CommandEvent::Terminated(_)) {
                    break;
                }
            }
        });

        let health = Self::wait_for_health(&base_url, project_root)?;
        let label = health
            .kb_label
            .clone()
            .unwrap_or_else(|| Path::new(project_root).file_name().unwrap().to_string_lossy().to_string());

        if let Ok(mut url) = self.base_url.lock() {
            *url = Some(base_url.clone());
        }
        if let Ok(mut root) = self.project_root.lock() {
            *root = Some(project_root.to_string());
        }
        if let Ok(mut lbl) = self.kb_label.lock() {
            *lbl = Some(label.clone());
        }

        Ok((base_url, label))
    }
}
