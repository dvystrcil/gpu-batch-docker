#!/usr/bin/env bash
# Smoke-test a built gpu-batch-freecad image (homelab#1961 AC4).
#
# Runs the image's real entrypoint against a fake moonraker, so it proves the
# composition a Job runs: guard -> FreeCAD steps -> output check ->
# status.json. Two cases:
#   1. printer "standby": two steps must both produce their outputs
#      a. headless: freecadcmd builds a Fasteners ISO 4762 M3x8 screw (the
#         workbench really generates geometry, not just imports), checks its
#         volume and exports it as STL
#      b. GUI: xvfb-run + freecad (GUI mode, as render_full.sh's export uses)
#         must come up with FreeCAD.GuiUp true and run the script
#   2. printer "printing": the job must be refused and run no step
# Prints SMOKE-COMPLETE only as its last line (ci-assert-completion-marker).
#
#   bin/smoke-test-freecad.sh <image>
set -euo pipefail
IMG=${1:?usage: smoke-test-freecad.sh <image>}
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
chmod 777 "$WORK"

# Outputs are written only after every check in the script passed, so a
# FreeCAD that swallows an exception (its exit codes are unreliable) still
# fails the runner's output check.
cat > "$WORK/fc_smoke_headless.py" <<'PY'
import json, os
import FreeCAD, Mesh
import FastenersCmd
out = os.environ["FC_OUT"]; os.makedirs(out, exist_ok=True)
doc = FreeCAD.newDocument("smoke")
o = doc.addObject("Part::FeaturePython", "Screw")
FastenersCmd.FSScrewObject(o, "ISO4762", None)
o.Diameter = "M3"; o.Length = "8"
doc.recompute()
vol = o.Shape.Volume
assert o.Shape.isValid() and vol > 10, f"bad screw: valid={o.Shape.isValid()} volume={vol}"
Mesh.export([o], os.path.join(out, "screw.stl"))
json.dump({"freecad": ".".join(FreeCAD.Version()[:3]), "gui_up": bool(FreeCAD.GuiUp),
           "fasteners": FastenersCmd.__file__, "screw_volume_mm3": round(vol, 2)},
          open(os.path.join(out, "headless.json"), "w"))
PY

# GUI mode IMPORTS a .py argument as a module (App::Application::processFiles,
# loadModule(<file stem>)), so the stem must not shadow a module that is
# already loaded: a script called gui.py is silently never run and FreeCAD
# idles in its event loop forever. os._exit: the GUI has no clean headless exit.
cat > "$WORK/fc_smoke_gui.py" <<'PY'
import json, os
import FreeCAD, FreeCADGui
out = os.environ["FC_OUT"]; os.makedirs(out, exist_ok=True)
if bool(FreeCAD.GuiUp):
    json.dump({"gui_up": True, "main_window": bool(FreeCADGui.getMainWindow())},
              open(os.path.join(out, "gui.json"), "w"))
os._exit(0)
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
{"id": "$1", "steps": [
  {"name": "headless", "timeout_s": 300,
   "argv": ["sh", "-c", "FC_OUT={job}/out exec freecadcmd /work/fc_smoke_headless.py"]},
  {"name": "gui", "timeout_s": 300,
   "argv": ["sh", "-c", "FC_OUT={job}/out exec xvfb-run -a freecad /work/fc_smoke_gui.py"]}],
 "expect_outputs": ["out/*.stl", "out/*.json"], "min_outputs": 3, "notify": null}
JSON
  fake_moonraker "$2" > "$WORK/$1/fake.py"
  # docker cp, not -v: on the ARC runners docker is a DinD sidecar, so a
  # bind mount names a path on the DAEMON's filesystem (see smoke-test.sh).
  local ctr="gpu-batch-smoke-freecad-$1-$$"
  docker create --name "$ctr" -e MOONRAKER_URL=http://127.0.0.1:7125 \
    --entrypoint sh "$IMG" -c \
    "python3 /work/$1/fake.py & sleep 1; python3 -m gpu_batch.runner /work/$1/spec.json" >/dev/null
  docker cp "$WORK/." "$ctr:/work"
  set +e
  docker start -a "$ctr"
  rc=$?
  set -e
  docker cp "$ctr:/work/$1/." "$WORK/$1/"
  docker rm -f "$ctr" >/dev/null
  echo "$1: exit $rc"
  return 0
}

state() { python3 -c 'import json,sys; s=json.load(open(sys.argv[1])); print(s["state"]); print(s["reason"], file=sys.stderr)' "$1"; }
field() { python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))[sys.argv[2]])' "$1" "$2"; }

run_case idle standby
[ "$(state "$WORK/idle/status.json")" = succeeded ] || { cat "$WORK/idle/job.err" || true; echo "FAIL: idle job did not succeed"; exit 1; }
[ "$(field "$WORK/idle/out/headless.json" freecad)" = "${EXPECT_FREECAD:-26.3.0}" ] || { cat "$WORK/idle/out/headless.json"; echo "FAIL: unexpected FreeCAD version"; exit 1; }
[ "$(field "$WORK/idle/out/headless.json" gui_up)" = False ] || { echo "FAIL: freecadcmd reports a GUI"; exit 1; }
grep -q '/Mod/fasteners/' "$WORK/idle/out/headless.json" || { echo "FAIL: Fasteners not loaded from the image's Mod dir"; exit 1; }
# Binary STL: 80-byte header, uint32 triangle count, 50 bytes per triangle.
python3 - "$WORK/idle/out/screw.stl" <<'PY' || { echo "FAIL: screw.stl is not a valid STL"; exit 1; }
import struct, sys
d = open(sys.argv[1], "rb").read()
n = struct.unpack("<I", d[80:84])[0] if len(d) >= 84 else 0
ok = (n > 0 and len(d) == 84 + 50 * n) or (d.startswith(b"solid") and d.count(b"facet normal") > 0)
sys.exit(0 if ok else 1)
PY
[ "$(field "$WORK/idle/out/gui.json" gui_up)" = True ] || { echo "FAIL: GUI mode did not come up under xvfb-run"; exit 1; }
echo "headless: $(cat "$WORK/idle/out/headless.json")"
echo "gui: $(cat "$WORK/idle/out/gui.json")"

run_case busy printing
[ "$(state "$WORK/busy/status.json")" = refused ] || { echo "FAIL: job was not refused while printing"; exit 1; }
[ ! -e "$WORK/busy/out" ] || { echo "FAIL: a refused job produced output"; exit 1; }

echo "SMOKE-COMPLETE"
