#!/usr/bin/env bash
# oAIo — repair.sh
# Interactive repair and maintenance tool.
# Diagnoses issues and offers fixes. Never destructive without confirmation.
# Usage: bash repair.sh
#        bash repair.sh --check   (dry-run / report-only, no prompts)

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

# ─── Flags ───────────────────────────────────────────────────────────────────

CHECK_ONLY=0
if [[ "${1:-}" == "--check" ]]; then
  CHECK_ONLY=1
fi

# ─── Counters ────────────────────────────────────────────────────────────────

TOTAL_FIXED=0
TOTAL_WARNINGS=0
TOTAL_SKIPPED=0

# ─── Prompt helper — skips in --check mode ───────────────────────────────────

ask_yn() {
  local prompt="$1" default="${2:-N}"
  if [[ $CHECK_ONLY -eq 1 ]]; then return 1; fi
  local hint="y/N"
  [[ "$default" == "Y" ]] && hint="Y/n"
  read -rp "  ${prompt} [${hint}]: " answer
  answer="${answer:-$default}"
  [[ "$answer" =~ ^[Yy] ]]
}

# ─── Utility — human-readable size ──────────────────────────────────────────

human_size() {
  local bytes="$1"
  if [[ $bytes -ge 1073741824 ]]; then
    printf "%.1f GB" "$(echo "scale=1; $bytes / 1073741824" | bc 2>/dev/null || echo 0)"
  elif [[ $bytes -ge 1048576 ]]; then
    printf "%.1f MB" "$(echo "scale=1; $bytes / 1048576" | bc 2>/dev/null || echo 0)"
  elif [[ $bytes -ge 1024 ]]; then
    printf "%.0f KB" "$(echo "scale=0; $bytes / 1024" | bc 2>/dev/null || echo 0)"
  else
    printf "%d B" "$bytes"
  fi
}

# ─── Utility — get disk tier for a path ──────────────────────────────────────

disk_tier() {
  local path="$1"
  local dev
  dev=$(df "$path" 2>/dev/null | awk 'NR==2 {print $1}' || echo "unknown")
  case "$dev" in
    *nvme*)  echo "nvme" ;;
    *sda*|*sdb*)  echo "sata" ;;
    tmpfs|ramfs)  echo "ram" ;;
    *)       echo "other" ;;
  esac
}

# ═════════════════════════════════════════════════════════════════════════════
# Step 1 — Diagnose
# ═════════════════════════════════════════════════════════════════════════════

diag_symlink_issues=()
diag_config_issues=()
diag_docker_issues=()
diag_disk_issues=()
diag_hf_dupes=()
diag_stray_models=()
diag_container_issues=()

