"""Which functions have been confirmed on which router model (per function, not per brand).

A function NetScope offers but that is not listed here works from the adapter's knowledge
of the firmware, but nobody has confirmed it on that exact model yet - the UI says so.
Add a model here only after a real test on that model.
"""
from .base import Cap

TESTED = {
    # Huawei DN8245V-56 (WE / TE Data VDSL) - confirmed Nov 2026
    ("huawei", "DN8245V-56"): {Cap.DEVICE_LIST, Cap.TOTAL_USAGE, Cap.LIVE_SPEED, Cap.LINE_RATE,
                               Cap.DEVICE_BLOCK, "lan_dns", "lan_dns_v6", "catch_up"},
}


def tested(backend: str, router_name: str) -> set:
    name = (router_name or "").upper()
    out = set()
    for (b, model), caps in TESTED.items():
        if b == backend and model.upper() in name:
            out |= caps
    return out
