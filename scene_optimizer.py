# SPDX-License-Identifier: GPL-3.0-or-later
"""
Scene Optimizer - frustum culling, occlusion culling and distance-based LOD
switching for large Blender environments.

Single-file add-on. Sections:
    1. Constants / small utilities
    2. Visibility manager   (the ONLY code that writes hide_viewport/hide_render)
    3. Camera model         (frustum planes, projection, ray generation)
    4. Scene gathering      (depsgraph instances -> culling units, bounds, geometry)
    5. Frustum culling
    6. Occlusion culling    (BVH ray casting)
    7. LOD system
    8. Culling pipeline     (multi-frame evaluation, preview/apply)
    9. Viewport overlay
   10. Handlers             (live LOD switching)
   11. Properties
   12. Operators
   13. UI
   14. Scatter systems      (Geometry Nodes scatter tool: distribution, per-instance
                            LOD/thinning/culling, color variation, preset library)
   15. Registration
"""

bl_info = {
    "name": "Scene Optimizer",
    "author": "Scene Optimizer contributors",
    "version": (1, 1, 0),
    "blender": (4, 2, 0),
    "location": "View3D > Sidebar (N) > Scene Optimizer",
    "description": "Frustum/occlusion culling and distance LOD for large environments, "
                   "plus a grass and foliage scatter tool with per-instance LOD and culling",
    "doc_url": "",
    "category": "3D View",
}

import json
import math
import os
import re
import time

import bpy
import bpy.utils.previews
import numpy as np
from bpy.app.handlers import persistent
from bpy.props import (BoolProperty, BoolVectorProperty, CollectionProperty, EnumProperty,
                       FloatProperty, FloatVectorProperty, IntProperty,
                       PointerProperty, StringProperty)
from bpy.types import Operator, Panel, PropertyGroup, UIList
from mathutils import Matrix, Vector
from mathutils.bvhtree import BVHTree


# =============================================================================
# 1. Constants / small utilities
# =============================================================================

STATE_KEY = "_scene_optimizer_state"

R_FRUSTUM = "FRUSTUM"
R_OCCLUSION = "OCCLUSION"
R_LOD = "LOD"            # inactive LOD variant
R_DISTANCE = "DISTANCE"  # whole LOD group beyond max draw distance
CULL_REASONS = frozenset({R_FRUSTUM, R_OCCLUSION})
LOD_REASONS = frozenset({R_LOD, R_DISTANCE})
ALL_REASONS = CULL_REASONS | LOD_REASONS

# Object types that produce renderable geometry with meaningful bounds.
GEOM_TYPES = {'MESH', 'CURVE', 'SURFACE', 'META', 'FONT', 'CURVES',
              'POINTCLOUD', 'VOLUME', 'GREASEPENCIL', 'GPENCIL'}
# Types that can be converted to triangles for the occlusion BVH.
TRI_TYPES = {'MESH', 'CURVE', 'SURFACE', 'META', 'FONT'}
# Types that may be hidden by culling. META is excluded on purpose: hiding one
# metaball element changes the shape of the whole metaball family.
CULLABLE_TYPES = GEOM_TYPES - {'META'}

MAX_LOD_LEVELS = 8
DEFAULT_LOD_DISTANCES = (15.0, 35.0, 70.0, 140.0, 280.0, 560.0, 1120.0)

LOD_RE = re.compile(
    r"^(?P<base>.*?)[\s_.\-]*lod[\s_.\-]*(?P<lvl>\d+)(?P<dup>\.\d{3,})?$",
    re.IGNORECASE)

VIS_DATA_PATHS = {"hide_viewport", "hide_render"}


def _log(msg):
    print("[Scene Optimizer] " + msg)


def settings_of(scene):
    return scene.scene_optimizer


def _obj_alive(obj):
    """True if `obj` is a usable bpy Object reference."""
    if obj is None:
        return False
    try:
        obj.name
    except ReferenceError:
        return False
    return True


def object_editable(obj):
    """Return (ok, reason). Linked (library) data cannot be modified."""
    if not _obj_alive(obj):
        return False, "missing"
    if obj.library is not None:
        return False, "linked from library"
    ovr = getattr(obj, "override_library", None)
    if ovr is not None and getattr(ovr, "is_system_override", False):
        return False, "system library override"
    return True, ""


def _iter_action_fcurves(action):
    """Yield every F-Curve in an action (layered actions in 4.4+/5.x,
    legacy flat actions before)."""
    found_layered = False
    for layer in getattr(action, "layers", ()):
        for strip in layer.strips:
            for bag in getattr(strip, "channelbags", ()):
                found_layered = True
                yield from bag.fcurves
    if not found_layered:
        yield from getattr(action, "fcurves", ())


def has_animated_visibility(obj):
    """True if hide_viewport/hide_render is driven or keyed. Writing to such
    properties is pointless: the animation system overwrites them on the next
    frame change, so these objects are left alone."""
    ad = obj.animation_data
    if ad is None:
        return False
    for drv in ad.drivers:
        if drv.data_path in VIS_DATA_PATHS:
            return True
    actions = []
    if ad.action is not None:
        actions.append(ad.action)
    for track in ad.nla_tracks:
        for strip in track.strips:
            if strip.action is not None:
                actions.append(strip.action)
    for act in actions:
        for fc in _iter_action_fcurves(act):
            if fc.data_path in VIS_DATA_PATHS:
                return True
    return False


def _object_is_animated(obj):
    ad = obj.animation_data
    if ad is not None and (ad.action is not None or len(ad.drivers) or len(ad.nla_tracks)):
        return True
    return len(obj.constraints) > 0


def tag_redraw_all():
    wm = bpy.context.window_manager
    if wm is None:
        return
    for win in wm.windows:
        for area in win.screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()


def get_active_camera(scene):
    """Return (camera_object, error_message)."""
    cam = scene.camera
    if cam is None:
        return None, "Scene has no active camera"
    if not _obj_alive(cam):
        return None, "Active camera reference is invalid"
    if cam.type != 'CAMERA':
        return None, "Active camera '%s' is not a Camera object" % cam.name
    return cam, ""


class _Runtime:
    """Non-persistent session state (never saved in the .blend)."""
    busy = False                 # suppresses handlers while we work
    scatter_cam_sig = {}         # scene -> camera signature written to scatter systems
    timer_scene = None           # scene name with a pending live-LOD update
    cam_sig = {}                 # scene name -> camera signature tuple
    lod_levels = {}              # scene name -> {group name: active level}
    lod_obj_names = {}           # scene name -> set of LOD object names
    overlay = {}                 # scene name -> overlay data dict
    draw_handles = []
    counts = None                # cached (n_cull, n_lod) for the panel
    cull_cam_sig = {}            # scene name -> camera signature at last Apply


def hidden_counts():
    """(objects hidden by culling, objects hidden by LOD). Cached so the
    sidebar does not scan bpy.data.objects on every redraw."""
    if _Runtime.counts is None:
        n_cull = n_lod = 0
        for o in bpy.data.objects:
            st = o.get(STATE_KEY)
            if st is None:
                continue
            rs = set(str(st.get("r", "")).split(","))
            n_cull += bool(rs & CULL_REASONS)
            n_lod += bool(rs & LOD_REASONS)
        _Runtime.counts = (n_cull, n_lod)
    return _Runtime.counts


# =============================================================================
# 2. Visibility manager
# =============================================================================
#
# Every object the add-on hides carries an ID property dict STATE_KEY:
#   hv0/hr0 : original hide_viewport / hide_render (the user's state)
#   hv1/hr1 : the values the add-on last wrote
#   r       : comma separated set of active reasons
# Storing this on the object (not in Python memory) means restore works after
# save/reload, renames and undo. If the current value differs from hv1/hr1,
# the user changed it manually; that value is adopted as the new original so
# restore never fights a deliberate user edit.

class Visibility:

    @staticmethod
    def managed_objects():
        return [o for o in bpy.data.objects if STATE_KEY in o]

    @staticmethod
    def reasons(obj):
        st = obj.get(STATE_KEY)
        if st is None:
            return set()
        return {r for r in str(st.get("r", "")).split(",") if r}

    @staticmethod
    def _desired(hv0, hr0, reasons, cull_target):
        hv, hr = bool(hv0), bool(hr0)
        if reasons & LOD_REASONS:
            # LOD always applies to both, otherwise several variants would
            # render together in whichever mode was not targeted.
            hv = hr = True
        if reasons & CULL_REASONS:
            if cull_target in {'VIEWPORT', 'BOTH'}:
                hv = True
            if cull_target in {'RENDER', 'BOTH'}:
                hr = True
        return hv, hr

    @staticmethod
    def update(obj, add=(), remove=(), cull_target='BOTH'):
        """Add/remove reasons on one object and write the resulting
        visibility. Returns True if the object's visibility changed."""
        ok, _why = object_editable(obj)
        if not ok:
            return False
        add = set(add)
        remove = set(remove)
        st = obj.get(STATE_KEY)
        if st is None:
            if not add:
                return False
            cv, cr = Visibility._current(obj)
            hv, hr = int(cv), int(cr)
            obj[STATE_KEY] = {"hv0": hv, "hr0": hr, "hv1": hv, "hr1": hr, "r": ""}
            st = obj[STATE_KEY]
        else:
            # Adopt manual user edits made since our last write.
            cv, cr = Visibility._current(obj)
            if int(cv) != int(st.get("hv1", 0)):
                st["hv0"] = int(cv)
            if int(cr) != int(st.get("hr1", 0)):
                st["hr0"] = int(cr)
        old = str(st.get("r", ""))
        reasons = {r for r in old.split(",") if r}
        reasons = (reasons | add) - remove
        hv0, hr0 = bool(st.get("hv0", 0)), bool(st.get("hr0", 0))
        if reasons:
            hv, hr = Visibility._desired(hv0, hr0, reasons, cull_target)
        else:
            hv, hr = hv0, hr0
        changed = Visibility._write(obj, hv, hr)
        _Runtime.counts = None
        if reasons:
            new = ",".join(sorted(reasons))
            # Only touch ID properties when something differs (live LOD
            # calls this for every variant on every camera move).
            if new != old:
                st["r"] = new
            if int(st.get("hv1", -1)) != int(hv):
                st["hv1"] = int(hv)
            if int(st.get("hr1", -1)) != int(hr):
                st["hr1"] = int(hr)
        else:
            del obj[STATE_KEY]
        return changed

    # --- batched writing -------------------------------------------------
    # Setting Object.hide_viewport through RNA makes Blender re-sync every
    # collection and tag a depsgraph relations rebuild - O(scene) per write,
    # i.e. O(n^2) when hiding thousands of objects (measured: 1.4 s for 5k
    # objects in a 10k scene). Inside a batch, writes are queued and flushed
    # with one foreach_set over bpy.data.objects plus ONE regular RNA write,
    # which triggers the collection sync / relations update once.
    _batch_depth = 0
    _pending = {}   # as_pointer() -> [obj, hv, hr]
    BATCH_MIN = 32

    @staticmethod
    def _current(obj):
        p = Visibility._pending.get(obj.as_pointer())
        if p is not None:
            return p[1], p[2]
        return obj.hide_viewport, obj.hide_render

    class _BatchCtx:
        def __enter__(self):
            Visibility._batch_depth += 1

        def __exit__(self, *exc):
            Visibility._batch_depth -= 1
            if Visibility._batch_depth == 0:
                Visibility.flush()
            return False

    @staticmethod
    def batch():
        return Visibility._BatchCtx()

    @staticmethod
    def flush():
        pending = Visibility._pending
        Visibility._pending = {}
        items = [p for p in pending.values() if _obj_alive(p[0])]
        if not items:
            return
        if len(items) < Visibility.BATCH_MIN:
            for obj, hv, hr in items:
                Visibility._write_rna(obj, hv, hr)
            return
        objects = bpy.data.objects
        n = len(objects)
        index = {o.as_pointer(): i for i, o in enumerate(objects)}
        hv_arr = np.zeros(n, dtype=bool)
        hr_arr = np.zeros(n, dtype=bool)
        objects.foreach_get("hide_viewport", hv_arr)
        objects.foreach_get("hide_render", hr_arr)
        hv_changed = hr_changed = None
        for obj, hv, hr in items:
            i = index.get(obj.as_pointer())
            if i is None:
                continue
            if hv_arr[i] != hv:
                hv_arr[i] = hv
                hv_changed = obj
            if hr_arr[i] != hr:
                hr_arr[i] = hr
                hr_changed = obj
        if hv_changed is not None:
            objects.foreach_set("hide_viewport", hv_arr)
            # one RNA write = one collection sync + relations update
            hv_changed.hide_viewport = bool(hv_arr[index[hv_changed.as_pointer()]])
        if hr_changed is not None:
            objects.foreach_set("hide_render", hr_arr)
            hr_changed.hide_render = bool(hr_arr[index[hr_changed.as_pointer()]])

    @staticmethod
    def _write(obj, hv, hr):
        cv, cr = Visibility._current(obj)
        if cv == hv and cr == hr:
            return False
        if Visibility._batch_depth > 0:
            Visibility._pending[obj.as_pointer()] = [obj, hv, hr]
            return True
        return Visibility._write_rna(obj, hv, hr)

    @staticmethod
    def _write_rna(obj, hv, hr):
        changed = False
        try:
            if obj.hide_viewport != hv:
                obj.hide_viewport = hv
                changed = True
            if obj.hide_render != hr:
                obj.hide_render = hr
                changed = True
        except (AttributeError, RuntimeError, TypeError) as exc:
            _log("Could not change visibility of '%s': %s" % (obj.name, exc))
        return changed

    @staticmethod
    def apply_reasons(mapping, owned, cull_target, clear_others=True):
        """mapping: {obj: set(reasons)}. For every object in `mapping`, the
        reasons in `owned` are replaced by mapping[obj] & owned. If
        clear_others, managed objects missing from `mapping` lose all `owned`
        reasons (that is how stale results are cleared)."""
        owned = set(owned)
        with Visibility.batch():
            return Visibility._apply_reasons(mapping, owned, cull_target, clear_others)

    @staticmethod
    def _apply_reasons(mapping, owned, cull_target, clear_others):
        changed = 0
        if clear_others:
            mapped = {o.as_pointer() for o in mapping}
            for obj in Visibility.managed_objects():
                if obj.as_pointer() not in mapped and Visibility.reasons(obj) & owned:
                    changed += Visibility.update(obj, remove=owned, cull_target=cull_target)
        for obj, rs in mapping.items():
            rs = set(rs) & owned
            changed += Visibility.update(obj, add=rs, remove=owned - rs,
                                         cull_target=cull_target)
        return changed

    @staticmethod
    def restore(owned=ALL_REASONS, cull_target='BOTH'):
        n = 0
        with Visibility.batch():
            for obj in Visibility.managed_objects():
                if Visibility.reasons(obj) & set(owned):
                    Visibility.update(obj, remove=owned, cull_target=cull_target)
                    n += 1
        return n

    @staticmethod
    def resync(cull_target):
        """Re-write managed objects (e.g. after the cull target changed)."""
        with Visibility.batch():
            for obj in Visibility.managed_objects():
                Visibility.update(obj, cull_target=cull_target)

    @staticmethod
    def snapshot():
        return [(o, Visibility.reasons(o)) for o in Visibility.managed_objects()]

    @staticmethod
    def apply_snapshot(snap, cull_target):
        with Visibility.batch():
            for obj, rs in snap:
                if _obj_alive(obj):
                    Visibility.update(obj, add=rs, cull_target=cull_target)


# =============================================================================
# 3. Camera model
# =============================================================================

class CameraModel:
    """Camera frustum in camera-local space (camera looks down -Z).

    Frame extents come from Camera.view_frame(scene) so sensor fit, aspect
    ratio and lens shift are handled by Blender itself. Camera object scale is
    ignored (it does not affect projection)."""

    def __init__(self, scene, cam_obj, cam_matrix, margin_pct=0.0, use_clip=True):
        cam = cam_obj.data
        self.ortho = cam.type == 'ORTHO'
        self.panoramic = cam.type == 'PANO'
        frame = cam.view_frame(scene=scene)
        xs = [v.x for v in frame]
        ys = [v.y for v in frame]
        self.zf = frame[0].z  # frame plane depth (negative); unused for ortho
        self.x0, self.x1 = min(xs), max(xs)
        self.y0, self.y1 = min(ys), max(ys)
        self.w0 = self.x1 - self.x0
        self.h0 = self.y1 - self.y0
        self.margin = max(0.0, margin_pct) / 100.0
        m = self.margin
        self.ex0, self.ex1 = self.x0 - m * self.w0, self.x1 + m * self.w0
        self.ey0, self.ey1 = self.y0 - m * self.h0, self.y1 + m * self.h0

        loc, rot, _scale = cam_matrix.decompose()
        self.rot = rot.to_matrix()
        self.mat = Matrix.Translation(loc) @ self.rot.to_4x4()
        self.inv = self.mat.inverted()
        self.origin = loc.copy()
        self.inv_np = np.array(self.inv, dtype=np.float64)
        self.rot_np = np.array(self.rot, dtype=np.float64)
        self.loc_np = np.array(loc, dtype=np.float64)

        self.clip_start = max(cam.clip_start, 1e-6)
        self.clip_end = cam.clip_end
        self.use_clip = use_clip

        r = scene.render
        pct = r.resolution_percentage / 100.0
        self.res_x = max(1.0, r.resolution_x * pct)
        self.res_y = max(1.0, r.resolution_y * pct)

        self.planes = self._build_planes()

    def _build_planes(self):
        """Inside half-spaces n.p + d >= 0, normalised, camera space."""
        planes = []
        if self.ortho:
            planes += [((1, 0, 0), -self.ex0), ((-1, 0, 0), self.ex1),
                       ((0, 1, 0), -self.ey0), ((0, -1, 0), self.ey1)]
        else:
            zf = self.zf
            planes += [((1, 0, -self.ex0 / zf), 0.0), ((-1, 0, self.ex1 / zf), 0.0),
                       ((0, 1, -self.ey0 / zf), 0.0), ((0, -1, self.ey1 / zf), 0.0)]
        if self.use_clip:
            planes.append(((0, 0, -1), -self.clip_start))
            planes.append(((0, 0, 1), self.clip_end))
        else:
            planes.append(((0, 0, -1), 0.0))  # still reject things behind camera
        out = np.zeros((len(planes), 4))
        for i, (n, d) in enumerate(planes):
            n = np.array(n, dtype=np.float64)
            ln = np.linalg.norm(n)
            out[i, :3] = n / ln
            out[i, 3] = d / ln
        return out

    def to_camera(self, pts):
        """pts (...,3) world -> camera space."""
        return pts @ self.inv_np[:3, :3].T + self.inv_np[:3, 3]

    def obbs_inside(self, corners_world, padding=0.0):
        """corners_world (K,8,3). Returns bool (K,): False only if the box is
        provably outside (all 8 corners outside one plane by > padding).
        Conservative: a box near a frustum edge may be reported inside."""
        if len(corners_world) == 0:
            return np.zeros(0, dtype=bool)
        c = self.to_camera(corners_world)                       # (K,8,3)
        d = c @ self.planes[:, :3].T + self.planes[:, 3]         # (K,8,P)
        outside = np.all(d < -padding, axis=1)                   # (K,P)
        return ~np.any(outside, axis=1)

    def project_uv(self, pts_cam):
        """Camera-space points (N,3) with z<0 -> frame uv (N,2); frame = [0,1]."""
        if self.ortho:
            x, y = pts_cam[:, 0], pts_cam[:, 1]
        else:
            s = self.zf / pts_cam[:, 2]
            x, y = pts_cam[:, 0] * s, pts_cam[:, 1] * s
        return np.stack(((x - self.x0) / self.w0, (y - self.y0) / self.h0), axis=1)

    def rays(self, uv):
        """uv (N,2) -> world origins (N,3), unit dirs (N,3), max distance (N,).
        Rays start on the near clip plane and end on the far clip plane, so
        geometry clipped away by the camera never acts as an occluder."""
        x = self.x0 + uv[:, 0] * self.w0
        y = self.y0 + uv[:, 1] * self.h0
        n = len(uv)
        cs, ce = self.clip_start, self.clip_end
        if self.ortho:
            o_loc = np.stack((x, y, np.full(n, -cs)), axis=1)
            d_loc = np.tile(np.array([0.0, 0.0, -1.0]), (n, 1))
            maxd = np.full(n, ce - cs)
        else:
            p = np.stack((x, y, np.full(n, self.zf)), axis=1)
            d_loc = p / np.linalg.norm(p, axis=1)[:, None]
            dz = -d_loc[:, 2]
            o_loc = d_loc * (cs / dz)[:, None]
            maxd = (ce - cs) / dz
        o_w = o_loc @ self.rot_np.T + self.loc_np
        d_w = d_loc @ self.rot_np.T
        return o_w, d_w, maxd

    def frustum_lines(self, length):
        """World-space line list of the (margin-expanded) frustum for drawing."""
        if self.ortho:
            z0, z1 = -self.clip_start, -min(self.clip_end, length)
            pts = []
            for z in (z0, z1):
                pts.append([(self.ex0, self.ey0, z), (self.ex1, self.ey0, z),
                            (self.ex1, self.ey1, z), (self.ex0, self.ey1, z)])
        else:
            dist = min(self.clip_end, length)
            pts = []
            for depth in (self.clip_start, dist):
                s = depth / -self.zf
                pts.append([(self.ex0 * s, self.ey0 * s, -depth), (self.ex1 * s, self.ey0 * s, -depth),
                            (self.ex1 * s, self.ey1 * s, -depth), (self.ex0 * s, self.ey1 * s, -depth)])
        near, far = pts
        lines = []
        for i in range(4):
            j = (i + 1) % 4
            lines += [near[i], near[j], far[i], far[j], near[i], far[i]]
        return [tuple(self.mat @ Vector(p)) for p in lines]


# =============================================================================
# 4. Scene gathering
# =============================================================================
#
# A "unit" is the thing that gets culled as a whole:
#   ("O", name)  an ordinary object. All instances it generates (collection
#                instance, particles, geometry-nodes instances) belong to it,
#                so hiding the instancer hides every instance.
#   ("G", name)  an LOD group. All variants map to one unit so co-located
#                variants never occlude each other and are culled together.

class _GeomCache:
    def __init__(self):
        self.local = {}  # key -> (verts (n,3) float64, tris (m,3) int32) or None

    def get(self, key, ob_eval):
        if key in self.local:
            return self.local[key]
        res = None
        try:
            res = _extract_triangles(ob_eval)
        except Exception as exc:  # never let one odd object abort the run
            _log("Could not extract geometry from '%s': %s" % (ob_eval.name, exc))
        self.local[key] = res
        return res


def _extract_triangles(ob_eval):
    me = None
    cleared = False
    if ob_eval.type == 'MESH':
        me = ob_eval.data
    else:
        me = ob_eval.to_mesh()
        cleared = True
    try:
        if me is None or len(me.vertices) == 0:
            return None
        me.calc_loop_triangles()
        nv = len(me.vertices)
        nt = len(me.loop_triangles)
        if nt == 0:
            return None
        co = np.empty(nv * 3, dtype=np.float32)
        me.vertices.foreach_get("co", co)
        tri = np.empty(nt * 3, dtype=np.int32)
        me.loop_triangles.foreach_get("vertices", tri)
        return co.reshape(nv, 3).astype(np.float64), tri.reshape(nt, 3)
    finally:
        if cleared:
            ob_eval.to_mesh_clear()


def _local_bbox(ob_eval):
    """Local bounds, or None if the evaluated object has no geometry. An
    object whose geometry-nodes modifier outputs only instances reports an
    empty mesh with a zero-size box at its origin; using that phantom point
    would keep the owner "visible" even when every instance is off-screen."""
    if ob_eval.type == 'MESH':
        me = ob_eval.data
        if me is None or len(me.vertices) == 0:
            return None
    bb = np.array([tuple(v) for v in ob_eval.bound_box], dtype=np.float64)
    if not np.all(np.isfinite(bb)):
        return None
    if ob_eval.type in {'CURVE', 'SURFACE', 'FONT'} and not np.any(bb):
        return None  # curve/text without splines/characters
    return bb


def _transform(pts, m4):
    return pts @ m4[:3, :3].T + m4[:3, 3]


_mat_transparent_cache = {}


def _node_tree_transparent(tree, depth=0):
    if tree is None or depth > 8:
        return False
    for n in tree.nodes:
        t = n.type
        if t in {'BSDF_TRANSPARENT', 'BSDF_GLASS', 'BSDF_REFRACTION'}:
            return True
        if t == 'BSDF_PRINCIPLED':
            a = n.inputs.get("Alpha")
            if a is not None and (a.is_linked or a.default_value < 0.999):
                return True
            tw = n.inputs.get("Transmission Weight") or n.inputs.get("Transmission")
            if tw is not None and (tw.is_linked or tw.default_value > 0.001):
                return True
        if t == 'GROUP' and _node_tree_transparent(getattr(n, "node_tree", None), depth + 1):
            return True
    return False


def material_is_transparent(mat):
    """Heuristic: True if the material may let the camera see through it.
    False positives only reduce culling (safe); see README limitations."""
    if mat is None:
        return False
    key = mat.name_full
    if key in _mat_transparent_cache:
        return _mat_transparent_cache[key]
    res = False
    if getattr(mat, "surface_render_method", None) == 'BLENDED':
        res = True
    elif len(mat.diffuse_color) == 4 and mat.diffuse_color[3] < 0.999:
        res = True
    elif (bpy.app.version >= (5, 0, 0) or mat.use_nodes) and mat.node_tree is not None:
        res = _node_tree_transparent(mat.node_tree)
    _mat_transparent_cache[key] = res
    return res


