"""Diagnose exactly how the HuggingFace Hub is being blocked.

The fix depends entirely on the mechanism, and the three candidates need different
remedies:

  DNS blocking          the name does not resolve, or resolves to a sinkhole
                        -> change resolver (1.1.1.1 / 8.8.8.8) or use DoH
  SNI / DPI filtering   TCP connects, then the connection is reset the moment the
                        TLS ClientHello reveals the hostname. Extremely common at
                        ISP level.
                        -> VPN, different network, or a mirror endpoint
  IP / firewall block   TCP never connects at all
                        -> VPN or different network

Run:  uv run python scripts/diagnose_hf.py
"""

from __future__ import annotations

import socket
import ssl
import time

HOSTS = [
    "huggingface.co",
    "cdn-lfs.huggingface.co",
    "cdn-lfs-us-1.hf.co",
    "hf-mirror.com",
    "github.com",          # control: known working
    "pypi.org",            # control: known working
]

RESOLVERS = {
    "system": None,
    "cloudflare 1.1.1.1": "1.1.1.1",
    "google 8.8.8.8": "8.8.8.8",
}


def resolve(host: str) -> tuple[bool, str]:
    try:
        return True, socket.gethostbyname(host)
    except Exception as e:
        return False, f"{type(e).__name__}"


def resolve_with(host: str, server: str) -> tuple[bool, str]:
    """Query a specific resolver over UDP, bypassing the system one.

    A minimal DNS/A query written by hand so this needs no extra dependency.
    """
    tid = b"\xab\xcd"
    header = tid + b"\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00"
    qname = b"".join(bytes([len(p)]) + p.encode() for p in host.split(".")) + b"\x00"
    packet = header + qname + b"\x00\x01\x00\x01"
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(4)
        s.sendto(packet, (server, 53))
        data, _ = s.recvfrom(512)
        s.close()
        ancount = int.from_bytes(data[6:8], "big")
        if ancount == 0:
            return False, "no answer records"
        # Walk past the question, then read the first A record's rdata.
        idx = 12 + len(qname) + 4
        for _ in range(ancount):
            idx += 2                                   # name pointer
            rtype = int.from_bytes(data[idx:idx + 2], "big")
            idx += 8                                   # type, class, ttl
            rdlen = int.from_bytes(data[idx:idx + 2], "big")
            idx += 2
            if rtype == 1 and rdlen == 4:
                return True, ".".join(str(b) for b in data[idx:idx + 4])
            idx += rdlen
        return False, "no A record"
    except Exception as e:
        return False, f"{type(e).__name__}"


def tcp_connect(host: str, port: int = 443, timeout: float = 6.0) -> tuple[bool, str, float]:
    """Can we open a plain TCP socket? Separates network reachability from TLS."""
    t0 = time.perf_counter()
    try:
        s = socket.create_connection((host, port), timeout=timeout)
        s.close()
        return True, "connected", (time.perf_counter() - t0) * 1000
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"[:70], (time.perf_counter() - t0) * 1000


def tls_handshake(host: str, port: int = 443, timeout: float = 8.0) -> tuple[bool, str]:
    """Complete a TLS handshake with SNI set.

    This is the decisive test. If TCP connects but this is reset, the block is
    keyed on the hostname in the ClientHello — that is SNI-based DPI filtering,
    and it is what an ISP does rather than a firewall.
    """
    ctx = ssl.create_default_context()
    try:
        with (
            socket.create_connection((host, port), timeout=timeout) as sock,
            ctx.wrap_socket(sock, server_hostname=host) as tls,
        ):
            cert = tls.getpeercert()
            issuer = dict(x[0] for x in cert.get("issuer", ())).get(
                "organizationName", "?"
            )
            return True, f"TLS {tls.version()}, issued by {issuer}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"[:70]


