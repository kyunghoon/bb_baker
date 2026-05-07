bl_info = {
    "name": "Brainbyte Baker",
    "author": "kyunghoon",
    "version": (5, 0, 0),
    "blender": (5, 1, 0),
    "location": "3D View > Sidebar > Bake Panel",
    "description": "Bake textures from selected objects to the active object (Selected to Active).",
    "category": "Object",
    "doc_url": "",
    "tracker_url": "",
}

import bpy
import numpy as np
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
    """Properties panel for bake settings"""
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


def show_popup(message="", title="Message Box", icon='INFO'):
    """Display a popup message in Blender"""

    def draw(self, context):
        self.layout.label(text=message)

    bpy.context.window_manager.popup_menu(draw, title=title, icon=icon)


def bypass_bsdf_to_surface(bsdf_node, input_name):
    """Bypass the BSDF node by connecting the input directly to the Material Output node.
    
    This is used during baking to ensure the source texture is directly connected to the
    surface output for accurate baking.
    
    Args:
        bsdf_node: The BSDF node to bypass
        input_name: Name of the input socket to bypass (e.g., 'Base Color')
    """
    tree = bsdf_node.id_data
    links = tree.links
    nodes = tree.nodes

    output_node = next((n for n in nodes if n.type == 'OUTPUT_MATERIAL'), None)
    if not output_node:
        return

    base_color_input = bsdf_node.inputs.get(input_name)
    
    if base_color_input and base_color_input.is_linked:
        # 1. Identify the immediate link and its source node
        old_link = base_color_input.links[0]
        source_socket = old_link.from_socket
        source_node = source_socket.node
        
        # --- SPECIAL HANDLE: Separate Color ---
        # If the source is a Separate Color node, walk back to its input
        if source_node.type in ['SEPARATE_COLOR', 'NORMAL_MAP']:
            sep_in = source_node.inputs.get('Color')
            if sep_in and sep_in.is_linked:
                # Override source_socket with the one feeding the Separate Color node
                source_socket = sep_in.links[0].from_socket
        
        # Create the new connection to the Surface pin
        links.new(source_socket, output_node.inputs['Surface'])


def restore_bsdf_connection(bsdf_node, input_name):
    """Restore the BSDF connection after baking.
    
    Reconnects the original BSDF output to the Material Output and reconnects
    the texture source to the BSDF input.
    
    Args:
        bsdf_node: The BSDF node to restore
        input_name: Name of the input socket to restore (e.g., 'Base Color')
    """
    tree = bsdf_node.id_data
    links = tree.links
    nodes = tree.nodes

    output_node = next((n for n in nodes if n.type == 'OUTPUT_MATERIAL' and n.is_active_output), None)
    if not output_node:
        output_node = next((n for n in nodes if n.type == 'OUTPUT_MATERIAL'), None)

    if not output_node or not bsdf_node:
        return

    surface_input = output_node.inputs.get('Surface')
    base_color_input = bsdf_node.inputs.get(input_name)

    if surface_input.is_linked:
        current_link = surface_input.links[0]
        
        if current_link.from_node != bsdf_node:
            # This is the texture/source currently bypassing the BSDF
            source_socket = current_link.from_socket
            
            # --- SPECIAL HANDLE REVERSAL: Separate Color ---
            # Search for a Separate Color node that is currently unlinked 
            # or was previously connected to this source.
            target_sep_node = None
            for link in source_socket.links:
                if link.to_node.type in ['SEPARATE_COLOR', 'NORMAL_MAP']:
                    target_sep_node = link.to_node
                    break
            
            # Remove the bypass link
            links.remove(current_link)
            
            if target_sep_node:
                # Ensure the texture is connected to the Separate Color input
                links.new(source_socket, target_sep_node.inputs['Color'])
            else:
                # Standard case: No Separate Color node found, connect directly
                links.new(source_socket, base_color_input)
            
            # 3. Restore the main BSDF -> Material Output connection
            bsdf_output = bsdf_node.outputs.get('BSDF') or bsdf_node.outputs[0]
            links.new(bsdf_output, surface_input)


