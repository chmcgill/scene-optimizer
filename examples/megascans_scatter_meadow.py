"""
Scene Optimizer - Megascans meadow with SCATTER SYSTEMS (Geometry Nodes)
=======================================================================

The same rolling meadow as megascans_grass_meadow.py, but made the way you
would make it by hand with the add-on's Scatter Systems panel:

  1. import the Megascans grass LODs as ordinary objects (*_LOD0, *_LOD1 ...)
  2. paint a "grass" vertex group on the terrain (here: generated)
  3. Scatter Systems > + : one system per look, each with its assets/LODs
       "Meadow Grass"  Tufted Grass + Grass, dense
       "Wild Grass"    Wild Grass clumps, sparse and taller
  4. shape the look: noise patches (clumps and bare spots), random tilt,
     dry grass in patches (per-instance color via "Add Variation to
     Materials"), and a footpath curve in the Avoid collection that keeps a
     trail clear through the meadow
  5. every instance picks its LOD by camera distance, far instances are
     thinned out and faded, and instances outside the frame or behind the
     hills are removed - per instance, inside Geometry Nodes, on every
     evaluation
  6. both systems are saved to a preset library as one combined preset
     (settings + asset .blend + rendered thumbnail)

Usage:
    blender -b --python examples/megascans_scatter_meadow.py -- \
        <wild_grass zip/folder> <grass ...> <tufted_grass ...> [--save f.blend] [--render-dir out]
"""

import importlib.util
import math
import os
import sys
import time

import bpy
from mathutils import Vector

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("meadow_lib", os.path.join(HERE, "megascans_grass_meadow.py"))
lib = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lib)          # helpers only; its main() is guarded

SCENE_NAME = "Megascans Scatter Meadow"
FIELD_X, FIELD_Y = (-130.0, 130.0), (-30.0, 190.0)
LOD_COLORS = ((0.9, 0.15, 0.1, 1), (1.0, 0.8, 0.1, 1), (0.15, 0.45, 1.0, 1), (0.6, 0.2, 0.9, 1))


def log(msg):
    print("[scatter meadow] " + msg)
    sys.stdout.flush()


def paint_grass_mask(terrain):
    """Vertex group 'grass': 1 inside the field, fading to 0 over 10 m."""
    vg = terrain.vertex_groups.new(name="grass")
    for v in terrain.data.vertices:
        x, y = v.co.x, v.co.y
        dx = max(FIELD_X[0] - x, x - FIELD_X[1], 0.0)
        dy = max(FIELD_Y[0] - y, y - FIELD_Y[1], 0.0)
        w = max(0.0, 1.0 - max(dx, dy) / 10.0)
        if w > 0.0:
            vg.add([v.index], w, 'REPLACE')


def make_sources(variations, coll):
    """Megascans LOD meshes -> plain objects named <variation>_LOD<n>."""
    by_asset = {}
    for var in variations:
        objs = []
        for level, me in enumerate(var["lods"]):
            o = bpy.data.objects.new("%s_LOD%d" % (var["name"], level), me)
            o.location = (0, 0, -500)
            o.color = LOD_COLORS[level]          # used by the top-down debug view
            coll.objects.link(o)
            objs.append(o)
        by_asset.setdefault(var["asset"], []).extend(objs)
    return by_asset


# the camera walks along this footpath (see megascans_grass_meadow.build_camera)
PATH_POINTS = ((-8.0, -40.0), (-7.0, -22.0), (-4.0, -6.0), (0.0, 8.0), (4.0, 26.0), (5.0, 44.0),
               (1.0, 62.0), (-6.0, 84.0), (-4.0, 112.0), (3.0, 150.0))
PATH_WIDTH = 1.3


