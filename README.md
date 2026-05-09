# GPS-VL101G
> Repurposing a defective Chinese GPS vehicle tracker into a self-hosted,
> locally-controlled telemetry device that does not phone home to mainland
> China, does not accept commands from anyone with the SIM number, and
> cannot remotely disable the vehicle while in motion.

This project documents how to take a **VL101G** (or similar Jimi/Concox/Topstar
family GPS tracker) shipped with insecure factory defaults, neutralise its
dangerous behaviours, point its telemetry at your own Traccar server running
on a Raspberry Pi, and end up with a locked-down vehicle tracker that you
actually control.

**Status:** Working. SMS lockdown sequence verified. Traccar install verified.
Pilot device installed and reporting cleanly to a self-hosted backend.

---

## Why this project exists

The **VL101G** is a representative example of a class of low-cost Chinese GPS
trackers (Jimi IoT, Concox, Coban, MiCODUS, SinoTrack, and various unbranded
"VL" SKUs) that ship with defaults so insecure they are arguably hazardous.
This document records what's wrong with the factory device, why the
manufacturer's installation instructions should not be followed as written,
and what to do instead.

If you've bought one of these devices and you just installed it according to
the supplied manual, **you have a vehicle that:**

1. Phones home to a server in Guangdong, China by default
2. Accepts SMS commands from anyone who knows the SIM phone number, with no
   password required
3. Can be remotely commanded to cut the fuel pump while in motion, with no
   speed interlock, killing power steering and brake assist
4. Was wired according to a manual that omits required fuses on the
   high-current relay circuit
5. Forwards inbound SMS to a configurable third-party server
6. Will phone home a permanent identifier (IMEI) every 10 seconds for the
   life of the device, regardless of who owns it

This project's goal is to eliminate every one of those properties.

---

## Why the factory installation is dangerous

A non-exhaustive list of issues with the **VL101G_Manual_2024.04.29** manual
and the device's factory configuration. All findings verified against the
manual text and reproduced via SMS query of a physical device.

### 1. Default password is `666666`

The factory SMS command password is `666666`. The manual itself documents
this in the `PASSWORD` command example. Six identical digits is the first
brute-force candidate any attacker tries. The factory ships every device
with the same default.

### 2. Password protection is OFF by default (`PWDSW:OFF`)

The device ships with the SMS command authentication gate disabled entirely.
**Anyone who knows the SIM phone number can issue commands without
authentication.** Combined with the next point, this means anyone with the
phone number can remotely cut the vehicle's fuel pump.

### 3. The SMS command `RELAY,1#` cuts the fuel pump

One SMS, no two-factor, no in-motion check, no rate limit. The default state
makes this command available to any sender. Documented in the manual, page 13:

> `RELAY,1#` → response: `Cut off the fuel supply: Success!`

There is no software interlock for vehicle speed. There is no warning to the
driver that this command was received. The driver simply experiences sudden
engine stall.

### 4. Default server is in mainland China

`CHECK#` on a factory device returns:

```
SERVER:1,test.topstargps.com,11139
GET IP:120.234.211.126                ← Guangdong, China Telecom
APN:CMnet                             ← China Mobile internet APN
IMSI:460044...                        ← MCC 460 = China
```

By default, every position fix, every speed event, every harsh-brake alert,
every ignition transition for the life of the vehicle is transmitted to
infrastructure in mainland China.

### 5. The wiring diagrams contain errors

The manual's wiring diagrams (pages 7-8) contain at least eleven specific
documentation errors. The most dangerous:

- A "white line" referenced in the relay-wiring text that does not exist on
  the device's harness. The device has six wires (Red, Black, Orange, Yellow,
  Blue, Green). There is no white wire.
- The same colour name "green" is used in the same document to mean two
  different wires (the device's TTL TX line, and the relay's high-current
  contact wires). Confusing these can route TTL signals onto fuel-pump
  current.
