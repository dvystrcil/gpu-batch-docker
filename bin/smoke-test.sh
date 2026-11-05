#!/usr/bin/env bash
# Smoke-test a built gpu-batch-blender image WITHOUT a GPU (CI has none).
#
# Runs the image's real entrypoint against a fake moonraker, so it proves
# the composition the cluster runs: guard -> Blender step -> output check ->
# status.json. Two cases:
#   1. printer "standby": a tiny CPU Cycles render must produce a valid PNG
#      and status.json must say succeeded
#   2. printer "printing": the job must be refused and run no step
# Prints SMOKE-COMPLETE only as its last line (ci-assert-completion-marker).
#
#   bin/smoke-test.sh <image>
set -euo pipefail
IMG=${1:?usage: smoke-test.sh <image>}
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
chmod 777 "$WORK"

cat > "$WORK/scene.py" <<'PY'
import bpy, sys
out = sys.argv[sys.argv.index("--") + 1]
bpy.ops.wm.read_factory_settings(use_empty=False)   # default cube, camera, light
sc = bpy.context.scene
sc.render.engine = "CYCLES"; sc.cycles.device = "CPU"; sc.cycles.samples = 1
sc.render.resolution_x = sc.render.resolution_y = 64
sc.render.filepath = out
bpy.ops.render.render(write_still=True)
PY

fake_moonraker() {  # $1 = state; serves print_stats on 127.0.0.1:7125
  cat <<PY
import json, http.server
class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        self.send_response(200); self.end_headers()
        self.wfile.write(json.dumps({"result": {"status": {"print_stats": {"state": "$1"}}}}).encode())
http.server.HTTPServer(("127.0.0.1", 7125), H).serve_forever()
PY
}

run_case() {  # $1 = case dir, $2 = printer state
  mkdir -p "$WORK/$1" && chmod 777 "$WORK/$1"
  cat > "$WORK/$1/spec.json" <<JSON
{"id": "$1", "steps": [{"name": "render", "timeout_s": 600,
  "argv": ["blender", "-b", "--factory-startup", "-P", "/work/scene.py", "--", "{job}/out/smoke.png"]}],
 "expect_outputs": ["out/*.png"], "min_outputs": 1, "notify": null}
JSON
  fake_moonraker "$2" > "$WORK/$1/fake.py"
  set +e
  docker run --rm -v "$WORK:/work" -e MOONRAKER_URL=http://127.0.0.1:7125 \
    --entrypoint sh "$IMG" -c \
    "python3 /work/$1/fake.py & sleep 1; python3 -m gpu_batch.runner /work/$1/spec.json"
  rc=$?
  set -e
  echo "$1: exit $rc"
  return 0
}

state() { python3 -c 'import json,sys; s=json.load(open(sys.argv[1])); print(s["state"]); print(s["reason"], file=sys.stderr)' "$1"; }

run_case idle standby
[ "$(state "$WORK/idle/status.json")" = succeeded ] || { cat "$WORK/idle/job.err" || true; echo "FAIL: idle render did not succeed"; exit 1; }
head -c 8 "$WORK/idle/out/smoke.png" | od -An -tx1 | grep -q '89 50 4e 47' || { echo "FAIL: smoke.png is not a PNG"; exit 1; }
grep -q '"width": 64' "$WORK/idle/status.json" || { echo "FAIL: status.json lacks the 64px output"; exit 1; }

run_case busy printing
[ "$(state "$WORK/busy/status.json")" = refused ] || { echo "FAIL: render was not refused while printing"; exit 1; }
[ ! -e "$WORK/busy/out" ] || { echo "FAIL: a refused job produced output"; exit 1; }

echo "SMOKE-COMPLETE"
