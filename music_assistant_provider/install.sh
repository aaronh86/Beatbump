#!/bin/sh
set -eu

REPO="aaronh86/Beatbump"
REF="feature/music-assistant-provider"
RAW="https://raw.githubusercontent.com/${REPO}/${REF}/music_assistant_provider/beatbump"

# Locate the installed Music Assistant Python package in the current container.
MA_DIR="$(python - <<'PY'
import os
import music_assistant
print(os.path.dirname(music_assistant.__file__))
PY
)"

PROVIDER_DIR="${MA_DIR}/providers/beatbump"
echo "Music Assistant package: ${MA_DIR}"
echo "Installing Beatbump provider to: ${PROVIDER_DIR}"
mkdir -p "${PROVIDER_DIR}"

fetch() {
  src="$1"
  dst="$2"
  if command -v curl >/dev/null 2>&1; then
    curl -fL --retry 3 --connect-timeout 10 "$src" -o "$dst"
  elif command -v wget >/dev/null 2>&1; then
    wget -O "$dst" "$src"
  else
    echo "ERROR: curl or wget is required." >&2
    exit 1
  fi
}

for file in __init__.py constants.py provider.py setup_flow.py manifest.json; do
  echo "Downloading ${file}..."
  fetch "${RAW}/${file}" "${PROVIDER_DIR}/${file}"
done

python -m compileall -q "${PROVIDER_DIR}"

echo
echo "Beatbump provider installed successfully."
echo "Restart the Music Assistant container, then go to:"
echo "Settings -> Music Sources -> Add a music source -> Beatbump"
echo
echo "NOTE: this modifies the running container filesystem. A Music Assistant image update/recreation may remove it; rerun this installer after an update until the provider is packaged upstream or bind-mounted persistently."
