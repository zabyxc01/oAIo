# System / BIOS Optimization Research

> Pre-Alpha legwork. 2026-03-21. Parked — research when free afternoon available.

---

## Current State
- Kernel: 6.17.0-19-generic (bleeding edge, optimal)
- Mesa: 25.2.8 RADV (latest stable, optimal)
- Vulkan: 1.4.318 via RADV (optimal)
- Docker: 29.3.0 (current)
- Motherboard: Gigabyte B550 Vision D
- CPU: Ryzen 9 3900X (Zen 2, PCIe 4.0)
- RAM: 62GB DDR4 (speed unknown — check XMP)
- GPU: RX 7900 XT 20GB (PCIe 4.0 x16)

## BIOS Settings to Research

### XMP / DOCP — HIGH PRIORITY
- DDR4 defaults to 2133MHz without XMP profile enabled
- If 62GB is running at 2133 instead of rated (3200/3600), losing 30-50% memory bandwidth
- User saw "red number" next to XMP setting — likely overclock warning, normal for XMP
- Red number is expected: XMP IS overclocking, the BIOS warns about it. It's AMD-validated.
- **Check:** `sudo dmidecode -t memory | grep -E "Speed|Configured"`
- **Impact:** Everything — Docker builds, LLM inference, RAM tier, general responsiveness

### Resizable BAR / SAM — PARKED
- Enabled previously on Windows, caused stability issues
- Not currently enabled on Linux (dmesg shows nothing)
- Requires: Above 4G Decoding ON + Resizable BAR ON in BIOS
- **Impact:** Faster VRAM access from CPU side, faster model loads from RAM tier
- **Risk:** Stability issues seen on Windows. May need BIOS update for better support.
- **Test plan:** Enable in BIOS → boot Linux → run `sudo dmesg | grep rebar` → test ollama model load speed → run games → if unstable, revert

### PBO (Precision Boost Overdrive) — LOW PRIORITY
- AMD auto-overclock within thermal/power limits
- 5-15% more sustained boost on 3900X under multi-threaded loads
- Safe — stays within AMD spec, just extends boost duration
- **Check:** Look in BIOS under AMD Overclocking → PBO
- **Impact:** Faster Docker builds, faster Python execution, faster LLM prompt processing (CPU portion)

### SVM (AMD-V) — VERIFY
- Hardware virtualization for Docker/KVM
- User ran Hyper-V on Windows, so SVM should be enabled in BIOS
- If somehow off, Docker runs in slower emulation mode
- **Check:** `grep -c svm /proc/cpuinfo` (should be > 0)

### IOMMU — LOW PRIORITY
- Device passthrough for VMs
- Not needed for current Docker setup (Docker uses cgroups, not IOMMU)
- Would matter if ever running GPU passthrough to a VM
- Leave as-is

### PCIe Gen — VERIFY
- B550 + Ryzen 3900X supports PCIe 4.0 on primary GPU slot
- RX 7900 XT is PCIe 4.0 x16
- If BIOS set to "Auto" it should negotiate Gen 4. If forced to Gen 3, losing half bandwidth.
- **Check:** `sudo lspci -vv -s 55:00.0 | grep -i "lnksta\|speed\|width"`

### C-States / Cool'n'Quiet — LEAVE ON
- CPU power saving in idle
- Ryzen handles boost/idle transitions correctly
- Saves power during gaming when AI stack is paused
- No performance cost — boost still reaches 4.67GHz when needed

## Software Updates (pending, safe)
```bash
sudo apt update && sudo apt upgrade -y
```
- Docker compose 5.1.0 → 5.1.1 (minor)
- VS Code 1.110 → 1.112 (minor)
- binutils, coreutils, gdb — security patches
- GTK, gnome-keyring — desktop stability
- No Mesa/kernel/RADV changes in queue

## Quick Checks to Run
```bash
# RAM speed (is XMP working?)
sudo dmidecode -t memory | grep -E "Speed|Configured"

# SVM enabled?
grep -c svm /proc/cpuinfo

# PCIe negotiated speed?
sudo lspci -vv -s 55:00.0 | grep -i "lnksta"

# Resizable BAR?
sudo dmesg | grep -i rebar
```
