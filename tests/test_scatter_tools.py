"""Scatter tool features: spacing, slope, patches, avoid/paths, edge fade,
rotation, color attributes, distance options, system management, presets.
    blender -b --factory-startup --python tests/test_scatter_tools.py
"""
import importlib.util
import math
import os
import shutil
import sys
import tempfile

import bmesh
import bpy
from mathutils import Vector
from mathutils.kdtree import KDTree

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location("scene_optimizer", os.path.join(ROOT, "scene_optimizer.py"))
so = importlib.util.module_from_spec(spec)
sys.modules["scene_optimizer"] = so
spec.loader.exec_module(so)
so.register()

failures = []


def check(cond, msg):
    print(("ok    " if cond else "FAIL  ") + msg)
    if not cond:
        failures.append(msg)


for o in list(bpy.data.objects):
    bpy.data.objects.remove(o)
scene = bpy.context.scene
vl = bpy.context.view_layer
scene.camera = None


def grid(name, size, segs, z=lambda x, y: 0.0, loc=(0, 0, 0)):
    me = bpy.data.meshes.new(name)
    bm = bmesh.new()
    bmesh.ops.create_grid(bm, x_segments=segs, y_segments=segs, size=size / 2.0)
    for v in bm.verts:
        v.co.z = z(v.co.x, v.co.y)
    bm.to_mesh(me)
    bm.free()
    ob = bpy.data.objects.new(name, me)
    ob.location = loc
    scene.collection.objects.link(ob)
    return ob


ground = grid("Ground", 40.0, 40)                        # flat, -20..20
extra = bpy.data.collections.new("Extra Ground")
scene.collection.children.link(extra)
ramp = grid("Ramp", 10.0, 10, z=lambda x, y: 0.0, loc=(40, 0, 0))
ramp.rotation_euler = (0.0, math.radians(70.0), 0.0)     # 70 degree wall
scene.collection.objects.unlink(ramp)
extra.objects.link(ramp)
gentle = grid("Gentle", 10.0, 10, loc=(70, 0, 0))
gentle.rotation_euler = (math.radians(25.0), 0.0, 0.0)   # 25 degree slope
scene.collection.objects.unlink(gentle)
extra.objects.link(gentle)


def tuft(name, segs):
    m = bpy.data.meshes.new(name)
    b = bmesh.new()
    bmesh.ops.create_cone(b, cap_ends=True, segments=segs, radius1=0.1, radius2=0.0, depth=0.4)
    bmesh.ops.translate(b, verts=b.verts, vec=(0, 0, 0.2))
    b.to_mesh(m)
    b.free()
    o = bpy.data.objects.new(name, m)
    o.location = (500, 500, 0)
    scene.collection.objects.link(o)
    return o


lods = [tuft("Blade_LOD0", 24), tuft("Blade_LOD1", 6)]
mat = bpy.data.materials.new("Blade")
if bpy.app.version < (5, 0, 0) or mat.node_tree is None:
    mat.use_nodes = True
for o in lods:
    o.data.materials.append(mat)

with bpy.context.temp_override(scene=scene, view_layer=vl, active_object=ground,
                               selected_objects=lods + [ground]):
    bpy.ops.scene_optimizer.scatter_add()
s = scene.scene_optimizer
sy = s.scatter_systems[0]


def setp(**kw):
    so._Runtime.busy = True
    try:
        for k, v in kw.items():
            setattr(sy, k, v)
    finally:
        so._Runtime.busy = False
    so.scatter_sync(scene, sy)


setp(density=4.0, max_slope=math.radians(45.0), slope_blur=0.0)


def instances(system=None):
    system = system or sy
    dg = bpy.context.evaluated_depsgraph_get()
    dg.update()
    out = []
    for inst in dg.object_instances:
        if inst.is_instance and inst.parent and inst.parent.original == system.holder:
            out.append((inst.object.original.name, inst.matrix_world.copy()))
    return out


def positions(system=None):
    return [m.translation.copy() for _n, m in instances(system)]


