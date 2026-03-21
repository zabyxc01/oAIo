# SYS-PANDORA-OAO — System Specifications

> Live snapshot taken 2026-03-21. This is the machine the Alpha is built on and for.

---

## Hardware

| Component | Spec |
|-----------|------|
| **CPU** | AMD Ryzen 9 3900X — 12 cores / 24 threads, 3.8 GHz base, 4.67 GHz boost |
| **RAM** | 62 GB DDR4 |
| **GPU** | AMD Radeon RX 7900 XT — 20 GB VRAM (Navi 31, gfx1100, RDNA3) |
| **Motherboard** | B550 Vision D (Gigabyte) |
| **Network** | enp80s0 (Ethernet, 1Gbps) — 192.168.50.189 |
| **Monitor** | Dell AW3425DW — 3440x1440 ultrawide on DP-3 |

---

## Storage

| Device | Type | Mount | Size | Used | Speed | Purpose |
|--------|------|-------|------|------|-------|---------|
| nvme0n1p1 | m.2 SATA (SATA III on M.2 slot) | `/` | 606 GB | ~83% | ~550 MB/s | OS, Steam, home, Docker |
| nvme0n1p2 | m.2 SATA (same drive) | `/boot/efi` | 512 MB | — | — | EFI boot |
| nvme0n1p3 | m.2 SATA (same drive) | `/mnt/storage` | 324 GB | ~57% | ~550 MB/s | Staging, I/O, output buffers |
| sda1 | Motherboard SATA (standard SATA to board) | `/mnt/windows-sata` | 223 GB | ~61% | ~550 MB/s | oAIo project, ollama models, oaio-hub |
| sdb1 | USB 3.0 HDD | `/media/oao/My Passport` | 2.6 TB | — | ~100 MB/s | Archives, backups |
| zram0 | Compressed RAM | [SWAP] | 15.7 GB | 6.2 GB | — | Swap (compressed) |

**No NVMe in the system.** Both SSDs are SATA — one in M.2 form factor, one on board connector. Same speed ceiling (~550 MB/s).

### Symlink Bus

`/mnt/oaio/` — 20+ symlinks routing all Docker volumes. Zero hardcoded paths in compose.

---

## Software

| Component | Version |
|-----------|---------|
| **OS** | Ubuntu 24.04 LTS |
| **Kernel** | 6.17.0-19-generic |
| **Desktop** | KDE Plasma 5.27.12 on X11 (NOT Wayland), KWin compositor |
| **Mesa** | 25.2.8 |
| **Vulkan Driver** | RADV (Mesa) — ONLY driver. No AMDVLK, no ROCm on host |
| **Docker** | 29.3.0 |
| **Docker Compose** | v5.1.0 |
| **PipeWire** | 1.0.5 + RNNoise noise suppression (mono, default mic source) |
| **Godot** | 4.6.1 stable at `~/.local/bin/godot` |
| **Python** | 3 (always `python3`, prefer Docker over system pip) |

---

## GPU Details

| Property | Value |
|----------|-------|
| Product | Navi 31 (Radeon RX 7900 XT/XTX/M) |
| Architecture | RDNA3, gfx1100 |
| VRAM Total | 20 GB (21,458,059,264 bytes) |
| VRAM sysfs | `/sys/class/drm/card1/device/mem_info_vram_used\|total\|gpu_busy_percent` |
| ROCm | Docker containers ONLY via `/dev/kfd` + `/dev/dri` passthrough |
| Host driver | RADV (Mesa Vulkan). No rocm-smi on host. |

---

## Network

| Interface | State | Address | Purpose |
|-----------|-------|---------|---------|
| enp80s0 | UP | 192.168.50.189/24 | LAN (Ethernet) |
| tailscale0 | UP | 100.117.188.118/32 | Tailscale VPN |
| br-82551d4dbcac | UP | 172.18.0.1/16 | Docker bridge (oaio-net) |
| enp81s0 | DOWN | — | Unused NIC |
| wlp82s0 | DOWN | — | WiFi (unused) |

### Tailscale Mesh

| Device | Address | OS | Status |
|--------|---------|-----|--------|
| **oao-b550-vision-d** | 100.117.188.118 | Linux | **Online** (this machine) |
| google-pixel-10 | 100.98.213.113 | Android | Offline (last seen 2d ago) |
| sys-pandora-oao | 100.99.194.124 | Linux | Offline (last seen 8d ago) |
| pandora-1 | 100.113.53.1 | Windows | Offline (last seen 121d ago) |
| pandoraxx | 100.114.203.123 | Linux | Offline (last seen 107d ago) |

---

## Docker Containers (14 services)

### Currently Running

| Container | Status | Ports |
|-----------|--------|-------|
| oaio | Up 7h (healthy) | 127.0.0.1:9000, :8002 |
| ollama | Up 6h | 127.0.0.1:11434 |
| open-webui | Up 7h (healthy) | 127.0.0.1:3000 |
| kokoro-tts | Up 25h | 127.0.0.1:8000 |
| rvc | Up 7h | 127.0.0.1:7865, :8001 |
| faster-whisper | Up 2d (healthy) | 127.0.0.1:7880, :8003 |
| comfyui | Up 4h | 127.0.0.1:8188 |
| searxng | Up 12h | 127.0.0.1:8888 |

### Currently Stopped

| Container | Status | Notes |
|-----------|--------|-------|
| f5-tts | Exited (137) 3d ago | OOM killed (signal 9) |
| styletts2 | Exited (137) 3d ago | OOM killed (signal 9) |
| indextts | Exited (0) 3d ago | Clean exit |
| momask | Not created | Docker build incomplete |
| florence-2 | Not created | Not yet built |
| letta | Not created | Not yet configured |

### Service Binding

All services bind to `127.0.0.1` (localhost only). For Tailscale remote access, change `OAIO_BIND=0.0.0.0` in `.env` + add auth token.

---

## Ollama Models (loaded)

| Model | Size |
|-------|------|
| qwen2.5:7b | 4.7 GB |
| llava:7b | 4.7 GB |
| gemma3:latest | 3.3 GB |

**Default model:** qwen2.5:7b (set 2026-03-20)

---

## Memory State (at snapshot)

```
Total:     62 GB
Used:      37 GB
Free:       1.5 GB
Buff/Cache: 31 GB
Available:  24 GB
Swap:      15.7 GB total, 6.2 GB used
```

---

## Philosophy

**Gaming is the host. Compute lives in containers. Nothing crosses the boundary.**

- RADV (Mesa) is the ONLY Vulkan driver
- ROCm ONLY inside Docker containers
- No system pip installs — Docker for everything
- Enforcement loop pauses when no mode active — safe for gaming
- All volumes through `/mnt/oaio/*` — zero hardcoded personal paths
