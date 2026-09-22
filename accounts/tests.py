"""회원 기능 테스트 (이슈 #44 PR 1).

실제 카카오는 부르지 않는다. 로그인 흐름은 카카오가 돌려줬다고 가정한 사용자 정보
응답으로 allauth의 로그인 완료 단계를 직접 돌리고, 연결 끊기는 requests를 가로챈다.
"""
from datetime import date
from unittest.mock import patch

import requests
from allauth.account.models import EmailAddress
from allauth.core import context
from allauth.socialaccount.adapter import get_adapter as get_social_adapter
from allauth.socialaccount.internal.flows.login import complete_login
from allauth.socialaccount.models import SocialAccount
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.contrib.messages.storage.fallback import FallbackStorage
from django.contrib.sessions.middleware import SessionMiddleware
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse

from disclosures.dart import dart_viewer_url
from disclosures.models import Company, Disclosure, DisclosureSummary, Sector
from disclosures.selection import SelectionState

from .adapter import minimal_extra_data

ADMIN_KEY = 'test-admin-key'

#: 닉네임만 동의받았을 때 카카오가 돌려주는 사용자 정보 응답의 모양.
#: 동의 여부 플래그·연결 시각처럼 우리가 쓰지 않는 필드가 섞여 온다.
KAKAO_RESPONSE = {
    'id': 4242424242,
    'connected_at': '2026-09-22T06:00:00Z',
    'properties': {'nickname': '공시러'},
    'kakao_account': {
        'profile_nickname_needs_agreement': False,
        'profile': {'nickname': '공시러', 'is_default_nickname': False},
    },
}


def _kakao_login(response=KAKAO_RESPONSE):
    """카카오가 `response`를 돌려줬다고 치고 allauth 로그인 완료 단계를 돌린다."""
    request = RequestFactory().get('/accounts/kakao/login/callback/')
    SessionMiddleware(lambda r: None).process_request(request)
    request.session.save()
    request._messages = FallbackStorage(request)
    request.user = AnonymousUser()
    # allauth는 현재 요청을 문맥 변수로 찾는다. 미들웨어 대신 직접 넣어 준다.
    with context.request_context(request):
        provider = get_social_adapter().get_provider(request, 'kakao')
        sociallogin = provider.sociallogin_from_response(request, response)
        complete_login(request, sociallogin)
    return SocialAccount.objects.get(provider='kakao', uid=str(response['id']))


@override_settings(
    KAKAO_LOGIN_ENABLED=True,
    SOCIALACCOUNT_PROVIDERS={'kakao': {
        'APPS': [{'client_id': 'test-client', 'secret': 'test-secret'}],
        'SCOPE': ['profile_nickname'],
    }},
)
class KakaoSignupTest(TestCase):
    """무엇이 저장되는가 — 회원번호와 닉네임뿐이어야 한다."""

    def test_first_login_creates_an_account(self):
        account = _kakao_login()
        user = account.user
        self.assertEqual(user.username, 'kakao_4242424242')
        self.assertEqual(user.first_name, '공시러')

    def test_email_and_password_are_not_stored(self):
        user = _kakao_login().user
        self.assertEqual(user.email, '')
        self.assertFalse(user.has_usable_password())

    def test_only_id_and_nickname_are_kept(self):
        account = _kakao_login()
        self.assertEqual(account.extra_data, {
            'id': 4242424242,
            'kakao_account': {'profile': {'nickname': '공시러'}},
        })

    def test_relogin_does_not_leave_the_full_response_behind(self):
        """allauth는 재로그인 때 원래 응답을 **먼저 저장**한다. 그 뒤에 덮어야 한다."""
        _kakao_login()
        account = _kakao_login()
        account.refresh_from_db()
        self.assertNotIn('connected_at', account.extra_data)
        self.assertNotIn('properties', account.extra_data)

    def test_relogin_does_not_create_a_second_account(self):
        _kakao_login()
        _kakao_login()
        self.assertEqual(get_user_model().objects.count(), 1)

    def test_nickname_change_is_reflected_on_next_login(self):
        _kakao_login()
        renamed = {
            **KAKAO_RESPONSE,
            'kakao_account': {'profile': {'nickname': '새이름'}},
        }
        user = _kakao_login(renamed).user
        user.refresh_from_db()
        self.assertEqual(user.first_name, '새이름')

    def test_extra_consent_items_are_dropped_even_if_the_console_changes(self):
        """누가 콘솔에서 이메일 동의를 켜도 저장 범위는 늘지 않는다."""
        leaked = {
            **KAKAO_RESPONSE,
            'kakao_account': {
                'profile': {'nickname': '공시러'},
                'email': 'someone@example.com',
                'phone_number': '+82 10-0000-0000',
            },
        }
        account = _kakao_login(leaked)
        self.assertNotIn('someone@example.com', str(account.extra_data))
        self.assertEqual(account.user.email, '')
        self.assertFalse(EmailAddress.objects.exists())

    def test_minimal_extra_data_handles_missing_profile(self):
        self.assertEqual(
            minimal_extra_data({'id': 1}),
            {'id': 1, 'kakao_account': {'profile': {'nickname': ''}}},
        )


