#!/usr/bin/env bash
set -Eeuo pipefail

if [[ "$(uname -s)" != Linux || ! -r /etc/os-release ]]; then
  echo "Backup host dependency setup requires Ubuntu or another apt-based Linux host." >&2
  exit 2
fi

if ! command -v sudo >/dev/null 2>&1 || ! sudo -n true; then
  echo "Non-interactive sudo access is required to install age and the AWS CLI." >&2
  exit 2
fi

sudo apt-get update
sudo apt-get install -y --no-install-recommends age ca-certificates curl jq

if ! command -v aws >/dev/null 2>&1; then
  curl -fsSL https://awscli.amazonaws.com/v2/install.sh | sudo bash -s -- --system
fi

command -v age >/dev/null
command -v age-keygen >/dev/null
command -v aws >/dev/null
command -v jq >/dev/null
aws --version
age --version
