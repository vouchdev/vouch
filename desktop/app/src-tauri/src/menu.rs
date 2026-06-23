//! Application menu: File → Open KB / Recent KBs / New KB (#207).

use tauri::menu::{Menu, MenuItem, PredefinedMenuItem, Submenu};
use tauri::{AppHandle, Emitter, Manager, Wry};

use crate::state::store::{load_state, label_for_path};

pub const MENU_OPEN_KB: &str = "open-kb";
pub const MENU_NEW_KB: &str = "new-kb";
pub const MENU_RECENT_PREFIX: &str = "recent-kb-";

pub fn build_menu(app: &AppHandle) -> tauri::Result<Menu<Wry>> {
    let open_kb = MenuItem::with_id(app, MENU_OPEN_KB, "Open KB…", true, None::<&str>)?;
    let new_kb = MenuItem::with_id(app, MENU_NEW_KB, "New KB…", true, None::<&str>)?;
    let recent_sub = build_recent_submenu(app)?;
    let separator = PredefinedMenuItem::separator(app)?;
    let quit = PredefinedMenuItem::quit(app, Some("Quit"))?;

    let file_menu = Submenu::with_items(
        app,
        "File",
        true,
        &[&open_kb, &recent_sub, &separator, &new_kb, &separator, &quit],
    )?;

    Menu::with_items(app, &[&file_menu])
}

pub fn rebuild_recent_submenu(app: &AppHandle) -> tauri::Result<()> {
    let menu = build_menu(app)?;
    app.set_menu(menu)?;
    Ok(())
}

fn build_recent_submenu(app: &AppHandle) -> tauri::Result<Submenu<Wry>> {
    let state = load_state(None);
    let mut items: Vec<MenuItem<Wry>> = Vec::new();
    if state.recent_kbs.is_empty() {
        let empty = MenuItem::with_id(app, "recent-empty", "(no recent KBs)", false, None::<&str>)?;
        items.push(empty);
    } else {
        for (idx, entry) in state.recent_kbs.iter().enumerate() {
            let id = format!("{MENU_RECENT_PREFIX}{idx}");
            let label = if entry.label.is_empty() {
                label_for_path(&entry.path)
            } else {
                entry.label.clone()
            };
            let item = MenuItem::with_id(app, &id, label, true, None::<&str>)?;
            items.push(item);
        }
    }
    let refs: Vec<&dyn tauri::menu::IsMenuItem<Wry>> =
        items.iter().map(|i| i as &dyn tauri::menu::IsMenuItem<Wry>).collect();
    Submenu::with_items(app, "Recent KBs", true, &refs)
}

pub fn handle_menu_event(app: &AppHandle, event_id: &str) {
    if event_id == MENU_OPEN_KB {
        let _ = app.emit("menu-open-kb", ());
        return;
    }
    if event_id == MENU_NEW_KB {
        let _ = app.emit("menu-new-kb", ());
        return;
    }
    if let Some(idx_str) = event_id.strip_prefix(MENU_RECENT_PREFIX) {
        if let Ok(idx) = idx_str.parse::<usize>() {
            let state = load_state(None);
            if let Some(entry) = state.recent_kbs.get(idx) {
                let _ = app.emit("menu-recent-kb", entry.path.clone());
            }
        }
    }
}
