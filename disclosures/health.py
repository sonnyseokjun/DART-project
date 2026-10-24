"""운영 상태 판정 — **"지금 정상인가"의 단일 출처** (10단계, 이슈 #56).

메일 알림(`check_health`)과 관리자 대시보드(`/admin/monitoring/`)가 같은 함수를 쓴다.
판정이 두 곳에 있으면 한쪽만 고쳤을 때 "화면은 정상인데 메일이 온다"가 생긴다.

## 판정 원칙 (PLAN.md 9.6)

**"실패했다"가 아니라 "마지막 성공이 너무 오래됐다"로 본다.** 실패를 기록하는 쪽은 실패
경로마다 기록을 심어야 하고 하나라도 빠지면 조용히 넘어간다. 이 프로젝트의 큰 사고 셋
(전체 폴링 이틀 미실행 · 재시도 사다리 5일 지연 · 1분 칸이 2분마다)이 전부 실패가 아니라
**아예 안 돎**이었고, 그동안 로그는 정상으로 보였다(9.3). 마지막 성공 시각만 보면 어떤
방식으로 멈췄든 잡힌다.

DART·LLM을 부르지 않는다. DB와 `/proc/meminfo`·디스크 사용량만 본다(PLAN.md 12.1).
"""
import os
import shutil
from dataclasses import dataclass
from datetime import datetime, time, timedelta

from django.conf import settings
from django.utils import timezone

from . import ai_budget, retry_policy
from .models import MAX_SUMMARY_ATTEMPTS, Disclosure, JobStatus
from .selection import SelectionState

# --- 임계값 -----------------------------------------------------------------

#: 감지 폴링이 매분 도는 시간대(deploy/crontab의 `* 9-18 * * 1-5`). 그 밖에는 매시 00분이다.
ACTIVE_WEEKDAYS = range(0, 5)   # 월~금
ACTIVE_HOURS = range(9, 19)     # 09:00~18:59

#: 마지막 감지 성공 뒤 **첫 예정 실행 시각부터** 이만큼 지나도 성공이 없으면 이상이다.
#:
#: 마지막 성공 시각에서 바로 재지 않는 이유는 시간대 경계다. 월요일 08:00에 감지가 돌고
#: 09:05에 점검하면 마지막 성공이 65분 전인데, 매분 시간대의 기준(10분)을 그대로 대면
#: 멀쩡한 파이프라인을 이상으로 알린다. 매분 실행은 09:00에 막 시작했을 뿐이다.
#: 예정 실행 시각에서 재면 그 실행이 어느 시간대에 속하는지가 기준을 정한다.
#:
#: 결과적으로 평소에는 마지막 성공부터 약 10분(매분 시간대) · 2시간 10분(그 외)이 된다.
#: 밤에 한 번을 봐주는 이유: 정각 줄은 잠금을 최대 10분 기다린다(pipeline.sh --wait).
#: 한 번 늦은 것을 이상으로 치면 밤마다 오보가 난다.
DETECT_GRACE_ACTIVE = timedelta(minutes=10)
DETECT_GRACE_IDLE = timedelta(hours=1, minutes=10)

#: 하루 1회 작업(07:05 전체 폴링 · 04:00 백업)은 26시간. 하루에 2시간 여유를 둔다.
FULL_POLL_MAX_AGE = timedelta(hours=26)
BACKUP_MAX_AGE = timedelta(hours=26)

#: 요청 후 이 시간이 지나도 요약이 없고 **처리할 수 있는 상태**면 이상이다.
#: 요청 처리는 매분 돌므로(평일 낮 수집 줄 · 그 외 --requests) 정상이면 1~2분이다.
REQUEST_MAX_WAIT = timedelta(minutes=15)

#: "처리할 수 있는 상태"의 판정에 주는 여유. 재시도 만기가 막 지난 건은 바로 다음 분
#: 실행이 처리한다 — 점검이 그보다 먼저 잠금을 잡았다고 이상으로 치면 안 된다.
REQUEST_OVERDUE_GRACE = timedelta(minutes=5)

#: 재시도 상한에 걸린 건은 이 기간 안에 마지막으로 시도한 것만 센다.
#: 상한에 걸린 공시는 사람이 되살리기 전까지 영원히 남는다. 기간 없이 세면 한 번 걸린
#: 뒤로는 "주의"가 풀리지 않아, 다음에 새로 걸린 건이 알림을 받지 못한다.
STUCK_WINDOW = timedelta(days=7)

#: 이번 달 AI 비용이 상한의 이 비율을 넘으면 주의.
AI_BUDGET_WARN_RATIO = 0.8

