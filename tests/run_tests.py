"""
Headless test-suite for scene_optimizer.py

    blender -b --factory-startup --python tests/run_tests.py -- path/to/scene_optimizer.py

Exits with code 1 if any test fails.
"""
import importlib.util
import math
import os
import sys
import tempfile
import time
import traceback

import bpy
from mathutils import Vector

argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
ADDON_PATH = os.path.abspath(argv[0] if argv else os.path.join(os.path.dirname(__file__), "..", "scene_optimizer.py"))
ONLY = argv[1:]  # optional test-name filter

spec = importlib.util.spec_from_file_location("scene_optimizer", ADDON_PATH)
so = importlib.util.module_from_spec(spec)
sys.modules["scene_optimizer"] = so
spec.loader.exec_module(so)
so.register()

RESULTS = []


# ----------------------------------------------------------------- helpers

def reset():
    so._Runtime.busy = False
    so._on_load_post()
    sc = bpy.context.scene
    for o in list(bpy.data.objects):
        bpy.data.objects.remove(o, do_unlink=True)
    for coll in (bpy.data.meshes, bpy.data.cameras, bpy.data.lights, bpy.data.materials,
                 bpy.data.curves):
        for d in list(coll):
            coll.remove(d)
    for c in list(bpy.data.collections):
        bpy.data.collections.remove(c)
    for a in list(bpy.data.actions):
        bpy.data.actions.remove(a)
    s = sc.scene_optimizer
    s.lod_groups.clear()
    s.results.clear()
    for p in s.bl_rna.properties:
        if p.identifier in {"rna_type", "lod_groups", "results"} or p.is_readonly:
            continue
        s.property_unset(p.identifier)
    sc.frame_start, sc.frame_end = 1, 100
    sc.frame_set(1)
    sc.render.resolution_x, sc.render.resolution_y, sc.render.resolution_percentage = 1920, 1080, 100
    return sc


def cube(name, loc, size=1.0, scale=None, coll=None):
    me = bpy.data.meshes.new(name)
    import bmesh
    bm = bmesh.new()
    bmesh.ops.create_cube(bm, size=size)
    bm.to_mesh(me)
    bm.free()
    ob = bpy.data.objects.new(name, me)
    ob.location = loc
    if scale:
        ob.scale = scale
    (coll or bpy.context.scene.collection).objects.link(ob)
    return ob


def camera(loc, target, lens=50.0, ortho=False, name="Cam"):
    cd = bpy.data.cameras.new(name)
    cd.lens = lens
    if ortho:
        cd.type = 'ORTHO'
        cd.ortho_scale = 10.0
    ob = bpy.data.objects.new(name, cd)
    bpy.context.scene.collection.objects.link(ob)
    ob.location = loc
    look_at(ob, target)
    bpy.context.scene.camera = ob
    return ob


def look_at(ob, target):
    d = Vector(target) - Vector(ob.location)
    ob.rotation_euler = d.to_track_quat('-Z', 'Y').to_euler()


def vis(ob):
    return (ob.hide_viewport, ob.hide_render)


def hidden(ob):
    return ob.hide_viewport and ob.hide_render


def shown(ob):
    return not ob.hide_viewport and not ob.hide_render


class Ctx:
    """Operator-free context access (bpy.context works in background)."""
    pass


def apply(preview=False):
    rep = so.run_culling(bpy.context, bpy.context.scene, preview=preview)
    return rep


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)


def test(fn):
    name = fn.__name__
    if ONLY and not any(f in name for f in ONLY):
        return fn
    reset()
    t = time.perf_counter()
    try:
        fn()
        RESULTS.append((name, True, "", time.perf_counter() - t))
        print("PASS  %-45s %.2fs" % (name, time.perf_counter() - t))
    except Exception as exc:
        RESULTS.append((name, False, traceback.format_exc(), time.perf_counter() - t))
        print("FAIL  %-45s %s" % (name, exc))
        traceback.print_exc()
    return fn


def glass_material():
    m = bpy.data.materials.new("Glass")
    m.use_nodes = True
    p = m.node_tree.nodes.get("Principled BSDF")
    p.inputs["Transmission Weight"].default_value = 1.0
    return m


def wall_with_window(name, y, coll=None, hole=(0.0, 0.0), hole_size=1.0, width=20.0, height=10.0, thick=0.2):
    """A wall made of 4 boxes leaving a square window at `hole` (x, z)."""
    hx, hz = hole
    hs = hole_size / 2
    parts = []
    # left / right
    lw = (hx - hs) - (-width / 2)
    parts.append(cube(name + "_L", ((-width / 2 + hx - hs) / 2, y, 0), 1, (lw, thick, height), coll))
    rw = (width / 2) - (hx + hs)
    parts.append(cube(name + "_R", ((hx + hs + width / 2) / 2, y, 0), 1, (rw, thick, height), coll))
    # top / bottom (only between left and right)
    th = (height / 2) - (hz + hs)
    parts.append(cube(name + "_T", (hx, y, (hz + hs + height / 2) / 2), 1, (hole_size, thick, th), coll))
    bh = (hz - hs) - (-height / 2)
    parts.append(cube(name + "_B", (hx, y, (-height / 2 + hz - hs) / 2), 1, (hole_size, thick, bh), coll))
    return parts


# ----------------------------------------------------------------- frustum

