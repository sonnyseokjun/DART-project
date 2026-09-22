"""원문 확보·요약 생성의 재시도 정책과 "지금 할 일이 남았는가" 판정.

**왜 명령 안이 아니라 별도 모듈인가.**

`deploy/pipeline.sh`는 감지 폴링에 신규가 없으면(`poll_dart --detect` 종료 코드 9)
뒷단계를 통째로 건너뛰고 끝난다. 1분 주기에서 헛호출을 막는 장치인데, **재시도를
기다리던 공시까지 함께 건너뛰어진다.**

2026-09-08 실측에서 드러났다. 17:38에 감지된 공시의 원문이 아직 없어(DART `[014]`)
5분 뒤 재시도로 잡혀 있었는데, 그 뒤 신규가 들어오지 않아 **8시간 반 동안 두 번째
시도가 한 번도 없었다.** 아래 사다리가 설계상 31시간에 6번인데, 실제로는 다음
전체 폴링(07:05)까지 밀려 **5일에 6번**이 된다. 원문이 늦게 올라오는 유형에서
7단계의 목표인 "접수 후 3분"이 성립하지 않는다.

같은 폭주를 두 겹으로 막고 있었던 것이 원인이다 — 바깥(스크립트의 조기 종료)이
안쪽(아래 사다리)보다 거칠어서, 정교한 쪽이 일할 기회를 얻지 못했다. 그래서 스크립트가
끝내기 전에 **"지금 처리할 수 있는 대기 건이 있는가"를 물어볼 수 있게** 정책을
여기로 모았다(`pending_work` 명령이 이 모듈만 본다).

정책 상수의 소유권은 그대로다. `fetch_documents`·`summarize_disclosures`는 여기서
다시 내보내므로 `fetch_command.MAX_FETCH_ATTEMPTS` 같은 기존 참조는 그대로 쓴다.
`MAX_SUMMARY_ATTEMPTS`만 models에 남아 있다 — 화면(views)도 그 값을 보기 때문이다.
"""
from datetime import timedelta

from django.utils import timezone

from disclosures.models import MAX_SUMMARY_ATTEMPTS, Disclosure
from disclosures.selection import SelectionState

#: 원문 확보 실패 후 다음 시도까지 기다리는 시간(분). 인덱스 = 쌓인 시도 횟수 - 1.
#:
#: 목록에는 떴는데 원문이 아직 공개되지 않은 공시(DART `[014]`)가 이 값을 결정했다.
#: 원문은 대개 몇 분~몇 시간 뒤에 올라오므로 **확률이 몰린 앞쪽에 예산을 몰아 쓴다.**
#:
#: 6단계까지는 이 장치가 없어도 됐다 — 파이프라인이 하루 1회라 재시도도 하루 1회였다.
#: 7단계에서 평일 낮 1분마다 돌게 되면서 상한이 선택이 아니라 필수가 됐다(PLAN.md 9.3).
#:
#: **2026-09-11에 (5, 15, 60, 360, 1440) 6회에서 아래로 바꿨다.** 옛 배치는 "1분마다
#: 재시도하면 하루 1,440번"이라는 걱정에서 나왔는데, 그건 *무한* 재시도의 비용이지
#: 짧고 촘촘한 구간의 비용이 아니다. 실측 사용량은 하루 614회로 한도 20,000회의 3%였다.
#: 여유가 그만큼인데 공시 하나에 6번만 쓰고 있었다.
#:
#: 성긴 사다리의 대가는 지연이다. **간격이 곧 "원문이 올라온 뒤 우리가 알아채기까지"의
#: 최악값**이라, 60분 칸에 걸리면 원문이 1분 뒤 올라와도 59분을 더 기다린다. 2026-09-09에
#: 17:36 공시가 18:59에야 화면에 떴다 — 4회째(60분 칸)에 성공한 것이라, 실제로 원문이
#: 언제 올라왔는지는 그 한 시간 어딘가로만 남고 알 수 없다.
#:
#:   구간         간격    시도  누적      이 구간의 최악 지연
#:   0~30분       1분      30    30분      1분
#:   30분~2시간   5분      18    2시간     5분
#:   2~6시간      30분      8    6시간     30분
#:   6~24시간     3시간     6    24시간    3시간
#:
#: 위 "최악 지연"은 만기 판정이 cron의 분 눈금과 맞을 때의 값이다. 2026-09-15까지는
#: 맞지 않아 1분 칸이 2분마다 돌았다 - 경위는 `is_retry_due` 주석.
#:
#: 공시 하나당 62회. DART 한도로는 3% → 3.4%로 무시할 만하다. **진짜 비용은 서버다** —
#: 대기 건이 있는 동안 파이프라인이 매분 끝까지 돌아 Django 프로세스를 띄운다(평소
#: 헛도는 실행은 1개, 대기 중에는 4개). 그래서 같은 날 요약 구간 메모리 측정을 함께
#: 넣었다(PLAN.md 13장 1번) — 그 값이 나오면 앞 구간을 더 촘촘하게 할지 판단한다.
#:
#: 촘촘한 사다리는 측정 장치이기도 하다. 지금 `[014]` 사례가 2건뿐이라 "원문이 보통
#: 몇 분 만에 올라오는가"를 모른다. 1분 간격으로 물으면 성공한 시도 번호가 곧 그 답이다.
RETRY_BACKOFF_MINUTES = (
    (1,) * 30       # 0~30분 — 대부분 여기서 풀린다고 보고 예산을 몰아 둔다
    + (5,) * 18     # 30분~2시간
    + (30,) * 8     # 2~6시간
    + (180,) * 6    # 6~24시간 — 여기까지 오면 그날 안에 안 올라온 공시다
)

