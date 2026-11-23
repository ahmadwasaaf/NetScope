"""Saved routers ("profiles").

Every router the user connects to is kept as its own profile:
  * its own password in Windows Credential Manager (key "profile:<id>")
  * its own usage history  (usage_<store_tag>.db)
  * its own undo data      (<adapter>_<state_tag>.json – the router's original settings)
  * its own bundle / start date / alarm / device nicknames
Identity is the router's LAN MAC address (+ model), never the IP alone: two different
networks often both use 192.168.1.1.

Settings keeps the ACTIVE router's values in its normal fields (backend, host, …) so the
rest of the app is unchanged; switching copies them in and out of the profile list.
"""
import json
import re
import uuid
from dataclasses import replace
from datetime import datetime
from typing import Dict, List, Optional

from .settings import KEYRING_SERVICE, Settings, data_dir, keyring

# per-router values that live in the Settings working copy while that router is active
ROUTER_FIELDS = ("backend", "host", "port", "username", "router_label", "start_date",
                 "quota_gb", "alarm_gb", "alarm_fired", "nicknames")


def _new_id() -> str:
    return uuid.uuid4().hex[:10]


def find(s: Settings, pid: str) -> Optional[dict]:
    return next((p for p in s.routers if p.get("id") == pid), None)


def active(s: Settings) -> Optional[dict]:
    return find(s, s.active_router) if s.active_router else None


def store_tag(s: Settings) -> str:
    p = active(s)
    if s.backend == "demo" or not p:
        return "demo" if s.backend == "demo" else re.sub(r"[^\w.-]", "_", f"{s.backend}_{s.host}")
    return p["store_tag"]


def state_tag(s: Settings) -> str:
    p = active(s)
    return p["state_tag"] if p else s.host


# ------------------------------------------------------------------ migration
def migrate(s: Settings) -> bool:
    """Turn the single router of older versions into the first profile (nothing is lost:
    the same usage file, undo file and password are reused)."""
    if s.routers or not s.onboarded or s.backend == "demo":
        return False
    pid = _new_id()
    prof = {f: getattr(s, f) for f in ROUTER_FIELDS}
    prof.update(id=pid, store_tag=re.sub(r"[^\w.-]", "_", f"{s.backend}_{s.host}"),
                state_tag=s.host, gw_mac="", model=s.router_label, added=datetime.now().isoformat(timespec="minutes"))
    s.routers = [prof]
    legacy = s.get_password()            # read with the old key before switching
    s.active_router = pid
    if legacy:
        s.set_password(legacy)           # copy to the profile key (the old key is kept as a backup)
    return True


# ------------------------------------------------------------------ switching
def save_active(s: Settings) -> None:
    """Copy the working values back into the active profile."""
    p = active(s)
    if p:
        for f in ROUTER_FIELDS:
            v = getattr(s, f)
            p[f] = dict(v) if isinstance(v, dict) else v


def activate(s: Settings, pid: str) -> bool:
    p = find(s, pid)
    if not p:
        return False
    save_active(s)
    s.active_router = pid
    for f in ROUTER_FIELDS:
        if f in p:
            v = p[f]
            setattr(s, f, dict(v) if isinstance(v, dict) else v)
    return True


def begin_new(s: Settings, host: str) -> Settings:
    """A working copy for the setup wizard: a fresh router id, fresh per-router values.
    Nothing is stored until commit_new()."""
    save_active(s)
    n = replace(s, nicknames={}, routers=[dict(p) for p in s.routers])
    n.active_router = _new_id()
    fresh = Settings()
    for f in ROUTER_FIELDS:
        setattr(n, f, getattr(fresh, f))
    n.host = host or fresh.host
    return n


def commit_new(s: Settings) -> None:
    """After the wizard: register the router it connected to (demo mode is never a profile)."""
    if s.backend == "demo":
        return
    if find(s, s.active_router):
        save_active(s)
        return
    prof = {f: getattr(s, f) for f in ROUTER_FIELDS}
    prof.update(id=s.active_router, store_tag=f"r_{s.active_router}", state_tag=f"r_{s.active_router}",
                gw_mac="", model=s.router_label, added=datetime.now().isoformat(timespec="minutes"))
    s.routers.append(prof)


def set_identity(s: Settings, pid: str, gw_mac: str = None, model: str = None) -> None:
    p = find(s, pid)
    if not p:
        return
    if gw_mac:
        p["gw_mac"] = gw_mac
    if model:
        p["model"] = model


def by_mac(s: Settings, mac: str) -> Optional[dict]:
    mac = (mac or "").lower()
    return next((p for p in s.routers if mac and p.get("gw_mac", "").lower() == mac), None)


# ------------------------------------------------------------------ removal
def _state_files(p: dict) -> List:
    tag = re.sub(r"[^\w.-]", "_", p.get("state_tag", ""))
    return [data_dir() / f"{p.get('backend', '')}_{tag}.json"]


def pending_changes(p: dict) -> List[str]:
    """Changes NetScope made on that router that are still in place (from its undo file)."""
    out = []
    for f in _state_files(p):
        try:
            st = json.loads(f.read_text())
        except Exception:
            continue
        if st.get("lan_dns_backup"):
            out.append("dns")
        if st.get("static_added"):
            out.append("reservation")
        if st.get("netscope_domains"):
            out.append("sites")
        if st.get("blocked_by_netscope"):
            out.append("devices")
    return out


def remove(s: Settings, pid: str) -> None:
    """Forget a router: profile, password, usage history and undo file."""
    p = find(s, pid)
    if not p:
        return
    if keyring:
        try:
            keyring.delete_password(KEYRING_SERVICE, f"profile:{pid}")
        except Exception:
            pass
    for f in _state_files(p) + [data_dir() / f"usage_{p.get('store_tag', '')}.db"]:
        try:
            f.unlink()
        except OSError:
            pass
    s.routers = [r for r in s.routers if r.get("id") != pid]
    if s.active_router == pid:
        s.active_router = ""
