FROM python:3.12-slim

WORKDIR /app
COPY . .

# No external dependencies today (stdlib only). This RUN line is a no-op
# placeholder for the day a requirements.txt exists (e.g. psycopg2 for
# Postgres, or an anthropic/openai SDK) — uncomment then:
# RUN pip install --no-cache-dir -r requirements.txt

ENV PORT=8000
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/health', timeout=2)" || exit 1

CMD ["python3", "server.py"]