#: 가용 메모리(MB). 요약 1건이 약 100~125MB를 쓰고, 7단계 실측 최저가 269MB였다(PLAN.md 9.3).
MEMORY_BAD_MB = 150
MEMORY_WARN_MB = 250

#: 디스크 사용률.
DISK_BAD_RATIO = 0.90
DISK_WARN_RATIO = 0.80

#: 점검 명령 자신이 이보다 오래 돌지 않았으면 대시보드에 주의로 띄운다(10분마다 돈다).
#: 메일 판정에는 넣지 않는다 — 점검이 멈췄으면 그 메일을 보낼 주체도 멈춰 있다.
#: 그 경우는 외부 감시(UptimeRobot)와 운영자가 대시보드를 열었을 때 잡는다.
HEALTH_CHECK_MAX_AGE = timedelta(minutes=25)

#: 일이 있었던 실행 이력(PipelineRun)의 보관 기간. check_health가 하루 한 번 지운다.
PIPELINE_RUN_RETENTION = timedelta(days=90)

MEMINFO_PATH = '/proc/meminfo'

LEVELS = ('ok', 'warn', 'bad', 'unknown')
_SEVERITY = {'unknown': -1, 'ok': 0, 'warn': 1, 'bad': 2}


@dataclass(frozen=True)
class Check:
    """점검 항목 하나의 판정. 대시보드와 메일이 이것을 그대로 쓴다.

    level이 `unknown`이면 판정할 재료가 없다는 뜻이다(측정 실패 · 기록 없음).
    알리지 않는다 — 모르는 것을 이상으로 치면 개발 PC처럼 `/proc`이 없는 곳에서 늘 울린다.
    """

    key: str
    title: str
    level: str
    summary: str
    detail: str = ''


# --- 공용 ---------------------------------------------------------------------