@test
def test_frustum_basic():
    sc = bpy.context.scene
    s = sc.scene_optimizer
    s.frustum_margin = 0.0
    s.frustum_padding = 0.0
    camera((0, 0, 0), (0, 10, 0))
    front = cube("Front", (0, 10, 0))
    behind = cube("Behind", (0, -10, 0))
    left = cube("FarLeft", (60, 10, 0))
    big = cube("BigPartial", (8, 10, 0), 1, (12, 1, 1))  # spans x 2..14, frame edge 3.6 at y=10
    far = cube("BeyondClip", (0, 500, 0))
    sc.camera.data.clip_end = 100
    never = cube("NeverCull", (0, -20, 0))
    never.so_never_cull = True
    exc_coll = bpy.data.collections.new("Excl")
    sc.collection.children.link(exc_coll)
    excl = cube("Excluded", (0, -30, 0), coll=exc_coll)
    s.exclude_collection = exc_coll
    pre_hidden = cube("PreHidden", (0, -40, 0))
    pre_hidden.hide_viewport = True
    lamp = bpy.data.objects.new("Lamp", bpy.data.lights.new("Lamp", 'POINT'))
    sc.collection.objects.link(lamp)
    lamp.location = (0, -5, 0)
    rep = apply()
    check(rep["error"] == "", rep["error"])
    check(shown(front), "front object must stay visible")
    check(hidden(behind), "object behind camera must be culled")
    check(hidden(left), "object far outside FOV must be culled")
    check(shown(big), "partially visible object must stay visible")
    check(hidden(far), "object beyond clip end must be culled")
    check(shown(never), "never-cull object must stay visible")
    check(shown(excl), "excluded-collection object must stay visible")
    check(shown(lamp), "lights are never culled")
    check(pre_hidden.hide_viewport and not pre_hidden.hide_render, "pre-hidden untouched")
    names = {r.obj.name for r in s.results}
    check(names == {"Behind", "FarLeft", "BeyondClip"}, "results list: %s" % names)
    check(all(r.reason == 'FRUSTUM' for r in s.results), "reasons")
    # restore
    so.Visibility.restore()
    for ob in (front, behind, left, big, far, never, excl, lamp):
        check(shown(ob), "%s restored" % ob.name)
    check(pre_hidden.hide_viewport, "pre-hidden object stays hidden after restore")
    check(not so.Visibility.managed_objects(), "no state left behind")


@test
def test_frustum_margin_and_padding():
    sc = bpy.context.scene
    s = sc.scene_optimizer
    camera((0, 0, 0), (0, 10, 0))
    # Half-width of the frame at depth 10 for 50 mm / 36 mm sensor = 3.6
    edge = cube("JustOutside", (4.4, 10, 0), 0.5)  # spans 4.15..4.65
    s.frustum_margin, s.frustum_padding = 0.0, 0.0
    apply()
    check(hidden(edge), "outside with no margin -> culled")
    s.frustum_margin = 20.0  # +20% of width each side -> half width 3.6+1.44
    apply()
    check(shown(edge), "inside margin -> kept")
    s.frustum_margin, s.frustum_padding = 0.0, 1.0
    apply()
    check(shown(edge), "inside padding -> kept")


@test
def test_frustum_ortho_and_shift():
    sc = bpy.context.scene
    s = sc.scene_optimizer
    s.frustum_margin, s.frustum_padding = 0.0, 0.0
    cam = camera((0, 0, 0), (0, 10, 0), ortho=True)  # ortho_scale 10 -> x in [-5, 5]
    a = cube("OrthoIn", (4.0, 50, 0), 0.5)
    b = cube("OrthoOut", (6.0, 50, 0), 0.5)
    apply()
    check(shown(a) and hidden(b), "ortho extents")
    cam.data.shift_x = 0.2  # shifts frame by 0.2 * ortho_scale = 2 to the right
    apply()
    check(shown(b), "lens shift respected")


@test
def test_no_camera_and_invalid_camera():
    sc = bpy.context.scene
    ob = cube("A", (0, 0, 0))
    sc.camera = None
    rep = apply()
    check("no active camera" in rep["error"], rep["error"])
    check(shown(ob), "nothing changed")
    emp = bpy.data.objects.new("Empty", None)
    sc.collection.objects.link(emp)
    sc.camera = emp
    rep = apply()
    check("not a Camera" in rep["error"], rep["error"])
    rep = so.update_lods(sc)
    check(rep["error"], "LOD also reports camera error")


@test
def test_cull_target_modes_and_original_state():
    sc = bpy.context.scene
    s = sc.scene_optimizer
    camera((0, 0, 0), (0, 10, 0))
    b = cube("Behind", (0, -10, 0))
    r_only = cube("RenderHiddenByUser", (0, -12, 0))
    r_only.hide_render = True
    s.cull_target = 'VIEWPORT'
    apply()
    check(vis(b) == (True, False), "viewport-only: %s" % (vis(b),))
    s.cull_target = 'RENDER'  # update callback re-syncs
    check(vis(b) == (False, True), "render-only after switch: %s" % (vis(b),))
    s.cull_target = 'BOTH'
    check(vis(b) == (True, True), "both")
    so.Visibility.restore()
    check(vis(b) == (False, False), "restored")
    check(vis(r_only) == (False, True), "user's hide_render preserved: %s" % (vis(r_only),))


