"""
VRAM monitoring via rocm-smi.
"""
import subprocess


def get_vram_usage() -> dict:
    try:
        result = subprocess.run(
            ["rocm-smi", "--showmeminfo", "vram"],
            capture_output=True, text=True
        )
        total = used = 0
        for line in result.stdout.splitlines():
            if "Total Memory" in line:
                total = int(line.split(":")[-1].strip())
            if "Total Used" in line:
                used = int(line.split(":")[-1].strip())
        return {
            "used_bytes": used,
            "total_bytes": total,
            "used_gb": round(used / 1e9, 2),
            "total_gb": round(total / 1e9, 2),
            "percent": round((used / total) * 100, 1) if total else 0
        }
    except Exception as e:
        return {"error": str(e)}


def get_gpu_utilization() -> dict:
    try:
        result = subprocess.run(
            ["rocm-smi", "--showuse"],
            capture_output=True, text=True
        )
        for line in result.stdout.splitlines():
            if "GPU use" in line:
                try:
                    percent = int(line.split(":")[-1].strip().replace("%", ""))
                    return {"gpu_use_percent": percent}
                except ValueError:
                    return {"gpu_use_percent": 0}
        return {"gpu_use_percent": 0}
    except Exception as e:
        return {"error": str(e)}
