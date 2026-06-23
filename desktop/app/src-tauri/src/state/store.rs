//! Recent-KB persistence (`~/.config/vouch-desktop/state.json`).

use chrono::Utc;
use serde::{Deserialize, Serialize};
use std::path::{Path, PathBuf};

use crate::util::paths::{ensure_config_dir, state_file_path};

pub const STATE_VERSION: i32 = 1;
pub const MAX_RECENT: usize = 5;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RecentKbEntry {
    pub path: String,
    pub label: String,
    pub opened_at: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DesktopState {
    pub version: i32,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub last_kb: Option<String>,
    pub recent_kbs: Vec<RecentKbEntry>,
}

impl Default for DesktopState {
    fn default() -> Self {
        Self {
            version: STATE_VERSION,
            last_kb: None,
            recent_kbs: Vec::new(),
        }
    }
}

pub fn load_state(path: Option<&Path>) -> DesktopState {
    let target = path.map(PathBuf::from).unwrap_or_else(state_file_path);
    if !target.is_file() {
        return DesktopState::default();
    }
    let text = match std::fs::read_to_string(&target) {
        Ok(t) => t,
        Err(_) => return DesktopState::default(),
    };
    serde_json::from_str(&text).unwrap_or_default()
}

pub fn save_state(state: &DesktopState, path: Option<&Path>) -> Result<PathBuf, String> {
    let target = path.map(PathBuf::from).unwrap_or_else(state_file_path);
    ensure_config_dir().map_err(|e| e.to_string())?;
    let tmp = target.with_extension("tmp");
    let payload = serde_json::to_string_pretty(state).map_err(|e| e.to_string())?;
    std::fs::write(&tmp, format!("{payload}\n")).map_err(|e| e.to_string())?;
    std::fs::rename(&tmp, &target).map_err(|e| e.to_string())?;
    Ok(target)
}

pub fn label_for_path(path: &str) -> String {
    Path::new(path)
        .file_name()
        .and_then(|s| s.to_str())
        .unwrap_or(path)
        .to_string()
}

pub fn touch_recent_kb(
    project_root: &str,
    label: Option<&str>,
    state: Option<DesktopState>,
) -> Result<DesktopState, String> {
    let root = Path::new(project_root)
        .canonicalize()
        .map_err(|e| e.to_string())?
        .to_string_lossy()
        .to_string();
    let entry_label = label
        .map(str::to_string)
        .unwrap_or_else(|| label_for_path(&root));
    let mut base = state.unwrap_or_else(|| load_state(None));
    base.recent_kbs
        .retain(|e| !paths_equal(&e.path, &root));
    base.recent_kbs.insert(
        0,
        RecentKbEntry {
            path: root.clone(),
            label: entry_label,
            opened_at: Utc::now().to_rfc3339(),
        },
    );
    base.recent_kbs.truncate(MAX_RECENT);
    base.last_kb = Some(root);
    base.version = STATE_VERSION;
    save_state(&base, None)?;
    Ok(base)
}

fn paths_equal(a: &str, b: &str) -> bool {
    Path::new(a)
        .canonicalize()
        .ok()
        .zip(Path::new(b).canonicalize().ok())
        .map(|(x, y)| x == y)
        .unwrap_or_else(|| a.eq_ignore_ascii_case(b))
}
