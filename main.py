import os
import subprocess
import getpass
import logging
import gi

gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw, GLib

# --- Logging Configuration ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)-7s | [%(name)s] %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger("FRONTEND")

# --- System Information Extractors ---
def get_net_interfaces():
    logger.info("Scanning for active network interfaces...")
    ifaces = []
    if os.path.exists('/sys/class/net/'):
        for f in os.listdir('/sys/class/net/'):
            # Filter out loopback, docker, and bridge interfaces
            if f != 'lo' and not f.startswith('br-') and not f.startswith('docker') and not f.startswith('veth'):
                ifaces.append(f)
    
    result = sorted(list(set(ifaces))) if ifaces else ["eth0", "wlan0"]
    logger.info(f"Found interfaces: {result}")
    return result

def parse_desktop_files():
    logger.info("Parsing installed Desktop applications...")
    apps = {}
    dirs = [
        "/usr/share/applications",
        os.path.expanduser("~/.local/share/applications")
    ]
    for d in dirs:
        if not os.path.exists(d): continue
        for f in os.listdir(d):
            if f.endswith(".desktop"):
                filepath = os.path.join(d, f)
                name, exec_cmd = None, None
                try:
                    with open(filepath, 'r', encoding='utf-8') as file:
                        for line in file:
                            line = line.strip()
                            if line.startswith("Name=") and not name:
                                name = line.split("=", 1)[1]
                            elif line.startswith("Exec=") and not exec_cmd:
                                exec_cmd = line.split("=", 1)[1].split(" %")[0]
                    
                    if name and exec_cmd:
                        apps[name] = exec_cmd
                except Exception:
                    pass
    logger.info(f"Successfully loaded {len(apps)} Desktop applications.")
    return dict(sorted(apps.items()))

def parse_flatpak_apps():
    logger.info("Querying Flatpak applications via CLI...")
    apps = {}
    try:
        # Run flatpak list to get name and app ID
        cmd = ['flatpak', 'list', '--app', '--columns=name,application']
        logger.info(f"Executing: {' '.join(cmd)}")
        result = subprocess.run(cmd, capture_output=True, text=True)
        
        lines = result.stdout.strip().split('\n')
        for line in lines:
            parts = line.split('\t')
            if len(parts) >= 2:
                name = parts[0].strip()
                app_id = parts[1].strip()
                if name and app_id:
                    apps[name] = f"flatpak run {app_id}"
                    
        logger.info(f"Successfully loaded {len(apps)} Flatpak applications.")
    except Exception as e:
        logger.warning(f"Failed to query Flatpak apps: {e}")
    return dict(sorted(apps.items()))

# --- Backend Communicator ---
class BackendController:
    def __init__(self):
        self.ns_name = "split_ns"
        self.backend_script = os.path.abspath("backend.py")
        self.user = getpass.getuser()
        self.uid = str(os.getuid())
        self.wayland_disp = os.environ.get('WAYLAND_DISPLAY', 'wayland-0')

    def execute_as_root(self, cmd_args):
        base_cmd = ["pkexec", "python3", self.backend_script]
        full_cmd = base_cmd + cmd_args
        logger.info(f"Requesting root privileges via pkexec...")
        logger.info(f"Executing Command: {' '.join(full_cmd)}")
        subprocess.Popen(full_cmd)

    def setup(self, interface):
        self.execute_as_root(["setup", "--ns", self.ns_name, "--iface", interface])

    def teardown(self, interface):
        self.execute_as_root(["teardown", "--ns", self.ns_name, "--iface", interface])

    def launch(self, command):
        self.execute_as_root([
            "launch", 
            "--ns", self.ns_name, 
            "--user", self.user, 
            "--uid", self.uid, 
            "--display", self.wayland_disp,
            "--cmd", command
        ])

# --- UI Components ---
class AppSearchPage(Gtk.Box):
    def __init__(self, controller):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.set_margin_top(10)
        self.set_margin_bottom(10)
        self.set_margin_start(10)
        self.set_margin_end(10)
        
        self.apps_dict = {}
        self.controller = controller
        
        # Search Bar
        self.search_entry = Gtk.SearchEntry(placeholder_text="Search applications...")
        self.search_entry.connect("search-changed", self.on_search_changed)
        self.append(self.search_entry)
        
        # Scrolled Window
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)
        self.append(scrolled)
        
        self.list_box = Gtk.ListBox()
        self.list_box.set_selection_mode(Gtk.SelectionMode.NONE)
        scrolled.set_child(self.list_box)

    def update_data(self, new_apps_dict):
        self.apps_dict = new_apps_dict
        self.search_entry.set_text("")
        self.populate_list(self.apps_dict)

    def populate_list(self, apps_to_show):
        self.list_box.remove_all()
        for name, cmd in apps_to_show.items():
            row = Adw.ActionRow(title=name, subtitle=cmd)
            
            launch_btn = Gtk.Button(label="Launch Isolated")
            launch_btn.set_valign(Gtk.Align.CENTER)
            launch_btn.set_css_classes(["suggested-action"])
            launch_btn.connect("clicked", self.on_launch_clicked, cmd)
            
            row.add_suffix(launch_btn)
            self.list_box.append(row)

    def on_search_changed(self, entry):
        query = entry.get_text().lower()
        filtered = {k: v for k, v in self.apps_dict.items() if query in k.lower()}
        self.populate_list(filtered)

    def on_launch_clicked(self, button, cmd):
        logger.info(f"User clicked launch for command: {cmd}")
        self.controller.launch(cmd)

