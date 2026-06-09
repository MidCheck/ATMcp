FROM python:3.12-slim

WORKDIR /app

# Runtime deps only (test deps live in requirements.txt for local dev).
RUN pip install --no-cache-dir \
      "mcp>=1.9,<2" "fastapi>=0.115" "uvicorn[standard]>=0.30" \
      "redis>=5.0" "aiosqlite>=0.20" "pydantic>=2.7" "python-ulid>=2.2"

COPY atmcp ./atmcp

ENV ATMCP_SQLITE_PATH=/data/atmcp.db \
    ATMCP_REDIS_URL=redis://redis:6379/0 \
    ATMCP_PUBLIC_URL=http://localhost:8000

VOLUME ["/data"]
EXPOSE 8000

CMD ["uvicorn", "atmcp.app:app", "--host", "0.0.0.0", "--port", "8000"]
