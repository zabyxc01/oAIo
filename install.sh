#!/usr/bin/env bash
# oAIo — install.sh
# Interactive installer for the oAIo AI infrastructure stack.
# Safe to re-run (idempotent). Supports AMD ROCm and NVIDIA CUDA.
# Usage: bash install.sh

set -euo pipefail

# ─── Colour helpers ──────────────────────────────────────────────────────────

if [[ -t 1 ]]; then
  C_RESET="\033[0m"
  C_BOLD="\033[1m"
  C_RED="\033[1;31m"
  C_GREEN="\033[1;32m"
  C_YELLOW="\033[1;33m"
  C_BLUE="\033[1;34m"
  C_CYAN="\033[1;36m"
  C_DIM="\033[2m"
else
  C_RESET="" C_BOLD="" C_RED="" C_GREEN="" C_YELLOW="" C_BLUE="" C_CYAN="" C_DIM=""
fi

info()    { printf "  ${C_BLUE}•${C_RESET} %s\n"          "$*"; }
ok()      { printf "  ${C_GREEN}✔${C_RESET} %s\n"         "$*"; }
warn()    { printf "  ${C_YELLOW}⚠${C_RESET}  %s\n"       "$*"; }
err()     { printf "  ${C_RED}✖${C_RESET} %s\n"           "$*" >&2; }
step()    { printf "\n${C_BOLD}${C_CYAN}══ %s${C_RESET}\n" "$*"; }
banner()  { printf "\n${C_BOLD}${C_BLUE}%s${C_RESET}\n"   "$*"; }
dim()     { printf "${C_DIM}%s${C_RESET}\n"               "$*"; }
die()     { err "$*"; exit 1; }

# ─── Absolute project root ────────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$SCRIPT_DIR"
COMPOSE_FILE="$PROJECT_ROOT/docker-compose.yml"
ENV_FILE="$PROJECT_ROOT/.env"

# ─── sudo helper — only escalate when needed ─────────────────────────────────

SUDO=""
_sudo_check_done=0
need_sudo() {
  if [[ $_sudo_check_done -eq 1 ]]; then return; fi
  _sudo_check_done=1
  if [[ $EUID -ne 0 ]]; then
    if command -v sudo &>/dev/null; then
      warn "Some operations require elevated privileges."
      info "You may be prompted for your sudo password."
      SUDO="sudo"
    else
      die "Root privileges needed but sudo is not available. Re-run as root."
    fi
  fi
}

run_root() {
  need_sudo
  $SUDO "$@"
}

# ─── Step 0 — System requirements check ──────────────────────────────────────

