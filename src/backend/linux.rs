//! Linux implementation of [`InterfaceBinder`] using network namespaces.
//!
//! Every privileged operation (namespace creation, moving the interface,
//! DHCP, DNS and launching applications) runs through `pkexec`, which shows a
//! graphical password prompt. No manual `sudoers` editing, setuid binaries or
//! other host configuration is required.

use super::{Dependencies, InterfaceBinder, InterfaceInfo};
use anyhow::{anyhow, bail, Context, Result};
use serde::Deserialize;
use std::process::{Child, Command};

pub struct LinuxBinder;

/// Subset of `ip -j addr` output we care about.
#[derive(Debug, Deserialize)]
struct IpAddrEntry {
    ifname: String,
    #[serde(default)]
    link_type: String,
    #[serde(default)]
    operstate: String,
    #[serde(default)]
    addr_info: Vec<IpAddrInfo>,
}

#[derive(Debug, Deserialize)]
struct IpAddrInfo {
    family: String,
    local: String,
}

fn which(tool: &str) -> bool {
    Command::new("sh")
        .arg("-c")
        .arg(format!("command -v {tool}"))
        .output()
        .map(|o| o.status.success())
        .unwrap_or(false)
}

/// Read a session environment variable, returning empty string if unset.
fn env_or_empty(key: &str) -> String {
    std::env::var(key).unwrap_or_default()
}

/// Build the list of `KEY=VALUE` strings to forward into the namespace.
fn forwarded_env() -> Vec<String> {
    let keys = [
        "DISPLAY",
        "XAUTHORITY",
        "WAYLAND_DISPLAY",
        "XDG_RUNTIME_DIR",
        "PULSE_SERVER",
        "DBUS_SESSION_BUS_ADDRESS",
        "HOME",
        "LANG",
    ];
    let mut out = Vec::new();
    for k in keys {
        let v = env_or_empty(k);
        if !v.is_empty() {
            out.push(format!("{k}={v}"));
        }
    }
    // Default PULSE_SERVER to the user's runtime socket if not set.
    if !out.iter().any(|e| e.starts_with("PULSE_SERVER=")) {
        let xdg = env_or_empty("XDG_RUNTIME_DIR");
        if !xdg.is_empty() {
            out.push(format!("PULSE_SERVER=unix:{xdg}/pulse/native"));
        }
    }
    out
}

fn current_user() -> String {
    std::env::var("USER")
        .or_else(|_| std::env::var("LOGNAME"))
        .unwrap_or_else(|_| "root".to_string())
}

/// Validate a profile/namespace name to avoid shell injection in privileged
/// scripts. Only allow a conservative character set.
fn validate_name(name: &str) -> Result<()> {
    if name.is_empty() || name.len() > 64 {
        bail!("profile name must be 1..=64 characters");
    }
    if !name
        .chars()
        .all(|c| c.is_ascii_alphanumeric() || c == '_' || c == '-')
    {
        bail!("profile name may only contain letters, digits, '_' and '-'");
    }
    Ok(())
}

fn validate_iface(iface: &str) -> Result<()> {
    if iface.is_empty() || iface.len() > 32 {
        bail!("interface name invalid");
    }
    if !iface
        .chars()
        .all(|c| c.is_ascii_alphanumeric() || c == '_' || c == '-' || c == '.' || c == ':')
    {
        bail!("interface name contains invalid characters");
    }
    Ok(())
}

/// The namespace name derived from a profile name.
fn netns_name(profile: &str) -> String {
    format!("splinter_{profile}")
}

/// Single-quote a string for safe use inside a `sh -c` command line.
fn shell_quote(s: &str) -> String {
    // Wrap in single quotes; escape embedded single quotes as '\''.
    let escaped = s.replace('\'', "'\\''");
    format!("'{escaped}'")
}

/// Run a privileged shell script via pkexec, which shows a graphical prompt.
fn run_privileged_script(script: &str) -> Result<()> {
    if !which("pkexec") {
        bail!("pkexec (PolicyKit) is required but was not found");
    }

    let output = Command::new("pkexec")
        .arg("sh")
        .arg("-c")
        .arg(script)
        .output()
        .context("failed to spawn pkexec")?;

    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        let msg = stderr.trim();
        if msg.is_empty() {
            bail!("privileged command was cancelled or failed");
        }
        bail!("privileged command failed: {msg}");
    }
    Ok(())
}

