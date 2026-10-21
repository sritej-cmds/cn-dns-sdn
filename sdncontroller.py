#!/usr/bin/env python3
"""UDP health monitor and OpenFlow failover controller for Mininet.

The process should normally be launched inside the h2 namespace by topology.py.
It probes the primary server using the project's existing b'ping' -> b'pong'
health-check convention. Failover rules use explicit output ports so rewritten
MAC addresses are not learned by OVS's NORMAL switching pipeline.
"""

import argparse
import socket
import subprocess
import time

OPENFLOW_VERSION = "OpenFlow13"
FAILOVER_PRIORITY = 200
HEALTH_PRIORITY = 300


def parse_args():
    parser = argparse.ArgumentParser(description="Monitor primary DNS server and program OpenFlow failover rules")
    parser.add_argument("--switch", default="s1")
    parser.add_argument("--primary-ip", default="10.0.0.1")
    parser.add_argument("--client-ip", default="10.0.0.2")
    parser.add_argument("--secondary-ip", default="10.0.0.3")
    parser.add_argument("--dns-port", type=int, default=9053)
    parser.add_argument("--health-source-port", type=int, default=9054)
    parser.add_argument("--primary-mac", default="00:00:00:00:00:01")
    parser.add_argument("--secondary-mac", default="00:00:00:00:00:03")
    parser.add_argument("--primary-port", type=int, default=1)
    parser.add_argument("--client-port", type=int, default=2)
    parser.add_argument("--secondary-port", type=int, default=3)
    parser.add_argument("--probe-timeout", type=float, default=0.8)
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--failure-threshold", type=int, default=2)
    parser.add_argument("--recovery-threshold", type=int, default=2)
    return parser.parse_args()


