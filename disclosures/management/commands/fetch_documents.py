"""요약 대상 공시의 원문을 DART에서 받아 전처리해 저장한다.

요약(LLM 호출)과 **별도 명령**으로 분리했다. 원문 확보는 네트워크·대용량 다운로드가
얽혀 실패가 잦은데, 요약과 한 명령에 묶으면 재시도할 때마다 LLM 비용이 함께 든다.

호출 절감 규칙:
  - 선별 정책상 요약 대상(selection_state=target)만 받는다
  - 이미 받은 공시(raw_fetched=True)는 절대 재호출하지 않는다
  - 실패한 공시는 간격을 벌려 재시도하고 상한에서 멈춘다(RETRY_BACKOFF_MINUTES)
  - 대형 서식(정기공시·발행공시)은 핵심 섹션만 추출해 저장한다(dart.KEY_SECTIONS_BY_TYPE)

사용법:
  python manage.py fetch_documents --limit 5            # 5건만 (검증용)
  python manage.py fetch_documents --type 거래소공시
  python manage.py fetch_documents --refetch --rcept-no 20260515002181
  python manage.py fetch_documents --stuck              # 멈춘 건 확인 (DART 미호출)
"""
import collections
import time
from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from disclosures.dart import fetch_document, preprocess_document
from disclosures.models import Disclosure
from disclosures.selection import SelectionState

# DART 서버 부하를 피하기 위한 호출 간 최소 간격(초).
REQUEST_INTERVAL_SEC = 0.2

#: 실패 후 다음 시도까지 기다리는 시간(분). 인덱스 = 지금까지 쌓인 시도 횟수 - 1.
#:
#: 목록에는 떴는데 원문이 아직 공개되지 않은 공시(DART `[014]`)가 이 값을 결정했다.
#: 그 유형은 대개 몇 분~몇 시간 뒤에 올라오므로 처음엔 촘촘히, 갈수록 느슨하게 본다.
#: 마지막 칸까지 쓰면 약 31시간에 걸쳐 6번 시도하고 멈춘다.
#:
#: 6단계까지는 이 장치가 없어도 됐다 — 파이프라인이 하루 1회라 재시도도 하루 1회였다.
#: 7단계에서 평일 낮 1분마다 돌게 되면서 같은 공시를 **하루 1,440번** 부르게 되므로
#: (PLAN.md 9.3) 상한이 선택이 아니라 필수가 됐다.
RETRY_BACKOFF_MINUTES = (5, 15, 60, 360, 1440)

