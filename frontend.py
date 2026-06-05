#!/usr/bin/env python3
"""
frontend.py - Wayland Traffic Splitter GUI
GTK4 + libadwaita application for Fedora / GNOME / Wayland
Provides custom color theming with live preview.
"""

import os
import sys
import json
import subprocess
import getpass
import logging
import threading

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw, GLib, Gdk, Gio

# --- Logging Configuration ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)-7s | [%(name)s] %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger("FRONTEND")

# --- Configuration File ---
CONFIG_DIR = os.path.expanduser("~/.config/traffic-splitter")
CONFIG_FILE = os.path.join(CONFIG_DIR, "settings.json")

DEFAULT_COLORS = {
    "bg_color": "#1a1b26",       # Dark background
    "headerbar_color": "#1a1b26", # Same as bg for unified look
    "fg_color": "#c0caf5",       # Light text
    "accent_color": "#7aa2f7",   # Blue accent
    "card_color": "#24283b",     # Slightly lighter cards
    "border_color": "#414868",   # Subtle borders
    "success_color": "#9ece6a",  # Green
    "warning_color": "#e0af68",  # Yellow/Orange
    "error_color": "#f7768e",    # Red/Pink
    "button_text": "#1a1b26",   # Dark text on accent buttons
}


def load_config():
    """Load saved color configuration."""
    try:
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, 'r') as f:
                saved = json.load(f)
                colors = DEFAULT_COLORS.copy()
                colors.update(saved)
                return colors
    except Exception as e:
        logger.warning(f"Failed to load config: {e}")
    return DEFAULT_COLORS.copy()


def save_config(colors):
    """Save color configuration to disk."""
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(CONFIG_FILE, 'w') as f:
            json.dump(colors, f, indent=2)
        logger.info("Configuration saved.")
    except Exception as e:
        logger.warning(f"Failed to save config: {e}")


# --- System Information ---
def get_net_interfaces():
    """Get list of active network interfaces."""
    logger.info("Scanning for network interfaces...")
    ifaces = []
    skip_prefixes = (
        'lo', 'br-', 'docker', 'veth',
        'virbr', 'tun', 'tap'
    )

    if os.path.exists('/sys/class/net/'):
        for name in os.listdir('/sys/class/net/'):
            if not any(name.startswith(p) for p in skip_prefixes):
                # Check if interface is UP
                operstate_path = f'/sys/class/net/{name}/operstate'
                try:
                    with open(operstate_path) as f:
                        state = f.read().strip()
                    if state == 'up':
                        ifaces.append(name)
                except FileNotFoundError:
                    ifaces.append(name)

    result = sorted(ifaces) if ifaces else ["eth0", "wlan0"]
    logger.info(f"Found interfaces: {result}")
    return result


def parse_desktop_files():
    """Parse .desktop files for installed applications."""
    logger.info("Parsing Desktop applications...")
    apps = {}
    dirs = [
        "/usr/share/applications",
        os.path.expanduser("~/.local/share/applications"),
        "/var/lib/flatpak/exports/share/applications",
        os.path.expanduser(
            "~/.local/share/flatpak/exports/share/applications"
        ),
    ]

    for d in dirs:
        if not os.path.exists(d):
            continue
        for filename in os.listdir(d):
            if not filename.endswith(".desktop"):
                continue
            filepath = os.path.join(d, filename)
            name = None
            exec_cmd = None
            no_display = False
            app_type = "Application"

            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    in_entry = False
                    for line in f:
                        line = line.strip()
                        if line == "[Desktop Entry]":
                            in_entry = True
                            continue
                        if line.startswith("[") and in_entry:
                            break
                        if not in_entry:
                            continue

                        if line.startswith("Name=") and not name:
                            name = line.split("=", 1)[1]
                        elif line.startswith("Exec=") and not exec_cmd:
                            raw = line.split("=", 1)[1]
                            # Remove field codes
                            clean = raw
                            for code in [
                                '%f', '%F', '%u', '%U',
                                '%d', '%D', '%n', '%N',
                                '%i', '%c', '%k', '%v', '%m'
                            ]:
                                clean = clean.replace(code, '')
                            exec_cmd = clean.strip()
                        elif line.startswith("NoDisplay="):
                            no_display = (
                                line.split("=", 1)[1].lower() == "true"
                            )
                        elif line.startswith("Type="):
                            app_type = line.split("=", 1)[1]

                if (
                    name and exec_cmd
                    and not no_display
                    and app_type == "Application"
                ):
                    apps[name] = exec_cmd

            except Exception:
                continue

    logger.info(f"Loaded {len(apps)} desktop applications.")
    return dict(sorted(apps.items()))


