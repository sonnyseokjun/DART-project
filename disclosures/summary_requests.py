"""요약 요청 정책 — "AI 요약 보기" 버튼을 누르면 무슨 일이 일어나는가 (이슈 #44).

버튼은 **요청만 기록한다.** 웹 요청 안에서 AI를 부르지 않는다. 요약 1건이 약 20초
걸리고 gunicorn 워커가 2개라, 그 자리에서 부르면 두 명이 동시에 누를 때 사이트 전체가
멈춘다. 다음 파이프라인 실행(1~2분 안)이 원문을 받고 요약한다.

## 안전장치

- **공시당 요약 1회** — 요청은 공시에 표시만 한다(`summary_requested_at`). 여러 사람이
  눌러도 파이프라인은 한 번만 만든다(DisclosureSummary OneToOne).
- **계정당 하루 20번** — 한 사람이 한 달 예산을 혼자 쓰지 못하게 한다.
- **월 비용 상한** — 닿으면 새 요청을 받지 않는다(ai_budget). 이미 받은 요청은 남는다.
- **실패한 공시는 다시 요청할 수 없다** — 같은 입력으로 다시 부르면 돈만 나가고 결과는
  같다(`Disclosure.summary_state == 'failed'`).

하루 제한은 watchlist와 같은 이유로 **만든 뒤 같은 트랜잭션에서 센다.**
"""
from django.db import transaction
from django.utils import timezone

from . import ai_budget
from .models import SummaryRequest
from .watchlist import today_start

MAX_REQUESTS_PER_DAY = 20


class RequestRefused(Exception):
    """요청을 받을 수 없다. 메시지는 화면에 그대로 보여준다."""


def requests_today(user, now=None):
    return SummaryRequest.objects.filter(
        user=user, created_at__gte=today_start(now or timezone.now())).count()


def request_summary(user, disclosure, now=None):
    """요약을 요청한다. 이미 요청됐거나 요약이 있으면 아무것도 하지 않는다(횟수도 안 센다).

    반환: 이번 호출로 새로 요청됐으면 True.
    """
    now = now or timezone.now()
    state = disclosure.summary_state
    if state in ('ready', 'queued'):
        return False
    if state == 'none':
        raise RequestRefused('요약 대상이 아닌 공시입니다. DART 원문에서 확인해 주세요.')
    if state == 'failed':
        raise RequestRefused('이 공시는 요약을 만들지 못했습니다. DART 원문에서 확인해 주세요.')
    if ai_budget.is_exhausted(now):
        raise RequestRefused(
            '이번 달 AI 요약 한도에 도달했습니다. 다음 달 1일부터 다시 요청할 수 있습니다.')

    with transaction.atomic():
        SummaryRequest.objects.get_or_create(user=user, disclosure=disclosure)
        if requests_today(user, now) > MAX_REQUESTS_PER_DAY:
            raise RequestRefused(
                f'AI 요약은 하루 {MAX_REQUESTS_PER_DAY}번까지 요청할 수 있습니다. '
                '내일 다시 시도해 주세요.')
        # 다른 사람이 먼저 요청했을 수 있다. 처음 요청한 시각을 지킨다(먼저 온 것부터 처리).
        updated = type(disclosure).objects.filter(
            pk=disclosure.pk, summary_requested_at__isnull=True,
        ).update(summary_requested_at=now)
    disclosure.refresh_from_db(fields=['summary_requested_at'])
    return bool(updated)