def make_path(scene):
    """A footpath curve hugging the terrain (Catmull-Rom through PATH_POINTS)."""
    pts = [Vector(p) for p in PATH_POINTS]
    samples = []
    for i in range(len(pts) - 1):
        p0, p1, p2, p3 = pts[max(i - 1, 0)], pts[i], pts[i + 1], pts[min(i + 2, len(pts) - 1)]
        for k in range(12):
            t = k / 12.0
            q = 0.5 * ((2 * p1) + (-p0 + p2) * t + (2 * p0 - 5 * p1 + 4 * p2 - p3) * t * t
                       + (-p0 + 3 * p1 - 3 * p2 + p3) * t * t * t)
            samples.append(q)
    samples.append(pts[-1])
    cu = bpy.data.curves.new("Footpath", 'CURVE')
    cu.dimensions = '3D'
    spl = cu.splines.new('POLY')
    spl.points.add(len(samples) - 1)
    for sp, q in zip(spl.points, samples):
        sp.co = (q.x, q.y, lib.height(q.x, q.y), 1.0)
    ob = bpy.data.objects.new("Footpath", cu)
    coll = bpy.data.collections.new("Keep Clear")
    scene.collection.children.link(coll)
    coll.objects.link(ob)
    scene.collection.objects.link(path_ribbon(samples))
    return coll


def path_ribbon(samples):
    """Visible dirt strip under the footpath curve, hugging the terrain, with
    soft edges (alpha from the strip's U coordinate)."""
    pts = []
    for a, b in zip(samples, samples[1:]):
        n = max(1, int((b - a).length / 0.5))
        pts += [a.lerp(b, k / n) for k in range(n)]
    pts.append(samples[-1])
    across = (-1.0, -0.5, 0.0, 0.5, 1.0)
    verts, faces, us = [], [], []
    w = len(across)
    for i, p in enumerate(pts):
        d = (pts[min(i + 1, len(pts) - 1)] - pts[max(i - 1, 0)]).normalized()
        side = Vector((-d.y, d.x)) * (PATH_WIDTH * 0.75)
        for k, f in enumerate(across):
            q = p + side * f
            verts.append((q.x, q.y, lib.height(q.x, q.y) + 0.04))
            us.append((0.5 + 0.5 * f, i * 0.5))
            if i and k:
                a0 = (i - 1) * w + k - 1
                faces.append((a0, a0 + 1, a0 + w + 1, a0 + w))
    me = bpy.data.meshes.new("Footpath_Dirt")
    me.from_pydata(verts, [], faces)
    uv = me.uv_layers.new(name="UVMap")
    for loop in me.loops:
        uv.data[loop.index].uv = us[loop.vertex_index]
    me.shade_smooth() if hasattr(me, "shade_smooth") else None
    ob = bpy.data.objects.new("Footpath_Dirt", me)
    ob.so_never_cull = True
    mat = bpy.data.materials.new("Footpath_Dirt")
    if bpy.app.version < (5, 0, 0) or mat.node_tree is None:
        mat.use_nodes = True
    if hasattr(mat, "blend_method"):
        try:
            mat.blend_method = 'HASHED'
        except TypeError:
            pass
    nt = mat.node_tree
    bsdf = nt.nodes.get("Principled BSDF")
    tc = nt.nodes.new("ShaderNodeTexCoord")
    nz = nt.nodes.new("ShaderNodeTexNoise")
    nz.inputs["Scale"].default_value = 0.9
    nz.inputs["Detail"].default_value = 12.0
    nt.links.new(tc.outputs["Object"], nz.inputs["Vector"])
    ramp = nt.nodes.new("ShaderNodeValToRGB")
    ramp.color_ramp.elements[0].color = (0.035, 0.022, 0.011, 1)
    ramp.color_ramp.elements[1].color = (0.11, 0.068, 0.032, 1)
    nt.links.new(nz.outputs["Fac"], ramp.inputs["Fac"])
    nt.links.new(ramp.outputs["Color"], bsdf.inputs["Base Color"])
    bump = nt.nodes.new("ShaderNodeBump")
    bump.inputs["Strength"].default_value = 0.6
    nt.links.new(nz.outputs["Fac"], bump.inputs["Height"])
    nt.links.new(bump.outputs["Normal"], bsdf.inputs["Normal"])
    # alpha: 1 in the middle, fading out over the outer third, broken up by noise
    sep = nt.nodes.new("ShaderNodeSeparateXYZ")
    nt.links.new(tc.outputs["UV"], sep.inputs["Vector"])
    edge = nt.nodes.new("ShaderNodeMath")
    edge.operation = 'SUBTRACT'
    nt.links.new(sep.outputs["X"], edge.inputs[0])
    edge.inputs[1].default_value = 0.5
    absn = nt.nodes.new("ShaderNodeMath")
    absn.operation = 'ABSOLUTE'
    nt.links.new(edge.outputs[0], absn.inputs[0])
    jit = nt.nodes.new("ShaderNodeMath")
    jit.operation = 'MULTIPLY_ADD'
    nt.links.new(nz.outputs["Fac"], jit.inputs[0])
    jit.inputs[1].default_value = 0.25
    nt.links.new(absn.outputs[0], jit.inputs[2])
    mr = nt.nodes.new("ShaderNodeMapRange")
    nt.links.new(jit.outputs[0], mr.inputs["Value"])
    mr.inputs["From Min"].default_value = 0.38
    mr.inputs["From Max"].default_value = 0.62
    mr.inputs["To Min"].default_value = 1.0
    mr.inputs["To Max"].default_value = 0.0
    nt.links.new(mr.outputs["Result"], bsdf.inputs["Alpha"])
    bsdf.inputs["Roughness"].default_value = 1.0
    if "Specular IOR Level" in bsdf.inputs:
        bsdf.inputs["Specular IOR Level"].default_value = 0.0
    me.materials.append(mat)
    return ob


