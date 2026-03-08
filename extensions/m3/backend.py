"""
M3 extension -- Multi-Model Mode: inference pipelines and training job management.
Mounted at /extensions/m3 by the extension loader.

Pipeline CRUD:
  GET    /pipelines           -- list all pipelines
  POST   /pipelines           -- create pipeline
  GET    /pipelines/{id}      -- get pipeline detail
  PATCH  /pipelines/{id}      -- update pipeline
  DELETE /pipelines/{id}      -- delete pipeline

Pipeline Execution:
  POST   /pipelines/{id}/run  -- execute pipeline with input text

Training Jobs:
  GET    /training/jobs           -- list all training jobs
  POST   /training/jobs           -- create job
  GET    /training/jobs/{id}      -- get job detail with progress
  POST   /training/jobs/{id}/start  -- start job (placeholder)
  POST   /training/jobs/{id}/cancel -- cancel job
  DELETE /training/jobs/{id}      -- delete job

Training Adapters:
  GET    /training/adapters              -- list saved LoRA adapters
  POST   /training/adapters/{name}/convert -- placeholder for GGUF conversion

Config:
  GET    /config              -- get M3 config
  PATCH  /config              -- update config

WebSocket:
  WS     /ws                  -- 1Hz push with pipelines, jobs, config
"""
import asyncio
import json
import os
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

router = APIRouter()

# Persistent state lives next to this file
_STATE_FILE = Path(__file__).parent / "m3.json"

# Allowed base paths for training dataset references
_ALLOWED_DATASET_PATHS = [
    "/mnt/oaio",
    "/mnt/storage",
    "/app/data",
]

_VALID_METHODS = {"qlora", "lora", "full"}

_VALID_JOB_STATUSES = {"created", "queued", "running", "completed", "failed", "cancelled"}

_DEFAULT_CONFIG = {
    "max_loaded_models": 2,
    "default_step_timeout": 120,
    "ollama_url": "http://ollama:11434",
}


# --- State helpers ------------------------------------------------------------

def _load_initial() -> dict:
    """Load state from disk once at import time."""
    if _STATE_FILE.exists():
        try:
            data = json.loads(_STATE_FILE.read_text())
        except Exception:
            data = {}
    else:
        data = {}
    data.setdefault("pipelines", {})
    data.setdefault("training_jobs", {})
    data.setdefault("adapters", {})
    data.setdefault("config", dict(_DEFAULT_CONFIG))
    return data


# Module-level in-memory state -- loaded once, mutated in place
_state: dict = _load_initial()


def _save() -> None:
    """Persist _state to disk atomically (temp + rename)."""
    tmp = _STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(_state, indent=2))
    tmp.rename(_STATE_FILE)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    return uuid.uuid4().hex[:8]


def _ollama_url() -> str:
    return _state["config"].get("ollama_url", _DEFAULT_CONFIG["ollama_url"])


def _step_timeout() -> float:
    return float(_state["config"].get("default_step_timeout", _DEFAULT_CONFIG["default_step_timeout"]))


# --- Validation helpers -------------------------------------------------------

_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9 _\-]{0,63}$")


def _validate_name(name: str) -> str | None:
    """Return error string if name is invalid, else None."""
    if not name or not name.strip():
        return "name is required"
    if not _NAME_RE.match(name.strip()):
        return "name must be 1-64 chars, alphanumeric/space/dash/underscore, starting with alphanumeric"
    return None


def _validate_model_name(model: str) -> str | None:
    """Basic validation for Ollama model names."""
    if not model or not model.strip():
        return "model name is required"
    m = model.strip()
    # Ollama model names: alphanumeric, colons, dots, dashes, slashes
    if not re.match(r"^[a-zA-Z0-9][a-zA-Z0-9.:/_\-]*$", m):
        return f"invalid model name: {m}"
    return None