#: 이 횟수만큼 실패하면 더 시도하지 않는다. 위 표의 길이보다 하나 크다 —
#: 마지막 대기(3시간)를 보낸 뒤 한 번 더 시도하고 멈춘다는 뜻이다. 누적 약 24시간.
MAX_FETCH_ATTEMPTS = len(RETRY_BACKOFF_MINUTES) + 1

#: 요약 실패 후 다음 시도까지 기다리는 시간(분). 원문 확보보다 짧고 성긴 이유가 둘이다.
#:   1. 실패 성격이 다르다. 원문 미공개는 시간이 해결하지만, 요약 실패는 스키마 위반이나
#:      원문 과대 같은 **결정적 원인**이 많아 그냥 다시 불러도 같은 결과가 나온다.
#:   2. 재시도 비용이 다르다. 원문 확보는 DART 호출 한도만 쓰지만 요약은 실제 돈이다.
#: 상한(MAX_SUMMARY_ATTEMPTS=4)까지 가도 건당 최대 4회, 약 $0.09에서 멈춘다.
SUMMARY_RETRY_BACKOFF_MINUTES = (10, 60, 360)

#: 관심 기업 백필(최근 공시 채워 넣기)의 재시도 간격(분)과 상한 (이슈 #44).
#:
#: 백필 실패는 대개 DART 쪽 일시 장애다. 1분마다 다시 부르면 장애 중에 한도만 닳으므로
#: 간격을 둔다. 상한에 걸리면 포기한다 — 그 기업은 이후 새 공시부터 정상 수집되고,
#: 빠지는 것은 추가 전 30일 목록뿐이다. 사용자에게는 "불러오지 못함"으로 보인다.
BACKFILL_RETRY_BACKOFF_MINUTES = (5, 30)
MAX_BACKFILL_ATTEMPTS = len(BACKFILL_RETRY_BACKOFF_MINUTES) + 1