class LocalAccountsAreClosedTest(TestCase):
    """아이디·비밀번호 가입과 비밀번호 찾기는 없다. 카카오로만 들어온다."""

    def test_signup_and_password_reset_pages_do_not_exist(self):
        for path in ('/accounts/signup/', '/accounts/password/reset/',
                     '/accounts/email/'):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 404)

    @override_settings(KAKAO_LOGIN_ENABLED=True, SOCIALACCOUNT_PROVIDERS={'kakao': {
        'APPS': [{'client_id': 'test-client', 'secret': 'test-secret'}],
    }})
    def test_get_on_the_kakao_login_url_does_not_redirect_to_kakao(self):
        """GET으로 로그인이 시작되면 남의 사이트 링크 하나로 로그인시킬 수 있다."""
        response = self.client.get('/accounts/kakao/login/')
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('kauth.kakao.com', response.get('Location', ''))

    def test_logout_by_get_does_not_log_out(self):
        user = get_user_model().objects.create_user(username='kakao_1')
        self.client.force_login(user)
        self.client.get('/accounts/logout/')
        self.assertIn('_auth_user_id', self.client.session)

    def test_admin_password_login_still_works(self):
        """카카오를 붙여도 검수자의 admin 비밀번호 로그인은 그대로여야 한다."""
        get_user_model().objects.create_superuser('reviewer', 'r@example.com', 'pw')
        self.assertTrue(self.client.login(username='reviewer', password='pw'))


def _make_published_disclosure():
    sector = Sector.objects.create(name='반도체', slug='semiconductor')
    company = Company.objects.create(
        sector=sector, corp_code='00126380', stock_code='005930', name='삼성전자',
    )
    disclosure = Disclosure.objects.create(
        company=company, rcept_no='20260701000001', report_name='단일판매ㆍ공급계약체결',
        disclosure_type='거래소공시', filed_at=date(2026, 7, 1),
        dart_url=dart_viewer_url('20260701000001'),
        selection_state=SelectionState.TARGET, raw_fetched=True, raw_content='원문',
    )
    DisclosureSummary.objects.create(
        disclosure=disclosure, one_line='한 줄 요약',
        easy_explanation='첫 문장이다. 둘째 문장이다.', why_important='이유다.',
        importance=DisclosureSummary.Importance.HIGH, model_name='gpt-5.6-luna',
        evidence=[],
    )
    return disclosure


