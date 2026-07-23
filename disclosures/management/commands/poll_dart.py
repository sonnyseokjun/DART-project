"""DART list.json을 폴링해 추적 기업의 신규 공시를 Disclosure로 적재한다.

날짜 범위 전체 공시를 받아 로컬에서 추적 기업만 필터링한다(PLAN.md 12.2 확장 전략).
rcept_no unique 제약으로 중복 실행에도 멱등하다.

사용법: python manage.py poll_dart [--days 3]
추후 Celery Beat 도입 시 이 로직을 그대로 태스크로 옮긴다.
"""
from datetime import date, timedelta

from django.core.management.base import BaseCommand

from disclosures.dart import dart_viewer_url, iter_disclosures
from disclosures.models import Company, Disclosure


class Command(BaseCommand):
    help = 'DART 공시 목록을 폴링해 신규 공시를 저장한다'

    def add_arguments(self, parser):
        parser.add_argument(
            '--days', type=int, default=3,
            help='오늘부터 며칠 전까지 조회할지 (기본 3일)',
        )

    def handle(self, *args, **options):
        companies = {
            c.corp_code: c
            for c in Company.objects.filter(is_active=True)
        }
        if not companies:
            self.stdout.write(self.style.WARNING(
                '추적 중인 기업이 없습니다. 먼저 seed_companies를 실행하세요.'
            ))
            return

        end = date.today()
        bgn = end - timedelta(days=options['days'])
        bgn_de, end_de = bgn.strftime('%Y%m%d'), end.strftime('%Y%m%d')
        self.stdout.write(f'조회 범위 {bgn_de} ~ {end_de} · 추적 기업 {len(companies)}곳')

        scanned, new = 0, 0
        for item in iter_disclosures(bgn_de, end_de):
            scanned += 1
            company = companies.get(item['corp_code'])
            if company is None:
                continue
            _, created = Disclosure.objects.get_or_create(
                rcept_no=item['rcept_no'],
                defaults={
                    'company': company,
                    'report_name': item['report_nm'].strip(),
                    'disclosure_type': item.get('pblntf_ty', ''),
                    'filed_at': date(
                        int(item['rcept_dt'][:4]),
                        int(item['rcept_dt'][4:6]),
                        int(item['rcept_dt'][6:8]),
                    ),
                    'dart_url': dart_viewer_url(item['rcept_no']),
                },
            )
            if created:
                new += 1
                self.stdout.write(f'  신규: [{company.name}] {item["report_nm"].strip()}')

        self.stdout.write(self.style.SUCCESS(
            f'완료: 전체 공시 {scanned:,}건 스캔, 신규 저장 {new}건'
        ))
