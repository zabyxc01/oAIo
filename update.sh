#!/usr/bin/env bash
# oAIo — update.sh
# Interactive updater for the oAIo stack.
# Safe to re-run. Backs up configs before changes.
# Usage: bash update.sh [--yes]

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

# ─── Absolute project root ──────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$SCRIPT_DIR"
COMPOSE_FILE="$PROJECT_ROOT/docker-compose.yml"
ENV_FILE="$PROJECT_ROOT/.env"
CONFIG_DIR="$PROJECT_ROOT/config"

# ─── Auto-yes flag ───────────────────────────────────────────────────────────

AUTO_YES=0
for arg in "$@"; do
  case "$arg" in
    --yes|-y) AUTO_YES=1 ;;
  esac
done

# ─── Prompt helper — respects --yes ──────────────────────────────────────────

confirm() {
  local prompt="$1" default="${2:-N}"
  if [[ $AUTO_YES -eq 1 ]]; then
    return 0
  fi
  local yn
  read -rp "  ${prompt} [y/N]: " yn
  case "${yn:-$default}" in
    [Yy]*) return 0 ;;
    *)     return 1 ;;
  esac
}

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

# ─── Service lists ───────────────────────────────────────────────────────────

ALL_SERVICES=(oaio ollama open-webui kokoro-tts rvc f5-tts comfyui styletts2)
REMOTE_IMAGES=(ollama open-webui)
LOCAL_BUILDS=(oaio comfyui styletts2 rvc kokoro-tts f5-tts)

# Restart order: dependencies first, oaio (control plane) last
RESTART_ORDER=(ollama kokoro-tts rvc f5-tts open-webui comfyui styletts2 oaio)

# ─── Tracking ────────────────────────────────────────────────────────────────

declare -a SUMMARY_DONE=()
declare -a SUMMARY_SKIPPED=()
declare -a SUMMARY_WARNINGS=()

track_done()    { SUMMARY_DONE+=("$*"); }
track_skip()    { SUMMARY_SKIPPED+=("$*"); }
track_warn()    { SUMMARY_WARNINGS+=("$*"); }

# ─── Step 1 — Pre-flight check ──────────────────────────────────────────────

preflight() {
  step "Step 1 — Pre-flight check"
  printf "\n"

  # Docker
  if ! command -v docker &>/dev/null; then
    die "Docker not found. Install Docker first."
  fi
  if ! docker info &>/dev/null 2>&1; then
    die "Docker daemon not reachable (not in docker group?)."
  fi
  local _docker_ver
  _docker_ver=$(docker --version 2>/dev/null | awk '{print $3}' | tr -d ',' || echo "?")
  ok "Docker ${_docker_ver}"

  # Compose
  if ! docker compose version &>/dev/null 2>&1; then
    die "docker compose (v2 plugin) not found."
  fi
  local _compose_ver
  _compose_ver=$(docker compose version --short 2>/dev/null || echo "v2")
  ok "docker compose ${_compose_ver}"

  # Project directory
  if [[ ! -f "$COMPOSE_FILE" ]]; then
    die "docker-compose.yml not found at ${COMPOSE_FILE}"
  fi
  ok "Project root: ${PROJECT_ROOT}"

  # Current container status
  printf "\n"
  info "Current container status:"
  printf "\n"
  docker compose --file "$COMPOSE_FILE" --project-directory "$PROJECT_ROOT" \
    ps --format "table {{.Name}}\t{{.Status}}\t{{.Ports}}" 2>/dev/null || \
    docker compose --file "$COMPOSE_FILE" --project-directory "$PROJECT_ROOT" ps 2>/dev/null || \
    warn "Could not read container status."
  printf "\n"

  track_done "Pre-flight check passed"
}

# ─── Step 2 — Backup configs ────────────────────────────────────────────────