@test
def test_user_edit_is_respected():
    sc = bpy.context.scene
    camera((0, 0, 0), (0, 10, 0))
    b = cube("Behind", (0, -10, 0))
    sc.scene_optimizer.cull_target = 'VIEWPORT'
    apply()
    check(vis(b) == (True, False), "culled in viewport only")
    # While culled, the user decides this object should never render.
    b.hide_render = True
    so.Visibility.restore()
    check(vis(b) == (False, True), "manual edit kept on restore: %s" % (vis(b),))
    # And a property we wrote that the user did not touch goes back.
    sc.scene_optimizer.cull_target = 'BOTH'
    b.hide_render = False
    apply()
    b.hide_viewport = False          # user re-enables in viewport only
    so.Visibility.restore()
    check(vis(b) == (False, False), "untouched property restored: %s" % (vis(b),))


@test
def test_persistence_save_reload():
    sc = bpy.context.scene
    camera((0, 0, 0), (0, 10, 0))
    cube("Front", (0, 10, 0))
    cube("Behind", (0, -10, 0))
    apply()
    path = os.path.join(tempfile.gettempdir(), "so_persist_test.blend")
    bpy.ops.wm.save_as_mainfile(filepath=path)
    bpy.ops.wm.open_mainfile(filepath=path)
    b = bpy.data.objects["Behind"]
    check(hidden(b), "still culled after reload")
    check(so.STATE_KEY in b, "state persisted")
    b.name = "RenamedBehind"
    so.Visibility.restore()
    check(shown(bpy.data.objects["RenamedBehind"]), "restore after reload + rename")
    os.remove(path)


@test
def test_preview_does_not_modify_scene():
    sc = bpy.context.scene
    s = sc.scene_optimizer
    camera((0, 0, 0), (0, 10, 0))
    objs = [cube("O%d" % i, (i * 7 - 30, (i % 3) * 10 - 10, 0)) for i in range(10)]
    s.lod_enabled = True
    a0 = cube("T_LOD0", (0, 30, 0))
    a1 = cube("T_LOD1", (0, 30, 0), 0.5)
    g = s.lod_groups.add()
    g.name = "T"
    g.variants.add().obj = a0
    g.variants.add().obj = a1
    so.update_lods(sc)
    s.lod_distances[0] = 1000
    apply()  # apply something first
    before = {o.name: vis(o) for o in bpy.data.objects}
    states = {o.name: dict(o[so.STATE_KEY]) if so.STATE_KEY in o else None for o in bpy.data.objects}
    s.frustum_margin = 50.0
    s.occlusion_enabled = True
    rep = apply(preview=True)
    check(rep["error"] == "", rep["error"])
    after = {o.name: vis(o) for o in bpy.data.objects}
    states2 = {o.name: dict(o[so.STATE_KEY]) if so.STATE_KEY in o else None for o in bpy.data.objects}
    check(before == after, "preview changed visibility")
    check(states == states2, "preview changed stored state")
    check(len(s.results) > 0, "preview produced results")


@test
def test_animated_visibility_and_linked_are_skipped():
    sc = bpy.context.scene
    camera((0, 0, 0), (0, 10, 0))
    anim = cube("AnimVis", (0, -10, 0))
    anim.keyframe_insert("hide_viewport", frame=1)
    drv = cube("DrivenVis", (0, -12, 0))
    drv.driver_add("hide_render")
    # Make a library file with one object and link it in.
    lib_path = os.path.join(tempfile.gettempdir(), "so_lib_test.blend")
    libob = cube("LibCube", (0, -14, 0))
    bpy.data.libraries.write(lib_path, {libob})
    bpy.data.objects.remove(libob)
    with bpy.data.libraries.load(lib_path, link=True) as (src, dst):
        dst.objects = ["LibCube"]
    linked = dst.objects[0]
    sc.collection.objects.link(linked)
    plain = cube("Plain", (0, -16, 0))
    rep = apply()
    check(hidden(plain), "plain culled")
    check(shown(anim), "animated-visibility object skipped")
    check(shown(drv), "driven-visibility object skipped")
    check(not linked.hide_viewport, "linked object untouched")
    check(any("linked" in w for w in rep["warnings"]), "linked warning: %s" % rep["warnings"])
    check(any("animated" in w for w in rep["warnings"]), "animated warning")
    os.remove(lib_path)


@test
def test_batched_writes_update_depsgraph():
    """>32 changes go through the foreach_set batch path. The evaluated
    depsgraph (what actually renders) must reflect every change."""
    sc = bpy.context.scene
    camera((0, 0, 0), (0, 10, 0))
    behind = [cube("B%03d" % i, (i * 0.5 - 50, -10 - i % 7, 0), 0.3) for i in range(200)]
    front = [cube("F%03d" % i, (i * 0.2 - 2, 20, 0), 0.1) for i in range(20)]
    user_hidden = behind[5]
    user_hidden.hide_render = True
    apply()
    dg = bpy.context.evaluated_depsgraph_get()
    evaluated = {i.object.original.name for i in dg.object_instances if not i.is_instance}
    check(not any(o.name in evaluated for o in behind), "culled objects left in depsgraph")
    check(all(o.name in evaluated for o in front), "visible objects missing from depsgraph")
    check(all(hidden(o) for o in behind), "flags")
    so.Visibility.restore()
    dg = bpy.context.evaluated_depsgraph_get()
    evaluated = {i.object.original.name for i in dg.object_instances if not i.is_instance}
    check(all(o.name in evaluated for o in behind), "restored objects back in depsgraph")
    check(vis(user_hidden) == (False, True), "user's hide_render preserved through batch")
    check(all(vis(o) == (False, False) for o in behind if o != user_hidden), "restored flags")


