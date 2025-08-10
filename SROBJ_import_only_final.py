import bpy
import os
import bmesh
import mathutils # mathutils importálva
from bpy.props import StringProperty, PointerProperty, BoolProperty
from bpy.types import Operator, Panel, PropertyGroup
from bpy_extras.io_utils import ImportHelper, ExportHelper

bl_info = {
    "name": "Silkroad OBJ Importer (Advanced)", # Renamed to Importer only
    "author": "szabo176",
    "blender": (4, 1, 0), # Blender 4.x compatibility: generally recommend 4.1.0, or exact desired version
    "version": (2, 5, 2), # Plugin version updated due to fixes
    "location": "View3D Sidebar > Silkroad Tab",
    "description": "Import .srobj files with vertex groups, UVs and automatic texture assignment (DDS/PNG/JPG support).", # Description updated
    "category": "Import-Export"
}

# --- Helper Functions ---

def name_exists(name):
    """Checks if an object with this name already exists in Blender."""
    return name in bpy.data.objects

# --- Import Function (bmesh based, with texture assignment) ---

def import_srobj_advanced(filepath, texturepath, flip_uv_v=False):
    """
    Import Silkroad OBJ (.srobj) file using bmesh,
    with texture assignment and optional UV V-flip.
    """
    verts = []
    uv_coords_from_file = []
    faces_data = []

    try:
        with open(filepath, 'r', encoding='utf-8') as f: # Added encoding='utf-8' for more robust file reading
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split()
                if not parts: # Check for empty lines after split
                    continue

                if parts[0] == "o":
                    # After 'o' line, we read the object name, but in srobj format it's not always used
                    # The code later generates the object name from the filename, which is fine.
                    # If the 'o' name in the file needs to be used:
                    # obj_name_from_file = parts[1] if len(parts) > 1 else "unnamed_object"
                    pass # Currently not using it, but good to know for future reference
                elif parts[0] == "v":
                    # Silkroad (X, Z, Y) -> Blender (X, -Y, Z)
                    # This conversion is specific to Silkroad and is correct if this is the desired behavior.
                    if len(parts) < 4: raise ValueError(f"Invalid 'v' line format: {line}")
                    x, z, y = map(float, parts[1:4])
                    verts.append((x, -y, z))
                elif parts[0] == "vt":
                    if len(parts) < 3: raise ValueError(f"Invalid 'vt' line format: {line}")
                    u, v = map(float, parts[1:3])
                    uv_coords_from_file.append((u, v))
                elif parts[0] == "f":
                    if len(parts) < 4: raise ValueError(f"Invalid 'f' line format: {line}. Faces require at least 3 vertices.")
                    face_verts_uvs = []
                    # Taking only the first 3 vertices (triangles)
                    # Additional check to ensure string is not empty before converting to int
                    for part in parts[1:4]:
                        indices = part.split('/')
                        if not indices[0]: raise ValueError(f"Empty vertex index in 'f' line: {line}")
                        v_idx = int(indices[0]) - 1
                        
                        uv_idx = -1
                        if len(indices) > 1 and indices[1]: # Check if UV index exists and is not empty
                            uv_idx = int(indices[1]) - 1
                        
                        face_verts_uvs.append((v_idx, uv_idx))
                    
                    if len(face_verts_uvs) == 3:
                        faces_data.append(tuple(face_verts_uvs))
                    else:
                        print(f"Warning: Non-triangular face found in SROBJ file, skipping: {line}")

    except Exception as e:
        raise Exception(f"Error reading SROBJ file '{filepath}': {e}")

    # Create Mesh and Object using bmesh
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
    bpy.context.view_layer.update() # Can be important when adding the object

    bm = bmesh.new()
    # Original: bm.from_mesh(mesh) - not needed if starting with an empty mesh
    
    bm_verts = [bm.verts.new(v) for v in verts]
    bm.verts.ensure_lookup_table()

    # Important: Create UV layer after bm.from_mesh(mesh) or bm.verts.new() calls are finished,
    # and bmesh already contains vertices.
    uv_layer = bm.loops.layers.uv.new("UVMap")

    # Assign Faces and UVs
    for face_info in faces_data:
        try:
            # Ensure vertex indices are valid
            bm_face_verts = []
            for v_idx, _ in face_info:
                if v_idx < 0 or v_idx >= len(bm_verts):
                    raise IndexError(f"Invalid vertex index ({v_idx}) in face: {face_info}")
                bm_face_verts.append(bm_verts[v_idx])
            
            # Check if face contains non-duplicate vertices
            if len(set(bm_face_verts)) != 3:
                print(f"Warning: Duplicate vertices in face {face_info}. Skipping.")
                continue

            bm_face = bm.faces.new(tuple(bm_face_verts))
            bm_face.normal_update() # Update normal after face creation
            
            for loop, (v_idx, uv_idx) in zip(bm_face.loops, face_info):
                if uv_idx != -1 and uv_idx < len(uv_coords_from_file):
                    uv_coord = uv_coords_from_file[uv_idx]
                    # UV V-flip can also be applied here at bmesh level if needed
                    # Currently, it's in the material node, which is generally better as it can be modified
                    loop[uv_layer].uv = uv_coord
                else:
                    print(f"Warning: Invalid UV index {uv_idx} for vertex {v_idx} in face {face_info}. UV not set.")
        except ValueError as e:
            print(f"Warning: Failed to create face with data {face_info} - {e}. Skipping.")
        except IndexError as e:
            print(f"Error: Invalid vertex or UV index in face {face_info}: {e}. Skipping.")
        except Exception as e:
            print(f"Unexpected error occurred while processing face {face_info}: {e}. Skipping.")

    # Check geometry in bmesh (optional, can be slow for large files)
    # try:
    #     bm.verts.ensure_lookup_table()
    #     bm.edges.ensure_lookup_table()
    #     bm.faces.ensure_lookup_table()
    # except Exception as e:
    #     print(f"Warning: Error updating bmesh lookup tables: {e}")

    bm.to_mesh(mesh)
    mesh.update()
    bm.free()

    # --- Texture and Material Assignment ---
    if texturepath and os.path.exists(texturepath):
        try:
            mat = bpy.data.materials.new(name=final_obj_name + "_Material")
            mat.use_nodes = True
            obj.data.materials.append(mat)

            nodes = mat.node_tree.nodes
            links = mat.node_tree.links

            # Delete all existing nodes for a clean start
            for node in nodes:
                nodes.remove(node)

            principled_bsdf = nodes.new(type='ShaderNodeBsdfPrincipled')
            principled_bsdf.location = (400, 0)

            material_output = nodes.new(type='ShaderNodeOutputMaterial')
            material_output.location = (600, 0)

            image_texture = nodes.new(type='ShaderNodeTexImage')
            # Check if image loading is successful
            try:
                image_texture.image = bpy.data.images.load(texturepath)
            except RuntimeError as e: # More specific error handling for image loading
                print(f"Error loading texture '{texturepath}': {e}. Please check file format and Blender DDS support.")
                # Do not raise exception here, so that material can be created without texture at least
                image_texture.image = None 
                
            image_texture.location = (0, 0)

            # Add UV Map node to explicitly use the "UVMap" layer
            uv_map_node = nodes.new(type='ShaderNodeUVMap')
            uv_map_node.location = (-600, 100)
            uv_map_node.uv_map = "UVMap" # Ensure it uses the correct UV Map

            mapping = nodes.new(type='ShaderNodeMapping')
            mapping.location = (-200, 0)
            
            if flip_uv_v:
                mapping.inputs['Scale'].default_value[1] = -1
            
            # Modify links to include UV Map node
            links.new(uv_map_node.outputs['UV'], mapping.inputs['Vector'])
            links.new(mapping.outputs['Vector'], image_texture.inputs['Vector'])
            
            # Only link texture if it loaded successfully
            if image_texture.image:
                links.new(image_texture.outputs['Color'], principled_bsdf.inputs['Base Color'])
            else:
                print("Warning: Texture loading failed, setting base color on material.")
                # We can set a base color if there is no texture
                principled_bsdf.inputs['Base Color'].default_value = (0.8, 0.8, 0.8, 1.0) # Grey
                
            links.new(principled_bsdf.outputs['BSDF'], material_output.inputs['Surface'])
            
            image_texture.interpolation = 'Closest'

        except Exception as e:
            print(f"Error assigning texture or material: {e}")
            # print(traceback.format_exc()) # For development: print traceback
            raise # Important to propagate error if critical
    else:
        print(f"No texture path specified or file does not exist: '{texturepath}'. Creating material without texture.")
        mat = bpy.data.materials.new(name=final_obj_name + "_Material_NoTex")
        obj.data.materials.append(mat)


