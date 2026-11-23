"""Huawei home gateways / ONTs (EchoLife & OptiXstar web UI: DN8245V, HG8245*, HG8145*, EG8145* …)
– the routers WE (Telecom Egypt) and many other ISPs hand out.

Talks to the router exactly like its own web page does:
  1. POST /asp/GetRandCount.asp           -> one-time token
  2. POST /login.cgi  (UserName, PassWord=base64, Language, x.X_HW_Token)
     The token is bound to the TCP connection, so both requests share one socket.
  3. POST /html/bbsp/common/GetLanUserDevInfo.asp -> JS `new USERDevice(...)` list
  4. Changes go through the same set/add/del .cgi forms the web page uses, carrying the
     page's `onttoken`. Every change is VERIFIED by reading the page back afterwards.

Firmware builds differ between ISPs; if a form is not accepted the action reports an
error instead of pretending it worked.
"""
import base64
import http.client
import re
import ssl
import threading
import time
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlencode

from .base import Cap, CounterDelta, Device, RouterBackend, Snapshot, clean_domain, clean_mac, guess_type

MACFILTER_PAGE = "html/bbsp/macfilter/macfilter.asp"
URLFILTER_PAGE = "html/bbsp/urlfilter/urlfilter.asp"
MACFILTER_OBJ = "InternetGatewayDevice.X_HW_Security.MacFilter"
# Pages that carry WAN statistics on different Huawei firmwares (first one that works wins)
WAN_STAT_PAGES = [
    "html/bbsp/common/get_wan_list_pppwanstat.asp",   # PPPoE line (WE / TE Data)
    "html/bbsp/common/get_wan_list_ipwanstat.asp",    # IPoE line
    "html/bbsp/waninfo/waninfo_tedata.asp",
    "html/bbsp/wan/wan_tedata.asp",
    "html/ssmp/deviceinfo/dslinfo.asp",
    "html/amp/ethinfo/ethinfo.asp",          # WanEthStats (Ethernet-WAN models)
    "html/bbsp/common/wan_list.asp",
    "html/bbsp/waninfo/waninfo.asp",
    "html/bbsp/common/wanstatistics.asp",
    "html/bbsp/wanstat/wanstat.asp",
    "html/amp/dslinfo/dslinfo.asp",
    "html/ssmp/deviceinfo/deviceinfo.asp",
]
DIAG_PAGES = WAN_STAT_PAGES + [
    "html/bbsp/common/GetLanUserDevInfo.asp", "html/bbsp/common/lanuserinfo.asp",
    "html/amp/ethinfo/ethinfo.asp", "html/bbsp/upnp/upnp.asp",
    MACFILTER_PAGE,
    "html/bbsp/urlfilter/urlfilter.asp", "html/bbsp/wlanmacfilter/wlanmacfilter.asp",
    "html/bbsp/parentalctrl/parentalctrlstatus_egypt.asp", "index.asp",
]
DHCP_PAGES = ["html/bbsp/dhcp/dhcp_tedata.asp", "html/bbsp/dhcpservercfg/dhcp2.asp",
              "html/bbsp/dhcpstatic/dhcpstatic.asp", "html/bbsp/dhcpstaticaddr/dhcpstaticaddress.asp",
              "html/bbsp/lanaddress/lanaddress_tedata.asp"]
DIAG_PAGES += DHCP_PAGES
LAN_DNS_PAGE = "html/bbsp/dhcpservercfg/dhcp2.asp"
STATIC_PAGE = "html/bbsp/dhcpstatic/dhcpstatic.asp"
LHCM = "InternetGatewayDevice.LANDevice.1.LANHostConfigManagement"
STATIC_OBJ = LHCM + ".DHCPStaticAddress"
SECONDARY_DNS = "1.1.1.1"
STAT_REF = re.compile(r"stat|wan|dsl|flow|traffic", re.I)


def _unescape(s: str) -> str:
    return re.sub(r"\\x([0-9a-fA-F]{2})", lambda m: chr(int(m.group(1), 16)), s)


def parse_js_objects(js: str, ctor: str) -> List[Dict[str, str]]:
    """Parse `function Ctor(a,b,c){...}` + `new Ctor("..","..")` into dicts keyed by the
    constructor's own parameter names (field order differs between firmwares)."""
    m = re.search(rf"function\s+{ctor}\s*\(([^)]*)\)", js)
    names = [n.strip() for n in m.group(1).split(",")] if m else []
    out = []
    for call in re.finditer(rf"new\s+{ctor}\s*\((.*?)\)\s*[,;\]]", js, re.S):
        args = [_unescape(a) for a in re.findall(r'"((?:[^"\\]|\\.)*)"', call.group(1))]
        if names and len(args) >= len(names):
            out.append(dict(zip(names, args)))
        else:
            out.append({f"f{i}": a for i, a in enumerate(args)})
    return out


def _field(d: Dict[str, str], *keys) -> str:
    low = {k.lower(): v for k, v in d.items()}
    for k in keys:
        if k.lower() in low:
            return low[k.lower()]
    return ""


def constructors(js: str) -> Dict[str, List[str]]:
    """Every `function Name(a,b,c)` in a page -> parameter names (no values)."""
    return {m.group(1): [a.strip() for a in m.group(2).split(",") if a.strip()]
            for m in re.finditer(r"function\s+(\w+)\s*\(([^)]*)\)", js)}


