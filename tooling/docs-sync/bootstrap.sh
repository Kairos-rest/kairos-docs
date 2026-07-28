#!/usr/bin/env bash
# Cron entry point for the docs sync pipeline.
#
# The pipeline code lives in this repo; only credentials and the cursor live on
# the VPS. This script is the one piece that has to exist on the box before the
# repo is available, so it stays deliberately small and stable: load the env,
# fast-forward a read-only checkout of the docs repo, then hand off to Python.
#
# The update-then-exec order matters. It updates the checkout and then execs
# `python3`, never another bash script from that same checkout — bash reads a
# script incrementally, so rewriting a running .sh underneath itself is how you
# get a half-old, half-new run.
#
# Install (or reinstall) on the VPS:
#   mkdir -p ~/kairos-docs-pipeline
#   cp tooling/docs-sync/bootstrap.sh ~/kairos-docs-pipeline/bootstrap.sh
#   chmod +x ~/kairos-docs-pipeline/bootstrap.sh
# Crontab line:
#   */30 * * * * /home/sophios/kairos-docs-pipeline/bootstrap.sh >> /home/sophios/logs/kairos-docs-sync.log 2>&1

set -euo pipefail

PIPELINE_HOME="${DOCS_SYNC_HOME:-$HOME/kairos-docs-pipeline}"
TOOLING_CHECKOUT="$PIPELINE_HOME/tooling-checkout"
DOCS_REMOTE="${DOCS_SYNC_TOOLING_REMOTE:-https://github.com/Kairos-rest/kairos-docs.git}"
DOCS_REF="${DOCS_SYNC_BASE_BRANCH:-main}"

# NAN_API_URL / NAN_API_KEY come from the shared credential file every other
# Kairos script on this box sources; the two GitHub PATs are pipeline-specific.
set -a
# shellcheck disable=SC1090
[ -f "$HOME/.hermes/.env.pulgita" ] && source "$HOME/.hermes/.env.pulgita"
# shellcheck disable=SC1091
source "$PIPELINE_HOME/.env-kairos-docs"
set +a

export DOCS_SYNC_HOME="$PIPELINE_HOME"

# Fast-forward the code checkout. This clone is read-only as far as the pipeline
# is concerned — the working clone it commits from is a separate directory
# (`kairos-docs-checkout`), so a force-push there can never disturb this one.
if [ ! -d "$TOOLING_CHECKOUT/.git" ]; then
  git clone --quiet --branch "$DOCS_REF" "$DOCS_REMOTE" "$TOOLING_CHECKOUT"
else
  git -C "$TOOLING_CHECKOUT" fetch --quiet origin "$DOCS_REF"
  git -C "$TOOLING_CHECKOUT" reset --quiet --hard "origin/$DOCS_REF"
fi

exec /usr/bin/python3 "$TOOLING_CHECKOUT/tooling/docs-sync/docs-sync.py"
