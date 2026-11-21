"""Interactive-mode smoke test: panels, overlay drawing, live LOD timer.
blender --factory-startup --python tests/gui_test.py -- scene_optimizer.py out_dir"""
import sys, os, importlib.util, traceback
import bpy
argv = sys.argv[sys.argv.index("--") + 1:]
ADDON, OUT = os.path.abspath(argv[0]), os.path.abspath(argv[1])
spec = importlib.util.spec_from_file_location("scene_optimizer", ADDON)
so = importlib.util.module_from_spec(spec); sys.modules["scene_optimizer"] = so
spec.loader.exec_module(so); so.register()
exec(open(os.path.join(os.path.dirname(ADDON), "create_test_scene.py")).read())
# Show our panels in the default "Tool" tab (active tab is read-only from Python)
for cls in so.classes:
    if issubclass(cls, bpy.types.Panel):
        bpy.utils.unregister_class(cls)
for cls in so.classes:
    if issubclass(cls, bpy.types.Panel):
        cls.bl_category = "Tool"
        if cls.__name__ != "SCENEOPT_PT_main":
            cls.bl_options = set()
        bpy.utils.register_class(cls)

def view3d():
    win = bpy.context.window_manager.windows[0]
    for area in win.screen.areas:
        if area.type == 'VIEW_3D':
            region = [r for r in area.regions if r.type == 'WINDOW'][0]
            return win, area, region
    raise RuntimeError("no 3D view")

def shot(name):
    win, area, region = view3d()
    with bpy.context.temp_override(window=win, area=area, region=region):
        bpy.ops.screen.screenshot_area(filepath=os.path.join(OUT, name))
    print("screenshot", name)

def op(fn, **kw):
    win, area, region = view3d()
    with bpy.context.temp_override(window=win, area=area, region=region):
        return fn(**kw)

steps = []
def step(f): steps.append(f); return f

@step
def setup():
    win, area, region = view3d()
    sp = area.spaces[0]
    sp.show_region_ui = True
    sp.region_3d.view_perspective = 'CAMERA'
    sp.shading.type = 'SOLID'
    sp.shading.color_type = 'MATERIAL'
    for r in area.regions:
        if r.type == 'UI':
            try:
                r.active_panel_category = "Scene Optimizer"
            except Exception as e:
                print("category:", e)
    s = bpy.context.scene.scene_optimizer
    print("groups:", len(s.lod_groups))

@step
def preview():
    print("preview:", op(bpy.ops.scene_optimizer.preview), bpy.context.scene.scene_optimizer.last_report)

@step
def shot_preview(): shot("1_preview.png")

@step
def apply():
    print("apply:", op(bpy.ops.scene_optimizer.apply), bpy.context.scene.scene_optimizer.last_report)

@step
def shot_apply(): shot("2_applied.png")

@step
def live():
    s = bpy.context.scene.scene_optimizer
    s.lod_live = True
    bpy.context.scene.frame_set(120)

@step
def wait(): pass

@step
def shot_live():
    lv = so._Runtime.lod_levels.get(bpy.context.scene.name_full, {})
    print("live levels at frame 120:", sorted(lv.items())[:6])
    shot("3_live_f120.png")

@step
def move_camera():
    # move the camera directly (no frame change) -> depsgraph handler + timer
    cam = bpy.context.scene.camera
    cam.animation_data_clear()
    cam.location.y = 125
    print("moved camera")

@step
def wait2(): pass

@step
def check_timer():
    lv = so._Runtime.lod_levels.get(bpy.context.scene.name_full, {})
    print("live levels after camera move:", sorted(lv.items())[:6])

@step
def restore():
    print("restore:", op(bpy.ops.scene_optimizer.restore, what='ALL'))
    print("managed after restore:", len(so.Visibility.managed_objects()))
    shot("4_restored.png")

def runner():
    if not steps:
        bpy.ops.wm.quit_blender()
        return None
    f = steps.pop(0)
    try:
        f()
    except Exception:
        traceback.print_exc()
    return 0.7

bpy.app.timers.register(runner, first_interval=2.0)
