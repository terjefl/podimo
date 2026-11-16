#!/bin/bash
# PODIMO_MODE selects what this container runs:
#   web     (default) only the HTTP server: no crontab, no cron daemon
#   harvest only cron with the mounted crontab (/etc/cron.d/podimo-crontab), no server
# Running the two as separate containers keeps the bulk writes of a harvest to the
# share out of the server's process tree, cgroup and I/O queue.
set -e

MODE="${PODIMO_MODE:-web}"

case "$MODE" in
  web)
    echo "Mode: web. Starting server..."
    exec podimo serve "$@"
    ;;
  harvest)
    echo "Mode: harvest. Installing crontab..."
    crontab -r 2>/dev/null || true
    # cron starts jobs with an empty environment, so the container's config path,
    # staging directory and PATH are written into the crontab itself.
    {
      echo "PODIMO_CONFIG_FILE=${PODIMO_CONFIG_FILE:-/app/config.yaml}"
      echo "STAGING_DIR=${STAGING_DIR:-/app/staging}"
      echo "PATH=${PATH}"
      cat /etc/cron.d/podimo-crontab
    } | crontab -
    crontab -l
    mkdir -p "${STAGING_DIR:-/app/staging}"
    # show the harvest log in `docker logs`
    tail -F /var/log/podimo.log &
    echo "Starting cron in the foreground..."
    exec cron -f
    ;;
  *)
    echo "Unknown PODIMO_MODE '$MODE' (use web or harvest)" >&2
    exit 64
    ;;
esac
