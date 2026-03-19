# Full Installation Guide — plug-and-sdr

Tested on Ubuntu 22.04 and Debian 12.

---

## 1. System packages

```bash
sudo apt update
sudo apt install -y rtl-sdr dump1090-mutability multimon-ng direwolf ffmpeg python3-pip
```

## 2. Python dependencies

```bash
pip install -r requirements.txt
```

---

## 3. Build rtl_433 from source

rtl_433 decodes ISM-band sensors (weather stations, tire sensors, etc.).

```bash
sudo apt install -y cmake librtlsdr-dev
git clone https://github.com/merbanan/rtl_433.git
cd rtl_433
mkdir build && cd build
cmake .. -DCMAKE_INSTALL_PREFIX=/usr/local
make -j$(nproc)
sudo make install
cd ../..
```

## 4. Build rtl_ais from source

rtl_ais decodes AIS ship tracking messages.

```bash
sudo apt install -y librtlsdr-dev
git clone https://github.com/dgiardini/rtl-ais.git
cd rtl-ais
make
sudo cp rtl_ais /usr/local/bin/
cd ..
```

## 5. Blacklist DVB kernel module

Prevents the kernel from claiming the dongle as a TV tuner.

```bash
echo 'blacklist dvb_usb_rtl28xxu' | sudo tee /etc/modprobe.d/rtlsdr.conf
sudo rmmod dvb_usb_rtl28xxu 2>/dev/null || true
```

## 6. udev rules (run without sudo)

```bash
sudo tee /etc/udev/rules.d/20-rtlsdr.rules <<'EOF'
SUBSYSTEM=="usb", ATTRS{idVendor}=="0bda", ATTRS{idProduct}=="2838", GROUP="plugdev", MODE="0666"
SUBSYSTEM=="usb", ATTRS{idVendor}=="0bda", ATTRS{idProduct}=="2832", GROUP="plugdev", MODE="0666"
EOF
sudo udevadm control --reload-rules
sudo udevadm trigger
sudo usermod -aG plugdev $USER
```

Log out and back in for the group change to take effect.

## 7. Configure

```bash
cp config.yaml config.yaml.bak
nano config.yaml   # Set site_lat, site_lon, site_name
```

## 8. Test run

```bash
python3 server.py
# Open http://localhost:8888
```

---

## 9. Run as a systemd service

Create `/etc/systemd/system/sdr-dashboard.service`:

```ini
[Unit]
Description=plug-and-sdr web dashboard
After=network.target

[Service]
Type=simple
User=YOUR_USERNAME
WorkingDirectory=/path/to/plug-and-sdr
ExecStart=/usr/bin/python3 server.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable sdr-dashboard
sudo systemctl start sdr-dashboard
sudo systemctl status sdr-dashboard
```

View logs:

```bash
journalctl -u sdr-dashboard -f
```
