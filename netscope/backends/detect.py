"""Find the router (default gateway) and recognise its brand from its own login page."""
import re
import socket
import ssl
import sys
import urllib.request
from dataclasses import dataclass

from .generic import _run

BRANDS = [  # (brand id, display, adapter, keywords)
    ("openwrt", "OpenWrt", "openwrt", ("luci", "openwrt", "lede")),
    ("huawei", "Huawei", "huawei", ("getrandcount", "huawei", "echolife", "optixstar", "hg8", "dn8", "eg8")),
    ("tplink", "TP-Link", "tplink", ("tp-link", "tplink", "archer", "mercusys", "tplinkwifi")),
    ("zte", "ZTE", "generic", ("zte", "zxhn", "zxv10")),
    ("mikrotik", "MikroTik", "generic", ("mikrotik", "routeros")),
    ("asus", "ASUS", "generic", ("asuswrt", "asus")),
    ("netgear", "NETGEAR", "generic", ("netgear",)),
    ("dlink", "D-Link", "generic", ("d-link", "dlink")),
    ("xiaomi", "Xiaomi", "generic", ("xiaomi", "miwifi")),
    ("nokia", "Nokia", "generic", ("nokia", "alcatel")),
    ("fritz", "FRITZ!Box", "generic", ("fritz",)),
]


@dataclass
class Detection:
    gateway: str = ""
    brand: str = "unknown"
    display: str = ""
    adapter: str = "generic"
    needs_login: bool = False


def default_gateway() -> str:
    if sys.platform == "win32":
        out = _run(["route", "print", "-4", "0.0.0.0"])
        m = re.search(r"^\s*0\.0\.0\.0\s+0\.0\.0\.0\s+(\d+\.\d+\.\d+\.\d+)", out, re.M)
    else:
        out = _run(["ip", "-4", "route", "show", "default"])
        m = re.search(r"default via (\d+\.\d+\.\d+\.\d+)", out)
    if m:
        return m.group(1)
    try:  # fallback: our own address with .1 at the end
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ".".join(ip.split(".")[:3]) + ".1"
    except OSError:
        return "192.168.1.1"


def _ctx():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE        # router pages use self-signed certificates
    return ctx


def _fetch(url: str, data: bytes = None) -> str:
    try:
        r = urllib.request.urlopen(url, data=data, timeout=4,
                                   context=_ctx() if url.startswith("https") else None)
        return " ".join(f"{k}:{v}" for k, v in r.headers.items()) + " " + \
            r.read(65536).decode("utf-8-sig", errors="replace")
    except Exception:
        return ""


def _page(gw: str) -> str:
    # read BOTH http and https – many ISP routers answer http with a tiny redirect page
    return (_fetch(f"http://{gw}/") + " " + _fetch(f"https://{gw}/")).lower()


def _is_huawei(gw: str) -> bool:
    """Huawei EchoLife / OptiXstar web UIs hand out a short login token here."""
    for scheme in ("https", "http"):
        body = _fetch(f"{scheme}://{gw}/asp/GetRandCount.asp", data=b"")
        token = body.split()[-1] if body.split() else ""
        if re.fullmatch(r"[0-9a-fA-F]{6,64}", token):
            return True
    return False


def _ssh_banner(gw: str) -> str:
    try:
        with socket.create_connection((gw, 22), timeout=2) as s:
            return s.recv(128).decode(errors="replace").lower()
    except OSError:
        return ""


def detect(gateway: str = "") -> Detection:
    gw = gateway or default_gateway()
    if _is_huawei(gw):
        return Detection(gw, "huawei", "Huawei", "huawei", True)
    blob = _page(gw)
    for brand, display, adapter, keys in BRANDS:
        if any(k in blob for k in keys):
            return Detection(gw, brand, display, adapter, adapter != "generic")
    if "dropbear" in _ssh_banner(gw):
        return Detection(gw, "openwrt", "OpenWrt", "openwrt", True)
    return Detection(gw, "unknown", "", "generic", False)
