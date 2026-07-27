"""DART list.json을 폴링해 추적 기업의 신규 공시를 Disclosure로 적재한다.

날짜 범위 전체 공시를 받아 로컬에서 추적 기업만 필터링한다(PLAN.md 12.2 확장 전략).
rcept_no unique 제약으로 중복 실행에도 멱등하다.

사용법:
  python manage.py poll_dart [--days 3]                    # 최근 N일 (정상 폴링)
  python manage.py poll_dart --bgn 20260101 --end 20260630  # 임의 구간 (백필·장애 복구)

corp_code 없는 조회는 검색기간이 3개월로 제한되므로(dart.MAX_LIST_SPAN_DAYS), 범위가
한도를 넘으면 자동으로 날짜 청크로 분할해 순회한다. 적재 경로는 청크 수와 무관하게
아래 get_or_create 한 곳이므로 멱등성은 그대로 유지된다.

추후 Celery Beat 도입 시 이 로직을 그대로 태스크로 옮긴다.
"""
from datetime import date, datetime, timedelta

from django.core.management.base import BaseCommand, CommandError

from disclosures.dart import (
    MAX_LIST_SPAN_DAYS,
    PBLNTF_TYPES,
    dart_viewer_url,
    iter_disclosures,
    split_date_range,
)
from disclosures.models import Company, Disclosure

# --days, --bgn/--end 모두 없을 때의 기본 조회 범위
DEFAULT_DAYS = 3

# 개인 인증키 일일 호출 한도. 절반을 넘을 것으로 추정되면 실행 전 경고한다.
DAILY_CALL_LIMIT = 20_000
CALL_WARN_THRESHOLD = DAILY_CALL_LIMIT // 2

# 호출량 개략 추정용: 시장 전체 공시는 하루 1,000건 안팎이고 page_count가 100이므로
# 하루 약 10페이지로 본다. 정확한 페이지 수는 조회 전에 알 수 없으므로 어림수다.
EST_PAGES_PER_DAY = 10


