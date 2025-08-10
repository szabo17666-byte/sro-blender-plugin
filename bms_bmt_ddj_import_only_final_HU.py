bl_info = {
    "name": "Silkroad JMX Importer",
    "author": "szabo176",
    "version": (3, 2, 7), # FINAL VERSION
    "blender": (4, 5, 0),
    "location": "View3D Sidebar > Silkroad",
    "description": "Imports BMS models with BMT/DDJ support, orientation and vertex groups.",
    "category": "Import-Export"
}

import bpy
import os
import bmesh
import struct
import tempfile
import io
import math

try:
    from PIL import Image
    PILLOW_OK = True
except ImportError:
    PILLOW_OK = False

from bpy.props import StringProperty, PointerProperty, BoolProperty
from bpy.types import Operator, Panel, PropertyGroup

# --- Segédfüggvények ---
def read_int(f): return int.from_bytes(f.read(4), 'little')
def read_short(f): return int.from_bytes(f.read(2), 'little')
def read_byte(f): return int.from_bytes(f.read(1), 'little')
def read_float(f): return struct.unpack('<f', f.read(4))[0]
def read_color4(f): return struct.unpack('<4f', f.read(16))
def read_str(f):
    str_len = read_int(f);
    if str_len <= 0: return ""
    str_bytes = f.read(str_len)
    try: return str_bytes.decode("cp949")
    except UnicodeDecodeError: return str_bytes.decode("utf-8", errors='ignore')
def name_exists(name): return name in bpy.data.objects

# --- Képfeldolgozás ---
def convert_ddj_to_png(ddj_path):
    if not PILLOW_OK: raise ImportError("A Pillow (PIL) könyvtár szükséges.")
    try:
        with open(ddj_path, 'rb') as f:
            f.seek(20); dds_data = f.read()
        image = Image.open(io.BytesIO(dds_data))
        temp_png_path = os.path.join(tempfile.gettempdir(), os.path.basename(ddj_path) + ".png")
        image.save(temp_png_path, 'PNG')
        return temp_png_path
    except Exception as e:
        print(f"DDJ -> PNG konverziós hiba: {e}"); return None

# --- BMT Fájl Értelmező ---
def read_bmt_file(bmt_filepath):
    if not bmt_filepath or not os.path.exists(bmt_filepath): return {}
    materials = {}
    try:
        with open(bmt_filepath, 'rb') as f:
            if b"JMXVBMT" not in f.read(12): return {}
            count = read_int(f)
            for _ in range(count):
                mat_name = read_str(f)
                props = {
                    'name': mat_name, 'diffuse': read_color4(f), 'ambient': read_color4(f),
                    'specular': read_color4(f), 'emissive': read_color4(f),
                    'shininess': read_float(f), 'flags': read_int(f), 'texture': None
                }
                if props['flags'] & 0x100:
                    props['texture'] = read_str(f)
                    f.seek(7, 1)
                materials[mat_name] = props
    except Exception as e:
        print(f"Hiba a BMT fájl olvasása közben: {e}")
    return materials

# --- UI és Operátorok ---
class SROProperties(PropertyGroup):
    import_bms_filepath: StringProperty(name=".BMS File", subtype='FILE_PATH')
    import_bmt_filepath: StringProperty(name=".BMT File", subtype='FILE_PATH')
    import_ddj_filepath: StringProperty(name=".DDJ|.DDS. PNG File", subtype='FILE_PATH')
    use_alpha_blend: BoolProperty(name="Enable Transparency", default=False)