def new_system(so, scene, terrain, objs, name):
    vl = scene.view_layers[0]
    for o in scene.objects:
        o.select_set(False)
    for o in objs + [terrain]:
        o.select_set(True)
    vl.objects.active = terrain
    with bpy.context.temp_override(scene=scene, view_layer=vl, active_object=terrain,
                                   selected_objects=objs + [terrain]):
        bpy.ops.scene_optimizer.scatter_add()
    s = scene.scene_optimizer
    system = s.scatter_systems[len(s.scatter_systems) - 1]
    system.name = name
    return system


def configure(so, scene, system, **kw):
    so._Runtime.busy = True
    try:
        for k, v in kw.items():
            setattr(system, k, v)
    finally:
        so._Runtime.busy = False
    so.scatter_rebuild(scene, system)


def report(so, scene, frame):
    with bpy.context.temp_override(scene=scene, view_layer=scene.view_layers[0]):
        scene.frame_set(frame)
        so.scatter_camera_sync(scene)
        lines = ["frame %d" % frame]
        tot = base = 0
        for system in scene.scene_optimizer.scatter_systems:
            st = so.scatter_stats(bpy.context, system)
            tot += st["tris"]
            base += st["base_tris"]
            lines.append("  %-13s %s of %s instances, LOD0/1/2: %s, triangles %s of %s" % (
                system.name, "{:,}".format(st["n"]), "{:,}".format(st["base_n"]),
                "/".join(str(v) for v in st["lod"][:3]),
                "{:,}".format(st["tris"]), "{:,}".format(st["base_tris"])))
        lines.append("  total triangles %s of %s (%.1f%% fewer)" % (
            "{:,}".format(tot), "{:,}".format(base), 100.0 * (1 - tot / max(base, 1))))
    for ln in lines:
        log(ln)
    return lines


