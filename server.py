import socket
import struct

SERVER_IP = "10.0.0.1"
SERVER_PORT = 9053
ROOT_IP = "10.0.0.3"
ROOT_PORT = 9053

dns_records = {
    "example.com": "93.184.216.34",
    "google.com": "142.250.190.46",
    "fedora.org": "8.43.85.67",
}

# In-memory cache: {domain: (flags, payload_bytes)}
cache = {}

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.bind((SERVER_IP, SERVER_PORT))

# Dedicated forwarding socket with a timeout to avoid hangs
fwd_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
fwd_sock.settimeout(2.0)

print(f"Binary DNS server is listening on {SERVER_IP}:{SERVER_PORT}.....", flush=True)

while True:
    try:
        data, addr = sock.recvfrom(1024)
    except Exception:
        continue

    # Health check probe from sdncontroller / run_all_tests.sh
    if data == b"ping":
        sock.sendto(b"pong", addr)
        continue

    if len(data) < 12:
        continue

    header = data[:12]
    payload = data[12:]
    trans_id, flags, qd, an, ns, ar = struct.unpack("!HHHHHH", header)
    domain_requested = payload.decode(errors="ignore").strip()

    # 1. Local resolution
    if domain_requested in dns_records:
        ip_answer = dns_records[domain_requested]
        resp_header = struct.pack("!HHHHHH", trans_id, 0x8180, 1, 1, 0, 0)
        sock.sendto(resp_header + ip_answer.encode(), addr)

    # 2. Cache hit
    elif domain_requested in cache:
        print(f"CACHE HIT: {domain_requested}",flush=True)
        cached_flags, cached_payload = cache[domain_requested]
        an_count = 1 if cached_flags == 0x8180 else 0
        resp_header = struct.pack("!HHHHHH", trans_id, cached_flags, 1, an_count, 0, 0)
        sock.sendto(resp_header + cached_payload, addr)

    # 3. Cache miss: Forward to Root / Secondary
    else:
        try:
            fwd_sock.sendto(data, (ROOT_IP, ROOT_PORT))
            h3_response, _ = fwd_sock.recvfrom(1024)

            if len(h3_response) >= 12:
                _, h3_flags, _, _, _, _ = struct.unpack("!HHHHHH", h3_response[:12])
                h3_payload = h3_response[12:]

                # Cache successful lookups and NXDOMAIN
                cache[domain_requested] = (h3_flags, h3_payload)

                # Re-stamp response with the current client's transaction ID
                an_count = 1 if h3_flags == 0x8180 else 0
                out_header = struct.pack("!HHHHHH", trans_id, h3_flags, 1, an_count, 0, 0)
                sock.sendto(out_header + h3_payload, addr)
            else:
                sock.sendto(h3_response, addr)

        except socket.timeout:
            # Server failure response (RCODE = 2: SERVFAIL)
            err_header = struct.pack("!HHHHHH", trans_id, 0x8182, 1, 0, 0, 0)
            sock.sendto(err_header + b"SERVFAIL: Upstream timeout", addr)






