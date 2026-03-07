#!/usr/bin/env bash
# oAIo — uninstall.sh
# Interactive uninstaller. Safe — confirms each step, never deletes model data.
# Usage: bash uninstall.sh

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

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$SCRIPT_DIR"
COMPOSE_FILE="$PROJECT_ROOT/docker-compose.yml"
OAIO_SYMLINK_ROOT="/mnt/oaio"

SUDO=""
need_sudo() {
  if [[ $EUID -ne 0 ]]; then
    if command -v sudo &>/dev/null; then
      SUDO="sudo"
    else
      err "Root privileges needed but sudo not available."
      return 1
    fi
  fi
}

# Detect deployment type from .env
DEPLOY_TYPE=""
if [[ -f "$PROJECT_ROOT/.env" ]]; then
  DEPLOY_TYPE=$(grep "^OAIO_DEPLOY_TYPE=" "$PROJECT_ROOT/.env" 2>/dev/null | cut -d= -f2- || true)
fi

ask() {
  local prompt="$1" default="${2:-N}"
  local yn
  if [[ "$default" == "Y" ]]; then
    read -rp "  $prompt [Y/n]: " yn
    case "${yn:-Y}" in [Yy]*) return 0 ;; *) return 1 ;; esac
  else
    read -rp "  $prompt [y/N]: " yn
    case "${yn:-N}" in [Yy]*) return 0 ;; *) return 1 ;; esac
  fi
}

# ─── Survey ─────────────────────────────────────────────────────────────────

survey() {
  step "Current oAIo Installation"

  # Running containers
  if [[ -f "$COMPOSE_FILE" ]] && docker compose --file "$COMPOSE_FILE" ps --quiet 2>/dev/null | grep -q .; then
    info "Running containers:"
    docker compose --file "$COMPOSE_FILE" ps --format '    {{.Name}}  ({{.Status}})' 2>/dev/null || true
  else
    dim "  No running oAIo containers found."
  fi

  # Built images
  local images
  images=$(docker images --format '{{.Repository}}:{{.Tag}}' 2>/dev/null \
    | grep -E '^(oaio|comfyui|rvc|kokoro-tts|f5-tts|styletts2|oaio-)' || true)
  if [[ -n "$images" ]]; then
    info "Local built images:"
    printf '%s\n' "$images" | while read -r img; do printf "    %s\n" "$img"; done
  else
    dim "  No locally-built oAIo images found."
  fi

  # Symlinks
  if [[ -d "$OAIO_SYMLINK_ROOT" ]]; then
    local link_count
    link_count=$(find "$OAIO_SYMLINK_ROOT" -maxdepth 1 -type l 2>/dev/null | wc -l)
    info "Symlinks: ${link_count} links in ${OAIO_SYMLINK_ROOT}"
  else
    dim "  No symlink directory at ${OAIO_SYMLINK_ROOT}"
  fi

  # Systemd
  if systemctl list-unit-files oaio-stack.service &>/dev/null 2>&1; then
    info "Systemd: oaio-stack.service found"
  else
    dim "  No systemd service found."
  fi

  # .env
  [[ -f "$PROJECT_ROOT/.env" ]] && info ".env file exists" || dim "  No .env file."

  [[ -n "$DEPLOY_TYPE" ]] && info "Deployment type: ${DEPLOY_TYPE}"

  printf "\n"
  printf "  ${C_BOLD}${C_RED}WARNING:${C_RESET} This will NOT delete your model data, audio files,\n"
  printf "  or any content in your actual data directories.\n"
  printf "  Only oAIo containers, images, symlinks, and config are affected.\n"
  printf "\n"
}

# ─── Step 1: Stop + remove containers ───────────────────────────────────────

