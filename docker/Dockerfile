# The transcription server: receives the live preview stream and the final
# upload from the recorder client, runs faster-whisper (CPU-only) against
# them. See meeting_notes/server/ for the application itself.
FROM python:3.11-slim

ARG INSTALL_DIARIZATION=false

# Faster-whisper's runtime (ctranslate2) links libgomp; curl is here only for
# the HEALTHCHECK below.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 curl \
    && if [ "$INSTALL_DIARIZATION" = "true" ]; then \
         apt-get install -y --no-install-recommends ffmpeg; \
       fi \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy just the build metadata first so `pip install` is cached across
# rebuilds that only touch application code.
COPY pyproject.toml README.md ./
COPY meeting_notes ./meeting_notes

RUN pip install --no-cache-dir ".[server]"

# Speaker diarization pulls in PyTorch and is intentionally opt-in. Building
# with --build-arg INSTALL_DIARIZATION=true produces the larger image; the
# ordinary recorder/transcription server stays lean.
RUN if [ "$INSTALL_DIARIZATION" = "true" ]; then \
      pip install --no-cache-dir ".[diarization]"; \
    fi

# /data is the store root (see meeting_notes/server/store.py); a session's
# audio, timing logs and transcripts all live under it. Declared as a volume
# so `docker run -v` / compose can persist it outside the container.
RUN mkdir -p /data
VOLUME ["/data"]

# --- Model preloading (NOT done here) ---------------------------------
# MEETING_NOTES_MODEL selects the faster-whisper model the server bootstraps
# with (see meeting_notes/server/settings.py and app.py's
# _settings_transcriber_factory) -- until an operator saves different
# settings through the web UI or PUT /v1/settings, at which point
# <MEETING_NOTES_DATA>/settings.json takes over and this env var is only
# read again if that file is ever deleted. You can bake a model into the
# image at build time so the container never needs
# network access to Hugging Face at runtime, by adding, after the pip
# install above:
#
#   ARG PRELOAD_MODEL=""
#   RUN if [ -n "$PRELOAD_MODEL" ]; then \
#         python -c "from meeting_notes.transcribe.faster_whisper_backend import download_model; \
#                    download_model('$PRELOAD_MODEL')"; \
#       fi
#
# and building with `--build-arg PRELOAD_MODEL=base.en`. This repo does NOT
# do that here: it requires contacting huggingface.co, which this build
# environment cannot reach, and would break the build. Preload it yourself
# in an environment that has network access, or mount a pre-populated
# HF cache / MEETING_NOTES_MODEL_DIR at runtime instead.

# HF_HOME is where faster-whisper (via huggingface_hub) caches downloaded
# model weights. Its default, /root/.cache/huggingface, lives in the
# container's writable layer -- so every `docker compose up --build` (or any
# recreate) threw the ~145 MB+ model away and re-downloaded it on the next
# job. Pointing it at its own volume (see docker-compose.yml) makes the
# download a one-time cost per host, and lets the server start with no
# network at all once a model is cached.
ENV MEETING_NOTES_DATA=/data \
    MEETING_NOTES_DEVICE=cpu \
    HF_HOME=/models \
    PYTHONUNBUFFERED=1

RUN mkdir -p /models
VOLUME ["/models"]

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

CMD ["uvicorn", "meeting_notes.server.app:app", "--host", "0.0.0.0", "--port", "8000"]
