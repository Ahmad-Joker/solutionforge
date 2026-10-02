#!/bin/sh
# Entrypoint for every API-image container (api, worker, migrate).
# Local compose only: if SF_JWT_SECRET / SF_CREDENTIALS_KEYS are unset, take the values the
# one-shot `dev-secrets` service generated into the shared volume, so every process and
# container signs tokens and encrypts connector credentials with the SAME keys. (Unset, each
# process would mint its own: tokens from one API process were rejected by the others, and
# workers could not decrypt credentials saved through the API.) Real deployments set both
# variables and never mount this volume.
set -eu
DEV=/run/sf-dev/secrets.env
if [ -f "$DEV" ]; then
  if [ -z "${SF_JWT_SECRET:-}" ]; then
    SF_JWT_SECRET="$(sed -n 's/^SF_JWT_SECRET=//p' "$DEV")"; export SF_JWT_SECRET
  fi
  if [ -z "${SF_CREDENTIALS_KEYS:-}" ]; then
    SF_CREDENTIALS_KEYS="$(sed -n 's/^SF_CREDENTIALS_KEYS=//p' "$DEV")"; export SF_CREDENTIALS_KEYS
  fi
fi
exec "$@"
