#!/usr/bin/env sh
# Fetch the large vendored assets that are deliberately not committed.
#
# Run once after cloning:
#     sh tools/fetch_vendor.sh
#
# Everything lands under frontend/js/vendor/ and is then served locally, so the
# dashboard never reaches a CDN at runtime. That matters for a ground station
# that may sit on an isolated network.

set -eu

THREE_VERSION="${THREE_VERSION:-0.160.0}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VENDOR="$ROOT/frontend/js/vendor"

mkdir -p "$VENDOR"

# --- three.js -----------------------------------------------------------------
# The globe used to be CesiumJS, which is a 23 MB download for a panel that
# shows one orbit and one marker — and if the fetch was ever skipped the panel
# rendered black with no hint why. three.js is 1.3 MB, ships an ES module the
# browser loads directly with no build step, and the globe is drawn from the
# Natural Earth GeoJSON already committed in frontend/assets, so there is no
# imagery to download and nothing is fetched at runtime.
if [ -f "$VENDOR/three/three.module.js" ]; then
  echo "three.js already present, skipping (delete $VENDOR/three to refetch)"
else
  echo "fetching three.js $THREE_VERSION…"
  tmp="$(mktemp -d)"
  curl -sSL -o "$tmp/three.tgz" \
    "https://registry.npmjs.org/three/-/three-$THREE_VERSION.tgz"
  tar -xzf "$tmp/three.tgz" -C "$tmp" package/build/three.module.js package/LICENSE
  mkdir -p "$VENDOR/three"
  mv "$tmp/package/build/three.module.js" "$VENDOR/three/three.module.js"
  mv "$tmp/package/LICENSE" "$VENDOR/three/LICENSE"      # MIT; keep it with the code
  rm -rf "$tmp"
  echo "three.js -> $VENDOR/three"
fi

# --- satellite.js -------------------------------------------------------------
# Committed, because it is 40 KB and the browser needs it on first paint. Listed
# here so the pinned version is documented in one place.
#   https://cdn.jsdelivr.net/npm/satellite.js@7.1.0/+esm
#
# Do not downgrade to v5 for a UMD build: its dopplerFactor returns the wrong
# sign depending on whether the satellite is approaching or receding.

# --- coastlines ---------------------------------------------------------------
# frontend/assets/ne_110m_land.json is Natural Earth 110m land (public domain),
# committed at ~230 KB.

echo "done"