# ----------------------------------------------------------------- occlusion

@test
def test_occlusion_basic():
    sc = bpy.context.scene
    s = sc.scene_optimizer
    s.occlusion_enabled = True
    camera((0, 0, 0), (0, 10, 0))
    # wall x in [-2, 2] at depth 10 -> shadows |x| < 4 at depth 20
    wall_with_window("Wall", 10, hole=(0.0, 0.0), hole_size=0.6, width=4.0)
    hidden_box = cube("BehindWall", (-1.5, 20, 0), 1.0)
    peek = cube("Peeking", (4.3, 20, 0), 1.0)  # spans 3.8..4.8: 0.2 hidden, 0.8 visible
    window = cube("ThroughWindow", (0, 25, 0), 0.4)  # tiny, only visible through the hole
    behind_window_blocked = cube("BlockedSmall", (3, 25, 3), 0.4)
    rep = apply()
    check(rep["error"] == "", rep["error"])
    check(hidden(hidden_box), "box behind wall occluded")
    check(shown(peek), "partially visible box kept")
    check(shown(window), "object visible through window kept")
    check(hidden(behind_window_blocked), "small box behind solid wall occluded")
    for part in ("Wall_L", "Wall_R", "Wall_T", "Wall_B"):
        check(shown(bpy.data.objects[part]), "wall parts visible")
    reasons = {r.obj.name: r.reason for r in s.results}
    check(reasons.get("BehindWall") == "OCCLUSION", "reason OCCLUSION: %s" % reasons)


@test
def test_occlusion_transparency_and_flags():
    sc = bpy.context.scene
    s = sc.scene_optimizer
    s.occlusion_enabled = True
    camera((0, 0, 0), (0, 10, 0))
    # three walls side by side at depth 10; boxes at depth 20 project to x/2
    glass = cube("GlassWall", (-6, 10, 0), 1, (6, 0.2, 10))
    glass.data.materials.append(glass_material())
    behind_glass = cube("BehindGlass", (-7, 20, 0))
    flagged = cube("FlaggedWall", (0, 10, 0), 1, (6, 0.2, 10))
    flagged.so_not_occluder = True
    behind_flagged = cube("BehindFlagged", (0, 20, 0))
    solid = cube("SolidWall", (6, 10, 0), 1, (6, 0.2, 10))
    behind_solid = cube("BehindSolid", (7, 20, 0))
    apply()
    check(shown(behind_glass), "transparent wall does not occlude")
    check(shown(behind_flagged), "not-occluder flag respected")
    check(hidden(behind_solid), "solid wall occludes")
    # Occluder collection without the solid wall -> nothing occludes
    oc = bpy.data.collections.new("Occluders")
    sc.collection.children.link(oc)
    s.occluder_collection = oc
    apply()
    check(shown(behind_solid), "only occluder-collection objects occlude")
    oc.objects.link(solid)
    apply()
    check(hidden(behind_solid), "wall in occluder collection occludes")
    # Glass ignored only when option enabled
    s.occluder_collection = None
    s.occlusion_ignore_transparent = False
    apply()
    check(hidden(behind_glass), "glass occludes when transparency is not ignored")


@test
def test_occlusion_collection_instances():
    sc = bpy.context.scene
    s = sc.scene_optimizer
    s.occlusion_enabled = True
    camera((0, 0, 0), (0, 10, 0))
    src = bpy.data.collections.new("Prop")
    sc.collection.children.link(src)
    cube("PropMesh", (0, 0, 0), 1.0, coll=src)
    # exclude the source collection from the view layer (the usual setup)
    bpy.context.view_layer.layer_collection.children["Prop"].exclude = True
    def inst(name, loc):
        e = bpy.data.objects.new(name, None)
        e.instance_type = 'COLLECTION'
        e.instance_collection = src
        e.location = loc
        sc.collection.objects.link(e)
        return e
    wall = cube("Wall", (0, 10, 0), 1, (12, 0.2, 10))
    i_hidden = inst("InstBehind", (0, 20, 0))
    i_visible = inst("InstVisible", (0, 5, 0))
    i_out = inst("InstOutside", (0, -10, 0))
    # instanced wall as occluder: a collection instance of a wall
    wsrc = bpy.data.collections.new("WallAsset")
    sc.collection.children.link(wsrc)
    cube("WallAssetMesh", (0, 0, 0), 1, (12, 0.2, 10), coll=wsrc)
    bpy.context.view_layer.layer_collection.children["WallAsset"].exclude = True
    w2 = bpy.data.objects.new("WallInst", None)
    w2.instance_type = 'COLLECTION'
    w2.instance_collection = wsrc
    w2.location = (0, 10, 0)
    sc.collection.objects.link(w2)
    wall.location = (100, 100, 0)  # move the real wall away: only instanced wall occludes
    i_hidden2 = inst("InstBehind2", (2, 30, 0))
    apply()
    check(i_hidden.hide_viewport, "instance behind instanced wall occluded")
    check(i_hidden2.hide_viewport, "second instance occluded")
    check(not i_visible.hide_viewport, "visible instance kept")
    check(i_out.hide_viewport, "instance behind camera frustum culled")
    check(not w2.hide_viewport, "occluding instance itself visible")


