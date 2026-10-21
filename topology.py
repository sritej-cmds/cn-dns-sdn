#!/usr/bin/env python3
"""Mininet topology for the Distributed DNS-Like Name Resolution project.

Person 2 deliverable: creates h1 (primary DNS), h2 (client/controller monitor),
 h3 (secondary DNS), and an Open vSwitch bridge s1. Existing server.py and
root.py are started automatically when they are present in this directory.
"""

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

from mininet.cli import CLI
from mininet.log import info, setLogLevel
from mininet.net import Mininet
from mininet.node import OVSSwitch

PROJECT_DIR = Path(__file__).resolve().parent
LOG_DIR = PROJECT_DIR / "logs"

HOSTS = {
    "h1": {"ip": "10.0.0.1/24", "mac": "00:00:00:00:00:01"},  # primary
    "h2": {"ip": "10.0.0.2/24", "mac": "00:00:00:00:00:02"},  # client
    "h3": {"ip": "10.0.0.3/24", "mac": "00:00:00:00:00:03"},  # secondary
}
DNS_PORT = 9053


def run_ofctl(*args, check=True):
    """Run ovs-ofctl in the root namespace and show useful errors."""
    command = ["ovs-ofctl", "-O", "OpenFlow13", *args]
    result = subprocess.run(command, text=True, capture_output=True)
    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or "unknown error"
        if check:
            raise RuntimeError(f"Command failed: {' '.join(command)}\n{message}")
        info(f"*** Warning: {' '.join(command)} failed: {message}\n")
    return result


def start_host_program(host, script_path, log_path):
    """Start one teammate-owned application inside its Mininet host namespace."""
    if not script_path.is_file():
        info(f"*** Not starting {script_path.name}: file was not found.\n")
        return None

    log_handle = open(log_path, "w", buffering=1, encoding="utf-8")
    try:
        process = host.popen(
            ["python3", "-u", str(script_path)],
            cwd=str(PROJECT_DIR),
            stdout=log_handle,
            stderr=subprocess.STDOUT,
        )
    finally:
        # The child keeps its inherited file descriptor open.
        log_handle.close()

    info(f"*** Started {script_path.name} in {host.name}; log: {log_path}\n")
    return process


def terminate_process(process, label):
    if process is None or process.poll() is not None:
        return
    info(f"*** Stopping {label}...\n")
    process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2)


class ProjectCLI(CLI):
    """Mininet CLI with safe, targeted commands for the demo."""

    def __init__(self, net, nodes, processes):
        self.project_nodes = nodes
        self.project_processes = processes
        super().__init__(net)

    def do_dns_stop_primary(self, _line):
        """Stop only the primary server process (used to demonstrate failover)."""
        process = self.project_processes.get("primary")
        if process is None or process.poll() is not None:
            info("*** Primary server is already stopped.\n")
            self.project_processes["primary"] = None
            return
        terminate_process(process, "primary server")
        self.project_processes["primary"] = None
        info("*** Primary application stopped; wait for the controller to detect failure.\n")

    def do_dns_start_primary(self, _line):
        """Restart the primary server process after a failover test."""
        process = self.project_processes.get("primary")
        if process is not None and process.poll() is None:
            info("*** Primary server is already running.\n")
            return
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        self.project_processes["primary"] = start_host_program(
            self.project_nodes["h1"],
            PROJECT_DIR / "server.py",
            LOG_DIR / "server_h1.log",
        )
        if self.project_processes["primary"] is not None:
            info("*** Primary application restarted; wait for recovery probes.\n")

    def do_dns_flows(self, _line):
        """Display the current OpenFlow table for s1."""
        result = run_ofctl("dump-flows", "s1", check=False)
        if result.stdout:
            info(result.stdout)
        if result.stderr:
            info(result.stderr)

    def do_dns_logs(self, _line):
        """Show the last lines of the SDN controller log."""
        log_path = LOG_DIR / "sdncontroller.log"
        if not log_path.is_file():
            info("*** Controller log not found.\n")
            return
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        info("\n".join(lines[-30:]) + "\n")