class SDNFailoverController:
    def __init__(self, args):
        self.args = args
        self.failover_active = False
        self.missed_probes = 0
        self.successful_probes = 0

        self.health_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.health_socket.settimeout(args.probe_timeout)
        self.health_socket.bind((args.client_ip, args.health_source_port))

    def ofctl(self, *arguments, check=True):
        command = ["ovs-ofctl", "-O", OPENFLOW_VERSION, *arguments]
        result = subprocess.run(command, text=True, capture_output=True)
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "unknown error"
            message = f"OpenFlow command failed: {' '.join(command)}: {detail}"
            if check:
                raise RuntimeError(message)
            print(f"[SDN] WARNING: {message}", flush=True)
        return result.returncode == 0

    def delete_strict(self, match):
        # --strict plus the full match prevents removing the health-check rules.
        return self.ofctl("--strict", "del-flows", self.args.switch, match, check=False)

    def install_health_exceptions(self):
        """Keep UDP health probes/replies outside the translation rules."""
        a = self.args
        self.delete_strict(
            f"priority={HEALTH_PRIORITY},in_port={a.client_port},udp,"
            f"nw_src={a.client_ip},tp_src={a.health_source_port},"
            f"nw_dst={a.primary_ip},tp_dst={a.dns_port}"
        )
        self.delete_strict(
            f"priority={HEALTH_PRIORITY},in_port={a.primary_port},udp,"
            f"nw_src={a.primary_ip},tp_src={a.dns_port},"
            f"nw_dst={a.client_ip},tp_dst={a.health_source_port}"
        )
        outbound_probe = (
            f"priority={HEALTH_PRIORITY},in_port={a.client_port},udp,"
            f"nw_src={a.client_ip},tp_src={a.health_source_port},"
            f"nw_dst={a.primary_ip},tp_dst={a.dns_port},"
            f"actions=output:{a.primary_port}"
        )
        inbound_probe_reply = (
            f"priority={HEALTH_PRIORITY},in_port={a.primary_port},udp,"
            f"nw_src={a.primary_ip},tp_src={a.dns_port},"
            f"nw_dst={a.client_ip},tp_dst={a.health_source_port},"
            f"actions=output:{a.client_port}"
        )
        self.ofctl("add-flow", a.switch, outbound_probe)
        self.ofctl("add-flow", a.switch, inbound_probe_reply)
        print("[SDN] Health-check bypass flows installed.", flush=True)

    def failover_matches(self):
        a = self.args
        request_match = (
            f"priority={FAILOVER_PRIORITY},in_port={a.client_port},udp,"
            f"nw_src={a.client_ip},nw_dst={a.primary_ip},tp_dst={a.dns_port}"
        )
        reply_match = (
            f"priority={FAILOVER_PRIORITY},in_port={a.secondary_port},udp,"
            f"nw_src={a.secondary_ip},tp_src={a.dns_port},nw_dst={a.client_ip}"
        )
        return request_match, reply_match

    def enable_failover(self):
        """Redirect client DNS queries to h3 and rewrite replies as if from h1."""
        a = self.args
        request_match, reply_match = self.failover_matches()
        self.delete_strict(request_match)
        self.delete_strict(reply_match)

        # Explicit output actions avoid changing the switch's learned MAC table.
        redirect_request = (
            request_match +
            f",actions=mod_nw_dst:{a.secondary_ip},mod_dl_dst:{a.secondary_mac},"
            f"output:{a.secondary_port}"
        )
        rewrite_reply = (
            reply_match +
            f",actions=mod_nw_src:{a.primary_ip},mod_dl_src:{a.primary_mac},"
            f"output:{a.client_port}"
        )
        self.ofctl("add-flow", a.switch, redirect_request)
        self.ofctl("add-flow", a.switch, rewrite_reply)
        self.failover_active = True
        self.successful_probes = 0
        print(
            f"[SDN] FAILOVER ACTIVE: UDP/{a.dns_port} client requests are redirected "
            f"from {a.primary_ip} to {a.secondary_ip}.",
            flush=True,
        )

    def disable_failover(self):
        request_match, reply_match = self.failover_matches()
        self.delete_strict(request_match)
        self.delete_strict(reply_match)
        self.failover_active = False
        self.missed_probes = 0
        print("[SDN] PRIMARY RECOVERED: failover rules removed; normal resolution restored.", flush=True)

    def primary_is_healthy(self):
        a = self.args
        try:
            self.health_socket.sendto(b"ping", (a.primary_ip, a.dns_port))
            response, peer = self.health_socket.recvfrom(1024)
            return peer[0] == a.primary_ip and response == b"pong"
        except (socket.timeout, OSError):
            return False

    def run(self):
        a = self.args
        print(
            f"[SDN] Monitoring primary {a.primary_ip}:{a.dns_port}; "
            f"probe source port={a.health_source_port}.",
            flush=True,
        )
        self.install_health_exceptions()

        try:
            while True:
                if self.primary_is_healthy():
                    self.missed_probes = 0
                    if self.failover_active:
                        self.successful_probes += 1
                        print(
                            f"[SDN] Primary recovery probe {self.successful_probes}/"
                            f"{a.recovery_threshold} succeeded.",
                            flush=True,
                        )
                        if self.successful_probes >= a.recovery_threshold:
                            self.disable_failover()
                    else:
                        self.successful_probes = 0
                else:
                    self.successful_probes = 0
                    self.missed_probes += 1
                    print(
                        f"[SDN] Primary health check missed "
                        f"({self.missed_probes}/{a.failure_threshold}).",
                        flush=True,
                    )
                    if not self.failover_active and self.missed_probes >= a.failure_threshold:
                        try:
                            self.enable_failover()
                        except RuntimeError as exc:
                            print(f"[SDN] Failed to program failover: {exc}", flush=True)
                time.sleep(a.interval)
        finally:
            self.health_socket.close()


def main():
    args = parse_args()
    if args.probe_timeout <= 0 or args.interval < 0:
        raise SystemExit("probe timeout must be positive and interval cannot be negative")
    if args.failure_threshold < 1 or args.recovery_threshold < 1:
        raise SystemExit("failure and recovery thresholds must be at least 1")
    controller = SDNFailoverController(args)
    try:
        controller.run()
    except KeyboardInterrupt:
        print("\n[SDN] Controller stopped.", flush=True)
    except OSError as exc:
        raise SystemExit(f"Cannot start controller health socket: {exc}") from exc


if __name__ == "__main__":
    main()
