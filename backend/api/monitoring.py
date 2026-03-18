"""
Monitoring routes — WebSocket 1Hz push, enforcement, benchmarks, request monitor.
"""
import asyncio
import json
import math
from datetime import datetime

import psutil
from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect

from api.shared import (
    SERVICES_CFG_FILE,
    config_lock, atomic_write,
    services_cfg, enforcement_mode,
    active_modes, enforcer, kill_log,
    bench_history, req_log, req_stats, monitor_ws_clients,
    profiles_cfg, build_kill_order, get_ollama_loaded,
    get_vram_usage, get_gpu_utilization, all_status, check_alerts,
    get_system_accounting,
)
from core import ram_tier, resources

router = APIRouter()


# ── System status ────────────────────────────────────────────────────────────

@router.get("/system/status", tags=["System"])
def system_status():
    vram = get_vram_usage()
    gpu  = get_gpu_utilization()
    ram  = psutil.virtual_memory()
    acct = get_system_accounting(services_cfg())
    return {
        "vram":         vram,
        "gpu":          gpu,
        "ram":          {"used_gb": round(ram.used/1e9,2), "total_gb": round(ram.total/1e9,2), "percent": ram.percent},
        "ram_tier":     ram_tier.get_usage(),
        "accounting":   acct,
        "active_modes": list(active_modes),
        "services":     all_status(services_cfg()),
        "alerts":       check_alerts(),
    }


@router.get("/vram", tags=["System"])
def vram_status():
    return get_vram_usage()


@router.get("/benchmark/history", tags=["System"])
def benchmark_history():
    return list(bench_history)


# ── WebSocket 1Hz push ──────────────────────────────────────────────────────

@router.websocket("/ws")
async def ws_status(websocket: WebSocket):
    await websocket.accept()
    loop = asyncio.get_event_loop()
    try:
        while True:
            vram    = get_vram_usage()
            gpu     = get_gpu_utilization()
            ram     = psutil.virtual_memory()
            svcs    = services_cfg()
            acct    = get_system_accounting(svcs)
            loaded  = await get_ollama_loaded()

            bench_history.append({
                "ts":        datetime.now().strftime("%H:%M:%S"),
                "vram_used": round(vram.get("used_gb", 0), 2),
                "gpu_pct":   gpu.get("gpu_use_percent", 0),
                "models":    [m["name"] for m in loaded],
            })

            pct = vram.get("percent", 0) / 100
            vram_total = resources.get_effective_vram_total()
            _pcfg = profiles_cfg()
            _active_profile_name = _pcfg.get("active")

            svc_statuses = await loop.run_in_executor(None, all_status, svcs)
            kill_order   = await loop.run_in_executor(None, build_kill_order, svcs)

            payload = {
                "vram":           vram,
                "gpu":            gpu,
                "ram":            {"used_gb": round(ram.used/1e9,2), "total_gb": round(ram.total/1e9,2), "percent": ram.percent},
                "ram_tier":       ram_tier.get_usage(),
                "accounting":     acct,
                "active_modes":   list(active_modes),
                "services":       svc_statuses,
                "alerts":         check_alerts(),
                "kill_log":       list(kill_log)[:10],
                "ollama_loaded":  loaded,
                "enforcement": {
                    "enabled":           enforcer.enforcer_enabled,
                    "active_modes":      list(active_modes),
                    "enforcing":         enforcer.enforcer_enabled and pct >= resources.HARD_THRESHOLD and bool(active_modes),
                    "warn_threshold":    resources.WARN_THRESHOLD,
                    "hard_threshold":    resources.HARD_THRESHOLD,
                    "warn_at_gb":        round(vram_total * resources.WARN_THRESHOLD, 1),
                    "hard_at_gb":        round(vram_total * resources.HARD_THRESHOLD, 1),
                    "vram_ceiling_gb":   enforcer.vram_virtual_ceiling_gb,
                    "real_total_gb":     vram.get("total_gb", 20),
                    "effective_total_gb": round(vram_total, 1),
                    "kill_order":        kill_order,
                    "mode":              enforcement_mode(),
                    "per_container_vram": enforcer.per_container_vram if enforcement_mode() == "realtime" else {},
                },
                "profile": {
                    "active":          _active_profile_name is not None,
                    "name":            _active_profile_name,
                    "vram_ceiling_gb": enforcer.vram_virtual_ceiling_gb,
                },
            }
            await websocket.send_json(payload)
            await asyncio.sleep(1)
    except WebSocketDisconnect:
        pass


# ── Enforcement ──────────────────────────────────────────────────────────────

