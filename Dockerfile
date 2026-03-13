FROM python:3.12-slim

WORKDIR /app

# Install dependencies first for layer caching
COPY pyproject.toml .
RUN pip install --no-cache-dir .

# Copy application code
COPY src/ src/

# Create data directory
RUN mkdir -p /app/data
ENV MCP_CLOUD_DATA_DIR=/app/data

EXPOSE 8000

CMD ["uvicorn", "src.app:app", "--host", "0.0.0.0", "--port", "8000"]