check_system() {
  step "Step 0 — System Requirements"
  printf "\n"

  local _errors=0
  local _warnings=0

  # ── Docker present + daemon reachable ────────────────────────────────────
  if command -v docker &>/dev/null; then
    local _docker_ver
    _docker_ver=$(docker --version 2>/dev/null | awk '{print $3}' | tr -d ',' || echo "?")
    if docker info &>/dev/null 2>&1; then
      ok "Docker ${_docker_ver}"
    else
      err "Docker ${_docker_ver} found but daemon not reachable (not in docker group?)"
      (( _errors++ )) || true
    fi
  else
    err "Docker not found — install from https://docs.docker.com/engine/install/"
    (( _errors++ )) || true
  fi

  # ── docker compose v2 ────────────────────────────────────────────────────
  if docker compose version &>/dev/null 2>&1; then
    local _compose_ver
    _compose_ver=$(docker compose version --short 2>/dev/null || echo "v2")
    ok "docker compose ${_compose_ver}"
  else
    err "docker compose (v2 plugin) not found — install the Docker Compose plugin"
    (( _errors++ )) || true
  fi

  # ── RAM check ────────────────────────────────────────────────────────────
  local _ram_kb _ram_gb
  _ram_kb=$(awk '/MemTotal/ {print $2}' /proc/meminfo 2>/dev/null || echo 0)
  _ram_gb=$(( _ram_kb / 1024 / 1024 ))
  if [[ $_ram_gb -lt 8 ]]; then
    err "RAM: ${_ram_gb} GB — minimum 8 GB required"
    (( _errors++ )) || true
  elif [[ $_ram_gb -lt 16 ]]; then
    warn "RAM: ${_ram_gb} GB — 16 GB or more recommended"
    (( _warnings++ )) || true
  else
    ok "RAM: ${_ram_gb} GB"
  fi

  # ── Disk free at project root ─────────────────────────────────────────────
  local _disk_avail_kb _disk_avail_gb
  _disk_avail_kb=$(df -k "$PROJECT_ROOT" 2>/dev/null | awk 'NR==2 {print $4}' || echo 0)
  _disk_avail_gb=$(( _disk_avail_kb / 1024 / 1024 ))
  if [[ $_disk_avail_gb -lt 50 ]]; then
    warn "Disk: ${_disk_avail_gb} GB free at ${PROJECT_ROOT} — 50 GB or more recommended"
    (( _warnings++ )) || true
  else
    ok "Disk: ${_disk_avail_gb} GB free at ${PROJECT_ROOT}"
  fi

  # ── GPU detection (required: AMD or NVIDIA) ───────────────────────────────
  local _sys_gpu_vendor="none"
  local _sys_amd_gfx="" _sys_amd_hsa=""

  if [[ -e /dev/kfd ]]; then
    _sys_gpu_vendor="amd"

    # GFX auto-detect (same logic as detect_gpu, reported here)
    local _gfx_raw=""
    if command -v rocminfo &>/dev/null; then
      _gfx_raw=$(rocminfo 2>/dev/null \
        | grep -i "Name:" | grep -oi "gfx[0-9a-f]*" | head -1 || true)
    fi
    if [[ -z "$_gfx_raw" ]]; then
      for _uevent in /sys/class/drm/card*/device/uevent; do
        [[ -f "$_uevent" ]] || continue
        local _cand
        _cand=$(grep -oi "gfx[0-9a-f]*" "$_uevent" 2>/dev/null | head -1 || true)
        [[ -n "$_cand" ]] && { _gfx_raw="$_cand"; break; }
      done
    fi
    if [[ -z "$_gfx_raw" ]]; then
      for _namefile in /sys/class/drm/card*/device/ip_discovery/die/*/gfx/*/name; do
        [[ -f "$_namefile" ]] || continue
        local _raw
        _raw=$(cat "$_namefile" 2>/dev/null || true)
        if [[ "$_raw" =~ (gfx[0-9a-f]+) ]]; then
          _gfx_raw="${BASH_REMATCH[1]}"; break
        fi
      done
    fi

    if [[ -n "$_gfx_raw" ]]; then
      _sys_amd_gfx=$(printf '%s' "$_gfx_raw" | tr '[:upper:]' '[:lower:]')
      local _digits="${_sys_amd_gfx#gfx}" _len=${#_digits}
      if [[ $_len -eq 4 ]]; then
        _sys_amd_hsa="${_digits:0:2}.${_digits:2:1}.${_digits:3:1}"
      elif [[ $_len -eq 3 ]]; then
        _sys_amd_hsa="${_digits:0:1}.${_digits:1:1}.${_digits:2:1}"
      else
        _sys_amd_hsa="${_digits}.0.0"
      fi
    else
      _sys_amd_gfx="unknown"
      _sys_amd_hsa="?"
    fi

    ok "AMD GPU detected (${_sys_amd_gfx})"

    # ROCm installed check
    local _rocm_ver="unknown"
    if [[ -f /opt/rocm/.info/version ]]; then
      _rocm_ver=$(cat /opt/rocm/.info/version 2>/dev/null | head -1 | tr -d '[:space:]' || echo "unknown")
    elif command -v rocm-smi &>/dev/null; then
      _rocm_ver=$(rocm-smi --version 2>/dev/null | grep -i "ROCm" | grep -oi "[0-9][0-9.]*" | head -1 || echo "unknown")
    fi

    if [[ -f /opt/rocm/bin/rocminfo ]] || command -v rocm-smi &>/dev/null; then
      # version comparison: warn if < 5.0
      local _rocm_major
      _rocm_major=$(printf '%s' "$_rocm_ver" | cut -d. -f1)
      if [[ "$_rocm_major" =~ ^[0-9]+$ ]] && [[ "$_rocm_major" -lt 5 ]]; then
        warn "ROCm ${_rocm_ver} installed — upgrade to 5.0+ recommended"
        (( _warnings++ )) || true
      else
        ok "ROCm ${_rocm_ver} installed"
      fi
    else
      warn "ROCm not detected — install from https://rocm.docs.amd.com/"
      (( _warnings++ )) || true
    fi

    ok "GFX target: ${_sys_amd_gfx} (HSA: ${_sys_amd_hsa})"

  elif command -v nvidia-smi &>/dev/null && nvidia-smi &>/dev/null 2>&1; then
    _sys_gpu_vendor="nvidia"
    local _nvgpu
    _nvgpu=$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1 || echo "Unknown")
    ok "NVIDIA GPU detected (${_nvgpu})"
  else
    err "No GPU detected — AMD /dev/kfd or nvidia-smi required"
    (( _errors++ )) || true
  fi

  # ── SAM / Resizable BAR (optional) ───────────────────────────────────────
  local _sam_found=0
  for _res in /sys/bus/pci/devices/*/resource; do
    [[ -f "$_res" ]] || continue
    local _line0
    _line0=$(head -1 "$_res" 2>/dev/null || true)
    # format: 0x<start> 0x<end> 0x<flags>
    if [[ "$_line0" =~ ^0x([0-9a-fA-F]+)[[:space:]]+0x([0-9a-fA-F]+) ]]; then
      local _start _end _size
      _start=$(( 16#${BASH_REMATCH[1]} ))
      _end=$(( 16#${BASH_REMATCH[2]} ))
      _size=$(( _end - _start + 1 ))
      if [[ $_size -ge $(( 4 * 1024 * 1024 * 1024 )) ]]; then
        _sam_found=1; break
      fi
    fi
  done
  if [[ $_sam_found -eq 1 ]]; then
    ok "SAM (Resizable BAR): enabled"
  else
    warn "SAM (Resizable BAR): not detected — enable in BIOS: Above 4G Decoding + Re-Size BAR"
    (( _warnings++ )) || true
  fi

  # ── Thunderbolt (optional) ────────────────────────────────────────────────
  if [[ -d /sys/bus/thunderbolt/devices ]]; then
    local _tb_count
    _tb_count=$(ls /sys/bus/thunderbolt/devices/ 2>/dev/null | wc -l || echo 0)
    if [[ $_tb_count -gt 0 ]]; then
      ok "Thunderbolt: ${_tb_count} controller(s)"
    else
      ok "Thunderbolt: bus present, no controllers"
    fi
  else
    info "Thunderbolt: not present"
  fi

  # ── CPU cores (informational) ─────────────────────────────────────────────
  local _threads
  _threads=$(nproc 2>/dev/null || grep -c ^processor /proc/cpuinfo 2>/dev/null || echo 0)
  if [[ $_threads -lt 8 ]]; then
    warn "CPU: ${_threads} threads — 8+ recommended for running multiple containers"
    (( _warnings++ )) || true
  else
    printf "  ${C_BLUE}•${C_RESET} CPU: ${_threads} threads\n"
  fi

  printf "\n"
  printf "  System check complete. ${_errors} error(s), ${_warnings} warning(s).\n"
  printf "\n"

  if [[ $_errors -gt 0 ]]; then
    err "System check failed with ${_errors} error(s). Fix the above issues and re-run."
    exit 1
  fi
}

# ─── Prereq check ────────────────────────────────────────────────────────────

check_prereqs() {
  step "Checking prerequisites"
  local missing=()

  command -v git &>/dev/null || missing+=("git")

  if [[ ${#missing[@]} -gt 0 ]]; then
    err "Missing required tools: ${missing[*]}"
    info "Install with: sudo apt-get install -y ${missing[*]}"
    exit 1
  fi

  if ! docker compose version &>/dev/null 2>&1; then
    die "docker compose (v2 plugin) not found. Install the Docker Compose plugin."
  fi

  ok "git $(git --version | awk '{print $3}')"
  ok "docker compose $(docker compose version --short 2>/dev/null || echo 'v2')"
}

# ─── Step 1 — Deployment type ────────────────────────────────────────────────

DEPLOY_TYPE=""

select_deployment_type() {
  step "Step 1 — Deployment type"
  printf "\n"
  printf "  ${C_BOLD}1)${C_RESET} Local Workstation   — daily driver, full control\n"
  printf "  ${C_BOLD}2)${C_RESET} Training Node       — rented GPU, job-focused\n"
  printf "  ${C_BOLD}3)${C_RESET} Fleet Node          — managed instance, remotely orchestrated\n"
  printf "  ${C_BOLD}4)${C_RESET} Custom              — I know what I'm doing\n"
  printf "  ${C_BOLD}5)${C_RESET} Headless Server     — API + containers only, no frontend UI\n"
  printf "  ${C_BOLD}6)${C_RESET} UI Only             — remote control panel, no local containers\n"
  printf "\n"

  while true; do
    read -rp "  Select deployment type [1-6]: " choice
    case "$choice" in
      1) DEPLOY_TYPE="workstation"; break ;;
      2) DEPLOY_TYPE="training";    break ;;
      3) DEPLOY_TYPE="fleet";       break ;;
      4) DEPLOY_TYPE="custom";      break ;;
      5) DEPLOY_TYPE="headless";    break ;;
      6) DEPLOY_TYPE="ui-only";     break ;;
      *) warn "Enter 1, 2, 3, 4, 5, or 6." ;;
    esac
  done

  ok "Deployment type: ${C_BOLD}${DEPLOY_TYPE}${C_RESET}"
}

# ─── Step 2 — Component selection ────────────────────────────────────────────

COMP_CONTROL=1
COMP_LLM=0
COMP_VOICE=0
COMP_RENDER=0
COMP_TRAINING=0
COMP_FLEET=0

select_components() {
  step "Step 2 — Component selection"

  local avail_llm avail_voice avail_render avail_training avail_fleet
  case "$DEPLOY_TYPE" in
    workstation)
      COMP_LLM=0; COMP_VOICE=0; COMP_RENDER=0; COMP_TRAINING=0; COMP_FLEET=0
      avail_llm=1; avail_voice=1; avail_render=1; avail_training=1; avail_fleet=0
      ;;
    training)
      COMP_LLM=0; COMP_VOICE=0; COMP_RENDER=0; COMP_TRAINING=1; COMP_FLEET=0
      avail_llm=0; avail_voice=0; avail_render=0; avail_training=1; avail_fleet=0
      ;;
    fleet)
      COMP_LLM=0; COMP_VOICE=0; COMP_RENDER=0; COMP_TRAINING=0; COMP_FLEET=1
      avail_llm=1; avail_voice=1; avail_render=1; avail_training=1; avail_fleet=1
      ;;
    custom)
      COMP_LLM=0; COMP_VOICE=0; COMP_RENDER=0; COMP_TRAINING=0; COMP_FLEET=0
      avail_llm=1; avail_voice=1; avail_render=1; avail_training=1; avail_fleet=1
      ;;
    headless)
      COMP_LLM=0; COMP_VOICE=0; COMP_RENDER=0; COMP_TRAINING=0; COMP_FLEET=0
      avail_llm=1; avail_voice=1; avail_render=1; avail_training=1; avail_fleet=1
      ;;
    ui-only)
      COMP_CONTROL=0; COMP_LLM=0; COMP_VOICE=0; COMP_RENDER=0; COMP_TRAINING=0; COMP_FLEET=0
      avail_llm=0; avail_voice=0; avail_render=0; avail_training=0; avail_fleet=0
      ;;
  esac

  if [[ "$DEPLOY_TYPE" == "ui-only" ]]; then
    info "UI Only mode — no local containers."
    return
  fi

  _render_menu() {
    printf "\n"
    printf "  Toggle by number. Press ${C_BOLD}Enter${C_RESET} to confirm.\n\n"
    printf "  ${C_DIM}[x] = selected   [ ] = not selected   [—] = unavailable${C_RESET}\n\n"
    printf "  ${C_GREEN}[x]${C_RESET} ${C_BOLD}1) Control Plane${C_RESET}     — oAIo API + UI ${C_DIM}(always required)${C_RESET}\n"

    local llm_mark voice_mark render_mark training_mark fleet_mark
    [[ $COMP_LLM      -eq 1 ]] && llm_mark="${C_GREEN}[x]${C_RESET}"      || llm_mark="[ ]"
    [[ $COMP_VOICE    -eq 1 ]] && voice_mark="${C_GREEN}[x]${C_RESET}"    || voice_mark="[ ]"
    [[ $COMP_RENDER   -eq 1 ]] && render_mark="${C_GREEN}[x]${C_RESET}"   || render_mark="[ ]"
    [[ $COMP_TRAINING -eq 1 ]] && training_mark="${C_GREEN}[x]${C_RESET}" || training_mark="[ ]"
    [[ $COMP_FLEET    -eq 1 ]] && fleet_mark="${C_GREEN}[x]${C_RESET}"    || fleet_mark="[ ]"

    if [[ $avail_llm -eq 1 ]]; then
      printf "  %b ${C_BOLD}2) LLM Stack${C_RESET}         — Ollama + Open-WebUI\n" "$llm_mark"
    else
      printf "  ${C_DIM}[—] 2) LLM Stack         — not available for this deployment${C_RESET}\n"
    fi
    if [[ $avail_voice -eq 1 ]]; then
      printf "  %b ${C_BOLD}3) Voice Stack${C_RESET}       — Kokoro + RVC + F5-TTS + StyleTTS2\n" "$voice_mark"
    else
      printf "  ${C_DIM}[—] 3) Voice Stack       — not available for this deployment${C_RESET}\n"
    fi
    if [[ $avail_render -eq 1 ]]; then
      printf "  %b ${C_BOLD}4) Render Stack${C_RESET}      — ComfyUI\n" "$render_mark"
    else
      printf "  ${C_DIM}[—] 4) Render Stack      — not available for this deployment${C_RESET}\n"
    fi
    if [[ $avail_training -eq 1 ]]; then
      printf "  %b ${C_BOLD}5) Training Stack${C_RESET}    — ${C_DIM}(placeholder — future)${C_RESET}\n" "$training_mark"
    else
      printf "  ${C_DIM}[—] 5) Training Stack    — not available for this deployment${C_RESET}\n"
    fi
    if [[ $avail_fleet -eq 1 ]]; then
      printf "  %b ${C_BOLD}6) Fleet Services${C_RESET}    — ${C_DIM}(placeholder — future)${C_RESET}\n" "$fleet_mark"
    else
      printf "  ${C_DIM}[—] 6) Fleet Services    — not available for this deployment${C_RESET}\n"
    fi
    printf "\n"
  }

  while true; do
    _render_menu
    read -rp "  Toggle [2-6] or press Enter to confirm: " choice
    case "$choice" in
      "")   break ;;
      1)    warn "Control Plane is always required." ;;
      2)    [[ $avail_llm      -eq 1 ]] && COMP_LLM=$(( 1 - COMP_LLM ))           || warn "Not available." ;;
      3)    [[ $avail_voice    -eq 1 ]] && COMP_VOICE=$(( 1 - COMP_VOICE ))        || warn "Not available." ;;
      4)    [[ $avail_render   -eq 1 ]] && COMP_RENDER=$(( 1 - COMP_RENDER ))      || warn "Not available." ;;
      5)    [[ $avail_training -eq 1 ]] && COMP_TRAINING=$(( 1 - COMP_TRAINING ))  || warn "Not available." ;;
      6)    [[ $avail_fleet    -eq 1 ]] && COMP_FLEET=$(( 1 - COMP_FLEET ))        || warn "Not available." ;;
      *)    warn "Enter 2–6 or press Enter." ;;
    esac
  done

  ok "Control Plane selected (required)"
  [[ $COMP_LLM      -eq 1 ]] && ok "LLM Stack selected"
  [[ $COMP_VOICE    -eq 1 ]] && ok "Voice Stack selected"
  [[ $COMP_RENDER   -eq 1 ]] && ok "Render Stack selected"
  [[ $COMP_TRAINING -eq 1 ]] && ok "Training Stack selected"
  [[ $COMP_FLEET    -eq 1 ]] && ok "Fleet Services selected"
  true
}

REMOTE_API_URL=""

configure_remote_api() {
  if [[ "$DEPLOY_TYPE" != "ui-only" && "$DEPLOY_TYPE" != "fleet" ]]; then
    return 0
  fi

  step "Remote API Configuration"
  printf "\n"
  info "This instance will connect to a remote oAIo backend."
  info "Enter the URL of the remote oAIo API (e.g., http://100.99.194.124:9000)"
  printf "\n"

  while true; do
    read -rp "  Remote API URL: " REMOTE_API_URL
    if [[ -z "$REMOTE_API_URL" ]]; then
      warn "URL cannot be empty."
    elif [[ "$REMOTE_API_URL" =~ ^https?:// ]]; then
      ok "Remote API: ${REMOTE_API_URL}"
      break
    else
      warn "URL must start with http:// or https://"
    fi
  done
}

# ─── Step 3 — GPU detection + .env ───────────────────────────────────────────

GPU_VENDOR=""
AMD_GFX=""
AMD_HSA_VERSION=""
DOCKER_GPU_FLAGS=""

detect_gpu() {
  step "Step 3 — GPU detection"

  if [[ -e /dev/kfd ]]; then
    GPU_VENDOR="amd"
    ok "AMD GPU detected (/dev/kfd present)"

    local gfx_raw=""

    if command -v rocminfo &>/dev/null; then
      gfx_raw=$(rocminfo 2>/dev/null \
        | grep -i "Name:" | grep -oi "gfx[0-9a-f]*" | head -1 || true)
    fi

    if [[ -z "$gfx_raw" ]]; then
      for uevent in /sys/class/drm/card*/device/uevent; do
        [[ -f "$uevent" ]] || continue
        local candidate
        candidate=$(grep -oi "gfx[0-9a-f]*" "$uevent" 2>/dev/null | head -1 || true)
        [[ -n "$candidate" ]] && { gfx_raw="$candidate"; break; }
      done
    fi

    if [[ -z "$gfx_raw" ]]; then
      for namefile in /sys/class/drm/card*/device/ip_discovery/die/*/gfx/*/name; do
        [[ -f "$namefile" ]] || continue
        local raw
        raw=$(cat "$namefile" 2>/dev/null || true)
        if [[ "$raw" =~ (gfx[0-9a-f]+) ]]; then
          gfx_raw="${BASH_REMATCH[1]}"; break
        fi
      done
    fi

    if [[ -n "$gfx_raw" ]]; then
      AMD_GFX=$(printf '%s' "$gfx_raw" | tr '[:upper:]' '[:lower:]')
      local digits="${AMD_GFX#gfx}" len=${#digits}
      if [[ $len -eq 4 ]]; then
        AMD_HSA_VERSION="${digits:0:2}.${digits:2:1}.${digits:3:1}"
      elif [[ $len -eq 3 ]]; then
        AMD_HSA_VERSION="${digits:0:1}.${digits:1:1}.${digits:2:1}"
      else
        AMD_HSA_VERSION="${digits}.0.0"
      fi
      ok "GFX target: ${C_BOLD}${AMD_GFX}${C_RESET}  (HSA: ${AMD_HSA_VERSION})"
    else
      warn "Could not auto-detect AMD GFX version. Defaulting to gfx1100 / 11.0.0"
      warn "Edit .env after install if wrong."
      AMD_GFX="gfx1100"; AMD_HSA_VERSION="11.0.0"
    fi

    DOCKER_GPU_FLAGS="--device /dev/kfd --device /dev/dri --group-add video"

  elif command -v nvidia-smi &>/dev/null && nvidia-smi &>/dev/null 2>&1; then
    GPU_VENDOR="nvidia"
    local nvgpu
    nvgpu=$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1 || echo "Unknown")
    ok "NVIDIA GPU: ${C_BOLD}${nvgpu}${C_RESET}"
    AMD_GFX="n/a"; AMD_HSA_VERSION="n/a"
    if ! command -v nvidia-container-runtime &>/dev/null; then
      warn "nvidia-container-runtime not found — install NVIDIA Container Toolkit."
    fi
    DOCKER_GPU_FLAGS="--gpus all"

  else
    GPU_VENDOR="none"
    warn "No GPU detected — containers may fail at runtime."
    AMD_GFX="gfx1100"; AMD_HSA_VERSION="11.0.0"; DOCKER_GPU_FLAGS=""
  fi
}

