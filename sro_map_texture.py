bl_info = {
    "name": "Silkroad Map - Project UI",
    "author": "szabo176",
    "version": (6, 3, 0),
    "blender": (4, 5, 0),
    "location": "3D View > Sidebar > SRO Project",
    "description": "Import Silkroad map with correct terrain texture blending from .m + tile2d.ifo.",
    "category": "Import-Export",
}

import bpy, os, re, time, math, struct, tempfile, array
from bpy.types import Operator, Panel, PropertyGroup
from bpy.props import StringProperty, BoolProperty, FloatProperty, PointerProperty, EnumProperty
from collections import defaultdict, Counter

REGION_SIZE = 1920.0
VERTS_PER_AXIS = 97
TEXTURE_TILING_FACTOR = 64.0

PARSED_REGIONS = []
NAMED_REGIONS_DATA = {}
REGION_ORIENT = "ZX"
TERRAIN_DDJ = []
TEXTURE_CACHE = {}
MATERIAL_CACHE = {}
MAX_WARN = 20
WARN_CNT = 0

def _banner():
    print("\n"*5, end=""); time.sleep(0.2)
    v = bl_info["version"]
    print(f"[{bl_info['name']}] v{v[0]}.{v[1]}.{v[2]} loaded.")

def validate_root(path: str) -> bool:
    if not path: return False
    for r in ("Data","Music","Map","Media"):
        if not os.path.isdir(os.path.join(path, r)): return False
    return os.path.isfile(os.path.join(path,"Map","mapinfo.mfo"))

def parse_mfo(mfo_path: str):
    with open(mfo_path, "rb") as f:
        if not f.read(12).startswith(b"JMXVMFO"): raise ValueError("Not JMXVMFO")
        mw,mh = struct.unpack("<HH", f.read(4))
        f.read(8)
        bits = f.read(8192)
    regs=[]
    for i,b in enumerate(bits):
        if b==0: continue
        for k in range(8):
            if (b>>k)&1:
                idx=i*8+k
                x = idx & 0xFF
                zr= (idx>>8)&0xFF
                z = zr & 0x7F
                regs.append((x,z,1 if zr>=128 else 0))
    print(f"[INFO] MFO OK: {len(regs)} active regions found.")
    return regs

def find_res(map_root, a, b, ext):
    aS,bS,a3,b3 = str(a),str(b),f"{a:03d}",f"{b:03d}"
    for p in (os.path.join(map_root,aS,bS+ext),
              os.path.join(map_root,aS,b3+ext),
              os.path.join(map_root,a3,bS+ext),
              os.path.join(map_root,a3,b3+ext)):
        if os.path.isfile(p): return p
    return None

def detect_orient(map_root, regs):
    test = regs[:64] if len(regs)>64 else regs
    xz = sum(1 for (x,z,db) in test if find_res(map_root,x,z,".m"))
    zx = sum(1 for (x,z,db) in test if find_res(map_root,z,x,".m"))
    mode = "XZ" if xz>=zx else "ZX"
    print(f"[INFO] Orientation detected: {mode} (Hits: XZ={xz}, ZX={zx})")
    return mode

def region_center_world(rx,rz,orient):
    gx,gy = (rx,rz) if orient=="XZ" else (rz,rx)
    half = REGION_SIZE*0.5
    return gx*REGION_SIZE+half, -(gy*REGION_SIZE+half)

def parse_regioninfo(path):
    out={}
    if not os.path.isfile(path): return out
    cur=None
    for line in open(path,'r',encoding='utf-8',errors='ignore'):
        s=line.strip()
        if not s: continue
        if s.startswith('#'):
            parts=re.split(r'\s+',s)
            if len(parts)>1: cur=parts[1]; out.setdefault(cur,[])
            continue
        if cur:
            try:
                xs,zs,*_ = re.split(r'\s+',s)
                x,z=int(xs),int(zs)
                if (x,z) not in out[cur]: out[cur].append((x,z))
            except: pass
    return {k:v for k,v in out.items() if v}

