"""카카오 연결 끊기 (이슈 #44).

회원 탈퇴 때 우리 DB에서 계정만 지우면 **카카오 쪽 연결은 남는다.** 그러면 그 사람이
다시 로그인할 때 동의 화면 없이 곧바로 새 계정이 만들어진다 — 탈퇴했는데 동의를
다시 받지 않는 셈이다. 그래서 카카오에도 "연결을 끊어 달라"고 알린다.

## 어드민 키를 쓰는 이유

연결 끊기는 사용자의 액세스 토큰으로도, 앱 어드민 키 + 회원번호로도 할 수 있다.
토큰 방식은 **사용자마다 토큰을 DB에 보관**해야 한다(`SOCIALACCOUNT_STORE_TOKENS`).
보관하는 민감 정보를 늘리지 않으려고 어드민 키 방식을 쓴다. 같은 키가 연결 해제
웹훅의 발신자 확인에도 쓰인다(`views.kakao_unlink_webhook`).

## 이 모듈은 DART·LLM과 무관하다

사용자 요청 경로에서 외부를 부르지 않는다는 규칙(PLAN.md 12.1)은 **DART 호출 수와
LLM 비용이 트래픽에 비례하지 않게** 하려는 것이다. 카카오 호출은 탈퇴 1건당 1회이고
돈이 들지 않는다. 로그인 자체도 카카오를 부른다(allauth).
"""
import logging

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

UNLINK_URL = 'https://kapi.kakao.com/v1/user/unlink'

#: 탈퇴 화면이 카카오 응답을 기다리는 최대 시간(초). gunicorn 워커가 2개라
#: 오래 붙잡으면 다른 사용자가 기다린다.
UNLINK_TIMEOUT_SECONDS = 5


def unlink(kakao_user_id):
    """카카오에 연결 끊기를 요청한다. 성공하면 True.

    실패해도 예외를 올리지 않는다 — 탈퇴는 카카오 응답과 무관하게 진행해야 한다.
    우리 쪽 개인정보를 지우는 것이 본질이고, 카카오 연결은 사용자가 카카오 앱에서도
    끊을 수 있다. 실패는 로그로 남긴다.
    """
    admin_key = settings.KAKAO_ADMIN_KEY
    if not admin_key:
        logger.warning('KAKAO_ADMIN_KEY가 없어 카카오 연결 끊기를 건너뛴다 (회원 %s)',
                       kakao_user_id)
        return False
    try:
        response = requests.post(
            UNLINK_URL,
            headers={'Authorization': f'KakaoAK {admin_key}'},
            data={'target_id_type': 'user_id', 'target_id': kakao_user_id},
            timeout=UNLINK_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        logger.warning('카카오 연결 끊기 실패 (회원 %s): %s', kakao_user_id, exc)
        return False
    if response.status_code != 200:
        # 본문에는 키가 들어 있지 않다. 상태 코드와 카카오 오류 코드만 남는다.
        logger.warning('카카오 연결 끊기 실패 (회원 %s): HTTP %s %s',
                       kakao_user_id, response.status_code, response.text[:200])
        return False
    return True