def object_is_transparent(ob_eval):
    for slot in ob_eval.material_slots:
        if material_is_transparent(slot.material):
            return True
    return False


class GatherResult:
    def __init__(self):
        self.keys = []            # unit idx -> key
        self.index = {}           # key -> unit idx
        self.owner = {}           # unit idx -> owner object (original)
        self.obb = []             # list of (8,3) world corner arrays
        self.obb_unit = []        # list of unit idx
        self.chunks = []          # geometry chunks (see _Chunk)
        self.geom = _GeomCache()
        self.instances = 0

    def unit(self, key, owner):
        idx = self.index.get(key)
        if idx is None:
            idx = len(self.keys)
            self.keys.append(key)
            self.index[key] = idx
            self.owner[idx] = owner
        return idx

    def finalize(self):
        self.obb = np.array(self.obb, dtype=np.float64).reshape(-1, 8, 3)
        self.obb_unit = np.array(self.obb_unit, dtype=np.int64)


class _Chunk:
    __slots__ = ("unit", "m4", "gkey", "occluder", "variant", "obb_i")

    def __init__(self, unit, m4, gkey, occluder, variant, obb_i):
        self.unit = unit          # unit index
        self.m4 = m4              # world matrix (np 4x4)
        self.gkey = gkey          # key into GatherResult.geom
        self.occluder = occluder  # eligible to block rays (before LOD check)
        self.variant = variant    # LOD variant object name, or None
        self.obb_i = obb_i        # index into obb arrays


class RunContext:
    """Static (per-run) lookups shared by all frames."""

    def __init__(self, scene, view_layer, cam_obj):
        s = settings_of(scene)
        self.scene = scene
        self.view_layer = view_layer
        self.s = s
        self.cam_obj = cam_obj
        self.warnings = []
        self.exclude = set()
        if s.exclude_collection is not None:
            self.exclude = {o.name_full for o in s.exclude_collection.all_objects}
        self.occluder_names = None
        if s.occluder_collection is not None:
            self.occluder_names = {o.name_full for o in s.occluder_collection.all_objects}
        # LOD membership
        self.lod_enabled = s.lod_enabled
        self.obj_group = {}       # object name_full -> group name
        self.groups = {}          # group name -> [objects]
        if self.lod_enabled:
            ptrs = scene_object_pointers(scene)
            for g in s.lod_groups:
                objs = lod_group_objects(scene, g, ptrs)
                if len(objs) < 1:
                    continue
                self.groups[g.name] = objs
                for o in objs:
                    self.obj_group.setdefault(o.name_full, g.name)
        self._cand_cache = {}
        self.skipped = {"linked": 0, "animated": 0}

    def unit_key(self, owner):
        g = self.obj_group.get(owner.name_full)
        if g is not None:
            return ("G", g)
        return ("O", owner.name_full)

    def unit_objects(self, key, owner):
        if key[0] == "G":
            return self.groups.get(key[1], [])
        return [owner]

    def _obj_cullable(self, obj):
        if obj == self.cam_obj:
            return False
        if obj.type not in CULLABLE_TYPES and not (
                obj.type == 'EMPTY' and obj.instance_type == 'COLLECTION'):
            return False
        ok, why = object_editable(obj)
        if not ok:
            self.skipped["linked"] += 1
            return False
        if obj.so_never_cull or obj.name_full in self.exclude:
            return False
        if has_animated_visibility(obj):
            self.skipped["animated"] += 1
            return False
        return True

    def is_candidate(self, key, owner):
        res = self._cand_cache.get(key)
        if res is None:
            res = all(self._obj_cullable(o) for o in self.unit_objects(key, owner))
            self._cand_cache[key] = res
        return res

    def occluder_eligible(self, owner, ob_eval, other_instance):
        s = self.s
        if owner.so_not_occluder or owner.hide_render:
            return False
        src = ob_eval.original if ob_eval.original is not None else ob_eval
        if src != owner and getattr(src, "so_not_occluder", False):
            return False
        if self.occluder_names is not None and owner.name_full not in self.occluder_names:
            return False
        if other_instance and not s.occlusion_instance_occluders:
            return False
        if not getattr(ob_eval, "visible_camera", True):
            return False
        if s.occlusion_ignore_transparent and object_is_transparent(ob_eval):
            return False
        return True


def gather(depsgraph, rc, cam=None, want_geometry=False, prune=True):
    """Walk every evaluated instance once. Collects world OBBs per unit and,
    if want_geometry, references to local triangle data per instance.
    If prune, geometry outside the (margin-expanded) frustum is skipped: it
    cannot occlude anything the camera sees."""
    gr = GatherResult()
    for inst in depsgraph.object_instances:
        ob = inst.object
        if ob.type not in GEOM_TYPES:
            continue
        if inst.is_instance:
            owner = inst.parent.original if inst.parent is not None else None
            if owner is None:
                continue
            other_instance = not (owner.type == 'EMPTY' and owner.instance_type == 'COLLECTION')
        else:
            owner = ob.original
            other_instance = False
        bb = _local_bbox(ob)
        if bb is None:
            continue
        # Create the unit only once it has valid bounds: a unit without
        # bounds would look "outside the frustum" and be culled wrongly.
        key = rc.unit_key(owner)
        ui = gr.unit(key, owner)
        gr.instances += 1
        data = getattr(ob, "data", None)
        if inst.is_instance and data is not None:
            gkey = ("D", data.as_pointer())
        else:
            gkey = ("O", ob.original.name_full if ob.original else ob.name_full,
                    inst.is_instance, data.as_pointer() if data is not None else 0)
        m4 = np.array(inst.matrix_world, dtype=np.float64)
        corners = _transform(bb, m4)
        gr.obb.append(corners)
        gr.obb_unit.append(ui)
        obb_i = len(gr.obb) - 1
        if want_geometry and ob.type in TRI_TYPES:
            if prune and cam is not None and not cam.obbs_inside(corners[None])[0]:
                continue
            occl = rc.occluder_eligible(owner, ob, other_instance)
            # non-occluders are represented by their box (OcclusionScene), so
            # their triangles are never read
            if occl and gr.geom.get(gkey, ob) is None:
                continue
            variant = owner.name_full if key[0] == "G" else None
            gr.chunks.append(_Chunk(ui, m4, gkey, occl, variant, obb_i))
    gr.finalize()
    return gr


# =============================================================================
# 5. Frustum culling
# =============================================================================

def frustum_units(gr, cam, padding):
    """Per unit: inside frustum (user padding) and inside strict frustum
    (no padding; region covered by occlusion rays)."""
    n = len(gr.keys)
    inside_pad = np.zeros(n, dtype=bool)
    inside_strict = np.zeros(n, dtype=bool)
    if len(gr.obb):
        ip = cam.obbs_inside(gr.obb, padding)
        ist = cam.obbs_inside(gr.obb, 0.0)
        np.logical_or.at(inside_pad, gr.obb_unit, ip)
        np.logical_or.at(inside_strict, gr.obb_unit, ist)
    return inside_pad, inside_strict


# =============================================================================
# 6. Occlusion culling
# =============================================================================

# bound_box corner order: 0 ---, 1 --+, 2 -++, 3 -+-, 4 +--, 5 +-+, 6 +++, 7 ++-
_BOX_TRIS = np.array([(0, 1, 2), (0, 2, 3), (4, 7, 6), (4, 6, 5), (0, 4, 5), (0, 5, 1),
                      (3, 2, 6), (3, 6, 7), (0, 3, 7), (0, 7, 4), (1, 5, 6), (1, 6, 2)],
                     dtype=np.int64)


class OcclusionScene:
    """Merged world-space BVH of all relevant geometry + per-triangle lookup
    tables (owning unit, whether it blocks rays)."""

    def __init__(self, gr, chunks, max_tris):
        self.tree = None
        self.tri_unit = None
        self.chunk_tri_counts = []
        self.chunks = chunks
        self.units_with_tris = set()
        self.too_big = False
        self.tri_total = 0
        verts, tris = [], []
        voff = 0
        for ch in chunks:
            if not ch.occluder:
                # A chunk that can never block rays is only there so its own
                # unit can be "seen". Its bounding box is a conservative proxy:
                # a ray that reaches any point of the geometry entered the box
                # earlier on the same ray, with no blocker in between. Dense
                # non-occluders (foliage, alpha cards) then cost 12 triangles.
                lv, lt = gr.obb[ch.obb_i], _BOX_TRIS
                wv = lv
            else:
                local = gr.geom.local.get(ch.gkey)
                if local is None:
                    self.chunk_tri_counts.append(0)
                    continue
                lv, lt = local
                wv = None
            self.tri_total += len(lt)
            if self.tri_total > max_tris:
                self.too_big = True
                return
            verts.append(wv if wv is not None else _transform(lv, ch.m4))
            tris.append(lt + voff)
            voff += len(lv)
            self.chunk_tri_counts.append(len(lt))
            self.units_with_tris.add(ch.unit)
        if not tris:
            return
        v = np.concatenate(verts)
        t = np.concatenate(tris)
        self.tree = BVHTree.FromPolygons(v.tolist(), t.tolist(), all_triangles=True)
        counts = np.array(self.chunk_tri_counts, dtype=np.int64)
        self.tri_unit = np.repeat(np.array([c.unit for c in chunks], dtype=np.int64), counts)
        self._counts = counts
        self.tri_opaque = None

    def set_lod_state(self, active_variants):
        """active_variants: set of variant object names visible this frame.
        Inactive LOD variants never block rays (they will not be rendered),
        but still count as evidence that their group is visible."""
        if self.tree is None:
            return
        flags = np.array([c.occluder and (c.variant is None or c.variant in active_variants)
                          for c in self.chunks], dtype=bool)
        self.tri_opaque = np.repeat(flags, self._counts)


class OcclusionParams:
    def __init__(self, s):
        self.spacing = max(0.5, s.occlusion_sample_spacing)
        self.min_samples = max(1, s.occlusion_min_samples)
        self.max_rays = max(4, s.occlusion_max_rays)
        self.max_pass = max(1, s.occlusion_max_passthrough)


def unit_screen_rects(gr, cam):
    """Per unit: (rect u0,u1,v0,v1 clipped to expanded frame, straddles_near).
    A unit straddling the near plane (or behind/around the camera) is treated
    as visible by the caller."""
    n = len(gr.keys)
    rect = np.zeros((n, 4))
    rect[:, 0] = rect[:, 2] = np.inf
    rect[:, 1] = rect[:, 3] = -np.inf
    straddle = np.zeros(n, dtype=bool)
    if not len(gr.obb):
        return rect, straddle
    c = cam.to_camera(gr.obb.reshape(-1, 3))
    zlim = -cam.clip_start
    bad = c[:, 2] > zlim - 1e-9
    corner_unit = np.repeat(gr.obb_unit, 8)
    np.logical_or.at(straddle, corner_unit, bad)
    good = ~bad
    if np.any(good):
        uv = cam.project_uv(c[good])
        cu = corner_unit[good]
        np.minimum.at(rect[:, 0], cu, uv[:, 0])
        np.maximum.at(rect[:, 1], cu, uv[:, 0])
        np.minimum.at(rect[:, 2], cu, uv[:, 1])
        np.maximum.at(rect[:, 3], cu, uv[:, 1])
    m = cam.margin
    rect[:, 0] = np.maximum(rect[:, 0], -m)
    rect[:, 1] = np.minimum(rect[:, 1], 1.0 + m)
    rect[:, 2] = np.maximum(rect[:, 2], -m)
    rect[:, 3] = np.minimum(rect[:, 3], 1.0 + m)
    return rect, straddle


def unit_visible_by_rays(occ, cam, uidx, rect, params, rng, stats):
    """Cast rays through a grid covering the unit's screen rectangle. The unit
    is visible as soon as ONE ray's first blocking hit is the unit itself.
    Hits on non-blocking geometry (transparent, inactive LOD, excluded
    occluders) are passed through. Returns True (visible) / False (occluded)."""
    u0, u1, v0, v1 = rect
    du, dv = (u1 - u0), (v1 - v0)
    nu = int(math.ceil(du * cam.res_x / params.spacing))
    nv = int(math.ceil(dv * cam.res_y / params.spacing))
    nu = max(nu, params.min_samples)
    nv = max(nv, params.min_samples)
    capped = nu * nv > params.max_rays
    if capped:
        k = math.sqrt(params.max_rays / float(nu * nv))
        nu, nv = max(1, int(nu * k)), max(1, int(nv * k))
    gu = u0 + (np.arange(nu) + 0.5) / nu * du
    gv = v0 + (np.arange(nv) + 0.5) / nv * dv
    uu, vv = np.meshgrid(gu, gv)
    uv = np.stack((uu.ravel(), vv.ravel()), axis=1)
    uv = uv[rng.permutation(len(uv))]  # random order -> early exit sooner
    origins, dirs, maxd = cam.rays(uv)
    tree = occ.tree
    tri_unit = occ.tri_unit
    tri_opaque = occ.tri_opaque
    max_pass = params.max_pass
    for i in range(len(uv)):
        o = Vector(origins[i])
        d = Vector(dirs[i])
        remaining = maxd[i]
        stats["rays"] += 1
        for _ in range(max_pass):
            loc, _nor, idx, dist = tree.ray_cast(o, d, remaining)
            if loc is None:
                break
            if tri_unit[idx] == uidx:
                return True
            if tri_opaque[idx]:
                break
            eps = 1e-4 * max(1.0, dist)
            remaining -= dist + eps
            if remaining <= 0.0:
                break
            o = loc + d * eps
        else:
            return True  # too many pass-through layers: be conservative
    if capped:
        stats["capped"] += 1  # culled with a reduced ray density
    return False


# =============================================================================
# 7. LOD system
# =============================================================================

def scene_object_pointers(scene):
    """Set of as_pointer() of all objects in the scene. scene.objects.get()
    is a linear search, so membership tests go through this set."""
    return {o.as_pointer() for o in scene.objects}


def lod_group_objects(scene, group, scene_ptrs=None):
    """Valid variant objects of a group, in LOD order. Variants that were
    deleted or are not in this scene are dropped."""
    if scene_ptrs is None:
        scene_ptrs = scene_object_pointers(scene)
    out = []
    for v in group.variants:
        o = v.obj
        if not _obj_alive(o):
            continue
        if o.as_pointer() not in scene_ptrs:
            continue
        out.append(o)
    return out


def lod_thresholds(group, s, n_levels):
    src = group.distances if group.use_custom_distances else s.lod_distances
    return [float(src[i]) for i in range(max(0, n_levels - 1))]


def lod_max_distance(group, s):
    if group.use_custom_distances and group.use_max_distance:
        return group.max_distance
    if s.lod_hide_beyond:
        return s.lod_max_distance
    return None


def choose_lod_level(dist, thresholds, current=None, hysteresis=0.0, max_dist=None):
    """Level i is active for thresholds[i-1] <= dist < thresholds[i].
    Hysteresis (fraction) only matters when switching from a known level, to
    stop flicker when the camera hovers around a threshold. Returns -1 when
    beyond max draw distance."""
    if max_dist is not None and max_dist > 0:
        if current == -1 and hysteresis > 0:
            if dist >= max_dist * (1.0 - hysteresis):
                return -1
        elif dist >= max_dist * (1.0 + (hysteresis if current is not None else 0.0)):
            return -1
    n = len(thresholds)
    if current is None or current < 0 or current > n or hysteresis <= 0:
        return sum(1 for t in thresholds if dist >= t)
    lvl = current
    while lvl < n and dist >= thresholds[lvl] * (1.0 + hysteresis):
        lvl += 1
    while lvl > 0 and dist < thresholds[lvl - 1] * (1.0 - hysteresis):
        lvl -= 1
    return lvl


def _visible_in(obj, view_layer):
    try:
        return obj.visible_get(view_layer=view_layer) if view_layer else obj.visible_get()
    except (RuntimeError, TypeError):
        return not obj.hide_viewport


def _computed_world_matrix(obj, view_layer, depth=0):
    """World matrix that stays correct for hidden objects (Blender stops
    updating matrix_world of objects disabled in the viewport): rebuilt from
    the parent chain for un-animated objects."""
    if depth > 32 or _visible_in(obj, view_layer):
        return obj.matrix_world.copy()
    if _object_is_animated(obj):
        return obj.matrix_world.copy()
    if obj.parent is None:
        return obj.matrix_basis.copy()
    if obj.parent_type != 'OBJECT':
        return obj.matrix_world.copy()
    return (_computed_world_matrix(obj.parent, view_layer, depth + 1)
            @ obj.matrix_parent_inverse @ obj.matrix_basis)


def lod_reference(objs, view_layer):
    """(world matrix, local bbox min, local bbox max) used to measure distance.
    Uses LOD0's bounds; LOD variants are expected to share one placement."""
    lod0 = objs[0]
    if _visible_in(lod0, view_layer) or not _object_is_animated(lod0):
        m = _computed_world_matrix(lod0, view_layer)
    else:
        m = None
        for o in objs[1:]:
            if _visible_in(o, view_layer):
                m = o.matrix_world.copy()
                break
        if m is None:
            m = lod0.matrix_world.copy()
    bb = [Vector(c) for c in lod0.bound_box]
    bmin = Vector((min(c.x for c in bb), min(c.y for c in bb), min(c.z for c in bb)))
    bmax = Vector((max(c.x for c in bb), max(c.y for c in bb), max(c.z for c in bb)))
    return m, bmin, bmax


def lod_distance(objs, cam_pos, mode, view_layer):
    m, bmin, bmax = lod_reference(objs, view_layer)
    if mode == 'ORIGIN':
        p = m.translation
        return (p - cam_pos).length, p
    if mode == 'CENTER':
        p = m @ ((bmin + bmax) * 0.5)
        return (p - cam_pos).length, p
    # NEAREST point of the oriented bounding box
    try:
        inv = m.inverted()
    except ValueError:
        p = m.translation
        return (p - cam_pos).length, p
    lp = inv @ cam_pos
    q = Vector((min(max(lp.x, bmin.x), bmax.x), min(max(lp.y, bmin.y), bmax.y),
                min(max(lp.z, bmin.z), bmax.z)))
    p = m @ q
    return (p - cam_pos).length, m @ ((bmin + bmax) * 0.5)


_REF_HALF_ANGLE_TAN = 18.0 / 50.0  # 50 mm lens on a 36 mm sensor


def fov_factor(cam_obj):
    cam = cam_obj.data
    if cam.type != 'PERSP':
        return 1.0
    t = math.tan(cam.angle * 0.5)
    return t / _REF_HALF_ANGLE_TAN if t > 1e-9 else 1.0


def camera_matrix(scene, depsgraph=None):
    cam = scene.camera
    if depsgraph is not None:
        try:
            return cam.evaluated_get(depsgraph).matrix_world.copy()
        except (RuntimeError, ReferenceError):
            pass
    return cam.matrix_world.copy()


def compute_lod_levels(scene, cam_obj, cam_pos, view_layer, use_hysteresis):
    """Returns {group name: (level, objs, distance, ref_point)} and warnings."""
    s = settings_of(scene)
    prev = _Runtime.lod_levels.get(scene.name_full, {})
    h = s.lod_hysteresis / 100.0 if use_hysteresis else 0.0
    ff = fov_factor(cam_obj) if s.lod_fov_compensation else 1.0
    result, warnings = {}, []
    seen = set()
    ptrs = scene_object_pointers(scene)
    for g in s.lod_groups:
        objs = lod_group_objects(scene, g, ptrs)
        if not objs:
            continue
        dup = [o.name for o in objs if o.name_full in seen]
        if dup:
            warnings.append("LOD group '%s': %s already used by another group" % (g.name, ", ".join(dup)))
            continue
        seen.update(o.name_full for o in objs)
        if not all(object_editable(o)[0] for o in objs):
            warnings.append("LOD group '%s' contains linked objects (skipped)" % g.name)
            continue
        dist, ref = lod_distance(objs, cam_pos, s.lod_distance_mode, view_layer)
        dist *= ff
        if g.lock_level >= 0:
            level = min(g.lock_level, len(objs) - 1)
        else:
            th = lod_thresholds(g, s, len(objs))
            level = choose_lod_level(dist, th, prev.get(g.name), h, lod_max_distance(g, s))
            if level >= 0:
                level = min(level, len(objs) - 1)
        result[g.name] = (level, objs, dist, ref)
    return result, warnings


def update_lods(scene, depsgraph=None, live=False, preview=False, clear_others=True):
    """Evaluate every LOD group against the active camera and (unless
    preview) hide all variants except the active one. Returns a report dict."""
    s = settings_of(scene)
    report = {"groups": 0, "changed": 0, "warnings": [], "error": ""}
    cam, err = get_active_camera(scene)
    if cam is None:
        report["error"] = err
        return report
    view_layer = depsgraph.view_layer if depsgraph is not None else None
    cam_mat = camera_matrix(scene, depsgraph)
    cam_pos = cam_mat.translation
    levels, warnings = compute_lod_levels(scene, cam, cam_pos, view_layer, use_hysteresis=live)
    report["warnings"] = warnings
    report["groups"] = len(levels)
    if not preview:
        mapping = {}
        for _name, (level, objs, _d, _r) in levels.items():
            for i, o in enumerate(objs):
                if level < 0:
                    mapping[o] = {R_DISTANCE}
                else:
                    mapping[o] = set() if i == level else {R_LOD}
        _Runtime.busy = True
        try:
            report["changed"] = Visibility.apply_reasons(
                mapping, LOD_REASONS, s.cull_target, clear_others=clear_others)
        finally:
            _Runtime.busy = False
        _Runtime.lod_levels[scene.name_full] = {n: v[0] for n, v in levels.items()}
    overlay = _Runtime.overlay.setdefault(scene.name_full, {})
    # Labels: nearest N groups only (thousands of labels are unreadable),
    # skipping groups that culling has hidden.
    fwd = cam_mat.to_3x3() @ Vector((0.0, 0.0, -1.0))
    shown = sorted((d, lvl, ref) for (lvl, objs, d, ref) in levels.values()
                   if (Vector(ref) - cam_pos).dot(fwd) > 0.0          # in front of camera
                   and not any(Visibility.reasons(o) & CULL_REASONS for o in objs))
    overlay["labels"] = [(tuple(ref), ("LOD%d" % lvl if lvl >= 0 else "HIDDEN") + "  %.1fm" % d,
                          lod_color(lvl)) for (d, lvl, ref) in shown[:s.lod_max_labels]]
    overlay["lod_preview"] = preview
    report["levels"] = {n: v[0] for n, v in levels.items()}
    if s.lod_colors:              # keep colours on groups added since the toggle
        _Runtime.busy = True
        try:
            object_lod_colors(scene, True, prune=False)
        except Exception as exc:
            _log("LOD colours failed: %s" % exc)
        finally:
            _Runtime.busy = False
    return report


def detect_lod_name(name):
    """'Tree_LOD1' -> ('Tree', 1, ''); 'Rock.lod0.003' -> ('Rock', 0, '.003')."""
    m = LOD_RE.match(name)
    if not m:
        return None
    return m.group("base"), int(m.group("lvl")), m.group("dup") or ""


def auto_detect_groups(objects):
    """Group objects by naming convention. Key = (parent, base name) when the
    variants share a parent (typical FBX import), else (base, duplicate
    suffix) so 'Tree_LOD0.001' pairs with 'Tree_LOD1.001'."""
    buckets = {}
    for o in objects:
        info = detect_lod_name(o.name)
        if info is None:
            continue
        base, lvl, dup = info
        if o.parent is not None:
            key = (o.parent.name_full, base, "")
            label = base or o.parent.name
        else:
            key = ("", base, dup)
            label = (base or "LOD") + dup
        buckets.setdefault(key, (label, []))[1].append((lvl, o))
    groups, problems = [], []
    for key, (label, items) in sorted(buckets.items(), key=lambda kv: kv[1][0]):
        levels = [lvl for lvl, _ in items]
        if len(items) < 2:
            problems.append("'%s': only one variant" % items[0][1].name)
            continue
        if len(set(levels)) != len(levels):
            problems.append("'%s': duplicate LOD numbers" % label)
            continue
        items.sort(key=lambda t: t[0])
        if len(items) > MAX_LOD_LEVELS:
            problems.append("'%s': more than %d levels, extra ignored" % (label, MAX_LOD_LEVELS))
            items = items[:MAX_LOD_LEVELS]
        groups.append((label, [o for _, o in items]))
    return groups, problems


def _poly_count(o):
    if o.type == 'MESH' and o.data is not None:
        return len(o.data.polygons)
    return 0


def order_variants(objs):
    """Order by LOD number in the name if every object has one, otherwise by
    polygon count (most detailed first)."""
    infos = [detect_lod_name(o.name) for o in objs]
    if all(infos) and len({i[1] for i in infos}) == len(infos):
        return [o for _, o in sorted(zip([i[1] for i in infos], objs), key=lambda t: t[0])]
    return sorted(objs, key=_poly_count, reverse=True)


def unique_group_name(s, base):
    names = {g.name for g in s.lod_groups}
    if base not in names:
        return base
    i = 1
    while "%s.%03d" % (base, i) in names:
        i += 1
    return "%s.%03d" % (base, i)


def invalidate_lod_cache(scene=None):
    if scene is None:
        _Runtime.lod_obj_names.clear()
    else:
        _Runtime.lod_obj_names.pop(scene.name_full, None)


def lod_object_names(scene):
    names = _Runtime.lod_obj_names.get(scene.name_full)
    if names is None:
        names = set()
        for g in settings_of(scene).lod_groups:
            for v in g.variants:
                if _obj_alive(v.obj):
                    names.add(v.obj.name)
        _Runtime.lod_obj_names[scene.name_full] = names
    return names


