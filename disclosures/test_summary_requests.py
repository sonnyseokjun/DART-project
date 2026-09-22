"""누르면 요약 · 월 비용 상한 테스트 (이슈 #44 PR 3).

실제 OpenAI는 부르지 않는다. 비용 장부는 `_get_client`만 가짜로 바꿔 **진짜
`_call_openai`를 태워** 확인한다 — 호출 경계(_call_openai)를 통째로 바꾸면 장부를
채우는 코드 자체를 건너뛰게 되어 "기록되는가"를 볼 수 없다.
"""
import io
import json
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from disclosures import ai_budget, retry_policy, summarizer, summary_requests
from disclosures.dart import dart_viewer_url
from disclosures.management.commands import summarize_disclosures as summarize_command
from disclosures.models import (
    MAX_SUMMARY_ATTEMPTS, AiUsage, Company, Disclosure, DisclosureSummary, Sector,
    SummaryRequest, Watch,
)
from disclosures.selection import ExclusionReason, SelectionState

RAW = '단일판매ㆍ공급계약 체결\n계약금액 | 1,234,567\n매출액 대비 | 12.34%'


def _payload():
    return json.dumps({
        'one_line': '삼성전자가 1,234,567원 규모의 공급계약을 체결했다.',
        'easy_explanation': '첫 문장이다. 둘째 문장이다. 셋째 문장이다.',
        'why_important': '매출에 영향을 준다.',
        'importance': 'medium',
        'evidence': [{'field': 'one_line', 'claim': '계약금액 1,234,567원',
                      'quote': '계약금액 | 1,234,567'}],
    }, ensure_ascii=False)


def _response(content, finish_reason='stop'):
    usage = SimpleNamespace(
        prompt_tokens=1500, completion_tokens=200, total_tokens=1700,
        prompt_tokens_details=SimpleNamespace(cached_tokens=0, cache_write_tokens=0),
        completion_tokens_details=SimpleNamespace(reasoning_tokens=0))
    message = SimpleNamespace(content=content, refusal=None)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason=finish_reason)],
        usage=usage, model='gpt-5.6-luna')


class _FakeClient:
    """OpenAI 클라이언트 대역. 준비한 응답을 차례로 돌려주고 호출 횟수를 센다."""

    def __init__(self, *contents):
        self.contents = list(contents)
        self.calls = 0
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls += 1
        body = self.contents.pop(0) if len(self.contents) > 1 else self.contents[0]
        return _response(body)


class _Fixture(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username='kakao_1')
        # 기타 섹터는 데이터 마이그레이션(0011)이 이미 만들어 둔다.
        sector, _ = Sector.objects.get_or_create(slug='etc', defaults={'name': '기타'})
        self.company = Company.objects.create(
            sector=sector, corp_code='00126380', stock_code='005930', name='삼성전자')
        Watch.objects.create(user=self.user, company=self.company)
        self.target = self._disclosure('20260901000001')

    def _disclosure(self, rcept_no, **fields):
        defaults = dict(
            company=self.company, rcept_no=rcept_no, report_name='단일판매ㆍ공급계약체결',
            disclosure_type='거래소공시', filed_at=date(2026, 9, 1),
            dart_url=dart_viewer_url(rcept_no), selection_state=SelectionState.TARGET)
        defaults.update(fields)
        return Disclosure.objects.create(**defaults)