def render(so, scene, out_dir, frame):
    os.makedirs(out_dir, exist_ok=True)
    r = scene.render
    r.resolution_x, r.resolution_y, r.resolution_percentage = 1280, 720, 100
    engines = {i.identifier for i in bpy.types.RenderSettings.bl_rna.properties["engine"].enum_items}
    r.engine = "BLENDER_EEVEE_NEXT" if "BLENDER_EEVEE_NEXT" in engines else "BLENDER_EEVEE"
    try:
        scene.eevee.taa_render_samples = 32
    except AttributeError:
        pass
    r.filepath = os.path.join(out_dir, "scatter_f%03d.png" % frame)
    with bpy.context.temp_override(scene=scene, view_layer=scene.view_layers[0]):
        bpy.ops.render.render(write_still=True, scene=scene.name)
    log("rendered " + r.filepath)

    # top-down debug: instances keep the SHOT camera's culling (pinned while
    # the top camera renders), coloured by LOD
    cam = scene.camera
    top = bpy.data.objects.new("TopCam", bpy.data.cameras.new("TopCam"))
    top.data.type, top.data.ortho_scale, top.data.clip_end = 'ORTHO', 280, 2000
    top.location = (0, 80, 400)
    scene.collection.objects.link(top)
    mark = bpy.data.objects.new("CamMarker", bpy.data.meshes.new("CamMarker"))
    c = cam.matrix_world.translation
    fwd = cam.matrix_world.to_3x3() @ Vector((0, 0, -1))
    fwd = Vector((fwd.x, fwd.y, 0)).normalized()
    side = Vector((-fwd.y, fwd.x, 0))
    up = Vector((0, 0, 60))
    mark.data.from_pydata([c + side * 3 + up, c - side * 3 + up, c + fwd * 9 + up], [], [(0, 1, 2)])
    mark.color = (1, 1, 1, 1)
    scene.collection.objects.link(mark)
    for o in scene.objects:
        if o.type == 'MESH' and o.name == "Terrain":
            o.color = (0.3, 0.3, 0.3, 1)
    # LOD colours: object-linked debug materials on the LOD source objects
    # (instances reference those objects, so they show the debug colour)
    dbg = []
    for lvl, col in enumerate(LOD_COLORS):
        m = bpy.data.materials.get("SO_debug_LOD%d" % lvl) or bpy.data.materials.new("SO_debug_LOD%d" % lvl)
        m.diffuse_color = col
        dbg.append(m)
    terrain_dbg = bpy.data.materials.get("SO_debug_terrain") or bpy.data.materials.new("SO_debug_terrain")
    terrain_dbg.diffuse_color = (0.3, 0.3, 0.3, 1)
    swapped = []
    for o in scene.objects:
        lvl = o.name.rsplit("_LOD", 1)[-1] if "_LOD" in o.name else None
        mat = dbg[int(lvl)] if lvl is not None and lvl.isdigit() else (
            terrain_dbg if o.name == "Terrain" else None)
        if mat is None or o.type != 'MESH':
            continue
        for slot in o.material_slots:
            swapped.append((slot, slot.link))
            slot.link = 'OBJECT'
            slot.material = mat
    so._Runtime.busy = True                       # keep scatter inputs on the shot camera
    try:
        scene.camera = top
        r.engine = 'BLENDER_WORKBENCH'
        sh = scene.display.shading
        sh.light, sh.color_type = 'STUDIO', 'MATERIAL'
        r.resolution_x = r.resolution_y = 1100
        r.filepath = os.path.join(out_dir, "scatter_f%03d_topdown.png" % frame)
        with bpy.context.temp_override(scene=scene, view_layer=scene.view_layers[0]):
            bpy.ops.render.render(write_still=True, scene=scene.name)
        log("rendered " + r.filepath)
    finally:
        scene.camera = cam
        so._Runtime.busy = False
        for slot, link in swapped:
            slot.material = None
            slot.link = link
    bpy.data.objects.remove(top)
    bpy.data.objects.remove(mark)


