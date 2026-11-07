# blender step: render the FreeCAD STL with CPU Cycles, 1 sample, 64 px.
#   blender -b --factory-startup --python-exit-code 1 -P render_part.py -- <in.stl> <out.png>
import math
import sys

import bpy

stl, out = sys.argv[sys.argv.index("--") + 1:][:2]
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.wm.stl_import(filepath=stl)
obj = bpy.context.selected_objects[0]
assert len(obj.data.polygons) >= 12, f"{len(obj.data.polygons)} faces imported"
bpy.ops.object.origin_set(type="ORIGIN_GEOMETRY", center="BOUNDS")
obj.location = (0, 0, 0)

sc = bpy.context.scene
cam = bpy.data.objects.new("cam", bpy.data.cameras.new("cam"))
sc.collection.objects.link(cam)
cam.location = (60, -60, 50)
track = cam.constraints.new("TRACK_TO")
track.target, track.track_axis, track.up_axis = obj, "TRACK_NEGATIVE_Z", "UP_Y"
sun = bpy.data.objects.new("sun", bpy.data.lights.new("sun", "SUN"))
sun.rotation_euler = (math.radians(45), 0, math.radians(45))
sc.collection.objects.link(sun)
sc.camera = cam

sc.render.engine = "CYCLES"
sc.cycles.device = "CPU"
sc.cycles.samples = 1
sc.render.resolution_x = sc.render.resolution_y = 64
sc.render.resolution_percentage = 100
sc.render.filepath = out
bpy.ops.render.render(write_still=True)