- **No fuse specified on the relay's high-current contact side.** A vehicle
  fuel pump pulls 5–15 amps continuously; the diagram routes that through
  the relay contacts and into a wire labelled "cut here" with the user
  expected to splice it in, with no inline fuse. If the relay welds, fails
  short, or the wire chafes against a body panel, an unfused circuit can
  ignite.
- Pin 87 is missing from the relay diagram (only 86, 30, 87a, 85 shown).
- The instruction to "cut into the side closing to starter motor" can lead
  an installer to splice into the starter solenoid trigger wire, where
  inrush current will weld a fuel-pump-rated relay contact on the first
  crank attempt.

### 6. SMS forwarding (`FW`) and SMS transparent transmission (`SMSTC`)

The device can be configured to:
- Send arbitrary SMS to any number under its own SIM (`FW` command — useful
  for SMS-laundering / anonymising messages)
- Forward all inbound SMS to a configurable server (`SMSTC` command — turns
  the device into an SMS interception primitive)

Combined with the `SERVER` command (which can redirect the entire device to
an attacker-controlled endpoint), and the absence of authentication by
default, the factory state allows anyone with the SIM number to exfiltrate
SMS, redirect the tracker, and/or use the SIM as an anonymous SMS relay.

### 7. The `FACTORY#` command resets all configuration

Anyone with command access can wipe any password / server / SOS-list
configuration the legitimate owner has set, returning the device to the
default-attackable state. There is no audit log. The legitimate owner sees
the device "go offline briefly" and never knows.

### 8. No declared regulatory certification

The manual does not declare an FCC ID, CE mark, RCM (Australia), or IC
(Canada) marking. Sale of an unmarked cellular device in any of those
jurisdictions is a regulatory violation independent of the safety findings
above.

---

## Part 1: Securing the device (SMS lockdown)

This section assumes you have a physical VL101G with a working SIM (ideally
an Australian IoT SIM — see notes on SIM swap below), and that you have
identified the device's SIM phone number.

> **Do this with the device on a bench, GNSS antenna disconnected.** Don't
> install in a vehicle until lockdown is complete and verified. With the GNSS
> antenna disconnected during initial setup, the device cannot transmit a
> position fix to its default Chinese server during the configuration window.

### Recommended SIM swap

If the device shipped with the original Chinese SIM, swap it for an
Australian IoT SIM (Telstra IoT, Optus IoT, or a multi-network IoT SIM such
as Hologram or Soracom). Reasons:

- Chinese-issued SIMs route data through Chinese telecom infrastructure
- The original SIM's phone number is known to the manufacturer
- Telstra/Optus IoT plans support the standard `telstra.internet` /
  `connect` APNs without WAP-gateway restrictions

Update the device's APN setting after the swap (see step below).

### Lockdown sequence

Send each of the following SMS commands from your management mobile number
to the device's SIM number, in this order. Wait for the device's response
to each before sending the next.

```
 1. STATUS#                        ← baseline check, confirm device responds
 2. CHECK#                         ← record factory state for your records
 3. CENTER,A,<your-mobile>#        ← register YOUR number as the centre
 4. SOS,A,<your-mobile>,,#         ← add YOUR number to SOS list
 5. SOSPERMIT,0,1#                 ← only SOS-listed numbers can issue cmds
 6. SERVER,0,<your-server-ip>,5023,0#   ← redirect telemetry to your server
 7. APN,<your-AU-APN>,,,#          ← set the APN for your SIM
 8. GMT,E,10,0#                    ← AEST timezone
 9. RELAY,0#                       ← disable relay control in firmware
10. SMSTC,0#                       ← disable SMS forwarding to remote server
11. PERMIT,1#                      ← enable permission scheme
12. CHECK#                         ← verify all settings before lockdown
13. PASSWORD,666666,<NEW-6-DIGITS>#  ← change password from default
14. PWDSW,ON#                      ← enable password gate
```

