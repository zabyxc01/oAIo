#!/bin/bash
set -e

echo "[oAIo] Starting oLLMo API + frontend on :9000"
python3 -m uvicorn backend.ollmo.main:app --host 0.0.0.0 --port 9000 &

echo "[oAIo] Starting oAudio API on :8002"
python3 -m uvicorn backend.oaudio.main:app --host 0.0.0.0 --port 8002 &

# Wait for either process to exit (if one crashes, container stops and restarts)
wait -n
echo "[oAIo] A service exited — container will restart"
exit 1