def parse_flatpak_apps():
    """Get list of installed Flatpak applications."""
    logger.info("Querying Flatpak applications...")
    apps = {}
    try:
        result = subprocess.run(
            ['flatpak', 'list', '--app', '--columns=name,application'],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            for line in result.stdout.strip().split('\n'):
                parts = line.split('\t')
                if len(parts) >= 2:
                    name = parts[0].strip()
                    app_id = parts[1].strip()
                    if name and app_id:
                        apps[name] = f"flatpak run {app_id}"
        logger.info(f"Loaded {len(apps)} Flatpak applications.")
    except FileNotFoundError:
        logger.info("Flatpak not installed.")
    except Exception as e:
        logger.warning(f"Flatpak query failed: {e}")
    return dict(sorted(apps.items()))


# --- Backend Controller ---
class BackendController:
    """Communicates with backend.py via pkexec."""

    def __init__(self):
        self.ns_name = "split_ns"
        self.backend_script = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "backend.py"
        )
        self.user = getpass.getuser()
        self.uid = str(os.getuid())
        self.wayland_disp = os.environ.get(
            'WAYLAND_DISPLAY', 'wayland-0'
        )
        self._active_iface = None

    def _run_backend(self, cmd_args, callback=None):
        """Run backend command via pkexec in a thread."""
        def worker():
            full_cmd = [
                "pkexec", "python3", self.backend_script
            ] + cmd_args

            logger.info(f"pkexec command: {' '.join(full_cmd)}")
            try:
                result = subprocess.run(
                    full_cmd,
                    capture_output=True, text=True,
                    timeout=60
                )
                success = result.returncode == 0
                if result.stdout:
                    logger.info(f"Backend stdout: {result.stdout}")
                if result.stderr:
                    logger.warning(f"Backend stderr: {result.stderr}")

                if callback:
                    GLib.idle_add(callback, success)

            except subprocess.TimeoutExpired:
                logger.error("Backend command timed out!")
                if callback:
                    GLib.idle_add(callback, False)
            except Exception as e:
                logger.error(f"Backend execution failed: {e}")
                if callback:
                    GLib.idle_add(callback, False)

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()

    def setup(self, interface, callback=None):
        """Setup namespace routing through interface."""
        self._active_iface = interface
        self._run_backend(
            ["setup", "--ns", self.ns_name, "--iface", interface],
            callback
        )

    def teardown(self, callback=None):
        """Teardown namespace."""
        iface = self._active_iface or "eth0"
        self._run_backend(
            ["teardown", "--ns", self.ns_name, "--iface", iface],
            callback
        )
        self._active_iface = None

    def launch(self, command):
        """Launch an app inside the namespace."""
        self._run_backend([
            "launch",
            "--ns", self.ns_name,
            "--user", self.user,
            "--uid", self.uid,
            "--display", self.wayland_disp,
            "--cmd", command
        ])


