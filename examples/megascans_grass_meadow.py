"""
Scene Optimizer - Megascans grass meadow
========================================

A rolling meadow covered in real Quixel Megascans grass (Fab "high" FBX zips),
set up the way a game engine would draw it:

  * grass is grouped into 4 m patches; every patch is one LOD group
      LOD0  every clump at its full-detail mesh            (close to camera)
      LOD1  every clump at the Megascans LOD1 mesh         (mid distance)
      LOD2  half the clumps, lowest mesh LOD, scaled up    (far: density thinning)
      > max draw distance: patch hidden (the ground shader carries the colour)
  * frustum culling hides patches outside the camera frame
  * occlusion culling hides patches behind hills (only the terrain occludes;
    grass is alpha-tested and never blocks anything)

The Megascans billboard LOD (last LOD, a single flat card) is skipped: it is
made for engines that rotate it towards the camera, and seen edge-on in
Blender it disappears.

Usage (command line):
    blender -b --python examples/megascans_grass_meadow.py -- <grass zip or folder> [...]
            [--seed 3] [--save meadow.blend] [--render-dir out_dir]

Requires the Scene Optimizer add-on (installed, or scene_optimizer.py next to
the examples folder).
"""

import math
import os
import random
import re
import sys
import time
import zipfile

import bpy
from mathutils import Matrix, Vector, noise

# --------------------------------------------------------------------- settings
PATCH_SIZE = 4.0            # m, one LOD group
CLUMPS_PER_PATCH = 150
PATCH_PROTOTYPES = 12       # distinct patch layouts (meshes are shared)
FIELD_X = (-120.0, 120.0)   # grass area (camera starts near y = 0 looking +y)
FIELD_Y = (-24.0, 180.0)
TERRAIN_HALF = 400.0        # terrain extends well beyond the grass
LOD_DISTANCES = (10.0, 30.0)  # LOD1 from 10 m, LOD2 from 30 m
MAX_DRAW = 140.0            # patches beyond this are hidden
FAR_KEEP = 0.3              # fraction of clumps kept at LOD2
SCENE_NAME = "Megascans Grass Meadow"
FILE_RE = re.compile(r"_Var([A-Z0-9]+)_LOD(\d+)\.fbx$", re.IGNORECASE)


def log(msg):
    print("[meadow] " + msg)
    sys.stdout.flush()


def scene_optimizer_module():
    if not hasattr(bpy.types.Scene, "scene_optimizer"):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        path = os.path.join(here, "scene_optimizer.py")
        if os.path.isfile(path):
            import importlib.util
            spec = importlib.util.spec_from_file_location("scene_optimizer", path)
            mod = importlib.util.module_from_spec(spec)
            sys.modules["scene_optimizer"] = mod
            spec.loader.exec_module(mod)
            mod.register()
        else:
            import addon_utils
            addon_utils.enable("scene_optimizer", default_set=True)
    return sys.modules["scene_optimizer"]


# --------------------------------------------------------------------- terrain

def height(x, y):
    p = Vector((x, y, 0.0))
    h = 4.0 * noise.noise(p / 70.0 + Vector((3.1, 7.7, 0)))
    h += 1.6 * noise.noise(p / 26.0 + Vector((11.0, 2.0, 0)))
    h += 0.35 * noise.noise(p / 7.0)
    # rolling ridges across the view: each one hides the valley behind it
    h += 5.5 * math.exp(-((y - 30.0 - 6.0 * math.sin(x / 31.0)) / 8.0) ** 2)
    h += 9.0 * math.exp(-((y - 82.0 - 9.0 * math.sin(x / 43.0 + 1.0)) / 13.0) ** 2)
    h += 14.0 * math.exp(-((y - 150.0) / 22.0) ** 2)
    return h


def terrain_normal(x, y, e=0.75):
    dx = height(x + e, y) - height(x - e, y)
    dy = height(x, y + e) - height(x, y - e)
    return Vector((-dx, -dy, 2 * e)).normalized()


