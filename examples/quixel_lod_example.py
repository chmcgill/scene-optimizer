"""
Scene Optimizer - Quixel Megascans LOD example
==============================================

Turns one or more Megascans 3D assets (Bridge export folder or Fab .zip) into a
working LOD setup:

  1. imports every LOD mesh of each asset (FBX / OBJ / glTF / Alembic),
     including plant assets with several variations (Var1_LOD0, Var2_LOD0 ...)
  2. builds a PBR material from the Megascans textures (Albedo, Roughness,
     Normal - per-LOD normal maps when present - and Opacity for plants)
  3. scatters linked copies (meshes are shared, not duplicated) across a field
     in front of a camera, from 2 m out past the last LOD distance
  4. creates one Scene Optimizer LOD group per copy (auto-detected by name),
     with switch distances scaled to each asset's size
  5. switches LODs, frustum-culls what is off screen and prints the triangle
     savings; Live Update is on and the camera is animated, so scrubbing the
     timeline shows the LODs switching

Everything is built in a NEW scene called "Megascans LOD Example"; your other
scenes are not touched.

Requirements: Blender 4.2+ with the Scene Optimizer add-on installed + enabled.

Usage A - inside Blender:
    Scripting workspace > Text > Open > this file. Edit ASSET_PATHS below to
    point at your downloaded asset folder(s) or .zip file(s). Run Script.

Usage B - command line:
    blender --python examples/quixel_lod_example.py -- <asset> [<asset> ...]
            [--copies 40] [--seed 7] [--save out.blend] [--render out.png]

Where to find assets:
    Bridge: <Megascans Library>/Downloaded/3d/<asset_name_id>/
            <Megascans Library>/Downloaded/3dplant/<asset_name_id>/
    Fab:    the downloaded .zip (choose FBX format and include all LODs)
"""

import math
import os
import random
import re
import sys
import zipfile

import bpy
from mathutils import Matrix, Vector

# --------------------------------------------------------------------------
# Settings for "Run Script" use. Command-line arguments override these.
ASSET_PATHS = [
    # r"C:\Users\you\Documents\Megascans Library\Downloaded\3d\rock_sandstone_tkmfdcbfa",
    # r"C:\Users\you\Downloads\plant_fern_ulruch3ia.zip",
]
COPIES_PER_VARIATION = 80     # placed copies of every asset variation
SEED = 7
TEXTURE_RESOLUTION = ("2K", "4K", "1K", "8K")   # preferred order
LOD1_DISTANCE_FACTOR = 12.0   # LOD1 starts at 12 x the asset's bounding radius,
                              # every further level doubles that distance
# --------------------------------------------------------------------------

SCENE_NAME = "Megascans LOD Example"
MESH_EXTS = {".fbx", ".obj", ".glb", ".gltf", ".abc"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".exr", ".tif", ".tiff", ".tga"}
LOD_TOKEN = re.compile(r"lod[\s_.\-]*(\d+)", re.IGNORECASE)


def log(msg):
    print("[Megascans LOD example] " + msg)


# ----------------------------------------------------------------- add-on

def scene_optimizer_module():
    if not hasattr(bpy.types.Scene, "scene_optimizer"):
        import addon_utils
        addon_utils.enable("scene_optimizer", default_set=True)
    mod = sys.modules.get("scene_optimizer")
    if mod is None or not hasattr(bpy.types.Scene, "scene_optimizer"):
        raise RuntimeError("Scene Optimizer add-on is not installed/enabled "
                           "(Preferences > Add-ons > Install from Disk > scene_optimizer.py)")
    return mod


# ----------------------------------------------------------------- asset files

def resolve_asset(path):
    """Folder -> folder. Fab .zip -> extracted next to the zip (once)."""
    path = os.path.abspath(path)
    if os.path.isdir(path):
        return path
    if path.lower().endswith(".zip") and os.path.isfile(path):
        dest = path[:-4]
        if not os.path.isdir(dest):
            with zipfile.ZipFile(path) as z:
                for member in z.namelist():
                    target = os.path.abspath(os.path.join(dest, member))
                    if not target.startswith(os.path.abspath(dest) + os.sep):
                        raise RuntimeError("Unsafe path in zip: %s" % member)
                z.extractall(dest)
            log("extracted %s" % os.path.basename(path))
        return dest
    raise FileNotFoundError("Asset not found: %s" % path)


