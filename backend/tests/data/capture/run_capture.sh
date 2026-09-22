#!/usr/bin/env bash
# Re-capture backend/tests/data/schedule_dry_run.log.
#
# Everything runs inside the backend image, because that is where the official
# satnogs-auto-scheduler is installed and because the stub API has to be
# reachable on the same loopback interface as the tool.
#
#   --network=none  the container gets a network namespace with loopback and
#                   nothing else, so this provably cannot reach the real
#                   network.satnogs.org or db.satnogs.org.
#   --user          so the container can write the transcript back into the
#                   repo as you rather than as the image's uid 10001.
#
# Usage:  ./run_capture.sh
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
data_dir="$(cd "${here}/.." && pwd)"
repo_root="$(cd "${here}/../../../.." && pwd)"
image="${IMAGE:-ground-station-dashboard-backend:latest}"

# The fixtures the stub serves are not committed - see the script's header.
"${here}/fetch_upstream_fixtures.sh"

exec docker run --rm \
  --network=none \
  --user "$(id -u):$(id -g)" \
  -e HOME=/tmp \
  -v "${here}":/w:ro \
  -v "${repo_root}":/repo:ro \
  -v "${data_dir}":/out \
  "${image}" \
  python /w/harness.py
