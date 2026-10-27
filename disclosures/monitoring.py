"""운영 모니터링 대시보드 `/admin/monitoring/` (10단계, 이슈 #56).

메일은 "무엇이 깨졌나"만 알린다. "평소와 비교해 어떤가"는 이 화면이 답한다(PLAN.md 9.6).

- 판정은 `health.py`가 한다. 여기서는 숫자를 모아 문자열로 다듬기만 한다.
- DART·LLM을 부르지 않는다(PLAN.md 12.1). DB와 `/proc/meminfo`·디스크만 본다.
- **사용자 개인정보는 숫자로만.** 누가 어느 기업을 담았는지 나열하지 않는다.
- `/admin/*`의 IP 화이트리스트(Caddy)와 staff 확인(`admin_view`)을 그대로 물려받는다.

뷰 ↔ 템플릿 계약은 `_workspace/00_input.md`에 있다. 키 이름을 바꾸면 템플릿이 조용히
빈 칸을 그리므로 계약 쪽을 먼저 고친다.
"""
from datetime import timedelta
from statistics import median

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.db.models import F, Q, Sum
from django.db.models.functions import TruncDate
from django.shortcuts import render
from django.utils import timezone

from . import ai_budget, health
from .models import (
    AiUsage, Company, Disclosure, DisclosureSummary, JobStatus, PipelineRun, Watch,
)
from .watchlist import today_start

#: 요청→완료 중앙값을 낼 기간.
TURNAROUND_DAYS = 30
#: 일별 AI 비용 막대의 기간.
DAILY_COST_DAYS = 30
#: 최근 실행 표의 행 수.
RECENT_RUNS = 20
#: "최근 접속"을 셀 기간.
ACTIVE_USER_DAYS = 7


def _item(label, value, level=None, hint=''):
    return {'label': label, 'value': value, 'level': level, 'hint': hint}


def _job_item(label, status, now, level=None, hint=''):
    if status is None or status.last_success_at is None:
        return _item(label, '기록 없음', level, hint)
    return _item(label, f'{health.format_age(now - status.last_success_at)} 전',
                 level, f'{health.format_local(status.last_success_at)}' + (f' · {hint}' if hint else ''))


def _format_duration(seconds):
    seconds = int(seconds)
    if seconds < 60:
        return f'{seconds}초'
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f'{minutes}분 {seconds}초' if seconds else f'{minutes}분'
    return health.format_age(timedelta(minutes=minutes))


def service_items(now):
    # 회원은 카카오 로그인 사용자다. 검수자(staff) 계정은 세지 않는다.
    members = get_user_model().objects.filter(is_staff=False, is_superuser=False)
    total = members.count()
    joined_today = members.filter(date_joined__gte=today_start(now)).count()
    active = members.filter(last_login__gte=now - timedelta(days=ACTIVE_USER_DAYS)).count()
    watches = Watch.objects.count()
    tracked = Company.objects.filter(is_active=True).count()
    return [
        _item('회원 수', f'{total:,}명', hint=f'오늘 신규 {joined_today:,}명'),
        _item(f'최근 {ACTIVE_USER_DAYS}일 접속', f'{active:,}명', hint='마지막 로그인 기준'),
        _item('관심 기업 수', f'{watches:,}건', hint='회원들이 담은 관심 기업 합계'),
        _item('추적 기업 수', f'{tracked:,}곳', hint='한 명 이상이 고른 기업'),
    ]


def pipeline_items(now, statuses, checks):
    health_check = statuses.get(JobStatus.Kind.HEALTH_CHECK)
    if health_check is None or health_check.last_success_at is None:
        watchdog = 'warn'
    else:
        watchdog = 'ok' if now - health_check.last_success_at <= health.HEALTH_CHECK_MAX_AGE else 'warn'
    detected_today = Disclosure.objects.filter(created_at__gte=today_start(now)).count()
    return [
        _job_item('마지막 감지', statuses.get(JobStatus.Kind.DETECT), now, checks['detect'].level),
        _job_item('마지막 전체 폴링', statuses.get(JobStatus.Kind.FULL_POLL), now,
                  checks['full_poll'].level, '매일 07:05'),
        _item('오늘 감지 공시', f'{detected_today:,}건', hint='추적 기업 공시 · 백필 포함'),
        _job_item('마지막 점검', health_check, now, watchdog,
                  f'10분마다 · {health.format_age(health.HEALTH_CHECK_MAX_AGE)} 넘으면 주의'),
    ]


def turnaround_seconds(now, days=TURNAROUND_DAYS):
    """최근 `days`일 동안 만들어진 요약의 요청→완료 시간(초) 목록."""
    # 요청보다 먼저 만들어진 요약(8단계 전 자동 요약을 나중에 누가 요청한 경우)은 뺀다.
    rows = DisclosureSummary.objects.filter(
        created_at__gte=now - timedelta(days=days),
        disclosure__summary_requested_at__isnull=False,
    ).filter(created_at__gte=F('disclosure__summary_requested_at')).values_list('created_at', 'disclosure__summary_requested_at')
    return [(created - requested).total_seconds() for created, requested in rows]


