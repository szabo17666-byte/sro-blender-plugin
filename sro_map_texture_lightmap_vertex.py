bl_info = {
    "name": "Silkroad Map - Project UI",
    "author": "szabo176",
    "version": (6, 8, 0),
    "blender": (4, 5, 0),
    "location": "3D View > Sidebar > SRO Project",
    "description": "Import Silkroad map with terrain, textures, lightmaps, vertex brightness and optimized water.",
    "category": "Import-Export",
}

import bpy, os, re, time, math, struct, tempfile, array
from bpy.types import Operator, Panel, PropertyGroup
from bpy.props import StringProperty, BoolProperty, PointerProperty, EnumProperty
from collections import defaultdict, Counter
from mathutils import Vector

REGION_SIZE = 1920.0
VERTS_PER_AXIS = 97
BLOCKS_PER_AXIS = 6
BLOCK_SIZE = REGION_SIZE / BLOCKS_PER_AXIS
TEXTURE_TILING_FACTOR = 64.0
WATER_TILING_FACTOR = 16.0

PARSED_REGIONS = []
NAMED_REGIONS_DATA = {}
REGION_ORIENT = "ZX"
TERRAIN_DDJ = []
TEXTURE_CACHE = {}
LIGHTMAP_CACHE = {}
MATERIAL_CACHE = {}
WATER_TEX_CACHE = {}
WATER_MAT_CACHE = {}
MAX_WARN = 20
WARN_CNT = 0
TEMP_DIR = os.path.join(tempfile.gettempdir(), "sro_importer_cache")

def _banner():
    print("\n" * 5, end="")
    time.sleep(0.2)
    v = bl_info["version"]
    print(f"[{bl_info['name']}] v{v[0]}.{v[1]}.{v[2]} loaded.")
    os.makedirs(TEMP_DIR, exist_ok=True)
    print(f"[INFO] Cache directory set to: {TEMP_DIR}")

def validate_root(path: str) -> bool:
    if not path: return False
    for r in ("Data", "Music", "Map", "Media"):
        if not os.path.isdir(os.path.join(path, r)): return False
    return os.path.isfile(os.path.join(path, "Map", "mapinfo.mfo"))

def parse_mfo(mfo_path: str):
    with open(mfo_path, "rb") as f:
        if not f.read(12).startswith(b"JMXVMFO"): raise ValueError("Not a JMXVMFO file.")
        f.read(4); f.read(8); bits = f.read(8192)
    regs = []
    for i, b in enumerate(bits):
        if b == 0: continue
        for k in range(8):
            if (b >> k) & 1:
                idx = i * 8 + k
                x = idx & 0xFF
                zr = (idx >> 8) & 0xFF
                z = zr & 0x7F
                regs.append((x, z, 1 if zr >= 128 else 0))
    print(f"[INFO] MFO OK: {len(regs)} active regions found.")
    return regs

def find_res(map_root, a, b, ext):
    aS, bS, a3, b3 = str(a), str(b), f"{a:03d}", f"{b:03d}"
    for p in (os.path.join(map_root, aS, bS + ext),
              os.path.join(map_root, aS, b3 + ext),
              os.path.join(map_root, a3, bS + ext),
              os.path.join(map_root, a3, b3 + ext)):
        if os.path.isfile(p): return p
    return None

def detect_orient(map_root, regs):
    test = regs[:64] if len(regs) > 64 else regs
    xz = sum(1 for (x, z, db) in test if find_res(map_root, x, z, ".m"))
    zx = sum(1 for (x, z, db) in test if find_res(map_root, z, x, ".m"))
    mode = "XZ" if xz >= zx else "ZX"
    print(f"[INFO] Orientation detected: {mode} (Hits: XZ={xz}, ZX={zx})")
    return mode

def region_center_world(rx, rz, orient):
    gx, gy = (rx, rz) if orient == "XZ" else (rz, rx)
    half = REGION_SIZE * 0.5
    return gx * REGION_SIZE + half, -(gy * REGION_SIZE + half)

