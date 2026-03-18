FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
    torch --index-url https://download.pytorch.org/whl/cpu

RUN pip install --no-cache-dir \
    transformers accelerate Pillow \
    fastapi uvicorn[standard] python-multipart

# Download model at build time
RUN python -c "from transformers import AutoModelForCausalLM, AutoProcessor; \
    AutoProcessor.from_pretrained('microsoft/Florence-2-base', trust_remote_code=True); \
    AutoModelForCausalLM.from_pretrained('microsoft/Florence-2-base', trust_remote_code=True)"

COPY docker/florence-2/server.py /app/server.py

EXPOSE 8010

HEALTHCHECK --interval=60s --timeout=10s --retries=3 \
    CMD curl -f http://localhost:8010/health || exit 1

WORKDIR /app
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8010"]
