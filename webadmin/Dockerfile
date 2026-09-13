FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
COPY fork_update.py .
COPY version_lifecycle.py .
COPY transcoding_config.py .
COPY transcoding_runtime_fix.py .
COPY WEBADMIN_VERSION .
COPY static ./static
ENV PYTHONUNBUFFERED=1
ENV WEBADMIN_VERSION_FILE=/app/WEBADMIN_VERSION
EXPOSE 8090
HEALTHCHECK --interval=30s --timeout=5s --retries=3 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8090/health', timeout=3)" || exit 1
CMD ["uvicorn", "transcoding_runtime_fix:app", "--host", "0.0.0.0", "--port", "8090"]
