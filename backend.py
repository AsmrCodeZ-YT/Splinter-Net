#!/usr/bin/env python3
"""
backend.py - Network Namespace Manager Backend
Must be run as root (via pkexec).
Uses veth pairs + iptables MASQUERADE to route namespace traffic
through a specific physical interface WITHOUT stealing it from the host.
"""

import sys
import os
import subprocess
import argparse
import logging
import time
import signal

# --- Logging Configuration ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)-7s | [%(name)s] %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger("BACKEND")


# --- Utility Functions ---
def run_cmd(cmd, check=False, capture=False):
    """Execute a shell command with logging."""
    logger.info(f"CMD: {cmd}")
    try:
        result = subprocess.run(
            cmd, shell=True, check=check,
            capture_output=capture, text=True
        )
        if capture:
            return result.stdout.strip()
        return result.returncode == 0
    except subprocess.CalledProcessError as e:
        logger.error(f"Command failed (rc={e.returncode}): {cmd}")
        return False if not capture else ""


def cmd_success(cmd):
    """Check if a command succeeds silently."""
    result = subprocess.run(
        cmd, shell=True,
        capture_output=True, text=True
    )
    return result.returncode == 0


def get_interface_gateway(iface):
    """Get the default gateway IP reachable through a specific interface."""
    output = run_cmd(
        f"ip route show dev {iface} default",
        capture=True
    )
    # Format: "default via 192.168.1.1 ..."
    if output and "via" in output:
        parts = output.split()
        try:
            idx = parts.index("via")
            return parts[idx + 1]
        except (ValueError, IndexError):
            pass

    # Fallback: try getting any gateway
    output = run_cmd("ip route show default", capture=True)
    if output and "via" in output:
        for line in output.splitlines():
            if iface in line:
                parts = line.split()
                try:
                    idx = parts.index("via")
                    return parts[idx + 1]
                except (ValueError, IndexError):
                    pass
    return None


def get_dns_servers():
    """Extract current DNS servers from systemd-resolved or resolv.conf."""
    servers = []

    # Try systemd-resolve first (Fedora default)
    try:
        result = subprocess.run(
            ["resolvectl", "dns"],
            capture_output=True, text=True
        )
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                parts = line.split(":")
                if len(parts) >= 2:
                    for token in parts[-1].strip().split():
                        # Basic IPv4 check
                        if token.count('.') == 3:
                            servers.append(token)
    except FileNotFoundError:
        pass

    # Fallback to resolv.conf
    if not servers:
        try:
            with open("/etc/resolv.conf", "r") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("nameserver"):
                        srv = line.split()[1]
                        if srv != "127.0.0.53":  # skip stub resolver
                            servers.append(srv)
        except FileNotFoundError:
            pass

    # Ultimate fallback
    if not servers:
        servers = ["8.8.8.8", "1.1.1.1"]

    return servers


