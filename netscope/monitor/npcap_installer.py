"""Writes Install-Npcap.bat – a double-click helper that downloads the official Npcap
installer with PowerShell, verifies its digital signature, and starts it.

If the network's DNS can't find npcap.com, the helper looks the address up through a
public DNS server (1.1.1.1 / 8.8.8.8) and downloads with curl's --resolve, which still
checks the site's real HTTPS certificate.
"""
from pathlib import Path

BAT = r'''@echo off
title NetScope - Npcap installer
echo.
powershell -NoProfile -ExecutionPolicy Bypass -Command "$s=(Get-Content -Raw -LiteralPath '%~f0') -split '#=PS=#'; Invoke-Expression $s[-1]"
echo.
pause
exit /b
#=PS=#
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
Write-Host ' NetScope - Npcap installer helper' -ForegroundColor Magenta
Write-Host ' ---------------------------------'
try {
    $dll = Join-Path $env:SystemRoot 'System32\Npcap\wpcap.dll'
    if (Test-Path $dll) { Write-Host ' Npcap is already installed. Restart NetScope.' -ForegroundColor Green; return }
    $curl = Join-Path $env:SystemRoot 'System32\curl.exe'
    if (-not (Test-Path $curl)) { throw 'curl.exe was not found (Windows 10 1803 or newer is needed).' }

    $extra = @()
    try { [void][System.Net.Dns]::GetHostAddresses('npcap.com') }
    catch {
        Write-Host ' Your network DNS cannot find npcap.com - using public DNS for this download.' -ForegroundColor Yellow
        $ip = $null
        foreach ($srv in '1.1.1.1', '8.8.8.8') {
            try {
                $ip = (Resolve-DnsName npcap.com -Server $srv -Type A -ErrorAction Stop |
                       Where-Object { $_.IPAddress } | Select-Object -First 1).IPAddress
                if ($ip) { break }
            } catch {}
        }
        if (-not $ip) { throw 'npcap.com could not be resolved, even through public DNS. Check the internet connection.' }
        $extra = @('--resolve', "npcap.com:443:$ip")
    }

    $ver = '1.88'
    try {
        $html = (& $curl -s -L --max-time 20 @extra 'https://npcap.com/dist/') -join "`n"
        $best = [regex]::Matches($html, 'npcap-(\d+)\.(\d+)\.exe') |
                ForEach-Object { [version]("{0}.{1}" -f $_.Groups[1].Value, $_.Groups[2].Value) } |
                Sort-Object -Descending | Select-Object -First 1
        if ($best) { $ver = '{0}.{1}' -f $best.Major, $best.Minor }
    } catch {}

    $out = Join-Path $env:TEMP "npcap-$ver.exe"
    Write-Host " Downloading Npcap $ver ..."
    & $curl -L --fail --max-time 300 -o $out @extra "https://npcap.com/dist/npcap-$ver.exe"
    if ($LASTEXITCODE -ne 0) { throw "Download failed (curl exit code $LASTEXITCODE)." }

    $sig = Get-AuthenticodeSignature -FilePath $out
    if ($sig.Status -ne 'Valid') {
        Remove-Item $out -Force
        throw "The downloaded file has no valid digital signature ($($sig.Status)) - stopped for safety."
    }
    $who = $sig.SignerCertificate.Subject
    if ($who -notmatch 'Insecure\.Com|Nmap') {
        Write-Host " Signed by: $who" -ForegroundColor Yellow
        if ((Read-Host ' This is not the usual Npcap publisher. Continue? (Y/N)') -notmatch '^[Yy]') { Remove-Item $out -Force; return }
    } else {
        Write-Host ' Digital signature verified (Npcap / Nmap Project).' -ForegroundColor Green
    }

    Write-Host ' Starting the installer - keep the default options and click Install.'
    Start-Process -FilePath $out -Wait
    if (Test-Path $dll) { Write-Host ' Done! Restart NetScope.' -ForegroundColor Green }
    else { Write-Host ' Npcap was not installed (installer closed?).' -ForegroundColor Yellow }
}
catch {
    Write-Host (' Error: ' + $_.Exception.Message) -ForegroundColor Red
}
'''


def write_installer(folder: Path) -> Path:
    path = Path(folder) / "Install-Npcap.bat"
    path.write_text(BAT.replace("\r\n", "\n").replace("\n", "\r\n"), encoding="ascii", newline="")
    return path