diagnose_symlinks() {
  step "Step 1a — Symlink Health"
  printf "\n"

  local paths_file="$PROJECT_ROOT/config/paths.json"
  if [[ ! -f "$paths_file" ]]; then
    err "config/paths.json not found — cannot check symlinks."
    diag_symlink_issues+=("MISSING:config/paths.json")
    return
  fi

  local link_count=0 ok_count=0 bad_count=0

  while IFS='|' read -r key link_path target_path; do
    (( link_count++ )) || true

    if [[ ! -L "$link_path" ]]; then
      if [[ -e "$link_path" ]]; then
        err "$link_path exists but is NOT a symlink"
        diag_symlink_issues+=("NOT_SYMLINK:$key:$link_path")
        (( bad_count++ )) || true
      else
        err "$link_path is missing"
        diag_symlink_issues+=("MISSING_LINK:$key:$link_path:$target_path")
        (( bad_count++ )) || true
      fi
      continue
    fi

    local actual_target
    actual_target=$(readlink "$link_path" 2>/dev/null || echo "")

    if [[ -z "$actual_target" ]]; then
      err "$link_path — cannot read link target"
      diag_symlink_issues+=("UNREADABLE:$key:$link_path")
      (( bad_count++ )) || true
    elif [[ ! -e "$link_path" ]]; then
      err "$link_path -> $actual_target (DANGLING — target does not exist)"
      diag_symlink_issues+=("DANGLING:$key:$link_path:$actual_target")
      (( bad_count++ )) || true
    elif [[ "$actual_target" != "$target_path" ]]; then
      warn "$link_path -> $actual_target (expected: $target_path)"
      diag_symlink_issues+=("WRONG_TARGET:$key:$link_path:$actual_target:$target_path")
      (( bad_count++ )) || true
    else
      ok "$link_path -> $actual_target"
      (( ok_count++ )) || true
    fi
  done < <(python3 -c "
import json, sys
with open('$paths_file') as f:
    d = json.load(f)
for k, v in d.items():
    print(f\"{k}|{v['link']}|{v['default_target']}\")
" 2>/dev/null)

  printf "\n"
  info "Symlinks: $link_count total, $ok_count healthy, $bad_count issues"
}

diagnose_configs() {
  step "Step 1b — Config Validation"
  printf "\n"

  # ── services.json ──────────────────────────────────────────────────────────
  local svc_file="$PROJECT_ROOT/config/services.json"
  if [[ ! -f "$svc_file" ]]; then
    err "config/services.json not found"
    diag_config_issues+=("MISSING_FILE:services.json")
  else
    if ! python3 -c "import json; json.load(open('$svc_file'))" 2>/dev/null; then
      err "config/services.json — invalid JSON syntax"
      diag_config_issues+=("BAD_JSON:services.json")
    else
      ok "config/services.json — valid JSON"

      # Check required fields per service
      local svc_issues
      svc_issues=$(python3 -c "
import json, sys
with open('$svc_file') as f:
    data = json.load(f)
services = data.get('services', {})
required = ['container', 'port', 'group']
for name, svc in services.items():
    for field in required:
        if field not in svc:
            print(f'MISSING_FIELD:services.json:{name}:{field}')
" 2>/dev/null || echo "")

      if [[ -z "$svc_issues" ]]; then
        ok "config/services.json — all services have required fields"
      else
        while IFS= read -r issue; do
          local svc_name field_name
          svc_name=$(echo "$issue" | cut -d: -f3)
          field_name=$(echo "$issue" | cut -d: -f4)
          warn "services.json: service '$svc_name' missing field '$field_name'"
          diag_config_issues+=("$issue")
        done <<< "$svc_issues"
      fi
    fi
  fi

  # ── modes.json ─────────────────────────────────────────────────────────────
  local modes_file="$PROJECT_ROOT/config/modes.json"
  if [[ ! -f "$modes_file" ]]; then
    err "config/modes.json not found"
    diag_config_issues+=("MISSING_FILE:modes.json")
  else
    if ! python3 -c "import json; json.load(open('$modes_file'))" 2>/dev/null; then
      err "config/modes.json — invalid JSON syntax"
      diag_config_issues+=("BAD_JSON:modes.json")
    else
      ok "config/modes.json — valid JSON"

      # Check required fields per mode
      local mode_issues
      mode_issues=$(python3 -c "
import json, sys
with open('$modes_file') as f:
    data = json.load(f)
modes = data.get('modes', {})
for name, mode in modes.items():
    if 'services' not in mode:
        print(f'MISSING_FIELD:modes.json:{name}:services')
    elif not isinstance(mode['services'], list):
        print(f'BAD_TYPE:modes.json:{name}:services:expected list')
" 2>/dev/null || echo "")

      if [[ -z "$mode_issues" ]]; then
        ok "config/modes.json — all modes have required fields"
      else
        while IFS= read -r issue; do
          local mode_name field_name2
          mode_name=$(echo "$issue" | cut -d: -f3)
          field_name2=$(echo "$issue" | cut -d: -f4)
          warn "modes.json: mode '$mode_name' missing field '$field_name2'"
          diag_config_issues+=("$issue")
        done <<< "$mode_issues"
      fi
    fi
  fi

  # ── Cross-reference: modes -> services ─────────────────────────────────────
  if [[ -f "$svc_file" && -f "$modes_file" ]]; then
    local xref_issues
    xref_issues=$(python3 -c "
import json
with open('$svc_file') as f:
    svc_data = json.load(f)
with open('$modes_file') as f:
    mode_data = json.load(f)
known = set(svc_data.get('services', {}).keys())
for mname, mode in mode_data.get('modes', {}).items():
    for s in mode.get('services', []):
        if s not in known:
            print(f'XREF:modes.json:{mname}:{s}')
" 2>/dev/null || echo "")

    if [[ -z "$xref_issues" ]]; then
      ok "Cross-reference — all mode services exist in services.json"
    else
      while IFS= read -r issue; do
        local xref_mode xref_svc
        xref_mode=$(echo "$issue" | cut -d: -f3)
        xref_svc=$(echo "$issue" | cut -d: -f4)
        warn "modes.json: mode '$xref_mode' references unknown service '$xref_svc'"
        diag_config_issues+=("$issue")
      done <<< "$xref_issues"
    fi
  fi

  # ── paths.json ─────────────────────────────────────────────────────────────
  local paths_file="$PROJECT_ROOT/config/paths.json"
  if [[ ! -f "$paths_file" ]]; then
    err "config/paths.json not found"
    diag_config_issues+=("MISSING_FILE:paths.json")
  else
    if ! python3 -c "import json; json.load(open('$paths_file'))" 2>/dev/null; then
      err "config/paths.json — invalid JSON syntax"
      diag_config_issues+=("BAD_JSON:paths.json")
    else
      ok "config/paths.json — valid JSON"
    fi
  fi
}

diagnose_docker() {
  step "Step 1c — Docker Health"
  printf "\n"

  if ! command -v docker &>/dev/null; then
    err "Docker not found"
    diag_docker_issues+=("NO_DOCKER")
    return
  fi
  if ! docker info &>/dev/null 2>&1; then
    err "Docker daemon not reachable"
    diag_docker_issues+=("DAEMON_DOWN")
    return
  fi

  # ── Container states ────────────────────────────────────────────────────────
  local compose_containers
  compose_containers=$(docker compose --file "$COMPOSE_FILE" --project-directory "$PROJECT_ROOT" \
    ps -a --format '{{.Name}}|{{.State}}|{{.Status}}' 2>/dev/null || echo "")

  if [[ -n "$compose_containers" ]]; then
    while IFS='|' read -r cname cstate cstatus; do
      [[ -z "$cname" ]] && continue
      case "$cstate" in
        running)
          ok "$cname — running ($cstatus)"
          ;;
        exited|dead|created)
          warn "$cname — $cstate ($cstatus)"
          diag_container_issues+=("$cname:$cstate")
          ;;
        restarting)
          err "$cname — restarting ($cstatus)"
          diag_container_issues+=("$cname:restarting")
          ;;
        *)
          warn "$cname — $cstate ($cstatus)"
          diag_container_issues+=("$cname:$cstate")
          ;;
      esac
    done <<< "$compose_containers"
  else
    info "No compose containers found (stack may not be running)"
  fi

  printf "\n"

  # ── Dangling images ─────────────────────────────────────────────────────────
  local dangling_count dangling_size
  dangling_count=$(docker images -f "dangling=true" -q 2>/dev/null | wc -l || echo 0)
  if [[ $dangling_count -gt 0 ]]; then
    dangling_size=$(docker images -f "dangling=true" --format '{{.Size}}' 2>/dev/null \
      | head -5 | tr '\n' ', ' || echo "unknown")
    warn "Dangling images: $dangling_count (sizes: ${dangling_size%, })"
    diag_docker_issues+=("DANGLING_IMAGES:$dangling_count")
  else
    ok "No dangling images"
  fi

  # ── Orphaned volumes ────────────────────────────────────────────────────────
  local orphaned_volumes
  orphaned_volumes=$(docker volume ls -f "dangling=true" --format '{{.Name}}' 2>/dev/null || echo "")
  if [[ -n "$orphaned_volumes" ]]; then
    local ov_count
    ov_count=$(echo "$orphaned_volumes" | wc -l)
    warn "Orphaned volumes: $ov_count"
    while IFS= read -r vol; do
      [[ -z "$vol" ]] && continue
      local vol_size
      vol_size=$(docker system df -v 2>/dev/null \
        | awk -v v="$vol" '$0 ~ v {print $3 " " $4}' | head -1 || echo "unknown size")
      info "  $vol ($vol_size)"
      diag_docker_issues+=("ORPHAN_VOLUME:$vol")
    done <<< "$orphaned_volumes"
  else
    ok "No orphaned volumes"
  fi

  # ── Build cache ─────────────────────────────────────────────────────────────
  local build_cache_reclaimable
  build_cache_reclaimable=$(docker system df 2>/dev/null \
    | awk '/Build Cache/ {print $NF}' | head -1 || echo "0B")
  if [[ "$build_cache_reclaimable" != "0B" && "$build_cache_reclaimable" != "0" ]]; then
    warn "Build cache reclaimable: $build_cache_reclaimable"
    diag_docker_issues+=("BUILD_CACHE:$build_cache_reclaimable")
  else
    ok "Build cache clean"
  fi
}

diagnose_disk() {
  step "Step 1d — Disk Usage"
  printf "\n"

  # Show per-tier usage
  local _tiers=("/mnt/storage" "/mnt/windows-sata" "/")
  local _labels=("nvme (storage)" "sata (windows)" "nvme (root)")

  for i in "${!_tiers[@]}"; do
    local mnt="${_tiers[$i]}"
    local label="${_labels[$i]}"
    if mountpoint -q "$mnt" 2>/dev/null || [[ "$mnt" == "/" ]]; then
      local used avail pct
      read -r used avail pct < <(df -h "$mnt" 2>/dev/null | awk 'NR==2 {print $3, $4, $5}' || echo "? ? ?")
      local pct_num="${pct%\%}"
      if [[ "$pct_num" =~ ^[0-9]+$ ]] && [[ $pct_num -ge 90 ]]; then
        err "$label: ${used} used, ${avail} free (${pct} full)"
        diag_disk_issues+=("CRITICAL:$mnt:$pct")
      elif [[ "$pct_num" =~ ^[0-9]+$ ]] && [[ $pct_num -ge 80 ]]; then
        warn "$label: ${used} used, ${avail} free (${pct} full)"
        diag_disk_issues+=("WARNING:$mnt:$pct")
      else
        ok "$label: ${used} used, ${avail} free (${pct} full)"
      fi
    else
      info "$label: $mnt not mounted"
    fi
  done
}

diagnose_hf_cache() {
  step "Step 1e — HuggingFace Cache Duplication"
  printf "\n"

  local home_hf="$HOME/.cache/huggingface/hub"
  local storage_hf="/mnt/windows-sata/oaio-hub/f5-tts/hf-cache/hub"

  if [[ ! -d "$home_hf" ]]; then
    ok "No home cache at $home_hf"
    return
  fi
  if [[ ! -d "$storage_hf" ]]; then
    info "No storage cache at $storage_hf — nothing to compare"
    return
  fi

  # Check if ~/.cache/huggingface is already a symlink
  if [[ -L "$HOME/.cache/huggingface" ]]; then
    ok "$HOME/.cache/huggingface is already a symlink"
    return
  fi

  local home_models=()
  local storage_models=()
  local dupes=()

  for d in "$home_hf"/models--*; do
    [[ -d "$d" ]] || continue
    home_models+=("$(basename "$d")")
  done
  for d in "$storage_hf"/models--*; do
    [[ -d "$d" ]] || continue
    storage_models+=("$(basename "$d")")
  done

  for hm in "${home_models[@]}"; do
    for sm in "${storage_models[@]}"; do
      if [[ "$hm" == "$sm" ]]; then
        dupes+=("$hm")
        break
      fi
    done
  done

  if [[ ${#dupes[@]} -gt 0 ]]; then
    warn "Duplicated model dirs found in both caches:"
    for d in "${dupes[@]}"; do
      local home_size storage_size
      home_size=$(du -sh "$home_hf/$d" 2>/dev/null | cut -f1 || echo "?")
      storage_size=$(du -sh "$storage_hf/$d" 2>/dev/null | cut -f1 || echo "?")
      info "  $d — home: $home_size, storage: $storage_size"
      diag_hf_dupes+=("$d")
    done
    local total_home_size
    total_home_size=$(du -sh "$HOME/.cache/huggingface" 2>/dev/null | cut -f1 || echo "?")
    warn "Total home HF cache: $total_home_size"
  else
    if [[ ${#home_models[@]} -gt 0 ]]; then
      warn "Home cache has ${#home_models[@]} model(s) not in storage cache"
      for hm in "${home_models[@]}"; do
        info "  $hm (home only)"
        diag_hf_dupes+=("HOME_ONLY:$hm")
      done
    else
      ok "No duplicated HuggingFace models"
    fi
  fi
}

diagnose_stray_models() {
  step "Step 1f — Stray Model Files"
  printf "\n"

  local search_dir="$HOME/Downloads"
  if [[ ! -d "$search_dir" ]]; then
    info "No ~/Downloads directory — skipping"
    return
  fi

  local found=0
  while IFS= read -r -d '' fpath; do
    local fsize fsize_h fname ext
    fsize=$(stat -c '%s' "$fpath" 2>/dev/null || echo 0)
    # Only flag files > 100MB
    if [[ $fsize -lt 104857600 ]]; then continue; fi
    fsize_h=$(human_size "$fsize")
    fname=$(basename "$fpath")
    ext="${fname##*.}"

    warn "Found: $fpath ($fsize_h)"
    diag_stray_models+=("$fpath|$fsize|$ext")
    (( found++ )) || true
  done < <(find "$search_dir" -maxdepth 3 -type f \
    \( -name "*.safetensors" -o -name "*.gguf" -o -name "*.pth" -o -name "*.ckpt" \) \
    -print0 2>/dev/null)

  if [[ $found -eq 0 ]]; then
    ok "No stray model files found in ~/Downloads"
  else
    warn "$found stray model file(s) found"
  fi
}

run_diagnosis() {
  banner "oAIo Repair Tool — Diagnosis"
  printf "\n"

  diagnose_symlinks
  diagnose_configs
  diagnose_docker
  diagnose_disk
  diagnose_hf_cache
  diagnose_stray_models

  # ── Report card ────────────────────────────────────────────────────────────
  step "Diagnosis Report Card"
  printf "\n"

  local total_issues=0
  local sym_n=${#diag_symlink_issues[@]}
  local cfg_n=${#diag_config_issues[@]}
  local dkr_n=${#diag_docker_issues[@]}
  local dsk_n=${#diag_disk_issues[@]}
  local hf_n=${#diag_hf_dupes[@]}
  local stray_n=${#diag_stray_models[@]}
  local ctr_n=${#diag_container_issues[@]}

  total_issues=$(( sym_n + cfg_n + dkr_n + dsk_n + hf_n + stray_n + ctr_n ))

  _report_line() {
    local label="$1" count="$2"
    if [[ $count -eq 0 ]]; then
      printf "  ${C_GREEN}✔${C_RESET} %-30s %s\n" "$label" "OK"
    else
      printf "  ${C_YELLOW}⚠${C_RESET}  %-30s %s issue(s)\n" "$label" "$count"
    fi
  }

  _report_line "Symlinks"               "$sym_n"
  _report_line "Config files"           "$cfg_n"
  _report_line "Docker (images/vols)"   "$dkr_n"
  _report_line "Disk usage"             "$dsk_n"
  _report_line "HuggingFace cache"      "$hf_n"
  _report_line "Stray model files"      "$stray_n"
  _report_line "Container health"       "$ctr_n"

  printf "\n"
  if [[ $total_issues -eq 0 ]]; then
    ok "All checks passed — system is healthy."
  else
    warn "$total_issues total issue(s) found."
  fi

  return $total_issues
}

# ═════════════════════════════════════════════════════════════════════════════
# Step 2 — Fix Symlinks
# ═════════════════════════════════════════════════════════════════════════════

fix_symlinks() {
  if [[ ${#diag_symlink_issues[@]} -eq 0 ]]; then return 0; fi

  step "Step 2 — Fix Symlinks"
  printf "\n"

  for issue in "${diag_symlink_issues[@]}"; do
    local issue_type key link_path target_path expected_target
    issue_type=$(echo "$issue" | cut -d: -f1)

    case "$issue_type" in
      MISSING_LINK)
        key=$(echo "$issue" | cut -d: -f2)
        link_path=$(echo "$issue" | cut -d: -f3)
        target_path=$(echo "$issue" | cut -d: -f4)

        warn "Missing symlink: $link_path -> $target_path"

        if [[ ! -d "$target_path" ]]; then
          info "Target directory does not exist: $target_path"
          if ask_yn "Create target directory $target_path?"; then
            mkdir -p "$target_path" 2>/dev/null || run_root mkdir -p "$target_path"
            ok "Created $target_path"
          else
            (( TOTAL_SKIPPED++ )) || true
            continue
          fi
        fi

        if ask_yn "Create symlink $link_path -> $target_path?"; then
          run_root ln -sfn "$target_path" "$link_path"
          ok "Created $link_path -> $target_path"
          (( TOTAL_FIXED++ )) || true
        else
          (( TOTAL_SKIPPED++ )) || true
        fi
        ;;

      DANGLING)
        key=$(echo "$issue" | cut -d: -f2)
        link_path=$(echo "$issue" | cut -d: -f3)
        target_path=$(echo "$issue" | cut -d: -f4)

        warn "Dangling symlink: $link_path -> $target_path (target missing)"

        if ask_yn "Create missing target directory $target_path?"; then
          mkdir -p "$target_path" 2>/dev/null || run_root mkdir -p "$target_path"
          ok "Created $target_path — symlink is now valid"
          (( TOTAL_FIXED++ )) || true
        else
          (( TOTAL_SKIPPED++ )) || true
        fi
        ;;

      WRONG_TARGET)
        key=$(echo "$issue" | cut -d: -f2)
        link_path=$(echo "$issue" | cut -d: -f3)
        local actual_target
        actual_target=$(echo "$issue" | cut -d: -f4)
        expected_target=$(echo "$issue" | cut -d: -f5)

        warn "Wrong target: $link_path -> $actual_target (expected: $expected_target)"

        if ask_yn "Update symlink to point to $expected_target?"; then
          if [[ ! -d "$expected_target" ]]; then
            mkdir -p "$expected_target" 2>/dev/null || run_root mkdir -p "$expected_target"
          fi
          run_root ln -sfn "$expected_target" "$link_path"
          ok "Updated $link_path -> $expected_target"
          (( TOTAL_FIXED++ )) || true
        else
          (( TOTAL_SKIPPED++ )) || true
        fi
        ;;

      NOT_SYMLINK)
        key=$(echo "$issue" | cut -d: -f2)
        link_path=$(echo "$issue" | cut -d: -f3)
        warn "$link_path exists but is not a symlink — manual intervention needed"
        (( TOTAL_WARNINGS++ )) || true
        ;;

      *)
        warn "Unknown symlink issue: $issue"
        (( TOTAL_SKIPPED++ )) || true
        ;;
    esac
  done

  # ── Offer full symlink re-run ──────────────────────────────────────────────
  local symlink_script="$PROJECT_ROOT/scripts/setup-oaio-symlinks.sh"
  if [[ -f "$symlink_script" ]]; then
    printf "\n"
    if ask_yn "Re-run scripts/setup-oaio-symlinks.sh to fix all symlinks at once?"; then
      run_root bash "$symlink_script"
      ok "Symlink setup script completed"
      (( TOTAL_FIXED++ )) || true
    else
      (( TOTAL_SKIPPED++ )) || true
    fi
  fi
}

# ═════════════════════════════════════════════════════════════════════════════
# Step 3 — Fix Configs
# ═════════════════════════════════════════════════════════════════════════════

fix_configs() {
  if [[ ${#diag_config_issues[@]} -eq 0 ]]; then return 0; fi

  step "Step 3 — Fix Configs"
  printf "\n"

  info "Config issues are reported below. Manual editing is recommended."
  info "Auto-fix is intentionally limited to prevent data corruption."
  printf "\n"

  for issue in "${diag_config_issues[@]}"; do
    local issue_type
    issue_type=$(echo "$issue" | cut -d: -f1)

    case "$issue_type" in
      MISSING_FILE)
        local filename
        filename=$(echo "$issue" | cut -d: -f2)
        err "Missing config file: config/$filename"
        info "Re-run install.sh or restore from backup."
        (( TOTAL_WARNINGS++ )) || true
        ;;

      BAD_JSON)
        local filename2
        filename2=$(echo "$issue" | cut -d: -f2)
        err "Invalid JSON: config/$filename2"
        info "Check syntax with: python3 -m json.tool config/$filename2"
        (( TOTAL_WARNINGS++ )) || true
        ;;

      MISSING_FIELD)
        local file3 svc3 field3
        file3=$(echo "$issue" | cut -d: -f2)
        svc3=$(echo "$issue" | cut -d: -f3)
        field3=$(echo "$issue" | cut -d: -f4)

        local defaults=""
        case "$field3" in
          container) defaults="(default: \"$svc3\")" ;;
          port)      defaults="(no sensible default — check docker-compose.yml)" ;;
          group)     defaults="(one of: oLLM, oAudio, Render)" ;;
        esac

        warn "$file3: service '$svc3' missing '$field3' $defaults"
        (( TOTAL_WARNINGS++ )) || true
        ;;

      XREF)
        local xfile xmode xsvc
        xfile=$(echo "$issue" | cut -d: -f2)
        xmode=$(echo "$issue" | cut -d: -f3)
        xsvc=$(echo "$issue" | cut -d: -f4)
        warn "$xfile: mode '$xmode' references '$xsvc' which is not in services.json"
        info "Either add '$xsvc' to services.json or remove from mode."
        (( TOTAL_WARNINGS++ )) || true
        ;;

      *)
        warn "Config issue: $issue"
        (( TOTAL_WARNINGS++ )) || true
        ;;
    esac
  done
}

# ═════════════════════════════════════════════════════════════════════════════
# Step 4 — Docker Cleanup
# ═════════════════════════════════════════════════════════════════════════════

docker_cleanup() {
  if [[ ${#diag_docker_issues[@]} -eq 0 ]]; then return 0; fi

  step "Step 4 — Docker Cleanup"
  printf "\n"

  local space_before space_after
  space_before=$(docker system df --format '{{.Reclaimable}}' 2>/dev/null \
    | tr '\n' ' ' || echo "unknown")
  info "Current reclaimable space: $space_before"
  printf "\n"

  # ── Dangling images ────────────────────────────────────────────────────────
  local has_dangling=0
  for issue in "${diag_docker_issues[@]}"; do
    [[ "$issue" == DANGLING_IMAGES:* ]] && has_dangling=1
  done

  if [[ $has_dangling -eq 1 ]]; then
    local dcount
    dcount=$(docker images -f "dangling=true" -q 2>/dev/null | wc -l || echo 0)
    warn "$dcount dangling image(s) found"

    # Show them
    docker images -f "dangling=true" --format '    {{.Repository}}:{{.Tag}}  {{.Size}}  ({{.ID}})' 2>/dev/null || true
    printf "\n"

    if ask_yn "Prune all dangling images?"; then
      local prune_out
      prune_out=$(docker image prune -f 2>/dev/null || echo "")
      local reclaimed
      reclaimed=$(echo "$prune_out" | grep -i "reclaimed" || echo "done")
      ok "Dangling images pruned — $reclaimed"
      (( TOTAL_FIXED++ )) || true
    else
      (( TOTAL_SKIPPED++ )) || true
    fi
  fi

  # ── Orphaned volumes ──────────────────────────────────────────────────────
  for issue in "${diag_docker_issues[@]}"; do
    if [[ "$issue" == ORPHAN_VOLUME:* ]]; then
      local vol_name="${issue#ORPHAN_VOLUME:}"
      local vol_detail
      vol_detail=$(docker volume inspect "$vol_name" --format '{{.Mountpoint}}' 2>/dev/null || echo "?")

      # Get approximate size
      local vol_size_h="unknown"
      if [[ "$vol_detail" != "?" && -d "$vol_detail" ]]; then
        vol_size_h=$(du -sh "$vol_detail" 2>/dev/null | cut -f1 || echo "unknown")
      fi

      warn "Orphaned volume: $vol_name (mountpoint: $vol_detail, size: $vol_size_h)"

      if ask_yn "Remove orphaned volume '$vol_name'?"; then
        docker volume rm "$vol_name" 2>/dev/null && \
          ok "Removed volume: $vol_name" || \
          err "Failed to remove volume: $vol_name"
        (( TOTAL_FIXED++ )) || true
      else
        (( TOTAL_SKIPPED++ )) || true
      fi
    fi
  done

  # ── Build cache ────────────────────────────────────────────────────────────
  local has_cache=0
  for issue in "${diag_docker_issues[@]}"; do
    [[ "$issue" == BUILD_CACHE:* ]] && has_cache=1
  done

  if [[ $has_cache -eq 1 ]]; then
    local cache_size
    cache_size=$(docker system df 2>/dev/null \
      | awk '/Build Cache/ {print $NF}' | head -1 || echo "?")
    warn "Build cache reclaimable: $cache_size"

    if ask_yn "Clear Docker build cache?"; then
      local cache_out
      cache_out=$(docker builder prune -f 2>/dev/null || echo "")
      local cache_reclaimed
      cache_reclaimed=$(echo "$cache_out" | grep -i "reclaimed" || echo "done")
      ok "Build cache cleared — $cache_reclaimed"
      (( TOTAL_FIXED++ )) || true
    else
      (( TOTAL_SKIPPED++ )) || true
    fi
  fi

  # ── Summary ────────────────────────────────────────────────────────────────
  printf "\n"
  space_after=$(docker system df --format '{{.Reclaimable}}' 2>/dev/null \
    | tr '\n' ' ' || echo "unknown")
  info "Reclaimable space after cleanup: $space_after"
}

# ═════════════════════════════════════════════════════════════════════════════
# Step 5 — HuggingFace Cache Dedup
# ═════════════════════════════════════════════════════════════════════════════

hf_cache_dedup() {
  if [[ ${#diag_hf_dupes[@]} -eq 0 ]]; then return 0; fi

  step "Step 5 — HuggingFace Cache Dedup"
  printf "\n"

  local home_hf_root="$HOME/.cache/huggingface"
  local storage_hf_root="/mnt/windows-sata/oaio-hub/f5-tts/hf-cache"
  local home_hub="$home_hf_root/hub"
  local storage_hub="$storage_hf_root/hub"

  # Check if already a symlink
  if [[ -L "$home_hf_root" ]]; then
    ok "$home_hf_root is already a symlink — nothing to do"
    return
  fi

  local has_real_dupes=0
  for d in "${diag_hf_dupes[@]}"; do
    [[ "$d" != HOME_ONLY:* ]] && has_real_dupes=1
  done

  if [[ $has_real_dupes -eq 1 ]]; then
    info "The following models exist in BOTH caches:"
    for d in "${diag_hf_dupes[@]}"; do
      [[ "$d" == HOME_ONLY:* ]] && continue
      local home_sz storage_sz
      home_sz=$(du -sh "$home_hub/$d" 2>/dev/null | cut -f1 || echo "?")
      storage_sz=$(du -sh "$storage_hub/$d" 2>/dev/null | cut -f1 || echo "?")
      info "  $d — home: $home_sz / storage: $storage_sz"
    done
    printf "\n"
  fi

  local total_home_sz
  total_home_sz=$(du -sh "$home_hf_root" 2>/dev/null | cut -f1 || echo "?")
  info "Total home HF cache: $total_home_sz"
  info "Containers use HF_HOME=/mnt/oaio/hf-cache -> $storage_hf_root"
  printf "\n"

  if ask_yn "Remove $home_hf_root and replace with symlink to $storage_hf_root?"; then
    # Safety: move any home-only models to storage first
    for d in "${diag_hf_dupes[@]}"; do
      if [[ "$d" == HOME_ONLY:* ]]; then
        local model_name="${d#HOME_ONLY:}"
        if [[ -d "$home_hub/$model_name" ]]; then
          info "Moving home-only model to storage: $model_name"
          mkdir -p "$storage_hub" 2>/dev/null || true
          cp -a "$home_hub/$model_name" "$storage_hub/" 2>/dev/null || \
            run_root cp -a "$home_hub/$model_name" "$storage_hub/"
          ok "Moved $model_name to storage"
        fi
      fi
    done

    # Remove home cache and create symlink
    rm -rf "$home_hf_root"
    ln -sfn "$storage_hf_root" "$home_hf_root"
    ok "Replaced $home_hf_root with symlink to $storage_hf_root"
    ok "Reclaimed: $total_home_sz"
    (( TOTAL_FIXED++ )) || true
  else
    (( TOTAL_SKIPPED++ )) || true
  fi
}

# ═════════════════════════════════════════════════════════════════════════════
# Step 6 — Stray Model Files
# ═════════════════════════════════════════════════════════════════════════════

stray_model_cleanup() {
  if [[ ${#diag_stray_models[@]} -eq 0 ]]; then return 0; fi

  step "Step 6 — Stray Model Files"
  printf "\n"

  for entry in "${diag_stray_models[@]}"; do
    local fpath fsize ext fsize_h fname
    fpath=$(echo "$entry" | cut -d'|' -f1)
    fsize=$(echo "$entry" | cut -d'|' -f2)
    ext=$(echo "$entry" | cut -d'|' -f3)
    fsize_h=$(human_size "$fsize")
    fname=$(basename "$fpath")

    local dest_dir="" dest_label=""

    case "$ext" in
      safetensors|ckpt)
        dest_dir="/mnt/windows-sata/oaio-hub/comfyui/models/checkpoints"
        dest_label="ComfyUI checkpoints"
        ;;
      pth)
        dest_dir="/mnt/windows-sata/oaio-hub/rvc/weights"
        dest_label="RVC weights"
        ;;
      gguf)
        warn "  $fname ($fsize_h) — GGUF is an Ollama model format"
        info "  Use: ollama cp <model> to import instead of moving the file"
        info "  File: $fpath"
        (( TOTAL_SKIPPED++ )) || true
        continue
        ;;
    esac

    warn "  $fname ($fsize_h)"
    info "  Source:      $fpath"
    info "  Destination: $dest_dir/ ($dest_label)"

    if ask_yn "Move $fname to $dest_dir/?"; then
      mkdir -p "$dest_dir" 2>/dev/null || true
      if [[ -e "$dest_dir/$fname" ]]; then
        warn "File already exists at destination — skipping to avoid overwrite"
        (( TOTAL_SKIPPED++ )) || true
      else
        mv "$fpath" "$dest_dir/$fname"
        ok "Moved $fname to $dest_dir/"
        (( TOTAL_FIXED++ )) || true
      fi
    else
      (( TOTAL_SKIPPED++ )) || true
    fi
  done
}

# ═════════════════════════════════════════════════════════════════════════════
# Step 7 — Container Health
# ═════════════════════════════════════════════════════════════════════════════

fix_containers() {
  if [[ ${#diag_container_issues[@]} -eq 0 ]]; then return 0; fi

  step "Step 7 — Container Health"
  printf "\n"

  for entry in "${diag_container_issues[@]}"; do
    local cname cstate
    cname=$(echo "$entry" | cut -d: -f1)
    cstate=$(echo "$entry" | cut -d: -f2)

    warn "Container '$cname' is in state: $cstate"
    printf "\n"
    info "Last 10 log lines:"
    printf "  ${C_DIM}─────────────────────────────────────────${C_RESET}\n"
    docker logs --tail 10 "$cname" 2>&1 | while IFS= read -r line; do
      printf "  ${C_DIM}%s${C_RESET}\n" "$line"
    done
    printf "  ${C_DIM}─────────────────────────────────────────${C_RESET}\n"
    printf "\n"

    if ask_yn "Restart container '$cname'?"; then
      docker restart "$cname" 2>/dev/null && \
        ok "Restarted $cname" || \
        err "Failed to restart $cname"
      (( TOTAL_FIXED++ )) || true
    else
      (( TOTAL_SKIPPED++ )) || true
    fi
  done
}

# ═════════════════════════════════════════════════════════════════════════════
# Step 8 — Summary
# ═════════════════════════════════════════════════════════════════════════════

print_summary() {
  step "Summary"
  printf "\n"

  printf "  ${C_BOLD}${C_GREEN}Fixed:${C_RESET}    %d\n" "$TOTAL_FIXED"
  printf "  ${C_BOLD}${C_YELLOW}Warnings:${C_RESET} %d\n" "$TOTAL_WARNINGS"
  printf "  ${C_BOLD}${C_DIM}Skipped:${C_RESET}  %d\n" "$TOTAL_SKIPPED"
  printf "\n"

  if [[ $TOTAL_WARNINGS -eq 0 && $TOTAL_FIXED -eq 0 && $TOTAL_SKIPPED -eq 0 ]]; then
    printf "  ${C_BOLD}${C_GREEN}══════════════════════════════════════════${C_RESET}\n"
    printf "  ${C_BOLD}${C_GREEN}  System is healthy — nothing to repair.${C_RESET}\n"
    printf "  ${C_BOLD}${C_GREEN}══════════════════════════════════════════${C_RESET}\n"
  elif [[ $TOTAL_WARNINGS -gt 0 ]]; then
    printf "  ${C_BOLD}${C_YELLOW}Some issues require manual attention.${C_RESET}\n"
  else
    printf "  ${C_BOLD}${C_GREEN}Repair complete.${C_RESET}\n"
  fi
  printf "\n"
}

# ═════════════════════════════════════════════════════════════════════════════
# Main
# ═════════════════════════════════════════════════════════════════════════════

main() {
  banner "oAIo Repair Tool"
  dim   "  Project : $PROJECT_ROOT"
  dim   "  User    : $(whoami)  |  Host: $(hostname)"
  if [[ $CHECK_ONLY -eq 1 ]]; then
    dim   "  Mode    : --check (report only, no prompts)"
  fi
  printf "\n"

  # ── Phase 1: Diagnose everything ───────────────────────────────────────────
  run_diagnosis
  local diag_result=$?

  if [[ $CHECK_ONLY -eq 1 ]]; then
    printf "\n"
    if [[ $diag_result -eq 0 ]]; then
      ok "All checks passed."
    else
      info "Run without --check to interactively fix issues."
    fi
    return 0
  fi

  if [[ $diag_result -eq 0 ]]; then
    print_summary
    return 0
  fi

  # ── Phase 2: Offer fixes ───────────────────────────────────────────────────
  printf "\n"
  if ! ask_yn "Proceed with interactive repairs?" "Y"; then
    info "Exiting without changes."
    return 0
  fi

  fix_symlinks
  fix_configs
  docker_cleanup
  hf_cache_dedup
  stray_model_cleanup
  fix_containers
  print_summary
}

main "$@"
