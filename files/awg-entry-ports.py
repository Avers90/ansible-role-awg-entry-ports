#!/usr/bin/env python3
# Managed by Ansible (ansible-role-awg-entry-ports)
"""Which UDP entry port does each AmneziaWG/WireGuard peer use?

Extra entry ports are nat REDIRECTs to the listen port, so awg itself only sees
the listen port. The original destination port lives in conntrack:

    orig:  src=<client> sport=<cport> dport=<entry port>
    reply: sport=<listen port>

Joining conntrack (client ip:port -> entry port) with `awg show endpoints`
(pubkey -> client ip:port) gives pubkey -> entry port. Only flows alive in
conntrack (UDP timeout, ~120 s) count, so stale endpoints of offline peers are
ignored.

    collect   read once, merge into the local buffer (timer, every few minutes)
    push      POST the buffer, clear it only on HTTP 2xx (timer, hourly)
    status    print the buffer summary
    show      print the current matches without touching the buffer
"""

import fcntl
import ipaddress
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

IFACE = os.environ.get("AWG_EP_IFACE", "awg0")
CLI = os.environ.get("AWG_EP_CLI", "awg")
SERVER = os.environ.get("AWG_EP_SERVER", "")
URL = os.environ.get("AWG_EP_URL", "")
TOKEN = os.environ.get("AWG_EP_TOKEN", "")
TIMEOUT = int(os.environ.get("AWG_EP_TIMEOUT", "30"))
STATE_DIR = os.environ.get("AWG_EP_STATE_DIR", "/var/lib/awg-entry-ports")
MAX_ENTRIES = int(os.environ.get("AWG_EP_MAX_ENTRIES", "50000"))

BUFFER = os.path.join(STATE_DIR, "buffer.json")
LOCK = os.path.join(STATE_DIR, "lock")
KV_RE = re.compile(r"(src|dst|sport|dport)=(\S+)")


def log(msg):
    print(msg, flush=True)


def run(cmd):
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if res.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)}: rc={res.returncode} {res.stderr.strip()}")
    return res.stdout


def norm_ip(ip):
    return ipaddress.ip_address(ip.strip("[]")).compressed


def listen_port():
    return int(run([CLI, "show", IFACE, "listen-port"]).strip())


def parse_conntrack(text):
    """conntrack -L lines -> {(client_ip, client_port): entry_port}."""
    flows = {}
    for line in text.splitlines():
        pairs = KV_RE.findall(line)
        orig = {}
        for key, val in pairs:
            if key in orig:  # second src= starts the reply tuple
                break
            orig[key] = val
        if not {"src", "sport", "dport"} <= orig.keys():
            continue
        try:
            flows[(norm_ip(orig["src"]), int(orig["sport"]))] = int(orig["dport"])
        except ValueError:
            continue
    return flows


def parse_endpoints(text):
    """`awg show <iface> endpoints` -> {(client_ip, client_port): pubkey}."""
    peers = {}
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) != 2 or parts[1] == "(none)":
            continue
        host, _, port = parts[1].rpartition(":")
        try:
            peers[(norm_ip(host), int(port))] = parts[0]
        except ValueError:
            continue
    return peers


def match(flows, peers):
    """-> {(pubkey, entry_port)}; unmatched flows (scanners) are dropped."""
    return {(pubkey, flows[key]) for key, pubkey in peers.items() if key in flows}


def snapshot():
    lport = listen_port()
    flows = {}
    for family in ("ipv4", "ipv6"):
        out = run(["conntrack", "-L", "-f", family, "-p", "udp",
                   "--reply-port-src", str(lport)])
        flows.update(parse_conntrack(out))
    peers = parse_endpoints(run([CLI, "show", IFACE, "endpoints"]))
    return lport, match(flows, peers)


def load():
    try:
        with open(BUFFER) as fh:
            data = json.load(fh)
        return data.get("entries", {})
    except (FileNotFoundError, ValueError):
        return {}


def save(entries):
    tmp = BUFFER + ".tmp"
    with open(tmp, "w") as fh:
        json.dump({"version": 1, "entries": entries}, fh)
    os.replace(tmp, BUFFER)


def collect():
    lport, pairs = snapshot()
    now = int(time.time())
    entries = load()
    for pubkey, port in pairs:
        key = f"{pubkey}|{port}"
        cur = entries.get(key)
        if cur:
            cur["last_seen"] = now
            cur["samples"] += 1
        elif len(entries) < MAX_ENTRIES:
            entries[key] = {"first_seen": now, "last_seen": now, "samples": 1}
    save(entries)
    by_port = {}
    for _, port in pairs:
        by_port[port] = by_port.get(port, 0) + 1
    log(f"collect: listen={lport} matched={len(pairs)} by_port={by_port} buffer={len(entries)}")


def push():
    if not URL or not TOKEN:
        log("push: URL or token not set, skipped")
        return
    entries = load()
    body = {
        "server": SERVER,
        "listen_port": listen_port(),
        "sent_at": int(time.time()),
        "entries": [
            {"pubkey": key.rsplit("|", 1)[0], "port": int(key.rsplit("|", 1)[1]), **val}
            for key, val in entries.items()
        ],
    }
    req = urllib.request.Request(
        URL,
        data=json.dumps(body).encode(),
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {TOKEN}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            status = resp.status
    except urllib.error.HTTPError as exc:
        log(f"push: HTTP {exc.code}, buffer kept ({len(entries)} entries)")
        sys.exit(1)
    except (urllib.error.URLError, OSError) as exc:
        log(f"push: {exc}, buffer kept ({len(entries)} entries)")
        sys.exit(1)
    save({})
    log(f"push: HTTP {status}, sent {len(entries)} entries")


def status():
    entries = load()
    by_port = {}
    for key in entries:
        port = key.rsplit("|", 1)[1]
        by_port[port] = by_port.get(port, 0) + 1
    log(json.dumps({"entries": len(entries), "by_port": by_port}))


def show():
    lport, pairs = snapshot()
    log(f"listen={lport}")
    for pubkey, port in sorted(pairs, key=lambda p: (p[1], p[0])):
        log(f"{port}\t{pubkey}")


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    actions = {"collect": collect, "push": push, "status": status, "show": show}
    if cmd not in actions:
        sys.exit(f"usage: {sys.argv[0]} {{{'|'.join(actions)}}}")
    os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
    with open(LOCK, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        actions[cmd]()


if __name__ == "__main__":
    main()
