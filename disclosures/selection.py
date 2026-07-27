"""요약 대상 선별 정책 — 단일 출처.

LLM 비용의 대부분이 여기서 결정된다. 수집한 공시 전부를 요약하면 비용이 폭증하므로,
규칙(LLM 호출 없음)으로 "일반 투자자에게 설명할 가치가 있는 공시"만 골라낸다.
표본 963건 기준 약 86%가 여기서 걸러진다.

정책은 이 모듈 한 곳에만 둔다. 관리 명령·모델·테스트는 모두 evaluate()를 호출한다.

## 판정 순서

1. **제목 블랙리스트** (`TITLE_BLACKLIST`) — 정형·반복 공시. 정확 일치로 판정한다
2. **공시유형 제외** (`EXCLUDED_TYPES`) — 지분공시·기타공시는 기본 제외.
   단 `TYPE_WHITELIST_PREFIXES`에 걸리면 대상으로 되살린다
3. 나머지는 모두 대상

블랙리스트를 유형 제외보다 먼저 보는 이유는 제외 사유를 더 구체적으로 남기기 위해서다.
둘 다 해당하는 지분공시 정형 공시는 "정형 반복 공시"로 기록되어야 분포가 읽힌다.

## 제목 매칭 방식

- 블랙리스트는 **정확 일치**다. 부분 일치를 쓰면
  `임원ㆍ주요주주특정증권등거래계획보고서`(사전 매도계획 신고)가
  `임원ㆍ주요주주특정증권등소유상황보고서`(사후 신고)에 우연히 걸린다. 성격이 다른 공시다
- 화이트리스트는 **접두어 매칭**이다. `주식등의대량보유상황보고서(약식)`처럼 접미가,
  `[기재정정]자기주식처분결과보고서`처럼 접두가 붙어 정확 일치로는 잡히지 않는다
- 두 경우 모두 normalize_title()로 정규화한 뒤 비교한다:
  선행 대괄호 태그(`[기재정정]`·`[발행조건확정]` 등) 제거 + 모든 공백 제거
"""
import re

from django.db import models


class SelectionState(models.TextChoices):
    """공시의 요약 대상 여부. 미판정과 의도적 제외를 구분해야 재평가를 건너뛸 수 있다."""

    PENDING = 'pending', '미판정'
    TARGET = 'target', '요약 대상'
    EXCLUDED = 'excluded', '제외'


class ExclusionReason(models.TextChoices):
    """제외 사유. 대상이면 빈 문자열이다."""

    BLACKLIST = 'blacklist', '정형 반복 공시(제목 블랙리스트)'
    EXCLUDED_TYPE = 'excluded_type', '요약 가치 낮은 공시유형'


# --- 정책 상수 -----------------------------------------------------------

#: 기본 제외 공시유형. 나머지 8종(정기·주요사항·발행·외부감사·펀드·자산유동화·거래소·공정위)은 대상.
#: 지분공시는 850건 중 821건이 임원 소유상황 정형 신고이고, 기타공시는 대부분 절차 신고다.
EXCLUDED_TYPES = frozenset({'지분공시', '기타공시'})

#: 제목이 정확히 일치하면 무조건 제외. 내용이 정형화되어 있어 요약해도 정보가 없다.
TITLE_BLACKLIST = frozenset({'임원ㆍ주요주주특정증권등소유상황보고서'})

#: 유형 제외의 예외. {공시유형: (제목 접두어, ...)} — 접두어로 시작하면 대상으로 되살린다.
#: - 주식등의대량보유상황보고서: 5% 룰 신고. 지배구조·경영권 변동 신호라 일반 투자자에게 중요하다
#: - 임원ㆍ주요주주특정증권등거래계획보고서: 아래 "이름이 비슷한 두 서류" 참고
#: - 자기주식처분결과보고서: 주요사항보고서(자기주식처분결정)를 대상에 넣었으므로
#:   그 결과 보고도 함께 넣어야 사건이 끊기지 않는다
#:
#: ## 이름이 비슷한 두 서류를 반대로 취급하는 이유 (정리하지 말 것)
#:
#: `임원ㆍ주요주주특정증권등소유상황보고서`(블랙리스트 → 제외)와
#: `임원ㆍ주요주주특정증권등거래계획보고서`(화이트리스트 → 대상)는 이름이 거의 같지만
#: **정보의 방향이 반대**다.
#:   - 소유상황보고서: 이미 일어난 보유 변동의 **사후** 신고. 표본 963건 중 821건을 차지하는
#:     정형 대량 발생 서류로, 요약해도 새 정보가 없다
#:   - 거래계획보고서: 내부자가 앞으로 대량 매도를 하겠다고 알리는 **사전** 보고. 표본에 2건뿐이며,
#:     앞으로 일어날 일을 미리 알리므로 일반 투자자 관점의 신호 가치가 사후 신고보다 높다
#: 중복으로 보여 한쪽을 지우면 선별 정책이 깨진다. 두 서류가 갈리는 것은 테스트로 고정되어 있다.
TYPE_WHITELIST_PREFIXES = {
    '지분공시': (
        '주식등의대량보유상황보고서',
        '임원ㆍ주요주주특정증권등거래계획보고서',
    ),
    '기타공시': ('자기주식처분결과보고서',),
}

#: 제목 앞에 붙는 대괄호 태그(`[기재정정]`, `[발행조건확정]`, `[기재정정][첨부정정]` 등).
_BRACKET_PREFIX_RE = re.compile(r'^(?:\[[^\]]*\])+')
_WHITESPACE_RE = re.compile(r'\s+')


# --- 판정 ---------------------------------------------------------------

def normalize_title(report_name):
    """제목을 판정용으로 정규화한다.

    1) 모든 공백 제거 — DART 제목에는 `소송등의판결ㆍ결정        (주주총회결의취소)`처럼
       정렬용 연속 공백이 섞여 있다
    2) 선행 대괄호 태그 제거 — `[기재정정]`이 붙어도 같은 공시로 판정해야 한다
    """
    collapsed = _WHITESPACE_RE.sub('', report_name or '')
    return _BRACKET_PREFIX_RE.sub('', collapsed)


def is_blacklisted(report_name):
    """정규화한 제목이 블랙리스트와 **정확히** 일치하는지."""
    return normalize_title(report_name) in TITLE_BLACKLIST


def is_whitelisted(disclosure_type, report_name):
    """제외 유형이지만 화이트리스트 접두어로 시작해 대상으로 되살릴지."""
    prefixes = TYPE_WHITELIST_PREFIXES.get(disclosure_type)
    if not prefixes:
        return False
    normalized = normalize_title(report_name)
    return normalized.startswith(prefixes)


def evaluate(disclosure_type, report_name):
    """(선별 상태, 제외 사유)를 반환한다. 모델에 의존하지 않는 순수 함수."""
    if is_blacklisted(report_name):
        return SelectionState.EXCLUDED, ExclusionReason.BLACKLIST

    if disclosure_type in EXCLUDED_TYPES and not is_whitelisted(disclosure_type, report_name):
        return SelectionState.EXCLUDED, ExclusionReason.EXCLUDED_TYPE

    return SelectionState.TARGET, ''


def evaluate_disclosure(disclosure):
    """Disclosure 인스턴스에 정책을 적용한다(저장은 호출자 몫)."""
    return evaluate(disclosure.disclosure_type, disclosure.report_name)