def build_terrain(coll):
    n = 320
    step = 2 * TERRAIN_HALF / n
    verts, faces = [], []
    for j in range(n + 1):
        for i in range(n + 1):
            x, y = -TERRAIN_HALF + i * step, -TERRAIN_HALF + j * step + 100.0
            verts.append((x, y, height(x, y)))
    for j in range(n):
        for i in range(n):
            a = j * (n + 1) + i
            faces.append((a, a + 1, a + n + 2, a + n + 1))
    me = bpy.data.meshes.new("Terrain")
    me.from_pydata(verts, [], faces)
    me.shade_smooth() if hasattr(me, "shade_smooth") else None
    ob = bpy.data.objects.new("Terrain", me)
    coll.objects.link(ob)
    ob.so_never_cull = True

    mat = bpy.data.materials.new("Meadow_Ground")
    if bpy.app.version < (5, 0, 0) or mat.node_tree is None:
        mat.use_nodes = True
    nt = mat.node_tree
    bsdf = nt.nodes.get("Principled BSDF")
    tc = nt.nodes.new("ShaderNodeTexCoord")
    nz = nt.nodes.new("ShaderNodeTexNoise")
    nz.inputs["Scale"].default_value = 0.08
    nz.inputs["Detail"].default_value = 8.0
    nt.links.new(tc.outputs["Object"], nz.inputs["Vector"])
    ramp = nt.nodes.new("ShaderNodeValToRGB")
    ramp.color_ramp.elements[0].color = (0.03, 0.045, 0.012, 1)   # shaded undergrowth
    ramp.color_ramp.elements[1].color = (0.085, 0.08, 0.03, 1)    # dry undergrowth
    nt.links.new(nz.outputs["Fac"], ramp.inputs["Fac"])
    nt.links.new(ramp.outputs["Color"], bsdf.inputs["Base Color"])
    bsdf.inputs["Roughness"].default_value = 0.95
    if "Specular IOR Level" in bsdf.inputs:
        bsdf.inputs["Specular IOR Level"].default_value = 0.0   # no sky sheen at grazing angles
    me.materials.append(mat)
    return ob


# --------------------------------------------------------------------- assets

def resolve(path):
    path = os.path.abspath(path)
    if os.path.isdir(path):
        return path
    dest = path[:-4]
    if not os.path.isdir(dest):
        with zipfile.ZipFile(path) as z:
            for m in z.namelist():
                t = os.path.abspath(os.path.join(dest, m))
                if not t.startswith(os.path.abspath(dest) + os.sep):
                    raise RuntimeError("unsafe path in zip: " + m)
            z.extractall(dest)
    return dest


def find_tex(folder, kind):
    for f in sorted(os.listdir(folder)):
        lf = f.lower()
        if "billboard" in lf or not lf.endswith((".jpg", ".png")):
            continue
        if lf.rsplit("_", 1)[-1].startswith(kind):
            return os.path.join(folder, f)
    return None


def grass_material(name, folder):
    mat = bpy.data.materials.new("MS_" + name)
    if bpy.app.version < (5, 0, 0) or mat.node_tree is None:
        mat.use_nodes = True
    nt = mat.node_tree
    nt.nodes.clear()
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    bsdf = nt.nodes.new("ShaderNodeBsdfPrincipled")
    if "Specular IOR Level" in bsdf.inputs:
        bsdf.inputs["Specular IOR Level"].default_value = 0.1    # no glints on blade edges
    trans = nt.nodes.new("ShaderNodeBsdfTranslucent")
    mix = nt.nodes.new("ShaderNodeMixShader")
    mix.inputs["Fac"].default_value = 0.25
    nt.links.new(bsdf.outputs[0], mix.inputs[1])
    nt.links.new(trans.outputs[0], mix.inputs[2])
    nt.links.new(mix.outputs[0], out.inputs["Surface"])

    def img(kind, non_color):
        p = find_tex(folder, kind)
        if not p:
            return None
        n = nt.nodes.new("ShaderNodeTexImage")
        n.image = bpy.data.images.load(p, check_existing=True)
        if non_color:
            n.image.colorspace_settings.name = "Non-Color"
        return n

    col = img("basecolor", False)
    if col:
        nt.links.new(col.outputs["Color"], bsdf.inputs["Base Color"])
        tr = img("translucency", False)
        nt.links.new((tr or col).outputs["Color"], trans.inputs["Color"])
    rough = img("roughness", True)
    if rough:
        nt.links.new(rough.outputs["Color"], bsdf.inputs["Roughness"])
    nrm = img("normal", True)
    if nrm:
        nm = nt.nodes.new("ShaderNodeNormalMap")
        nt.links.new(nrm.outputs["Color"], nm.inputs["Color"])
        nt.links.new(nm.outputs["Normal"], bsdf.inputs["Normal"])
        nt.links.new(nm.outputs["Normal"], trans.inputs["Normal"])
    op = img("opacity", True)
    if op:
        # alpha-tested foliage: wrap the surface in a cutout mix
        cut = nt.nodes.new("ShaderNodeMixShader")
        tsp = nt.nodes.new("ShaderNodeBsdfTransparent")
        nt.links.new(op.outputs["Color"], cut.inputs["Fac"])
        nt.links.new(tsp.outputs[0], cut.inputs[1])
        nt.links.new(mix.outputs[0], cut.inputs[2])
        nt.links.new(cut.outputs[0], out.inputs["Surface"])
    if hasattr(mat, "surface_render_method"):
        mat.surface_render_method = 'DITHERED'
    elif hasattr(mat, "blend_method"):
        mat.blend_method = 'CLIP'
    return mat


