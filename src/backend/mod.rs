//! Backend abstraction for binding network interfaces to applications.
//!
//! The [`InterfaceBinder`] trait defines the operations the GUI relies on.
//! A concrete implementation is provided for Linux (network namespaces).

use anyhow::Result;
use std::process::Child;

#[cfg(target_os = "linux")]
pub mod linux;

#[cfg(target_os = "linux")]
pub use linux::LinuxBinder as PlatformBinder;

/// Information about a network interface as shown to the user.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct InterfaceInfo {
    pub name: String,
    /// e.g. "ether", "loopback". Best-effort.
    pub kind: String,
    /// First IPv4 address if any.
    pub ip: Option<String>,
    /// Operational state, e.g. "up", "down", "unknown".
    pub state: String,
}

/// Result of probing the host for required external tools.
#[derive(Debug, Clone, Default)]
pub struct Dependencies {
    pub ip: bool,
    pub dhclient: bool,
    pub pkexec: bool,
}

impl Dependencies {
    pub fn all_ok(&self) -> bool {
        self.ip && self.dhclient && self.pkexec
    }

    pub fn missing(&self) -> Vec<&'static str> {
        let mut m = Vec::new();
        if !self.ip {
            m.push("ip (iproute2)");
        }
        if !self.dhclient {
            m.push("dhclient");
        }
        if !self.pkexec {
            m.push("pkexec (PolicyKit)");
        }
        m
    }
}

/// Core operations needed to bind interfaces to applications.
pub trait InterfaceBinder {
    /// List network interfaces visible in the default namespace.
    fn list_interfaces() -> Vec<InterfaceInfo>;

    /// Create a persistent namespace for `name` and move `iface` into it,
    /// then configure loopback, DHCP and DNS. Requires privilege escalation.
    fn create_profile(name: &str, iface: &str) -> Result<()>;

    /// Tear down the namespace for `name`, returning the interface to the
    /// default namespace.
    fn delete_profile(name: &str) -> Result<()>;

    /// Launch `command` (with `args`) inside the profile's namespace as the
    /// current user, forwarding the GUI/session environment.
    fn launch_in_profile(profile: &str, command: &str, args: &[String]) -> Result<Child>;

    /// Verify required external tools are present.
    fn check_dependencies() -> Dependencies;
}
