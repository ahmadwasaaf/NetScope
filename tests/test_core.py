"""Regression tests without a UI: saved routers, router DNS setup/undo, DNS helper.
Run: python tests/test_core.py   (exit code 1 on failure)"""
import os
import socket
import struct
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path

os.environ["APPDATA"] = tempfile.mkdtemp()
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import netscope.settings as st          # noqa: E402


class FakeKeyring:
    def __init__(self):
        self.d = {}

    def get_password(self, svc, key):
        return self.d.get((svc, key))

    def set_password(self, svc, key, pw):
        self.d[(svc, key)] = pw

    def delete_password(self, svc, key):
        self.d.pop((svc, key), None)


st.keyring = FakeKeyring()
from netscope import profiles  # noqa: E402
profiles.keyring = st.keyring
from netscope.settings import Settings, data_dir  # noqa: E402

FAILED = []


def check(name, cond):
    print(("ok   " if cond else "FAIL ") + name, flush=True)
    if not cond:
        FAILED.append(name)


def test_profiles():
    # an older single-router install
    s = Settings(onboarded=True, backend="huawei", host="192.168.1.1", username="admin",
                 router_label="Huawei", quota_gb=110, start_date="2026-11-22T23:38")
    s.set_password("old-secret")                                    # legacy key
    (data_dir() / "usage_huawei_192.168.1.1.db").write_text("x")
    (data_dir() / "huawei_192.168.1.1.json").write_text('{"lan_dns_backup": {"value": ""}}')
    check("migrate creates one profile", profiles.migrate(s) and len(s.routers) == 1)
    p1 = s.routers[0]
    check("migrated router keeps its usage file", profiles.store_tag(s) == "huawei_192.168.1.1")
    check("migrated router keeps its undo file", profiles.state_tag(s) == "192.168.1.1")
    check("migrated password readable", s.get_password() == "old-secret")
    check("pending changes seen", profiles.pending_changes(p1) == ["dns"])
    check("migrate runs once", not profiles.migrate(s))

    # add a second router at the SAME address (another network)
    draft = profiles.begin_new(s, "192.168.1.1")
    draft.backend, draft.username, draft.router_label = "tplink", "admin", "TP-Link"
    draft.set_password("new-secret")
    profiles.commit_new(draft)
    s = draft
    p2 = profiles.active(s)
    check("second router added", len(s.routers) == 2 and p2["id"] != p1["id"])
    check("own usage file", profiles.store_tag(s) != p1["store_tag"])
    check("own undo file", profiles.state_tag(s) != p1["state_tag"])
    check("fresh bundle for new router", s.quota_gb == 0)
    check("passwords don't mix", s.get_password() == "new-secret")

    profiles.activate(s, p1["id"])
    check("switch back restores values", s.backend == "huawei" and s.quota_gb == 110
          and s.start_date == "2026-11-22T23:38" and s.get_password() == "old-secret")
    profiles.activate(s, p2["id"])
    check("switch again", s.backend == "tplink" and s.get_password() == "new-secret")

    profiles.set_identity(s, p1["id"], "AA:BB:CC:00:00:01", "Huawei DN8245V-56")
    check("identity by MAC, not IP", profiles.by_mac(s, "aa:bb:cc:00:00:01")["id"] == p1["id"]
          and profiles.by_mac(s, "aa:bb:cc:00:00:02") is None)

    profiles.remove(s, p2["id"])
    check("remove deletes profile and password", len(s.routers) == 1 and s.active_router == ""
          and st.keyring.get_password(st.KEYRING_SERVICE, f"profile:{p2['id']}") is None)
    check("other router untouched", (data_dir() / "usage_huawei_192.168.1.1.db").exists())

    s2 = Settings(onboarded=True, backend="demo")
    check("demo is never a profile", not profiles.migrate(s2))


