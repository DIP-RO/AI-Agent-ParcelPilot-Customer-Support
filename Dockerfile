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
COPY --from=ui /ui/dist frontend/dist

ENV PORT=8000
EXPOSE 8000

# ANTHROPIC_API_KEY must be provided at runtime:
#   docker run -p 8000:8000 -e ANTHROPIC_API_KEY=sk-ant-... <image>
CMD ["sh", "-c", "uvicorn app.main:app --app-dir backend --host 0.0.0.0 --port ${PORT}"]
