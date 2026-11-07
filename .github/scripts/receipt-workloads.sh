#!/usr/bin/env bash
# receipt-workloads.sh <deploy-repo-root> <image>...
#
# Prints the comma-separated names of the CronJobs in <root>/base/ whose
# container image is harbor.sirddail.net/ai/<image>, one per bumped image,
# for the bump PR's `receipt-target: image-bump ns=gpu-batch workloads=...`
# marker (homelab#1992). homelab's receipt sweep verifies exactly those
# templates.
#
# Fails, and prints nothing, if any bumped image has no CronJob: half a
# target is worse than none, because it would verify the wrong set.
set -euo pipefail
root=$1; shift
names=()
for img in "$@"; do
  found=""
  for f in "$root"/base/*.yaml; do
    grep -qE "^[[:space:]]*(- )?image:[[:space:]]*harbor\.sirddail\.net/ai/${img}(:[^[:space:]]*)?[[:space:]]*$" "$f" || continue
    n=$(awk '/^kind: CronJob[[:space:]]*$/ {cj=1; next} cj && /^  name:/ {print $2; exit}' "$f")
    [ -n "$n" ] && found="$n" && names+=("$n")
  done
  [ -n "$found" ] || { echo "no CronJob in $root/base uses harbor.sirddail.net/ai/${img}" >&2; exit 1; }
done
(IFS=,; echo "${names[*]}")
