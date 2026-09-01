"""공시 조회 화면 (PLAN.md 7장).

## 이 모듈의 절대 규칙 — DART·LLM을 호출하지 않는다

뷰는 **로컬 DB만 읽는다**. `dart.py`·`summarizer.py`를 import 하지 않으며,
"원문이 없으면 그때 가져오자", "요약이 없으면 즉석에서 만들자" 같은 코드를 넣지 않는다.
사용자 요청과 외부 호출이 붙는 순간 DART 호출 수와 LLM 비용이 트래픽에 비례하게 되어
프로젝트의 핵심 설계가 무너진다(PLAN.md 12.1). 수집·요약은 관리 명령의 몫이다.

이 규칙은 ViewsDoNotCallExternalApisTest 가 import 수준에서 고정한다.
"""
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, render

from .models import (
    MAX_SUMMARY_ATTEMPTS, Company, Disclosure, DisclosureSummary, Sector,
)
from .selection import SelectionState

#: 목록 화면의 페이지당 공시 수.
PAGE_SIZE = 20

#: 메인 화면 상단에 띄우는 주요 공시 수.
HIGHLIGHT_COUNT = 5


def published_disclosures():
    """화면에 노출할 공시 큐리셋 — **노출 정책의 단일 출처**.

    두 부류를 노출한다.

    1. **요약이 준비된 공시** — 게시 중인 요약이 붙어 있다.
    2. **요약을 기다리는 공시** — 선별 대상이고 원문까지 확보됐는데 아직 요약이 없다.
       화면에는 "AI가 정리 중"으로 나간다(낙관적 렌더링, PLAN.md 9.3).
       단 요약을 상한만큼 실패한 공시는 뺀다 — 만들어지지 않을 요약을 기다리게 하면
       안 된다(`MAX_SUMMARY_ATTEMPTS`).

    선별에서 제외된 공시(표본 963건 중 823건)는 두 조건 모두에 걸리지 않아 자연히 빠진다.

    ## 왜 "원문 확보 완료"까지를 조건으로 두는가

    감지 즉시 노출하면 더 빠르지만, **원문을 끝내 못 받는 공시**(DART `[014]`)가
    "정리 중"인 채로 영원히 남는다. 재시도는 상한에서 멈추는데 카드는 멈추지 않기
    때문이다(fetch_documents.MAX_FETCH_ATTEMPTS). 원문이 손에 있는 것만 내보내면
    남은 단계가 요약뿐이라 이 상태가 오래갈 수 없다.

    실제 이득도 크지 않다 — deploy/pipeline.sh가 수집부터 요약까지 한 실행에서
    잇기 때문에 두 조건의 차이는 수십 초다.

    ## 미게시 요약은 사라진다

    미검수 요약은 노출한다. 현재 `is_reviewed=True`가 0건이라 검수분만 보이면 화면이
    완전히 비기 때문이다. 대신 검수가 필요한 요약에는 템플릿에서 배지를 단다
    (`DisclosureSummary.needs_review`).

    반대로 검수자가 숨겼거나 검증에 실패해 자동 미게시된 요약(`is_published=False`)은
    목록·상세·하이라이트 어디에도 내보내지 않는다. **"정리 중"으로도 되돌리지 않는다** —
    요약을 만들었으나 내보낼 수 없다는 뜻이지 아직 만드는 중이라는 뜻이 아니므로,
    그렇게 표시하면 사용자에게 오지 않을 것을 기다리게 한다. 카드만 남기고 내용을
    비우는 것도 "뭔가 있었는데 가려졌다"는 잘못된 신호라서 아예 제거한다.

    select_related 는 목록에서 카드마다 기업·요약을 참조하므로 필수다(N+1 방지).
    """
    ready = Q(summary__isnull=False, summary__is_published=True)
    pending = Q(
        summary__isnull=True,
        selection_state=SelectionState.TARGET,
        raw_fetched=True,
        # 요약을 상한만큼 실패한 공시는 뺀다. 만들어지지 않을 요약을 계속 기다리게
        # 하는 셈이기 때문이다(models.MAX_SUMMARY_ATTEMPTS).
        summary_attempts__lt=MAX_SUMMARY_ATTEMPTS,
    )
    return (
        Disclosure.objects
        .filter(ready | pending)
        .select_related('company', 'company__sector', 'summary')
    )


def _filter_by_importance(queryset, importance):
    """중요도 필터. 유효하지 않은 값은 무시한다(잘못된 쿼리스트링으로 500이 나면 안 된다).

    중요도를 고르면 "정리 중" 공시는 빠진다. 중요도는 요약이 매기는 값이라 아직 없기
    때문이다. 억지로 남기면 "높음만 보기"에 중요도 미상이 섞여 필터가 거짓말을 한다.
    """
    valid = {choice.value for choice in DisclosureSummary.Importance}
    if importance in valid:
        return queryset.filter(summary__importance=importance)
    return queryset