@test
def test_occlusion_near_plane_and_inside():
    sc = bpy.context.scene
    s = sc.scene_optimizer
    s.occlusion_enabled = True
    camera((0, 0, 0), (0, 10, 0))
    room = cube("Room", (0, 0, 0), 1, (30, 30, 30))   # camera inside a closed box
    inside = cube("InsideRoom", (0, 8, 0))
    outside = cube("OutsideRoom", (0, 40, 0), 2)
    apply()
    check(shown(room), "object surrounding the camera stays visible")
    check(shown(inside), "object inside room visible")
    check(hidden(outside), "object outside closed room occluded")


@test
def test_occlusion_without_frustum_keeps_offscreen():
    sc = bpy.context.scene
    s = sc.scene_optimizer
    s.frustum_enabled = False
    s.occlusion_enabled = True
    camera((0, 0, 0), (0, 10, 0))
    off = cube("Offscreen", (0, -10, 0))
    wall = cube("Wall", (0, 10, 0), 1, (12, 0.2, 10))
    occl = cube("Occluded", (0, 20, 0))
    apply()
    check(shown(off), "frustum disabled: off-screen objects are not occlusion-culled")
    check(hidden(occl), "occlusion still works")


@test
def test_occlusion_narrow_slit():
    """A slit ~13 px wide at 1920 px (wider than Balanced spacing) must be
    found; the README documents that narrower gaps can be missed."""
    sc = bpy.context.scene
    s = sc.scene_optimizer
    s.occlusion_enabled = True
    camera((0, 0, 0), (0, 10, 0))
    # frame width at depth 10 = 7.2 units -> 266 px/unit; slit 0.05 = 13 px
    cube("WallLeft", (-5.025, 10, 0), 1, (10, 0.2, 10))
    cube("WallRight", (5.025, 10, 0), 1, (10, 0.2, 10))
    seen = cube("BehindSlit", (0, 20, 0), 1.0)
    rep = apply()
    check(shown(seen), "object seen only through a 13 px slit is kept")
    # with a very coarse spacing the same slit is (expectedly) missed
    s.occlusion_preset = 'CUSTOM'
    s.occlusion_sample_spacing = 100.0
    s.occlusion_min_samples = 2
    apply()
    print("      coarse sampling result for slit object: %s" % ("kept" if shown(seen) else "culled (documented limitation)"))


def gn_scatter(name, loc, points):
    """Mesh whose geometry-nodes modifier replaces it with cube instances at
    `points` (object-local offsets)."""
    import bmesh
    me = bpy.data.meshes.new(name)
    bm = bmesh.new()
    for p in points:
        bm.verts.new(p)
    bm.to_mesh(me)
    bm.free()
    ob = bpy.data.objects.new(name, me)
    ob.location = loc
    bpy.context.scene.collection.objects.link(ob)
    tree = bpy.data.node_groups.new(name + "_GN", 'GeometryNodeTree')
    tree.interface.new_socket("Geometry", in_out='INPUT', socket_type='NodeSocketGeometry')
    tree.interface.new_socket("Geometry", in_out='OUTPUT', socket_type='NodeSocketGeometry')
    n_in = tree.nodes.new("NodeGroupInput")
    n_out = tree.nodes.new("NodeGroupOutput")
    iop = tree.nodes.new("GeometryNodeInstanceOnPoints")
    cubeprim = tree.nodes.new("GeometryNodeMeshCube")
    tree.links.new(n_in.outputs[0], iop.inputs["Points"])
    tree.links.new(cubeprim.outputs[0], iop.inputs["Instance"])
    tree.links.new(iop.outputs[0], n_out.inputs[0])
    mod = ob.modifiers.new("GN", 'NODES')
    mod.node_group = tree
    return ob


@test
def test_geometry_nodes_instance_bounds():
    sc = bpy.context.scene
    camera((0, 0, 0), (0, 10, 0))
    # origin in view, but every instance is behind the camera -> culled
    a = gn_scatter("ScatterBehind", (0, 10, 0), [(0, -20, 0), (2, -25, 0)])
    # origin behind the camera, instances in view -> kept
    b = gn_scatter("ScatterInFront", (0, -10, 0), [(0, 20, 0), (1, 25, 0)])
    apply()
    check(hidden(a), "GN owner culled when all instances are out of view")
    check(shown(b), "GN owner kept when instances are in view")
    sc.scene_optimizer.occlusion_enabled = True
    cube("Wall", (0, 5, 0), 1, (10, 0.2, 10))
    c = gn_scatter("ScatterOccluded", (0, 0, 0), [(0, 20, 0), (1, 25, 0)])
    bpy.data.objects.remove(b)
    apply()
    check(hidden(c), "GN instances behind a wall -> owner occluded")


# ----------------------------------------------------------------- LOD

def make_lod_group(name, loc, sizes=(1.0, 0.8, 0.6), parent=None):
    s = bpy.context.scene.scene_optimizer
    objs = []
    for i, sz in enumerate(sizes):
        o = cube("%s_LOD%d" % (name, i), loc if parent is None else (0, 0, 0), sz)
        if parent is not None:
            o.parent = parent
        objs.append(o)
    g = s.lod_groups.add()
    g.name = name
    for o in objs:
        g.variants.add().obj = o
    return g, objs


