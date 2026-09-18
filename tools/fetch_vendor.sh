#!/usr/bin/env sh
# Fetch the large vendored assets that are deliberately not committed.
#
# Run once after cloning:
#     sh tools/fetch_vendor.sh
#
# Everything lands under frontend/ and is then served from our own origin, so
# the dashboard never reaches a CDN or a NASA endpoint at runtime. That matters
# for a ground station that may sit on an isolated network.
#
# ONE of the two things fetched here is required and the other is not:
#
#   three.js  is required. Without it the 3D globe panel cannot render at all,
#             and says so in its heading.
#   imagery   is optional. Without it the globe draws its own surface from the
#             Natural Earth coastlines already committed in frontend/assets,
#             and says which surface it is using in its heading. This is a
#             supported state, not a broken one. See the note below.

set -eu

THREE_VERSION="${THREE_VERSION:-0.160.0}"
# 2048 | 4096 | 8192 — the equirectangular width of the Blue Marble map.
#
# 4096 by default. 2048 is already several times oversampled at the size this
# panel is normally drawn, but the globe can be zoomed and 2048 goes soft when
# it is; 4096 is still sharp there and is under MAX_TEXTURE_SIZE on every
# desktop GPU. 8192 is the rung that can exceed it — a software renderer stops
# at 8192 and some mobile GPUs at 4096 — and a texture past that ceiling is not
# a sharper globe, it is a failed upload. The frontend checks the ceiling and
# falls back to its drawn surface rather than showing a black sphere, but the
# way to not need that check is to not ask for 8192.
EARTH_PX="${EARTH_PX:-4096}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VENDOR="$ROOT/frontend/js/vendor"
ASSETS="$ROOT/frontend/assets"

mkdir -p "$VENDOR"

# --- three.js -----------------------------------------------------------------
# The globe used to be CesiumJS, which is a 23 MB download for a panel that
# shows one orbit and one marker — and if the fetch was ever skipped the panel
# rendered black with no hint why. three.js is 1.3 MB, ships an ES module the
# browser loads directly with no build step, and the globe still draws from the
# Natural Earth GeoJSON committed in frontend/assets whenever the imagery below
# is absent. Nothing is fetched at runtime either way.
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

# --- NASA Blue Marble, for the globe's surface --------------------------------
# LICENCE: NASA Blue Marble is a work of the U.S. Government and is in the
# public domain. No attribution is required; the frontend gives it anyway, in
# frontend/js/lib/globe-surface.js and in the globe panel's own tooltip.
#
# The idea of putting real imagery on this globe, the WMS request that gets it,
# and the axis-order trap below all come from SattrackSlop
# (https://github.com/ColaBear101/SattrackSlop, MIT), which fetches the same
# layers from GIBS in the browser. That part is not copied: this station may be
# on an isolated LAN, which is the same reason there are no webfonts and no CDN
# script tags anywhere in the frontend. The image is fetched once, here, and
# served locally forever after.
#
# NOT COMMITTED, and not required. This is the trap the Cesium era set: 23 MB
# fetched by a setup script, so a clone that skipped the script showed a black
# rectangle and nobody could tell why. The globe therefore renders correctly
# with this file absent — it paints its own coastline surface from
# frontend/assets/ne_110m_land.json — and its heading says which of the two
# surfaces is on screen, so the absence is visible instead of mysterious. If
# you are setting up a station with no route to the internet, skip this and the
# panel is still right.
#
# ON THE AXIS ORDER: WMS 1.3.0 with CRS=EPSG:4326 takes BBOX as lat,lon — not
# lon,lat, which is the 1.1.1 convention and the source of a great deal of
# silently transposed imagery. -90,-180,90,180 is the whole Earth, north up,
# dateline at both edges: exactly the equirectangular convention the sphere's
# UVs already use, so the map applies with no transform.
#
# ONE FILENAME, whatever is inside it. The resolution is not in the name on
# purpose: the frontend reads it off the decoded image and reports it, so there
# is no constant in a JS file that has to be kept in step with EARTH_PX here.
# .gitignore already covers frontend/assets/earth-*.jpg.
GIBS="https://gibs.earthdata.nasa.gov/wms/epsg4326/best/wms.cgi"
EARTH_LAYER="BlueMarble_ShadedRelief_Bathymetry"
EARTH_OUT="$ASSETS/earth-surface.jpg"

if [ -f "$EARTH_OUT" ]; then
  echo "globe imagery already present, skipping (delete $EARTH_OUT to refetch)"
else
  echo "fetching NASA Blue Marble at ${EARTH_PX}px…"
  # GetMap renders on demand rather than serving a tile, so this takes seconds,
  # not milliseconds — and more of them at 8192. It is a one-time cost.
  tmp="$(mktemp -d)"
  if curl -fsSL --max-time 300 -o "$tmp/earth.jpg" \
      "$GIBS?SERVICE=WMS&REQUEST=GetMap&VERSION=1.3.0&CRS=EPSG:4326\
&BBOX=-90,-180,90,180&STYLES=&FORMAT=image/jpeg\
&WIDTH=$EARTH_PX&HEIGHT=$((EARTH_PX / 2))&LAYERS=$EARTH_LAYER"; then
    # A WMS answers a bad request with a ServiceException document and an
    # HTTP 200, so -f does not catch it. Check for the JPEG magic instead: a
    # 4 KB XML file saved as earth-surface.jpg would load as nothing, and
    # "nothing" is the one outcome this whole arrangement exists to prevent.
    magic="$(head -c 2 "$tmp/earth.jpg" | od -An -tx1 | tr -d ' \n')"
    if [ "$magic" = "ffd8" ]; then
      mkdir -p "$ASSETS"
      mv "$tmp/earth.jpg" "$EARTH_OUT"
      echo "Blue Marble -> $EARTH_OUT ($EARTH_PX x $((EARTH_PX / 2)))"
    else
      echo "globe imagery: GIBS answered something that is not a JPEG — skipping."
      echo "  the globe will draw its own coastline surface; nothing else breaks."
      head -c 400 "$tmp/earth.jpg" || true
      echo
    fi
  else
    echo "globe imagery: could not reach NASA GIBS — skipping."
    echo "  the globe will draw its own coastline surface; nothing else breaks."
  fi
  rm -rf "$tmp"
fi

# If you want the sharpest possible Earth and have a machine with ImageMagick
# on it, the original beats what the WMS will hand back: GIBS downsamples to
# serve, and Visible Earth publishes the 2004-12 Blue Marble Next Generation
# with topography and bathymetry at
#   https://eoimages.gsfc.nasa.gov/images/imagerecords/73000/73909/world.topo.bathy.200412.3x5400x2700.jpg
# (2.4 MB, 5400x2700) and in 21600x10800 tiles beside it. Resample to a power
# of two and drop it at frontend/assets/earth-surface.jpg. The WMS path above is
# the default because it needs no image tooling and returns the exact size
# asked for.

# --- satellite.js -------------------------------------------------------------
# Committed, because it is 40 KB and the browser needs it on first paint. Listed
# here so the pinned version is documented in one place.
#   https://cdn.jsdelivr.net/npm/satellite.js@7.1.0/+esm
#
# Do not downgrade to v5 for a UMD build: its dopplerFactor returns the wrong
# sign depending on whether the satellite is approaching or receding.

# --- coastlines ---------------------------------------------------------------
# frontend/assets/ne_110m_land.json is Natural Earth 110m land (public domain),
# committed at ~230 KB. This is what the globe and the 2D map both draw from,
# and what the globe falls back to when the imagery above is not there.

echo "done"
