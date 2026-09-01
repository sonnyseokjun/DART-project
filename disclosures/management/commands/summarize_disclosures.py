"""요약 대상 공시의 전처리된 원문을 LLM으로 요약해 DisclosureSummary에 적재한다.

선별 정책(selection.py)이 대상으로 판정하고 원문이 확보된 공시만 처리한다.
요약이 이미 있으면 LLM을 재호출하지 않는다 — 공시당 1회 원칙(PLAN.md 11)의 실행 지점이다.
원문 확보(fetch_documents)와 분리되어 있어 요약만 재시도할 수 있다.

사용법:
  python manage.py summarize_disclosures --dry-run          # 비용 추정만 (LLM 미호출)
  python manage.py summarize_disclosures --limit 5          # 5건만 요약
  python manage.py summarize_disclosures --type 거래소공시    # 특정 유형만
  python manage.py summarize_disclosures --resummarize --only-warned --dry-run
                                                            # 경고 붙은 요약만 (비용 먼저 확인)
  python manage.py summarize_disclosures --resummarize --stale-prompt
                                                            # 옛 프롬프트로 만든 요약만
                                                            # (중단돼도 재실행이 이어받는다)
  python manage.py summarize_disclosures                    # 대상 전체

검증에 실패한(수치 경고가 붙은) 요약은 **자동으로 미게시 상태로 저장**한다. 검증 결과가
노출에 아무 영향을 주지 않던 탓에, 39조를 3조로 적은 요약이 경고를 단 채 웹에 그대로
떠 있던 일이 있었다. 무엇이 게시를 막는지는 verification.blocking_warnings 가 정한다.

추후 Celery 태스크로 옮길 때는 _summarize_one()을 그대로 태스크 본문으로 쓰면 된다.
"""
import inspect
from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from disclosures.models import MAX_SUMMARY_ATTEMPTS, Disclosure, DisclosureSummary
from disclosures.review_policy import MAX_REGENERATION_ATTEMPTS, should_regenerate
from disclosures.selection import SelectionState
from disclosures.summarizer import (
    DEFAULT_MODEL,
    PROMPT_VERSION,
    SummarizerError,
    build_review_warnings,
    check_api_key,
    count_tokens,
    estimate_summary_cost,
    summarize_disclosure,
)
from disclosures.verification import AUTO_HIDDEN_REASON, blocking_warnings

#: 요약 실패 후 다음 시도까지 기다리는 시간(분). 인덱스 = 지금까지 쌓인 시도 횟수 - 1.
#:
#: 원문 확보(fetch_documents.RETRY_BACKOFF_MINUTES)보다 짧고 성깁니다 — 이유가 둘이다.
#:   1. 실패 성격이 다르다. 원문 미공개는 시간이 해결하지만, 요약 실패는 스키마 위반이나
#:      원문 과대 같은 **결정적 원인**이 많아 그냥 다시 불러도 같은 결과가 나온다.
#:   2. 재시도 비용이 다르다. 원문 확보는 DART 호출 한도만 쓰지만 요약은 실제 돈이다.
#: 상한(MAX_SUMMARY_ATTEMPTS=4)까지 가도 건당 최대 4회, 약 $0.09에서 멈춘다.
SUMMARY_RETRY_BACKOFF_MINUTES = (10, 60, 360)

#: 재생성을 요청할 때 summarize_disclosure 에 넘길 키워드 이름.
#: 이 키워드가 시그니처에 있어야 재생성 경로가 살아난다(아래 _regeneration_supported 참고).
CORRECTION_KWARG = 'correction_warnings'

#: 직전 요약을 되돌려 줄 키워드. summarizer 문서상 **함께 넘기는 것이 강하게 권장**된다 —
#: 없으면 모델이 "인용 근거 없는 수치: 3조 9,891억"이라는 지적을 받고도 자기가 그 값을
#: 어디에 썼는지 몰라 교정이 아니라 재작성을 한다. 있으면 넘기고 없으면 생략한다.
PREVIOUS_KWARG = 'correction_previous'