def _num(obj: Dict[str, str], key: str) -> int:
    """Huawei splits big counters into `name` (low 32 bit) + `name_H` (high 32 bit)."""
    try:
        low = int(float(obj.get(key, "0") or 0))
        high = int(float(obj.get(key + "_H", "0") or 0))
        return (high << 32) + low
    except ValueError:
        return 0


def counter_sets(js: str):
    """Yield (object type, rx field, tx field, [objects]) for every object list holding
    received/sent byte counters on a WAN interface (LAN-port stats are skipped)."""
    for ctor, params in constructors(js).items():
        cand = [p for p in params if "bytes" in p.lower() and not p.endswith("_H")
                and "bad" not in p.lower()]
        rx = next((p for p in cand if re.search(r"rec|rx", p, re.I)), None)
        tx = next((p for p in cand if re.search(r"sent|tx", p, re.I)), None)
        if not rx or not tx:
            continue
        objs = [o for o in parse_js_objects(js, ctor)
                if not ("LANDevice" in " ".join(o.values()) and "WANDevice" not in " ".join(o.values()))]
        if objs:
            yield ctor, rx, tx, objs


KV_RE = re.compile(r"""["']?(\w*Bytes\w*)["']?\s*[:=]\s*["']?(\d+)""")


def kv_counters(js: str):
    """Counters written as key/value pairs: {BytesReceived:"123", BytesSent:"456"} or
    obj.BytesSent = 456 – used when a data page carries no constructor definition."""
    vals: Dict[str, int] = {}
    for key, num in KV_RE.findall(_unescape(js)):
        if re.search(r"bad|drop|err", key, re.I):
            continue
        vals[key] = vals.get(key, 0) + int(num)
    rx = sum(v for k, v in vals.items() if re.search(r"rec|rx", k, re.I) and not k.endswith("_H")) + \
        sum(v << 32 for k, v in vals.items() if re.search(r"rec|rx", k, re.I) and k.endswith("_H"))
    tx = sum(v for k, v in vals.items() if re.search(r"sent|tx", k, re.I) and not k.endswith("_H")) + \
        sum(v << 32 for k, v in vals.items() if re.search(r"sent|tx", k, re.I) and k.endswith("_H"))
    return (rx, tx) if (rx or tx) else None


def wan_counters(js: str, context: str = ""):
    """(rx, tx) from the first WAN counter set that is actually counting, else None.
    `context` = the page that loaded this one (it may hold the object definitions)."""
    for _, rx, tx, objs in counter_sets(context + "\n" + js if context else js):
        total_rx = sum(_num(o, rx) for o in objs)
        total_tx = sum(_num(o, tx) for o in objs)
        if total_rx or total_tx:
            return total_rx, total_tx
    return kv_counters(js)


_CODE_STR = re.compile(r"[a-z]\.[A-Za-z_]\w*|&?[a-z]=(?:InternetGatewayDevice[\w.{}]*)?|&?RequestFile=[\w./-]*"
                       r"|InternetGatewayDevice[\w.{}]*|[\w./-]*\.(?:cgi|asp)(?:\?[\w.=&{}/-]*)?"
                       r"|POST|GET|json|text|true|false|none|block")


def sanitize(js: str) -> str:
    """Replace every quoted value with S and every number with N. Only code-like strings
    survive (identifiers, x.Field names, TR-098 object paths, .cgi/.asp URLs) – never
    MACs, device names, URLs, IPs, user names or tokens (plain words are hidden too)."""
    def keep(m):
        q, body = m.group(1), m.group(2)
        return m.group(0) if _CODE_STR.fullmatch(body) else f"{q}S{q}"
    t = re.sub(r"""(["'])((?:(?!\1)[^\\]|\\.)*)\1""", keep, js)
    t = re.sub(r"(?<![\w.])\d+(?![\w])", "N", t)
    return re.sub(r"\s+", " ", t)


def shape(js: str, limit: int = 700) -> str:
    return sanitize(js)[:limit]


def referenced_pages(js: str) -> List[str]:
    """.asp pages a page loads by script (e.g. WanStatsLoader) – used to auto-discover stats."""
    out = []
    for m in re.finditer(r"['\"]/?((?:html|asp)/[\w/.-]+\.asp)", js):
        if m.group(1) not in out:
            out.append(m.group(1))
    return out


def form_fields(js: str) -> List[str]:
    """Names of `x.Something` parameters a page submits (names only)."""
    return sorted(set(re.findall(r"['\"](x\.\w+)['\"]", js)))


def all_form_fields(js: str) -> List[str]:
    """Like form_fields, for every one-letter object prefix (x., y., z. …)."""
    return sorted(set(re.findall(r"['\"]([a-z]\.[A-Za-z_]\w*)['\"]", js)))