write_env() {
  step "Writing .env"

  local existing_ollama_dir=""
  if [[ -f "$ENV_FILE" ]]; then
    existing_ollama_dir=$(grep "^OLLAMA_MODELS_DIR=" "$ENV_FILE" 2>/dev/null \
      | cut -d= -f2- || true)
    info "Existing .env found — updating."
  fi

  local _headless=0 _ui_only=0
  [[ "$DEPLOY_TYPE" == "headless" ]] && _headless=1
  [[ "$DEPLOY_TYPE" == "ui-only" ]] && _ui_only=1

  cat > "$ENV_FILE" <<ENVEOF
# oAIo — generated by install.sh on $(date -u +"%Y-%m-%dT%H:%M:%SZ")
# Edit manually or re-run install.sh to regenerate.

# ── Deployment ────────────────────────────────────────────────────────────────
OAIO_DEPLOY_TYPE=${DEPLOY_TYPE}

# ── GPU ───────────────────────────────────────────────────────────────────────
GPU_VENDOR=${GPU_VENDOR}
PYTORCH_ROCM_ARCH=${AMD_GFX}
HSA_OVERRIDE_GFX_VERSION=${AMD_HSA_VERSION}
DOCKER_GPU_FLAGS=${DOCKER_GPU_FLAGS}

# ── Component selection ───────────────────────────────────────────────────────
OAIO_COMP_LLM=${COMP_LLM}
OAIO_COMP_VOICE=${COMP_VOICE}
OAIO_COMP_RENDER=${COMP_RENDER}
OAIO_COMP_TRAINING=${COMP_TRAINING}
OAIO_COMP_FLEET=${COMP_FLEET}

# ── Split deployment ─────────────────────────────────────────────────────────
OAIO_HEADLESS=${_headless}
OAIO_UI_ONLY=${_ui_only}
OAIO_REMOTE_API=${REMOTE_API_URL:-}

# ── Paths ─────────────────────────────────────────────────────────────────────
OLLAMA_MODELS_DIR=${existing_ollama_dir:-/mnt/windows-sata/ollama-models}
STORAGE_ROOT=/mnt/storage
OAIO_SYMLINK_ROOT=/mnt/oaio

# ── Service ports ─────────────────────────────────────────────────────────────
OAIO_PORT=9000
OAIO_AUDIO_PORT=8002
OLLAMA_PORT=11434
OPEN_WEBUI_PORT=3000
KOKORO_PORT=8000
RVC_PORT=8001
F5_PORT=7860
COMFYUI_PORT=8188

# ── HuggingFace ───────────────────────────────────────────────────────────────
HF_HOME=/mnt/oaio/hf-cache
ENVEOF

  ok ".env written to $ENV_FILE"
}

