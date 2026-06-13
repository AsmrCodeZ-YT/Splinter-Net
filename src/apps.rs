//! Discovery of installed applications via freedesktop `.desktop` entries.
//!
//! Scans the standard application directories (system, user, Flatpak and
//! Snap exports), parses the `[Desktop Entry]` group and produces a sorted,
//! de-duplicated list of launchable applications.

use std::collections::BTreeMap;
use std::path::PathBuf;

/// A discovered desktop application.
#[derive(Debug, Clone)]
#[allow(dead_code)]
pub struct DesktopApp {
    /// Human readable name (from `Name=`).
    pub name: String,
    /// Command to execute (from `Exec=`, with field codes stripped).
    pub exec: String,
    /// Whether the app wants a terminal (`Terminal=true`).
    pub terminal: bool,
}

impl DesktopApp {
    /// Split the exec string into a program plus its arguments.
    pub fn command_and_args(&self) -> (String, Vec<String>) {
        let mut parts = self.exec.split_whitespace().map(|s| s.to_string());
        let cmd = parts.next().unwrap_or_default();
        let args: Vec<String> = parts.collect();
        (cmd, args)
    }
}

/// Return the directories that may contain `.desktop` files.
fn application_dirs() -> Vec<PathBuf> {
    let mut dirs = vec![
        PathBuf::from("/usr/share/applications"),
        PathBuf::from("/usr/local/share/applications"),
        PathBuf::from("/var/lib/flatpak/exports/share/applications"),
        PathBuf::from("/var/lib/snapd/desktop/applications"),
    ];
    if let Some(home) = dirs::data_dir() {
        dirs.push(home.join("applications"));
        dirs.push(home.join("flatpak/exports/share/applications"));
    }
    dirs
}

/// Remove desktop entry field codes such as %u %U %f %F %i %c %k.
fn strip_field_codes(exec: &str) -> String {
    let mut out = String::with_capacity(exec.len());
    let mut chars = exec.chars().peekable();
    while let Some(c) = chars.next() {
        if c == '%' {
            // Skip the field code character that follows '%'. A literal
            // percent is written as "%%".
            if let Some(&next) = chars.peek() {
                if next == '%' {
                    out.push('%');
                }
                chars.next();
            }
        } else {
            out.push(c);
        }
    }
    out.split_whitespace().collect::<Vec<_>>().join(" ")
}

/// Parse a single `.desktop` file's `[Desktop Entry]` group.
fn parse_desktop_file(contents: &str) -> Option<DesktopApp> {
    let mut in_entry = false;
    let mut name = None;
    let mut exec = None;
    let mut typ = None;
    let mut no_display = false;
    let mut hidden = false;
    let mut terminal = false;

    for line in contents.lines() {
        let line = line.trim();
        if line.starts_with('[') && line.ends_with(']') {
            in_entry = line == "[Desktop Entry]";
            continue;
        }
        if !in_entry {
            continue;
        }
        let Some((key, value)) = line.split_once('=') else {
            continue;
        };
        let key = key.trim();
        let value = value.trim();
        match key {
            "Name" if name.is_none() => name = Some(value.to_string()),
            "Exec" if exec.is_none() => exec = Some(value.to_string()),
            "Type" => typ = Some(value.to_string()),
            "NoDisplay" => no_display = value.eq_ignore_ascii_case("true"),
            "Hidden" => hidden = value.eq_ignore_ascii_case("true"),
            "Terminal" => terminal = value.eq_ignore_ascii_case("true"),
            _ => {}
        }
    }

    if no_display || hidden {
        return None;
    }
    if let Some(t) = &typ {
        if t != "Application" {
            return None;
        }
    }

    let name = name?;
    let exec_raw = exec?;
    let exec = strip_field_codes(&exec_raw);
    if exec.is_empty() {
        return None;
    }

    Some(DesktopApp {
        name,
        exec,
        terminal,
    })
}

/// Scan all application directories and return a sorted, de-duplicated list.
pub fn discover_apps() -> Vec<DesktopApp> {
    // Key by lowercased name to de-duplicate; BTreeMap keeps it sorted.
    let mut found: BTreeMap<String, DesktopApp> = BTreeMap::new();

    for dir in application_dirs() {
        let entries = match std::fs::read_dir(&dir) {
            Ok(e) => e,
            Err(_) => continue,
        };
        for entry in entries.flatten() {
            let path = entry.path();
            if path.extension().and_then(|e| e.to_str()) != Some("desktop") {
                continue;
            }
            let Ok(contents) = std::fs::read_to_string(&path) else {
                continue;
            };
            if let Some(app) = parse_desktop_file(&contents) {
                found.entry(app.name.to_lowercase()).or_insert(app);
            }
        }
    }

    found.into_values().collect()
}
