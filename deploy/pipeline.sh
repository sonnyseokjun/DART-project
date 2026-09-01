#!/bin/bash
# 수집 → 선별 → 원문 확보 → 요약을 **한 번에 이어서** 돈다. cron이 자주 부른다.
#
# 왜 네 명령을 이어 붙였나:
#   6단계에서는 07:00/07:10/07:20/07:30으로 10분씩 띄워 걸었다. 공시를 감지하고도
#   화면에 뜨기까지 30분이 더 걸린다는 뜻이다. 7단계의 목표가 "접수 후 3분"이므로
#   (PLAN.md 9.3) 감지 직후 같은 실행에서 요약까지 끝내야 한다.
#
# 왜 Celery가 아니라 셸 스크립트인가:
#   PLAN.md 9.3 참조. 요지는 이 파이프라인이 **한 번에 하나만 돌면 된다**는 것이다.
#   Celery worker가 여럿이 되는 순간 SQLite의 동시 쓰기 한계에 부딪혀 PostgreSQL까지
#   끌려온다. flock으로 중복 실행만 막으면 주기를 아무리 줄여도 쓰기 주체는 하나다.
#
# 사용법:
#   ./deploy/pipeline.sh            # 감지 모드 — 신규가 없으면 1회 호출로 끝난다
#   ./deploy/pipeline.sh --full     # 전체 폴링 — 유형 라벨 보정·누락 보강 (하루 1회)
set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-/home/ubuntu/DART-project}"
cd "$PROJECT_DIR"

# 조회 창. 전날치를 겹쳐 받아 경계 시각의 누락을 막는다(rcept_no unique라 중복은 무해).
POLL_DAYS="${POLL_DAYS:-2}"

# 한 번에 처리할 상한. 요약은 이 프로젝트에서 돈이 나가는 유일한 경로이므로
# 상한 없이 두면 백필 등으로 대상이 밀렸을 때 한 번에 다 태운다.
FETCH_LIMIT="${FETCH_LIMIT:-5}"
SUMMARIZE_LIMIT="${SUMMARIZE_LIMIT:-5}"

# poll_dart가 "--detect로 봤는데 신규 없음"을 알리는 종료 코드.
# poll_dart.NOTHING_NEW_EXIT_CODE와 같은 값이어야 한다.
NOTHING_NEW=9

LOCK_FILE="${LOCK_FILE:-/tmp/dart-pipeline.lock}"

FULL=0
if [ "${1:-}" = "--full" ]; then
    FULL=1
fi

log() {
    echo "[$(date -Is)] $*"
}

dart() {
    docker compose exec -T web python manage.py "$@"
}

# --- 중복 실행 방지 ------------------------------------------------------
# 잠금을 crontab이 아니라 스크립트 안에서 잡는다. 사람이 손으로 실행하는 경우까지
# 함께 막기 위해서다 — cron이 도는 중에 수동 실행이 겹치면 같은 문제가 생긴다.
#
# flock은 프로세스가 죽으면 커널이 잠금을 놓아준다. 잠금 파일이 남아도 다음 실행이
# 막히지 않으므로, 스크립트가 강제 종료돼도 뒤처리가 필요 없다.
exec 9>"$LOCK_FILE"

# flock의 종료 코드를 구분해서 받는다. 1(잠금 획득 실패)만 "앞 실행이 돌고 있음"이고,
# 그 밖의 값은 flock 자체가 실패한 것이다 — 미설치(127)가 대표적이다.
#
# 처음에는 `if ! flock -n 9`로 뭉뚱그려 썼다가 테스트에서 잡았다. 그렇게 두면
# flock이 없는 환경에서 스크립트가 **"평소처럼 건너뜀"을 찍고 종료 0으로 끝난다.**
# 파이프라인이 며칠 멈춰도 로그가 정상으로 보이는, 가장 나쁜 종류의 실패다.
set +e
flock -n 9
lock_rc=$?
set -e

if [ "$lock_rc" -eq 1 ]; then
    log "앞 실행이 아직 돌고 있어 건너뜁니다 (정상)"
    exit 0
elif [ "$lock_rc" -ne 0 ]; then
    log "flock 실행 실패 (종료 코드 $lock_rc) — 잠금 없이 돌지 않습니다"
    exit "$lock_rc"
fi

# --- 수집 ---------------------------------------------------------------
if [ "$FULL" -eq 1 ]; then
    log "전체 폴링 시작 (유형 라벨 보정 · 누락 보강)"
    dart poll_dart --days "$POLL_DAYS"
else
    # set -e 아래에서는 0이 아닌 종료 코드가 곧바로 스크립트를 끝낸다.
    # "신규 없음"(9)은 정상이므로 여기서만 잠시 꺼두고 직접 분기한다.
    set +e
    dart poll_dart --detect --days "$POLL_DAYS"
    rc=$?
    set -e

    if [ "$rc" -eq "$NOTHING_NEW" ]; then
        # 뒷단계를 건너뛰는 것이 이 스크립트의 핵심이다. 특히 fetch_documents는
        # 원문이 아직 안 올라온 공시(DART [014])를 매번 다시 부르므로, 1분 주기에서
        # 그대로 두면 하루 수천 번의 헛호출이 된다.
        exit 0
    fi
    if [ "$rc" -ne 0 ]; then
        log "수집 실패 (종료 코드 $rc) — 뒷단계를 진행하지 않습니다"
        exit "$rc"
    fi
fi

# --- 선별 → 원문 → 요약 --------------------------------------------------
log "신규 공시 처리 시작"
dart apply_selection
dart fetch_documents --limit "$FETCH_LIMIT"
dart summarize_disclosures --limit "$SUMMARIZE_LIMIT"
log "파이프라인 완료"
