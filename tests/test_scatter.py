"""Scatter systems (Geometry Nodes per-instance LOD + culling).
    blender -b --factory-startup --python tests/test_scatter.py
"""
import importlib.util
import math
import os
import sys

import bmesh
import bpy
from mathutils import Vector

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
scene.render.resolution_x, scene.render.resolution_y = 1280, 720


def ridge(x, y):
    # wall-like hill at y=40, flat valley behind it, then a slope rising
    # from y=80 that the camera sees again over the ridge
    return 6.0 * math.exp(-((y - 40.0) / 5.0) ** 2) + max(0.0, y - 80.0) * 0.6


# terrain 120 x 160 m, camera at y=0 looking +y; ridge at 40 m hides 45..70 m
me = bpy.data.meshes.new("Terrain")
bm = bmesh.new()
bmesh.ops.create_grid(bm, x_segments=120, y_segments=160, size=1.0)
for v in bm.verts:
    v.co.x *= 60.0
    v.co.y = v.co.y * 80.0 + 60.0
    v.co.z = ridge(v.co.x, v.co.y)
bm.to_mesh(me)
bm.free()
terrain = bpy.data.objects.new("Terrain", me)
scene.collection.objects.link(terrain)


def cone(name, segs, height=0.6):
    m = bpy.data.meshes.new(name)
    b = bmesh.new()
    bmesh.ops.create_cone(b, cap_ends=True, segments=segs, radius1=0.15, radius2=0.0, depth=height)
    bmesh.ops.translate(b, verts=b.verts, vec=(0, 0, height / 2))
    b.to_mesh(m)
    b.free()
    o = bpy.data.objects.new(name, m)
    o.location = (500, 500, 0)          # source objects live far away
    scene.collection.objects.link(o)
    return o


tuft = [cone("Tuft_LOD%d" % i, s) for i, s in enumerate((64, 16, 6))]
rock = [cone("Rock_LOD%d" % i, s, 0.3) for i, s in enumerate((48, 8))]
for o in tuft[1:] + rock[1:]:
    o.scale = (1, 1, 1)
tuft[0].scale = (1.0, 1.0, 1.0)

cam = bpy.data.objects.new("Cam", bpy.data.cameras.new("Cam"))
cam.location = (0, 0, 1.7)
cam.rotation_euler = (math.radians(90), 0, 0)
cam.data.lens = 35
cam.data.clip_end = 1000
scene.collection.objects.link(cam)
scene.camera = cam

# ---- create via the operator: select assets, surface active
for o in scene.objects:
    o.select_set(o in tuft + rock + [terrain])
vl.objects.active = terrain
with bpy.context.temp_override(scene=scene, view_layer=vl, active_object=terrain,
                               selected_objects=tuft + rock + [terrain]):
    res = bpy.ops.scene_optimizer.scatter_add()
s = scene.scene_optimizer
check(res == {'FINISHED'} and len(s.scatter_systems) == 1, "scatter_add creates a system")
sy = s.scatter_systems[0]
check([a.name for a in sy.assets] == ["Tuft", "Rock"] or sorted(a.name for a in sy.assets) == ["Rock", "Tuft"],
      "LOD-named selection grouped into 2 assets: %s" % [a.name for a in sy.assets])
ta = next(a for a in sy.assets if a.name == "Tuft")
check(ta.lod0 == tuft[0] and ta.lod1 == tuft[1] and ta.lod2 == tuft[2] and ta.lod3 is None,
      "Tuft asset has LOD0..2 in order")

so._Runtime.busy = True
sy.density = 2.0
sy.lod_distances = (8.0, 20.0, 1000.0)
sy.max_distance = 110.0
sy.thin_start = 1000.0     # no thinning for exact checks
sy.margin = 0.0
so._Runtime.busy = False
so.scatter_sync(scene, sy)
so.scatter_camera_sync(scene, force=True)

lod_of = {}
for a in sy.assets:
    for i in range(4):
        o = getattr(a, "lod%d" % i)
        if o:
            lod_of[o.name] = i


def instances(with_scale=False):
    dg = bpy.context.evaluated_depsgraph_get()
    dg.update()
    out = []
    for inst in dg.object_instances:
        if inst.is_instance and inst.parent and inst.parent.original == sy.holder:
            item = (inst.object.original.name, inst.matrix_world.translation.copy())
            out.append(item + (inst.matrix_world.to_scale().z,) if with_scale else item)
    return out


inst = instances()
check(len(inst) > 200, "instances generated: %d" % len(inst))

# LOD by distance
cam_pos = cam.matrix_world.translation
bad_lod = [(n, round((p - cam_pos).length, 1)) for n, p in inst
           if lod_of[n] != min(sum((p - cam_pos).length > d for d in (8.0, 20.0)),
                               (2 if n.startswith("Tuft") else 1))]
check(not bad_lod, "every instance uses the LOD its distance calls for %s" % bad_lod[:3])
levels = {lod_of[n] for n, _p in inst}
check({0, 1, 2} <= levels, "near/mid/far LODs all present: %s" % sorted(levels))

