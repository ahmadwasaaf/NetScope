"""OpenWrt backend (administrator access over SSH).

Router-side requirements (install once from LuCI › System › Software, or `opkg install`):
  * nlbwmon            – per-device byte counters
  * conntrack accounting: `sysctl -w net.netfilter.nf_conntrack_acct=1`
  * dnsmasq query log:  `uci set dhcp.@dnsmasq[0].logqueries='1'; uci commit dhcp`
Device blocking  -> firewall rule (REJECT, by MAC) named ns_<mac>
Website blocking -> dnsmasq `address=/domain/` entry (domain resolves to nothing)
"""
import json
import re
import shlex
from typing import Dict, Tuple

from .base import (Cap, Device, RouterBackend, Snapshot, clean_domain, clean_mac,
                   guess_type)

try:
    import paramiko
except ImportError:  # pragma: no cover
    paramiko = None

TWO_LEVEL_TLDS = {"co.uk", "com.eg", "com.sa", "co.jp", "com.au", "com.br", "org.uk", "gov.eg", "edu.eg"}
CONNTRACK_RE = re.compile(
    r"src=(?P<src>\S+) dst=(?P<dst>\S+)(?: sport=(?P<sp>\d+) dport=(?P<dp>\d+))?.*?bytes=(?P<b1>\d+)"
    r".*?bytes=(?P<b2>\d+)")
DNS_REPLY_RE = re.compile(r"reply (\S+) is (\d+\.\d+\.\d+\.\d+|[0-9a-f:]+)$")


def base_domain(host: str) -> str:
    parts = host.lower().rstrip(".").split(".")
    if len(parts) >= 3 and ".".join(parts[-2:]) in TWO_LEVEL_TLDS:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


