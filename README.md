# 📡 FileTransfer

A minimal local web server that lets you browse, download, and upload files between your **Mac** and **Android phone** over a USB-C cable — entirely through a browser. No apps, no cloud, no Wi-Fi required.

---

## Requirements

- macOS
- Python 3 (pre-installed on Mac)
- Android phone with USB Debugging enabled
- USB-C cable

---

## Quick Start

**1. Install all requirements (one time):**
```bash
brew install android-platform-tools
```

**2. Run the server:**
```bash
python3 server.py [port]
```
Replace `[port]` with any port number (e.g. `8080`, `9000`). Defaults to `8765` if omitted.

Then open **http://localhost:[port]** in your browser.

---

## Setup (one time)

### 1. Install adb
```bash
brew install android-platform-tools
```

### 2. Enable USB Debugging on your Android phone
- **Settings → About Phone → Version** → tap **Build Number** 7 times
- Go back → **Additional Settings → Developer Options → USB Debugging** → ON

### 3. Connect your phone
- Plug in the USB-C cable
- On your phone, tap **Allow** on the USB Debugging popup
- Select **File Transfer** mode in the USB notification

---

## Usage

```bash
# Start the server (shares your home folder by default)
python3 server.py

# Or use the launcher script
./start.sh

# Share a specific folder
./start.sh ~/Desktop

# Custom port
./start.sh ~/Desktop 9000
```

Open **http://localhost:8765** in your browser.

---

## Features

- **💻 Mac tab** — browse your Mac filesystem, upload files into any folder
- **📱 Android tab** — browse your phone's storage over USB via adb
- **⬇ Save button** — on every file and folder; saves directly to `~/Downloads`
  - Files download with a real-time progress bar
  - Folders are pulled and zipped automatically
- **🔍 Search** — searches across the entire phone storage recursively (type 2+ chars)
- **Upload** — send files from Mac into any Android folder, or vice versa
- **Disk space check** — verifies free space on Mac before starting any download

---

## How it works

```
Browser (localhost:8765)
       │
       ▼
  server.py  ──── Mac filesystem (direct read/write)
       │
       └── adb ── USB-C cable ── Android phone
```

The server runs entirely on your Mac. `adb` handles all communication with the phone over the USB cable. Nothing leaves your local machine.

---

## Why it can't be deployed to the cloud (Vercel, etc.)

This tool is **intentionally local-only**:
- `adb` must run on the same machine the phone is physically connected to
- Files are streamed directly from your Mac's disk
- No data is sent to any external server

To access it remotely over the internet, use [ngrok](https://ngrok.com/):
```bash
ngrok http 8765
```

---

## Stop the server

Press `Ctrl+C` in the terminal, or:
```bash
kill $(lsof -ti:8765)
```

---

## File structure

```
file-transfer/
├── server.py     # main server
├── start.sh      # launcher script
└── README.md     # this file
```
