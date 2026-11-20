"""This-PC monitor: real bytes per website for the computer NetScope runs on.

Reads THIS computer's own network traffic through Npcap (the capture driver Wireshark
uses, https://npcap.com) and attributes every byte to a website:
  1. TLS ClientHello SNI  – the site name a browser/app asks for on HTTPS (most accurate)
  2. DNS answers          – which IP addresses a name resolved to
  3. otherwise            – counted as "Other / unresolved"
Only packet headers + the first bytes of connections are read; nothing is stored except
per-site byte totals.
"""
import ctypes
import os
import socket
import struct
import sys
import threading
from collections import defaultdict
from typing import Dict, Optional, Set, Tuple

from .domains import is_public, site_of

UNRESOLVED = "Other / unresolved"
Flow = Tuple[int, str, int, int]          # proto, remote ip, remote port, local port


# ============================================================ protocol parsing (pure)
def parse_sni(payload: bytes) -> Optional[str]:
    """Server name from a TLS ClientHello, or None."""
    try:
        if len(payload) < 43 or payload[0] != 0x16 or payload[5] != 0x01:
            return None
        p = 43                                   # record(5)+handshake hdr(4)+version(2)+random(32)
        p += 1 + payload[p]                      # session id
        p += 2 + struct.unpack("!H", payload[p:p + 2])[0]      # cipher suites
        p += 1 + payload[p]                      # compression
        end = p + 2 + struct.unpack("!H", payload[p:p + 2])[0]
        p += 2
        while p + 4 <= min(end, len(payload)):
            etype, elen = struct.unpack("!HH", payload[p:p + 4])
            p += 4
            if etype == 0:                       # server_name
                q = p + 2                        # list length
                if payload[q] == 0:              # host_name
                    nlen = struct.unpack("!H", payload[q + 1:q + 3])[0]
                    return payload[q + 3:q + 3 + nlen].decode("ascii", "ignore").lower()
                return None
            p += elen
    except (IndexError, struct.error):
        return None
    return None


def _dns_name(msg: bytes, p: int, depth: int = 0) -> Tuple[str, int]:
    labels = []
    jumped, ret = False, p
    while p < len(msg) and depth < 20:
        ln = msg[p]
        if ln == 0:
            p += 1
            break
        if ln & 0xC0 == 0xC0:                    # compression pointer
            if not jumped:
                ret = p + 2
            p = ((ln & 0x3F) << 8) | msg[p + 1]
            jumped, depth = True, depth + 1
            continue
        labels.append(msg[p + 1:p + 1 + ln].decode("ascii", "ignore"))
        p += 1 + ln
    return ".".join(labels).lower(), (ret if jumped else p)


def parse_dns(msg: bytes):
    """-> (is_response, question name, [answer IPs]) or None."""
    try:
        if len(msg) < 12:
            return None
        flags, qd, an = struct.unpack("!HHH", msg[2:8])
        p = 12
        qname = ""
        for _ in range(qd):
            qname, p = _dns_name(msg, p)
            p += 4
        ips = []
        for _ in range(an):
            _, p = _dns_name(msg, p)
            rtype, _, _, rdlen = struct.unpack("!HHIH", msg[p:p + 10])
            p += 10
            rdata = msg[p:p + rdlen]
            if rtype == 1 and rdlen == 4:
                ips.append(socket.inet_ntop(socket.AF_INET, rdata))
            elif rtype == 28 and rdlen == 16:
                ips.append(socket.inet_ntop(socket.AF_INET6, rdata))
            p += rdlen
        return bool(flags & 0x8000), qname, ips
    except (IndexError, struct.error, ValueError, OSError):
        return None


class TrafficAccountant:
    """Feeds on raw Ethernet frames, keeps bytes per website."""

    def __init__(self, local_ips: Set[str]):
        self.local_ips = set(local_ips)
        self.ip_name: Dict[str, str] = {}
        self.flow_name: Dict[Flow, str] = {}
        self.pending: Dict[Flow, int] = defaultdict(int)
        self.lock = threading.Lock()

    def feed(self, frame: bytes, wire_len: int) -> None:
        if len(frame) < 14:
            return
        etype = struct.unpack("!H", frame[12:14])[0]
        off = 14
        if etype == 0x8100 and len(frame) >= 18:          # VLAN tag
            etype = struct.unpack("!H", frame[16:18])[0]
            off = 18
        if etype == 0x0800:
            ihl = (frame[off] & 0x0F) * 4
            proto = frame[off + 9]
            src = socket.inet_ntop(socket.AF_INET, frame[off + 12:off + 16])
            dst = socket.inet_ntop(socket.AF_INET, frame[off + 16:off + 20])
            l4 = off + ihl
        elif etype == 0x86DD:
            proto = frame[off + 6]
            src = socket.inet_ntop(socket.AF_INET6, frame[off + 8:off + 24])
            dst = socket.inet_ntop(socket.AF_INET6, frame[off + 24:off + 40])
            l4 = off + 40
        else:
            return
        if proto not in (6, 17) or len(frame) < l4 + 8:
            return
        sport, dport = struct.unpack("!HH", frame[l4:l4 + 4])
        if proto == 6:
            payload = frame[l4 + ((frame[l4 + 12] >> 4) * 4):]
        else:
            payload = frame[l4 + 8:]

        # DNS answers (from any server, including the router) teach IP -> name
        if proto == 17 and sport == 53:
            d = parse_dns(payload)
            if d and d[0] and d[1]:
                with self.lock:
                    for ip in d[2]:
                        self.ip_name[ip] = d[1]
            return

        outgoing = src in self.local_ips
        if not outgoing and dst not in self.local_ips:
            return
        remote, rport, lport = (dst, dport, sport) if outgoing else (src, sport, dport)
        if not is_public(remote):
            return                                    # LAN traffic is not internet usage
        flow = (proto, remote, rport, lport)
        with self.lock:
            if outgoing and proto == 6 and payload[:1] == b"\x16":
                sni = parse_sni(payload)
                if sni:
                    self.flow_name[flow] = sni
                    self.ip_name.setdefault(remote, sni)
            self.pending[flow] += wire_len

    def drain(self) -> Dict[str, int]:
        """Bytes per website since the last call."""
        with self.lock:
            pending, self.pending = self.pending, defaultdict(int)
            out: Dict[str, int] = defaultdict(int)
            for flow, n in pending.items():
                host = self.flow_name.get(flow) or self.ip_name.get(flow[1])
                out[site_of(host) or UNRESOLVED] += n
            if len(self.flow_name) > 20000:          # keep memory bounded
                self.flow_name.clear()
            return dict(out)


