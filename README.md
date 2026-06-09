# 📡 FileTransfer Wireless

Transfer files between your **Mac and Android phone over Wi-Fi** — entirely through a browser. No USB cable, no app to install, no cloud, no internet required. Both devices just need to be on the same Wi-Fi network.

---

## Quick Start

```bash
./filetransfer-wireless
```

That's it. On first run it installs everything it needs (adb, qrcode library), then starts the server and opens the browser automatically.

---

## Requirements

- macOS 10.15+
- Python 3 (pre-installed on Mac)
- Android phone on the **same Wi-Fi network**

Everything else (`adb`, Python libraries) is installed automatically on first run.

---

## First-time Phone Setup (one time only)

**1. Enable Developer Options on your phone:**
- Settings → About Phone → tap **Build Number** 7 times

**2. Enable Wireless Debugging:**
- Settings → Developer Options → **Wireless Debugging** → ON

**3. Pair with your Mac:**
- In the Android tab on your Mac, scan the QR code with your phone
- On your phone: Wireless Debugging → **Pair device with pairing code**
- Enter the port and 6-digit code shown → tap **Pair & Connect**

This pairing is **permanent** — you never need to do it again for the same Mac.

---

## Daily Use (after first-time setup)

1. Run `./filetransfer-wireless` on your Mac
2. On your phone: turn on **Wireless Debugging**
3. Scan the QR code in the Android tab → connected

---

## Features

- **💻 Mac tab** — browse your Mac filesystem, upload files into any folder
- **📱 Android tab** — browse your phone's storage wirelessly via adb over Wi-Fi
- **⬇ Save** — download any file or folder to `~/Downloads` with a real-time progress bar
- **Folders** — pulled and zipped automatically
- **🔍 Search** — searches entire phone storage recursively (type 2+ chars)
- **Upload** — send files from Mac → Android or Android → Mac
- **Disk space check** — verifies free space before every download
- **QR connect** — scan once to connect, no typing IPs

---

## Usage

```bash
# Start (default port 8765)
./filetransfer-wireless

# Custom port
./filetransfer-wireless 9000

# Stop
Ctrl+C
# or
kill $(lsof -ti:8765)
```

---

## How it works

```
Browser (any device on the network)
        │
        ▼
   server.py  ──── Mac filesystem (direct read/write)
        │
        └── adb (Wi-Fi) ──── Android phone
```

The server runs entirely on your Mac. `adb` communicates with the phone over Wi-Fi using Android's Wireless Debugging. Nothing leaves your local network.

---

## File structure

```
file-transfer-wireless/
├── filetransfer-wireless   # single executable — run this
├── server.py               # HTTP server
├── start.sh                # alias for filetransfer-wireless
├── install.sh              # manual dependency installer (optional)
└── README.md               # this file
```
