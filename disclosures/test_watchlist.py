"""관심 기업 테스트 (이슈 #44 PR 2).

정책(watchlist.py) · 백필(poll_dart) · 상장사 명단(sync_listed_corps) · 화면을 본다.
DART는 부르지 않는다 — 목록 조회는 가짜로 바꾸고, 부르면 안 되는 곳은 호출 여부를 본다.
"""
import io
import zipfile
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from django.apps import apps as django_apps
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from disclosures import dart, retry_policy, watchlist
from disclosures.dart import DartApiError, dart_viewer_url
from disclosures.management.commands import poll_dart
from disclosures.models import (
    Company, Disclosure, DisclosureSummary, ListedCorp, Sector, Watch, WatchAddition,
)
from disclosures.selection import SelectionState

POLL = 'disclosures.management.commands.poll_dart'


def _user(name='kakao_1'):
    return get_user_model().objects.create_user(username=name)


def _listed(n):
    """상장사 명단 n번째 기업. 종목코드·고유번호가 겹치지 않게 만든다."""
    return ListedCorp.objects.create(
        corp_code=f'{n:08d}', stock_code=f'{n:06d}', name=f'테스트기업{n}')


def _published(company, rcept_no, name='공시'):
    disclosure = Disclosure.objects.create(
        company=company, rcept_no=rcept_no, report_name=name,
        disclosure_type='거래소공시', filed_at=date(2026, 9, 1),
        dart_url=dart_viewer_url(rcept_no),
        selection_state=SelectionState.TARGET, raw_fetched=True, raw_content='원문',
    )
    DisclosureSummary.objects.create(
        disclosure=disclosure, one_line=f'{name} 한 줄', easy_explanation='설명이다.',
        why_important='이유다.', importance=DisclosureSummary.Importance.MEDIUM,
        model_name='gpt-5.6-luna', evidence=[])
    return disclosure


