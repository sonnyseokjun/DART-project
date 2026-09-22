"""공시 조회 화면 (PLAN.md 7장).

## 이 모듈의 절대 규칙 — DART·LLM을 호출하지 않는다

뷰는 **로컬 DB만 읽는다**. `dart.py`·`summarizer.py`를 import 하지 않으며,
"원문이 없으면 그때 가져오자", "요약이 없으면 즉석에서 만들자" 같은 코드를 넣지 않는다.
사용자 요청과 외부 호출이 붙는 순간 DART 호출 수와 LLM 비용이 트래픽에 비례하게 되어
프로젝트의 핵심 설계가 무너진다(PLAN.md 12.1). 수집·요약은 관리 명령의 몫이다.

이 규칙은 ViewsDoNotCallExternalApisTest 가 import 수준에서 고정한다.
"""
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Count, Max, Q
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from . import ai_budget, summary_requests, watchlist
from .models import Company, Disclosure, DisclosureSummary, ListedCorp
from .selection import SelectionState

#: 목록 화면의 페이지당 공시 수.
PAGE_SIZE = 20

def published_disclosures(show_all=False):
    """화면에 노출할 공시 큐리셋 — **노출 정책의 단일 출처**.

    8단계부터 요약은 요청받은 공시만 만든다(이슈 #44). 그래서 "요약이 있는 것"만 보여주면
    버튼을 누를 공시가 목록에 없다. 노출 기준은 이렇다.

    - **기본(요약 대상)** — 요약이 있는 공시 + 요약 대상(선별 통과) 공시. 요약이 없으면
      카드에 상태가 붙는다: "AI 요약 보기" 버튼 · "AI가 정리 중" · "만들지 못함"
      (`Disclosure.summary_state`).
    - **전체(`show_all`)** — 요약 대상이 아닌 공시도 보인다. 공시의 대부분(약 85%)이
      임원 주식 보유 보고 같은 단순 보고라, 기본으로 보여주면 중요한 공시가 묻힌다.
      이것들은 제목과 DART 원문 링크만 나온다.

    ## 숨긴 요약은 어디에도 나오지 않는다

    검수자가 숨겼거나 검증에 실패해 자동 미게시된 요약(`is_published=False`)의 공시는
    두 경우 모두 빠진다. **"AI 요약 보기" 버튼으로도 되돌리지 않는다** — 요약을 만들었으나
    내보낼 수 없다는 뜻이지, 아직 없다는 뜻이 아니다. 버튼을 달면 다시 만들 수 없는
    요약을 요청하게 된다.

    미검수 요약은 노출한다. 검수가 필요한 요약에는 템플릿에서 배지를 단다
    (`DisclosureSummary.needs_review`).

    select_related 는 목록에서 카드마다 기업·요약을 참조하므로 필수다(N+1 방지).
    """
    queryset = Disclosure.objects.exclude(summary__is_published=False)
    if not show_all:
        queryset = queryset.filter(
            Q(summary__isnull=False) | Q(selection_state=SelectionState.TARGET))
    return queryset.select_related('company', 'company__sector', 'summary')


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


def _feed_options(request):
    """목록 화면 공통 값 — "전체 공시 보기" 여부와 월 한도 안내."""
    return {
        'show_all': request.GET.get('all') == '1',
        'budget_exhausted': ai_budget.is_exhausted(),
    }


def _render_feed(request, template, context):
    """목록 화면을 그린다. `partial=1`이면 피드 조각만 돌려준다.

    자동 갱신(live-updates.js)이 지금 보고 있는 URL을 그대로 다시 받아 목록만 바꿔
    끼우기 위한 것이다. **같은 뷰·같은 템플릿 조각을 쓴다** — 갱신용 HTML을 따로
    만들면 필터·페이지네이션 로직이 두 벌이 되어 한쪽만 고쳤을 때 조용히 어긋난다.
    """
    if request.GET.get('partial') == '1':
        return render(request, 'disclosures/_disclosure_feed.html', context)
    return render(request, template, context)


def _poll_interval_seconds(now=None):
    """지금 얼마나 자주 물어볼지(초). 파이프라인 주기에 맞춘다.

    평일 09~18시에는 수집 파이프라인이 1분마다 돌지만 그 외에는 1시간마다다
    (deploy/crontab). 새 공시가 나올 수 없는 시간대에 30초마다 묻는 것은 낭비다.

    판단을 서버가 하는 이유: 주기 정책이 crontab·settings에 있으므로 한 곳에서만
    바꾸면 되고, 브라우저 시계(사용자 시간대·오작동)에 의존하지 않아도 된다.
    """
    base = settings.REALTIME_POLL_INTERVAL_SECONDS
    if base <= 0:
        return 0
    now = now or timezone.localtime()
    busy = now.weekday() < 5 and 9 <= now.hour < 19
    return base if busy else base * settings.REALTIME_OFF_HOURS_MULTIPLIER


