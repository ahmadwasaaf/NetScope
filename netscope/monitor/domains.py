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
    "googleusercontent.com": "google.com", "akamaized.net": "akamai (CDN)", "akamaiedge.net": "akamai (CDN)", "akamai.net": "akamai (CDN)",
    "cloudfront.net": "amazon (CDN)", "amazonaws.com": "amazon (CDN)",
    "live.com": "microsoft.com", "office.net": "microsoft.com", "msn.com": "microsoft.com",
    "xboxlive.com": "microsoft.com", "microsoftonline.com": "microsoft.com", "bing.com": "microsoft.com",
    "steamcontent.com": "steampowered.com", "steamstatic.com": "steampowered.com",
    "aaplimg.com": "apple.com", "icloud.com": "apple.com", "mzstatic.com": "apple.com",
    "spotifycdn.com": "spotify.com", "scdn.co": "spotify.com",
    "telegram.org": "telegram.org", "t.me": "telegram.org",
    "shahid.net": "shahid.mbc.net", "mbc.net": "shahid.mbc.net",
}

# Background traffic of phones/PCs (OS services, updates, push, analytics, ads, time, carrier).
# Shown grouped as one "System services" row so the sites people actually use stay on top.
SYSTEM_SITES = {
    # Google / Android infrastructure
    "gstatic.com", "googleapis.com", "gvt1.com", "gvt2.com", "gvt3.com", "1e100.net", "googleadservices.com",
    "googlesyndication.com", "doubleclick.net", "google-analytics.com", "googletagmanager.com",
    "app-measurement.com", "crashlytics.com", "firebaseio.com", "firebase.com", "android.com",
    "googlezip.net", "withgoogle.com", "xn--ngstr-lra8j.com",
    # carriers / Wi-Fi calling
    "3gppnetwork.org", "pub.3gppnetwork.org",
    # phone makers
    "samsung.com", "samsungcloud.com", "samsungapps.com", "samsungosp.com", "samsungdm.com",
    "samsungqbe.com", "samsungrs.com", "samsungiotcloud.com", "samsungconsent.com", "samsungdisplay.com",
    "xiaomi.com", "xiaomi.net", "miui.com", "mi.com", "hicloud.com", "dbankcloud.com", "huawei.com",
    "oppomobile.com", "heytapmobi.com", "heytapmobile.com", "coloros.com", "realme.com",
    "transsion.com", "shalltry.com", "transsion-os.com", "itel-mobile.com", "infinixmobility.com",
    "vivo.com", "vivo.com.cn", "oneplus.com", "oneplus.net", "motorola.com", "lenovo.com",
    # Apple / Microsoft background
    "icloud-content.com", "apple-dns.net", "aaplimg.com", "push.apple.com",
    "msftconnecttest.com", "msftncsi.com", "windowsupdate.com", "msedge.net", "trafficmanager.net",
    "azureedge.net", "windows.com", "events.data.microsoft.com", "digicert.com", "msocsp.com",
    "lencr.org", "pki.goog", "letsencrypt.org", "sectigo.com", "globalsign.com",
    # time, analytics, ad networks, SDKs
    "ntp.org", "time.google.com", "appsflyer.com", "adjust.com", "branch.io", "onesignal.com",
    "unity3d.com", "applovin.com", "ironsrc.com", "ironsrc.mobi", "inmobi.com", "mopub.com",
    "facebook-hardware.com", "scorecardresearch.com", "sentry.io", "bugsnag.com", "mixpanel.com",
    "amplitude.com", "segment.io", "newrelic.com", "nr-data.net", "adcolony.com", "vungle.com",
    "chartboost.com", "pangle.io", "pangleglobal.com", "bytedance-ads.com",
    # security apps (updates, cloud scans)
    "kaspersky.com", "kaspersky-labs.com", "kaspersky.ru", "kas-labs.com", "avast.com", "avg.com",
    "avcdn.net", "norton.com", "nortonlifelock.com", "symantec.com", "symcd.com", "mcafee.com",
    "mcafee-cloud.com", "eset.com", "eset.systems", "bitdefender.com", "bitdefender.net",
    "malwarebytes.com", "avira.com", "sophos.com", "trendmicro.com", "drweb.com",
    # Samsung assistant / Galaxy AI
    "bixby.com", "bixbyllm.com", "samsungbixby.com", "galaxyai.com",
}

# country versions of the same site (google.com.eg, amazon.eg …) are shown as one
BRANDS = {"google", "youtube", "amazon", "yahoo", "bing", "ebay", "apple", "microsoft", "netflix",
          "samsung", "xiaomi", "booking", "olx", "jumia", "noon", "aliexpress", "ikea", "zara",
          "hm", "nike", "adidas", "shein", "temu", "uber", "careem", "talabat", "spotify"}


HOST_ALIASES = [
    (re.compile(r"^(youtubei|youtube|youtubeanalytics|yt3)\.(googleapis|ggpht)\.com$"), "youtube.com"),
    (re.compile(r"^(www\.)?youtube\.googleapis\.com$"), "youtube.com"),
    (re.compile(r"^(instagram|i\.instagram)\.c10r\.facebook\.com$"), "instagram.com"),
    (re.compile(r"(^|\.)whatsapp\.(net|com)$"), "whatsapp.com"),
]


def is_system(site: str) -> bool:
    return site in SYSTEM_SITES


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
    for rx, site in HOST_ALIASES:              # app APIs that live on shared infrastructure
        if rx.search(h):
            return site
    b = base_domain(h)
    brand = b.split(".")[0]
    if brand in BRANDS and b != f"{brand}.com":
        b = f"{brand}.com"
    return ALIASES.get(b, b)


def is_public(ip: str) -> bool:
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return not (a.is_private or a.is_loopback or a.is_multicast or a.is_link_local
                or a.is_reserved or a.is_unspecified)
