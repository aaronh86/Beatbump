#!/bin/sh
set -eu

REPO="aaronh86/Beatbump"
REF="ma-provider"
RAW="https://raw.githubusercontent.com/${REPO}/${REF}/music_assistant_provider/beatbump"
LOGO_RAW="https://raw.githubusercontent.com/${REPO}/main/app/static/logo.svg"

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

for file in __init__.py setup_flow.py manifest.json; do
  echo "Downloading ${file}..."
  fetch "${RAW}/${file}" "${PROVIDER_DIR}/${file}"
done

# Music Assistant natively supports icon.svg in the provider directory.
# Reuse Beatbump's own logo asset rather than maintaining a duplicate copy.
echo "Downloading Beatbump icon..."
fetch "${LOGO_RAW}" "${PROVIDER_DIR}/icon.svg"

rm -f "${PROVIDER_DIR}/constants.py" "${PROVIDER_DIR}/provider.py"
python -m compileall -q "${PROVIDER_DIR}"

echo
echo "Beatbump provider installed successfully."
echo "Restart Music Assistant, then add/configure Beatbump under Music Sources."
echo "NOTE: for TrueNAS, use a persistent host-path mount for this provider directory."