@test
def test_lod_distance_switching():
    sc = bpy.context.scene
    s = sc.scene_optimizer
    s.lod_enabled = True
    s.lod_distances[0], s.lod_distances[1] = 10.0, 30.0
    cam = camera((0, -5, 0), (0, 0, 0))
    g, objs = make_lod_group("Tree", (0, 0, 0))
    mats = [o.matrix_world.copy() for o in objs]
    for dist, expect in ((5, 0), (20, 1), (50, 2), (9.9, 0), (10.1, 1)):
        cam.location = (0, -dist, 0)
        bpy.context.view_layer.update()
        so.update_lods(sc)
        vis_idx = [i for i, o in enumerate(objs) if shown(o)]
        check(vis_idx == [expect], "dist %s: visible %s expected [%d]" % (dist, vis_idx, expect))
        for o in objs:
            check(o.hide_viewport == o.hide_render, "viewport/render consistent")
    for o, m in zip(objs, mats):
        check(o.matrix_world == m, "transforms untouched")
    # Max draw distance
    s.lod_hide_beyond, s.lod_max_distance = True, 100.0
    cam.location = (0, -150, 0)
    bpy.context.view_layer.update()
    so.update_lods(sc)
    check(all(hidden(o) for o in objs), "beyond max distance: all hidden")
    check(all("DISTANCE" in so.Visibility.reasons(o) for o in objs), "DISTANCE reason")
    # Disabling LOD restores
    s.lod_enabled = False
    check(all(shown(o) for o in objs), "disabling LOD restores all variants")
    check(not so.Visibility.managed_objects(), "no state")


@test
def test_lod_colors_visualize():
    sc = bpy.context.scene
    s = sc.scene_optimizer
    s.lod_enabled = True
    camera((0, -5, 0), (0, 0, 0))
    _g, objs = make_lod_group("Tree", (0, 0, 0))
    plain = cube("Plain", (5, 0, 0))
    s.lod_colors = True
    for lvl, o in enumerate(objs):
        mod = o.modifiers.get(so.LOD_PREVIEW_MOD)
        check(mod is not None and not mod.show_render, "LOD%d gets a viewport-only colour modifier" % lvl)
        check(mod.node_group.name == "SO LOD Color %d" % lvl, "LOD%d uses its own colour" % lvl)
    check(plain.modifiers.get(so.LOD_PREVIEW_MOD) is None, "objects outside LOD groups are untouched")
    dg = bpy.context.evaluated_depsgraph_get()
    me = objs[1].evaluated_get(dg).data
    check(me.materials[me.polygons[0].material_index].name == "SO LOD1", "LOD1 is drawn in the LOD1 colour")
    check(len(objs[1].data.materials) == 0, "original mesh data unchanged")
    # a group added while on is coloured on the next LOD update
    _g2, objs2 = make_lod_group("Bush", (3, 0, 0))
    so.update_lods(sc)
    check(all(o.modifiers.get(so.LOD_PREVIEW_MOD) for o in objs2), "new groups follow on LOD update")
    s.lod_colors = False
    check(not any(o.modifiers.get(so.LOD_PREVIEW_MOD) for o in objs + objs2), "switching off removes them")
    # one switch also drives the scatter systems
    ground = cube("Ground", (0, 0, -1))
    blade = cube("Blade_LOD0", (50, 50, 0), size=0.1)
    system = so.new_scatter_system(sc, ground, [blade])
    s.lod_colors = True
    check(system.lod_colors, "Visualize LODs switches scatter LOD colours on")
    s.lod_colors = False
    check(not system.lod_colors, "and off again")
    so.delete_scatter_system(sc, 0)


@test
def test_lod_custom_lock_and_hysteresis():
    sc = bpy.context.scene
    s = sc.scene_optimizer
    s.lod_enabled = True
    cam = camera((0, -20, 0), (0, 0, 0))
    g, objs = make_lod_group("Rock", (0, 0, 0))
    g.use_custom_distances = True
    g.distances[0], g.distances[1] = 50.0, 100.0
    so.update_lods(sc)
    check(shown(objs[0]) and hidden(objs[1]), "custom distances")
    g.lock_level = 2
    so.update_lods(sc)
    check([shown(o) for o in objs] == [False, False, True], "lock level")
    g.lock_level = -1
    # hysteresis in live mode: at 51 with 5% band, stay at LOD0 coming from LOD0
    so.update_lods(sc, live=True)
    cam.location = (0, -51, 0)
    bpy.context.view_layer.update()
    so.update_lods(sc, live=True)
    check(shown(objs[0]), "hysteresis keeps LOD0 at 51 (band 52.5)")
    cam.location = (0, -53, 0)
    bpy.context.view_layer.update()
    so.update_lods(sc, live=True)
    check(shown(objs[1]), "switch at 53")
    cam.location = (0, -49, 0)
    bpy.context.view_layer.update()
    so.update_lods(sc, live=True)
    check(shown(objs[1]), "hysteresis keeps LOD1 at 49 (band 47.5)")
    check(so.choose_lod_level(10, [5, 20]) == 1, "choose_lod_level basic")
    check(so.choose_lod_level(100, [5, 20], max_dist=50) == -1, "max distance")