def tri_count(me):
    return sum(len(p.vertices) - 2 for p in me.polygons)


def import_grass(path):
    """-> list of variations: {'name', 'lods': [mesh, ...], 'height'}.
    LOD numbers come from the FILE names; the object names inside Fab's FBX
    files do not match them (e.g. *_LOD1.fbx contains *_VarA_LOD2)."""
    folder = resolve(path)
    asset = re.sub(r"_[a-z0-9]{6,10}_high$", "", os.path.basename(folder), flags=re.I)
    mat = grass_material(asset, folder)
    files = {}
    for f in os.listdir(folder):
        m = FILE_RE.search(f)
        if m:
            files.setdefault(m.group(1).upper(), {})[int(m.group(2))] = os.path.join(folder, f)
    out = []
    for var in sorted(files):
        meshes = []
        for lod in sorted(files[var]):
            before = set(bpy.data.objects)
            bpy.ops.import_scene.fbx(filepath=files[var][lod])
            new = [o for o in bpy.data.objects if o not in before]
            obj = next(o for o in new if o.type == 'MESH')
            me = obj.data.copy()
            me.transform(obj.matrix_world)          # bake cm scale + Y-up rotation
            for o in new:
                bpy.data.objects.remove(o)
            billboard = any(m and m.name.lower().startswith("billboard") for m in me.materials) \
                or tri_count(me) < 20
            if billboard:
                bpy.data.meshes.remove(me)
                continue
            me.materials.clear()
            me.materials.append(mat)
            if me.uv_layers:
                me.uv_layers[0].name = "UVMap"
            me.name = "%s_Var%s_LOD%d" % (asset, var, lod)
            meshes.append(me)
        if len(meshes) >= 2:
            zs = [v.co.z for v in meshes[0].vertices]
            out.append({"name": "%s_Var%s" % (asset, var), "asset": asset, "lods": meshes,
                        "height": max(zs) - min(zs)})
    for m in list(bpy.data.materials):
        if m.users == 0:
            bpy.data.materials.remove(m)
    for im in list(bpy.data.images):
        if im.users == 0:
            bpy.data.images.remove(im)
    log("%s: %d variations, tris per LOD: %s" % (asset, len(out), "; ".join(
        "/".join(str(tri_count(m)) for m in v["lods"]) for v in out)))
    return out


# --------------------------------------------------------------------- patches

ASSET_WEIGHTS = {"tufted_grass": 0.5, "grass": 0.3, "wild_grass": 0.2}


def pick(variations, rnd):
    w = [ASSET_WEIGHTS.get(v["asset"], 0.3) / sum(1 for u in variations if u["asset"] == v["asset"])
         for v in variations]
    return rnd.choices(variations, weights=w)[0]