def _validate_pipeline_steps(steps: list) -> str | None:
    """Validate pipeline step definitions. Returns error or None."""
    if not steps or not isinstance(steps, list):
        return "steps must be a non-empty list"
    if len(steps) > 10:
        return "maximum 10 steps per pipeline"
    for i, step in enumerate(steps):
        if not isinstance(step, dict):
            return f"step {i}: must be an object"
        model = step.get("model", "")
        err = _validate_model_name(model)
        if err:
            return f"step {i}: {err}"
        tpl = step.get("prompt_template", "")
        if tpl and not isinstance(tpl, str):
            return f"step {i}: prompt_template must be a string"
        params = step.get("params", {})
        if params and not isinstance(params, dict):
            return f"step {i}: params must be an object"
    return None


def _validate_dataset_path(path: str) -> str | None:
    """Validate dataset path exists and is under allowed directories."""
    if not path or not path.strip():
        return "dataset_path is required"
    p = Path(path.strip()).resolve()
    # Check under allowed base paths
    allowed = False
    for base in _ALLOWED_DATASET_PATHS:
        try:
            p.relative_to(base)
            allowed = True
            break
        except ValueError:
            continue
    if not allowed:
        return f"dataset_path must be under one of: {', '.join(_ALLOWED_DATASET_PATHS)}"
    if not p.exists():
        return f"dataset_path does not exist: {p}"
    return None


# --- Pipeline CRUD ------------------------------------------------------------

@router.get("/pipelines", tags=["M3-Pipelines"])
def list_pipelines():
    """List all inference pipelines."""
    return list(_state["pipelines"].values())


@router.post("/pipelines", tags=["M3-Pipelines"])
def create_pipeline(body: dict):
    """
    Create a new inference pipeline.
    body: {name, description?, steps: [{model, prompt_template?, params?}]}
    """
    name = (body.get("name") or "").strip()
    err = _validate_name(name)
    if err:
        return {"error": err}

    description = (body.get("description") or "").strip()
    steps = body.get("steps", [])

    err = _validate_pipeline_steps(steps)
    if err:
        return {"error": err}

    # Normalize steps
    normalized_steps = []
    for step in steps:
        normalized_steps.append({
            "model": step["model"].strip(),
            "prompt_template": (step.get("prompt_template") or "").strip(),
            "params": step.get("params", {}),
        })

    pid = _new_id()
    pipeline = {
        "id": pid,
        "name": name,
        "description": description,
        "steps": normalized_steps,
        "created_at": _now(),
        "updated_at": _now(),
        "run_count": 0,
        "last_run_at": None,
    }
    _state["pipelines"][pid] = pipeline
    _save()

    return pipeline


@router.get("/pipelines/{pipeline_id}", tags=["M3-Pipelines"])
def get_pipeline(pipeline_id: str):
    """Get pipeline detail by ID."""
    pipeline = _state["pipelines"].get(pipeline_id)
    if not pipeline:
        return {"error": "Pipeline not found"}
    return pipeline


@router.patch("/pipelines/{pipeline_id}", tags=["M3-Pipelines"])
def update_pipeline(pipeline_id: str, body: dict):
    """
    Update pipeline fields.
    body: {name?, description?, steps?}
    """
    pipeline = _state["pipelines"].get(pipeline_id)
    if not pipeline:
        return {"error": "Pipeline not found"}

    if "name" in body:
        name = (body["name"] or "").strip()
        err = _validate_name(name)
        if err:
            return {"error": err}
        pipeline["name"] = name

    if "description" in body:
        pipeline["description"] = (body["description"] or "").strip()

    if "steps" in body:
        steps = body["steps"]
        err = _validate_pipeline_steps(steps)
        if err:
            return {"error": err}
        normalized_steps = []
        for step in steps:
            normalized_steps.append({
                "model": step["model"].strip(),
                "prompt_template": (step.get("prompt_template") or "").strip(),
                "params": step.get("params", {}),
            })
        pipeline["steps"] = normalized_steps

    pipeline["updated_at"] = _now()
    _save()

    return pipeline


