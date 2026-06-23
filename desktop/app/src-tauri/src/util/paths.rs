//! XDG config paths for the desktop shell.

use std::path::PathBuf;

pub const APP_DIRNAME: &str = "vouch-desktop";
pub const STATE_FILENAME: &str = "state.json";

pub fn config_dir() -> PathBuf {
    dirs::config_dir()
        .unwrap_or_else(|| dirs::home_dir().unwrap_or_default().join(".config"))
        .join(APP_DIRNAME)
}

pub fn state_file_path() -> PathBuf {
    config_dir().join(STATE_FILENAME)
}

pub fn ensure_config_dir() -> std::io::Result<PathBuf> {
    let dir = config_dir();
    std::fs::create_dir_all(&dir)?;
    Ok(dir)
}
