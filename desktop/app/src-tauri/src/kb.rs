//! KB folder validation and init via the ``vouch`` CLI.

use serde::{Deserialize, Serialize};
use std::path::{Path, PathBuf};
use tauri_plugin_shell::process::CommandEvent;
use tauri_plugin_shell::ShellExt;

const KB_DIRNAME: &str = ".vouch";

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct KbCheckResult {
    pub ok: bool,
    pub project_root: Option<String>,
    pub kb_dir: Option<String>,
    pub message: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct KbInitResult {
    pub ok: bool,
    pub project_root: String,
    pub kb_dir: String,
    pub claim_id: String,
    pub starter_present: bool,
    pub label: String,
}

fn resolve_selection(selected: &Path) -> Result<PathBuf, String> {
    let resolved = selected
        .canonicalize()
        .map_err(|e| format!("{selected:?}: {e}"))?;
    if resolved.file_name().and_then(|s| s.to_str()) == Some(KB_DIRNAME) {
        return Ok(resolved
            .parent()
            .ok_or_else(|| "invalid .vouch path".to_string())?
            .to_path_buf());
    }
    Ok(resolved)
}

pub fn check_kb_folder(selected: &str) -> KbCheckResult {
    let path = Path::new(selected);
    let root = match resolve_selection(path) {
        Ok(r) => r,
        Err(message) => {
            return KbCheckResult {
                ok: false,
                project_root: None,
                kb_dir: None,
                message,
            }
        }
    };
    if !root.is_dir() {
        return KbCheckResult {
            ok: false,
            project_root: Some(root.to_string_lossy().to_string()),
            kb_dir: None,
            message: format!("{} is not a directory", root.display()),
        };
    }
    let kb_dir = root.join(KB_DIRNAME);
    if kb_dir.is_dir() {
        return KbCheckResult {
            ok: true,
            project_root: Some(root.to_string_lossy().to_string()),
            kb_dir: Some(kb_dir.to_string_lossy().to_string()),
            message: "ok".into(),
        };
    }
    KbCheckResult {
        ok: false,
        project_root: Some(root.to_string_lossy().to_string()),
        kb_dir: Some(kb_dir.to_string_lossy().to_string()),
        message: format!("no {KB_DIRNAME}/ directory at {}", root.display()),
    }
}

pub async fn run_vouch_json<R: serde::de::DeserializeOwned>(
    app: &tauri::AppHandle,
    args: &[&str],
) -> Result<R, String> {
    let shell = app.shell();
    let sidecar = shell
        .sidecar("vouch")
        .or_else(|_| shell.command("vouch"))
        .map_err(|e| e.to_string())?;
    let (mut rx, _child) = sidecar
        .args(args)
        .spawn()
        .map_err(|e| e.to_string())?;

    let mut stdout = String::new();
    while let Some(event) = rx.recv().await {
        match event {
            CommandEvent::Stdout(line) => stdout.push_str(&line),
            CommandEvent::Stderr(line) => stdout.push_str(&line),
            CommandEvent::Terminated(payload) => {
                if payload.code != Some(0) {
                    return Err(format!("vouch exited {:?}: {stdout}", payload.code));
                }
                break;
            }
            _ => {}
        }
    }
    serde_json::from_str(stdout.trim()).map_err(|e| format!("invalid json: {e}; raw={stdout}"))
}

pub async fn init_kb_at(app: &tauri::AppHandle, selected: &str) -> Result<KbInitResult, String> {
    std::fs::create_dir_all(selected).map_err(|e| e.to_string())?;
  let result: KbInitResult = run_vouch_json(
        app,
        &["desktop", "kb-init", selected],
    )
    .await?;
    if !result.ok {
        return Err("kb-init returned ok=false".into());
    }
    Ok(result)
}
