"""Validates examples/quixel_lod_example.py against the stand-in Megascans assets.
    blender -b --factory-startup --python tests/make_fake_megascans.py -- <dir>
    blender -b --factory-startup --python tests/test_quixel_example.py -- <dir>
"""
import importlib.util
import os
import sys

import bpy

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAKE = os.path.abspath(sys.argv[sys.argv.index("--") + 1])
spec = importlib.util.spec_from_file_location("scene_optimizer", os.path.join(ROOT, "scene_optimizer.py"))
so = importlib.util.module_from_spec(spec)
sys.modules["scene_optimizer"] = so
spec.loader.exec_module(so)
so.register()

sys.argv = ["blender", "--",
            os.path.join(FAKE, "3d", "rock_sandstone_tkmfdcbfa"),
            os.path.join(FAKE, "3dplant", "plant_fern_ulruch3ia.zip"),
            "--copies", "30"]
exec(compile(open(os.path.join(ROOT, "examples", "quixel_lod_example.py")).read(),
             "quixel_lod_example.py", "exec"))

failures = []


def check(cond, msg):
    print(("ok    " if cond else "FAIL  ") + msg)
    if not cond:
        failures.append(msg)


scene = bpy.data.scenes["Megascans LOD Example"]
s = scene.scene_optimizer
check(len(s.lod_groups) == 90, "90 LOD groups (3 variations x 30 copies): %d" % len(s.lod_groups))
check(all(len(g.variants) in (4, 6) for g in s.lod_groups), "rock groups have 6 LODs, fern 4")


def state(frame):
    with bpy.context.temp_override(scene=scene, view_layer=scene.view_layers[0]):
        scene.frame_set(frame)
    cam_pos = scene.camera.matrix_world.translation
    rows = []
    for g in s.lod_groups:
        objs = so.lod_group_objects(scene, g)
        vis = [i for i, o in enumerate(objs) if not o.hide_render and not o.hide_viewport]
        culled = any(so.R_FRUSTUM in so.Visibility.reasons(o) for o in objs)
        d, _ = so.lod_distance(objs, cam_pos, s.lod_distance_mode, scene.view_layers[0])
        th = [g.distances[i] for i in range(len(objs) - 1)]
        rows.append((g.name, d, vis, culled, th))
    return rows


def level_errors(rows, hysteresis):
    """Each group's level must equal the level its own thresholds give, except
    inside the live-mode hysteresis band around a threshold."""
    bad = []
    for name, d, vis, culled, th in rows:
        if not vis:
            continue
        expected = sum(1 for t in th if d >= t)
        near = any(abs(d - t) <= t * hysteresis + 1e-6 for t in th)
        if vis[0] != expected and not near:
            bad.append((name, round(d, 2), vis[0], expected))
    return bad


for frame in (1, 200):  # camera at ~25% of field depth at 200
    rows = state(frame)
    check(all(len(r[2]) <= 1 for r in rows), "frame %d: never more than one variant visible" % frame)
    check(all(len(r[2]) == 1 for r in rows if not r[3]), "frame %d: every non-culled group shows one LOD" % frame)
    bad = level_errors(rows, s.lod_hysteresis / 100.0)
    check(not bad, "frame %d: every group shows the level its distance calls for %s" % (frame, bad[:3]))
    print("      frame %d levels: %s" % (frame, sorted({r[2][0] for r in rows if r[2]})))

lv1 = {r[0]: tuple(r[2]) for r in state(1)}
lv200 = {r[0]: tuple(r[2]) for r in state(200)}
check(sum(lv1[n] != lv200[n] for n in lv1) > 20, "scrubbing to frame 200 switched many LODs (live frame handler)")

# materials + shared meshes
lod_meshes = {o.data for o in scene.objects if o.type == 'MESH' and "_LOD" in o.name}
check(len(lod_meshes) == 6 + 4 + 4, "copies share mesh data (%d unique LOD meshes)" % len(lod_meshes))
for me in lod_meshes:
    mats = [m for m in me.materials if m]
    ok = mats and mats[0].name.startswith("MS_") and all(
        n.image is not None and n.image.size[0] > 0
        for n in mats[0].node_tree.nodes if n.type == 'TEX_IMAGE')
    if not ok:
        check(False, "material on %s" % me.name)
check(True, "all LOD meshes have Megascans materials with loaded images")
rock_mats = {m.name for me in lod_meshes for m in me.materials if "rock" in m.name}
check(len(rock_mats) == 6, "rock uses per-LOD normal maps (%d materials)" % len(rock_mats))
fern = bpy.data.materials.get("MS_plant_fern")
check(fern is not None and fern.node_tree.nodes["Principled BSDF"].inputs["Alpha"].is_linked,
      "fern opacity map wired to Alpha")
check(bpy.data.scenes.get("Scene") is not None and len(bpy.data.scenes["Scene"].objects) == 3,
      "user's original scene untouched")

print("\nQUIXEL EXAMPLE TEST", "FAILED (%d)" % len(failures) if failures else "PASSED",
      "Blender", bpy.app.version_string)
sys.stdout.flush()
os._exit(1 if failures else 0)
