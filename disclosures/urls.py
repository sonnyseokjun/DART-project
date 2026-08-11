"""공시 조회 화면 URL (PLAN.md 7장).

식별자는 사람이 읽을 수 있는 자연 키를 쓴다 — 섹터는 slug, 기업은 종목코드,
공시는 접수번호(rcept_no)다. 셋 다 unique 제약이 있고 외부에 공개해도 무방한 값이라
DB의 auto increment id를 노출할 이유가 없다. 접수번호는 DART 원문 URL과도 같은 키라
사용자가 주소만 보고 원문을 찾아갈 수 있다.
"""
from django.urls import path

from . import views

app_name = 'disclosures'

urlpatterns = [
    path('', views.sector_list, name='sector_list'),
    path('sectors/<slug:slug>/', views.sector_detail, name='sector_detail'),
    # 종목코드는 6자리 숫자지만 str 컨버터로 받는다. int로 받으면 선행 0이 사라져
    # '000660'(SK하이닉스)이 660으로 조회돼 404가 난다.
    path('companies/<str:stock_code>/', views.company_detail, name='company_detail'),
    path('disclosures/<str:rcept_no>/', views.disclosure_detail, name='disclosure_detail'),
]
