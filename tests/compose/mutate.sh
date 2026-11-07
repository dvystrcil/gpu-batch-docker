#!/usr/bin/env bash
# Prove the compose harness goes red when it should (gpu-batch-docker#10 AC4).
# A harness that is green on a broken image is worse than none.
#
#   tests/compose/mutate.sh [mutant ...]      default: all (about 10 minutes)
#
# Each mutant copies the harness (bin/compose-test.sh + tests/compose/) to a
# scratch dir, applies ONE change (it fails loudly if the change didn't
# apply, so a typo can't pass as "killed"), and runs one case. The mutant is
# killed if the driver does NOT end with COMPOSE-TEST-OK. Same image env as
# bin/compose-test.sh. Last line: MUTANTS-ALL-KILLED, or MUTANTS-SURVIVED and exit 1.
set -euo pipefail
cd "$(dirname "$0")/../.."
ROOT=$PWD

# name | case | env (space-separated K=V, or -) | file | old | new
# (old/new are literal; "-" for file = no source change)
MUTANTS=$(cat <<'EOF'
wrong-output-glob|chain|-|tests/compose/seed.py|"expect_outputs": ["out/part.png"]|"expect_outputs": ["out/part.jpg"]
cpu-tool-guard-switch-removed|printing|-|tests/compose/compose.yaml|      <<: *tool-env\n      GPU_BATCH_PRINT_GUARD: "off"\n\n  prusaslicer:|      <<: *tool-env\n\n  prusaslicer:
sink-returns-500|chain|SINK_FAIL=500|-||
unknown-printer-state|chain|MOCK_STATE=bogus|-||
print-never-starts|print-starts|-|tests/compose/check.py|            urllib.request.urlopen(req, timeout=10).read()\n|            pass\n
reset-is-not-unreachable|unreachable|MOCK_STATE=reset|-||
no-gpu-so-guard-off-honoured|guard-ignored|-|tests/compose/check.py|"blender_env": {"GPU_BATCH_PRINT_GUARD": "off", "NVIDIA_VISIBLE_DEVICES": "GPU-test"}|"blender_env": {"GPU_BATCH_PRINT_GUARD": "off"}
image-without-the-slicer|chain|PRUSASLICER_IMAGE=gpu-batch-runner:local|-||
freecad-version-drift|chain|EXPECT_FREECAD=26.4.0|-||
prusaslicer-version-drift|chain|EXPECT_PRUSASLICER=2.9.8|-||
gui-step-runs-headless|chain|-|tests/compose/seed.py|exec xvfb-run -a freecad /work/_fixtures/fc_gui_check.py|exec freecadcmd /work/_fixtures/fc_gui_check.py
image-without-freecad|chain|FREECAD_IMAGE=gpu-batch-blender:local|-||
assert-dies-before-marker|chain|-|tests/compose/check.py|    print("COMPOSE-TEST-OK", flush=True)\n|    raise SystemExit(0)\n
EOF
)

want=("$@")
survived=()
ran=0
expected=$(printf '%s\n' "$MUTANTS" | grep -c . || true)
[ ${#want[@]} -eq 0 ] || expected=${#want[@]}
SCRATCH=$(mktemp -d)
trap 'rm -rf "$SCRATCH"' EXIT
while IFS='|' read -r name case envs file old new; do
  [ -n "$name" ] || continue
  if [ ${#want[@]} -gt 0 ] && ! printf '%s\n' "${want[@]}" | grep -qxF "$name"; then continue; fi
  d="$SCRATCH/$name"
  mkdir -p "$d/bin" "$d/tests"
  cp "$ROOT/bin/compose-test.sh" "$d/bin/"
  cp -r "$ROOT/tests/compose" "$d/tests/"
  if [ "$file" != - ]; then
    python3 - "$d/$file" "$old" "$new" <<'PY'
import sys
path, old, new = sys.argv[1], sys.argv[2].replace("\\n", "\n"), sys.argv[3].replace("\\n", "\n")
s = open(path).read()
if s.count(old) != 1:
    sys.exit(f"mutant did not apply: {old!r} occurs {s.count(old)} times in {path}")
open(path, "w").write(s.replace(old, new))
PY
  fi
  env_args=()
  [ "$envs" = - ] || read -r -a env_args <<< "$envs"
  set +e
  # </dev/null: `compose run` reads stdin, which is this loop's mutant list.
  out=$(env "${env_args[@]}" "$d/bin/compose-test.sh" "$case" 2>&1 </dev/null)
  set -e
  ran=$((ran + 1))
  last=$(printf '%s\n' "$out" | tail -n 1)
  why=$(printf '%s\n' "$out" | grep -m3 -E '^FAIL |dependency failed|missing image' | tr '\n' ';' || true)
  if [ "$last" = COMPOSE-TEST-OK ]; then
    echo "SURVIVED $name ($case)"
    survived+=("$name")
  else
    echo "killed   $name ($case): ${why:-$last}"
  fi
done <<< "$MUTANTS"

# A mutant that never ran is not a killed one.
if [ "$ran" -ne "$expected" ]; then
  echo "MUTANTS-INCOMPLETE: ran $ran of $expected"
  exit 1
fi
if [ ${#survived[@]} -gt 0 ]; then
  echo "MUTANTS-SURVIVED: ${survived[*]}"
  exit 1
fi
echo "MUTANTS-ALL-KILLED"
