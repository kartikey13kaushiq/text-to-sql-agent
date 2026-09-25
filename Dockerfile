FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir ".[postgres,api]"
COPY sql ./sql
COPY evals/bench ./evals/bench
USER nobody
EXPOSE 8000
CMD ["uvicorn", "text2sql.api:app", "--host", "0.0.0.0", "--port", "8000"]