def on_ground(ps):
    return [p for p in ps if abs(p.x) < 20.5 and abs(p.y) < 20.5 and abs(p.z) < 0.01]


base = positions()
n_base = len(on_ground(base))
check(n_base > 4.0 * 1600 * 0.85, "no camera: plain scatter on 1600 m2 at 4/m2 (%d)" % n_base)
check(all(n.endswith("LOD0") for n, _m in instances()), "no camera: every instance uses LOD0")

# ---- extra surfaces + slope
setp(extra_surfaces=extra)
ps = positions()
on_ramp = [p for p in ps if 30 < p.x < 50]
on_gentle = [p for p in ps if 60 < p.x < 80]
check(not on_ramp, "70 degree wall in Extra Surfaces gets nothing at Max Slope 45 (%d)" % len(on_ramp))
check(len(on_gentle) > 200, "25 degree slope in Extra Surfaces is populated (%d)" % len(on_gentle))
setp(max_slope=math.radians(25.0), slope_blur=math.radians(20.0))
n_soft = len([p for p in positions() if 60 < p.x < 80])
check(0 < n_soft < len(on_gentle) * 0.8, "slope blur thins the 25 degree slope at Max 25 +- 10 (%d)" % n_soft)
setp(max_slope=math.radians(45.0), slope_blur=0.0, extra_surfaces=None)

# ---- even spacing (Poisson)
def min_gap(ps):
    kd = KDTree(len(ps))
    for i, p in enumerate(ps):
        kd.insert(p, i)
    kd.balance()
    return min(kd.find_n(p, 2)[1][2] for p in ps[:2000])


gap_random = min_gap(base)
setp(distribution='EVEN', min_distance=0.35)
even = on_ground(positions())
gap_even = min_gap(even)
check(gap_even >= 0.35 * 0.98 and gap_random < 0.2,
      "even spacing keeps Min Distance (%.3f), random does not (%.3f)" % (gap_even, gap_random))
setp(distribution='RANDOM')

