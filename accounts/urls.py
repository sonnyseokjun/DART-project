"""회원 관련 URL. allauth의 `accounts/` 경로보다 **먼저** 포함해야 가려지지 않는다."""
from django.urls import path

from . import views

app_name = 'accounts'

urlpatterns = [
    path('accounts/withdraw/', views.withdraw, name='withdraw'),
    # 카카오 개발자 콘솔 "연결 해제 웹훅"에 등록하는 주소다. 바꾸면 콘솔도 함께 바꿔야 한다.
    path('accounts/kakao/unlink-webhook/', views.kakao_unlink_webhook,
         name='kakao_unlink_webhook'),
    path('privacy/', views.privacy, name='privacy'),
]
