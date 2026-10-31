#!/usr/bin/env python3
"""
Morpheus Lab connector for Blender.

Two ways to use this one file:
  1. Test without Blender (any Python 3.8+):   python morpheus_connector.py --mock
  2. Real use: in Blender, Edit > Preferences > Add-ons > Install from disk, pick this file,
     tick it on, then open the 3D view sidebar (press N) > Morpheus tab > Start.

The connector listens only on this computer (127.0.0.1) and every request needs the token.
Python scripts from the AI stay blocked until you switch "Allow scripts" on yourself.
"""
bl_info = {
    "name": "Morpheus Lab Connector",
    "author": "Noven Empire",
    "version": (1, 0, 0),
    "blender": (3, 6, 0),
    "location": "3D View > Sidebar > Morpheus",
    "description": "Lets Morpheus Lab look at and edit the open scene over a local, token-protected connection.",
    "category": "Interface",
}

import argparse, base64, contextlib, hmac, io, json, math, os, queue, re, secrets
import struct, sys, tempfile, threading, time, zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    import bpy
except ImportError:
    bpy = None

VERSION = "1.0.0"
CFG = {"token": "", "allow_scripts": False,
       "workdir": os.path.join(os.path.expanduser("~"), "MorpheusExports")}
ACTIONS = ["ping", "get_scene_info", "create_object", "modify_object", "set_material",
           "add_keyframes", "render_preview", "save_project", "export_scene", "run_script"]


class ConnErr(Exception):
    def __init__(self, code, msg):
        Exception.__init__(self, msg)
        self.code, self.msg = code, msg


# ---------- argument checks ----------
def vec(a, key, n=3, required=False):
    v = a.get(key)
    if v is None:
        if required:
            raise ConnErr("bad_args", "'%s' is required." % key)
        return None
    if not isinstance(v, (list, tuple)) or len(v) != n or not all(isinstance(x, (int, float)) for x in v):
        raise ConnErr("bad_args", "'%s' must be a list of %d numbers." % (key, n))
    return [float(x) for x in v]


def text(a, key, required=False, default=None):
    v = a.get(key, default)
    if v is None or v == "":
        if required:
            raise ConnErr("bad_args", "'%s' is required." % key)
        return default
    if not isinstance(v, str):
        raise ConnErr("bad_args", "'%s' must be text." % key)
    return v


def color(c):
    if isinstance(c, str) and re.fullmatch(r"#?[0-9a-fA-F]{6}", c):
        c = c.lstrip("#")
        return [int(c[i:i + 2], 16) / 255.0 for i in (0, 2, 4)]
    if isinstance(c, (list, tuple)) and len(c) == 3 and all(isinstance(x, (int, float)) for x in c):
        return [min(1.0, max(0.0, float(x))) for x in c]
    raise ConnErr("bad_args", "'color' must be a hex like #ff8800 or three numbers from 0 to 1.")


def safe_path(name, ext):
    base = re.sub(r"[^A-Za-z0-9_.-]", "_", (name or "scene"))[:60] or "scene"
    if not base.lower().endswith(ext):
        base += ext
    os.makedirs(CFG["workdir"], exist_ok=True)
    return os.path.join(CFG["workdir"], base)


def r3(v):
    return [round(x, 3) for x in v]