def summary_items(now, backlog, checks):
    fetch_stuck, summary_stuck, empty = health.stuck_counts(now)
    durations = turnaround_seconds(now)
    warned = DisclosureSummary.objects.exclude(review_warnings=[]).count()
    # admin.NeedsReviewFilter와 같은 조건이다(DisclosureSummary.needs_review).
    unreviewed = DisclosureSummary.objects.filter(
        Q(is_reviewed=False) & (~Q(disclosure__review_category='') | ~Q(review_warnings=[]))
    ).count()
    pending = backlog.total - backlog.stuck
    return [
        _item('요청 대기', f'{pending:,}건', checks['requests'].level,
              f'처리 지연 {len(backlog.overdue)}건' if backlog.overdue else '요약이 아직 없는 요청'),
        _item('원문 대기(사다리)', f'{backlog.fetch_waiting:,}건',
              hint='DART 원문 미공개 — 재시도 대기는 정상'),
        _item('포기', f'{fetch_stuck + summary_stuck + empty:,}건', checks['stuck'].level,
              f'최근 {health.STUCK_WINDOW.days}일 · 전체 {backlog.stuck:,}건'),
        _item('요청→완료 중앙값', _format_duration(median(durations)) if durations else '—',
              hint=f'최근 {TURNAROUND_DAYS}일 {len(durations):,}건'),
        _item('경고 붙은 요약', f'{warned:,}건', hint='자동 검증 경고'),
        _item('미검수', f'{unreviewed:,}건', hint='검수 큐(검수 필요 필터)'),
    ]


def cost_items(now, checks):
    spent, budget = ai_budget.spent_this_month(now), ai_budget.monthly_budget()
    ratio = spent / budget if budget else 1.0
    return [
        _item('이번 달 / 상한', f'${spent:.2f} / ${budget:.2f}', checks['ai_budget'].level,
              f'{ratio * 100:.0f}% · 추정치'),
        _item('남은 금액', f'${ai_budget.remaining(now):.2f}', hint='다음 달 1일에 초기화'),
    ]


def server_items(now, statuses, checks):
    available = health.memory_available_mb()
    usage = health.disk_usage()
    if usage:
        used, total = usage
        disk_value = f'{used / total * 100:.0f}%' if total else '—'
        disk_hint = f'{health.format_bytes(used)} / {health.format_bytes(total)}'
    else:
        disk_value, disk_hint = '측정 불가', ''
    return [
        _item('메모리 가용', f'{available:,}MB' if available is not None else '측정 불가',
              checks['memory'].level, f'주의 {health.MEMORY_WARN_MB}MB · 이상 {health.MEMORY_BAD_MB}MB 미만'),
        _item('디스크', disk_value, checks['disk'].level, disk_hint),
        _job_item('마지막 백업', statuses.get(JobStatus.Kind.BACKUP), now,
                  checks['backup'].level, '매일 04:00'),
    ]


def daily_cost(now, days=DAILY_COST_DAYS):
    """최근 `days`일(오늘 포함)의 일별 AI 비용. 쓰지 않은 날도 0으로 채운다."""
    today = timezone.localdate(now)
    first = today - timedelta(days=days - 1)
    totals = dict(
        AiUsage.objects.filter(created_at__gte=today_start(now) - timedelta(days=days - 1))
        .annotate(day=TruncDate('created_at', tzinfo=timezone.get_current_timezone()))
        .values('day').annotate(total=Sum('cost_usd')).values_list('day', 'total')
    )
    rows = [(first + timedelta(days=i), totals.get(first + timedelta(days=i)) or 0.0)
            for i in range(days)]
    peak = max((cost for _, cost in rows), default=0.0)
    return [{'date': day, 'cost_usd': cost, 'ratio': cost / peak if peak else 0.0}
            for day, cost in rows]


def ordered_jobs(statuses):
    """종류 순서대로. 기록이 없는 종류는 저장하지 않은 빈 행으로 채워 '기록 없음'을 보이게 한다."""
    return [statuses.get(kind) or JobStatus(kind=kind) for kind in JobStatus.Kind.values]


def build_context(now=None):
    now = now or timezone.now()
    statuses = health.job_statuses()
    backlog = health.request_backlog(now)
    checks = health.run_checks(now, statuses=statuses, backlog=backlog)
    by_key = {check.key: check for check in checks}
    return {
        'checked_at': now,
        'overall': health.overall_level(checks),
        'checks': checks,
        'sections': [
            {'key': 'service', 'title': '서비스', 'items': service_items(now)},
            {'key': 'pipeline', 'title': '파이프라인', 'items': pipeline_items(now, statuses, by_key)},
            {'key': 'summary', 'title': '요약', 'items': summary_items(now, backlog, by_key)},
            {'key': 'cost', 'title': '비용', 'items': cost_items(now, by_key)},
            {'key': 'server', 'title': '서버', 'items': server_items(now, statuses, by_key)},
        ],
        'daily_cost': daily_cost(now),
        'recent_runs': list(PipelineRun.objects.all()[:RECENT_RUNS]),
        'jobs': ordered_jobs(statuses),
    }


def dashboard(request):
    """`admin.site.admin_view`로 감싸 config/urls.py에 건다 — staff만 들어온다."""
    context = {
        **admin.site.each_context(request),
        'title': '운영 모니터링',
        **build_context(),
    }
    return render(request, 'admin/monitoring.html', context)