class AnonymousAccessTest(TestCase):
    """로그인하지 않은 방문자가 보는 것 — 소개, 공시 상세 링크, 개인정보처리방침."""

    def setUp(self):
        self.disclosure = _make_published_disclosure()

    @override_settings(KAKAO_LOGIN_ENABLED=True, SOCIALACCOUNT_PROVIDERS={'kakao': {
        'APPS': [{'client_id': 'test-client', 'secret': 'test-secret'}],
    }})
    def test_home_shows_the_landing_with_a_kakao_button(self):
        response = self.client.get(reverse('disclosures:home'))
        self.assertContains(response, '카카오로 시작하기')
        self.assertContains(response, 'action="/accounts/kakao/login/"')
        # 비로그인에게는 목록을 보여주지 않는다.
        self.assertNotContains(response, '한 줄 요약')

    @override_settings(KAKAO_LOGIN_ENABLED=False,
                       SOCIALACCOUNT_PROVIDERS={'kakao': {'APPS': []}})
    def test_home_does_not_500_when_kakao_keys_are_missing(self):
        """키 없이 로그인 버튼을 그리면 allauth가 앱을 못 찾아 첫 화면이 통째로 죽는다."""
        response = self.client.get(reverse('disclosures:home'))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'action="/accounts/kakao/login/"')

    def test_list_pages_send_anonymous_visitors_to_the_landing(self):
        for url in (reverse('disclosures:search'),
                    reverse('disclosures:company_detail', args=['005930'])):
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 302)
                self.assertTrue(response['Location'].startswith('/?next='))

    def test_a_shared_disclosure_link_opens_without_login(self):
        response = self.client.get(
            reverse('disclosures:disclosure_detail', args=[self.disclosure.rcept_no]))
        self.assertContains(response, '한 줄 요약')

    def test_logged_in_member_still_sees_the_lists(self):
        self.client.force_login(get_user_model().objects.create_user(username='kakao_1'))
        response = self.client.get(reverse('disclosures:home'))
        self.assertContains(response, '내 관심 기업')
        self.assertNotContains(response, 'action="/accounts/kakao/login/"')

    @override_settings(PRIVACY_CONTACT_EMAIL='owner@example.com')
    def test_privacy_policy_is_public_and_names_the_officer(self):
        response = self.client.get(reverse('accounts:privacy'))
        self.assertContains(response, 'DART 공시 요약 운영자')
        self.assertContains(response, 'owner@example.com')

    def test_every_page_links_to_the_privacy_policy(self):
        for url in (reverse('disclosures:home'),
                    reverse('disclosures:disclosure_detail', args=[self.disclosure.rcept_no]),
                    reverse('accounts:privacy')):
            with self.subTest(url=url):
                self.assertContains(self.client.get(url), reverse('accounts:privacy'))


@override_settings(
    KAKAO_ADMIN_KEY=ADMIN_KEY, KAKAO_LOGIN_ENABLED=True,
    SOCIALACCOUNT_PROVIDERS={'kakao': {
        'APPS': [{'client_id': 'test-client', 'secret': 'test-secret'}],
    }},
)
class WithdrawTest(TestCase):
    """탈퇴 — 계정과 카카오 연결은 지우고, 요약은 남긴다."""

    def setUp(self):
        self.disclosure = _make_published_disclosure()
        self.account = _kakao_login()
        self.user = self.account.user
        self.client.force_login(self.user)

    def _ok(self, *args, **kwargs):
        response = requests.Response()
        response.status_code = 200
        return response

    def test_get_only_shows_a_confirmation(self):
        with patch('accounts.kakao.requests.post') as post:
            response = self.client.get(reverse('accounts:withdraw'))
        self.assertContains(response, '탈퇴하기')
        post.assert_not_called()
        self.assertTrue(get_user_model().objects.filter(pk=self.user.pk).exists())

    def test_post_deletes_the_account_and_the_kakao_link(self):
        with patch('accounts.kakao.requests.post', side_effect=self._ok):
            self.client.post(reverse('accounts:withdraw'))
        self.assertFalse(get_user_model().objects.filter(pk=self.user.pk).exists())
        self.assertFalse(SocialAccount.objects.exists())

    def test_post_asks_kakao_to_unlink_with_the_admin_key(self):
        with patch('accounts.kakao.requests.post', side_effect=self._ok) as post:
            self.client.post(reverse('accounts:withdraw'))
        _, kwargs = post.call_args
        self.assertEqual(kwargs['headers'], {'Authorization': f'KakaoAK {ADMIN_KEY}'})
        self.assertEqual(kwargs['data'],
                         {'target_id_type': 'user_id', 'target_id': '4242424242'})
        self.assertIn('timeout', kwargs)

    def test_summaries_survive_withdrawal(self):
        with patch('accounts.kakao.requests.post', side_effect=self._ok):
            self.client.post(reverse('accounts:withdraw'))
        self.assertTrue(DisclosureSummary.objects.filter(
            disclosure=self.disclosure).exists())

    def test_withdrawal_logs_the_user_out(self):
        with patch('accounts.kakao.requests.post', side_effect=self._ok):
            self.client.post(reverse('accounts:withdraw'))
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_kakao_failure_does_not_block_withdrawal(self):
        """카카오가 응답하지 않아도 우리 쪽 개인정보는 지워야 한다."""
        with patch('accounts.kakao.requests.post',
                   side_effect=requests.ConnectionError('down')):
            self.client.post(reverse('accounts:withdraw'))
        self.assertFalse(get_user_model().objects.filter(pk=self.user.pk).exists())

    @override_settings(KAKAO_ADMIN_KEY='')
    def test_missing_admin_key_skips_the_kakao_call(self):
        with patch('accounts.kakao.requests.post') as post:
            self.client.post(reverse('accounts:withdraw'))
        post.assert_not_called()
        self.assertFalse(get_user_model().objects.filter(pk=self.user.pk).exists())

    def test_anonymous_cannot_withdraw(self):
        self.client.logout()
        response = self.client.post(reverse('accounts:withdraw'))
        self.assertEqual(response.status_code, 302)
        self.assertTrue(get_user_model().objects.filter(pk=self.user.pk).exists())


