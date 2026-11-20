#!/usr/bin/env bash
# Blocked-mirror build test for the apt snapshot pin (homelab#1981 AC3, D-051).
#
# Builds the Dockerfile's `base` stage twice, with --no-cache so apt really runs:
#
#   1. mirror-blocked  archive.ubuntu.com and security.ubuntu.com resolve to
#                      127.0.0.1 inside every RUN step. The build must still
#                      succeed, every apt fetch must come from the snapshot source,
#                      and none from the live mirror.
#   2. source-blocked  the snapshot source itself (the host in APT_SNAPSHOT_BASE:
#                      snapshot.ubuntu.com, or the apt-cacher-ng Service in CI) is
#                      blocked the same way. The build must FAIL, with apt's own
#                      error in the log, and without falling back to the live mirror.
#
# Why --add-host: it is a build flag, so it reaches the RUN containers through the
# daemon on any runner. On the ARC DinD runner the daemon is a sidecar that can't
# see this container's files, so nothing here bind-mounts from the host; the build
# context is the repo, sent over the API like any build.
#
#   APT_SNAPSHOT_BASE  passed as --build-arg when set (CI passes the cache URL).
#                      Unset = the Dockerfile's default, snapshot.ubuntu.com direct.
#   DPKG_OUT           optional: write the built image's `dpkg-query -W` here.
#
# Prints APT-SNAPSHOT-TEST-OK as its last line only if both cases held.
set -euo pipefail
cd "$(dirname "$0")/.."

IMG=gpu-batch-apt-snapshot-test:ci
LOGDIR=$(mktemp -d)
trap 'rm -rf "$LOGDIR"' EXIT

args=()
if [ -n "${APT_SNAPSHOT_BASE:-}" ]; then
  args+=(--build-arg "APT_SNAPSHOT_BASE=${APT_SNAPSHOT_BASE}")
  base="$APT_SNAPSHOT_BASE"
else
  base=$(sed -n 's/^ARG APT_SNAPSHOT_BASE=//p' Dockerfile)
fi
src_host=$(printf '%s\n' "$base" | sed -E 's|^[a-z]+://([^/:]+).*|\1|')
[ -n "$src_host" ] || { echo "could not read a host from '$base'"; exit 1; }
echo "snapshot source: $base (host $src_host)"

build() {  # build <log> <extra args...>; returns docker's exit code
  local log=$1; shift
  set +e
  docker buildx build --builder default --no-cache --progress=plain --target base \
    "${args[@]}" "$@" -t "$IMG" . >"$log" 2>&1
  local rc=$?
  set -e
  return $rc
}

live=(--add-host archive.ubuntu.com:127.0.0.1 --add-host security.ubuntu.com:127.0.0.1)
fail=0

echo "== case 1: live mirror blocked, snapshot reachable: must build"
if build "$LOGDIR/1.log" "${live[@]}"; then
  gets=$(grep -cE "Get:[0-9]+ [a-z]+://${src_host}[:/]" "$LOGDIR/1.log" || true)
  livegets=$(grep -cE "(Get|Hit|Ign):[0-9]+ [a-z]+://(archive|security)\.ubuntu\.com" "$LOGDIR/1.log" || true)
  echo "   fetches from $src_host: $gets; from the live mirror: $livegets"
  if [ "$gets" -lt 1 ] || [ "$livegets" -ne 0 ]; then
    echo "FAIL: case 1 built, but apt did not read only the snapshot source"; fail=1
  else
    echo "   ok"
    [ -z "${DPKG_OUT:-}" ] || docker run --rm --entrypoint dpkg-query "$IMG" -W >"$DPKG_OUT"
  fi
else
  echo "FAIL: case 1 did not build with the live mirror blocked"; fail=1
  grep -E "^#[0-9]+ .*(E:|W:|Err:)" "$LOGDIR/1.log" | head -20 || true
  tail -n 20 "$LOGDIR/1.log"
fi

echo "== case 2: snapshot source blocked too: must fail clearly, no fallback"
if build "$LOGDIR/2.log" "${live[@]}" --add-host "${src_host}:127.0.0.1"; then
  echo "FAIL: case 2 built with its only apt source unreachable"; fail=1
else
  err=$(grep -E "E: (Failed to fetch|Some index files failed)" "$LOGDIR/2.log" | head -3 || true)
  livegets=$(grep -cE "(Get|Hit):[0-9]+ [a-z]+://(archive|security)\.ubuntu\.com" "$LOGDIR/2.log" || true)
  if [ -z "$err" ]; then
    echo "FAIL: case 2 failed, but not with a readable apt error"; fail=1
    tail -n 20 "$LOGDIR/2.log"
  elif [ "$livegets" -ne 0 ]; then
    echo "FAIL: case 2 reached the live mirror"; fail=1
  else
    printf '   apt said: %s\n' "$err"
    echo "   ok"
  fi
fi

docker image rm -f "$IMG" >/dev/null 2>&1 || true
[ "$fail" -eq 0 ] || exit 1
echo APT-SNAPSHOT-TEST-OK
