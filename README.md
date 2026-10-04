# ansible-role-awg-entry-ports

Find out which UDP entry port each AmneziaWG / WireGuard peer connects to, when a
server accepts several ports (e.g. `51820`, `443`, `27015`) via nat `REDIRECT`
to one listen port.

## Why conntrack

After `REDIRECT`, awg (and any dashboard on top of it) only sees the listen
port. The original destination port survives only in conntrack:

```
client 31.4.5.6:41000 → :27015 ─ nat REDIRECT ─▶ awg :51820
conntrack: src=31.4.5.6 sport=41000 dport=27015 | reply sport=51820
awg show endpoints: <pubkey>  31.4.5.6:41000
                    ⇒ <pubkey> uses 27015
```

The join key is the client `ip:port`, so peers behind one CGNAT address are
told apart. Only flows alive in conntrack (UDP timeout, ~120 s) are counted:
stale endpoints of offline peers and scanners that never completed a handshake
are ignored.

## How it runs

| Unit | When | What |
|---|---|---|
| `awg-entry-ports-collect.timer` | every 5 min | one `conntrack -L` + one `awg show endpoints`, merge into `/var/lib/awg-entry-ports/buffer.json` |
| `awg-entry-ports-push.timer` | hourly + up to 10 min jitter | POST the buffer, clear it only on HTTP 2xx |

Both run `Nice=19`, `IOSchedulingClass=idle`, `CPUQuota=5%`, `MemoryMax=64M`,
with `ProtectSystem=strict` and only `CAP_NET_ADMIN`. A failed push keeps the
buffer; the next push sends everything. The buffer is bounded by peers × ports.

Without `awg_entry_ports_push_url` only collection runs (push timer disabled).

Every run writes one line to journald and to
`/var/log/awg-entry-ports/awg-entry-ports.log` (UTC timestamp, rotated weekly).
journald on busy hosts rotates by size together with everything else, so the
file gives a predictable history. Errors (conntrack / awg failed, HTTP errors)
land there too:

```
2026-10-04T12:05:09Z collect: listen=51820 matched=20 by_port={51820: 19, 27015: 1} buffer=22
2026-10-04T13:04:41Z push: HTTP 200, sent 22 entries
2026-10-04T14:07:12Z push: HTTP 500, buffer kept (25 entries)
```

## Requirements

- Debian / Ubuntu, `python3` (stdlib only)
- AmneziaWG (`awg`) or WireGuard (`wg`) interface
- Entry ports as nat `REDIRECT` to the listen port (e.g. `ansible-role-firewall`
  `firewall_wireguard_extra_ports_udp`)

## Role Variables

| Variable | Default | Description |
|---|---|---|
| `awg_entry_ports_interface` | `amneziawg_interface` or `awg0` | Interface to inspect |
| `awg_entry_ports_wg_cli` | `awg` | `awg` or `wg` |
| `awg_entry_ports_server_code` | `inventory_hostname_short` | Server id sent in each batch |
| `awg_entry_ports_collect_interval` | `5min` | Collect interval (systemd time span) |
| `awg_entry_ports_push_url` | `""` | HTTPS endpoint; empty = collect only |
| `awg_entry_ports_push_token` | `""` | Bearer token (vault), required with URL |
| `awg_entry_ports_push_on_calendar` | `hourly` | Push schedule (`OnCalendar`) |
| `awg_entry_ports_push_randomized_delay` | `10min` | Push jitter |
| `awg_entry_ports_push_timeout` | `30` | HTTP timeout, seconds |
| `awg_entry_ports_max_entries` | `50000` | Buffer cap (peer × port pairs) |
| `awg_entry_ports_cpu_quota` / `_memory_max` | `5%` / `64M` | systemd limits |
| `awg_entry_ports_log_dir` | `/var/log/awg-entry-ports` | Own log file (UTC timestamps), empty = journald only |
| `awg_entry_ports_log_rotate_weeks` | `8` | logrotate: weekly, N kept |

## Push format

`POST <url>`, `Authorization: Bearer <token>`, `Content-Type: application/json`:

```json
{
  "server": "awg-de-01",
  "listen_port": 51820,
  "sent_at": 1791140400,
  "entries": [
    {"pubkey": "Ab3k...=", "port": 27015, "first_seen": 1791137100, "last_seen": 1791140100, "samples": 11}
  ]
}
```

Times are unix seconds (UTC). One entry per (peer, entry port) seen since the
last successful push; a peer that switched ports appears twice. An empty
`entries` list is still sent — it works as a heartbeat. Any 2xx clears the buffer.

## Example Playbook

```yaml
- hosts: awg_servers
  become: true
  roles:
    - role: awg-entry-ports
      vars:
        awg_entry_ports_push_url: "https://example.com/internal/awg-entry-ports"
        awg_entry_ports_push_token: "{{ vault_awg_entry_ports_token }}"
```

## Manual checks

```bash
sudo awg-entry-ports show      # current matches, buffer untouched (needs env: see below)
sudo systemctl start awg-entry-ports@collect.service && journalctl -u awg-entry-ports@collect -n 3
sudo systemctl start awg-entry-ports@push.service    && journalctl -u awg-entry-ports@push -n 3
sudo sh -c 'set -a; . /etc/awg-entry-ports.env; awg-entry-ports status'
sudo tail -n 20 /var/log/awg-entry-ports/awg-entry-ports.log
```

`show`/`status` read the interface from `/etc/awg-entry-ports.env`; run them
through `set -a; . /etc/awg-entry-ports.env` as in the last line.

## License

MIT
