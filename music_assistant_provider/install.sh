#!/bin/sh
set -eu

REPO="aaronh86/Beatbump"
REF="ma-provider"
RAW="https://raw.githubusercontent.com/${REPO}/${REF}/music_assistant_provider/beatbump"

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

# The MVP provider implementation is intentionally contained in __init__.py.
# setup_flow.py handles configuration and manifest.json describes the provider.
for file in __init__.py setup_flow.py manifest.json; do
  echo "Downloading ${file}..."
  fetch "${RAW}/${file}" "${PROVIDER_DIR}/${file}"
done

# Remove files from any earlier incomplete installer attempt.
rm -f "${PROVIDER_DIR}/constants.py" "${PROVIDER_DIR}/provider.py"

python -m compileall -q "${PROVIDER_DIR}"

echo
echo "Beatbump provider installed successfully."
echo "Restart the Music Assistant container, then go to:"
echo "Settings -> Music Sources -> Add a music source -> Beatbump"
echo
echo "NOTE: this modifies the running container filesystem. A Music Assistant image update/recreation may remove it."