@router.delete("/pipelines/{pipeline_id}", tags=["M3-Pipelines"])
def delete_pipeline(pipeline_id: str):
    """Delete a pipeline."""
    if pipeline_id not in _state["pipelines"]:
        return {"error": "Pipeline not found"}
    name = _state["pipelines"].pop(pipeline_id)["name"]
    _save()
    return {"deleted": pipeline_id, "name": name}


# --- Pipeline Execution ------------------------------------------------------

@router.post("/pipelines/{pipeline_id}/run", tags=["M3-Pipelines"])
async def run_pipeline(pipeline_id: str, body: dict):
    """
    Execute a pipeline with input text.
    body: {input: "user text"}
    Returns: {pipeline_id, input, output, steps: [{model, input, output, duration_ms}], total_duration_ms}
    """
    pipeline = _state["pipelines"].get(pipeline_id)
    if not pipeline:
        return {"error": "Pipeline not found"}

    user_input = body.get("input", "")
    if not user_input or not isinstance(user_input, str):
        return {"error": "input is required and must be a non-empty string"}

    steps = pipeline.get("steps", [])
    if not steps:
        return {"error": "Pipeline has no steps defined"}

    ollama = _ollama_url()
    timeout = _step_timeout()
    step_results = []
    current_input = user_input.strip()
    total_start = time.monotonic()

    for i, step in enumerate(steps):
        model = step["model"]
        template = step.get("prompt_template", "")
        params = step.get("params", {})

        # Build the message content
        if template:
            content = template.replace("{{input}}", current_input)
        else:
            content = current_input

        # Build Ollama /api/chat request
        chat_body = {
            "model": model,
            "messages": [{"role": "user", "content": content}],
            "stream": False,
        }
        # Merge optional params (temperature, top_p, etc.)
        if params:
            if "options" not in chat_body:
                chat_body["options"] = {}
            chat_body["options"].update(params)

        step_start = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                r = await client.post(f"{ollama}/api/chat", json=chat_body)
                if r.status_code != 200:
                    err_text = r.text[:200] if r.text else "Unknown error"
                    return {
                        "error": f"Step {i} ({model}) failed: HTTP {r.status_code}",
                        "detail": err_text,
                        "completed_steps": step_results,
                    }
                data = r.json()
        except httpx.TimeoutException:
            return {
                "error": f"Step {i} ({model}) timed out after {timeout}s",
                "completed_steps": step_results,
            }
        except Exception as e:
            return {
                "error": f"Step {i} ({model}) failed: {type(e).__name__}",
                "completed_steps": step_results,
            }

        step_duration = time.monotonic() - step_start
        output = data.get("message", {}).get("content", "")

        step_results.append({
            "step": i,
            "model": model,
            "input": content,
            "output": output,
            "duration_ms": round(step_duration * 1000),
        })

        # Feed output to next step
        current_input = output

    total_duration = time.monotonic() - total_start

    # Update pipeline stats
    pipeline["run_count"] = pipeline.get("run_count", 0) + 1
    pipeline["last_run_at"] = _now()
    _save()

    return {
        "pipeline_id": pipeline_id,
        "pipeline_name": pipeline["name"],
        "input": user_input,
        "output": current_input,
        "steps": step_results,
        "total_duration_ms": round(total_duration * 1000),
    }


# --- Training Jobs ------------------------------------------------------------

@router.get("/training/jobs", tags=["M3-Training"])
def list_training_jobs():
    """List all training jobs."""
    jobs = list(_state["training_jobs"].values())
    return sorted(jobs, key=lambda j: j.get("created_at", ""), reverse=True)