def _regeneration_supported():
    """summarizer 가 교정 재생성을 받을 준비가 됐는지.

    `summarize_disclosure` 시그니처에 CORRECTION_KWARG 가 있는지만 본다.
    try/except TypeError 로 떠보지 않는 이유는, 그러면 **LLM을 이미 한 번 부른 뒤에**
    실패를 알게 되어 돈이 나가기 때문이다. 호출 전에 값싸게 판정한다.
    """
    parameters = inspect.signature(summarize_disclosure).parameters
    return CORRECTION_KWARG in parameters


def _previous_supported():
    """직전 요약을 되먹일 수 있는지. 없으면 경고만 넘기고 재생성은 그대로 진행한다."""
    return PREVIOUS_KWARG in inspect.signature(summarize_disclosure).parameters


class Command(BaseCommand):
    help = '요약 대상 공시를 LLM으로 요약해 DisclosureSummary에 저장한다'

    def add_arguments(self, parser):
        parser.add_argument(
            '--limit', type=int, default=None,
            help='처리할 최대 건수 (기본: 제한 없음)',
        )
        parser.add_argument(
            '--type', dest='disclosure_type', default=None,
            help='공시 유형으로 필터 (예: 거래소공시)',
        )
        parser.add_argument(
            '--rcept-no', dest='rcept_no', default=None,
            help='특정 접수번호 1건만 처리',
        )
        parser.add_argument(
            '--dry-run', action='store_true',
            help='LLM을 호출하지 않고 대상 건수와 예상 비용만 출력',
        )
        parser.add_argument(
            '--stuck', action='store_true',
            help=f'재시도 상한({MAX_SUMMARY_ATTEMPTS}회)에 걸려 멈춘 공시를 조회만 한다 '
                 '(LLM을 호출하지 않는다)',
        )
        parser.add_argument(
            '--retry-stuck', dest='retry_stuck', action='store_true',
            help='상한에 걸린 공시의 시도 기록을 지워 다시 시도하게 한다. '
                 '원인을 고친 뒤 손으로 되살리는 용도 — 비용이 발생한다',
        )
        parser.add_argument(
            '--model', default=DEFAULT_MODEL,
            help=f'사용할 모델 (기본 {DEFAULT_MODEL})',
        )
        parser.add_argument(
            '--resummarize', action='store_true',
            help='이미 요약이 있어도 다시 생성한다 (기존 요약을 덮어씀). '
                 '비용이 다시 발생하므로 프롬프트를 고쳤을 때만 사용한다',
        )
        parser.add_argument(
            '--only-warned', dest='only_warned', action='store_true',
            help='검증 경고가 붙은 요약만 다시 생성한다. --resummarize 와 함께 써야 한다. '
                 '프롬프트를 고친 뒤 "문제 있는 것만" 되돌리는 용도',
        )
        parser.add_argument(
            '--stale-prompt', dest='stale_prompt', action='store_true',
            help=f'현재 프롬프트({PROMPT_VERSION})로 만들지 않은 요약만 다시 생성한다. '
                 '--resummarize 와 함께 써야 한다. 전량 재요약이 도중에 끊겨도 '
                 '다시 실행하면 남은 것부터 이어받는다',
        )
        parser.add_argument(
            '--regenerate', action='store_true',
            help=f'검증에 실패한 요약을 경고를 되먹여 최대 {MAX_REGENERATION_ATTEMPTS}회 '
                 '다시 생성한다. 건당 LLM을 한 번 더 부르므로 비용이 늘어난다. '
                 '기본값은 꺼짐 — 비용이 드는 동작은 명시적으로 켤 때만 돈다',
        )

    def handle(self, *args, **options):
        if options['stuck']:
            self._report_stuck()
            return
        if options['retry_stuck']:
            self._reset_stuck()
            return
        if options['limit'] is not None and options['limit'] < 1:
            raise CommandError('--limit은 1 이상이어야 합니다.')
        if options['only_warned'] and not options['resummarize']:
            # 경고가 붙은 요약은 **이미 존재하는** 요약이므로, --resummarize 없이는
            # 대상이 항상 0건이 된다. 조용히 아무것도 안 하는 대신 이유를 말한다.
            # 비용이 드는 동작(덮어쓰기)을 옵션 하나가 몰래 켜지 않게 하는 뜻도 있다.
            raise CommandError(
                '--only-warned 는 --resummarize 와 함께 써야 합니다. '
                '경고가 붙은 요약을 덮어쓰는 동작이라 비용이 발생합니다.'
            )
        if options['stale_prompt'] and not options['resummarize']:
            # --only-warned 와 같은 이유다. 대상이 이미 존재하는 요약이므로
            # --resummarize 없이는 항상 0건이 된다.
            raise CommandError(
                '--stale-prompt 는 --resummarize 와 함께 써야 합니다. '
                '옛 프롬프트로 만든 요약을 덮어쓰는 동작이라 비용이 발생합니다.'
            )

        targets = self._select_targets(options)
        if not targets:
            self.stdout.write(self.style.WARNING(
                '처리할 공시가 없습니다. 선별 정책 적용(apply_selection)과 '
                '원문 확보(fetch_documents)를 먼저 실행했는지 확인하세요.'
            ))
            return

        model = options['model']
        self.stdout.write(f'대상 {len(targets)}건 · 모델 {model}')

        if options['dry_run']:
            self._report_estimate(targets, model)
            return

        # 여기서부터는 돈이 나간다. 키 형식이 어긋났다면 **첫 호출 전에** 멈춘다 —
        # 건별 실패는 _run 안에서 삼켜지므로, 그대로 두면 배치 전체가 401을 건수만큼 맞는다.
        try:
            check_api_key()
        except SummarizerError as exc:
            raise CommandError(str(exc)) from exc

        if options['regenerate'] and not _regeneration_supported():
            self.stdout.write(self.style.WARNING(
                '--regenerate를 켰지만 summarizer가 아직 교정 재생성을 지원하지 않습니다'
                f'(summarize_disclosure에 {CORRECTION_KWARG} 키워드 없음). '
                '재생성 없이 진행합니다.'
            ))

        self._run(targets, model, options['resummarize'], options['regenerate'])

    # --- 대상 선정 -------------------------------------------------------

    def _select_targets(self, options):
        """선별 대상 + 원문 확보 완료 + (요약 없음) 조건을 만족하는 공시.

        여기에 실패 재시도 정책이 함께 걸린다 — 실패한 공시를 곧바로 다시 부르지 않고,
        상한에서 멈춘다. 사람이 명시적으로 지시한 실행(--rcept-no·--resummarize 계열)은
        정책을 건너뛴다. 정책은 자동 실행(cron)을 위한 것이지 사람의 판단을 막는
        장치가 아니다.
        """
        qs = (
            Disclosure.objects
            .filter(selection_state=SelectionState.TARGET, raw_fetched=True)
            .exclude(raw_content='')
            .select_related('company')
            .order_by('filed_at', 'rcept_no')
        )
        if not options['resummarize']:
            qs = qs.filter(summary__isnull=True)
        if options['only_warned']:
            qs = qs.filter(summary__isnull=False).exclude(summary__review_warnings=[])
        if options['stale_prompt']:
            # 이미 현재 프롬프트로 만든 요약은 다시 부르지 않는다. 전량 재요약이
            # 도중에 끊겨도 재실행이 남은 것부터 이어받게 하는 장치다 —
            # --limit 은 filed_at 오름차순이라 **오래된 것부터** 골라 이 용도로 쓸 수 없다.
            qs = (qs.filter(summary__isnull=False)
                    .exclude(summary__prompt_version=PROMPT_VERSION))
        if options['disclosure_type']:
            qs = qs.filter(disclosure_type=options['disclosure_type'])
        if options['rcept_no']:
            qs = qs.filter(rcept_no=options['rcept_no'])
        explicit = options['rcept_no'] or options['resummarize']
        if not explicit:
            qs = self._apply_retry_policy(qs)
        if options['limit']:
            qs = qs[:options['limit']]
        return list(qs)

    def _apply_retry_policy(self, queryset):
        """실패 후 대기 중이거나 상한에 걸린 공시를 대상에서 뺀다.

        대기 시간이 시도 횟수마다 달라 SQL 한 줄로 거르기 어렵다. 상한 미만인 것만
        DB에서 좁힌 뒤(대부분 여기서 걸러진다) 대기 판정만 파이썬에서 한다.
        """
        now = timezone.now()
        ready = []
        for disclosure in queryset.filter(summary_attempts__lt=MAX_SUMMARY_ATTEMPTS):
            attempts = disclosure.summary_attempts
            if attempts and disclosure.summary_attempted_at:
                index = min(attempts, len(SUMMARY_RETRY_BACKOFF_MINUTES)) - 1
                due = disclosure.summary_attempted_at + timedelta(
                    minutes=SUMMARY_RETRY_BACKOFF_MINUTES[index]
                )
                if now < due:
                    continue
            ready.append(disclosure)
        return ready

    # --- 실패 기록 · 멈춘 건 -------------------------------------------

    def _record_failure(self, disclosure, exc):
        """실패를 기록하고 누적 시도 횟수를 돌려준다."""
        disclosure.summary_attempts += 1
        disclosure.summary_attempted_at = timezone.now()
        disclosure.summary_error = f'{type(exc).__name__}: {exc}'[:300]
        disclosure.save(update_fields=[
            'summary_attempts', 'summary_attempted_at', 'summary_error',
        ])
        return disclosure.summary_attempts

    def _clear_failures(self, disclosure):
        """성공했으니 실패 기록을 지운다.

        남겨두면 --stuck 목록에 계속 나타나고, 나중에 --resummarize 로 다시 만들 때
        지난 실패 횟수가 이어져 조기에 멈춘다.
        """
        if not (disclosure.summary_attempts or disclosure.summary_error):
            return
        disclosure.summary_attempts = 0
        disclosure.summary_attempted_at = None
        disclosure.summary_error = ''
        disclosure.save(update_fields=[
            'summary_attempts', 'summary_attempted_at', 'summary_error',
        ])

    def _stuck_queryset(self):
        return (
            Disclosure.objects
            .filter(selection_state=SelectionState.TARGET, summary__isnull=True,
                    summary_attempts__gte=MAX_SUMMARY_ATTEMPTS)
            .select_related('company')
            .order_by('filed_at', 'rcept_no')
        )

    def _report_stuck(self):
        """상한에 걸려 멈춘 공시를 보여준다. LLM을 호출하지 않는다."""
        stuck = list(self._stuck_queryset())
        if not stuck:
            self.stdout.write(self.style.SUCCESS(
                f'재시도 상한({MAX_SUMMARY_ATTEMPTS}회)에 걸린 공시가 없습니다.'
            ))
            return

        self.stdout.write(self.style.WARNING(
            f'재시도 상한({MAX_SUMMARY_ATTEMPTS}회)에 걸려 멈춘 공시 {len(stuck):,}건 '
            '— 화면에는 노출되지 않습니다'
        ))
        for disclosure in stuck:
            attempted = disclosure.summary_attempted_at
            when = f'{attempted:%Y-%m-%d %H:%M}' if attempted else '기록 없음'
            self.stdout.write(
                f'  {disclosure.rcept_no} [{disclosure.company.name}] '
                f'({disclosure.disclosure_type}) {disclosure.report_name[:40]}'
            )
            self.stdout.write(f'    마지막 시도 {when} · {disclosure.summary_error}')
        self.stdout.write('')
        self.stdout.write(self.style.WARNING(
            '원인을 고친 뒤 --retry-stuck 으로 되살릴 수 있습니다. 비용이 발생합니다.'
        ))

    def _reset_stuck(self):
        updated = self._stuck_queryset().update(
            summary_attempts=0, summary_attempted_at=None, summary_error='',
        )
        self.stdout.write(self.style.SUCCESS(
            f'{updated:,}건의 시도 기록을 지웠습니다. 다음 실행에서 다시 시도합니다.'
        ))

    # --- 비용 추정 -------------------------------------------------------

    def _report_estimate(self, targets, model):
        """LLM 호출 없이 유형별 토큰·비용을 추정해 출력한다."""
        by_type = {}
        total_cost = total_tokens = 0.0
        for idx, d in enumerate(targets):
            # 첫 건만 캐시 미적중, 이후는 정적 접두사가 캐시에 적중한다고 가정
            est = estimate_summary_cost(d.raw_content, model=model, cache_hit=idx > 0)
            cost = est['usd']
            tokens = count_tokens(d.raw_content)
            row = by_type.setdefault(d.disclosure_type, {'n': 0, 'tok': 0, 'cost': 0.0})
            row['n'] += 1
            row['tok'] += tokens
            row['cost'] += cost
            total_cost += cost
            total_tokens += tokens

        self.stdout.write('')
        self.stdout.write(f'{"공시유형":<12} {"건수":>5} {"원문토큰":>12} {"예상비용(USD)":>14}')
        self.stdout.write('-' * 48)
        for name, row in sorted(by_type.items(), key=lambda kv: -kv[1]['cost']):
            self.stdout.write(
                f'{name:<12} {row["n"]:>5} {row["tok"]:>12,} {row["cost"]:>14.4f}'
            )
        self.stdout.write('-' * 48)
        self.stdout.write(
            f'{"합계":<12} {len(targets):>5} {int(total_tokens):>12,} {total_cost:>14.4f}'
        )
        self.stdout.write('')
        self.stdout.write(self.style.WARNING(
            'LLM을 호출하지 않았습니다. 실제 요약은 --dry-run 없이 실행하세요.'
        ))

    # --- 실행 -----------------------------------------------------------

    def _run(self, targets, model, resummarize, regenerate=False):
        ok = failed = 0
        cost = 0.0
        tokens_in = tokens_out = cached = 0
        flagged = []
        hidden = []
        failures = []

        for idx, d in enumerate(targets, start=1):
            label = f'[{idx}/{len(targets)}] {d.company.name} {d.report_name[:40]}'
            try:
                result = self._summarize_one(d, model, resummarize, regenerate)
            except SummarizerError as exc:
                # 건별 실패는 예외로 죽지 않고 기록 후 계속 진행한다.
                failed += 1
                attempts = self._record_failure(d, exc)
                left = MAX_SUMMARY_ATTEMPTS - attempts
                note = (
                    f'재시도 {left}회 남음' if left > 0
                    else '상한 도달 — 더 시도하지 않고 화면에서도 뺍니다'
                )
                failures.append((d.rcept_no, type(exc).__name__, str(exc)))
                self.stdout.write(self.style.ERROR(f'{label} → 실패: {exc} ({note})'))
                continue

            self._clear_failures(d)
            ok += 1
            usage = result['usage']
            cost += result['cost_usd']
            tokens_in += usage['input_tokens']
            tokens_out += usage['output_tokens']
            cached += usage['cached_tokens']

            warn = result['unsupported_numbers'] or [
                e for e in result['evidence'] if not e.get('quote_found', True)
            ]
            if warn:
                flagged.append(d.rcept_no)
            if result.get('auto_hidden'):
                hidden.append(d.rcept_no)
            mark = ' ⚠' if warn else ''
            mark += ' [미게시]' if result.get('auto_hidden') else ''
            retried = result.get('regeneration_count', 0)
            mark += f' [재생성 {retried}회]' if retried else ''
            self.stdout.write(
                f'{label} → {result["importance"]}{mark} '
                f'(입력 {usage["input_tokens"]:,} / 출력 {usage["output_tokens"]:,} / '
                f'${result["cost_usd"]:.4f})'
            )

        self._report_run(ok, failed, cost, tokens_in, tokens_out, cached, flagged,
                         hidden, failures)

    def _summarize_one(self, disclosure, model, resummarize, regenerate=False):
        """공시 1건을 요약해 저장하고 결과 dict를 반환한다.

        흐름: 생성 → 검증 → (막히면) 재생성 1회 → 그래도 막히면 미게시로 저장.
        """
        call = dict(
            company_name=disclosure.company.name,
            report_name=disclosure.report_name,
            filed_at=disclosure.filed_at,
            rcept_no=disclosure.rcept_no,
            raw_text=disclosure.raw_content,
            disclosure_type=disclosure.disclosure_type,
            model=model,
        )
        result = summarize_disclosure(**call)

        # 경고 문구는 build_review_warnings 가 단일 출처다
        # (재검증 명령과 판정이 어긋나지 않게 한다).
        warnings = build_review_warnings(result)
        blocking = blocking_warnings(warnings)

        history = []
        attempts = 0
        total_cost = result['cost_usd']
        if regenerate:
            result, warnings, blocking, history, attempts, extra_cost = self._regenerate(
                call, result, warnings, blocking,
            )
            total_cost += extra_cost

        fields = {
            'one_line': result['one_line'],
            'easy_explanation': result['easy_explanation'],
            'why_important': result['why_important'],
            'importance': result['importance'],
            'model_name': result['model_name'][:50],
            # summarizer 가 이미 돌려주는 값을 그대로 남긴다. 여기서 PROMPT_VERSION 을
            # 다시 읽으면 재생성 도중 상수가 바뀌었을 때 실제로 쓴 버전과 어긋난다.
            'prompt_version': result.get('prompt_version', '')[:10],
            'evidence': result.get('evidence', []),
            'review_warnings': warnings,
            # 검수 게이트는 공시 유형(Disclosure.review_category)과 경고가 정한다.
            # 요약을 만드는 쪽에서 검수 여부를 미리 정하지 않는다.
            'is_reviewed': False,
            # 검증에 막힌 요약은 **처음부터 비공개로** 저장한다. 사람이 확인해 풀기 전까지
            # 웹에 내보내지 않는다 — 검증 결과가 노출에 영향을 주게 만드는 지점이다.
            'is_published': not blocking,
            'hidden_by': DisclosureSummary.HiddenBy.AUTO if blocking else '',
            'hidden_reason': AUTO_HIDDEN_REASON if blocking else '',
            'regeneration_count': attempts,
            'regeneration_history': history,
        }
        with transaction.atomic():
            if resummarize:
                DisclosureSummary.objects.update_or_create(
                    disclosure=disclosure, defaults=fields,
                )
            else:
                DisclosureSummary.objects.create(disclosure=disclosure, **fields)

        result = dict(result)
        result['auto_hidden'] = bool(blocking)
        result['regeneration_count'] = attempts
        result['cost_usd'] = total_cost
        return result

    # --- 재생성 -----------------------------------------------------------

    def _regenerate(self, call, result, warnings, blocking):
        """검증에 막힌 요약을 경고를 되먹여 다시 생성한다.

        반환: (결과, 경고, 막는 경고, 이력, 시도 횟수, 추가 비용)

        ## 상한이 이 함수의 존재 이유다

        재생성 여부는 검증 결과가 정하는데, 검증은 계속 실패할 수 있다. 상한이 없으면
        한 건이 LLM을 무한히 부른다. 그래서 `review_policy.should_regenerate` 가
        **매 회차마다** 상한을 다시 확인하고, 이 루프는 그 판정에만 따른다.
        상한 판정을 여기 인라인으로 적지 않은 이유는, 호출부가 늘어날 때 한 곳만
        빠뜨려도 비용이 새기 때문이다.

        ## ai-prompt 와의 접합면

        교정 프롬프트는 ai-prompt 소유다(`summarizer.py`). 이 함수는 `summarize_disclosure`
        에 `correction_warnings=[...]` 키워드를 넘기는 것까지만 한다. 원 프롬프트에 경고를
        덧붙이든 별도 교정 프롬프트를 쓰든, 그 선택은 summarizer 안에서 하면 되고
        여기는 바뀌지 않는다.

        그 키워드가 아직 없으면 재생성을 **건너뛴다**. 없는 기능을 흉내 내 빈 요약을
        만들면 비용만 쓰고 결과가 나빠진다.
        """
        history = []
        attempts = 0
        extra_cost = 0.0
        if not _regeneration_supported():
            return result, warnings, blocking, history, attempts, extra_cost

        while should_regenerate(blocking, attempts):
            attempts += 1
            entry = {
                'attempt': attempts,
                'at': timezone.now().isoformat(),
                'reason': 'blocking_warnings',
                'warnings': list(blocking),
            }
            correction = {CORRECTION_KWARG: blocking}
            if _previous_supported():
                correction[PREVIOUS_KWARG] = result
            try:
                retried = summarize_disclosure(**call, **correction)
            except SummarizerError as exc:
                # 재생성 실패는 최초 요약까지 버릴 이유가 못 된다. 이력만 남기고
                # 처음 결과를 그대로 쓴다(미게시 상태로 사람에게 넘어간다).
                entry['error'] = f'{type(exc).__name__}: {exc}'
                history.append(entry)
                break

            extra_cost += retried['cost_usd']
            retried_warnings = build_review_warnings(retried)
            retried_blocking = blocking_warnings(retried_warnings)
            entry['model'] = retried.get('model_name', '')
            entry['cost_usd'] = retried['cost_usd']
            entry['resolved'] = not retried_blocking
            entry['remaining_warnings'] = list(retried_blocking)
            history.append(entry)

            # 교정 결과가 더 나쁘면 되돌린다 — 재생성이 요약을 망가뜨리면 안 된다.
            if len(retried_blocking) > len(blocking):
                entry['rolled_back'] = True
                break

            result, warnings, blocking = retried, retried_warnings, retried_blocking

        return result, warnings, blocking, history, attempts, extra_cost

    # --- 보고 -----------------------------------------------------------

    def _report_run(self, ok, failed, cost, tokens_in, tokens_out, cached, flagged,
                    hidden, failures):
        self.stdout.write('')
        if failures:
            self.stdout.write(self.style.ERROR(f'실패 {len(failures)}건:'))
            for rcept_no, kind, msg in failures:
                self.stdout.write(f'  {rcept_no} {kind}: {msg}')
        if flagged:
            self.stdout.write(self.style.WARNING(
                f'자동 검증 경고 {len(flagged)}건 — admin에서 검수 필요: '
                + ', '.join(flagged[:10]) + (' 외' if len(flagged) > 10 else '')
            ))
        if hidden:
            self.stdout.write(self.style.ERROR(
                f'자동 미게시 {len(hidden)}건 — 검증 실패로 웹에 노출하지 않습니다: '
                + ', '.join(hidden[:10]) + (' 외' if len(hidden) > 10 else '')
            ))
        cache_pct = (cached / tokens_in * 100) if tokens_in else 0
        self.stdout.write(self.style.SUCCESS(
            f'완료: 요약 {ok}건 생성, 실패 {failed}건 · '
            f'입력 {tokens_in:,}토큰(캐시 적중 {cached:,} · {cache_pct:.0f}%) / '
            f'출력 {tokens_out:,}토큰 · 실제 비용 ${cost:.4f}'
        ))
