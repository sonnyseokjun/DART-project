"""저장된 요약의 금액 병기를 원문 숫자와 전수 대조한다. LLM을 호출하지 않는다.

검증기(verification.py)가 보는 것은 "요약의 수치가 원문 근거로 뒷받침되는가"다.
이 명령이 보는 것은 다른 질문이다 — **"괄호 안 병기가 앞의 숫자와 맞는가"**.

이 검사가 따로 필요한 이유는 실제 사고에서 나왔다. rcept_no 20260710000008 은
`40,023,070,290,000원(약 4조 원)` 을 **경고 0건으로 게시**했다. 검증기는 앞의 숫자가
원문에 있다는 것만 확인했고, 괄호 안이 10배 틀렸다는 것은 보지 않았다. 그 오류는
사람이 병기 25곳을 손으로 대조하다 우연히 찾았다.

우연에 기대지 않으려면 그 대조가 명령이어야 한다. 병기는 코드(`units.py`)가 만들므로
정답이 결정적으로 정해진다 — 같은 입력에 같은 출력이라 대조가 기계적으로 끝난다.
불일치가 나온다면 병기를 코드가 만들지 않았거나(LLM이 직접 썼거나) 사람이 검수 중
고쳤다는 뜻이고, 둘 다 확인 대상이다.

사용법:
  python manage.py audit_amounts            # 전수 대조, 불일치만 출력
  python manage.py audit_amounts --verbose  # 대조한 병기를 모두 출력
"""
import re

from django.core.management.base import BaseCommand

from disclosures.models import DisclosureSummary
from disclosures.units import format_korean_usd, format_korean_won

#: `123,456,789원(약 1억 2,345만 원)` 형태를 잡는다. 통화 접미사는 units.py 와 맞춘다.
#: 앞 숫자와 괄호 사이 공백은 허용한다 — 사람이 검수하며 띄어 쓸 수 있다.
_ANNOTATED = re.compile(
    r'(\d[\d,]*)\s*(원|USD|달러)\s*\((약\s*)?([^)]+)\)'
)

#: 통화별 정답 생성기. 병기를 만든 것과 **같은 함수**라야 대조가 의미를 갖는다.
_FORMATTERS = {'원': format_korean_won, 'USD': format_korean_usd, '달러': format_korean_usd}


def _canonical(text):
    """비교용 정규화. `약` 과 공백 차이는 불일치로 보지 않는다."""
    return text.replace('약', '').replace(' ', '').strip()


class Command(BaseCommand):
    help = '저장된 요약의 금액 병기를 원문 숫자와 대조한다 (LLM 미호출)'

    def add_arguments(self, parser):
        parser.add_argument(
            '--verbose', action='store_true',
            help='불일치뿐 아니라 대조한 병기를 전부 출력한다',
        )

    def handle(self, *args, **options):
        checked = 0
        mismatches = []

        for summary in DisclosureSummary.objects.select_related('disclosure').order_by('pk'):
            body = ' '.join([
                summary.one_line, summary.easy_explanation, summary.why_important,
            ])
            for match in _ANNOTATED.finditer(body):
                checked += 1
                value = int(match.group(1).replace(',', ''))
                formatter = _FORMATTERS[match.group(2)]
                expected = formatter(value)
                actual = match.group(0).split('(', 1)[1].rstrip(')')
                ok = _canonical(actual) == _canonical(expected)
                if not ok:
                    mismatches.append((summary, match.group(0), expected))
                if options['verbose']:
                    self.stdout.write(
                        f'  {"OK" if ok else "NG"}  {match.group(0)[:70]}'
                    )

        self.stdout.write('')
        if mismatches:
            self.stdout.write(self.style.ERROR(f'불일치 {len(mismatches)}건:'))
            for summary, found, expected in mismatches:
                self.stdout.write(
                    f'  pk {summary.pk} · {summary.disclosure.rcept_no} '
                    f'· {summary.disclosure.company.name}'
                )
                self.stdout.write(f'      본문: {found}')
                self.stdout.write(f'      정답: {expected}')
            self.stdout.write('')
            self.stdout.write(
                '병기는 코드가 만든다. 불일치는 LLM이 직접 썼거나 사람이 고쳤다는 뜻이다.'
            )
        else:
            self.stdout.write(self.style.SUCCESS('불일치 없음'))

        self.stdout.write(
            f'요약 {DisclosureSummary.objects.count()}건에서 병기 {checked}개를 대조했다.'
        )
