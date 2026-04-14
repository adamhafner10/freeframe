#!/usr/bin/env bash
# Daily backup of FileStream: encrypted Neon DB dump + encrypted .env.prod
# → Backblaze B2 bucket `filestream-backups`.
#
# Prereqs on the server (root-only files, not in git):
#   /root/.backup-passphrase    GPG symmetric passphrase (single line)
#   /root/.backup-env           B2_BACKUP_KEY_ID / SECRET / BUCKET / ENDPOINT
#
# Retention: 7 days local, 30 days on B2.
#
# Run manually:   bash /opt/freeframe/scripts/backup.sh
# Run via cron:   0 3 * * * /opt/freeframe/scripts/backup.sh >> /var/log/filestream-backup.log 2>&1

set -euo pipefail

LOCAL_DIR=/var/backups/filestream
COMPOSE_DIR=/opt/freeframe
PASSPHRASE_FILE=/root/.backup-passphrase
ENV_FILE=/root/.backup-env

# ── sanity ─────────────────────────────────────────────────────────────────
[[ -r "$PASSPHRASE_FILE" ]] || { echo "FATAL: $PASSPHRASE_FILE missing" >&2; exit 1; }
[[ -r "$ENV_FILE" ]]        || { echo "FATAL: $ENV_FILE missing" >&2; exit 1; }
[[ -r "$COMPOSE_DIR/.env.prod" ]] || { echo "FATAL: $COMPOSE_DIR/.env.prod missing" >&2; exit 1; }

mkdir -p "$LOCAL_DIR"

# shellcheck disable=SC1090
set -a
source "$ENV_FILE"
set +a

DATABASE_URL=$(grep '^DATABASE_URL=' "$COMPOSE_DIR/.env.prod" | cut -d= -f2-)
[[ -n "$DATABASE_URL" ]] || { echo "FATAL: DATABASE_URL not found in .env.prod" >&2; exit 1; }

DATE=$(date -u +%Y-%m-%d)
STAMP=$(date -u +%FT%TZ)
DB_FILE="$LOCAL_DIR/db_${DATE}.sql.gpg"
ENV_ENCRYPTED="$LOCAL_DIR/env_${DATE}.env.gpg"

PASS=$(< "$PASSPHRASE_FILE")

echo "[${STAMP}] backup start"

# ── 1. pg_dump → gpg (no plaintext on disk) ────────────────────────────────
# Use the postgres:15 image that docker already has cached; no host deps.
docker run --rm -i --network freeframe_default \
  -e PGPASSWORD_NEVER_USED=1 \
  postgres:17-alpine \
  pg_dump "$DATABASE_URL" --no-owner --no-acl --clean --if-exists 2>/dev/null \
  | gpg --batch --yes --symmetric --cipher-algo AES256 \
        --passphrase "$PASS" --output "$DB_FILE"

DB_BYTES=$(stat -c %s "$DB_FILE")
echo "[${STAMP}] db dump encrypted: ${DB_BYTES} bytes → $(basename "$DB_FILE")"

# ── 2. Encrypt .env.prod ───────────────────────────────────────────────────
gpg --batch --yes --symmetric --cipher-algo AES256 \
    --passphrase "$PASS" --output "$ENV_ENCRYPTED" \
    "$COMPOSE_DIR/.env.prod"

ENV_BYTES=$(stat -c %s "$ENV_ENCRYPTED")
echo "[${STAMP}] env encrypted: ${ENV_BYTES} bytes → $(basename "$ENV_ENCRYPTED")"

# ── 3. Upload to B2 via api container's python+boto3 ──────────────────────
export DATE
docker run --rm \
  -v "$LOCAL_DIR:/backup:ro" \
  -e B2_BACKUP_KEY_ID \
  -e B2_BACKUP_SECRET \
  -e B2_BACKUP_BUCKET \
  -e B2_BACKUP_ENDPOINT \
  -e DATE \
  --entrypoint python3 \
  freeframe-api -c "
import os, boto3
s3 = boto3.client('s3',
    endpoint_url=os.environ['B2_BACKUP_ENDPOINT'],
    aws_access_key_id=os.environ['B2_BACKUP_KEY_ID'],
    aws_secret_access_key=os.environ['B2_BACKUP_SECRET'],
    region_name='us-east-005')
bucket = os.environ['B2_BACKUP_BUCKET']
date = os.environ['DATE']
s3.upload_file(f'/backup/db_{date}.sql.gpg', bucket, f'db/{date}.sql.gpg')
print(f'[upload] db/{date}.sql.gpg OK')
s3.upload_file(f'/backup/env_{date}.env.gpg', bucket, f'env/{date}.env.gpg')
print(f'[upload] env/{date}.env.gpg OK')
"

# ── 4. Remote retention: delete backups older than 30 days ────────────────
docker run --rm \
  -e B2_BACKUP_KEY_ID \
  -e B2_BACKUP_SECRET \
  -e B2_BACKUP_BUCKET \
  -e B2_BACKUP_ENDPOINT \
  --entrypoint python3 \
  freeframe-api -c "
import os, boto3, datetime
s3 = boto3.client('s3',
    endpoint_url=os.environ['B2_BACKUP_ENDPOINT'],
    aws_access_key_id=os.environ['B2_BACKUP_KEY_ID'],
    aws_secret_access_key=os.environ['B2_BACKUP_SECRET'],
    region_name='us-east-005')
bucket = os.environ['B2_BACKUP_BUCKET']
cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=30)
for prefix in ('db/', 'env/'):
    resp = s3.list_objects_v2(Bucket=bucket, Prefix=prefix)
    for obj in resp.get('Contents', []):
        if obj['LastModified'] < cutoff:
            s3.delete_object(Bucket=bucket, Key=obj['Key'])
            print(f'[retention] deleted {obj[\"Key\"]}')
"

# ── 5. Local retention: keep 7 days ────────────────────────────────────────
find "$LOCAL_DIR" -type f -name '*.gpg' -mtime +7 -delete

echo "[${STAMP}] backup complete"