class RequestPolicyTest(_Fixture):
    """버튼을 누르면 — 요청만 기록하고, 공시당 한 번, 계정당 하루 20번."""

    def test_request_marks_the_disclosure_queued(self):
        self.assertEqual(self.target.summary_state, 'available')
        self.assertTrue(summary_requests.request_summary(self.user, self.target))
        self.target.refresh_from_db()
        self.assertEqual(self.target.summary_state, 'queued')

    def test_request_does_not_call_the_ai(self):
        with patch.object(summarizer, '_get_client') as client:
            summary_requests.request_summary(self.user, self.target)
        client.assert_not_called()

    def test_a_second_request_by_anyone_is_a_no_op(self):
        other = get_user_model().objects.create_user(username='kakao_2')
        summary_requests.request_summary(self.user, self.target)
        first = Disclosure.objects.get(pk=self.target.pk).summary_requested_at
        self.target.refresh_from_db()
        self.assertFalse(summary_requests.request_summary(other, self.target))
        self.assertEqual(
            Disclosure.objects.get(pk=self.target.pk).summary_requested_at, first)

    def test_twenty_first_request_of_the_day_is_refused_and_leaves_nothing(self):
        for n in range(summary_requests.MAX_REQUESTS_PER_DAY):
            summary_requests.request_summary(
                self.user, self._disclosure(f'2026090200{n:04d}'))
        extra = self._disclosure('20260903000001')
        with self.assertRaises(summary_requests.RequestRefused):
            summary_requests.request_summary(self.user, extra)
        extra.refresh_from_db()
        self.assertIsNone(extra.summary_requested_at)
        self.assertFalse(SummaryRequest.objects.filter(disclosure=extra).exists())

    def test_non_target_and_failed_disclosures_cannot_be_requested(self):
        excluded = self._disclosure(
            '20260901000002', selection_state=SelectionState.EXCLUDED,
            exclusion_reason=ExclusionReason.BLACKLIST)
        failed = self._disclosure(
            '20260901000003', summary_attempts=MAX_SUMMARY_ATTEMPTS)
        for disclosure in (excluded, failed):
            with self.subTest(state=disclosure.summary_state):
                with self.assertRaises(summary_requests.RequestRefused):
                    summary_requests.request_summary(self.user, disclosure)

    def test_fetch_failures_at_the_cap_also_count_as_failed(self):
        stuck = self._disclosure(
            '20260901000004', raw_fetch_attempts=retry_policy.MAX_FETCH_ATTEMPTS)
        self.assertEqual(stuck.summary_state, 'failed')

    @override_settings(AI_MONTHLY_BUDGET_USD=0.05)
    def test_requests_are_refused_once_the_budget_is_used_up(self):
        AiUsage.objects.create(model_name='gpt-5.6-luna', cost_usd=0.05)
        with self.assertRaises(summary_requests.RequestRefused) as caught:
            summary_requests.request_summary(self.user, self.target)
        self.assertIn('다음 달 1일', str(caught.exception))


class AutomaticSummaryStoppedTest(_Fixture):
    """자동 요약 중단 — 요청이 없는 공시는 원문도 받지 않고 요약도 하지 않는다."""

    def setUp(self):
        super().setUp()
        self.target.raw_fetched = True
        self.target.raw_content = RAW
        self.target.save()

    def test_unrequested_target_is_not_summarized(self):
        client = _FakeClient(_payload())
        with patch.object(summarizer, '_get_client', return_value=client):
            call_command('summarize_disclosures', stdout=io.StringIO())
        self.assertEqual(client.calls, 0)
        self.assertFalse(DisclosureSummary.objects.exists())

    def test_unrequested_target_is_not_fetched(self):
        unfetched = self._disclosure('20260901000009')
        with patch('disclosures.management.commands.fetch_documents.fetch_document') as fetch:
            call_command('fetch_documents', stdout=io.StringIO())
        fetch.assert_not_called()
        unfetched.refresh_from_db()
        self.assertFalse(unfetched.raw_fetched)

    def test_pending_work_ignores_unrequested_targets(self):
        self._disclosure('20260901000009')
        counts = retry_policy.pending_counts()
        self.assertEqual((counts['원문'], counts['요약']), (0, 0))

    def test_requested_target_is_summarized_once(self):
        summary_requests.request_summary(self.user, self.target)
        client = _FakeClient(_payload())
        with patch.object(summarizer, '_get_client', return_value=client), \
                patch.object(summarizer, 'check_api_key', return_value='sk-test'), \
                patch.object(summarize_command, 'check_api_key', return_value='sk-test'):
            call_command('summarize_disclosures', stdout=io.StringIO())
            call_command('summarize_disclosures', stdout=io.StringIO())
        self.assertEqual(client.calls, 1)
        self.assertTrue(DisclosureSummary.objects.filter(disclosure=self.target).exists())

    def test_earliest_request_is_processed_first(self):
        later = self._disclosure('20260801000001', raw_fetched=True, raw_content=RAW)
        Disclosure.objects.filter(pk=later.pk).update(
            summary_requested_at=timezone.now())
        Disclosure.objects.filter(pk=self.target.pk).update(
            summary_requested_at=timezone.now() - timedelta(minutes=5))
        client = _FakeClient(_payload())
        with patch.object(summarizer, '_get_client', return_value=client), \
                patch.object(summarize_command, 'check_api_key', return_value='sk-test'):
            call_command('summarize_disclosures', limit=1, stdout=io.StringIO())
        self.assertTrue(DisclosureSummary.objects.filter(disclosure=self.target).exists())
        self.assertFalse(DisclosureSummary.objects.filter(disclosure=later).exists())