def parse_regioninfo(path):
    out = {}
    if not os.path.isfile(path): return out
    cur = None
    try:
        with open(path, 'r', encoding='utf-8', errors='ignore') as f:
            for line in f:
                s = line.strip()
                if not s: continue
                if s.startswith('#'):
                    parts = re.split(r'\s+', s)
                    if len(parts) > 1:
                        cur = parts[1]
                        out.setdefault(cur, [])
                    continue
                if cur:
                    try:
                        xs, zs, *_ = re.split(r'\s+', s)
                        x, z = int(xs), int(zs)
                        if (x, z) not in out[cur]: out[cur].append((x, z))
                    except (ValueError, IndexError):
                        pass
    except Exception as e:
        print(f"[ERROR] Could not parse regioninfo.txt: {e}")
    return {k: v for k, v in out.items() if v}

def read_mapm_data(path_m: str):
    H = [[0.0] * VERTS_PER_AXIS for _ in range(VERTS_PER_AXIS)]
    T = [[0] * VERTS_PER_AXIS for _ in range(VERTS_PER_AXIS)]
    B = [[255] * VERTS_PER_AXIS for _ in range(VERTS_PER_AXIS)]
    W = []
    with open(path_m, "rb") as f:
        if not f.read(12).startswith(b"JMXVMAPM"): raise ValueError(f"Not a JMXVMAPM file: {os.path.basename(path_m)}")
        for zb in range(BLOCKS_PER_AXIS):
            for xb in range(BLOCKS_PER_AXIS):
                f.read(4)
                f.read(2)
                for vz in range(17):
                    for vx in range(17):
                        h = struct.unpack("<f", f.read(4))[0]
                        tex = struct.unpack("<H", f.read(2))[0]
                        bright = struct.unpack("<B", f.read(1))[0]
                        gx = xb * 16 + vx
                        gz = zb * 16 + vz
                        if gx < VERTS_PER_AXIS and gz < VERTS_PER_AXIS:
                            H[gz][gx] = h
                            T[gz][gx] = tex
                            B[gz][gx] = bright
                wtype, wwave, wheight = struct.unpack("<bBf", f.read(1 + 1 + 4))
                f.read(16 * 16 * 2)
                f.read(4 + 4 + 20)
                if wtype >= 0:
                    W.append((xb, zb, wheight, wtype, wwave))
    return H, T, B, W

def read_mapt_lightmap(path_t: str):
    print(f"    [INFO] Reading lightmap file: {os.path.basename(path_t)}")
    try:
        with open(path_t, "rb") as f:
            if not f.read(12).startswith(b"JMXVMAPT"):
                print(f"    [WARN] Not a JMXVMAPT file or unknown version: {os.path.basename(path_t)}")
                return None
            f.seek(9216, 1)
            buffer_size = struct.unpack("<I", f.read(4))[0]
            f.read(4)
            if buffer_size > 0:
                print(f"    [INFO] Lightmap DDS buffer found, size: {buffer_size} bytes.")
                return f.read(buffer_size)
            else:
                print(f"    [WARN] Lightmap file contains no DDS data.")
                return None
    except Exception as e:
        print(f"    [ERROR] Failed to read lightmap file {os.path.basename(path_t)}: {e}")
        return None

def decode_vertex_tex(vtex: int):
    tid = vtex & 0x03FF
    scl = (vtex >> 10) & 0x3F
    return tid, scl

def parse_tile2d_ifo(root):
    cands = [os.path.join(root, "Data", "tile2d.ifo"), os.path.join(root, "Map", "tile2d.ifo")]
    path = next((p for p in cands if os.path.isfile(p)), None)
    if not path:
        print(f"[WARN] tile2d.ifo not found.")
        return []
    txt = ""
    for enc in ("cp1250", "cp949", "utf-8", "latin-1"):
        try:
            with open(path, 'r', encoding=enc) as f:
                txt = f.read()
            break
        except UnicodeDecodeError:
            continue
    if not txt:
        print(f"[WARN] Could not decode tile2d.ifo with any known encoding.")
        return []
    lines = [l.strip() for l in txt.splitlines() if l.strip()]
    if len(lines) < 3: return []
    out = []
    for l in lines[2:]:
        m = re.search(r'"([^"]+\.ddj)"', l, re.IGNORECASE)
        if m:
            fn = m.group(1).replace("\\", "/")
            if "/" not in fn: fn = "tile2d/" + os.path.basename(fn)
            out.append(os.path.normpath(fn))
    print(f"[INFO] Loaded {len(out)} texture paths from tile2d.ifo.")
    return out