Then verify the auth syntax works on your firmware variant **before** you
rely on it:

```
15. <NEW-6-DIGITS>,STATUS#         ← argless command with password
16. RELAY,0,<NEW-6-DIGITS>#        ← arged command with password
```

Both should return real responses, not `password error`. If only one form
works, that's your auth syntax going forward. If neither works, immediately
disable the gate via `<NEW-6-DIGITS>,PWDSW,OFF#` and try alternate password
positions (`COMMAND,<password>,args#` or `<password>,COMMAND,args#`) until
you find what your firmware variant accepts.

### Why this order matters

- `CENTER` and `SOS` are set **before** `PWDSW,ON` so that there is a
  privileged-sender escape hatch if the password gate misbehaves.
- All configuration that takes parameters (`SERVER`, `APN`, `GMT`, `RELAY`,
  `SMSTC`, `PERMIT`) is sent **before** the password gate is enabled, so the
  syntax is simple and you don't get locked out partway through.
- `PASSWORD` change is second-to-last, immediately before enabling the gate.
- The very last step is a verification that the gate works for both argless
  and arged commands, so any syntax surprises are caught immediately rather
  than discovered later in production.

### Recovery if locked out

If you enable PWDSW and cannot get any auth syntax to work:

1. Hardware factory reset — most VL series have a recessed pinhole near the
   SIM tray; hold for 10+ seconds while powered on.
2. If no pinhole, try power-on with the SOS button held (if present).
3. Last resort: pull battery + SIM, wait 60+ seconds, reinsert.

After factory reset, the password reverts to `666666` and PWDSW reverts to
OFF. Redo the lockdown.

### Physical install — relay wire handling

**Do not connect the yellow wire (relay control) to anything.** Trim it
short, heat-shrink the end, tie it back into the harness. The relay itself
should not be installed at all. With no relay present, even if an attacker
breaks your auth and sends `RELAY,1#`, there is no physical relay to
actuate — the immobilizer attack surface is removed at the hardware level.

If you bought the device specifically because you wanted the immobilizer
(e.g., for fleet repossession), this entire project is not the right fit
for you. The reasons not to use that feature are documented in the "dangers"
section above.

---

## Part 2: Installing Traccar on a Raspberry Pi

### Hardware

- Raspberry Pi 3B+, 4, or 5 (any model with at least 1GB RAM works; 2GB+
  recommended)
- 16GB+ microSD card or USB SSD (SSD strongly recommended for log volume)
- Ethernet cable (Wi-Fi works but wired is more reliable for an always-on
  service)
- Reliable power supply (official Pi PSU; under-voltage causes Traccar to
  drop connections silently)

### Initial Pi setup

Flash Raspberry Pi OS Lite (64-bit) using `rpi-imager`. Configure SSH,
hostname, Wi-Fi (if needed), and a strong user password during the imaging
step.

After first boot:

```bash
ssh <user>@raspberrypi.local

# Update everything
sudo apt update
sudo apt full-upgrade -y
sudo apt install -y curl unzip wget

# Set a static LAN IP via DHCP reservation in your router admin, OR
# configure a static IP in /etc/dhcpcd.conf if you prefer. Required so
# that Traccar's LAN address doesn't change after a reboot and break your
# router's port-forward rule.
```

### Install Traccar

Traccar's official ARM64 installer:

```bash
cd /tmp
wget https://www.traccar.org/download/traccar-linux-arm-64-latest.zip
unzip traccar-linux-arm-64-latest.zip
sudo ./traccar.run
```

The installer creates `/opt/traccar/`, installs a `systemd` service called
`traccar`, and starts it. Default config is in `/opt/traccar/conf/traccar.xml`.

### Verify Traccar is running

```bash
sudo systemctl status traccar
```

You want to see `active (running)`. Also check what it's listening on:

```bash
sudo ss -tlnp | grep java | head -20
```