@router.post("/training/jobs", tags=["M3-Training"])
def create_training_job(body: dict):
    """
    Create a new training job.
    body: {name, base_model, dataset_path, method, hyperparams?}
    method: "qlora" | "lora" | "full"
    hyperparams: {epochs?, learning_rate?, batch_size?, lora_r?, lora_alpha?, ...}
    """
    name = (body.get("name") or "").strip()
    err = _validate_name(name)
    if err:
        return {"error": err}

    base_model = (body.get("base_model") or "").strip()
    err = _validate_model_name(base_model)
    if err:
        return {"error": f"base_model: {err}"}

    dataset_path = (body.get("dataset_path") or "").strip()
    err = _validate_dataset_path(dataset_path)
    if err:
        return {"error": err}

    method = (body.get("method") or "").strip().lower()
    if method not in _VALID_METHODS:
        return {"error": f"method must be one of: {', '.join(sorted(_VALID_METHODS))}"}

    hyperparams = body.get("hyperparams", {})
    if not isinstance(hyperparams, dict):
        return {"error": "hyperparams must be an object"}

    # Validate hyperparams
    hp_errors = _validate_hyperparams(hyperparams, method)
    if hp_errors:
        return {"error": hp_errors}

    # Set defaults
    defaults = {
        "epochs": 3,
        "learning_rate": 2e-4 if method in ("qlora", "lora") else 5e-5,
        "batch_size": 4,
    }
    if method in ("qlora", "lora"):
        defaults["lora_r"] = 16
        defaults["lora_alpha"] = 32

    for k, v in defaults.items():
        hyperparams.setdefault(k, v)

    job_id = _new_id()
    job = {
        "id": job_id,
        "name": name,
        "base_model": base_model,
        "dataset_path": str(Path(dataset_path).resolve()),
        "method": method,
        "hyperparams": hyperparams,
        "status": "created",
        "progress": {
            "epoch": 0,
            "total_epochs": hyperparams.get("epochs", 3),
            "loss": None,
            "eta_seconds": None,
        },
        "output": {
            "adapter_path": None,
            "checkpoints": [],
        },
        "error": None,
        "created_at": _now(),
        "started_at": None,
        "completed_at": None,
    }

    _state["training_jobs"][job_id] = job
    _save()

    return job


def _validate_hyperparams(hp: dict, method: str) -> str | None:
    """Validate hyperparameter values. Returns error or None."""
    if "epochs" in hp:
        if not isinstance(hp["epochs"], int) or hp["epochs"] < 1 or hp["epochs"] > 100:
            return "epochs must be an integer between 1 and 100"
    if "learning_rate" in hp:
        lr = hp["learning_rate"]
        if not isinstance(lr, (int, float)) or lr <= 0 or lr > 1:
            return "learning_rate must be a number between 0 and 1"
    if "batch_size" in hp:
        bs = hp["batch_size"]
        if not isinstance(bs, int) or bs < 1 or bs > 128:
            return "batch_size must be an integer between 1 and 128"
    if method in ("qlora", "lora"):
        if "lora_r" in hp:
            r = hp["lora_r"]
            if not isinstance(r, int) or r < 1 or r > 256:
                return "lora_r must be an integer between 1 and 256"
        if "lora_alpha" in hp:
            a = hp["lora_alpha"]
            if not isinstance(a, int) or a < 1 or a > 512:
                return "lora_alpha must be an integer between 1 and 512"
    return None


@router.get("/training/jobs/{job_id}", tags=["M3-Training"])
def get_training_job(job_id: str):
    """Get training job detail with progress."""
    job = _state["training_jobs"].get(job_id)
    if not job:
        return {"error": "Training job not found"}
    return job


@router.post("/training/jobs/{job_id}/start", tags=["M3-Training"])
def start_training_job(job_id: str):
    """
    Start a training job.
    Placeholder -- training container is not yet available.
    """
    job = _state["training_jobs"].get(job_id)
    if not job:
        return {"error": "Training job not found"}

    if job["status"] not in ("created", "failed"):
        return {"error": f"Cannot start job in state '{job['status']}'"}

    # Placeholder: mark as queued and log that the training container is not available
    job["status"] = "queued"
    job["started_at"] = _now()
    job["error"] = None
    _save()

    print(f"[M3] Training job '{job['name']}' ({job_id}) queued -- training container not yet available")

    return {
        **job,
        "notice": "Training container not yet available. Job has been queued and will run when the training backend is implemented.",
    }


