#!/usr/bin/env bash
# oAIo symlink layer setup — run once as root: sudo bash scripts/setup-oaio-symlinks.sh
set -euo pipefail

OAIO=/mnt/oaio
COUNT=0

echo "==> Creating /mnt/oaio mount point"
mkdir -p "$OAIO"

echo "==> Ensuring training dir exists"
mkdir -p /mnt/storage/ai/training

echo "==> Creating symlinks"

create_link() {
  local name="$1" target="$2"
  local link="$OAIO/$name"
  ln -sfn "$target" "$link"
  echo "  OK: $link -> $(readlink "$link")"
  COUNT=$((COUNT + 1))
}

create_link ollama        /mnt/windows-sata/ollama-models
create_link models        /mnt/storage/ai/comfyui/models
create_link lora          /mnt/storage/ai/comfyui/models/loras
create_link custom-nodes  /home/oao/ComfyUI/custom_nodes
create_link comfyui-user  /home/oao/ComfyUI/user
create_link outputs       /home/oao/ComfyUI/output
create_link inputs        /home/oao/ComfyUI/input
create_link audio         /mnt/storage/ai/audio
create_link kokoro-voices /mnt/storage/ai/audio/kokoro-voices
create_link hf-cache      /mnt/storage/ai/audio/huggingface
create_link ref-audio     /home/oao/reference-audio
create_link rvc-ref       /home/oao/Videos/audio/_EDITED
create_link swap          /mnt/storage/swap
create_link training      /mnt/storage/ai/training
create_link rvc-weights   /mnt/storage/ai/audio/rvc-weights
create_link rvc-indices   /mnt/storage/ai/audio/rvc-indices

echo ""
echo "==> Verification:"
ls -la "$OAIO"
echo ""
echo "Summary: $COUNT/16 symlinks created/updated under $OAIO"
echo "Done. Restart oaio container: docker compose -f /mnt/storage/oAIo/docker-compose.yml restart oaio"