You should see ~100 lines, one per protocol Traccar supports. The two
relevant to this project:

- `LISTEN 0 50 *:8082 ...` ← web UI (HTTP/HTML for browsers)
- `LISTEN 0 50 *:5023 ...` ← GT06 protocol (TCP binary for the GPS device)

If both are present and bound to `*` (all interfaces) rather than
`127.0.0.1`, you're good.

### First login

In a browser on the same LAN, navigate to `http://<pi-lan-ip>:8082`.

Default credentials: `admin / admin`. **Change this immediately** in
`Settings → Account`. Use a strong password — this account has full
administrative control of the tracking server, and once you expose port
8082 to the internet (or proxy it via Cloudflare Tunnel / Tailscale),
brute-force attempts will start within hours.

### Add the device record

In the Traccar UI:

1. Click the `+` button in the Devices panel (left sidebar)
2. Identifier: paste the device's IMEI exactly as returned by `CHECK#`
   (15 digits, e.g. `1234567890987654321`)
3. Name: whatever you want to see in the map
4. Save

The device record now exists. Traccar will accept connections from this IMEI
when the device dials in. Until then, the device shows as offline.

### Watch the connection log

While the GPS device is being configured to dial in, leave this running on
the Pi:

```bash
sudo tail -f /opt/traccar/logs/tracker-server.log | grep -i 'gt06\|<your-IMEI>'
```

When the device connects, you'll see lines like:

```
INFO: [Txxxxxxxx: gt06 < <device-cellular-ip>] HEX: 78 78 0d 01 ...
INFO: [Txxxxxxxx: gt06] id: 123567890987654321
```

The `id:` line confirms the device's IMEI matches the one in the device
record. The device should appear "online" in the Traccar UI within seconds.

If the IMEI in the log doesn't match the one you registered, Traccar logs
`unknown device` — copy the actual IMEI from the log line into the device
record and retry.

---

## Part 3: Network architecture — making the device reach your Pi

The GPS device dials outbound from a cellular network to a public IP+port.
Your Pi is on your home LAN. For the device to phone home to Traccar, the
TCP connection has to traverse the internet, hit your home network, and
land on the Pi's port 5023. There are three architectural options. Pick one
based on what your ISP gives you.

### Step 1 — Check whether you're behind CGNAT

This is the question that determines which architecture is available to you.
On the Pi:

```bash
curl -4 ifconfig.me
```

This gives you the IP the public internet sees as your home connection.
Now log into your modem admin and check the WAN IP it shows on the status
page. Three possibilities:

| `curl ifconfig.me` shows | Modem WAN status shows | Verdict |
|---|---|---|
| `203.x.y.z` (or any normal-looking IP) | Same `203.x.y.z` | **Real public IP.** Port forwarding works. |
| `203.x.y.z` | `100.64.0.5` (or anything `100.64.x.x` – `100.127.x.x`) | **You are behind CGNAT.** Port forwarding will not work. |
| Two different "real" looking IPs | — | Double-NAT (rare). Investigate router topology. |

Telstra residential 4G/NBN connections in Australia are usually real public
IPs. Optus, TPG, and some Aussie Broadband plans are commonly behind CGNAT.
Most 4G/5G home wireless plans across all carriers are CGNAT.

### Option A — Real public IP + port forward (works directly)

Requires: real public IP at home (verified above).

Pros: simplest, lowest latency, no third-party services.
Cons: home IP is dynamic on residential plans (use DDNS — see below);
exposes Pi to the internet (use Cloudflare or VPS in front for production).

This is the setup the rest of this section assumes.

### Option B — VPS relay (works always, even behind CGNAT)

Requires: a $5/month VPS with a static public IP (Hetzner, Linode, Vultr,
Oracle Cloud free tier).

Architecture:

