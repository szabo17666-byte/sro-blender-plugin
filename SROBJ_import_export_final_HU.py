import bpy
import os
import bmesh
import mathutils # mathutils importálva
from bpy.props import StringProperty, PointerProperty, BoolProperty
from bpy.types import Operator, Panel, PropertyGroup
from bpy_extras.io_utils import ImportHelper, ExportHelper

bl_info = {
    "name": "Silkroad OBJ Importer/Exporter (Advanced)",
    "author": "szabo176",
    "blender": (4, 1, 0), # Blender 4.x kompatibilitás: általánosan 4.1.0-t javaslok, vagy pontosan a kívánt verziót
    "version": (2, 5, 2), # Plugin verziószáma frissítve a javítások miatt
    "location": "View3D Sidebar > Silkroad Tab",
    "description": "Import/Export .srobj files with vertex groups, UVs and automatic texture assignment (DDS/PNG/JPG support).",
    "category": "Import-Export"
}

# --- Segédfüggvények ---

def name_exists(name):
    """Ellenőrzi, hogy létezik-e már ilyen nevű objektum a Blenderben."""
    return name in bpy.data.objects

# --- Import Funkció (bmesh alapú, textúra hozzárendeléssel) ---

def import_srobj_advanced(filepath, texturepath, flip_uv_v=False):
    """
    Silkroad OBJ (.srobj) fájl importálása bmesh segítségével,
    textúra hozzárendeléssel és opcionális UV V-flip-pel.
    """
    verts = []
    uv_coords_from_file = []
    faces_data = []

    try:
        with open(filepath, 'r', encoding='utf-8') as f: # Hozzáadva az encoding='utf-8' a robusztusabb fájlolvasáshoz
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split()
                if not parts: # Üres sorok ellenőrzése split után
                    continue

                if parts[0] == "o":
                    # Az 'o' sor után az objektum nevét olvassuk, de az srobj formátumban ez nem mindig használt
                    # A kód később a fájlnévből generálja az objektum nevet, ami rendben van.
                    # Ha szükség van a fájlban lévő 'o' név használatára:
                    # obj_name_from_file = parts[1] if len(parts) > 1 else "unnamed_object"
                    pass # Jelenleg nem használjuk fel, de a jövőre nézve jó tudni
                elif parts[0] == "v":
                    # Silkroad (X, Z, Y) -> Blender (X, -Y, Z)
                    # Ez a konverzió specifikus a Silkroad-hoz, és rendben van, ha ez a helyes.
                    if len(parts) < 4: raise ValueError(f"Hibás 'v' sor formátum: {line}")
                    x, z, y = map(float, parts[1:4])
                    verts.append((x, -y, z))
                elif parts[0] == "vt":
                    if len(parts) < 3: raise ValueError(f"Hibás 'vt' sor formátum: {line}")
                    u, v = map(float, parts[1:3])
                    uv_coords_from_file.append((u, v))
                elif parts[0] == "f":
                    if len(parts) < 4: raise ValueError(f"Hibás 'f' sor formátum: {line}. A face-eknek legalább 3 vertex-re van szükségük.")
                    face_verts_uvs = []
                    # Csak az első 3 vertex-et vesszük (háromszögek)
                    # További ellenőrzés, hogy a string nem üres-e, mielőtt int-é konvertáljuk
                    for part in parts[1:4]:
                        indices = part.split('/')
                        if not indices[0]: raise ValueError(f"Üres vertex index az 'f' sorban: {line}")
                        v_idx = int(indices[0]) - 1
                        
                        uv_idx = -1
                        if len(indices) > 1 and indices[1]: # Ellenőrizzük, hogy van-e UV index, és nem üres-e
                            uv_idx = int(indices[1]) - 1
                        
                        face_verts_uvs.append((v_idx, uv_idx))
                    
                    if len(face_verts_uvs) == 3:
                        faces_data.append(tuple(face_verts_uvs))
                    else:
                        print(f"Figyelem: Nem háromszög alakú face található az SROBJ fájlban, kihagyva: {line}")

    except Exception as e:
        raise Exception(f"Hiba az SROBJ fájl olvasása közben '{filepath}': {e}")

    # Mesh és Objektum létrehozása bmesh segítségével
    base_name = os.path.basename(filepath).split('.')[0]
    final_obj_name = base_name
    idx = 0
    while name_exists(final_obj_name):
        idx += 1
        final_obj_name = f"{base_name}.{idx:03d}"
    
    mesh = bpy.data.meshes.new(final_obj_name + "_Mesh")
    obj = bpy.data.objects.new(final_obj_name, mesh)
    bpy.context.collection.objects.link(obj)
    bpy.context.view_layer.objects.active = obj
    bpy.context.view_layer.update() # Fontos lehet az objektum hozzáadásakor

    bm = bmesh.new()
    # A vertexek közvetlen hozzáadása a bmesh-hez, majd a mesh-hez írás.
    # Eredeti: bm.from_mesh(mesh) - erre nincs szükség, ha üres mesh-ből indulunk
    
    bm_verts = [bm.verts.new(v) for v in verts]
    bm.verts.ensure_lookup_table()

    # Fontos: A UV réteg létrehozása azután, hogy a bm.from_mesh(mesh) vagy bm.verts.new() hívások befejeződtek,
    # és a bmesh már tartalmazza a vertexeket.
    uv_layer = bm.loops.layers.uv.new("UVMap")

    # Face-ek és UV-k hozzárendelése
    for face_info in faces_data:
        try:
            # Győződjünk meg róla, hogy a vertex indexek érvényesek
            bm_face_verts = []
            for v_idx, _ in face_info:
                if v_idx < 0 or v_idx >= len(bm_verts):
                    raise IndexError(f"Érvénytelen vertex index ({v_idx}) a face-ben: {face_info}")
                bm_face_verts.append(bm_verts[v_idx])
            
            # Ellenőrizzük, hogy a face nem-kétszeres vertexeket tartalmaz-e
            if len(set(bm_face_verts)) != 3:
                print(f"Figyelem: Duplikált vertexek a face-ben {face_info}. Kihagyva.")
                continue

            bm_face = bm.faces.new(tuple(bm_face_verts))
            bm_face.normal_update() # Frissítjük a normalt a face létrehozása után
            
            for loop, (v_idx, uv_idx) in zip(bm_face.loops, face_info):
                if uv_idx != -1 and uv_idx < len(uv_coords_from_file):
                    uv_coord = uv_coords_from_file[uv_idx]
                    # Az UV V-flip-et itt is alkalmazhatjuk a bmesh szinten, ha szükséges
                    # Jelenleg a material node-ban van, ami általában jobb, mert módosítható
                    loop[uv_layer].uv = uv_coord
                else:
                    print(f"Figyelem: Érvénytelen UV index {uv_idx} a {v_idx} vertexhez a {face_info} face-ben. Az UV nem került beállításra.")
        except ValueError as e:
            print(f"Figyelem: Nem sikerült létrehozni a face-t a {face_info} adatokkal - {e}. Kihagyva.")
        except IndexError as e:
            print(f"Hiba: Érvénytelen vertex vagy UV index a face-ben {face_info}: {e}. Kihagyva.")
        except Exception as e:
            print(f"Váratlan hiba történt a face {face_info} feldolgozása közben: {e}. Kihagyva.")

    # A bmesh-ben lévő geometry ellenőrzése (opcionális, nagy fájloknál lassú lehet)
    # try:
    #     bm.verts.ensure_lookup_table()
    #     bm.edges.ensure_lookup_table()
    #     bm.faces.ensure_lookup_table()
    # except Exception as e:
    #     print(f"Figyelem: Hiba a bmesh lookup táblák frissítésekor: {e}")

    bm.to_mesh(mesh)
    mesh.update()
    bm.free()

    # --- Textúra és Material hozzárendelés ---
    if texturepath and os.path.exists(texturepath):
        try:
            mat = bpy.data.materials.new(name=final_obj_name + "_Material")
            mat.use_nodes = True
            obj.data.materials.append(mat)

            nodes = mat.node_tree.nodes
            links = mat.node_tree.links

            # Töröljük az összes meglévő node-ot a tiszta kezdethez
            for node in nodes:
                nodes.remove(node)

            principled_bsdf = nodes.new(type='ShaderNodeBsdfPrincipled')
            principled_bsdf.location = (400, 0)

            material_output = nodes.new(type='ShaderNodeOutputMaterial')
            material_output.location = (600, 0)

            image_texture = nodes.new(type='ShaderNodeTexImage')
            # Ellenőrizzük, hogy az image betöltése sikeres-e
            try:
                image_texture.image = bpy.data.images.load(texturepath)
            except RuntimeError as e: # Specifikusabb hibakezelés képbetöltéshez
                print(f"Hiba a textúra betöltésekor '{texturepath}': {e}. Kérjük, ellenőrizze a fájlformátumot és a Blender DDS támogatását.")
                # Nem dobunk itt kivételt, hogy a material legalább létrejöjjön textúra nélkül
                image_texture.image = None 
                
            image_texture.location = (0, 0)

            # UV Map node hozzáadása, hogy expliciten az "UVMap" réteget használja
            uv_map_node = nodes.new(type='ShaderNodeUVMap')
            uv_map_node.location = (-600, 100)
            uv_map_node.uv_map = "UVMap" # Biztosítjuk, hogy a megfelelő UV Map-et használja

            mapping = nodes.new(type='ShaderNodeMapping')
            mapping.location = (-200, 0)
            
            if flip_uv_v:
                mapping.inputs['Scale'].default_value[1] = -1
            
            # Linkek módosítása az UV Map node bevonásával
            links.new(uv_map_node.outputs['UV'], mapping.inputs['Vector'])
            links.new(mapping.outputs['Vector'], image_texture.inputs['Vector'])
            
            # Csak akkor linkeljük a textúrát, ha sikeresen betöltődött
            if image_texture.image:
                links.new(image_texture.outputs['Color'], principled_bsdf.inputs['Base Color'])
            else:
                print("Figyelem: A textúra betöltése sikertelen volt, az alap szín beállítása az anyagon.")
                # Beállíthatunk egy alap színt, ha nincs textúra
                principled_bsdf.inputs['Base Color'].default_value = (0.8, 0.8, 0.8, 1.0) # Szürke
                
            links.new(principled_bsdf.outputs['BSDF'], material_output.inputs['Surface'])
            
            image_texture.interpolation = 'Closest'

        except Exception as e:
            print(f"Hiba a textúra vagy material hozzárendelésekor: {e}")
            # print(traceback.format_exc()) # Fejlesztéshez: traceback kiírása
            raise # Fontos, hogy a hibát továbbítsuk, ha kritikus
    else:
        print(f"Nincs megadva textúra útvonal, vagy a fájl nem létezik: '{texturepath}'. Material létrehozása textúra nélkül.")
        mat = bpy.data.materials.new(name=final_obj_name + "_Material_NoTex")
        obj.data.materials.append(mat)