def format_age(delta):
    """'3분' · '2시간 5분' · '1일 3시간'. 1분 미만은 '1분 미만'."""
    minutes = int(delta.total_seconds() // 60)
    if minutes < 1:
        return '1분 미만'
    days, rest = divmod(minutes, 60 * 24)
    hours, minutes = divmod(rest, 60)
    if days:
        return f'{days}일 {hours}시간' if hours else f'{days}일'
    if hours:
        return f'{hours}시간 {minutes}분' if minutes else f'{hours}시간'
    return f'{minutes}분'


def format_local(dt):
    return f'{timezone.localtime(dt):%m-%d %H:%M}'


def worst_level(levels):
    """가장 나쁜 level. `unknown`은 무시하고, 남는 것이 없으면 'ok'."""
    known = [level for level in levels if level != 'unknown']
    return max(known, key=_SEVERITY.__getitem__, default='ok')


def job_statuses():
    """종류별 JobStatus를 {kind: JobStatus}로. 없는 종류는 빠진다."""
    return {status.kind: status for status in JobStatus.objects.all()}


def _age_basis(status):
    """얼마나 오래됐는지 재는 기준 시각. 성공이 한 번도 없으면 행이 생긴 때부터 잰다.

    성공 기록이 없다고 "판정 불가"로 두면, 배포 후 한 번도 돈 적 없는 백업이 영원히
    알림 없이 넘어간다. 행은 check_health가 첫 실행에서 만들어 둔다(ensure_job_rows).
    """
    return status.last_success_at or status.created_at


def ensure_job_rows():
    """모든 종류의 JobStatus 행을 만들어 둔다. 기록이 없을 때의 기준 시각이 여기서 생긴다."""
    for kind in JobStatus.Kind.values:
        JobStatus.objects.get_or_create(kind=kind)


def _no_record(key, title):
    return Check(key, title, 'unknown', '기록 없음',
                 '점검 명령이 아직 한 번도 돌지 않았다 — 첫 점검에서 기준 시각이 생긴다')


def _success_summary(status, now):
    if status.last_success_at:
        return (f'마지막 성공 {format_age(now - status.last_success_at)} 전 '
                f'({format_local(status.last_success_at)})')
    return f'성공 기록 없음 (기록 시작 {format_local(status.created_at)})'


def _failure_detail(status):
    """마지막 성공 뒤에 실패가 있었으면 그 내용을 덧붙인다. 판정에는 쓰지 않는다(보조)."""
    if status.last_failure_at and (
            not status.last_success_at or status.last_failure_at > status.last_success_at):
        return f'마지막 실패 {format_local(status.last_failure_at)} · {status.last_message}'
    return ''


# --- 감지 ---------------------------------------------------------------------

def is_active_minute(local_dt):
    """이 시각(로컬)이 감지 폴링이 매분 도는 시간대인가."""
    return local_dt.weekday() in ACTIVE_WEEKDAYS and local_dt.hour in ACTIVE_HOURS


def next_detect_slot(after):
    """`after` 다음에 예정된 감지 실행 시각(로컬, 분 단위).

    cron 일정(deploy/crontab)을 그대로 옮긴 것이다 — 매분 시간대에는 다음 분,
    그 밖에는 다음 정각. 일정을 바꾸면 여기와 ACTIVE_* 를 함께 고친다.
    """
    candidate = timezone.localtime(after).replace(second=0, microsecond=0) + timedelta(minutes=1)
    while True:
        if is_active_minute(candidate) or candidate.minute == 0:
            return candidate
        candidate = (candidate + timedelta(hours=1)).replace(minute=0)


def next_active_slot(after):
    """`after` 다음에 오는 첫 매분 시간대의 분. 다음 분이 아직 그 안이면 다음 분, 아니면 다음 평일 09:00."""
    local = timezone.localtime(after)
    candidate = local.replace(second=0, microsecond=0) + timedelta(minutes=1)
    if is_active_minute(candidate):
        return candidate
    for offset in range(8):
        day = local.date() + timedelta(days=offset)
        start = timezone.make_aware(datetime.combine(day, time(ACTIVE_HOURS.start)))
        if day.weekday() in ACTIVE_WEEKDAYS and start > local:
            return start
    raise AssertionError('평일이 없는 일주일')  # pragma: no cover


def detect_deadline(last):
    """마지막 감지 성공이 `last`일 때, 이 시각까지 새 성공이 없으면 이상이다.

    다음 예정 실행 + 그 실행이 속한 시간대의 여유. 첫 예정 실행이 밤 정각이어도 그 사이에
    매분 시간대가 시작되면 그쪽 기한이 더 이를 수 있어 둘 중 이른 쪽을 쓴다.
    (예: 08:00 성공 → 09:00 정각 실행은 매분 시간대에 속하므로 09:10.)
    """
    first = next_detect_slot(last)
    if is_active_minute(first):
        return first + DETECT_GRACE_ACTIVE
    return min(first + DETECT_GRACE_IDLE, next_active_slot(last) + DETECT_GRACE_ACTIVE)


def check_detect(statuses, now):
    status = statuses.get(JobStatus.Kind.DETECT)
    title = '공시 감지'
    if status is None:
        return _no_record('detect', title)
    deadline = detect_deadline(_age_basis(status))
    summary = _success_summary(status, now)
    detail = _failure_detail(status)
    if now > deadline:
        return Check('detect', title, 'bad', summary,
                     f'기한 {format_local(deadline)}을 넘겼다' + (f' · {detail}' if detail else ''))
    return Check('detect', title, 'ok', summary, detail)


# --- 하루 1회 작업 ---------------------------------------------------------------

def _check_daily(key, title, kind, max_age, statuses, now):
    status = statuses.get(kind)
    if status is None:
        return _no_record(key, title)
    summary = _success_summary(status, now)
    detail = _failure_detail(status)
    if now - _age_basis(status) > max_age:
        return Check(key, title, 'bad', summary,
                     f'{format_age(max_age)} 넘게 성공이 없다' + (f' · {detail}' if detail else ''))
    return Check(key, title, 'ok', summary, detail)


def check_full_poll(statuses, now):
    return _check_daily('full_poll', '전체 폴링', JobStatus.Kind.FULL_POLL,
                        FULL_POLL_MAX_AGE, statuses, now)


def check_backup(statuses, now):
    return _check_daily('backup', '백업', JobStatus.Kind.BACKUP, BACKUP_MAX_AGE, statuses, now)


# --- 요청 처리 -------------------------------------------------------------------

@dataclass
class RequestBacklog:
    """요약을 요청받았는데 아직 요약이 없는 공시를 상태별로 나눈 것."""

    total: int            # 요청됐고 요약이 없는 공시 전체(상한 걸린 것 포함)
    fetch_waiting: int    # 원문 미공개로 재시도 사다리를 기다리는 중 — 정상
    summary_waiting: int  # 요약 실패 후 재시도 대기 중 — 정상
    budget_held: int      # 월 상한에 닿아 다음 달로 미뤄진 것 — ai_budget이 따로 잡는다
    stuck: int            # 재시도 상한에 걸린 것(기간 무관) — 다시 시도하지 않는다
    overdue: list         # 처리할 수 있는데 REQUEST_MAX_WAIT 넘게 그대로인 공시


def request_backlog(now):
    """요청 대기열을 나눈다. `requests` 판정과 대시보드의 요약 패널이 함께 쓴다.

    "처리할 수 있는데 오래 그대로인 것"만 이상이다. 원문이 아직 DART에 안 올라와 사다리를
    기다리는 건은 우리가 할 수 있는 일이 없으므로 정상(대기)이다 — 사다리가 24시간이라
    이걸 이상으로 치면 원문이 늦게 올라오는 공시마다 알림이 온다.

    처리 가능 여부는 retry_policy의 만기 판정을 **REQUEST_OVERDUE_GRACE만큼 과거 시각으로**
    물어 정한다. "5분 전에 이미 만기였다"는 뜻이라, 막 만기가 된 건이 다음 분 실행을
    기다리는 동안 오보가 나지 않는다.
    """
    exhausted = ai_budget.is_exhausted(now)
    cutoff = now - REQUEST_MAX_WAIT
    probe = now - REQUEST_OVERDUE_GRACE
    backlog = RequestBacklog(0, 0, 0, 0, 0, [])

    for disclosure in retry_policy.unfetched_targets().select_related('company'):
        backlog.total += 1
        if disclosure.raw_fetch_attempts >= retry_policy.MAX_FETCH_ATTEMPTS:
            backlog.stuck += 1
        elif not retry_policy.is_retry_due(
                disclosure.raw_fetch_attempts, disclosure.raw_fetch_attempted_at,
                retry_policy.RETRY_BACKOFF_MINUTES, probe):
            backlog.fetch_waiting += 1
        elif disclosure.summary_requested_at <= cutoff:
            backlog.overdue.append(disclosure)

    for disclosure in retry_policy.unsummarized_targets().select_related('company'):
        backlog.total += 1
        if disclosure.summary_attempts >= MAX_SUMMARY_ATTEMPTS:
            backlog.stuck += 1
        elif exhausted:
            backlog.budget_held += 1
        elif not retry_policy.is_retry_due(
                disclosure.summary_attempts, disclosure.summary_attempted_at,
                retry_policy.SUMMARY_RETRY_BACKOFF_MINUTES, probe):
            backlog.summary_waiting += 1
        elif disclosure.summary_requested_at <= cutoff:
            backlog.overdue.append(disclosure)

    backlog.overdue.sort(key=lambda d: d.summary_requested_at)
    return backlog


def check_requests(now, backlog=None):
    backlog = backlog or request_backlog(now)
    title = '요약 요청 처리'
    pending = backlog.total - backlog.stuck
    if backlog.overdue:
        oldest = backlog.overdue[0]
        shown = ', '.join(d.rcept_no for d in backlog.overdue[:5])
        more = f' 외 {len(backlog.overdue) - 5}건' if len(backlog.overdue) > 5 else ''
        return Check(
            'requests', title, 'bad',
            f'처리할 수 있는데 {format_age(REQUEST_MAX_WAIT)} 넘게 요약이 없는 요청 '
            f'{len(backlog.overdue)}건',
            f'가장 오래된 요청 {format_age(now - oldest.summary_requested_at)} 전 · {shown}{more}')
    if not pending:
        return Check('requests', title, 'ok', '대기 중인 요청 없음')
    held = [f'{label} {count}건' for label, count in (
        ('원문 미공개 대기', backlog.fetch_waiting),
        ('요약 재시도 대기', backlog.summary_waiting),
        ('월 상한으로 보류', backlog.budget_held),
    ) if count]
    return Check('requests', title, 'ok', f'처리 중인 요청 {pending}건',
                 ' · '.join(held))


# --- 재시도 상한 -----------------------------------------------------------------

def stuck_counts(now, window=STUCK_WINDOW):
    """최근 `window` 안에 재시도 상한에 걸린 요청 건수 (원문, 요약, 빈 원문)."""
    since = now - window
    fetch = retry_policy.unfetched_targets().filter(
        raw_fetch_attempts__gte=retry_policy.MAX_FETCH_ATTEMPTS,
        raw_fetch_attempted_at__gte=since).count()
    summary = retry_policy.unsummarized_targets().filter(
        summary_attempts__gte=MAX_SUMMARY_ATTEMPTS,
        summary_attempted_at__gte=since).count()
    # 원문은 받았는데 전처리 결과가 비어 요약 대상에서 빠진 공시. unsummarized_targets가
    # 거르므로 재시도도 상한도 없이 화면에 "정리 중"으로 남는다 — 사람이 봐야 한다.
    empty = Disclosure.objects.filter(
        selection_state=SelectionState.TARGET, raw_fetched=True,
        raw_content='', summary__isnull=True,
        summary_requested_at__gte=since).count()
    return fetch, summary, empty


def check_stuck(now):
    fetch, summary, empty = stuck_counts(now)
    title = '재시도 포기'
    total = fetch + summary + empty
    if not total:
        return Check('stuck', title, 'ok',
                     f'최근 {STUCK_WINDOW.days}일 재시도 상한에 걸린 요청 없음')
    parts = [f'{label} {count}건' for label, count in (
        ('원문', fetch), ('요약', summary), ('빈 원문', empty)) if count]
    return Check('stuck', title, 'warn',
                 f'최근 {STUCK_WINDOW.days}일 재시도를 포기한 요청 {total}건',
                 ' · '.join(parts) + ' — fetch_documents --stuck으로 원인을 확인한다')


# --- AI 비용 ---------------------------------------------------------------------

def check_ai_budget(now):
    spent, budget = ai_budget.spent_this_month(now), ai_budget.monthly_budget()
    ratio = spent / budget if budget else 1.0
    summary = f'이번 달 ${spent:.2f} / 상한 ${budget:.2f} ({ratio * 100:.0f}%)'
    if ai_budget.is_exhausted(now):
        return Check('ai_budget', 'AI 비용', 'bad', summary,
                     '상한 소진 — 새 요약 요청을 받지 않고, 받은 요청은 다음 달 1일에 처리한다')
    if ratio >= AI_BUDGET_WARN_RATIO:
        return Check('ai_budget', 'AI 비용', 'warn', summary,
                     f'상한의 {AI_BUDGET_WARN_RATIO * 100:.0f}%를 넘었다')
    return Check('ai_budget', 'AI 비용', 'ok', summary)


# --- 서버 ------------------------------------------------------------------------

def memory_available_mb(path=MEMINFO_PATH):
    """`/proc/meminfo`의 MemAvailable(MB). 읽지 못하면 None.

    컨테이너에 메모리 제한이 없어 호스트 값이 그대로 보인다(PLAN.md 9.6). `free -m`의
    available 열이 같은 값이다(pipeline.sh의 메모리 표본과 같은 기준).
    """
    try:
        with open(path, encoding='ascii') as meminfo:
            for line in meminfo:
                if line.startswith('MemAvailable:'):
                    return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def disk_usage(path=None):
    """DB가 있는 디스크의 (사용 바이트, 전체 바이트). 읽지 못하면 None."""
    path = path or os.path.dirname(os.fspath(settings.DATABASES['default']['NAME']))
    try:
        usage = shutil.disk_usage(path or '.')
    except OSError:
        return None
    return usage.used, usage.total


def check_memory():
    available = memory_available_mb()
    title = '메모리'
    if available is None:
        return Check('memory', title, 'unknown', '측정 불가', f'{MEMINFO_PATH}을 읽지 못했다')
    summary = f'가용 {available:,}MB'
    if available < MEMORY_BAD_MB:
        return Check('memory', title, 'bad', summary, f'{MEMORY_BAD_MB}MB 미만')
    if available < MEMORY_WARN_MB:
        return Check('memory', title, 'warn', summary, f'{MEMORY_WARN_MB}MB 미만')
    return Check('memory', title, 'ok', summary)


def format_bytes(size):
    gb = size / 1024 ** 3
    return f'{gb:.1f}GB'


def check_disk():
    usage = disk_usage()
    title = '디스크'
    if usage is None:
        return Check('disk', title, 'unknown', '측정 불가', 'DB 디렉터리의 디스크 사용량을 읽지 못했다')
    used, total = usage
    ratio = used / total if total else 0.0
    summary = f'사용 {ratio * 100:.0f}% ({format_bytes(used)} / {format_bytes(total)})'
    if ratio > DISK_BAD_RATIO:
        return Check('disk', title, 'bad', summary, f'{DISK_BAD_RATIO * 100:.0f}% 초과')
    if ratio > DISK_WARN_RATIO:
        return Check('disk', title, 'warn', summary, f'{DISK_WARN_RATIO * 100:.0f}% 초과')
    return Check('disk', title, 'ok', summary)


# --- 전체 ------------------------------------------------------------------------

def run_checks(now=None, *, statuses=None, backlog=None):
    """모든 점검 항목을 00_input의 표 순서대로 판정한다."""
    now = now or timezone.now()
    statuses = job_statuses() if statuses is None else statuses
    return [
        check_detect(statuses, now),
        check_full_poll(statuses, now),
        check_backup(statuses, now),
        check_requests(now, backlog),
        check_stuck(now),
        check_ai_budget(now),
        check_memory(),
        check_disk(),
    ]


def overall_level(checks):
    return worst_level(check.level for check in checks)
