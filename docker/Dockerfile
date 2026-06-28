FROM python:3.11.10-bullseye

RUN apt update && apt -y install cron

# Set local timezone (for cron)
RUN ln -sf /usr/share/zoneinfo/Europe/Oslo /etc/localtime

# Create cron log file
RUN touch /var/log/podimo.log

WORKDIR /app

RUN curl -sSL https://install.python-poetry.org | python3 - -y --version 1.8.3

RUN /root/.local/bin/poetry config virtualenvs.create false

COPY pyproject.toml poetry.lock README.md ./

COPY podimo podimo

RUN /root/.local/bin/poetry install --only main

COPY docker/episode_not_available.mp3 .
COPY docker/entrypoint.sh .
RUN chmod +x ./entrypoint.sh
ENTRYPOINT ["./entrypoint.sh"]
CMD ["--host", "0.0.0.0", "--port", "8000"]