# --- Export Funkció ---

def export_srobj(path, context):
    """Silkroad OBJ (.srobj) fájl exportálása."""
    obj = context.object    
    if obj is None or obj.type != 'MESH':
        raise IOError('Ki kell választania egy MESH objektumot az exportáláshoz.')

    # A Blender 2.8+ verziókban az adatokhoz való hozzáféréshez aktiválni kell a függvénymezőket
    # Ha módosítás történik, ensure_lookup_table() és mesh.update() szükséges lehet.
    mesh = obj.data

    meshname = obj.name
    groupnames = []
    # A vertex group-ok tárolása: [group1_idx, group2_idx] (255 ha nincs)
    vertgroups_export = [[255, 255] for _ in range(len(mesh.vertices))] 
    verts = []
    
    for group in obj.vertex_groups:
        groupnames.append(group.name)

    # A mesh.calc_utils_looptriangles() vagy bmesh konverzió jobb lehet
    # A vertex.co a lokális koordináták.
    # A Blender globális koordinátáihoz: obj.matrix_world @ vert.co
    # Azonban az SROBJ valószínűleg lokális koordinátákat vár, így a vert.co megfelelő
    for v_idx, vert in enumerate(mesh.vertices):
        # A Blender (X, Y, Z) -> Silkroad (X, Z, -Y) konverzió exportáláskor
        verts.append((vert.co.x, vert.co.z, -vert.co.y))

        current_groups_for_vert = []
        for vg_entry in vert.groups:
            # Csak azokat a csoportokat vesszük figyelembe, amelyeknek 0-nál nagyobb súlyuk van
            if vg_entry.weight > 0.0001: # Kis tolerancia a lebegőpontos számokhoz
                current_groups_for_vert.append(vg_entry.group)
        
        # Silkroad formátum feltételezi, hogy max 2 csoportot kezel
        if len(current_groups_for_vert) > 0:
            vertgroups_export[v_idx][0] = current_groups_for_vert[0]
        if len(current_groups_for_vert) > 1:
            vertgroups_export[v_idx][1] = current_groups_for_vert[1]
        # Ha több mint 2 csoportja van egy vertexnek, a többi figyelmen kívül marad
        if len(current_groups_for_vert) > 2:
            print(f"Figyelem: A {v_idx} vertexnek több mint 2 vertex csoportja van. Csak az első kettő kerül exportálásra.")


    uv_layer = mesh.uv_layers.active
    if not uv_layer:
        print("Figyelem: Nincs aktív UV map található az exportáláshoz. Az UV koordináták 0,0-ra lesznek beállítva.")
    
    unique_uv_map = {}
    exported_uv_list = []
    unique_normals_map = {}
    exported_normal_list = []
    
    face_data_for_export = []
    
    # A loop_triangles garantálja, hogy a mesh háromszögekre van bontva
    # Ez Blender 2.8+ esetén a loop.normal helyett használatos a face normalhoz.
    mesh.calc_loop_triangles() 

    for tri in mesh.loop_triangles: # loop_triangles használata polygonok helyett
        # A Blender normalja (poly.normal) már objektum lokális.
        bl_normal = tri.normal
        # A Silkroad koordináta-rendszerhez illeszkedő normal konverziója (Y és Z felcserélése)
        sr_normal = mathutils.Vector((bl_normal.x, bl_normal.z, -bl_normal.y)) # X, Z, -Y a normalhoz
        sr_normal.normalize()

        normal_key = tuple(round(coord, 6) for coord in sr_normal)
        if normal_key not in unique_normals_map:
            unique_normals_map[normal_key] = len(exported_normal_list)
            exported_normal_list.append(sr_normal)
        face_normal_idx = unique_normals_map[normal_key]

        poly_face_data = []
        for loop_idx in tri.loops: # loop_indices helyett tri.loops használata
            v_idx = mesh.loops[loop_idx].vertex_index

            uv_coord = uv_layer.data[loop_idx].uv if uv_layer else mathutils.Vector((0.0, 0.0))
            uv_key = tuple(round(coord, 6) for coord in uv_coord)
            if uv_key not in unique_uv_map:
                unique_uv_map[uv_key] = len(exported_uv_list)
                exported_uv_list.append(uv_coord)
            uv_idx = unique_uv_map[uv_key]
            
            poly_face_data.append((v_idx, uv_idx, face_normal_idx))
        
        face_data_for_export.append(poly_face_data)

    lines = []
    
    lines.append('#SROBJ by Perry\'s Blender plugin (Updated by AI Assistant).\n')
    lines.append(f'o {meshname}\n')

    # Group nevek exportálása
    for groupname in groupnames:
        lines.append(f'gn {groupname}\n')
    
    # Vertex group indexek exportálása
    for groups in vertgroups_export:
        lines.append(f'vg {groups[0]}/{groups[1]}\n')
    
    # Vertex koordináták exportálása
    for vert_co in verts:
        # **KRITIKUS HIBA JAVÍTVA:** Korábban vert_co[0]-t kétszer használt, most helyesen X, Z, -Y
        lines.append(f'v {vert_co[0]:.6f} {vert_co[1]:.6f} {vert_co[2]:.6f}\n') 
    
    # Normal vektorok exportálása
    for norm_co in exported_normal_list:
        lines.append(f'vn {norm_co[0]:.6f} {norm_co[1]:.6f} {norm_co[2]:.6f}\n')
        
    # UV koordináták exportálása
    for uv_co in exported_uv_list:
        lines.append(f'vt {uv_co[0]:.6f} {uv_co[1]:.6f}\n')
            
    # Face-ek exportálása (1-alapú indexeléssel)
    for face_loops in face_data_for_export:
        line_parts = []
        for v_idx, uv_idx, vn_idx in face_loops:
            line_parts.append(f"{v_idx+1}/{uv_idx+1}/{vn_idx+1}")
        lines.append(f"f {' '.join(line_parts)}\n")
            
    try:
        with open(path, 'w', encoding='utf-8') as f: # encoding='utf-8' hozzáadva
            f.writelines(lines)
    except Exception as e:
        raise Exception(f"Hiba az SROBJ fájl írása közben '{path}': {e}")