# --- Export Function (REMOVED) ---
# The entire export_srobj function is removed.


# --- UI and Operators ---

class SROBJProperties(PropertyGroup):
    """Properties for import settings.""" # Description updated
    import_srobj_path: StringProperty(
        name="SROBJ File",
        subtype='FILE_PATH',
        default="",
        description="Select the .srobj file to import."
    )
    import_texture_path: StringProperty(
        name="Texture File",
        subtype='FILE_PATH',
        default="",
        description="Select the texture file (.dds, .png, .jpg) to assign to the object."
    )
    import_flip_uv_v: BoolProperty(
        name="Flip UV (V-axis)",
        description="Flip imported UV coordinates vertically (useful for some game textures).",
        default=False # Changed default to False (unchecked)
    )
    # export_srobj_path and related properties are removed

class SROBJ_OT_ImportUI(Operator):
    """Import SROBJ from UI"""
    bl_idname = "srobj.import_ui"
    bl_label = "Import SROBJ"
    bl_description = "Import an SROBJ file with optional texture assignment."

    def execute(self, context):
        props = context.scene.srobj_props
        if not props.import_srobj_path:
            self.report({'ERROR'}, "SROBJ file path is not set.")
            return {'CANCELLED'}
        
        try:
            import_srobj_advanced(props.import_srobj_path, props.import_texture_path, props.import_flip_uv_v)
            self.report({'INFO'}, f"Successfully imported: {os.path.basename(props.import_srobj_path)}")
        except Exception as e:
            self.report({'ERROR'}, f"Import failed: {e}")
            print(f"SROBJ Import Error: {e}") # Also print detailed error to console
            return {'CANCELLED'}
        return {'FINISHED'}

# SROBJ_OT_ExportUI operator is removed.

class VIEW3D_PT_srobj_panel(Panel):
    """SROBJ Importer UI Panel.""" # Title updated
    bl_label = "Silkroad SROBJ Tools"
    bl_idname = "VIEW3D_PT_srobj_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Silkroad'

    def draw(self, context):
        layout = self.layout
        props = context.scene.srobj_props

        # --- Import Section ---
        box = layout.box()
        box.label(text="Import SROBJ", icon='IMPORT')
        box.prop(props, "import_srobj_path")
        box.prop(props, "import_texture_path")
        box.prop(props, "import_flip_uv_v")
        box.operator(SROBJ_OT_ImportUI.bl_idname, text="Import SROBJ")

        # --- Export Section (REMOVED) ---
        # The entire export section from draw method is removed.


# --- Registration ---

classes = (
    SROBJProperties,
    SROBJ_OT_ImportUI,
    # SROBJ_OT_ExportUI is removed
    VIEW3D_PT_srobj_panel
)

def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.srobj_props = PointerProperty(type=SROBJProperties)

def unregister():
    # Important: reverse order for dependencies during unregister
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
    del bpy.types.Scene.srobj_props

if __name__ == "__main__":
    register()