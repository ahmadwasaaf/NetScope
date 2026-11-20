"""Router adapter interface.

NetScope never touches other machines' traffic directly: every measurement and every
block/unblock is performed *by the router itself* through its admin interface, using the
credentials of the router administrator.

Each adapter declares the capabilities its router really offers; the UI adapts to them
(features a router can't do are shown as unavailable instead of failing silently).
"""
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

MAC_RE = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")
DOMAIN_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")


class Cap:
    TOTAL_USAGE = "total_usage"      # whole-line counter (WAN)
    LIVE_SPEED = "live_speed"        # current down / up rate
    DEVICE_LIST = "device_list"
    DEVICE_USAGE = "device_usage"    # bytes per device
    DEVICE_BLOCK = "device_block"    # cut / restore internet per device
    SITE_USAGE = "site_usage"
    SITE_BLOCK = "site_block"
    ALL = {TOTAL_USAGE, LIVE_SPEED, DEVICE_LIST, DEVICE_USAGE, DEVICE_BLOCK, SITE_USAGE, SITE_BLOCK}


def clean_mac(mac: str) -> str:
    m = mac.strip().lower().replace("-", ":")
    if re.fullmatch(r"[0-9a-f]{12}", m):
        m = ":".join(m[i:i + 2] for i in range(0, 12, 2))
    if not MAC_RE.match(m):
        raise ValueError(f"Invalid MAC address: {mac}")
    return m


def clean_domain(domain: str) -> str:
    d = domain.strip().lower()
    for prefix in ("https://", "http://"):
        if d.startswith(prefix):
            d = d[len(prefix):]
    d = d.split("/")[0].split(":")[0]
    if d.startswith("www."):
        d = d[4:]
    if not DOMAIN_RE.match(d):
        raise ValueError(f"Invalid domain: {domain}")
    return d


@dataclass
class Device:
    mac: str
    name: str
    ip: str
    dtype: str = "other"   # pc | phone | tv | laptop | tablet | other
    online: bool = True


@dataclass
class Snapshot:
    online: bool = True
    devices: List[Device] = field(default_factory=list)
    device_deltas: Dict[str, int] = field(default_factory=dict)   # mac -> bytes since last poll
    site_deltas: Dict[str, int] = field(default_factory=dict)     # domain -> bytes since last poll
    wan_delta: Optional[int] = None                               # whole line, bytes since last poll
    down_bps: Optional[float] = None                              # bytes / second
    up_bps: Optional[float] = None
    blocked_macs: Set[str] = field(default_factory=set)
    blocked_domains: Set[str] = field(default_factory=set)
    router_name: str = ""
    capabilities: Set[str] = field(default_factory=set)
    error: str = ""


class RouterBackend(ABC):
    kind = "base"
    is_demo = False
    min_interval = 2           # seconds – some routers dislike frequent logins

    def __init__(self):
        self.capabilities: Set[str] = set()
        self.router_name = ""

    def has(self, cap: str) -> bool:
        return cap in self.capabilities

    @abstractmethod
    def connect(self) -> None:
        """Log in / probe the router and fill self.capabilities + self.router_name."""

    @abstractmethod
    def poll(self) -> Snapshot: ...

    def block_device(self, mac: str) -> None:
        raise NotImplementedError("This router does not allow cutting a device from NetScope")

    def unblock_device(self, mac: str) -> None:
        raise NotImplementedError("This router does not allow restoring a device from NetScope")

    def block_domain(self, domain: str) -> None:
        raise NotImplementedError("This router does not support website blocking")

    def unblock_domain(self, domain: str) -> None:
        raise NotImplementedError("This router does not support website blocking")

    def close(self) -> None:
        pass


def guess_type(name: str) -> str:
    n = (name or "").lower()
    if any(k in n for k in ("iphone", "android", "galaxy", "redmi", "pixel", "phone", "huawei",
                            "oppo", "vivo", "realme", "xiaomi", "honor", "infinix", "tecno")):
        return "phone"
    if any(k in n for k in ("tv", "bravia", "roku", "chromecast", "firetv", "webos", "tizen", "shield")):
        return "tv"
    if any(k in n for k in ("ipad", "tab")):
        return "tablet"
    if any(k in n for k in ("laptop", "macbook", "notebook", "thinkpad", "book")):
        return "laptop"
    if any(k in n for k in ("desktop", "pc", "win", "imac")):
        return "pc"
    return "other"


class CounterDelta:
    """Turns cumulative counters into deltas, surviving counter resets / 32-bit wraps."""

    def __init__(self):
        self.prev: Dict[str, int] = {}

    def __call__(self, key: str, value: int, wrap32: bool = False) -> int:
        prev = self.prev.get(key)
        self.prev[key] = value
        if prev is None:
            return 0
        if value >= prev:
            return value - prev
        if wrap32 and prev > 2 ** 31:
            return value + 2 ** 32 - prev
        return value          # counter restarted from zero
