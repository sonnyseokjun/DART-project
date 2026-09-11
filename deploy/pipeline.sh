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
#
# 감지 모드는 "신규 없음"이어도 곧바로 끝내지 않는다. 재시도를 기다리던 원문·요약이
# 있으면 그것만 이어서 처리한다(아래 '재개' 참고).
set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-/home/ubuntu/DART-project}"
cd "$PROJECT_DIR"

# 조회 창. 전날치를 겹쳐 받아 경계 시각의 누락을 막는다(rcept_no unique라 중복은 무해).
POLL_DAYS="${POLL_DAYS:-2}"

# 한 번에 처리할 상한. 요약은 이 프로젝트에서 돈이 나가는 유일한 경로이므로
# 상한 없이 두면 백필 등으로 대상이 밀렸을 때 한 번에 다 태운다.
FETCH_LIMIT="${FETCH_LIMIT:-5}"
SUMMARIZE_LIMIT="${SUMMARIZE_LIMIT:-5}"

# "할 일 없음"을 알리는 종료 코드. 두 명령이 같은 값을 쓴다 —
# poll_dart.NOTHING_NEW_EXIT_CODE(신규 공시 없음)와
# pending_work.NOTHING_PENDING_EXIT_CODE(대기 중인 후속 작업 없음).
NOTHING_NEW=9

LOCK_FILE="${LOCK_FILE:-/tmp/dart-pipeline.lock}"

FULL=0
if [ "${1:-}" = "--full" ]; then
    FULL=1
fi

# 신규는 없지만 대기 중이던 후속 작업만 이어서 도는 실행인지.
RESUME=0

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
        # 신규가 없어도 곧바로 끝내면 안 된다. 재시도를 기다리던 공시까지 함께
        # 건너뛰기 때문이다 — 2026-09-08에 원문 미공개(DART [014])로 5분 뒤 재시도가
        # 잡혀 있던 공시가, 뒤이어 신규가 없었다는 이유만으로 8시간 반 동안 두 번째
        # 시도를 못 받았다. 배경은 disclosures/retry_policy.py 첫 주석.
        #
        # 헛호출 방지는 여기가 아니라 그 사다리가 한다. 여기서는 "지금 처리할 수 있는
        # 대기 건이 있는가"만 묻는다 — DB만 보므로 DART도 LLM도 부르지 않는다.
        set +e
        dart pending_work
        pending_rc=$?
        set -e

        if [ "$pending_rc" -eq "$NOTHING_NEW" ]; then
            exit 0
        fi
        if [ "$pending_rc" -ne 0 ]; then
            log "대기 확인 실패 (종료 코드 $pending_rc) — 뒷단계를 진행하지 않습니다"
            exit "$pending_rc"
        fi
        RESUME=1
    elif [ "$rc" -ne 0 ]; then
        log "수집 실패 (종료 코드 $rc) — 뒷단계를 진행하지 않습니다"
        exit "$rc"
    fi
fi

# --- 요약 구간 메모리 표본 ----------------------------------------------
# 여기까지 왔다는 것은 실제로 처리할 일이 있다는 뜻이다 - 헛도는 실행(신규 없음 ·
# 대기 없음)은 위에서 이미 끝났다.
#
# 왜 따로 재나: mem.log는 30분마다 표본을 뜨는데(deploy/crontab) 실행이 20초대에
# 끝나 그 사이에 걸리지 않는다. 7단계 실측에서 지연·비용은 채워졌는데 요약 중
# 메모리 피크만 끝내 비어 있던 이유가 이것이다(PLAN.md 13장 1번). 표본 간격을
# 줄이면 하루 43,200줄이 쌓이므로, 일이 있는 실행에서만 2초 간격으로 재고
# **최저 가용 한 줄만** 남긴다.
#
# 300회(10분)에서 스스로 멈춘다. 스크립트가 강제 종료돼 trap이 돌지 못해도
# 표본기가 영원히 남지 않게 하는 상한이다.
MEM_SAMPLES="$(mktemp)"
for _ in $(seq 300); do free -m | awk 'NR==2 {print $7}'; sleep 2; done \
    >> "$MEM_SAMPLES" &
MEM_SAMPLER=$!
# `|| true`가 없으면 안 된다. set -e 아래에서는 trap 안의 kill이 실패하는 순간
# 뒤처리(rm)가 끊기고, 무엇보다 **성공한 실행이 종료 코드 1로 끝난다.** 아래에서
# 이미 죽인 뒤라 여기서 kill이 실패하는 것이 오히려 정상 경로다.
trap 'kill "$MEM_SAMPLER" 2>/dev/null || true; rm -f "$MEM_SAMPLES"' EXIT

# --- 선별 → 원문 → 요약 --------------------------------------------------
# 재개 실행은 apply_selection을 건너뛴다. 새로 수집한 공시가 없으니 선별 상태가
# 달라질 수 없고, 1분마다 도는 경로라 프로세스 하나가 그대로 비용이다.
if [ "$RESUME" -eq 1 ]; then
    log "대기 중이던 후속 작업 재개 (신규 공시 없음)"
else
    log "신규 공시 처리 시작"
    dart apply_selection
fi
dart fetch_documents --limit "$FETCH_LIMIT"
dart summarize_disclosures --limit "$SUMMARIZE_LIMIT"

# 최저 가용만 뽑는다. `sort -n | head -1`은 쓰지 않는다 - head가 먼저 닫으면 sort가
# SIGPIPE로 죽고, 이 스크립트는 pipefail이라 그 순간 파이프라인 전체가 실패한다.
kill "$MEM_SAMPLER" 2>/dev/null || true
mem_min="$(awk 'NR==1 || $1 < m {m=$1} END {if (NR) print m}' "$MEM_SAMPLES")"
if [ -n "$mem_min" ]; then
    log "메모리 최저 가용 ${mem_min}MB (표본 $(wc -l < "$MEM_SAMPLES")개 · 2초 간격)"
fi
log "파이프라인 완료"
