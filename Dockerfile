FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends git curl jq nodejs npm build-essential sqlite3 ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# The agent's shell commands run as `agent` (home /workspace). The runner itself runs as
# root so it can keep /runner (state.db, logs, STOP) and /app (config with the budget)
# unreadable to the agent.
RUN useradd --no-create-home --home-dir /workspace --shell /bin/bash agent \
 && mkdir -p /workspace /runner && chown agent:agent /workspace && chmod 700 /runner

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY agent/ agent/
COPY cli.py analyze.py config.toml entrypoint.sh ./
RUN sed -i 's/\r$//' entrypoint.sh && chmod 700 /app && chmod +x entrypoint.sh

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["python", "-m", "agent.runner", "/app/config.toml"]