class OpenWrtBackend(RouterBackend):
    kind = "openwrt"

    def __init__(self, host: str, port: int, username: str, password: str):
        super().__init__()
        self.host, self.port, self.username, self.password = host, port, username, password
        self.client = None
        self._prev_dev: Dict[str, int] = {}
        self._prev_conn: Dict[Tuple, int] = {}
        self._ip_to_domain: Dict[str, str] = {}

    # ------------------------------------------------------------------ ssh
    def connect(self) -> None:
        if paramiko is None:
            raise RuntimeError("paramiko is not installed (pip install paramiko)")
        self.client = paramiko.SSHClient()
        self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self.client.connect(self.host, port=self.port, username=self.username,
                            password=self.password, timeout=8, look_for_keys=False)
        model = self._run("cat /tmp/sysinfo/model 2>/dev/null").strip()
        self.router_name = f"OpenWrt · {model}" if model else "OpenWrt"
        self.capabilities = {Cap.DEVICE_LIST, Cap.DEVICE_BLOCK, Cap.SITE_BLOCK}
        if self._run("command -v nlbw").strip():
            self.capabilities.add(Cap.DEVICE_USAGE)
        if self._run("test -r /proc/net/nf_conntrack && echo y").strip():
            self.capabilities.add(Cap.SITE_USAGE)

    def _run(self, cmd: str, check: bool = False) -> str:
        if self.client is None:
            self.connect()
        _, out, err = self.client.exec_command(cmd, timeout=20)
        text = out.read().decode("utf-8", "replace")
        if check and out.channel.recv_exit_status() != 0:
            raise RuntimeError(err.read().decode("utf-8", "replace").strip() or cmd)
        return text

    def close(self) -> None:
        if self.client:
            self.client.close()
            self.client = None

    # ------------------------------------------------------------------ poll
    def poll(self) -> Snapshot:
        if self.client is None:
            self.connect()
        s = Snapshot(router_name=self.router_name)
        s.devices = self._devices()
        s.device_deltas = self._device_deltas()
        s.site_deltas = self._site_deltas()
        s.blocked_macs = self._blocked_macs()
        s.blocked_domains = self._blocked_domains()
        return s

    def _devices(self):
        devs = []
        for line in self._run("cat /tmp/dhcp.leases 2>/dev/null").splitlines():
            parts = line.split()
            if len(parts) >= 4:
                mac, ip, name = parts[1].lower(), parts[2], parts[3]
                if name == "*":
                    name = mac
                devs.append(Device(mac, name, ip, guess_type(name)))
        return devs

    def _device_deltas(self) -> Dict[str, int]:
        raw = self._run("nlbw -c json -g mac 2>/dev/null")
        deltas: Dict[str, int] = {}
        try:
            data = json.loads(raw)
        except ValueError:
            return deltas
        cols = data.get("columns", [])
        try:
            i_mac, i_rx, i_tx = cols.index("mac"), cols.index("rx_bytes"), cols.index("tx_bytes")
        except ValueError:
            return deltas
        for row in data.get("data", []):
            mac = str(row[i_mac]).lower()
            total = int(row[i_rx]) + int(row[i_tx])
            prev = self._prev_dev.get(mac)
            if prev is not None:
                # nlbwmon period rolled over -> counter restarted from zero
                deltas[mac] = total - prev if total >= prev else total
            self._prev_dev[mac] = total
        return deltas

    def _refresh_dns_map(self) -> None:
        for line in self._run("logread -e 'reply ' 2>/dev/null | tail -n 3000").splitlines():
            m = DNS_REPLY_RE.search(line.strip())
            if m:
                self._ip_to_domain[m.group(2)] = base_domain(m.group(1))

    def _site_deltas(self) -> Dict[str, int]:
        self._refresh_dns_map()
        raw = self._run("cat /proc/net/nf_conntrack 2>/dev/null")
        deltas: Dict[str, int] = {}
        seen = {}
        for line in raw.splitlines():
            m = CONNTRACK_RE.search(line)
            if not m:
                continue
            key = (m["src"], m["dst"], m["sp"], m["dp"])
            total = int(m["b1"]) + int(m["b2"])
            seen[key] = total
            domain = self._ip_to_domain.get(m["dst"])
            if not domain:
                continue
            prev = self._prev_conn.get(key, 0 if self._prev_conn else None)
            if prev is None:            # first poll: only set the baseline
                continue
            if total > prev:
                deltas[domain] = deltas.get(domain, 0) + total - prev
        self._prev_conn = seen
        return deltas

    def _blocked_macs(self):
        out = self._run("uci -q show firewall | grep '^firewall\\.ns_[0-9a-f]*\\.src_mac='")
        return {line.split("=", 1)[1].strip("'").lower()
                for line in out.splitlines() if ".src_mac=" in line}

    def _blocked_domains(self):
        out = self._run("uci -q get dhcp.@dnsmasq[0].address")
        found = set()
        for item in out.split():
            m = re.fullmatch(r"'?/([^/]+)/'?", item)
            if m:
                found.add(m.group(1))
        return found

    # --------------------------------------------------------------- actions
    def block_device(self, mac: str) -> None:
        mac = clean_mac(mac)
        sec = "ns_" + mac.replace(":", "")
        self._run(" && ".join([
            f"uci set firewall.{sec}=rule",
            f"uci set firewall.{sec}.name='NetScope block {mac}'",
            f"uci set firewall.{sec}.src='lan'",
            f"uci set firewall.{sec}.dest='wan'",
            f"uci set firewall.{sec}.src_mac='{mac}'",
            f"uci set firewall.{sec}.target='REJECT'",
            "uci commit firewall",
            "/etc/init.d/firewall reload >/dev/null 2>&1",
        ]), check=True)

    def unblock_device(self, mac: str) -> None:
        sec = "ns_" + clean_mac(mac).replace(":", "")
        self._run(f"uci -q delete firewall.{sec}; uci commit firewall && "
                  "/etc/init.d/firewall reload >/dev/null 2>&1", check=True)

    def block_domain(self, domain: str) -> None:
        entry = shlex.quote(f"/{clean_domain(domain)}/")
        self._run(f"uci add_list dhcp.@dnsmasq[0].address={entry} && uci commit dhcp && "
                  "/etc/init.d/dnsmasq restart >/dev/null 2>&1", check=True)

    def unblock_domain(self, domain: str) -> None:
        entry = shlex.quote(f"/{clean_domain(domain)}/")
        self._run(f"uci del_list dhcp.@dnsmasq[0].address={entry}; uci commit dhcp && "
                  "/etc/init.d/dnsmasq restart >/dev/null 2>&1", check=True)