remove_containers() {
  step "Step 1 — Stop and remove containers"

  if [[ "$DEPLOY_TYPE" == "ui-only" ]]; then
    dim "  UI Only deployment — no containers to remove."
    return 0
  fi

  if [[ ! -f "$COMPOSE_FILE" ]]; then
    dim "  No docker-compose.yml found — skipping."
    return 0
  fi

  local running
  running=$(docker compose --file "$COMPOSE_FILE" ps --quiet 2>/dev/null | wc -l)
  if [[ "$running" -eq 0 ]]; then
    dim "  No running containers."
  fi

  if ask "Stop and remove all oAIo containers?"; then
    docker compose --file "$COMPOSE_FILE" down --remove-orphans 2>&1 | sed 's/^/    /'
    ok "Containers stopped and removed."
  else
    info "Skipped."
  fi
}

# ─── Step 2: Remove built images ────────────────────────────────────────────

remove_images() {
  step "Step 2 — Remove locally-built Docker images"

  if [[ "$DEPLOY_TYPE" == "ui-only" ]]; then
    dim "  UI Only deployment — no images to remove."
    return 0
  fi

  local images
  images=$(docker images --format '{{.Repository}}:{{.Tag}}  ({{.Size}})' 2>/dev/null \
    | grep -E '^(oaio|comfyui|rvc|kokoro-tts|f5-tts|styletts2|oaio-)' || true)

  if [[ -z "$images" ]]; then
    dim "  No locally-built oAIo images found."
    return 0
  fi

  printf "  Images to remove:\n"
  printf '%s\n' "$images" | while read -r img; do printf "    ${C_RED}%s${C_RESET}\n" "$img"; done
  printf "\n"
  info "Base images (ollama/ollama:rocm, open-webui) will NOT be removed."

  if ask "Remove these locally-built images?"; then
    for name in oaio comfyui rvc kokoro-tts f5-tts styletts2; do
      docker rmi "$name" 2>/dev/null && ok "Removed: $name" || true
    done
    # Also catch tagged variants
    docker images --format '{{.Repository}}:{{.Tag}}' 2>/dev/null \
      | grep -E '^oaio-' | while read -r img; do
        docker rmi "$img" 2>/dev/null && ok "Removed: $img" || true
      done
    ok "Built images removed."
  else
    info "Skipped."
  fi
}

# ─── Step 3: Remove symlinks ────────────────────────────────────────────────

remove_symlinks() {
  step "Step 3 — Remove /mnt/oaio symlinks"

  if [[ ! -d "$OAIO_SYMLINK_ROOT" ]]; then
    dim "  No symlink directory at ${OAIO_SYMLINK_ROOT}"
    return 0
  fi

  local link_count
  link_count=$(find "$OAIO_SYMLINK_ROOT" -maxdepth 1 -type l 2>/dev/null | wc -l)
  info "${link_count} symlinks in ${OAIO_SYMLINK_ROOT}"
  warn "This removes the SYMLINKS only — your actual data directories are untouched."

  if ask "Remove ${OAIO_SYMLINK_ROOT} and all symlinks?"; then
    need_sudo
    $SUDO rm -rf "$OAIO_SYMLINK_ROOT"
    ok "Symlinks removed."
  else
    info "Skipped."
  fi
}

# ─── Step 4: Remove systemd service ─────────────────────────────────────────

remove_systemd() {
  step "Step 4 — Remove systemd service"

  local svc_file="/etc/systemd/system/oaio-stack.service"
  if [[ ! -f "$svc_file" ]]; then
    dim "  No systemd service installed."
    return 0
  fi

  if ask "Disable and remove oaio-stack.service?"; then
    need_sudo
    $SUDO systemctl stop oaio-stack.service 2>/dev/null || true
    $SUDO systemctl disable oaio-stack.service 2>/dev/null || true
    $SUDO rm -f "$svc_file"
    $SUDO systemctl daemon-reload
    ok "Systemd service removed."
  else
    info "Skipped."
  fi
}

# ─── Step 5: Remove .env ────────────────────────────────────────────────────

