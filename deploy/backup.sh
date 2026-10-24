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

# 성공을 DB에 남긴다(10단계, 이슈 #56). 운영 점검은 "마지막 백업 성공이 26시간을 넘었나"로
# 백업이 멈춘 것을 알아챈다 — 이 줄이 없으면 백업이 며칠 멈춰도 아무도 모른다.
#
# 맨 끝에 두는 이유: 위 단계 중 하나라도 실패하면 set -e로 여기까지 오지 않는다. 실패를
# 따로 적지 않아도 성공 시각이 멈추므로 판정에 걸린다(disclosures/health.py 첫 주석).
#
# 파이프라인과 같은 잠금을 잡는다. DB에 쓰는 프로세스를 하나로 유지하는 것이 SQLite를 쓰는
# 근거다(PLAN.md 9.3). 위의 스냅샷은 읽기라 잠금 없이 돈다.
# 기록이 실패해도 백업은 끝났다 — 종료 코드를 바꾸지 않고 로그만 남긴다.
if ! flock -w 600 /tmp/dart-pipeline.lock \
        docker compose exec -T web python manage.py record_job backup \
        --message "s3://${S3_BACKUP_BUCKET}/db/db_${STAMP}.sqlite3.gz"; then
    echo "[backup] 경고: 백업 성공 기록 실패 — 백업 자체는 완료됐습니다"
fi
