bl_info = {
    "name": "Silkroad Map – Project UI",
    "author": "szabo176",
    "version": (4, 2, 0),
    "blender": (4, 1, 0),
    "location": "3D View > Sidebar > SRO Project",
    "description": "Import Silkroad map by named area or full map.",
    "category": "Import-Export",
}

import bpy, os, struct, time, re, math
from bpy.types import Operator, Panel, PropertyGroup
from bpy.props import StringProperty, BoolProperty, FloatProperty, PointerProperty, EnumProperty

PARSED_REGIONS = []
NAMED_REGIONS_DATA = {}
REGION_ORIENT  = "XZ"
MFO_HEADER     = (0, 0)

REGION_SIZE = 1920.0
VERTS_PER_AXIS = 97
TILE_UNIT = REGION_SIZE / (VERTS_PER_AXIS - 1)

def _print_install_header():
    print("\n"*5, end=""); time.sleep(0.2)
    v = bl_info.get("version", (0,0,0))
    print(f"[{bl_info.get('name','SRO Addon')}] v{v[0]}.{v[1]}.{v[2]} loaded.")

def validate_root(path: str) -> bool:
    if not path: return False
    for r in ("Data","Music","Map","Media"):
        rp = os.path.join(path, r)
        if not os.path.isdir(rp):
            print(f"[WARN] Missing required folder: {rp}"); return False
    mfo = os.path.join(path, "Map", "mapinfo.mfo")
    if not os.path.isfile(mfo):
        print(f"[WARN] Missing required file: {mfo}"); return False
    return True

def parse_mfo(mfo_path: str):
    with open(mfo_path, "rb") as f:
        sig = f.read(12)
        if not sig.startswith(b"JMXVMFO"): raise ValueError("Not a JMXVMFO file.")
        mw = struct.unpack("<H", f.read(2))[0]
        mh = struct.unpack("<H", f.read(2))[0]
        f.read(8)
        bitmask = f.read(8192)
        if len(bitmask) != 8192: raise ValueError("RegionData must be 8192 bytes.")
    regs = []
    for bi, bv in enumerate(bitmask):
        if bv == 0: continue
        for b in range(8):
            if (bv >> b) & 1:
                idx = bi*8 + b
                x = idx & 0xFF
                z_raw = (idx >> 8) & 0xFF
                z = z_raw & 0x7F
                db = 1 if z_raw >= 128 else 0
                regs.append((x, z, db))
    print(f"[INFO] MFO OK: {len(regs)} active regions found.")
    return mw, mh, regs

def parse_regioninfo(regioninfo_path: str):
    global NAMED_REGIONS_DATA
    NAMED_REGIONS_DATA.clear()
    if not os.path.isfile(regioninfo_path):
        print(f"[WARN] regioninfo.txt not found at: {regioninfo_path}")
        return
    cur = None; tmp = {}
    with open(regioninfo_path, 'r', encoding='utf-8', errors='ignore') as f:
        for raw in f:
            line = raw.strip()
            if not line: continue
            if line.startswith('#'):
                parts = re.split(r'\s+', line)
                if len(parts) > 1:
                    cur = parts[1]; tmp.setdefault(cur, [])
                continue
            if cur:
                try:
                    xs, zs, *_ = re.split(r'\s+', line)
                    x, z = int(xs), int(zs)
                    if (x, z) not in tmp[cur]: tmp[cur].append((x, z))
                except: pass
    NAMED_REGIONS_DATA = {k: v for k, v in sorted(tmp.items()) if v}
    print(f"[INFO] Loaded {len(NAMED_REGIONS_DATA)} named areas from regioninfo.txt.")

def find_resource_candidate(map_root: str, a: int, b: int, ext: str):
    a_s, b_s, a3, b3 = str(a), str(b), f"{a:03d}", f"{b:03d}"
    for c in (os.path.join(map_root, a_s,  b_s+ext),
              os.path.join(map_root, a_s,  b3 +ext),
              os.path.join(map_root, a3,   b_s+ext),
              os.path.join(map_root, a3,   b3 +ext)):
        if os.path.isfile(c): return c
    return None

def detect_orientation(map_root: str, regions, sample=64) -> str:
    test = regions[:sample] if len(regions) > sample else regions
    hits_xz = sum(1 for (x, z, db) in test if find_resource_candidate(map_root, x, z, ".m"))
    hits_zx = sum(1 for (x, z, db) in test if find_resource_candidate(map_root, z, x, ".m"))
    mode = "XZ" if hits_xz >= hits_zx else "ZX"
    print(f"[INFO] Orientation detected: {mode} (Hits: XZ={hits_xz}, ZX={hits_zx})")
    return mode

def region_center_world(rx: int, rz: int, orient: str):
    gx, gy = (rx, rz) if orient == "XZ" else (rz, rx)
    half = REGION_SIZE * 0.5
    cx = gx*REGION_SIZE + half
    cy = gy*REGION_SIZE + half
    return cx, -cy