class CostLedgerTest(_Fixture):
    """비용 장부 — AI 응답마다 한 줄. 버린 응답도 돈은 나갔다."""

    def setUp(self):
        super().setUp()
        Disclosure.objects.filter(pk=self.target.pk).update(
            raw_fetched=True, raw_content=RAW, summary_requested_at=timezone.now())

    def _run(self, *contents, **options):
        client = _FakeClient(*contents)
        with patch.object(summarizer, '_get_client', return_value=client), \
                patch.object(summarize_command, 'check_api_key', return_value='sk-test'):
            call_command('summarize_disclosures', stdout=io.StringIO(), **options)
        return client

    def test_each_successful_call_is_recorded(self):
        self._run(_payload())
        usage = AiUsage.objects.get()
        self.assertEqual(usage.disclosure, self.target)
        self.assertEqual((usage.input_tokens, usage.output_tokens), (1500, 200))
        self.assertGreater(usage.cost_usd, 0)

    def test_discarded_responses_are_recorded_too(self):
        """형식이 틀려 버린 응답도 돈이 나갔다. 8단계 전에는 어디에도 남지 않았다."""
        client = self._run('JSON이 아님', _payload())
        self.assertEqual(client.calls, 2)
        self.assertEqual(AiUsage.objects.count(), 2)

    def test_a_summary_that_finally_fails_still_leaves_its_cost(self):
        client = self._run('JSON이 아님')
        self.assertFalse(DisclosureSummary.objects.exists())
        self.assertEqual(AiUsage.objects.count(), client.calls)
        self.assertGreater(client.calls, 1)

    def test_last_months_usage_does_not_count(self):
        old = AiUsage.objects.create(model_name='gpt-5.6-luna', cost_usd=100)
        AiUsage.objects.filter(pk=old.pk).update(
            created_at=ai_budget.month_start() - timedelta(seconds=1))
        self.assertEqual(ai_budget.spent_this_month(), 0)


class MonthlyBudgetTest(_Fixture):
    """월 상한 — 닿으면 요약하지 않고, 요청은 남겨 다음 달에 처리한다."""

    def setUp(self):
        super().setUp()
        Disclosure.objects.filter(pk=self.target.pk).update(
            raw_fetched=True, raw_content=RAW, summary_requested_at=timezone.now())

    @override_settings(AI_MONTHLY_BUDGET_USD=0.05)
    def test_exhausted_budget_stops_before_calling_the_ai(self):
        AiUsage.objects.create(model_name='gpt-5.6-luna', cost_usd=0.05)
        client = _FakeClient(_payload())
        with patch.object(summarizer, '_get_client', return_value=client), \
                patch.object(summarize_command, 'check_api_key', return_value='sk-test'):
            call_command('summarize_disclosures', stdout=io.StringIO())
        self.assertEqual(client.calls, 0)
        self.target.refresh_from_db()
        self.assertIsNotNone(self.target.summary_requested_at, '요청이 지워지면 안 된다')

    @override_settings(AI_MONTHLY_BUDGET_USD=0.05)
    def test_a_nearly_used_budget_does_not_start_an_item_it_cannot_afford(self):
        """남은 돈이 이 건의 추정 비용 × 2보다 적으면 시작하지 않는다(재생성 몫)."""
        estimate = summarizer.estimate_summary_cost(RAW)['usd']
        AiUsage.objects.create(
            model_name='gpt-5.6-luna', cost_usd=0.05 - estimate * 1.5)
        client = _FakeClient(_payload())
        with patch.object(summarizer, '_get_client', return_value=client), \
                patch.object(summarize_command, 'check_api_key', return_value='sk-test'):
            call_command('summarize_disclosures', stdout=io.StringIO())
        self.assertEqual(client.calls, 0)

    @override_settings(AI_MONTHLY_BUDGET_USD=0.05)
    def test_pending_work_does_not_count_summaries_while_exhausted(self):
        """세면 다음 달까지 파이프라인이 매분 헛돈다."""
        self.assertEqual(retry_policy.pending_counts()['요약'], 1)
        AiUsage.objects.create(model_name='gpt-5.6-luna', cost_usd=0.05)
        self.assertEqual(retry_policy.pending_counts()['요약'], 0)

    @override_settings(AI_MONTHLY_BUDGET_USD=0.05)
    def test_next_month_the_queued_request_goes_through(self):
        spent = AiUsage.objects.create(model_name='gpt-5.6-luna', cost_usd=0.05)
        AiUsage.objects.filter(pk=spent.pk).update(
            created_at=ai_budget.month_start() - timedelta(days=1))
        self.assertFalse(ai_budget.is_exhausted())

    def test_default_budget_is_below_ten_thousand_won(self):
        """운영자가 정한 월 1만 원(약 $7)보다 낮아야 한다 — 비용은 추정치다."""
        self.assertLess(ai_budget.monthly_budget(), 7)


