#!/usr/bin/env bash
# One-time, idempotent Pub/Sub setup for agent-mail's Gmail push notifications.
# Runs as YOUR gcloud user (the service account needs no project IAM roles, by design).
#
# Gmail keeps one watch per mailbox. If another service already watches the same mailboxes with a topic, pass that
# topic: each service just needs its own subscription on it (Pub/Sub fans out).
#
#   topic         projects/$PROJECT/topics/$TOPIC          Gmail publishes here (created if missing)
#   subscription  projects/$PROJECT/subscriptions/$SUB     pull; agent-mail reads it
#   IAM           gmail-api-push@system.gserviceaccount.com  -> roles/pubsub.publisher on the topic only
#                 $SA                                        -> roles/pubsub.subscriber on the subscription only
#
# Usage: SA=<service-account-email> scripts/setup_gcp.sh <project> [topic] [subscription]
set -euo pipefail

GCLOUD="${GCLOUD:-$(command -v gcloud || echo "$HOME/google-cloud-sdk/bin/gcloud")}"
PROJECT="${1:?usage: SA=<service-account-email> $0 <project> [topic] [subscription]}"
TOPIC="${2:-gmail-agent-mail}"
SUB="${3:-agent-mail-pull}"
SA="${SA:?set SA to the service account's email (client_email in its JSON key)}"
G=("$GCLOUD" --project "$PROJECT" --quiet)

echo "gcloud account: $("$GCLOUD" config get-value account 2>/dev/null)"

if "${G[@]}" pubsub topics describe "$TOPIC" >/dev/null 2>&1; then
  echo "topic $TOPIC exists"
else
  "${G[@]}" pubsub topics create "$TOPIC" --message-retention-duration=1d
fi
"${G[@]}" pubsub topics add-iam-policy-binding "$TOPIC" \
  --member="serviceAccount:gmail-api-push@system.gserviceaccount.com" --role="roles/pubsub.publisher" >/dev/null
echo "granted gmail-api-push publisher on $TOPIC"

if "${G[@]}" pubsub subscriptions describe "$SUB" >/dev/null 2>&1; then
  echo "subscription $SUB exists"
else
  # Pull (no public endpoint). Never expires; keep undelivered notifications 1 day (a 15-minute catch-up sync
  # covers any that are lost).
  "${G[@]}" pubsub subscriptions create "$SUB" --topic="$TOPIC" \
    --ack-deadline=30 --message-retention-duration=1d --expiration-period=never
fi
"${G[@]}" pubsub subscriptions add-iam-policy-binding "$SUB" \
  --member="serviceAccount:$SA" --role="roles/pubsub.subscriber" >/dev/null
echo "granted $SA subscriber on $SUB"
echo "done: projects/$PROJECT/topics/$TOPIC -> projects/$PROJECT/subscriptions/$SUB"
