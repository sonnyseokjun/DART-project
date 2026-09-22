"""관심 기업 정책 — **추적 대상을 누가 정하는가의 단일 출처** (이슈 #44).

8단계부터 수집 대상(`Company.is_active`)은 운영자가 아니라 사용자가 정한다.
한 명 이상이 관심 기업으로 고른 기업만 추적하고, 마지막 한 명이 빼면 추적을 멈춘다.
이미 쌓인 공시와 요약은 지우지 않는다 — 누가 다시 고르면 비용 없이 그대로 보인다.

## 제한 두 가지와 그 이유

- **계정당 관심 기업 10곳** — 한 계정의 남용이 다른 사용자에게 번지지 않게 한다.
- **계정당 하루 추가 20번** — 10곳 제한만 있으면 "추가 → 삭제 → 다른 기업 추가"를
  끝없이 반복할 수 있다. 새로 추적하는 기업마다 백필이 DART를 공시유형 수만큼(10회)
  부르므로, 반복을 막지 않으면 **모든 사용자가 함께 쓰는 DART 하루 한도**가 닳는다.

## 제한은 "만든 뒤 센다"

먼저 세고 만들면 거의 동시에 들어온 두 요청이 둘 다 9곳을 보고 통과해 11곳이 된다.
SQLite는 쓰기를 한 줄로 세우므로, **만든 뒤 같은 트랜잭션 안에서 세면** 뒤에 온
요청은 앞 요청이 커밋한 결과까지 본다. 넘치면 예외로 트랜잭션째 되돌린다.
"""
from datetime import datetime, time, timedelta

from django.db import transaction
from django.db.models.signals import post_delete
from django.dispatch import receiver
from django.utils import timezone

from .models import Company, ListedCorp, Sector, Watch, WatchAddition

MAX_WATCHES_PER_USER = 10
MAX_ADDITIONS_PER_DAY = 20

#: 새로 추적을 시작한 기업의 공시를 며칠 전까지 채워 넣을지. 요약은 만들지 않는다.
BACKFILL_DAYS = 30

#: 추적이 이보다 짧게 끊겼다가 돌아온 기업은 백필하지 않는다. 그 사이의 공시는
#: 매일 07:05 전체 폴링(최근 2일, deploy/crontab)이 이미 받았다.
BACKFILL_UNNEEDED_WITHIN = timedelta(days=1)

#: 사용자가 추가한 기업이 들어가는 섹터. 업종 자동 분류는 후속 작업이다(PLAN.md 9.4).
DEFAULT_SECTOR_SLUG = 'etc'
DEFAULT_SECTOR_NAME = '기타'


class WatchLimitError(Exception):
    """제한에 걸렸다. 메시지는 화면에 그대로 보여준다."""


def today_start(now):
    local = timezone.localtime(now)
    return timezone.make_aware(datetime.combine(local.date(), time.min))


def additions_today(user, now=None):
    now = now or timezone.now()
    return WatchAddition.objects.filter(
        user=user, created_at__gte=today_start(now)).count()


def needs_backfill(company, now):
    """다시 추적을 시작할 때 최근 공시를 채워 넣어야 하는가."""
    if company.untracked_at is None:
        # 추적을 멈춘 적이 없다 = 이번에 처음 추적한다(새로 만든 기업).
        return company.backfilled_at is None
    return now - company.untracked_at >= BACKFILL_UNNEEDED_WITHIN


def _start_tracking(company, now):
    if company.is_active:
        return
    company.is_active = True
    if needs_backfill(company, now):
        company.backfill_requested_at = now
        company.backfill_attempts = 0
    company.untracked_at = None
    company.save(update_fields=[
        'is_active', 'backfill_requested_at', 'backfill_attempts', 'untracked_at',
    ])


def stop_tracking_if_unwatched(company, now=None):
    """아무도 고르지 않은 기업의 추적을 멈춘다. 쌓인 데이터는 그대로 둔다."""
    if not company.is_active or Watch.objects.filter(company=company).exists():
        return
    company.is_active = False
    company.untracked_at = now or timezone.now()
    # 아직 못 한 백필은 취소한다. 아무도 안 보는 기업의 과거를 채울 이유가 없다.
    company.backfill_requested_at = None
    company.save(update_fields=['is_active', 'untracked_at', 'backfill_requested_at'])


def _company_from_listed(listed):
    """명단의 기업을 수집 대상(Company)으로 옮긴다. 이미 있으면 그대로 쓴다."""
    company = Company.objects.filter(corp_code=listed.corp_code).first()
    if company is not None:
        return company
    sector, _ = Sector.objects.get_or_create(
        slug=DEFAULT_SECTOR_SLUG, defaults={'name': DEFAULT_SECTOR_NAME},
    )
    # is_active=False로 만든다. 추적은 _start_tracking이 켜야 백필 요청도 함께 걸린다.
    return Company.objects.create(
        sector=sector, corp_code=listed.corp_code, stock_code=listed.stock_code,
        name=listed.name, is_active=False,
    )


def follow(user, listed, now=None):
    """관심 기업에 추가한다. 이미 있으면 아무것도 하지 않는다(추가 횟수도 안 센다)."""
    now = now or timezone.now()
    existing = Watch.objects.filter(
        user=user, company__corp_code=listed.corp_code).first()
    if existing is not None:
        return existing

    with transaction.atomic():
        company = _company_from_listed(listed)
        watch = Watch.objects.create(user=user, company=company)
        WatchAddition.objects.create(user=user, company=company)
        if Watch.objects.filter(user=user).count() > MAX_WATCHES_PER_USER:
            raise WatchLimitError(
                f'관심 기업은 {MAX_WATCHES_PER_USER}곳까지 추가할 수 있습니다. '
                f'다른 기업을 빼고 추가해 주세요.')
        if additions_today(user, now) > MAX_ADDITIONS_PER_DAY:
            raise WatchLimitError(
                f'관심 기업은 하루 {MAX_ADDITIONS_PER_DAY}번까지 추가할 수 있습니다. '
                f'내일 다시 시도해 주세요.')
        _start_tracking(company, now)
    return watch


def unfollow(user, company):
    """관심 기업에서 뺀다. 추적 중단은 삭제 신호가 처리한다(탈퇴 때도 같은 경로)."""
    Watch.objects.filter(user=user, company=company).delete()


@receiver(post_delete, sender=Watch)
def _stop_tracking_after_unwatch(sender, instance, **kwargs):
    """관심 기업이 어떤 경로로 지워지든(직접 빼기·회원 탈퇴·admin) 추적을 정리한다.

    탈퇴는 User 삭제의 CASCADE로 Watch가 지워진다. unfollow()만 추적을 멈추게 하면
    탈퇴한 회원이 고른 기업은 아무도 보지 않는데 영원히 수집된다.
    """
    company = Company.objects.filter(pk=instance.company_id).first()
    if company is not None:
        stop_tracking_if_unwatched(company)


def search_listed(query, limit=20):
    """기업 검색. 저장된 상장사 명단만 본다 — DART를 부르지 않는다."""
    query = (query or '').strip()
    if not query:
        return ListedCorp.objects.none()
    if query.isdigit():
        return ListedCorp.objects.filter(stock_code__startswith=query)[:limit]
    return ListedCorp.objects.filter(name__icontains=query).order_by('name')[:limit]