class FollowPolicyTest(TestCase):
    """누가 추적 대상을 정하는가 — 사용자가 고르면 켜지고, 아무도 안 보면 꺼진다."""

    def setUp(self):
        self.user = _user()
        self.listed = _listed(1)

    def test_first_follow_creates_a_tracked_company_in_the_default_sector(self):
        watchlist.follow(self.user, self.listed)
        company = Company.objects.get(corp_code=self.listed.corp_code)
        self.assertTrue(company.is_active)
        self.assertEqual(company.sector.slug, watchlist.DEFAULT_SECTOR_SLUG)
        self.assertIsNotNone(company.backfill_requested_at)

    def test_following_an_already_tracked_company_does_not_backfill_again(self):
        watchlist.follow(_user('kakao_other'), self.listed)
        company = Company.objects.get(corp_code=self.listed.corp_code)
        company.backfill_requested_at = None
        company.backfilled_at = timezone.now()
        company.save()

        watchlist.follow(self.user, self.listed)
        company.refresh_from_db()
        self.assertIsNone(company.backfill_requested_at)

    def test_following_twice_is_a_no_op(self):
        watchlist.follow(self.user, self.listed)
        watchlist.follow(self.user, self.listed)
        self.assertEqual(Watch.objects.count(), 1)
        self.assertEqual(WatchAddition.objects.count(), 1)

    def test_eleventh_company_is_refused_and_nothing_is_left_behind(self):
        for n in range(1, watchlist.MAX_WATCHES_PER_USER + 1):
            watchlist.follow(self.user, _listed(100 + n))
        extra = _listed(999)
        with self.assertRaises(watchlist.WatchLimitError):
            watchlist.follow(self.user, extra)
        self.assertEqual(self.user.watches.count(), watchlist.MAX_WATCHES_PER_USER)
        # 거부된 기업은 수집 대상으로 만들어지지도 않는다(트랜잭션째 되돌림).
        self.assertFalse(Company.objects.filter(corp_code=extra.corp_code).exists())

    def test_add_remove_churn_hits_the_daily_limit(self):
        """10곳 제한만으로는 추가·삭제 반복을 막지 못한다. 백필이 DART를 부르므로 막아야 한다."""
        for n in range(watchlist.MAX_ADDITIONS_PER_DAY):
            listed = _listed(200 + n)
            watchlist.follow(self.user, listed)
            watchlist.unfollow(self.user, Company.objects.get(corp_code=listed.corp_code))
        with self.assertRaises(watchlist.WatchLimitError):
            watchlist.follow(self.user, self.listed)
        self.assertEqual(self.user.watches.count(), 0)

    def test_daily_limit_resets_the_next_day(self):
        for n in range(watchlist.MAX_ADDITIONS_PER_DAY):
            listed = _listed(300 + n)
            watchlist.follow(self.user, listed)
            watchlist.unfollow(self.user, Company.objects.get(corp_code=listed.corp_code))
        WatchAddition.objects.update(created_at=timezone.now() - timedelta(days=1))
        watchlist.follow(self.user, self.listed)
        self.assertEqual(self.user.watches.count(), 1)

    def test_last_unfollow_stops_tracking_but_keeps_the_data(self):
        watchlist.follow(self.user, self.listed)
        company = Company.objects.get(corp_code=self.listed.corp_code)
        _published(company, '20260901000001')

        watchlist.unfollow(self.user, company)
        company.refresh_from_db()
        self.assertFalse(company.is_active)
        self.assertIsNotNone(company.untracked_at)
        self.assertIsNone(company.backfill_requested_at)
        self.assertEqual(company.disclosures.count(), 1)
        self.assertTrue(DisclosureSummary.objects.exists())

    def test_tracking_continues_while_someone_still_watches(self):
        other = _user('kakao_other')
        watchlist.follow(self.user, self.listed)
        watchlist.follow(other, self.listed)
        company = Company.objects.get(corp_code=self.listed.corp_code)

        watchlist.unfollow(self.user, company)
        company.refresh_from_db()
        self.assertTrue(company.is_active)

    def test_withdrawal_stops_tracking_too(self):
        """탈퇴는 CASCADE로 관심 기업이 지워진다. 그 경로도 추적을 멈춰야 한다."""
        watchlist.follow(self.user, self.listed)
        self.user.delete()
        company = Company.objects.get(corp_code=self.listed.corp_code)
        self.assertFalse(company.is_active)

    def test_short_gap_needs_no_backfill_but_a_long_one_does(self):
        watchlist.follow(self.user, self.listed)
        company = Company.objects.get(corp_code=self.listed.corp_code)
        company.backfill_requested_at = None
        company.backfilled_at = timezone.now()
        company.save()
        watchlist.unfollow(self.user, company)

        watchlist.follow(self.user, self.listed)
        company.refresh_from_db()
        self.assertIsNone(company.backfill_requested_at, '잠깐 빠졌던 기업을 다시 백필했다')

        watchlist.unfollow(self.user, company)
        Company.objects.filter(pk=company.pk).update(
            untracked_at=timezone.now() - timedelta(days=2))
        WatchAddition.objects.all().delete()
        watchlist.follow(self.user, self.listed)
        company.refresh_from_db()
        self.assertIsNotNone(company.backfill_requested_at)

    def test_search_uses_only_the_stored_list(self):
        with patch('disclosures.dart.requests.get') as dart_get:
            self.assertEqual(list(watchlist.search_listed('테스트기업1')), [self.listed])
            self.assertEqual(list(watchlist.search_listed('000001')), [self.listed])
            self.assertEqual(list(watchlist.search_listed('   ')), [])
        dart_get.assert_not_called()


class StopFixedTrackingMigrationTest(TestCase):
    """반도체 10곳 고정 추적 중단 — 추적만 끄고 아무것도 지우지 않는다."""

    def test_unwatched_companies_stop_and_data_survives(self):
        import importlib
        migration = importlib.import_module(
            'disclosures.migrations.0011_stop_fixed_tracking')
        sector = Sector.objects.create(name='반도체', slug='semiconductor')
        company = Company.objects.create(
            sector=sector, corp_code='00126380', stock_code='005930', name='삼성전자')
        _published(company, '20260701000001')

        migration.stop_unwatched_tracking(django_apps, None)

        company.refresh_from_db()
        self.assertFalse(company.is_active)
        self.assertIsNotNone(company.untracked_at)
        self.assertEqual(DisclosureSummary.objects.count(), 1)
        self.assertTrue(Sector.objects.filter(slug='etc').exists())


def _typed_iter(calls):
    """iter_disclosures 대역. 백필 조회(corp_code 있음)는 유형마다 1건씩 돌려준다."""
    def fake(bgn_de, end_de, corp_code=None, pblntf_ty=None):
        calls.append((corp_code, pblntf_ty, bgn_de, end_de))
        if corp_code and pblntf_ty in ('B', 'I'):
            return iter([{
                'corp_code': corp_code, 'rcept_no': f'2026090{1 if pblntf_ty == "B" else 2}'
                f'{corp_code[-6:]}', 'report_nm': f'{pblntf_ty} 공시 ',
                'rcept_dt': '20260901',
            }])
        return iter([])
    return fake