# ─── Step 4 — Path configuration + symlinks ──────────────────────────────────

OLLAMA_MODELS_PATH="/mnt/windows-sata/ollama-models"
COMFYUI_MODELS_PATH="/mnt/storage/ai/comfyui/models"
AUDIO_PATH="/mnt/storage/ai/audio"
HF_CACHE_PATH="/mnt/storage/ai/audio/huggingface"
TRAINING_PATH="/mnt/storage/ai/training"
COMFYUI_BASE_PATH="$HOME/ComfyUI"
REF_AUDIO_PATH="$HOME/reference-audio"
RVC_REF_PATH="$HOME/Videos/audio/_EDITED"
STORAGE_ROOT="/mnt/storage"

ask_custom_paths() {
  step "Step 4 — Path configuration"
  printf "\n"
  info "The symlink layer maps /mnt/oaio/* to your actual data directories."
  info "Defaults shown in [brackets]. Press Enter to accept.\n"

  _ask_path() {
    local label="$1" default="$2" varname="$3"
    printf "  ${C_BOLD}%-28s${C_RESET} [%s]: " "$label" "$default"
    local input; read -r input
    printf -v "$varname" '%s' "${input:-$default}"
  }

  _ask_path "Ollama models dir"   "$OLLAMA_MODELS_PATH" OLLAMA_MODELS_PATH
  _ask_path "AI storage root"     "$STORAGE_ROOT"       STORAGE_ROOT

  COMFYUI_MODELS_PATH="${STORAGE_ROOT}/ai/comfyui/models"
  AUDIO_PATH="${STORAGE_ROOT}/ai/audio"
  HF_CACHE_PATH="${STORAGE_ROOT}/ai/audio/huggingface"
  TRAINING_PATH="${STORAGE_ROOT}/ai/training"

  _ask_path "ComfyUI install dir" "$COMFYUI_BASE_PATH"  COMFYUI_BASE_PATH
  _ask_path "Reference audio dir" "$REF_AUDIO_PATH"     REF_AUDIO_PATH
  _ask_path "RVC reference audio" "$RVC_REF_PATH"       RVC_REF_PATH

  printf "\n"
  ok "Paths confirmed."
}