def _filter_by_company(queryset, stock_code):
    """기업 필터. 존재하지 않는 종목코드면 빈 결과가 되는 게 자연스럽다."""
    if stock_code:
        return queryset.filter(company__stock_code=stock_code)
    return queryset


def _paginate(request, queryset):
    return Paginator(queryset, PAGE_SIZE).get_page(request.GET.get('page'))


def _importance_options():
    """템플릿 필터 UI용 (값, 라벨) 목록."""
    return [
        {'value': choice.value, 'label': choice.label}
        for choice in DisclosureSummary.Importance
    ]


def sector_list(request):
    """메인 — 섹터 카드 + 최신 주요 공시 하이라이트."""
    sectors = (
        Sector.objects
        .annotate(
            company_count=Count('companies', distinct=True),
            # 요약이 있고 숨기지 않은 공시만 세어야 화면에 보이는 수와 일치한다.
            # 조건은 published_disclosures()의 필터와 반드시 같이 움직여야 한다
            # (집계는 큐리셋 필터로 대체할 수 없어 여기서 한 번 더 적는다).
            summary_count=Count(
                'companies__disclosures',
                filter=Q(
                    companies__disclosures__summary__isnull=False,
                    companies__disclosures__summary__is_published=True,
                ),
                distinct=True,
            ),
        )
        .order_by('name')
    )
    highlights = published_disclosures().filter(
        summary__importance=DisclosureSummary.Importance.HIGH
    )[:HIGHLIGHT_COUNT]

    return render(request, 'disclosures/sector_list.html', {
        'sectors': sectors,
        'highlights': highlights,
    })


def sector_detail(request, slug):
    """섹터 상세 — 소속 기업의 공시 통합 피드. 기업·중요도 필터."""
    sector = get_object_or_404(Sector, slug=slug)
    companies = sector.companies.filter(is_active=True).order_by('name')

    disclosures = published_disclosures().filter(company__sector=sector)
    selected_company = request.GET.get('company', '')
    selected_importance = request.GET.get('importance', '')
    disclosures = _filter_by_company(disclosures, selected_company)
    disclosures = _filter_by_importance(disclosures, selected_importance)

    return render(request, 'disclosures/sector_detail.html', {
        'sector': sector,
        'companies': companies,
        'page_obj': _paginate(request, disclosures),
        'total_count': disclosures.count(),
        'selected_company': selected_company,
        'selected_importance': selected_importance,
        'importance_options': _importance_options(),
    })


def company_detail(request, stock_code):
    """기업 상세 — 기업 개황 + 공시 타임라인. 중요도 필터."""
    company = get_object_or_404(
        Company.objects.select_related('sector'), stock_code=stock_code
    )

    disclosures = published_disclosures().filter(company=company)
    selected_importance = request.GET.get('importance', '')
    disclosures = _filter_by_importance(disclosures, selected_importance)

    return render(request, 'disclosures/company_detail.html', {
        'company': company,
        'page_obj': _paginate(request, disclosures),
        'total_count': disclosures.count(),
        'selected_importance': selected_importance,
        'importance_options': _importance_options(),
    })


def disclosure_detail(request, rcept_no):
    """공시 상세 — 한 줄 요약 → 쉬운 설명 → 왜 중요한가 → 원문 근거 → DART 원문 링크.

    노출 대상이 아닌 공시는 404다(published_disclosures가 단일 출처).

    요약을 기다리는 중인 공시는 404가 아니라 "정리 중" 화면을 준다. 목록에 카드가
    보이는데 눌렀더니 404가 나는 것은 사용자에게 고장으로 읽힌다. 이때도 DART 원문
    링크는 그대로 내보낸다 — 요약이 없을수록 원문 경로가 더 필요하다(PLAN.md 5.3).

    여기서 요약을 생성하지 않는다 — 모듈 docstring 참고.
    """
    disclosure = get_object_or_404(published_disclosures(), rcept_no=rcept_no)
    summary = None if disclosure.is_summary_pending else disclosure.summary

    # 근거는 원문에서 확인된 인용만 보여준다. 검증에 실패한 인용을 그대로 노출하면
    # 사용자가 원문에서 찾지 못해 오히려 신뢰를 떨어뜨린다(자동 검증 경고 자체는
    # 검수용 내부 지표이므로 화면에 내보내지 않는다).
    evidence = [
        item for item in (summary.evidence or [])
        if item.get('quote') and item.get('quote_found', True)
    ] if summary else []

    return render(request, 'disclosures/disclosure_detail.html', {
        'disclosure': disclosure,
        'summary': summary,
        'evidence': evidence,
    })