def read_mapm_97x97(path_m: str):
    with open(path_m, "rb") as f:
        if not f.read(12).startswith(b"JMXVMAPM"): raise ValueError("Not JMXVMAPM.")
        H = [[0.0 for _ in range(VERTS_PER_AXIS)] for __ in range(VERTS_PER_AXIS)]
        for zb in range(6):
            for xb in range(6):
                f.read(4); f.read(2)
                for vz in range(17):
                    for vx in range(17):
                        h = struct.unpack("<f", f.read(4))[0]
                        f.read(2); f.read(1)
                        gx = xb*16 + vx; gz = zb*16 + vz
                        if gx < VERTS_PER_AXIS and gz < VERTS_PER_AXIS:
                            H[gz][gx] = h
                f.read(1+1+4)
                f.read(16*16*2)
                f.read(4+4+20)
    return H

def apply_heights(obj, H, height_scale: float):
    me = obj.data
    if len(me.vertices) != VERTS_PER_AXIS * VERTS_PER_AXIS: return
    coords = [0.0]*(len(me.vertices)*3)
    me.vertices.foreach_get("co", coords)
    for i in range(len(me.vertices)):
        r = i // VERTS_PER_AXIS
        c = i % VERTS_PER_AXIS
        coords[i*3+2] = H[r][c] * height_scale
    me.vertices.foreach_set("co", coords)
    me.update()

def create_grid_object(name: str, cx: float, cy: float):
    bpy.ops.mesh.primitive_grid_add(
        x_subdivisions=VERTS_PER_AXIS-1, y_subdivisions=VERTS_PER_AXIS-1,
        size=REGION_SIZE, align='WORLD', enter_editmode=False,
        location=(cx, cy, 0.0)
    )
    obj = bpy.context.active_object
    obj.name = name
    return obj

def ensure_collection(name: str):
    coll = bpy.data.collections.get(name)
    if not coll:
        coll = bpy.data.collections.new(name)
        bpy.context.scene.collection.children.link(coll)
    return coll

def link_object_to_collection(obj, coll):
    for c in list(obj.users_collection):
        try: c.objects.unlink(obj)
        except: pass
    try: coll.objects.link(obj)
    except RuntimeError: pass

def _named_region_enum_items(self, context):
    return [(name, f"{name} ({len(coords)} tiles)", "") for name, coords in NAMED_REGIONS_DATA.items()] or [("none","(no named regions)","")]

class SRO_ProjectProps(PropertyGroup):
    sro_root: StringProperty(name="Silkroad Root", subtype='DIR_PATH')
    is_root_valid: BoolProperty(default=False)
    is_data_parsed: BoolProperty(default=False)
    import_mode: EnumProperty(
        name="Import Mode",
        items=[
            ('NAMED',"Named Area","Import tiles by named area (regioninfo.txt)"),
            ('FULL', "Full Map",  "Import all active regions"),
        ],
        default='NAMED'
    )
    named_region_choice: EnumProperty(name="Area", items=_named_region_enum_items)
    height_scale:       FloatProperty(name="Height Scale", default=1.0, min=0.01, max=100.0)

class SRO_OT_ParseData(Operator):
    bl_idname = "sro.parse_data"; bl_label = "Parse Game Data"
    bl_description = "Validate root, load mapinfo.mfo, read Data/regioninfo.txt"

    def execute(self, context):
        p = context.scene.sro_props
        root = bpy.path.abspath(p.sro_root)
        if not validate_root(root):
            self.report({'ERROR'}, "Invalid Silkroad root directory."); p.is_root_valid=False
            return {'CANCELLED'}
        p.is_root_valid = True
        map_root = os.path.join(root, "Map")
        mfo_path = os.path.join(map_root, "mapinfo.mfo")
        try:
            global MFO_HEADER, REGION_ORIENT, PARSED_REGIONS
            mw, mh, all_regs = parse_mfo(mfo_path); MFO_HEADER=(mw,mh)
            REGION_ORIENT = detect_orientation(map_root, all_regs)
            existing=[]
            for (rx, rz, db) in all_regs:
                a, b = (rx, rz) if REGION_ORIENT=="XZ" else (rz, rx)
                if find_resource_candidate(map_root, a, b, ".m"): existing.append((rx,rz,db))
            if not existing:
                self.report({'ERROR'}, "No .m files found in active regions."); return {'CANCELLED'}
            PARSED_REGIONS = sorted(existing, key=lambda t:(t[0],t[1]))
            parse_regioninfo(os.path.join(root, "Data", "regioninfo.txt"))
            p.is_data_parsed = True
            self.report({'INFO'}, f"Parsed: {len(PARSED_REGIONS)} regions, {len(NAMED_REGIONS_DATA)} areas. Orientation: {REGION_ORIENT}")
        except Exception as e:
            self.report({'ERROR'}, f"Error during data parsing: {e}"); p.is_data_parsed=False
            return {'CANCELLED'}
        return {'FINISHED'}