```
[GPS device] ──cellular──> [VPS public IP:5023] ──TCP relay──> [Pi:5023]
                                                                   │
[Your laptop/phone] ───────Tailscale to Pi───────────────────────> [Pi:8082 web UI]
```

The VPS does dumb TCP forwarding from its public 5023 to the Pi's 5023 over
a Tailscale link. The web UI on 8082 stays private and you reach it via
Tailscale from anywhere, never exposing it to the public internet.

`socat` config on the VPS, one line:

```bash
sudo apt install socat
socat TCP-LISTEN:5023,fork,reuseaddr TCP:<pi-tailscale-name>:5023
```

Run under `systemd` so it persists across VPS reboots.

### Option C — Move Traccar entirely onto the VPS

Same $5/month, runs Traccar directly on the VPS, no Pi involved in the data
path. Simplest, most reliable for production. Use this if you want
set-and-forget operation. The Pi can become a spare or do other things.

### Recommendation

For a personal/hobby tracker on a real public IP at home: **Option A** with
DDNS and Tailscale for the web UI.

For anything you want to keep working when your home internet goes down:
**Option C**. The Pi-at-home model is going to give you maybe 99% uptime;
a $5/month VPS gives you 99.95%+ and is genuinely set-and-forget.

---

## Part 4: Port forwarding (Option A only — skip if using VPS relay)

The Pi needs to be reachable from the internet on **port 5023 TCP** for the
device to phone home, and (optionally) on **port 8082 TCP** if you also want
to access the Traccar web UI directly from outside without using Tailscale
or Cloudflare Tunnel.

> **Recommended:** forward only **5023** for the device. Use **Tailscale**
> (next section) for your own access to the web UI. This keeps the web UI
> off the public internet entirely, which significantly reduces the
> brute-force-attack surface.

### Find the Pi's LAN IP and MAC

On the Pi:

```bash
hostname -I            # → e.g., 192.168.0.42
ip -br link show       # → MAC of the active interface (eth0 or wlan0)
```

Note both. You'll need them for the port-forward rule and for the DHCP
reservation.

### Set a DHCP reservation in the modem

Critical step that's often skipped. Without it, your Pi can get a new LAN
IP after a reboot, and the port-forward rule will silently break.

In the modem admin, find the DHCP / Address Reservation section. Look up
the Pi by its MAC (or by its hostname `raspberrypi`). Reserve its current
LAN IP. Now every future DHCP request from the Pi gets the same IP back.

### Add the port-forward rule

The form fields on a Telstra Smart Modem (or most ISP-supplied modems)
will be:

| Field | Value |
|---|---|
| Name | `traccar-gt06` |
| Protocol | `TCP` |
| WAN port | `5023` |
| LAN port | `5023` |
| Destination IP | *Pi's LAN IP* (the reserved one above) |
| Destination MAC | *Pi's MAC* |

Save. **Reboot the modem** — Telstra modems often refuse to apply new
forward rules until reboot. Power off 30 seconds, on, wait 2 minutes for
re-sync.

If the modem has a separate "Firewall Security Level" setting (Telstra
Smart Modem 3 does), set it to `Low` or `Custom`. The default `Medium`
sometimes overrides explicit forward rules.

### Verify the forward works (from outside your network)

**Do not test from your home network.** NAT loopback on most home modems
either silently drops or returns RST when you try to reach your own public
IP from inside the LAN. The internal test will lie to you.

From a phone with **Wi-Fi off and mobile data on**, browse to:

```
http://<your-public-ip>:5023
```

You'll see "connection closed" or similar — that's correct. Port 5023
speaks GT06 binary, not HTTP. The fact that you get *something* (not a
connection timeout) means the path is open and Traccar's listener is
responding. That's the test.

Or use an external port checker: https://www.yougetsignal.com/tools/open-ports/.
Enter your public IP and 5023, hit check, look for **open**. The site scans
from their server, not yours, so it's a real external test.

