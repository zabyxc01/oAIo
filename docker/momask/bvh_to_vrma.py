"""BVH to VRMA (VRM Animation) converter.

Parses BVH files from MoMask (HumanML3D skeleton) and converts them to
VRMA format — a GLTF/GLB with the VRMC_vrm_animation extension.

Usage:
    python bvh_to_vrma.py input.bvh output.glb

Or as a module:
    from bvh_to_vrma import convert_bvh_to_vrma
    glb_bytes = convert_bvh_to_vrma(bvh_text)
"""

import json
import math
import struct
import sys
from dataclasses import dataclass, field


# ── HumanML3D → VRM bone name mapping ───────────────────────────────────────
BONE_MAP = {
    "Hips": "hips",
    "Spine": "spine",
    "Spine1": "chest",
    "Spine2": "upperChest",
    "Neck": "neck",
    "Head": "head",
    "LeftShoulder": "leftShoulder",
    "LeftArm": "leftUpperArm",
    "LeftForeArm": "leftLowerArm",
    "LeftHand": "leftHand",
    "RightShoulder": "rightShoulder",
    "RightArm": "rightUpperArm",
    "RightForeArm": "rightLowerArm",
    "RightHand": "rightHand",
    "LeftUpLeg": "leftUpperLeg",
    "LeftLeg": "leftLowerLeg",
    "LeftFoot": "leftFoot",
    "LeftToeBase": "leftToes",
    "RightUpLeg": "rightUpperLeg",
    "RightLeg": "rightLowerLeg",
    "RightFoot": "rightFoot",
    "RightToeBase": "rightToes",
}


@dataclass
class BvhJoint:
    name: str
    channels: list = field(default_factory=list)
    children: list = field(default_factory=list)
    offset: tuple = (0.0, 0.0, 0.0)


def parse_bvh(text: str) -> tuple:
    """Parse BVH text → (root_joint, frame_count, frame_time, channel_data)."""
    lines = [l.strip() for l in text.strip().splitlines() if l.strip()]
    idx = 0

    def parse_joint():
        nonlocal idx
        while idx < len(lines) and lines[idx] in ("HIERARCHY",):
            idx += 1

        # ROOT/JOINT/End
        parts = lines[idx].split()
        is_end = parts[0] == "End"
        name = parts[1] if len(parts) > 1 else "End"
        idx += 1  # skip name line

        if lines[idx] == "{":
            idx += 1

        joint = BvhJoint(name=name)

        while idx < len(lines):
            line = lines[idx]
            if line == "}":
                idx += 1
                return joint
            elif line.startswith("OFFSET"):
                vals = line.split()[1:]
                joint.offset = tuple(float(v) for v in vals[:3])
                idx += 1
            elif line.startswith("CHANNELS"):
                parts = line.split()
                n = int(parts[1])
                joint.channels = parts[2:2+n]
                idx += 1
            elif line.startswith(("ROOT", "JOINT", "End")):
                child = parse_joint()
                joint.children.append(child)
            else:
                idx += 1
        return joint

    root = parse_joint()

    # Parse MOTION section
    while idx < len(lines) and not lines[idx].startswith("MOTION"):
        idx += 1
    idx += 1  # skip "MOTION"

    frame_count = int(lines[idx].split(":")[1].strip())
    idx += 1
    frame_time = float(lines[idx].split(":")[1].strip())
    idx += 1

    channel_data = []
    for i in range(frame_count):
        if idx + i < len(lines):
            vals = [float(v) for v in lines[idx + i].split()]
            channel_data.append(vals)

    return root, frame_count, frame_time, channel_data


def euler_to_quat(x_deg: float, y_deg: float, z_deg: float, order: str = "ZYX") -> tuple:
    """Convert Euler angles (degrees) to quaternion (x, y, z, w)."""
    x = math.radians(x_deg)
    y = math.radians(y_deg)
    z = math.radians(z_deg)

    cx, sx = math.cos(x/2), math.sin(x/2)
    cy, sy = math.cos(y/2), math.sin(y/2)
    cz, sz = math.cos(z/2), math.sin(z/2)

    # ZYX order (most common in BVH)
    qw = cx*cy*cz + sx*sy*sz
    qx = sx*cy*cz - cx*sy*sz
    qy = cx*sy*cz + sx*cy*sz
    qz = cx*cy*sz - sx*sy*cz

    return (qx, qy, qz, qw)