def main() -> None:
    print("\nHuggingFace Hub connectivity diagnosis")
    print("=" * 88)

    print("\n[1] DNS resolution")
    print(f"    {'host':<28}{'system':<22}{'1.1.1.1':<20}{'8.8.8.8'}")
    print("    " + "-" * 82)
    dns_results: dict[str, dict[str, str]] = {}
    for host in HOSTS:
        row = {}
        ok, val = resolve(host)
        row["system"] = val if ok else f"FAIL ({val})"
        for label, server in (("1.1.1.1", "1.1.1.1"), ("8.8.8.8", "8.8.8.8")):
            ok2, val2 = resolve_with(host, server)
            row[label] = val2 if ok2 else f"FAIL ({val2})"
        dns_results[host] = row
        print(f"    {host:<28}{row['system']:<22}{row['1.1.1.1']:<20}{row['8.8.8.8']}")

    print("\n[2] TCP reachability (port 443)")
    tcp_results: dict[str, bool] = {}
    for host in HOSTS:
        ok, detail, ms = tcp_connect(host)
        tcp_results[host] = ok
        mark = "OK  " if ok else "FAIL"
        print(f"    [{mark}] {host:<28} {ms:>6.0f}ms  {'' if ok else detail}")

    print("\n[3] TLS handshake with SNI  <- the decisive test")
    tls_results: dict[str, bool] = {}
    for host in HOSTS:
        if not tcp_results.get(host):
            print(f"    [skip] {host:<28} (no TCP)")
            tls_results[host] = False
            continue
        ok, detail = tls_handshake(host)
        tls_results[host] = ok
        mark = "OK  " if ok else "FAIL"
        print(f"    [{mark}] {host:<28} {detail}")

    # ── verdict ──────────────────────────────────────────────────────────
    print("\n" + "=" * 88)
    print("VERDICT")
    print("=" * 88)

    hf_dns = not dns_results["huggingface.co"]["system"].startswith("FAIL")
    hf_tcp = tcp_results.get("huggingface.co", False)
    hf_tls = tls_results.get("huggingface.co", False)
    control_ok = tls_results.get("github.com", False)

    if not control_ok:
        print("\n  Your whole network looks down — even GitHub failed. Check the")
        print("  connection before drawing conclusions about HuggingFace.")
        return

    if hf_tls:
        print("\n  HuggingFace is REACHABLE. Nothing to fix.")
        print("  Remove HF_HUB_OFFLINE=1 from .env and the open-vocabulary")
        print("  Grounding DINO backend will load automatically.")
        return

    if not hf_dns:
        print("\n  DNS-LEVEL BLOCK — the name does not resolve.")
        print("\n  Most likely fix: change your DNS resolver.")
        print("    Settings > Network > Change adapter options > IPv4 properties")
        print("    Preferred DNS: 1.1.1.1     Alternate: 8.8.8.8")
        if not dns_results["huggingface.co"]["1.1.1.1"].startswith("FAIL"):
            print("\n  Cloudflare DNS resolved it, so this should work.")
    elif hf_tcp and not hf_tls:
        print("\n  SNI / DPI FILTERING — TCP connects, then the connection is reset")
        print("  the moment TLS reveals the hostname. Your ISP or a security")
        print("  product is inspecting traffic and dropping this specific domain.")
        print("\n  Changing DNS will NOT help — the name already resolves.")
        print("\n  What does work, in order of effort:")
        print("    1. Mobile hotspot          — different ISP, usually unfiltered")
        print("    2. Any VPN                 — encrypts the SNI, defeats the filter")
        print("    3. Fetch weights elsewhere — see scripts/fetch_hf_models.py")
    elif not hf_tcp:
        print("\n  IP / FIREWALL BLOCK — TCP never connects.")
        print("  A firewall or security product is dropping packets to this host.")
        print("\n  Check Windows Defender Firewall and any third-party antivirus")
        print("  with HTTPS scanning, then try a VPN or a mobile hotspot.")

    # A cached model makes all of the above moot, so check before advising anyone
    # to change their network.
    cached = []
    try:
        from kestrel.perception.detect import hf_model_cached

        for repo in ("IDEA-Research/grounding-dino-tiny", "PekingU/rtdetr_r50vd_coco_o365"):
            if hf_model_cached(repo):
                cached.append(repo)
    except Exception:
        pass

    if cached:
        print("\n  BUT — the following are ALREADY CACHED locally and need no network:")
        for repo in cached:
            print(f"    · {repo}")
        print("\n  Nothing above affects them. Confirm with `uv run kestrel doctor`;")
        print("  the detector line should read 'grounding-dino ... open-vocabulary'.")
    else:
        print("\n  Try the mirror first — it is usually the whole fix:")
        print("    uv run python scripts/fetch_hf_models.py")
        print("\n  It only has to succeed once. After that the weights live in")
        print("  ~/.cache/huggingface and load with no network at all.")

    print("\n  And KESTREL does not require the Hub regardless. Detection falls back")
    print("  to YOLO11 (GitHub-hosted) and open-vocabulary queries route through the")
    print("  VLM — slower and coarser, but functional.")


if __name__ == "__main__":
    main()