def load_ddj_image(sro_root: str, rel_path: str, cache: dict):
    global WARN_CNT
    if not rel_path: return None
    candidate_paths = [
        os.path.join(sro_root, rel_path),
        os.path.join(sro_root, "Map", rel_path)
    ]
    ddj_path = next((p for p in candidate_paths if os.path.exists(p)), None)
    if not ddj_path:
        if WARN_CNT < MAX_WARN:
            print(f"    [WARN] Texture not found: {rel_path}")
            WARN_CNT += 1
        return None
    key = os.path.normcase(ddj_path)
    if key in cache: return cache[key]
    try:
        with open(ddj_path, "rb") as f: data = f.read()
        if len(data) < 20: return None
        dds_data = data[20:]
        temp_path = os.path.join(TEMP_DIR, f"tex_{os.path.basename(ddj_path)}.{int(os.path.getmtime(ddj_path))}.dds")
        if not os.path.exists(temp_path):
            with open(temp_path, "wb") as w: w.write(dds_data)
        img = bpy.data.images.load(temp_path, check_existing=True)
        cache[key] = img
        return img
    except Exception as e:
        print(f"    [ERROR] Failed to load DDJ image: {os.path.basename(ddj_path)} :: {e}")
        return None

def load_dds_from_data(dds_data, name: str):
    key = name
    if key in LIGHTMAP_CACHE: return LIGHTMAP_CACHE[key]
    try:
        temp_path = os.path.join(TEMP_DIR, f"lm_{name}.dds")
        if not os.path.exists(temp_path):
            with open(temp_path, "wb") as w: w.write(dds_data)
        img = bpy.data.images.load(temp_path, check_existing=True)
        img.colorspace_settings.name = 'Non-Color'
        LIGHTMAP_CACHE[key] = img
        print(f"    [INFO] Successfully loaded lightmap image '{name}' from DDS data.")
        return img
    except Exception as e:
        print(f"    [ERROR] Failed to load DDS from data for '{name}': {e}")
        return None

def ensure_collection(name: str):
    coll = bpy.data.collections.get(name)
    if not coll:
        coll = bpy.data.collections.new(name)
        bpy.context.scene.collection.children.link(coll)
    return coll

def create_grid_object(name: str, cx: float, cy: float):
    bpy.ops.mesh.primitive_grid_add(x_subdivisions=VERTS_PER_AXIS - 1, y_subdivisions=VERTS_PER_AXIS - 1, size=REGION_SIZE, align='WORLD', enter_editmode=False, location=(cx, cy, 0.0))
    obj = bpy.context.active_object
    obj.name = name
    return obj

def apply_heights(obj, H):
    me = obj.data
    if len(me.vertices) != VERTS_PER_AXIS * VERTS_PER_AXIS: return
    coords = array.array('f', [0.0] * (len(me.vertices) * 3))
    me.vertices.foreach_get("co", coords)
    for i in range(len(me.vertices)):
        r = i // VERTS_PER_AXIS
        c = i % VERTS_PER_AXIS
        coords[i * 3 + 2] = H[r][c]
    me.vertices.foreach_set("co", coords)
    me.update()

def choose_water_images(sro_root, wtype, wwave):
    base, wave = None, None
    water_dir = os.path.join(sro_root, "Map", "water")
    if not os.path.isdir(water_dir): return None, None
    prefs=["water201.ddj","water121.ddj","water111.ddj","water101.ddj"]
    for fn in prefs:
        if os.path.exists(os.path.join(water_dir, fn)):
            base = fn
            break
    if not base:
        for fn in sorted(os.listdir(water_dir)):
            if fn.lower().startswith("water") and fn.lower().endswith(".ddj"):
                base = fn
                break
    if wwave in (1,2,3):
        wf = f"wave{wwave}.ddj"
        if os.path.exists(os.path.join(water_dir, wf)):
            wave = wf
    return base, wave