class MainWindow(Adw.ApplicationWindow):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.set_title("Wayland Traffic Splitter")
        self.set_default_size(650, 600)
        
        self.controller = BackendController()
        
        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.set_content(main_box)

        # HeaderBar & Master Switch
        header = Adw.HeaderBar()
        main_box.append(header)
        
        self.master_switch = Gtk.Switch()
        self.master_switch.set_valign(Gtk.Align.CENTER)
        self.master_switch.connect("state-set", self.on_switch_toggled)
        header.pack_end(self.master_switch)

        # Interface Selector
        iface_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        iface_box.set_margin_top(10)
        iface_box.set_margin_bottom(10)
        iface_box.set_margin_start(10)
        iface_box.set_margin_end(10)
        iface_box.append(Gtk.Label(label="Target Interface:"))
        
        self.iface_model = Gtk.StringList()
        for iface in get_net_interfaces():
            self.iface_model.append(iface)
            
        self.iface_combo = Adw.ComboRow()
        self.iface_combo.set_model(self.iface_model)
        self.iface_combo.set_hexpand(True)
        iface_box.append(self.iface_combo)
        main_box.append(iface_box)

        # ViewStack (Tabs)
        self.view_stack = Adw.ViewStack()
        self.view_stack.set_vexpand(True)
        self.view_stack.set_sensitive(False) # Disabled until switch is ON
        
        switcher = Adw.ViewSwitcherBar(stack=self.view_stack)
        switcher.set_reveal(True)
        main_box.append(switcher)
        main_box.append(self.view_stack)

        # Pages
        self.desktop_page = AppSearchPage(self.controller)
        self.view_stack.add_titled(self.desktop_page, "desktop", "Desktop Apps")

        self.flatpak_page = AppSearchPage(self.controller)
        self.view_stack.add_titled(self.flatpak_page, "flatpak", "Flatpak Apps")

        # CLI Page
        cli_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        cli_box.set_margin_top(20)
        cli_box.set_margin_start(20)
        cli_box.set_margin_end(20)
        
        self.cli_entry = Gtk.Entry(placeholder_text="e.g. wget http://example.com")
        cli_btn = Gtk.Button(label="Run Command", css_classes=["suggested-action"])
        cli_btn.connect("clicked", self.on_cli_launch)
        
        cli_box.append(Gtk.Label(label="Custom CLI Command:", xalign=0))
        cli_box.append(self.cli_entry)
        cli_box.append(cli_btn)
        
        self.view_stack.add_titled(cli_box, "cli", "CLI / Custom")
        logger.info("Application initialized successfully.")

    def on_switch_toggled(self, switch, state):
        selected_idx = self.iface_combo.get_selected()
        interface = self.iface_model.get_string(selected_idx)

        if state:
            logger.info("Master Switch turned ON. Initiating isolation sequence.")
            self.controller.setup(interface)
            
            desktop_apps = parse_desktop_files()
            flatpak_apps = parse_flatpak_apps()
            
            self.desktop_page.update_data(desktop_apps)
            self.flatpak_page.update_data(flatpak_apps)

            self.view_stack.set_sensitive(True)
            self.iface_combo.set_sensitive(False)
        else:
            logger.info("Master Switch turned OFF. Restoring normal network.")
            self.controller.teardown(interface)
            self.view_stack.set_sensitive(False)
            self.iface_combo.set_sensitive(True)
        return False

    def on_cli_launch(self, button):
        cmd = self.cli_entry.get_text().strip()
        if cmd:
            logger.info(f"User entered custom CLI command: {cmd}")
            self.controller.launch(cmd)

class TrafficApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id="com.codez.trafficsplitter")

    def do_activate(self):
        win = MainWindow(application=self)
        win.present()
import sys
if __name__ == '__main__':
    app = TrafficApp()
    app.run(sys.argv)