# =============================================================================
# 8. Culling pipeline
# =============================================================================

def _frames_to_evaluate(scene, s):
    if s.frame_mode == 'CURRENT':
        return [scene.frame_current]
    step = max(1, s.frame_step)
    frames = list(range(scene.frame_start, scene.frame_end + 1, step))
    if not frames or frames[-1] != scene.frame_end:
        frames.append(scene.frame_end)
    return frames


def run_culling(context, scene, preview):
    """Evaluate frustum + occlusion culling. preview=True leaves the scene's
    visibility exactly as it was. Returns a report dict."""
    s = settings_of(scene)
    view_layer = context.view_layer
    t_start = time.perf_counter()
    rep = {"error": "", "warnings": [], "frustum": 0, "occlusion": 0, "candidates": 0,
           "rays": 0, "tris": 0, "frames": 0, "time": 0.0, "lod": None}
    cam_obj, err = get_active_camera(scene)
    if cam_obj is None:
        rep["error"] = err
        return rep
    if cam_obj.data.type == 'PANO':
        rep["error"] = "Panoramic cameras are not supported (frustum is not a pyramid)"
        return rep
    use_frustum = s.frustum_enabled
    use_occl = s.occlusion_enabled
    if not (use_frustum or use_occl):
        rep["warnings"].append("Frustum and occlusion culling are both disabled")

    snapshot = Visibility.snapshot()
    orig_frame, orig_sub = scene.frame_current, scene.frame_subframe
    multi = False
    _Runtime.busy = True
    try:
        # Everything must be visible to be evaluated by the depsgraph.
        Visibility.restore(ALL_REASONS, s.cull_target)
        view_layer.update()
        rc = RunContext(scene, view_layer, cam_obj)
        frames = _frames_to_evaluate(scene, s) if (use_frustum or use_occl) else []
        multi = len(frames) > 1
        params = OcclusionParams(s)
        rng = np.random.default_rng(12345)
        stats = {"rays": 0, "capped": 0}

        ever_frustum = set()      # unit keys inside the frustum at some frame
        ever_visible = set()      # unit keys visible at some frame
        ever_lod_active = set()   # LOD group keys not distance-hidden at some frame
        candidates = {}           # key -> owner
        static_cache = None
        overlay_boxes = {}
        last_cam = None

        for f in frames:
            if multi:
                scene.frame_set(f)
            dg = context.evaluated_depsgraph_get()
            cam = CameraModel(scene, cam_obj, cam_obj.evaluated_get(dg).matrix_world,
                              s.frustum_margin if use_frustum else 0.0, s.frustum_use_clip)
            last_cam = cam
            # LOD state for this frame (no hysteresis: fresh evaluation)
            active_variants, hidden_groups = set(), set()
            if rc.lod_enabled and rc.groups:
                levels, w = compute_lod_levels(scene, cam_obj, cam.origin, view_layer, False)
                for gname, (lvl, objs, _d, _r) in levels.items():
                    if lvl < 0:
                        hidden_groups.add(gname)
                    else:
                        active_variants.add(objs[lvl].name_full)
                        ever_lod_active.add(("G", gname))
                if f == frames[0]:
                    rep["warnings"] += w

            if static_cache is not None:
                gr, occ = static_cache
            else:
                gr = gather(dg, rc, cam=cam, want_geometry=use_occl,
                            prune=not (multi and s.static_geometry))
                occ = None
            n = len(gr.keys)
            inside_pad, inside_strict = frustum_units(gr, cam, s.frustum_padding)
            rect, straddle = unit_screen_rects(gr, cam)

            # Decide per unit
            to_test = []
            for ui in range(n):
                key = gr.keys[ui]
                owner = gr.owner[ui]
                if not rc.is_candidate(key, owner):
                    continue
                if key[0] == "G" and key[1] in hidden_groups:
                    continue  # LOD hides it this frame anyway
                candidates[key] = owner
                if use_frustum and not inside_pad[ui]:
                    continue
                ever_frustum.add(key)
                if key in ever_visible:
                    continue
                if not use_occl:
                    ever_visible.add(key)
                    continue
                r = rect[ui]
                if straddle[ui] or not inside_strict[ui] or r[0] >= r[1] or r[2] >= r[3]:
                    ever_visible.add(key)  # untestable -> keep (conservative)
                    continue
                to_test.append(ui)

            if use_occl and to_test:
                if occ is None:
                    test_set = set(to_test)
                    chunks = [c for c in gr.chunks if c.occluder or c.unit in test_set] \
                        if not (multi and s.static_geometry) else \
                        [c for c in gr.chunks if c.occluder or rc.is_candidate(gr.keys[c.unit], gr.owner[c.unit])]
                    occ = OcclusionScene(gr, chunks, s.occlusion_max_tris)
                    rep["tris"] = max(rep["tris"], occ.tri_total)
                    if multi and s.static_geometry:
                        static_cache = (gr, occ)
                if occ.too_big:
                    rep["warnings"].append(
                        "Occlusion skipped: more than %d triangles. Raise 'Max Triangles' "
                        "or restrict occluders with an Occluder Collection" % s.occlusion_max_tris)
                    use_occl = False
                    for ui in to_test:
                        ever_visible.add(gr.keys[ui])
                else:
                    occ.set_lod_state(active_variants)
                    for ui in to_test:
                        key = gr.keys[ui]
                        if occ.tree is None or ui not in occ.units_with_tris:
                            ever_visible.add(key)  # no triangles -> untestable
                            continue
                        if unit_visible_by_rays(occ, cam, ui, rect[ui], params, rng, stats):
                            ever_visible.add(key)
            if multi and s.static_geometry and static_cache is None:
                static_cache = (gr, occ)
            # Overlay boxes of the frame closest to the current one
            if f == orig_frame or not overlay_boxes or f == frames[-1]:
                overlay_boxes = {"gr": gr}
            rep["frames"] += 1

        if multi:
            scene.frame_set(orig_frame, subframe=orig_sub)

        # Results
        cull_map = {}
        result_rows = []
        for key, owner in candidates.items():
            if key in ever_visible:
                continue
            if key[0] == "G" and key not in ever_lod_active:
                continue
            reason = R_FRUSTUM if key not in ever_frustum else R_OCCLUSION
            rep["frustum" if reason == R_FRUSTUM else "occlusion"] += 1
            for o in rc.unit_objects(key, owner):
                cull_map[o] = {reason}
                result_rows.append((o, reason))
        rep["candidates"] = len(candidates)
        rep["rays"] = stats["rays"]
        if stats["capped"]:
            rep["warnings"].append("%d occluded objects were tested with a reduced ray density "
                                   "(Max Rays per Object); raise it for large objects" % stats["capped"])
        if rc.skipped["linked"]:
            rep["warnings"].append("%d linked objects skipped (not editable)" % rc.skipped["linked"])
        if rc.skipped["animated"]:
            rep["warnings"].append("%d objects with animated visibility skipped" % rc.skipped["animated"])

        # Apply / roll back
        if preview:
            Visibility.apply_snapshot(snapshot, s.cull_target)
        else:
            Visibility.apply_reasons(cull_map, CULL_REASONS, s.cull_target, clear_others=True)
            _Runtime.cull_cam_sig[scene.name_full] = (
                _camera_signature(scene) if s.frame_mode == 'CURRENT' else None)
            # re-apply LOD state from scratch for the current camera
            lod_snap = [(o, rs & LOD_REASONS) for o, rs in snapshot if rs & LOD_REASONS]
            if s.lod_enabled:
                Visibility.apply_snapshot(lod_snap, s.cull_target)
        _store_results(s, result_rows)
        # A single frustum outline is only meaningful for one camera position.
        _store_overlay(scene, overlay_boxes.get("gr"), last_cam if not multi else None,
                       cull_map, rc, preview)
    except Exception:
        # Never leave the scene half-processed.
        try:
            if multi:
                scene.frame_set(orig_frame, subframe=orig_sub)
        except Exception:
            pass
        Visibility.restore(ALL_REASONS, s.cull_target)
        Visibility.apply_snapshot(snapshot, s.cull_target)
        raise
    finally:
        _Runtime.busy = False

    if not preview and s.lod_enabled:
        rep["lod"] = update_lods(scene, live=False, clear_others=True)
    rep["time"] = time.perf_counter() - t_start
    s.last_report = _format_report(rep, preview)
    return rep


def _store_results(s, rows):
    s.results.clear()
    for o, reason in sorted(rows, key=lambda t: (t[1], t[0].name)):
        it = s.results.add()
        it.obj = o
        it.reason = reason
    s.results_index = 0


def _format_report(rep, preview):
    if rep["error"]:
        return "Error: " + rep["error"]
    txt = "%s: %d frustum, %d occluded of %d candidates (%d frame%s, %.2fs)" % (
        "Preview" if preview else "Applied", rep["frustum"], rep["occlusion"],
        rep["candidates"], rep["frames"], "" if rep["frames"] == 1 else "s", rep["time"])
    return txt


# =============================================================================
# 9. Viewport overlay
# =============================================================================

# LOD0..LOD7: overlay labels, scatter LOD colours and object LOD colours
LOD_COLORS = [(0.10, 0.75, 0.15, 1.0), (0.95, 0.85, 0.05, 1.0), (1.00, 0.40, 0.00, 1.0),
              (0.90, 0.05, 0.05, 1.0), (0.85, 0.10, 0.75, 1.0), (0.45, 0.15, 0.90, 1.0),
              (0.10, 0.35, 1.00, 1.0), (0.05, 0.85, 0.90, 1.0)]
LOD_COLOR_NAMES = ("green", "yellow", "orange", "red", "magenta", "purple", "blue", "cyan")
COLOR_FRUSTUM = (1.0, 0.2, 0.2, 1.0)
COLOR_OCCLUSION = (1.0, 0.6, 0.0, 1.0)
COLOR_CAMERA = (0.2, 0.8, 1.0, 1.0)
COLOR_HIDDEN = (0.55, 0.55, 0.55, 1.0)
BOX_EDGES = [(0, 1), (1, 2), (2, 3), (3, 0), (4, 5), (5, 6), (6, 7), (7, 4),
             (0, 4), (1, 5), (2, 6), (3, 7)]
MAX_OVERLAY_BOXES = 20000
MAX_OVERLAY_LABELS = 400


def lod_color(level):
    if level < 0:
        return COLOR_HIDDEN
    return LOD_COLORS[level % len(LOD_COLORS)]


def _store_overlay(scene, gr, cam, cull_map, rc, preview):
    ov = _Runtime.overlay.setdefault(scene.name_full, {})
    pos, col = [], []
    if gr is not None and len(gr.obb):
        culled_reason = {}
        for o, rs in cull_map.items():
            culled_reason[o.name_full] = next(iter(rs))
        for i in range(min(len(gr.obb), MAX_OVERLAY_BOXES)):
            ui = gr.obb_unit[i]
            key = gr.keys[ui]
            objs = rc.unit_objects(key, gr.owner[ui])
            reason = culled_reason.get(objs[0].name_full) if objs else None
            if reason is None:
                continue
            c = COLOR_FRUSTUM if reason == R_FRUSTUM else COLOR_OCCLUSION
            corners = gr.obb[i]
            for a, b in BOX_EDGES:
                pos += [tuple(corners[a]), tuple(corners[b])]
                col += [c, c]
    if cam is not None:
        for p in cam.frustum_lines(200.0):
            pos.append(p)
            col.append(COLOR_CAMERA)
    ov["lines"] = (pos, col)
    ov["batch"] = None
    ov["preview"] = preview
    tag_redraw_all()


def clear_overlay(scene):
    _Runtime.overlay.pop(scene.name_full, None)
    tag_redraw_all()


def _draw_overlay_3d():
    ctx = bpy.context
    scene = ctx.scene
    if scene is None or not settings_of(scene).show_overlay:
        return
    ov = _Runtime.overlay.get(scene.name_full)
    if not ov or "lines" not in ov:
        return
    try:
        import gpu
        from gpu_extras.batch import batch_for_shader
        shader = gpu.shader.from_builtin('FLAT_COLOR')
        if ov.get("batch") is None:
            pos, col = ov["lines"]
            if not pos:
                return
            ov["batch"] = batch_for_shader(shader, 'LINES', {"pos": pos, "color": col})
        gpu.state.blend_set('ALPHA')
        gpu.state.depth_test_set('NONE')
        gpu.state.line_width_set(2.0)
        ov["batch"].draw(shader)
        gpu.state.line_width_set(1.0)
        gpu.state.blend_set('NONE')
    except Exception as exc:  # drawing must never raise into Blender
        ov["lines"] = ([], [])
        _log("Overlay drawing disabled: %s" % exc)


def _draw_overlay_2d():
    ctx = bpy.context
    scene = ctx.scene
    if scene is None or not settings_of(scene).show_overlay:
        return
    ov = _Runtime.overlay.get(scene.name_full)
    if not ov or not ov.get("labels"):
        return
    try:
        import blf
        from bpy_extras.view3d_utils import location_3d_to_region_2d
        region, rv3d = ctx.region, ctx.region_data
        if region is None or rv3d is None:
            return
        font = 0
        blf.size(font, 12)
        drawn = 0
        for pos, text, color in ov["labels"]:
            p2 = location_3d_to_region_2d(region, rv3d, Vector(pos))
            if p2 is None or not (0 <= p2.x <= region.width and 0 <= p2.y <= region.height):
                continue
            blf.color(font, *color)
            blf.position(font, p2.x + 6, p2.y + 6, 0)
            blf.draw(font, text)
            drawn += 1
            if drawn >= MAX_OVERLAY_LABELS:
                break
    except Exception as exc:
        ov["labels"] = []
        _log("Label drawing disabled: %s" % exc)


# =============================================================================
# 10. Handlers (live LOD switching, scatter camera sync)
# =============================================================================
#
# No per-frame work happens unless "Live Update" is enabled for LOD. Even
# then the depsgraph handler only compares a camera signature and scans the
# update list; the actual LOD evaluation runs in a debounced timer.

def _camera_signature(scene):
    cam = scene.camera
    if cam is None or cam.type != 'CAMERA':
        return None
    m = cam.matrix_world
    d = cam.data
    return tuple(round(v, 5) for row in m for v in row) + (
        round(d.lens, 4), round(d.ortho_scale, 4), d.type, round(d.sensor_width, 4))


def _lod_timer():
    name = _Runtime.timer_scene
    _Runtime.timer_scene = None
    scene = bpy.data.scenes.get(name) if name else None
    if scene is None:
        return None
    s = settings_of(scene)
    if s.lod_enabled and s.lod_live and not _Runtime.busy:
        try:
            update_lods(scene, live=True, clear_others=False)
            tag_redraw_all()
        except Exception as exc:
            _log("Live LOD update failed: %s" % exc)
    return None


@persistent
def _on_depsgraph_update(scene, depsgraph):
    if _Runtime.busy or scene is None:
        return
    s = settings_of(scene)
    if s.scatter_systems:
        scatter_camera_sync(scene)
    if not (s.lod_enabled and s.lod_live):
        return
    sig = _camera_signature(scene)
    if sig is None:
        return
    dirty = sig != _Runtime.cam_sig.get(scene.name_full)
    if not dirty:
        names = lod_object_names(scene)
        if names:
            for upd in depsgraph.updates:
                idd = upd.id
                if isinstance(idd, bpy.types.Object) and upd.is_updated_transform \
                        and idd.name in names:
                    dirty = True
                    break
    if not dirty:
        return
    _Runtime.cam_sig[scene.name_full] = sig
    if _Runtime.timer_scene is None:
        _Runtime.timer_scene = scene.name_full
        if not bpy.app.timers.is_registered(_lod_timer):
            bpy.app.timers.register(_lod_timer, first_interval=0.05)


@persistent
def _on_frame_change(scene, depsgraph=None):
    """Synchronous so animation playback and animation renders switch LODs
    on the correct frame (timers do not run during renders)."""
    if _Runtime.busy or scene is None:
        return
    s = settings_of(scene)
    if s.scatter_systems:
        scatter_camera_sync(scene)   # animated lens / camera switches
    if not (s.lod_enabled and s.lod_live and s.lod_on_frame_change):
        return
    try:
        update_lods(scene, depsgraph=depsgraph, live=True, clear_others=False)
        _Runtime.cam_sig[scene.name_full] = _camera_signature(scene)
    except Exception as exc:
        _log("Frame-change LOD update failed: %s" % exc)


@persistent
def _on_load_post(*_args):
    _Runtime.cam_sig.clear()
    _Runtime.lod_levels.clear()
    _Runtime.lod_obj_names.clear()
    _Runtime.overlay.clear()
    _Runtime.timer_scene = None
    _Runtime.counts = None
    _Runtime.cull_cam_sig.clear()
    _Runtime.scatter_cam_sig.clear()
    _mat_transparent_cache.clear()


# =============================================================================
# 11. Properties
# =============================================================================

def _variant_poll(_self, obj):
    return obj.type in GEOM_TYPES or (obj.type == 'EMPTY' and obj.instance_type == 'COLLECTION')


def _lod_changed(_self, context):
    invalidate_lod_cache()


def _overlay_toggle(_self, _context):
    tag_redraw_all()


def _cull_target_changed(self, context):
    if _Runtime.busy:
        return
    Visibility.resync(self.cull_target)


def _lod_enabled_changed(self, context):
    if _Runtime.busy:
        return
    if not self.lod_enabled:
        Visibility.restore(LOD_REASONS, self.cull_target)
        ov = _Runtime.overlay.get(context.scene.name_full)
        if ov:
            ov["labels"] = []
        tag_redraw_all()


def _lod_live_changed(self, context):
    _Runtime.cam_sig.pop(context.scene.name_full, None)
    if self.lod_enabled and self.lod_live:
        update_lods(context.scene, live=True)
        tag_redraw_all()


OCCLUSION_PRESETS = {
    'FAST': (12.0, 4, 1024),
    'BALANCED': (6.0, 8, 4096),
    'ACCURATE': (2.0, 12, 16384),
}


def _occl_preset_changed(self, _context):
    p = OCCLUSION_PRESETS.get(self.occlusion_preset)
    if p is not None:
        self.occlusion_sample_spacing, self.occlusion_min_samples, self.occlusion_max_rays = p


class SCENEOPT_PG_lod_variant(PropertyGroup):
    obj: PointerProperty(name="Object", type=bpy.types.Object, poll=_variant_poll,
                         update=_lod_changed)


class SCENEOPT_PG_lod_group(PropertyGroup):
    name: StringProperty(name="Name", default="LOD Group")
    variants: CollectionProperty(type=SCENEOPT_PG_lod_variant)
    variant_index: IntProperty(default=0)
    use_custom_distances: BoolProperty(
        name="Custom Distances", default=False,
        description="Use per-group switch distances instead of the scene defaults")
    distances: FloatVectorProperty(
        name="Distances", size=MAX_LOD_LEVELS - 1, default=DEFAULT_LOD_DISTANCES,
        min=0.0, subtype='NONE', unit='LENGTH',
        description="Distance at which LOD1, LOD2, ... become active")
    use_max_distance: BoolProperty(name="Max Draw Distance", default=False,
                                   description="Hide the whole group beyond a distance")
    max_distance: FloatProperty(name="Max Distance", default=1000.0, min=0.0, unit='LENGTH')
    lock_level: IntProperty(
        name="Lock Level", default=-1, min=-1, max=MAX_LOD_LEVELS - 1,
        description="-1 = automatic, otherwise always show this LOD level")


class SCENEOPT_PG_result(PropertyGroup):
    obj: PointerProperty(type=bpy.types.Object)
    reason: EnumProperty(items=[(R_FRUSTUM, "Frustum", "Outside the camera frustum"),
                                (R_OCCLUSION, "Occluded", "Hidden behind other geometry")])


class SCENEOPT_PG_settings(PropertyGroup):
    lod_colors: BoolProperty(
        name="Visualize LODs", default=False,
        update=lambda self, ctx: _lod_colors_changed(self, ctx),
        description="Viewport only: colour LOD group variants and every scatter system by LOD "
                    "level (LOD0 green, LOD1 yellow, LOD2 orange, LOD3 red, then magenta, "
                    "purple, blue, cyan). In Solid shading, Color must be set to Material")
    # ---- general
    cull_target: EnumProperty(
        name="Cull Target",
        items=[('BOTH', "Viewport & Render", "Hide culled objects in viewport and render"),
               ('VIEWPORT', "Viewport", "Only disable culled objects in the viewport"),
               ('RENDER', "Render", "Only disable culled objects in renders")],
        default='BOTH', update=_cull_target_changed,
        description="Which visibility culling writes (LOD always switches both)")
    show_overlay: BoolProperty(name="Show Overlay", default=True, update=_overlay_toggle,
                               description="Draw culled bounds, camera frustum and LOD labels")
    exclude_collection: PointerProperty(
        name="Never Cull", type=bpy.types.Collection,
        description="Objects in this collection (recursively) are never culled")
    frame_mode: EnumProperty(
        name="Evaluate",
        items=[('CURRENT', "Current Frame", "Cull for the current camera position only"),
               ('RANGE', "Frame Range", "Keep anything visible at ANY sampled frame of the "
                                        "scene range (for animated cameras/objects)")],
        default='CURRENT')
    frame_step: IntProperty(name="Step", default=5, min=1,
                            description="Evaluate every Nth frame (end frame always included)")
    static_geometry: BoolProperty(
        name="Static Geometry", default=False,
        description="Frame range: build occlusion geometry once and only move the camera. "
                    "Much faster, but wrong if objects move")
    # ---- frustum
    frustum_enabled: BoolProperty(name="Frustum Culling", default=True)
    frustum_margin: FloatProperty(
        name="Margin", default=10.0, min=0.0, max=200.0, subtype='PERCENTAGE',
        description="Expand the camera frame by this percentage of its size on every side")
    frustum_padding: FloatProperty(
        name="Padding", default=0.5, min=0.0, unit='LENGTH',
        description="Keep objects whose bounds are within this world distance of the frustum")
    frustum_use_clip: BoolProperty(name="Use Clip Distances", default=True,
                                   description="Cull objects beyond the camera clip end / before clip start")
    # ---- occlusion
    occlusion_enabled: BoolProperty(name="Occlusion Culling", default=False)
    occlusion_preset: EnumProperty(
        name="Accuracy",
        items=[('FAST', "Fast", "12 px spacing"), ('BALANCED', "Balanced", "6 px spacing"),
               ('ACCURATE', "Accurate", "2 px spacing"), ('CUSTOM', "Custom", "Manual settings")],
        default='BALANCED', update=_occl_preset_changed)
    occlusion_sample_spacing: FloatProperty(
        name="Ray Spacing (px)", default=6.0, min=0.5, max=128.0,
        description="Screen distance between test rays at render resolution. A visible "
                    "sliver narrower than this can be missed")
    occlusion_min_samples: IntProperty(
        name="Min Rays per Axis", default=8, min=1, max=128,
        description="Minimum rays across each object's screen footprint (protects small objects)")
    occlusion_max_rays: IntProperty(
        name="Max Rays per Object", default=4096, min=4, max=1000000,
        description="Ray budget per object; spacing grows when exceeded")
    occluder_collection: PointerProperty(
        name="Occluders", type=bpy.types.Collection,
        description="If set, only objects in this collection block visibility")
    occlusion_ignore_transparent: BoolProperty(
        name="Transparent Materials Don't Occlude", default=True,
        description="Objects with glass/alpha/transmission materials let rays through")
    occlusion_instance_occluders: BoolProperty(
        name="Particle/GN Instances Occlude", default=False,
        description="Let particle and geometry-nodes instances block rays (collection "
                    "instances always can). Off is safer for foliage")
    occlusion_max_tris: IntProperty(name="Max Triangles", default=8_000_000, min=1000,
                                    description="Abort occlusion if the BVH would exceed this")
    occlusion_max_passthrough: IntProperty(
        name="Max Pass-Through", default=16, min=1, max=256,
        description="Transparent layers a ray may cross before the object counts as visible")
    # ---- LOD
    lod_enabled: BoolProperty(name="LOD Switching", default=False, update=_lod_enabled_changed)
    lod_live: BoolProperty(name="Live Update", default=False, update=_lod_live_changed,
                           description="Switch LODs automatically when the camera or LOD objects move")
    lod_on_frame_change: BoolProperty(
        name="Update on Frame Change", default=True,
        description="With Live Update: switch during playback and animation renders")
    lod_distance_mode: EnumProperty(
        name="Measure To",
        items=[('CENTER', "Bounds Center", "Distance to LOD0's bounding box center"),
               ('NEAREST', "Nearest Bounds", "Distance to the closest point of LOD0's bounds "
                                             "(better for large objects)"),
               ('ORIGIN', "Origin", "Distance to LOD0's object origin")],
        default='CENTER')
    lod_distances: FloatVectorProperty(
        name="Default Distances", size=MAX_LOD_LEVELS - 1, default=DEFAULT_LOD_DISTANCES,
        min=0.0, unit='LENGTH', description="Distance at which LOD1, LOD2, ... become active")
    lod_hysteresis: FloatProperty(name="Hysteresis", default=5.0, min=0.0, max=50.0,
                                  subtype='PERCENTAGE',
                                  description="Live mode: band around thresholds that prevents flicker")
    lod_hide_beyond: BoolProperty(name="Max Draw Distance", default=False,
                                  description="Hide whole LOD groups beyond a distance")
    lod_max_distance: FloatProperty(name="Max Distance", default=2000.0, min=0.0, unit='LENGTH')
    lod_fov_compensation: BoolProperty(
        name="Focal Length Compensation", default=False,
        description="Scale distances by FOV relative to a 50 mm lens, so telephoto shots keep detail")
    lod_max_labels: IntProperty(
        name="Max LOD Labels", default=40, min=0, max=MAX_OVERLAY_LABELS, update=_overlay_toggle,
        description="Overlay: label only the N nearest LOD groups (0 = no labels)")
    lod_groups: CollectionProperty(type=SCENEOPT_PG_lod_group)
    lod_group_index: IntProperty(default=0)
    lod_detect_collection: PointerProperty(
        name="Source", type=bpy.types.Collection,
        description="Collection to scan for *_LOD# objects (empty = selected objects)")
    # ---- results
    results: CollectionProperty(type=SCENEOPT_PG_result)
    results_index: IntProperty(default=0)
    last_report: StringProperty(default="")