def read_mapm_heights_and_tex(path_m: str):
    H = [[0.0]*VERTS_PER_AXIS for _ in range(VERTS_PER_AXIS)]
    T = [[0]*VERTS_PER_AXIS for _ in range(VERTS_PER_AXIS)]
    with open(path_m,"rb") as f:
        if not f.read(12).startswith(b"JMXVMAPM"): raise ValueError("Not JMXVMAPM")
        for zb in range(6):
            for xb in range(6):
                f.read(4); f.read(2)
                for vz in range(17):
                    for vx in range(17):
                        h = struct.unpack("<f", f.read(4))[0]
                        tex = struct.unpack("<H", f.read(2))[0]
                        f.read(1)
                        gx = xb*16+vx
                        gz = zb*16+vz
                        if gx<VERTS_PER_AXIS and gz<VERTS_PER_AXIS:
                            H[gz][gx]=h
                            T[gz][gx]=tex
                f.read(1+1+4)
                f.read(16*16*2)
                f.read(4+4+20)
    return H, T

def decode_vertex_tex(vtex:int):
    tid = vtex & 0x03FF
    scl = (vtex >> 10) & 0x3F
    return tid, scl

def parse_tile2d_ifo(root):
    cands=[
        os.path.join(root,"Data","tile2d.ifo"),
        os.path.join(root,"Map","tile2d.ifo"),
        os.path.join(root,"tile2d.ifo"),
    ]
    path=None
    for p in cands:
        if os.path.isfile(p): path=p; break
    if not path:
        print(f"[WARN] tile2d.ifo not found at: {cands[0]}")
        return []
    raw=open(path,'rb').read()
    txt=None
    for enc in ("utf-8","cp949","cp1250","latin-1"):
        try: txt=raw.decode(enc); break
        except: pass
    if not txt: return []
    lines=[l.strip() for l in txt.splitlines() if l.strip()]
    if len(lines)<3: return []
    out=[]
    for l in lines[2:]:
        m=re.search(r'"([^"]+\.ddj)"', l, re.IGNORECASE)
        if m:
            fn=m.group(1).replace("\\","/")
            if "/" not in fn: fn="tile2d/"+os.path.basename(fn)
            out.append(os.path.normpath(fn))
    print(f"[INFO] Loaded {len(out)} textures from tile2d.ifo.")
    return out

def load_ddj_image(map_root: str, rel_path: str):
    global WARN_CNT
    if not rel_path: return None
    ddj=os.path.join(map_root, rel_path)
    if not os.path.exists(ddj):
        cand=os.path.join(map_root,"tile2d", os.path.basename(rel_path))
        if os.path.exists(cand): ddj=cand
        else:
            if WARN_CNT<MAX_WARN:
                print(f"    [WARN] Texture not found: {rel_path}")
                WARN_CNT+=1
            return None
    key=os.path.normcase(ddj)
    if key in TEXTURE_CACHE: return TEXTURE_CACHE[key]
    try:
        data=open(ddj,"rb").read()
        if len(data)<64: return None
        dds=data[20:]
        cache=os.path.join(tempfile.gettempdir(),"sro_ddj_cache"); os.makedirs(cache,exist_ok=True)
        temp=os.path.join(cache,f"{os.path.basename(ddj)}.{int(os.path.getmtime(ddj))}.dds")
        if not os.path.exists(temp):
            with open(temp,"wb") as w: w.write(dds)
        img=bpy.data.images.load(temp, check_existing=True)
        TEXTURE_CACHE[key]=img
        return img
    except Exception as e:
        print(f"    [ERROR] DDJ load failed: {ddj} :: {e}")
        return None

def ensure_collection(name: str):
    coll=bpy.data.collections.get(name)
    if not coll:
        coll=bpy.data.collections.new(name)
        bpy.context.scene.collection.children.link(coll)
    return coll

def create_grid_object(name: str, cx: float, cy: float):
    bpy.ops.mesh.primitive_grid_add(x_subdivisions=VERTS_PER_AXIS-1, y_subdivisions=VERTS_PER_AXIS-1, size=REGION_SIZE, align='WORLD', enter_editmode=False, location=(cx, cy, 0.0))
    obj=bpy.context.active_object
    obj.name=name
    return obj

