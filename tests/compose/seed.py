"""Seed the harness's `work` volume for one case (gpu-batch-docker#10).

Writes /work/_fixtures/* and one job dir per tool, /work/<tool>/spec.json,
the same spec shape bin/gpu-batch-submit writes for a Job. Each tool
service then runs `python3 -m gpu_batch.runner /work/<tool>/spec.json`.

  python3 seed.py <case>        (cases: see CASES in check.py)
"""
import json
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
from check import CASES  # noqa: E402

WORK = Path("/work")
NOTIFY = {"github": "dvystrcil/gpu-batch-docker#10"}


def specs(case: str) -> dict:
    blender_argv = ["blender", "-b", "--factory-startup", "--python-exit-code", "1",
                    "-P", "/work/_fixtures/render_part.py", "--",
                    "/work/freecad/out/part.stl", "{job}/out/part.png"]
    if case == "print-starts":
        # Outlive the guard's 60 s mid-step re-check, so a print that starts
        # while this step runs is what stops it.
        blender_argv = ["sh", "-c", " ".join(blender_argv) + " && exec sleep 300"]
    return {
        "freecad": {
            "id": "freecad", "notify": NOTIFY,
            "steps": [{"name": "model-to-stl", "timeout_s": 300,
                       "argv": ["sh", "-c", "FC_OUT={job}/out exec freecadcmd /work/_fixtures/make_part.py"]},
                      {"name": "gui-mode", "timeout_s": 300,
                       "argv": ["sh", "-c", "FC_OUT={job}/out exec xvfb-run -a freecad /work/_fixtures/fc_gui_check.py"]}],
            "expect_outputs": ["out/part.stl", "out/headless.json", "out/gui.json"], "min_outputs": 3},
        "prusaslicer": {
            "id": "prusaslicer", "notify": NOTIFY,
            "steps": [{"name": "slice", "timeout_s": 300,
                       "argv": ["sh", "-c", "mkdir -p {job}/tmp/datadir {job}/out && exec prusa-slicer "
                                "--datadir {job}/tmp/datadir --export-gcode --center 100,100 "
                                "--output {job}/out/part.gcode /work/freecad/out/part.stl"]}],
            "expect_outputs": ["out/part.gcode"], "min_outputs": 1},
        "orcaslicer": {
            "id": "orcaslicer", "notify": NOTIFY,
            "steps": [{"name": "slice", "timeout_s": 300,
                       "argv": ["sh", "/work/_fixtures/orca_slice.sh", "{job}", "/work/freecad/out/part.stl"]}],
            "expect_outputs": ["out/plate_1.gcode", "out/plate_1.statistics.json"], "min_outputs": 2},
        "blender": {
            "id": "blender", "notify": NOTIFY,
            "steps": [{"name": "render", "timeout_s": 600, "argv": blender_argv}],
            "expect_outputs": ["out/part.png"], "min_outputs": 1},
    }


def main(argv) -> int:
    case = argv[1] if len(argv) > 1 else ""
    if case not in CASES:
        print(f"seed: unknown case {case!r}; known: {', '.join(CASES)}", file=sys.stderr)
        return 1
    shutil.copytree(HERE / "fixtures", WORK / "_fixtures")
    for tool, spec in specs(case).items():
        d = WORK / tool
        d.mkdir()
        (d / "spec.json").write_text(json.dumps(spec, indent=1))
    (WORK / "_case").write_text(case)
    print(f"seed: {case}: {', '.join(specs(case))}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
