#!/bin/bash

# Start logging in background
tail -f /var/log/podimo.log &

echo "Installing crontab..."
crontab -r 2>/dev/null
# cron starts jobs with an empty environment, so the container's config path
# (and PATH) are written into the crontab itself.
{
  echo "PODIMO_CONFIG_FILE=${PODIMO_CONFIG_FILE:-/app/config.yaml}"
  echo "PATH=${PATH}"
  cat /etc/cron.d/podimo-crontab
} | crontab -
crontab -l

echo "Starting cron service..."
cron

echo "Starting server..."
podimo serve "$@"
