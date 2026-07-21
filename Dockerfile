FROM python:3.11-slim

WORKDIR /app
RUN apt-get update \
 && apt-get install -y --no-install-recommends openssh-client \
 && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
ARG PIP_INDEX_URL=https://pypi.org/simple
RUN pip install --no-cache-dir --default-timeout=180 \
    --index-url "${PIP_INDEX_URL}" \
    -r requirements.txt
COPY app.py .
COPY templates/ templates/
COPY static/ static/
RUN mkdir -p data

EXPOSE 9090
CMD ["python", "app.py"]