# =============================================================================
# 12. Operators
# =============================================================================

def _report_warnings(op, warnings):
    for w in warnings:
        _log("Warning: " + w)
    if warnings:
        op.report({'WARNING'}, warnings[0] + (" (+%d more, see console)" % (len(warnings) - 1)
                                              if len(warnings) > 1 else ""))


class SCENEOPT_OT_preview(Operator):
    bl_idname = "scene_optimizer.preview"
    bl_label = "Preview"
    bl_description = ("Compute culling and LOD levels without changing the scene. "
                      "Culled objects are listed and drawn in the overlay")
    bl_options = {'REGISTER'}

    def execute(self, context):
        scene = context.scene
        s = settings_of(scene)
        rep = run_culling(context, scene, preview=True) if (s.frustum_enabled or s.occlusion_enabled) \
            else {"error": "", "warnings": []}
        if rep["error"]:
            self.report({'ERROR'}, rep["error"])
            return {'CANCELLED'}
        if s.lod_enabled:
            lrep = update_lods(scene, preview=True)
            rep["warnings"] += lrep["warnings"]
        _report_warnings(self, rep["warnings"])
        if s.last_report:
            self.report({'INFO'}, s.last_report)
        s.show_overlay = True
        return {'FINISHED'}


class SCENEOPT_OT_apply(Operator):
    bl_idname = "scene_optimizer.apply"
    bl_label = "Apply"
    bl_description = "Hide culled objects and switch LODs (fully reversible with Restore)"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        scene = context.scene
        s = settings_of(scene)
        warnings = []
        if s.frustum_enabled or s.occlusion_enabled:
            rep = run_culling(context, scene, preview=False)
            if rep["error"]:
                self.report({'ERROR'}, rep["error"])
                return {'CANCELLED'}
            warnings += rep["warnings"]
            if rep.get("lod"):
                warnings += rep["lod"]["warnings"]
        else:
            Visibility.restore(CULL_REASONS, s.cull_target)
            s.results.clear()
            if s.lod_enabled:
                lrep = update_lods(scene)
                if lrep["error"]:
                    self.report({'ERROR'}, lrep["error"])
                    return {'CANCELLED'}
                warnings += lrep["warnings"]
                s.last_report = "Applied LOD to %d groups" % lrep["groups"]
        _report_warnings(self, warnings)
        self.report({'INFO'}, s.last_report)
        return {'FINISHED'}


class SCENEOPT_OT_restore(Operator):
    bl_idname = "scene_optimizer.restore"
    bl_label = "Restore"
    bl_description = "Restore the original visibility of every object the add-on changed"
    bl_options = {'REGISTER', 'UNDO'}

    what: EnumProperty(items=[('ALL', "All", ""), ('CULL', "Culling", ""), ('LOD', "LOD", "")],
                       default='ALL')

    def execute(self, context):
        s = settings_of(context.scene)
        owned = {'ALL': ALL_REASONS, 'CULL': CULL_REASONS, 'LOD': LOD_REASONS}[self.what]
        _Runtime.busy = True
        try:
            n = Visibility.restore(owned, s.cull_target)
        finally:
            _Runtime.busy = False
        if self.what in {'ALL', 'CULL'}:
            _Runtime.cull_cam_sig.pop(context.scene.name_full, None)
            s.results.clear()
            s.last_report = ""
        if self.what == 'ALL':
            # Stop live LOD from immediately re-hiding variants.
            if s.lod_live:
                _Runtime.busy = True
                s.lod_live = False
                _Runtime.busy = False
            clear_overlay(context.scene)
        self.report({'INFO'}, "Restored %d objects" % n)
        return {'FINISHED'}


class SCENEOPT_OT_clear_overlay(Operator):
    bl_idname = "scene_optimizer.clear_overlay"
    bl_label = "Clear Overlay"
    bl_options = {'REGISTER'}

    def execute(self, context):
        clear_overlay(context.scene)
        return {'FINISHED'}


class SCENEOPT_OT_select_results(Operator):
    bl_idname = "scene_optimizer.select_results"
    bl_label = "Select Culled"
    bl_description = "Select the objects in the result list (only possible while they are visible, e.g. after Preview)"
    bl_options = {'REGISTER', 'UNDO'}

    reason: EnumProperty(items=[('ALL', "All", ""), (R_FRUSTUM, "Frustum", ""),
                                (R_OCCLUSION, "Occluded", "")], default='ALL')

    def execute(self, context):
        s = settings_of(context.scene)
        vl = context.view_layer
        for o in context.selected_objects:
            o.select_set(False)
        n = skipped = 0
        for it in s.results:
            if it.obj is None or (self.reason != 'ALL' and it.reason != self.reason):
                continue
            if it.obj.name not in vl.objects or not it.obj.visible_get():
                skipped += 1
                continue
            it.obj.select_set(True)
            vl.objects.active = it.obj
            n += 1
        msg = "Selected %d objects" % n
        if skipped:
            msg += " (%d hidden objects cannot be selected - use Preview instead of Apply)" % skipped
        self.report({'INFO'}, msg)
        return {'FINISHED'}


class SCENEOPT_OT_lod_apply(Operator):
    bl_idname = "scene_optimizer.lod_apply"
    bl_label = "Update LODs"
    bl_description = "Evaluate all LOD groups for the active camera now"
    bl_options = {'REGISTER', 'UNDO'}

    preview: BoolProperty(default=False, options={'SKIP_SAVE'})

    def execute(self, context):
        rep = update_lods(context.scene, preview=self.preview)
        if rep["error"]:
            self.report({'ERROR'}, rep["error"])
            return {'CANCELLED'}
        _report_warnings(self, rep["warnings"])
        if self.preview:
            settings_of(context.scene).show_overlay = True
        self.report({'INFO'}, "%s %d LOD groups" % ("Previewed" if self.preview else "Updated",
                                                     rep["groups"]))
        tag_redraw_all()
        return {'FINISHED'}


def _objects_in_groups(s):
    used = set()
    for g in s.lod_groups:
        for v in g.variants:
            if _obj_alive(v.obj):
                used.add(v.obj.name_full)
    return used


def _add_group(s, name, objs):
    g = s.lod_groups.add()
    g.name = unique_group_name(s, name)
    for o in objs:
        v = g.variants.add()
        v.obj = o
    return g


class SCENEOPT_OT_lod_group_from_selected(Operator):
    bl_idname = "scene_optimizer.lod_group_from_selected"
    bl_label = "Group from Selected"
    bl_description = ("Create ONE LOD group from the selected objects, ordered by LOD number "
                      "in their names, or by polygon count (most detailed = LOD0)")
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        s = settings_of(context.scene)
        used = _objects_in_groups(s)
        objs = [o for o in context.selected_objects if _variant_poll(None, o)
                and o.name_full not in used]
        if len(objs) < 2:
            self.report({'ERROR'}, "Select at least 2 objects that are not already in an LOD group")
            return {'CANCELLED'}
        if len(objs) > MAX_LOD_LEVELS:
            self.report({'ERROR'}, "At most %d LOD levels are supported" % MAX_LOD_LEVELS)
            return {'CANCELLED'}
        objs = order_variants(objs)
        info = detect_lod_name(objs[0].name)
        name = (info[0] + info[2]) if info and info[0] else objs[0].name
        _add_group(s, name, objs)
        s.lod_group_index = len(s.lod_groups) - 1
        invalidate_lod_cache()
        self.report({'INFO'}, "Created LOD group '%s' with %d levels" % (name, len(objs)))
        return {'FINISHED'}


class SCENEOPT_OT_lod_autodetect(Operator):
    bl_idname = "scene_optimizer.lod_autodetect"
    bl_label = "Auto-Detect Groups"
    bl_description = ("Create LOD groups from objects named like 'Name_LOD0', 'Name_LOD1' in the "
                      "source collection (or the selection). Works for many assets at once")
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        s = settings_of(context.scene)
        if s.lod_detect_collection is not None:
            pool = list(s.lod_detect_collection.all_objects)
        else:
            pool = list(context.selected_objects)
        if not pool:
            self.report({'ERROR'}, "Select objects or choose a source collection")
            return {'CANCELLED'}
        used = _objects_in_groups(s)
        scene_objs = context.scene.objects
        pool = [o for o in pool if o.name_full not in used and _variant_poll(None, o)
                and scene_objs.get(o.name) is not None]
        groups, problems = auto_detect_groups(pool)
        for name, objs in groups:
            _add_group(s, name, objs)
        invalidate_lod_cache()
        for p in problems:
            _log("LOD auto-detect: " + p)
        if not groups:
            self.report({'WARNING'}, "No LOD groups found (expected names like 'Tree_LOD0')")
            return {'CANCELLED'}
        self.report({'INFO'}, "Created %d LOD groups%s" % (
            len(groups), (", %d issues (see console)" % len(problems)) if problems else ""))
        return {'FINISHED'}


class SCENEOPT_OT_lod_group_remove(Operator):
    bl_idname = "scene_optimizer.lod_group_remove"
    bl_label = "Remove LOD Group"
    bl_description = "Remove the active LOD group (its variants become visible again)"
    bl_options = {'REGISTER', 'UNDO'}

    all: BoolProperty(default=False, options={'SKIP_SAVE'})

    def execute(self, context):
        s = settings_of(context.scene)
        if not len(s.lod_groups):
            return {'CANCELLED'}
        groups = list(range(len(s.lod_groups))) if self.all else [s.lod_group_index]
        for gi in sorted(groups, reverse=True):
            if 0 <= gi < len(s.lod_groups):
                for v in s.lod_groups[gi].variants:
                    if _obj_alive(v.obj):
                        Visibility.update(v.obj, remove=LOD_REASONS, cull_target=s.cull_target)
                s.lod_groups.remove(gi)
        s.lod_group_index = min(s.lod_group_index, max(0, len(s.lod_groups) - 1))
        invalidate_lod_cache()
        return {'FINISHED'}


class SCENEOPT_OT_lod_variant_add(Operator):
    bl_idname = "scene_optimizer.lod_variant_add"
    bl_label = "Add Selected as Variants"
    bl_description = "Append the selected objects to the active LOD group as the next levels"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        s = settings_of(context.scene)
        if not (0 <= s.lod_group_index < len(s.lod_groups)):
            return {'CANCELLED'}
        g = s.lod_groups[s.lod_group_index]
        used = _objects_in_groups(s)
        added = 0
        for o in order_variants([o for o in context.selected_objects if _variant_poll(None, o)
                                 and o.name_full not in used]):
            if len(g.variants) >= MAX_LOD_LEVELS:
                self.report({'WARNING'}, "Maximum of %d levels reached" % MAX_LOD_LEVELS)
                break
            g.variants.add().obj = o
            added += 1
        invalidate_lod_cache()
        self.report({'INFO'}, "Added %d variants" % added)
        return {'FINISHED'}


class SCENEOPT_OT_lod_variant_remove(Operator):
    bl_idname = "scene_optimizer.lod_variant_remove"
    bl_label = "Remove Variant"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        s = settings_of(context.scene)
        if not (0 <= s.lod_group_index < len(s.lod_groups)):
            return {'CANCELLED'}
        g = s.lod_groups[s.lod_group_index]
        i = g.variant_index
        if 0 <= i < len(g.variants):
            o = g.variants[i].obj
            if _obj_alive(o):
                Visibility.update(o, remove=LOD_REASONS, cull_target=s.cull_target)
            g.variants.remove(i)
            g.variant_index = min(i, max(0, len(g.variants) - 1))
        invalidate_lod_cache()
        return {'FINISHED'}


class SCENEOPT_OT_lod_variant_move(Operator):
    bl_idname = "scene_optimizer.lod_variant_move"
    bl_label = "Move Variant"
    bl_options = {'REGISTER', 'UNDO'}

    direction: EnumProperty(items=[('UP', "Up", ""), ('DOWN', "Down", "")])

    def execute(self, context):
        s = settings_of(context.scene)
        if not (0 <= s.lod_group_index < len(s.lod_groups)):
            return {'CANCELLED'}
        g = s.lod_groups[s.lod_group_index]
        i = g.variant_index
        j = i - 1 if self.direction == 'UP' else i + 1
        if 0 <= i < len(g.variants) and 0 <= j < len(g.variants):
            g.variants.move(i, j)
            g.variant_index = j
        return {'FINISHED'}


class SCENEOPT_OT_lod_snap_variants(Operator):
    bl_idname = "scene_optimizer.lod_snap_variants"
    bl_label = "Align Variants to LOD0"
    bl_description = ("Give every variant of the active group (or all groups) LOD0's world "
                      "transform. Only needed if variants were laid out side by side. Undoable")
    bl_options = {'REGISTER', 'UNDO'}

    all: BoolProperty(default=False, options={'SKIP_SAVE'})

    def execute(self, context):
        s = settings_of(context.scene)
        if self.all:
            groups = list(s.lod_groups)
        elif 0 <= s.lod_group_index < len(s.lod_groups):
            groups = [s.lod_groups[s.lod_group_index]]
        else:
            groups = []
        n = 0
        for g in groups:
            objs = lod_group_objects(context.scene, g)
            if len(objs) < 2:
                continue
            m = _computed_world_matrix(objs[0], context.view_layer)
            for o in objs[1:]:
                if object_editable(o)[0]:
                    o.matrix_world = m
                    n += 1
        self.report({'INFO'}, "Aligned %d variants" % n)
        return {'FINISHED'}


class SCENEOPT_OT_lod_distances_to_all(Operator):
    bl_idname = "scene_optimizer.lod_distances_to_all"
    bl_label = "Use Scene Defaults for All"
    bl_description = "Turn off custom distances on every LOD group"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        for g in settings_of(context.scene).lod_groups:
            g.use_custom_distances = False
        return {'FINISHED'}


class SCENEOPT_OT_select_group(Operator):
    bl_idname = "scene_optimizer.select_group"
    bl_label = "Select Group Objects"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        s = settings_of(context.scene)
        if not (0 <= s.lod_group_index < len(s.lod_groups)):
            return {'CANCELLED'}
        for o in context.selected_objects:
            o.select_set(False)
        for o in lod_group_objects(context.scene, s.lod_groups[s.lod_group_index]):
            if o.visible_get():
                o.select_set(True)
                context.view_layer.objects.active = o
        return {'FINISHED'}


# =============================================================================
# 13. UI
# =============================================================================

class SCENEOPT_UL_results(UIList):
    def draw_item(self, _context, layout, _data, item, _icon, _active, _propname, _index=0):
        icon = 'HIDE_ON' if item.reason == R_FRUSTUM else 'MOD_MASK'
        row = layout.row(align=True)
        if item.obj is None:
            row.label(text="(deleted)", icon='ERROR')
        else:
            row.label(text=item.obj.name, icon=icon)
            row.label(text="Frustum" if item.reason == R_FRUSTUM else "Occluded")

    def filter_items(self, _context, data, propname):
        items = getattr(data, propname)
        flt = [self.bitflag_filter_item] * len(items)
        if self.filter_name:
            pat = self.filter_name.lower()
            for i, it in enumerate(items):
                if it.obj is None or pat not in it.obj.name.lower():
                    flt[i] = 0
        return flt, []


class SCENEOPT_UL_lod_groups(UIList):
    def draw_item(self, context, layout, _data, item, _icon, _active, _propname, _index=0):
        levels = _Runtime.lod_levels.get(context.scene.name_full, {})
        lvl = levels.get(item.name)
        row = layout.row(align=True)
        row.prop(item, "name", text="", emboss=False, icon='MESH_DATA')
        n = len([v for v in item.variants if _obj_alive(v.obj)])
        if item.lock_level >= 0:
            row.label(text="locked %d" % item.lock_level, icon='LOCKED')
        elif lvl is None:
            row.label(text="%d lv" % n)
        else:
            row.label(text=("LOD%d" % lvl) if lvl >= 0 else "hidden", icon='CHECKMARK' if lvl >= 0 else 'HIDE_ON')


class SCENEOPT_UL_lod_variants(UIList):
    def draw_item(self, context, layout, data, item, _icon, _active, _propname, index=0):
        levels = _Runtime.lod_levels.get(context.scene.name_full, {})
        active = levels.get(data.name)
        row = layout.row(align=True)
        row.label(text="LOD%d" % index, icon='RADIOBUT_ON' if active == index else 'RADIOBUT_OFF')
        row.prop(item, "obj", text="")


class SCENEOPT_PT_base:
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Scene Optimizer"


def draw_lod_legend(layout, context, n):
    sub = layout.column(align=True)
    sub.scale_y = 0.8
    names = ["%d %s" % (i, LOD_COLOR_NAMES[i]) for i in range(n)]
    for i in range(0, n, 4):
        sub.label(text=("LOD " if i == 0 else "") + " · ".join(names[i:i + 4]))
    shading = getattr(getattr(context, "space_data", None), "shading", None)
    if shading is not None and shading.type == 'SOLID' and shading.color_type != 'MATERIAL':
        sub.label(text="Solid shading: set Color to Material", icon='ERROR')


class SCENEOPT_PT_main(SCENEOPT_PT_base, Panel):
    bl_label = "Scene Optimizer"
    bl_idname = "SCENEOPT_PT_main"

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        s = settings_of(scene)
        cam, err = get_active_camera(scene)
        box = layout.box()
        if cam is None:
            box.label(text=err, icon='ERROR')
        else:
            box.label(text="Camera: %s (%s)" % (cam.name, cam.data.type.title()), icon='CAMERA_DATA')
            if cam.data.type == 'PANO':
                box.label(text="Panoramic: culling unsupported", icon='ERROR')
        layout.prop(s, "cull_target")
        col = layout.column(align=True)
        row = col.row(align=True)
        row.scale_y = 1.4
        row.operator("scene_optimizer.preview", icon='HIDE_OFF')
        row.operator("scene_optimizer.apply", icon='CHECKMARK')
        row = col.row(align=True)
        row.operator("scene_optimizer.restore", text="Restore Original Scene", icon='LOOP_BACK').what = 'ALL'
        row = layout.row(align=True)
        row.prop(s, "show_overlay", toggle=True, icon='OVERLAY')
        row.operator("scene_optimizer.clear_overlay", text="", icon='X')
        layout.prop(s, "lod_colors", toggle=True, icon='COLOR')
        if s.lod_colors:
            draw_lod_legend(layout, context, len(LOD_COLOR_NAMES))
        # status
        n_cull, n_lod = hidden_counts()
        sb = layout.box()
        sb.label(text="Hidden now: %d by culling, %d by LOD" % (n_cull, n_lod), icon='INFO')
        if s.last_report:
            sb.label(text=s.last_report)
        sig = _Runtime.cull_cam_sig.get(scene.name_full)
        if n_cull and sig is not None and sig != _camera_signature(scene):
            wb = layout.box()
            wb.alert = True
            wb.label(text="Camera moved since Apply:", icon='ERROR')
            wb.label(text="culling is stale. Re-apply, or use")
            wb.label(text="Scope > Evaluate: Frame Range.")


class SCENEOPT_PT_scope(SCENEOPT_PT_base, Panel):
    bl_label = "Scope"
    bl_parent_id = "SCENEOPT_PT_main"
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        s = settings_of(context.scene)
        layout = self.layout
        layout.use_property_split = True
        layout.prop(s, "exclude_collection")
        layout.prop(s, "frame_mode")
        if s.frame_mode == 'RANGE':
            layout.prop(s, "frame_step")
            layout.prop(s, "static_geometry")
        ob = context.object
        if ob is not None:
            b = layout.box()
            b.label(text="Active: " + ob.name, icon='OBJECT_DATA')
            b.prop(ob, "so_never_cull")
            b.prop(ob, "so_not_occluder")
            rs = Visibility.reasons(ob)
            if rs:
                b.label(text="Hidden by: " + ", ".join(sorted(rs)), icon='HIDE_ON')
            if not object_editable(ob)[0]:
                b.label(text="Linked object: will not be culled", icon='LIBRARY_DATA_DIRECT')


class SCENEOPT_PT_frustum(SCENEOPT_PT_base, Panel):
    bl_label = "Frustum Culling"
    bl_parent_id = "SCENEOPT_PT_main"

    def draw_header(self, context):
        self.layout.prop(settings_of(context.scene), "frustum_enabled", text="")

    def draw(self, context):
        s = settings_of(context.scene)
        layout = self.layout
        layout.use_property_split = True
        layout.active = s.frustum_enabled
        layout.prop(s, "frustum_margin")
        layout.prop(s, "frustum_padding")
        layout.prop(s, "frustum_use_clip")


class SCENEOPT_PT_occlusion(SCENEOPT_PT_base, Panel):
    bl_label = "Occlusion Culling"
    bl_parent_id = "SCENEOPT_PT_main"
    bl_options = {'DEFAULT_CLOSED'}

    def draw_header(self, context):
        self.layout.prop(settings_of(context.scene), "occlusion_enabled", text="")

    def draw(self, context):
        s = settings_of(context.scene)
        layout = self.layout
        layout.use_property_split = True
        layout.active = s.occlusion_enabled
        layout.prop(s, "occlusion_preset")
        col = layout.column(align=True)
        col.prop(s, "occlusion_sample_spacing")
        col.prop(s, "occlusion_min_samples")
        col.prop(s, "occlusion_max_rays")
        layout.prop(s, "occluder_collection")
        layout.prop(s, "occlusion_ignore_transparent")
        layout.prop(s, "occlusion_instance_occluders")
        col = layout.column(align=True)
        col.prop(s, "occlusion_max_tris")
        col.prop(s, "occlusion_max_passthrough")
        b = layout.box()
        b.scale_y = 0.7
        b.label(text="Ray-sampled at render resolution:", icon='INFO')
        b.label(text="gaps narrower than the ray spacing")
        b.label(text="can be missed. Use Accurate for final.")


class SCENEOPT_PT_lod(SCENEOPT_PT_base, Panel):
    bl_label = "LOD Switching"
    bl_parent_id = "SCENEOPT_PT_main"
    bl_options = {'DEFAULT_CLOSED'}

    def draw_header(self, context):
        self.layout.prop(settings_of(context.scene), "lod_enabled", text="")

    def draw(self, context):
        s = settings_of(context.scene)
        layout = self.layout
        layout.active = s.lod_enabled
        row = layout.row(align=True)
        row.prop(s, "lod_live", toggle=True, icon='REC' if s.lod_live else 'PLAY')
        row.operator("scene_optimizer.lod_apply", text="Update", icon='FILE_REFRESH').preview = False
        row.operator("scene_optimizer.lod_apply", text="Preview", icon='HIDE_OFF').preview = True
        col = layout.column()
        col.use_property_split = True
        if s.lod_live:
            col.prop(s, "lod_on_frame_change")
            col.prop(s, "lod_hysteresis")
        col.prop(s, "lod_distance_mode")
        col.prop(s, "lod_fov_compensation")
        col.prop(s, "lod_max_labels")
        col.prop(s, "lod_hide_beyond")
        if s.lod_hide_beyond:
            col.prop(s, "lod_max_distance")
        dcol = layout.column(align=True)
        dcol.label(text="Default switch distances:")
        nmax = max([len(g.variants) for g in s.lod_groups] + [3])
        for i in range(min(nmax, MAX_LOD_LEVELS) - 1):
            dcol.prop(s, "lod_distances", index=i, text="LOD%d from" % (i + 1))
        _distance_warning(dcol, s.lod_distances, nmax)

        layout.separator()
        layout.label(text="LOD Groups (%d)" % len(s.lod_groups))
        row = layout.row()
        row.template_list("SCENEOPT_UL_lod_groups", "", s, "lod_groups", s, "lod_group_index", rows=4)
        col = row.column(align=True)
        col.operator("scene_optimizer.lod_group_from_selected", text="", icon='ADD')
        col.operator("scene_optimizer.lod_group_remove", text="", icon='REMOVE').all = False
        col.separator()
        col.operator("scene_optimizer.select_group", text="", icon='RESTRICT_SELECT_OFF')
        col.operator("scene_optimizer.lod_group_remove", text="", icon='TRASH').all = True
        b = layout.box()
        b.label(text="Batch setup:")
        b.prop(s, "lod_detect_collection")
        r = b.row(align=True)
        r.operator("scene_optimizer.lod_autodetect", icon='VIEWZOOM')
        r.operator("scene_optimizer.lod_distances_to_all", text="Defaults for All")
        b.operator("scene_optimizer.lod_snap_variants", text="Align All Variants to LOD0").all = True

        if 0 <= s.lod_group_index < len(s.lod_groups):
            g = s.lod_groups[s.lod_group_index]
            gb = layout.box()
            gb.label(text=g.name, icon='GROUP')
            row = gb.row()
            row.template_list("SCENEOPT_UL_lod_variants", "", g, "variants", g, "variant_index", rows=3)
            c = row.column(align=True)
            c.operator("scene_optimizer.lod_variant_add", text="", icon='ADD')
            c.operator("scene_optimizer.lod_variant_remove", text="", icon='REMOVE')
            c.separator()
            c.operator("scene_optimizer.lod_variant_move", text="", icon='TRIA_UP').direction = 'UP'
            c.operator("scene_optimizer.lod_variant_move", text="", icon='TRIA_DOWN').direction = 'DOWN'
            gb.prop(g, "lock_level")
            gb.prop(g, "use_custom_distances")
            if g.use_custom_distances:
                dc = gb.column(align=True)
                for i in range(max(0, len(g.variants) - 1)):
                    dc.prop(g, "distances", index=i, text="LOD%d from" % (i + 1))
                _distance_warning(dc, g.distances, len(g.variants))
                gb.prop(g, "use_max_distance")
                if g.use_max_distance:
                    gb.prop(g, "max_distance")
            gb.operator("scene_optimizer.lod_snap_variants", text="Align Variants to LOD0").all = False
            missing = len(g.variants) - len(lod_group_objects(context.scene, g))
            if missing:
                gb.label(text="%d variant(s) missing or not in scene" % missing, icon='ERROR')