class HuaweiSession:
    def __init__(self, host: str, username: str, password: str):
        host = host.replace("https://", "").replace("http://", "").strip("/")
        self.host = host
        self.username, self.password = username, password
        self.https = None          # decided on first connect
        self.conn = None
        self.cookie = ""
        self.lock = threading.Lock()

    # ------------------------------------------------------------- transport
    def _new_conn(self):
        if self.https is not False:
            try:
                ctx = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE     # routers use self-signed certificates
                c = http.client.HTTPSConnection(self.host, timeout=10, context=ctx)
                c.connect()
                self.https = True
                return c
            except (OSError, ssl.SSLError):
                if self.https:
                    raise
                self.https = False
        c = http.client.HTTPConnection(self.host, timeout=10)
        c.connect()
        return c

    def _request(self, method: str, path: str, body: Optional[dict] = None, cookie: str = None) -> str:
        if self.conn is None:
            self.conn = self._new_conn()
        data = urlencode(body) if body is not None else None
        headers = {"Cookie": cookie if cookie is not None else self.cookie,
                   "Referer": f"{'https' if self.https else 'http'}://{self.host}/",
                   "X-Requested-With": "XMLHttpRequest"}
        if data is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        try:
            self.conn.request(method, "/" + path.lstrip("/"), body=data, headers=headers)
            resp = self.conn.getresponse()
        except (http.client.HTTPException, OSError):
            self.conn.close()
            self.conn = self._new_conn()
            self.conn.request(method, "/" + path.lstrip("/"), body=data, headers=headers)
            resp = self.conn.getresponse()
        raw = resp.read()
        sc = resp.getheader("Set-Cookie")
        if sc:
            m = re.search(r"Cookie=([^;]+)", sc)
            if m and "sid=" in m.group(1):
                self.cookie = "Cookie=" + m.group(1)
        return raw.decode("utf-8-sig", errors="replace")

    # ------------------------------------------------------------- auth
    def login(self):
        with self.lock:
            if self.conn:
                self.conn.close()
            self.conn = None
            self.cookie = ""
            token = self._request("POST", "asp/GetRandCount.asp", {}, cookie="").strip()
            if not token:
                raise RuntimeError("Router did not return a login token")
            pwd = base64.b64encode(self.password.encode()).decode()
            self._request("POST", "login.cgi", {
                "UserName": self.username, "PassWord": pwd,
                "Language": "english", "x.X_HW_Token": token,
            }, cookie="Cookie=body:Language:english:id=-1")
            if "sid=" not in self.cookie:
                raise PermissionError("Wrong username or password (the router locks sign-in after a few failures)")

    def get(self, path: str, post: bool = False, expect: str = "") -> str:
        """`expect` = text a valid answer must contain. If it's missing, the session was
        silently dropped (e.g. someone else logged in to the router) -> sign in again."""
        with self.lock:
            text = self._request("POST" if post else "GET", path, {} if post else None)
        if self._logged_out(text) or (expect and expect not in text):
            self.login()
            with self.lock:
                text = self._request("POST" if post else "GET", path, {} if post else None)
        return text

    @staticmethod
    def _logged_out(text: str) -> bool:
        t = text[:3000].lower()
        return "getrandcount" in t or ("login.cgi" in t and "txt_password" in t)

    TOKEN_PATTERNS = [
        r'<input[^>]*onttoken[^>]*value=["\']([0-9a-fA-F]{8,})',     # id="hwonttoken" value=…
        r'<input[^>]*value=["\']([0-9a-fA-F]{8,})["\'][^>]*onttoken',
        r'onttoken["\']?\s*[:=]\s*["\']([0-9a-fA-F]{8,})',
        r'\btoken\s*=\s*["\']([0-9a-fA-F]{8,})',                   # var token="…"
    ]

    def token_of(self, page_html: str) -> str:
        for pat in self.TOKEN_PATTERNS:
            m = re.search(pat, page_html, re.I)
            if m:
                return m.group(1)
        # newer firmwares hand the token out on request (HWGetToken in index.asp)
        for path in ("html/ssmp/common/GetRandToken.asp", "asp/GetRandToken.asp"):
            try:
                with self.lock:
                    t = self._request("POST", path, {}).strip()
                if re.fullmatch(r"[0-9a-fA-F]{8,}", t):
                    return t
            except Exception:
                pass
        raise RuntimeError("Could not read the page security token")

    def token_source(self, page_html: str) -> str:
        """For the report: which pattern found the token (never the token itself)."""
        for i, pat in enumerate(self.TOKEN_PATTERNS):
            if re.search(pat, page_html, re.I):
                return f"pattern {i + 1}"
        snippets = [shape(m.group(0), 160) for m in re.finditer(r".{0,80}token.{0,80}", page_html, re.I)][:6]
        return "not found; context: " + " | ".join(snippets)

    def submit(self, cgi: str, obj: str, request_file: str, fields: Dict[str, str]):
        page = self.get(request_file)
        token = self.token_of(page)
        body = dict(fields)
        body["x.X_HW_Token"] = token
        with self.lock:
            query = f"x={obj}&RequestFile={request_file}" if obj else f"RequestFile={request_file}"
            return self._request("POST", f"{cgi}?{query}", body)

    def logout(self):
        try:
            with self.lock:
                if self.cookie:
                    self._request("POST", "logout.cgi?RequestFile=html/logout.html", {})
        except Exception:
            pass
        finally:
            if self.conn:
                self.conn.close()
            self.conn, self.cookie = None, ""


PARENTAL_PAGES = ["html/bbsp/parentalctrl/parentalctrlstatus_egypt.asp",
                  "html/bbsp/parentalctrl/parentalctrlstatus.asp",
                  "html/bbsp/parentalctrl/parentalctrl.asp"]
PC_MAC_OBJ = "InternetGatewayDevice.X_HW_Security.ParentalCtrl.MAC"
NETSCOPE_TAG = "NetScope"
MAC_STR = r"(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}"