def ensure_uv_map(obj):
    """Ensure the object has a UV map, creating one if necessary.
    
    If no UV map exists, creates a new one and performs smart UV projection.
    
    Args:
        obj: Blender object to ensure UV map for
        
    Returns:
        The UV layer object
    """
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
        return uv
    else:
        # Ensure active UV layer is the first one
        obj.data.uv_layers.active = obj.data.uv_layers[0]
        return obj.data.uv_layers.active


def get_bsdf_from_mesh(obj):
    """Get the Principled BSDF node from the object's active material.
    
    Args:
        obj: Blender mesh object
        
    Returns:
        Principled BSDF node or None if not found
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
    """Get the name of the output socket connected to a BSDF input.
    
    Args:
        bsdf_node: BSDF node to check
        input_name: Name of the input socket to check
        
    Returns:
        Name of the connected output socket or None
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


def create_or_get_image(name, width, height):
    """Create a new image or return existing one with the same name.
    
    Args:
        name: Name for the image
        width: Width in pixels
        height: Height in pixels
        
    Returns:
        Blender image object
    """
    bake_img = bpy.data.images.get(name)
    if not bake_img:
        bake_img = bpy.data.images.new(
            name=name,
            width=width,
            height=height,
            alpha=False,
            float_buffer=True,
            is_data=False,
        )
    return bake_img


def setup_bake_material(obj):
    """Ensure the object has a material with nodes enabled.
    
    Args:
        obj: Blender object to set up
        
    Returns:
        Material object
    """
    if not obj.data.materials:
        # Create a new material if the list is empty
        new_mat = bpy.data.materials.new(name=f"Mat_{obj.name}")
        obj.data.materials.append(new_mat)
        return new_mat
    else:
        mat = obj.data.materials[0]
        if not mat.use_nodes:
            mat.use_nodes = True
        return mat


def setup_image_texture_node(nodes, bake_type, bake_img):
    """Set up an Image Texture node for baking.
    
    Args:
        nodes: Node tree nodes collection
        links: Node tree links collection
        bake_type: Type of bake (e.g., 'DIFFUSE', 'NORMAL')
        bake_img: Image to assign to the texture node
        
    Returns:
        Image Texture node
    """
    # Find or create Image Texture node
    tex_node = None
    for n in nodes:
        if n.type == 'TEX_IMAGE' and n.label == f'{bake_type} Bake Target':
            tex_node = n
            break
    if not tex_node:
        tex_node = nodes.new('ShaderNodeTexImage')
        tex_node.label = f'{bake_type} Bake Target'
        # Position nodes for better visibility
        if bake_type == 'NORMAL':
            tex_node.location = (-425, 0)
        elif bake_type == 'ORM':
            tex_node.location = (-425, -300)
        elif bake_type == 'EMISSION':
            tex_node.location = (-425, -600)
        else:
            tex_node.location = (-425, 300)
    
    tex_node.image = bake_img
    return tex_node


def setup_bsdf_node(nodes):
    """Ensure there's a Principled BSDF node in the material.
    
    Args:
        nodes: Node tree nodes collection
        links: Node tree links collection
        
    Returns:
        Principled BSDF node
    """
    # Find existing BSDF node
    dst_bsdf = None
    for n in nodes:
        if n.type == 'BSDF_PRINCIPLED':
            dst_bsdf = n
            break
    
    # Create new BSDF if none exists
    if not dst_bsdf:
        dst_bsdf = nodes.new('ShaderNodeBsdfPrincipled')
        dst_bsdf.location = (0, 300)
    
    return dst_bsdf

