"""재시도를 기다리다 지금 처리할 수 있게 된 후속 작업이 있는지 답한다.

`deploy/pipeline.sh`가 감지 폴링 뒤에 부른다. 신규 공시가 없어도 **원문 확보나 요약이
밀려 있으면** 뒷단계를 이어서 돌아야 하는데, 그 판단을 스크립트가 할 수 없어서 만든
명령이다. 배경과 실측은 `disclosures/retry_policy.py` 첫 주석에 있다.

DART도 LLM도 부르지 않는다 — DB만 본다. 1분마다 실행되므로 그래야 한다.

사용법:
  python manage.py pending_work

종료 코드:
  0  지금 처리할 후속 작업이 있다 (스크립트가 뒷단계로 넘어간다)
  9  없다 (스크립트가 여기서 끝낸다)
"""
import sys

from django.core.management.base import BaseCommand

from disclosures import retry_policy

#: 처리할 것이 없을 때의 종료 코드. poll_dart.NOTHING_NEW_EXIT_CODE와 같은 값을 쓴다 —
#: 스크립트 쪽에서 "할 일 없음"을 한 가지 값으로만 다루면 분기가 하나로 줄어든다.
#: 0·1을 피하는 이유도 같다: 1은 CommandError라, 겹치면 진짜 오류를 "할 일 없음"으로
#: 삼켜 파이프라인이 조용히 멈춘다.
NOTHING_PENDING_EXIT_CODE = 9


class Command(BaseCommand):
    help = ('재시도 대기가 끝나 지금 처리할 수 있는 후속 작업이 있는지 확인한다 '
            '(DART·LLM 미호출). 없으면 종료 코드 %d' % NOTHING_PENDING_EXIT_CODE)

    def handle(self, *args, **options):
        counts = retry_policy.pending_counts()
        ready = counts['원문'] + counts['요약']

        if not ready:
            held = ' · '.join(
                f'{label} {counts[label]:,}건'
                for label in ('대기', '상한') if counts[label]
            )
            self.stdout.write(
                f'대기 중인 후속 작업 없음{f" ({held})" if held else ""}'
            )
            sys.exit(NOTHING_PENDING_EXIT_CODE)

        self.stdout.write(
            f'후속 작업 있음: 원문 확보 {counts["원문"]:,}건 · '
            f'요약 {counts["요약"]:,}건'
        )