def walk_files(folder, exts, max_depth=3):
    base = folder.rstrip("\\/").count(os.sep)
    for root, dirs, files in os.walk(folder):
        if root.count(os.sep) - base >= max_depth:
            dirs[:] = []
        for f in files:
            if os.path.splitext(f)[1].lower() in exts:
                yield os.path.join(root, f)


def find_mesh_files(folder):
    files = sorted(walk_files(folder, MESH_EXTS))
    with_lod = [f for f in files if LOD_TOKEN.search(os.path.basename(f))]
    # Megascans also ships *_High.fbx (multi-million tris) - not a game LOD.
    if with_lod:
        return with_lod
    return [f for f in files if "high" not in os.path.basename(f).lower()] or files


def asset_name(folder):
    """'rock_sandstone_tkmfdcbfa' -> 'rock_sandstone'."""
    name = os.path.basename(folder.rstrip("\\/"))
    parts = name.split("_")
    if len(parts) > 1 and re.fullmatch(r"[a-z0-9]{6,12}", parts[-1]):
        name = "_".join(parts[:-1])
    return name or "megascan"


def import_file(path):
    before = {o.as_pointer() for o in bpy.data.objects}
    ext = os.path.splitext(path)[1].lower()
    if ext == ".fbx":
        bpy.ops.import_scene.fbx(filepath=path)
    elif ext == ".obj":
        bpy.ops.wm.obj_import(filepath=path)
    elif ext in {".glb", ".gltf"}:
        bpy.ops.import_scene.gltf(filepath=path)
    elif ext == ".abc":
        bpy.ops.wm.alembic_import(filepath=path, as_background_job=False)
    return [o for o in bpy.data.objects if o.as_pointer() not in before]


# ----------------------------------------------------------------- textures

TEX_KINDS = (
    ("opacity", r"opacity|alpha|mask"),
    ("normal", r"normal(?!.*bump)"),
    ("albedo", r"albedo|basecolou?r|base_colou?r|diffuse"),
    ("roughness", r"roughness"),
)


def find_textures(folder):
    """{'albedo': path, 'roughness': path, 'opacity': path,
        'normal': {None: path, 0: path_lod0, 1: ...}}"""
    found = {}
    for path in walk_files(folder, IMAGE_EXTS):
        name = os.path.basename(path).lower()
        for kind, pattern in TEX_KINDS:
            if re.search(pattern, name):
                m = LOD_TOKEN.search(name) if kind == "normal" else None
                lod = int(m.group(1)) if m else None
                found.setdefault(kind, {}).setdefault(lod, []).append(path)
                break

    def pick(paths):
        def rank(p):
            n = os.path.basename(p).upper()
            for i, res in enumerate(TEXTURE_RESOLUTION):
                if "_%s_" % res in n or n.startswith(res + "_"):
                    return i
            return len(TEXTURE_RESOLUTION)
        return sorted(paths, key=rank)[0]

    out = {}
    for kind, by_lod in found.items():
        if kind == "normal":
            out["normal"] = {lod: pick(p) for lod, p in by_lod.items()}
        else:
            out[kind] = pick(sum(by_lod.values(), []))
    return out


def _image(path, non_color):
    img = bpy.data.images.load(path, check_existing=True)
    if non_color:
        try:
            img.colorspace_settings.name = "Non-Color"
        except TypeError:
            pass
    return img


def build_material(name, tex, lod):
    mat = bpy.data.materials.get(name)
    if mat is not None:
        return mat
    mat = bpy.data.materials.new(name)
    if bpy.app.version < (5, 0, 0) or mat.node_tree is None:
        mat.use_nodes = True
    nt = mat.node_tree
    nt.nodes.clear()
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    out.location = (400, 0)
    bsdf = nt.nodes.new("ShaderNodeBsdfPrincipled")
    nt.links.new(bsdf.outputs[0], out.inputs["Surface"])
    y = 300

    def tex_node(path, non_color):
        nonlocal y
        n = nt.nodes.new("ShaderNodeTexImage")
        n.image = _image(path, non_color)
        n.location = (-500, y)
        y -= 280
        return n

    if "albedo" in tex:
        nt.links.new(tex_node(tex["albedo"], False).outputs["Color"], bsdf.inputs["Base Color"])
    if "roughness" in tex:
        nt.links.new(tex_node(tex["roughness"], True).outputs["Color"], bsdf.inputs["Roughness"])
    normals = tex.get("normal", {})
    npath = normals.get(lod) or normals.get(None) or normals.get(0)
    if npath:
        nmap = nt.nodes.new("ShaderNodeNormalMap")
        nmap.location = (-200, y + 140)
        nt.links.new(tex_node(npath, True).outputs["Color"], nmap.inputs["Color"])
        nt.links.new(nmap.outputs["Normal"], bsdf.inputs["Normal"])
    if "opacity" in tex:
        nt.links.new(tex_node(tex["opacity"], True).outputs["Color"], bsdf.inputs["Alpha"])
        if hasattr(mat, "surface_render_method"):
            mat.surface_render_method = 'DITHERED'
    return mat