run_symlinks() {
  step "Creating /mnt/oaio symlinks"
  need_sudo

  [[ -d /mnt/oaio ]] || run_root mkdir -p /mnt/oaio

  local dirs=(
    "$COMFYUI_MODELS_PATH" "${COMFYUI_MODELS_PATH}/loras"
    "$AUDIO_PATH" "${AUDIO_PATH}/kokoro-voices"
    "$HF_CACHE_PATH" "${AUDIO_PATH}/rvc-weights" "${AUDIO_PATH}/rvc-indices"
    "$TRAINING_PATH" "${STORAGE_ROOT}/swap" "$REF_AUDIO_PATH"
    "${COMFYUI_BASE_PATH}/custom_nodes" "${COMFYUI_BASE_PATH}/user"
    "${COMFYUI_BASE_PATH}/output" "${COMFYUI_BASE_PATH}/input"
  )
  for d in "${dirs[@]}"; do
    [[ -d "$d" ]] || { info "Creating: $d"; mkdir -p "$d" 2>/dev/null || run_root mkdir -p "$d"; }
  done

  _link() {
    local name="$1" target="$2" link="/mnt/oaio/$1"
    if [[ -L "$link" ]] && [[ "$(readlink "$link")" == "$target" ]]; then
      ok "  $link ${C_DIM}(unchanged)${C_RESET}"
    else
      [[ -L "$link" ]] && info "  Updating: $link -> $target" || info "  Creating: $link -> $target"
      run_root ln -sfn "$target" "$link"
      ok "  $link -> $target"
    fi
  }

  _link ollama        "$OLLAMA_MODELS_PATH"
  _link models        "$COMFYUI_MODELS_PATH"
  _link lora          "${COMFYUI_MODELS_PATH}/loras"
  _link custom-nodes  "${COMFYUI_BASE_PATH}/custom_nodes"
  _link comfyui-user  "${COMFYUI_BASE_PATH}/user"
  _link outputs       "${COMFYUI_BASE_PATH}/output"
  _link inputs        "${COMFYUI_BASE_PATH}/input"
  _link audio         "$AUDIO_PATH"
  _link kokoro-voices "${AUDIO_PATH}/kokoro-voices"
  _link hf-cache      "$HF_CACHE_PATH"
  _link ref-audio     "$REF_AUDIO_PATH"
  _link rvc-ref       "$RVC_REF_PATH"
  _link swap          "${STORAGE_ROOT}/swap"
  _link training      "$TRAINING_PATH"
  _link rvc-weights   "${AUDIO_PATH}/rvc-weights"
  _link rvc-indices   "${AUDIO_PATH}/rvc-indices"

  printf "\n"
  ok "All 16 symlinks verified under /mnt/oaio"

  [[ -f "$ENV_FILE" ]] && \
    sed -i "s|^OLLAMA_MODELS_DIR=.*|OLLAMA_MODELS_DIR=${OLLAMA_MODELS_PATH}|" "$ENV_FILE"

  update_paths_config
}

