use anyhow::{Context, Result};
use serde::{Deserialize, Serialize};
use std::path::PathBuf;

/// A single profile: an interface bound to a list of applications.
#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct Profile {
    pub name: String,
    pub interface: String,
    #[serde(default)]
    pub apps: Vec<String>,
}

/// Default accent color used to theme the UI.
fn default_accent() -> String {
    "#7C3AED".to_string()
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Config {
    #[serde(default, rename = "profiles")]
    pub profiles: Vec<Profile>,
    /// Accent color as a hex string, e.g. "#7C3AED".
    #[serde(default = "default_accent")]
    pub accent: String,
}

impl Default for Config {
    fn default() -> Self {
        Self {
            profiles: Vec::new(),
            accent: default_accent(),
        }
    }
}

impl Config {
    /// Path to ~/.config/netbinder/profiles.toml
    pub fn config_path() -> Result<PathBuf> {
        let mut dir = dirs::config_dir().context("could not determine config dir")?;
        dir.push("netbinder");
        std::fs::create_dir_all(&dir).context("could not create config dir")?;
        dir.push("profiles.toml");
        Ok(dir)
    }

    /// Load config from disk, returning an empty config if the file is missing.
    pub fn load() -> Result<Self> {
        let path = Self::config_path()?;
        if !path.exists() {
            return Ok(Self::default());
        }
        let raw = std::fs::read_to_string(&path)
            .with_context(|| format!("reading {}", path.display()))?;
        let cfg: Config = toml::from_str(&raw).context("parsing profiles.toml")?;
        Ok(cfg)
    }

    /// Persist config to disk.
    pub fn save(&self) -> Result<()> {
        let path = Self::config_path()?;
        let raw = toml::to_string_pretty(self).context("serializing config")?;
        std::fs::write(&path, raw).with_context(|| format!("writing {}", path.display()))?;
        Ok(())
    }

    pub fn find(&self, name: &str) -> Option<&Profile> {
        self.profiles.iter().find(|p| p.name == name)
    }

    pub fn remove(&mut self, name: &str) {
        self.profiles.retain(|p| p.name != name);
    }
}
