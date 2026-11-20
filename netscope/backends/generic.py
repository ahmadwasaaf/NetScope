"""Universal adapter – works with almost any home router, no login needed.

* Whole-line usage + live speed: UPnP IGD counters (WANCommonInterfaceConfig), which nearly
  every consumer router exposes when UPnP is enabled.
* Device list: this PC's neighbour (ARP) table – read-only, nothing is sent to other devices
  except a normal ping to refresh the table.

Per-device usage, cutting devices and website control are not possible without the router's
own admin API, so this adapter reports only the capabilities it can really deliver.
"""
import ipaddress
import re
import socket
import subprocess
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin

from .base import Cap, CounterDelta, Device, RouterBackend, Snapshot, guess_type

SSDP_ADDR = ("239.255.255.250", 1900)
WAN_SERVICE = "urn:schemas-upnp-org:service:WANCommonInterfaceConfig:1"
NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def _run(cmd: List[str], timeout: int = 6) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                              creationflags=NO_WINDOW, errors="replace").stdout
    except Exception:
        return ""


# --------------------------------------------------------------------------- UPnP
class UPnPCounter:
    def __init__(self, control_url: str, friendly: str = ""):
        self.control_url = control_url
        self.friendly = friendly

    @classmethod
    def discover(cls, timeout: float = 2.5) -> Optional["UPnPCounter"]:
        msg = ("M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: \"ssdp:discover\"\r\n"
               "MX: 2\r\nST: urn:schemas-upnp-org:device:InternetGatewayDevice:1\r\n\r\n")
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        s.settimeout(timeout)
        locations = []
        try:
            s.sendto(msg.encode(), SSDP_ADDR)
            end = time.time() + timeout
            while time.time() < end:
                data, _ = s.recvfrom(4096)
                m = re.search(r"(?im)^location:\s*(\S+)", data.decode(errors="replace"))
                if m and m.group(1) not in locations:
                    locations.append(m.group(1))
        except (socket.timeout, OSError):
            pass
        finally:
            s.close()
        for loc in locations:
            found = cls._from_description(loc)
            if found:
                return found
        return None

    @classmethod
    def _from_description(cls, location: str) -> Optional["UPnPCounter"]:
        try:
            xml = urllib.request.urlopen(location, timeout=4).read()
            root = ET.fromstring(xml)
        except Exception:
            return None
        ns = {"d": "urn:schemas-upnp-org:device-1-0"}
        friendly = root.findtext(".//d:friendlyName", default="", namespaces=ns)
        model = root.findtext(".//d:modelName", default="", namespaces=ns)
        for svc in root.iter("{urn:schemas-upnp-org:device-1-0}service"):
            if svc.findtext("d:serviceType", namespaces=ns) == WAN_SERVICE:
                ctrl = svc.findtext("d:controlURL", namespaces=ns)
                return cls(urljoin(location, ctrl), (friendly or model).strip())
        return None

    def _call(self, action: str) -> int:
        body = (f'<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
                f's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
                f'<u:{action} xmlns:u="{WAN_SERVICE}"/></s:Body></s:Envelope>')
        req = urllib.request.Request(self.control_url, data=body.encode(), headers={
            "Content-Type": 'text/xml; charset="utf-8"', "SOAPAction": f'"{WAN_SERVICE}#{action}"'})
        resp = urllib.request.urlopen(req, timeout=4).read().decode(errors="replace")
        m = re.search(r"<\w*:?New\w+>(\d+)<", resp) or re.search(r">(\d+)<", resp)
        if not m:
            raise RuntimeError(f"Unexpected UPnP answer for {action}")
        return int(m.group(1))

    def totals(self) -> Tuple[int, int]:
        return self._call("GetTotalBytesReceived"), self._call("GetTotalBytesSent")