# --- Custom CSS Theming ---
def build_css(colors):
    """Generate GTK CSS from color dictionary."""
    return f"""
    /* ===== Window & Background ===== */
    window,
    .background {{
        background-color: {colors['bg_color']};
        color: {colors['fg_color']};
    }}

    /* ===== Headerbar - unified with body ===== */
    headerbar {{
        background-color: {colors['headerbar_color']};
        color: {colors['fg_color']};
        border-bottom: 1px solid {colors['border_color']};
        box-shadow: none;
    }}

    headerbar .title {{
        color: {colors['fg_color']};
    }}

    /* ===== Cards & List Rows ===== */
    .card,
    list,
    list > row,
    .boxed-list,
    .boxed-list > row {{
        background-color: {colors['card_color']};
        color: {colors['fg_color']};
        border-color: {colors['border_color']};
    }}

    list > row:hover,
    .boxed-list > row:hover {{
        background-color: alpha({colors['accent_color']}, 0.1);
    }}

    row.activatable:hover {{
        background-color: alpha({colors['accent_color']}, 0.08);
    }}

    /* ===== Accent Buttons ===== */
    button.suggested-action {{
        background-color: {colors['accent_color']};
        color: {colors['button_text']};
        border: none;
        border-radius: 8px;
        padding: 6px 16px;
        font-weight: bold;
    }}

    button.suggested-action:hover {{
        background-color: lighter({colors['accent_color']});
        box-shadow: 0 2px 6px alpha({colors['accent_color']}, 0.3);
    }}

    button.destructive-action {{
        background-color: {colors['error_color']};
        color: {colors['button_text']};
        border: none;
        border-radius: 8px;
    }}

    /* ===== Regular Buttons ===== */
    button {{
        background-color: {colors['card_color']};
        color: {colors['fg_color']};
        border: 1px solid {colors['border_color']};
        border-radius: 6px;
    }}

    button:hover {{
        background-color: alpha({colors['accent_color']}, 0.15);
        border-color: {colors['accent_color']};
    }}

    /* ===== Switch ===== */
    switch:checked {{
        background-color: {colors['accent_color']};
    }}

    switch:checked slider {{
        background-color: {colors['button_text']};
    }}

    /* ===== Search Entry ===== */
    searchentry,
    entry {{
        background-color: {colors['card_color']};
        color: {colors['fg_color']};
        border: 1px solid {colors['border_color']};
        border-radius: 8px;
        padding: 8px;
        caret-color: {colors['accent_color']};
    }}

    searchentry:focus,
    entry:focus {{
        border-color: {colors['accent_color']};
        box-shadow: 0 0 0 2px alpha({colors['accent_color']}, 0.25);
    }}

    /* ===== View Switcher (Tabs) ===== */
    viewswitcher > button {{
        background: transparent;
        color: alpha({colors['fg_color']}, 0.6);
        border: none;
    }}

    viewswitcher > button:checked {{
        color: {colors['accent_color']};
        border-bottom: 2px solid {colors['accent_color']};
    }}

    viewswitcherbar actionbar {{
        background-color: {colors['bg_color']};
        border-top: 1px solid {colors['border_color']};
    }}

    /* ===== Combo Row / Drop Down ===== */
    comborow,
    dropdown {{
        background-color: {colors['card_color']};
        color: {colors['fg_color']};
    }}

    /* ===== Scrollbar ===== */
    scrollbar slider {{
        background-color: alpha({colors['fg_color']}, 0.3);
        border-radius: 4px;
        min-width: 6px;
    }}

    scrollbar slider:hover {{
        background-color: alpha({colors['accent_color']}, 0.5);
    }}

    /* ===== Status Labels ===== */
    .status-active {{
        color: {colors['success_color']};
        font-weight: bold;
    }}

    .status-inactive {{
        color: alpha({colors['fg_color']}, 0.5);
    }}

    .status-error {{
        color: {colors['error_color']};
    }}

    /* ===== Info Bar ===== */
    .info-bar {{
        background-color: alpha({colors['accent_color']}, 0.1);
        border: 1px solid alpha({colors['accent_color']}, 0.3);
        border-radius: 8px;
        padding: 12px;
    }}

    /* ===== Section Title ===== */
    .section-title {{
        font-weight: bold;
        font-size: 1.1em;
        color: {colors['accent_color']};
    }}

    /* ===== Tooltip ===== */
    tooltip {{
        background-color: {colors['card_color']};
        color: {colors['fg_color']};
        border: 1px solid {colors['border_color']};
    }}

    /* ===== Popover ===== */
    popover,
    popover > contents {{
        background-color: {colors['card_color']};
        color: {colors['fg_color']};
        border-color: {colors['border_color']};
    }}

    /* ===== Color Button in Settings ===== */
    .color-preview {{
        border-radius: 50%;
        min-width: 32px;
        min-height: 32px;
        border: 2px solid {colors['border_color']};
    }}
    """


def apply_css(colors):
    """Apply CSS to the application."""
    css = build_css(colors)
    provider = Gtk.CssProvider()
    provider.load_from_data(css.encode())
    Gtk.StyleContext.add_provider_for_display(
        Gdk.Display.get_default(),
        provider,
        Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
    )
    return provider


