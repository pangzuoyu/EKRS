FROM python:3.11-slim

WORKDIR /app

# Install shared package first (cache layer)
COPY ../shared /app/shared
RUN pip install --no-cache-dir /app/shared

# Install RAG package
COPY . /app/rag
RUN pip install --no-cache-dir /app/rag

WORKDIR /app/rag

EXPOSE 8000

CMD ["python", "-m", "ekrs_rag.main"]