If the port shows **closed** from outside:
- Re-check the modem rule's destination IP matches `hostname -I` exactly
- Reboot the modem again
- Check the modem's firewall security level
- Check `sudo ss -tlnp | grep 5023` on the Pi to confirm Traccar is actually
  listening (`*:5023` good, `127.0.0.1:5023` bad)

---

## Part 5: Tailscale for personal access to the web UI

Recommended way for *you* to access the Traccar web UI on port 8082 from
anywhere — phone, laptop, friend's house, airport — without exposing port
8082 to the public internet at all.

### What Tailscale does

Builds a peer-to-peer encrypted mesh between your devices. Each device gets
a private IP in the `100.x.y.z` range that only your devices can route to.
No public ports opened. Works behind CGNAT. Works through firewalls. Free
for up to 100 devices and 3 users.

### Install on the Pi

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
```

Click the URL it prints. Log in with Google/GitHub/email. The Pi now has a
Tailscale identity.

Find its Tailscale IP:

```bash
tailscale ip -4
# → 100.x.y.z
```

### Install on your laptop / phone

Get the Tailscale app from the App Store, Play Store, or
https://tailscale.com/download. Log in with the same account.

Now from anywhere — any network, any country, mobile data, hotel Wi-Fi —
browse to:

```
http://100.x.y.z:8082
```

Or, with MagicDNS enabled in the Tailscale admin (recommended):

```
http://raspberrypi:8082
```

That's it. The Traccar web UI loads. No port forward. No public IP. No
DDNS. Nothing exposed to the internet.

### Optional polish

- **MagicDNS** — toggle in the Tailscale admin console. Replaces IP
  addresses with hostnames (`raspberrypi` instead of `100.x.y.z`).
- **Tailscale SSH** — `sudo tailscale up --ssh` replaces SSH key management
  with Tailscale identity. Cleaner than password auth + `fail2ban`.
- **Funnel** — if you want to share a specific URL with someone who isn't
  on your Tailnet (e.g., showing the Traccar UI to a teammate), Tailscale
  Funnel exposes `https://raspberrypi.<tailnet>.ts.net` publicly with auto
  HTTPS. Free.

---

### Restore

Stop Traccar, replace the file, start Traccar:

```bash
sudo systemctl stop traccar
sudo cp /opt/traccar/backups/database-<date>.mv.db /opt/traccar/data/database.mv.db
sudo chown traccar:traccar /opt/traccar/data/database.mv.db
sudo systemctl start traccar
```

---

## Hardening notes (brief)

The above gets you a working tracker. For long-term unattended operation,
also do:

- **Unattended security upgrades:**
  ```bash
  sudo apt install -y unattended-upgrades
  sudo dpkg-reconfigure -plow unattended-upgrades
  ```
- **`fail2ban`** on SSH if you have password auth enabled at all
  (better: disable password auth, use Tailscale SSH or key-based SSH only)
- **`logrotate`** on Traccar logs — `/opt/traccar/logs/` grows indefinitely
  by default
- **UPS** for the Pi — even a cheap mini-UPS like a USB-C battery hat. Pi
  SD cards corrupt on hard power loss.
- **Monitoring** — at minimum, a cron that checks `systemctl is-active
  traccar` and pings you on Pushover/ntfy/Discord webhook if it's down.
  Otherwise you find out the tracker stopped working a month later when
  you go looking for a position.

---

## Disclaimer

This project documents how to repurpose a device you legally own. It does
not advise modifying devices you don't own, intercepting third-party
communications, or operating cellular equipment in jurisdictions where it
is not lawfully marked.

The factory device is described as it is, based on the published manufacturer
manual (`VL101G_Manual_2024.04.29.pdf`) and SMS-query responses from a
physical device. Findings are verifiable; readers are encouraged to verify
independently rather than take this document on trust.

---

## License

MIT. Reuse, adapt, redistribute. If this saved you from a stalker tracker
or a fleet immobilizer hazard, that's the point.
