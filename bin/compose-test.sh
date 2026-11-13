#!/usr/bin/env bash
# Compose test harness for the tool images (gpu-batch-docker#10).
#
#   bin/compose-test.sh [case ...]       default: every case
#
# Each case is one `docker compose up` of tests/compose/compose.yaml under its
# own project name, so cases share nothing; `down -v` removes the containers
# and the work volume on every exit path. Images come from the environment,
# so CI tests the images it just built:
#   FREECAD_IMAGE, PRUSASLICER_IMAGE, ORCASLICER_IMAGE, BLENDER_IMAGE   (default <name>:local)
#   RUNNER_IMAGE                                      (the harness's base; default gpu-batch-runner:local)
# Test knobs (to prove the harness goes red): SINK_FAIL=<http code>,
# MOCK_STATE=<state> overrides the case's printer state.
#
# The last line is COMPOSE-TEST-OK only if every case's assert printed
# COMPOSE-TEST-OK as ITS last line (ci-assert-completion-marker); otherwise
# COMPOSE-TEST-FAILED and exit 1.
set -euo pipefail
cd "$(dirname "$0")/.."
F=tests/compose/compose.yaml
export FREECAD_IMAGE=${FREECAD_IMAGE:-gpu-batch-freecad:local}
export PRUSASLICER_IMAGE=${PRUSASLICER_IMAGE:-gpu-batch-prusaslicer:local}
export ORCASLICER_IMAGE=${ORCASLICER_IMAGE:-gpu-batch-orcaslicer:local}
export BLENDER_IMAGE=${BLENDER_IMAGE:-gpu-batch-blender:local}
export RUNNER_IMAGE=${RUNNER_IMAGE:-gpu-batch-runner:local}
export HARNESS_TAG=${HARNESS_TAG:-local-$$}
mapfile -t ALL < <(python3 -c 'import sys; sys.path.insert(0, "tests/compose"); from check import CASES; print("\n".join(CASES))')
CASES_RUN=("$@")
[ ${#CASES_RUN[@]} -gt 0 ] || CASES_RUN=("${ALL[@]}")

for img in "$FREECAD_IMAGE" "$PRUSASLICER_IMAGE" "$ORCASLICER_IMAGE" "$BLENDER_IMAGE" "$RUNNER_IMAGE"; do
  docker image inspect "$img" >/dev/null 2>&1 || {
    echo "missing image $img: docker build --target <freecad|prusaslicer|orcaslicer|blender|runner> -t <name>:local ." >&2
    echo "COMPOSE-TEST-FAILED"; exit 1; }
done

PROJECTS=()
LOGDIR=$(mktemp -d)
cleanup() {
  rm -rf "$LOGDIR"
  for p in "${PROJECTS[@]}"; do
    CASE=x docker compose -p "$p" -f "$F" down -v --remove-orphans >/dev/null 2>&1 || true
  done
  docker image rm "gpu-batch-harness:${HARNESS_TAG}" >/dev/null 2>&1 || true
}
trap cleanup EXIT

CASE=chain docker compose -f "$F" build --quiet seed

# Captured once: inside the loop MOCK_STATE holds the previous case's value.
CALLER_MOCK_STATE=${MOCK_STATE:-}
failed=()
for case in "${CASES_RUN[@]}"; do
  printf '%s\n' "${ALL[@]}" | grep -qxF -- "$case" || { echo "unknown case $case (known: ${ALL[*]})"; failed+=("$case"); continue; }
  p="gbd-compose-test-${case}-$$"
  PROJECTS+=("$p")
  # Per-case settings come from check.py's CASES table; MOCK_STATE from the
  # caller's env wins (test knob).
  while IFS='=' read -r k v; do export "$k=$v"; done < <(python3 tests/compose/check.py --env "$case")
  [ -z "$CALLER_MOCK_STATE" ] || export MOCK_STATE="$CALLER_MOCK_STATE"
  export CASE="$case"
  echo "=== case $case (printer $MOCK_STATE${BLENDER_PRINT_GUARD:+, blender GPU_BATCH_PRINT_GUARD=$BLENDER_PRINT_GUARD}${BLENDER_NVIDIA_VISIBLE_DEVICES:+ NVIDIA_VISIBLE_DEVICES=$BLENDER_NVIDIA_VISIBLE_DEVICES})"
  set +e
  # A hang is a failure, not a stuck job; the trap's down -v cleans up after it.
  # `run assert` starts the whole graph with its depends_on conditions and
  # returns assert's own exit code. Not `up --abort-on-container-exit`: that
  # aborts on the FIRST exit, which is the seed's successful one.
  timeout --kill-after=30 "${COMPOSE_TEST_TIMEOUT_S:-900}" \
    docker compose -p "$p" -f "$F" run --rm --quiet-pull assert > "$LOGDIR/$p.assert" 2> "$LOGDIR/$p.log" </dev/null
  rc=$?
  set -e
  cat "$LOGDIR/$p.assert"
  if [ "$rc" -eq 0 ] && [ "$(tail -n 1 "$LOGDIR/$p.assert")" = COMPOSE-TEST-OK ]; then
    echo "--- case $case: ok"
  else
    echo "--- case $case: FAILED (compose run exit $rc); compose log tail:"
    tail -n 40 "$LOGDIR/$p.log"
    for t in freecad prusaslicer orcaslicer blender; do
      docker compose -p "$p" -f "$F" logs --no-log-prefix "$t" 2>/dev/null | tail -n 5 | sed "s/^/  [$t] /"
    done
    # The jobs' own stderr, read from the work volume (no bind mount to read it from).
    docker compose -p "$p" -f "$F" run --rm --no-deps -T assert sh -c \
      'for t in freecad prusaslicer orcaslicer blender; do [ -f /work/$t/job.err ] && tail -n 15 /work/$t/job.err | sed "s/^/  [$t job.err] /"; done' 2>/dev/null || true
    failed+=("$case")
  fi
  rm -f "$LOGDIR/$p.log" "$LOGDIR/$p.assert"
  CASE=x docker compose -p "$p" -f "$F" down -v --remove-orphans >/dev/null 2>&1 || true
done

if [ ${#failed[@]} -gt 0 ]; then
  echo "COMPOSE-TEST-FAILED: ${failed[*]}"
  exit 1
fi
echo "COMPOSE-TEST-OK"