# --- UI és Operátorok ---

class SROBJProperties(PropertyGroup):
    """Tulajdonságok az import/export beállításokhoz."""
    import_srobj_path: StringProperty(
        name="SROBJ Fájl",
        subtype='FILE_PATH',
        default="",
        description="Válaszd ki az importálni kívánt .srobj fájlt."
    )
    import_texture_path: StringProperty(
        name="Textúra Fájl",
        subtype='FILE_PATH',
        default="",
        description="Válaszd ki az objektumhoz hozzárendelni kívánt textúra fájlt (.dds, .png, .jpg)."
    )
    import_flip_uv_v: BoolProperty(
        name="UV fordítás (V-tengely)",
        description="Függőlegesen megfordítja az importált UV-koordinátákat (hasznos néhány játék textúrájához).",
        default=True
    )
    export_srobj_path: StringProperty(
        name="Export SROBJ Fájl",
        subtype='FILE_PATH',
        default="",
        description="Add meg az exportált .srobj fájl útvonalát és nevét."
    )

class SROBJ_OT_ImportUI(Operator):
    """Import SROBJ from UI"""
    bl_idname = "srobj.import_ui"
    bl_label = "SROBJ importálása"
    bl_description = "SROBJ fájl importálása opcionális textúra hozzárendeléssel."

    def execute(self, context):
        props = context.scene.srobj_props
        if not props.import_srobj_path:
            self.report({'ERROR'}, "Az SROBJ fájl útvonala nincs beállítva.")
            return {'CANCELLED'}
        
        try:
            import_srobj_advanced(props.import_srobj_path, props.import_texture_path, props.import_flip_uv_v)
            self.report({'INFO'}, f"Sikeresen importálva: {os.path.basename(props.import_srobj_path)}")
        except Exception as e:
            self.report({'ERROR'}, f"Importálás sikertelen: {e}")
            print(f"SROBJ Import Hiba: {e}") # A konzolba is kiírjuk a részletesebb hibát
            return {'CANCELLED'}
        return {'FINISHED'}

