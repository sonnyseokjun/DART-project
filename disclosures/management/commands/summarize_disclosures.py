"""요약 대상 공시의 전처리된 원문을 LLM으로 요약해 DisclosureSummary에 적재한다.

선별 정책(selection.py)이 대상으로 판정하고 원문이 확보된 공시만 처리한다.
요약이 이미 있으면 LLM을 재호출하지 않는다 — 공시당 1회 원칙(PLAN.md 11)의 실행 지점이다.
원문 확보(fetch_documents)와 분리되어 있어 요약만 재시도할 수 있다.

사용법:
  python manage.py summarize_disclosures --dry-run          # 비용 추정만 (LLM 미호출)
  python manage.py summarize_disclosures --limit 5          # 5건만 요약
  python manage.py summarize_disclosures --type 거래소공시    # 특정 유형만
  python manage.py summarize_disclosures                    # 대상 전체

추후 Celery 태스크로 옮길 때는 _summarize_one()을 그대로 태스크 본문으로 쓰면 된다.
"""
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from disclosures.models import Disclosure, DisclosureSummary
from disclosures.selection import SelectionState
from disclosures.summarizer import (
    DEFAULT_MODEL,
    SummarizerError,
    build_review_warnings,
    count_tokens,
    estimate_summary_cost,
    summarize_disclosure,
)


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
            '--model', default=DEFAULT_MODEL,
            help=f'사용할 모델 (기본 {DEFAULT_MODEL})',
        )
        parser.add_argument(
            '--resummarize', action='store_true',
            help='이미 요약이 있어도 다시 생성한다 (기존 요약을 덮어씀). '
                 '비용이 다시 발생하므로 프롬프트를 고쳤을 때만 사용한다',
        )

    def handle(self, *args, **options):
        if options['limit'] is not None and options['limit'] < 1:
            raise CommandError('--limit은 1 이상이어야 합니다.')

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

        self._run(targets, model, options['resummarize'])

    # --- 대상 선정 -------------------------------------------------------

    def _select_targets(self, options):
        """선별 대상 + 원문 확보 완료 + (요약 없음) 조건을 만족하는 공시."""
        qs = (
            Disclosure.objects
            .filter(selection_state=SelectionState.TARGET, raw_fetched=True)
            .exclude(raw_content='')
            .select_related('company')
            .order_by('filed_at', 'rcept_no')
        )
        if not options['resummarize']:
            qs = qs.filter(summary__isnull=True)
        if options['disclosure_type']:
            qs = qs.filter(disclosure_type=options['disclosure_type'])
        if options['rcept_no']:
            qs = qs.filter(rcept_no=options['rcept_no'])
        if options['limit']:
            qs = qs[:options['limit']]
        return list(qs)

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

    def _run(self, targets, model, resummarize):
        ok = failed = 0
        cost = 0.0
        tokens_in = tokens_out = cached = 0
        flagged = []
        failures = []

        for idx, d in enumerate(targets, start=1):
            label = f'[{idx}/{len(targets)}] {d.company.name} {d.report_name[:40]}'
            try:
                result = self._summarize_one(d, model, resummarize)
            except SummarizerError as exc:
                # 건별 실패는 예외로 죽지 않고 기록 후 계속 진행한다.
                failed += 1
                failures.append((d.rcept_no, type(exc).__name__, str(exc)))
                self.stdout.write(self.style.ERROR(f'{label} → 실패: {exc}'))
                continue

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
            mark = ' ⚠' if warn else ''
            self.stdout.write(
                f'{label} → {result["importance"]}{mark} '
                f'(입력 {usage["input_tokens"]:,} / 출력 {usage["output_tokens"]:,} / '
                f'${result["cost_usd"]:.4f})'
            )

        self._report_run(ok, failed, cost, tokens_in, tokens_out, cached, flagged, failures)

    def _summarize_one(self, disclosure, model, resummarize):
        """공시 1건을 요약해 저장하고 결과 dict를 반환한다."""
        result = summarize_disclosure(
            company_name=disclosure.company.name,
            report_name=disclosure.report_name,
            filed_at=disclosure.filed_at,
            rcept_no=disclosure.rcept_no,
            raw_text=disclosure.raw_content,
            disclosure_type=disclosure.disclosure_type,
            model=model,
        )

        # 경고 문구는 summarizer.build_review_warnings 가 단일 출처다
        # (재검증 명령과 판정이 어긋나지 않게 한다).
        warnings = build_review_warnings(result)

        fields = {
            'one_line': result['one_line'],
            'easy_explanation': result['easy_explanation'],
            'why_important': result['why_important'],
            'importance': result['importance'],
            'model_name': result['model_name'][:50],
            'evidence': result.get('evidence', []),
            'review_warnings': warnings,
            # 중요도 '높음'은 사람 검수 게이트를 거친다(PLAN.md 5.3).
            # 경고가 붙은 요약도 중요도와 무관하게 검수 대상이다.
            'is_reviewed': False,
        }
        with transaction.atomic():
            if resummarize:
                DisclosureSummary.objects.update_or_create(
                    disclosure=disclosure, defaults=fields,
                )
            else:
                DisclosureSummary.objects.create(disclosure=disclosure, **fields)
        return result

    # --- 보고 -----------------------------------------------------------

    def _report_run(self, ok, failed, cost, tokens_in, tokens_out, cached, flagged, failures):
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
        cache_pct = (cached / tokens_in * 100) if tokens_in else 0
        self.stdout.write(self.style.SUCCESS(
            f'완료: 요약 {ok}건 생성, 실패 {failed}건 · '
            f'입력 {tokens_in:,}토큰(캐시 적중 {cached:,} · {cache_pct:.0f}%) / '
            f'출력 {tokens_out:,}토큰 · 실제 비용 ${cost:.4f}'
        ))