@login_required
def latest_status(request):
    """목록이 바뀌었는지 판단할 서명과 다음 확인 간격을 돌려준다.

    ## DART를 호출하지 않는다

    이 뷰는 사용자 요청 경로에 있으므로 **로컬 DB만 읽는다**(PLAN.md 12.1).
    방문자가 늘어도 DART 호출은 0이고, 폴링 주기를 줄여도 마찬가지다.
    이 성질이 깨지면 폴링 빈도가 곧 DART 호출량이 되어 설계가 무너진다.

    ## 서명에 세 값을 넣는 이유

    - `latest` — 새 공시가 들어오면 바뀐다
    - `total` — 요약이 내려가 목록에서 빠지면 바뀐다
    - `summarized` — **"정리 중"이던 카드에 요약이 붙으면** 바뀐다.
      이것이 없으면 앞의 둘이 그대로라 화면이 "정리 중"에 머문다.

    rcept_no는 자리수가 고정된 숫자 문자열이라 사전순 최댓값이 곧 최신이다.

    **내 관심 기업만 센다**(이슈 #44). 남이 고른 기업에 새 공시가 올라와도 내 화면은
    바뀌지 않으므로, 전체를 세면 쓸데없이 목록을 다시 받는다.
    """
    stats = published_disclosures().filter(
        company__in=_my_companies(request.user),
    ).aggregate(
        total=Count('id'),
        summarized=Count('summary'),
        latest=Max('rcept_no'),
    )
    return JsonResponse({
        'signature': '%s:%s:%s' % (
            stats['latest'] or '', stats['total'], stats['summarized'],
        ),
        'interval_seconds': _poll_interval_seconds(),
    })


def _importance_options():
    """템플릿 필터 UI용 (값, 라벨) 목록."""
    return [
        {'value': choice.value, 'label': choice.label}
        for choice in DisclosureSummary.Importance
    ]


def _my_companies(user):
    """이 사용자가 고른 관심 기업. 목록 화면은 **이것의 공시만** 보여준다(이슈 #44)."""
    return Company.objects.filter(watches__user=user).order_by('name')


def home(request):
    """첫 화면. 비로그인은 서비스 소개, 로그인하면 **내 관심 기업의 공시 모아보기**.

    섹터(업종)별 화면은 8단계에서 없앴다. 사용자가 추가하는 기업은 모두 "기타"로
    들어가 업종 구분이 의미가 없어졌다(PLAN.md 9.4). 업종 자동 분류를 만들면 다시 본다.
    """
    if not request.user.is_authenticated:
        return render(request, 'disclosures/landing.html', {
            'kakao_login_enabled': settings.KAKAO_LOGIN_ENABLED,
        })

    companies = list(_my_companies(request.user))
    options = _feed_options(request)
    disclosures = published_disclosures(options['show_all']).filter(company__in=companies)
    selected_company = request.GET.get('company', '')
    selected_importance = request.GET.get('importance', '')
    disclosures = _filter_by_company(disclosures, selected_company)
    disclosures = _filter_by_importance(disclosures, selected_importance)

    return _render_feed(request, 'disclosures/home.html', {
        **options,
        'companies': companies,
        'watch_limit': watchlist.MAX_WATCHES_PER_USER,
        'page_obj': _paginate(request, disclosures),
        'total_count': disclosures.count(),
        'selected_company': selected_company,
        'selected_importance': selected_importance,
        'importance_options': _importance_options(),
    })


@login_required
def company_detail(request, stock_code):
    """기업 상세. **관심 기업일 때만 공시를 보여준다.**

    고르지 않은 기업(예: 남이 보낸 링크)은 이름과 "관심 기업에 추가"만 보여준다.
    아직 누구도 고르지 않아 Company가 없는 상장사도 명단(ListedCorp)에 있으면 연다.
    """
    company = Company.objects.select_related('sector').filter(
        stock_code=stock_code).first()
    listed = ListedCorp.objects.filter(stock_code=stock_code).first()
    if company is None and listed is None:
        raise Http404('등록되지 않은 종목코드입니다.')

    watching = company is not None and company.watches.filter(
        user=request.user).exists()
    if not watching:
        return render(request, 'disclosures/company_detail.html', {
            'company': company, 'listed': listed, 'watching': False,
            'watch_count': request.user.watches.count(),
            'watch_limit': watchlist.MAX_WATCHES_PER_USER,
        })

    options = _feed_options(request)
    disclosures = published_disclosures(options['show_all']).filter(company=company)
    selected_importance = request.GET.get('importance', '')
    disclosures = _filter_by_importance(disclosures, selected_importance)

    return _render_feed(request, 'disclosures/company_detail.html', {
        **options,
        'company': company,
        'watching': True,
        # 카드에서 기업명을 감춘다. 템플릿의 include 인자가 아니라 컨텍스트에 두는
        # 이유는, 자동 갱신이 피드 조각만 따로 렌더링할 때도 같은 값이 필요해서다.
        'hide_company': True,
        'page_obj': _paginate(request, disclosures),
        'total_count': disclosures.count(),
        'selected_importance': selected_importance,
        'importance_options': _importance_options(),
    })


