bl_info = {
    "name": "Bake Selected to Active",
    "author": "kyunghoon",
    "version": (1, 0, 0),
    "blender": (5, 1, 0),
    "location": "3D View > Sidebar > Bake Panel",
    "description": "Bake textures from selected objects to the active object (Selected to Active).",
    "category": "Object",
    "doc_url": "",
    "tracker_url": "",
}

import bpy
import os
from bpy.props import (
    StringProperty,
    IntProperty,
    BoolProperty,
    EnumProperty,
    FloatProperty,
    PointerProperty,
)
from bpy.types import (
    Operator,
    Panel,
    PropertyGroup,
)


class BakeProperties(PropertyGroup):
    width: IntProperty(
        name="Width",
        description="Image width in pixels",
        default=1024,
        min=8,
        max=16384,
    )

    height: IntProperty(
        name="Height",
        description="Image height in pixels",
        default=1024,
        min=8,
        max=16384,
    )

    bake_type: EnumProperty(
        name="Bake Type",
        description="Type of data to bake",
        items=[
            ('ALL', "All", "Bake all properties"),
            ('DIFFUSE', "Diffuse", "Diffuse color (albedo)")
        ],
        default='DIFFUSE',
    )

    margin: IntProperty(
        name="Margin",
        description="Bake margin in pixels (prevents edge bleeding)",
        default=4,
        min=0,
        max=64,
    )

    bake_samples: IntProperty(
        name="Bake Samples",
        description="Number of samples for baking (higher = cleaner, slower)",
        default=1,
        min=1,
        max=4096,
    )

    use_cage: BoolProperty(
        name="Use Cage",
        description="Use cage for ray casting (recommended)",
        default=True,
    )

    cage_extrusion: FloatProperty(
        name="Cage Extrusion",
        description="Distance to extrude cage from low-poly mesh",
        default=0.02,
        min=0.001,
        max=1.0,
        subtype='DISTANCE',
    )

    max_ray_distance: FloatProperty(
        name="Max Ray Distance",
        description="Maximum distance rays travel (0.0 = infinite)",
        default=0.1,
        min=0.0,
        max=10.0,
        subtype='DISTANCE',
    )

def show_popup(message = "", title = "Message Box", icon = 'INFO'):

    def draw(self, context):
        self.layout.label(text=message)

    bpy.context.window_manager.popup_menu(draw, title = title, icon = icon)
    
def bypass_bsdf_to_surface(bsdf_node, input):
    tree = bsdf_node.id_data
    links = tree.links
    nodes = tree.nodes

    output_node = next((n for n in nodes if n.type == 'OUTPUT_MATERIAL'), None)
    if not output_node:
        return

    base_color_input = bsdf_node.inputs.get(input)
    
    if base_color_input and base_color_input.is_linked:
        # 1. Identify the existing link
        old_link = base_color_input.links[0]
        source_socket = old_link.from_socket
        
        # 2. Create the new connection to the Surface pin
        links.new(source_socket, output_node.inputs['Surface'])
        
        # 3. Explicitly remove the old link to the BSDF
        links.remove(old_link)

def restore_bsdf_connection(bsdf_node, input):
    tree = bsdf_node.id_data
    links = tree.links
    nodes = tree.nodes

    # 1. Locate the active Material Output
    output_node = next((n for n in nodes if n.type == 'OUTPUT_MATERIAL' and n.is_active_output), None)
    if not output_node:
        output_node = next((n for n in nodes if n.type == 'OUTPUT_MATERIAL'), None)

    if not output_node or not bsdf_node:
        return

    surface_input = output_node.inputs.get('Surface')
    base_color_input = bsdf_node.inputs.get(input)

    # 2. Check if we actually need to restore (is something else on Surface?)
    if surface_input.is_linked:
        current_link = surface_input.links[0]
        
        if current_link.from_node != bsdf_node:
            # This is our bypass link
            source_socket = current_link.from_socket
            
            # 3. CRITICAL: Clear the Surface input entirely first
            # This forces Blender to accept a new connection in the next step
            links.remove(current_link)
            
            # 4. Reconnect the original source to the BSDF
            links.new(source_socket, base_color_input)
            
            # 5. Reconnect the BSDF to the Material Output
            # We try by name 'BSDF', and fallback to index if name fails
            bsdf_output = bsdf_node.outputs.get('BSDF') or bsdf_node.outputs[0]
            
            # Use the actual socket object to ensure 5.1 compatibility
            links.new(bsdf_output, surface_input)
            
            print(f"Bypass cleared and BSDF restored for {bsdf_node.name}")

# Helper: ensure UV map exists on target and source, with fallback unwrap
def ensure_uv_map(obj):
    if not obj.data.uv_layers:
        uv = obj.data.uv_layers.new(name="Bake_UV")
        # Auto-unwrap if no UVs
        if obj.type == 'MESH' and obj.data.polygons:
            # Switch to Object mode, select, unwrap
            prev_mode = bpy.context.object.mode if bpy.context.object else 'OBJECT'
            bpy.context.view_layer.objects.active = obj
            bpy.ops.object.mode_set(mode='OBJECT')
            bpy.ops.object.select_all(action='DESELECT')
            obj.select_set(True)
            bpy.context.view_layer.objects.active = obj
            bpy.ops.uv.smart_project(angle_limit=66, island_margin=0.02, user_area_weight=1.0, use_aspect=True, stretch_to_bounds=False)
            bpy.ops.object.mode_set(mode=prev_mode)
    else:
        # Ensure active UV layer is the first one
        obj.data.uv_layers.active = obj.data.uv_layers[0]