class SROBJ_OT_ExportUI(Operator):
    """Export SROBJ from UI"""
    bl_idname = "srobj.export_ui"
    bl_label = "SROBJ exportálása"
    bl_description = "A kiválasztott MESH objektum exportálása .srobj fájlba."

    def execute(self, context):
        props = context.scene.srobj_props
        if not props.export_srobj_path:
            self.report({'ERROR'}, "Az exportálási fájl útvonala nincs beállítva.")
            return {'CANCELLED'}
        
        if context.object is None or context.object.type != 'MESH':
            self.report({'ERROR'}, "Nincs MESH objektum kiválasztva az exportáláshoz.")
            return {'CANCELLED'}

        export_path = props.export_srobj_path
        if not export_path.lower().endswith(".srobj"):
            export_path += ".srobj"

        try:
            export_srobj(export_path, context)
            self.report({'INFO'}, f"Sikeresen exportálva: {os.path.basename(export_path)}")
        except Exception as e:
            self.report({'ERROR'}, f"Exportálás sikertelen: {e}")
            print(f"SROBJ Export Hiba: {e}") # A konzolba is kiírjuk a részletesebb hibát
            return {'CANCELLED'}
        return {'FINISHED'}

class VIEW3D_PT_srobj_panel(Panel):
    """SROBJ Importer/Exporter UI Panel."""
    bl_label = "Silkroad SROBJ Eszközök" # Cím magyarosítva
    bl_idname = "VIEW3D_PT_srobj_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Silkroad'

    def draw(self, context):
        layout = self.layout
        props = context.scene.srobj_props

        # --- Importálás szekció ---
        box = layout.box()
        box.label(text="SROBJ importálása", icon='IMPORT')
        box.prop(props, "import_srobj_path")
        box.prop(props, "import_texture_path")
        box.prop(props, "import_flip_uv_v")
        box.operator(SROBJ_OT_ImportUI.bl_idname, text="SROBJ importálása")

        # --- Exportálás szekció ---
        box = layout.box()
        box.label(text="SROBJ exportálása", icon='EXPORT')
        box.prop(props, "export_srobj_path")
        box.operator(SROBJ_OT_ExportUI.bl_idname, text="SROBJ exportálása")


# --- Regisztráció ---

classes = (
    SROBJProperties,
    SROBJ_OT_ImportUI,
    SROBJ_OT_ExportUI,
    VIEW3D_PT_srobj_panel
)

def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.srobj_props = PointerProperty(type=SROBJProperties)

def unregister():
    # Fontos a fordított sorrend a függőségek miatt az unregister során
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
    del bpy.types.Scene.srobj_props

if __name__ == "__main__":
    register()