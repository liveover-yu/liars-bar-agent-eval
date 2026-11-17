ARG PYTHON_IMAGE=python:3.11-slim
FROM ${PYTHON_IMAGE}
ARG PIP_INDEX_URL=https://pypi.org/simple
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --index-url "$PIP_INDEX_URL" -r requirements.txt
COPY eval_worker/ eval_worker/
COPY analysis/*.py analysis/
COPY player.py game_record.py llm_client.py ./
COPY prompt/ prompt/
COPY contracts/ contracts/
ENTRYPOINT ["python", "-m", "eval_worker"]
