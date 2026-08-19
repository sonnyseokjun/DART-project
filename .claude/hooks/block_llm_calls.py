"""LLM 실호출을 차단하는 PreToolUse 훅.

`summarize_disclosures`는 이 프로젝트에서 사용자 비용이 실제로 발생하는 유일한 경로다.
언제 얼마를 쓸지는 사용자가 정하며, 에이전트가 검증 목적으로 임의 실행해서는 안 된다.
프롬프트 변경 효과는 코드·단위 테스트·모킹으로 검증하고, 실호출이 필요하면 리더에게 보고만 한다.

차단 대상은 "실행"뿐이다. 소스 파일 읽기·grep(`summarize_disclosures.py`)은 통과시킨다 —
조사까지 막으면 훅이 고장난 것처럼 보이고 에이전트가 우회를 시도하게 된다.

LLM을 부르지 않는 `revalidate_summaries`는 검증기 수정 효과 측정에 필요하므로 허용한다.

종료 코드 2 = 차단(stderr가 사유로 전달됨), 0 = 통과.
"""
import json
import re
import sys

# `manage.py summarize_disclosures ...` 형태의 실행만 잡는다.
# 파일 경로(`.../commands/summarize_disclosures.py`)에는 `manage.py `가 앞서지 않으므로 걸리지 않는다.
EXEC_PATTERN = re.compile(r'manage\.py\s+summarize_disclosures\b')

DENY_MESSAGE = (
    'summarize_disclosures 실행이 하네스 정책으로 차단되었습니다.\n'
    'LLM 실호출은 사용자 비용이 발생하는 유일한 경로이며, 실행 시점은 사용자가 정합니다.\n'
    '대안: (1) 프롬프트·스키마 변경은 단위 테스트와 모킹으로 검증한다. '
    '(2) 검증기 수정 효과는 LLM을 부르지 않는 revalidate_summaries 로 측정한다. '
    '(3) 실호출이 꼭 필요하면 실행하지 말고 리더에게 사유·예상 건수·예상 비용을 보고한다.'
)


def main():
    try:
        payload = json.loads(sys.stdin.buffer.read().decode('utf-8', 'replace') or '{}')
    except (json.JSONDecodeError, ValueError):
        # 훅이 입력을 못 읽었다고 정상 작업을 막지는 않는다 (fail-open).
        return 0

    tool_input = payload.get('tool_input') or {}
    command = tool_input.get('command') or ''

    if EXEC_PATTERN.search(command):
        sys.stderr.write(DENY_MESSAGE)
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