def apply_heights(obj, H, scale):
    me=obj.data
    if len(me.vertices)!=VERTS_PER_AXIS*VERTS_PER_AXIS: return
    coords=array.array('f',[0.0]*(len(me.vertices)*3))
    me.vertices.foreach_get("co", coords)
    for i in range(len(me.vertices)):
        r=i//VERTS_PER_AXIS
        c=i%VERTS_PER_AXIS
        coords[i*3+2]=H[r][c]*scale
    me.vertices.foreach_set("co", coords)
    me.update()

def get_pair_material(map_root, base_tid, layer_tid):
    a=min(base_tid, layer_tid)
    b=max(base_tid, layer_tid)
    key=(a,b)
    if key in MATERIAL_CACHE: return MATERIAL_CACHE[key]
    base_rel = TERRAIN_DDJ[a] if 0<=a<len(TERRAIN_DDJ) else None
    layer_rel = TERRAIN_DDJ[b] if 0<=b<len(TERRAIN_DDJ) else None
    base_img=load_ddj_image(map_root, base_rel) if base_rel else None
    if not base_img: return None
    mat_name=f"MAT_{a:03d}_{b:03d}"
    if mat_name in bpy.data.materials:
        mat=bpy.data.materials[mat_name]
        MATERIAL_CACHE[key]=mat
        return mat
    mat=bpy.data.materials.new(mat_name); mat.use_nodes=True
    nt=mat.node_tree; nodes=nt.nodes; links=nt.links
    for n in list(nodes): nodes.remove(n)
    out=nodes.new("ShaderNodeOutputMaterial"); out.location=(900,0)
    bsdf=nodes.new("ShaderNodeBsdfPrincipled"); bsdf.location=(700,0)
    sp=bsdf.inputs.get("Specular") or bsdf.inputs.get("Specular IOR Level")
    if sp: sp.default_value=0.0
    ro=bsdf.inputs.get("Roughness"); 
    if ro: ro.default_value=0.85
    links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])
    texcoord=nodes.new("ShaderNodeTexCoord"); texcoord.location=(-900,0)
    mapping =nodes.new("ShaderNodeMapping"); mapping.location=(-700,0)
    mapping.inputs['Scale'].default_value=(TEXTURE_TILING_FACTOR,TEXTURE_TILING_FACTOR,1.0)
    links.new(texcoord.outputs['UV'], mapping.inputs['Vector'])
    t_base=nodes.new("ShaderNodeTexImage"); t_base.location=(-450,150); t_base.image=base_img
    links.new(mapping.outputs['Vector'], t_base.inputs['Vector'])
    final=t_base.outputs['Color']
    if a!=b and layer_rel:
        layer_img=load_ddj_image(map_root, layer_rel)
        if layer_img:
            t_layer=nodes.new("ShaderNodeTexImage"); t_layer.location=(-450,-100); t_layer.image=layer_img
            links.new(mapping.outputs['Vector'], t_layer.inputs['Vector'])
            vattr=nodes.new("ShaderNodeAttribute"); vattr.location=(-450,-350); vattr.attribute_name="Blend"
            mix=nodes.new("ShaderNodeMixRGB"); mix.location=(200,0); mix.blend_type='MIX'
            links.new(t_base.outputs['Color'], mix.inputs['Color1'])
            links.new(t_layer.outputs['Color'], mix.inputs['Color2'])
            links.new(vattr.outputs['Color'], mix.inputs['Fac'])
            final=mix.outputs['Color']
    links.new(final, bsdf.inputs['Base Color'])
    MATERIAL_CACHE[key]=mat
    return mat

def paint_region(obj, tex_ids_flat, map_root):
    me=obj.data
    obj.data.materials.clear()
    if "Blend" in obj.data.vertex_colors:
        vcol=obj.data.vertex_colors["Blend"]
    else:
        vcol=obj.data.vertex_colors.new(name="Blend")
    mat_faces=defaultdict(list)
    for poly_idx, poly in enumerate(me.polygons):
        vids=[me.loops[i].vertex_index for i in poly.loop_indices]
        tids=[tex_ids_flat[v] for v in vids]
        tids4=[decode_vertex_tex(t)[0] for t in tids]
        common=[t for t,_ in Counter(tids4).most_common(2)]
        if len(common)==1: common.append(common[0])
        base_tid, layer_tid = common[0], common[1]
        for li,loop_i in enumerate(poly.loop_indices):
            vt = tids4[li]
            fac = 1.0 if vt==layer_tid else 0.0
            vcol.data[loop_i].color=(fac,fac,fac,1.0)
        mat_faces[(base_tid,layer_tid)].append(poly_idx)
    for (a,b), faces in mat_faces.items():
        mat=get_pair_material(map_root,a,b)
        if not mat: continue
        if mat.name not in me.materials:
            me.materials.append(mat)
        midx=me.materials.find(mat.name)
        for fi in faces:
            me.polygons[fi].material_index=midx