#: 이 횟수만큼 실패하면 더 시도하지 않는다. 위 표의 길이보다 하나 크다 —
#: 마지막 대기(24시간)를 보낸 뒤 한 번 더 시도하고 멈춘다는 뜻이다.
MAX_FETCH_ATTEMPTS = len(RETRY_BACKOFF_MINUTES) + 1

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
        parser.add_argument(
            '--stuck', action='store_true',
            help=f'재시도 상한({MAX_FETCH_ATTEMPTS}회)에 걸려 멈춘 공시를 조회만 한다 '
                 '(DART를 호출하지 않는다)',
        )
        parser.add_argument(
            '--retry-stuck', dest='retry_stuck', action='store_true',
            help='상한에 걸린 공시의 시도 기록을 지워 처음부터 다시 시도하게 한다. '
                 '원인을 고친 뒤 손으로 되살리는 용도',
        )

    def handle(self, *args, **options):
        limit = options['limit']
        if limit is not None and limit < 1:
            raise CommandError('--limit은 1 이상이어야 합니다.')

        if options['stuck']:
            self._report_stuck()
            return
        if options['retry_stuck']:
            self._reset_stuck()
            return

        queryset = Disclosure.objects.filter(
            selection_state=SelectionState.TARGET
        ).select_related('company').order_by('filed_at', 'rcept_no')

        if not options['refetch']:
            queryset = queryset.filter(raw_fetched=False)
        if options['disclosure_type']:
            queryset = queryset.filter(disclosure_type=options['disclosure_type'])
        if options['rcept_no']:
            queryset = queryset.filter(rcept_no=options['rcept_no'])

        # --rcept-no는 사람이 특정 1건을 콕 집어 다시 받으라는 뜻이므로 대기를 건너뛴다.
        # 자동 실행(cron)만 재시도 정책을 따른다.
        if options['rcept_no']:
            candidates, waiting, stuck = list(queryset), 0, 0
        else:
            candidates, waiting, stuck = self._apply_retry_policy(queryset)

        targets = candidates[:limit] if limit else candidates
        if not targets:
            self._report_nothing_to_do(waiting, stuck)
            return

        remaining = Disclosure.objects.filter(
            selection_state=SelectionState.TARGET, raw_fetched=False
        ).count()
        held = self._describe_held(waiting, stuck)
        self.stdout.write(
            f'요약 대상 미확보 {remaining:,}건 중 {len(targets):,}건 처리{held}'
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
                attempts = self._record_failure(disclosure, exc)
                left = MAX_FETCH_ATTEMPTS - attempts
                note = (
                    f'{attempts}회째 실패 · 재시도 {left}회 남음' if left > 0
                    else f'{attempts}회째 실패 · 상한 도달, 더 시도하지 않습니다'
                )
                self.stderr.write(self.style.ERROR(
                    f'[{index}/{len(targets)}] 실패 {disclosure.rcept_no} '
                    f'({disclosure.disclosure_type}) {disclosure.report_name}: '
                    f'{type(exc).__name__}: {exc} — {note}'
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
        # 성공했으니 실패 기록을 지운다. 남겨두면 --stuck 목록에 계속 나타나고,
        # --refetch로 다시 받을 때 지난 실패 횟수가 이어져 조기에 멈춘다.
        disclosure.raw_fetch_attempts = 0
        disclosure.raw_fetch_attempted_at = None
        disclosure.raw_fetch_error = ''
        disclosure.save(update_fields=[
            'raw_content', 'raw_fetched',
            'raw_fetch_attempts', 'raw_fetch_attempted_at', 'raw_fetch_error',
        ])
        return len(raw), len(cleaned), count_tokens(cleaned)

    # --- 재시도 정책 -----------------------------------------------------

    def _apply_retry_policy(self, queryset):
        """(지금 시도할 것, 대기 중, 상한 도달) 으로 나눈다.

        대기 시간이 시도 횟수마다 달라 SQL 한 줄로 거르기 어렵다. 요약 대상 중
        미확보분은 많아야 수백 건이라 파이썬에서 나누는 편이 읽기 쉽다.
        """
        now = timezone.now()
        ready, waiting, stuck = [], 0, 0
        for disclosure in queryset:
            attempts = disclosure.raw_fetch_attempts
            if attempts >= MAX_FETCH_ATTEMPTS:
                stuck += 1
                continue
            if attempts and disclosure.raw_fetch_attempted_at:
                index = min(attempts, len(RETRY_BACKOFF_MINUTES)) - 1
                due = disclosure.raw_fetch_attempted_at + timedelta(
                    minutes=RETRY_BACKOFF_MINUTES[index]
                )
                if now < due:
                    waiting += 1
                    continue
            ready.append(disclosure)
        return ready, waiting, stuck

    def _record_failure(self, disclosure, exc):
        """실패를 기록하고 누적 시도 횟수를 돌려준다."""
        disclosure.raw_fetch_attempts += 1
        disclosure.raw_fetch_attempted_at = timezone.now()
        # 메시지가 길어도 필드 길이를 넘기면 저장이 통째로 실패한다. 원인 파악에는
        # 앞부분이면 충분하므로 자른다.
        disclosure.raw_fetch_error = f'{type(exc).__name__}: {exc}'[:300]
        disclosure.save(update_fields=[
            'raw_fetch_attempts', 'raw_fetch_attempted_at', 'raw_fetch_error',
        ])
        return disclosure.raw_fetch_attempts

    # --- 멈춘 건 조회·되살리기 -------------------------------------------

    def _stuck_queryset(self):
        return Disclosure.objects.filter(
            selection_state=SelectionState.TARGET, raw_fetched=False,
            raw_fetch_attempts__gte=MAX_FETCH_ATTEMPTS,
        ).select_related('company').order_by('filed_at', 'rcept_no')

    def _report_stuck(self):
        """상한에 걸려 멈춘 공시를 보여준다. DART를 호출하지 않는다.

        상한을 두면 조용히 사라지는 공시가 생긴다. 그게 "아직 안 올라온 것"인지
        "우리 코드가 깨진 것"인지 사람이 볼 수 있어야 상한이 안전장치로 성립한다.
        """
        stuck = list(self._stuck_queryset())
        if not stuck:
            self.stdout.write(self.style.SUCCESS(
                f'재시도 상한({MAX_FETCH_ATTEMPTS}회)에 걸린 공시가 없습니다.'
            ))
            return

        self.stdout.write(self.style.WARNING(
            f'재시도 상한({MAX_FETCH_ATTEMPTS}회)에 걸려 멈춘 공시 {len(stuck):,}건'
        ))
        for disclosure in stuck:
            attempted = disclosure.raw_fetch_attempted_at
            when = f'{attempted:%Y-%m-%d %H:%M}' if attempted else '기록 없음'
            self.stdout.write(
                f'  {disclosure.rcept_no} [{disclosure.company.name}] '
                f'({disclosure.disclosure_type}) {disclosure.report_name[:40]}'
            )
            self.stdout.write(
                f'    마지막 시도 {when} · {disclosure.raw_fetch_error}'
            )
        self.stdout.write('')
        self.stdout.write(
            '원인을 고쳤다면 --retry-stuck 으로 시도 기록을 지워 되살릴 수 있습니다.'
        )

    def _reset_stuck(self):
        updated = self._stuck_queryset().update(
            raw_fetch_attempts=0, raw_fetch_attempted_at=None, raw_fetch_error='',
        )
        self.stdout.write(self.style.SUCCESS(
            f'{updated:,}건의 시도 기록을 지웠습니다. 다음 실행에서 다시 시도합니다.'
        ))

    # --- 안내 문구 -------------------------------------------------------

    @staticmethod
    def _describe_held(waiting, stuck):
        parts = []
        if waiting:
            parts.append(f'재시도 대기 {waiting:,}건')
        if stuck:
            parts.append(f'상한 도달 {stuck:,}건')
        return f' ({" · ".join(parts)})' if parts else ''

    def _report_nothing_to_do(self, waiting, stuck):
        if not (waiting or stuck):
            self.stdout.write(self.style.WARNING(
                '확보할 원문이 없습니다. 먼저 apply_selection을 실행했는지 확인하세요.'
            ))
            return
        self.stdout.write(
            f'지금 확보할 원문이 없습니다{self._describe_held(waiting, stuck)}.'
        )
        if stuck:
            self.stdout.write('  상한 도달 건은 --stuck 으로 확인하세요.')

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