def _distance_warning(layout, dists, n):
    vals = [dists[i] for i in range(max(0, min(n, MAX_LOD_LEVELS) - 1))]
    if any(b <= a for a, b in zip(vals, vals[1:])):
        layout.label(text="Distances should increase", icon='ERROR')


class SCENEOPT_PT_results(SCENEOPT_PT_base, Panel):
    bl_label = "Culled Objects"
    bl_parent_id = "SCENEOPT_PT_main"
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        s = settings_of(context.scene)
        layout = self.layout
        nf = sum(1 for r in s.results if r.reason == R_FRUSTUM)
        layout.label(text="%d frustum   %d occluded" % (nf, len(s.results) - nf))
        layout.template_list("SCENEOPT_UL_results", "", s, "results", s, "results_index", rows=6)
        row = layout.row(align=True)
        row.operator("scene_optimizer.select_results", text="Select All").reason = 'ALL'
        row.operator("scene_optimizer.select_results", text="Frustum").reason = R_FRUSTUM
        row.operator("scene_optimizer.select_results", text="Occluded").reason = R_OCCLUSION
        # Plain text legend: colour icon names differ between Blender versions.
        col = layout.column(align=True)
        col.scale_y = 0.8
        col.label(text="Overlay: red = frustum, orange = occluded,", icon='OVERLAY')
        col.label(text="cyan = camera frustum incl. margin")


# =============================================================================
# 14. Scatter systems (Geometry Nodes, per-instance LOD and culling)
# =============================================================================
#
# Object-level culling cannot help a particle/GN scatter: all instances belong
# to ONE emitter object. A scatter system therefore does the work per
# instance, inside Geometry Nodes, every time the depsgraph evaluates:
#
#   surfaces --Distribute--> points --(look: masks, patches, slope, avoid, edges)
#            --(camera: frustum, occlusion, distance fade, thinning)--> survivors
#            --Instance--> asset LODs, with per-instance color attributes
#
#   * look       vertex-group mask, noise patches (clumping), soft slope limit,
#                keep-away from objects/curves (paths), soft fade at mesh edges,
#                random or even (Poisson) spacing, scale/rotation/tilt/lean
#   * distance LOD: each instance picks LOD0..LOD3 of its asset by its
#     distance to the scene camera
#   * distance thinning: beyond "Thin Start" a growing share of instances is
#     dropped (optionally scaling the rest up) - density LOD, as engines do
#   * frustum culling: instances outside the camera frame (+ margin) are removed
#   * occlusion culling: a ray from the top of each instance towards the camera
#     is cast against the surface (+ optional occluder collection); if it hits,
#     the instance is behind a hill and removed
#   * color: every instance carries so_random / so_dry / so_patch attributes;
#     the "SO Instance Variation" shader group turns them into hue and dryness
#
# Python builds the node tree, keeps a few camera inputs (frame tangents, which
# GN cannot read) in sync, and manages a preset library on disk (settings as
# JSON + the asset objects in a .blend + a rendered thumbnail). Each system is
# a holder object with one GN modifier; any number of systems can exist.

SCATTER_MAX_LODS = 4
SCATTER_COLLECTION = "SO Scatter Systems"
SCATTER_ASSET_COLLECTION = "SO Scatter Assets"
SCATTER_MOD = "SO Scatter"
VARIATION_GROUP = "SO Instance Variation"
PRESET_FORMAT = "scene_optimizer.scatter_preset"
PRESET_VERSION = 1

# (name, socket type, default, min, max, description)
_SCATTER_INPUTS = (
    ("Surface", 'NodeSocketObject', None, None, None, "Mesh to scatter on"),
    ("Extra Surfaces", 'NodeSocketCollection', None, None, None, "More meshes to scatter on"),
    ("Camera", 'NodeSocketObject', None, None, None, "Set automatically to the scene camera"),
    ("Has Camera", 'NodeSocketBool', False, None, None, "Set automatically"),
    ("Occluders", 'NodeSocketCollection', None, None, None, "Extra geometry that hides instances"),
    # distribution
    ("Density", 'NodeSocketFloat', 10.0, 0.0, 100000.0, "Instances per square meter"),
    ("Viewport Density", 'NodeSocketFloat', 1.0, 0.0, 1.0, "Share of instances shown in the viewport"),
    ("Seed", 'NodeSocketInt', 0, 0, 100000, "Random seed"),
    ("Even Spacing", 'NodeSocketBool', False, None, None, "Poisson disk: keep Min Distance apart"),
    ("Min Distance", 'NodeSocketFloat', 0.1, 0.0, 1e6, "Smallest gap between instances"),
    ("Density Mask", 'NodeSocketString', "", None, None, "Vertex group / attribute scaling density"),
    ("Max Slope", 'NodeSocketFloat', math.radians(60.0), 0.0, math.pi, "No instances on steeper ground"),
    ("Slope Blur", 'NodeSocketFloat', math.radians(10.0), 0.0, math.pi, "Soft band around Max Slope"),
    ("Patch Size", 'NodeSocketFloat', 8.0, 0.01, 1e6, "Size of the noise patches (m)"),
    ("Patch Amount", 'NodeSocketFloat', 0.0, 0.0, 1.0, "How strongly patches thin out density"),
    ("Patch Scale", 'NodeSocketFloat', 0.0, 0.0, 2.0, "How strongly patches change instance size"),
    ("Avoid", 'NodeSocketCollection', None, None, None, "Objects / curves to keep away from"),
    ("Avoid Distance", 'NodeSocketFloat', 0.5, 0.0, 1e6, "Empty radius around avoid geometry"),
    ("Avoid Blur", 'NodeSocketFloat', 0.5, 0.0, 1e6, "Soft band beyond Avoid Distance"),
    ("Edge Offset", 'NodeSocketFloat', 0.0, 0.0, 1e6, "Fade density out towards open mesh borders"),
    # scale / rotation
    ("Scale Min", 'NodeSocketFloat', 0.8, 0.0, 1000.0, ""),
    ("Scale Max", 'NodeSocketFloat', 1.2, 0.0, 1000.0, ""),
    ("Random Rotation", 'NodeSocketFloat', 1.0, 0.0, 1.0, "Share of a full random turn around Z"),
    ("Align to Normal", 'NodeSocketFloat', 0.0, 0.0, 1.0, "0 = upright, 1 = follow the ground"),
    ("Random Tilt", 'NodeSocketFloat', 0.0, 0.0, math.pi, "Random lean in any direction"),
    ("Lean Angle", 'NodeSocketFloat', 0.0, 0.0, math.pi, "Global lean, e.g. wind-swept grass"),
    ("Lean Direction", 'NodeSocketFloat', 0.0, -1e3, 1e3, "Heading the lean points to"),
    # color
    ("Dryness", 'NodeSocketFloat', 0.0, 0.0, 1.0, "Share of dry instances (so_dry attribute)"),
    ("Dry Patchiness", 'NodeSocketFloat', 0.5, 0.0, 1.0, "0 = dry ones random, 1 = dry in patches"),
    # LOD / distance
    ("LOD1 Distance", 'NodeSocketFloat', 10.0, 0.0, 1e7, ""),
    ("LOD2 Distance", 'NodeSocketFloat', 30.0, 0.0, 1e7, ""),
    ("LOD3 Distance", 'NodeSocketFloat', 60.0, 0.0, 1e7, ""),
    ("Max Distance", 'NodeSocketFloat', 150.0, 0.0, 1e7, ""),
    ("Viewport Distance", 'NodeSocketFloat', 0.0, 0.0, 1e7, "Shorter draw distance in the viewport (0 = same)"),
    ("Fade", 'NodeSocketFloat', 0.0, 0.0, 1e7, "Soft band before the max distance"),
    ("Thin Start", 'NodeSocketFloat', 40.0, 0.0, 1e7, ""),
    ("Thin Keep", 'NodeSocketFloat', 0.3, 0.0, 1.0, ""),
    ("Thin Compensate", 'NodeSocketBool', True, None, None, ""),
    ("Show LOD0", 'NodeSocketBool', True, None, None, "Draw instances at LOD0"),
    ("Show LOD1", 'NodeSocketBool', True, None, None, "Draw instances at LOD1"),
    ("Show LOD2", 'NodeSocketBool', True, None, None, "Draw instances at LOD2"),
    ("Show LOD3", 'NodeSocketBool', True, None, None, "Draw instances at LOD3"),
    ("LOD Colors", 'NodeSocketBool', False, None, None, "Viewport only: colour instances by LOD"),
    # culling
    ("Frustum", 'NodeSocketBool', True, None, None, ""),
    ("Tan Left", 'NodeSocketFloat', -1.0, -1e6, 1e6, "Set automatically"),
    ("Tan Right", 'NodeSocketFloat', 1.0, -1e6, 1e6, "Set automatically"),
    ("Tan Bottom", 'NodeSocketFloat', -1.0, -1e6, 1e6, "Set automatically"),
    ("Tan Top", 'NodeSocketFloat', 1.0, -1e6, 1e6, "Set automatically"),
    ("Pad", 'NodeSocketFloat', 1.0, 0.0, 1e6, "Set automatically (largest asset radius at scale 1)"),
    ("Occlusion", 'NodeSocketBool', True, None, None, ""),
    ("Lift", 'NodeSocketFloat', 0.5, 0.0, 1e6, "Set automatically (largest asset height at scale 1)"),
    ("Cull Viewport", 'NodeSocketBool', True, None, None, ""),
    ("Cull Render", 'NodeSocketBool', True, None, None, ""),
)


class _NodeBuilder:
    """Tiny helper: create nodes and wire sockets or constants by name/index
    (looking only at sockets that are enabled for the node's current mode)."""

    def __init__(self, ng):
        self.ng = ng
        self.col = 0
        self.row = 0
        self.seed = 0

    def node(self, idname, **props):
        n = self.ng.nodes.new(idname)
        for k, v in props.items():
            setattr(n, k, v)
        n.location = (self.col * 220, -self.row * 160)
        self.row += 1
        if self.row > 14:
            self.row, self.col = 0, self.col + 1
        return n

    @staticmethod
    def _pick(sockets, key):
        live = [s for s in sockets if getattr(s, "enabled", True)]
        if isinstance(key, int):
            return live[key]
        for s in live:
            if s.name == key or getattr(s, "identifier", None) == key:
                return s
        raise KeyError("socket %r not in %s" % (key, [s.name for s in live]))

    def out(self, node, key=0):
        return self._pick(node.outputs, key)

    def put(self, node, key, value):
        sock = self._pick(node.inputs, key)
        if isinstance(value, bpy.types.NodeSocket):
            self.ng.links.new(value, sock)
        elif value is not None:
            sock.default_value = value
        return node

    # -- shorthands returning output sockets
    def math(self, op, a, b=None, clamp=False):
        n = self.node("ShaderNodeMath", operation=op, use_clamp=clamp)
        self.put(n, 0, a)
        if b is not None:
            self.put(n, 1, b)
        return self.out(n)

    def vmath(self, op, a, b=None, out="Vector"):
        n = self.node("ShaderNodeVectorMath", operation=op)
        self.put(n, 0, a)
        if b is not None:
            self.put(n, 1, b)
        return self.out(n, out)

    def cmp(self, op, a, b):
        n = self.node("FunctionNodeCompare", data_type='FLOAT', operation=op)
        self.put(n, 0, a)
        self.put(n, 1, b)
        return self.out(n)

    def boolean(self, op, a, b=None):
        n = self.node("FunctionNodeBooleanMath", operation=op)
        self.put(n, 0, a)
        if b is not None:
            self.put(n, 1, b)
        return self.out(n)

    def all_of(self, *socks):
        res = socks[0]
        for s in socks[1:]:
            res = self.boolean('AND', res, s)
        return res

    def switch(self, kind, cond, if_false, if_true):
        n = self.node("GeometryNodeSwitch", input_type=kind)
        self.put(n, "Switch", cond)
        self.put(n, "False", if_false)
        self.put(n, "True", if_true)
        return self.out(n)

    def rand(self, lo, hi, seed, offset):
        n = self.node("FunctionNodeRandomValue", data_type='FLOAT')
        self.put(n, "Min", lo)
        self.put(n, "Max", hi)
        self.put(n, "Seed", self.math('ADD', seed, float(offset)))
        return self.out(n)

    def clamp01(self, x):
        return self.math('MULTIPLY', x, 1.0, clamp=True)

    def lerp(self, a, b, t):
        return self.math('ADD', a, self.math('MULTIPLY', self.math('SUBTRACT', b, a), t))

    def chance(self, p, offset):
        """True for a share p of the points (stable per point and offset)."""
        return self.cmp('LESS_THAN', self.rand(0.0, 1.0, self.seed, offset), p)

    def xyz(self, x=0.0, y=0.0, z=0.0):
        n = self.node("ShaderNodeCombineXYZ")
        self.put(n, "X", x)
        self.put(n, "Y", y)
        self.put(n, "Z", z)
        return self.out(n)

    def axis_angle(self, axis, angle):
        n = self.node("FunctionNodeAxisAngleToRotation")
        self.put(n, "Axis", axis)
        self.put(n, "Angle", angle)
        return self.out(n)

    def rotate(self, rot, by, space):
        n = self.node("FunctionNodeRotateRotation", rotation_space=space)
        self.put(n, "Rotation", rot)
        self.put(n, "Rotate By", by)
        return self.out(n)

    def join(self, *geos):
        n = self.node("GeometryNodeJoinGeometry")
        for g in geos:
            self.ng.links.new(g, self._pick(n.inputs, "Geometry"))
        return self.out(n)

    def realized_collection(self, coll_socket):
        info = self.node("GeometryNodeCollectionInfo", transform_space='RELATIVE')
        self.put(info, "Collection", coll_socket)
        real = self.node("GeometryNodeRealizeInstances")
        self.put(real, "Geometry", self.out(info))
        return self.out(real)

    def proximity(self, geo, pos, element):
        """Distance to geo, or 1e9 where geo has nothing of that element."""
        n = self.node("GeometryNodeProximity", target_element=element)
        self.put(n, "Geometry", geo)
        self.put(n, "Sample Position", pos)
        return self.switch('FLOAT', self.out(n, "Is Valid"), 1e9, self.out(n, "Distance"))


LOD_PREVIEW_MOD = "SO LOD Colors"


def lod_debug_material(level):
    """Flat material 'SO LOD<n>' used by the LOD colour preview (solid and
    material/rendered viewport shading)."""
    name = "SO LOD%d" % level
    mat = bpy.data.materials.get(name)
    if mat is None:
        mat = bpy.data.materials.new(name)
        mat.use_nodes = True
        bsdf = next((n for n in mat.node_tree.nodes if n.type == 'BSDF_PRINCIPLED'), None)
        if bsdf is not None:
            bsdf.inputs["Base Color"].default_value = LOD_COLORS[level]
            bsdf.inputs["Roughness"].default_value = 0.8
    mat.diffuse_color = LOD_COLORS[level]
    return mat


def _lod_color_group(level):
    """Node group 'SO LOD Color <n>': Set Material to the LOD colour."""
    name = "SO LOD Color %d" % level
    ng = bpy.data.node_groups.get(name)
    if ng is not None and ng.bl_idname == 'GeometryNodeTree':
        return ng
    ng = bpy.data.node_groups.new(name, 'GeometryNodeTree')
    ng.interface.new_socket(name="Geometry", in_out='INPUT', socket_type='NodeSocketGeometry')
    ng.interface.new_socket(name="Geometry", in_out='OUTPUT', socket_type='NodeSocketGeometry')
    gi = ng.nodes.new("NodeGroupInput")
    go = ng.nodes.new("NodeGroupOutput")
    sm = ng.nodes.new("GeometryNodeSetMaterial")
    gi.location, sm.location, go.location = (-250, 0), (0, 0), (250, 0)
    sm.inputs["Material"].default_value = lod_debug_material(level)
    ng.links.new(gi.outputs[0], sm.inputs["Geometry"])
    ng.links.new(sm.outputs[0], go.inputs[0])
    return ng


def object_lod_colors(scene, on, prune=True):
    """Colour the variants of every object LOD group by level (LOD0 green ...),
    with a viewport-only Geometry Nodes modifier: renders and mesh data are
    untouched, and switching off removes the modifiers again.
    prune=False only adds/updates (cheap enough for live LOD updates)."""
    s = settings_of(scene)
    wanted = {}
    if on:
        ptrs = scene_object_pointers(scene)
        for g in s.lod_groups:
            for level, ob in enumerate(lod_group_objects(scene, g, ptrs)):
                if ob.type == 'MESH' and ob.library is None and level < len(LOD_COLORS):
                    wanted[ob] = level
    changed = 0
    for ob, level in wanted.items():
        mod = ob.modifiers.get(LOD_PREVIEW_MOD)
        if mod is None:
            mod = ob.modifiers.new(LOD_PREVIEW_MOD, 'NODES')
            mod.show_render = False
            mod.show_in_editmode = False
            changed += 1
        ng = _lod_color_group(level)
        if mod.node_group != ng:
            mod.node_group = ng
            changed += 1
    if prune:
        for ob in scene.objects:
            if ob not in wanted and ob.library is None:
                mod = ob.modifiers.get(LOD_PREVIEW_MOD)
                if mod is not None:
                    ob.modifiers.remove(mod)
                    changed += 1
    return changed


def _lod_colors_changed(self, context):
    scene = self.id_data
    try:
        object_lod_colors(scene, self.lod_colors)
    except Exception as exc:
        _log("LOD colours failed: %s" % exc)
    for system in self.scatter_systems:
        if system.lod_colors != self.lod_colors:
            system.lod_colors = self.lod_colors     # syncs the system