# ============================================================
#  SETUP: Create namespace with veth pair + masquerade
# ============================================================
def setup_ns(ns_name, iface):
    """
    Create a network namespace and route its traffic through
    the specified physical interface using NAT/masquerade.
    
    Architecture:
        [NS: veth-ns (10.200.1.2)] <--veth--> [HOST: veth-host (10.200.1.1)]
        Host does MASQUERADE on 'iface' for packets from 10.200.1.0/24
    """
    logger.info(f"=== SETUP namespace '{ns_name}' routing via '{iface}' ===")

    veth_host = f"veth-{ns_name[:8]}-h"
    veth_ns = f"veth-{ns_name[:8]}-n"
    subnet = "10.200.1"
    host_ip = f"{subnet}.1"
    ns_ip = f"{subnet}.2"

    # 1. Cleanup any previous state
    logger.info("Step 1: Cleaning up previous namespace if exists...")
    run_cmd(f"ip netns delete {ns_name} 2>/dev/null")
    run_cmd(f"ip link delete {veth_host} 2>/dev/null")
    time.sleep(0.5)

    # 2. Create namespace
    logger.info("Step 2: Creating network namespace...")
    run_cmd(f"ip netns add {ns_name}", check=True)

    # 3. Create veth pair
    logger.info("Step 3: Creating veth pair...")
    run_cmd(
        f"ip link add {veth_host} type veth peer name {veth_ns}",
        check=True
    )

    # 4. Move one end into the namespace
    logger.info("Step 4: Moving veth-ns into namespace...")
    run_cmd(f"ip link set {veth_ns} netns {ns_name}", check=True)

    # 5. Configure host-side veth
    logger.info("Step 5: Configuring host-side veth...")
    run_cmd(f"ip addr add {host_ip}/24 dev {veth_host}", check=True)
    run_cmd(f"ip link set {veth_host} up", check=True)

    # 6. Configure namespace-side
    logger.info("Step 6: Configuring namespace network...")
    run_cmd(f"ip netns exec {ns_name} ip link set lo up")
    run_cmd(f"ip netns exec {ns_name} ip link set {veth_ns} up")
    run_cmd(
        f"ip netns exec {ns_name} ip addr add {ns_ip}/24 dev {veth_ns}",
        check=True
    )
    run_cmd(
        f"ip netns exec {ns_name} ip route add default via {host_ip}",
        check=True
    )

    # 7. Enable IP forwarding
    logger.info("Step 7: Enabling IP forwarding...")
    run_cmd("sysctl -w net.ipv4.ip_forward=1")

    # 8. iptables masquerade (route NS traffic through specific interface)
    logger.info("Step 8: Setting up iptables MASQUERADE...")
    # Clear any previous rules for this subnet
    run_cmd(
        f"iptables -t nat -D POSTROUTING "
        f"-s {subnet}.0/24 -o {iface} -j MASQUERADE 2>/dev/null"
    )
    run_cmd(
        f"iptables -D FORWARD "
        f"-i {veth_host} -o {iface} -j ACCEPT 2>/dev/null"
    )
    run_cmd(
        f"iptables -D FORWARD "
        f"-i {iface} -o {veth_host} "
        f"-m state --state RELATED,ESTABLISHED -j ACCEPT 2>/dev/null"
    )

    # Add fresh rules
    run_cmd(
        f"iptables -t nat -A POSTROUTING "
        f"-s {subnet}.0/24 -o {iface} -j MASQUERADE",
        check=True
    )
    run_cmd(
        f"iptables -A FORWARD "
        f"-i {veth_host} -o {iface} -j ACCEPT",
        check=True
    )
    run_cmd(
        f"iptables -A FORWARD "
        f"-i {iface} -o {veth_host} "
        f"-m state --state RELATED,ESTABLISHED -j ACCEPT",
        check=True
    )

    # 9. Setup DNS inside namespace
    logger.info("Step 9: Configuring DNS in namespace...")
    ns_resolv = f"/etc/netns/{ns_name}"
    os.makedirs(ns_resolv, exist_ok=True)
    dns_servers = get_dns_servers()
    with open(f"{ns_resolv}/resolv.conf", "w") as f:
        for dns in dns_servers:
            f.write(f"nameserver {dns}\n")
    logger.info(f"DNS servers configured: {dns_servers}")

    # 10. Verify connectivity
    logger.info("Step 10: Verifying namespace connectivity...")
    time.sleep(1)
    if cmd_success(f"ip netns exec {ns_name} ping -c 1 -W 3 {host_ip}"):
        logger.info("✓ Namespace can reach host gateway")
    else:
        logger.warning("✗ Cannot reach host gateway from namespace")

    if cmd_success(f"ip netns exec {ns_name} ping -c 1 -W 5 8.8.8.8"):
        logger.info("✓ Namespace has internet connectivity")
    else:
        logger.warning("✗ No internet in namespace (check iptables/routing)")

    logger.info("=== SETUP COMPLETE ===")
    return True


# ============================================================
#  TEARDOWN: Remove namespace and cleanup rules
# ============================================================
def teardown_ns(ns_name, iface):
    """Remove the network namespace and associated iptables rules."""
    logger.info(f"=== TEARDOWN namespace '{ns_name}' ===")

    veth_host = f"veth-{ns_name[:8]}-h"
    subnet = "10.200.1"

    # Remove iptables rules
    logger.info("Removing iptables rules...")
    run_cmd(
        f"iptables -t nat -D POSTROUTING "
        f"-s {subnet}.0/24 -o {iface} -j MASQUERADE 2>/dev/null"
    )
    run_cmd(
        f"iptables -D FORWARD "
        f"-i {veth_host} -o {iface} -j ACCEPT 2>/dev/null"
    )
    run_cmd(
        f"iptables -D FORWARD "
        f"-i {iface} -o {veth_host} "
        f"-m state --state RELATED,ESTABLISHED -j ACCEPT 2>/dev/null"
    )

    # Delete veth (deleting one end removes the pair)
    run_cmd(f"ip link delete {veth_host} 2>/dev/null")

    # Delete namespace
    run_cmd(f"ip netns delete {ns_name} 2>/dev/null")

    # Clean DNS config
    ns_resolv = f"/etc/netns/{ns_name}"
    run_cmd(f"rm -rf {ns_resolv}")

    logger.info("=== TEARDOWN COMPLETE ===")