class ParentalState:
    """What the router's parental-control page currently holds (read at runtime, locally)."""

    def __init__(self, page_path: str, js: str):
        self.page = page_path
        t = _unescape(js)
        m = re.search(r"ParentalCtrl\.Templates\.(\d+)\.UrlFilter['\"]", t)
        self.template = m.group(1) if m else None
        self.template_obj = f"InternetGatewayDevice.X_HW_Security.ParentalCtrl.Templates.{self.template}"
        self.url_obj = self.template_obj + ".UrlFilter"
        base = [o for o in parse_js_objects(js, "UrlFilterBaseValueClass")
                if _field(o, "_Domain").endswith(f"Templates.{self.template}")]
        self.right = _field(base[0], "_Right") if base else ""
        self.policy = _field(base[0], "_Policy") if base else ""
        # blocked sites: Templates.N.UrlFilter.M  ->  domain
        self.urls: Dict[str, str] = {}
        for u in re.finditer(rf'"({re.escape(self.url_obj)}\.\d+)"\s*,\s*"([^"]+)"', t):
            try:
                self.urls[clean_domain(u.group(2))] = u.group(1)
            except ValueError:
                pass
        # devices enrolled in parental control:  MAC.N -> (mac, description)
        self.devices: Dict[str, Tuple[str, str]] = {}
        for d in re.finditer(rf'"({re.escape(PC_MAC_OBJ)}\.\d+)"', t):
            window = t[d.end():d.end() + 300].split(PC_MAC_OBJ)[0]     # up to the next entry
            mac = re.search(MAC_STR, window)
            if mac:
                desc = NETSCOPE_TAG if f'"{NETSCOPE_TAG}"' in window else ""
                self.devices[d.group(1)] = (clean_mac(mac.group(0)), desc)
        # time-of-day rules on this template – NetScope never enrols devices then
        self.has_time_rules = bool(re.search(rf"{re.escape(self.template_obj)}\.Duration\.\d+", t))
        mx = re.search(r"MAX_DEVICES\s*=\s*(\d+)", t)
        self.max_devices = int(mx.group(1)) if mx else 8

    @property
    def usable(self) -> bool:
        return self.template is not None

    def enrolled_macs(self):
        return {mac for mac, _ in self.devices.values()}


