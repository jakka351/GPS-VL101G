#!/bin/bash
# Setup script for Project Repatriate Navigation GUI on a Raspberry Pi.
# Tested on Raspberry Pi OS Bookworm (64-bit) on Pi 4 / Pi 5.
#
# Usage:  bash setup_pi.sh

set -e

echo "═══════════════════════════════════════════════════════════════"
echo "  Project Repatriate — Pi Setup"
echo "═══════════════════════════════════════════════════════════════"

if [ "$(id -u)" -eq 0 ]; then
    echo "Don't run this as root — it'll handle sudo where needed."
    exit 1
fi

# ── 1. System packages ──────────────────────────────────────────────────
echo ""
echo "[1/5] Installing system packages..."
sudo apt update
sudo apt install -y \
    python3 python3-pip python3-venv python3-tk \
    fonts-inter fonts-jetbrains-mono \
    bluez bluetooth libbluetooth-dev \
    git

# Allow the current user to talk to BlueZ without sudo
sudo usermod -aG bluetooth "$USER" || true

# ── 2. Enable serial UART (for GPS) ─────────────────────────────────────
echo ""
echo "[2/5] Configuring serial UART..."
echo "  Disabling serial login shell so /dev/serial0 is free for GPS."
sudo raspi-config nonint do_serial_hw 0     # enable serial port hardware
sudo raspi-config nonint do_serial_cons 1   # disable serial login console

# Some Pi models need this in /boot/firmware/config.txt to enable PL011 on GPIO14/15
if ! grep -q "^enable_uart=1" /boot/firmware/config.txt 2>/dev/null; then
    echo "  Adding 'enable_uart=1' to /boot/firmware/config.txt"
    echo "enable_uart=1" | sudo tee -a /boot/firmware/config.txt > /dev/null
fi

# Add user to dialout group so they can read serial without sudo
sudo usermod -aG dialout "$USER"

# ── 3. Python venv + dependencies ───────────────────────────────────────
echo ""
echo "[3/5] Creating Python virtual environment..."
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

python3 -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

# ── 4. Optional: kiosk autostart ────────────────────────────────────────
echo ""
echo "[4/5] (Optional) Install systemd service for auto-start on boot?"
read -p "  Install autostart service? [y/N] " yn
if [[ "$yn" =~ ^[Yy]$ ]]; then
    SERVICE_FILE=/etc/systemd/system/repatriate-gui.service
    sudo tee "$SERVICE_FILE" > /dev/null <<EOF
[Unit]
Description=Project Repatriate Navigation GUI
After=network.target

[Service]
Type=simple
User=$USER
Environment="DISPLAY=:0"
Environment="XAUTHORITY=/home/$USER/.Xauthority"
WorkingDirectory=$SCRIPT_DIR
ExecStart=$SCRIPT_DIR/.venv/bin/python3 $SCRIPT_DIR/main.py --fullscreen
Restart=on-failure
RestartSec=5

[Install]
WantedBy=graphical.target
EOF
    sudo systemctl daemon-reload
    sudo systemctl enable repatriate-gui.service
    echo "  Service installed. Will start on next reboot."
    echo "  Manual control: sudo systemctl {start|stop|restart} repatriate-gui"
fi

# ── 5. Done ─────────────────────────────────────────────────────────────
echo ""
echo "[5/5] Setup complete."
echo ""
echo "═══════════════════════════════════════════════════════════════"
echo "  Next steps:"
echo "═══════════════════════════════════════════════════════════════"
echo ""
echo "  1. REBOOT to apply UART + group changes:"
echo "       sudo reboot"
echo ""
echo "  2. After reboot, test the GPS connection alone:"
echo "       cd $SCRIPT_DIR"
echo "       source .venv/bin/activate"
echo "       python3 gps.py /dev/serial0 9600"
echo ""
echo "     You should see lines like:"
echo "       connected=True fix=True sats=8 lat=-31.95 lon=115.86 ..."
echo ""
echo "     If fix=False after 5 minutes outdoors, check:"
echo "       - GNSS antenna is connected to the device"
echo "       - Antenna has clear sky view"
echo "       - Try other baud rates: 38400, 57600, 115200"
echo ""
echo "  3. (Optional but recommended) Pre-cache map tiles for offline use."
echo "     Run this while the Pi has internet — covers the area you drive in:"
echo "       python3 tile_prefetch.py --center \"-31.95,115.86\" \\"
echo "                                 --radius-km 25 --zoom 12-17"
echo "     (replace coords with your home base; ~12,000 tiles, ~180 MB)"
echo ""
echo "  4. Launch the GUI:"
echo "       python3 main.py --fullscreen"
echo "     With BLE battery monitor:"
echo "       python3 main.py --fullscreen --battery-mac AA:BB:CC:DD:EE:FF"
echo "     Bench-test the battery card without the device:"
echo "       python3 main.py --battery-sim"
echo ""
echo "  5. Wiring reminder (only do this with the Pi POWERED OFF):"
echo "       VL101G Green (TX)  →  Pi GPIO 15 / pin 10  (RX)"
echo "       VL101G Blue  (RX)  →  Pi GPIO 14 / pin 8   (TX)"
echo "       Common ground."
echo ""