# ----------------------------------------------------------------- import

class Variation:
    """One Megascans mesh variation with its LOD prototypes."""

    def __init__(self, asset, key):
        self.asset = asset
        self.key = key
        self.lods = {}          # level -> (mesh data, matrix relative to origin)
        self.radius = 1.0


def import_asset(so, folder, source_coll):
    name = asset_name(folder)
    files = find_mesh_files(folder)
    if not files:
        raise RuntimeError("No mesh files (FBX/OBJ/glTF/ABC) in %s" % folder)
    tex = find_textures(folder)
    log("%s: %d mesh file(s), textures: %s" % (name, len(files), ", ".join(sorted(tex)) or "none"))
    variations = {}
    for path in files:
        file_lod = LOD_TOKEN.search(os.path.basename(path))
        objs = import_file(path)
        meshes = [o for o in objs if o.type == 'MESH']
        for o in meshes:
            info = so.detect_lod_name(o.name)
            if info is not None:
                base, level = info[0], info[1]
            elif file_lod is not None:
                base, level = name, int(file_lod.group(1))
            else:
                base, level = o.name, 0
            key = base or name
            var = variations.setdefault(key, Variation(name, key))
            if level in var.lods:
                log("  duplicate %s LOD%d in %s - skipped" % (key, level, os.path.basename(path)))
                continue
            var.lods[level] = (o.data, o.matrix_world.copy())
            # keep the imported object as a reference copy in the source collection
            for c in list(o.users_collection):
                c.objects.unlink(o)
            source_coll.objects.link(o)
            o.parent = None
            o.matrix_world = var.lods[level][1]
        for o in objs:
            if o.type != 'MESH':
                bpy.data.objects.remove(o)   # FBX root empties etc.
    # materials (only replace when Megascans textures were found)
    for var in variations.values():
        for level, (me, _m) in var.lods.items():
            if tex:
                per_lod = len(tex.get("normal", {})) > 1
                mname = "MS_%s_LOD%d" % (name, level) if per_lod else "MS_%s" % name
                me.materials.clear()
                me.materials.append(build_material(mname, tex, level))
        lod0_me, lod0_m = var.lods[min(var.lods)]
        corners = [lod0_m @ Vector(c) for c in _mesh_bounds(lod0_me)]
        var.radius = max(0.05, max((c - Vector((0, 0, 0))).length for c in corners))
    ok = {k: v for k, v in variations.items() if len(v.lods) >= 2}
    for k, v in variations.items():
        if len(v.lods) < 2:
            log("  %s has only one LOD - not usable for LOD switching, skipped" % k)
    for v in ok.values():
        log("  variation %-40s LODs %s  radius %.2f m  tris %s" % (
            v.key, sorted(v.lods), v.radius,
            "/".join(str(tri_count(v.lods[l][0])) for l in sorted(v.lods))))
    return list(ok.values())


def _mesh_bounds(me):
    xs = [v.co.x for v in me.vertices] or [0]
    ys = [v.co.y for v in me.vertices] or [0]
    zs = [v.co.z for v in me.vertices] or [0]
    return [(x, y, z) for x in (min(xs), max(xs)) for y in (min(ys), max(ys)) for z in (min(zs), max(zs))]


def tri_count(me):
    return sum(len(p.vertices) - 2 for p in me.polygons)


def lod_distances(var):
    n = max(var.lods) if var.lods else 1
    d1 = var.radius * LOD1_DISTANCE_FACTOR
    return [round(d1 * 2 ** i, 2) for i in range(n)]


# ----------------------------------------------------------------- scene