def main():
    args = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    paths, opts, i = [], {"save": None, "render_dir": None}, 0
    while i < len(args):
        if args[i] in ("--save", "--render-dir"):
            opts[args[i][2:].replace("-", "_")] = os.path.abspath(args[i + 1])
            i += 2
        else:
            paths.append(args[i])
            i += 1
    if not paths:
        raise SystemExit("usage: ... megascans_scatter_meadow.py -- <grass zips/folders>")
    so = lib.scene_optimizer_module()
    t0 = time.perf_counter()
    old = bpy.data.scenes.get(SCENE_NAME)
    if old:
        bpy.data.scenes.remove(old)
    scene = bpy.data.scenes.new(SCENE_NAME)
    vl = scene.view_layers[0]
    if bpy.context.window is not None:
        bpy.context.window.scene = scene
    ground = bpy.data.collections.new("Terrain")
    sources = bpy.data.collections.new("Scatter Sources (Megascans LODs)")
    scene.collection.children.link(ground)
    scene.collection.children.link(sources)

    with bpy.context.temp_override(scene=scene, view_layer=vl):
        variations = []
        for p in paths:
            variations += lib.import_grass(p)
        terrain = lib.build_terrain(ground)
        paint_grass_mask(terrain)
        by_asset = make_sources(variations, sources)
        lib.build_camera(scene)
        lib.build_world(scene)

        keep_clear = make_path(scene)
        meadow = new_system(so, scene, terrain,
                            by_asset.get("tufted_grass", []) + by_asset.get("grass", []), "Meadow Grass")
        for a in meadow.assets:                    # Tufted Grass dominates
            a.weight = 1.4 if a.name.startswith("tufted") else 1.0
        configure(so, scene, meadow, density=13.0, density_mask="grass", scale_min=0.85,
                  scale_max=1.35, lod_distances=(10.0, 30.0, 60.0), thin_start=30.0,
                  thin_keep=0.3, max_distance=140.0, fade=20.0, margin=8.0,
                  patch_size=9.0, patch_amount=0.25, patch_scale=0.5,
                  random_tilt=math.radians(8.0), lean_angle=math.radians(4.0),
                  lean_direction=math.radians(60.0), dryness=0.25, dry_patchiness=0.75,
                  avoid=keep_clear, avoid_distance=0.75, avoid_blur=0.8)
        wild = new_system(so, scene, terrain, by_asset.get("wild_grass", []), "Wild Grass")
        configure(so, scene, wild, density=1.2, density_mask="grass", seed=99, scale_min=0.9,
                  scale_max=1.4, lod_distances=(8.0, 25.0, 60.0), thin_start=40.0,
                  thin_keep=0.35, max_distance=140.0, fade=20.0, margin=8.0,
                  patch_size=14.0, patch_amount=0.6, random_tilt=math.radians(6.0),
                  dryness=0.45, dry_patchiness=0.6,
                  avoid=keep_clear, avoid_distance=1.0, avoid_blur=1.2)
        mats = {sl.material for sy in (meadow, wild) for _w, objs in so.scatter_assets(sy)
                for o in objs for sl in o.material_slots if sl.material}
        wired = sum(so.add_variation_to_material(m) for m in mats)
        log("color variation wired into %d inputs of %d materials" % (wired, len(mats)))
        with bpy.context.temp_override(scene=scene, view_layer=vl):
            bpy.ops.scene_optimizer.scatter_hide_sources()
        log("built in %.1f s: %d assets in 2 systems" % (
            time.perf_counter() - t0, len(meadow.assets) + len(wild.assets)))

    lines = []
    for f in (1, 120, 240):
        lines += report(so, scene, f)
        if opts["render_dir"]:
            render(so, scene, opts["render_dir"], f)
    with bpy.context.temp_override(scene=scene, view_layer=vl):
        scene.frame_set(1)
    if opts["render_dir"]:
        so.settings_of(scene).scatter_library = os.path.join(opts["render_dir"], "library")
        stem = so.save_scatter_preset(scene, list(scene.scene_optimizer.scatter_systems),
                                      "Megascans Meadow", thumbnail=True)
        log("saved combined preset '%s' to %s" % (stem, so.library_dir(scene)))
    txt = bpy.data.texts.new("Scatter meadow report")
    txt.from_string("\n".join(lines) + "\n")
    if opts["save"]:
        bpy.ops.wm.save_as_mainfile(filepath=opts["save"])
        log("saved " + opts["save"])
    log("total %.1f s" % (time.perf_counter() - t0))


if __name__ == "__main__":
    main()