def get_water_material(sro_root, wtype, wwave):
    base_fn, wave_fn = choose_water_images(sro_root, wtype, wwave)
    key = (base_fn, wave_fn)
    if key in WATER_MAT_CACHE: return WATER_MAT_CACHE[key]
    mat_name = "SRO_Water"
    if base_fn: mat_name += "_" + os.path.splitext(base_fn)[0]
    if wave_fn: mat_name += "_" + os.path.splitext(wave_fn)[0]
    if mat_name in bpy.data.materials:
        mat = bpy.data.materials[mat_name]
        WATER_MAT_CACHE[key] = mat
        return mat
    mat = bpy.data.materials.new(name=mat_name)
    mat.use_nodes = True
    mat.blend_method = 'BLEND'
    nt = mat.node_tree
    nodes = nt.nodes
    links = nt.links
    for n in list(nodes): nodes.remove(n)
    out = nodes.new("ShaderNodeOutputMaterial"); out.location = Vector((400, 0))
    bsdf = nodes.new("ShaderNodeBsdfPrincipled"); bsdf.location = Vector((200, 0))
    links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])
    bsdf.inputs['Roughness'].default_value = 0.05
    bsdf.inputs['Specular IOR Level'].default_value = 0.2
    bsdf.inputs['Transmission Weight'].default_value = 0.8
    bsdf.inputs['Alpha'].default_value = 0.7
    base_img_node = None
    uvn = nodes.new("ShaderNodeUVMap"); uvn.location = bsdf.location - Vector((600, 0))
    uvn.uv_map = "WaterUV"
    if base_fn:
        base_img = load_ddj_image(sro_root, os.path.join("Map", "water", base_fn), WATER_TEX_CACHE)
        if base_img:
            t_base = nodes.new('ShaderNodeTexImage'); t_base.location = bsdf.location - Vector((400, 0))
            t_base.image = base_img
            links.new(uvn.outputs['UV'], t_base.inputs['Vector'])
            base_img_node = t_base
    if wave_fn and base_img_node:
        wave_img = load_ddj_image(sro_root, os.path.join("Map", "water", wave_fn), WATER_TEX_CACHE)
        if wave_img:
            t_wave = nodes.new('ShaderNodeTexImage'); t_wave.location = bsdf.location - Vector((400, -200))
            t_wave.image = wave_img
            links.new(uvn.outputs['UV'], t_wave.inputs['Vector'])
            add = nodes.new("ShaderNodeMixRGB"); add.location = bsdf.location - Vector((200,0)); add.blend_type = 'ADD'
            add.inputs['Fac'].default_value = 0.5
            links.new(base_img_node.outputs['Color'], add.inputs['Color1'])
            links.new(t_wave.outputs['Color'], add.inputs['Color2'])
            links.new(add.outputs['Color'], bsdf.inputs['Base Color'])
    elif base_img_node:
        links.new(base_img_node.outputs['Color'], bsdf.inputs['Base Color'])
    WATER_MAT_CACHE[key] = mat
    return mat

def create_water_object(region_name, cx, cy, water_blocks, rot_z, sro_root, coll):
    if not water_blocks: return
    verts, faces, face_keys = [], [], []
    for xb, zb, h, wtype, wwave in water_blocks:
        x0 = -REGION_SIZE * 0.5 + xb * BLOCK_SIZE
        x1 = x0 + BLOCK_SIZE
        y0 = -REGION_SIZE * 0.5 + zb * BLOCK_SIZE
        y1 = y0 + BLOCK_SIZE
        base_idx = len(verts)
        verts.extend([(x0, y0, h), (x1, y0, h), (x1, y1, h), (x0, y1, h)])
        faces.append((base_idx, base_idx + 1, base_idx + 2, base_idx + 3))
        face_keys.append((wtype, wwave))
    mesh_name = f"{region_name}_WaterMesh"
    me = bpy.data.meshes.new(mesh_name)
    me.from_pydata(verts, [], faces)
    me.update()
    obj_name = f"{region_name}_Water"
    obj = bpy.data.objects.new(obj_name, me)
    coll.objects.link(obj)
    obj.location = (cx, cy, 0.0)
    obj.rotation_euler = (0, 0, rot_z)
    uv_layer = me.uv_layers.new(name="WaterUV")
    for poly in me.polygons:
        li = poly.loop_indices
        uv_layer.data[li[0]].uv = (0.0, 0.0)
        uv_layer.data[li[1]].uv = (WATER_TILING_FACTOR, 0.0)
        uv_layer.data[li[2]].uv = (WATER_TILING_FACTOR, WATER_TILING_FACTOR)
        uv_layer.data[li[3]].uv = (0.0, WATER_TILING_FACTOR)
    for fi, (wtype, wwave) in enumerate(face_keys):
        mat = get_water_material(sro_root, wtype, wwave)
        if mat and mat.name not in me.materials:
            me.materials.append(mat)
        if mat:
            me.polygons[fi].material_index = me.materials.find(mat.name)
    me.update()
    print(f"    [INFO] Created single water object for region {region_name} with {len(faces)} blocks.")

