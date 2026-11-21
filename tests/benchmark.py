"""blender -b --factory-startup --python tests/benchmark.py -- scene_optimizer.py"""
import sys, os, time, importlib.util, bpy, bmesh
argv = sys.argv[sys.argv.index("--") + 1:]
spec = importlib.util.spec_from_file_location("scene_optimizer", os.path.abspath(argv[0]))
so = importlib.util.module_from_spec(spec); sys.modules["scene_optimizer"] = so
spec.loader.exec_module(so); so.register()
sc = bpy.context.scene
for o in list(bpy.data.objects): bpy.data.objects.remove(o)
def mesh(sub):
    me = bpy.data.meshes.new("m%d" % sub); bm = bmesh.new()
    bmesh.ops.create_icosphere(bm, subdivisions=sub, radius=0.5); bm.to_mesh(me); bm.free(); return me
m3 = mesh(3)  # 1280 tris
for x in range(100):
    for y in range(100):
        o = bpy.data.objects.new("P", m3); o.location = (x * 3 - 150, y * 3 - 150, 0); sc.collection.objects.link(o)
box = bpy.data.meshes.new("b"); bm = bmesh.new(); bmesh.ops.create_cube(bm, size=1); bm.to_mesh(box); bm.free()
for i in range(40):
    o = bpy.data.objects.new("Bld", box); o.location = ((i % 8) * 35 - 140, (i // 8) * 50 - 100, 10); o.scale = (20, 20, 20)
    sc.collection.objects.link(o)
cd = bpy.data.cameras.new("C"); cam = bpy.data.objects.new("C", cd); sc.collection.objects.link(cam); sc.camera = cam
cam.location = (0, -170, 3); cam.rotation_euler = (1.53, 0, 0)
s = sc.scene_optimizer
for occl, preset in ((False, None), (True, 'FAST'), (True, 'BALANCED'), (True, 'ACCURATE')):
    s.occlusion_enabled = occl
    if preset: s.occlusion_preset = preset
    t = time.perf_counter(); r = so.run_culling(bpy.context, sc, preview=False); dt = time.perf_counter() - t
    print("10040 objects  occlusion=%-8s  frustum %5d  occluded %5d  rays %7d  tris %8d  %.2fs" % (
        preset or "off", r["frustum"], r["occlusion"], r["rays"], r["tris"], dt))
so.Visibility.restore()
# LOD live update cost: 1000 groups x 3 variants
s.lod_enabled = True
objs = list(sc.objects)[:3000]
for i in range(1000):
    g = s.lod_groups.add(); g.name = "G%d" % i
    for o in objs[i * 3:i * 3 + 3]:
        g.variants.add().obj = o
so.update_lods(sc)
t = time.perf_counter(); so.update_lods(sc, live=True, clear_others=False); dt = time.perf_counter() - t
print("live LOD update, 1000 groups, no level change: %.1f ms" % (dt * 1000))
cam.location.y = 100; bpy.context.view_layer.update()
t = time.perf_counter(); so.update_lods(sc, live=True, clear_others=False); dt = time.perf_counter() - t
print("live LOD update, 1000 groups, levels changed:  %.1f ms" % (dt * 1000))
sys.stdout.flush(); os._exit(0)
