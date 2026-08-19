"""수집된 공시에 제목 기반 규칙 정책 두 가지를 적용한다.

  1. 요약 대상 선별 (disclosures/selection.py) → selection_state / exclusion_reason
  2. 사람 검수 게이트 (disclosures/review_policy.py) → review_category

둘 다 LLM 호출이 없는 순수 규칙 판정이고 입력이 같으므로(공시 유형·제목) 한 번에 돈다.
따로 두면 정책을 고친 뒤 한쪽만 재적용하는 일이 생기고, 그 순간 검수 큐가 어긋난다.

판정을 DB에 기록하므로 재실행해도 이미 판정된 행은 건너뛴다 — 정책을 고친 뒤에는
--force로 전건을 재판정한다.

사용법:
  python manage.py apply_selection            # 미판정(pending) 행만 판정
  python manage.py apply_selection --force    # 전건 재판정 (정책 변경 후)
  python manage.py apply_selection --dry-run  # 저장 없이 분포만 확인
"""
import collections

from django.core.management.base import BaseCommand

from disclosures.models import Disclosure
from disclosures.review_policy import ReviewCategory
from disclosures.review_policy import evaluate as evaluate_review_gate
from disclosures.selection import ExclusionReason, SelectionState, evaluate

# 한 번에 저장할 행 수. SQLite 단일 파일이므로 과한 트랜잭션을 피한다.
BATCH_SIZE = 500


class Command(BaseCommand):
    help = '수집된 공시에 요약 대상 선별 정책을 적용한다'

    def add_arguments(self, parser):
        parser.add_argument(
            '--force', action='store_true',
            help='이미 판정된 공시도 다시 판정한다 (정책을 수정했을 때)',
        )
        parser.add_argument(
            '--dry-run', action='store_true',
            help='DB에 저장하지 않고 판정 결과만 출력한다',
        )

    def handle(self, *args, **options):
        force, dry_run = options['force'], options['dry_run']

        queryset = Disclosure.objects.all().order_by('pk')
        if not force:
            queryset = queryset.filter(selection_state=SelectionState.PENDING)

        total_all = Disclosure.objects.count()
        pending = list(queryset)
        skipped = total_all - len(pending)

        self.stdout.write(
            f'전체 공시 {total_all:,}건 · 판정 대상 {len(pending):,}건 · '
            f'건너뜀(판정 완료) {skipped:,}건'
            + (' · [dry-run]' if dry_run else '')
        )
        if not pending:
            self.stdout.write(self.style.WARNING(
                '판정할 공시가 없습니다. 정책을 수정했다면 --force로 재판정하세요.'
            ))
            self._report_distribution()
            return

        changed = []
        for disclosure in pending:
            state, reason = evaluate(disclosure.disclosure_type, disclosure.report_name)
            category = evaluate_review_gate(disclosure.report_name)
            if (disclosure.selection_state == state
                    and disclosure.exclusion_reason == reason
                    and disclosure.review_category == category):
                continue
            disclosure.selection_state = state
            disclosure.exclusion_reason = reason
            disclosure.review_category = category
            changed.append(disclosure)

        if dry_run:
            self.stdout.write(f'변경될 행 {len(changed):,}건 (저장하지 않음)')
        else:
            Disclosure.objects.bulk_update(
                changed,
                ['selection_state', 'exclusion_reason', 'review_category'],
                batch_size=BATCH_SIZE,
            )
            self.stdout.write(self.style.SUCCESS(f'판정 저장 완료: {len(changed):,}건 갱신'))

        self._report_distribution(overrides=pending if dry_run else None)
        self._report_review_gate(overrides=pending if dry_run else None)

    # --- 집계 출력 -------------------------------------------------------

    def _report_distribution(self, overrides=None):
        """대상/제외 건수, 제외 사유 분포, 유형별 대상 건수를 출력한다.

        overrides는 dry-run에서 아직 저장하지 않은 판정 결과를 반영하기 위한 목록이다.
        """
        rows = {
            d.pk: (d.disclosure_type, d.selection_state, d.exclusion_reason)
            for d in Disclosure.objects.all().only(
                'disclosure_type', 'selection_state', 'exclusion_reason'
            )
        }
        for d in overrides or []:
            rows[d.pk] = (d.disclosure_type, d.selection_state, d.exclusion_reason)

        states = collections.Counter(state for _t, state, _r in rows.values())
        reasons = collections.Counter(
            reason for _t, state, reason in rows.values()
            if state == SelectionState.EXCLUDED
        )
        by_type = collections.defaultdict(collections.Counter)
        for disclosure_type, state, _reason in rows.values():
            by_type[disclosure_type][state] += 1

        labels = dict(SelectionState.choices)
        self.stdout.write('')
        self.stdout.write(f'== 선별 결과 (전체 {len(rows):,}건) ==')
        for state, _label in SelectionState.choices:
            self.stdout.write(f'  {labels[state]:<10} {states.get(state, 0):>6,}건')

        reason_labels = dict(ExclusionReason.choices)
        self.stdout.write('')
        self.stdout.write('== 제외 사유 분포 ==')
        for reason, count in reasons.most_common():
            self.stdout.write(f'  {reason_labels.get(reason, reason or "(미기록)"):<30} {count:>6,}건')

        self.stdout.write('')
        self.stdout.write(f'== 공시유형별 (대상/전체) ==')
        for disclosure_type in sorted(by_type, key=lambda t: -sum(by_type[t].values())):
            counter = by_type[disclosure_type]
            self.stdout.write(
                f'  {disclosure_type or "(미분류)":<10} '
                f'{counter[SelectionState.TARGET]:>4,} / {sum(counter.values()):>4,}'
            )

    def _report_review_gate(self, overrides=None):
        """검수 필수 유형 분포. 게이트 정책을 고친 뒤 영향 범위를 바로 확인하기 위한 것이다."""
        rows = {
            d.pk: d.review_category
            for d in Disclosure.objects.all().only('review_category')
        }
        for d in overrides or []:
            rows[d.pk] = d.review_category

        counts = collections.Counter(rows.values())
        gated = sum(count for category, count in counts.items() if category)
        labels = dict(ReviewCategory.choices)

        self.stdout.write('')
        self.stdout.write(f'== 사람 검수 필수 유형 (전체 {len(rows):,}건 중 {gated:,}건) ==')
        for category, _label in ReviewCategory.choices:
            self.stdout.write(f'  {labels[category]:<20} {counts.get(category, 0):>6,}건')
        self.stdout.write(f'  {"(게이트 비대상)":<20} {counts.get("", 0):>6,}건')
