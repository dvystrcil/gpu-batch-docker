# freecadcmd step: a 30 x 20 x 10 mm box -> $FC_OUT/part.stl, plus
# $FC_OUT/headless.json proving the image's FreeCAD: its version, that
# freecadcmd has no GUI, and that the Fasteners workbench really generates
# geometry (an ISO 4762 M3x8 screw), loaded from the image's Mod dir.
# FreeCAD's exit code is unreliable (it can swallow an exception), so the
# outputs are written only after every check here passed; the runner's
# output check then fails the step if anything went wrong.
import json
import os

import FreeCAD
import FastenersCmd
import Mesh

out = os.environ["FC_OUT"]
doc = FreeCAD.newDocument("harness")
box = doc.addObject("Part::Box", "Box")
box.Length, box.Width, box.Height = 30, 20, 10
screw = doc.addObject("Part::FeaturePython", "Screw")
FastenersCmd.FSScrewObject(screw, "ISO4762", None)
screw.Diameter, screw.Length = "M3", "8"
doc.recompute()
assert box.Shape.isValid(), "box shape invalid"
assert abs(box.Shape.Volume - 6000.0) < 1e-6, f"box volume {box.Shape.Volume}"
assert screw.Shape.isValid() and screw.Shape.Volume > 10, f"bad screw: volume {screw.Shape.Volume}"
os.makedirs(out, exist_ok=True)
Mesh.export([box], os.path.join(out, "part.stl"))
json.dump({"freecad": ".".join(FreeCAD.Version()[:3]), "gui_up": bool(FreeCAD.GuiUp),
           "fasteners": FastenersCmd.__file__, "screw_volume_mm3": round(screw.Shape.Volume, 2)},
          open(os.path.join(out, "headless.json"), "w"))