class HuaweiBackend(RouterBackend):
    kind = "huawei"
    min_interval = 10

    def __init__(self, host: str, username: str, password: str):
        super().__init__()
        self.s = HuaweiSession(host, username or "admin", password)
        self.wan_page = None
        self.wan_context = ""
        self.delta = CounterDelta()
        self.t_last = None

    def _line_rate(self):
        """(down kbps, up kbps) the DSL line synced at, from the router's DSL page."""
        try:
            js = self.s.get("html/ssmp/deviceinfo/dslinfo.asp", expect="DSLInfo")
        except Exception:
            return None, None
        for o in parse_js_objects(js, "DSLInfo"):
            try:
                down = float(_field(o, "DownstreamCurrRate") or 0)
                up = float(_field(o, "UpstreamCurrRate") or 0)
            except ValueError:
                continue
            if down > 0:
                return down, up
        return None, None

    def _find_wan_page(self):
        queue = [(p, "") for p in WAN_STAT_PAGES] + [("index.asp", "")]
        seen = set()
        while queue and len(seen) < 40:
            page, parent = queue.pop(0)
            if page in seen:
                continue
            seen.add(page)
            try:
                js = self.s.get(page)
            except Exception:
                continue
            if wan_counters(js, parent):
                self.wan_context = parent
                return page
            queue += [(r, js) for r in referenced_pages(js) if STAT_REF.search(r)]
        return None

    @staticmethod
    def _parental_probe(js: str) -> List[str]:
        """How the parental-control page builds its requests (object paths + value-free
        code around each form) – needed to add website blocking safely."""
        return HuaweiBackend._probe(js, r"InternetGatewayDevice\.X_HW_Security\.ParentalCtrl[\w.{}]*",
                                    ("add.cgi", "set.cgi", "del.cgi", "TemplateInst", "UrlAddress", "MACAddress",
                                     "UrlFilterRight", "Templates.", "new Array", "DataPersistentClass"))

    @staticmethod
    def _dhcp_probe(js: str) -> List[str]:
        """How the DHCP pages save DNS servers and fixed addresses (structure only)."""
        return HuaweiBackend._probe(js, r"InternetGatewayDevice\.LANDevice[\w.{}]*",
                                    ("add.cgi", "set.cgi", "del.cgi", "DNSServers", "DNSServer", "DnsServer",
                                     "StaticAddress", "Chaddr", "Yiaddr", "IPAddress", "MinAddress",
                                     "DHCPServerEnable", "addParameter", "MainDNS", "SetDNS", "=InternetGatewayDevice"))

    @staticmethod
    def _probe(js: str, path_re: str, keys) -> List[str]:
        t = _unescape(js)
        out = []
        paths = sorted(set(re.findall(path_re, t)))
        t = sanitize(t)          # values are hidden BEFORE any window is cut
        if paths:
            out.append("    paths: " + ", ".join(paths[:30]))
        for key in keys:
            for i, m in enumerate(re.finditer(re.escape(key), t)):
                if i >= 3:
                    break
                a, b = max(0, m.start() - 220), m.end() + 220
                out.append(f"    ctx[{key}]: " + t[a:b])
        return out

    def diagnostics(self) -> str:
        """Structure-only report (page -> object types and field NAMES, never values),
        safe to share to add support for a firmware."""
        lines = [f"Router: {self.router_name}", ""]
        for page in DIAG_PAGES:
            try:
                js = self.s.get(page)
            except Exception as e:
                lines.append(f"[{page}] error: {e.__class__.__name__}")
                continue
            ctors = constructors(js)
            lines.append(f"[{page}] {len(js)} bytes, {len(ctors)} object types")
            for name, params in ctors.items():
                count = len(re.findall(rf"new\s+{name}\s*\(", js))
                if count or params:
                    lines.append(f"    {name}({', '.join(params)})  x{count}")
            for ctor, rx, tx, objs in counter_sets(js):
                state = "counting" if any(_num(o, rx) or _num(o, tx) for o in objs) else "all zero"
                lines.append(f"    counters: {ctor}.{rx}/{tx} -> {state}")
            refs = referenced_pages(js)
            if refs:
                lines.append("    loads: " + ", ".join(refs[:25]))
            fields = all_form_fields(js) if page in DHCP_PAGES else form_fields(js)
            if fields:
                lines.append("    fields: " + ", ".join(fields[:60]))
            if "cgi?" in js:
                lines.append("    token: " + self.s.token_source(js))
            if "parentalctrl" in page.lower():
                lines += self._parental_probe(js)
            if page in DHCP_PAGES:
                lines += self._dhcp_probe(js)
            if page == LAN_DNS_PAGE:
                form = self._dns_form(js)
                lines.append(f"    auto DNS form: {form[0] + '.' + form[1] + ' on ' + form[2] if form else 'not found'}")
            kv = kv_counters(js)
            if kv:
                lines.append("    key/value counters: counting")
            if len(js) < 6000 and len(js) > 60:
                lines.append("    shape: " + shape(js))
            for form in sorted(set(re.findall(r"(\w+\.cgi\?[^'\"\s]*)", js)))[:15]:
                form = re.sub(r"X_HW_Token=\w+", "X_HW_Token=...", form)
                lines.append("    form: " + form)
        return "\n".join(lines)

    def connect(self) -> None:
        self.s.login()
        self.router_name = "Huawei"
        try:
            info = self.s.get("html/ssmp/deviceinfo/deviceinfo.asp")
            m = re.search(r"\b(?:DN|HG|EG|HS|HN|WA|WS)\d{3,4}[A-Z0-9-]*\b", _unescape(info))
            if m:
                self.router_name = f"Huawei {m.group(0)}"
        except Exception:
            pass
        self.capabilities = {Cap.DEVICE_LIST}
        self.wan_page = self._find_wan_page()
        if self.wan_page:
            self.capabilities |= {Cap.TOTAL_USAGE, Cap.LIVE_SPEED}
        if self._line_rate()[0]:
            self.capabilities.add(Cap.LINE_RATE)
        if self._mac_filter_page():
            self.capabilities.add(Cap.DEVICE_BLOCK)
        if self._parental() is not None:
            self.capabilities.add(Cap.SITE_BLOCK)

    # ------------------------------------------------------------- reads
    def _mac_filter_page(self) -> str:
        try:
            page = self.s.get(MACFILTER_PAGE)
            return page if "MacFilter" in page or "macfilter" in page.lower() else ""
        except Exception:
            return ""

    def _parental(self) -> Optional[ParentalState]:
        for path in ([self.pc_page] if getattr(self, "pc_page", None) else PARENTAL_PAGES):
            try:
                js = self.s.get(path, expect="ParentalCtrl")
            except Exception:
                continue
            st = ParentalState(path, js)
            if st.usable:
                self.pc_page = path
                return st
        return None

    @staticmethod
    def _mac_entries(page: str) -> Dict[str, str]:
        """mac -> instance path, read from the MAC filter page."""
        found = {}
        for m in re.finditer(rf'"({re.escape(MACFILTER_OBJ)}\.\d+)"(.{{0,200}}?)"((?:[0-9A-Fa-f]{{2}}[:-]){{5}}[0-9A-Fa-f]{{2}})"',
                             _unescape(page), re.S):
            found[clean_mac(m.group(3))] = m.group(1)
        return found

    def poll(self) -> Snapshot:
        if not self.capabilities:
            self.connect()
        snap = Snapshot(router_name=self.router_name)
        if self.wan_page:
            try:
                rx, tx = wan_counters(self.s.get(self.wan_page, expect="Bytes"), self.wan_context) or (None, None)
                if rx is not None:
                    now = time.monotonic()
                    first = "rx" not in self.delta.prev
                    if first:                     # resume from the counters saved last time
                        saved = self._state_get("wan_counters") or {}
                        if "rx" in saved and "tx" in saved:
                            self.delta.prev.update(rx=int(saved["rx"]), tx=int(saved["tx"]))
                    d_rx, d_tx = self.delta("rx", rx, wrap32=True), self.delta("tx", tx, wrap32=True)
                    if first and "rx" in self.delta.prev:
                        snap.catch_up_bytes = d_rx + d_tx
                        d_rx = d_tx = 0           # counted as catch-up, not as live speed
                    snap.wan_delta = d_rx + d_tx + snap.catch_up_bytes
                    if time.monotonic() - getattr(self, "_saved_at", 0) > 30 or first:
                        self._state_set("wan_counters", {"rx": rx, "tx": tx, "t": time.time()})
                        self._saved_at = time.monotonic()
                    if self.t_last:
                        dt = now - self.t_last
                        snap.down_bps, snap.up_bps = d_rx / dt, d_tx / dt
                    self.t_last = now
            except Exception:
                pass
        js = self.s.get("html/bbsp/common/GetLanUserDevInfo.asp", post=True, expect="USERDevice")
        seen = set()
        for d in parse_js_objects(js, "USERDeviceNew") + parse_js_objects(js, "USERDevice"):
            mac = _field(d, "RealMacAddr") or _field(d, "MacAddr", "MACAddr", "Mac")
            try:
                mac = clean_mac(mac)
            except ValueError:
                continue
            if mac in seen:
                continue
            seen.add(mac)
            alias = _field(d, "UserDevAlias")
            name = (alias if alias and alias != "--" else "") or _field(d, "HostName", "DevName") or mac
            ip = _field(d, "IpAddr", "IPAddr", "Ip")
            status = _field(d, "DevStatus", "Status").lower()
            hint = _field(d, "UserSpecifiedDeviceType") or _field(d, "DeviceType") or _field(d, "DevType")
            snap.devices.append(Device(mac, name, ip, guess_type(name, mac, hint), status in ("", "online", "1")))
        self._last_devices = [d for d in snap.devices if d.online]
        # filter pages are big (20-70 KB) for a small router CPU: read them once a minute
        last = getattr(self, "_heavy_at", None)
        if last is None or time.monotonic() - last > 60:
            self._heavy_at = time.monotonic()
            if Cap.DEVICE_BLOCK in self.capabilities:
                self._cache_macs = set(self._mac_entries(self._mac_filter_page()))
            if Cap.SITE_BLOCK in self.capabilities:
                st = self._parental()
                self._cache_sites = set(st.urls) if st else set()
            self._cache_line = self._line_rate()
        snap.line_down_kbps, snap.line_up_kbps = getattr(self, "_cache_line", (None, None))
        snap.blocked_macs = set(getattr(self, "_cache_macs", set()))
        snap.blocked_domains = set(getattr(self, "_cache_sites", set()))
        return snap

    # ------------------------------------------------------------- actions
    def _ensure_filter_on(self, page_path: str, kind: str):
        """Turn the filter on in blacklist mode, using the field names the page itself uses."""
        fields = form_fields(self.s.get(page_path))
        enable = next((f for f in fields if kind in f and re.search(r"Right|Enable", f)), None)
        mode = next((f for f in fields if kind in f and re.search(r"Policy|Mode", f)), None)
        body = {}
        if enable:
            body[enable] = "1"
        if mode:
            body[mode] = "0"          # 0 = blacklist on Huawei firmwares
        if body:
            self.s.submit("set.cgi", "InternetGatewayDevice.X_HW_Security", page_path, body)

    def _need(self, cap: str):
        if cap not in self.capabilities:
            raise NotImplementedError("This router firmware does not offer this from NetScope")

    def block_device(self, mac: str) -> None:
        self._heavy_at = None
        self._need(Cap.DEVICE_BLOCK)
        mac = clean_mac(mac)
        page = self.s.get(MACFILTER_PAGE)
        fields = form_fields(page)
        mac_field = next((f for f in fields if re.search(r"MAC", f, re.I) and "Address" in f), "x.MACAddress")
        alias_field = next((f for f in fields if "Alias" in f), None)
        self._ensure_filter_on(MACFILTER_PAGE, "Mac")
        body = {mac_field: mac.upper()}
        if alias_field:
            body[alias_field] = "NetScope"
        self.s.submit("add.cgi", MACFILTER_OBJ, MACFILTER_PAGE, body)
        time.sleep(1)
        if mac not in self._mac_entries(self._mac_filter_page()):
            raise RuntimeError("The router did not accept the MAC filter rule on this firmware")

    def unblock_device(self, mac: str) -> None:
        self._heavy_at = None
        mac = clean_mac(mac)
        path = self._mac_entries(self._mac_filter_page()).get(mac)
        if not path:
            return
        self.s.submit("del.cgi", "", MACFILTER_PAGE, {path: ""})
        time.sleep(1)
        if mac in self._mac_entries(self._mac_filter_page()):
            raise RuntimeError("The router did not remove the MAC filter rule")

    # Website blocking = the router's own Parental Control URL filter.
    # Safety rules:
    #  * only the URL list of the existing template is touched; its time rules never are
    #  * devices are enrolled only when that template has NO time-of-day rules, and only
    #    with the description "NetScope", so NetScope can tell its own entries apart
    #  * if NetScope switched the filter on, it switches it off again (and removes its
    #    enrolled devices) when the last blocked site is removed
    def _enrol_devices(self, st: ParentalState, macs) -> None:
        if st.has_time_rules:
            return
        tried = self.__dict__.setdefault("_enrol_tried", set())
        missing = [m for m in macs if m not in st.enrolled_macs() and m not in tried]
        room = max(0, st.max_devices - len(st.devices))
        for mac in missing[:room]:
            tried.add(mac)
            self.s.submit("add.cgi", PC_MAC_OBJ, st.page, {
                "x.MACAddress": mac.upper(), "x.Description": NETSCOPE_TAG, "x.TemplateInst": st.template})

    def block_domain(self, domain: str) -> None:
        self._heavy_at = None
        self._need(Cap.SITE_BLOCK)
        domain = clean_domain(domain)
        st = self._parental()
        if st is None:
            raise RuntimeError("Parental control page not found")
        if domain in st.urls:
            return
        if st.right != "1":
            # filter was off: switch it on in blacklist mode and remember that we did it
            self.s.submit("set.cgi", st.template_obj, st.page,
                          {"x.UrlFilterRight": "1", "x.UrlFilterPolicy": "0"})
            self._state_set("url_filter_enabled_by_netscope", True)
        self.s.submit("add.cgi", st.url_obj, st.page, {"x.UrlAddress": domain})
        added = set(self._state_get("netscope_domains") or [])
        self._state_set("netscope_domains", sorted(added | {domain}))
        time.sleep(1)
        st = self._parental()
        if not st or domain not in st.urls:
            raise RuntimeError("The router did not accept the website rule")
        online = [d.mac for d in getattr(self, "_last_devices", [])]
        self._enrol_devices(st, online)

    def unblock_domain(self, domain: str) -> None:
        self._heavy_at = None
        domain = clean_domain(domain)
        st = self._parental()
        if st is None or domain not in st.urls:
            return
        self.s.submit("del.cgi", "", st.page, {st.urls[domain]: ""})
        time.sleep(1)
        st = self._parental()
        if st and domain in st.urls:
            raise RuntimeError("The router did not remove the website rule")
        if st and not st.urls:
            # last site gone: undo what NetScope itself changed
            ours = [path for path, (_, desc) in st.devices.items() if desc == NETSCOPE_TAG]
            if ours:
                self.s.submit("del.cgi", "", st.page, {p: "" for p in ours})
            if self._state_get("url_filter_enabled_by_netscope"):
                self.s.submit("set.cgi", st.template_obj, st.page, {"x.UrlFilterRight": "0"})
                self._state_set("url_filter_enabled_by_netscope", False)

    def undo_all(self) -> str:
        """Put the router back the way it was before NetScope (only NetScope's own changes)."""
        done = []
        # MAC filter: remove NetScope's rules; switch the filter off when nothing is left
        page = self._mac_filter_page()
        if page:
            entries = self._mac_entries(page)
            if entries:
                self.s.submit("del.cgi", "", MACFILTER_PAGE, {p: "" for p in entries.values()})
                done.append(f"MAC filter: removed {len(entries)} blocked device(s)")
            fields = form_fields(page)
            right = next((f for f in fields if "Mac" in f and "Right" in f), None)
            if right:
                self.s.submit("set.cgi", "InternetGatewayDevice.X_HW_Security", MACFILTER_PAGE, {right: "0"})
                done.append("MAC filter: switched off")
        # Parental control: NetScope's sites + devices, and the filter switch if NetScope turned it on
        st = self._parental()
        if st:
            ours = set(self._state_get("netscope_domains") or [])
            urls = {d: p for d, p in st.urls.items() if d in ours or not ours}
            if urls:
                self.s.submit("del.cgi", "", st.page, {p: "" for p in urls.values()})
                done.append("Parental control: removed sites " + ", ".join(sorted(urls)))
            devs = [p for p, (_, desc) in st.devices.items() if desc == NETSCOPE_TAG]
            if devs:
                self.s.submit("del.cgi", "", st.page, {p: "" for p in devs})
                done.append(f"Parental control: removed {len(devs)} device(s) added by NetScope")
            if self._state_get("url_filter_enabled_by_netscope") or st.right == "1":
                self.s.submit("set.cgi", st.template_obj, st.page, {"x.UrlFilterRight": "0"})
                done.append("Parental control: website filter switched off")
            self._state_set("url_filter_enabled_by_netscope", False)
            self._state_set("netscope_domains", [])
        try:
            d = self.lan_dns_undo()
            if d:
                done.append(d)
        except Exception as e:
            done.append(f"Network DNS: could not restore ({e})")
        self._heavy_at = None
        return "\n".join(done) or "Nothing to undo – the router has no NetScope changes."

    # ------------------------------------------------------------- phones' DNS -> this PC
    # Order and safety (each step is checked by reading the router back):
    #   1. the router's original DNS setting is saved to disk BEFORE anything changes
    #   2. this PC's address is reserved (DHCP static address) so it can never move
    #   3. only then the LAN DHCP DNS becomes "<this PC>,1.1.1.1"
    #   any failure rolls back what this run changed; undo restores the saved original
    #   and removes only the reservation NetScope itself created.
    @staticmethod
    def _dns_form(js: str):
        """(prefix, field, object) the DHCP page uses to save the LAN DNS servers, or None."""
        t = _unescape(js)
        names = re.findall(r"['\"]([a-z])\.((?:X_HW_)?(?:DNSServers|DNSServer|MainDNS|DNSList))['\"]", t)
        for prefix, field in sorted(set(names), key=lambda n: n[1] != "DNSServers"):
            if re.search(rf"[?&'\"]{prefix}=({re.escape(LHCM)})(?![\w.])", t):
                return prefix, field, LHCM
        return None

    def _lan_dns_value(self, js: str, field: str):
        objs = parse_js_objects(_unescape(js), "dhcpmainst")
        if not objs:
            return None
        o = objs[0]
        for key in (field, field.replace("X_HW_", "")):
            if key in o:
                return o[key]
        return None

    def _statics(self) -> Dict[str, tuple]:
        """mac -> (instance path, ip) of the router's DHCP reservations."""
        out = {}
        for page in (STATIC_PAGE, LAN_DNS_PAGE):
            try:
                js = _unescape(self.s.get(page))
            except Exception:
                continue
            for o in parse_js_objects(js, "stDhcp"):
                mac = clean_mac(_field(o, "macAddress", "Chaddr"))
                dom = _field(o, "domain")
                if mac and dom.startswith(STATIC_OBJ):
                    out[mac] = (dom, _field(o, "ipAddress", "Yiaddr"))
            if out:
                break
        return out

    def _post(self, query: str, request_file: str, fields: Dict[str, str]):
        token = self.s.token_of(self.s.get(request_file))
        body = dict(fields)
        body["x.X_HW_Token"] = token
        with self.s.lock:
            return self.s._request("POST", f"{query}&RequestFile={request_file}", body)

    def lan_dns_status(self, pc_ip: str, pc_mac: str) -> dict:
        try:
            js = self.s.get(LAN_DNS_PAGE)
        except Exception:
            return {"supported": False}
        form = self._dns_form(js)
        cur = self._lan_dns_value(js, form[1]) if form else None
        servers = [x.strip() for x in re.split(r"[,;\s]+", cur or "") if x.strip()]
        st = self._statics()
        return {"supported": bool(form) and cur is not None,
                "active": bool(servers) and servers[0] == pc_ip,
                "reserved": st.get(clean_mac(pc_mac), ("", ""))[1] == pc_ip,
                "backup": self._state_get("lan_dns_backup") is not None}

    def lan_dns_setup(self, pc_ip: str, pc_mac: str) -> str:
        mac = clean_mac(pc_mac)
        if not re.fullmatch(r"\d+\.\d+\.\d+\.\d+", pc_ip or "") or not mac:
            raise RuntimeError("This PC's address or MAC isn't known yet")
        js = self.s.get(LAN_DNS_PAGE)
        form = self._dns_form(js)
        orig = self._lan_dns_value(js, form[1]) if form else None
        if not form or orig is None:
            raise NotImplementedError("unsupported: send a router report from Settings › Router")
        prefix, field, obj = form
        # 1. save the original before touching anything (kept if setup runs twice)
        if self._state_get("lan_dns_backup") is None:
            self._state_set("lan_dns_backup", {"prefix": prefix, "field": field, "obj": obj, "value": orig})
        # 2. reserve this PC's address
        statics = self._statics()
        added = None
        if mac in statics:
            if statics[mac][1] != pc_ip:
                raise RuntimeError(f"This PC is already reserved to {statics[mac][1]} in the router")
        else:
            if any(ip == pc_ip for _, ip in statics.values()):
                raise RuntimeError(f"{pc_ip} is reserved for another device in the router")
            self.s.submit("add.cgi", STATIC_OBJ, STATIC_PAGE,
                          {"x.Yiaddr": pc_ip, "x.Chaddr": mac, "x.Enable": "1"})
            time.sleep(1)
            statics = self._statics()
            if statics.get(mac, ("", ""))[1] != pc_ip:
                raise RuntimeError("The router didn't accept the address reservation – nothing else was changed")
            added = statics[mac][0]
            self._state_set("static_added", added)
        # 3. point the network's DNS at this PC (fallback 1.1.1.1)
        value = f"{pc_ip},{SECONDARY_DNS}"
        try:
            self._post(f"set.cgi?{prefix}={obj}", LAN_DNS_PAGE, {f"{prefix}.{field}": value})
            time.sleep(1)
            now = self._lan_dns_value(self.s.get(LAN_DNS_PAGE), field) or ""
            if [x for x in re.split(r"[,;\s]+", now) if x][:1] != [pc_ip]:
                raise RuntimeError("The router didn't accept the new DNS setting")
        except Exception:
            if added:                       # roll back this run's reservation
                try:
                    self.s.submit("del.cgi", "", STATIC_PAGE, {added: ""})
                    self._state_set("static_added", None)
                except Exception:
                    pass
            raise
        self._heavy_at = None
        return f"DNS {value}; reserved {pc_ip}"

    def lan_dns_undo(self) -> str:
        done = []
        b = self._state_get("lan_dns_backup")
        if b:
            self._post(f"set.cgi?{b['prefix']}={b['obj']}", LAN_DNS_PAGE, {f"{b['prefix']}.{b['field']}": b["value"]})
            time.sleep(1)
            now = self._lan_dns_value(self.s.get(LAN_DNS_PAGE), b["field"])
            if (now or "") != (b["value"] or ""):
                raise RuntimeError("The router didn't take back its original DNS setting")
            self._state_set("lan_dns_backup", None)
            done.append("Network DNS: original setting restored")
        added = self._state_get("static_added")
        if added:
            if any(p == added for p, _ in self._statics().values()):
                self.s.submit("del.cgi", "", STATIC_PAGE, {added: ""})
                time.sleep(1)
            self._state_set("static_added", None)
            done.append("Address reservation added by NetScope: removed")
        return "\n".join(done)

    # small persistent state (survives restarts) ------------------------------
    def _state_file(self):
        from ..settings import data_dir
        safe_host = re.sub(r"[^\w.-]", "_", self.s.host)
        return data_dir() / f"huawei_{safe_host}.json"

    def _state_get(self, key):
        import json
        try:
            return json.loads(self._state_file().read_text()).get(key)
        except Exception:
            return None

    def _state_set(self, key, value):
        import json
        try:
            data = json.loads(self._state_file().read_text())
        except Exception:
            data = {}
        data[key] = value
        self._state_file().write_text(json.dumps(data))

    def close(self) -> None:
        self.s.logout()