impl InterfaceBinder for LinuxBinder {
    fn list_interfaces() -> Vec<InterfaceInfo> {
        let output = match Command::new("ip").args(["-j", "addr"]).output() {
            Ok(o) if o.status.success() => o.stdout,
            _ => return Vec::new(),
        };

        let entries: Vec<IpAddrEntry> = match serde_json::from_slice(&output) {
            Ok(e) => e,
            Err(_) => return Vec::new(),
        };

        entries
            .into_iter()
            .map(|e| {
                let ip = e
                    .addr_info
                    .iter()
                    .find(|a| a.family == "inet")
                    .map(|a| a.local.clone());
                InterfaceInfo {
                    name: e.ifname,
                    kind: if e.link_type.is_empty() {
                        "unknown".to_string()
                    } else {
                        e.link_type
                    },
                    ip,
                    state: if e.operstate.is_empty() {
                        "unknown".to_string()
                    } else {
                        e.operstate.to_lowercase()
                    },
                }
            })
            .collect()
    }

    fn create_profile(name: &str, iface: &str) -> Result<()> {
        validate_name(name)?;
        validate_iface(iface)?;
        let ns = netns_name(name);

        // One privileged script does everything that needs root:
        //  1. create the namespace (idempotent)
        //  2. move the interface into it
        //  3. bring up loopback + interface
        //  4. run dhclient to obtain an address
        //  5. derive the gateway and write a per-namespace resolv.conf
        let script = format!(
            r#"set -e
NS="{ns}"
IFACE="{iface}"
ip netns add "$NS" 2>/dev/null || true
ip link set "$IFACE" netns "$NS"
ip netns exec "$NS" ip link set lo up
ip netns exec "$NS" ip link set "$IFACE" up
ip netns exec "$NS" dhclient -v "$IFACE" || true
mkdir -p "/etc/netns/$NS"
GW=$(ip netns exec "$NS" ip route show default 2>/dev/null | awk '/default/ {{print $3; exit}}')
if [ -n "$GW" ]; then
  echo "nameserver $GW" > "/etc/netns/$NS/resolv.conf"
else
  echo "nameserver 192.168.42.129" > "/etc/netns/$NS/resolv.conf"
fi
"#
        );

        run_privileged_script(&script)
            .with_context(|| format!("creating profile '{name}' on '{iface}'"))
    }

    fn delete_profile(name: &str) -> Result<()> {
        validate_name(name)?;
        let ns = netns_name(name);

        // Deleting the namespace returns moved interfaces to the default
        // namespace automatically. Also clean up the resolv.conf dir.
        let script = format!(
            r#"set -e
NS="{ns}"
ip netns delete "$NS" 2>/dev/null || true
rm -rf "/etc/netns/$NS"
"#
        );

        run_privileged_script(&script)
            .with_context(|| format!("deleting profile '{name}'"))
    }

    fn launch_in_profile(profile: &str, command: &str, args: &[String]) -> Result<Child> {
        validate_name(profile)?;
        let ns = netns_name(profile);
        let user = current_user();
        let envs = forwarded_env();

        // Entering a root-owned namespace needs privilege, so we use `pkexec`
        // which shows a GRAPHICAL password prompt. We never fall back to
        // `sudo`, because sudo would block on the controlling terminal (the
        // GUI cannot see it, so the launch appears to do nothing while sudo
        // silently waits for a password). No sudoers rule or other manual host
        // configuration is required.
        //
        // Inside the namespace we immediately drop back to the regular user
        // with `runuser` (no extra password, since the parent is already
        // root), so the launched application does not run as root.
        if !which("pkexec") {
            bail!("pkexec (PolicyKit) is required to launch applications but was not found. Install the 'polkit' package.");
        }
        if !which("runuser") {
            bail!("runuser (util-linux) is required to launch applications but was not found.");
        }

        // Command run as root by pkexec:
        //   ip netns exec <ns> runuser -u <user> -- env <ENV...> <cmd> <args...>
        let mut parts: Vec<String> = vec![
            "ip".to_string(),
            "netns".to_string(),
            "exec".to_string(),
            ns.clone(),
            "runuser".to_string(),
            "-u".to_string(),
            user.clone(),
            "--".to_string(),
            "env".to_string(),
        ];
        parts.extend(envs);
        parts.push(command.to_string());
        parts.extend(args.iter().cloned());

        let quoted = parts
            .iter()
            .map(|p| shell_quote(p))
            .collect::<Vec<_>>()
            .join(" ");

        // `pkexec` keeps a graphical agent; we pass DISPLAY/WAYLAND through so
        // the launched GUI app can reach the user's session.
        let mut cmd = Command::new("pkexec");
        cmd.arg("sh").arg("-c").arg(&quoted);
        // Ensure pkexec can find a graphical auth agent.
        if let Ok(display) = std::env::var("DISPLAY") {
            cmd.env("DISPLAY", display);
        }
        if let Ok(xauth) = std::env::var("XAUTHORITY") {
            cmd.env("XAUTHORITY", xauth);
        }

        cmd.spawn()
            .with_context(|| format!("launching '{command}' in namespace '{ns}'"))
            .map_err(|e| anyhow!(e))
    }

    fn check_dependencies() -> Dependencies {
        Dependencies {
            ip: which("ip"),
            dhclient: which("dhclient"),
            pkexec: which("pkexec"),
        }
    }
}