# ---- noise patches
setp(patch_amount=1.0, patch_size=6.0)
ps = on_ground(positions())
check(len(ps) < n_base * 0.8, "patch density removes instances (%d -> %d)" % (n_base, len(ps)))
cells = {}
for p in ps:
    k = (int((p.x + 20) // 5), int((p.y + 20) // 5))
    cells[k] = cells.get(k, 0) + 1
vals = [cells.get((i, j), 0) for i in range(8) for j in range(8)]
check(max(vals) > 2.5 * max(1, min(vals)), "patches make dense and sparse areas (%d..%d per 25 m2)" % (
    min(vals), max(vals)))
setp(patch_amount=0.0)

# ---- avoid: a path curve and a rock
avoid = bpy.data.collections.new("Avoid")
scene.collection.children.link(avoid)
cu = bpy.data.curves.new("Path", 'CURVE')
cu.dimensions = '3D'
spl = cu.splines.new('POLY')
spl.points.add(1)
spl.points[0].co = (-25, 0, 0, 1)
spl.points[1].co = (25, 0, 0, 1)
path = bpy.data.objects.new("Path", cu)
avoid.objects.link(path)
rock_me = bpy.data.meshes.new("Rock")
b = bmesh.new()
bmesh.ops.create_cube(b, size=4.0)
b.to_mesh(rock_me)
b.free()
rock = bpy.data.objects.new("Rock", rock_me)
rock.location = (10, 10, 0)
avoid.objects.link(rock)
setp(avoid=avoid, avoid_distance=1.5, avoid_blur=0.0)
ps = on_ground(positions())
on_path = [p for p in ps if abs(p.y) < 1.45]
under_rock = [p for p in ps if math.hypot(max(abs(p.x - 10) - 2.0, 0.0), max(abs(p.y - 10) - 2.0, 0.0)) < 1.45]
check(not on_path, "curve path is kept clear (%d inside 1.5 m)" % len(on_path))
check(not under_rock, "rock (+ clear radius) is kept clear (%d)" % len(under_rock))
check(len([p for p in ps if 2.0 < abs(p.y) < 4.0]) > 100, "grass resumes next to the path")
setp(avoid=None)

# ---- edge fade
setp(edge_offset=4.0)
ps = on_ground(positions())
rim = [p for p in ps if max(abs(p.x), abs(p.y)) > 19.0]          # outer metre
inner = [p for p in ps if 10.0 < max(abs(p.x), abs(p.y)) < 11.0]  # same-width ring inside
rim_area, inner_area = 40 ** 2 - 38 ** 2, 22 ** 2 - 20 ** 2
check(len(rim) / rim_area < 0.3 * len(inner) / inner_area,
      "edge fade thins the border (rim %.2f/m2 vs inside %.2f/m2)" % (
          len(rim) / rim_area, len(inner) / inner_area))
setp(edge_offset=0.0)

# ---- rotation
def ups(system=None):
    return [(m.to_3x3() @ Vector((0, 0, 1))).normalized() for _n, m in instances(system)]


setp(random_rotation=0.0)
check(all(abs((m.to_3x3().normalized() @ Vector((1, 0, 0))).x - 1) < 1e-3 for _n, m in instances()),
      "random rotation 0 keeps every instance unrotated")
setp(random_rotation=1.0, lean_angle=math.radians(30.0), lean_direction=0.0)
us = ups()
ang = sum(math.degrees(u.angle(Vector((0, 0, 1)))) for u in us) / len(us)
check(all(u.x > 0.2 for u in us) and 24 < ang < 36, "lean 30 deg towards +X (mean %.1f deg)" % ang)
setp(lean_angle=0.0, random_tilt=math.radians(20.0))
us = ups()
check(max(math.degrees(u.angle(Vector((0, 0, 1)))) for u in us) <= 20.5 and
      sum(u.x for u in us) / len(us) < 0.05, "random tilt up to 20 deg, no preferred direction")
setp(random_tilt=0.0, extra_surfaces=extra, align_amount=1.0)
gentle_n = (gentle.matrix_world.to_3x3() @ Vector((0, 0, 1))).normalized()
us = [u for (_n, m), u in zip(instances(), ups()) if 60 < m.translation.x < 80]
check(us and all(u.angle(gentle_n) < math.radians(1.0) for u in us),
      "Align to Ground 1 follows the 25 deg slope")
setp(align_amount=0.0)
us = [u for (_n, m), u in zip(instances(), ups()) if 60 < m.translation.x < 80]
check(us and all(u.z > 0.9999 for u in us), "Align to Ground 0 keeps instances upright")
setp(extra_surfaces=None)


# ---- color attributes on the instances
def inst_attr(name):
    ob = sy.holder.evaluated_get(bpy.context.evaluated_depsgraph_get())
    geo = ob.evaluated_geometry()          # keep alive while reading
    pc = geo.instances_pointcloud()
    vals = [a.value for a in pc.attributes[name].data] if pc and name in pc.attributes else None
    del pc, geo
    return vals


rnd = inst_attr("so_random")
check(rnd is not None and min(rnd) >= 0.0 and max(rnd) <= 1.0 and max(rnd) - min(rnd) > 0.9,
      "so_random on every instance, spread over 0..1")
check(max(inst_attr("so_dry")) == 0.0, "dryness 0: no dry instances")
setp(dryness=0.5, dry_patchiness=0.0)
dry = inst_attr("so_dry")
share = sum(1 for v in dry if v > 0.5) / len(dry)
check(0.3 < share < 0.6, "dryness 0.5 marks about half as dry (%.2f)" % share)
setp(dryness=0.0)
check(inst_attr("so_patch") is not None, "so_patch stored")
n1 = so.add_variation_to_material(mat)
nodes = [n for n in mat.node_tree.nodes if n.bl_idname == 'ShaderNodeGroup']
bsdf = next(n for n in mat.node_tree.nodes if n.bl_idname == 'ShaderNodeBsdfPrincipled')
check(n1 == 1 and nodes and bsdf.inputs["Base Color"].links[0].from_node == nodes[0],
      "variation group wired into Base Color")
check(so.add_variation_to_material(mat) == 0, "adding variation twice does nothing")

# ---- camera: viewport distance and fade
cam = bpy.data.objects.new("Cam", bpy.data.cameras.new("Cam"))
cam.location = (0, -20, 30)
cam.rotation_euler = (0, 0, 0)       # straight down
scene.collection.objects.link(cam)
scene.camera = cam
setp(frustum=False, occlusion=False, thin_start=1000.0, max_distance=1000.0,
     lod_distances=(36.0, 1000.0, 1000.0))
so.scatter_camera_sync(scene, force=True)
inst = instances()
check(any(n.endswith("LOD1") for n, _m in inst) and any(n.endswith("LOD0") for n, _m in inst),
      "with a camera, LOD follows distance again")
setp(viewport_distance=40.0)
d = [(p - cam.location).length for p in positions()]
check(d and max(d) <= 40.0 + 1e-3, "viewport distance limits the viewport (%.1f m)" % max(d))
setp(viewport_distance=0.0, max_distance=45.0, fade=10.0)
d = [(p - cam.location).length for p in positions()]
band_near = sum(1 for v in d if 30 < v <= 35)
band_far = sum(1 for v in d if 40 < v <= 45)
check(max(d) <= 45.0 + 1e-3 and band_far < band_near * 0.6,
      "fade thins the last 10 m (%d vs %d)" % (band_near, band_far))
setp(fade=0.0, max_distance=1000.0)

# ---- hide LODs / assets, LOD colour preview
inst = instances()
n_all = len(inst)
n_l0 = sum(1 for n, _m in inst if n.endswith("LOD0"))
setp(lod_show=(True, False, True, True))
inst = instances()
check(inst and not any(n.endswith("LOD1") for n, _m in inst) and len(inst) == n_l0,
      "hiding LOD1 removes only the LOD1 instances (%d of %d left)" % (len(inst), n_all))
setp(lod_show=(True, True, True, True))
sy.assets[0].show = False
check(len(instances()) == 0, "hiding the asset removes its instances")
sy.assets[0].show = True
check(len(instances()) == n_all, "showing it again restores them")


def inst_mats():
    dg = bpy.context.evaluated_depsgraph_get()
    dg.update()
    out = []
    for i in dg.object_instances:
        if i.is_instance and i.parent and i.parent.original == sy.holder:
            data = i.object.data           # the material the faces actually use
            m = data.materials[data.polygons[0].material_index] if data.materials else None
            out.append(m.name if m else "")
    return out


check(all(m == mat.name for m in inst_mats()), "without LOD colours, instances use their own material")
setp(lod_colors=True)
ms = inst_mats()
check(len(ms) == n_all and set(ms) == {"SO LOD0", "SO LOD1"},
      "LOD colours: same instances, coloured per LOD (%s)" % sorted(set(ms)))
check(all(abs(a - b) < 1e-4 for a, b in zip(bpy.data.materials["SO LOD1"].diffuse_color, so.LOD_COLORS[1])),
      "LOD colour shows in Solid shading (viewport display colour)")
with bpy.context.temp_override(scene=scene, view_layer=vl):
    st = so.scatter_stats(bpy.context, sy)
check(st and st["n"] == n_all and sy.lod_colors, "Count works with LOD colours on (%s)" % (st and st["n"]))
check("lod_colors" not in so.system_settings(sy) and "lod_show" not in so.system_settings(sy),
      "view toggles are not saved in presets")
setp(lod_colors=False)
ng = sy.holder.modifiers["SO Scatter"].node_group
ng.interface.remove(next(it for it in ng.interface.items_tree if it.name == "LOD Colors"))
so.scatter_sync(scene, sy)
check(any(it.name == "LOD Colors" for it in ng.interface.items_tree) and len(instances()) == n_all,
      "a tree from an older version is rebuilt with the new inputs")

# ---- system management
setp(density=1.0)
n1 = len(instances())
with bpy.context.temp_override(scene=scene, view_layer=vl):
    bpy.ops.scene_optimizer.scatter_duplicate()
check(len(s.scatter_systems) == 2 and s.scatter_index == 1, "duplicate adds a system after the original")
dup = s.scatter_systems[1]
check(dup.name == sy.name + " Copy" and dup.seed == sy.seed + 1 and abs(dup.density - 1.0) < 1e-6
      and [a.lod0 for a in dup.assets] == [a.lod0 for a in sy.assets] and dup.surface == ground,
      "duplicate copies settings, assets and surface, with a new seed")
n2 = len(instances(dup))
check(abs(n2 - n1) < n1 * 0.15 and n2 > 0, "duplicate scatters its own instances (%d vs %d)" % (n2, n1))
dup_name = dup.name
with bpy.context.temp_override(scene=scene, view_layer=vl):
    bpy.ops.scene_optimizer.scatter_move(direction='UP')
check(s.scatter_systems[0].name == dup_name and s.scatter_index == 0, "move up reorders")
dup = s.scatter_systems[0]
sy = s.scatter_systems[1]
dup.show_viewport = False
check(len(instances(dup)) == 0 and not dup.holder.modifiers["SO Scatter"].show_viewport,
      "viewport toggle hides a system")
dup.show_viewport = True
dup.show_render = False
check(not dup.holder.modifiers["SO Scatter"].show_render, "render toggle reaches the modifier")
s.scatter_index = 0
with bpy.context.temp_override(scene=scene, view_layer=vl):
    bpy.ops.scene_optimizer.scatter_remove()
sy = s.scatter_systems[0]

# ---- presets
lib = tempfile.mkdtemp(prefix="so_presets_")
s.scatter_library = lib
setp(density=2.0, patch_amount=0.4, dryness=0.3, lean_angle=math.radians(10.0))
stem = so.save_scatter_preset(scene, [sy], "Test Meadow", thumbnail=True)
for ext in (".json", ".blend", ".png"):
    check(os.path.exists(os.path.join(lib, stem + ext)), "preset writes %s" % ext)
items = so.scatter_preset_items(None, bpy.context)
check(any(it[0] == stem and it[1] == "Test Meadow" for it in items), "preset shows in the browser")

n_obj = len(bpy.data.objects)
made = so.add_scatter_preset(scene, stem, ground)
new = s.scatter_systems[s.scatter_index]
check(made == [new.name] and len(s.scatter_systems) == 2, "preset adds a system: %s" % made)
check(abs(new.patch_amount - 0.4) < 1e-6 and abs(new.lean_angle - math.radians(10.0)) < 1e-6
      and abs(new.density - 2.0) < 1e-6, "preset restores the settings")
srcs = [new.assets[0].lod0, new.assets[0].lod1]
check(all(o is not None and o not in lods and o.get("so_preset_source") for o in srcs),
      "preset assets are appended from the library .blend (%s)" % [o.name for o in srcs if o])
check(srcs[0].data.materials and srcs[0].data.materials[0] is not None, "appended asset keeps its material")
check(len(instances(new)) > 0, "preset system scatters")
added = len(bpy.data.objects) - n_obj
so.add_scatter_preset(scene, stem, ground)
check(len(bpy.data.objects) - n_obj == added + 1, "adding the preset again reuses the appended assets")

# combined preset: every system in one file
stem2 = so.save_scatter_preset(scene, list(s.scatter_systems), "Whole Field", thumbnail=False)
before = len(s.scatter_systems)
made = so.add_scatter_preset(scene, stem2, ground)
check(len(made) == before and len(s.scatter_systems) == 2 * before,
      "combined preset adds all its systems (%d)" % len(made))
check(so.delete_scatter_preset(scene, stem) == 3 and not os.path.exists(os.path.join(lib, stem + ".json")),
      "delete preset removes its files")
shutil.rmtree(lib, ignore_errors=True)

print("\nSCATTER TOOLS TEST", "FAILED (%d)" % len(failures) if failures else "PASSED",
      "Blender", bpy.app.version_string)
sys.stdout.flush()
os._exit(1 if failures else 0)