def collect_joints(joint: BvhJoint, out: list):
    """Flatten joint hierarchy into a list."""
    out.append(joint)
    for child in joint.children:
        collect_joints(child, out)


def convert_bvh_to_vrma(bvh_text: str) -> bytes:
    """Convert BVH text to VRMA GLB binary."""
    root, frame_count, frame_time, channel_data = parse_bvh(bvh_text)

    all_joints = []
    collect_joints(root, all_joints)

    # Map channels to joints
    channel_offset = 0
    joint_channels = {}
    for j in all_joints:
        if j.channels:
            joint_channels[j.name] = (channel_offset, j.channels)
            channel_offset += len(j.channels)

    # Build GLTF nodes (one per VRM-mapped joint)
    nodes = []
    node_indices = {}
    vrm_bone_map = {}  # node_index → vrm humanBone name

    for j in all_joints:
        if j.name in BONE_MAP:
            node_idx = len(nodes)
            node_indices[j.name] = node_idx
            vrm_bone_map[node_idx] = BONE_MAP[j.name]
            nodes.append({
                "name": j.name,
                "translation": [j.offset[0] * 0.01, j.offset[1] * 0.01, j.offset[2] * 0.01],
            })

    if not nodes:
        raise ValueError("No VRM-mappable bones found in BVH")

    # Build animation data
    # Time accessor
    times = [i * frame_time for i in range(frame_count)]
    time_min = times[0]
    time_max = times[-1]

    # Pack binary buffer
    buffer_data = bytearray()
    accessors = []
    buffer_views = []

    # Time buffer view
    time_offset = len(buffer_data)
    for t in times:
        buffer_data.extend(struct.pack('<f', t))
    time_bv_idx = len(buffer_views)
    buffer_views.append({"buffer": 0, "byteOffset": time_offset, "byteLength": frame_count * 4})
    time_acc_idx = len(accessors)
    accessors.append({
        "bufferView": time_bv_idx, "componentType": 5126, "count": frame_count,
        "type": "SCALAR", "min": [time_min], "max": [time_max],
    })

    # Animation channels + samplers
    channels = []
    samplers = []

    for j in all_joints:
        if j.name not in BONE_MAP or j.name not in joint_channels:
            continue
        if j.name not in node_indices:
            continue

        node_idx = node_indices[j.name]
        ch_offset, ch_names = joint_channels[j.name]

        has_position = any("position" in c.lower() for c in ch_names)
        has_rotation = any("rotation" in c.lower() for c in ch_names)

        if has_rotation:
            # Find rotation channel indices
            rot_indices = []
            rot_order = ""
            for i, cn in enumerate(ch_names):
                if "rotation" in cn.lower():
                    rot_indices.append(ch_offset + i)
                    rot_order += cn[0]  # X, Y, or Z

            # Pack quaternion data
            quat_offset = len(buffer_data)
            for frame in channel_data:
                if len(rot_indices) >= 3:
                    rx = frame[rot_indices[0]] if rot_indices[0] < len(frame) else 0
                    ry = frame[rot_indices[1]] if rot_indices[1] < len(frame) else 0
                    rz = frame[rot_indices[2]] if rot_indices[2] < len(frame) else 0
                    qx, qy, qz, qw = euler_to_quat(rx, ry, rz)
                    buffer_data.extend(struct.pack('<ffff', qx, qy, qz, qw))

            quat_bv_idx = len(buffer_views)
            buffer_views.append({"buffer": 0, "byteOffset": quat_offset, "byteLength": frame_count * 16})
            quat_acc_idx = len(accessors)
            accessors.append({
                "bufferView": quat_bv_idx, "componentType": 5126, "count": frame_count, "type": "VEC4",
            })

            sampler_idx = len(samplers)
            samplers.append({"input": time_acc_idx, "output": quat_acc_idx, "interpolation": "LINEAR"})
            channels.append({"sampler": sampler_idx, "target": {"node": node_idx, "path": "rotation"}})

        # Hips get translation too
        if has_position and j.name == "Hips":
            pos_indices = []
            for i, cn in enumerate(ch_names):
                if "position" in cn.lower():
                    pos_indices.append(ch_offset + i)

            if len(pos_indices) >= 3:
                pos_offset = len(buffer_data)
                for frame in channel_data:
                    px = frame[pos_indices[0]] * 0.01 if pos_indices[0] < len(frame) else 0
                    py = frame[pos_indices[1]] * 0.01 if pos_indices[1] < len(frame) else 0
                    pz = frame[pos_indices[2]] * 0.01 if pos_indices[2] < len(frame) else 0
                    buffer_data.extend(struct.pack('<fff', px, py, pz))

                pos_bv_idx = len(buffer_views)
                buffer_views.append({"buffer": 0, "byteOffset": pos_offset, "byteLength": frame_count * 12})
                pos_acc_idx = len(accessors)
                accessors.append({
                    "bufferView": pos_bv_idx, "componentType": 5126, "count": frame_count, "type": "VEC3",
                })

                sampler_idx = len(samplers)
                samplers.append({"input": time_acc_idx, "output": pos_acc_idx, "interpolation": "LINEAR"})
                channels.append({"sampler": sampler_idx, "target": {"node": node_idx, "path": "translation"}})

    # Pad buffer to 4-byte alignment
    while len(buffer_data) % 4 != 0:
        buffer_data.extend(b'\x00')

    # Build VRMC_vrm_animation extension
    human_bones = {}
    for node_idx, vrm_name in vrm_bone_map.items():
        human_bones[vrm_name] = {"node": node_idx}

    # Build GLTF JSON
    gltf = {
        "asset": {"version": "2.0", "generator": "oAIo-momask-converter"},
        "extensionsUsed": ["VRMC_vrm_animation"],
        "extensions": {
            "VRMC_vrm_animation": {
                "specVersion": "1.0",
                "humanoid": {"humanBones": human_bones},
            }
        },
        "scene": 0,
        "scenes": [{"nodes": list(range(len(nodes)))}],
        "nodes": nodes,
        "buffers": [{"byteLength": len(buffer_data)}],
        "bufferViews": buffer_views,
        "accessors": accessors,
        "animations": [{
            "name": "generated",
            "channels": channels,
            "samplers": samplers,
        }] if channels else [],
    }

    # Build GLB
    gltf_json = json.dumps(gltf, separators=(',', ':')).encode('utf-8')
    # Pad JSON to 4-byte alignment
    while len(gltf_json) % 4 != 0:
        gltf_json += b' '

    # GLB header (12 bytes) + JSON chunk (8 + len) + BIN chunk (8 + len)
    total_length = 12 + 8 + len(gltf_json) + 8 + len(buffer_data)

    glb = bytearray()
    # Header
    glb.extend(b'glTF')
    glb.extend(struct.pack('<I', 2))  # version
    glb.extend(struct.pack('<I', total_length))
    # JSON chunk
    glb.extend(struct.pack('<I', len(gltf_json)))
    glb.extend(struct.pack('<I', 0x4E4F534A))  # "JSON"
    glb.extend(gltf_json)
    # BIN chunk
    glb.extend(struct.pack('<I', len(buffer_data)))
    glb.extend(struct.pack('<I', 0x004E4942))  # "BIN\0"
    glb.extend(buffer_data)

    return bytes(glb)


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(f"Usage: {sys.argv[0]} input.bvh output.glb")
        sys.exit(1)

    with open(sys.argv[1], 'r') as f:
        bvh_text = f.read()

    glb = convert_bvh_to_vrma(bvh_text)

    with open(sys.argv[2], 'wb') as f:
        f.write(glb)

    print(f"Converted {sys.argv[1]} → {sys.argv[2]} ({len(glb)} bytes)")