def get_pair_material(sro_root, base_tid, layer_tid, lightmap_img, use_vbright):
    a = min(base_tid, layer_tid)
    b = max(base_tid, layer_tid)
    key = (a, b, lightmap_img.name if lightmap_img else None, bool(use_vbright))
    if key in MATERIAL_CACHE: return MATERIAL_CACHE[key]
    base_rel = TERRAIN_DDJ[a] if 0 <= a < len(TERRAIN_DDJ) else None
    layer_rel = TERRAIN_DDJ[b] if 0 <= b < len(TERRAIN_DDJ) else None
    base_img = load_ddj_image(sro_root, base_rel, TEXTURE_CACHE) if base_rel else None
    if not base_img: return None
    mat_name = f"MAT_{a:03d}_{b:03d}"
    if lightmap_img: mat_name += "_LM"
    if use_vbright: mat_name += "_VB"
    if mat_name in bpy.data.materials:
        mat = bpy.data.materials[mat_name]
        MATERIAL_CACHE[key] = mat
        return mat
    mat = bpy.data.materials.new(mat_name)
    mat.use_nodes = True
    nt = mat.node_tree
    nodes = nt.nodes
    links = nt.links
    for n in list(nodes): nodes.remove(n)
    out = nodes.new("ShaderNodeOutputMaterial"); out.location = Vector((900, 0))
    bsdf = nodes.new("ShaderNodeBsdfPrincipled"); bsdf.location = Vector((700, 0))
    sp = bsdf.inputs.get("Specular") or bsdf.inputs.get("Specular IOR Level")
    if sp: sp.default_value = 0.0
    ro = bsdf.inputs.get("Roughness")
    if ro: ro.default_value = 0.9
    links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])
    texcoord = nodes.new("ShaderNodeTexCoord"); texcoord.location = Vector((-900, 0))
    mapping = nodes.new("ShaderNodeMapping"); mapping.location = Vector((-700, 0))
    mapping.inputs['Scale'].default_value = (TEXTURE_TILING_FACTOR, TEXTURE_TILING_FACTOR, 1.0)
    links.new(texcoord.outputs['UV'], mapping.inputs['Vector'])
    t_base = nodes.new("ShaderNodeTexImage"); t_base.location = Vector((-450, 150))
    t_base.image = base_img
    links.new(mapping.outputs['Vector'], t_base.inputs['Vector'])
    final_color_node = t_base
    if a != b and layer_rel:
        layer_img = load_ddj_image(sro_root, layer_rel, TEXTURE_CACHE)
        if layer_img:
            t_layer = nodes.new("ShaderNodeTexImage"); t_layer.location = Vector((-450, -100))
            t_layer.image = layer_img
            links.new(mapping.outputs['Vector'], t_layer.inputs['Vector'])
            vattr = nodes.new("ShaderNodeAttribute"); vattr.location = Vector((-450, -350))
            vattr.attribute_name = "Blend"
            mix = nodes.new("ShaderNodeMixRGB"); mix.location = Vector((200, 0))
            links.new(t_base.outputs['Color'], mix.inputs['Color1'])
            links.new(t_layer.outputs['Color'], mix.inputs['Color2'])
            links.new(vattr.outputs['Color'], mix.inputs['Fac'])
            final_color_node = mix
    if use_vbright:
        vattr_vb = nodes.new("ShaderNodeAttribute"); vattr_vb.location = Vector((200, -260))
        vattr_vb.attribute_name = "VBright"
        mul_vb = nodes.new("ShaderNodeMixRGB"); mul_vb.location = Vector((450, -100)); mul_vb.blend_type = 'MULTIPLY'
        mul_vb.inputs['Fac'].default_value = 1.0
        links.new(final_color_node.outputs['Color'], mul_vb.inputs['Color1'])
        links.new(vattr_vb.outputs['Color'], mul_vb.inputs['Color2'])
        final_color_node = mul_vb
    if lightmap_img:
        lm_tex = nodes.new("ShaderNodeTexImage"); lm_tex.location = Vector((0, -420))
        lm_tex.image = lightmap_img
        lm_tex.interpolation = 'Linear'
        lm_tex.projection = 'FLAT'
        links.new(texcoord.outputs['UV'], lm_tex.inputs['Vector'])
        lm_mix = nodes.new("ShaderNodeMixRGB"); lm_mix.location = Vector((700, -20))
        lm_mix.blend_type = 'MULTIPLY'
        lm_mix.inputs['Fac'].default_value = 1.0
        links.new(final_color_node.outputs['Color'], lm_mix.inputs['Color1'])
        links.new(lm_tex.outputs['Color'], lm_mix.inputs['Color2'])
        final_color_node = lm_mix
    links.new(final_color_node.outputs['Color'], bsdf.inputs['Base Color'])
    MATERIAL_CACHE[key] = mat
    return mat

