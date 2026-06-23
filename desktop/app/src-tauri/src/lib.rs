//! vouch desktop — native shell for the review console (#207).

mod kb;
mod menu;
mod sidecar;
mod state;
mod util;

use kb::{check_kb_folder, init_kb_at, KbCheckResult, KbInitResult};
use menu::{build_menu, handle_menu_event, rebuild_recent_submenu};
use sidecar::SidecarManager;
use state::store::{load_state, touch_recent_kb, DesktopState};
use tauri::{AppHandle, Emitter, Manager, State};
use tauri_plugin_dialog::DialogExt;

struct AppState {
    sidecar: SidecarManager,
}

#[derive(serde::Serialize)]
struct SwitchKbResult {
    ok: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    base_url: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    kb_label: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    error: Option<String>,
}

#[tauri::command]
fn load_state_cmd() -> DesktopState {
    load_state(None)
}

#[tauri::command]
fn touch_recent_kb_cmd(project_root: String, label: Option<String>) -> Result<DesktopState, String> {
    touch_recent_kb(&project_root, label.as_deref(), None)
}

#[tauri::command]
fn check_kb_folder_cmd(selected: String) -> KbCheckResult {
    check_kb_folder(&selected)
}

#[tauri::command]
async fn init_kb_at_cmd(app: AppHandle, selected: String) -> Result<KbInitResult, String> {
    init_kb_at(&app, &selected).await
}

#[tauri::command]
async fn switch_kb(
    app: AppHandle,
    state: State<'_, AppState>,
    project_root: String,
) -> Result<SwitchKbResult, String> {
    let check = check_kb_folder(&project_root);
    if !check.ok {
        let message = check.message.clone();
        let _ = app.emit(
            "kb-error",
            serde_json::json!({ "selected": project_root, "message": message }),
        );
        return Ok(SwitchKbResult {
            ok: false,
            base_url: None,
            kb_label: None,
            error: Some(message),
        });
    }
    let root = check
        .project_root
        .clone()
        .ok_or_else(|| "missing project_root".to_string())?;

    let (base_url, kb_label) = state.sidecar.start(&app, &root).await?;
    touch_recent_kb(&root, Some(&kb_label), None)?;
    let _ = rebuild_recent_submenu(&app);

    if let Some(window) = app.get_webview_window("main") {
        let _ = window.set_title(&format!("vouch · {kb_label}"));
        let _ = window.navigate(tauri::Url::parse(&base_url).map_err(|e| e.to_string())?);
    }

    let payload = serde_json::json!({
        "project_root": root,
        "kb_label": kb_label,
        "base_url": base_url,
    });
    let _ = app.emit("kb-switched", payload);

    Ok(SwitchKbResult {
        ok: true,
        base_url: Some(base_url),
        kb_label: Some(kb_label),
        error: None,
    })
}

#[tauri::command]
async fn open_recent_kb(
    app: AppHandle,
    state: State<'_, AppState>,
    path: String,
) -> Result<SwitchKbResult, String> {
    switch_kb(app, state, path).await
}

#[tauri::command]
fn sidecar_status(state: State<'_, AppState>) -> sidecar::SidecarStatus {
    state.sidecar.status()
}

#[tauri::command]
async fn open_kb_dialog(app: AppHandle) -> Result<Option<String>, String> {
    let picked = app
        .dialog()
        .file()
        .set_title("Open knowledge base")
        .blocking_pick_folder();
    Ok(picked.map(|p| p.to_string()))
}

#[tauri::command]
async fn new_kb_dialog(app: AppHandle) -> Result<Option<String>, String> {
    let picked = app
        .dialog()
        .file()
        .set_title("New knowledge base location")
        .blocking_pick_folder();
    Ok(picked.map(|p| p.to_string()))
}

#[tauri::command]
fn navigate_to_review(app: AppHandle, state: State<'_, AppState>) -> Result<(), String> {
    let base = state
        .sidecar
        .status()
        .base_url
        .ok_or_else(|| "sidecar not running".to_string())?;
    if let Some(window) = app.get_webview_window("main") {
        window
            .navigate(tauri::Url::parse(&base).map_err(|e| e.to_string())?)
            .map_err(|e| e.to_string())?;
    }
    Ok(())
}

#[tauri::command]
fn show_welcome(app: AppHandle) -> Result<(), String> {
    if let Some(window) = app.get_webview_window("main") {
        window
            .navigate(tauri::Url::parse("http://localhost:1420").map_err(|e| e.to_string())?)
            .map_err(|e| e.to_string())?;
        window.set_title("vouch").map_err(|e| e.to_string())?;
    }
    Ok(())
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_shell::init())
        .manage(AppState {
            sidecar: SidecarManager::default(),
        })
        .setup(|app| {
            let menu = build_menu(app.handle())?;
            app.set_menu(menu)?;
            Ok(())
        })
        .on_menu_event(|app, event| {
            handle_menu_event(app, event.id().as_ref());
        })
        .invoke_handler(tauri::generate_handler![
            load_state_cmd,
            touch_recent_kb_cmd,
            check_kb_folder_cmd,
            init_kb_at_cmd,
            switch_kb,
            open_recent_kb,
            sidecar_status,
            open_kb_dialog,
            new_kb_dialog,
            navigate_to_review,
            show_welcome,
        ])
        .run(tauri::generate_context!())
        .expect("error while running vouch desktop");
}
