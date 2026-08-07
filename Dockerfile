# KESTREL API — deployable image.
#
# Deliberately runs in replay mode by default: the image works with no credentials,
# serving recorded model responses from committed cassettes. Set KESTREL_MODE=live
# and supply NVIDIA_API_KEY / GROQ_API_KEY to use real models.
#
# CPU-only. Local detection needs a GPU and is disabled here; the detector falls
# back automatically and reports itself as degraded rather than pretending.

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    KESTREL_MODE=replay \
    KESTREL_LOCAL_DETECTOR=false \
    HF_HUB_OFFLINE=1 \
    KESTREL_API_HOST=0.0.0.0 \
    KESTREL_API_PORT=8000

# OpenCV needs these even in the headless build.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libglib2.0-0 libsm6 libxext6 libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Dependency layer first, so source edits do not invalidate the install.
COPY pyproject.toml README.md ./
COPY src ./src
RUN uv pip install --system --no-cache -e .

# Cassettes and site definitions ship with the image — they are what make it run
# without credentials.
COPY data/cassettes ./data/cassettes
COPY data/sites ./data/sites
COPY data/scenarios ./data/scenarios
COPY data/eval ./data/eval

# The console is the demo, and without these three it renders an empty player:
#
#   footage   the clips the <video> element actually streams
#   playback  the dense index the overlay draws from, which is where the boxes,
#             track ids and gate verdicts live. It is prebuilt, which is why the
#             deployed image needs no GPU: detection already happened offline.
#   seed      a database built by `kestrel ingest` and committed, because
#             data/kestrel.db is gitignored and a build cannot regenerate it
#             without a detector this image deliberately does not carry.
COPY data/footage ./data/footage
COPY data/frames ./data/frames
COPY data/playback ./data/playback
COPY data/seed/kestrel.db ./data/kestrel.db

# No apt ffmpeg here on purpose. Uploaded clips are transcoded to browser-safe
# H.264 on ingest, and the `imageio-ffmpeg` base dependency already ships a
# binary that kestrel.media prefers over anything on PATH, so installing the
# system package would add roughly 200 MB to the image for a second copy.

# Uploads are written at runtime. On a host with no persistent disk this is
# ephemeral and resets on redeploy, which is acceptable for a demo and stated
# rather than hidden.
RUN mkdir -p data/uploads

EXPOSE 8000 10000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os,httpx,sys; p=os.environ.get('PORT','8000'); sys.exit(0 if httpx.get(f'http://127.0.0.1:{p}/api/health',timeout=4).status_code==200 else 1)"

CMD ["sh", "-c", "uvicorn kestrel.api.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
