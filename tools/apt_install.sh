#!/usr/bin/env bash
# Install apt packages on a CI runner, retrying a bad mirror answer (#2678).
#
# One failed `apt-get update` or `install` (exit 100) used to fail the job before any test ran.
# Each attempt runs both; a failed one is retried after DELAY seconds with a ::warning::, and the
# step fails only when the last of ATTEMPTS attempts fails. tests/test_ci_apt_retry.py runs this
# file with a stubbed sudo and sleep.
set -u

ATTEMPTS=3
DELAY=15

if [ "$#" -eq 0 ]; then
  echo "usage: apt_install.sh PACKAGE..." >&2
  exit 2
fi

attempt=1
while true; do
  if sudo apt-get update -q && sudo apt-get install -yq "$@"; then
    exit 0
  fi
  if [ "$attempt" -ge "$ATTEMPTS" ]; then
    echo "::error::apt failed $attempt times; giving up"
    exit 1
  fi
  echo "::warning::apt attempt $attempt of $ATTEMPTS failed; retrying in ${DELAY}s"
  sleep "$DELAY"
  attempt=$((attempt + 1))
done
