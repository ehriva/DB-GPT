# syntax=docker/dockerfile:1
FROM python:3.11-slim-bookworm
COPY --from=ghcr.io/astral-sh/uv:0.8.22 /uv /usr/local/bin/uv
ENV PYTHONUNBUFFERED=1 UV_HTTP_TIMEOUT=180 UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential pkg-config git libpq-dev default-libmysqlclient-dev \
    libgomp1 ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY packages ./packages
COPY skills ./skills
COPY assets ./assets
COPY i18n ./i18n
COPY pilot ./pilot
COPY docker/examples ./docker/examples
RUN --mount=type=cache,target=/root/.cache/uv \
    uv export --frozen --all-packages --no-dev --no-emit-workspace --no-hashes \
    --extra base --extra proxy_openai --extra rag --extra storage_chromadb \
    --extra hf --extra cpu --extra dbgpts --output-file /tmp/erp-requirements.txt && \
    uv venv /app/.venv && \
    UV_CONCURRENT_DOWNLOADS=8 uv pip install --no-deps --torch-backend cpu \
    --index-url https://pypi.tuna.tsinghua.edu.cn/simple \
    -r /tmp/erp-requirements.txt && \
    for manifest in packages/*/pyproject.toml packages/dbgpt-accelerator/*/pyproject.toml; do \
      uv pip install --no-deps -e "${manifest%/pyproject.toml}" || exit 1; \
    done && uv pip check && \
    python -c "import torch; assert torch.version.cuda is None; import dbgpt"
COPY docker/erp/entrypoint.py /app/docker/erp/entrypoint.py
EXPOSE 5670
ENTRYPOINT ["python", "/app/docker/erp/entrypoint.py"]
