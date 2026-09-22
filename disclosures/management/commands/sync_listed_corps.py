"""DART 상장사 명단을 받아 기업 검색용 사본(ListedCorp)을 갱신한다 (이슈 #44).

검색 화면은 DART를 부르지 않고 이 사본만 본다(PLAN.md 12.1). 서버에서는 하루 1회
cron이 파이프라인과 같은 잠금 아래에서 돌린다(deploy/crontab) — DB에 쓰는 프로세스가
동시에 둘이 되지 않게 하기 위해서다.

DART 호출은 1회다(corpCode.xml). 명단은 약 4천 곳이고, 받는 데 수 초가 걸린다.

사용법:
  python manage.py sync_listed_corps
"""
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from disclosures.dart import download_corp_codes
from disclosures.models import ListedCorp

#: 받은 명단이 이보다 적으면 갱신하지 않는다. DART가 잘린 파일을 주거나 형식이 바뀌어
#: 몇 곳만 읽혔을 때 기존 명단을 통째로 지우지 않기 위한 안전장치다. 상장사는 약 4천 곳.
MIN_EXPECTED_CORPS = 2000


class Command(BaseCommand):
    help = 'DART 상장사 명단으로 기업 검색용 사본을 갱신한다 (DART 호출 1회)'

    def handle(self, *args, **options):
        rows = download_corp_codes()
        if len(rows) < MIN_EXPECTED_CORPS:
            raise CommandError(
                f'받은 상장사가 {len(rows)}곳뿐이라 갱신하지 않습니다 '
                f'(기대 {MIN_EXPECTED_CORPS}곳 이상). 기존 명단을 그대로 둡니다.')

        incoming = {row['corp_code']: row for row in rows}
        existing = {c.corp_code: c for c in ListedCorp.objects.all()}

        created, changed = [], []
        for corp_code, row in incoming.items():
            corp = existing.get(corp_code)
            if corp is None:
                created.append(ListedCorp(
                    corp_code=corp_code, stock_code=row['stock_code'],
                    name=row['corp_name']))
            elif (corp.name, corp.stock_code) != (row['corp_name'], row['stock_code']):
                corp.name, corp.stock_code = row['corp_name'], row['stock_code']
                changed.append(corp)
        # 상장폐지 등으로 명단에서 빠진 기업은 검색에서만 뺀다. 이미 관심 기업인
        # 기업(Company)은 별도 표라 영향이 없다.
        removed = [code for code in existing if code not in incoming]

        with transaction.atomic():
            ListedCorp.objects.filter(corp_code__in=removed).delete()
            # 종목코드는 unique다. 상장폐지된 기업의 코드를 신규 상장사가 물려받는 경우가
            # 있어, 지우기·고치기를 만들기보다 먼저 한다.
            ListedCorp.objects.bulk_update(changed, ['name', 'stock_code'])
            ListedCorp.objects.bulk_create(created)

        self.stdout.write(self.style.SUCCESS(
            f'상장사 명단 갱신: 전체 {len(incoming):,}곳 · 신규 {len(created)} · '
            f'변경 {len(changed)} · 제외 {len(removed)}'))