def is_retry_due(attempts, attempted_at, backoff_minutes, now):
    """이 시도 이력이면 `now`에 다시 시도해도 되는가.

    한 번도 시도하지 않았거나 시각 기록이 없으면 곧바로 대상이다. 기록이 없는데
    기다리게 하면 되살릴 방법이 없어 영영 멈춘다.

    **시도 시각의 '초'는 버리고 센다.** 사다리도 cron도 분 단위인데 `attempted_at`에는
    초가 붙는다 - 파이프라인이 원문 확보 단계에 닿기까지 몇 초가 걸리기 때문이다.
    초를 그대로 두면 만기가 분 중간에 걸리고, cron은 매분 00초에만 뜨므로 **그 분을
    통째로 건너뛴다.**

    2026-09-15 실측에서 드러났다 - 1분 칸이 실제로는 2분마다 돌고 있었다. 14:17:08에
    실패하면 만기가 14:18:08인데, 14:18:00 실행의 판정은 8초 이르다는 이유로 지나가고
    14:19:00이 첫 기회가 됐다. 오차가 칸 크기와 같아 **첫 30분 구간의 최악 지연이 1분이
    아니라 2분이었고, 사다리 전체가 24시간이 아니라 약 25시간이었다.** 2026-09-09에도
    같은 일이 있었으나 5분 칸이 6분이 되는 정도라 눈에 띄지 않았다.

    **지터가 아니라 구조다.** `deploy/pipeline.sh`는 `pending_work`를 부른 뒤에야
    `fetch_documents`를 부르고, 둘은 각각 별도 프로세스다. 즉 "대기가 끝났는가"를 묻는
    시각은 직전 실행이 실패를 기록한 시각보다 **매번 몇 초 이르다.** 그래서 어긋남이
    우연히 생기는 것이 아니라 항상 생긴다.

    초를 버리면 14:17:08은 14:17:00으로 세어 만기가 14:18:00이 되고, 14:18:00 실행이
    제때 잡는다. 만기는 언제나 분 경계이므로 **같은 분 안에서 다시 시도하는 일은 없다.**
    다만 분 경계 직전(예: 14:17:59)에 시도했다면 실제 간격이 1분보다 짧아질 수 있다 -
    63칸 중 한 칸이 짧아지는 것뿐이고, 파이프라인이 그때까지 끌리는 일 자체가 드물다
    (보통 실행 시작 후 4~10초).

    요약 재시도(`SUMMARY_RETRY_BACKOFF_MINUTES`)도 이 함수를 쓰므로 같이 고쳐진다.
    """
    if not attempts or attempted_at is None:
        return True
    index = min(attempts, len(backoff_minutes)) - 1
    since = attempted_at.replace(second=0, microsecond=0)
    return now >= since + timedelta(minutes=backoff_minutes[index])


def unfetched_targets():
    """요약 대상인데 원문을 아직 못 받은 공시."""
    return Disclosure.objects.filter(
        selection_state=SelectionState.TARGET, raw_fetched=False
    )


def unsummarized_targets():
    """원문은 있는데 요약이 아직 없는 공시."""
    return Disclosure.objects.filter(
        selection_state=SelectionState.TARGET, raw_fetched=True,
        summary__isnull=True,
    ).exclude(raw_content='')


def split_fetch_targets(queryset, now=None):
    """(지금 시도할 것, 대기 중, 상한 도달) 으로 나눈다.

    대기 시간이 시도 횟수마다 달라 SQL 한 줄로 거르기 어렵다. 요약 대상 중
    미확보분은 많아야 수백 건이라 파이썬에서 나누는 편이 읽기 쉽다.
    """
    now = now or timezone.now()
    ready, waiting, stuck = [], 0, 0
    for disclosure in queryset:
        if disclosure.raw_fetch_attempts >= MAX_FETCH_ATTEMPTS:
            stuck += 1
        elif is_retry_due(disclosure.raw_fetch_attempts,
                          disclosure.raw_fetch_attempted_at,
                          RETRY_BACKOFF_MINUTES, now):
            ready.append(disclosure)
        else:
            waiting += 1
    return ready, waiting, stuck


def due_summary_targets(queryset, now=None):
    """실패 후 대기 중이거나 상한에 걸린 공시를 뺀 나머지.

    상한 미만인 것만 DB에서 좁힌 뒤(대부분 여기서 걸러진다) 대기 판정만 파이썬에서 한다.
    """
    now = now or timezone.now()
    return [
        disclosure
        for disclosure in queryset.filter(summary_attempts__lt=MAX_SUMMARY_ATTEMPTS)
        if is_retry_due(disclosure.summary_attempts, disclosure.summary_attempted_at,
                        SUMMARY_RETRY_BACKOFF_MINUTES, now)
    ]


def pending_counts(now=None):
    """지금 곧바로 처리할 수 있는 후속 작업 건수. DART·LLM을 부르지 않는다.

    `대기`·`상한`은 판단 근거로만 쓴다 — 그 둘은 "지금 할 일"이 아니므로
    파이프라인을 다시 돌릴 이유가 되지 않는다.
    """
    now = now or timezone.now()
    fetch_ready, fetch_waiting, fetch_stuck = split_fetch_targets(
        unfetched_targets(), now)
    summary_ready = due_summary_targets(unsummarized_targets(), now)
    return {
        '원문': len(fetch_ready),
        '요약': len(summary_ready),
        '대기': fetch_waiting,
        '상한': fetch_stuck,
    }
