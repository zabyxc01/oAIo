#!/usr/bin/env bash
# oAIo symlink layer setup — run once as root: sudo bash scripts/setup-oaio-symlinks.sh
# Source of truth: config/paths.json
set -euo pipefail

OAIO=/mnt/oaio
COUNT=0

echo "==> Creating /mnt/oaio mount point"
mkdir -p "$OAIO"
mkdir -p "$OAIO/staging"

echo "==> Creating symlinks"

create_link() {
  local name="$1" target="$2"
  local link="$OAIO/$name"
  # Ensure target directory exists so Docker mounts don't fail on dangling symlinks
  if [[ ! "$target" =~ \. ]] && [ ! -e "$target" ]; then
    mkdir -p "$target"
    echo "  CREATED: $target"
  fi
  ln -sfn "$target" "$link"
  echo "  OK: $link -> $(readlink "$link")"
  COUNT=$((COUNT + 1))
}

# All targets match config/paths.json defaults
create_link ollama           /mnt/windows-sata/ollama
create_link models           /mnt/windows-sata/oaio-hub/comfyui/models
create_link custom-nodes     /mnt/windows-sata/oaio-hub/comfyui/custom_nodes
create_link comfyui-user     /mnt/windows-sata/oaio-hub/comfyui/user
create_link outputs          /mnt/windows-sata/oaio-hub/comfyui/output
create_link inputs           /mnt/windows-sata/oaio-hub/comfyui/input
create_link kokoro-voices    /mnt/windows-sata/oaio-hub/kokoro/models
create_link hf-cache         /mnt/windows-sata/oaio-hub/f5-tts/hf-cache
create_link ref-audio        /mnt/windows-sata/oaio-hub/f5-tts/ref-audio
create_link rvc-ref          /mnt/storage/staging/rvc-ref
create_link rvc-weights      /mnt/windows-sata/oaio-hub/rvc/weights
create_link rvc-indices      /mnt/windows-sata/oaio-hub/rvc/indices
create_link swap             /mnt/windows-sata/oaio-hub/_staging
create_link workflows        /mnt/windows-sata/oaio-hub/comfyui/workflows

# Staging outputs (NVMe fast tier)
create_link staging          /mnt/storage/staging
create_link staging/rvc-output      /mnt/storage/staging/rvc-output
create_link staging/f5-output       /mnt/storage/staging/f5-output
create_link staging/styletts2-output /mnt/storage/staging/styletts2-output

echo ""
echo "==> Verification:"
ls -la "$OAIO"
echo ""
echo "Summary: $COUNT symlinks created/updated under $OAIO"
echo "Done. Restart oaio container: docker compose -f /mnt/windows-sata/oAIo/docker-compose.yml restart oaio"