def _named_region_enum_items(self, context):
    return [(n, f"{n} ({len(v)} tiles)", "") for n,v in NAMED_REGIONS_DATA.items()] or [("none","(no named regions)","")]

class SRO_ProjectProps(PropertyGroup):
    sro_root: StringProperty(name="Silkroad Root", subtype='DIR_PATH')
    is_root_valid: BoolProperty(default=False)
    is_data_parsed: BoolProperty(default=False)
    import_mode: EnumProperty(
        name="Import Mode",
        items=[('NAMED',"Named Area","Import tiles by named area (regioninfo.txt)"),
               ('FULL',"Full Map","Import all active regions")],
        default='NAMED'
    )
    named_region_choice: EnumProperty(name="Area", items=_named_region_enum_items)
    height_scale: FloatProperty(name="Height Scale", default=1.0, min=0.01, max=100.0)
    import_textures: BoolProperty(name="Paint Terrain", default=True)

class SRO_OT_ParseData(Operator):
    bl_idname="sro.parse_data"; bl_label="Parse Game Data"
    def execute(self, ctx):
        p=ctx.scene.sro_props
        root=bpy.path.abspath(p.sro_root)
        if not validate_root(root):
            self.report({'ERROR'},"Invalid Silkroad root or missing Map/mapinfo.mfo.")
            p.is_root_valid=False
            return {'CANCELLED'}
        p.is_root_valid=True
        map_root=os.path.join(root,"Map")
        try:
            global REGION_ORIENT, PARSED_REGIONS, NAMED_REGIONS_DATA, TERRAIN_DDJ, WARN_CNT, MATERIAL_CACHE, TEXTURE_CACHE
            WARN_CNT=0
            MATERIAL_CACHE.clear(); TEXTURE_CACHE.clear()
            regs=parse_mfo(os.path.join(map_root,"mapinfo.mfo"))
            REGION_ORIENT=detect_orient(map_root, regs)
            exist=[]
            for (rx,rz,db) in regs:
                a,b=(rx,rz) if REGION_ORIENT=="XZ" else (rz,rx)
                if find_res(map_root,a,b,".m"): exist.append((rx,rz,db))
            PARSED_REGIONS=sorted(exist, key=lambda t:(t[0],t[1]))
            NAMED_REGIONS_DATA=parse_regioninfo(os.path.join(root,"Data","regioninfo.txt"))
            TERRAIN_DDJ=parse_tile2d_ifo(root)
            p.is_data_parsed=True
            self.report({'INFO'},f"Parsed: {len(PARSED_REGIONS)} regions, {len(NAMED_REGIONS_DATA)} named areas. Orientation: {REGION_ORIENT}")
        except Exception as e:
            self.report({'ERROR'}, f"Parse failed: {e}")
            p.is_data_parsed=False
            return {'CANCELLED'}
        return {'FINISHED'}