@router.get("/enforcement/status", tags=["Enforcement"])
def enforcement_status():
    vram = get_vram_usage()
    pct  = vram.get("percent", 0) / 100
    services = services_cfg()
    vram_total = vram.get("total_gb", resources._FALLBACK_TOTAL)
    enabled = enforcer.enforcer_enabled
    return {
        "vram":            vram,
        "pct":             round(pct * 100, 1),
        "warn_threshold":  resources.WARN_THRESHOLD,
        "hard_threshold":  resources.HARD_THRESHOLD,
        "warn_at_gb":      round(vram_total * resources.WARN_THRESHOLD, 1),
        "hard_at_gb":      round(vram_total * resources.HARD_THRESHOLD, 1),
        "enabled":         enabled,
        "enforcing":       enabled and pct >= resources.HARD_THRESHOLD and bool(active_modes),
        "active_modes":    list(active_modes),
        "paused":          not bool(active_modes),
        "kill_order":      build_kill_order(services),
        "kill_log":        list(kill_log),
    }


@router.post("/enforcement/enable", tags=["Enforcement"])
def enforcement_enable():
    enforcer.enforcer_enabled = True
    return {"enabled": True}


@router.post("/enforcement/disable", tags=["Enforcement"])
def enforcement_disable():
    enforcer.enforcer_enabled = False
    return {"enabled": False}


@router.post("/enforcement/ceiling", tags=["Enforcement"])
def enforcement_set_ceiling(body: dict):
    real_total = get_vram_usage().get("total_gb", 20.0)
    warnings = []

    if "vram_ceiling_gb" in body:
        val = body["vram_ceiling_gb"]
        if val is None or val == 0:
            enforcer.vram_virtual_ceiling_gb = None
        else:
            val = float(val)
            if math.isnan(val) or math.isinf(val):
                return {"error": "VRAM ceiling must be a finite number"}
            if val > real_total:
                warnings.append(f"Ceiling {val}GB exceeds real VRAM ({real_total}GB)")
            if val < 0:
                return {"error": "VRAM ceiling cannot be negative"}
            enforcer.vram_virtual_ceiling_gb = val

    if "warn_threshold" in body:
        wt = float(body["warn_threshold"])
        if math.isnan(wt) or math.isinf(wt):
            return {"error": "warn_threshold must be a finite number"}
        resources.WARN_THRESHOLD = max(0.0, min(1.0, wt))
    if "hard_threshold" in body:
        ht = float(body["hard_threshold"])
        if math.isnan(ht) or math.isinf(ht):
            return {"error": "hard_threshold must be a finite number"}
        resources.HARD_THRESHOLD = max(0.0, min(1.0, ht))

    effective = enforcer.vram_virtual_ceiling_gb or real_total
    return {
        "vram_ceiling_gb": enforcer.vram_virtual_ceiling_gb,
        "effective_total_gb": effective,
        "real_total_gb": real_total,
        "warn_threshold": resources.WARN_THRESHOLD,
        "hard_threshold": resources.HARD_THRESHOLD,
        "warn_at_gb": round(effective * resources.WARN_THRESHOLD, 1),
        "hard_at_gb": round(effective * resources.HARD_THRESHOLD, 1),
        "warnings": warnings,
    }


@router.post("/enforcement/mode", tags=["Enforcement"])
async def enforcement_set_mode(body: dict):
    mode = body.get("mode", "").strip().lower()
    if mode not in ("estimated", "realtime"):
        raise HTTPException(status_code=400, detail="mode must be 'estimated' or 'realtime'")
    async with config_lock:
        cfg = json.loads(SERVICES_CFG_FILE.read_text())
        cfg["enforcement_mode"] = mode
        atomic_write(SERVICES_CFG_FILE, json.dumps(cfg, indent=2))
    return {"enforcement_mode": mode}


@router.get("/enforcement/mode", tags=["Enforcement"])
def enforcement_get_mode():
    cfg = json.loads(SERVICES_CFG_FILE.read_text())
    return {"enforcement_mode": cfg.get("enforcement_mode", "estimated")}


# ── API Monitor ──────────────────────────────────────────────────────────────

@router.get("/api/monitor/stream", tags=["Monitor"])
def monitor_stream(limit: int = 50):
    items = list(req_log)
    return items[-limit:]


@router.get("/api/monitor/stats", tags=["Monitor"])
def monitor_stats():
    total = req_stats["total"]
    avg = round(req_stats["latency_sum"] / total, 1) if total else 0
    top = sorted(req_stats["endpoints"].items(), key=lambda x: -x[1])[:5]
    return {
        "total_reqs": total,
        "avg_latency_ms": avg,
        "error_count": req_stats["errors"],
        "top_endpoints": [{"path": p, "count": c} for p, c in top],
    }


@router.websocket("/api/monitor/ws")
async def monitor_ws(websocket: WebSocket):
    await websocket.accept()
    monitor_ws_clients.append(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        try:
            monitor_ws_clients.remove(websocket)
        except ValueError:
            pass
