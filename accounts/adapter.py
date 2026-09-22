"""카카오 로그인이 **무엇을 저장하는지** 정하는 곳 (이슈 #44).

## 저장하는 개인정보는 회원번호와 닉네임뿐이다

allauth는 카카오가 돌려준 사용자 정보 응답을 `SocialAccount.extra_data`에 **통째로**
저장한다. 동의항목을 닉네임 하나로 좁혀 두었어도(카카오 개발자 콘솔) 응답에는 연결 시각,
동의 여부 플래그 같은 부가 필드가 따라온다. 나중에 누가 콘솔에서 동의항목을 늘리면
그 값도 아무도 모르게 DB에 쌓이기 시작한다.

그래서 저장 직전에 **허용 목록으로 걸러낸다.** 콘솔 설정이 바뀌어도 이 파일을 고치지
않는 한 저장 범위는 늘지 않는다. 막는 쪽이 코드에 있어야 리뷰에서 보인다.

## 사용자 이름을 닉네임으로 쓰지 않는다

allauth 카카오 공급자는 닉네임을 `username`에 넣으려 한다. 닉네임은 겹치고 바뀌므로
`username`은 `kakao_<회원번호>`로 고정하고, 닉네임은 화면 표시용 `first_name`에 둔다.
"""
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter

#: User.first_name 길이 상한. 카카오 닉네임은 이보다 짧지만 잘라서 넣는다.
NICKNAME_MAX_LENGTH = 150


def kakao_nickname(extra_data):
    """카카오 응답에서 닉네임을 꺼낸다. 없으면 빈 문자열."""
    profile = (extra_data.get('kakao_account') or {}).get('profile') or {}
    nickname = profile.get('nickname') or (
        (extra_data.get('properties') or {}).get('nickname')
    )
    return (nickname or '').strip()[:NICKNAME_MAX_LENGTH]


def minimal_extra_data(extra_data):
    """저장해도 되는 필드만 남긴다 — 회원번호와 닉네임.

    구조는 allauth의 KakaoAccount가 읽는 모양(`kakao_account.profile.nickname`)을
    그대로 따른다. 모양을 바꾸면 allauth 내부 표시가 깨진다.
    """
    return {
        'id': extra_data.get('id'),
        'kakao_account': {'profile': {'nickname': kakao_nickname(extra_data)}},
    }


class KakaoSocialAccountAdapter(DefaultSocialAccountAdapter):

    def populate_user(self, request, sociallogin, data):
        user = super().populate_user(request, sociallogin, data)
        user.username = f'kakao_{sociallogin.account.uid}'
        user.first_name = kakao_nickname(sociallogin.account.extra_data)
        # 이메일은 받지 않기로 했다. 공급자가 무엇을 주든 비워 둔다.
        user.email = ''
        return user

    def pre_social_login(self, request, sociallogin):
        """로그인할 때마다 저장 범위를 다시 좁히고 닉네임을 갱신한다.

        allauth는 재로그인 때 extra_data를 새 응답으로 덮어쓴다. 첫 가입에서만
        거르면 두 번째 로그인부터 원래 응답이 그대로 저장된다.

        재로그인이면 **이미 저장된 뒤다** — allauth의 `lookup()`이 이 메서드보다
        먼저 돌면서 원래 응답을 저장해 버린다. 그래서 여기서 다시 저장해 덮는다.
        같은 요청 안에서 바로 덮이고, 닉네임만 동의받은 응답에는 동의 여부 플래그와
        연결 시각 정도만 더 들어 있다.
        """
        account = sociallogin.account
        account.extra_data = minimal_extra_data(account.extra_data)
        # 이메일은 extra_data와 **별도 경로**로도 저장된다. allauth가 응답에서 뽑은
        # 이메일 목록을 가입 때 User.email과 EmailAddress 표에 넣는다. 콘솔에서 이메일
        # 동의를 켜는 순간 조용히 쌓이기 시작하므로 여기서 비운다(테스트로 확인됨).
        sociallogin.email_addresses = []
        if sociallogin.is_existing:
            account.save(update_fields=['extra_data'])
            user = sociallogin.user
            nickname = kakao_nickname(account.extra_data)
            if nickname and user.first_name != nickname:
                user.first_name = nickname
                user.save(update_fields=['first_name'])
