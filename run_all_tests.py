#!/usr/bin/env python3
"""One-command test, benchmark, and failover runner for cn-dns-sdn.

Run from the repository root:
    sudo python3 run_all_tests.py

The test network intentionally uses Open vSwitch standalone forwarding
(fail_mode=standalone / actions=NORMAL) for baseline DNS resolution. The
separate sdncontroller.py installs higher-priority SDN rules only for failover.
"""
from __future__ import annotations

import os
import sys
import time
import statistics
import subprocess
import traceback
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPORT_DIR = ROOT / "reports"
REPORT_DIR.mkdir(exist_ok=True)
STAMP = datetime.now().strftime("%Y%m%d_%H%M%S")
REPORT = REPORT_DIR / f"test_report_{STAMP}.txt"
PRIMARY_LOG = REPORT_DIR / f"test_primary_{STAMP}.log"
SECONDARY_LOG = REPORT_DIR / f"test_secondary_{STAMP}.log"
CONTROLLER_LOG = REPORT_DIR / f"test_controller_{STAMP}.log"
PASS = 0
FAIL = 0


class AbortRun(RuntimeError):
    """Raised when a critical stage fails and later stages must not run."""


def log(msg: str = "") -> None:
    print(msg, flush=True)
    with REPORT.open("a", encoding="utf-8") as handle:
        handle.write(msg + "\n")


def check(ok: bool, name: str) -> bool:
    global PASS, FAIL
    log(f"[{'PASS' if ok else 'FAIL'}] {name}")
    if ok:
        PASS += 1
    else:
        FAIL += 1
    return ok


def run_command(args, timeout=30, cwd=ROOT, env=None):
    return subprocess.run(
        args,
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
    )


