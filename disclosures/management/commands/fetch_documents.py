"""요약 대상 공시의 원문을 DART에서 받아 전처리해 저장한다.

요약(LLM 호출)과 **별도 명령**으로 분리했다. 원문 확보는 네트워크·대용량 다운로드가
얽혀 실패가 잦은데, 요약과 한 명령에 묶으면 재시도할 때마다 LLM 비용이 함께 든다.

호출 절감 규칙:
  - 선별 정책상 요약 대상(selection_state=target)만 받는다
  - 이미 받은 공시(raw_fetched=True)는 절대 재호출하지 않는다
  - 대형 서식(정기공시·발행공시)은 핵심 섹션만 추출해 저장한다(dart.KEY_SECTIONS_BY_TYPE)

사용법:
  python manage.py fetch_documents --limit 5            # 5건만 (검증용)
  python manage.py fetch_documents --type 거래소공시
  python manage.py fetch_documents --refetch --rcept-no 20260515002181
"""
import collections
import time

from django.core.management.base import BaseCommand, CommandError

from disclosures.dart import fetch_document, preprocess_document
from disclosures.models import Disclosure
from disclosures.selection import SelectionState

# DART 서버 부하를 피하기 위한 호출 간 최소 간격(초).
REQUEST_INTERVAL_SEC = 0.2

# tiktoken 인코딩. OpenAI 최신 모델 계열의 토크나이저.
TIKTOKEN_ENCODING = 'o200k_base'


class Command(BaseCommand):
    help = '요약 대상 공시의 원문을 확보해 전처리 후 저장한다'

    def add_arguments(self, parser):
        parser.add_argument(
            '--limit', type=int, default=None,
            help='처리할 최대 건수 (검증용). 생략하면 미확보분 전부',
        )
        parser.add_argument(
            '--type', dest='disclosure_type', default=None,
            help='공시유형으로 좁힌다 (예: 거래소공시, 정기공시)',
        )
        parser.add_argument(
            '--rcept-no', dest='rcept_no', default=None,
            help='특정 접수번호 1건만 처리한다',
        )
        parser.add_argument(
            '--refetch', action='store_true',
            help='이미 확보한 원문도 다시 받는다 (전처리 로직 변경 시)',
        )

    def handle(self, *args, **options):
        limit = options['limit']
        if limit is not None and limit < 1:
            raise CommandError('--limit은 1 이상이어야 합니다.')

        queryset = Disclosure.objects.filter(
            selection_state=SelectionState.TARGET
        ).select_related('company').order_by('filed_at', 'rcept_no')

        if not options['refetch']:
            queryset = queryset.filter(raw_fetched=False)
        if options['disclosure_type']:
            queryset = queryset.filter(disclosure_type=options['disclosure_type'])
        if options['rcept_no']:
            queryset = queryset.filter(rcept_no=options['rcept_no'])

        targets = list(queryset[:limit] if limit else queryset)
        if not targets:
            self.stdout.write(self.style.WARNING(
                '확보할 원문이 없습니다. 먼저 apply_selection을 실행했는지 확인하세요.'
            ))
            return

        remaining = Disclosure.objects.filter(
            selection_state=SelectionState.TARGET, raw_fetched=False
        ).count()
        self.stdout.write(
            f'요약 대상 미확보 {remaining:,}건 중 {len(targets):,}건 처리'
        )

        stats = collections.defaultdict(
            lambda: {'건수': 0, '원문자수': 0, '정제자수': 0, '토큰': 0}
        )
        succeeded, failed = 0, 0

        for index, disclosure in enumerate(targets, start=1):
            try:
                raw_len, clean_len, tokens = self._process(disclosure)
            # DartApiError·네트워크 오류·ZIP 손상 등 건별 실패로 전체가 죽으면 안 된다.
            except Exception as exc:
                failed += 1
                self.stderr.write(self.style.ERROR(
                    f'[{index}/{len(targets)}] 실패 {disclosure.rcept_no} '
                    f'({disclosure.disclosure_type}) {disclosure.report_name}: '
                    f'{type(exc).__name__}: {exc}'
                ))
                continue

            succeeded += 1
            bucket = stats[disclosure.disclosure_type]
            bucket['건수'] += 1
            bucket['원문자수'] += raw_len
            bucket['정제자수'] += clean_len
            bucket['토큰'] += tokens
            self.stdout.write(
                f'[{index}/{len(targets)}] {disclosure.rcept_no} '
                f'({disclosure.disclosure_type}) {disclosure.report_name[:40]} '
                f'· {raw_len:,}자 → {clean_len:,}자 / {tokens:,}토큰'
            )
            if index < len(targets):
                time.sleep(REQUEST_INTERVAL_SEC)

        self._report(stats, succeeded, failed)

    # --- 건별 처리 -------------------------------------------------------

    def _process(self, disclosure):
        """원문 1건을 받아 전처리해 저장하고 (원문 자수, 정제 자수, 토큰 수)를 반환."""
        raw = fetch_document(disclosure.rcept_no)
        cleaned = preprocess_document(raw, disclosure_type=disclosure.disclosure_type)
        disclosure.raw_content = cleaned
        disclosure.raw_fetched = True
        disclosure.save(update_fields=['raw_content', 'raw_fetched'])
        return len(raw), len(cleaned), count_tokens(cleaned)

    # --- 집계 출력 -------------------------------------------------------

    def _report(self, stats, succeeded, failed):
        self.stdout.write('')
        self.stdout.write('== 공시유형별 원문 규모 (정제 후 기준) ==')
        header = f'{"공시유형":<10} {"건수":>5} {"원문자수":>12} {"정제자수":>11} {"토큰":>10} {"건당토큰":>9}'
        self.stdout.write(header)
        total = {'건수': 0, '원문자수': 0, '정제자수': 0, '토큰': 0}
        for disclosure_type in sorted(stats, key=lambda t: -stats[t]['토큰']):
            bucket = stats[disclosure_type]
            for key in total:
                total[key] += bucket[key]
            self.stdout.write(
                f'{disclosure_type:<10} {bucket["건수"]:>5,} {bucket["원문자수"]:>12,} '
                f'{bucket["정제자수"]:>11,} {bucket["토큰"]:>10,} '
                f'{bucket["토큰"] // max(bucket["건수"], 1):>9,}'
            )
        if total['건수']:
            self.stdout.write(
                f'{"합계":<10} {total["건수"]:>5,} {total["원문자수"]:>12,} '
                f'{total["정제자수"]:>11,} {total["토큰"]:>10,} '
                f'{total["토큰"] // total["건수"]:>9,}'
            )

        message = f'완료: 성공 {succeeded:,}건, 실패 {failed:,}건'
        self.stdout.write('')
        self.stdout.write(
            self.style.WARNING(message) if failed else self.style.SUCCESS(message)
        )


def count_tokens(text, encoding_name=TIKTOKEN_ENCODING):
    """tiktoken으로 토큰 수를 센다. tiktoken을 못 쓰면 0을 돌려주고 집계만 비운다.

    (테스트 환경에서 인코딩 파일 다운로드에 실패해도 원문 확보 자체는 막지 않는다.)
    """
    try:
        import tiktoken

        return len(tiktoken.get_encoding(encoding_name).encode(text))
    except Exception:
        return 0