# ---------- the real thing, inside Blender ----------
class BlenderBackend:
    def ping(self, a):
        return {"pong": True}

    def _obj(self, name):
        o = bpy.data.objects.get(name)
        if o is None:
            raise ConnErr("not_found", "No object named '%s'. Use get_scene_info to see the names." % name)
        return o

    def _info(self, o):
        return {"name": o.name, "type": o.type, "location": r3(o.location),
                "rotation_degrees": [round(math.degrees(x), 1) for x in o.rotation_euler],
                "scale": r3(o.scale), "visible": not o.hide_viewport}

    def get_scene_info(self, a):
        sc = bpy.context.scene
        objs = [self._info(o) for o in list(sc.objects)[:200]]
        return {"scene": sc.name, "object_count": len(sc.objects), "objects": objs,
                "frame_start": sc.frame_start, "frame_end": sc.frame_end,
                "render_engine": sc.render.engine, "file": bpy.data.filepath or "(not saved yet)"}

    def create_object(self, a):
        kind = text(a, "type", True).lower()
        loc = vec(a, "location") or [0, 0, 0]
        ops = bpy.ops.mesh
        table = {"cube": ops.primitive_cube_add, "sphere": ops.primitive_uv_sphere_add,
                 "plane": ops.primitive_plane_add, "cylinder": ops.primitive_cylinder_add,
                 "cone": ops.primitive_cone_add, "torus": ops.primitive_torus_add,
                 "monkey": ops.primitive_monkey_add}
        if kind in table:
            table[kind](location=loc)
        elif kind == "light":
            bpy.ops.object.light_add(type="POINT", location=loc)
        elif kind == "camera":
            bpy.ops.object.camera_add(location=loc)
        elif kind == "empty":
            bpy.ops.object.empty_add(location=loc)
        else:
            raise ConnErr("bad_args", "Unknown type '%s'. Use cube, sphere, plane, cylinder, cone, torus, monkey, light, camera or empty." % kind)
        o = bpy.context.active_object
        if a.get("name"):
            o.name = text(a, "name")
        s = vec(a, "scale")
        if s:
            o.scale = s
        if a.get("color") is not None:
            self.set_material({"name": o.name, "color": a["color"]})
        return self._info(o)

    def modify_object(self, a):
        o = self._obj(text(a, "name", True))
        if a.get("delete") is True:
            n = o.name
            bpy.data.objects.remove(o, do_unlink=True)
            return {"deleted": n}
        loc, rot, sc = vec(a, "location"), vec(a, "rotation_degrees"), vec(a, "scale")
        if loc:
            o.location = loc
        if rot:
            o.rotation_euler = [math.radians(x) for x in rot]
        if sc:
            o.scale = sc
        if isinstance(a.get("visible"), bool):
            o.hide_viewport = not a["visible"]
            o.hide_render = not a["visible"]
        if a.get("new_name"):
            o.name = text(a, "new_name")
        return self._info(o)

    def set_material(self, a):
        o = self._obj(text(a, "name", True))
        col = color(a.get("color"))
        if not hasattr(o.data, "materials"):
            raise ConnErr("bad_args", "'%s' cannot hold a material." % o.name)
        m = bpy.data.materials.new(o.name + "_material")
        m.use_nodes = True
        b = m.node_tree.nodes.get("Principled BSDF")
        if b:
            b.inputs["Base Color"].default_value = (col[0], col[1], col[2], 1.0)
            if isinstance(a.get("metallic"), (int, float)):
                b.inputs["Metallic"].default_value = float(a["metallic"])
            if isinstance(a.get("roughness"), (int, float)):
                b.inputs["Roughness"].default_value = float(a["roughness"])
        o.data.materials.clear()
        o.data.materials.append(m)
        return {"name": o.name, "material": m.name}

    def add_keyframes(self, a):
        o = self._obj(text(a, "name", True))
        fr = a.get("frames")
        if not isinstance(fr, list) or not fr or len(fr) > 100:
            raise ConnErr("bad_args", "'frames' must be a list of 1 to 100 keyframes.")
        last = 0
        for k in fr:
            f = int(k.get("frame", 1))
            loc, rot, sc = vec(k, "location"), vec(k, "rotation_degrees"), vec(k, "scale")
            if loc:
                o.location = loc
                o.keyframe_insert("location", frame=f)
            if rot:
                o.rotation_euler = [math.radians(x) for x in rot]
                o.keyframe_insert("rotation_euler", frame=f)
            if sc:
                o.scale = sc
                o.keyframe_insert("scale", frame=f)
            last = max(last, f)
        sc_ = bpy.context.scene
        if last > sc_.frame_end:
            sc_.frame_end = last
        return {"name": o.name, "keyframes": len(fr), "frame_end": sc_.frame_end}

    def render_preview(self, a):
        w = min(1280, max(64, int(a.get("width", 640))))
        h = min(1280, max(64, int(a.get("height", 360))))
        sc = bpy.context.scene
        if sc.camera is None:
            raise ConnErr("no_camera", "The scene has no camera. Create one first with create_object type camera.")
        old = (sc.render.resolution_x, sc.render.resolution_y, sc.render.resolution_percentage, sc.render.filepath)
        path = os.path.join(tempfile.gettempdir(), "morpheus_preview.png")
        try:
            sc.render.resolution_x, sc.render.resolution_y, sc.render.resolution_percentage = w, h, 100
            sc.render.filepath = path
            bpy.ops.render.render(write_still=True)
        finally:
            sc.render.resolution_x, sc.render.resolution_y, sc.render.resolution_percentage, sc.render.filepath = old
        data = open(path, "rb").read()
        if len(data) > 1500000:
            raise ConnErr("too_big", "The preview is too large. Try a smaller width and height.")
        return {"width": w, "height": h, "png_base64": base64.b64encode(data).decode()}

    def save_project(self, a):
        p = safe_path(a.get("filename") or "scene", ".blend")
        bpy.ops.wm.save_as_mainfile(filepath=p, copy=True)
        return {"saved_to": p}

    def export_scene(self, a):
        fmt = (text(a, "format", default="glb") or "glb").lower()
        p = safe_path(a.get("filename") or "scene", "." + fmt)
        if fmt == "glb":
            bpy.ops.export_scene.gltf(filepath=p, export_format="GLB")
        elif fmt == "fbx":
            bpy.ops.export_scene.fbx(filepath=p)
        elif fmt == "obj":
            (bpy.ops.wm.obj_export if hasattr(bpy.ops.wm, "obj_export") else bpy.ops.export_scene.obj)(filepath=p)
        else:
            raise ConnErr("bad_args", "format must be glb, fbx or obj.")
        return {"exported_to": p}

    def run_script(self, a):
        if not CFG["allow_scripts"]:
            raise ConnErr("scripts_disabled", "Python scripts are switched off in the connector. The person can turn on 'Allow scripts' in Blender's Morpheus panel.")
        code = text(a, "code", True)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            exec(compile(code, "<morpheus>", "exec"), {"bpy": bpy, "math": math})
        return {"output": buf.getvalue()[-4000:]}