def setup_shader_connections(nodes, links, tex_node, dst_bsdf, bake_type, bake_input):
    """Set up the shader connections for baking.
    
    Args:
        nodes: Node tree nodes collection
        links: Node tree links collection
        tex_node: Image Texture node
        dst_bsdf: BSDF node
        bake_type: Type of bake (e.g., 'DIFFUSE', 'NORMAL')
        bake_input: Input name to connect to (e.g., 'Base Color')
        
    Returns:
        SeparateColorNode if bake_type is 'ORM' otherwise None
    """
    if bake_type == 'ORM':
        # Handle ORM (Roughness, Metallic) baking
        sep_color = nodes.new(type='ShaderNodeSeparateColor')
        sep_color.location = (-150, -200)
        links.new(tex_node.outputs['Color'], sep_color.inputs['Color'])
        links.new(sep_color.outputs['Green'], dst_bsdf.inputs['Roughness'])
        links.new(sep_color.outputs['Blue'], dst_bsdf.inputs['Metallic'])
        return sep_color
    elif bake_type == 'NORMAL':
        # Handle Normal baking
        normal_map = nodes.new(type='ShaderNodeNormalMap')
        normal_map.location = (-150, 100)
        # Use the active UV layer
        uv_map_name = nodes.get('UV Map')
        if uv_map_name:
            normal_map.uv_map = uv_map_name.name
        links.new(tex_node.outputs['Color'], normal_map.inputs['Color'])
        links.new(normal_map.outputs['Normal'], dst_bsdf.inputs['Normal'])
        return None
    else:
        # Handle standard baking (Diffuse/Albedo)
        base_input = dst_bsdf.inputs.get(bake_input)
        if base_input:
            # Try to get the output name connected to the source BSDF
            output_name = get_connected_output_name(get_bsdf_from_mesh(bpy.context.selected_objects[1]), bake_input)
            if output_name and tex_node.outputs.get(output_name):
                links.new(tex_node.outputs[output_name], base_input)
            else:
                # Fallback to Color output
                if tex_node.outputs.get('Color'):
                    links.new(tex_node.outputs['Color'], base_input)
        if bake_type == 'EMISSION':
            socket = dst_bsdf.inputs.get('Emission Strength')
            if socket and not socket.is_linked:
                socket.default_value = 1.0
                
        return None

def bake_ambient_occlusion(obj, scene, image_name="AO_Bake", width=1024, height=1024):
    # Create or get the bake target image
    if image_name not in bpy.data.images:
        image = bpy.data.images.new(image_name, width=width, height=height)
    else:
        image = bpy.data.images[image_name]
    image.colorspace_settings.name = 'Non-Color'

    # Set up the material node tree
    # Every material on the object needs an Image Texture node selected to act as the target
    for mat_slot in obj.material_slots:
        mat = mat_slot.material
        if mat and mat.use_nodes:
            nodes = mat.node_tree.nodes
            
            # Create a temporary texture node for the bake target
            bake_node = nodes.new('ShaderNodeTexImage')
            bake_node.image = image
            bake_node.select = True
            nodes.active = bake_node # This tells Blender where the pixels go
            
    # Save original render settings
    prev_bake_samples = scene.cycles.samples
    prev_preview_samples = scene.cycles.preview_samples
    prev_device = scene.cycles.device
            
    # Configure render settings for baking
    scene.cycles.samples = 256
    scene.cycles.preview_samples = 1
    scene.cycles.device = 'GPU'
    scene.render.bake.use_clear = True
            
    # Configure Bake Settings
    scene.cycles.bake_type = 'AO'

    # 6. Execute Bake
    bpy.ops.object.bake(type='AO')
    
    # Restore render settings
    scene.cycles.device = prev_device
    scene.cycles.preview_samples = prev_preview_samples
    scene.cycles.samples = prev_bake_samples
    
    # 7. Clean up (Optional: remove the temporary nodes)
    # If you want to keep the nodes, skip this part
    for mat_slot in obj.material_slots:
        mat = mat_slot.material
        if mat and mat.use_nodes:
            mat.node_tree.nodes.remove(mat.node_tree.nodes.active)
            
    return image

    
def copy_between_nodes(self, src_node, dst_node, src_channel, dst_channel):
    src_img = src_node.image
    dst_img = dst_node.image

    # Fast Numpy Copy
    num_pixels = src_img.size[0] * src_img.size[1]
    
    # Pull data
    src_pixels = np.empty(num_pixels * 4, dtype=np.float32)
    dst_pixels = np.empty(num_pixels * 4, dtype=np.float32)
    src_img.pixels.foreach_get(src_pixels)
    dst_img.pixels.foreach_get(dst_pixels)

    # Reshape to (N, 4) and copy the specific channel
    src_pixels = src_pixels.reshape(num_pixels, 4)
    dst_pixels = dst_pixels.reshape(num_pixels, 4)
    
    dst_pixels[:, dst_channel] = src_pixels[:, src_channel]

    # Push data back
    dst_img.pixels.foreach_set(dst_pixels.flatten())
    dst_img.update()
    
    for area in bpy.context.screen.areas:
        area.tag_redraw()

