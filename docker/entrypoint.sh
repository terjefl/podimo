#!/bin/bash

# Start logging in background
tail -f /var/log/podimo.log &

echo "Installing crontab..."
crontab -r
crontab /etc/cron.d/podimo-crontab
cat /etc/cron.d/podimo-crontab

echo "Starting cron service..."
cron

echo "Starting server..."
podimo serve "$@"