# --- UI Components ---
class AppListPage(Gtk.Box):
    """Page showing searchable list of applications with launch buttons."""

    def __init__(self, controller, page_type="desktop"):
        super().__init__(
            orientation=Gtk.Orientation.VERTICAL, spacing=8
        )
        self.set_margin_top(12)
        self.set_margin_bottom(12)
        self.set_margin_start(12)
        self.set_margin_end(12)

        self.apps_dict = {}
        self.controller = controller
        self.page_type = page_type

        # Search bar
        search_box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=8
        )
        self.search_entry = Gtk.SearchEntry()
        self.search_entry.set_placeholder_text(
            f"Search {page_type} applications..."
        )
        self.search_entry.set_hexpand(True)
        self.search_entry.connect(
            "search-changed", self.on_search_changed
        )
        search_box.append(self.search_entry)

        self.count_label = Gtk.Label(label="0 apps")
        self.count_label.add_css_class("dim-label")
        search_box.append(self.count_label)

        self.append(search_box)

        # Scrolled list
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)
        scrolled.set_policy(
            Gtk.PolicyType.NEVER,
            Gtk.PolicyType.AUTOMATIC
        )

        self.list_box = Gtk.ListBox()
        self.list_box.set_selection_mode(Gtk.SelectionMode.NONE)
        self.list_box.add_css_class("boxed-list")
        scrolled.set_child(self.list_box)
        self.append(scrolled)

    def update_data(self, new_apps):
        """Refresh the application list."""
        self.apps_dict = new_apps
        self.search_entry.set_text("")
        self.populate(self.apps_dict)

    def populate(self, apps):
        """Fill the list with app rows."""
        # Remove all children
        while True:
            child = self.list_box.get_first_child()
            if child is None:
                break
            self.list_box.remove(child)

        self.count_label.set_text(f"{len(apps)} apps")

        for name, cmd in apps.items():
            row = Adw.ActionRow()
            row.set_title(name)
            row.set_subtitle(cmd)
            row.set_subtitle_lines(1)

            btn = Gtk.Button(label="Launch")
            btn.set_valign(Gtk.Align.CENTER)
            btn.add_css_class("suggested-action")
            btn.set_tooltip_text(f"Run '{name}' through isolated interface")
            btn.connect("clicked", self.on_launch, cmd, name)
            row.add_suffix(btn)

            self.list_box.append(row)

    def on_search_changed(self, entry):
        query = entry.get_text().lower()
        if not query:
            self.populate(self.apps_dict)
        else:
            filtered = {
                k: v for k, v in self.apps_dict.items()
                if query in k.lower() or query in v.lower()
            }
            self.populate(filtered)

    def on_launch(self, button, cmd, name):
        logger.info(f"Launching: {name} -> {cmd}")
        self.controller.launch(cmd)

        # Visual feedback
        button.set_label("✓ Launched")
        button.set_sensitive(False)
        GLib.timeout_add(2000, self._reset_button, button)

    def _reset_button(self, button):
        button.set_label("Launch")
        button.set_sensitive(True)
        return False


