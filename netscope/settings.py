"""Persistent settings stored as JSON in %APPDATA%\\NetScope (password goes to Windows Credential Manager)."""
import json
import os
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path

try:
    import keyring  # Windows Credential Manager
except ImportError:  # pragma: no cover
    keyring = None

KEYRING_SERVICE = "NetScope-router"


def data_dir() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home() / ".config")
    p = Path(base) / "NetScope"
    p.mkdir(parents=True, exist_ok=True)
    return p


@dataclass
class Settings:
    language: str = "en"          # "en" | "ar"
    theme: str = "dark"           # "dark" | "light"
    accent: str = "#D6006A"
    start_date: str = field(default_factory=lambda: date.today().replace(day=1).isoformat())
    backend: str = "demo"         # demo | tplink | openwrt | generic
    onboarded: bool = False
    quota_gb: float = 0.0         # monthly bundle, 0 = no limit
    nicknames: dict = field(default_factory=dict)   # mac -> custom name
    router_label: str = ""
    pc_monitor: bool = True       # real bytes per website for this PC (Npcap)
    dns_monitor: bool = False     # rank websites of other devices (DNS on this PC)
    sites_tab: str = "pc"         # "pc" | "others"
    host: str = "192.168.1.1"
    port: int = 22
    username: str = "admin"
    refresh_sec: int = 10
    devices_open: bool = True
    sites_open: bool = False

    # ---------- password (never written to the JSON file) ----------
    def get_password(self) -> str:
        if keyring:
            try:
                return keyring.get_password(KEYRING_SERVICE, f"{self.username}@{self.host}") or ""
            except Exception:
                return ""
        return ""

    def set_password(self, pwd: str) -> None:
        if keyring:
            try:
                keyring.set_password(KEYRING_SERVICE, f"{self.username}@{self.host}", pwd)
            except Exception:
                pass

    # ---------- load / save ----------
    @property
    def start(self) -> date:
        return date.fromisoformat(self.start_date)

    @classmethod
    def load(cls) -> "Settings":
        f = data_dir() / "settings.json"
        if f.exists():
            try:
                raw = json.loads(f.read_text(encoding="utf-8"))
                known = {k: v for k, v in raw.items() if k in cls.__dataclass_fields__}
                return cls(**known)
            except Exception:
                pass
        return cls()

    def save(self) -> None:
        (data_dir() / "settings.json").write_text(
            json.dumps(asdict(self), indent=2, ensure_ascii=False), encoding="utf-8")
