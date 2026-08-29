"""근거(evidence) 적재 상태를 집계한다. LLM을 호출하지 않고 DB만 읽는다.

프롬프트를 고쳐 전량 재요약을 하기 전과 후에 같은 명령을 돌려 **효과를 수치로**
비교하기 위한 것이다. v3 → v4 개정이 이 명령 없이 진행됐다면 "좋아진 것 같다" 외에
할 수 있는 말이 없었다.

집계 항목은 셋이다.
  - 근거 칸 수 분포 — 상한에 눌려 있는지 본다. v3에서는 140건 중 101건(72%)이
    정확히 8칸에서 멈췄고, 그것이 상한을 12로 올린 근거였다.
  - 중복 인용 — 앞선 근거가 이미 담고 있는 구간을 다시 인용한 것(`duplicate_of`).
    칸을 낭비하므로 상한 병목과 같은 문제의 뒤쪽이다.
  - 경고 유형 — 상한 확대가 겨냥하는 것은 "인용에 없는 수치"뿐이다. 다른 유형이
    함께 줄기를 기대하면 효과를 잘못 읽는다.

`duplicate_of` 는 `revalidate_summaries` 가 채운다. 값이 없는 요약이 있으면 먼저
재검증을 돌리라고 알린다 — 0건으로 보고해 "중복이 없다"고 오해하는 편보다 낫다.
"""
import re
from collections import Counter

from django.core.management.base import BaseCommand

from disclosures.models import DisclosureSummary
from disclosures.summarizer import SUMMARY_JSON_SCHEMA

#: 경고 문구에서 수치·인덱스를 지워 유형만 남기는 패턴. 문구에 값이 박혀 있어
#: 그대로 세면 전부 다른 유형으로 잡힌다.
_DIGITS = re.compile(r'[0-9][0-9,.]*')
_INDEX = re.compile(r'\(근거 \d+번\)')


def _warning_kind(warning):
    """경고 문구에서 유형만 남긴다. 콜론 앞이 접두어이므로 그 앞을 쓴다."""
    head = _INDEX.sub('', warning).split(':')[0].strip()
    return _DIGITS.sub('N', head) or '(빈 경고)'


class Command(BaseCommand):
    help = '요약 근거(evidence)의 칸 수·중복·경고 분포를 집계한다 (읽기 전용)'

    def handle(self, *args, **options):
        summaries = list(
            DisclosureSummary.objects.values_list(
                'id', 'evidence', 'review_warnings', 'prompt_version'
            )
        )
        if not summaries:
            self.stdout.write('요약이 없습니다.')
            return

        cap = SUMMARY_JSON_SCHEMA['properties']['evidence']['maxItems']
        sizes = Counter()
        versions = Counter()
        kinds = Counter()
        at_cap = dup_summaries = dup_items = unjudged = warned = 0

        for _pk, evidence, warnings, version in summaries:
            evidence = evidence or []
            sizes[len(evidence)] += 1
            versions[version or '(없음)'] += 1
            if len(evidence) >= cap:
                at_cap += 1
            if any('duplicate_of' not in item for item in evidence):
                unjudged += 1
            hits = sum(
                1 for item in evidence if isinstance(item.get('duplicate_of'), int)
                and not isinstance(item.get('duplicate_of'), bool)
            )
            if hits:
                dup_summaries += 1
                dup_items += hits
            if warnings:
                warned += 1
                for warning in warnings:
                    kinds[_warning_kind(warning)] += 1

        total = len(summaries)
        self.stdout.write(f'요약 {total}건  (프롬프트 {dict(versions)})')
        self.stdout.write('')

        self.stdout.write(f'근거 칸 수 분포 (현재 상한 {cap})')
        for size in sorted(sizes):
            mark = '  <- 상한' if size >= cap else ''
            self.stdout.write(
                f'  {size:2d}칸 : {sizes[size]:3d}건  {"#" * sizes[size]}{mark}'
            )
        self.stdout.write(
            f'  상한에 닿은 요약 : {at_cap}건 ({self._pct(at_cap, total)})'
        )
        self.stdout.write('')

        self.stdout.write('중복 인용 (앞선 근거가 이미 담고 있는 구간)')
        if unjudged:
            self.stdout.write(
                f'  ⚠ {unjudged}건은 아직 판정 전이다. '
                'revalidate_summaries 를 먼저 돌려야 정확한 수치가 나온다.'
            )
        self.stdout.write(
            f'  중복이 있는 요약 : {dup_summaries}건 '
            f'({self._pct(dup_summaries, total)}) · 중복 근거 {dup_items}개'
        )
        self.stdout.write('')

        self.stdout.write(f'경고 (경고 있는 요약 {warned}건 · {self._pct(warned, total)})')
        if not kinds:
            self.stdout.write('  없음')
        for kind, count in kinds.most_common():
            self.stdout.write(f'  {count:3d}  {kind}')

    @staticmethod
    def _pct(part, whole):
        return f'{round(part * 100 / whole)}%' if whole else '-'
