"""공시 조회 화면 URL (PLAN.md 7장).

식별자는 사람이 읽을 수 있는 자연 키를 쓴다 — 기업은 종목코드,
공시는 접수번호(rcept_no)다. 셋 다 unique 제약이 있고 외부에 공개해도 무방한 값이라
DB의 auto increment id를 노출할 이유가 없다. 접수번호는 DART 원문 URL과도 같은 키라
사용자가 주소만 보고 원문을 찾아갈 수 있다.
"""
from django.urls import path

from . import views

app_name = 'disclosures'

urlpatterns = [
    path('', views.home, name='home'),
    # 섹터 화면(sectors/<slug>/)은 8단계에서 없앴다. 목록은 계정별 관심 기업이다(이슈 #44).
    path('search/', views.search, name='search'),
    path('watch/add/', views.watch_add, name='watch_add'),
    path('watch/remove/', views.watch_remove, name='watch_remove'),
    path('summary/request/', views.summary_request, name='summary_request'),
    # 종목코드는 6자리 숫자지만 str 컨버터로 받는다. int로 받으면 선행 0이 사라져
    # '000660'(SK하이닉스)이 660으로 조회돼 404가 난다.
    path('companies/<str:stock_code>/', views.company_detail, name='company_detail'),
    path('disclosures/<str:rcept_no>/', views.disclosure_detail, name='disclosure_detail'),
    # 목록 자동 갱신용. 로컬 DB만 읽는 작은 JSON이며 DART를 호출하지 않는다.
    path('api/latest/', views.latest_status, name='latest_status'),
]
