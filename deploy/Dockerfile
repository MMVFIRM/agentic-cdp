FROM python:3.11-slim
WORKDIR /app
COPY pyproject.toml ./
COPY acdp ./acdp
RUN pip install --no-cache-dir ".[postgres]" && useradd -r -u 10001 acdp
USER acdp
EXPOSE 8080
CMD ["uvicorn", "acdp.api:app_factory", "--factory", "--host", "0.0.0.0", "--port", "8080"]