class BackfillTest(TestCase):
    """새로 추적하는 기업의 최근 30일 공시 채우기 — 목록만 받고 요약은 만들지 않는다."""

    def setUp(self):
        self.user = _user()
        self.listed = _listed(1)
        watchlist.follow(self.user, self.listed)
        self.company = Company.objects.get(corp_code=self.listed.corp_code)
        self.calls = []

    def _run(self, **kwargs):
        with patch(f'{POLL}.iter_disclosures', side_effect=_typed_iter(self.calls)), \
                patch(f'{POLL}.latest_disclosures', return_value=[]):
            call_command('poll_dart', detect=True, days=2, stdout=io.StringIO(), **kwargs)

    def test_backfill_saves_typed_disclosures_for_that_company_only(self):
        self._run()
        corp_calls = [c for c in self.calls if c[0]]
        self.assertEqual({c[1] for c in corp_calls}, set(dart.PBLNTF_TYPES))
        self.assertTrue(all(c[0] == self.company.corp_code for c in corp_calls))
        types = set(self.company.disclosures.values_list('disclosure_type', flat=True))
        self.assertEqual(types, {'주요사항보고', '거래소공시'})

    def test_backfill_covers_the_last_30_days(self):
        self._run()
        _, _, bgn_de, end_de = next(c for c in self.calls if c[0])
        today = timezone.localdate()
        self.assertEqual(bgn_de, f'{today - timedelta(days=30):%Y%m%d}')
        self.assertEqual(end_de, f'{today:%Y%m%d}')

    def test_backfill_is_marked_done(self):
        self._run()
        self.company.refresh_from_db()
        self.assertIsNone(self.company.backfill_requested_at)
        self.assertIsNotNone(self.company.backfilled_at)

    def test_backfill_creates_no_summaries(self):
        self._run()
        self.assertFalse(DisclosureSummary.objects.exists())

    def test_detect_run_continues_when_only_the_backfill_found_something(self):
        """시장 전체의 신규가 없어도 백필로 공시가 들어왔으면 뒷단계(선별)가 돌아야 한다."""
        self._run()  # SystemExit(9)가 나지 않아야 한다

    def test_detect_run_stops_when_there_is_nothing_at_all(self):
        self._run()
        with self.assertRaises(SystemExit) as caught:
            self._run()
        self.assertEqual(caught.exception.code, poll_dart.NOTHING_NEW_EXIT_CODE)

    def test_no_tracked_companies_ends_detect_without_calling_dart(self):
        watchlist.unfollow(self.user, self.company)
        with patch(f'{POLL}.latest_disclosures') as latest, \
                self.assertRaises(SystemExit) as caught:
            call_command('poll_dart', detect=True, days=2, stdout=io.StringIO())
        self.assertEqual(caught.exception.code, poll_dart.NOTHING_NEW_EXIT_CODE)
        latest.assert_not_called()

    def test_failure_waits_before_retrying_and_gives_up_at_the_limit(self):
        def broken(*args, **kwargs):
            raise DartApiError('020', '요청 제한 초과')

        with patch(f'{POLL}.iter_disclosures', side_effect=broken), \
                patch(f'{POLL}.latest_disclosures', return_value=[]):
            with self.assertRaises(SystemExit):
                call_command('poll_dart', detect=True, days=2, stdout=io.StringIO())
            self.company.refresh_from_db()
            self.assertEqual(self.company.backfill_attempts, 1)
            self.assertIsNotNone(self.company.backfill_requested_at)

            # 바로 다음 실행(1분 뒤)은 재시도 간격 전이라 부르지 않는다.
            with self.assertRaises(SystemExit):
                call_command('poll_dart', detect=True, days=2, stdout=io.StringIO())
            self.company.refresh_from_db()
            self.assertEqual(self.company.backfill_attempts, 1)

            for _ in range(retry_policy.MAX_BACKFILL_ATTEMPTS):
                Company.objects.filter(pk=self.company.pk).update(
                    backfill_attempted_at=timezone.now() - timedelta(days=1))
                with self.assertRaises(SystemExit):
                    call_command('poll_dart', detect=True, days=2, stdout=io.StringIO())
        self.company.refresh_from_db()
        self.assertEqual(self.company.backfill_attempts, retry_policy.MAX_BACKFILL_ATTEMPTS)
        self.assertIsNone(self.company.backfill_requested_at)
        self.assertTrue(self.company.backfill_gave_up)

    def test_one_run_backfills_a_bounded_number_of_companies(self):
        for n in range(2, 2 + poll_dart.BACKFILL_COMPANIES_PER_RUN + 3):
            watchlist.follow(_user(f'kakao_{n}'), _listed(n))
        self._run()
        self.assertEqual(
            Company.objects.filter(backfilled_at__isnull=False).count(),
            poll_dart.BACKFILL_COMPANIES_PER_RUN)

    def test_daily_budget_stops_backfill(self):
        with patch(f'{POLL}.BACKFILL_COMPANIES_PER_DAY', 0), \
                self.assertRaises(SystemExit):
            self._run()
        self.assertFalse([c for c in self.calls if c[0]])