def build_scatter_tree(ng, assets):
    """(Re)build the node tree. assets: list of (weight, [lod objects])."""
    ng.nodes.clear()
    iface = ng.interface
    for item in list(iface.items_tree):
        iface.remove(item)
    for name, stype, default, lo, hi, desc in _SCATTER_INPUTS:
        sock = iface.new_socket(name=name, in_out='INPUT', socket_type=stype)
        sock.description = desc
        if default is not None:
            sock.default_value = default
        if lo is not None:
            sock.min_value, sock.max_value = lo, hi
    usable = [(w, objs) for w, objs in assets if objs]
    for k in range(len(usable)):
        sock = iface.new_socket(name="Show Asset %d" % (k + 1), in_out='INPUT',
                                socket_type='NodeSocketBool')
        sock.default_value = True
        sock.description = "Draw this asset"
    iface.new_socket(name="Geometry", in_out='OUTPUT', socket_type='NodeSocketGeometry')

    b = _NodeBuilder(ng)
    gi = b.node("NodeGroupInput")
    go = b.node("NodeGroupOutput")
    go.location = (2600, 0)
    G = gi.outputs.get
    seed = G("Seed")
    b.seed = seed

    # ---- surfaces (holder sits at the origin, so RELATIVE == world space)
    surf = b.node("GeometryNodeObjectInfo", transform_space='RELATIVE')
    b.put(surf, "Object", G("Surface"))
    surf_geo = b.join(b.out(surf, "Geometry"), b.realized_collection(G("Extra Surfaces")))

    # ---- face pre-filter: slope (incl. its soft band) and density mask
    half_blur = b.math('MULTIPLY', G("Slope Blur"), 0.5)
    cos_lo = b.math('COSINE', b.math('MINIMUM', b.math('ADD', G("Max Slope"), half_blur), math.pi))
    cos_hi = b.math('COSINE', b.math('MAXIMUM', b.math('SUBTRACT', G("Max Slope"), half_blur), 0.0))
    nz = b.node("ShaderNodeSeparateXYZ")
    b.put(nz, 0, b.out(b.node("GeometryNodeInputNormal"), "Normal"))
    slope_face = b.cmp('GREATER_EQUAL', b.out(nz, "Z"), b.math('SUBTRACT', cos_lo, 1e-4))
    face_mask = b.node("GeometryNodeInputNamedAttribute", data_type='FLOAT')
    b.put(face_mask, "Name", G("Density Mask"))
    mask_face = b.boolean('OR', b.boolean('NOT', b.out(face_mask, "Exists")),
                          b.cmp('GREATER_THAN', b.out(face_mask, "Attribute"), 0.001))
    sel = b.boolean('AND', slope_face, mask_face)

    # ---- distribution: random or even (Poisson disk)
    d_rand = b.node("GeometryNodeDistributePointsOnFaces", distribute_method='RANDOM')
    b.put(d_rand, "Mesh", surf_geo)
    b.put(d_rand, "Selection", sel)
    b.put(d_rand, "Density", G("Density"))
    b.put(d_rand, "Seed", seed)
    d_even = b.node("GeometryNodeDistributePointsOnFaces", distribute_method='POISSON')
    b.put(d_even, "Mesh", surf_geo)
    b.put(d_even, "Selection", sel)
    b.put(d_even, "Distance Min", G("Min Distance"))
    b.put(d_even, "Density Max", G("Density"))
    b.put(d_even, "Seed", seed)
    even = G("Even Spacing")
    points = b.switch('GEOMETRY', even, b.out(d_rand, "Points"), b.out(d_even, "Points"))
    normal = b.switch('VECTOR', even, b.out(d_rand, "Normal"), b.out(d_even, "Normal"))

    pos = b.out(b.node("GeometryNodeInputPosition"))

    # ---- look: patches, mask, soft slope, avoid, edge offset
    noise = b.node("ShaderNodeTexNoise")
    b.put(noise, "Vector", pos)
    b.put(noise, "Scale", b.math('DIVIDE', 1.0, b.math('MAXIMUM', G("Patch Size"), 0.01)))
    b.put(noise, "Detail", 2.0)
    b.put(noise, "Roughness", 0.5)
    patch = b.clamp01(b.math('ADD', b.math('MULTIPLY', b.math(
        'SUBTRACT', b.out(noise, "Fac"), 0.5), 3.5), 0.5))
    keep_patch = b.chance(b.lerp(1.0, patch, G("Patch Amount")), 37)

    mask = b.node("GeometryNodeInputNamedAttribute", data_type='FLOAT')
    b.put(mask, "Name", G("Density Mask"))
    keep_mask = b.boolean('OR', b.boolean('NOT', b.out(mask, "Exists")),
                          b.chance(b.out(mask, "Attribute"), 13))

    nzp = b.node("ShaderNodeSeparateXYZ")
    b.put(nzp, 0, normal)
    slope_p = b.clamp01(b.math('DIVIDE', b.math('SUBTRACT', b.out(nzp, "Z"), cos_lo),
                               b.math('MAXIMUM', b.math('SUBTRACT', cos_hi, cos_lo), 1e-4)))
    keep_slope = b.chance(slope_p, 41)

    avoid_geo = b.realized_collection(G("Avoid"))
    c2m = b.node("GeometryNodeCurveToMesh")
    b.put(c2m, "Curve", avoid_geo)
    d_avoid = b.math('MINIMUM', b.proximity(avoid_geo, pos, 'FACES'),
                     b.proximity(b.out(c2m), pos, 'EDGES'))
    # points INSIDE an avoid mesh (under a big rock) are far from its faces:
    # an upward ray that hits a face from behind means "inside"
    up_ray = b.node("GeometryNodeRaycast")
    b.put(up_ray, "Target Geometry", avoid_geo)
    b.put(up_ray, "Source Position", pos)
    b.put(up_ray, "Ray Direction", (0.0, 0.0, 1.0))
    b.put(up_ray, "Ray Length", 1e4)
    hn = b.node("ShaderNodeSeparateXYZ")
    b.put(hn, 0, b.out(up_ray, "Hit Normal"))
    inside_avoid = b.boolean('AND', b.out(up_ray, "Is Hit"), b.cmp('GREATER_THAN', b.out(hn, "Z"), 0.0))
    d_avoid = b.switch('FLOAT', inside_avoid, d_avoid, 0.0)
    keep_avoid = b.chance(b.clamp01(b.math('DIVIDE', b.math('SUBTRACT', d_avoid, G("Avoid Distance")),
                                           b.math('MAXIMUM', G("Avoid Blur"), 0.001))), 43)

    # open borders: edges with exactly one face
    fc = b.node("GeometryNodeInputMeshEdgeNeighbors")
    border = b.node("GeometryNodeDeleteGeometry", domain='EDGE', mode='ALL')
    b.put(border, "Geometry", surf_geo)
    b.put(border, "Selection", b.cmp('NOT_EQUAL', b.out(fc, "Face Count"), 1.0))
    d_edge = b.proximity(b.out(border), pos, 'EDGES')
    keep_edge = b.boolean('OR', b.cmp('LESS_EQUAL', G("Edge Offset"), 0.0), b.chance(b.clamp01(
        b.math('DIVIDE', d_edge, b.math('MAXIMUM', G("Edge Offset"), 0.001))), 47))

    # ---- camera relative
    cam = b.node("GeometryNodeObjectInfo", transform_space='RELATIVE')
    b.put(cam, "Object", G("Camera"))
    cam_loc = b.out(cam, "Location")
    # no camera: everything counts as near (LOD0, no thinning)
    d = b.switch('FLOAT', G("Has Camera"), 0.0, b.vmath('DISTANCE', pos, cam_loc, out="Value"))
    is_vp = b.out(b.node("GeometryNodeIsViewport"))
    vp_max = b.switch('FLOAT', b.cmp('GREATER_THAN', G("Viewport Distance"), 0.0), G("Max Distance"),
                      b.math('MINIMUM', G("Max Distance"), G("Viewport Distance")))
    max_d = b.switch('FLOAT', is_vp, G("Max Distance"), vp_max)

    # distance thinning share (1 near, Thin Keep at the max distance) and the
    # per-instance scale; both are needed by the culling tests below
    span = b.math('MAXIMUM', b.math('SUBTRACT', max_d, G("Thin Start")), 0.001)
    t = b.math('DIVIDE', b.math('SUBTRACT', d, G("Thin Start")), span, clamp=True)
    keep_share = b.math('SUBTRACT', 1.0, b.math('MULTIPLY', b.math('SUBTRACT', 1.0, G("Thin Keep")), t))
    comp = b.switch('FLOAT', G("Thin Compensate"), 1.0,
                    b.math('DIVIDE', 1.0, b.math('SQRT', b.math('MAXIMUM', keep_share, 0.05))))
    patch_size = b.math('ADD', 1.0, b.math('MULTIPLY', G("Patch Scale"), b.math('SUBTRACT', patch, 0.5)))
    scale = b.math('MULTIPLY', b.math('MULTIPLY', b.rand(G("Scale Min"), G("Scale Max"), seed, 19), comp),
                   b.math('MAXIMUM', patch_size, 0.05))
    cull_on = b.boolean('AND', b.switch('BOOLEAN', is_vp, G("Cull Render"), G("Cull Viewport")),
                        G("Has Camera"))

    # frustum (camera space: looking down -Z)
    inv = b.node("FunctionNodeInvertMatrix")
    b.put(inv, "Matrix", b.out(cam, "Transform"))
    tp = b.node("FunctionNodeTransformPoint")
    b.put(tp, "Vector", pos)
    b.put(tp, "Transform", b.out(inv, "Matrix"))
    pc = b.node("ShaderNodeSeparateXYZ")
    b.put(pc, 0, b.out(tp))
    depth = b.math('MULTIPLY', b.out(pc, "Z"), -1.0)
    pad = b.math('MULTIPLY', G("Pad"), scale)       # this instance's radius

    def side(coord, tan, sign):
        # sign=+1: coord <= depth*tan + pad*(1+|tan|) ; sign=-1: coord >= ...
        slack = b.math('MULTIPLY', pad, b.math('ADD', b.math('ABSOLUTE', tan), 1.0))
        edge = b.math('MULTIPLY', depth, tan)
        if sign > 0:
            return b.cmp('LESS_EQUAL', coord, b.math('ADD', edge, slack))
        return b.cmp('GREATER_EQUAL', coord, b.math('SUBTRACT', edge, slack))

    inside = b.all_of(
        b.cmp('GREATER_THAN', depth, b.math('MULTIPLY', pad, -1.0)),
        side(b.out(pc, "X"), G("Tan Left"), -1), side(b.out(pc, "X"), G("Tan Right"), +1),
        side(b.out(pc, "Y"), G("Tan Bottom"), -1), side(b.out(pc, "Y"), G("Tan Top"), +1))
    frustum_ok = b.boolean('OR', b.boolean('NOT', G("Frustum")), inside)

    # occlusion: ray from the instance top towards the camera
    src = b.vmath('ADD', pos, b.xyz(z=b.math('MULTIPLY', G("Lift"), scale)))
    to_cam = b.vmath('SUBTRACT', cam_loc, src)
    target = b.join(b.realized_collection(G("Occluders")), surf_geo)
    ray = b.node("GeometryNodeRaycast")
    b.put(ray, "Target Geometry", target)
    b.put(ray, "Source Position", src)
    b.put(ray, "Ray Direction", b.vmath('NORMALIZE', to_cam))
    b.put(ray, "Ray Length", b.math('MAXIMUM', b.math(
        'SUBTRACT', b.vmath('LENGTH', to_cam, out="Value"), 0.05), 0.0))
    occl_ok = b.boolean('OR', b.boolean('NOT', G("Occlusion")),
                        b.boolean('NOT', b.out(ray, "Is Hit")))

    # max distance with an optional soft fade band
    dist_ok = b.cmp('GREATER_EQUAL', b.math('SUBTRACT', max_d, d),
                    b.math('MULTIPLY', b.rand(0.0, 1.0, seed, 53), G("Fade")))
    cull_keep = b.boolean('OR', b.boolean('NOT', cull_on), b.all_of(frustum_ok, occl_ok, dist_ok))
    keep_thin = b.chance(keep_share, 11)
    keep_vp = b.boolean('OR', b.boolean('NOT', is_vp), b.chance(G("Viewport Density"), 17))

    keep = b.all_of(keep_mask, keep_patch, keep_slope, keep_avoid, keep_edge,
                    cull_keep, keep_thin, keep_vp)
    delete = b.node("GeometryNodeDeleteGeometry", domain='POINT')
    b.put(delete, "Geometry", points)
    b.put(delete, "Selection", b.boolean('NOT', keep))
    pts = b.out(delete)

    # ---- per-instance color attributes (read in materials: Attribute node, Instancer)
    dry_base = b.lerp(b.rand(0.0, 1.0, seed, 73), patch, G("Dry Patchiness"))
    so_dry = b.clamp01(b.math('MULTIPLY', b.math('SUBTRACT', b.math('ADD', dry_base, G("Dryness")), 1.0), 4.0))
    for name, value in (("so_random", b.rand(0.0, 1.0, seed, 71)), ("so_dry", so_dry), ("so_patch", patch)):
        st = b.node("GeometryNodeStoreNamedAttribute", data_type='FLOAT', domain='POINT')
        b.put(st, "Geometry", pts)
        b.put(st, "Name", name)
        b.put(st, "Value", value)
        pts = b.out(st)

    # ---- rotation: random spin, align to ground, random tilt, global lean
    spin = b.axis_angle((0.0, 0.0, 1.0), b.math('MULTIPLY', b.rand(0.0, math.tau, seed, 23),
                                                G("Random Rotation")))
    align = b.node("FunctionNodeAlignRotationToVector", axis='Z', pivot_axis='AUTO')
    b.put(align, "Rotation", spin)
    b.put(align, "Factor", G("Align to Normal"))
    b.put(align, "Vector", normal)
    ta = b.rand(0.0, math.tau, seed, 59)
    tilt = b.axis_angle(b.xyz(b.math('COSINE', ta), b.math('SINE', ta), 0.0),
                        b.math('MULTIPLY', b.rand(0.0, 1.0, seed, 61), G("Random Tilt")))
    rot = b.rotate(b.out(align), tilt, 'LOCAL')
    ld = G("Lean Direction")      # lean towards (cos d, sin d): axis (-sin d, cos d, 0)
    lean = b.axis_angle(b.xyz(b.math('MULTIPLY', b.math('SINE', ld), -1.0), b.math('COSINE', ld), 0.0),
                        b.math('MULTIPLY', G("Lean Angle"), b.rand(0.75, 1.25, seed, 67)))
    rot = b.rotate(rot, lean, 'GLOBAL')

    # ---- LOD index and instancing
    lod = b.math('ADD', b.math('ADD', b.cmp('GREATER_THAN', d, G("LOD1 Distance")),
                                b.cmp('GREATER_THAN', d, G("LOD2 Distance"))),
                 b.cmp('GREATER_THAN', d, G("LOD3 Distance")))

    total = sum(max(0.0, w) for w, objs in assets if objs) or 1.0
    pick = b.rand(0.0, 1.0, seed, 29)
    asset_id = None
    acc = 0.0
    for w, _objs in usable[:-1]:
        acc += max(0.0, w) / total
        step = b.cmp('GREATER_THAN', pick, acc)
        asset_id = step if asset_id is None else b.math('ADD', asset_id, step)

    # LOD preview colours: viewport only, so renders always use the real materials
    colors_on = b.boolean('AND', G("LOD Colors"), is_vp)
    join = b.node("GeometryNodeJoinGeometry")
    for k, (_w, objs) in enumerate(usable):
        is_k = b.cmp('EQUAL', asset_id, float(k)) if asset_id is not None else None
        level = b.math('MINIMUM', lod, float(len(objs) - 1))
        for li, ob in enumerate(objs):
            info = b.node("GeometryNodeObjectInfo", transform_space='ORIGINAL')
            info.inputs["Object"].default_value = ob
            b.put(info, "As Instance", True)
            # keep the source object's rotation/scale (e.g. FBX 0.01 scale), not its location
            xf = b.node("GeometryNodeTransform")
            b.put(xf, "Geometry", b.out(info, "Geometry"))
            b.put(xf, "Rotation", b.out(info, "Rotation"))
            b.put(xf, "Scale", b.out(info, "Scale"))
            # coloured copy: the source mesh (once, not per instance) with the LOD material
            info_c = b.node("GeometryNodeObjectInfo", transform_space='ORIGINAL')
            info_c.inputs["Object"].default_value = ob
            b.put(info_c, "As Instance", False)
            xf_c = b.node("GeometryNodeTransform")
            b.put(xf_c, "Geometry", b.out(info_c, "Geometry"))
            b.put(xf_c, "Rotation", b.out(info_c, "Rotation"))
            b.put(xf_c, "Scale", b.out(info_c, "Scale"))
            setmat = b.node("GeometryNodeSetMaterial")
            b.put(setmat, "Geometry", b.out(xf_c))
            setmat.inputs["Material"].default_value = lod_debug_material(li)
            instance = b.switch('GEOMETRY', colors_on, b.out(xf), b.out(setmat))
            sel = b.all_of(b.cmp('EQUAL', level, float(li)), G("Show LOD%d" % li),
                           G("Show Asset %d" % (k + 1)))
            if is_k is not None:
                sel = b.boolean('AND', is_k, sel)
            iop = b.node("GeometryNodeInstanceOnPoints")
            b.put(iop, "Points", pts)
            b.put(iop, "Selection", sel)
            b.put(iop, "Instance", instance)
            b.put(iop, "Rotation", rot)
            b.put(iop, "Scale", scale)
            ng.links.new(b.out(iop), b._pick(join.inputs, "Geometry"))
    ng.links.new(b.out(join), go.inputs[0])


# ---------------------------------------------------------------- python side

def _scatter_index(scene, system):
    for i, sy in enumerate(settings_of(scene).scatter_systems):
        if sy == system:
            return i
    return -1


def _owner_system(prop_group):
    """Scatter system owning an asset/system property group."""
    path = prop_group.path_from_id()
    m = re.search(r"scatter_systems\[(\d+)\]", path)
    if not m:
        return None, None
    scene = prop_group.id_data
    return scene, settings_of(scene).scatter_systems[int(m.group(1))]


def scatter_assets(system):
    out = []
    for a in system.assets:
        objs = [getattr(a, "lod%d" % i) for i in range(SCATTER_MAX_LODS)]
        objs = [o for o in objs if o is not None]
        out.append((a.weight, objs))
    return out


def _asset_metrics(system):
    """Largest instance radius / height (world units at scale 1)."""
    radius = height = 0.25
    for _w, objs in scatter_assets(system):
        if not objs:
            continue
        ob = objs[0]
        m3 = ob.matrix_world.to_3x3()
        pts = [m3 @ Vector(c) for c in ob.bound_box]
        radius = max(radius, max(p.length for p in pts))
        height = max(height, max(p.z for p in pts))
    return radius, height


def _camera_tans(scene, margin_pct):
    cam = scene.camera
    if cam is None or cam.type != 'CAMERA':
        return None
    if cam.data.type != 'PERSP':
        return (-1e6, 1e6, -1e6, 1e6)   # ortho/panoramic: frustum test disabled
    fr = cam.data.view_frame(scene=scene)
    z = -fr[0].z
    if z <= 0:
        return None
    xs = [v.x / z for v in fr]
    ys = [v.y / z for v in fr]
    l, r, bo, t = min(xs), max(xs), min(ys), max(ys)
    m = margin_pct / 100.0
    w, h = r - l, t - bo
    return (l - m * w, r + m * w, bo - m * h, t + m * h)


def _ensure_collection(scene, name):
    coll = bpy.data.collections.get(name)
    if coll is None:
        coll = bpy.data.collections.new(name)
    if coll.name not in scene.collection.children:
        scene.collection.children.link(coll)
    return coll


def scatter_holder(scene, system, create=False):
    ob = system.holder
    if ob is not None and ob.name in bpy.data.objects:
        return ob
    if not create:
        return None
    me = bpy.data.meshes.new("SO_Scatter_" + system.name)
    ob = bpy.data.objects.new("SO_Scatter_" + system.name, me)
    _ensure_collection(scene, SCATTER_COLLECTION).objects.link(ob)
    ob.so_never_cull = True        # culling happens per instance inside GN
    ob.so_not_occluder = True
    system.holder = ob
    return ob


def _scatter_modifier(system):
    ob = system.holder
    return ob.modifiers.get(SCATTER_MOD) if ob is not None else None


def scatter_rebuild(scene, system):
    ob = scatter_holder(scene, system, create=True)
    mod = ob.modifiers.get(SCATTER_MOD)
    if mod is None:
        mod = ob.modifiers.new(SCATTER_MOD, 'NODES')
    ng = mod.node_group
    if ng is None:
        ng = bpy.data.node_groups.new("SO_Scatter_" + system.name, 'GeometryNodeTree')
        mod.node_group = ng
    build_scatter_tree(ng, scatter_assets(system))
    scatter_sync(scene, system)


def scatter_sync(scene, system, camera_only=False):
    """Write the system's settings (and the camera inputs) into the modifier."""
    ob = scatter_holder(scene, system)
    mod = ob.modifiers.get(SCATTER_MOD) if ob else None
    ng = mod.node_group if mod else None
    if ng is None:
        return
    ids = {it.name: it.identifier for it in ng.interface.items_tree
           if getattr(it, "in_out", None) == 'INPUT'}
    if not camera_only and any(n not in ids for n, *_rest in _SCATTER_INPUTS):
        scatter_rebuild(scene, system)        # tree from an older version: add the new inputs
        return

    def setv(name, value):
        key = ids.get(name)
        if key is None:
            return
        try:
            if mod[key] != value:
                mod[key] = value
        except (KeyError, TypeError):
            mod[key] = value

    radius, height = _asset_metrics(system)      # at scale 1; GN multiplies per instance
    tans = _camera_tans(scene, system.margin)
    setv("Camera", scene.camera if tans is not None else None)
    setv("Has Camera", tans is not None)
    if tans is not None:
        for name, v in zip(("Tan Left", "Tan Right", "Tan Bottom", "Tan Top"), tans):
            setv(name, float(v))
    setv("Pad", float(radius))
    setv("Lift", float(height))
    if not camera_only:
        if mod.show_viewport != system.show_viewport:
            mod.show_viewport = system.show_viewport
        if mod.show_render != system.show_render:
            mod.show_render = system.show_render
        for name, value in (
                ("Surface", system.surface), ("Extra Surfaces", system.extra_surfaces),
                ("Occluders", system.occluders), ("Avoid", system.avoid),
                ("Density", system.density), ("Viewport Density", system.viewport_density / 100.0),
                ("Seed", system.seed), ("Even Spacing", system.distribution == 'EVEN'),
                ("Min Distance", system.min_distance), ("Density Mask", system.density_mask),
                ("Max Slope", system.max_slope), ("Slope Blur", system.slope_blur),
                ("Patch Size", system.patch_size), ("Patch Amount", system.patch_amount),
                ("Patch Scale", system.patch_scale), ("Avoid Distance", system.avoid_distance),
                ("Avoid Blur", system.avoid_blur), ("Edge Offset", system.edge_offset),
                ("Scale Min", system.scale_min), ("Scale Max", system.scale_max),
                ("Random Rotation", system.random_rotation), ("Align to Normal", system.align_amount),
                ("Random Tilt", system.random_tilt), ("Lean Angle", system.lean_angle),
                ("Lean Direction", system.lean_direction), ("Dryness", system.dryness),
                ("Dry Patchiness", system.dry_patchiness),
                ("LOD1 Distance", system.lod_distances[0]), ("LOD2 Distance", system.lod_distances[1]),
                ("LOD3 Distance", system.lod_distances[2]), ("Max Distance", system.max_distance),
                ("Viewport Distance", system.viewport_distance), ("Fade", system.fade),
                ("Thin Start", system.thin_start), ("Thin Keep", system.thin_keep),
                ("Thin Compensate", system.thin_compensate), ("Frustum", system.frustum),
                ("Occlusion", system.occlusion),
                ("Cull Viewport", system.cull_in in {'BOTH', 'VIEWPORT'}),
                ("Cull Render", system.cull_in in {'BOTH', 'RENDER'}),
                ("LOD Colors", system.lod_colors)):
            setv(name, value)
        for i in range(SCATTER_MAX_LODS):
            setv("Show LOD%d" % i, bool(system.lod_show[i]))
        k = 0
        for a in system.assets:
            if any(getattr(a, "lod%d" % i) is not None for i in range(SCATTER_MAX_LODS)):
                k += 1
                setv("Show Asset %d" % k, bool(a.show))
    ob.update_tag()


def _scatter_camera_signature(scene):
    cam = scene.camera
    r = scene.render
    if cam is None or cam.type != 'CAMERA':
        return ("none",)
    d = cam.data
    return (cam.name_full, d.type, round(d.lens, 5), round(d.sensor_width, 5),
            round(d.sensor_height, 5), d.sensor_fit, round(d.shift_x, 5), round(d.shift_y, 5),
            round(d.ortho_scale, 5), r.resolution_x, r.resolution_y,
            round(r.pixel_aspect_x, 5), round(r.pixel_aspect_y, 5))


def scatter_camera_sync(scene, force=False):
    """Cheap: only writes when the camera's lens/frame/assignment changed.
    (Camera MOVEMENT needs nothing: the node tree reads its transform.)"""
    s = settings_of(scene)
    if not s.scatter_systems:
        return
    sig = _scatter_camera_signature(scene)
    if not force and _Runtime.scatter_cam_sig.get(scene.name_full) == sig:
        return
    _Runtime.scatter_cam_sig[scene.name_full] = sig
    for system in s.scatter_systems:
        try:
            scatter_sync(scene, system, camera_only=True)
        except Exception as exc:
            _log("Scatter camera sync failed: %s" % exc)


def scatter_stats(context, system):
    """Count what the system draws now vs. without optimization (render
    settings emulated: culling as in render, full density, all LOD0)."""
    scene = context.scene
    ob = scatter_holder(scene, system)
    if ob is None:
        return None
    lod_of = {}
    lod0_tris = {}
    for ai, (_w, objs) in enumerate(scatter_assets(system)):
        for li, o in enumerate(objs):
            lod_of[o.name_full] = (ai, li)
        if objs and objs[0].type == 'MESH':
            lod0_tris[ai] = sum(len(p.vertices) - 2 for p in objs[0].data.polygons)

    tri_cache = {}

    def count():
        dg = context.evaluated_depsgraph_get()
        dg.update()
        by_lod = [0] * SCATTER_MAX_LODS
        by_asset = {}
        tris = 0
        for inst in dg.object_instances:
            if not inst.is_instance or inst.parent is None or inst.parent.original != ob:
                continue
            src = inst.object.original
            key = lod_of.get(src.name_full)
            if key is None:
                continue
            by_lod[key[1]] += 1
            by_asset[key[0]] = by_asset.get(key[0], 0) + 1
            if src.name_full not in tri_cache:
                tri_cache[src.name_full] = sum(len(p.vertices) - 2 for p in src.data.polygons) \
                    if src.type == 'MESH' else 0
            tris += tri_cache[src.name_full]
        return by_lod, by_asset, tris

    saved = (system.viewport_density, system.cull_in, system.viewport_distance, system.lod_colors)
    _Runtime.busy = True
    try:
        system["viewport_density"] = 100.0
        system["viewport_distance"] = 0.0
        system["lod_colors"] = False      # coloured copies are not the source objects
        system["cull_in"] = 0 if system.cull_in in {'BOTH', 'RENDER'} else 2   # BOTH / RENDER
        scatter_sync(scene, system)
        by_lod, _by_asset, tris = count()
        # baseline: no culling, no thinning, everything at LOD0
        mod = ob.modifiers[SCATTER_MOD]
        ids = {it.name: it.identifier for it in mod.node_group.interface.items_tree
               if getattr(it, "in_out", None) == 'INPUT'}
        mod[ids["Cull Viewport"]] = False
        mod[ids["Thin Keep"]] = 1.0
        for i in range(3):
            mod[ids["LOD%d Distance" % (i + 1)]] = 1e9
        for name, ident in ids.items():         # baseline draws every asset and LOD
            if name.startswith("Show "):
                mod[ident] = True
        ob.update_tag()
        _bl, base_assets, _t = count()
        base_n = sum(base_assets.values())
        base_tris = sum(n * lod0_tris.get(a, 0) for a, n in base_assets.items())
    finally:
        system["viewport_density"] = saved[0]
        system["viewport_distance"] = saved[2]
        system["lod_colors"] = saved[3]
        system.cull_in = saved[1]
        scatter_sync(scene, system)
        _Runtime.busy = False
    return {"lod": by_lod, "n": sum(by_lod), "tris": tris, "base_n": base_n, "base_tris": base_tris}


# ---------------------------------------------------------------- system data helpers

# never stored in presets / copied by "duplicate" settings
_SYSTEM_SKIP = {"rna_type", "name", "holder", "assets", "asset_index", "stats",
                "lod_show", "lod_colors"}      # view toggles, not part of the look
_SYSTEM_SCENE_POINTERS = ("surface", "extra_surfaces", "occluders", "avoid")


def system_settings(system, pointers=False):
    """Plain dict of a system's settings (JSON-safe unless pointers=True)."""
    out = {}
    for p in system.bl_rna.properties:
        k = p.identifier
        if k in _SYSTEM_SKIP or p.type == 'COLLECTION':
            continue
        if p.type == 'POINTER':
            if pointers:
                out[k] = getattr(system, k)
            continue
        v = getattr(system, k)
        if getattr(p, "is_array", False):
            v = list(v)
        out[k] = v
    return out


def apply_system_settings(system, values):
    """Set settings from a dict; unknown or invalid keys are skipped."""
    _Runtime.busy = True
    try:
        for k, v in values.items():
            if k in _SYSTEM_SKIP or not hasattr(system, k):
                continue
            try:
                setattr(system, k, v)
            except (TypeError, ValueError, AttributeError):
                pass
    finally:
        _Runtime.busy = False


def _unique_system_name(s, base):
    names = {sy.name for sy in s.scatter_systems}
    if base not in names:
        return base
    i = 2
    while "%s %d" % (base, i) in names:
        i += 1
    return "%s %d" % (base, i)


def new_scatter_system(scene, surface, objs=(), name=None):
    """Create a system on surface with the given asset objects (LOD-grouped)."""
    s = settings_of(scene)
    _Runtime.busy = True
    try:
        system = s.scatter_systems.add()
        system.name = _unique_system_name(s, name or "Scatter 1") if name else \
            _unique_system_name(s, "Scatter %d" % len(s.scatter_systems))
        system.surface = surface
        system.seed = len(s.scatter_systems) * 7
        system.lod_colors = s.lod_colors       # follow Visualize LODs
    finally:
        _Runtime.busy = False
    _add_assets(system, list(objs))
    s.scatter_index = len(s.scatter_systems) - 1
    scatter_rebuild(scene, system)
    return s.scatter_systems[s.scatter_index]


def delete_scatter_system(scene, index):
    s = settings_of(scene)
    system = s.scatter_systems[index]
    ob = system.holder
    if ob is not None:
        mod = ob.modifiers.get(SCATTER_MOD)
        ng = mod.node_group if mod else None
        me = ob.data
        bpy.data.objects.remove(ob)
        if me is not None and me.users == 0:
            bpy.data.meshes.remove(me)
        if ng is not None and ng.users == 0:
            bpy.data.node_groups.remove(ng)
    s.scatter_systems.remove(index)
    s.scatter_index = max(0, min(s.scatter_index, len(s.scatter_systems) - 1))


def duplicate_scatter_system(scene, index):
    s = settings_of(scene)
    src = s.scatter_systems[index]
    values = system_settings(src, pointers=True)
    assets = [(a.name, a.weight, [getattr(a, "lod%d" % i) for i in range(SCATTER_MAX_LODS)])
              for a in src.assets]
    name = _unique_system_name(s, src.name + " Copy")
    _Runtime.busy = True
    try:
        system = s.scatter_systems.add()        # invalidates `src`
        system.name = name
    finally:
        _Runtime.busy = False
    apply_system_settings(system, values)
    _Runtime.busy = True
    try:
        system.seed = system.seed + 1          # same look, different placement
        for label, weight, lods in assets:
            a = system.assets.add()
            a.name, a.weight = label, weight
            for i, o in enumerate(lods):
                setattr(a, "lod%d" % i, o)
    finally:
        _Runtime.busy = False
    new_i = len(s.scatter_systems) - 1
    s.scatter_systems.move(new_i, index + 1)
    s.scatter_index = index + 1
    system = s.scatter_systems[index + 1]
    scatter_rebuild(scene, system)
    return system