class WanMeter:
    """UPnP counters -> (delta bytes, down B/s, up B/s)."""

    def __init__(self, upnp: UPnPCounter):
        self.upnp = upnp
        self.delta = CounterDelta()
        self.t = None

    def read(self) -> Tuple[int, Optional[float], Optional[float]]:
        rx, tx = self.upnp.totals()
        now = time.monotonic()
        d_rx, d_tx = self.delta("rx", rx, wrap32=True), self.delta("tx", tx, wrap32=True)
        dt = (now - self.t) if self.t else None
        self.t = now
        if not dt:
            return 0, None, None
        return d_rx + d_tx, d_rx / dt, d_tx / dt


# --------------------------------------------------------------------------- ARP
# the 3rd column ("dynamic") is translated on non-English Windows, so don't depend on it
ARP_WIN = re.compile(r"^\s*(\d+\.\d+\.\d+\.\d+)\s+([0-9a-f]{2}(?:-[0-9a-f]{2}){5})\b", re.I | re.M)
ARP_NIX = re.compile(r"\((\d+\.\d+\.\d+\.\d+)\) at ([0-9a-f]{1,2}(?::[0-9a-f]{1,2}){5})", re.I)


def arp_table() -> Dict[str, str]:
    out = _run(["arp", "-a"])
    pairs = ARP_WIN.findall(out) or ARP_NIX.findall(out)
    table = {}
    for ip, mac in pairs:
        mac = ":".join(p.zfill(2) for p in re.split("[-:]", mac.lower()))
        first = int(mac[:2], 16)
        if mac == "ff:ff:ff:ff:ff:ff" or first & 1:      # broadcast / multicast
            continue
        table[ip] = mac
    return table


class NameCache:
    def __init__(self):
        self.names: Dict[str, str] = {}
        self.pool = ThreadPoolExecutor(max_workers=8)

    def lookup(self, ips) -> None:
        def resolve(ip):
            try:
                self.names[ip] = socket.gethostbyaddr(ip)[0].split(".")[0]
            except Exception:
                self.names[ip] = ""
        for ip in ips:
            if ip not in self.names:
                self.names[ip] = ""
                self.pool.submit(resolve, ip)


class GenericBackend(RouterBackend):
    kind = "generic"

    def __init__(self, gateway: str = ""):
        super().__init__()
        self.gateway = gateway
        self.wan: Optional[WanMeter] = None
        self.names = NameCache()
        self._last_sweep = 0.0

    def connect(self) -> None:
        upnp = UPnPCounter.discover()
        self.capabilities = {Cap.DEVICE_LIST}
        if upnp:
            self.wan = WanMeter(upnp)
            self.capabilities |= {Cap.TOTAL_USAGE, Cap.LIVE_SPEED}
            self.router_name = upnp.friendly
        self.router_name = self.router_name or "Router"

    def _sweep(self):
        """Refresh the ARP table about every 2 minutes with one quick ping per address."""
        if not self.gateway or time.time() - self._last_sweep < 120:
            return
        self._last_sweep = time.time()
        try:
            net = ipaddress.ip_network(self.gateway + "/24", strict=False)
        except ValueError:
            return
        flag = ["-n", "1", "-w", "300"] if sys.platform == "win32" else ["-c", "1", "-W", "1"]
        with ThreadPoolExecutor(max_workers=48) as pool:
            for host in net.hosts():
                pool.submit(_run, ["ping", *flag, str(host)], 3)

    def poll(self) -> Snapshot:
        if self.wan is None and not self.capabilities:
            self.connect()
        s = Snapshot(router_name=self.router_name)
        if self.wan:
            try:
                s.wan_delta, s.down_bps, s.up_bps = self.wan.read()
            except Exception:
                pass
        self._sweep()
        table = arp_table()
        prefix = ".".join(self.gateway.split(".")[:3]) + "." if self.gateway else ""
        ips = [ip for ip in table if ip.startswith(prefix) and not ip.endswith(".255") and ip != self.gateway]
        self.names.lookup(ips)
        for ip in sorted(ips, key=lambda x: tuple(int(p) for p in x.split("."))):
            name = self.names.names.get(ip) or ip
            s.devices.append(Device(table[ip], name, ip, guess_type(name)))
        return s