@test
def test_lod_hidden_lod0_with_moving_parent():
    """LOD0 hidden -> Blender stops updating its matrix_world. The add-on must
    still measure the correct distance when the parent moves."""
    sc = bpy.context.scene
    s = sc.scene_optimizer
    s.lod_enabled = True
    s.lod_distances[0], s.lod_distances[1] = 10.0, 30.0
    camera((0, 0, 0), (0, 10, 0))
    root = bpy.data.objects.new("Root", None)
    sc.collection.objects.link(root)
    root.location = (0, 20, 0)
    g, objs = make_lod_group("Bld", None, parent=root)
    bpy.context.view_layer.update()
    so.update_lods(sc)
    check(shown(objs[1]), "LOD1 at 20 m")
    root.location = (0, 5, 0)  # move close; LOD0 is hidden so its matrix is stale
    bpy.context.view_layer.update()
    so.update_lods(sc)
    check(shown(objs[0]) and hidden(objs[1]), "LOD0 after parent moved close: %s" % [vis(o) for o in objs])
    root.location = (0, 60, 0)
    bpy.context.view_layer.update()
    so.update_lods(sc)
    check(shown(objs[2]), "LOD2 after parent moved far")


@test
def test_lod_autodetect():
    sc = bpy.context.scene
    s = sc.scene_optimizer
    for i in range(3):
        cube("Tree_LOD%d" % i, (0, 0, 0))
        cube("Tree_LOD%d.001" % i, (5, 0, 0))
    cube("rock.lod0", (0, 5, 0))
    cube("rock.lod1", (0, 5, 0))
    root = bpy.data.objects.new("SM_House", None)
    sc.collection.objects.link(root)
    for i in range(2):
        o = cube("House_LOD%d" % i, (0, 0, 0))
        o.parent = root
    cube("Lonely_LOD0", (0, 0, 0))
    cube("Dup_LOD1", (0, 0, 0))
    cube("Dup_LOD1.002", (0, 0, 0))  # different dup -> separate single-variant buckets
    groups, problems = so.auto_detect_groups(list(sc.objects))
    names = {n: [o.name for o in objs] for n, objs in groups}
    check(names.get("Tree") == ["Tree_LOD0", "Tree_LOD1", "Tree_LOD2"], names)
    check(names.get("Tree.001") == ["Tree_LOD0.001", "Tree_LOD1.001", "Tree_LOD2.001"], names)
    check(names.get("rock") == ["rock.lod0", "rock.lod1"], names)
    check(names.get("House") == ["House_LOD0", "House_LOD1"], names)
    check(len(groups) == 4, "group count %d" % len(groups))
    check(any("Lonely" in p for p in problems), problems)
    # operator path (collection source)
    coll = bpy.data.collections.new("Assets")
    sc.collection.children.link(coll)
    for o in list(sc.collection.objects):
        coll.objects.link(o)
    s.lod_detect_collection = coll
    bpy.ops.scene_optimizer.lod_autodetect()
    check(len(s.lod_groups) == 4, "operator created %d groups" % len(s.lod_groups))
    bpy.ops.scene_optimizer.lod_autodetect()
    check(len(s.lod_groups) == 4, "second run does not duplicate groups")


@test
def test_lod_with_culling_integration():
    sc = bpy.context.scene
    s = sc.scene_optimizer
    s.lod_enabled = True
    s.occlusion_enabled = True
    s.lod_distances[0], s.lod_distances[1] = 8.0, 100.0
    camera((0, 0, 0), (0, 10, 0))
    # Co-located variants in plain view: must not occlude each other.
    g1, front = make_lod_group("Front", (0, 12, 0), sizes=(1.0, 1.05, 0.9))
    # A group behind a wall: culled as a unit.
    cube("Wall", (3, 10, 0), 1, (4, 0.2, 10))      # x 1..5 at depth 10
    g2, back = make_lod_group("Back", (5, 20, 0))   # projects to 2.25..2.75
    # A group behind the camera
    g3, out = make_lod_group("Out", (0, -20, 0))
    rep = apply()
    check(rep["error"] == "", rep["error"])
    check([shown(o) for o in front] == [False, True, False], "front group shows LOD1 only: %s"
          % [vis(o) for o in front])
    check(all(hidden(o) for o in back), "occluded group fully hidden")
    check(all("OCCLUSION" in so.Visibility.reasons(o) for o in back), "occlusion reason on all variants")
    check(all("FRUSTUM" in so.Visibility.reasons(o) for o in out), "frustum reason on all variants")
    # Re-applying must give the same result (no flip-flop from hidden state)
    apply()
    check([shown(o) for o in front] == [False, True, False], "stable on re-apply")
    check(all(hidden(o) for o in back), "stable occlusion on re-apply")
    # Restoring culling keeps LOD switching
    bpy.ops.scene_optimizer.restore(what='CULL')
    check([shown(o) for o in back].count(True) == 1, "after culling restore LOD still exclusive")
    bpy.ops.scene_optimizer.restore(what='ALL')
    check(all(shown(o) for o in front + back + out), "full restore")


@test
def test_lod_frame_change_handler():
    sc = bpy.context.scene
    s = sc.scene_optimizer
    s.lod_enabled = True
    s.lod_distances[0], s.lod_distances[1] = 10.0, 30.0
    cam = camera((0, -5, 0), (0, 0, 0))
    g, objs = make_lod_group("Car", (0, 0, 0))
    cam.location = (0, -5, 0)
    cam.keyframe_insert("location", frame=1)
    cam.location = (0, -50, 0)
    cam.keyframe_insert("location", frame=100)
    s.lod_live = True
    sc.frame_set(1)
    check(shown(objs[0]), "frame 1 -> LOD0")
    sc.frame_set(100)
    check(shown(objs[2]) and hidden(objs[0]), "frame 100 -> LOD2: %s" % [vis(o) for o in objs])
    sc.frame_set(50)  # ~ 27 m
    check(shown(objs[1]), "frame 50 -> LOD1")
    s.lod_live = False
    sc.frame_set(1)
    check(shown(objs[1]), "no per-frame work when live is off")


