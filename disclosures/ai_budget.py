"""월 AI 비용 상한 — **요약에 돈이 나가는지를 정하는 단일 출처** (이슈 #44).

운영자가 모든 비용을 부담하는 개인 프로젝트라, 사용자가 무엇을 하든 한 달 AI 비용이
정한 금액을 넘지 않아야 한다. 장부는 `AiUsage`(AI 응답마다 한 줄)이고, 이번 달 합계가
상한에 닿으면 새 요약을 만들지 않는다.

## 상한에 닿으면

- 새 "AI 요약 보기" 요청을 받지 않는다(화면에 안내).
- 이미 받은 요청은 **지우지 않고 다음 달 1일에 처리한다.** 사용자가 다시 누를 필요가 없다.
- 파이프라인은 요약 단계를 건너뛴다. 대기 확인(`pending_work`)도 요약을 "할 일"로
  세지 않는다 — 세면 한도가 풀릴 때까지 매분 뒷단계가 헛돈다.

## 왜 상한을 1만 원보다 낮게 잡나

비용은 토큰 × 단가로 계산한 **추정치**다. OpenAI 청구와 조금 다를 수 있고, 한 건을
시작하기 전에는 그 건의 비용을 정확히 알 수 없다. 그래서 기본 상한을 $6.5(약 9,100원)로
두고, 한 건을 시작하기 전에 "이 건의 추정 비용 × 2"가 남아 있는지 본다
(교정 재생성까지 가면 호출이 늘어나기 때문이다). 최종 안전장치는 OpenAI 선불 잔액이다.
"""
from datetime import datetime, time

from django.conf import settings
from django.db.models import Sum
from django.utils import timezone

from .models import AiUsage

#: 한 건을 시작하기 전에 남아 있어야 하는 여유 = 그 건의 추정 비용 × 이 값.
#: 최초 호출 + 교정 재생성 1회를 감당하는 크기다(review_policy.MAX_REGENERATION_ATTEMPTS).
START_MARGIN_MULTIPLIER = 2


def month_start(now=None):
    """이번 달 1일 0시(한국 시간). 상한은 달력 기준 한 달이다."""
    local = timezone.localtime(now or timezone.now())
    return timezone.make_aware(datetime.combine(local.date().replace(day=1), time.min))


def monthly_budget():
    return settings.AI_MONTHLY_BUDGET_USD


def spent_this_month(now=None):
    total = AiUsage.objects.filter(created_at__gte=month_start(now)).aggregate(
        total=Sum('cost_usd'))['total']
    return total or 0.0


def remaining(now=None):
    return max(monthly_budget() - spent_this_month(now), 0.0)


def is_exhausted(now=None):
    """이번 달 상한에 닿았는가. 화면(요청 버튼)과 파이프라인이 모두 이것을 본다."""
    return remaining(now) <= 0


def can_afford(estimated_cost, now=None):
    """이 추정 비용의 요약 1건을 지금 시작해도 되는가."""
    return remaining(now) >= estimated_cost * START_MARGIN_MULTIPLIER


def record(*, usage, model_name, cost_usd, disclosure=None):
    """AI 응답 1건의 비용을 장부에 적는다. summarizer가 응답을 받을 때마다 부른다."""
    return AiUsage.objects.create(
        disclosure=disclosure, model_name=(model_name or '')[:50],
        input_tokens=usage.get('input_tokens', 0),
        output_tokens=usage.get('output_tokens', 0),
        cached_tokens=usage.get('cached_tokens', 0),
        cost_usd=cost_usd,
    )
