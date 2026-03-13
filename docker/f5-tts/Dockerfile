FROM f5-tts AS base

# Image ships scipy 1.15.3 (compiled for numpy 2.x) but numpy 1.26.4.
# Fix: upgrade numpy to 2.x to match scipy's compiled extensions.
# Also remove torchcodec which causes import errors.
RUN pip install --upgrade pip -q && \
    pip install 'numpy>=2.1,<2.3' --force-reinstall --no-cache-dir -q && \
    pip uninstall torchcodec -y 2>/dev/null || true