remove_env() {
  step "Step 5 — Remove .env file"

  if [[ ! -f "$PROJECT_ROOT/.env" ]]; then
    dim "  No .env file."
    return 0
  fi

  if ask "Remove .env file?"; then
    rm -f "$PROJECT_ROOT/.env"
    ok ".env removed."
  else
    info "Skipped."
  fi
}

# ─── Step 6: Remove config (dangerous) ──────────────────────────────────────

remove_config() {
  step "Step 6 — Remove config directory"

  if [[ ! -d "$PROJECT_ROOT/config" ]]; then
    dim "  No config directory."
    return 0
  fi

  printf "  ${C_RED}${C_BOLD}WARNING:${C_RESET} This deletes modes.json, services.json, nodes.json,\n"
  printf "  routing.json, paths.json — all your oAIo configuration.\n\n"

  if ask "Delete config directory? (THIS IS DESTRUCTIVE)"; then
    rm -rf "$PROJECT_ROOT/config"
    ok "Config directory removed."
  else
    info "Skipped — config preserved."
  fi
}

# ─── Step 7: Remove Docker volume ───────────────────────────────────────────

remove_volumes() {
  step "Step 7 — Remove Docker volumes"

  local vol_exists=0
  docker volume inspect open-webui &>/dev/null && vol_exists=1 || true

  if [[ $vol_exists -eq 0 ]]; then
    dim "  No oAIo Docker volumes found."
    return 0
  fi

  warn "The open-webui volume contains your chat history and RAG data."

  if ask "Remove open-webui Docker volume?"; then
    docker volume rm open-webui 2>/dev/null && ok "Volume removed." || warn "Could not remove (may still be in use)."
  else
    info "Skipped — volume preserved."
  fi
}

# ─── Summary ────────────────────────────────────────────────────────────────

print_summary() {
  printf "\n"
  printf "${C_BOLD}${C_CYAN}══ Uninstall Complete${C_RESET}\n\n"

  printf "  ${C_BOLD}What was removed:${C_RESET}\n"
  printf "  Check the output above for details on each step.\n\n"

  printf "  ${C_BOLD}What was preserved:${C_RESET}\n"
  if [[ -f "$PROJECT_ROOT/.env" ]]; then
    local _storage_root _ollama_dir
    _storage_root=$(grep "^STORAGE_ROOT=" "$PROJECT_ROOT/.env" 2>/dev/null | cut -d= -f2- || echo "/mnt/storage")
    _ollama_dir=$(grep "^OLLAMA_MODELS_DIR=" "$PROJECT_ROOT/.env" 2>/dev/null | cut -d= -f2- || echo "(configured location)")
    printf "  • Model data:     ${_storage_root}/ai/*\n"
    printf "  • Ollama models:  ${_ollama_dir}\n"
    printf "  • Audio files:    ${_storage_root}/ai/audio/*\n"
    printf "  • ComfyUI data:   ${_storage_root}/ai/comfyui/*\n"
  else
    printf "  • All model data, audio files, and content in your data directories\n"
  fi
  printf "  • Source code:    ${PROJECT_ROOT}\n\n"

  printf "  To reinstall: ${C_BOLD}cd ${PROJECT_ROOT} && bash install.sh${C_RESET}\n"
  printf "  To fully remove source: ${C_BOLD}rm -rf ${PROJECT_ROOT}${C_RESET}\n\n"
}

# ─── Main ───────────────────────────────────────────────────────────────────

main() {
  banner "oAIo Uninstaller"
  dim   "  Project : $PROJECT_ROOT"
  dim   "  User    : $(whoami)  |  Host: $(hostname)"

  survey

  if ! ask "Proceed with uninstall?"; then
    info "Cancelled."
    exit 0
  fi

  remove_containers
  remove_images
  remove_symlinks
  remove_systemd
  remove_env
  remove_config
  remove_volumes
  print_summary
}

main "$@"
