#!/usr/bin/env bash
# oAIo symlink layer setup — run once as root: sudo bash scripts/setup-oaio-symlinks.sh
set -euo pipefail

OAIO=/mnt/oaio

echo "==> Creating /mnt/oaio mount point"
mkdir -p "$OAIO"

echo "==> Ensuring training dir exists"
mkdir -p /mnt/storage/ai/training

echo "==> Creating symlinks"

create_link() {
  local name="$1" target="$2"
  local link="$OAIO/$name"
  if [ -L "$link" ]; then
    echo "  SKIP (exists): $link -> $(readlink "$link")"
  elif [ -e "$link" ]; then
    echo "  ERROR: $link exists and is not a symlink — remove it manually"
    return 1
  else
    ln -s "$target" "$link"
    echo "  OK: $link -> $target"
  fi
}

create_link models    /mnt/storage/ai/comfyui/models
create_link lora      /mnt/storage/ai/comfyui/models/loras
create_link audio     /mnt/storage/ai/audio
create_link hf-cache  /mnt/storage/ai/audio/huggingface
create_link ref-audio /home/oao/reference-audio
create_link outputs   /home/oao/ComfyUI/output
create_link ollama    /mnt/windows-sata/ollama-models
create_link training  /mnt/storage/ai/training

echo ""
echo "==> Verification:"
ls -la "$OAIO"
echo ""
echo "Done. Restart oaio container: docker compose -f /mnt/storage/oAIo/docker-compose.yml restart oaio"