def _hide_source(o):
    o.hide_render = True
    try:
        o.hide_set(True)
    except RuntimeError:
        pass   # not in the current view layer


# ---------------------------------------------------------------- color variation

_VARIATION_TARGETS = {"ShaderNodeBsdfPrincipled": "Base Color", "ShaderNodeBsdfTranslucent": "Color",
                      "ShaderNodeBsdfDiffuse": "Color", "ShaderNodeSubsurfaceScattering": "Color"}


def variation_group():
    """Shader group: Color -> Color varied by the instance's so_random / so_dry."""
    ng = bpy.data.node_groups.get(VARIATION_GROUP)
    if ng is not None and ng.bl_idname == 'ShaderNodeTree':
        return ng
    ng = bpy.data.node_groups.new(VARIATION_GROUP, 'ShaderNodeTree')
    iface = ng.interface
    for name, stype, default, lo, hi in (
            ("Color", 'NodeSocketColor', (0.8, 0.8, 0.8, 1.0), None, None),
            ("Dry Color", 'NodeSocketColor', (0.62, 0.50, 0.24, 1.0), None, None),
            ("Hue Variation", 'NodeSocketFloat', 0.03, 0.0, 0.5),
            ("Brightness Variation", 'NodeSocketFloat', 0.3, 0.0, 1.0),
            ("Dry Brightness", 'NodeSocketFloat', 1.25, 0.0, 4.0)):
        sock = iface.new_socket(name=name, in_out='INPUT', socket_type=stype)
        sock.default_value = default
        if lo is not None:
            sock.min_value, sock.max_value = lo, hi
    iface.new_socket(name="Color", in_out='OUTPUT', socket_type='NodeSocketColor')
    b = _NodeBuilder(ng)
    gi = b.node("NodeGroupInput")
    go = b.node("NodeGroupOutput")
    G = gi.outputs.get

    def attr(name):
        n = b.node("ShaderNodeAttribute", attribute_type='INSTANCER', attribute_name=name)
        return b.out(n, "Fac")

    rnd = b.math('SUBTRACT', attr("so_random"), 0.5)
    hsv = b.node("ShaderNodeHueSaturation")
    b.put(hsv, "Hue", b.math('ADD', 0.5, b.math('MULTIPLY', rnd, G("Hue Variation"))))
    b.put(hsv, "Saturation", 1.0)
    b.put(hsv, "Value", b.math('ADD', 1.0, b.math('MULTIPLY', rnd, G("Brightness Variation"))))
    b.put(hsv, "Fac", 1.0)
    b.put(hsv, "Color", G("Color"))
    tint = b.node("ShaderNodeMix", data_type='RGBA', blend_type='COLOR')
    b.put(tint, "Factor_Float", 0.9)
    b.put(tint, "A_Color", b.out(hsv))
    b.put(tint, "B_Color", G("Dry Color"))
    bright = b.node("ShaderNodeHueSaturation")
    b.put(bright, "Value", G("Dry Brightness"))
    b.put(bright, "Color", b.out(tint, "Result_Color"))
    mix = b.node("ShaderNodeMix", data_type='RGBA', blend_type='MIX')
    b.put(mix, "Factor_Float", attr("so_dry"))
    b.put(mix, "A_Color", b.out(hsv))
    b.put(mix, "B_Color", b.out(bright))
    ng.links.new(b.out(mix, "Result_Color"), go.inputs[0])
    return ng


def add_variation_to_material(mat):
    """Insert the variation group in front of every BSDF color input.
    Returns how many inputs were wired (0 if already done)."""
    if mat is None or mat.node_tree is None:
        return 0
    nt = mat.node_tree
    group = variation_group()
    n = 0
    for node in list(nt.nodes):
        sock_name = _VARIATION_TARGETS.get(node.bl_idname)
        sock = node.inputs.get(sock_name) if sock_name else None
        if sock is None:
            continue
        g = None
        if sock.is_linked:
            link = sock.links[0]
            if link.from_node.bl_idname == 'ShaderNodeGroup' and link.from_node.node_tree == group:
                continue
            g = nt.nodes.new("ShaderNodeGroup")
            g.node_tree = group
            nt.links.new(link.from_socket, g.inputs["Color"])
        else:
            g = nt.nodes.new("ShaderNodeGroup")
            g.node_tree = group
            g.inputs["Color"].default_value = sock.default_value
        g.location = (node.location.x - 220, node.location.y)
        g.label = "Instance Variation"
        nt.links.new(g.outputs["Color"], sock)
        n += 1
    return n


# ---------------------------------------------------------------- preset library

def library_dir(scene):
    """Preset folder: the scene setting, else <user scripts>/presets/scene_optimizer/scatter."""
    s = settings_of(scene)
    p = bpy.path.abspath(s.scatter_library) if s.scatter_library.strip() else ""
    if not p:
        p = bpy.utils.user_resource('SCRIPTS', path=os.path.join("presets", "scene_optimizer", "scatter"),
                                    create=True)
    os.makedirs(p, exist_ok=True)
    return p


def _preset_stem(name):
    stem = re.sub(r"[^\w\- ]+", "_", name).strip() or "Preset"
    return stem


def _eevee_engine():
    engines = {i.identifier for i in bpy.types.RenderSettings.bl_rna.properties["engine"].enum_items}
    return "BLENDER_EEVEE_NEXT" if "BLENDER_EEVEE_NEXT" in engines else "BLENDER_EEVEE"


def render_preset_thumbnail(objs, path, size=256):
    """Render a small clump of the given LOD0 objects on a patch of ground."""
    sc = bpy.data.scenes.new("SO_Thumbnail")
    made_obs, made_data = [], []
    try:
        objs = [o for o in objs if o is not None][:6]
        if not objs:
            return False
        shapes = []
        for ob in objs:
            _loc, rot, scl = ob.matrix_world.decompose()
            m3 = rot.to_matrix() @ Matrix.Diagonal(scl)
            corners = [m3 @ Vector(v) for v in ob.bound_box]
            foot = max(max(abs(v.x), abs(v.y)) for v in corners)
            top = max(v.z for v in corners)
            shapes.append((ob, m3, foot, top))
        foot = max(sh[2] for sh in shapes) or 0.5
        top = max(sh[3] for sh in shapes) or 0.5
        spread = max(foot, 0.35 * top) * 1.1
        n = 28
        golden = math.pi * (3.0 - math.sqrt(5.0))
        for i in range(n):                         # sunflower layout, stable, no RNG
            ob, m3, _f, _t = shapes[i % len(shapes)]
            r = spread * math.sqrt((i + 0.5) / n)
            a = i * golden
            spin = Matrix.Rotation(a * 3.7, 3, 'Z')
            c = ob.copy()
            c.parent = None
            c.hide_render = c.hide_viewport = False
            s = 0.85 + 0.3 * ((i * 0.618) % 1.0)
            c.matrix_world = Matrix.Translation((r * math.cos(a), r * math.sin(a), 0.0)) @                 (spin @ m3 @ Matrix.Diagonal((s, s, s))).to_4x4()
            sc.collection.objects.link(c)
            made_obs.append(c)
        ground_r = spread + foot * 0.8
        gme = bpy.data.meshes.new("SO_ThumbGround")
        segs = 48
        gme.from_pydata([(0, 0, 0)] + [(ground_r * math.cos(k * math.tau / segs),
                                        ground_r * math.sin(k * math.tau / segs), 0) for k in range(segs)],
                        [], [(0, k + 1, (k + 1) % segs + 1) for k in range(segs)])
        gmat = bpy.data.materials.new("SO_ThumbGround")
        gmat.diffuse_color = (0.05, 0.045, 0.025, 1.0)
        if gmat.node_tree is not None or bpy.app.version < (5, 0, 0):
            gmat.use_nodes = True
            bs = next((nd for nd in gmat.node_tree.nodes if nd.bl_idname == "ShaderNodeBsdfPrincipled"), None)
            if bs is not None:
                bs.inputs["Base Color"].default_value = (0.05, 0.045, 0.025, 1.0)
                bs.inputs["Roughness"].default_value = 1.0
        gme.materials.append(gmat)
        ground = bpy.data.objects.new("SO_ThumbGround", gme)
        sc.collection.objects.link(ground)
        made_obs.append(ground)
        made_data += [gme, gmat]
        center = Vector((0.0, 0.0, top * 0.35))
        radius = max(spread * 0.95, top * 0.7)
        cam = bpy.data.objects.new("SO_ThumbCam", bpy.data.cameras.new("SO_ThumbCam"))
        made_obs.append(cam)
        made_data.append(cam.data)
        cam.data.lens = 50
        view = Vector((0.35, -1.0, 0.42)).normalized()
        cam.location = center + view * radius * 3.2
        cam.rotation_euler = (center - cam.location).to_track_quat('-Z', 'Y').to_euler()
        cam.data.clip_end = radius * 40
        sc.collection.objects.link(cam)
        sc.camera = cam
        sun = bpy.data.objects.new("SO_ThumbSun", bpy.data.lights.new("SO_ThumbSun", 'SUN'))
        made_obs.append(sun)
        made_data.append(sun.data)
        sun.data.energy = 4.5
        sun.data.angle = math.radians(3.0)
        sun.rotation_euler = (math.radians(45), math.radians(8), math.radians(150))
        sc.collection.objects.link(sun)
        world = bpy.data.worlds.new("SO_ThumbWorld")
        made_data.append(world)
        world.color = (0.32, 0.4, 0.5)
        if world.node_tree is not None or bpy.app.version < (5, 0, 0):
            world.use_nodes = True
            bg = world.node_tree.nodes.get("Background")
            if bg is not None:
                bg.inputs["Color"].default_value = (0.32, 0.4, 0.5, 1.0)
                bg.inputs["Strength"].default_value = 1.0
        sc.world = world
        r = sc.render
        r.engine = _eevee_engine()
        r.resolution_x = r.resolution_y = size
        r.resolution_percentage = 100
        r.film_transparent = False
        try:
            r.image_settings.file_format = 'PNG'
            r.image_settings.color_mode = 'RGB'
        except (TypeError, AttributeError):
            pass
        try:
            sc.eevee.taa_render_samples = 24
        except AttributeError:
            pass
        try:
            sc.view_settings.view_transform = bpy.context.scene.view_settings.view_transform
        except (TypeError, AttributeError):
            pass
        r.filepath = path
        bpy.ops.render.render(write_still=True, scene=sc.name)
        return os.path.exists(path)
    finally:
        for ob in made_obs:
            bpy.data.objects.remove(ob)
        for d in made_data:
            try:
                coll = {bpy.types.Camera: bpy.data.cameras, bpy.types.Light: bpy.data.lights,
                        bpy.types.World: bpy.data.worlds, bpy.types.Mesh: bpy.data.meshes,
                        bpy.types.Material: bpy.data.materials}
                for typ, store in coll.items():
                    if isinstance(d, typ):
                        store.remove(d)
                        break
            except ReferenceError:
                pass
        bpy.data.scenes.remove(sc)