class Command(BaseCommand):
    help = 'DART 공시 목록을 폴링해 신규 공시를 저장한다 (--bgn/--end로 임의 구간 백필)'

    def add_arguments(self, parser):
        parser.add_argument(
            '--days', type=int, default=None,
            help=f'오늘부터 며칠 전까지 조회할지 (기본 {DEFAULT_DAYS}일). '
                 '--bgn/--end와 함께 쓸 수 없다',
        )
        parser.add_argument(
            '--bgn', default=None,
            help='조회 시작일 YYYYMMDD (백필용). --days와 함께 쓸 수 없다',
        )
        parser.add_argument(
            '--end', default=None,
            help='조회 종료일 YYYYMMDD (생략 시 오늘). --bgn과 함께 지정한다',
        )

    def handle(self, *args, **options):
        bgn, end = self._resolve_range(options)

        companies = {
            c.corp_code: c
            for c in Company.objects.filter(is_active=True)
        }
        if not companies:
            self.stdout.write(self.style.WARNING(
                '추적 중인 기업이 없습니다. 먼저 seed_companies를 실행하세요.'
            ))
            return

        chunks = split_date_range(bgn, end)
        total_days = (end - bgn).days + 1
        self.stdout.write(
            f'조회 범위 {bgn:%Y%m%d} ~ {end:%Y%m%d} ({total_days}일) · '
            f'추적 기업 {len(companies)}곳 · 날짜 청크 {len(chunks)}개'
        )
        if len(chunks) > 1:
            self.stdout.write(
                f'  corp_code 없는 list.json은 검색기간 {MAX_LIST_SPAN_DAYS}일 초과 시 '
                f'오류(코드 100)이므로 구간을 분할해 순회합니다(경계일 1일 중복 조회).'
            )
        self._warn_if_call_heavy(chunks, total_days)

        scanned, new = 0, 0
        for idx, (chunk_bgn, chunk_end) in enumerate(chunks, start=1):
            bgn_de, end_de = f'{chunk_bgn:%Y%m%d}', f'{chunk_end:%Y%m%d}'
            if len(chunks) > 1:
                self.stdout.write(f'[청크 {idx}/{len(chunks)}] {bgn_de} ~ {end_de} 조회')

            chunk_scanned, chunk_new = self._collect(bgn_de, end_de, companies)
            scanned += chunk_scanned
            new += chunk_new

            if len(chunks) > 1:
                self.stdout.write(
                    f'[청크 {idx}/{len(chunks)}] 완료: '
                    f'스캔 {chunk_scanned:,}건, 신규 {chunk_new}건 '
                    f'(누적 스캔 {scanned:,}건, 누적 신규 {new}건)'
                )

        self.stdout.write(self.style.SUCCESS(
            f'완료: 전체 공시 {scanned:,}건 스캔, 신규 저장 {new}건 (청크 {len(chunks)}개)'
        ))

    # --- 입력 해석 -------------------------------------------------------

    def _resolve_range(self, options):
        """옵션에서 조회 구간(date, date)을 정하고 검증한다."""
        days, bgn_opt, end_opt = options['days'], options['bgn'], options['end']
        today = date.today()

        if days is not None and (bgn_opt or end_opt):
            raise CommandError(
                '--days와 --bgn/--end는 함께 지정할 수 없습니다. 하나만 사용하세요.'
            )

        if bgn_opt or end_opt:
            if not bgn_opt:
                raise CommandError('--end만으로는 구간을 정할 수 없습니다. --bgn을 함께 지정하세요.')
            bgn = self._parse_date(bgn_opt, '--bgn')
            end = self._parse_date(end_opt, '--end') if end_opt else today
        else:
            days = DEFAULT_DAYS if days is None else days
            if days < 0:
                raise CommandError('--days는 0 이상이어야 합니다.')
            end = today
            bgn = end - timedelta(days=days)

        if bgn > end:
            raise CommandError(
                f'시작일({bgn:%Y%m%d})이 종료일({end:%Y%m%d})보다 늦습니다.'
            )
        if end > today:
            raise CommandError(
                f'종료일({end:%Y%m%d})이 미래입니다. 오늘({today:%Y%m%d}) 이후는 조회할 수 없습니다.'
            )
        return bgn, end

    @staticmethod
    def _parse_date(value, label):
        try:
            return datetime.strptime(value, '%Y%m%d').date()
        except ValueError:
            raise CommandError(f'{label} 날짜 형식이 잘못되었습니다: {value!r} (YYYYMMDD)')

    def _warn_if_call_heavy(self, chunks, total_days):
        """호출량이 일일 한도를 위협할 규모면 실행 전에 경고한다."""
        # 청크당 유형별 최소 1회 + 날짜 규모에 비례하는 페이지네이션으로 어림잡는다.
        estimated = len(chunks) * len(PBLNTF_TYPES) + total_days * EST_PAGES_PER_DAY
        if estimated < CALL_WARN_THRESHOLD:
            return
        self.stdout.write(self.style.WARNING(
            f'경고: 예상 DART 호출 약 {estimated:,}회로 일일 한도({DAILY_CALL_LIMIT:,}회)를 '
            f'위협하는 규모입니다. 구간을 나눠 여러 날에 걸쳐 실행하는 것을 권합니다.'
        ))

    # --- 수집 -----------------------------------------------------------

    def _collect(self, bgn_de, end_de, companies):
        """한 날짜 창을 유형별로 조회해 (스캔 건수, 신규 저장 건수)를 반환.

        공시유형은 list.json 응답에 없으므로 유형별로 나눠 조회해 각 공시에 유형을 태깅한다.
        유형은 전체 공시를 분할하므로 기업 수와 무관하게 호출 수가 고정된다(PLAN.md 12.2).
        """
        scanned, new = 0, 0
        for code, type_name in PBLNTF_TYPES.items():
            for item in iter_disclosures(bgn_de, end_de, pblntf_ty=code):
                scanned += 1
                company = companies.get(item['corp_code'])
                if company is None:
                    continue
                _, created = Disclosure.objects.get_or_create(
                    rcept_no=item['rcept_no'],
                    defaults={
                        'company': company,
                        'report_name': item['report_nm'].strip(),
                        'disclosure_type': type_name,
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
                    self.stdout.write(
                        f'  신규: [{company.name}] ({type_name}) {item["report_nm"].strip()}'
                    )
        return scanned, new