# ============================================================ Npcap capture (Windows)
class _Timeval(ctypes.Structure):
    _fields_ = [("tv_sec", ctypes.c_long), ("tv_usec", ctypes.c_long)]


class _PktHdr(ctypes.Structure):
    _fields_ = [("ts", _Timeval), ("caplen", ctypes.c_uint32), ("len", ctypes.c_uint32)]


class _SockAddr(ctypes.Structure):
    _fields_ = [("family", ctypes.c_ushort), ("data", ctypes.c_ubyte * 26)]


class _PcapAddr(ctypes.Structure):
    pass


_PcapAddr._fields_ = [("next", ctypes.POINTER(_PcapAddr)), ("addr", ctypes.POINTER(_SockAddr)),
                      ("netmask", ctypes.c_void_p), ("broadaddr", ctypes.c_void_p),
                      ("dstaddr", ctypes.c_void_p)]


class _PcapIf(ctypes.Structure):
    pass


_PcapIf._fields_ = [("next", ctypes.POINTER(_PcapIf)), ("name", ctypes.c_char_p),
                    ("description", ctypes.c_char_p), ("addresses", ctypes.POINTER(_PcapAddr)),
                    ("flags", ctypes.c_uint)]


def load_npcap():
    """wpcap.dll from Npcap, or None when Npcap is not installed."""
    if sys.platform != "win32":
        return None
    npcap_dir = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "Npcap")
    try:
        if os.path.isdir(npcap_dir):
            os.add_dll_directory(npcap_dir)
            return ctypes.WinDLL(os.path.join(npcap_dir, "wpcap.dll"))
        return ctypes.WinDLL("wpcap.dll")
    except OSError:
        return None


def _primary_ipv4() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return ""


class PcMonitor:
    """Background capture thread. status: 'off' | 'running' | 'no_npcap' | 'error:<msg>'."""

    def __init__(self):
        self.status = "off"
        self.acct: Optional[TrafficAccountant] = None
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="NetScope-PcMonitor", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def drain(self) -> Dict[str, int]:
        return self.acct.drain() if self.acct else {}

    def _run(self):
        lib = load_npcap()
        if lib is None:
            self.status = "no_npcap"
            return
        lib.pcap_open_live.restype = ctypes.c_void_p
        lib.pcap_open_live.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_char_p]
        lib.pcap_next_ex.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.POINTER(_PktHdr)),
                                     ctypes.POINTER(ctypes.POINTER(ctypes.c_ubyte))]
        lib.pcap_close.argtypes = [ctypes.c_void_p]
        lib.pcap_findalldevs.argtypes = [ctypes.POINTER(ctypes.POINTER(_PcapIf)), ctypes.c_char_p]
        lib.pcap_freealldevs.argtypes = [ctypes.POINTER(_PcapIf)]
        err = ctypes.create_string_buffer(256)
        devs = ctypes.POINTER(_PcapIf)()
        if lib.pcap_findalldevs(ctypes.byref(devs), err) != 0:
            self.status = "error:" + err.value.decode(errors="replace")
            return
        want = _primary_ipv4()
        name, local_ips = None, set()
        d = devs
        while d:
            ips, a = set(), d.contents.addresses
            while a:
                sa = a.contents.addr
                if sa and sa.contents.family == socket.AF_INET:
                    ips.add(socket.inet_ntop(socket.AF_INET, bytes(sa.contents.data[2:6])))
                elif sa and sa.contents.family == 23:            # AF_INET6 on Windows
                    ips.add(socket.inet_ntop(socket.AF_INET6, bytes(sa.contents.data[6:22])))
                a = a.contents.next
            if want and want in ips:
                name, local_ips = d.contents.name, ips
                break
            d = d.contents.next
        if name is None:
            lib.pcap_freealldevs(devs)
            self.status = "error:network adapter not found"
            return
        handle = lib.pcap_open_live(name, 2048, 0, 500, err)
        lib.pcap_freealldevs(devs)
        if not handle:
            self.status = "error:" + err.value.decode(errors="replace")
            return
        self.acct = TrafficAccountant(local_ips)
        self.status = "running"
        hdr = ctypes.POINTER(_PktHdr)()
        data = ctypes.POINTER(ctypes.c_ubyte)()
        try:
            while not self._stop.is_set():
                r = lib.pcap_next_ex(handle, ctypes.byref(hdr), ctypes.byref(data))
                if r == 1:
                    h = hdr.contents
                    self.acct.feed(ctypes.string_at(data, h.caplen), h.len)
                elif r < 0:
                    self.status = "error:capture stopped"
                    break
        finally:
            lib.pcap_close(handle)
            if self.status == "running":
                self.status = "off"
