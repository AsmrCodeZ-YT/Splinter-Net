import sys
import os
import subprocess
import argparse
import logging

# --- Logging Configuration ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)-7s | [%(name)s] %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger("BACKEND")

# --- Utility Functions ---
def run_cmd(cmd):
    logger.info(f"Executing shell command: {cmd}")
    subprocess.run(cmd, shell=True, check=False)

def setup_ns(ns_name, iface):
    logger.info(f"Setting up network namespace '{ns_name}' with interface '{iface}'")
    run_cmd(f"ip netns add {ns_name}")
    run_cmd(f"ip link set {iface} netns {ns_name}")
    run_cmd(f"ip netns exec {ns_name} ip link set lo up")
    run_cmd(f"ip netns exec {ns_name} ip link set {iface} up")
    run_cmd(f"ip netns exec {ns_name} dhclient {iface}")
    logger.info("Namespace setup completed.")

def teardown_ns(ns_name, iface):
    logger.info(f"Tearing down network namespace '{ns_name}'")
    run_cmd(f"ip netns exec {ns_name} ip link set {iface} netns 1")
    run_cmd(f"ip netns delete {ns_name}")
    logger.info("Namespace teardown completed.")

def launch_app(ns_name, user, uid, w_disp, cmd):
    xdg_dir = f"/run/user/{uid}"
    dbus_addr = f"unix:path={xdg_dir}/bus"
    
    # Environment variables for Wayland and D-Bus integration
    env = f"XDG_RUNTIME_DIR={xdg_dir} WAYLAND_DISPLAY={w_disp} DBUS_SESSION_BUS_ADDRESS={dbus_addr}"
    
    # Drop root privileges and run as the standard user inside the namespace
    full_cmd = f"ip netns exec {ns_name} su - {user} -c '{env} {cmd}'"
    
    logger.info(f"Launching isolated application...")
    logger.info(f"Command: {full_cmd}")
    subprocess.Popen(full_cmd, shell=True)

# --- Main CLI Router ---
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Network Namespace Manager Backend")
    parser.add_argument("action", choices=["setup", "teardown", "launch"])
    parser.add_argument("--ns", default="split_ns")
    parser.add_argument("--iface", default="")
    parser.add_argument("--user", default="")
    parser.add_argument("--uid", default="")
    parser.add_argument("--display", default="")
    parser.add_argument("--cmd", default="", nargs=argparse.REMAINDER)
    
    args = parser.parse_args()
    
    # Require root for these operations
    if os.geteuid() != 0:
        logger.error("Backend script must be run as root. Exiting.")
        sys.exit(1)

    if args.action == "setup":
        setup_ns(args.ns, args.iface)
    elif args.action == "teardown":
        teardown_ns(args.ns, args.iface)
    elif args.action == "launch":
        cmd_str = " ".join(args.cmd)
        launch_app(args.ns, args.user, args.uid, args.display, cmd_str)