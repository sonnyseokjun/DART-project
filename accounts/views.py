"""회원 탈퇴 · 카카오 연결 해제 웹훅 · 개인정보처리방침 (이슈 #44).

## 탈퇴하면 무엇이 지워지나

계정(`User`)을 지우면 카카오 연결 정보(`SocialAccount`)가 함께 지워진다(CASCADE).
**공시 요약은 지우지 않는다.** 요약은 특정 사용자의 것이 아니라 공시의 것이고,
다른 사용자도 보며, 지우면 다시 만드는 데 돈이 든다(PLAN.md 11).

## 웹훅이 필요한 이유

사용자가 우리 사이트가 아니라 **카카오 앱에서 직접** 연결을 끊을 수 있다. 그때 카카오가
알려주지 않으면 우리는 모르고 개인정보를 계속 갖고 있게 된다. 카카오 개발자 콘솔의
"연결 해제 웹훅"에 이 뷰의 주소를 등록한다(RUNBOOK 3.1).
"""
import hmac
import logging

from allauth.socialaccount.models import SocialAccount
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model, logout
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, HttpResponseForbidden
from django.shortcuts import redirect, render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from . import kakao

logger = logging.getLogger(__name__)


@login_required
@require_http_methods(['GET', 'POST'])
def withdraw(request):
    """회원 탈퇴. GET은 확인 화면, POST가 실제 탈퇴다.

    GET 한 번으로 탈퇴되면 링크 미리보기나 실수 클릭으로도 계정이 사라진다.
    """
    if request.method == 'GET':
        return render(request, 'accounts/withdraw.html')

    user = request.user
    for account in SocialAccount.objects.filter(user=user, provider='kakao'):
        kakao.unlink(account.uid)
    # 세션을 먼저 끊는다. 계정을 지운 뒤에는 로그아웃이 지울 대상을 못 찾는다.
    logout(request)
    user.delete()
    messages.info(request, '탈퇴가 완료되었습니다. 저장된 회원 정보를 모두 삭제했습니다.')
    return redirect('disclosures:home')


@csrf_exempt
@require_http_methods(['GET', 'POST'])
def kakao_unlink_webhook(request):
    """카카오 "연결 해제 웹훅" 수신. 해당 회원을 지운다.

    ## 발신자 확인

    카카오는 `Authorization: KakaoAK <앱 어드민 키>` 헤더를 붙여 보낸다. 누구나 이 주소를
    부를 수 있으므로 **헤더가 맞지 않으면 아무것도 지우지 않는다.** 비교는 상수 시간으로
    해서 응답 시간으로 키를 추측하지 못하게 한다.

    CSRF 검사를 끄는 것은 이 때문이다 — 카카오 서버는 CSRF 토큰을 모른다. 대신 위의
    헤더 확인이 그 역할을 한다.

    ## 항상 200으로 답하는 경우

    이미 지워진 회원이어도 200이다. 카카오는 실패로 보면 재시도하는데, 우리가
    탈퇴 처리 중 먼저 연결을 끊은 경우(withdraw) 웹훅이 뒤따라 온다.
    """
    admin_key = settings.KAKAO_ADMIN_KEY
    expected = f'KakaoAK {admin_key}'
    received = request.headers.get('Authorization', '')
    # bytes로 비교한다. str끼리 비교하면 헤더에 한글이 섞여 올 때 TypeError로 500이 난다.
    if not admin_key or not hmac.compare_digest(
            received.encode(), expected.encode()):
        return HttpResponseForbidden()

    params = request.POST if request.method == 'POST' else request.GET
    kakao_user_id = params.get('user_id', '').strip()
    if not kakao_user_id:
        return HttpResponse(status=400)

    user_ids = SocialAccount.objects.filter(
        provider='kakao', uid=kakao_user_id,
    ).values_list('user_id', flat=True)
    deleted, _ = get_user_model().objects.filter(id__in=list(user_ids)).delete()
    logger.info('카카오 연결 해제 웹훅: 회원 %s (%s, 삭제 행 %d)',
                kakao_user_id, params.get('referrer_type', ''), deleted)
    return HttpResponse(status=200)


def privacy(request):
    """개인정보처리방침. 로그인하지 않아도 볼 수 있어야 한다(가입 전에 읽는 문서)."""
    return render(request, 'accounts/privacy.html', {
        'officer_name': settings.PRIVACY_OFFICER_NAME,
        'contact_email': settings.PRIVACY_CONTACT_EMAIL,
    })