# ---------- a stand-in scene, so the whole chain can be tested without Blender ----------
def make_png(w, h):
    raw = b"".join(b"\x00" + b"".join(bytes([x * 255 // w, y * 255 // h, 160]) for x in range(w)) for y in range(h))
    def chunk(t, d):
        c = struct.pack(">I", len(d)) + t + d
        return c + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


class MockBackend:
    def __init__(self):
        self.objs = {"Cube": {"type": "MESH", "location": [0, 0, 0], "rotation_degrees": [0, 0, 0], "scale": [1, 1, 1], "visible": True}}
        self.frame_end = 250
        self.has_camera = False

    def ping(self, a):
        return {"pong": True}

    def _get(self, name):
        if name not in self.objs:
            raise ConnErr("not_found", "No object named '%s'. Use get_scene_info to see the names." % name)
        return self.objs[name]

    def _info(self, n):
        o = self.objs[n]
        return {"name": n, "type": o["type"], "location": r3(o["location"]), "rotation_degrees": o["rotation_degrees"],
                "scale": r3(o["scale"]), "visible": o["visible"]}

    def get_scene_info(self, a):
        return {"scene": "Scene (mock)", "object_count": len(self.objs), "objects": [self._info(n) for n in self.objs],
                "frame_start": 1, "frame_end": self.frame_end, "render_engine": "MOCK", "file": "(mock scene)"}

    def create_object(self, a):
        kind = text(a, "type", True).lower()
        if kind not in ("cube", "sphere", "plane", "cylinder", "cone", "torus", "monkey", "light", "camera", "empty"):
            raise ConnErr("bad_args", "Unknown type '%s'." % kind)
        name, i = text(a, "name") or kind.capitalize(), 1
        base = name
        while name in self.objs:
            name, i = "%s.%03d" % (base, i), i + 1
        self.objs[name] = {"type": kind.upper(), "location": vec(a, "location") or [0, 0, 0],
                           "rotation_degrees": [0, 0, 0], "scale": vec(a, "scale") or [1, 1, 1], "visible": True}
        if a.get("color") is not None:
            color(a["color"])
        self.has_camera = self.has_camera or kind == "camera"
        return self._info(name)

    def modify_object(self, a):
        n = text(a, "name", True)
        o = self._get(n)
        if a.get("delete") is True:
            del self.objs[n]
            return {"deleted": n}
        for k in ("location", "rotation_degrees", "scale"):
            v = vec(a, k)
            if v:
                o[k] = v
        if isinstance(a.get("visible"), bool):
            o["visible"] = a["visible"]
        if a.get("new_name"):
            self.objs[a["new_name"]] = self.objs.pop(n)
            n = a["new_name"]
        return self._info(n)

    def set_material(self, a):
        n = text(a, "name", True)
        self._get(n)
        color(a.get("color"))
        return {"name": n, "material": n + "_material"}

    def add_keyframes(self, a):
        self._get(text(a, "name", True))
        fr = a.get("frames")
        if not isinstance(fr, list) or not fr or len(fr) > 100:
            raise ConnErr("bad_args", "'frames' must be a list of 1 to 100 keyframes.")
        self.frame_end = max([self.frame_end] + [int(k.get("frame", 1)) for k in fr])
        return {"name": a["name"], "keyframes": len(fr), "frame_end": self.frame_end}

    def render_preview(self, a):
        if not self.has_camera:
            raise ConnErr("no_camera", "The scene has no camera. Create one first with create_object type camera.")
        w, h = 64, 36
        return {"width": w, "height": h, "png_base64": base64.b64encode(make_png(w, h)).decode()}

    def save_project(self, a):
        return {"saved_to": safe_path(a.get("filename") or "scene", ".blend") + " (mock: nothing written)"}

    def export_scene(self, a):
        fmt = (text(a, "format", default="glb") or "glb").lower()
        if fmt not in ("glb", "fbx", "obj"):
            raise ConnErr("bad_args", "format must be glb, fbx or obj.")
        return {"exported_to": safe_path(a.get("filename") or "scene", "." + fmt) + " (mock: nothing written)"}

    def run_script(self, a):
        if not CFG["allow_scripts"]:
            raise ConnErr("scripts_disabled", "Python scripts are switched off in the connector. The person can turn on 'Allow scripts' in Blender's Morpheus panel.")
        raise ConnErr("mock", "Mock mode cannot run Blender scripts.")


BACKEND = None
LOCK = threading.Lock()
Q = queue.Queue()


def _pump():
    try:
        while True:
            fn, box = Q.get_nowait()
            try:
                box["r"] = fn()
            except ConnErr as e:
                box["e"] = e
            except Exception as e:
                box["e"] = ConnErr("blender_error", str(e)[:300])
            box["ev"].set()
    except queue.Empty:
        pass
    return 0.05


def on_main(fn, timeout=120):
    """Blender only allows scene edits from its main thread, so requests wait in a queue for it."""
    if bpy is None:
        with LOCK:
            try:
                return fn()
            except ConnErr:
                raise
            except Exception as e:
                raise ConnErr("mock_error", str(e)[:300])
    box = {"ev": threading.Event()}
    Q.put((fn, box))
    if not box["ev"].wait(timeout):
        raise ConnErr("timeout", "Blender did not answer in time. It may be busy or showing a dialog.")
    if "e" in box:
        raise box["e"]
    return box["r"]


class Handler(BaseHTTPRequestHandler):
    server_version = "MorpheusConnector/" + VERSION

    def log_message(self, *a):
        pass

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", self.headers.get("Origin") or "*")
        self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Headers", "authorization, content-type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Access-Control-Max-Age", "600")

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self._cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _fail(self, code, status, c, m, rid=None):
        self._send(status, {"id": rid, "ok": False, "error": {"code": c, "message": m}})

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        if self.path.split("?")[0] != "/health":
            return self._fail(0, 404, "not_found", "Unknown address.")
        self._send(200, {"ok": True, "name": "morpheus-connector", "version": VERSION,
                         "app": "blender" if bpy else "mock",
                         "blender": bpy.app.version_string if bpy else None,
                         "scripts": CFG["allow_scripts"], "actions": ACTIONS})

    def do_POST(self):
        if self.path.split("?")[0] != "/rpc":
            return self._fail(0, 404, "not_found", "Unknown address.")
        tok = (self.headers.get("Authorization") or "").replace("Bearer ", "", 1)
        if not CFG["token"] or not hmac.compare_digest(tok.encode(), CFG["token"].encode()):
            return self._fail(0, 401, "unauthorized", "Wrong or missing token.")
        n = int(self.headers.get("Content-Length") or 0)
        if n > 1000000:
            return self._fail(0, 413, "too_big", "Request is too large.")
        try:
            req = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return self._fail(0, 400, "bad_json", "The request was not valid JSON.")
        rid, act = req.get("id"), req.get("action")
        args = req.get("args") if isinstance(req.get("args"), dict) else {}
        if act not in ACTIONS:
            return self._fail(0, 200, "unknown_action", "Unknown action '%s'." % act, rid)
        try:
            res = on_main(lambda: getattr(BACKEND, act)(args))
            self._send(200, {"id": rid, "ok": True, "result": res})
        except ConnErr as e:
            self._fail(0, 200, e.code, e.msg, rid)


SRV = None


def start(host="127.0.0.1", port=8765):
    global SRV, BACKEND
    if SRV:
        return
    BACKEND = BlenderBackend() if bpy else MockBackend()
    SRV = ThreadingHTTPServer((host, port), Handler)
    threading.Thread(target=SRV.serve_forever, daemon=True).start()
    if bpy and not bpy.app.timers.is_registered(_pump):
        bpy.app.timers.register(_pump, persistent=True)


def stop():
    global SRV
    if SRV:
        SRV.shutdown()
        SRV.server_close()
        SRV = None
    if bpy and bpy.app.timers.is_registered(_pump):
        bpy.app.timers.unregister(_pump)


# ---------- Blender add-on interface ----------
if bpy:
    class MORPH_Prefs(bpy.types.AddonPreferences):
        bl_idname = __name__
        port: bpy.props.IntProperty(name="Port", default=8765, min=1024, max=65535)
        token: bpy.props.StringProperty(name="Token")
        allow_scripts: bpy.props.BoolProperty(
            name="Allow scripts", default=False,
            description="Let the AI run Python inside Blender. Powerful: only switch on if you trust what you are asking it to do")

        def draw(self, context):
            self.layout.prop(self, "port")
            self.layout.prop(self, "allow_scripts")

    def _prefs():
        return bpy.context.preferences.addons[__name__].preferences

    class MORPH_OT_start(bpy.types.Operator):
        bl_idname, bl_label = "morpheus.start", "Start connector"

        def execute(self, context):
            p = _prefs()
            if not p.token:
                p.token = secrets.token_urlsafe(18)
            CFG["token"], CFG["allow_scripts"] = p.token, p.allow_scripts
            try:
                start("127.0.0.1", p.port)
            except OSError as e:
                self.report({"ERROR"}, "Could not start on port %d: %s" % (p.port, e))
                return {"CANCELLED"}
            return {"FINISHED"}

    class MORPH_OT_stop(bpy.types.Operator):
        bl_idname, bl_label = "morpheus.stop", "Stop connector"

        def execute(self, context):
            stop()
            return {"FINISHED"}

    class MORPH_OT_copy(bpy.types.Operator):
        bl_idname, bl_label = "morpheus.copy_token", "Copy token"

        def execute(self, context):
            context.window_manager.clipboard = _prefs().token
            self.report({"INFO"}, "Token copied")
            return {"FINISHED"}

    class MORPH_PT_panel(bpy.types.Panel):
        bl_label, bl_idname = "Morpheus Lab", "MORPH_PT_panel"
        bl_space_type, bl_region_type, bl_category = "VIEW_3D", "UI", "Morpheus"

        def draw(self, context):
            p, col = _prefs(), self.layout.column()
            if SRV:
                col.label(text="Running on port %d" % p.port)
                col.operator("morpheus.copy_token")
                col.operator("morpheus.stop")
            else:
                col.label(text="Stopped")
                col.prop(p, "allow_scripts")
                col.operator("morpheus.start")

    CLASSES = (MORPH_Prefs, MORPH_OT_start, MORPH_OT_stop, MORPH_OT_copy, MORPH_PT_panel)

    def register():
        for c in CLASSES:
            bpy.utils.register_class(c)

    def unregister():
        stop()
        for c in reversed(CLASSES):
            bpy.utils.unregister_class(c)


def main():
    ap = argparse.ArgumentParser(description="Morpheus Lab connector (test mode, no Blender needed)")
    ap.add_argument("--mock", action="store_true", help="use a fake scene instead of Blender")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1", help="use 0.0.0.0 only if you must reach it from another device")
    ap.add_argument("--token", default="")
    ap.add_argument("--allow-scripts", action="store_true")
    ap.add_argument("--workdir", default=CFG["workdir"])
    a = ap.parse_args()
    if bpy:
        print("Install this file as a Blender add-on instead of running it directly.")
        return
    if not a.mock:
        print("Blender was not found. Run with --mock to test without it, or install this file as a Blender add-on.")
        return
    CFG.update(token=a.token or secrets.token_urlsafe(18), allow_scripts=a.allow_scripts, workdir=a.workdir)
    start(a.host, a.port)
    print("Morpheus connector (mock scene) is running.")
    print("Address: http://%s:%d" % ("localhost" if a.host == "127.0.0.1" else a.host, a.port))
    print("Token:   " + CFG["token"])
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        stop()


if __name__ == "__main__":
    main()