update_paths_config() {
  local _cfg="${PROJECT_ROOT}/config/paths.json"
  [[ -f "$_cfg" ]] || { warn "config/paths.json not found — skipping paths sync."; return 0; }

  info "Syncing config/paths.json with configured paths..."

  python3 - <<PYEOF
import json, sys

cfg_path = "${_cfg}"
with open(cfg_path, "r") as f:
    cfg = json.load(f)

updates = {
    "ollama":        "${OLLAMA_MODELS_PATH}",
    "models":        "${COMFYUI_MODELS_PATH}",
    "lora":          "${COMFYUI_MODELS_PATH}/loras",
    "custom-nodes":  "${COMFYUI_BASE_PATH}/custom_nodes",
    "comfyui-user":  "${COMFYUI_BASE_PATH}/user",
    "outputs":       "${COMFYUI_BASE_PATH}/output",
    "inputs":        "${COMFYUI_BASE_PATH}/input",
    "audio":         "${AUDIO_PATH}",
    "kokoro-voices": "${AUDIO_PATH}/kokoro-voices",
    "hf-cache":      "${HF_CACHE_PATH}",
    "ref-audio":     "${REF_AUDIO_PATH}",
    "rvc-ref":       "${RVC_REF_PATH}",
    "rvc-weights":   "${AUDIO_PATH}/rvc-weights",
    "rvc-indices":   "${AUDIO_PATH}/rvc-indices",
    "swap":          "${STORAGE_ROOT}/swap",
    "training":      "${TRAINING_PATH}",
}

for key, new_target in updates.items():
    if key in cfg:
        cfg[key]["default_target"] = new_target

with open(cfg_path, "w") as f:
    json.dump(cfg, f, indent=2)

print("  paths.json updated with", len(updates), "entries")
PYEOF

  ok "config/paths.json synced."
}

