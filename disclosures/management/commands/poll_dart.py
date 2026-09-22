"""DART list.json을 폴링해 추적 기업의 신규 공시를 Disclosure로 적재한다.

날짜 범위 전체 공시를 받아 로컬에서 추적 기업만 필터링한다(PLAN.md 12.2 확장 전략).
rcept_no unique 제약으로 중복 실행에도 멱등하다.

사용법:
  python manage.py poll_dart [--days 3]                    # 최근 N일 (정상 폴링)
  python manage.py poll_dart --detect --days 2             # 잦은 감지용 (7단계)
  python manage.py poll_dart --bgn 20260101 --end 20260630  # 임의 구간 (백필·장애 복구)

corp_code 없는 조회는 검색기간이 3개월로 제한되므로(dart.MAX_LIST_SPAN_DAYS), 범위가
한도를 넘으면 자동으로 날짜 청크로 분할해 순회한다. 적재 경로는 청크 수와 무관하게
아래 get_or_create 한 곳이므로 멱등성은 그대로 유지된다.

--detect는 유형별 순회 앞에 **호출 1회짜리 사전 확인**을 붙인다. 신규가 없으면 거기서
끝나므로 1분 주기로 돌려도 호출량이 감당된다(PLAN.md 9.3). 자세한 근거는 _has_new 참조.

## 백필 (8단계, 이슈 #44)

사용자가 새로 관심 기업으로 고른 기업은 **최근 30일 공시를 채워 넣는다.** 매 실행의
맨 앞에서 대기열을 조금씩 처리한다. 기업 1곳에 공시유형 수(10)만큼 DART를 부르므로,
한 실행과 하루의 처리량에 상한을 둔다(BACKFILL_*). 목록만 받으므로 요약 비용은 없다.

추후 Celery Beat 도입 시 이 로직을 그대로 태스크로 옮긴다.
"""
import sys
from datetime import date, datetime, timedelta

import requests
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from disclosures import retry_policy, watchlist

