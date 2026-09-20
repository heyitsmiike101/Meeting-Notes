# The transcription server: receives the live preview stream and the final
# upload from the recorder client, runs faster-whisper (CPU-only) against
# them. See meeting_notes/server/ for the application itself.
FROM python:3.11-slim

# Faster-whisper's runtime (ctranslate2) links libgomp; curl is here only for
# the HEALTHCHECK below.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy just the build metadata first so `pip install` is cached across
# rebuilds that only touch application code.
COPY pyproject.toml README.md ./
COPY meeting_notes ./meeting_notes

RUN pip install --no-cache-dir ".[server]"

# /data is the store root (see meeting_notes/server/store.py); a session's
# audio, timing logs and transcripts all live under it. Declared as a volume
# so `docker run -v` / compose can persist it outside the container.
RUN mkdir -p /data
VOLUME ["/data"]

# --- Model preloading (NOT done here) ---------------------------------
# MEETING_NOTES_MODEL selects the faster-whisper model the server loads at
# startup (see meeting_notes/server/app.py:_env_transcriber_factory). You can
# bake a model into the image at build time so the container never needs
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

ENV MEETING_NOTES_DATA=/data \
    MEETING_NOTES_DEVICE=cpu \
    PYTHONUNBUFFERED=1

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

CMD ["uvicorn", "meeting_notes.server.app:app", "--host", "0.0.0.0", "--port", "8000"]
