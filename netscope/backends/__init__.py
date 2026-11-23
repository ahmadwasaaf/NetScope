"""Adapter registry + SmartBackend (adds UPnP line counters to adapters that lack them)."""
from .base import Cap, Device, RouterBackend, Snapshot, clean_domain, clean_mac
from .demo import DemoBackend
from .detect import Detection, default_gateway, detect
from .generic import GenericBackend, UPnPCounter, WanMeter
from .openwrt import OpenWrtBackend
from .tplink import TPLinkBackend
from .huawei import HuaweiBackend

ADAPTERS = {
    "demo": "Demo",
    "tplink": "TP-Link / Mercusys",
    "huawei": "Huawei (WE · EchoLife / OptiXstar)",
    "openwrt": "OpenWrt",
    "generic": "Universal (UPnP)",
}


class SmartBackend(RouterBackend):
    """Wraps the brand adapter and fills gaps (line total / live speed) from UPnP."""

    def __init__(self, primary: RouterBackend):
        super().__init__()
        self.primary = primary
        self.kind = primary.kind
        self.is_demo = primary.is_demo
        self.min_interval = primary.min_interval
        self.wan = None

    def connect(self) -> None:
        self.primary.connect()
        self.capabilities = set(self.primary.capabilities)
        self.router_name = self.primary.router_name
        if not {Cap.TOTAL_USAGE, Cap.LIVE_SPEED} <= self.capabilities and not self.primary.is_demo:
            upnp = UPnPCounter.discover()
            if upnp:
                self.wan = WanMeter(upnp)
                self.capabilities |= {Cap.TOTAL_USAGE, Cap.LIVE_SPEED}

    def poll(self) -> Snapshot:
        if not self.capabilities:
            self.connect()
        s = self.primary.poll()
        if self.wan and (s.wan_delta is None or s.down_bps is None):
            try:
                d, down, up = self.wan.read()
                s.wan_delta = d if s.wan_delta is None else s.wan_delta
                if s.down_bps is None:
                    s.down_bps, s.up_bps = down, up
            except Exception:
                pass
        return s

    def block_device(self, mac): self.primary.block_device(mac)
    def unblock_device(self, mac): self.primary.unblock_device(mac)
    def block_domain(self, d): self.primary.block_domain(d)
    def unblock_domain(self, d): self.primary.unblock_domain(d)
    def close(self): self.primary.close()

    def lan_dns_status(self, pc_ip, pc_mac):
        if not hasattr(self.primary, "lan_dns_status"):
            return {"supported": False}
        return self.primary.lan_dns_status(pc_ip, pc_mac)

    def lan_dns_setup(self, pc_ip, pc_mac):
        if not hasattr(self.primary, "lan_dns_setup"):
            raise NotImplementedError("This router type can't be set up automatically yet")
        return self.primary.lan_dns_setup(pc_ip, pc_mac)

    def lan_dns_undo(self):
        if not hasattr(self.primary, "lan_dns_undo"):
            return ""
        return self.primary.lan_dns_undo()

    def undo_all(self) -> str:
        if not hasattr(self.primary, "undo_all"):
            return "Nothing to undo for this router type."
        return self.primary.undo_all()

    def diagnostics(self) -> str:
        if not hasattr(self.primary, "diagnostics"):
            raise NotImplementedError("No report for this router type")
        text = self.primary.diagnostics()
        caps = ", ".join(sorted(self.capabilities))
        return text + f"\n\nCapabilities: {caps}\nWAN stats page: {getattr(self.primary, 'wan_page', None)}\n"


def create_backend(settings, seed_demo: bool = False) -> RouterBackend:
    a = settings.backend
    if a == "openwrt":
        prim = OpenWrtBackend(settings.host, settings.port, settings.username, settings.get_password())
    elif a == "tplink":
        prim = TPLinkBackend(settings.host, settings.username, settings.get_password())
    elif a == "huawei":
        prim = HuaweiBackend(settings.host, settings.username, settings.get_password())
    elif a == "generic":
        prim = GenericBackend(settings.host)
    else:
        prim = DemoBackend(seed_history=seed_demo)
    return SmartBackend(prim)
