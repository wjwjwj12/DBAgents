FROM python:3.12-slim

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /tmp/ai-ppt-sandbox-requirements.txt

RUN python -m pip install --no-cache-dir -r /tmp/ai-ppt-sandbox-requirements.txt \
    && python -m playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/* /root/.cache /tmp/ai-ppt-sandbox-requirements.txt

RUN curl --version \
    && python -c "import PIL, docx, fitz, playwright, pptx, xlsxwriter"

WORKDIR /workspace