class SRO_OT_ImportUI(Operator):
    bl_idname = "silkroad.import_bms"
    bl_label = "Modell Importálása"

    def execute(self, context):
        print(f"\n--- Új Importálási Folyamat Indul (v{bl_info['version'][0]}.{bl_info['version'][1]}.{bl_info['version'][2]}) ---")
        if not PILLOW_OK: self.report({'ERROR'}, "Pillow könyvtár hiányzik!"); return {'CANCELLED'}
        props = context.scene.sro_props
        bms_path, bmt_path, ddj_path = props.import_bms_filepath, props.import_bmt_filepath, props.import_ddj_filepath

        if not bms_path or not os.path.exists(bms_path):
            self.report({'ERROR'}, "BMS fájl nincs kiválasztva vagy nem létezik."); return {'CANCELLED'}
        try:
            bmt_data = read_bmt_file(bmt_path)
            
            verts, uvs, faces, bones, weights = [], [], [], [], []
            mesh_name_from_bms, mat_name_from_bms = "", ""

            print(f"[LOG] BMS fájl megnyitása: {os.path.basename(bms_path)}")
            with open(bms_path, 'rb') as f:
                if b"JMXVBMS" not in f.read(12): raise ValueError("Nem érvényes BMS fájl.")
                p_verticies, p_bones, p_faces = read_int(f), read_int(f), read_int(f)
                for _ in range(7): read_int(f)
                for _ in range(5): read_int(f)
                mesh_name_from_bms, mat_name_from_bms = read_str(f), read_str(f)
                
                f.seek(p_verticies); vcount = read_int(f)
                print(f"  [LOG] {vcount} vertex beolvasása...")
                for _ in range(vcount):
                    verts.append((read_float(f), -read_float(f), read_float(f)))
                    f.seek(12, 1); u, v = read_float(f), read_float(f); uvs.append((u, 1.0 - v)); f.seek(12, 1)
                f.seek(p_faces); fcount = read_int(f)
                print(f"  [LOG] {fcount} lap (face) beolvasása...")
                faces = [tuple(read_short(f) for _ in range(3)) for _ in range(fcount)]
                if p_bones > 0:
                    f.seek(p_bones); bcount = read_int(f)
                    if bcount > 0:
                        print(f"  [LOG] {bcount} csont (bone) és súlyozás beolvasása...")
                        bones = [read_str(f) for _ in range(bcount)]
                        weights = [(read_byte(f), read_short(f), read_byte(f), read_short(f)) for _ in range(vcount)]
            print("[LOG] BMS adatok sikeresen a memóriába olvasva.")
            
            print("[LOG] Blender objektum létrehozása...")
            obj_name = mesh_name_from_bms or os.path.basename(bms_path).split('.')[0]
            mesh = bpy.data.meshes.new(obj_name + '_Mesh')
            obj = bpy.data.objects.new(obj_name, mesh)
            context.collection.objects.link(obj); context.view_layer.objects.active = obj
            mesh.from_pydata(verts, [], faces); mesh.update()
            
            bm = bmesh.new(); bm.from_mesh(mesh)
            uv_layer = bm.loops.layers.uv.new("UVMap")
            for face in bm.faces:
                for loop in face.loops: loop[uv_layer].uv = uvs[loop.vert.index]
            bm.to_mesh(mesh); bm.free()
            if bones:
                print(f"  [LOG] Vertex csoportok létrehozása: {bones}")
                for b_name in bones: obj.vertex_groups.new(name=b_name)
                for i, w in enumerate(weights):
                    bi1, bw1, bi2, bw2 = w
                    if bw1 > 0 and bi1 < len(bones): obj.vertex_groups[bones[bi1]].add([i], bw1 / 10000.0, 'REPLACE')
                    if bw2 > 0 and bi2 < len(bones): obj.vertex_groups[bones[bi2]].add([i], bw2 / 10000.0, 'ADD')
            print("[LOG] Geometria és súlyozás kész.")

            print("[LOG] Anyag létrehozása...")
            mat_props = bmt_data.get(mat_name_from_bms)
            final_mat_name = (mat_props.get('name') if mat_props else mat_name_from_bms) or "Material"
            mat = bpy.data.materials.new(name=final_mat_name)
            mat.use_nodes = True; obj.data.materials.append(mat)
            
            # VÉGLEGES JAVÍTÁS: Külön sorokban definiáljuk a változókat.
            nodes = mat.node_tree.nodes
            links = mat.node_tree.links
            bsdf = nodes.get("Principled BSDF")
            
            final_ddj_path = ddj_path
            if mat_props and mat_props.get('texture'):
                bmt_dir = os.path.dirname(bmt_path) if bmt_path else ""
                path_from_bmt = os.path.join(bmt_dir, mat_props['texture'])
                if os.path.exists(path_from_bmt): final_ddj_path = path_from_bmt
            
            if final_ddj_path and os.path.exists(final_ddj_path):
                png_path = convert_ddj_to_png(final_ddj_path)
                if png_path and os.path.exists(png_path):
                    print(f"  [LOG] Textúra node-ok létrehozása...")
                    tex_node = nodes.new("ShaderNodeTexImage")
                    tex_node.image = bpy.data.images.load(png_path)
                    tex_node.interpolation = 'Closest'
                    uv_map_node = nodes.new(type='ShaderNodeUVMap'); uv_map_node.uv_map = "UVMap"
                    mapping_node = nodes.new(type='ShaderNodeMapping')
                    links.new(uv_map_node.outputs['UV'], mapping_node.inputs['Vector'])
                    links.new(mapping_node.outputs['Vector'], tex_node.inputs['Vector'])
                    links.new(bsdf.inputs['Base Color'], tex_node.outputs['Color'])
                    if props.use_alpha_blend:
                        mat.blend_method = 'BLEND'
                        links.new(bsdf.inputs['Alpha'], tex_node.outputs['Alpha'])
            
            if mat_props: 
                print(f"  [LOG] BMT adatok alkalmazása: Specular/Roughness...")
                bsdf.inputs['Base Color'].default_value = mat_props['diffuse']
                specular_value = mat_props['specular'][0]
                roughness_value = max(0.0, min(1.0, 1.0 - mat_props['shininess']))
                bsdf.inputs['Specular IOR Level'].default_value = specular_value
                bsdf.inputs['Roughness'].default_value = roughness_value
                print(f"    [LOG] Beállított Specular: {specular_value:.4f}, Roughness: {roughness_value:.4f}")
            else:
                bsdf.inputs['Base Color'].default_value = (0.8, 0.8, 0.8, 1.0)

            self.report({'INFO'}, f"Sikeres import: {obj.name}")
            print("--- Importálási Folyamat Befejeződött ---")
            return {'FINISHED'}
        except Exception as e:
            self.report({'ERROR'}, f"Importálás sikertelen: {e}"); import traceback; traceback.print_exc()
            return {'CANCELLED'}

class VIEW3D_PT_sro_panel(Panel):
    bl_label="Silkroad Eszközök"; bl_idname="VIEW3D_PT_silkroad_panel"; bl_space_type='VIEW_3D'; bl_region_type='UI'; bl_category='Silkroad'
    def draw(self, context):
        props=context.scene.sro_props; box=self.layout.box()
        box.label(text="Modell Importálása", icon='IMPORT')
        box.prop(props, "import_bms_filepath"); box.prop(props, "import_bmt_filepath"); box.prop(props, "import_ddj_filepath")
        box.prop(props, "use_alpha_blend")
        box.operator(SRO_OT_ImportUI.bl_idname)

# --- Regisztráció ---
classes = (SROProperties, SRO_OT_ImportUI, VIEW3D_PT_sro_panel)
def register():
    if not PILLOW_OK: print("FIGYELEM: A 'Pillow' Python könyvtár nincs telepítve.")
    for cls in classes: bpy.utils.register_class(cls)
    bpy.types.Scene.sro_props = PointerProperty(type=SROProperties)
def unregister():
    for cls in reversed(classes): bpy.utils.unregister_class(cls)
    del bpy.types.Scene.sro_props
if __name__ == "__main__":
    register()