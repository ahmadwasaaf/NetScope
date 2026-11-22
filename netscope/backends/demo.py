"""Demo backend – sample data so the interface can be tried without a router."""
import random

from .base import Cap, Device, RouterBackend, Snapshot, clean_domain, clean_mac

GB = 1024 ** 3

DEVICES = [
    Device("aa:bb:cc:00:00:02", "Desktop PC", "192.168.1.2", "pc"),
    Device("aa:bb:cc:00:00:03", "Mobile Device", "192.168.1.3", "phone"),
    Device("aa:bb:cc:00:00:04", "Smart TV", "192.168.1.4", "tv"),
    Device("aa:bb:cc:00:00:05", "Laptop", "192.168.1.5", "laptop"),
]
SEED_DEV = [72.40, 41.22, 26.10, 8.90]
SEED_SITES = {"youtube.com": 48.20, "facebook.com": 22.14, "netflix.com": 19.60,
              "instagram.com": 14.32, "tiktok.com": 8.75, "google.com": 3.42}


class DemoBackend(RouterBackend):
    is_demo = True
    kind = "demo"

    def __init__(self, seed_history: bool = True):
        super().__init__()
        self.capabilities = set(Cap.ALL)
        self.router_name = "Demo Router"
        self.first = seed_history
        self.blocked_macs: set = set()
        self.blocked_domains: set = set()

    def connect(self) -> None:
        pass

    def poll(self) -> Snapshot:
        s = Snapshot(devices=list(DEVICES), router_name=self.router_name,
                     blocked_macs=set(self.blocked_macs),
                     blocked_domains=set(self.blocked_domains))
        if self.first:
            self.first = False
            s.device_deltas = {d.mac: int(g * GB) for d, g in zip(DEVICES, SEED_DEV)}
            s.site_deltas = {k: int(v * GB) for k, v in SEED_SITES.items()}
            s.wan_delta = sum(s.device_deltas.values())
            s.line_down_kbps, s.line_up_kbps = 35840, 5120
            return s
        for d in DEVICES:
            if d.mac not in self.blocked_macs:
                s.device_deltas[d.mac] = random.randint(1, 40) * 1024 * 1024
        for site in SEED_SITES:
            if site not in self.blocked_domains:
                s.site_deltas[site] = random.randint(0, 25) * 1024 * 1024
        s.wan_delta = sum(s.device_deltas.values())
        s.line_down_kbps, s.line_up_kbps = 35840, 5120
        s.down_bps = random.uniform(0.6, 4.5) * 1024 * 1024
        s.up_bps = random.uniform(0.05, 0.6) * 1024 * 1024
        return s

    def block_device(self, mac): self.blocked_macs.add(clean_mac(mac))
    def unblock_device(self, mac): self.blocked_macs.discard(clean_mac(mac))
    def block_domain(self, domain): self.blocked_domains.add(clean_domain(domain))
    def unblock_domain(self, domain): self.blocked_domains.discard(clean_domain(domain))
