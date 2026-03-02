"""
Extension loader — scans extensions/ at startup, mounts FastAPI routers,
registers frontend assets. Extensions are self-contained directories with
a manifest.json describing what they provide.
"""
import json
import importlib.util
from pathlib import Path

EXTENSIONS_DIR = Path(__file__).parent.parent.parent / "extensions"

# name → {manifest fields + loaded state}
_registry: dict[str, dict] = {}


def _load_one(app, ext_dir: Path) -> bool:
    manifest_path = ext_dir / "manifest.json"
    if not manifest_path.exists():
        return False
    try:
        manifest = json.loads(manifest_path.read_text())
    except Exception as e:
        print(f"[ext] {ext_dir.name}: bad manifest — {e}")
        return False

    if not manifest.get("enabled", True):
        return False

    name = manifest.get("name", ext_dir.name)

    # ── Mount backend router ──────────────────────────────────────────────────
    be = manifest.get("backend", {})
    router_file = be.get("router")
    prefix = be.get("prefix", f"/extensions/{name}")
    mounted = False

    if router_file:
        router_path = ext_dir / router_file
        if router_path.exists():
            try:
                spec = importlib.util.spec_from_file_location(
                    f"oaio_ext_{name}", str(router_path)
                )
                mod = importlib.util.module_from_spec(spec)
                # Inject extension dir so modules can resolve relative paths
                mod.__ext_dir__ = ext_dir
                spec.loader.exec_module(mod)
                if hasattr(mod, "router"):
                    app.include_router(mod.router, prefix=prefix)
                    mounted = True
                    print(f"[ext] {name}: router → {prefix}")
            except Exception as e:
                print(f"[ext] {name}: router load failed — {e}")

    _registry[name] = {
        "name":        name,
        "version":     manifest.get("version", "0.0.0"),
        "description": manifest.get("description", ""),
        "author":      manifest.get("author", ""),
        "enabled":     True,
        "dir":         ext_dir.name,
        "prefix":      prefix if mounted else None,
        "frontend":    manifest.get("frontend", {}),
        "services":    manifest.get("services", []),
        "mounted":     mounted,
    }
    print(f"[ext] loaded: {name} v{manifest.get('version', '?')}")
    return True


def load_all(app) -> None:
    """Called once at startup — mounts all enabled extensions."""
    EXTENSIONS_DIR.mkdir(parents=True, exist_ok=True)
    for ext_dir in sorted(EXTENSIONS_DIR.iterdir()):
        if ext_dir.is_dir() and not ext_dir.name.startswith("."):
            _load_one(app, ext_dir)


def list_all() -> list[dict]:
    """All extensions — loaded + disabled — from disk."""
    result = []
    if not EXTENSIONS_DIR.exists():
        return result
    for ext_dir in sorted(EXTENSIONS_DIR.iterdir()):
        if not ext_dir.is_dir() or ext_dir.name.startswith("."):
            continue
        manifest_path = ext_dir / "manifest.json"
        if not manifest_path.exists():
            continue
        try:
            manifest = json.loads(manifest_path.read_text())
            name = manifest.get("name", ext_dir.name)
            result.append({
                **manifest,
                "dir":    ext_dir.name,
                "loaded": name in _registry,
            })
        except Exception:
            pass
    return result


def set_enabled(name: str, enabled: bool) -> dict:
    """Toggle enabled flag in manifest. Restart required to take effect."""
    if not EXTENSIONS_DIR.exists():
        return {"error": "extensions dir not found"}
    for ext_dir in EXTENSIONS_DIR.iterdir():
        if not ext_dir.is_dir():
            continue
        manifest_path = ext_dir / "manifest.json"
        if not manifest_path.exists():
            continue
        try:
            manifest = json.loads(manifest_path.read_text())
            if manifest.get("name", ext_dir.name) == name:
                manifest["enabled"] = enabled
                manifest_path.write_text(json.dumps(manifest, indent=2))
                return {"name": name, "enabled": enabled, "note": "restart required"}
        except Exception as e:
            return {"error": str(e)}
    return {"error": f"Extension '{name}' not found"}


def get_registry() -> dict:
    return _registry
