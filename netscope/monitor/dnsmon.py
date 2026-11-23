"""Other-devices monitor: a small DNS forwarder running inside NetScope.

When the router hands out THIS computer as the network's DNS server, every device asks
NetScope for website addresses before connecting. NetScope forwards each question,
unchanged, to a public DNS service and counts which websites were asked for. That count
ranks websites by activity - it is not a byte count, so the UI shows names only.

Reliability rules (learned the hard way):
* Questions go to public resolvers (1.1.1.1, 8.8.8.8, 9.9.9.9), never back to the router,
  whose own DNS can be slow or broken.
* A question with no answer in ~1 s is re-sent to the next resolver.
* Big answers are served over TCP as well as UDP (clients retry over TCP when an answer
  is "truncated"; without TCP some sites fail to open or uploads stall).
* Recommended router setting: primary DNS = this PC, secondary DNS = 1.1.1.1, so devices
  keep browsing normally whenever this PC is off.
"""
import socket
import struct
import threading
import time
from collections import defaultdict
from typing import Dict, List, Optional, Set, Tuple

from .domains import site_of
from .pcap import parse_dns

COUNTED_QTYPES = {1, 28, 65}        # A, AAAA, HTTPS
PUBLIC_DNS = ["1.1.1.1", "8.8.8.8", "9.9.9.9"]
RETRY_AFTER = 1.0                   # seconds before asking the next resolver
GIVE_UP_AFTER = 6.0


def _qtype(msg: bytes) -> int:
    """Question type of a DNS query (0 if unreadable)."""
    try:
        p = 12
        while msg[p] != 0:
            p += 1 + msg[p]
        return struct.unpack("!H", msg[p + 1:p + 3])[0]
    except (IndexError, struct.error):
        return 0


