#!/bin/bash
set -e

echo "Installing FileTransfer dependencies..."

# Install Homebrew if not present
if ! command -v brew &>/dev/null; then
  echo "Installing Homebrew..."
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
fi

# Install adb (Android Debug Bridge)
if ! command -v adb &>/dev/null; then
  echo "Installing adb..."
  brew install android-platform-tools
else
  echo "adb already installed: $(adb version | head -1)"
fi

# Check Python 3
if ! command -v python3 &>/dev/null; then
  echo "Python 3 not found. Install it from https://www.python.org/downloads/"
  exit 1
else
  echo "Python 3 found: $(python3 --version)"
fi

echo ""
echo "All done! Run the server with:"
echo "  python3 server.py [port]"