@override_settings(
    KAKAO_ADMIN_KEY=ADMIN_KEY, KAKAO_LOGIN_ENABLED=True,
    SOCIALACCOUNT_PROVIDERS={'kakao': {
        'APPS': [{'client_id': 'test-client', 'secret': 'test-secret'}],
    }},
)
class KakaoUnlinkWebhookTest(TestCase):
    """카카오 앱에서 연결을 끊었을 때 — 진짜 카카오가 보낸 알림만 믿는다."""

    url = '/accounts/kakao/unlink-webhook/'

    def setUp(self):
        self.user = _kakao_login().user
        self.params = {'app_id': '1585419', 'user_id': '4242424242',
                       'referrer_type': 'UNLINK_FROM_APPS'}

    def _exists(self):
        return get_user_model().objects.filter(pk=self.user.pk).exists()

    def test_valid_get_deletes_the_member(self):
        response = self.client.get(self.url, self.params,
                                   HTTP_AUTHORIZATION=f'KakaoAK {ADMIN_KEY}')
        self.assertEqual(response.status_code, 200)
        self.assertFalse(self._exists())

    def test_valid_post_deletes_the_member(self):
        response = self.client.post(self.url, self.params,
                                    HTTP_AUTHORIZATION=f'KakaoAK {ADMIN_KEY}')
        self.assertEqual(response.status_code, 200)
        self.assertFalse(self._exists())

    def test_missing_or_wrong_key_deletes_nothing(self):
        for header in ({}, {'HTTP_AUTHORIZATION': 'KakaoAK wrong'},
                       {'HTTP_AUTHORIZATION': ADMIN_KEY},
                       {'HTTP_AUTHORIZATION': 'KakaoAK 한글키'}):
            with self.subTest(header=header):
                response = self.client.get(self.url, self.params, **header)
                self.assertEqual(response.status_code, 403)
                self.assertTrue(self._exists())

    @override_settings(KAKAO_ADMIN_KEY='')
    def test_unconfigured_key_rejects_everything(self):
        """키를 안 넣은 서버에서 `KakaoAK `(빈 키)가 통과하면 누구나 회원을 지운다."""
        response = self.client.get(self.url, self.params, HTTP_AUTHORIZATION='KakaoAK ')
        self.assertEqual(response.status_code, 403)
        self.assertTrue(self._exists())

    def test_unknown_member_is_still_200(self):
        """탈퇴 때 먼저 연결을 끊었으면 웹훅이 뒤따라 온다. 실패로 답하면 카카오가 재시도한다."""
        response = self.client.get(self.url, {**self.params, 'user_id': '1'},
                                   HTTP_AUTHORIZATION=f'KakaoAK {ADMIN_KEY}')
        self.assertEqual(response.status_code, 200)
        self.assertTrue(self._exists())

    def test_missing_user_id_is_400(self):
        response = self.client.get(self.url, {'app_id': '1585419'},
                                   HTTP_AUTHORIZATION=f'KakaoAK {ADMIN_KEY}')
        self.assertEqual(response.status_code, 400)
