# --- Stage 1: build the React frontend -------------------------------------
FROM node:20-alpine AS ui
WORKDIR /ui
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-fund --no-audit
COPY frontend/ ./
RUN npm run build

# --- Stage 2: Python runtime (serves API + built UI) ------------------------
FROM python:3.12-slim
WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt "uvicorn>=0.30"

COPY backend/ backend/
COPY data/ data/
COPY scripts/fetch_slm_model.py scripts/fetch_slm_model.py

# Local SLM fallback model (app/slm.py), used automatically when
# ANTHROPIC_API_KEY is unset -- fetched at build time rather than committed
# to git. Its own layer so it's cached across rebuilds unless the fetch
# script itself changes. Skip with --build-arg SKIP_SLM=1 for a smaller/
# faster image on a deploy that will always have an API key.
ARG SKIP_SLM=0
RUN if [ "$SKIP_SLM" != "1" ]; then python scripts/fetch_slm_model.py; fi

COPY --from=ui /ui/dist frontend/dist

ENV PORT=8000
EXPOSE 8000

# ANTHROPIC_API_KEY is optional: if unset, the app serves chat from the local
# SLM fallback fetched above instead of erroring (see app/agent.py).
#   docker run -p 8000:8000 -e ANTHROPIC_API_KEY=sk-ant-... <image>
CMD ["sh", "-c", "uvicorn app.main:app --app-dir backend --host 0.0.0.0 --port ${PORT}"]