def build_scene(so, asset_paths, copies, seed):
    scene = bpy.data.scenes.get(SCENE_NAME)
    if scene is not None:
        bpy.data.scenes.remove(scene)
    scene = bpy.data.scenes.new(SCENE_NAME)
    view_layer = scene.view_layers[0]
    if bpy.context.window is not None:
        bpy.context.window.scene = scene

    src = bpy.data.collections.new("Megascans_Source (reference, excluded)")
    placed = bpy.data.collections.new("Megascans_Placed")
    scene.collection.children.link(src)
    scene.collection.children.link(placed)
    view_layer.layer_collection.children[src.name].exclude = True

    with bpy.context.temp_override(scene=scene, view_layer=view_layer):
        variations = []
        for p in asset_paths:
            variations += import_asset(so, resolve_asset(p), src)
        if not variations:
            raise RuntimeError("No usable LOD assets found")

        # camera + light + ground
        cam = bpy.data.objects.new("Camera", bpy.data.cameras.new("Camera"))
        cam.data.lens = 35
        cam.data.clip_end = 1000
        scene.collection.objects.link(cam)
        scene.camera = cam
        sun = bpy.data.objects.new("Sun", bpy.data.lights.new("Sun", 'SUN'))
        sun.data.energy = 3
        sun.rotation_euler = (math.radians(45), 0, math.radians(35))
        scene.collection.objects.link(sun)

        max_far = max(lod_distances(v)[-1] for v in variations) * 1.35
        far = min(max(max_far, 30.0), 600.0)
        ground_me = bpy.data.meshes.new("Ground")
        ground_me.from_pydata([(-far, -10, 0), (far, -10, 0), (far, far + 10, 0), (-far, far + 10, 0)],
                              [], [(0, 1, 2, 3)])
        g = bpy.data.objects.new("Ground", ground_me)
        scene.collection.objects.link(g)

        # scatter: log-uniform distance so every LOD band gets copies
        rnd = random.Random(seed)
        half_fov = math.atan(18.0 / cam.data.lens)
        n_roots = 0
        for var in variations:
            for i in range(copies):
                dist = math.exp(rnd.uniform(math.log(2.0), math.log(far)))
                spread = math.tan(half_fov) * dist * 1.25   # ~20% land off screen
                x = rnd.uniform(-spread, spread)
                root = bpy.data.objects.new("%s_%s_%03d" % (var.asset, var.key, i), None)
                root.empty_display_size = var.radius * 0.5
                root.location = (x, dist, 0)
                root.rotation_euler.z = rnd.uniform(0, math.tau)
                s = rnd.uniform(0.8, 1.25)
                root.scale = (s, s, s)
                root["megascans_variation"] = var.key
                placed.objects.link(root)
                for level in sorted(var.lods):
                    me, m = var.lods[level]
                    ch = bpy.data.objects.new("%s_LOD%d" % (var.key, level), me)  # shares mesh
                    ch.parent = root
                    ch.matrix_parent_inverse = Matrix.Identity(4)
                    ch.matrix_basis = m
                    placed.objects.link(ch)
                n_roots += 1

        # camera fly-through: walks 30% into the field, so copies at every LOD
        # band stay ahead of it for the whole shot
        scene.frame_start, scene.frame_end = 1, 250
        for frame, y in ((1, -4.0), (250, far * 0.3)):
            cam.location = (0, y, 1.7)
            cam.rotation_euler = (math.radians(84), 0, 0)
            cam.keyframe_insert("location", frame=frame)
        scene.frame_set(1)

        # ---- Scene Optimizer setup
        s = scene.scene_optimizer
        s.lod_groups.clear()
        groups, problems = so.auto_detect_groups(list(placed.all_objects))
        for p in problems:
            log("auto-detect: " + p)
        dist_by_key = {v.key: lod_distances(v) for v in variations}
        for gname, objs in groups:
            grp = so._add_group(s, gname, objs)
            key = objs[0].parent.get("megascans_variation") if objs[0].parent else None
            d = dist_by_key.get(key)
            if d:
                grp.use_custom_distances = True
                for i, val in enumerate(d[:so.MAX_LOD_LEVELS - 1]):
                    grp.distances[i] = val
        so.invalidate_lod_cache()
        s.lod_enabled = True
        s.lod_distance_mode = 'NEAREST'
        s.frustum_enabled = True
        s.frustum_margin = 5.0
        s.occlusion_enabled = False   # open field; foliage is alpha-tested anyway
        # The camera flies: cull for the whole shot, not just frame 1
        # (keeps every copy that is on screen at ANY sampled frame).
        s.frame_mode = 'RANGE'
        s.frame_step = 10
        s.static_geometry = True      # only the camera moves
        rep = so.run_culling(bpy.context, scene, preview=False)   # culls + switches LODs
        if rep["error"]:
            raise RuntimeError(rep["error"])
        s.lod_on_frame_change = True
        s.lod_live = True
        report = summarize(so, scene, variations, n_roots, rep)
    return scene, report


