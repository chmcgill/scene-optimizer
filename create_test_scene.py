"""
Scene Optimizer - test scene generator.

Builds a small city block that exercises all three systems:

  * Frustum culling : props behind / beside the camera, props beyond clip end
  * Occlusion       : props hidden behind buildings, a prop visible only through
                      a window, a prop behind a glass shelter (must stay visible),
                      a prop peeking around a corner (must stay visible)
  * LOD switching   : 12 trees named Tree_LOD0/1/2(.###) along the street,
                      colour coded LOD0 green / LOD1 yellow / LOD2 red
  * Collection instances (benches), an animated camera fly-through (1-240)

Run it from Blender's Text Editor (Run Script) or:
    blender --python create_test_scene.py

WARNING: it clears the current file's objects first.
"""
import math
import bpy
import bmesh
from mathutils import Vector

SEED_COLORS = {
    "building": (0.55, 0.55, 0.6, 1), "ground": (0.25, 0.27, 0.25, 1),
    "prop": (0.2, 0.45, 0.9, 1), "hidden": (0.9, 0.2, 0.7, 1),
    "lod0": (0.1, 0.8, 0.2, 1), "lod1": (0.95, 0.85, 0.1, 1), "lod2": (0.95, 0.2, 0.1, 1),
    "trunk": (0.35, 0.22, 0.1, 1), "bench": (0.6, 0.4, 0.2, 1),
}


def material(name, rgba, glass=False):
    m = bpy.data.materials.get(name) or bpy.data.materials.new(name)
    m.diffuse_color = rgba if not glass else (rgba[0], rgba[1], rgba[2], 0.3)
    if bpy.app.version < (5, 0, 0):
        m.use_nodes = True
    p = m.node_tree.nodes.get("Principled BSDF")
    if p is not None:
        p.inputs["Base Color"].default_value = rgba
        if glass:
            p.inputs["Transmission Weight"].default_value = 1.0
            p.inputs["Roughness"].default_value = 0.05
    if glass and hasattr(m, "surface_render_method"):
        m.surface_render_method = 'BLENDED'
    return m


def coll(name, parent=None):
    c = bpy.data.collections.get(name) or bpy.data.collections.new(name)
    if c.name not in (parent or bpy.context.scene.collection).children:
        (parent or bpy.context.scene.collection).children.link(c)
    return c


def box(name, loc, dims, mat, collection):
    me = bpy.data.meshes.new(name)
    bm = bmesh.new()
    bmesh.ops.create_cube(bm, size=1.0)
    bmesh.ops.scale(bm, vec=dims, verts=bm.verts)
    bm.to_mesh(me)
    bm.free()
    me.materials.append(mat)
    ob = bpy.data.objects.new(name, me)
    ob.location = loc
    collection.objects.link(ob)
    return ob


def sphere(name, loc, radius, subdiv, mat, collection, z_scale=1.0):
    me = bpy.data.meshes.new(name)
    bm = bmesh.new()
    bmesh.ops.create_icosphere(bm, subdivisions=subdiv, radius=radius)
    bmesh.ops.scale(bm, vec=(1, 1, z_scale), verts=bm.verts)
    bm.to_mesh(me)
    bm.free()
    me.materials.append(mat)
    ob = bpy.data.objects.new(name, me)
    ob.location = loc
    collection.objects.link(ob)
    return ob


def tree_variant(name, loc, level, collection):
    """One mesh = trunk + crown, detail decreasing with level."""
    me = bpy.data.meshes.new(name)
    bm = bmesh.new()
    segs = (16, 8, 4)[level]
    bmesh.ops.create_cone(bm, cap_ends=True, segments=segs, radius1=0.25, radius2=0.2, depth=2.5)
    bmesh.ops.translate(bm, vec=(0, 0, 1.25), verts=bm.verts)
    crown = bmesh.ops.create_icosphere(bm, subdivisions=(3, 2, 1)[level], radius=1.6)
    bmesh.ops.translate(bm, vec=(0, 0, 3.5), verts=crown["verts"])
    for f in bm.faces:
        f.material_index = 1 if any(v.co.z > 2.4 for v in f.verts) else 0
    bm.to_mesh(me)
    bm.free()
    me.materials.append(material("Trunk", SEED_COLORS["trunk"]))
    me.materials.append(material("Crown_LOD%d" % level, SEED_COLORS["lod%d" % level]))
    ob = bpy.data.objects.new(name, me)
    ob.location = loc
    collection.objects.link(ob)
    return ob