def get_bsdf_from_mesh(obj):
    """
    Given a mesh object, returns the Principled BSDF node 
    found in its active material.
    """
    # 1. Basic checks: is it an object and does it have materials?
    if not obj or obj.type != 'MESH':
        return None
    
    if not obj.data.materials:
        print(f"Object {obj.name} has no materials.")
        return None

    # 2. Get the active material
    # If no material is 'active', we take the first one in the slots
    mat = obj.active_material
    if not mat:
        mat = obj.data.materials[0]

    # 3. Ensure the material uses nodes
    if not mat.use_nodes:
        print(f"Material {mat.name} does not use nodes.")
        return None

    nodes = mat.node_tree.nodes

    # 4. Find the Principled BSDF node
    # We search by type to ensure it works even if the user renamed the node
    bsdf = next((n for n in nodes if n.type == 'BSDF_PRINCIPLED'), None)

    if bsdf:
        return bsdf
    else:
        print(f"No Principled BSDF found in {mat.name}")
        return None

def get_connected_output_name(bsdf_node, input_name):
    """
    Given a BSDF node and an input name (e.g., 'Metallic'),
    returns the name of the output pin connected to it.
    """
    # 1. Safely grab the input socket by name
    target_input = bsdf_node.inputs.get(input_name)
    
    # 2. Check if the socket exists and has a connection
    if target_input and target_input.is_linked:
        # The first link [0] is the active connection
        link = target_input.links[0]
        
        # from_socket is the output pin on the "source" node
        source_pin = link.from_socket
        
        return source_pin.identifier
        
    return None