def build_patch_meshes(variations, rnd, scratch):
    """PATCH_PROTOTYPES layouts x 3 LOD meshes. Each is many clumps joined into
    one mesh (one draw call per patch, like an engine's grass cluster)."""
    protos = []
    src_names = {me.name for v in variations for me in v["lods"]}
    g = 13  # jittered grid -> even coverage without clumping
    for p in range(PATCH_PROTOTYPES):
        cells = [(i, j) for i in range(g) for j in range(g)]
        rnd.shuffle(cells)
        clumps = []
        for k in range(CLUMPS_PER_PATCH):
            i, j = cells[k % len(cells)]
            x = (i + rnd.random()) / g * PATCH_SIZE - PATCH_SIZE / 2
            y = (j + rnd.random()) / g * PATCH_SIZE - PATCH_SIZE / 2
            clumps.append((pick(variations, rnd), x, y, rnd.uniform(0, math.tau),
                           rnd.uniform(0.85, 1.35), rnd.random() < FAR_KEEP))
        lods = []
        for level in range(3):
            parts = []
            for var, x, y, rot, s, keep in clumps:
                if level == 2 and not keep:
                    continue
                me = var["lods"][min(level, len(var["lods"]) - 1)] if level < 2 else var["lods"][-1]
                sc = s * (1.2 if level == 2 else 1.0)
                o = bpy.data.objects.new("tmp", me.copy())
                o.matrix_world = Matrix.Translation((x, y, -0.03)) @ Matrix.Rotation(rot, 4, 'Z') \
                    @ Matrix.Scale(sc, 4)
                scratch.objects.link(o)
                parts.append(o)
            with bpy.context.temp_override(active_object=parts[0], object=parts[0],
                                           selected_objects=parts, selected_editable_objects=parts):
                bpy.ops.object.join()
            joined = parts[0]
            joined.data.transform(joined.matrix_world)
            me = joined.data
            me.name = "GrassPatch_%02d_LOD%d" % (p, level)
            bpy.data.objects.remove(joined)
            lods.append(me)
        protos.append(lods)
        for m in list(bpy.data.meshes):     # join leaves the other copies orphaned
            if m.users == 0 and m.name not in src_names and not m.name.startswith("GrassPatch_"):
                bpy.data.meshes.remove(m)
    return protos


def place_patches(so, scene, protos, coll, rnd):
    s = scene.scene_optimizer
    groups = []
    nx = int((FIELD_X[1] - FIELD_X[0]) / PATCH_SIZE)
    ny = int((FIELD_Y[1] - FIELD_Y[0]) / PATCH_SIZE)
    for j in range(ny):
        for i in range(nx):
            cx = FIELD_X[0] + (i + 0.5) * PATCH_SIZE
            cy = FIELD_Y[0] + (j + 0.5) * PATCH_SIZE
            n = terrain_normal(cx, cy)
            tilt = n.to_track_quat('Z', 'Y').to_matrix().to_4x4()
            m = Matrix.Translation((cx, cy, height(cx, cy))) @ tilt @ \
                Matrix.Rotation(rnd.choice((0, 1, 2, 3)) * math.pi / 2, 4, 'Z')
            proto = rnd.choice(protos)
            objs = []
            for level, me in enumerate(proto):
                o = bpy.data.objects.new("Grass_%03d_%03d_LOD%d" % (i, j, level), me)
                o.matrix_world = m
                o.so_not_occluder = True
                coll.objects.link(o)
                objs.append(o)
            groups.append(("Grass_%03d_%03d" % (i, j), objs))
    s.lod_groups.clear()
    for name, objs in groups:
        g = s.lod_groups.add()      # names are unique by construction
        g.name = name
        for o in objs:
            g.variants.add().obj = o
    so.invalidate_lod_cache()
    return len(groups)


# --------------------------------------------------------------------- camera

def build_camera(scene):
    cam = bpy.data.objects.new("Camera", bpy.data.cameras.new("Camera"))
    cam.data.lens = 28
    cam.data.clip_end = 600
    scene.collection.objects.link(cam)
    scene.camera = cam
    scene.frame_start, scene.frame_end = 1, 240
    # walk forward and up the slope towards the ridge, turning slightly
    for frame, (x, y, yaw) in ((1, (-6.0, -16.0, -6.0)), (120, (0.0, 6.0, 3.0)),
                               (240, (4.0, 27.0, 8.0))):
        cam.location = (x, y, height(x, y) + 1.7)
        cam.rotation_euler = (math.radians(88), 0, math.radians(yaw))
        cam.keyframe_insert("location", frame=frame)
        cam.keyframe_insert("rotation_euler", frame=frame)
    return cam


def build_world(scene):
    world = bpy.data.worlds.new("Meadow_Sky")
    scene.world = world
    if bpy.app.version < (5, 0, 0) or world.node_tree is None:
        world.use_nodes = True
    nt = world.node_tree
    bg = nt.nodes.get("Background")
    bg.inputs["Color"].default_value = (0.45, 0.62, 0.95, 1)
    bg.inputs["Strength"].default_value = 1.0
    try:                                    # physically based sky when available
        sky = nt.nodes.new("ShaderNodeTexSky")
        for t in ("MULTIPLE_SCATTERING", "NISHITA"):
            try:
                sky.sky_type = t
                break
            except TypeError:
                pass
        sky.sun_elevation = math.radians(35)
        sky.sun_rotation = math.radians(-150 + 90)
        nt.links.new(sky.outputs["Color"], bg.inputs["Color"])
        bg.inputs["Strength"].default_value = 0.35
    except Exception:
        pass
    sun = bpy.data.objects.new("Sun", bpy.data.lights.new("Sun", 'SUN'))
    sun.data.energy = 5.0
    sun.data.color = (1.0, 0.93, 0.82)
    sun.data.angle = math.radians(2)
    sun.rotation_euler = (math.radians(55), 0, math.radians(-150))  # 35 deg elevation
    scene.collection.objects.link(sun)


