"""TP-Link / Mercusys adapter (uses the open-source `tplinkrouterc6u` library, which handles
the encrypted login of Archer, Deco, MR/LTE, VR/VDSL, EX, XDR, C-series … firmwares).

The TP-Link web interface allows only ONE logged-in admin, so NetScope logs in, reads,
and logs out on every poll – you can still open the router page in your browser.
"""
import time
from typing import Optional

from .base import (Cap, CounterDelta, Device, RouterBackend, Snapshot, clean_mac,
                   guess_type)

try:
    from tplinkrouterc6u import TplinkRouterProvider
except ImportError:  # pragma: no cover
    TplinkRouterProvider = None


def _mac_of(item) -> Optional[str]:
    for attr in ("macaddr", "mac", "mac_address"):
        v = item.get(attr) if isinstance(item, dict) else getattr(item, attr, None)
        if v:
            try:
                return clean_mac(str(v))
            except ValueError:
                return None
    return None


class TPLinkBackend(RouterBackend):
    kind = "tplink"
    min_interval = 15

    def __init__(self, host: str, username: str, password: str):
        super().__init__()
        self.url = host if host.startswith("http") else f"http://{host}"
        self.username = username or "admin"
        self.password = password
        self.client = None
        self.delta = CounterDelta()
        self.t_last = None
        self.has_traffic = False

    # -------------------------------------------------------------- session
    def _session(self):
        if TplinkRouterProvider is None:
            raise RuntimeError("Missing library: pip install tplinkrouterc6u")
        if self.client is None:
            self.client = TplinkRouterProvider.get_client(self.url, self.password, self.username)
        self.client.authorize()
        return self.client

    def _logout(self):
        try:
            if self.client:
                self.client.logout()
        except Exception:
            pass

    def connect(self) -> None:
        c = self._session()
        try:
            try:
                fw = c.get_firmware()
                self.router_name = f"TP-Link {getattr(fw, 'model', '') or ''}".strip()
            except Exception:
                self.router_name = "TP-Link"
            status = c.get_status()
            self.capabilities = {Cap.DEVICE_LIST}
            devs = getattr(status, "devices", []) or []
            if any(getattr(d, "traffic_usage", None) is not None for d in devs):
                self.capabilities.add(Cap.DEVICE_USAGE)
                self.has_traffic = True
            if any(getattr(d, "down_speed", None) is not None for d in devs) or \
                    getattr(status, "clients_down_speed", None) is not None or hasattr(c, "get_wan_speed"):
                self.capabilities.add(Cap.LIVE_SPEED)
            if hasattr(c, "block_device") and hasattr(c, "unblock_device"):
                self.capabilities.add(Cap.DEVICE_BLOCK)
        finally:
            self._logout()

    # -------------------------------------------------------------- poll
    def poll(self) -> Snapshot:
        if not self.capabilities:
            self.connect()
        c = self._session()
        s = Snapshot(router_name=self.router_name)
        now = time.monotonic()
        dt = (now - self.t_last) if self.t_last else None
        self.t_last = now
        try:
            status = c.get_status()
            down = up = 0.0
            for d in getattr(status, "devices", []) or []:
                mac = _mac_of(d)
                if not mac:
                    continue
                name = getattr(d, "hostname", "") or mac
                ip = str(getattr(d, "ipaddr", "") or "")
                active = getattr(d, "active", True)
                s.devices.append(Device(mac, name, ip, guess_type(name), bool(active)))
                ds, us = getattr(d, "down_speed", None), getattr(d, "up_speed", None)
                down += ds or 0
                up += us or 0
                usage = getattr(d, "traffic_usage", None)
                if usage is not None:
                    s.device_deltas[mac] = self.delta(mac, int(usage))
                elif dt and (ds or us):
                    # router gives only live rates -> integrate them over the poll interval
                    s.device_deltas[mac] = int(((ds or 0) + (us or 0)) * dt)
            s.down_bps = getattr(status, "clients_down_speed", None) or down or None
            s.up_bps = getattr(status, "clients_up_speed", None) or up or None
            if s.down_bps is None and hasattr(c, "get_wan_speed"):
                try:
                    w = c.get_wan_speed()
                    s.down_bps = getattr(w, "download", None) or getattr(w, "down", None)
                    s.up_bps = getattr(w, "upload", None) or getattr(w, "up", None)
                except Exception:
                    pass
            if Cap.DEVICE_BLOCK in self.capabilities and hasattr(c, "get_blocked_devices"):
                try:
                    s.blocked_macs = {m for m in map(_mac_of, c.get_blocked_devices() or []) if m}
                except Exception:
                    pass
        finally:
            self._logout()
        return s

    # -------------------------------------------------------------- actions
    def block_device(self, mac: str) -> None:
        c = self._session()
        try:
            c.block_device(clean_mac(mac).replace(":", "-").upper())
            if hasattr(c, "get_access_control") and hasattr(c, "set_access_control"):
                state = c.get_access_control()
                enabled = getattr(state, "enable", None)
                if enabled is None and isinstance(state, dict):
                    enabled = state.get("enable")
                if not enabled:
                    c.set_access_control(enable=True)
        finally:
            self._logout()

    def unblock_device(self, mac: str) -> None:
        c = self._session()
        try:
            c.unblock_device(clean_mac(mac).replace(":", "-").upper())
        finally:
            self._logout()

    def close(self) -> None:
        self._logout()
        self.client = None
