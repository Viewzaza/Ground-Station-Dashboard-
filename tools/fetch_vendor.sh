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

CESIUM_VERSION="${CESIUM_VERSION:-1.145.0}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VENDOR="$ROOT/frontend/js/vendor"

mkdir -p "$VENDOR"

# --- CesiumJS -----------------------------------------------------------------
# The npm tarball carries a prebuilt Build/Cesium, which is exactly what a
# no-build frontend needs. Assets/ must come with it: that is where the bundled
# Natural Earth II imagery lives, and it is what lets the globe render with no
# Ion account and no network.
if [ -d "$VENDOR/cesium" ]; then
  echo "cesium already present, skipping (delete $VENDOR/cesium to refetch)"
else
  echo "fetching cesium $CESIUM_VERSION…"
  tmp="$(mktemp -d)"
  curl -sSL -o "$tmp/cesium.tgz" \
    "https://registry.npmjs.org/cesium/-/cesium-$CESIUM_VERSION.tgz"
  tar -xzf "$tmp/cesium.tgz" -C "$tmp" package/Build/Cesium
  mv "$tmp/package/Build/Cesium" "$VENDOR/cesium"
  rm -rf "$tmp"
  echo "cesium -> $VENDOR/cesium"
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