# ─── Step 5 — Build + compose up ─────────────────────────────────────────────

build_and_start() {
  step "Step 5 — Starting oAIo stack"

  if [[ "$DEPLOY_TYPE" == "ui-only" ]]; then
    info "UI Only mode — no containers to start."
    info "Serve frontend/src/ with any web server or open index.html directly."
    return
  fi

  local services=("oaio")
  [[ $COMP_LLM    -eq 1 ]] && services+=("ollama" "open-webui")
  [[ $COMP_VOICE  -eq 1 ]] && services+=("kokoro-tts" "rvc" "f5-tts" "styletts2")
  [[ $COMP_RENDER -eq 1 ]] && services+=("comfyui")
  [[ $COMP_TRAINING -eq 1 ]] && warn "Training Stack: no containers yet — skipping."
  [[ $COMP_FLEET    -eq 1 ]] && info "Fleet extension is always active — no extra containers needed."

  info "Services: ${services[*]}"

  local build_services=()
  for svc in "${services[@]}"; do
    case "$svc" in oaio|comfyui|styletts2) build_services+=("$svc") ;; esac
  done

  if [[ ${#build_services[@]} -gt 0 ]]; then
    info "Building: ${build_services[*]}"
    docker compose --file "$COMPOSE_FILE" --project-directory "$PROJECT_ROOT" \
      build "${build_services[@]}"
    ok "Images built."
  fi

  info "Pulling remote images..."
  docker compose --file "$COMPOSE_FILE" --project-directory "$PROJECT_ROOT" \
    pull --ignore-pull-failures "${services[@]}" 2>/dev/null || true

  info "Starting containers..."
  docker compose --file "$COMPOSE_FILE" --project-directory "$PROJECT_ROOT" \
    up -d --remove-orphans "${services[@]}"

  ok "Containers started."

  # ── Post-start health check ───────────────────────────────────────────────
  [[ "$DEPLOY_TYPE" == "headless" ]] && \
    info "Headless mode — frontend served without cache for remote access."

  local _port="${OAIO_PORT:-9000}"
  local _url="http://localhost:${_port}/system/status"
  local _elapsed=0 _healthy=0

  info "Waiting for oAIo API at ${_url} ..."
  while [[ $_elapsed -lt 15 ]]; do
    if curl -sf "$_url" &>/dev/null; then
      _healthy=1; break
    fi
    sleep 2
    _elapsed=$(( _elapsed + 2 ))
  done

  if [[ $_healthy -eq 1 ]]; then
    ok "oAIo API healthy at http://localhost:${_port}"
  else
    warn "API not responding yet — check: docker compose logs oaio"
  fi
}

# ─── Optional: pull Ollama models ─────────────────────────────────────────────

pull_models() {
  [[ $COMP_LLM -eq 1 ]] || return 0

  step "Model pulling"
  printf "\n"

  read -rp "  Pull default Ollama models? [y/N]: " yn
  case "${yn:-N}" in
    [Yy]*)
      info "Pulling nomic-embed-text (required for Open WebUI RAG)..."
      docker exec ollama ollama pull nomic-embed-text
      ok "nomic-embed-text pulled."
      ;;
    *) info "Skipping default models." ;;
  esac

  printf "\n"
  read -rp "  Pull additional LLM models? [y/N]: " yn2
  case "${yn2:-N}" in
    [Yy]*)
      local available=("mistral" "gemma3" "qwen2.5" "dolphin3")
      local selected=()

      printf "\n"
      printf "  Select models to pull (toggle by number, Enter to confirm):\n\n"

      local flags=(0 0 0 0)

      while true; do
        for i in "${!available[@]}"; do
          local mark="[ ]"
          [[ ${flags[$i]} -eq 1 ]] && mark="${C_GREEN}[x]${C_RESET}"
          printf "  %b ${C_BOLD}%d) %s${C_RESET}\n" "$mark" "$(( i + 1 ))" "${available[$i]}"
        done
        printf "\n"
        read -rp "  Toggle [1-${#available[@]}] or press Enter to confirm: " choice
        case "$choice" in
          "") break ;;
          [1-4])
            local idx=$(( choice - 1 ))
            flags[$idx]=$(( 1 - flags[$idx] ))
            ;;
          *) warn "Enter 1-${#available[@]} or press Enter." ;;
        esac
      done

      for i in "${!available[@]}"; do
        if [[ ${flags[$i]} -eq 1 ]]; then
          selected+=("${available[$i]}")
        fi
      done

      if [[ ${#selected[@]} -gt 0 ]]; then
        for model in "${selected[@]}"; do
          info "Pulling ${model}..."
          docker exec ollama ollama pull "$model"
          ok "${model} pulled."
        done
      else
        info "No additional models selected."
      fi
      ;;
    *) info "Skipping additional models." ;;
  esac
}

# ─── Optional: systemd auto-start ────────────────────────────────────────────

install_systemd() {
  local service_src="$PROJECT_ROOT/scripts/oaio-stack.service"
  local service_dst="/etc/systemd/system/oaio-stack.service"
  [[ -f "$service_src" ]] || return 0

  printf "\n"
  read -rp "  Install systemd service for auto-start on boot? [y/N]: " yn
  case "${yn:-N}" in
    [Yy]*)
      need_sudo
      sed "s|WorkingDirectory=.*|WorkingDirectory=${PROJECT_ROOT}|" \
        "$service_src" | run_root tee "$service_dst" > /dev/null
      run_root systemctl daemon-reload
      run_root systemctl enable oaio-stack.service
      ok "oaio-stack.service enabled."
      ;;
    *) info "Skipping systemd install." ;;
  esac
}