class SRO_OT_ExecuteImport(Operator):
    bl_idname="sro.execute_import"; bl_label="Import Map"
    @classmethod
    def poll(cls, ctx):
        p=ctx.scene.sro_props
        return p.is_root_valid and p.is_data_parsed
    def execute(self, ctx):
        ctx.space_data.clip_end=75000.0
        p=ctx.scene.sro_props
        root=bpy.path.abspath(p.sro_root)
        map_root=os.path.join(root,"Map")
        if p.import_mode=='NAMED':
            area=p.named_region_choice
            if area in NAMED_REGIONS_DATA:
                tiles=list(NAMED_REGIONS_DATA[area]); coll_name=f"SRO_Area_{area}"
            else:
                self.report({'ERROR'},"Invalid area.")
                return {'CANCELLED'}
        else:
            tiles=[(x,z) for (x,z,db) in PARSED_REGIONS]; coll_name="SRO_Full_Map"
        if not tiles:
            self.report({'WARNING'}, "No regions to import.")
            return {'CANCELLED'}
        tiles=sorted(set(tiles))
        zdeg=-90.0 if REGION_ORIENT=="ZX" else 0.0
        zrad=math.radians(zdeg)
        print(f"\n[INFO] Starting import: mode={p.import_mode}, tiles={len(tiles)}, Z-rot={zdeg} deg, textures={'ON' if p.import_textures else 'OFF'}")
        coll=ensure_collection(coll_name)
        for ob in list(coll.objects):
            try: bpy.data.objects.remove(ob, do_unlink=True)
            except: pass
        MATERIAL_CACHE.clear()
        created=0; t0=time.time()
        for i,(rx,rz) in enumerate(tiles,1):
            a,b=(rx,rz) if REGION_ORIENT=="XZ" else (rz,rx)
            mpath=find_res(map_root,a,b,".m")
            if not mpath:
                print(f"  [WARN] Missing .m for region ({rx},{rz})"); continue
            name=f"Region_{rx:03d}_{rz:03d}"
            try:
                H,Tex=read_mapm_heights_and_tex(mpath)
                cx,cy=region_center_world(rx,rz,REGION_ORIENT)
                obj=create_grid_object(name,cx,cy)
                for c in list(obj.users_collection): c.objects.unlink(obj)
                coll.objects.link(obj)
                apply_heights(obj,H,p.height_scale)
                obj.rotation_euler=(0,0,zrad)
                if p.import_textures and TERRAIN_DDJ:
                    me=obj.data
                    tex_flat=[0]*(len(me.vertices))
                    for vid in range(len(me.vertices)):
                        r=vid//VERTS_PER_AXIS; c=vid%VERTS_PER_AXIS
                        tex_flat[vid]=Tex[r][c]
                    paint_region(obj, tex_flat, map_root)
                created+=1
                print(f"  [{i:04d}/{len(tiles)}] OK: {name} processed.")
            except Exception as e:
                print(f"  [ERROR] Failed region ({rx},{rz}): {e}")
        dt=time.time()-t0
        print(f"[OK] Import finished: {created} regions created/updated in {dt:.2f}s")
        self.report({'INFO'}, f"Import finished: {created} tiles in {dt:.2f}s.")
        return {'FINISHED'}

class SRO_PT_Project(Panel):
    bl_label="SRO Project"
    bl_idname="SRO_PT_Project"
    bl_space_type='VIEW_3D'
    bl_region_type='UI'
    bl_category="SRO Project"
    def draw(self, ctx):
        layout=self.layout
        p=ctx.scene.sro_props
        b=layout.box(); b.label(text="1. SRO Game Client", icon='FILE_FOLDER'); b.prop(p,"sro_root",text=""); b.operator(SRO_OT_ParseData.bl_idname, icon='FILE_REFRESH')
        if p.is_root_valid and p.is_data_parsed:
            b=layout.box(); b.label(text="2. Import Settings", icon='IMPORT')
            col=b.column(align=True); col.prop(p,"import_mode",expand=True)
            if p.import_mode=='NAMED': col.prop(p,"named_region_choice")
            col.prop(p,"height_scale"); col.prop(p,"import_textures")
            layout.separator(); layout.operator(SRO_OT_ExecuteImport.bl_idname, icon='PLAY', text="Import Map")

CLASSES=(SRO_ProjectProps,SRO_OT_ParseData,SRO_OT_ExecuteImport,SRO_PT_Project)

def register():
    for c in CLASSES: bpy.utils.register_class(c)
    bpy.types.Scene.sro_props=PointerProperty(type=SRO_ProjectProps)
    _banner()

def unregister():
    if hasattr(bpy.types.Scene,"sro_props"): del bpy.types.Scene.sro_props
    for c in reversed(CLASSES):
        try: bpy.utils.unregister_class(c)
        except: pass
    TEXTURE_CACHE.clear(); MATERIAL_CACHE.clear()

if __name__ == "__main__":
    register()