def paint_region(obj, tex_ids_flat, sro_root, lightmap_img, use_vbright, vbright_flat=None):
    me = obj.data
    me.materials.clear()
    if "Blend" in me.color_attributes:
        vcol = me.color_attributes["Blend"]
    else:
        vcol = me.color_attributes.new(name="Blend", type='FLOAT_COLOR', domain='CORNER')
    vb_attr = None
    if use_vbright:
        vb_attr = me.color_attributes.get("VBright") or me.color_attributes.new(name="VBright", type='FLOAT_COLOR', domain='CORNER')
    mat_faces = defaultdict(list)
    for poly_idx, poly in enumerate(me.polygons):
        vids = [me.loops[i].vertex_index for i in poly.loop_indices]
        tids = [tex_ids_flat[v] for v in vids]
        tids4 = [decode_vertex_tex(t)[0] for t in tids]
        common = [t for t, _ in Counter(tids4).most_common(2)]
        if not common: continue
        if len(common) == 1: common.append(common[0])
        base_tid, layer_tid = common[0], common[1]
        for li, loop_i in enumerate(poly.loop_indices):
            vt = tids4[li]
            fac = 1.0 if vt == layer_tid else 0.0
            vcol.data[loop_i].color = (fac, fac, fac, 1.0)
            if use_vbright and vbright_flat is not None:
                vv = max(0.0, min(1.0, vbright_flat[vids[li]]))
                vb_attr.data[loop_i].color = (vv, vv, vv, 1.0)
        mat_faces[(base_tid, layer_tid)].append(poly_idx)
    for (a, b), faces in mat_faces.items():
        mat = get_pair_material(sro_root, a, b, lightmap_img, use_vbright)
        if not mat: continue
        if mat.name not in me.materials:
            me.materials.append(mat)
        midx = me.materials.find(mat.name)
        for fi in faces:
            me.polygons[fi].material_index = midx

def _named_region_enum_items(self, context):
    items = [(n, f"{n} ({len(v)} tiles)", "") for n, v in NAMED_REGIONS_DATA.items()]
    return items or [("none", "(no named regions found)", "Check regioninfo.txt path and content")]

class SRO_ProjectProps(PropertyGroup):
    sro_root: StringProperty(name="Silkroad Root", subtype='DIR_PATH')
    is_root_valid: BoolProperty(default=False)
    is_data_parsed: BoolProperty(default=False)
    import_mode: EnumProperty(
        name="Import Mode",
        items=[('NAMED', "Named Area", "Import tiles by named area (from regioninfo.txt)"),
               ('FULL', "Full Map", "Import all active regions from mapinfo.mfo")],
        default='NAMED'
    )
    named_region_choice: EnumProperty(name="Area", items=_named_region_enum_items)
    import_textures: BoolProperty(name="Paint Terrain", default=True)
    import_lightmaps: BoolProperty(name="Import Lightmaps", default=True)
    import_vertex_brightness: BoolProperty(name="Import Vertex Brightness", default=False)
    import_water: BoolProperty(name="Import Water", default=True)