def build_topology(start_apps=True):
    """Create and return the Mininet network plus important host/port details."""
    net = Mininet(
        controller=None,
        switch=OVSSwitch,
        autoSetMacs=False,
        autoStaticArp=True,
        build=False,
    )

    h1 = net.addHost("h1", ip=HOSTS["h1"]["ip"], mac=HOSTS["h1"]["mac"])
    h2 = net.addHost("h2", ip=HOSTS["h2"]["ip"], mac=HOSTS["h2"]["mac"])
    h3 = net.addHost("h3", ip=HOSTS["h3"]["ip"], mac=HOSTS["h3"]["mac"])
    s1 = net.addSwitch("s1", protocols="OpenFlow13", failMode="secure")

    # Keep the link objects so that switch port numbers are discovered rather
    # than assumed to be 1, 2, and 3.
    link_h1 = net.addLink(h1, s1)
    link_h2 = net.addLink(h2, s1)
    link_h3 = net.addLink(h3, s1)

    net.build()
    net.start()
    net.staticArp()

    port_map = {
        "primary": s1.ports[link_h1.intf2],
        "client": s1.ports[link_h2.intf2],
        "secondary": s1.ports[link_h3.intf2],
    }

    # Start from a clean flow table on every run and install a normal-switching
    # baseline. The controller adds higher-priority direct-output failover rules.
    run_ofctl("del-flows", "s1")
    run_ofctl("add-flow", "s1", "priority=0,actions=NORMAL")

    info("\n*** Topology started\n")
    info("*** h1 primary DNS:   10.0.0.1\n")
    info("*** h2 client/monitor: 10.0.0.2\n")
    info("*** h3 backup DNS:    10.0.0.3\n")
    info(f"*** Switch ports: {port_map}\n")
    info(f"*** DNS-like UDP port: {DNS_PORT}\n\n")

    processes = {"primary": None, "secondary": None, "controller": None}
    if start_apps:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        processes["primary"] = start_host_program(h1, PROJECT_DIR / "server.py", LOG_DIR / "server_h1.log")
        processes["secondary"] = start_host_program(h3, PROJECT_DIR / "root.py", LOG_DIR / "root_h3.log")
        # Give both UDP sockets a moment to bind before the controller probes.
        time.sleep(0.5)

        controller_path = PROJECT_DIR / "sdncontroller.py"
        if controller_path.is_file():
            controller_args = [
                "python3", "-u", str(controller_path),
                "--switch", "s1",
                "--primary-ip", "10.0.0.1",
                "--client-ip", "10.0.0.2",
                "--secondary-ip", "10.0.0.3",
                "--dns-port", str(DNS_PORT),
                "--health-source-port", "9054",
                "--primary-mac", HOSTS["h1"]["mac"],
                "--secondary-mac", HOSTS["h3"]["mac"],
                "--primary-port", str(port_map["primary"]),
                "--client-port", str(port_map["client"]),
                "--secondary-port", str(port_map["secondary"]),
            ]
            controller_log = open(LOG_DIR / "sdncontroller.log", "w", buffering=1, encoding="utf-8")
            try:
                processes["controller"] = h2.popen(
                    controller_args,
                    cwd=str(PROJECT_DIR),
                    stdout=controller_log,
                    stderr=subprocess.STDOUT,
                )
            finally:
                controller_log.close()
            info(f"*** Started sdncontroller.py in h2; log: {LOG_DIR / 'sdncontroller.log'}\n")
        else:
            info("*** sdncontroller.py not found; automatic failover monitor was not started.\n")

    return net, {"h1": h1, "h2": h2, "h3": h3, "s1": s1}, port_map, processes


def main():
    parser = argparse.ArgumentParser(description="Run the project Mininet topology")
    parser.add_argument(
        "--no-apps", action="store_true",
        help="start only the Mininet hosts/switch (do not launch server.py, root.py, or sdncontroller.py)",
    )
    args = parser.parse_args()
    setLogLevel("info")

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    net = None
    processes = {}
    try:
        net, _nodes, _port_map, processes = build_topology(start_apps=not args.no_apps)
        info(
            "*** Mininet CLI ready. Try: nodes, net, pingall, dns-stop-primary, "
            "dns-start-primary, dns-flows, dns-logs.\n"
        )
        ProjectCLI(net, {"h1": _nodes["h1"], "h2": _nodes["h2"], "h3": _nodes["h3"]}, processes)
    except KeyboardInterrupt:
        info("\n*** Interrupted by user.\n")
    finally:
        for key in ("controller", "primary", "secondary"):
            terminate_process(processes.get(key), key)
        if net is not None:
            info("*** Stopping Mininet network...\n")
            net.stop()
        info("*** Network stopped.\n")


if __name__ == "__main__":
    main()