def _corp_zip(entries):
    """corpCode.xml ZIP 대역. entries = [(corp_code, name, stock_code)]"""
    rows = ''.join(
        f'<list><corp_code>{c}</corp_code><corp_name>{n}</corp_name>'
        f'<stock_code>{s}</stock_code><modify_date>20260101</modify_date></list>'
        for c, n, s in entries)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        zf.writestr('CORPCODE.xml', f'<?xml version="1.0" encoding="UTF-8"?><result>{rows}</result>')
    return buf.getvalue()


class ListedCorpSyncTest(TestCase):
    """상장사 명단 사본 — 검색은 이것만 본다."""

    def _sync(self, entries):
        response = type('R', (), {})()
        response.content = _corp_zip(entries)
        response.headers = {'Content-Type': 'application/x-msdownload'}
        response.raise_for_status = lambda: None
        with patch('disclosures.dart.requests.get', return_value=response), \
                patch('disclosures.management.commands.sync_listed_corps.MIN_EXPECTED_CORPS', 2):
            call_command('sync_listed_corps', stdout=io.StringIO())

    def test_download_keeps_only_listed_companies(self):
        self._sync([('00000001', '상장사', '000001'), ('00000002', '비상장사', ' '),
                    ('00000003', '상장사2', '000003')])
        self.assertEqual(
            sorted(ListedCorp.objects.values_list('name', flat=True)), ['상장사', '상장사2'])

    def test_sync_adds_renames_and_removes(self):
        self._sync([('00000001', '옛이름', '000001'), ('00000002', '폐지될곳', '000002')])
        self._sync([('00000001', '새이름', '000001'), ('00000003', '신규상장', '000003')])
        self.assertEqual(
            dict(ListedCorp.objects.values_list('corp_code', 'name')),
            {'00000001': '새이름', '00000003': '신규상장'})

    def test_a_suspiciously_short_list_changes_nothing(self):
        self._sync([('00000001', '가', '000001'), ('00000002', '나', '000002')])
        response = type('R', (), {})()
        response.content = _corp_zip([('00000009', '하나뿐', '000009')])
        response.headers = {}
        response.raise_for_status = lambda: None
        with patch('disclosures.dart.requests.get', return_value=response), \
                self.assertRaises(CommandError):
            call_command('sync_listed_corps', stdout=io.StringIO())
        self.assertEqual(ListedCorp.objects.count(), 2)

    def test_sync_is_scheduled_under_the_pipeline_lock(self):
        """DB에 쓰는 프로세스를 하나로 — 파이프라인과 같은 잠금을 기다렸다 잡는다."""
        crontab = (Path(__file__).resolve().parent.parent / 'deploy' / 'crontab').read_text(
            encoding='utf-8')
        line = next(l for l in crontab.splitlines()
                    if 'sync_listed_corps' in l and not l.startswith('#'))
        self.assertIn('flock -w', line)
        self.assertIn('/tmp/dart-pipeline.lock', line)
        minute = line.split()[0]
        self.assertNotIn(minute, ('0', '*', '5'), '파이프라인 줄과 같은 분에 뜬다(주의 3)')