# ─── Summary ─────────────────────────────────────────────────────────────────

print_summary() {
  printf "\n"
  printf "${C_BOLD}${C_GREEN}══════════════════════════════════════════${C_RESET}\n"
  if [[ "$DEPLOY_TYPE" == "ui-only" ]]; then
    printf "${C_BOLD}${C_GREEN}  oAIo UI configured!${C_RESET}\n"
  else
    printf "${C_BOLD}${C_GREEN}  oAIo is running!${C_RESET}\n"
  fi
  printf "${C_BOLD}${C_GREEN}══════════════════════════════════════════${C_RESET}\n\n"

  if [[ "$DEPLOY_TYPE" == "ui-only" ]]; then
    printf "  ${C_BOLD}Remote API:${C_RESET}     ${REMOTE_API_URL}\n\n"
    printf "  Open frontend/src/index.html in a browser, or serve it with:\n"
    printf "    ${C_BOLD}python3 -m http.server 8080 -d frontend/src${C_RESET}\n\n"
  elif [[ "$DEPLOY_TYPE" == "headless" ]]; then
    printf "  ${C_BOLD}Headless mode${C_RESET} — connect from a remote UI or browser at http://<this-ip>:9000\n\n"
    printf "  ${C_BOLD}Control Plane:${C_RESET}  http://localhost:9000\n"
    printf "  ${C_BOLD}oAudio API:${C_RESET}     http://localhost:8002\n"
    [[ $COMP_LLM    -eq 1 ]] && printf "  ${C_BOLD}Ollama:${C_RESET}         http://localhost:11434\n"
    [[ $COMP_LLM    -eq 1 ]] && printf "  ${C_BOLD}Open-WebUI:${C_RESET}     http://localhost:3000\n"
    [[ $COMP_VOICE  -eq 1 ]] && printf "  ${C_BOLD}Kokoro TTS:${C_RESET}     http://localhost:8000\n"
    [[ $COMP_VOICE  -eq 1 ]] && printf "  ${C_BOLD}RVC proxy:${C_RESET}      http://localhost:8001\n"
    [[ $COMP_VOICE  -eq 1 ]] && printf "  ${C_BOLD}F5-TTS:${C_RESET}         http://localhost:7860\n"
    [[ $COMP_VOICE  -eq 1 ]] && printf "  ${C_BOLD}StyleTTS2:${C_RESET}      http://localhost:7870\n"
    [[ $COMP_RENDER -eq 1 ]] && printf "  ${C_BOLD}ComfyUI:${C_RESET}        http://localhost:8188\n"
    printf "\n"
  else
    printf "  ${C_BOLD}Control Plane:${C_RESET}  http://localhost:9000\n"
    printf "  ${C_BOLD}oAudio API:${C_RESET}     http://localhost:8002\n"
    [[ $COMP_LLM    -eq 1 ]] && printf "  ${C_BOLD}Ollama:${C_RESET}         http://localhost:11434\n"
    [[ $COMP_LLM    -eq 1 ]] && printf "  ${C_BOLD}Open-WebUI:${C_RESET}     http://localhost:3000\n"
    [[ $COMP_VOICE  -eq 1 ]] && printf "  ${C_BOLD}Kokoro TTS:${C_RESET}     http://localhost:8000\n"
    [[ $COMP_VOICE  -eq 1 ]] && printf "  ${C_BOLD}RVC proxy:${C_RESET}      http://localhost:8001\n"
    [[ $COMP_VOICE  -eq 1 ]] && printf "  ${C_BOLD}F5-TTS:${C_RESET}         http://localhost:7860\n"
    [[ $COMP_VOICE  -eq 1 ]] && printf "  ${C_BOLD}StyleTTS2:${C_RESET}      http://localhost:7870\n"
    [[ $COMP_RENDER -eq 1 ]] && printf "  ${C_BOLD}ComfyUI:${C_RESET}        http://localhost:8188\n"
    printf "\n"
  fi

  printf "  ${C_BOLD}Extensions:${C_RESET}     fleet (multi-node orchestration)\n"
  printf "                  debugger (live log streaming)\n"
  printf "\n"
  if [[ "$DEPLOY_TYPE" != "ui-only" ]]; then
    printf "  ${C_DIM}GPU: ${GPU_VENDOR}"
    [[ "$GPU_VENDOR" == "amd" ]] && printf " / ${AMD_GFX} (HSA: ${AMD_HSA_VERSION})"
    printf "${C_RESET}\n"
    printf "  ${C_DIM}Links  : /mnt/oaio/*${C_RESET}\n"
  fi
  printf "  ${C_DIM}Config : ${ENV_FILE}${C_RESET}\n\n"
  if [[ "$DEPLOY_TYPE" != "ui-only" ]]; then
    printf "  Logs:  ${C_BOLD}docker compose -f ${COMPOSE_FILE} logs -f${C_RESET}\n"
    printf "  Stop:  ${C_BOLD}docker compose -f ${COMPOSE_FILE} down${C_RESET}\n\n"
  fi
}

# ─── Main ────────────────────────────────────────────────────────────────────

main() {
  banner "oAIo Installer"
  dim   "  Project : $PROJECT_ROOT"
  dim   "  User    : $(whoami)  |  Host: $(hostname)"
  printf "\n"

  check_system
  check_prereqs
  select_deployment_type
  select_components
  configure_remote_api
  if [[ "$DEPLOY_TYPE" != "ui-only" ]]; then
    detect_gpu
  fi
  write_env
  if [[ "$DEPLOY_TYPE" != "ui-only" ]]; then
    ask_custom_paths
    run_symlinks
  fi
  build_and_start
  if [[ "$DEPLOY_TYPE" != "ui-only" ]]; then
    pull_models
    install_systemd
  fi
  print_summary
}

main "$@"
