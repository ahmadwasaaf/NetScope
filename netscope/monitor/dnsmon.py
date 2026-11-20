"""Other-devices monitor: a small DNS forwarder running inside NetScope.

When the router hands out THIS computer as the network's DNS server, every device asks
NetScope for website addresses before connecting. NetScope forwards each question to the
real DNS server (the router) unchanged, and counts which websites were asked for. That
count ranks websites by activity – it is not a byte count, so the UI shows names only.

Recommended router setting: primary DNS = this PC, secondary DNS = the router itself,
so devices keep browsing normally whenever this PC is switched off.
"""
import socket
import struct
import threading
import time
from collections import defaultdict
from typing import Dict, Optional, Set, Tuple

from .domains import site_of
from .pcap import parse_dns

COUNTED_QTYPES = {1, 28, 65}        # A, AAAA, HTTPS


def _qtype(msg: bytes) -> int:
    """Question type of a DNS query (0 if unreadable)."""
    try:
        p = 12
        while msg[p] != 0:
            p += 1 + msg[p]
        return struct.unpack("!H", msg[p + 1:p + 3])[0]
    except (IndexError, struct.error):
        return 0


class DnsMonitor:
    """status: 'off' | 'running' | 'error:<msg>'"""

    def __init__(self, upstream: str, port: int = 53, exclude: Optional[Set[str]] = None):
        self.upstream = (upstream, 53) if isinstance(upstream, str) else upstream
        self.port = port
        self.exclude = set(exclude or ())          # this PC's own addresses
        self.status = "off"
        self.counts: Dict[str, int] = defaultdict(int)
        self.clients: Set[str] = set()          # other devices that used NetScope as DNS
        self.total_queries = 0                  # every question received (diagnostics)
        self.last_query = 0.0
        self.lock = threading.Lock()
        self.pending: Dict[int, Tuple[Tuple[str, int], int, float]] = {}
        self.next_id = 1
        self._stop = threading.Event()
        self.sock = self.up = None

    # ------------------------------------------------------------------ control
    def start(self):
        if self.status == "running":
            return
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.sock.bind(("0.0.0.0", self.port))
            self.sock.settimeout(0.5)
            self.up = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.up.settimeout(0.5)
        except OSError as e:
            self.status = f"error:{e.strerror or e}"
            return
        self._stop.clear()
        self.status = "running"
        threading.Thread(target=self._from_clients, name="NetScope-DNS-in", daemon=True).start()
        threading.Thread(target=self._from_upstream, name="NetScope-DNS-out", daemon=True).start()

    def stop(self):
        self._stop.set()
        self.status = "off"
        for s in (self.sock, self.up):
            try:
                s and s.close()
            except OSError:
                pass

    def drain(self) -> Dict[str, int]:
        with self.lock:
            out, self.counts = dict(self.counts), defaultdict(int)
        return out

    @property
    def receiving(self) -> bool:
        """True once other devices have actually used NetScope as their DNS."""
        return bool(self.clients) and time.time() - self.last_query < 600

    # ------------------------------------------------------------------ loops
    def _from_clients(self):
        while not self._stop.is_set():
            try:
                msg, addr = self.sock.recvfrom(4096)
            except socket.timeout:
                self._expire()
                continue
            except OSError:
                break
            if len(msg) < 12:
                continue
            with self.lock:
                self.total_queries += 1
            q = parse_dns(msg)
            if q and not q[0] and addr[0] not in self.exclude and _qtype(msg) in COUNTED_QTYPES:
                site = site_of(q[1])
                if site:
                    with self.lock:
                        self.counts[site] += 1
                        self.clients.add(addr[0])
                        self.last_query = time.time()
            # forward unchanged except for the transaction id (so answers can't mix up)
            with self.lock:
                nid = self.next_id
                self.next_id = 1 if self.next_id >= 0xFFFF else self.next_id + 1
                self.pending[nid] = (addr, struct.unpack("!H", msg[:2])[0], time.time())
            try:
                self.up.sendto(struct.pack("!H", nid) + msg[2:], self.upstream)
            except OSError:
                pass

    def _from_upstream(self):
        while not self._stop.is_set():
            try:
                msg, _ = self.up.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            if len(msg) < 12:
                continue
            with self.lock:
                entry = self.pending.pop(struct.unpack("!H", msg[:2])[0], None)
            if entry:
                addr, orig_id, _ = entry
                try:
                    self.sock.sendto(struct.pack("!H", orig_id) + msg[2:], addr)
                except OSError:
                    pass

    def _expire(self):
        cutoff = time.time() - 10
        with self.lock:
            for k in [k for k, v in self.pending.items() if v[2] < cutoff]:
                del self.pending[k]