class WatchViewsTest(TestCase):
    """화면 — 내 관심 기업만 보이고, 검색·추가는 DART를 부르지 않는다."""

    def setUp(self):
        self.user = _user()
        self.client.force_login(self.user)
        self.mine_listed, self.other_listed = _listed(1), _listed(2)
        watchlist.follow(self.user, self.mine_listed)
        watchlist.follow(_user('kakao_other'), self.other_listed)
        self.mine = Company.objects.get(corp_code=self.mine_listed.corp_code)
        self.other = Company.objects.get(corp_code=self.other_listed.corp_code)
        _published(self.mine, '20260901000001', '내기업공시')
        _published(self.other, '20260901000002', '남의기업공시')

    def test_home_shows_only_my_companies(self):
        response = self.client.get(reverse('disclosures:home'))
        self.assertContains(response, '내기업공시')
        self.assertNotContains(response, '남의기업공시')

    def test_home_without_watches_invites_a_search(self):
        watchlist.unfollow(self.user, self.mine)
        response = self.client.get(reverse('disclosures:home'))
        self.assertContains(response, '아직 관심 기업이 없습니다')

    def test_unwatched_company_page_shows_only_the_add_button(self):
        response = self.client.get(
            reverse('disclosures:company_detail', args=[self.other.stock_code]))
        self.assertContains(response, '관심 기업에 추가')
        self.assertNotContains(response, '남의기업공시')

    def test_company_nobody_tracks_yet_still_opens_from_the_list(self):
        listed = _listed(3)
        response = self.client.get(
            reverse('disclosures:company_detail', args=[listed.stock_code]))
        self.assertContains(response, listed.name)
        self.assertContains(response, '관심 기업에 추가')

    def test_unknown_stock_code_is_404(self):
        self.assertEqual(self.client.get(
            reverse('disclosures:company_detail', args=['999999'])).status_code, 404)

    def test_watched_company_page_shows_its_feed(self):
        response = self.client.get(
            reverse('disclosures:company_detail', args=[self.mine.stock_code]))
        self.assertContains(response, '내기업공시')
        self.assertContains(response, '관심 기업에서 빼기')

    def test_backfill_in_progress_is_announced(self):
        response = self.client.get(
            reverse('disclosures:company_detail', args=[self.mine.stock_code]))
        self.assertContains(response, '최근 30일 공시를 불러오고 있습니다')

    def test_search_finds_listed_companies_without_calling_dart(self):
        with patch('disclosures.dart.requests.get') as dart_get:
            response = self.client.get(reverse('disclosures:search'), {'q': '테스트기업'})
        dart_get.assert_not_called()
        self.assertContains(response, '테스트기업2')
        # 이미 고른 기업에는 추가 버튼 대신 표시가 붙는다.
        self.assertContains(response, '· 관심 기업')

    def test_add_and_remove(self):
        listed = _listed(4)
        self.client.post(reverse('disclosures:watch_add'), {'corp_code': listed.corp_code})
        self.assertTrue(self.user.watches.filter(company__corp_code=listed.corp_code).exists())
        self.client.post(reverse('disclosures:watch_remove'), {'corp_code': listed.corp_code})
        self.assertFalse(self.user.watches.filter(company__corp_code=listed.corp_code).exists())

    def test_add_requires_post(self):
        response = self.client.get(
            reverse('disclosures:watch_add'), {'corp_code': self.other_listed.corp_code})
        self.assertEqual(response.status_code, 405)

    def test_limit_is_shown_as_a_message_not_an_error_page(self):
        for n in range(10, 10 + watchlist.MAX_WATCHES_PER_USER - 1):
            watchlist.follow(self.user, _listed(n))
        response = self.client.post(
            reverse('disclosures:watch_add'), {'corp_code': _listed(99).corp_code},
            follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f'{watchlist.MAX_WATCHES_PER_USER}곳까지')

    def test_next_never_redirects_off_site(self):
        response = self.client.post(reverse('disclosures:watch_add'), {
            'corp_code': _listed(5).corp_code, 'next': 'https://evil.example/'})
        self.assertEqual(response['Location'], reverse('disclosures:home'))

    def test_live_status_ignores_other_peoples_companies(self):
        before = self.client.get(reverse('disclosures:latest_status')).json()['signature']
        _published(self.other, '20260902000009', '남의새공시')
        after = self.client.get(reverse('disclosures:latest_status')).json()['signature']
        self.assertEqual(before, after)
        _published(self.mine, '20260902000010', '내새공시')
        self.assertNotEqual(
            after, self.client.get(reverse('disclosures:latest_status')).json()['signature'])

    def test_anonymous_cannot_add(self):
        self.client.logout()
        response = self.client.post(
            reverse('disclosures:watch_add'), {'corp_code': _listed(6).corp_code})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Watch.objects.filter(company__corp_code='00000006').count(), 0)