# ----------------------------------------------------------------- multi-frame

@test
def test_frame_range_union():
    sc = bpy.context.scene
    s = sc.scene_optimizer
    cam = camera((0, 0, 0), (0, 10, 0))
    a = cube("A_North", (0, 10, 0))
    b = cube("B_South", (0, -10, 0))
    cam.keyframe_insert("rotation_euler", frame=1)
    look_at(cam, (0, -10, 0))
    cam.keyframe_insert("rotation_euler", frame=100)
    sc.frame_set(1)
    apply()
    check(shown(a) and hidden(b), "current frame only")
    s.frame_mode = 'RANGE'
    s.frame_step = 10
    apply()
    check(shown(a) and shown(b), "frame range keeps objects seen at any frame")
    check(sc.frame_current == 1, "frame restored")
    s.occlusion_enabled = True
    s.static_geometry = True
    cube("WallNorth", (0, 5, 0), 1, (10, 0.2, 10))
    c = cube("C_BehindWall", (0, 15, 0))
    apply()
    check(hidden(c), "static-geometry occlusion over a range")
    check(shown(b), "B still kept")


# ----------------------------------------------------------------- misc

@test
def test_unregister_restores():
    sc = bpy.context.scene
    camera((0, 0, 0), (0, 10, 0))
    b = cube("Behind", (0, -10, 0))
    apply()
    check(hidden(b), "culled")
    so.unregister()
    try:
        check(shown(b), "unregister restored visibility")
    finally:
        so.register()


@test
def test_operators_run():
    sc = bpy.context.scene
    s = sc.scene_optimizer
    camera((0, 0, 0), (0, 10, 0))
    cube("Front", (0, 10, 0))
    cube("Behind", (0, -10, 0))
    check(bpy.ops.scene_optimizer.preview() == {'FINISHED'}, "preview op")
    check(len(s.results) == 1, "preview results")
    check(bpy.ops.scene_optimizer.select_results(reason='ALL') == {'FINISHED'}, "select")
    check(bpy.data.objects["Behind"].select_get(), "selected culled object")
    check(bpy.ops.scene_optimizer.apply() == {'FINISHED'}, "apply op")
    check(bpy.ops.scene_optimizer.restore(what='ALL') == {'FINISHED'}, "restore op")
    for o in sc.objects:
        o.select_set(o.type == 'MESH')
    check(bpy.ops.scene_optimizer.lod_group_from_selected() == {'FINISHED'}, "group op")
    check(len(s.lod_groups) == 1 and len(s.lod_groups[0].variants) == 2, "group created")
    s.lod_enabled = True
    check(bpy.ops.scene_optimizer.lod_apply() == {'FINISHED'}, "lod apply")
    check(bpy.ops.scene_optimizer.lod_snap_variants(all=True) == {'FINISHED'}, "snap")
    check(bpy.ops.scene_optimizer.lod_group_remove(all=True) == {'FINISHED'}, "remove")
    check(all(shown(o) for o in sc.objects if o.type == 'MESH'), "removed group restored")
    sc.camera = None
    try:  # in background mode an operator ERROR report raises RuntimeError
        res = bpy.ops.scene_optimizer.apply()
    except RuntimeError as exc:
        res = {'CANCELLED'} if "no active camera" in str(exc) else {'?'}
    check(res == {'CANCELLED'}, "apply without camera cancels")


@test
def test_performance_smoke():
    sc = bpy.context.scene
    s = sc.scene_optimizer
    s.occlusion_enabled = True
    camera((0, -60, 5), (0, 0, 0))
    base = bpy.data.meshes.new("Base")
    import bmesh
    bm = bmesh.new()
    bmesh.ops.create_icosphere(bm, subdivisions=2, radius=0.5)
    bm.to_mesh(base)
    bm.free()
    n = 0
    for x in range(-40, 41, 2):
        for y in range(-40, 41, 2):
            o = bpy.data.objects.new("P_%d_%d" % (x, y), base)
            o.location = (x, y, 0)
            sc.collection.objects.link(o)
            n += 1
    for i in range(8):
        cube("Building%d" % i, (-35 + i * 10, -20, 5), 1, (8, 8, 10))
    t = time.perf_counter()
    rep = apply(preview=True)
    dt = time.perf_counter() - t
    print("      %d objects: %d frustum, %d occluded, %d rays, %d tris, %.2fs" % (
        n, rep["frustum"], rep["occlusion"], rep["rays"], rep["tris"], dt))
    check(rep["occlusion"] > 0 and rep["frustum"] > 0, "both systems active")
    check(dt < 60, "reasonable time")


# ----------------------------------------------------------------- summary

failed = [r for r in RESULTS if not r[1]]
print("\n%d tests, %d passed, %d failed  (Blender %s)" % (
    len(RESULTS), len(RESULTS) - len(failed), len(failed), bpy.app.version_string))
for name, _ok, tb, _t in failed:
    print("--- %s\n%s" % (name, tb))
sys.stdout.flush()
os._exit(1 if failed else 0)