# Main bake operator
class OBJECT_OT_bake_selected_to_active(Operator):
    bl_idname = "object.bake_selected_to_active"
    bl_label = "Bake Selected to Active"
    bl_description = "Bake from selected objects to active object using 'Selected to Active'"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return (
            context.scene is not None
            and context.object is not None
            and context.object.type == 'MESH'
            and len(context.selected_objects) > 1
        )
        
    def execute(self, context):
        props = context.scene.bake_props
    
        # Capture the total selection
        selection = context.selected_objects
    
        # Validation: Exactly two objects must be selected
        if len(selection) != 2:
            self.report({'WARNING'}, "Select exactly two objects (Source then Destination)")
            return {'CANCELLED'}
    
        # Validation: Both selected objects must be meshes
        if any(o.type != 'MESH' for o in selection):
            self.report({'WARNING'}, "Both selected objects must be of type 'MESH'")
            return {'CANCELLED'}
    
        # Assign Target (Active) and Source (The other one)
        target_obj = context.active_object
        source_objs = [o for o in selection if o != target_obj]

        # Safety check in case selection exists but no object is "Active"
        if not target_obj:
            self.report({'WARNING'}, "No active object found. Ensure the destination is selected last.")
            return {'CANCELLED'}

        src = source_objs[0]
        dst = target_obj
        
        if not dst.data.materials:
            # Create a new material if the list is empty
            new_mat = bpy.data.materials.new(name=f"Mat_{dst.name}")
            dst.data.materials.append(new_mat)
            self.report({'INFO'}, f"Created new material for {dst.name}")
            
        # Get a reference to the active material (the first slot)
        target_mat = dst.data.materials[0]
        
        # Ensure Use Nodes is enabled (required for most baking workflows)
        if not target_mat.use_nodes:
            target_mat.use_nodes = True
            
        # Handle Node Tree and Connection
        target_mat = dst.data.materials[0]
        target_mat.use_nodes = True
        nodes = target_mat.node_tree.nodes
        links = target_mat.node_tree.links
        
        # Configure Render Settings for Baking
        scene = context.scene
        self.report({'INFO'}, f"Created new material for {scene.render.engine}")
        
        if scene.render.engine != 'CYCLES':
            show_popup("Please set the Render Engine to Cycles")
            return {'CANCELLED'}
        scene.render.engine = 'CYCLES'
        
        # Bake settings
        bake_settings = scene.render.bake
        bake_settings.use_selected_to_active = True
        bake_settings.use_clear = True
        bake_settings.target = 'IMAGE_TEXTURES'
        bake_settings.margin = props.margin
        
        # Bake type (dynamic from UI)
        scene.cycles.bake_type = 'EMIT'
        
        # — Ensure UVs on both
        ensure_uv_map(src)
        ensure_uv_map(dst)
        
        if props.bake_type == 'ALL':
            bake_queue = [
                {'type': 'DIFFUSE', 'input': 'Base Color'},
                #{'type': 'METALLIC', 'input': 'Metallic'},
                #{'type': 'ROUGHNESS', 'input': 'Roughness'},
                {'type': 'NORMAL', 'input': 'Normal'}
            ]
        else:
            bake_queue = [
                {'type': 'DIFFUSE', 'input': 'Base Color'}
            ]

        for bake in bake_queue:
            # — Ensure image is float + linear
            bake_type = bake['type']
            
            img_name = f"Bake_{dst.name}_{bake_type}"
            bake_img = bpy.data.images.get(img_name)
            if not bake_img:
                bake_img = bpy.data.images.new(
                    name=img_name,
                    width=props.width,
                    height=props.height,
                    alpha=False,
                    float_buffer=True,
                    is_data=False,
                )
                # Set linear colorspace
                try:
                    bake_img.colorspace_settings.name = 'scene_linear'
                except Exception:
                    bake_img.colorspace_settings.name = 'Linear'
            
            # — Setup material & node
            if not dst.data.materials:
                mat = bpy.data.materials.new(name=f"{dst.name}_BakeMat")
                mat.use_nodes = True
                dst.data.materials.append(mat)
            else:
                mat = dst.data.materials[0]
                mat.use_nodes = True
            
            nodes = mat.node_tree.nodes
            links = mat.node_tree.links
            
            
            # Find or create Image Texture node
            tex_node = None
            for n in nodes:
                if n.type == 'TEX_IMAGE' and n.label == f'{bake_type} Bake Target':
                    tex_node = n
                    break
            if not tex_node:
                tex_node = nodes.new('ShaderNodeTexImage')
                tex_node.label = f'{bake_type} Bake Target'
                tex_node.location = (-400, 300)
            
            tex_node.image = bake_img
            
            # Find BSDF
            src_bsdf = get_bsdf_from_mesh(src)
            dst_bsdf = None
            for n in nodes:
                if n.type == 'BSDF_PRINCIPLED':
                    dst_bsdf = n
                    break
            if not dst_bsdf:
                dst_bsdf = nodes.new('ShaderNodeBsdfPrincipled')
                dst_bsdf.location = (0, 300)
                

            output_name = get_connected_output_name(src_bsdf, bake['input'])
            emission_input = dst_bsdf.inputs.get('Emission')
            if output_name:
                if emission_input:
                    links.new(tex_node.outputs[output_name], emission_input)
                else:
                    base_input = dst_bsdf.inputs.get(bake['input'])
                    if base_input:
                        links.new(tex_node.outputs[output_name], base_input)
                        
                if src_bsdf:
                    bypass_bsdf_to_surface(src_bsdf, bake['input'])
                if dst_bsdf:
                    bypass_bsdf_to_surface(dst_bsdf, bake['input'])
                
                # Save state
                prev_bake_samples = scene.cycles.samples;
                scene.cycles.samples = 1;
                
                # Cage settings
                bake_settings.use_cage = props.use_cage
                bake_settings.cage_extrusion = props.cage_extrusion
                bake_settings.max_ray_distance = props.max_ray_distance

                
                # restore state
                scene.cycles.samples = prev_bake_samples
                
                # 7. Execute the Bake
                # Ensure the objects are still selected correctly for "Selected to Active"
                # The 'src' must be selected and 'dst' must be the active object.
                try:
                    nodes.active = tex_node
                    bpy.ops.object.bake(type='EMIT', save_mode='INTERNAL')
                    self.report({'INFO'}, f"{bake_type} bake complete!")
                except Exception as e:
                    self.report({'ERROR'}, f"Bake failed: {str(e)}")
                    return {'CANCELLED'}
                
                if dst_bsdf:
                    restore_bsdf_connection(dst_bsdf, bake['input'])
                if src_bsdf:
                    restore_bsdf_connection(src_bsdf, bake['input'])

        return {'FINISHED'}


# Panel in sidebar
class BAKE_PT_panel(Panel):
    bl_label = "Bake Selected to Active"
    bl_idname = "BAKE_PT_selected_to_active"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Brainbyte Baker"

    def draw(self, context):
        layout = self.layout
        props = context.scene.bake_props

        layout.use_property_split = True
        layout.use_property_decorate = False

        box = layout.box()
        box.label(text="Bake Settings", icon='RENDER_STILL')
        box.prop(props, "width")
        box.prop(props, "height")

        box.separator()

        box.label(text="Bake Options", icon='SETTINGS')
        box.prop(props, "bake_type")
        #box.prop(props, "margin")
        #box.prop(props, "bake_samples")

        box.separator()

        box.label(text="Cage Settings", icon='MESH_CUBE')
        box.prop(props, "use_cage")
        if props.use_cage:
            box.prop(props, "cage_extrusion")
            box.prop(props, "max_ray_distance")

        layout.separator()

        row = layout.row()
        row.scale_y = 1.3
        row.operator("object.bake_selected_to_active", icon='RENDER_STILL')


classes = (
    BakeProperties,
    OBJECT_OT_bake_selected_to_active,
    BAKE_PT_panel,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.bake_props = PointerProperty(type=BakeProperties)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
    del bpy.types.Scene.bake_props


if __name__ == "__main__":
    register()