class ColorSettingsPage(Gtk.Box):
    """Settings page for customizing application colors."""

    COLOR_LABELS = {
        "bg_color": "Background / Titlebar",
        "headerbar_color": "Header Bar",
        "fg_color": "Text Color",
        "accent_color": "Accent / Buttons",
        "card_color": "Cards / Panels",
        "border_color": "Borders",
        "success_color": "Success Status",
        "warning_color": "Warning Status",
        "error_color": "Error / Stop",
        "button_text": "Button Text",
    }

    def __init__(self, colors, on_colors_changed):
        super().__init__(
            orientation=Gtk.Orientation.VERTICAL, spacing=8
        )
        self.set_margin_top(12)
        self.set_margin_bottom(12)
        self.set_margin_start(12)
        self.set_margin_end(12)

        self.colors = colors.copy()
        self.on_colors_changed = on_colors_changed
        self.color_buttons = {}

        # Title
        title = Gtk.Label(label="Color Customization")
        title.add_css_class("section-title")
        title.set_halign(Gtk.Align.START)
        self.append(title)

        subtitle = Gtk.Label(
            label="Click any color swatch to customize. "
                  "Titlebar and body will share the same color."
        )
        subtitle.set_halign(Gtk.Align.START)
        subtitle.add_css_class("dim-label")
        subtitle.set_wrap(True)
        self.append(subtitle)

        # Scrollable color list
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)
        scrolled.set_policy(
            Gtk.PolicyType.NEVER,
            Gtk.PolicyType.AUTOMATIC
        )

        color_list = Gtk.ListBox()
        color_list.set_selection_mode(Gtk.SelectionMode.NONE)
        color_list.add_css_class("boxed-list")

        for key, label in self.COLOR_LABELS.items():
            row = Adw.ActionRow()
            row.set_title(label)
            row.set_subtitle(self.colors.get(key, "#000000"))

            # Color button
            color_btn = Gtk.ColorButton()
            rgba = Gdk.RGBA()
            rgba.parse(self.colors.get(key, "#000000"))
            color_btn.set_rgba(rgba)
            color_btn.set_valign(Gtk.Align.CENTER)
            color_btn.connect(
                "color-set", self.on_color_picked, key, row
            )
            self.color_buttons[key] = color_btn

            row.add_suffix(color_btn)
            color_list.append(row)

        scrolled.set_child(color_list)
        self.append(scrolled)

        # Action buttons
        btn_box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=8
        )
        btn_box.set_halign(Gtk.Align.END)
        btn_box.set_margin_top(8)

        reset_btn = Gtk.Button(label="Reset to Defaults")
        reset_btn.add_css_class("destructive-action")
        reset_btn.connect("clicked", self.on_reset)
        btn_box.append(reset_btn)

        apply_btn = Gtk.Button(label="Save & Apply")
        apply_btn.add_css_class("suggested-action")
        apply_btn.connect("clicked", self.on_apply)
        btn_box.append(apply_btn)

        self.append(btn_box)

    def on_color_picked(self, btn, key, row):
        rgba = btn.get_rgba()
        hex_color = "#{:02x}{:02x}{:02x}".format(
            int(rgba.red * 255),
            int(rgba.green * 255),
            int(rgba.blue * 255)
        )
        self.colors[key] = hex_color
        row.set_subtitle(hex_color)

        # Keep bg and headerbar synced
        if key == "bg_color":
            self.colors["headerbar_color"] = hex_color
            if "headerbar_color" in self.color_buttons:
                hb_rgba = Gdk.RGBA()
                hb_rgba.parse(hex_color)
                self.color_buttons["headerbar_color"].set_rgba(hb_rgba)

        # Live preview
        self.on_colors_changed(self.colors, save=False)

    def on_apply(self, button):
        self.on_colors_changed(self.colors, save=True)

    def on_reset(self, button):
        self.colors = DEFAULT_COLORS.copy()
        # Update all color buttons
        for key, btn in self.color_buttons.items():
            rgba = Gdk.RGBA()
            rgba.parse(self.colors.get(key, "#000000"))
            btn.set_rgba(rgba)
        self.on_colors_changed(self.colors, save=True)


