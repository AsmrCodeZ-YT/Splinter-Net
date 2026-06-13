# Splinter Net

Assign network interfaces (for example two USB modems from two phones) to
specific applications on Linux, with full traffic isolation, using **Rust** and
**egui**.

Each profile maps one network interface to a list of applications. When you run
an application from a profile, it is launched inside a dedicated Linux **network
namespace** that owns that interface, so its traffic cannot leak through any
other interface, and other applications cannot use the bound interface.

Works with regular desktop apps, Flatpak, Snap, AppImage and CLI tools
(`wget`, `curl`, ...). Multiple applications in the same profile share one
namespace and therefore one interface.

## How it works

For each profile a persistent namespace `splinter_<name>` is created:

1. The selected interface is moved into the namespace:
   `ip link set <iface> netns splinter_<name>`
2. Loopback and the interface are brought up, `dhclient` obtains an address.
3. The default gateway is detected and written to
   `/etc/netns/splinter_<name>/resolv.conf` for DNS.
4. Applications are launched inside the namespace and dropped back to your
   normal user with `runuser`, so they do not run as root.

Every privileged step (namespace creation, moving the interface, DHCP, DNS and
entering the namespace to launch an app) runs through **`pkexec`**, which shows
a graphical password prompt. **No manual configuration is required**: there is
no `sudoers` editing, no setuid binary and no host setup. When an action needs
root, the app simply asks for your password through the standard PolicyKit
dialog.

## Requirements

- `iproute2` (`ip`)
- `dhclient`
- `pkexec` (PolicyKit) — ships by default on all major desktop distros
- A Rust toolchain (`cargo`) to build

## Build & run

```sh
cargo build --release
./target/release/splinter-net
```

## Using the app

- **Profiles tab**: create a profile (name + interface) and add applications to
  it. Creating a profile asks for your password once (via pkexec) to set up the
  namespace.
- **Applications tab**: all installed apps (desktop, Flatpak, Snap) are
  discovered automatically. Search by name, pick a profile next to an app and
  press **Launch**.
- **Script tab**: run any command or shell script inside a chosen profile.
- **Settings tab**: set a custom accent color (hex) to recolor the whole app.

## Configuration

Profiles are stored in `~/.config/netbinder/profiles.toml`:

```toml
[[profiles]]
name = "YouTube"
interface = "usb1"
apps = ["/usr/bin/firefox", "/usr/bin/mpv"]
```

## Notes & caveats

- Moving an interface into a namespace disconnects it from the default
  namespace. This is the intended isolation.
- Audio: `PULSE_SERVER` is forwarded (PipeWire is usually compatible).
- Wayland: `WAYLAND_DISPLAY` and `XDG_RUNTIME_DIR` are forwarded.
- If the app crashes, namespaces persist; delete the profile to clean up.
- Flatpak/Snap: use the executable path; `flatpak run` / `snap run` spawn their
  children in the same namespace.

