"""Verifies live LOD switching inside an actual animation render (Cycles).
blender -b --factory-startup --python tests/render_test.py -- scene_optimizer.py"""
import sys, os, tempfile, importlib.util
import bpy
argv = sys.argv[sys.argv.index("--") + 1:]
spec = importlib.util.spec_from_file_location("scene_optimizer", os.path.abspath(argv[0]))
so = importlib.util.module_from_spec(spec); sys.modules["scene_optimizer"] = so
spec.loader.exec_module(so); so.register()

sc = bpy.context.scene
for o in list(bpy.data.objects): bpy.data.objects.remove(o)
def emissive(name, rgb):
    m = bpy.data.materials.new(name); m.use_nodes = True
    nt = m.node_tree; nt.nodes.clear()
    e = nt.nodes.new("ShaderNodeEmission"); e.inputs[0].default_value = (*rgb, 1); e.inputs[1].default_value = 1
    out = nt.nodes.new("ShaderNodeOutputMaterial"); nt.links.new(e.outputs[0], out.inputs[0])
    return m
s = sc.scene_optimizer
g = s.lod_groups.add(); g.name = "Sign"
for i, rgb in enumerate([(1, 0, 0), (0, 1, 0), (0, 0, 1)]):
    bpy.ops.mesh.primitive_plane_add(size=400, location=(0, 0, 0))
    p = bpy.context.object; p.name = "Sign_LOD%d" % i
    p.data.materials.append(emissive("M%d" % i, rgb))
    g.variants.add().obj = p
cd = bpy.data.cameras.new("C"); cam = bpy.data.objects.new("C", cd); sc.collection.objects.link(cam)
sc.camera = cam
cam.location = (0, 0, 5); cam.keyframe_insert("location", frame=1)
cam.location = (0, 0, 20); cam.keyframe_insert("location", frame=2)
cam.location = (0, 0, 60); cam.keyframe_insert("location", frame=3)
s.lod_enabled = True
s.lod_distances[0], s.lod_distances[1] = 10, 40
s.lod_live = True
sc.view_settings.view_transform = 'Standard'
sc.render.engine = 'CYCLES'; sc.cycles.samples = 1; sc.cycles.device = 'CPU'
sc.render.resolution_x = sc.render.resolution_y = 16
sc.frame_start, sc.frame_end = 1, 3
out = os.path.join(tempfile.gettempdir(), "so_render_")
sc.render.filepath = out
sc.render.image_settings.file_format = 'PNG'
sc.frame_set(3)   # leave the scene at the "wrong" LOD before rendering
bpy.ops.render.render(animation=True)
expect = {1: "red", 2: "green", 3: "blue"}
ok = True
for f in (1, 2, 3):
    img = bpy.data.images.load(out + "%04d.png" % f)
    px = list(img.pixels[:4 * 16 * 16])
    n = len(px) // 4
    r, gg, b = (sum(px[i::4]) / n for i in range(3))
    got = max((("red", r), ("green", gg), ("blue", b)), key=lambda t: t[1])[0]
    print("frame %d  rgb=(%.2f %.2f %.2f)  -> %s  expected %s" % (f, r, gg, b, got, expect[f]))
    ok &= got == expect[f]
print("RENDER LOD TEST", "PASSED" if ok else "FAILED")
sys.stdout.flush(); os._exit(0 if ok else 1)