class MainWindow(Adw.ApplicationWindow):
    """Main application window."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.set_title("Traffic Splitter")
        self.set_default_size(720, 650)

        self.controller = BackendController()
        self.colors = load_config()
        self.css_provider = None
        self.is_active = False

        # Apply initial theme
        self.apply_theme(self.colors)

        # Main layout
        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.set_content(main_box)

        # Header bar
        header = Adw.HeaderBar()
        header.set_title_widget(
            Gtk.Label(label="Traffic Splitter")
        )
        main_box.append(header)

        # Status + switch box in header
        switch_box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=8
        )
        self.status_label = Gtk.Label(label="Inactive")
        self.status_label.add_css_class("status-inactive")
        switch_box.append(self.status_label)

        self.master_switch = Gtk.Switch()
        self.master_switch.set_valign(Gtk.Align.CENTER)
        self.master_switch.set_tooltip_text("Enable/Disable traffic isolation")
        self.master_switch.connect("state-set", self.on_switch_toggled)
        switch_box.append(self.master_switch)
        header.pack_end(switch_box)

        # Interface selector (using Adw.ComboRow in a listbox)
        iface_group = Adw.PreferencesGroup()
        iface_group.set_title("Network Configuration")
        iface_group.set_margin_start(12)
        iface_group.set_margin_end(12)
        iface_group.set_margin_top(8)

        self.iface_model = Gtk.StringList()
        self.interfaces = get_net_interfaces()
        for iface in self.interfaces:
            self.iface_model.append(iface)

        self.iface_combo = Adw.ComboRow()
        self.iface_combo.set_title("Target Interface")
        self.iface_combo.set_subtitle(
            "Applications will route traffic through this interface"
        )
        self.iface_combo.set_model(self.iface_model)
        iface_group.add(self.iface_combo)
        main_box.append(iface_group)

        # Info bar
        self.info_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=4
        )
        self.info_box.set_margin_start(12)
        self.info_box.set_margin_end(12)
        self.info_box.set_margin_top(8)
        self.info_box.add_css_class("info-bar")

        self.info_label = Gtk.Label()
        self.info_label.set_wrap(True)
        self.info_label.set_markup(
            "Toggle the switch to start traffic isolation. "
            "Select which network interface to route app traffic through."
        )
        self.info_box.append(self.info_label)
        main_box.append(self.info_box)

        # View stack (tabs)
        stack_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        stack_box.set_vexpand(True)

        self.view_stack = Adw.ViewStack()
        self.view_stack.set_vexpand(True)

        # View Switcher in header
        switcher = Adw.ViewSwitcher()
        switcher.set_stack(self.view_stack)
        switcher.set_policy(Adw.ViewSwitcherPolicy.WIDE)
        header.set_title_widget(switcher)

        # Bottom switcher bar for narrow mode
        switcher_bar = Adw.ViewSwitcherBar()
        switcher_bar.set_stack(self.view_stack)
        switcher_bar.set_reveal(True)

        stack_box.append(self.view_stack)
        stack_box.append(switcher_bar)
        main_box.append(stack_box)

        # Desktop Apps page
        self.desktop_page = AppListPage(self.controller, "desktop")
        self.view_stack.add_titled_with_icon(
            self.desktop_page, "desktop",
            "Desktop Apps", "application-x-executable-symbolic"
        )

        # Flatpak Apps page
        self.flatpak_page = AppListPage(self.controller, "flatpak")
        self.view_stack.add_titled_with_icon(
            self.flatpak_page, "flatpak",
            "Flatpak Apps", "system-component-flatpak-symbolic"
        )

        # CLI page
        cli_page = self._build_cli_page()
        self.view_stack.add_titled_with_icon(
            cli_page, "cli",
            "Custom Command", "utilities-terminal-symbolic"
        )

        # Settings page
        self.settings_page = ColorSettingsPage(
            self.colors, self.on_colors_changed
        )
        self.view_stack.add_titled_with_icon(
            self.settings_page, "settings",
            "Appearance", "preferences-desktop-appearance-symbolic"
        )

        # Initially disable app pages
        self.set_pages_sensitive(False)

        logger.info("Application window initialized.")

    def _build_cli_page(self):
        """Build the custom command page."""
        page = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=12
        )
        page.set_margin_top(20)
        page.set_margin_bottom(20)
        page.set_margin_start(20)
        page.set_margin_end(20)

        # Title
        title = Gtk.Label(label="Run Custom Command")
        title.add_css_class("section-title")
        title.set_halign(Gtk.Align.START)
        page.append(title)

        desc = Gtk.Label(
            label="Execute any command through the isolated "
                  "network interface. The command will run in "
                  "a separate network namespace."
        )
        desc.set_halign(Gtk.Align.START)
        desc.set_wrap(True)
        desc.add_css_class("dim-label")
        page.append(desc)

        # Command entry
        entry_box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=8
        )
        entry_box.set_margin_top(12)

        self.cli_entry = Gtk.Entry()
        self.cli_entry.set_placeholder_text(
            "e.g., firefox, wget https://example.com, steam"
        )
        self.cli_entry.set_hexpand(True)
        self.cli_entry.connect("activate", self.on_cli_activate)
        entry_box.append(self.cli_entry)

        cli_btn = Gtk.Button(label="Run")
        cli_btn.add_css_class("suggested-action")
        cli_btn.connect("clicked", self.on_cli_launch)
        entry_box.append(cli_btn)

        page.append(entry_box)

        # Quick launch buttons
        quick_label = Gtk.Label(label="Quick Launch:")
        quick_label.set_halign(Gtk.Align.START)
        quick_label.set_margin_top(20)
        quick_label.add_css_class("section-title")
        page.append(quick_label)

        quick_box = Gtk.FlowBox()
        quick_box.set_selection_mode(Gtk.SelectionMode.NONE)
        quick_box.set_max_children_per_line(4)
        quick_box.set_column_spacing(8)
        quick_box.set_row_spacing(8)

        quick_apps = [
            ("Firefox", "firefox"),
            ("Chrome", "google-chrome-stable"),
            ("Steam", "steam"),
            ("Terminal", "gnome-terminal"),
        ]

        for name, cmd in quick_apps:
            btn = Gtk.Button(label=name)
            btn.set_tooltip_text(f"Launch {name} in isolated network")
            btn.connect("clicked", self._quick_launch, cmd)
            quick_box.append(btn)

        page.append(quick_box)

        return page

    def _quick_launch(self, button, cmd):
        logger.info(f"Quick launch: {cmd}")
        self.controller.launch(cmd)

    def set_pages_sensitive(self, sensitive):
        """Enable/disable app pages."""
        self.desktop_page.set_sensitive(sensitive)
        self.flatpak_page.set_sensitive(sensitive)

    def apply_theme(self, colors):
        """Apply color theme to the application."""
        if self.css_provider:
            Gtk.StyleContext.remove_provider_for_display(
                Gdk.Display.get_default(),
                self.css_provider
            )
        self.css_provider = apply_css(colors)

    def on_colors_changed(self, new_colors, save=False):
        """Handle color changes from settings page."""
        self.colors = new_colors.copy()
        self.apply_theme(self.colors)
        if save:
            save_config(self.colors)
            logger.info("Theme saved and applied.")

    def on_switch_toggled(self, switch, state):
        """Handle master switch toggle."""
        selected_idx = self.iface_combo.get_selected()
        if selected_idx == Gtk.INVALID_LIST_POSITION:
            selected_idx = 0
        interface = self.iface_model.get_string(selected_idx)

        if state:
            logger.info(
                f"Enabling isolation on interface: {interface}"
            )
            self.status_label.set_text("Setting up...")
            self.status_label.set_css_classes(["status-inactive"])
            self.master_switch.set_sensitive(False)
            self.iface_combo.set_sensitive(False)

            self.controller.setup(interface, self._on_setup_done)
        else:
            logger.info("Disabling isolation...")
            self.status_label.set_text("Shutting down...")
            self.master_switch.set_sensitive(False)

            self.controller.teardown(self._on_teardown_done)

        return False

    def _on_setup_done(self, success):
        """Called when backend setup completes."""
        self.master_switch.set_sensitive(True)

        if success:
            self.is_active = True
            self.status_label.set_text("● Active")
            self.status_label.set_css_classes(["status-active"])
            self.info_label.set_markup(
                "<b>Isolation active!</b> Launch applications "
                "below to route their traffic through the "
                "selected interface."
            )

            # Load apps in background
            def load_apps():
                desktop = parse_desktop_files()
                flatpak = parse_flatpak_apps()
                GLib.idle_add(self.desktop_page.update_data, desktop)
                GLib.idle_add(self.flatpak_page.update_data, flatpak)
                GLib.idle_add(self.set_pages_sensitive, True)

            thread = threading.Thread(target=load_apps, daemon=True)
            thread.start()
        else:
            self.is_active = False
            self.master_switch.set_active(False)
            self.iface_combo.set_sensitive(True)
            self.status_label.set_text("Setup Failed")
            self.status_label.set_css_classes(["status-error"])
            self.info_label.set_markup(
                "<b>Setup failed.</b> Check that the selected "
                "interface is active and you authorized the "
                "root access prompt."
            )

    def _on_teardown_done(self, success):
        """Called when backend teardown completes."""
        self.is_active = False
        self.master_switch.set_sensitive(True)
        self.iface_combo.set_sensitive(True)
        self.set_pages_sensitive(False)
        self.status_label.set_text("Inactive")
        self.status_label.set_css_classes(["status-inactive"])
        self.info_label.set_markup(
            "Toggle the switch to start traffic isolation."
        )

    def on_cli_activate(self, entry):
        """Handle Enter key in CLI entry."""
        self.on_cli_launch(None)

    def on_cli_launch(self, button):
        """Launch custom CLI command."""
        cmd = self.cli_entry.get_text().strip()
        if cmd:
            logger.info(f"CLI command: {cmd}")
            self.controller.launch(cmd)
            self.cli_entry.set_text("")


class TrafficSplitterApp(Adw.Application):
    """Main application class."""

    def __init__(self):
        super().__init__(
            application_id="com.codez.trafficsplitter",
            flags=Gio.ApplicationFlags.FLAGS_NONE
        )

    def do_activate(self):
        win = self.get_active_window()
        if not win:
            win = MainWindow(application=self)
        win.present()


# --- Entry Point ---
if __name__ == '__main__':
    app = TrafficSplitterApp()
    app.run(sys.argv)