def summarize(so, scene, variations, n_roots, rep):
    s = scene.scene_optimizer
    levels = so._Runtime.lod_levels.get(scene.name_full, {})
    hist = {}
    tris_lod0 = tris_visible = 0
    for g in s.lod_groups:
        objs = so.lod_group_objects(scene, g)
        tris_lod0 += tri_count(objs[0].data)
        lvl = levels.get(g.name)
        culled = all(so.R_FRUSTUM in so.Visibility.reasons(o) for o in objs)
        tag = "culled" if culled else ("LOD%d" % lvl if lvl is not None and lvl >= 0 else "hidden")
        hist[tag] = hist.get(tag, 0) + 1
        for o in objs:
            if not o.hide_render:
                tris_visible += tri_count(o.data)
        visible = [o for o in objs if not o.hide_render]
        assert len(visible) <= 1, "LOD group %s renders %d variants" % (g.name, len(visible))
    lines = [
        "Megascans LOD example - %d variation(s), %d placed copies, %d LOD groups" % (
            len(variations), n_roots, len(s.lod_groups)),
        "Switch distances (m): " + "; ".join(
            "%s: %s" % (v.key, ", ".join("LOD%d>%g" % (i + 1, d) for i, d in enumerate(lod_distances(v))))
            for v in variations),
        "Groups by state at frame %d: %s" % (scene.frame_current, ", ".join(
            "%s %d" % (k, hist[k]) for k in sorted(hist))),
        "Triangles: all LOD0 = %s  ->  rendered now = %s  (%.1f%% fewer)" % (
            "{:,}".format(tris_lod0), "{:,}".format(tris_visible),
            100.0 * (1 - tris_visible / max(1, tris_lod0))),
        "Culling: " + s.last_report,
        "Live Update is ON: scrub the timeline (camera flies forward) to watch LODs switch.",
    ]
    return "\n".join(lines)


# ----------------------------------------------------------------- main

def parse_args():
    paths, opts = list(ASSET_PATHS), {"copies": COPIES_PER_VARIATION, "seed": SEED,
                                      "save": None, "render": None}
    if "--" in sys.argv:
        args = sys.argv[sys.argv.index("--") + 1:]
        cli_paths = []
        i = 0
        while i < len(args):
            a = args[i]
            if a in ("--copies", "--seed"):
                opts[a[2:]] = int(args[i + 1])
                i += 2
            elif a in ("--save", "--render"):
                opts[a[2:]] = os.path.abspath(args[i + 1])
                i += 2
            else:
                cli_paths.append(a)
                i += 1
        if cli_paths:
            paths = cli_paths
    return paths, opts


def render_preview(scene, path):
    scene.render.engine = 'BLENDER_WORKBENCH'
    scene.display.shading.color_type = 'TEXTURE'
    scene.render.resolution_x, scene.render.resolution_y = 1280, 720
    scene.render.filepath = path
    with bpy.context.temp_override(scene=scene, view_layer=scene.view_layers[0]):
        bpy.ops.render.render(write_still=True, scene=scene.name)
    log("rendered %s" % path)


def main():
    paths, opts = parse_args()
    if not paths:
        msg = ("No assets given. Edit ASSET_PATHS at the top of this script, or run:\n"
               "  blender --python quixel_lod_example.py -- <asset folder or Fab .zip>")
        log(msg)
        if bpy.app.background:
            raise SystemExit(msg)
        return
    so = scene_optimizer_module()
    scene, report = build_scene(so, paths, opts["copies"], opts["seed"])
    print("\n" + report + "\n")
    txt = bpy.data.texts.get("Megascans LOD Example - report") or bpy.data.texts.new(
        "Megascans LOD Example - report")
    txt.from_string(report + "\n")
    if opts["render"]:
        render_preview(scene, opts["render"])
    if opts["save"]:
        bpy.ops.wm.save_as_mainfile(filepath=opts["save"])
        log("saved %s" % opts["save"])


main()
