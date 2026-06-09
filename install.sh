#!/bin/bash
# install.sh — manual dependency installer (optional, filetransfer-wireless does this automatically)
set -e

echo "Installing FileTransfer Wireless dependencies..."

# Homebrew
if ! command -v brew &>/dev/null; then
  echo "Installing Homebrew..."
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
fi

# adb
if ! command -v adb &>/dev/null; then
  echo "Installing adb..."
  brew install android-platform-tools
else
  echo "adb already installed: $(adb version | head -1)"
fi

# Python 3
if ! command -v python3 &>/dev/null; then
  echo "Python 3 not found. Install from https://www.python.org/downloads/"
  exit 1
else
  echo "Python 3: $(python3 --version)"
fi

# qrcode library
python3 -m pip install --quiet "qrcode[svg]" && echo "qrcode library: ok"

echo ""
echo "All done! Start with:  ./filetransfer-wireless"
