# Notes for the next build (do not build until the user says "ابدأ")

## Already in main (unreleased)
- Screenshot: long lists inside cards are captured in full (VISIBLE_ITEMS override).
- WAN counters counted per connection + rejection of impossible jumps + counter_events.log.

## Fix: catch-up usage from this PC while NetScope was closed
Confirmed by the user (two screenshots):
- 12:42 → 13:22, with NetScope closed in between.
- The total went up by 0.42 GB, but "This PC" stayed at 1.05 GB.
- So everything the PC used while NetScope was closed goes into the total without being attributed to any device. This is what made the phone look like the culprit in the earlier investigation.

Solution:
- Read this PC's usage for the closed period from Windows itself:
  - Primary source: `ConnectionProfile.GetNetworkUsageAsync(start, end)` (WinRT). No admin needed, and it works across reboots and shutdowns. Use PowerShell or winrt.
  - Fallback: adapter counters (psutil.net_io_counters) plus a saved boot time. If the PC did not restart, delta = PC usage.
- Credit it to "This PC" in the DB as `pcsite` = "(while NetScope was closed)". The site breakdown is unknown for that period.
- Cap the amount at the router catch-up value. The remainder goes to "Other devices / unknown", not to any specific device.
- Show it in the catch-up message: "X GB from this PC + Y GB from other devices."
- Test: simulate a gap with fake values and check that the PC total and the overall total are both correct.

### Addition from the user: show the remainder on the Mobile Device card
- Remainder = router total − this PC's measured usage (while open + from Windows for the closed periods).
- Keep it continuously, not only for closed periods, because the router gives no per-device counters. Store it in the DB as kind `rest`.
- Show it as a number next to "Mobile Device" with an "≈" sign and a tooltip: "Usage of devices other than this PC — most likely the phones."
- To be honest about it: if another device (Desktop / TV / Laptop) was online during the same period, add the remainder to "Other devices / shared" instead of putting it all on Mobile. Or show the label "Mobile + others".
- Router management traffic (TR069) is very small, but it ends up in the remainder too.

### User request: real per-device usage for each other device (second PC, TV…)
- Today the router gives the total only; other devices get site names only (from DNS), with no byte counts.
- Probes to add to the report in the next build (read only):
  - Statistics for each LAN port (Ethernet port stats). A wired device on its own port → its port counter = its usage.
  - Statistics for each Wi-Fi client (WLAN associated device / station info). Check whether it has bytes sent/received or only rates.
  - Any page with per-host counters (some Huawei firmware has "user device statistics").
- If found: show each device's usage next to it, and the remainder shrinks automatically.
- If not found (or for completeness): a future option is a light NetScope agent on the other PCs that reports to the main copy.

## New: Settings → Security section
**Condition from the user:** do not write any Security write code before studying a real router report.
- Step 1: add probes to the report for the admin-account page, the WLAN page and the SSID page (structure only, sanitized).
- Step 2: the user sends the report → I study the fields and how the change is sent → only then do I build the write actions with verify and rollback.
- Account-change order: keep the old credentials → apply the change → log in with the new ones → if that fails, log in with the old ones and roll back → save the new ones only after success.
1. **Change the router admin login: username + password** (the router settings page login).
   - Fields: new username, new password, confirm.
   - After the change, log in again with the new credentials to verify. Only then save them to the keyring under `profile:<id>`, replacing the old ones.
   - If the new login fails, keep the old credentials and try to roll back.
   - Some ISP firmware doesn't allow changing the username, or allows the password only. Detect this from the page and disable the field with an explanation.
   - Warning: anyone else using the router page will need the new credentials.
2. **Hide network toggle** (SSID broadcast on/off).
   - Hidden: the network doesn't appear in lists; you can join only by typing the name and password, or by scanning the QR.
   - The toggle can show the network again.
3. **Wi-Fi QR code** shown on the settings page.
   - Format: `WIFI:T:WPA;S:<ssid>;P:<pass>;H:true;;` (`H:true` when the network is hidden).
   - **Download PNG** button saves it to the Desktop.

### Technical notes
- Follow the same safety rules as any router change:
  - explicit consent;
  - saved backup;
  - read back to verify;
  - rollback on failure;
  - Undo.
- Changing the Wi-Fi password disconnects every device, including this PC if it is on Wi-Fi. Show a clear warning, and show the new QR right after the change.
- Huawei: find the WLAN fields (`WLANConfiguration.N`: SSIDAdvertisementEnabled, PreSharedKey/KeyPassphrase).
  - Handle 2.4 GHz and 5 GHz (several SSIDs).
  - Add a structure probe to the diagnostics report first, before writing anything.
- The password may come back masked from the router. If it is masked, ask the user to enter it once for the QR, and keep it in the keyring only.
- QR library: add `qrcode` (or `segno`) to requirements and test it in CI.
- verified.py: add the new capabilities only after a real test on DN8245V-56.

## Vision (larger stages, after the current release) — from the user
1. **Advanced routers that report per-device usage**: when the backend has the per-device usage capability (`DEVICE_USAGE`), each device's usage appears next to it automatically (phone, TV…).
2. **Strict monitoring mode** (opt-in setting for the network owner; off by default for home use):
   - Devices must use NetScope DNS; encrypted DNS is not allowed on this network.
   - Network-side enforcement, without touching the devices:
     - Return NXDOMAIN for the canary domains `use-application-dns.net` (turns off Firefox DoH) and `mask.icloud.com` / `mask-h2.icloud.com` (turns off iCloud Private Relay).
     - A router rule blocking port 853 (DoT): Android "Automatic" falls back to normal DNS, and "Strict" loses its connection ("take it or leave it").
     - Block known DoH servers if the router supports IP filters.
   - Warning shown to the owner: in companies, employees must be told about monitoring in writing; the law differs between countries.
   - Limitation: DNS gives site names and the number of requests, not bytes per site.
3. **Exact GB per site per device** needs traffic visibility:
   - router counters + DNS → GB per device + sites ranked by activity (per-site GB is an estimate);
   - or a port-mirroring switch to the NetScope PC (Npcap sees all traffic) → exact numbers (company option);
   - or "gateway" mode (the PC routes traffic) — not recommended: single point of failure.
4. **Employees and departments**: link a device to a person name + department (Ahmed — Marketing). Group and rank by department.
5. **Monthly PDF report** (automatic at the end of each 30-day cycle from the start date + manual export):
   - total;
   - each section (Mobile / PC / TV…) → devices ordered by highest usage → under each device its sites from highest to lowest;
   - departments.
