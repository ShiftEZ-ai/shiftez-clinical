plan=$(dc run --rm -T migrate python scripts/manage.py check-migrations "${confirm[@]}") || exit 1
APP_BACKUP_CMD=${APP_BACKUP_CMD:-infra/production/ops.sh backup}
: "${APP_BACKUP_CMD:=sudo systemctl start rota-backup.service}"
phase verified-backup
if [ "$pending" != 0 ]; then
  log "stopping writers before the migration backup"
  dc stop api worker
fi

if [ "${APP_SKIP_BACKUP:-no}" = yes ] && [ "$pending" = 0 ]; then
  log "backup skipped (APP_SKIP_BACKUP=yes, no schema change)"
else
  log "verified backup before $pending pending migration(s) ($APP_BACKUP_CMD)"
  if ! $APP_BACKUP_CMD; then
    log "BACKUP FAILED - no migration ran; the schema is unchanged"
    if [ "$pending" != 0 ] && [ -n "$previous" ]; then
      log "restarting the previous image $previous"
      recover_previous
    elif [ "$pending" = 0 ]; then
      log "the previous application image is retained; no migration-related writer stop ran"
    else
      log "writers stay stopped: no previous image is available"
    fi
    exit 1
  fi
fi

if [ "$pending" = 0 ]; then
  log "stopping writers after the backup"
  dc stop api worker
fi

if [[ ":$COMPOSE_FILE:" = *"/production/compose.production.yml:"* ]]; then
  log "provisioning the separate worker database login"
  worker_role sql | dc exec -T -e 'PGOPTIONS=-c log_statement=none -c log_min_error_statement=panic -c log_min_duration_statement=-1 -c log_min_duration_sample=-1 -c log_transaction_sample_rate=0' \
    db psql -v ON_ERROR_STOP=1 -X -q -U postgres -d postgres > /dev/null
fi

log "migrating with the new image"
phase migration
migration_options=()
if [ -n "${APP_DEPLOY_OPERATION_DIR:-}" ]; then
  budget=${APP_MIGRATION_TIMEOUT_SECONDS:-3600}
  [[ "$budget" =~ ^[0-9]+$ && "$budget" -ge 60 && "$budget" -le 5400 ]] || { log 'Invalid migration budget (60..5400 seconds)'; exit 1; }
  migration_options=(-e "PGOPTIONS=-c transaction_timeout=${budget}s")
fi
if ! dc run --rm "${migration_options[@]}" migrate python scripts/manage.py migrate "${confirm[@]}"; then
  observed=$(dc run --rm -T migrate python scripts/manage.py check-migrations "${confirm[@]}" 2>/dev/null |
    sed -n 's/^Database head: \([^;]*\);.*$/\1/p') || observed=unknown
  if [ -n "$previous" ] && [ -n "$base_head" ] && [ "$observed" = "$base_head" ]; then
    log "MIGRATION FAILED - confirmed unchanged database; restarting the previous image"
    recover_previous
  else
    log "MIGRATION FAILED - database outcome changed or unknown; writers stay stopped. Fix forward or restore the verified backup."
  fi
  exit 1
fi

if grep -qx db <<< "$services"; then
  phase runtime-grants
  log "reapplying runtime grants"
  dc exec -T db sh -c 'exec psql -v ON_ERROR_STOP=1 -X -q -U postgres -d "$POSTGRES_DB"' < infra/database-roles.sql > /dev/null
fi

if [ "$pending" != 0 ]; then
  phase fairness-rebuild
  log "rebuilding the fairness ledger from current publications"
  dc run --rm migrate python scripts/manage.py rebuild-fairness ||
    { log "FAIRNESS REBUILD FAILED (rolled back, no data changed); api/worker stay stopped. Fix, run: docker compose run --rm migrate python scripts/manage.py rebuild-fairness, then: docker compose up -d api worker"; exit 1; }
fi

phase production-preflight
log "read-only production preflight with runtime credentials"
dc run --rm -T --no-deps api python scripts/preflight.py --require-production --connect --release-image "$image" ||
  { log "PREFLIGHT FAILED (reason above); api/worker stay stopped. Fix and rerun deployment."; exit 1; }

log "starting api and worker"
phase application-health
dc up -d api worker

fail() {
  log "$1"
  dc logs --tail 100 api worker || true
  if [ "$pending" = 0 ] && [ -n "$previous" ]; then
    log "ROLLING BACK to $previous (no migration ran, the schema is unchanged)"
    ROTA_IMAGE=$previous dc up -d api worker
  else
    dc stop api worker
    log "no automatic rollback: ${pending} migration(s) ran; previous image was ${previous:-none}"
  fi
  exit 1
}