# --------------------------------------------------------------------- stats

def stats(so, scene):
    s = scene.scene_optimizer
    full = drawn = 0
    hist = {"LOD0": 0, "LOD1": 0, "LOD2": 0, "frustum": 0, "occluded": 0, "too far": 0}
    for g in s.lod_groups:
        objs = [v.obj for v in g.variants]
        full += tri_count_cached(objs[0].data)
        vis = [o for o in objs if not o.hide_render]
        assert len(vis) <= 1, g.name
        if vis:
            drawn += tri_count_cached(vis[0].data)
            hist["LOD%d" % objs.index(vis[0])] += 1
        else:
            r = set().union(*(so.Visibility.reasons(o) for o in objs))
            if so.R_OCCLUSION in r:
                hist["occluded"] += 1
            elif so.R_FRUSTUM in r:
                hist["frustum"] += 1
            else:
                hist["too far"] += 1
    return full, drawn, hist


_TRIS = {}


def tri_count_cached(me):
    if me.name not in _TRIS:
        _TRIS[me.name] = tri_count(me)
    return _TRIS[me.name]


def optimize_frame(so, scene, frame):
    with bpy.context.temp_override(scene=scene, view_layer=scene.view_layers[0]):
        scene.frame_set(frame)
        t = time.perf_counter()
        rep = so.run_culling(bpy.context, scene, preview=False)
        dt = time.perf_counter() - t
    if rep["error"]:
        raise RuntimeError(rep["error"])
    for w in rep["warnings"]:
        log("  warning: " + w)
    full, drawn, hist = stats(so, scene)
    line = ("frame %3d: %s patches -> LOD0 %d, LOD1 %d, LOD2 %d | culled: frustum %d, "
            "behind hills %d, beyond %g m %d | triangles %s -> %s (%.1f%% fewer) | %.1f s" % (
                frame, len(scene.scene_optimizer.lod_groups), hist["LOD0"], hist["LOD1"],
                hist["LOD2"], hist["frustum"], hist["occluded"], MAX_DRAW, hist["too far"],
                "{:,}".format(full), "{:,}".format(drawn), 100.0 * (1 - drawn / max(full, 1)), dt))
    log(line)
    return line


# --------------------------------------------------------------------- renders

LOD_COLORS = ((0.9, 0.15, 0.1, 1), (1.0, 0.8, 0.1, 1), (0.15, 0.45, 1.0, 1))


def render_views(scene, out_dir, frame):
    os.makedirs(out_dir, exist_ok=True)
    r = scene.render
    r.resolution_x, r.resolution_y, r.resolution_percentage = 1280, 720, 100
    # 1) beauty, EEVEE
    engines = [e for e in ("BLENDER_EEVEE_NEXT", "BLENDER_EEVEE")
               if e in {i.identifier for i in bpy.types.RenderSettings.bl_rna.properties["engine"].enum_items}]
    r.engine = engines[0]
    try:
        scene.eevee.taa_render_samples = 32
    except AttributeError:
        pass
    r.filepath = os.path.join(out_dir, "meadow_f%03d.png" % frame)
    with bpy.context.temp_override(scene=scene, view_layer=scene.view_layers[0]):
        bpy.ops.render.render(write_still=True, scene=scene.name)
    log("rendered " + r.filepath)

    # 2) debug top-down: LOD colours, terrain grey, culled patches simply absent
    for o in scene.objects:
        m = re.search(r"_LOD(\d)$", o.name)
        o.color = LOD_COLORS[int(m.group(1))] if m and o.name.startswith("Grass_") else (0.35, 0.35, 0.35, 1)
    cam = scene.camera
    top = bpy.data.objects.new("TopCam", bpy.data.cameras.new("TopCam"))
    top.data.type = 'ORTHO'
    top.data.ortho_scale = 260
    top.data.clip_end = 1000
    top.location = (0, 85, 300)
    scene.collection.objects.link(top)
    # draw the shot camera as a big marker so the top view shows where it is
    mark = bpy.data.objects.new("CamMarker", bpy.data.meshes.new("CamMarker"))
    c = cam.matrix_world.translation
    fwd = (cam.matrix_world.to_3x3() @ Vector((0, 0, -1))).normalized()
    side = Vector((-fwd.y, fwd.x, 0)).normalized()
    tip = c + Vector((fwd.x, fwd.y, 0)).normalized() * 8
    mark.data.from_pydata([c + side * 3 + Vector((0, 0, 40)), c - side * 3 + Vector((0, 0, 40)),
                           tip + Vector((0, 0, 40))], [], [(0, 1, 2)])
    mark.color = (1, 1, 1, 1)
    scene.collection.objects.link(mark)
    scene.camera = top
    r.engine = 'BLENDER_WORKBENCH'
    sh = scene.display.shading
    sh.light, sh.color_type = 'STUDIO', 'OBJECT'
    r.resolution_x, r.resolution_y = 1100, 1100
    r.filepath = os.path.join(out_dir, "meadow_f%03d_topdown.png" % frame)
    with bpy.context.temp_override(scene=scene, view_layer=scene.view_layers[0]):
        bpy.ops.render.render(write_still=True, scene=scene.name)
    log("rendered " + r.filepath)
    scene.camera = cam
    bpy.data.objects.remove(top)
    bpy.data.objects.remove(mark)