def test_lan_dns():
    import threading as th
    import netscope.backends.huawei as H
    H.time.sleep = lambda x: None
    LH = H.LHCM

    class Fake:
        host = "192.168.1.1"
        lock = th.Lock()

        def __init__(self, dns="", accept_dns=True):
            self.dns, self.statics, self.n, self.accept_dns = dns, {}, 0, accept_dns

        def page(self, path):
            if path == H.LAN_DNS_PAGE:
                return ('function dhcpmainst(domain, enable, startip, endip, leasetime, l2relayenable, HGWstartip, '
                        'HGWendip, STBstartip, STBendip, Camerastartip, Cameraendip, Computerstartip, Computerendip, '
                        'Phonestartip, Phoneendip, MainDNS, X_HW_Option125Enable, DNSServers, OptionEnable) {}'
                        f'var m = new Array(new dhcpmainst("{LH}","1","192.168.1.2","192.168.1.65","86400","0","","",'
                        f'"","","","","","","","","{self.dns}","0","","0"),null);'
                        "Form.addParameter('y.DNSServers', a); Form.addParameter('z.X_HW_DNSList', b);"
                        "url = 'set.cgi?' + '&z=" + LH + "' + '&RequestFile=' + RequestFile;")
            if path == H.STATIC_PAGE:
                objs = ",".join(f'new stDhcp("{d}","1","{ip}","{m}")' for m, (d, ip) in self.statics.items())
                return 'function stDhcp(domain, Enable, ipAddress, macAddress) {} var D = new Array(' + objs + \
                       (',' if objs else '') + 'null);'
            return ""

        def get(self, path, post=False, expect=""):
            return self.page(path)

        def token_of(self, html):
            return "tok"

        def submit(self, cgi, obj, rf, fields):
            if cgi == "add.cgi":
                self.n += 1
                self.statics[fields["x.Chaddr"]] = (f"{obj}.{self.n}", fields["x.Yiaddr"])
            if cgi == "del.cgi":
                for k in fields:
                    self.statics = {m: v for m, v in self.statics.items() if v[0] != k}

        def _request(self, method, q, body):
            if self.accept_dns and q.startswith("set.cgi?z=" + LH + "&"):
                self.dns = body["z.X_HW_DNSList"]

    b = H.HuaweiBackend.__new__(H.HuaweiBackend)
    b._heavy_at = None
    b.state_tag = "r_test"
    b.s = Fake(dns="")
    check("finds the DNS field", b._dns_form(b.s.page(H.LAN_DNS_PAGE))[:2] == ("z", "X_HW_DNSList"))
    b.lan_dns_setup("192.168.1.2", "AA-BB-CC-DD-EE-FF")
    check("reservation then DNS", b.s.dns == "192.168.1.2,1.1.1.1" and len(b.s.statics) == 1)
    check("backup saved to disk", (data_dir() / "huawei_r_test.json").exists()
          and b._state_get("lan_dns_backup")["value"] == "")
    b.lan_dns_undo()
    check("undo restores original + removes own reservation", b.s.dns == "" and not b.s.statics)
    b.s = Fake(dns="", accept_dns=False)
    try:
        b.lan_dns_setup("192.168.1.2", "aa:bb:cc:dd:ee:ff")
        check("rejected DNS raises", False)
    except Exception:
        check("rejected DNS rolls back reservation", not b.s.statics)
    b._state_set("lan_dns_backup", None)
    b.s = Fake(dns="")
    b.s.statics["aa:bb:cc:dd:ee:ff"] = (LH + ".DHCPStaticAddress.9", "192.168.1.2")
    b.lan_dns_setup("192.168.1.2", "aa:bb:cc:dd:ee:ff")
    b.lan_dns_undo()
    check("user's own reservation kept", len(b.s.statics) == 1)


def test_dns_helper():
    from netscope.monitor.dnsmon import DnsMonitor

    def q(name, qid):
        h = struct.pack("!HHHHHH", qid, 0x0100, 1, 0, 0, 0)
        return h + b"".join(bytes([len(p)]) + p.encode() for p in name.split(".")) + b"\0" + struct.pack("!HH", 1, 1)

    big = b"X" * 3000

    def udp_srv(sock):
        while True:
            m, a = sock.recvfrom(65535)
            sock.sendto(m[:2] + b"\x81\x80" + m[4:] + big, a)

    def tcp_srv(sock):
        while True:
            c, _ = sock.accept()
            n = struct.unpack("!H", c.recv(2))[0]
            m = c.recv(n)
            r = m[:2] + b"\x81\x80" + m[4:] + big * 5
            c.sendall(struct.pack("!H", len(r)) + r)
            c.close()
    us = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    us.bind(("127.0.0.1", 0))
    port = us.getsockname()[1]
    ts = socket.socket()
    ts.bind(("127.0.0.1", port))
    ts.listen(5)
    threading.Thread(target=udp_srv, args=(us,), daemon=True).start()
    threading.Thread(target=tcp_srv, args=(ts,), daemon=True).start()
    dead = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    dead.bind(("127.0.0.1", 0))
    mon = DnsMonitor([("127.0.0.1", dead.getsockname()[1]), ("127.0.0.1", port)], port=0)
    # pick a free port for the helper
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    probe.bind(("127.0.0.1", 0))
    mon.port = probe.getsockname()[1]
    probe.close()
    mon.start()
    check("helper starts", mon.status == "running")
    c = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    c.settimeout(5)
    c.sendto(q("www.youtube.com", 0x1234), ("127.0.0.1", mon.port))
    r, _ = c.recvfrom(65535)
    check("UDP: big answer, retried on 2nd resolver", len(r) > 3000 and r[:2] == b"\x12\x34")
    if mon.tcp_ok:
        s = socket.create_connection(("127.0.0.1", mon.port), timeout=8)
        m = q("chatgpt.com", 0x4321)
        s.sendall(struct.pack("!H", len(m)) + m)
        n = struct.unpack("!H", s.recv(2))[0]
        buf = b""
        while len(buf) < n:
            buf += s.recv(n - len(buf))
        check("TCP: very big answer", len(buf) > 15000 and buf[:2] == b"\x43\x21")
    mon.stop()


for t in (test_profiles, test_lan_dns, test_dns_helper):
    try:
        t()
    except Exception:
        traceback.print_exc()
        FAILED.append(t.__name__)
print("FAILED: " + ", ".join(FAILED) if FAILED else "all core tests passed")
sys.exit(1 if FAILED else 0)