def query_from_host(host, domain: str, txid: int, timeout: float = 2.0):
    """Run the protocol helper inside a Mininet host namespace and return (answer, latency, raw_output)."""
    code = (
        "from tests.protocol_helpers import query_once; "
        f"r=query_once('10.0.0.1',9053,{domain!r},{timeout!r},{txid}); "
        "print(f'ANSWER={r.answer} LATENCY_MS={r.latency_ms:.3f} FLAGS={hex(r.flags)}'); "
        "print(f'COUNTS={r.question_count},{r.answer_count}')"
    )
    proc = host.popen(
        [sys.executable, "-c", code],
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        output, _ = proc.communicate(timeout=timeout + 2.0)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            output, _ = proc.communicate(timeout=1.0)
        except subprocess.TimeoutExpired:
            output = "<query process did not exit after kill>"
        raise TimeoutError(f"query timed out for {domain}; output={output.strip()}")

    if proc.returncode:
        raise RuntimeError(output.strip() or f"query exited with status {proc.returncode}")

    answer = None
    latency = None
    for line in output.splitlines():
        if line.startswith("ANSWER="):
            # Extract ANSWER value up to LATENCY_MS
            if " LATENCY_MS=" in line:
                ans_part, rest = line.split(" LATENCY_MS=", 1)
                answer = ans_part.replace("ANSWER=", "", 1).strip()
                lat_part = rest.split()[0]
                try:
                    latency = float(lat_part)
                except ValueError:
                    latency = 0.0
            else:
                answer = line.replace("ANSWER=", "", 1).strip()
                latency = 0.0

    if answer is None or latency is None:
        raise RuntimeError(f"Could not parse query result: {output.strip()}")

    return answer, latency, output.strip()



def terminate_process(proc, name: str, timeout: float = 2.0) -> None:
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        log(f"[WARN] {name} did not terminate; killing it.")
        proc.kill()
        proc.wait(timeout=timeout)


def main() -> int:
    global PASS, FAIL
    if os.geteuid() != 0:
        print("Please run as root: sudo python3 run_all_tests.py", file=sys.stderr)
        return 2

    log("=" * 68)
    log("CN DNS-like SDN: automated tests, benchmarks, and failover")
    log(f"Started: {datetime.now().isoformat(timespec='seconds')}")
    log(f"Project: {ROOT}")
    log(f"Report: {REPORT}")
    log(f"Primary server log: {PRIMARY_LOG}")
    log(f"Secondary server log: {SECONDARY_LOG}")
    log(f"Controller log: {CONTROLLER_LOG}")
    log("=" * 68)

    net = None
    controller_proc = None
    primary = None
    secondary = None
    server_log_handles = []
    controller_log_handle = None

    try:
        required = [
            "topology.py", "server.py", "root.py", "sdncontroller.py",
            "benchmark.py", "tests/protocol_helpers.py",
            "tests/test_protocol_helpers.py",
        ]
        for name in required:
            check((ROOT / name).is_file(), f"Required file {name}")
        if FAIL:
            raise AbortRun("Required project files are missing; stopping.")

        log("\n[STAGE] Protocol unit tests")
        unit = run_command(
            [sys.executable, "-m", "unittest", "discover", "-s", "tests",
             "-p", "test_protocol_helpers.py", "-v"],
            timeout=60,
        )
        log(unit.stdout.rstrip())
        if not check(unit.returncode == 0, "Protocol unit tests"):
            raise AbortRun("Protocol unit tests failed; network stages skipped.")

        try:
            from mininet.net import Mininet
            from mininet.log import setLogLevel
        except Exception as exc:
            check(False, "Import Mininet")
            raise AbortRun(f"Could not import Mininet: {exc}") from exc

        log("\n[STAGE] Create Mininet topology programmatically")
        setLogLevel("warning")
        # Important: match the verified manual topology. Do not attach
        # OVSController here; it leaves the switch controller-dependent and
        # breaks normal DNS traffic before the failover controller is started.
        net = Mininet(controller=None, autoSetMacs=False)
        h1 = net.addHost("h1", ip="10.0.0.1")
        h2 = net.addHost("h2", ip="10.0.0.2")
        h3 = net.addHost("h3", ip="10.0.0.3", mac="00:00:00:00:00:03")
        s1 = net.addSwitch("s1", failMode="standalone")
        net.addLink(h1, s1)
        net.addLink(h2, s1)
        net.addLink(h3, s1)
        net.start()
        check(True, "Mininet topology started (h1, h2, h3, s1)")

        # Record switch configuration rather than deleting the NORMAL rule
        # that standalone mode uses for normal forwarding.
        switch_state = run_command(["ovs-vsctl", "get", "Bridge", "s1", "fail_mode"])
        log(f"Switch fail_mode: {switch_state.stdout.strip()}")
        flows = run_command(["ovs-ofctl", "dump-flows", "s1"])
        log("Initial OpenFlow flows:\n" + flows.stdout.strip())
        if switch_state.returncode != 0 or "standalone" not in switch_state.stdout:
            check(False, "Switch is in standalone forwarding mode")
            raise AbortRun("Switch forwarding mode is not standalone; stopping before DNS tests.")
        check(True, "Switch is in standalone forwarding mode")

        server_log_handle = PRIMARY_LOG.open("w", encoding="utf-8")
        secondary_log_handle = SECONDARY_LOG.open("w", encoding="utf-8")
        server_log_handles.extend([server_log_handle, secondary_log_handle])

        primary = h1.popen(
            [sys.executable, "-u", str(ROOT / "server.py")],
            cwd=str(ROOT), stdout=server_log_handle, stderr=subprocess.STDOUT,
        )
        secondary = h3.popen(
            [sys.executable, "-u", str(ROOT / "root.py")],
            cwd=str(ROOT), stdout=secondary_log_handle, stderr=subprocess.STDOUT,
        )
        time.sleep(0.7)
        if not check(primary.poll() is None, "Primary DNS process remains running"):
            server_log_handle.flush()
            raise AbortRun(f"Primary DNS process exited. See {PRIMARY_LOG}")
        if not check(secondary.poll() is None, "Secondary DNS process remains running"):
            secondary_log_handle.flush()
            raise AbortRun(f"Secondary DNS process exited. See {SECONDARY_LOG}")

        log("\n[STAGE] DNS smoke tests (fail-fast)")
        for domain, expected, txid in [
            ("example.com", "93.184.216.34", 41001),
            ("google.com", "142.250.190.46", 41002),
            ("netflix.com", "192.27.78.9", 41003),
        ]:
            try:
                answer, latency, raw = query_from_host(h2, domain, txid, timeout=2.0)
            except Exception as exc:
                log(f"{domain} query error: {exc}")
                server_log_handle.flush()
                secondary_log_handle.flush()
                log("Primary server log:\n" + PRIMARY_LOG.read_text(encoding="utf-8", errors="replace"))
                log("Secondary server log:\n" + SECONDARY_LOG.read_text(encoding="utf-8", errors="replace"))
                check(False, f"Resolve {domain}")
                raise AbortRun(
                    f"DNS smoke test for {domain} failed. Benchmarks and failover were skipped. "
                    "Use the server logs above to diagnose the first failure."
                ) from exc
            log(f"{domain}: {raw}")
            if not check(answer == expected, f"Resolve {domain} -> {expected}"):
                raise AbortRun(
                    f"DNS returned {answer!r} for {domain}; expected {expected!r}. "
                    "Benchmarks and failover were skipped."
                )


        log("\n[STAGE] Unknown domain handling")
        unknown_domain = "nonexistent-domain-test.xyz"
        try:
            ans,lat,raw = query_from_host(h2,"nonexistent-domain-test.xyz",42001,timeout=2.0)
            log(f"Unknown domain query output: {raw}")
            # Check that the server returned NXDOMAIN (0x8183)
            is_nxdomain = ("0x8183" in raw) or ("NXDOMAIN" in ans.upper()) or ("NOT FOUND" in ans.upper())
            if not check(is_nxdomain, f"Query '{unknown_domain}' returns NXDOMAIN"):
                raise AbortRun(f"Expected NXDOMAIN for {unknown_domain}, got output={raw!r}")
        except Exception as exc:
            check(False, "Unknown domain handling")
            raise AbortRun(f"Unknown domain test failed with exception: {exc}") from exc

        log("\n[STAGE] Cache hit vs miss verification")
        target_domain = "netflix.com"
        # First query: Cache Miss / Recursion to Root
        ans1, lat_miss, raw1 = query_from_host(h2, target_domain, 43001, timeout=2.0)
        # Second query: Cache Hit from memory
        ans2, lat_hit, raw2 = query_from_host(h2, target_domain, 43002, timeout=2.0)

        log(f"Cache miss query latency: {lat_miss:.3f} ms")
        log(f"Cache hit query latency:  {lat_hit:.3f} ms")
        check(ans1 == ans2 == "192.27.78.9", f"Consistent answer across cache miss and hit for {target_domain}")

        server_log_handle.flush()
        primary_log_content = PRIMARY_LOG.read_text(encoding="utf-8", errors="replace")
        cache_logged = f"CACHE HIT: {target_domain}" in primary_log_content or "cache" in primary_log_content.lower()
        check(cache_logged, f"Server cache log confirms lookup for {target_domain}")


        log("\n[STAGE] Upstream timeout resilience")
        # Temporarily stop the secondary root server to simulate upstream failure
        secondary.terminate()
        try:
            secondary.wait(timeout=2)
        except subprocess.TimeoutExpired:
            secondary.kill()
            secondary.wait()
        log("Secondary DNS (h3) stopped to test primary upstream timeout.")

        uncached_domain = "crunchyroll.com"
        try:
            # Query domain that requires forwarding to h3
            ans_fail,lat_fail,raw_fail = query_from_host(h2,"crunchyroll.com",44001,timeout=3.0)
            log(f"Upstream timeout query output: {raw_fail}")
            # Expect SERVFAIL (0x8182) or upstream timeout error payload
            servfail_ok = ("FLAGS=0x8182" in raw_fail) or ("SERVFAIL" in ans_fail)
            check(servfail_ok, "Primary returns SERVFAIL (0x8182) when upstream times out")
        except Exception as exc:
            log(f"Query timed out at client level: {exc}")
            check(False, "Primary server failed to return SERVFAIL on upstream timeout")
            raise AbortRun("Upstream timeout handling failed.") from exc

        # Restart secondary root server for subsequent stages
        secondary_log_handle = SECONDARY_LOG.open("a", encoding="utf-8")
        server_log_handles.append(secondary_log_handle)
        secondary = h3.popen(
            [sys.executable, "-u", str(ROOT / "root.py")],
            cwd=str(ROOT), stdout=secondary_log_handle, stderr=subprocess.STDOUT,
        )
        time.sleep(0.7)
        check(secondary.poll() is None, "Secondary DNS restarted for benchmark and failover stages")


        log("\n[STAGE] Benchmarks (30 requests per domain)")
        for domain, expected in [("example.com", "93.184.216.34"),
                                 ("netflix.com", "192.27.78.9")]:
            samples = []
            for i in range(30):
                try:
                    answer, latency, _ = query_from_host(h2, domain, 50000 + i, timeout=2.0)
                except Exception as exc:
                    check(False, f"30-query benchmark: {domain}")
                    raise AbortRun(
                        f"Benchmark aborted on request {i + 1}/30 for {domain}: {exc}. "
                        "Automatic failover stage skipped."
                    ) from exc
                if answer != expected:
                    check(False, f"30-query benchmark: {domain}")
                    raise AbortRun(
                        f"Benchmark got {answer!r} for {domain} request {i + 1}; "
                        "automatic failover stage skipped."
                    )
                samples.append(latency)

            ordered = sorted(samples)
            p95 = ordered[round(0.95 * (len(ordered) - 1))]
            log(f"Benchmark {domain}: {len(samples)}/30 successful")
            log(
                f"  min={min(samples):.3f}ms median={statistics.median(samples):.3f}ms "
                f"p95={p95:.3f}ms max={max(samples):.3f}ms"
            )
            check(len(samples) == 30, f"30-query benchmark: {domain}")

        log("\n[STAGE] Automatic failover")
        env = os.environ.copy()
        env["MININET_H2_PID"] = str(h2.pid)
        controller_log_handle = CONTROLLER_LOG.open("w", encoding="utf-8")
        controller_proc = h2.popen(
            [sys.executable, str(ROOT / "sdncontroller.py")],
            cwd=str(ROOT), env=env,
            stdout=controller_log_handle, stderr=subprocess.STDOUT, text=True,
        )
        time.sleep(1.0)

        # Confirm baseline health through h2's network namespace before failure.
        try:
            health_answer, _, health_raw = query_from_host(h2, "example.com", 59001, timeout=2.0)
            healthy = health_answer == "93.184.216.34"
        except Exception as exc:
            healthy = False
            health_raw = f"health query failed: {exc}"
        log(f"Pre-failure health probe: {health_raw}")
        if not check(healthy, "Primary DNS responds before simulated failure"):
            raise AbortRun("Primary health precondition failed; failover test skipped.")

        primary.terminate()
        try:
            primary.wait(timeout=3)
        except subprocess.TimeoutExpired:
            primary.kill()
            primary.wait()
        log("Primary DNS process stopped to simulate failure.")



        deadline = time.time() + 18
        controller_text = ""
        while time.time() < deadline:
            if CONTROLLER_LOG.exists():
                controller_text = CONTROLLER_LOG.read_text(encoding="utf-8", errors="replace")
    
            # Accept either standard message format
            if ("Failover rules installed." in controller_text) or ("FAILOVER ACTIVE" in controller_text):
                break
            if controller_proc.poll() is not None:
                break
            time.sleep(0.5)

        log("Controller log:\n" + (controller_text.strip() or "<empty>"))
        rules_installed = ("Failover rules installed." in controller_text) or ("FAILOVER ACTIVE" in controller_text)
        if not check(rules_installed, "Controller detects primary failure and installs rules"):
            raise AbortRun(f"Controller failed to install failover rules. See {CONTROLLER_LOG}")





        # Give OVS a moment to apply the flows, then query the original primary IP.
        time.sleep(0.3)
        try:
            answer, latency, raw = query_from_host(h2, "netflix.com", 60001, timeout=3.0)
            log(f"Failover query: {raw}")
            if not check(answer == "192.27.78.9", "Query to primary IP succeeds through secondary after failure"):
                raise AbortRun(f"Failover query returned unexpected answer {answer!r}.")
        except AbortRun:
            raise
        except Exception as exc:
            log(f"Failover query error: {exc}")
            check(False, "Query to primary IP succeeds through secondary after failure")
            raise AbortRun(f"Failover query timed out or failed: {exc}") from exc

        final_flows = run_command(["ovs-ofctl", "dump-flows", "s1"], timeout=10)
        log("OpenFlow flows after failover:\n" + final_flows.stdout.strip())
        flow_ok = final_flows.returncode == 0 and "nw_dst=10.0.0.1" in final_flows.stdout
        check(flow_ok, "OpenFlow failover rules visible on switch")
        if not flow_ok:
            raise AbortRun("Failover query worked but expected flow evidence was not found.")

    except AbortRun as exc:
        log(f"\n[ABORT] {exc}")
    except KeyboardInterrupt:
        log("\n[INTERRUPTED] User interrupted the test run.")
        if FAIL == 0:
            FAIL += 1
    except Exception as exc:
        log(f"\n[FATAL] {type(exc).__name__}: {exc}")
        log(traceback.format_exc())
        check(False, "Test runner completed without an unexpected exception")
    finally:
        terminate_process(controller_proc, "controller")
        terminate_process(primary, "primary DNS")
        terminate_process(secondary, "secondary DNS")
        if net is not None:
            try:
                net.stop()
                log("Mininet network stopped cleanly.")
            except Exception as exc:
                log(f"[WARN] Mininet cleanup problem: {exc}")
        for handle in server_log_handles:
            try:
                handle.flush()
                handle.close()
            except Exception:
                pass
        if controller_log_handle is not None:
            try:
                controller_log_handle.flush()
                controller_log_handle.close()
            except Exception:
                pass

    log("\n" + "=" * 68)
    log(f"FINAL RESULTS: {PASS} passed, {FAIL} failed")
    log(f"Report saved to: {REPORT}")
    log(f"Primary server log: {PRIMARY_LOG}")
    log(f"Secondary server log: {SECONDARY_LOG}")
    log(f"Controller log: {CONTROLLER_LOG}")
    log("Finished: " + datetime.now().isoformat(timespec="seconds"))
    log("=" * 68)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