class SRO_OT_ParseData(Operator):
    bl_idname = "sro.parse_data"
    bl_label = "Parse Game Data"
    bl_description = "Reads mapinfo.mfo, regioninfo.txt, and tile2d.ifo to prepare for import"

    def execute(self, ctx):
        p = ctx.scene.sro_props
        root = bpy.path.abspath(p.sro_root)
        print("\n" + "="*50)
        print("[PROC] Starting data parsing process...")
        if not validate_root(root):
            self.report({'ERROR'}, "Invalid Silkroad root. Check path and ensure Map/mapinfo.mfo exists.")
            p.is_root_valid = False
            return {'CANCELLED'}
        p.is_root_valid = True
        map_root = os.path.join(root, "Map")
        try:
            global REGION_ORIENT, PARSED_REGIONS, NAMED_REGIONS_DATA, TERRAIN_DDJ, WARN_CNT
            global MATERIAL_CACHE, TEXTURE_CACHE, LIGHTMAP_CACHE, WATER_MAT_CACHE, WATER_TEX_CACHE
            WARN_CNT = 0
            MATERIAL_CACHE.clear(); TEXTURE_CACHE.clear(); LIGHTMAP_CACHE.clear()
            WATER_MAT_CACHE.clear(); WATER_TEX_CACHE.clear()
            regs = parse_mfo(os.path.join(map_root, "mapinfo.mfo"))
            REGION_ORIENT = detect_orient(map_root, regs)
            exist = []
            print("[PROC] Verifying existence of region .m files...")
            for (rx, rz, db) in regs:
                a, b = (rx, rz) if REGION_ORIENT == "XZ" else (rz, rx)
                if find_res(map_root, a, b, ".m"):
                    exist.append((rx, rz, db))
            PARSED_REGIONS = sorted(exist, key=lambda t:(t[0], t[1]))
            print(f"[INFO] Found {len(PARSED_REGIONS)} existing region files.")
            NAMED_REGIONS_DATA = parse_regioninfo(os.path.join(root, "Data", "regioninfo.txt"))
            TERRAIN_DDJ = parse_tile2d_ifo(root)
            p.is_data_parsed = True
            report_msg = f"Parse successful: {len(PARSED_REGIONS)} regions, {len(NAMED_REGIONS_DATA)} named areas. Orientation: {REGION_ORIENT}"
            self.report({'INFO'}, report_msg)
            print(f"[SUCCESS] {report_msg}")
        except Exception as e:
            report_msg = f"Data parsing failed: {e}"
            self.report({'ERROR'}, report_msg)
            print(f"[FATAL] {report_msg}")
            p.is_data_parsed = False
            return {'CANCELLED'}
        return {'FINISHED'}

class SRO_OT_ExecuteImport(Operator):
    bl_idname = "sro.execute_import"
    bl_label = "Import Map"
    bl_description = "Starts the map import process based on current settings"

    @classmethod
    def poll(cls, ctx):
        p = ctx.scene.sro_props
        return p.is_root_valid and p.is_data_parsed

    def execute(self, ctx):
        ctx.space_data.clip_end = 100000.0
        p = ctx.scene.sro_props
        sro_root = bpy.path.abspath(p.sro_root)
        map_root = os.path.join(sro_root, "Map")
        if p.import_mode == 'NAMED':
            area = p.named_region_choice
            if area in NAMED_REGIONS_DATA:
                tiles = list(NAMED_REGIONS_DATA[area])
                coll_name = f"SRO_Area_{area}"
            else:
                self.report({'ERROR'}, "Selected named area is not valid. Please re-parse data.")
                return {'CANCELLED'}
        else:
            tiles = [(x, z) for (x, z, db) in PARSED_REGIONS]
            coll_name = "SRO_Full_Map"
        if not tiles:
            self.report({'WARNING'}, "No regions selected to import.")
            return {'CANCELLED'}
        tiles = sorted(set(tiles))
        zdeg = -90.0 if REGION_ORIENT == "ZX" else 0.0
        zrad = math.radians(zdeg)
        print("\n" + "="*50)
        print(f"[PROC] Starting map import...")
        print(f"  Mode: {p.import_mode}, Tiles: {len(tiles)}, Z-rot: {zdeg} deg")
        print(f"  Paint Terrain: {'ON' if p.import_textures else 'OFF'}, Import Lightmaps: {'ON' if p.import_lightmaps else 'OFF'}, Import Vertex Brightness: {'ON' if p.import_vertex_brightness else 'OFF'}, Import Water: {'ON' if p.import_water else 'OFF'}")
        coll = ensure_collection(coll_name)
        bpy.context.view_layer.objects.active = None
        for ob in list(coll.objects):
            try:
                bpy.data.objects.remove(ob, do_unlink=True)
            except:
                pass
        MATERIAL_CACHE.clear()
        created = 0
        t0 = time.time()
        for i, (rx, rz) in enumerate(tiles, 1):
            a, b = (rx, rz) if REGION_ORIENT == "XZ" else (rz, rx)
            mpath = find_res(map_root, a, b, ".m")
            if not mpath:
                print(f"  [{i:04d}/{len(tiles)}] [WARN] Missing .m file for region ({rx},{rz}). Skipping.")
                continue
            name = f"Region_{rx:03d}_{rz:03d}"
            print(f"  [{i:04d}/{len(tiles)}] [PROC] Processing {name}...")
            try:
                H, Tex, VB, W_Data = read_mapm_data(mpath)
                cx, cy = region_center_world(rx, rz, REGION_ORIENT)
                obj = create_grid_object(name, cx, cy)
                for c in list(obj.users_collection): c.objects.unlink(obj)
                coll.objects.link(obj)
                apply_heights(obj, H)
                obj.rotation_euler = (0, 0, zrad)
                lightmap_image = None
                if p.import_lightmaps:
                    tpath = find_res(map_root, a, b, ".t")
                    if tpath:
                        dds_data = read_mapt_lightmap(tpath)
                        if dds_data:
                           lightmap_image = load_dds_from_data(dds_data, name)
                    else:
                        print(f"    [INFO] No .t file found for region {name}.")
                if p.import_textures and TERRAIN_DDJ:
                    me = obj.data
                    tex_flat = [0] * (len(me.vertices))
                    vbright_flat = [1.0] * (len(me.vertices))
                    for vid in range(len(me.vertices)):
                        r = vid // VERTS_PER_AXIS
                        c = vid % VERTS_PER_AXIS
                        tex_flat[vid] = Tex[r][c]
                        if p.import_vertex_brightness:
                            vbright_flat[vid] = VB[r][c] / 255.0
                    paint_region(obj, tex_flat, sro_root, lightmap_image, p.import_vertex_brightness, vbright_flat if p.import_vertex_brightness else None)
                if p.import_water and W_Data:
                    create_water_object(name, cx, cy, W_Data, zrad, sro_root, coll)
                created += 1
                print(f"  [{i:04d}/{len(tiles)}] [SUCCESS] Region {name} created successfully.")
            except Exception as e:
                print(f"  [{i:04d}/{len(tiles)}] [FATAL] Failed to process region ({rx},{rz}): {e}")
        dt = time.time() - t0
        report_msg = f"Import finished. Created {created}/{len(tiles)} regions in {dt:.2f}s."
        self.report({'INFO'}, report_msg)
        print(f"[SUCCESS] {report_msg}")
        return {'FINISHED'}