def build():
    scene = bpy.context.scene
    for o in list(bpy.data.objects):
        bpy.data.objects.remove(o, do_unlink=True)
    for c in list(bpy.data.collections):
        bpy.data.collections.remove(c)

    m_bld = material("Building", SEED_COLORS["building"])
    m_gnd = material("Ground", SEED_COLORS["ground"])
    m_prop = material("Prop", SEED_COLORS["prop"])
    m_hid = material("ShouldBeOccluded", SEED_COLORS["hidden"])
    m_glass = material("Glass", (0.7, 0.85, 1.0, 1.0), glass=True)

    c_env = coll("Environment")
    c_bld = coll("Buildings", c_env)
    c_props = coll("Props")
    c_trees = coll("Trees_LOD")
    c_never = coll("NeverCull")

    # Ground + street
    box("Ground", (0, 60, -0.05), (120, 200, 0.1), m_gnd, c_env).so_never_cull = True

    # Two rows of buildings along a street that runs along +Y (street x in -6..6)
    k = 0
    for side in (-1, 1):
        for i in range(8):
            y = 10 + i * 18
            h = 12 + (i * 7 % 5) * 4
            box("Building_%02d" % k, (side * 14, y, h / 2), (14, 14, h), m_bld, c_bld)
            k += 1

    # Visible props on the street
    for i in range(10):
        box("Crate_Street_%02d" % i, ((-1) ** i * 3, 8 + i * 12, 0.5), (1, 1, 1), m_prop, c_props)
    # Props hidden behind buildings (magenta = should be occlusion-culled)
    for i in range(12):
        side = -1 if i % 2 else 1
        box("Crate_BehindBuilding_%02d" % i, (side * 26, 12 + i * 11, 0.6), (1.2, 1.2, 1.2), m_hid, c_props)
    # Props behind the camera / outside the view (frustum culled)
    for i in range(8):
        box("Crate_BehindCamera_%02d" % i, (-8 + i * 2.3, -25, 0.5), (1, 1, 1), m_prop, c_props)
    for i in range(4):
        box("Crate_FarSide_%02d" % i, (-90 + i * 3, 30, 0.5), (1, 1, 1), m_prop, c_props)

    # Wall with a window, a prop only visible through it
    wall_y = 70
    box("Wall_Left", (-4.0, wall_y, 3), (4.0, 0.3, 6), m_bld, c_bld)
    box("Wall_Right", (4.0, wall_y, 3), (4.0, 0.3, 6), m_bld, c_bld)
    box("Wall_Top", (0, wall_y, 4.75), (4.0, 0.3, 2.5), m_bld, c_bld)
    box("Wall_Bottom", (0, wall_y, 0.75), (4.0, 0.3, 1.5), m_bld, c_bld)
    box("Crate_ThroughWindow", (0.3, wall_y + 12, 2.3), (0.8, 0.8, 0.8), m_prop, c_props)
    box("Crate_BehindWallSolid", (-4.5, wall_y + 6, 1.5), (1, 1, 1), m_hid, c_props)

    # Glass bus shelter, prop behind glass must stay visible
    box("BusShelter_Glass", (4.0, 30, 1.5), (3.0, 0.1, 3), m_glass, c_env)
    box("Crate_BehindGlass", (4.0, 31.5, 0.5), (0.8, 0.8, 0.8), m_prop, c_props)

    # Prop peeking around a building corner (must stay visible)
    box("Crate_PeekingCorner", (7.6, 19, 0.5), (1.5, 1.5, 1.0), m_prop, c_props)  # in an alley mouth

    # Collection-instance benches (source collection excluded from view layer)
    c_bench_src = coll("Asset_Bench")
    box("Bench_Seat", (0, 0, 0.45), (2.0, 0.5, 0.1), material("Bench", SEED_COLORS["bench"]), c_bench_src)
    box("Bench_Back", (0, 0.22, 0.8), (2.0, 0.08, 0.6), material("Bench", SEED_COLORS["bench"]), c_bench_src)
    bpy.context.view_layer.layer_collection.children["Asset_Bench"].exclude = True
    for i, (x, y) in enumerate([(5, 15), (-5, 40), (-25, 60), (5, -20), (25, 90)]):
        e = bpy.data.objects.new("BenchInstance_%02d" % i, None)
        e.instance_type = 'COLLECTION'
        e.instance_collection = c_bench_src
        e.location = (x, y, 0)
        c_props.objects.link(e)

    # LOD trees along the street at increasing distances
    for i in range(12):
        loc = ((-1) ** i * 5.2, 6 + i * 11, 0)
        suffix = "" if i == 0 else ".%03d" % i
        for level in range(3):
            tree_variant("Tree_LOD%d%s" % (level, suffix), loc, level, c_trees)

    # Never-cull hero object behind the camera
    sphere("Hero_NeverCull", (0, -15, 2), 1.5, 3, m_prop, c_never)

    # Sun + camera
    sun = bpy.data.objects.new("Sun", bpy.data.lights.new("Sun", 'SUN'))
    sun.rotation_euler = (math.radians(50), 0, math.radians(30))
    scene.collection.objects.link(sun)
    cd = bpy.data.cameras.new("Camera")
    cd.lens = 35
    cd.clip_end = 400
    cam = bpy.data.objects.new("Camera", cd)
    scene.collection.objects.link(cam)
    scene.camera = cam
    scene.frame_start, scene.frame_end = 1, 240
    for frame, y in ((1, -8), (240, 120)):
        cam.location = (0, y, 1.7)
        cam.rotation_euler = (math.radians(88), 0, 0)
        cam.keyframe_insert("location", frame=frame)
    scene.frame_set(1)
    scene.render.resolution_x, scene.render.resolution_y = 1920, 1080

    # Pre-configure the add-on if it is enabled
    s = getattr(scene, "scene_optimizer", None)
    if s is not None:
        s.exclude_collection = c_never
        s.frustum_enabled = True
        s.occlusion_enabled = True
        s.lod_enabled = True
        s.lod_distances[0], s.lod_distances[1] = 25.0, 60.0
        s.lod_groups.clear()
        s.lod_detect_collection = c_trees
        try:
            bpy.ops.scene_optimizer.lod_autodetect()
        except RuntimeError as exc:
            print("LOD auto-detect:", exc)
    print("Scene Optimizer test scene created: %d objects" % len(scene.objects))


build()