backup_configs() {
  step "Step 2 — Backup configs"
  printf "\n"

  if ! confirm "Backup config files before updating?"; then
    track_skip "Config backup"
    return 0
  fi

  local _ts
  _ts=$(date +"%Y-%m-%d_%H%M%S")
  local _backup_dir="${CONFIG_DIR}/backups/${_ts}"

  mkdir -p "$_backup_dir"
  info "Backup directory: ${_backup_dir}"

  local _count=0

  # Back up all JSON configs
  for f in "${CONFIG_DIR}"/*.json; do
    [[ -f "$f" ]] || continue
    cp "$f" "$_backup_dir/"
    ok "  $(basename "$f")"
    (( _count++ )) || true
  done

  # Back up .env if it exists
  if [[ -f "$ENV_FILE" ]]; then
    cp "$ENV_FILE" "$_backup_dir/.env"
    ok "  .env"
    (( _count++ )) || true
  fi

  if [[ $_count -eq 0 ]]; then
    warn "No config files found to backup."
    rmdir "$_backup_dir" 2>/dev/null || true
    track_skip "Config backup (no files)"
  else
    ok "Backed up ${_count} file(s) to ${_backup_dir}"
    track_done "Config backup -> ${_backup_dir}"
  fi
}

# ─── Step 3 — Pull remote images ────────────────────────────────────────────

pull_remote_images() {
  step "Step 3 — Pull remote images"
  printf "\n"

  info "Remote image services: ${REMOTE_IMAGES[*]}"
  printf "\n"

  if ! confirm "Pull latest remote images (ollama, open-webui)?"; then
    track_skip "Pull remote images"
    return 0
  fi

  # Capture before-image IDs
  declare -A _before_ids
  for svc in "${REMOTE_IMAGES[@]}"; do
    local _img
    _img=$(docker compose --file "$COMPOSE_FILE" --project-directory "$PROJECT_ROOT" \
      images "$svc" --format "{{.ID}}" 2>/dev/null | head -1 || echo "none")
    _before_ids[$svc]="$_img"
  done

  # Pull
  info "Pulling..."
  printf "\n"
  docker compose --file "$COMPOSE_FILE" --project-directory "$PROJECT_ROOT" \
    pull "${REMOTE_IMAGES[@]}" 2>&1 || {
    warn "Some images may have failed to pull."
    track_warn "Partial pull failure"
  }

  # Show before/after
  printf "\n"
  for svc in "${REMOTE_IMAGES[@]}"; do
    local _after
    _after=$(docker compose --file "$COMPOSE_FILE" --project-directory "$PROJECT_ROOT" \
      images "$svc" --format "{{.ID}}" 2>/dev/null | head -1 || echo "none")
    local _before="${_before_ids[$svc]}"
    if [[ "$_before" == "$_after" ]]; then
      info "${svc}: ${C_DIM}unchanged${C_RESET} (${_after})"
    else
      ok "${svc}: ${_before} -> ${_after}"
    fi
  done

  track_done "Pulled remote images"
}

# ─── Step 4 — Rebuild local images ──────────────────────────────────────────

rebuild_local_images() {
  step "Step 4 — Rebuild local images"
  printf "\n"

  info "Locally built services: ${LOCAL_BUILDS[*]}"
  printf "\n"

  if ! confirm "Rebuild local images?"; then
    track_skip "Rebuild local images"
    return 0
  fi

  # Let user choose which to rebuild (unless --yes)
  local _to_build=()

  if [[ $AUTO_YES -eq 1 ]]; then
    _to_build=("${LOCAL_BUILDS[@]}")
  else
    printf "\n"
    info "Select which services to rebuild (Enter = all):"
    printf "\n"
    for i in "${!LOCAL_BUILDS[@]}"; do
      printf "  ${C_BOLD}%d)${C_RESET} %s\n" "$((i + 1))" "${LOCAL_BUILDS[$i]}"
    done
    printf "\n"
    read -rp "  Enter numbers separated by spaces, or press Enter for all: " _selection

    if [[ -z "$_selection" ]]; then
      _to_build=("${LOCAL_BUILDS[@]}")
    else
      for num in $_selection; do
        local idx=$((num - 1))
        if [[ $idx -ge 0 && $idx -lt ${#LOCAL_BUILDS[@]} ]]; then
          _to_build+=("${LOCAL_BUILDS[$idx]}")
        else
          warn "Invalid selection: ${num} — skipping."
        fi
      done
    fi
  fi

  if [[ ${#_to_build[@]} -eq 0 ]]; then
    warn "No services selected for rebuild."
    track_skip "Rebuild (none selected)"
    return 0
  fi

  info "Building: ${_to_build[*]}"
  printf "\n"

  docker compose --file "$COMPOSE_FILE" --project-directory "$PROJECT_ROOT" \
    build "${_to_build[@]}" 2>&1 || {
    warn "Build completed with warnings or errors — check output above."
    track_warn "Build may have partial failures"
  }

  printf "\n"
  ok "Rebuilt: ${_to_build[*]}"
  track_done "Rebuilt images: ${_to_build[*]}"
}

# ─── Step 5 — Rolling restart ───────────────────────────────────────────────

rolling_restart() {
  step "Step 5 — Rolling restart"
  printf "\n"

  if ! confirm "Rolling-restart containers (oaio last)?"; then
    track_skip "Rolling restart"
    return 0
  fi

  # Figure out which containers are actually running or exist in compose
  local _running=()
  local _all_names
  _all_names=$(docker compose --file "$COMPOSE_FILE" --project-directory "$PROJECT_ROOT" \
    ps -a --format "{{.Name}}" 2>/dev/null || true)

  for svc in "${RESTART_ORDER[@]}"; do
    if echo "$_all_names" | grep -qx "$svc" 2>/dev/null; then
      _running+=("$svc")
    fi
  done

  if [[ ${#_running[@]} -eq 0 ]]; then
    warn "No containers currently managed. Starting all..."
    docker compose --file "$COMPOSE_FILE" --project-directory "$PROJECT_ROOT" \
      up -d --remove-orphans 2>&1
    ok "All containers started."
    track_done "Started all containers (none were running)"
    return 0
  fi

  info "Restart order: ${_running[*]}"
  printf "\n"

  local _total=${#_running[@]}
  local _current=0
  local _failed=()

  for svc in "${_running[@]}"; do
    (( _current++ )) || true
    printf "  ${C_BOLD}[%d/%d]${C_RESET} Restarting ${C_CYAN}%s${C_RESET}..." "$_current" "$_total" "$svc"

    # Stop
    docker compose --file "$COMPOSE_FILE" --project-directory "$PROJECT_ROOT" \
      stop "$svc" 2>/dev/null || true

    # Start
    docker compose --file "$COMPOSE_FILE" --project-directory "$PROJECT_ROOT" \
      up -d "$svc" 2>/dev/null || {
      printf " ${C_RED}FAILED${C_RESET}\n"
      warn "${svc} failed to start."
      _failed+=("$svc")
      track_warn "${svc} failed to start"
      continue
    }

    # Wait for healthy (up to 20 seconds)
    local _waited=0
    local _healthy=0
    while [[ $_waited -lt 20 ]]; do
      local _state
      _state=$(docker inspect --format '{{.State.Status}}' "$svc" 2>/dev/null || echo "unknown")
      if [[ "$_state" == "running" ]]; then
        # Check if container has a health check defined
        local _health
        _health=$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$svc" 2>/dev/null || echo "none")
        if [[ "$_health" == "healthy" || "$_health" == "none" ]]; then
          _healthy=1
          break
        fi
      fi
      sleep 2
      _waited=$(( _waited + 2 ))
    done

    if [[ $_healthy -eq 1 ]]; then
      printf " ${C_GREEN}OK${C_RESET}\n"
    else
      printf " ${C_YELLOW}TIMEOUT${C_RESET}\n"
      warn "${svc}: not healthy after 20s — continuing anyway."
      _failed+=("$svc")
      track_warn "${svc} health check timeout"
    fi
  done

  printf "\n"
  if [[ ${#_failed[@]} -eq 0 ]]; then
    ok "All ${_total} containers restarted successfully."
  else
    warn "${#_failed[@]} container(s) had issues: ${_failed[*]}"
  fi
  track_done "Rolling restart (${_total} containers)"
}

# ─── Step 6 — Heal symlinks ─────────────────────────────────────────────────

heal_symlinks() {
  step "Step 6 — Heal symlinks"
  printf "\n"

  local _oaio_root="/mnt/oaio"
  local _dangling=()

  if [[ ! -d "$_oaio_root" ]]; then
    warn "/mnt/oaio does not exist — skipping symlink check."
    track_skip "Heal symlinks (no /mnt/oaio)"
    return 0
  fi

  # Check for dangling symlinks
  for link in "$_oaio_root"/*; do
    [[ -L "$link" ]] || continue
    if [[ ! -e "$link" ]]; then
      _dangling+=("$(basename "$link")")
      warn "Dangling: $link -> $(readlink "$link")"
    else
      ok "  $(basename "$link") -> $(readlink "$link")"
    fi
  done

  if [[ ${#_dangling[@]} -eq 0 ]]; then
    printf "\n"
    ok "All symlinks healthy (no dangling links)."
    track_done "Symlinks verified (all healthy)"
    return 0
  fi

  printf "\n"
  warn "${#_dangling[@]} dangling symlink(s) found: ${_dangling[*]}"
  printf "\n"

  if ! confirm "Run scripts/setup-oaio-symlinks.sh to repair?"; then
    track_skip "Heal symlinks (user declined)"
    return 0
  fi

  local _script="${PROJECT_ROOT}/scripts/setup-oaio-symlinks.sh"
  if [[ ! -f "$_script" ]]; then
    err "Symlink script not found: ${_script}"
    track_warn "Symlink repair script missing"
    return 0
  fi

  run_root bash "$_script"
  ok "Symlink repair complete."
  track_done "Repaired ${#_dangling[@]} dangling symlink(s)"
}

# ─── Step 7 — Post-update health check ──────────────────────────────────────

post_health_check() {
  step "Step 7 — Post-update health check"
  printf "\n"

  if ! confirm "Run post-update health checks?"; then
    track_skip "Post-update health check"
    return 0
  fi

  # Hit /system/status
  local _port="${OAIO_API_PORT:-9000}"
  local _url="http://localhost:${_port}/system/status"

  info "Checking oAIo API at ${_url}..."
  local _resp
  _resp=$(curl -sf --max-time 5 "$_url" 2>/dev/null || true)

  if [[ -n "$_resp" ]]; then
    ok "oAIo API responding."
    # Pretty-print if python3 available
    if command -v python3 &>/dev/null; then
      printf "\n"
      echo "$_resp" | python3 -m json.tool 2>/dev/null | head -30 || echo "$_resp"
      printf "\n"
    fi
  else
    warn "oAIo API not responding at ${_url}."
    track_warn "API not responding at ${_url}"
  fi

  # VRAM usage
  local _vram_used_file="/sys/class/drm/card1/device/mem_info_vram_used"
  local _vram_total_file="/sys/class/drm/card1/device/mem_info_vram_total"
  if [[ -f "$_vram_used_file" && -f "$_vram_total_file" ]]; then
    local _vram_used _vram_total _vram_used_mb _vram_total_mb _vram_pct
    _vram_used=$(cat "$_vram_used_file" 2>/dev/null || echo 0)
    _vram_total=$(cat "$_vram_total_file" 2>/dev/null || echo 1)
    _vram_used_mb=$(( _vram_used / 1024 / 1024 ))
    _vram_total_mb=$(( _vram_total / 1024 / 1024 ))
    _vram_pct=$(( _vram_used * 100 / _vram_total ))
    ok "VRAM: ${_vram_used_mb} MB / ${_vram_total_mb} MB (${_vram_pct}%)"
  else
    info "VRAM sysfs not available (card1)."
  fi

  # System RAM
  local _ram_total _ram_avail _ram_used _ram_pct
  _ram_total=$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo 2>/dev/null || echo 0)
  _ram_avail=$(awk '/MemAvailable/ {print int($2/1024)}' /proc/meminfo 2>/dev/null || echo 0)
  _ram_used=$(( _ram_total - _ram_avail ))
  if [[ $_ram_total -gt 0 ]]; then
    _ram_pct=$(( _ram_used * 100 / _ram_total ))
    ok "RAM: ${_ram_used} MB / ${_ram_total} MB (${_ram_pct}%)"
  fi

  # Verify all expected containers are running
  printf "\n"
  info "Container status:"
  printf "\n"

  local _running_count=0 _expected_count=0 _not_running=()
  for svc in "${ALL_SERVICES[@]}"; do
    local _state
    _state=$(docker inspect --format '{{.State.Status}}' "$svc" 2>/dev/null || echo "not found")
    (( _expected_count++ )) || true
    if [[ "$_state" == "running" ]]; then
      ok "  ${svc}: running"
      (( _running_count++ )) || true
    else
      warn "  ${svc}: ${_state}"
      _not_running+=("$svc")
    fi
  done

  printf "\n"
  if [[ ${#_not_running[@]} -eq 0 ]]; then
    ok "All ${_expected_count} containers running."
  else
    warn "${_running_count}/${_expected_count} running. Not running: ${_not_running[*]}"
    track_warn "Containers not running: ${_not_running[*]}"
  fi

  track_done "Post-update health check complete"
}

# ─── Step 8 — Summary ───────────────────────────────────────────────────────

print_summary() {
  step "Step 8 — Update Summary"
  printf "\n"
  printf "  ${C_BOLD}${C_GREEN}══════════════════════════════════════════${C_RESET}\n"
  printf "  ${C_BOLD}${C_GREEN}  oAIo Update Complete${C_RESET}\n"
  printf "  ${C_BOLD}${C_GREEN}══════════════════════════════════════════${C_RESET}\n"
  printf "\n"

  if [[ ${#SUMMARY_DONE[@]} -gt 0 ]]; then
    printf "  ${C_BOLD}Completed:${C_RESET}\n"
    for item in "${SUMMARY_DONE[@]}"; do
      printf "    ${C_GREEN}✔${C_RESET} %s\n" "$item"
    done
    printf "\n"
  fi

  if [[ ${#SUMMARY_SKIPPED[@]} -gt 0 ]]; then
    printf "  ${C_BOLD}Skipped:${C_RESET}\n"
    for item in "${SUMMARY_SKIPPED[@]}"; do
      printf "    ${C_DIM}— %s${C_RESET}\n" "$item"
    done
    printf "\n"
  fi

  if [[ ${#SUMMARY_WARNINGS[@]} -gt 0 ]]; then
    printf "  ${C_BOLD}${C_YELLOW}Warnings:${C_RESET}\n"
    for item in "${SUMMARY_WARNINGS[@]}"; do
      printf "    ${C_YELLOW}⚠${C_RESET}  %s\n" "$item"
    done
    printf "\n"
  fi

  dim "  Logs:  docker compose -f ${COMPOSE_FILE} logs -f"
  dim "  Stop:  docker compose -f ${COMPOSE_FILE} down"
  printf "\n"
}

# ─── Main ────────────────────────────────────────────────────────────────────

main() {
  banner "oAIo Updater"
  dim   "  Project : $PROJECT_ROOT"
  dim   "  User    : $(whoami)  |  Host: $(hostname)"
  dim   "  Time    : $(date '+%Y-%m-%d %H:%M:%S')"
  [[ $AUTO_YES -eq 1 ]] && dim "  Mode    : --yes (non-interactive)"
  printf "\n"

  # Source .env if present (for port variables)
  if [[ -f "$ENV_FILE" ]]; then
    set +u
    # shellcheck disable=SC1090
    source "$ENV_FILE"
    set -u
  fi

  preflight
  backup_configs
  pull_remote_images
  rebuild_local_images
  rolling_restart
  heal_symlinks
  post_health_check
  print_summary
}

main "$@"