# --------------------------------------------------------------------- main

def main():
    args = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    paths, opts, i = [], {"seed": 3, "save": None, "render_dir": None}, 0
    while i < len(args):
        if args[i] in ("--seed", "--save", "--render-dir"):
            k = args[i][2:].replace("-", "_")
            opts[k] = int(args[i + 1]) if k == "seed" else os.path.abspath(args[i + 1])
            i += 2
        else:
            paths.append(args[i])
            i += 1
    if not paths:
        raise SystemExit("usage: blender -b --python megascans_grass_meadow.py -- <grass zip/folder> ...")
    so = scene_optimizer_module()
    rnd = random.Random(opts["seed"])
    t0 = time.perf_counter()

    old = bpy.data.scenes.get(SCENE_NAME)
    if old:
        bpy.data.scenes.remove(old)
    scene = bpy.data.scenes.new(SCENE_NAME)
    vl = scene.view_layers[0]
    if bpy.context.window is not None:
        bpy.context.window.scene = scene
    ground = bpy.data.collections.new("Terrain")
    grass = bpy.data.collections.new("Grass_Patches")
    scratch = bpy.data.collections.new("_scratch")
    for c in (ground, grass, scratch):
        scene.collection.children.link(c)

    with bpy.context.temp_override(scene=scene, view_layer=vl):
        variations = []
        for p in paths:
            variations += import_grass(p)
        build_terrain(ground)
        protos = build_patch_meshes(variations, rnd, scratch)
        bpy.data.collections.remove(scratch)
        log("patch prototypes: tris per LOD %s" % "; ".join(
            "/".join(str(tri_count(m)) for m in lods) for lods in protos[:4]))
        n = place_patches(so, scene, protos, grass, rnd)
        build_camera(scene)
        build_world(scene)
        log("built %d patches (%d objects) in %.1f s" % (n, len(grass.objects), time.perf_counter() - t0))

        s = scene.scene_optimizer
        s.lod_enabled = True
        s.lod_distance_mode = 'NEAREST'
        for k, d in enumerate(LOD_DISTANCES):
            s.lod_distances[k] = d
        s.lod_hide_beyond = True
        s.lod_max_distance = MAX_DRAW
        s.frustum_enabled = True
        s.frustum_margin = 5.0
        s.occlusion_enabled = True
        s.occlusion_preset = 'BALANCED'
        s.occluder_collection = ground      # only hills block sight
        s.frame_mode = 'CURRENT'
        s.lod_max_labels = 0

    report = []
    frames = (1, 120, 240)
    for f in frames:
        report.append(optimize_frame(so, scene, f))
        if opts["render_dir"]:
            render_views(scene, opts["render_dir"], f)
    report.append(optimize_frame(so, scene, 1))   # leave the file at frame 1, optimized
    txt = bpy.data.texts.new("Meadow report")
    txt.from_string("\n".join(report) + "\n")
    if opts["save"]:
        bpy.ops.wm.save_as_mainfile(filepath=opts["save"])
        log("saved " + opts["save"])
    log("total %.1f s" % (time.perf_counter() - t0))


if __name__ == "__main__":
    main()
