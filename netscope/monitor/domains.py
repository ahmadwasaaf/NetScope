"""Turn raw host names into the website a person would recognise."""
import ipaddress
import re

TWO_LEVEL_TLDS = {
    "co.uk", "org.uk", "ac.uk", "gov.uk", "com.eg", "gov.eg", "edu.eg", "net.eg", "org.eg",
    "com.sa", "gov.sa", "co.jp", "com.au", "com.br", "com.tr", "co.in", "com.cn", "com.hk",
}

# CDN / helper domains -> the site they belong to
ALIASES = {
    "googlevideo.com": "youtube.com", "ytimg.com": "youtube.com", "youtu.be": "youtube.com",
    "youtube-nocookie.com": "youtube.com", "ggpht.com": "youtube.com",
    "fbcdn.net": "facebook.com", "facebook.net": "facebook.com", "fb.com": "facebook.com",
    "messenger.com": "facebook.com",
    "cdninstagram.com": "instagram.com",
    "whatsapp.net": "whatsapp.com",
    "nflxvideo.net": "netflix.com", "nflximg.net": "netflix.com", "nflxext.com": "netflix.com",
    "nflxso.net": "netflix.com",
    "tiktokcdn.com": "tiktok.com", "tiktokv.com": "tiktok.com", "byteoversea.com": "tiktok.com",
    "ibytedtos.com": "tiktok.com", "tiktokcdn-us.com": "tiktok.com", "muscdn.com": "tiktok.com",
    "twimg.com": "x.com", "twitter.com": "x.com", "t.co": "x.com",
    "sc-cdn.net": "snapchat.com", "snapkit.com": "snapchat.com",
    "gvt1.com": "google.com", "gstatic.com": "google.com", "googleapis.com": "google.com",
    "googleusercontent.com": "google.com", "gvt2.com": "google.com", "1e100.net": "google.com",
    "googlesyndication.com": "google.com", "doubleclick.net": "google.com",
    "akamaized.net": "akamai (CDN)", "akamaiedge.net": "akamai (CDN)", "akamai.net": "akamai (CDN)",
    "cloudfront.net": "amazon (CDN)", "amazonaws.com": "amazon (CDN)",
    "msftconnecttest.com": "microsoft.com", "windowsupdate.com": "microsoft.com",
    "live.com": "microsoft.com", "office.net": "microsoft.com", "msn.com": "microsoft.com",
    "xboxlive.com": "microsoft.com", "microsoftonline.com": "microsoft.com", "bing.com": "microsoft.com",
    "steamcontent.com": "steampowered.com", "steamstatic.com": "steampowered.com",
    "aaplimg.com": "apple.com", "icloud.com": "apple.com", "mzstatic.com": "apple.com",
    "spotifycdn.com": "spotify.com", "scdn.co": "spotify.com",
    "telegram.org": "telegram.org", "t.me": "telegram.org",
    "shahid.net": "shahid.mbc.net", "mbc.net": "shahid.mbc.net",
}

IGNORE = re.compile(r"(\.local|\.lan|\.home|\.arpa|\.internal|localhost)$|^wpad\.|^_", re.I)
HOST_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")


def base_domain(host: str) -> str:
    parts = host.lower().rstrip(".").split(".")
    if len(parts) >= 3 and ".".join(parts[-2:]) in TWO_LEVEL_TLDS:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def site_of(host: str):
    """Host name -> website, or None for names that are not websites (local, reverse DNS…)."""
    if not host:
        return None
    h = host.lower().rstrip(".")
    if IGNORE.search(h) or not HOST_RE.match(h):
        return None
    b = base_domain(h)
    return ALIASES.get(b, b)


def is_public(ip: str) -> bool:
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return not (a.is_private or a.is_loopback or a.is_multicast or a.is_link_local
                or a.is_reserved or a.is_unspecified)