from disclosures.dart import (
    DETECT_PAGE_COUNT,
    DartApiError,
    MAX_LIST_SPAN_DAYS,
    PBLNTF_TYPES,
    dart_viewer_url,
    iter_disclosures,
    latest_disclosures,
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

# --detect로 돌았는데 신규가 없어 아무것도 하지 않았을 때의 종료 코드.
# deploy/pipeline.sh가 뒷단계(선별·원문·요약)를 건너뛸지 판단하는 신호다.
#
# 왜 0도 1도 아닌 별도 값인가:
#   0으로 두면 "할 일 없음"과 "수집 완료"를 구분할 수 없어 매분 뒷단계가 헛돈다.
#     특히 fetch_documents는 원문이 아직 안 올라온 공시를 매번 다시 부르므로
#     (DART [014]), 1분 주기에서 그대로 두면 하루 수천 번의 헛호출이 된다.
#   1은 CommandError의 종료 코드다. 네트워크 오류로 실패한 것과 새 공시가 없는 것을
#     같은 값으로 만들면, 스크립트가 진짜 장애를 "평소와 같음"으로 삼켜 버린다.
# grep이 "결과 없음"(1)과 "오류"(2)를 나누는 것과 같은 취지다.
NOTHING_NEW_EXIT_CODE = 9

#: 한 실행에서 백필할 기업 수. 1곳에 호출 10회라 5곳이면 50회, 수십 초다.
#: 1분 주기 실행이 다음 실행과 겹치지 않을 만큼만 한다(겹치면 flock이 다음 것을 건너뛴다).
BACKFILL_COMPANIES_PER_RUN = 5

#: 하루에 백필할 기업 수. 300곳 × 10회 = 3,000회로 DART 하루 한도(20,000)의 15%다.
#: 평소 수집이 쓰는 몫(약 3%, PLAN.md 9.3)과 합쳐도 한도에 한참 못 미친다. 넘치면
#: 다음 날로 밀린다 — 사용자에게는 "최근 공시를 불러오는 중"으로 보인다.
BACKFILL_COMPANIES_PER_DAY = 300


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
        parser.add_argument(
            '--detect', action='store_true',
            help='유형별 순회 전에 최신 %d건을 1회 호출로 훑어 신규가 있는지 먼저 본다. '
                 '없으면 종료 코드 %d로 끝낸다(호출 1회). 1분 주기 폴링용이며 '
                 '--bgn/--end와는 함께 쓸 수 없다'
                 % (DETECT_PAGE_COUNT, NOTHING_NEW_EXIT_CODE),
        )

    def handle(self, *args, **options):
        bgn, end = self._resolve_range(options)

        companies = {
            c.corp_code: c
            for c in Company.objects.filter(is_active=True)
        }
        if not companies:
            # 8단계부터는 정상 상태다 — 아직 아무도 관심 기업을 고르지 않았다.
            # 감지 모드에서 0으로 끝내면 파이프라인이 매분 뒷단계를 헛돌린다.
            self.stdout.write('추적 중인 기업이 없습니다 (관심 기업을 고른 사용자 없음).')
            if options['detect']:
                sys.exit(NOTHING_NEW_EXIT_CODE)
            return

        backfilled = self._backfill(companies)

        if options['detect'] and not self._has_new(bgn, end, companies):
            if backfilled:
                # 시장 전체의 신규는 없어도 백필로 새 공시가 들어왔다. 뒷단계(선별)가
                # 돌아야 하므로 0으로 끝낸다. 유형별 본 순회는 필요 없다.
                return
            sys.exit(NOTHING_NEW_EXIT_CODE)

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

        if options.get('detect') and (bgn_opt or end_opt):
            # --detect는 "최신 몇 건에 새 게 있나"를 보는 것이라 과거 구간과 의미가 맞지
            # 않는다. 백필에 붙이면 사전 확인이 항상 통과해 호출만 1회 늘 뿐이다.
            raise CommandError(
                '--detect는 --bgn/--end와 함께 쓸 수 없습니다. 백필에는 사용하지 마세요.'
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

    # --- 감지 -----------------------------------------------------------

    def _has_new(self, bgn, end, companies):
        """유형별 순회를 돌 가치가 있는지 **호출 1회로** 판단한다.

        1분 주기 폴링(PLAN.md 9.3)의 호출량을 감당하기 위한 사전 확인이다.
        2일 창을 유형 10종으로 훑으면 30회가 넘게 드는데, 하루 대부분의 실행은 새 공시가
        없어 그 호출이 통째로 헛돈다. 최신 DETECT_PAGE_COUNT건을 1회만 받아 추적 기업의
        미저장 공시가 있을 때만 본 순회로 넘어간다.

        ## 왜 여기서 저장하지 않는가 (중요)

        이 호출은 유형 필터가 없어 **공시유형을 알 수 없다.** 유형을 비운 채 저장하면
        선별 정책이 뚫린다 — selection.evaluate()는 `disclosure_type in EXCLUDED_TYPES`로
        제외를 판정하므로, 빈 문자열은 어디에도 걸리지 않고 그대로 TARGET이 된다.
        제외됐어야 할 지분공시·기타공시가 요약 대상이 되고, 요약은 이 프로젝트에서 돈이
        나가는 유일한 경로다. 그래서 여기서는 **판단만 하고 적재는 기존 유형별 순회에
        맡긴다** — 유형은 항상 정확하게 채워진다.

        ## 놓칠 수 있는 경우

        호출 사이에 시장 전체 공시가 DETECT_PAGE_COUNT건을 넘으면 창 밖으로 밀려난 신규는
        보이지 않는다(dart.latest_disclosures 참조). 하루 1회 도는 전체 폴링이 최종
        안전망이므로, 이 경로만으로 수집 완결성을 보장하지 않는다.
        """
        items = latest_disclosures(f'{bgn:%Y%m%d}', f'{end:%Y%m%d}')
        known = set(
            Disclosure.objects.filter(
                rcept_no__in=[item['rcept_no'] for item in items]
            ).values_list('rcept_no', flat=True)
        )
        new_items = [
            item for item in items
            if item['corp_code'] in companies and item['rcept_no'] not in known
        ]
        if not new_items:
            self.stdout.write(
                f'감지: 최신 {len(items)}건에 추적 기업의 신규 공시 없음 (호출 1회)'
            )
            return False

        shown = ', '.join(item['rcept_no'] for item in new_items[:5])
        more = ' 외' if len(new_items) > 5 else ''
        self.stdout.write(
            f'감지: 신규 {len(new_items)}건 발견 — 유형별 순회로 넘어갑니다 ({shown}{more})'
        )
        return True

    # --- 수집 -----------------------------------------------------------

    def _backfill(self, companies):
        """새로 추적을 시작한 기업의 최근 공시를 채운다. 새로 저장한 건수를 반환.

        대기열은 `Company.backfill_requested_at`이다(watchlist._start_tracking이 건다).
        기업별로 corp_code를 주고 유형별로 조회한다 — 유형 없이 한 번에 받으면 호출은
        1회로 줄지만 공시유형을 모른 채 저장하게 되어 선별 정책이 뚫린다(_has_new 참조).

        실패하면 재시도 간격을 두고 다시 하며, 상한에 걸리면 포기한다
        (retry_policy.MAX_BACKFILL_ATTEMPTS). 포기해도 그 기업의 새 공시는 정상 수집된다.
        """
        now = timezone.now()
        done_today = Company.objects.filter(
            backfill_attempted_at__gte=watchlist.today_start(now)).count()
        room = min(BACKFILL_COMPANIES_PER_RUN, BACKFILL_COMPANIES_PER_DAY - done_today)
        if room <= 0:
            return 0

        queue = [
            c for c in Company.objects.filter(
                is_active=True, backfill_requested_at__isnull=False,
            ).order_by('backfill_requested_at')
            if retry_policy.is_retry_due(
                c.backfill_attempts, c.backfill_attempted_at,
                retry_policy.BACKFILL_RETRY_BACKOFF_MINUTES, now)
        ][:room]

        today = timezone.localdate()
        bgn_de = f'{today - timedelta(days=watchlist.BACKFILL_DAYS):%Y%m%d}'
        end_de = f'{today:%Y%m%d}'
        total_new = 0
        for company in queue:
            company.backfill_attempts += 1
            company.backfill_attempted_at = now
            try:
                _, new = self._collect(
                    bgn_de, end_de, {company.corp_code: company},
                    corp_code=company.corp_code)
            except (DartApiError, requests.RequestException) as exc:
                if company.backfill_attempts >= retry_policy.MAX_BACKFILL_ATTEMPTS:
                    company.backfill_requested_at = None
                    note = '상한 도달, 포기'
                else:
                    note = '다음 실행에 재시도'
                company.save(update_fields=[
                    'backfill_attempts', 'backfill_attempted_at', 'backfill_requested_at'])
                self.stdout.write(self.style.WARNING(
                    f'백필 실패: [{company.name}] {exc} ({note})'))
                continue
            company.backfill_requested_at = None
            company.backfilled_at = now
            company.save(update_fields=[
                'backfill_attempts', 'backfill_attempted_at',
                'backfill_requested_at', 'backfilled_at'])
            total_new += new
            self.stdout.write(
                f'백필: [{company.name}] 최근 {watchlist.BACKFILL_DAYS}일 공시 {new}건 저장')
        return total_new

    def _collect(self, bgn_de, end_de, companies, corp_code=None):
        """한 날짜 창을 유형별로 조회해 (스캔 건수, 신규 저장 건수)를 반환.

        공시유형은 list.json 응답에 없으므로 유형별로 나눠 조회해 각 공시에 유형을 태깅한다.
        유형은 전체 공시를 분할하므로 기업 수와 무관하게 호출 수가 고정된다(PLAN.md 12.2).
        corp_code를 주면 그 기업만 조회한다(백필).
        """
        scanned, new = 0, 0
        for code, type_name in PBLNTF_TYPES.items():
            for item in iter_disclosures(
                    bgn_de, end_de, corp_code=corp_code, pblntf_ty=code):
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
