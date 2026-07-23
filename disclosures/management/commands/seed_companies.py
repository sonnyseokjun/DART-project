"""반도체 섹터 + 대상 10개 기업을 생성하고, DART corpCode.xml로 corp_code를 확정한다.

사용법: python manage.py seed_companies
"""
from django.core.management.base import BaseCommand, CommandError

from disclosures.dart import download_corp_codes
from disclosures.models import Company, Sector

# PLAN.md 2장 — 반도체 섹터 MVP 대상 기업 (종목코드 기준으로 corp_code 매칭)
TARGET_COMPANIES = [
    ('005930', '삼성전자', '종합 반도체(IDM)'),
    ('000660', 'SK하이닉스', '메모리'),
    ('000990', 'DB하이텍', '파운드리'),
    ('042700', '한미반도체', '후공정 장비'),
    ('058470', '리노공업', '테스트 부품'),
    ('240810', '원익IPS', '전공정 장비'),
    ('036930', '주성엔지니어링', '전공정 장비'),
    ('064760', '티씨케이', '소재'),
    ('039030', '이오테크닉스', '레이저 장비'),
    ('067310', '하나마이크론', '패키징(OSAT)'),
]


class Command(BaseCommand):
    help = '반도체 섹터와 대상 10개 기업을 corp_code 매핑과 함께 시드한다'

    def handle(self, *args, **options):
        sector, _ = Sector.objects.get_or_create(
            slug='semiconductor',
            defaults={'name': '반도체', 'description': '국내 대표 반도체 기업'},
        )

        self.stdout.write('DART corpCode.xml 다운로드 중...')
        corp_by_stock = {c['stock_code']: c for c in download_corp_codes()}
        self.stdout.write(f'상장사 {len(corp_by_stock):,}곳 로드 완료')

        created, updated, missing = 0, 0, []
        for stock_code, name, sub_category in TARGET_COMPANIES:
            corp = corp_by_stock.get(stock_code)
            if corp is None:
                missing.append(f'{name}({stock_code})')
                continue
            _, was_created = Company.objects.update_or_create(
                stock_code=stock_code,
                defaults={
                    'sector': sector,
                    'corp_code': corp['corp_code'],
                    'name': name,
                    'sub_category': sub_category,
                    'is_active': True,
                },
            )
            if was_created:
                created += 1
            else:
                updated += 1
            self.stdout.write(f'  {name} ({stock_code}) → corp_code {corp["corp_code"]}')

        if missing:
            raise CommandError(f'corpCode.xml에서 찾지 못한 기업: {", ".join(missing)}')

        self.stdout.write(self.style.SUCCESS(f'완료: 신규 {created}곳, 갱신 {updated}곳'))
