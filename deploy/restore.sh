#!/bin/bash
# S3 백업에서 DB를 복원한다.
#
#   ./deploy/restore.sh                     가장 최근 백업으로 복원
#   ./deploy/restore.sh db_20260826_040000.sqlite3.gz   특정 백업으로 복원
#
# 복원해본 적 없는 백업은 백업이 아니다. 6단계 Phase 5에서 실제로 한 번 돌려본다.
set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-/home/ubuntu/DART-project}"
cd "$PROJECT_DIR"

set -a
# shellcheck disable=SC1091
source .env
set +a

: "${S3_BACKUP_BUCKET:?S3_BACKUP_BUCKET이 .env에 없습니다}"

TARGET="${1:-}"
if [ -z "$TARGET" ]; then
    echo "[restore] 최근 백업 조회"
    TARGET=$(aws s3 ls "s3://${S3_BACKUP_BUCKET}/db/" | sort | tail -1 | awk '{print $4}')
    [ -n "$TARGET" ] || { echo "백업이 없습니다"; exit 1; }
fi
echo "[restore] 대상: $TARGET"

echo "[restore] 서비스 중지 (복원 중 쓰기 방지)"
docker compose stop web

STAMP=$(date +%Y%m%d_%H%M%S)
if [ -f ./data/db.sqlite3 ]; then
    echo "[restore] 기존 DB를 db.sqlite3.before_${STAMP}로 보존 (되돌릴 때 쓴다)"
    mv ./data/db.sqlite3 "./data/db.sqlite3.before_${STAMP}"
fi

echo "[restore] 내려받기"
aws s3 cp "s3://${S3_BACKUP_BUCKET}/db/${TARGET}" ./data/restore.sqlite3.gz --only-show-errors
gunzip -f ./data/restore.sqlite3.gz
mv ./data/restore.sqlite3 ./data/db.sqlite3
chown 1000:1000 ./data/db.sqlite3 2>/dev/null || sudo chown 1000:1000 ./data/db.sqlite3

echo "[restore] 서비스 기동 (엔트리포인트가 migrate를 적용한다)"
docker compose up -d web

echo "[restore] 검증"
docker compose exec -T web python -c "
import os, django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()
from disclosures.models import Disclosure, DisclosureSummary
print(f'공시 {Disclosure.objects.count()}건 / 요약 {DisclosureSummary.objects.count()}건')
"

echo "[restore] 완료. 이전 DB는 ./data/db.sqlite3.before_${STAMP}에 남아 있다."
