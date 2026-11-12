FROM python:3.13-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir . && useradd --create-home app && mkdir /data && chown app /data
USER app
ENV RECEPTIONIST_DB=/data/receptionist.db
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz')"
CMD ["sh", "-c", "receptionist serve --host 0.0.0.0 --port 8000 --db ${RECEPTIONIST_DB} --brain ${RECEPTIONIST_BRAIN:-rules}"]
