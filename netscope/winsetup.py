"""Windows settings NetScope can check and fix for "website names from phones".

Reading the state needs no admin rights. Each change runs one small PowerShell command
elevated, so Windows shows its normal UAC prompt ("Do you want to allow…?").
"""
import base64
import json
import subprocess
import sys

PUBLIC_DNS_CMD = ('Get-NetAdapter | ? Status -eq Up | Set-DnsClientServerAddress -ServerAddresses '
                  '("1.1.1.1","8.8.8.8","2606:4700:4700::1111","2001:4860:4860::8888")')
FW_NAME = "NetScope DNS"
NO_WINDOW = 0x08000000


def _ps(script: str, timeout: int = 20) -> str:
    if sys.platform != "win32":
        return ""
    enc = base64.b64encode(script.encode("utf-16-le")).decode()
    r = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                        "-EncodedCommand", enc], capture_output=True, text=True, timeout=timeout,
                       creationflags=NO_WINDOW)
    return r.stdout.strip()


def read_state(my_ip: str) -> dict:
    """{'ok': bool, 'if': int, 'profile': 'Private'|'Public'|'DomainAuthenticated'|'',
        'dns': [...], 'self_dns': bool, 'public_dns': bool, 'fw': bool}"""
    if sys.platform != "win32" or not my_ip:
        return {"ok": False}
    script = f"""
$ErrorActionPreference = 'SilentlyContinue'
$a = Get-NetIPAddress -AddressFamily IPv4 -IPAddress '{my_ip}' | Select-Object -First 1
$i = $a.InterfaceIndex
$p = (Get-NetConnectionProfile -InterfaceIndex $i | Select-Object -First 1).NetworkCategory
$d = @((Get-DnsClientServerAddress -InterfaceIndex $i -AddressFamily IPv4).ServerAddresses)
$f = @(Get-NetFirewallRule -DisplayName '{FW_NAME}*' | Where-Object Enabled -eq 'True').Count
@{{ if = $i; profile = "$p"; dns = $d; fw = $f }} | ConvertTo-Json -Compress
"""
    try:
        out = _ps(script)
        data = json.loads(out.splitlines()[-1]) if out else {}
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return {"ok": False}
    dns = data.get("dns") or []
    if isinstance(dns, str):
        dns = [dns]
    return {
        "ok": bool(data.get("if")),
        "if": data.get("if") or 0,
        "profile": data.get("profile") or "",
        "dns": dns,
        "self_dns": my_ip in dns or "127.0.0.1" in dns,
        "public_dns": any(d in ("1.1.1.1", "8.8.8.8", "9.9.9.9", "1.0.0.1", "8.8.4.4") for d in dns),
        "fw": int(data.get("fw") or 0) > 0,
    }


def run_elevated(script: str, timeout_ms: int = 60000) -> int:
    """Run PowerShell as administrator (UAC prompt). Returns the exit code,
    or -1223 when the user pressed No, or -1 when it couldn't start."""
    if sys.platform != "win32":
        return -1
    import ctypes
    from ctypes import wintypes

    class SHELLEXECUTEINFOW(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("fMask", ctypes.c_ulong), ("hwnd", wintypes.HWND),
                    ("lpVerb", wintypes.LPCWSTR), ("lpFile", wintypes.LPCWSTR),
                    ("lpParameters", wintypes.LPCWSTR), ("lpDirectory", wintypes.LPCWSTR),
                    ("nShow", ctypes.c_int), ("hInstApp", wintypes.HINSTANCE),
                    ("lpIDList", ctypes.c_void_p), ("lpClass", wintypes.LPCWSTR),
                    ("hkeyClass", wintypes.HKEY), ("dwHotKey", wintypes.DWORD),
                    ("hIconOrMonitor", wintypes.HANDLE), ("hProcess", wintypes.HANDLE)]

    enc = base64.b64encode(("$ErrorActionPreference='Stop'; try { " + script +
                            " ; exit 0 } catch { exit 1 }").encode("utf-16-le")).decode()
    info = SHELLEXECUTEINFOW()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = 0x00000040                      # SEE_MASK_NOCLOSEPROCESS
    info.lpVerb = "runas"
    info.lpFile = "powershell.exe"
    info.lpParameters = f"-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -EncodedCommand {enc}"
    info.nShow = 0
    if not ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(info)):
        return -ctypes.GetLastError() or -1
    k32 = ctypes.windll.kernel32
    k32.WaitForSingleObject(info.hProcess, timeout_ms)
    code = wintypes.DWORD(1)
    k32.GetExitCodeProcess(info.hProcess, ctypes.byref(code))
    k32.CloseHandle(info.hProcess)
    return int(code.value)


def set_private(if_index: int, private: bool = True) -> int:
    cat = "Private" if private else "Public"
    return run_elevated(f"Set-NetConnectionProfile -InterfaceIndex {int(if_index)} -NetworkCategory {cat}")


def allow_firewall(exe_path: str = "") -> int:
    rules = (f"Get-NetFirewallRule -DisplayName '{FW_NAME}*' -ErrorAction SilentlyContinue | Remove-NetFirewallRule; "
             f"New-NetFirewallRule -DisplayName '{FW_NAME} (UDP)' -Direction Inbound -Protocol UDP -LocalPort 53 "
             f"-Action Allow -Profile Private,Domain | Out-Null; "
             f"New-NetFirewallRule -DisplayName '{FW_NAME} (TCP)' -Direction Inbound -Protocol TCP -LocalPort 53 "
             f"-Action Allow -Profile Private,Domain | Out-Null")
    return run_elevated(rules)


def use_public_dns() -> int:
    return run_elevated(PUBLIC_DNS_CMD)
