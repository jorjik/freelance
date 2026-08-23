FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir --upgrade pip setuptools wheel

COPY pyproject.toml README.md ./
COPY src/ src/

RUN pip install --no-cache-dir .

CMD ["python", "-m", "src.main"]
