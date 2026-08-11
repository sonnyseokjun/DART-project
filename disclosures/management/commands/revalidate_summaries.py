"""이미 저장된 요약의 검증 결과(evidence 판정·review_warnings)를 다시 계산한다.

**LLM을 호출하지 않는다.** 요약 본문은 그대로 두고, 원문 대조 결과만 현재의 검증 규칙으로
다시 매긴다. 검증 규칙(summarizer.verify_evidence·validate_summary)을 고쳤을 때
기존 요약에 소급 적용하는 용도다. 비용은 0원이며 몇 번을 돌려도 안전하다.

요약을 다시 만들려면 이 명령이 아니라 `summarize_disclosures --resummarize` 를 쓴다
(그쪽은 LLM을 재호출하므로 비용이 든다).

사용법:
  python manage.py revalidate_summaries --dry-run   # 변화만 보고, 저장하지 않음
  python manage.py revalidate_summaries             # 전체 재검증 후 저장
  python manage.py revalidate_summaries --rcept-no 20260601000172
"""
from django.core.management.base import BaseCommand
from django.db import transaction

from disclosures.models import DisclosureSummary
from disclosures.summarizer import build_review_warnings, validate_summary


class Command(BaseCommand):
    help = '저장된 요약의 원문 대조 결과를 다시 계산한다 (LLM 미호출)'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run', action='store_true',
            help='저장하지 않고 경고 건수 변화만 출력',
        )
        parser.add_argument(
            '--rcept-no', dest='rcept_no', default=None,
            help='특정 접수번호 1건만 재검증',
        )
        parser.add_argument(
            '--verbose-diff', action='store_true',
            help='경고가 바뀐 요약을 건별로 출력',
        )

    def handle(self, *args, **options):
        summaries = self._select(options)
        if not summaries:
            self.stdout.write(self.style.WARNING('재검증할 요약이 없습니다.'))
            return

        self.stdout.write(f'재검증 대상 {len(summaries)}건 (LLM 호출 없음)')

        before_flagged = sum(1 for s in summaries if s.review_warnings)
        after_flagged = 0
        changed = 0
        skipped = 0

        for summary in summaries:
            raw_text = summary.disclosure.raw_content
            if not raw_text:
                # 원문이 없으면 대조 자체가 불가능하다. 기존 판정을 건드리지 않는다.
                skipped += 1
                if summary.review_warnings:
                    after_flagged += 1
                continue

            warnings, evidence = self._revalidate(summary, raw_text)
            if warnings:
                after_flagged += 1

            if warnings == summary.review_warnings and evidence == summary.evidence:
                continue

            changed += 1
            if options['verbose_diff']:
                self.stdout.write(
                    f'  {summary.disclosure.rcept_no} '
                    f'[{summary.disclosure.company.name}] '
                    f'{summary.disclosure.report_name[:36]}: '
                    f'경고 {len(summary.review_warnings)} → {len(warnings)}'
                )
            if not options['dry_run']:
                summary.review_warnings = warnings
                summary.evidence = evidence
                with transaction.atomic():
                    summary.save(update_fields=['review_warnings', 'evidence'])

        self._report(summaries, before_flagged, after_flagged, changed, skipped, options)

    def _select(self, options):
        qs = (
            DisclosureSummary.objects
            .select_related('disclosure', 'disclosure__company')
            .order_by('disclosure__filed_at', 'disclosure__rcept_no')
        )
        if options['rcept_no']:
            qs = qs.filter(disclosure__rcept_no=options['rcept_no'])
        return list(qs)

    def _revalidate(self, summary, raw_text):
        """저장된 요약을 validate_summary에 되먹여 판정만 다시 받는다.

        validate_summary는 생성 직후 검증과 같은 함수다. 재검증 전용 경로를 따로 두면
        두 경로의 규칙이 어긋날 수 있으므로 의도적으로 같은 함수를 쓴다.
        """
        data = {
            'one_line': summary.one_line,
            'easy_explanation': summary.easy_explanation,
            'why_important': summary.why_important,
            'importance': summary.importance,
            'evidence': summary.evidence or [],
        }
        result = validate_summary(data, raw_text)
        return build_review_warnings(result), result['evidence']

    def _report(self, summaries, before, after, changed, skipped, options):
        total = len(summaries)
        self.stdout.write('')
        self.stdout.write(f'  경고 있는 요약 : {before}건 → {after}건 (전체 {total}건)')
        if total:
            self.stdout.write(
                f'  경고 비율      : {before / total:.0%} → {after / total:.0%}'
            )
        self.stdout.write(f'  판정이 바뀐 요약: {changed}건')
        if skipped:
            self.stdout.write(self.style.WARNING(
                f'  원문 없어 건너뜀: {skipped}건 (fetch_documents 필요)'
            ))
        if options['dry_run']:
            self.stdout.write(self.style.WARNING('\n--dry-run: 저장하지 않았습니다.'))
        else:
            self.stdout.write(self.style.SUCCESS(f'\n{changed}건의 판정을 갱신했습니다.'))