class SRO_PT_Project(Panel):
    bl_label = "SRO Project"
    bl_idname = "SRO_PT_Project"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "SRO Project"

    def draw(self, ctx):
        layout = self.layout
        p = ctx.scene.sro_props
        box = layout.box()
        box.label(text="1. Setup & Parse", icon='FILE_FOLDER')
        box.prop(p, "sro_root", text="")
        box.operator(SRO_OT_ParseData.bl_idname, icon='FILE_REFRESH')
        if p.is_root_valid and p.is_data_parsed:
            box = layout.box()
            box.label(text="2. Import Settings", icon='SETTINGS')
            col = box.column(align=True)
            col.prop(p, "import_mode", expand=True)
            if p.import_mode == 'NAMED':
                col.prop(p, "named_region_choice")
            col.separator()
            col.prop(p, "import_textures")
            sub = col.column(align=True)
            sub.enabled = p.import_textures
            row = sub.row(); row.separator(); row.prop(p, "import_lightmaps")
            row2 = sub.row(); row2.separator(); row2.prop(p, "import_vertex_brightness")
            col.separator()
            col.prop(p, "import_water")
            layout.separator()
            layout.operator(SRO_OT_ExecuteImport.bl_idname, icon='PLAY', text="Import Map")
        elif p.is_root_valid:
             layout.label(text="Root is valid. Please parse data.", icon='ERROR')

CLASSES = (SRO_ProjectProps, SRO_OT_ParseData, SRO_OT_ExecuteImport, SRO_PT_Project)

def register():
    for c in CLASSES:
        bpy.utils.register_class(c)
    bpy.types.Scene.sro_props = PointerProperty(type=SRO_ProjectProps)
    _banner()

def unregister():
    if hasattr(bpy.types.Scene, "sro_props"):
        del bpy.types.Scene.sro_props
    for c in reversed(CLASSES):
        try:
            bpy.utils.unregister_class(c)
        except RuntimeError:
            pass
    TEXTURE_CACHE.clear(); MATERIAL_CACHE.clear(); LIGHTMAP_CACHE.clear()
    WATER_MAT_CACHE.clear(); WATER_TEX_CACHE.clear()

if __name__ == "__main__":
    register()
