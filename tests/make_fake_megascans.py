"""
Builds STAND-IN assets that mimic the folder layout and naming of Quixel
Megascans downloads, so examples/quixel_lod_example.py can be tested without
a Fab/Bridge account. This is NOT Quixel content - procedural meshes with
generated placeholder textures.

    blender -b --factory-startup --python tests/make_fake_megascans.py -- <out_dir>

Produces:
  <out>/3d/rock_sandstone_tkmfdcbfa/        one FBX per LOD (LOD0..LOD5),
        tkmfdcbfa_LOD0.fbx ...                object "Aset_rock_sandstone_M_tkmfdcbfa_LOD<n>"
        tkmfdcbfa_2K_Albedo.jpg, _Roughness.jpg, _Normal_LOD0..5.jpg, _AO.jpg
        tkmfdcbfa.json
  <out>/3dplant/plant_fern_ulruch3ia.zip    Fab-style zip, ONE fbx containing
        Var1_LOD0..LOD3 and Var2_LOD0..LOD3, plus _Opacity.jpg
"""
import json
import math
import os
import random
import shutil
import sys
import zipfile

import bmesh
import bpy
from mathutils import Vector, noise

OUT = os.path.abspath(sys.argv[sys.argv.index("--") + 1]) if "--" in sys.argv else os.path.abspath("fake_megascans")


def clear():
    for o in list(bpy.data.objects):
        bpy.data.objects.remove(o)
    for m in list(bpy.data.meshes):
        bpy.data.meshes.remove(m)


def rock_mesh(name, subdiv):
    me = bpy.data.meshes.new(name)
    bm = bmesh.new()
    bmesh.ops.create_icosphere(bm, subdivisions=subdiv, radius=0.6)
    for v in bm.verts:
        n = noise.noise(v.co * 2.5) * 0.18 + noise.noise(v.co * 7.0) * 0.05
        v.co = v.co * (1.0 + n)
        v.co.z *= 0.65
        v.co.z += 0.3
    bm.to_mesh(me)
    bm.free()
    me.uv_layers.new(name="UVMap")
    return me


def fern_mesh(name, blades, segs, seed):
    rnd = random.Random(seed)
    me = bpy.data.meshes.new(name)
    bm = bmesh.new()
    for b in range(blades):
        ang = b / blades * math.tau + rnd.uniform(-0.2, 0.2)
        d = Vector((math.cos(ang), math.sin(ang), 0))
        side = Vector((-d.y, d.x, 0))
        prev = None
        for s in range(segs + 1):
            t = s / segs
            c = d * (0.7 * t) + Vector((0, 0, 0.6 * math.sin(t * 2.2)))
            w = 0.12 * math.sin(math.pi * min(1.0, t + 0.05))
            pair = (bm.verts.new(c - side * w), bm.verts.new(c + side * w))
            if prev:
                bm.faces.new((prev[0], prev[1], pair[1], pair[0]))
            prev = pair
    bm.to_mesh(me)
    bm.free()
    me.uv_layers.new(name="UVMap")
    return me


def export_fbx(objs, path):
    bpy.ops.object.select_all(action='DESELECT')
    for o in objs:
        o.select_set(True)
    bpy.context.view_layer.objects.active = objs[0]
    bpy.ops.export_scene.fbx(filepath=path, use_selection=True, object_types={'MESH'},
                             apply_scale_options='FBX_SCALE_UNITS')


def texture(path, rgb, size=64):
    img = bpy.data.images.new(os.path.basename(path), size, size)
    px = []
    for y in range(size):
        for x in range(size):
            k = 0.85 + 0.15 * noise.noise(Vector((x * 0.2, y * 0.2, 0.0)))
            px += [rgb[0] * k, rgb[1] * k, rgb[2] * k, 1.0]
    img.pixels = px
    img.filepath_raw = path
    img.file_format = 'JPEG'
    img.save()
    bpy.data.images.remove(img)


def make_rock(root):
    aid = "tkmfdcbfa"
    d = os.path.join(root, "3d", "rock_sandstone_%s" % aid)
    os.makedirs(d, exist_ok=True)
    meshes = []
    for lod, sub in enumerate((5, 4, 3, 2, 2, 1)):
        clear()
        o = bpy.data.objects.new("Aset_rock_sandstone_M_%s_LOD%d" % (aid, lod), rock_mesh("rock%d" % lod, sub))
        bpy.context.scene.collection.objects.link(o)
        export_fbx([o], os.path.join(d, "%s_LOD%d.fbx" % (aid, lod)))
        meshes.append({"type": "lod", "uris": [{"uri": "%s_LOD%d.fbx" % (aid, lod)}]})
        texture(os.path.join(d, "%s_2K_Normal_LOD%d.jpg" % (aid, lod)), (0.5, 0.5, 1.0), 32)
    texture(os.path.join(d, "%s_2K_Albedo.jpg" % aid), (0.62, 0.45, 0.3))
    texture(os.path.join(d, "%s_2K_Roughness.jpg" % aid), (0.8, 0.8, 0.8), 32)
    texture(os.path.join(d, "%s_2K_AO.jpg" % aid), (0.9, 0.9, 0.9), 32)
    with open(os.path.join(d, "%s.json" % aid), "w") as f:
        json.dump({"id": aid, "name": "Sandstone Rock (stand-in)", "meshes": meshes,
                   "categories": ["3d", "rock"]}, f, indent=1)
    return d


def make_fern(root):
    aid = "ulruch3ia"
    stage = os.path.join(root, "_stage_fern")
    os.makedirs(stage, exist_ok=True)
    clear()
    objs = []
    for var in (1, 2):
        for lod, (blades, segs) in enumerate(((14, 10), (10, 6), (7, 3), (5, 2))):
            o = bpy.data.objects.new("Var%d_LOD%d" % (var, lod), fern_mesh("f", blades, segs, var))
            bpy.context.scene.collection.objects.link(o)
            objs.append(o)
    export_fbx(objs, os.path.join(stage, "%s.fbx" % aid))
    texture(os.path.join(stage, "%s_2K_Albedo.jpg" % aid), (0.15, 0.45, 0.12))
    texture(os.path.join(stage, "%s_2K_Opacity.jpg" % aid), (1, 1, 1), 32)
    texture(os.path.join(stage, "%s_2K_Normal.jpg" % aid), (0.5, 0.5, 1.0), 32)
    texture(os.path.join(stage, "%s_2K_Roughness.jpg" % aid), (0.7, 0.7, 0.7), 32)
    os.makedirs(os.path.join(root, "3dplant"), exist_ok=True)
    zpath = os.path.join(root, "3dplant", "plant_fern_%s.zip" % aid)
    with zipfile.ZipFile(zpath, "w") as z:
        for fn in os.listdir(stage):
            z.write(os.path.join(stage, fn), arcname=fn)
    shutil.rmtree(stage)
    return zpath


if os.path.isdir(OUT):
    shutil.rmtree(OUT)
os.makedirs(OUT)
print("rock:", make_rock(OUT))
print("fern:", make_fern(OUT))
sys.stdout.flush()
os._exit(0)
