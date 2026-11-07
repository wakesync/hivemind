#!/usr/bin/env bash
# Pin real values into tinfoil-config.yml (fallback regenerated automatically).
#   tinfoil/pin.sh api sha256:abc...                 # memory-api image digest
#   tinfoil/pin.sh qwen "<mpk value from wrap log>"  # Qwen models: block mpk
set -euo pipefail
cd "$(dirname "$0")"
what=${1:?api|qwen}; val=${2:?value}
case "$what" in
  api)  d=${val#sha256:}
        sed -i "s|hivemind@sha256:[A-Za-z0-9_]*|hivemind@sha256:$d|" tinfoil-config.yml ;;
  qwen) root=${val%%_*}
        sed -i -e "s|mpk: \"[^\"]*\"|mpk: \"$val\"|" \
               -e "s|/tinfoil/mpk/mpk-[A-Za-z0-9_]*|/tinfoil/mpk/mpk-$root|" tinfoil-config.yml ;;
  *) echo "usage: pin.sh api|qwen <value>" >&2; exit 2 ;;
esac
bash make-fallback.sh >/dev/null
grep -nE "hivemind@|mpk" tinfoil-config.yml