def inject_vector_multiply(material, target_node, input_socket_name="Vector"):
    """
    Inserts a Vector Math (Multiply) node before a specific vector input.
    """
    if not material.use_nodes:
        return
    
    nodes = material.node_tree.nodes
    links = material.node_tree.links
    
    # 1. Identify the specific Vector input socket
    vector_socket = target_node.inputs.get(input_socket_name)
    if not vector_socket:
        print(f"Node does not have a '{input_socket_name}' input.")
        return

    # 2. Create the Vector Math node
    vm_node = nodes.new(type='ShaderNodeVectorMath')
    vm_node.operation = 'MULTIPLY'
    
    # Position it to the left of the target node
    vm_node.location = (target_node.location.x - 300, target_node.location.y + 100)

    # 3. Handle existing connections
    if vector_socket.is_linked:
        existing_link = vector_socket.links[0]
        source_output = existing_link.from_socket
        
        # Connect original source to the first input of Vector Math
        links.new(source_output, vm_node.inputs[0])
    else:
        # If no link, copy the current static vector values (XYZ)
        vm_node.inputs[0].default_value = vector_socket.default_value

    # 4. Connect Vector Math output to the target node
    links.new(vm_node.outputs[0], vector_socket)
    
    return vm_node


# Main bake operator
class OBJECT_OT_bake_selected(Operator):
    bl_idname = "object.bake_selected"
    bl_label = "Bake Selected"
    bl_description = "Bake from selected objects to active object using 'Selected to Active'"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        """Check if the operator can be executed.
        
        Returns:
            bool: True if conditions are met for baking
        """
        return (
            context.scene is not None
            and context.object is not None
            and len(context.selected_objects) > 1
            and context.selected_objects[0].type == 'MESH'
            and context.selected_objects[1].type == 'MESH'
        )
        
    def execute(self, context):
        """Main execute method for the bake operator.
        
        This method handles the complete baking process:
        1. Validates selection
        2. Sets up materials and nodes
        3. Configures bake settings
        4. Processes each bake type in the queue
        5. Restores original connections
        """
        props = context.scene.bake_props
    
        # Capture the total selection
        selection = context.selected_objects
    
        # Assign Target (Active) and Source (The other one)
        target_obj = context.active_object
        source_objs = [o for o in selection if o != target_obj]

        # Safety check in case selection exists but no object is "Active"
        if not target_obj:
            self.report({'WARNING'}, "No active object found. Ensure the destination is selected last.")
            return {'CANCELLED'}

        src = source_objs[0]
        dst = target_obj
        
        # Setup target material
        target_mat = setup_bake_material(dst)
        target_mat.use_nodes = True
        
        # Ensure Cycles render engine is active
        scene = context.scene
        if scene.render.engine != 'CYCLES':
            scene.render.engine = 'CYCLES'
            self.report({'INFO'}, "Switched render engine to Cycles")

        # Configure bake settings
        bake_settings = scene.render.bake
        bake_settings.use_selected_to_active = True
        bake_settings.use_clear = True
        bake_settings.target = 'IMAGE_TEXTURES'
        bake_settings.margin = props.margin
        
        # Set up UV maps on both objects
        src_uv = ensure_uv_map(src)
        dst_uv = ensure_uv_map(dst)
        
        # Determine bake queue
        if props.bake_type == 'ALL':
            bake_queue = [
                {'type': 'DIFFUSE', 'input': 'Base Color', 'non_color': False},
                {'type': 'ORM', 'input': 'Roughness', 'non_color': True},
                {'type': 'NORMAL', 'input': 'Normal', 'non_color': True},
                {'type': 'EMISSION', 'input': 'Emission Color', 'non_color': False}
            ]
        else:
            bake_queue = [
                {'type': 'DIFFUSE', 'input': 'Base Color', 'non_color': False}
            ]
            
        # Setup material and nodes
        nodes = target_mat.node_tree.nodes
        links = target_mat.node_tree.links  
        
        # Bakeout AO
        ao_img = bake_ambient_occlusion(dst, scene, f'{dst.name}_AO', props.width, props.height)
        ao_tex_node = setup_image_texture_node(nodes, "AO", ao_img)

        orm_sep_color = None
        
        # Process each bake type
        for bake in bake_queue:
            bake_type = bake['type']
            bake_input = bake['input']
            non_color = bake['non_color']
            
            # Create or get bake image
            img_name = f"{dst.name}_{bake_type}"
            bake_img = create_or_get_image(img_name, props.width, props.height)
            if non_color:
                bake_img.colorspace_settings.name = 'Non-Color'
            
            # Setup Image Texture node
            tex_node = setup_image_texture_node(nodes, bake_type, bake_img)
            
            # Setup BSDF node
            dst_bsdf = setup_bsdf_node(nodes)
            
            # Setup shader connections
            sep_color = setup_shader_connections(nodes, links, tex_node, dst_bsdf, bake_type, bake_input)
            if sep_color:
                orm_sep_color = sep_color
            
            # Get source BSDF node
            src_bsdf = get_bsdf_from_mesh(src)
            
            # Bypass connections for baking
            if src_bsdf:
                bypass_bsdf_to_surface(src_bsdf, bake_input)
            if dst_bsdf:
                bypass_bsdf_to_surface(dst_bsdf, bake_input)
            
            # Save original render settings
            prev_bake_samples = scene.cycles.samples
            prev_preview_samples = scene.cycles.preview_samples
            prev_device = scene.cycles.device
            
            # Configure render settings for baking
            scene.cycles.samples = 1
            scene.cycles.preview_samples = 1
            scene.cycles.device = 'GPU'
            scene.render.bake.use_clear = True

            
            # Configure cage settings
            bake_settings.use_cage = props.use_cage
            bake_settings.cage_extrusion = props.cage_extrusion
            bake_settings.max_ray_distance = props.max_ray_distance
            
            # Set bake type
            scene.cycles.bake_type = 'EMIT'  # We'll override this per type
            
            # Execute the Bake
            #try:
            nodes.active = tex_node

            bpy.ops.object.bake(type='EMIT', save_mode='INTERNAL')

            self.report({'INFO'}, f"{bake_type} bake complete!")
            
            # Restore render settings
            scene.cycles.device = prev_device
            scene.cycles.preview_samples = prev_preview_samples
            scene.cycles.samples = prev_bake_samples
            
            # Restore BSDF connections
            if dst_bsdf:
                restore_bsdf_connection(dst_bsdf, bake_input)
            if src_bsdf:
                restore_bsdf_connection(src_bsdf, bake_input)
                
            if bake_type == 'ORM':
                copy_between_nodes(self, ao_tex_node, tex_node, 0, 0) 
                
        # Blend the AO into the BaseColor 
        if orm_sep_color:
            mult_node = inject_vector_multiply(target_mat, dst_bsdf, "Base Color")
            links.new(orm_sep_color.outputs['Red'], mult_node.inputs[1])
        
        # Cleanup AO nodes
        nodes.remove(ao_tex_node)
        bpy.data.images.remove(ao_img, do_unlink=True)
        
        return {'FINISHED'}
    
# Panel in sidebar
class BAKE_PT_panel(Panel):
    bl_label = "Bake Selected to Active"
    bl_idname = "BAKE_PT_selected_to_active"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Brainbyte Baker"

    def draw(self, context):
        """Draw the UI panel for bake settings"""
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

        box.separator()

        box.label(text="Cage Settings", icon='MESH_CUBE')
        box.prop(props, "use_cage")
        if props.use_cage:
            box.prop(props, "cage_extrusion")
            box.prop(props, "max_ray_distance")

        layout.separator()

        row = layout.row()
        row.scale_y = 1.3
        row.operator("object.bake_selected", icon='RENDER_STILL')


classes = (
    BakeProperties,
    OBJECT_OT_bake_selected,
    BAKE_PT_panel,
)


def register():
    """Register the add-on classes"""
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.bake_props = PointerProperty(type=BakeProperties)


def unregister():
    """Unregister the add-on classes"""
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
    del bpy.types.Scene.bake_props


if __name__ == "__main__":
    register()