def save_scatter_preset(scene, systems, name, thumbnail=True):
    """Write <stem>.json (settings, assets, LODs) + <stem>.blend (asset objects
    with meshes/materials) + <stem>.png into the library. Returns the stem."""
    lib = library_dir(scene)
    stem = _preset_stem(name)
    objs, thumb_objs = set(), []
    data = {"format": PRESET_FORMAT, "version": PRESET_VERSION, "name": name,
            "blender": bpy.app.version_string, "systems": []}
    for sy in systems:
        entry = {"name": sy.name, "settings": system_settings(sy), "assets": []}
        for a in sy.assets:
            lods = [getattr(a, "lod%d" % i) for i in range(SCATTER_MAX_LODS)]
            entry["assets"].append({"name": a.name, "weight": a.weight,
                                    "lods": [o.name if o else None for o in lods]})
            objs |= {o for o in lods if o}
            if lods[0] is not None and lods[0] not in thumb_objs:
                thumb_objs.append(lods[0])
        data["systems"].append(entry)
    if not objs:
        raise ValueError("The system has no assets to save")
    bpy.data.libraries.write(os.path.join(lib, stem + ".blend"), objs,
                             path_remap='ABSOLUTE', fake_user=True)
    with open(os.path.join(lib, stem + ".json"), "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    png = os.path.join(lib, stem + ".png")
    if thumbnail:
        try:
            render_preset_thumbnail(thumb_objs, png)
        except Exception as exc:
            _log("Thumbnail render failed: %s" % exc)
    _Previews.stamp = None
    return stem


def read_scatter_preset(scene, stem):
    path = os.path.join(library_dir(scene), stem + ".json")
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if data.get("format") != PRESET_FORMAT:
        raise ValueError("%s is not a scatter preset" % path)
    return data


def add_scatter_preset(scene, stem, surface):
    """Create the preset's system(s) on surface. Asset objects are appended
    once per file (re-adding a preset reuses them). Returns the new systems."""
    data = read_scatter_preset(scene, stem)
    blend = os.path.join(library_dir(scene), stem + ".blend")
    tag = stem + "/"
    have = {o.get("so_preset_source"): o for o in bpy.data.objects
            if str(o.get("so_preset_source", "")).startswith(tag)}
    needed = []
    for entry in data["systems"]:
        for a in entry["assets"]:
            needed += [n for n in a["lods"] if n and tag + n not in have and n not in needed]
    if needed:
        if not os.path.exists(blend):
            raise FileNotFoundError(blend)
        with bpy.data.libraries.load(blend, link=False) as (src, dst):
            names = [n for n in needed if n in set(src.objects)]
            dst.objects = list(names)
        coll = _ensure_collection(scene, SCATTER_ASSET_COLLECTION)
        for n, ob in zip(names, dst.objects):
            if ob is None:
                continue
            ob["so_preset_source"] = tag + n
            coll.objects.link(ob)
            ob.location = (0.0, 0.0, -1000.0)     # out of the way; instancing ignores location
            have[tag + n] = ob
    for ob in have.values():
        _hide_source(ob)
    s = settings_of(scene)
    made = []
    for entry in data["systems"]:
        system = new_scatter_system(scene, surface, (), name=entry.get("name") or data.get("name"))
        idx = s.scatter_index
        apply_system_settings(s.scatter_systems[idx], entry.get("settings", {}))
        system = s.scatter_systems[idx]
        _Runtime.busy = True
        try:
            system.surface = surface
            for a in entry["assets"]:
                item = system.assets.add()
                item.name = a.get("name", "Asset")
                item.weight = float(a.get("weight", 1.0))
                for i, n in enumerate(a["lods"][:SCATTER_MAX_LODS]):
                    if n:
                        setattr(item, "lod%d" % i, have.get(tag + n))
        finally:
            _Runtime.busy = False
        scatter_rebuild(scene, system)
        made.append(system.name)
    return made


def delete_scatter_preset(scene, stem):
    lib = library_dir(scene)
    n = 0
    for ext in (".json", ".blend", ".png", ".blend1"):
        p = os.path.join(lib, stem + ext)
        if os.path.exists(p):
            os.remove(p)
            n += 1
    _Previews.stamp = None
    return n


class _Previews:
    pcoll = None
    items = []
    stamp = None


def scatter_preset_items(_self, context):
    """Enum items (with rendered thumbnails) for the preset browser."""
    scene = context.scene if context else None
    if scene is None:
        return _Previews.items or [("NONE", "(no presets)", "", 'INFO', 0)]
    try:
        lib = library_dir(scene)
        files = sorted(f for f in os.listdir(lib) if f.lower().endswith(".json"))
    except OSError:
        lib, files = "", []
    stamp = (lib, tuple((f, os.path.getmtime(os.path.join(lib, f))) for f in files))
    if stamp == _Previews.stamp and _Previews.items:
        return _Previews.items
    if _Previews.pcoll is None:
        _Previews.pcoll = bpy.utils.previews.new()
    items = []
    for i, f in enumerate(files):
        stem = f[:-5]
        label, desc = stem, ""
        try:
            with open(os.path.join(lib, f), encoding="utf-8") as fh:
                data = json.load(fh)
            label = data.get("name", stem)
            systems = data.get("systems", [])
            desc = "%d system%s: %s" % (len(systems), "" if len(systems) == 1 else "s",
                                        ", ".join(sy.get("name", "?") for sy in systems))
        except (OSError, ValueError):
            continue
        icon = 'OUTLINER_OB_POINTCLOUD'
        png = os.path.join(lib, stem + ".png")
        if os.path.exists(png):
            key = "%s|%s" % (png, os.path.getmtime(png))
            if key not in _Previews.pcoll:
                _Previews.pcoll.load(key, png, 'IMAGE')
            icon = _Previews.pcoll[key].icon_id
        items.append((stem, label, desc, icon, i))
    _Previews.items = items or [("NONE", "(no presets)", "Save a system to start your library",
                                 'INFO', 0)]
    _Previews.stamp = stamp
    return _Previews.items


# ---------------------------------------------------------------- properties

def _scatter_changed(self, context):
    scene, system = _owner_system(self)
    if system is not None and not _Runtime.busy:
        scatter_sync(scene, system)


def _scatter_assets_changed(self, context):
    scene, system = _owner_system(self)
    if system is not None and not _Runtime.busy:
        scatter_rebuild(scene, system)


def _scatter_renamed(self, context):
    ob = self.holder
    if ob is not None:
        ob.name = "SO_Scatter_" + self.name
        mod = ob.modifiers.get(SCATTER_MOD)
        if mod and mod.node_group:
            mod.node_group.name = "SO_Scatter_" + self.name


def _mesh_poll(_self, obj):
    return obj.type == 'MESH'


class SCENEOPT_PG_scatter_asset(PropertyGroup):
    name: StringProperty(name="Name", default="Asset")
    lod0: PointerProperty(name="LOD0", type=bpy.types.Object, update=_scatter_assets_changed,
                          description="Full detail (closest)")
    lod1: PointerProperty(name="LOD1", type=bpy.types.Object, update=_scatter_assets_changed)
    lod2: PointerProperty(name="LOD2", type=bpy.types.Object, update=_scatter_assets_changed)
    lod3: PointerProperty(name="LOD3", type=bpy.types.Object, update=_scatter_assets_changed)
    weight: FloatProperty(name="Weight", default=1.0, min=0.0, soft_max=10.0,
                          update=_scatter_assets_changed,
                          description="Relative share of instances using this asset")
    show: BoolProperty(name="Show", default=True, update=_scatter_changed,
                       description="Draw this asset's instances (hidden ones leave gaps, the "
                                   "other assets keep their places)")


def _fp(name, default, desc="", **kw):
    return FloatProperty(name=name, default=default, description=desc, update=_scatter_changed, **kw)


class SCENEOPT_PG_scatter_system(PropertyGroup):
    name: StringProperty(name="Name", default="Scatter", update=_scatter_renamed)
    holder: PointerProperty(type=bpy.types.Object)
    show_viewport: BoolProperty(name="Viewport", default=True, update=_scatter_changed,
                                description="Show this system in the viewport")
    show_render: BoolProperty(name="Render", default=True, update=_scatter_changed,
                              description="Include this system in renders")
    surface: PointerProperty(name="Surface", type=bpy.types.Object, poll=_mesh_poll,
                             update=_scatter_changed, description="Mesh to scatter on")
    extra_surfaces: PointerProperty(name="More Surfaces", type=bpy.types.Collection,
                                    update=_scatter_changed,
                                    description="Collection of more meshes to scatter on")
    assets: CollectionProperty(type=SCENEOPT_PG_scatter_asset)
    asset_index: IntProperty(default=0)
    # distribution
    density: _fp("Density", 10.0, "Instances per square meter", min=0.0, soft_max=500.0)
    viewport_density: _fp("Viewport %", 100.0, "Show only this share in the viewport "
                          "(render is unaffected)", min=0.0, max=100.0, subtype='PERCENTAGE')
    seed: IntProperty(name="Seed", default=0, min=0, update=_scatter_changed)
    distribution: EnumProperty(name="Spacing", default='RANDOM', update=_scatter_changed,
                               items=[('RANDOM', "Random", "Fast, natural clumping"),
                                      ('EVEN', "Even", "Poisson disk: never closer than Min Distance")])
    min_distance: _fp("Min Distance", 0.1, "Smallest gap between instances (Even spacing)",
                      min=0.0, soft_max=10.0, unit='LENGTH')
    density_mask: StringProperty(name="Density Mask", default="", update=_scatter_changed,
                                 description="Vertex group (or float attribute) of the surface "
                                             "that scales density 0..1")
    max_slope: _fp("Max Slope", math.radians(60.0), "No instances on steeper ground",
                   min=0.0, max=math.pi, subtype='ANGLE')
    slope_blur: _fp("Slope Blur", math.radians(10.0), "Soft band around Max Slope",
                    min=0.0, max=math.pi, subtype='ANGLE')
    patch_size: _fp("Patch Size", 8.0, "Size of the noise patches", min=0.01, soft_max=100.0,
                    unit='LENGTH')
    patch_amount: _fp("Patch Density", 0.0, "Thin density out in noise patches (clumping, "
                      "bare spots)", min=0.0, max=1.0, subtype='FACTOR')
    patch_scale: _fp("Patch Scale", 0.0, "Grow/shrink instances with the patches",
                     min=0.0, max=2.0, subtype='FACTOR')
    avoid: PointerProperty(name="Avoid", type=bpy.types.Collection, update=_scatter_changed,
                           description="Meshes and curves to keep clear (rocks, paths, buildings)")
    avoid_distance: _fp("Clear Radius", 0.5, "Empty radius around avoid geometry (half a path's "
                        "width)", min=0.0, soft_max=20.0, unit='LENGTH')
    avoid_blur: _fp("Blend", 0.5, "Soft band beyond the clear radius", min=0.0, soft_max=20.0,
                    unit='LENGTH')
    edge_offset: _fp("Edge Fade", 0.0, "Fade density out over this distance from open mesh "
                     "borders (0 = off)", min=0.0, soft_max=20.0, unit='LENGTH')
    # scale / rotation
    scale_min: _fp("Scale Min", 0.8, min=0.0)
    scale_max: _fp("Scale Max", 1.2, min=0.0)
    random_rotation: _fp("Random Rotation", 1.0, "Random turn around the up axis",
                         min=0.0, max=1.0, subtype='FACTOR')
    align_amount: _fp("Align to Ground", 0.0, "0 = upright, 1 = follow the surface normal",
                      min=0.0, max=1.0, subtype='FACTOR')
    random_tilt: _fp("Random Tilt", 0.0, "Random lean in any direction", min=0.0,
                     max=math.radians(90.0), subtype='ANGLE')
    lean_angle: _fp("Lean", 0.0, "Global lean, e.g. wind-swept grass", min=0.0,
                    max=math.radians(90.0), subtype='ANGLE')
    lean_direction: _fp("Lean Direction", 0.0, "Heading the lean points to (0 = +X)",
                        subtype='ANGLE')
    # color
    dryness: _fp("Dryness", 0.0, "Share of dry instances. Needs 'Add Variation to Materials'",
                 min=0.0, max=1.0, subtype='FACTOR')
    dry_patchiness: _fp("Dry in Patches", 0.5, "0 = dry instances random, 1 = grouped in patches",
                        min=0.0, max=1.0, subtype='FACTOR')
    # LOD
    lod_distances: FloatVectorProperty(name="LOD Distances", size=3, default=(10.0, 30.0, 60.0),
                                       min=0.0, unit='LENGTH', update=_scatter_changed,
                                       description="Distance where LOD1, LOD2, LOD3 start")
    max_distance: _fp("Max Distance", 150.0, "No instances beyond this distance from the camera",
                      min=0.0, unit='LENGTH')
    viewport_distance: _fp("Viewport Distance", 0.0, "Shorter draw distance in the viewport for "
                           "speed (0 = same as Max Distance)", min=0.0, unit='LENGTH')
    fade: _fp("Fade", 0.0, "Thin out over this band before the max distance instead of a hard "
              "line", min=0.0, unit='LENGTH')
    thin_start: _fp("Thin From", 40.0, "Beyond this distance a growing share of instances is "
                    "dropped", min=0.0, unit='LENGTH')
    thin_keep: _fp("Keep at Max", 0.3, "Share of instances still drawn at Max Distance",
                   min=0.0, max=1.0, subtype='FACTOR')
    thin_compensate: BoolProperty(name="Compensate Scale", default=True, update=_scatter_changed,
                                  description="Scale up the remaining instances so coverage looks the same")
    # culling
    frustum: BoolProperty(name="Frustum Culling", default=True, update=_scatter_changed)
    margin: _fp("Margin", 10.0, "Extra frame border kept (shadows, motion blur)", min=0.0,
                max=200.0, subtype='PERCENTAGE')
    occlusion: BoolProperty(name="Occlusion Culling", default=True, update=_scatter_changed,
                            description="Drop instances whose top is hidden from the camera "
                                        "by the surface or the occluder collection")
    occluders: PointerProperty(name="Extra Occluders", type=bpy.types.Collection,
                               update=_scatter_changed,
                               description="More geometry that hides instances (rocks, buildings)")
    cull_in: EnumProperty(name="Cull In",
                          items=[('BOTH', "Viewport & Render", ""), ('VIEWPORT', "Viewport", ""),
                                 ('RENDER', "Render", "Viewport shows everything")],
                          default='BOTH', update=_scatter_changed)
    lod_show: BoolVectorProperty(name="Show LODs", size=4, default=(True, True, True, True),
                                 update=_scatter_changed,
                                 description="Draw instances at this LOD level")
    lod_colors: BoolProperty(name="LOD Colors", default=False, update=_scatter_changed,
                             description="Viewport only: colour instances by LOD (LOD0 green, "
                                         "LOD1 yellow, LOD2 orange, LOD3 red). In Solid "
                                         "shading, Color must be set to Material")
    stats: StringProperty(default="")


# ---------------------------------------------------------------- operators

def _group_lod_objects(objs):
    """Selected objects -> [(name, [lod0, lod1, ...])] by *_LOD<n> naming."""
    buckets, order = {}, []
    for o in objs:
        info = detect_lod_name(o.name)
        if info is None:
            key, lvl, label = (o.name_full, ""), 0, o.name
        else:
            base, lvl, dup = info
            key, label = (base, dup), (base or o.name) + dup
        if key not in buckets:
            buckets[key] = (label, {})
            order.append(key)
        buckets[key][1].setdefault(lvl, o)
    out = []
    for key in order:
        label, levels = buckets[key]
        out.append((label, [levels[k] for k in sorted(levels)][:SCATTER_MAX_LODS]))
    return out


def _add_assets(system, objs):
    added = 0
    _Runtime.busy = True
    try:
        for label, lods in _group_lod_objects(objs):
            a = system.assets.add()
            a.name = label
            for i, o in enumerate(lods):
                setattr(a, "lod%d" % i, o)
            added += 1
    finally:
        _Runtime.busy = False
    return added


def _active_system(context):
    s = settings_of(context.scene)
    if 0 <= s.scatter_index < len(s.scatter_systems):
        return s.scatter_systems[s.scatter_index]
    return None


class SCENEOPT_OT_scatter_add(Operator):
    bl_idname = "scene_optimizer.scatter_add"
    bl_label = "New Scatter System"
    bl_description = ("Create a Geometry Nodes scatter on the ACTIVE mesh (the surface). Other "
                      "selected meshes become its assets; *_LOD0/_LOD1... names are grouped")
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        surface = context.active_object
        if surface is None or surface.type != 'MESH':
            self.report({'ERROR'}, "Make the surface (terrain) mesh the active object")
            return {'CANCELLED'}
        assets = [o for o in context.selected_objects if o != surface and o.type == 'MESH']
        system = new_scatter_system(context.scene, surface, assets)
        self.report({'INFO'}, "Created '%s' on %s with %d asset(s)" % (
            system.name, surface.name, len(system.assets)))
        return {'FINISHED'}


class SCENEOPT_OT_scatter_remove(Operator):
    bl_idname = "scene_optimizer.scatter_remove"
    bl_label = "Remove Scatter System"
    bl_description = "Delete the system, its holder object and node group (asset objects are kept)"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        if _active_system(context) is None:
            return {'CANCELLED'}
        delete_scatter_system(context.scene, settings_of(context.scene).scatter_index)
        return {'FINISHED'}


class SCENEOPT_OT_scatter_duplicate(Operator):
    bl_idname = "scene_optimizer.scatter_duplicate"
    bl_label = "Duplicate Scatter System"
    bl_description = "Copy the system with all settings and assets (new seed)"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        if _active_system(context) is None:
            return {'CANCELLED'}
        system = duplicate_scatter_system(context.scene, settings_of(context.scene).scatter_index)
        self.report({'INFO'}, "Created '%s'" % system.name)
        return {'FINISHED'}


class SCENEOPT_OT_scatter_move(Operator):
    bl_idname = "scene_optimizer.scatter_move"
    bl_label = "Move Scatter System"
    bl_options = {'REGISTER', 'UNDO'}
    direction: EnumProperty(items=[('UP', "Up", ""), ('DOWN', "Down", "")])

    def execute(self, context):
        s = settings_of(context.scene)
        i = s.scatter_index
        j = i - 1 if self.direction == 'UP' else i + 1
        if not (0 <= i < len(s.scatter_systems) and 0 <= j < len(s.scatter_systems)):
            return {'CANCELLED'}
        s.scatter_systems.move(i, j)
        s.scatter_index = j
        return {'FINISHED'}


class SCENEOPT_OT_scatter_asset_add(Operator):
    bl_idname = "scene_optimizer.scatter_asset_add"
    bl_label = "Add Selected as Assets"
    bl_description = ("Add the selected meshes to the active scatter system. Objects named "
                      "*_LOD0, *_LOD1 ... become one asset with those LODs")
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        system = _active_system(context)
        if system is None:
            self.report({'ERROR'}, "No scatter system")
            return {'CANCELLED'}
        objs = [o for o in context.selected_objects
                if o.type == 'MESH' and o != system.surface and o != system.holder]
        if not objs:
            self.report({'ERROR'}, "Select the asset meshes (LOD objects) first")
            return {'CANCELLED'}
        n = _add_assets(system, objs)
        system.asset_index = len(system.assets) - 1
        scatter_rebuild(context.scene, system)
        self.report({'INFO'}, "Added %d asset(s)" % n)
        return {'FINISHED'}


class SCENEOPT_OT_scatter_asset_remove(Operator):
    bl_idname = "scene_optimizer.scatter_asset_remove"
    bl_label = "Remove Asset"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        system = _active_system(context)
        if system is None or not (0 <= system.asset_index < len(system.assets)):
            return {'CANCELLED'}
        system.assets.remove(system.asset_index)
        system.asset_index = max(0, min(system.asset_index, len(system.assets) - 1))
        scatter_rebuild(context.scene, system)
        return {'FINISHED'}


class SCENEOPT_OT_scatter_rebuild(Operator):
    bl_idname = "scene_optimizer.scatter_rebuild"
    bl_label = "Rebuild"
    bl_description = "Regenerate the node tree from the asset list and resync the camera"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        system = _active_system(context)
        if system is None:
            return {'CANCELLED'}
        scatter_rebuild(context.scene, system)
        return {'FINISHED'}


class SCENEOPT_OT_scatter_stats(Operator):
    bl_idname = "scene_optimizer.scatter_stats"
    bl_label = "Count"
    bl_description = ("Count instances and triangles the system renders now, compared with no "
                      "optimization (no culling, no thinning, all LOD0)")

    def execute(self, context):
        system = _active_system(context)
        if system is None:
            return {'CANCELLED'}
        st = scatter_stats(context, system)
        if st is None:
            return {'CANCELLED'}
        saved = 100.0 * (1.0 - st["tris"] / max(1, st["base_tris"]))
        system.stats = "%s of %s instances | LOD0-3: %s | %s of %s tris (%.1f%% fewer)" % (
            "{:,}".format(st["n"]), "{:,}".format(st["base_n"]),
            " / ".join(str(v) for v in st["lod"]),
            "{:,}".format(st["tris"]), "{:,}".format(st["base_tris"]), saved)
        self.report({'INFO'}, system.stats)
        return {'FINISHED'}


class SCENEOPT_OT_scatter_hide_sources(Operator):
    bl_idname = "scene_optimizer.scatter_hide_sources"
    bl_label = "Hide Asset Sources"
    bl_description = ("Hide the original LOD objects in viewport and render (the scatter keeps "
                      "using them)")
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        n = 0
        for system in settings_of(context.scene).scatter_systems:
            for _w, objs in scatter_assets(system):
                for o in objs:
                    _hide_source(o)
                    n += 1
        self.report({'INFO'}, "Hid %d source objects" % n)
        return {'FINISHED'}


class SCENEOPT_OT_scatter_variation(Operator):
    bl_idname = "scene_optimizer.scatter_variation"
    bl_label = "Add Variation to Materials"
    bl_description = ("Insert the 'SO Instance Variation' node in front of the color of every "
                      "BSDF in the assets' materials: per-instance hue/brightness and dryness")
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        system = _active_system(context)
        if system is None:
            return {'CANCELLED'}
        mats = []
        for _w, objs in scatter_assets(system):
            for o in objs:
                for slot in o.material_slots:
                    if slot.material is not None and slot.material not in mats:
                        mats.append(slot.material)
        n = sum(add_variation_to_material(m) for m in mats)
        self.report({'INFO'}, "Wired %d color input(s) in %d material(s)" % (n, len(mats)))
        return {'FINISHED'}


class SCENEOPT_OT_scatter_preset_save(Operator):
    bl_idname = "scene_optimizer.scatter_preset_save"
    bl_label = "Save Preset"
    bl_description = ("Save the active system (or all systems as one combined preset) to the "
                      "library: settings, asset objects with LODs and materials, and a thumbnail")
    name: StringProperty(name="Name", default="")
    all_systems: BoolProperty(name="All Systems as One Preset", default=False,
                              description="Save every system of the scene together (e.g. a whole meadow)")
    thumbnail: BoolProperty(name="Render Thumbnail", default=True)

    def invoke(self, context, _event):
        system = _active_system(context)
        if system is None:
            self.report({'ERROR'}, "No scatter system")
            return {'CANCELLED'}
        if not self.name:
            self.name = system.name
        return context.window_manager.invoke_props_dialog(self)

    def execute(self, context):
        s = settings_of(context.scene)
        systems = list(s.scatter_systems) if self.all_systems else [_active_system(context)]
        systems = [sy for sy in systems if sy is not None]
        if not systems or not self.name.strip():
            self.report({'ERROR'}, "Nothing to save")
            return {'CANCELLED'}
        try:
            stem = save_scatter_preset(context.scene, systems, self.name.strip(), self.thumbnail)
        except (ValueError, OSError, RuntimeError) as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        try:
            context.window_manager.so_scatter_preset = stem
        except TypeError:
            pass
        self.report({'INFO'}, "Saved preset '%s'" % stem)
        return {'FINISHED'}


class SCENEOPT_OT_scatter_preset_add(Operator):
    bl_idname = "scene_optimizer.scatter_preset_add"
    bl_label = "Add to Active Mesh"
    bl_description = "Create the selected preset's scatter system(s) on the active mesh"
    bl_options = {'REGISTER', 'UNDO'}
    preset: StringProperty(name="Preset", default="", description="Library file name (empty: browser)")

    def execute(self, context):
        surface = context.active_object
        if surface is None or surface.type != 'MESH':
            self.report({'ERROR'}, "Make the surface (terrain) mesh the active object")
            return {'CANCELLED'}
        stem = self.preset or context.window_manager.so_scatter_preset
        if not stem or stem == "NONE":
            self.report({'ERROR'}, "Pick a preset")
            return {'CANCELLED'}
        try:
            made = add_scatter_preset(context.scene, stem, surface)
        except (ValueError, OSError, KeyError) as exc:
            self.report({'ERROR'}, "Could not load preset: %s" % exc)
            return {'CANCELLED'}
        self.report({'INFO'}, "Added %s on %s" % (", ".join(made), surface.name))
        return {'FINISHED'}


class SCENEOPT_OT_scatter_preset_delete(Operator):
    bl_idname = "scene_optimizer.scatter_preset_delete"
    bl_label = "Delete Preset"
    bl_description = "Delete the selected preset's files from the library folder"

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        stem = context.window_manager.so_scatter_preset
        if not stem or stem == "NONE":
            return {'CANCELLED'}
        n = delete_scatter_preset(context.scene, stem)
        self.report({'INFO'}, "Deleted %d file(s) of '%s'" % (n, stem))
        return {'FINISHED'}


class SCENEOPT_OT_scatter_library_open(Operator):
    bl_idname = "scene_optimizer.scatter_library_open"
    bl_label = "Open Library Folder"

    def execute(self, context):
        bpy.ops.wm.path_open(filepath=library_dir(context.scene))
        return {'FINISHED'}


# ---------------------------------------------------------------- UI

class SCENEOPT_UL_scatter_systems(UIList):
    def draw_item(self, _context, layout, _data, item, _icon, _active, _prop):
        row = layout.row(align=True)
        row.prop(item, "name", text="", emboss=False, icon='OUTLINER_OB_POINTCLOUD')
        sub = row.row(align=True)
        sub.alignment = 'RIGHT'
        sub.label(text="%d asset%s" % (len(item.assets), "" if len(item.assets) == 1 else "s"))
        sub.prop(item, "show_viewport", text="", emboss=False,
                 icon='RESTRICT_VIEW_OFF' if item.show_viewport else 'RESTRICT_VIEW_ON')
        sub.prop(item, "show_render", text="", emboss=False,
                 icon='RESTRICT_RENDER_OFF' if item.show_render else 'RESTRICT_RENDER_ON')


class SCENEOPT_UL_scatter_assets(UIList):
    def draw_item(self, _context, layout, _data, item, _icon, _active, _prop):
        n = sum(1 for i in range(SCATTER_MAX_LODS) if getattr(item, "lod%d" % i) is not None)
        row = layout.row(align=True)
        row.prop(item, "name", text="", emboss=False, icon='MESH_DATA')
        row.label(text="%d LOD%s" % (n, "" if n == 1 else "s"))
        row.prop(item, "weight", text="")
        row.prop(item, "show", text="", emboss=False,
                 icon='HIDE_OFF' if item.show else 'HIDE_ON')


class SCENEOPT_PT_scatter(SCENEOPT_PT_base, Panel):
    bl_label = "Scatter Systems"
    bl_idname = "SCENEOPT_PT_scatter"

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        s = settings_of(scene)
        row = layout.row()
        row.template_list("SCENEOPT_UL_scatter_systems", "", s, "scatter_systems",
                          s, "scatter_index", rows=4)
        side = row.column(align=True)
        side.operator("scene_optimizer.scatter_add", text="", icon='ADD')
        side.operator("scene_optimizer.scatter_remove", text="", icon='REMOVE')
        side.separator()
        side.operator("scene_optimizer.scatter_duplicate", text="", icon='DUPLICATE')
        side.operator("scene_optimizer.scatter_move", text="", icon='TRIA_UP').direction = 'UP'
        side.operator("scene_optimizer.scatter_move", text="", icon='TRIA_DOWN').direction = 'DOWN'
        side.separator()
        side.operator("scene_optimizer.scatter_rebuild", text="", icon='FILE_REFRESH')
        system = _active_system(context)
        if system is None:
            layout.label(text="Select assets, then the ground (active), then +", icon='INFO')
            return
        col = layout.column(align=True)
        col.prop(system, "surface")
        col.prop(system, "extra_surfaces")
        if scene.camera is None:
            layout.label(text="No scene camera: LOD and culling inactive", icon='ERROR')
        row = layout.row(align=True)
        row.operator("scene_optimizer.scatter_stats", icon='INFO')
        if system.stats:
            col = layout.column(align=True)
            for part in system.stats.split(" | "):
                col.label(text=part)


class _ScatterSub(SCENEOPT_PT_base):
    bl_parent_id = "SCENEOPT_PT_scatter"
    bl_options = {'DEFAULT_CLOSED'}

    @classmethod
    def poll(cls, context):
        return _active_system(context) is not None


class SCENEOPT_PT_scatter_library(SCENEOPT_PT_base, Panel):
    bl_label = "Preset Library"
    bl_parent_id = "SCENEOPT_PT_scatter"

    def draw(self, context):
        layout = self.layout
        wm = context.window_manager
        layout.template_icon_view(wm, "so_scatter_preset", show_labels=True, scale=6.0,
                                  scale_popup=5.0)
        row = layout.row(align=True)
        row.scale_y = 1.3
        row.operator("scene_optimizer.scatter_preset_add", icon='ADD')
        row = layout.row(align=True)
        row.operator("scene_optimizer.scatter_preset_save", icon='FILE_TICK')
        row.operator("scene_optimizer.scatter_preset_delete", text="", icon='TRASH')
        row.operator("scene_optimizer.scatter_library_open", text="", icon='FILE_FOLDER')
        layout.prop(settings_of(context.scene), "scatter_library", text="Folder")


class SCENEOPT_PT_scatter_assets(_ScatterSub, Panel):
    bl_label = "Assets & LODs"
    bl_options = set()

    def draw(self, context):
        layout = self.layout
        system = _active_system(context)
        row = layout.row()
        row.template_list("SCENEOPT_UL_scatter_assets", "", system, "assets",
                          system, "asset_index", rows=3)
        side = row.column(align=True)
        side.operator("scene_optimizer.scatter_asset_add", text="", icon='ADD')
        side.operator("scene_optimizer.scatter_asset_remove", text="", icon='REMOVE')
        if 0 <= system.asset_index < len(system.assets):
            a = system.assets[system.asset_index]
            col = layout.column(align=True)
            for i in range(SCATTER_MAX_LODS):
                col.prop(a, "lod%d" % i)
            layout.prop(a, "weight")
        layout.operator("scene_optimizer.scatter_hide_sources", icon='HIDE_ON')


class SCENEOPT_PT_scatter_distribution(_ScatterSub, Panel):
    bl_label = "Distribution"

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        system = _active_system(context)
        col = layout.column(align=True)
        col.prop(system, "density")
        col.prop(system, "viewport_density")
        col.prop(system, "seed")
        layout.prop(system, "distribution", expand=True)
        if system.distribution == 'EVEN':
            layout.prop(system, "min_distance")
        if system.surface is not None:
            layout.prop_search(system, "density_mask", system.surface, "vertex_groups")
        else:
            layout.prop(system, "density_mask")
        col = layout.column(align=True)
        col.prop(system, "max_slope")
        col.prop(system, "slope_blur")
        col = layout.column(align=True, heading="Patches")
        col.prop(system, "patch_size")
        col.prop(system, "patch_amount")
        col.prop(system, "patch_scale")
        col = layout.column(align=True)
        col.prop(system, "avoid")
        sub = col.column(align=True)
        sub.active = system.avoid is not None
        sub.prop(system, "avoid_distance")
        sub.prop(system, "avoid_blur")
        layout.prop(system, "edge_offset")


class SCENEOPT_PT_scatter_transform(_ScatterSub, Panel):
    bl_label = "Scale & Rotation"

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        system = _active_system(context)
        col = layout.column(align=True)
        col.prop(system, "scale_min")
        col.prop(system, "scale_max")
        col = layout.column(align=True)
        col.prop(system, "random_rotation")
        col.prop(system, "align_amount")
        col.prop(system, "random_tilt")
        col = layout.column(align=True)
        col.prop(system, "lean_angle")
        sub = col.column(align=True)
        sub.active = system.lean_angle > 0.0
        sub.prop(system, "lean_direction")


class SCENEOPT_PT_scatter_color(_ScatterSub, Panel):
    bl_label = "Color Variation"

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        system = _active_system(context)
        col = layout.column(align=True)
        col.prop(system, "dryness")
        col.prop(system, "dry_patchiness")
        layout.operator("scene_optimizer.scatter_variation", icon='NODE_MATERIAL')
        col = layout.column(align=True)
        col.scale_y = 0.8
        col.label(text="Materials read so_random / so_dry / so_patch")
        col.label(text="(Attribute node, type Instancer).")


class SCENEOPT_PT_scatter_lod(_ScatterSub, Panel):
    bl_label = "Camera LOD & Distance"

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        system = _active_system(context)
        row = layout.row(heading="Show LOD", align=True)
        for i in range(SCATTER_MAX_LODS):
            row.prop(system, "lod_show", index=i, text=str(i), toggle=True)
        layout.prop(system, "lod_colors", icon='COLOR')
        if system.lod_colors:
            draw_lod_legend(layout, context, SCATTER_MAX_LODS)
        col = layout.column(align=True)
        for i in range(3):
            col.prop(system, "lod_distances", index=i, text="LOD%d from" % (i + 1))
        col = layout.column(align=True)
        col.prop(system, "max_distance")
        col.prop(system, "fade")
        col.prop(system, "viewport_distance")
        col = layout.column(align=True)
        col.prop(system, "thin_start")
        col.prop(system, "thin_keep")
        col.prop(system, "thin_compensate")


class SCENEOPT_PT_scatter_culling(_ScatterSub, Panel):
    bl_label = "Culling"

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        system = _active_system(context)
        layout.prop(system, "cull_in")
        layout.prop(system, "frustum")
        sub = layout.column()
        sub.active = system.frustum
        sub.prop(system, "margin")
        layout.prop(system, "occlusion")
        sub = layout.column()
        sub.active = system.occlusion
        sub.prop(system, "occluders")


SCATTER_CLASSES = (
    SCENEOPT_OT_scatter_add,
    SCENEOPT_OT_scatter_remove,
    SCENEOPT_OT_scatter_duplicate,
    SCENEOPT_OT_scatter_move,
    SCENEOPT_OT_scatter_asset_add,
    SCENEOPT_OT_scatter_asset_remove,
    SCENEOPT_OT_scatter_rebuild,
    SCENEOPT_OT_scatter_stats,
    SCENEOPT_OT_scatter_hide_sources,
    SCENEOPT_OT_scatter_variation,
    SCENEOPT_OT_scatter_preset_save,
    SCENEOPT_OT_scatter_preset_add,
    SCENEOPT_OT_scatter_preset_delete,
    SCENEOPT_OT_scatter_library_open,
    SCENEOPT_UL_scatter_systems,
    SCENEOPT_UL_scatter_assets,
    SCENEOPT_PT_scatter,
    SCENEOPT_PT_scatter_library,
    SCENEOPT_PT_scatter_assets,
    SCENEOPT_PT_scatter_distribution,
    SCENEOPT_PT_scatter_transform,
    SCENEOPT_PT_scatter_color,
    SCENEOPT_PT_scatter_lod,
    SCENEOPT_PT_scatter_culling,
)


# settings gain the scatter list (declared here because the property groups
# above are defined after SCENEOPT_PG_settings)
SCENEOPT_PG_settings.__annotations__["scatter_systems"] = CollectionProperty(
    type=SCENEOPT_PG_scatter_system)
SCENEOPT_PG_settings.__annotations__["scatter_index"] = IntProperty(default=0)
SCENEOPT_PG_settings.__annotations__["scatter_library"] = StringProperty(
    name="Library Folder", subtype='DIR_PATH', default="",
    description="Folder for scatter presets (empty: Blender's user presets folder)")


# =============================================================================
# 15. Registration
# =============================================================================

classes = (
    SCENEOPT_PG_scatter_asset,
    SCENEOPT_PG_scatter_system,
    SCENEOPT_PG_lod_variant,
    SCENEOPT_PG_lod_group,
    SCENEOPT_PG_result,
    SCENEOPT_PG_settings,
    SCENEOPT_OT_preview,
    SCENEOPT_OT_apply,
    SCENEOPT_OT_restore,
    SCENEOPT_OT_clear_overlay,
    SCENEOPT_OT_select_results,
    SCENEOPT_OT_lod_apply,
    SCENEOPT_OT_lod_group_from_selected,
    SCENEOPT_OT_lod_autodetect,
    SCENEOPT_OT_lod_group_remove,
    SCENEOPT_OT_lod_variant_add,
    SCENEOPT_OT_lod_variant_remove,
    SCENEOPT_OT_lod_variant_move,
    SCENEOPT_OT_lod_snap_variants,
    SCENEOPT_OT_lod_distances_to_all,
    SCENEOPT_OT_select_group,
    SCENEOPT_UL_results,
    SCENEOPT_UL_lod_groups,
    SCENEOPT_UL_lod_variants,
    SCENEOPT_PT_main,
    SCENEOPT_PT_scope,
    SCENEOPT_PT_frustum,
    SCENEOPT_PT_occlusion,
    SCENEOPT_PT_lod,
    SCENEOPT_PT_results,
) + SCATTER_CLASSES

_HANDLERS = (
    (bpy.app.handlers.depsgraph_update_post, _on_depsgraph_update),
    (bpy.app.handlers.frame_change_post, _on_frame_change),
    (bpy.app.handlers.load_post, _on_load_post),
)


def _remove_handlers():
    for lst, fn in _HANDLERS:
        for h in list(lst):
            if getattr(h, "__name__", "") == fn.__name__ and \
                    getattr(h, "__module__", "") == fn.__module__:
                lst.remove(h)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.scene_optimizer = PointerProperty(type=SCENEOPT_PG_settings)
    bpy.types.WindowManager.so_scatter_preset = EnumProperty(
        name="Scatter Preset", items=scatter_preset_items,
        description="Scatter presets in the library folder")
    bpy.types.Object.so_never_cull = BoolProperty(
        name="Never Cull", default=False,
        description="Scene Optimizer: never hide this object by frustum/occlusion culling")
    bpy.types.Object.so_not_occluder = BoolProperty(
        name="Not an Occluder", default=False,
        description="Scene Optimizer: this object never blocks visibility of others")
    _remove_handlers()
    for lst, fn in _HANDLERS:
        lst.append(fn)
    if not bpy.app.background:
        _Runtime.draw_handles = [
            bpy.types.SpaceView3D.draw_handler_add(_draw_overlay_3d, (), 'WINDOW', 'POST_VIEW'),
            bpy.types.SpaceView3D.draw_handler_add(_draw_overlay_2d, (), 'WINDOW', 'POST_PIXEL'),
        ]


def unregister():
    # Leave the scene as the user made it: undo everything we hid.
    try:
        _Runtime.busy = True
        Visibility.restore(ALL_REASONS)
        for sc in bpy.data.scenes:          # remove the LOD colour modifiers
            object_lod_colors(sc, False)
            sc.scene_optimizer["lod_colors"] = False
    except Exception as exc:
        _log("Restore on unregister failed: %s" % exc)
    finally:
        _Runtime.busy = False
    for h in _Runtime.draw_handles:
        try:
            bpy.types.SpaceView3D.draw_handler_remove(h, 'WINDOW')
        except (ValueError, RuntimeError):
            pass
    _Runtime.draw_handles = []
    if bpy.app.timers.is_registered(_lod_timer):
        bpy.app.timers.unregister(_lod_timer)
    _remove_handlers()
    del bpy.types.Object.so_not_occluder
    del bpy.types.Object.so_never_cull
    del bpy.types.Scene.scene_optimizer
    del bpy.types.WindowManager.so_scatter_preset
    if _Previews.pcoll is not None:
        bpy.utils.previews.remove(_Previews.pcoll)
        _Previews.pcoll, _Previews.items, _Previews.stamp = None, [], None
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
