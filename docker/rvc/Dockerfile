FROM rvc AS base

# Image ships scipy 1.15.3 (compiled for numpy 2.x) but numpy 1.23.5.
# Fix: upgrade numpy to 2.x and rebuild broken extensions (faiss, numba).
RUN pip install --upgrade pip -q && \
    pip install 'numpy>=2.1,<2.3' 'faiss-cpu>=1.8' 'numba>=0.60' \
    --force-reinstall --no-cache-dir -q && \
    pip install 'gradio_client==0.2.7' --no-cache-dir -q