# frustum: nothing behind the camera / outside the frame
cam_inv = cam.matrix_world.inverted()
fr = cam.data.view_frame(scene=scene)
tx = max(abs(v.x) for v in fr) / -fr[0].z
ty = max(abs(v.y) for v in fr) / -fr[0].z
outside = []
for n, p in inst:
    c = cam_inv @ p
    depth = -c.z
    if depth < -2.0 or abs(c.x) > depth * tx + 2.0 or abs(c.y) > depth * ty + 2.0:
        outside.append((n, tuple(round(v, 1) for v in p)))
check(not outside, "no instance outside the camera frustum %s" % outside[:3])
check(not any(p.y < -1.0 for _n, p in inst), "no instance behind the camera")

# occlusion: nothing hidden behind the ridge, nothing beyond max distance
deps = bpy.context.evaluated_depsgraph_get()
hidden_kept = []
for n, p, sc in instances(with_scale=True):
    top = p + Vector((0, 0, 0.6 * sc))     # tallest asset is 0.6 m at scale 1
    d = cam_pos - top
    hit, loc, _nor, _i, ob, _m = scene.ray_cast(deps, top + d.normalized() * 0.01, d.normalized(),
                                                distance=d.length - 0.1)
    if hit and ob is not None and ob.original == terrain:
        hidden_kept.append((n, tuple(round(v, 1) for v in p)))
check(not hidden_kept, "no kept instance is hidden behind the ridge %s" % hidden_kept[:3])
check(not any((p - cam_pos).length > 111.0 for _n, p in inst), "max distance respected")
behind_ridge = [p for _n, p in inst if 47.0 < p.y < 70.0 and abs(p.x) < 20]
check(len(behind_ridge) == 0, "valley behind the ridge is empty (%d)" % len(behind_ridge))
beyond = [p for _n, p in inst if p.y > 85.0]
check(len(beyond) > 0, "far slope visible above the ridge is still populated (%d)" % len(beyond))

# toggles
sy.occlusion = False
n_no_occl = len(instances())
sy.occlusion = True
sy.frustum = False
n_no_frustum = len(instances())
sy.frustum = True
check(n_no_occl > len(inst) and n_no_frustum > len(inst),
      "turning culling off adds instances (%d -> %d / %d)" % (len(inst), n_no_occl, n_no_frustum))
sy.cull_in = 'RENDER'
n_vp = len(instances())
sy.cull_in = 'BOTH'
check(n_vp > len(inst), "Cull In = Render leaves the viewport unculled (%d)" % n_vp)

# thinning
sy.thin_start = 20.0
sy.thin_keep = 0.2
n_thin = len(instances())
check(n_thin < len(inst), "distance thinning drops far instances (%d -> %d)" % (len(inst), n_thin))
sy.thin_start = 1000.0
sy.thin_keep = 0.3

# camera lens change is picked up by the depsgraph handler
key = next(it.identifier for it in sy.holder.modifiers["SO Scatter"].node_group.interface.items_tree
           if getattr(it, "name", "") == "Tan Right")
before = sy.holder.modifiers["SO Scatter"][key]
cam.data.lens = 85
bpy.context.evaluated_depsgraph_get().update()
so.scatter_camera_sync(scene)       # what the handler calls
after = sy.holder.modifiers["SO Scatter"][key]
check(after < before * 0.6, "lens change updates frustum tangents (%.3f -> %.3f)" % (before, after))
n_tele = len(instances())
check(n_tele < len(inst), "telephoto lens culls more (%d -> %d)" % (len(inst), n_tele))
cam.data.lens = 35
so.scatter_camera_sync(scene)

# hidden source objects still instance
with bpy.context.temp_override(scene=scene, view_layer=vl):
    bpy.ops.scene_optimizer.scatter_hide_sources()
n_hidden = len(instances())
check(all(o.hide_render for o in tuft + rock), "sources hidden from render")
check(abs(n_hidden - len(inst)) <= 2, "hidden sources still instanced (%d vs %d)" % (n_hidden, len(inst)))

# stats
with bpy.context.temp_override(scene=scene, view_layer=vl):
    r = bpy.ops.scene_optimizer.scatter_stats()
check(r == {'FINISHED'} and "fewer" in sy.stats, "stats: " + sy.stats)
check(sy.cull_in == 'BOTH' and abs(sy.viewport_density - 100.0) < 1e-6, "stats restore settings")

# second system on the same surface
for o in scene.objects:
    o.select_set(o in rock + [terrain])
with bpy.context.temp_override(scene=scene, view_layer=vl, active_object=terrain,
                               selected_objects=rock + [terrain]):
    bpy.ops.scene_optimizer.scatter_add()
check(len(s.scatter_systems) == 2 and s.scatter_systems[1].holder != sy.holder,
      "second independent system")
holders = [x.holder.name for x in s.scatter_systems]
s.scatter_index = 1
with bpy.context.temp_override(scene=scene, view_layer=vl):
    bpy.ops.scene_optimizer.scatter_remove()
check(len(s.scatter_systems) == 1 and holders[1] not in bpy.data.objects
      and "SO_Scatter_Scatter 2" not in bpy.data.node_groups, "remove deletes holder + node group")

# object-level culling ignores the holder (instances are handled inside GN)
check(sy.holder.so_never_cull and sy.holder.so_not_occluder, "holder flagged never-cull / not-occluder")

print("\nSCATTER TEST", "FAILED (%d)" % len(failures) if failures else "PASSED", "Blender", bpy.app.version_string)
sys.stdout.flush()
os._exit(1 if failures else 0)