def _recv_exact(sock, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("closed")
        buf += chunk
    return buf


class DnsMonitor:
    """status: 'off' | 'running' | 'error:<msg>'"""

    def __init__(self, upstream=None, port: int = 53, exclude: Optional[Set[str]] = None):
        if upstream is None:
            ups = PUBLIC_DNS
        elif isinstance(upstream, str) or (isinstance(upstream, tuple) and len(upstream) == 2
                                            and isinstance(upstream[1], int)):
            ups = [upstream]
        else:
            ups = list(upstream)
        self.upstreams: List[Tuple[str, int]] = [u if isinstance(u, tuple) else (u, 53) for u in ups]
        self.port = port
        self.exclude = set(exclude or ())          # this PC's own addresses
        self.status = "off"
        self.tcp_ok = False
        self.counts: Dict[Tuple[str, str], int] = defaultdict(int)     # (client ip, site) -> lookups
        self.clients: Set[str] = set()          # other devices that used NetScope as DNS
        self.total_queries = 0
        self.answered = 0
        self.last_query = 0.0
        self.lock = threading.Lock()
        # nid -> [client addr, original id, first sent, last sent, upstream index, query bytes]
        self.pending: Dict[int, list] = {}
        self.next_id = 1
        self.best = 0                            # resolver that answered last (asked first)
        self._stop = threading.Event()
        self.sock = self.up = self.tcp = None

    # ------------------------------------------------------------------ control
    def start(self):
        if self.status == "running":
            return
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.sock.bind(("0.0.0.0", self.port))
            self.sock.settimeout(0.25)
            self.up = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.up.settimeout(0.25)
        except OSError as e:
            self.status = f"error:{e.strerror or e}"
            return
        try:                                     # TCP is a bonus: keep UDP running if it fails
            self.tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.tcp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.tcp.bind(("0.0.0.0", self.port))
            self.tcp.listen(32)
            self.tcp.settimeout(0.5)
            self.tcp_ok = True
        except OSError:
            self.tcp = None
            self.tcp_ok = False
        self._stop.clear()
        self.status = "running"
        threading.Thread(target=self._from_clients, name="NetScope-DNS-in", daemon=True).start()
        threading.Thread(target=self._from_upstream, name="NetScope-DNS-out", daemon=True).start()
        if self.tcp:
            threading.Thread(target=self._tcp_accept, name="NetScope-DNS-tcp", daemon=True).start()

    def stop(self):
        self._stop.set()
        self.status = "off"
        for s in (self.sock, self.up, self.tcp):
            try:
                s and s.close()
            except OSError:
                pass

    def drain(self) -> Dict[Tuple[str, str], int]:
        with self.lock:
            out, self.counts = dict(self.counts), defaultdict(int)
        return out

    @property
    def receiving(self) -> bool:
        """True once other devices have actually used NetScope as their DNS."""
        return bool(self.clients) and time.time() - self.last_query < 600

    # ------------------------------------------------------------------ counting
    def _count(self, msg: bytes, client_ip: str):
        with self.lock:
            self.total_queries += 1
        q = parse_dns(msg)
        if q and not q[0] and client_ip not in self.exclude and _qtype(msg) in COUNTED_QTYPES:
            site = site_of(q[1])
            if site:
                with self.lock:
                    self.counts[(client_ip, site)] += 1
                    self.clients.add(client_ip)
                    self.last_query = time.time()

    # ------------------------------------------------------------------ UDP
    def _send_up(self, nid: int, entry: list):
        try:
            self.up.sendto(struct.pack("!H", nid) + entry[5][2:], self.upstreams[entry[4] % len(self.upstreams)])
        except OSError:
            pass

    def _from_clients(self):
        while not self._stop.is_set():
            try:
                msg, addr = self.sock.recvfrom(65535)
            except socket.timeout:
                self._retry()
                continue
            except ConnectionResetError:          # Windows: ICMP port unreachable from a client
                continue
            except OSError:
                break
            if len(msg) < 12:
                continue
            self._count(msg, addr[0])
            now = time.time()
            with self.lock:
                nid = self.next_id
                self.next_id = 1 if self.next_id >= 0xFFFF else self.next_id + 1
                entry = [addr, struct.unpack("!H", msg[:2])[0], now, now, self.best, msg]
                self.pending[nid] = entry
            self._send_up(nid, entry)
            self._retry()

    def _from_upstream(self):
        while not self._stop.is_set():
            try:
                msg, src = self.up.recvfrom(65535)
            except socket.timeout:
                continue
            except ConnectionResetError:
                continue
            except OSError:
                break
            if len(msg) < 12:
                continue
            with self.lock:
                entry = self.pending.pop(struct.unpack("!H", msg[:2])[0], None)
                if entry:
                    self.answered += 1
                    if src in self.upstreams:
                        self.best = self.upstreams.index(src)
            if entry:
                try:
                    self.sock.sendto(struct.pack("!H", entry[1]) + msg[2:], entry[0])
                except OSError:
                    pass

    def _retry(self):
        """Re-send unanswered questions to the next resolver; forget hopeless ones."""
        now = time.time()
        resend = []
        with self.lock:
            for nid, e in list(self.pending.items()):
                if now - e[2] > GIVE_UP_AFTER:
                    del self.pending[nid]
                elif now - e[3] > RETRY_AFTER:
                    e[3] = now
                    e[4] += 1
                    resend.append((nid, e))
        for nid, e in resend:
            self._send_up(nid, e)

    # ------------------------------------------------------------------ TCP
    def _tcp_accept(self):
        while not self._stop.is_set():
            try:
                conn, addr = self.tcp.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._tcp_client, args=(conn, addr), daemon=True).start()

    def _tcp_client(self, conn, addr):
        conn.settimeout(10)
        try:
            while not self._stop.is_set():
                try:
                    n = struct.unpack("!H", _recv_exact(conn, 2))[0]
                except (ConnectionError, socket.timeout, OSError):
                    break
                msg = _recv_exact(conn, n)
                if len(msg) < 12:
                    break
                self._count(msg, addr[0])
                answer = self._ask_tcp(msg)
                if answer is None:
                    break
                with self.lock:
                    self.answered += 1
                conn.sendall(struct.pack("!H", len(answer)) + answer)
        except (ConnectionError, socket.timeout, OSError):
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _ask_tcp(self, msg: bytes) -> Optional[bytes]:
        for up in self.upstreams:
            try:
                with socket.create_connection(up, timeout=3) as s:
                    s.sendall(struct.pack("!H", len(msg)) + msg)
                    n = struct.unpack("!H", _recv_exact(s, 2))[0]
                    return _recv_exact(s, n)
            except (OSError, ConnectionError, struct.error):
                continue
        return None