class SRO_OT_ExecuteImport(Operator):
    bl_idname = "sro.execute_import"; bl_label = "Import Map"
    bl_description = "Import selected named area or full map"

    @classmethod
    def poll(cls, context):
        p = context.scene.sro_props
        return p.is_root_valid and p.is_data_parsed

    def execute(self, context):
        context.space_data.clip_end = 75000.0
        
        p = context.scene.sro_props
        root = bpy.path.abspath(p.sro_root)
        map_root = os.path.join(root, "Map")

        tiles=[]; coll_name="SRO_Import"
        if p.import_mode=='NAMED':
            name=p.named_region_choice
            if name in NAMED_REGIONS_DATA:
                tiles=list(NAMED_REGIONS_DATA[name]); coll_name=f"SRO_Area_{name}"
            else:
                self.report({'ERROR'}, "Invalid area selected."); return {'CANCELLED'}
        elif p.import_mode=='FULL':
            tiles=[(x,z) for (x,z,db) in PARSED_REGIONS]; coll_name="SRO_Full_Map"

        if not tiles:
            self.report({'WARNING'}, "No regions to import."); return {'CANCELLED'}
        tiles = sorted(set(tiles))

        z_deg = -90.0 if REGION_ORIENT == "ZX" else 0.0
        z_rad = math.radians(z_deg)

        print(f"\n[INFO] Starting import: mode={p.import_mode}, tiles={len(tiles)}, objZ={z_deg}°")

        coll = ensure_collection(coll_name)
        created_objs=[]
        t0=time.time()

        for i,(rx,rz) in enumerate(tiles,1):
            a,b = (rx,rz) if REGION_ORIENT=="XZ" else (rz,rx)
            mpath = find_resource_candidate(map_root, a, b, ".m")
            if not mpath:
                print(f"  [WARN] Missing .m for ({rx},{rz}) - skipping."); continue
            name=f"Region_{rx:03d}_{rz:03d}"
            cx,cy = region_center_world(rx,rz,REGION_ORIENT)

            old = bpy.data.objects.get(name)
            if old:
                for c in list(old.users_collection):
                    try: c.objects.unlink(old)
                    except: pass
                try: bpy.data.objects.remove(old, do_unlink=True)
                except: pass

            try:
                H = read_mapm_97x97(mpath)
                obj = create_grid_object(name, cx, cy)
                link_object_to_collection(obj, coll)
                apply_heights(obj, H, p.height_scale)
                obj.rotation_euler = (0.0, 0.0, z_rad)
                created_objs.append(obj)
                print(f"  [OK] {i:04d}/{len(tiles)}: {name} @ {cx:.1f},{cy:.1f} (Zrot={z_deg}°)")
            except Exception as e:
                print(f"  [ERROR] Failed region ({rx},{rz}): {e}")

        dt=time.time()-t0
        self.report({'INFO'}, f"Completed: {len(created_objs)} tiles in {dt:.2f}s.")
        print(f"[OK] Import finished: {len(created_objs)} tiles in {dt:.2f}s")
        return {'FINISHED'}

class SRO_PT_Project(Panel):
    bl_label="SRO Project"; bl_idname="SRO_PT_Project"
    bl_space_type='VIEW_3D'; bl_region_type='UI'; bl_category="SRO Project"
    def draw(self, context):
        layout=self.layout; p=context.scene.sro_props
        b=layout.box(); b.label(text="1. SRO Game Client", icon='FILE_FOLDER')
        b.prop(p,"sro_root", text="")
        b.operator(SRO_OT_ParseData.bl_idname, icon='FILE_REFRESH')

        if p.is_root_valid and p.is_data_parsed:
            b=layout.box(); b.label(text="2. Import Settings", icon='IMPORT')
            col=b.column(align=True)
            col.prop(p,"import_mode", expand=True)
            if p.import_mode=='NAMED':
                col.prop(p,"named_region_choice")
            col.separator()
            col.prop(p,"height_scale")
            layout.separator()
            layout.operator(SRO_OT_ExecuteImport.bl_idname, icon='PLAY')

CLASSES=(SRO_ProjectProps, SRO_OT_ParseData, SRO_OT_ExecuteImport, SRO_PT_Project)
def register():
    for c in CLASSES: bpy.utils.register_class(c)
    bpy.types.Scene.sro_props = PointerProperty(type=SRO_ProjectProps)
    _print_install_header()
def unregister():
    if hasattr(bpy.types.Scene,"sro_props"): del bpy.types.Scene.sro_props
    for c in reversed(CLASSES):
        try: bpy.utils.unregister_class(c)
        except: pass
    print("Silkroad Map – Project UI disabled.")
if __name__=="__main__": register()