class RequestScreensTest(_Fixture):
    """화면 — 버튼은 요청만 기록하고, 상태별로 다른 안내를 보여준다."""

    def setUp(self):
        super().setUp()
        self.client.force_login(self.user)

    def test_unrequested_target_card_has_the_button(self):
        self.assertContains(self.client.get(reverse('disclosures:home')), 'AI 요약 보기')

    def test_pressing_the_button_queues_without_calling_the_ai(self):
        with patch.object(summarizer, '_get_client') as client:
            response = self.client.post(
                reverse('disclosures:summary_request'),
                {'rcept_no': self.target.rcept_no}, follow=True)
        client.assert_not_called()
        self.assertContains(response, 'AI가 정리 중')
        self.target.refresh_from_db()
        self.assertIsNotNone(self.target.summary_requested_at)

    def test_button_requires_post_and_login(self):
        url = reverse('disclosures:summary_request')
        self.assertEqual(self.client.get(url).status_code, 405)
        self.client.logout()
        self.assertEqual(self.client.post(url, {'rcept_no': self.target.rcept_no})
                         .status_code, 302)
        self.target.refresh_from_db()
        self.assertIsNone(self.target.summary_requested_at)

    def test_anonymous_detail_invites_login_instead_of_a_button(self):
        self.client.logout()
        response = self.client.get(
            reverse('disclosures:disclosure_detail', args=[self.target.rcept_no]))
        self.assertContains(response, '로그인</a>하면 AI 요약을 요청할 수 있습니다')
        self.assertNotContains(response, 'AI 요약 보기')

    def test_failed_disclosure_says_so_and_offers_no_button(self):
        Disclosure.objects.filter(pk=self.target.pk).update(
            summary_attempts=MAX_SUMMARY_ATTEMPTS)
        response = self.client.get(reverse('disclosures:home'))
        self.assertContains(response, '요약을 만들지 못했습니다')
        self.assertNotContains(response, 'AI 요약 보기')

    @override_settings(AI_MONTHLY_BUDGET_USD=0.05)
    def test_exhausted_budget_is_announced_and_the_button_disappears(self):
        AiUsage.objects.create(model_name='gpt-5.6-luna', cost_usd=0.05)
        response = self.client.get(reverse('disclosures:home'))
        self.assertContains(response, '이번 달 AI 요약 한도에 도달했습니다')
        self.assertNotContains(response, 'AI 요약 보기')

    def test_simple_reports_are_hidden_unless_asked_for(self):
        self._disclosure('20260901000005', report_name='임원ㆍ주요주주특정증권등소유상황보고서',
                         selection_state=SelectionState.EXCLUDED,
                         exclusion_reason=ExclusionReason.BLACKLIST)
        home = reverse('disclosures:home')
        self.assertNotContains(self.client.get(home), '임원ㆍ주요주주특정증권등소유상황보고서')
        response = self.client.get(home, {'all': '1'})
        self.assertContains(response, '임원ㆍ주요주주특정증권등소유상황보고서')
        self.assertContains(response, '단순 보고')

    def test_returning_from_a_live_updated_card_drops_the_partial_flag(self):
        """자동 갱신으로 다시 그린 카드의 폼은 next에 partial=1이 붙어 온다."""
        response = self.client.post(reverse('disclosures:summary_request'), {
            'rcept_no': self.target.rcept_no, 'next': '/?all=1&partial=1'})
        self.assertEqual(response['Location'], '/?all=1')

    def test_hidden_summary_offers_no_button(self):
        """숨긴 요약을 버튼으로 되살리면 다시 만들 수 없는 요약을 요청하게 된다."""
        DisclosureSummary.objects.create(
            disclosure=self.target, one_line='숨긴 요약', easy_explanation='설명이다.',
            why_important='이유다.', importance=DisclosureSummary.Importance.LOW,
            model_name='gpt-5.6-luna', evidence=[], is_published=False)
        response = self.client.get(reverse('disclosures:home'), {'all': '1'})
        self.assertNotContains(response, self.target.report_name)
