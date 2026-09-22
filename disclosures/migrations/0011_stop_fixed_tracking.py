"""운영자가 고른 반도체 10곳의 고정 추적을 멈춘다 (이슈 #44, PLAN.md 9.4).

8단계부터 추적 대상은 사용자가 관심 기업으로 고른 기업뿐이다. 이 시점에는 관심 기업이
하나도 없으므로 모든 기업의 추적이 꺼진다.

**아무것도 지우지 않는다.** 공시와 요약은 그대로 남는다. 누가 이 기업들을 다시 고르면
이미 만든 요약이 비용 없이 바로 보인다.

추적 중단 시각을 지금으로 적어 두는 이유: 하루 안에 누가 다시 고르면 그 사이 공시는
정기 폴링이 이미 받았으므로 백필이 필요 없다(watchlist.needs_backfill).
"""
from django.db import migrations
from django.utils import timezone


def stop_unwatched_tracking(apps, schema_editor):
    Company = apps.get_model('disclosures', 'Company')
    Sector = apps.get_model('disclosures', 'Sector')
    Sector.objects.get_or_create(slug='etc', defaults={'name': '기타'})
    Company.objects.filter(is_active=True, watches__isnull=True).update(
        is_active=False, untracked_at=timezone.now(),
    )


def restart_tracking(apps, schema_editor):
    """되돌리면 예전처럼 전부 추적한다. '기타' 섹터는 기업이 걸려 있을 수 있어 남긴다."""
    Company = apps.get_model('disclosures', 'Company')
    Company.objects.filter(is_active=False).update(is_active=True, untracked_at=None)


class Migration(migrations.Migration):

    dependencies = [
        ('disclosures', '0010_watchlist'),
    ]

    operations = [
        migrations.RunPython(stop_unwatched_tracking, restart_tracking),
    ]