@router.post("/training/jobs/{job_id}/cancel", tags=["M3-Training"])
def cancel_training_job(job_id: str):
    """Cancel a training job."""
    job = _state["training_jobs"].get(job_id)
    if not job:
        return {"error": "Training job not found"}

    if job["status"] in ("completed", "failed", "cancelled"):
        return {"error": f"Cannot cancel job in state '{job['status']}'"}

    job["status"] = "cancelled"
    job["completed_at"] = _now()
    _save()

    return job


@router.delete("/training/jobs/{job_id}", tags=["M3-Training"])
def delete_training_job(job_id: str):
    """Delete a training job."""
    if job_id not in _state["training_jobs"]:
        return {"error": "Training job not found"}

    job = _state["training_jobs"][job_id]
    if job["status"] == "running":
        return {"error": "Cannot delete a running job -- cancel it first"}

    name = _state["training_jobs"].pop(job_id)["name"]
    _save()
    return {"deleted": job_id, "name": name}


# --- Training Adapters --------------------------------------------------------

@router.get("/training/adapters", tags=["M3-Training"])
def list_adapters():
    """List saved LoRA adapters."""
    return list(_state["adapters"].values())


@router.post("/training/adapters/{name}/convert", tags=["M3-Training"])
def convert_adapter(name: str):
    """
    Convert a saved adapter to Ollama GGUF format.
    Placeholder -- conversion tooling is not yet available.
    """
    adapter = _state["adapters"].get(name)
    if not adapter:
        # Also check by name field in values
        adapter = next((a for a in _state["adapters"].values() if a.get("name") == name), None)

    if not adapter:
        return {"error": f"Adapter '{name}' not found"}

    return {
        "adapter": name,
        "status": "not_implemented",
        "notice": "GGUF conversion is not yet available. This endpoint will convert the adapter when the conversion tooling is implemented.",
    }


# --- Config -------------------------------------------------------------------

@router.get("/config", tags=["M3-Config"])
def get_config():
    """Get M3 configuration."""
    return dict(_state["config"])


@router.patch("/config", tags=["M3-Config"])
def patch_config(body: dict):
    """
    Update M3 configuration.
    body: {max_loaded_models?, default_step_timeout?, ollama_url?}
    """
    cfg = _state["config"]
    errors = []

    if "max_loaded_models" in body:
        val = body["max_loaded_models"]
        if not isinstance(val, int) or val < 1 or val > 8:
            errors.append("max_loaded_models must be an integer between 1 and 8")
        else:
            cfg["max_loaded_models"] = val

    if "default_step_timeout" in body:
        val = body["default_step_timeout"]
        if not isinstance(val, (int, float)) or val < 10 or val > 600:
            errors.append("default_step_timeout must be between 10 and 600 seconds")
        else:
            cfg["default_step_timeout"] = int(val)

    if "ollama_url" in body:
        url = (body["ollama_url"] or "").strip()
        if not url or not url.startswith("http"):
            errors.append("ollama_url must be a valid http(s) URL")
        else:
            cfg["ollama_url"] = url.rstrip("/")

    if errors:
        return {"error": errors}

    _save()
    return dict(cfg)


# --- WebSocket ----------------------------------------------------------------

@router.websocket("/ws")
async def m3_ws(websocket: WebSocket):
    """1Hz push -- pipelines, training jobs, config."""
    await websocket.accept()
    try:
        while True:
            pipelines = list(_state["pipelines"].values())
            jobs = sorted(
                _state["training_jobs"].values(),
                key=lambda j: j.get("created_at", ""),
                reverse=True,
            )[:50]
            adapters = list(_state["adapters"].values())

            await websocket.send_json({
                "pipelines": pipelines,
                "training_jobs": jobs,
                "adapters": adapters,
                "config": dict(_state["config"]),
                "counts": {
                    "pipelines": len(pipelines),
                    "jobs_total": len(_state["training_jobs"]),
                    "jobs_active": sum(
                        1 for j in _state["training_jobs"].values()
                        if j.get("status") in ("created", "queued", "running")
                    ),
                    "adapters": len(adapters),
                },
            })
            await asyncio.sleep(1)
    except WebSocketDisconnect:
        pass
