#!/usr/bin/env bash
# Fetch the satellite/transmitter/TLE fixtures the stub API serves.
#
# These are satnogs-auto-scheduler's OWN test fixtures, at the exact commit
# backend/requirements.txt pins. They are deliberately NOT committed here.
# That project is AGPL-3.0 and this repository is MIT, and the whole reason
# the scheduler is invoked as a separate process is so that none of its
# material lives in this tree - see the README's Licence section. Copying
# 1.5 MB of its files in through the back door, as test data, would make that
# claim false.
#
# So: fetched on demand, into a gitignored directory, by anyone re-capturing
# the transcript.
#
# Usage:  ./fetch_upstream_fixtures.sh
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
dest="${here}/upstream_fixtures"
repo="https://gitlab.com/librespacefoundation/satnogs/satnogs-auto-scheduler.git"
# The same commit backend/requirements.txt installs. Keep the two in step.
sha="0f7ec01776dad49f5ba2e66f8678649451075bac"

if [ -d "${dest}" ] && [ -f "${dest}/satellites.json" ]; then
  echo "fixtures already present in ${dest}"
  exit 0
fi

tmp="$(mktemp -d)"
trap 'rm -rf "${tmp}"' EXIT

echo "fetching ${sha:0:9} from ${repo}"
git -C "${tmp}" init -q .
git -C "${tmp}" remote add origin "${repo}"
# That commit is a merge-request head, not on master, so it is fetched by sha
# directly rather than by branch.
git -C "${tmp}" fetch -q --depth 1 origin "${sha}"
git -C "${tmp}" checkout -q FETCH_HEAD

mkdir -p "${dest}"
for f in satellites.json transmitters_receivable.json transmitters_stats.json \
         tles.json search_satellites_output.json; do
  cp "${tmp}/tests/fixtures/${f}" "${dest}/${f}"
done
echo "fixtures written to ${dest}"
