#!/bin/bash
# SQLite DB를 S3에 백업한다. cron이 매일 04:00에 부른다.
#
# 파일을 그냥 cp하면 안 된다 — WAL 모드에서는 커밋된 내용이 아직 -wal 파일에만
# 있을 수 있어 반쪽짜리 백업이 나온다. sqlite3의 backup API는 잠금을 잡고
# 일관된 스냅샷을 만든다.
set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-/home/ubuntu/DART-project}"
cd "$PROJECT_DIR"

# .env에서 S3_BACKUP_BUCKET, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY,
# AWS_DEFAULT_REGION을 읽는다.
# shellcheck disable=SC1091
. ./deploy/_load_env.sh
_load_env .env

: "${S3_BACKUP_BUCKET:?S3_BACKUP_BUCKET이 .env에 없습니다}"

STAMP=$(date +%Y%m%d_%H%M%S)
TMP_IN_CONTAINER="/app/data/backup_${STAMP}.sqlite3"
TMP_ON_HOST="./data/backup_${STAMP}.sqlite3"

echo "[backup] 일관된 스냅샷 생성"
docker compose exec -T web python -c "
import sqlite3, sys
src = sqlite3.connect('/app/data/db.sqlite3')
dst = sqlite3.connect('${TMP_IN_CONTAINER}')
with dst:
    src.backup(dst)
dst.close()
src.close()
print('ok', file=sys.stderr)
"

echo "[backup] 압축"
gzip -9 "$TMP_ON_HOST"

echo "[backup] S3 업로드"
aws s3 cp "${TMP_ON_HOST}.gz" \
    "s3://${S3_BACKUP_BUCKET}/db/db_${STAMP}.sqlite3.gz" \
    --only-show-errors

echo "[backup] 로컬 임시 파일 정리"
unlink "${TMP_ON_HOST}.gz"

echo "[backup] 완료: s3://${S3_BACKUP_BUCKET}/db/db_${STAMP}.sqlite3.gz"