@login_required
def search(request):
    """기업 검색. 저장된 상장사 명단만 본다 — **DART를 부르지 않는다.**"""
    query = request.GET.get('q', '').strip()
    results = list(watchlist.search_listed(query))
    watched = set(
        request.user.watches.values_list('company__corp_code', flat=True))
    return render(request, 'disclosures/search.html', {
        'query': query,
        'results': results,
        'watched_corp_codes': watched,
        'watch_count': len(watched),
        'watch_limit': watchlist.MAX_WATCHES_PER_USER,
    })


def _redirect_back(request, fallback):
    """폼이 보낸 next로 돌아간다. 외부 주소로는 보내지 않는다(열린 리다이렉트 방지)."""
    target = request.POST.get('next', '')
    # 자동 갱신으로 다시 그린 카드의 폼은 next에 `partial=1`이 붙어 온다. 그대로 돌아가면
    # 화면 틀 없이 목록 조각만 뜬다.
    parts = urlsplit(target)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if k != 'partial']
    target = urlunsplit(parts._replace(query=urlencode(query)))
    if target and url_has_allowed_host_and_scheme(
            target, allowed_hosts={request.get_host()}):
        return redirect(target)
    return redirect(fallback)


@login_required
@require_POST
def watch_add(request):
    listed = get_object_or_404(ListedCorp, corp_code=request.POST.get('corp_code', ''))
    try:
        watchlist.follow(request.user, listed)
    except watchlist.WatchLimitError as exc:
        messages.warning(request, str(exc))
    else:
        messages.success(request, f'{listed.name}을(를) 관심 기업에 추가했습니다.')
    return _redirect_back(request, 'disclosures:home')


@login_required
@require_POST
def watch_remove(request):
    company = get_object_or_404(Company, corp_code=request.POST.get('corp_code', ''))
    watchlist.unfollow(request.user, company)
    messages.info(request, f'{company.name}을(를) 관심 기업에서 뺐습니다.')
    return _redirect_back(request, 'disclosures:home')


def disclosure_detail(request, rcept_no):
    """공시 상세 — 한 줄 요약 → 쉬운 설명 → 왜 중요한가 → 원문 근거 → DART 원문 링크.

    **로그인 없이 열린다**(이슈 #44). 링크를 받은 사람이 그 공시 하나는 볼 수 있어야
    공유가 되고, 가입으로 이어진다. 목록 화면만 로그인이 필요하다.

    노출 대상이 아닌 공시는 404다(published_disclosures가 단일 출처).

    요약을 기다리는 중인 공시는 404가 아니라 "정리 중" 화면을 준다. 목록에 카드가
    보이는데 눌렀더니 404가 나는 것은 사용자에게 고장으로 읽힌다. 이때도 DART 원문
    링크는 그대로 내보낸다 — 요약이 없을수록 원문 경로가 더 필요하다(PLAN.md 5.3).

    여기서 요약을 생성하지 않는다 — 모듈 docstring 참고.
    """
    # 상세는 "전체" 기준으로 연다. 전체 보기 목록에서 누른 단순 보고도 열려야 한다.
    disclosure = get_object_or_404(published_disclosures(show_all=True), rcept_no=rcept_no)
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
        'budget_exhausted': ai_budget.is_exhausted(),
    })


@login_required
@require_POST
def summary_request(request):
    """"AI 요약 보기" 버튼. **요청만 기록한다** — 여기서 AI를 부르지 않는다.

    다음 파이프라인 실행이 원문을 받고 요약한다(summary_requests 첫 주석). 화면은
    "AI가 정리 중"으로 바뀌고, 요약이 붙으면 자동 갱신이 알아챈다.
    """
    disclosure = get_object_or_404(
        published_disclosures(), rcept_no=request.POST.get('rcept_no', ''))
    try:
        summary_requests.request_summary(request.user, disclosure)
    except summary_requests.RequestRefused as exc:
        messages.warning(request, str(exc))
    else:
        messages.success(request, 'AI 요약을 요청했습니다. 1~2분 안에 정리됩니다.')
    return _redirect_back(request, 'disclosures:home')