# ============================================================
#  LAUNCH: Run application inside namespace as regular user
# ============================================================
def launch_app(ns_name, user, uid, wayland_display, cmd):
    """Launch an application inside the network namespace."""
    logger.info(f"=== LAUNCHING APP in '{ns_name}' ===")
    logger.info(f"User: {user} (UID: {uid})")
    logger.info(f"Command: {cmd}")

    xdg_dir = f"/run/user/{uid}"
    wayland_socket = f"{xdg_dir}/{wayland_display}"

    # Build environment variables
    env_vars = {
        "XDG_RUNTIME_DIR": xdg_dir,
        "WAYLAND_DISPLAY": wayland_display,
        "DBUS_SESSION_BUS_ADDRESS": f"unix:path={xdg_dir}/bus",
        "XDG_SESSION_TYPE": "wayland",
        "HOME": f"/home/{user}",
        "USER": user,
        "LOGNAME": user,
        "SHELL": "/bin/bash",
        "PATH": "/usr/local/bin:/usr/bin:/bin",
    }

    # Check if Wayland socket exists
    if os.path.exists(wayland_socket):
        logger.info(f"✓ Wayland socket found: {wayland_socket}")
    else:
        logger.warning(f"✗ Wayland socket NOT found: {wayland_socket}")

    # Build the env string for su
    env_str = " ".join(f'{k}="{v}"' for k, v in env_vars.items())

    # Construct the command
    full_cmd = (
        f"ip netns exec {ns_name} "
        f"su - {user} -s /bin/bash -c "
        f"'env {env_str} {cmd}'"
    )

    logger.info(f"Full command: {full_cmd}")

    # Launch as detached process
    proc = subprocess.Popen(
        full_cmd,
        shell=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        preexec_fn=os.setpgrp  # Detach from parent
    )

    logger.info(f"Application launched with PID: {proc.pid}")
    logger.info("=== LAUNCH COMPLETE ===")


# ============================================================
#  STATUS: Check namespace state
# ============================================================
def check_status(ns_name):
    """Check if the namespace is active and has connectivity."""
    exists = cmd_success(f"ip netns list | grep -q '^{ns_name}'")
    if not exists:
        print("STATUS:INACTIVE")
        return

    has_net = cmd_success(
        f"ip netns exec {ns_name} ping -c 1 -W 2 8.8.8.8"
    )
    if has_net:
        print("STATUS:ACTIVE:CONNECTED")
    else:
        print("STATUS:ACTIVE:NO_INTERNET")


# --- Main CLI Router ---
if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Network Namespace Manager Backend"
    )
    parser.add_argument(
        "action",
        choices=["setup", "teardown", "launch", "status"]
    )
    parser.add_argument("--ns", default="split_ns")
    parser.add_argument("--iface", default="")
    parser.add_argument("--user", default="")
    parser.add_argument("--uid", default="")
    parser.add_argument("--display", default="")
    parser.add_argument("--cmd", default="", nargs=argparse.REMAINDER)

    args = parser.parse_args()

    if args.action in ("setup", "teardown", "launch"):
        if os.geteuid() != 0:
            logger.error(
                "Backend must be run as root. Use pkexec. Exiting."
            )
            sys.exit(1)

    if args.action == "setup":
        if not args.iface:
            logger.error("Interface (--iface) is required for setup.")
            sys.exit(1)
        success = setup_ns(args.ns, args.iface)
        sys.exit(0 if success else 1)

    elif args.action == "teardown":
        if not args.iface:
            logger.error("Interface (--iface) is required for teardown.")
            sys.exit(1)
        teardown_ns(args.ns, args.iface)

    elif args.action == "launch":
        cmd_str = " ".join(args.cmd) if args.cmd else ""
        if not cmd_str:
            logger.error("No command specified for launch.")
            sys.exit(1)
        if not args.user or not args.uid:
            logger.error("--user and --uid are required for launch.")
            sys.exit(1)
        display = args.display or "wayland-0"
        launch_app(args.ns, args.user, args.uid, display, cmd_str)

    elif args.action == "status":
        check_status(args.ns)
