# freecad GUI-mode step, under xvfb-run (render_full.sh's export runs this
# way): $FC_OUT/gui.json only if the GUI really came up.
# GUI mode IMPORTS a .py argument as a module (loadModule(<file stem>)), so
# the stem must not shadow a loaded module: a script called gui.py is
# silently never run and FreeCAD idles forever. os._exit: the GUI has no
# clean headless exit.
import json
import os

import FreeCAD
import FreeCADGui

out = os.environ["FC_OUT"]
os.makedirs(out, exist_ok=True)
if bool(FreeCAD.GuiUp):
    json.dump({"gui_up": True, "main_window": bool(FreeCADGui.getMainWindow())},
              open(os.path.join(out, "gui.json"), "w"))
os._exit(0)
