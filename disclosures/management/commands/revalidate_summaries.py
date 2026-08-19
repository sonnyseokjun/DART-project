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
from disclosures.verification import AUTO_HIDDEN_REASON, blocking_warnings


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
        newly_hidden = 0
        restored = 0

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

            publication = self._publication_fields(summary, warnings)
            unchanged = (
                warnings == summary.review_warnings
                and evidence == summary.evidence
                and not publication
            )
            if unchanged:
                continue

            changed += 1
            if publication:
                if publication['is_published']:
                    restored += 1
                else:
                    newly_hidden += 1
            if options['verbose_diff']:
                mark = ''
                if publication:
                    mark = ' · 게시 복구' if publication['is_published'] else ' · 자동 미게시'
                self.stdout.write(
                    f'  {summary.disclosure.rcept_no} '
                    f'[{summary.disclosure.company.name}] '
                    f'{summary.disclosure.report_name[:36]}: '
                    f'경고 {len(summary.review_warnings)} → {len(warnings)}{mark}'
                )
            if not options['dry_run']:
                summary.review_warnings = warnings
                summary.evidence = evidence
                fields = ['review_warnings', 'evidence']
                for name, value in (publication or {}).items():
                    setattr(summary, name, value)
                    fields.append(name)
                with transaction.atomic():
                    summary.save(update_fields=fields)

        self._report(summaries, before_flagged, after_flagged, changed, skipped,
                     newly_hidden, restored, options)

    def _publication_fields(self, summary, warnings):
        """게시 여부를 검증 결과에 맞춰 되계산한다. 바꿀 게 없으면 빈 dict.

        검증 결과가 노출에 아무 영향을 주지 않던 탓에, 39조를 3조로 적은 요약이 경고를
        단 채 웹에 그대로 떠 있었다(입력 명세 2.3). 요약 생성 경로만 고치면 **이미 저장된
        요약은 영원히 그 상태로 남는다** — 다시 만들려면 LLM 비용이 드니까. 그래서 재검증도
        같은 판정을 적용한다. 이 명령은 LLM을 부르지 않으므로 비용이 0이다.

        **사람이 내린 결정은 건드리지 않는다.** hidden_by='human'은 검수자가 판단해 내린
        것이고, 코드가 "경고가 사라졌으니 올리자"고 되돌리면 사람의 판단을 덮어쓴다.
        """
        blocking = blocking_warnings(warnings)
        if summary.hidden_by == DisclosureSummary.HiddenBy.HUMAN:
            return {}
        if blocking and summary.is_published:
            return {
                'is_published': False,
                'hidden_by': DisclosureSummary.HiddenBy.AUTO,
                'hidden_reason': AUTO_HIDDEN_REASON,
            }
        if not blocking and summary.hidden_by == DisclosureSummary.HiddenBy.AUTO:
            # 경고가 사라졌으면 자동으로 내렸던 것도 자동으로 되돌린다.
            return {'is_published': True, 'hidden_by': '', 'hidden_reason': ''}
        return {}

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

    def _report(self, summaries, before, after, changed, skipped,
                newly_hidden, restored, options):
        total = len(summaries)
        self.stdout.write('')
        self.stdout.write(f'  경고 있는 요약 : {before}건 → {after}건 (전체 {total}건)')
        if total:
            self.stdout.write(
                f'  경고 비율      : {before / total:.0%} → {after / total:.0%}'
            )
        self.stdout.write(f'  판정이 바뀐 요약: {changed}건')
        if newly_hidden or restored:
            self.stdout.write(
                f'  게시 상태 변경  : 자동 미게시 {newly_hidden}건 · 복구 {restored}건'
            )
        if skipped:
            self.stdout.write(self.style.WARNING(
                f'  원문 없어 건너뜀: {skipped}건 (fetch_documents 필요)'
            ))
        if options['dry_run']:
            self.stdout.write(self.style.WARNING('\n--dry-run: 저장하지 않았습니다.'))
        else:
            self.stdout.write(self.style.SUCCESS(f'\n{changed}건의 판정을 갱신했습니다.'))
