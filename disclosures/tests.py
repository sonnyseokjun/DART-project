"""데이터 수집·선별·원문 확보 파이프라인 회귀 테스트.

DART 실호출 없이(네트워크·API 키 불필요) 동작을 고정한다.
seed_companies는 download_corp_codes를, poll_dart는 iter_disclosures를,
fetch_documents는 fetch_document를 목으로 대체한다.
"""
import json
import os
import re
import subprocess
import sys
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from django.contrib.admin.sites import AdminSite
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import RequestFactory, TestCase
from django.urls import reverse

from disclosures.dart import (
    MAX_LIST_SPAN_DAYS, OFFERING_KEY_SECTIONS, PBLNTF_TYPES, PERIODIC_KEY_SECTIONS,
    DartApiError, dart_viewer_url, extract_key_sections, is_dart_xml,
    preprocess_document, split_date_range, strip_markup,
)
from disclosures.admin import (
    DEFAULT_HIDDEN_REASON, DisclosureSummaryAdmin, DisclosureSummaryInline,
)
from disclosures.management.commands import (
    summarize_disclosures as summarize_command,
)
from disclosures.management.commands.seed_companies import TARGET_COMPANIES
from disclosures.models import Company, Disclosure, DisclosureSummary, Sector
from disclosures.templatetags.review_panel import (
    evidence_field_label, has_key, highlight_terms,
)
from disclosures import review_policy, summarizer, units, verification, views
from disclosures.review_policy import (
    MAX_REGENERATION_ATTEMPTS, ReviewCategory, should_regenerate,
)
from disclosures.selection import (
    ExclusionReason, SelectionState, evaluate, is_blacklisted, normalize_title,
)


def _fake_corp_codes(skip=()):
    """대상 10개 기업 + 비추적 1곳을 담은 corpCode.xml 파싱 결과 모사."""
    rows = [
        {'corp_code': f'{i:08d}', 'corp_name': name, 'stock_code': stock_code}
        for i, (stock_code, name, _sub) in enumerate(TARGET_COMPANIES, start=1)
        if stock_code not in skip
    ]
    # 상장사 목록에는 추적하지 않는 기업도 섞여 있다(로컬 필터 대상).
    rows.append({'corp_code': '99990000', 'corp_name': '무관기업', 'stock_code': '999999'})
    return rows


class SeedCompaniesTest(TestCase):
    @patch('disclosures.management.commands.seed_companies.download_corp_codes')
    def test_maps_all_ten_companies(self, mock_download):
        mock_download.return_value = _fake_corp_codes()

        call_command('seed_companies')

        self.assertEqual(Sector.objects.filter(slug='semiconductor').count(), 1)
        self.assertEqual(Company.objects.count(), len(TARGET_COMPANIES))
        # 종목코드로 corp_code가 매핑됐는지(누락 0건)
        samsung = Company.objects.get(stock_code='005930')
        self.assertEqual(samsung.corp_code, '00000001')
        self.assertTrue(samsung.is_active)

    @patch('disclosures.management.commands.seed_companies.download_corp_codes')
    def test_missing_company_raises(self, mock_download):
        # corpCode.xml에서 삼성전자가 빠지면 CommandError로 실패해야 한다.
        mock_download.return_value = _fake_corp_codes(skip=('005930',))

        with self.assertRaises(CommandError) as ctx:
            call_command('seed_companies')
        self.assertIn('삼성전자', str(ctx.exception))

    @patch('disclosures.management.commands.seed_companies.download_corp_codes')
    def test_seed_is_idempotent(self, mock_download):
        mock_download.return_value = _fake_corp_codes()

        call_command('seed_companies')
        call_command('seed_companies')  # 두 번째 실행은 갱신만

        self.assertEqual(Company.objects.count(), len(TARGET_COMPANIES))


# 추적 기업(삼성전자) corp_code와 비추적 corp_code
TRACKED_CORP = '00126380'
UNTRACKED_CORP = '99990000'

# poll_dart가 pblntf_ty별로 iter_disclosures를 호출하면 유형에 맞는 항목을 돌려준다.
FAKE_BY_TYPE = {
    'D': [  # 지분공시 — 추적 1건 + 비추적 1건(필터되어야 함)
        {
            'corp_code': TRACKED_CORP,
            'report_nm': '임원ㆍ주요주주특정증권등소유상황보고서              ',
            'rcept_no': '20260724000001',
            'rcept_dt': '20260724',
        },
        {
            'corp_code': UNTRACKED_CORP,
            'report_nm': '주식등의대량보유상황보고서',
            'rcept_no': '20260724000002',
            'rcept_dt': '20260724',
        },
    ],
    'I': [  # 거래소공시 — 추적 1건
        {
            'corp_code': TRACKED_CORP,
            'report_nm': '기업가치제고계획(자율공시)',
            'rcept_no': '20260725000003',
            'rcept_dt': '20260725',
        },
    ],
}


def _fake_iter(bgn_de, end_de, corp_code=None, pblntf_ty=None):
    return iter(FAKE_BY_TYPE.get(pblntf_ty, []))


class _RecordingIter:
    """iter_disclosures 대역. 호출된 (bgn_de, end_de)를 순서대로 기록한다.

    날짜 범위와 무관하게 같은 항목을 돌려주므로, 청크가 여러 개면 같은 공시가
    청크 수만큼 반복 조회된다 — 멱등성 검증에 그대로 쓸 수 있다.
    """

    def __init__(self):
        self.calls = []

    def __call__(self, bgn_de, end_de, corp_code=None, pblntf_ty=None):
        self.calls.append((bgn_de, end_de))
        return iter(FAKE_BY_TYPE.get(pblntf_ty, []))

    @property
    def chunks(self):
        """유형별 반복 호출을 접어 날짜 청크 목록만 순서대로 반환."""
        chunks = []
        for date_range in self.calls:
            if date_range not in chunks:
                chunks.append(date_range)
        return chunks


class PollDartTest(TestCase):
    def setUp(self):
        self.sector = Sector.objects.create(name='반도체', slug='semiconductor')
        self.samsung = Company.objects.create(
            sector=self.sector, corp_code=TRACKED_CORP,
            stock_code='005930', name='삼성전자', is_active=True,
        )

    @patch('disclosures.management.commands.poll_dart.iter_disclosures', side_effect=_fake_iter)
    def test_collects_only_tracked_with_type(self, _mock_iter):
        call_command('poll_dart', days=7)

        # 비추적 기업 공시는 저장되지 않는다(로컬 필터).
        self.assertEqual(Disclosure.objects.count(), 2)
        self.assertFalse(Disclosure.objects.filter(rcept_no='20260724000002').exists())

        # 유형이 조회한 pblntf_ty에 맞게 태깅된다.
        d = Disclosure.objects.get(rcept_no='20260724000001')
        self.assertEqual(d.disclosure_type, PBLNTF_TYPES['D'])  # 지분공시
        self.assertEqual(
            Disclosure.objects.get(rcept_no='20260725000003').disclosure_type,
            PBLNTF_TYPES['I'],  # 거래소공시
        )

    @patch('disclosures.management.commands.poll_dart.iter_disclosures', side_effect=_fake_iter)
    def test_field_mapping(self, _mock_iter):
        call_command('poll_dart', days=7)

        d = Disclosure.objects.get(rcept_no='20260724000001')
        self.assertEqual(d.company, self.samsung)
        # report_nm의 뒤쪽 공백이 제거된다.
        self.assertEqual(d.report_name, '임원ㆍ주요주주특정증권등소유상황보고서')
        # rcept_dt(YYYYMMDD)가 날짜로 파싱된다.
        self.assertEqual(d.filed_at, date(2026, 7, 24))
        # 원문 링크가 rcept_no로 구성된다.
        self.assertEqual(d.dart_url, dart_viewer_url('20260724000001'))

    @patch('disclosures.management.commands.poll_dart.iter_disclosures', side_effect=_fake_iter)
    def test_idempotent_rerun(self, _mock_iter):
        call_command('poll_dart', days=7)
        call_command('poll_dart', days=7)  # 재실행

        # 중복 저장이 없어야 한다(rcept_no unique).
        self.assertEqual(Disclosure.objects.count(), 2)
        self.assertEqual(
            Disclosure.objects.values('rcept_no').distinct().count(), 2
        )

    def test_no_companies_warns_and_skips(self):
        Company.objects.all().delete()
        with patch(
            'disclosures.management.commands.poll_dart.iter_disclosures',
            side_effect=_fake_iter,
        ) as mock_iter:
            call_command('poll_dart', days=7)
        # 추적 기업이 없으면 DART를 호출하지 않고 조기 반환한다.
        mock_iter.assert_not_called()
        self.assertEqual(Disclosure.objects.count(), 0)


class SplitDateRangeTest(TestCase):
    """검색기간 3개월 한도(오류 코드 100) 회피용 날짜 분할 단위 테스트."""

    def test_short_range_is_single_chunk(self):
        chunks = split_date_range(date(2026, 7, 1), date(2026, 7, 10))
        self.assertEqual(chunks, [(date(2026, 7, 1), date(2026, 7, 10))])

    def test_range_at_limit_is_not_split(self):
        bgn = date(2026, 1, 1)
        end = bgn + timedelta(days=MAX_LIST_SPAN_DAYS)  # 실측 통과 경계(89일)
        self.assertEqual(split_date_range(bgn, end), [(bgn, end)])

    def test_one_day_over_limit_splits(self):
        bgn = date(2026, 1, 1)
        end = bgn + timedelta(days=MAX_LIST_SPAN_DAYS + 1)
        self.assertEqual(split_date_range(bgn, end), [
            (bgn, bgn + timedelta(days=MAX_LIST_SPAN_DAYS)),
            (bgn + timedelta(days=MAX_LIST_SPAN_DAYS), end),
        ])

    def test_every_chunk_within_limit_and_covers_range(self):
        bgn, end = date(2024, 1, 1), date(2026, 7, 26)  # 2년 반
        chunks = split_date_range(bgn, end)

        self.assertGreater(len(chunks), 1)
        for chunk_bgn, chunk_end in chunks:
            self.assertLessEqual((chunk_end - chunk_bgn).days, MAX_LIST_SPAN_DAYS)
        # 청크들의 합집합이 전체 범위를 빠짐없이 덮는다.
        covered = set()
        for chunk_bgn, chunk_end in chunks:
            covered.update(
                chunk_bgn + timedelta(days=i)
                for i in range((chunk_end - chunk_bgn).days + 1)
            )
        expected = {bgn + timedelta(days=i) for i in range((end - bgn).days + 1)}
        self.assertEqual(covered, expected)

    def test_chunks_overlap_by_one_day(self):
        chunks = split_date_range(date(2026, 1, 1), date(2026, 6, 30))
        for prev, nxt in zip(chunks, chunks[1:]):
            # 앞 창의 종료일 = 뒤 창의 시작일 (경계일 중복 조회)
            self.assertEqual(prev[1], nxt[0])

    def test_reversed_range_raises(self):
        with self.assertRaises(ValueError):
            split_date_range(date(2026, 7, 10), date(2026, 7, 1))


class PollDartBackfillTest(TestCase):
    """--bgn/--end 임의 구간 지정과 3개월 초과 범위 자동 분할."""

    def setUp(self):
        self.sector = Sector.objects.create(name='반도체', slug='semiconductor')
        Company.objects.create(
            sector=self.sector, corp_code=TRACKED_CORP,
            stock_code='005930', name='삼성전자', is_active=True,
        )

    def _run(self, **options):
        """iter_disclosures를 기록용 대역으로 바꿔 poll_dart를 실행한다."""
        recorder = _RecordingIter()
        with patch(
            'disclosures.management.commands.poll_dart.iter_disclosures',
            new=recorder,
        ):
            call_command('poll_dart', **options)
        return recorder

    def test_bgn_end_queries_given_range(self):
        recorder = self._run(bgn='20260701', end='20260710')

        self.assertEqual(recorder.chunks, [('20260701', '20260710')])
        # 유형 10종을 모두 조회한다(공시유형 태깅).
        self.assertEqual(len(recorder.calls), len(PBLNTF_TYPES))
        self.assertEqual(Disclosure.objects.count(), 2)

    def test_bgn_without_end_uses_today(self):
        recorder = self._run(bgn=f'{date.today() - timedelta(days=2):%Y%m%d}')

        self.assertEqual(
            recorder.chunks, [(
                f'{date.today() - timedelta(days=2):%Y%m%d}',
                f'{date.today():%Y%m%d}',
            )]
        )

    def test_days_with_bgn_end_raises(self):
        with self.assertRaises(CommandError) as ctx:
            self._run(days=7, bgn='20260701', end='20260710')
        self.assertIn('함께 지정할 수 없습니다', str(ctx.exception))

        with self.assertRaises(CommandError):
            self._run(days=7, bgn='20260701')
        with self.assertRaises(CommandError):
            self._run(days=7, end='20260710')

    def test_end_without_bgn_raises(self):
        with self.assertRaises(CommandError) as ctx:
            self._run(end='20260710')
        self.assertIn('--bgn', str(ctx.exception))

    def test_invalid_date_format_raises(self):
        for bad in ('2026-07-01', '20260732', 'abcdefgh', '202607'):
            with self.assertRaises(CommandError, msg=bad):
                self._run(bgn=bad, end='20260710')

    def test_bgn_after_end_raises(self):
        with self.assertRaises(CommandError) as ctx:
            self._run(bgn='20260710', end='20260701')
        self.assertIn('늦습니다', str(ctx.exception))

    def test_future_end_raises(self):
        future = date.today() + timedelta(days=10)
        with self.assertRaises(CommandError) as ctx:
            self._run(bgn=f'{date.today():%Y%m%d}', end=f'{future:%Y%m%d}')
        self.assertIn('미래', str(ctx.exception))

    def test_negative_days_raises(self):
        with self.assertRaises(CommandError):
            self._run(days=-1)

    def test_long_range_splits_into_expected_chunks(self):
        # 2026-01-01 ~ 06-30 (181일) → 89일 이하 창 3개. 경계일이 하루씩 겹친다.
        recorder = self._run(bgn='20260101', end='20260630')

        self.assertEqual(recorder.chunks, [
            ('20260101', '20260331'),
            ('20260331', '20260628'),
            ('20260628', '20260630'),
        ])
        # 청크 × 공시유형 만큼 호출된다.
        self.assertEqual(len(recorder.calls), 3 * len(PBLNTF_TYPES))

    def test_chunks_cover_range_without_gap(self):
        bgn, end = date(2025, 1, 1), date(2026, 7, 20)
        recorder = self._run(bgn=f'{bgn:%Y%m%d}', end=f'{end:%Y%m%d}')

        covered = set()
        for bgn_de, end_de in recorder.chunks:
            chunk_bgn = date(int(bgn_de[:4]), int(bgn_de[4:6]), int(bgn_de[6:8]))
            chunk_end = date(int(end_de[:4]), int(end_de[4:6]), int(end_de[6:8]))
            self.assertLessEqual((chunk_end - chunk_bgn).days, MAX_LIST_SPAN_DAYS)
            covered.update(
                chunk_bgn + timedelta(days=i)
                for i in range((chunk_end - chunk_bgn).days + 1)
            )
        # 전체 범위의 모든 날짜가 어느 한 청크에는 반드시 들어간다(누락 0일).
        expected = {bgn + timedelta(days=i) for i in range((end - bgn).days + 1)}
        self.assertEqual(covered, expected)

    def test_days_over_limit_completes_without_error(self):
        # --days 150은 3개월 한도를 넘지만 자동 분할로 오류 없이 완료돼야 한다.
        recorder = self._run(days=150)

        self.assertEqual(len(recorder.chunks), 2)
        for bgn_de, end_de in recorder.chunks:
            chunk_bgn = date(int(bgn_de[:4]), int(bgn_de[4:6]), int(bgn_de[6:8]))
            chunk_end = date(int(end_de[:4]), int(end_de[4:6]), int(end_de[6:8]))
            self.assertLessEqual((chunk_end - chunk_bgn).days, MAX_LIST_SPAN_DAYS)
        self.assertEqual(f'{date.today():%Y%m%d}', recorder.chunks[-1][1])

    def test_overlapping_chunks_do_not_duplicate(self):
        # 대역은 청크마다 같은 공시를 돌려준다 → 3회 조회되지만 저장은 1건씩.
        recorder = self._run(bgn='20260101', end='20260630')

        self.assertEqual(len(recorder.chunks), 3)
        self.assertEqual(Disclosure.objects.count(), 2)
        self.assertEqual(
            Disclosure.objects.filter(rcept_no='20260724000001').count(), 1
        )

    def test_backfill_then_rerun_is_idempotent(self):
        self._run(bgn='20260101', end='20260630')
        self._run(bgn='20260101', end='20260630')

        self.assertEqual(Disclosure.objects.count(), 2)


# ---------------------------------------------------------------------------
# 요약 대상 선별 정책 (disclosures/selection.py)
# ---------------------------------------------------------------------------

class SelectionPolicyTest(TestCase):
    """선별 규칙 자체를 순수 함수 수준에서 고정한다. LLM 비용이 여기 달려 있다."""

    def test_normalize_strips_bracket_prefix_and_whitespace(self):
        self.assertEqual(
            normalize_title('[기재정정]임원ㆍ주요주주특정증권등소유상황보고서'),
            '임원ㆍ주요주주특정증권등소유상황보고서',
        )
        # 정렬용 연속 공백이 섞여도 같은 제목으로 본다.
        self.assertEqual(
            normalize_title('[기재정정]소송등의판결ㆍ결정      (주주총회결의취소)'),
            '소송등의판결ㆍ결정(주주총회결의취소)',
        )
        # 대괄호 태그가 겹쳐 붙어도 모두 벗긴다.
        self.assertEqual(normalize_title('[기재정정][첨부정정]분기보고서'), '분기보고서')

    def test_blacklist_matches_exactly(self):
        self.assertTrue(is_blacklisted('임원ㆍ주요주주특정증권등소유상황보고서'))
        # [기재정정]이 붙어도 같은 공시로 판정한다.
        self.assertTrue(is_blacklisted('[기재정정]임원ㆍ주요주주특정증권등소유상황보고서'))

    def test_lookalike_reports_are_split_opposite_ways(self):
        """이름이 거의 같은 두 서류가 제외/대상으로 갈리는 것을 고정한다.

        `소유상황보고서`는 사후 신고(821건, 정형) → 제외.
        `거래계획보고서`는 내부자의 사전 매도계획 보고(2건, 신호 가치 높음) → 대상.
        부분 일치 블랙리스트를 쓰면 후자가 우연히 걸린다 (한미반도체 20260702000197 회귀).
        """
        self.assertTrue(is_blacklisted('임원ㆍ주요주주특정증권등소유상황보고서'))
        self.assertFalse(is_blacklisted('임원ㆍ주요주주특정증권등거래계획보고서'))

        state, reason = evaluate('지분공시', '임원ㆍ주요주주특정증권등소유상황보고서')
        self.assertEqual(state, SelectionState.EXCLUDED)
        self.assertEqual(reason, ExclusionReason.BLACKLIST)

        state, reason = evaluate('지분공시', '임원ㆍ주요주주특정증권등거래계획보고서')
        self.assertEqual(state, SelectionState.TARGET)
        self.assertEqual(reason, '')

    def test_excluded_types_are_excluded(self):
        for disclosure_type, report_name in (
            ('지분공시', '주식소유상황보고서'),
            ('기타공시', '주식매수선택권부여에관한신고'),
        ):
            state, reason = evaluate(disclosure_type, report_name)
            self.assertEqual(state, SelectionState.EXCLUDED, msg=report_name)
            self.assertEqual(reason, ExclusionReason.EXCLUDED_TYPE, msg=report_name)

    def test_whitelist_revives_equity_reports(self):
        """지분공시 유형 제외의 예외 — 접두어 매칭이라 (약식)·(일반)이 붙어도 걸린다."""
        for report_name in (
            '주식등의대량보유상황보고서(약식)',
            '주식등의대량보유상황보고서(일반)',
            '[기재정정]주식등의대량보유상황보고서(일반)',
            '임원ㆍ주요주주특정증권등거래계획보고서',
        ):
            state, reason = evaluate('지분공시', report_name)
            self.assertEqual(state, SelectionState.TARGET, msg=report_name)
            self.assertEqual(reason, '', msg=report_name)

    def test_whitelist_revives_treasury_stock_result_report(self):
        """지분공시 유형 제외의 예외 2 — 주요사항보고서(자기주식처분결정)의 결과 보고."""
        for report_name in (
            '자기주식처분결과보고서',
            '[기재정정]자기주식처분결과보고서',
        ):
            state, reason = evaluate('기타공시', report_name)
            self.assertEqual(state, SelectionState.TARGET, msg=report_name)
            self.assertEqual(reason, '', msg=report_name)

    def test_other_types_are_targets(self):
        for disclosure_type, report_name in (
            ('정기공시', '분기보고서 (2026.03)'),
            ('주요사항보고', '주요사항보고서(유상증자결정)'),
            ('발행공시', '증권신고서(지분증권)'),
            ('거래소공시', '단일판매ㆍ공급계약체결'),
            ('공정위공시', '특수관계인과의내부거래'),
        ):
            state, reason = evaluate(disclosure_type, report_name)
            self.assertEqual(state, SelectionState.TARGET, msg=report_name)
            self.assertEqual(reason, '', msg=report_name)


class ApplySelectionCommandTest(TestCase):
    """판정 결과가 DB에 남아 재실행 시 재평가되지 않는지."""

    def setUp(self):
        self.sector = Sector.objects.create(name='반도체', slug='semiconductor')
        self.company = Company.objects.create(
            sector=self.sector, corp_code=TRACKED_CORP,
            stock_code='005930', name='삼성전자',
        )
        self.target = self._make('20260701000001', '거래소공시', '단일판매ㆍ공급계약체결')
        self.excluded = self._make(
            '20260701000002', '지분공시', '임원ㆍ주요주주특정증권등소유상황보고서'
        )

    def _make(self, rcept_no, disclosure_type, report_name):
        return Disclosure.objects.create(
            company=self.company, rcept_no=rcept_no, report_name=report_name,
            disclosure_type=disclosure_type, filed_at=date(2026, 7, 1),
            dart_url=dart_viewer_url(rcept_no),
        )

    def test_marks_target_and_excluded(self):
        call_command('apply_selection')

        self.target.refresh_from_db()
        self.excluded.refresh_from_db()
        self.assertEqual(self.target.selection_state, SelectionState.TARGET)
        self.assertEqual(self.target.exclusion_reason, '')
        self.assertTrue(self.target.is_summary_target)
        self.assertEqual(self.excluded.selection_state, SelectionState.EXCLUDED)
        self.assertEqual(self.excluded.exclusion_reason, ExclusionReason.BLACKLIST)

    def test_rerun_skips_already_decided(self):
        call_command('apply_selection')
        # 수동으로 뒤집어 둔 판정은 --force 없이 재실행해도 유지돼야 한다.
        Disclosure.objects.filter(pk=self.target.pk).update(
            selection_state=SelectionState.EXCLUDED,
            exclusion_reason=ExclusionReason.EXCLUDED_TYPE,
        )

        call_command('apply_selection')

        self.target.refresh_from_db()
        self.assertEqual(self.target.selection_state, SelectionState.EXCLUDED)

    def test_force_reevaluates(self):
        call_command('apply_selection')
        Disclosure.objects.filter(pk=self.target.pk).update(
            selection_state=SelectionState.EXCLUDED,
            exclusion_reason=ExclusionReason.EXCLUDED_TYPE,
        )

        call_command('apply_selection', force=True)

        self.target.refresh_from_db()
        self.assertEqual(self.target.selection_state, SelectionState.TARGET)
        self.assertEqual(self.target.exclusion_reason, '')

    def test_dry_run_does_not_save(self):
        call_command('apply_selection', dry_run=True)

        self.target.refresh_from_db()
        self.assertEqual(self.target.selection_state, SelectionState.PENDING)


# ---------------------------------------------------------------------------
# 원문 전처리 (disclosures/dart.py)
# ---------------------------------------------------------------------------

# 거래소공시 원문을 모사한 HTML. charset을 euc-kr로 잘못 선언하는 실제 특성을 담았다.
FAKE_HTML_DOCUMENT = """<html>
<head><meta http-equiv="Content-Type" content="text/html; charset=euc-kr">
<style>.t { color: red; }</style>
<script>var noise = "버려져야 하는 스크립트";</script>
</head>
<body>
<!-- 주석도 사라져야 한다 -->
<p>단일판매ㆍ공급계약 체결</p>
<table><tr><th>구분</th><th>금액</th></tr>
<tr><td>계약금액</td><td>1,234,567</td></tr>
<tr><td>매출액 대비</td><td>12.34%</td></tr>
<tr><td>&nbsp;</td><td>&nbsp;</td></tr></table>
</body></html>"""

# 정기공시 DART XML을 모사한 최소 문서.
FAKE_DART_XML = """<?xml version="1.0" encoding="utf-8"?>
<DOCUMENT xsi:noNamespaceSchemaLocation="dart4.xsd">
<DOCUMENT-NAME ACODE="11013">분기보고서</DOCUMENT-NAME>
<BODY>
<SECTION-1><TITLE ATOC="Y">I. 회사의 개요</TITLE><P>회사 개요 본문</P></SECTION-1>
<SECTION-1><TITLE ATOC="Y">III. 재무에 관한 사항</TITLE>
  <SECTION-2><TITLE>1. 요약재무정보</TITLE>
    <TABLE><TR><TD>매출액</TD><TE>79,987,654</TE></TR></TABLE>
  </SECTION-2>
  <SECTION-2><TITLE>3. 연결재무제표 주석</TITLE><P>버려져야 하는 주석 본문</P></SECTION-2>
</SECTION-1>
<SECTION-1><TITLE ATOC="Y">VIII. 임원 및 직원 등에 관한 사항</TITLE>
  <P>버려져야 하는 임직원 명부</P></SECTION-1>
</BODY></DOCUMENT>"""


class StripMarkupTest(TestCase):
    def test_removes_script_style_and_comments(self):
        text = strip_markup(FAKE_HTML_DOCUMENT)

        self.assertNotIn('<', text)
        self.assertNotIn('noise', text)
        self.assertNotIn('color: red', text)
        self.assertNotIn('주석도 사라져야', text)

    def test_preserves_table_numbers_separately(self):
        text = strip_markup(FAKE_HTML_DOCUMENT)

        # 표의 수치가 살아남아야 요약의 숫자 대조가 가능하다.
        self.assertIn('1,234,567', text)
        self.assertIn('12.34%', text)
        # 셀이 붙어 `계약금액1,234,567`이 되면 안 된다.
        self.assertIn('계약금액 | 1,234,567', text)

    def test_drops_empty_rows_and_normalizes_whitespace(self):
        text = strip_markup(FAKE_HTML_DOCUMENT)

        self.assertNotIn('|  |', text)
        self.assertNotIn('   ', text)
        for line in text.split('\n'):
            self.assertEqual(line, line.strip())

    def test_is_dart_xml_distinguishes_formats(self):
        self.assertTrue(is_dart_xml(FAKE_DART_XML))
        self.assertFalse(is_dart_xml(FAKE_HTML_DOCUMENT))


class ExtractKeySectionsTest(TestCase):
    def test_keeps_key_sections_only(self):
        extracted = extract_key_sections(FAKE_DART_XML, PERIODIC_KEY_SECTIONS)
        text = strip_markup(extracted)

        self.assertIn('79,987,654', text)          # 요약재무정보의 수치는 남는다
        self.assertNotIn('주석 본문', text)         # 재무제표 주석은 버린다
        self.assertNotIn('임직원 명부', text)       # 임원·직원 섹션은 버린다
        self.assertNotIn('회사 개요 본문', text)    # 회사 개요 상용구도 버린다

    def test_returns_none_when_no_section_matches(self):
        # 형식이 다른 보고서는 None → 호출자가 전체 정제로 되돌아간다.
        self.assertIsNone(extract_key_sections(FAKE_DART_XML, OFFERING_KEY_SECTIONS))

    def test_preprocess_uses_section_map_only_for_large_types(self):
        sectioned = preprocess_document(FAKE_DART_XML, disclosure_type='정기공시')
        whole = preprocess_document(FAKE_DART_XML, disclosure_type='거래소공시')

        self.assertNotIn('임직원 명부', sectioned)
        self.assertIn('임직원 명부', whole)      # 맵이 없는 유형은 원문 전체를 정제
        self.assertLess(len(sectioned), len(whole))

    def test_preprocess_falls_back_when_sections_missing(self):
        # 발행공시 맵으로는 매칭되는 섹션이 없으므로 전체 정제로 되돌아간다(빈 결과 금지).
        text = preprocess_document(FAKE_DART_XML, disclosure_type='발행공시')
        self.assertIn('임직원 명부', text)


# ---------------------------------------------------------------------------
# 원문 확보 명령 (fetch_documents)
# ---------------------------------------------------------------------------

class FetchDocumentsTest(TestCase):
    def setUp(self):
        self.sector = Sector.objects.create(name='반도체', slug='semiconductor')
        self.company = Company.objects.create(
            sector=self.sector, corp_code=TRACKED_CORP,
            stock_code='005930', name='삼성전자',
        )
        self.target = self._make('20260701000001', SelectionState.TARGET)
        self.excluded = self._make('20260701000002', SelectionState.EXCLUDED)
        self.pending = self._make('20260701000003', SelectionState.PENDING)

    def _make(self, rcept_no, state, raw_fetched=False, disclosure_type='거래소공시'):
        return Disclosure.objects.create(
            company=self.company, rcept_no=rcept_no,
            report_name='단일판매ㆍ공급계약체결', disclosure_type=disclosure_type,
            filed_at=date(2026, 7, 1), dart_url=dart_viewer_url(rcept_no),
            selection_state=state, raw_fetched=raw_fetched,
        )

    def _run(self, side_effect=None, **options):
        with patch(
            'disclosures.management.commands.fetch_documents.fetch_document',
            side_effect=side_effect or (lambda rcept_no: FAKE_HTML_DOCUMENT),
        ) as mock_fetch:
            call_command('fetch_documents', **options)
        return mock_fetch

    def test_fetches_only_targets(self):
        mock_fetch = self._run()

        self.assertEqual(
            [c.args[0] for c in mock_fetch.call_args_list], ['20260701000001']
        )
        self.target.refresh_from_db()
        self.assertTrue(self.target.raw_fetched)
        self.assertIn('1,234,567', self.target.raw_content)
        # 제외·미판정 공시는 건드리지 않는다.
        for disclosure in (self.excluded, self.pending):
            disclosure.refresh_from_db()
            self.assertFalse(disclosure.raw_fetched)
            self.assertEqual(disclosure.raw_content, '')

    def test_does_not_refetch_already_fetched(self):
        """이미 확보한 원문은 재호출하지 않는다 (DART 호출량 규칙)."""
        self._run()
        mock_fetch = self._run()

        mock_fetch.assert_not_called()

    def test_refetch_option_forces_recall(self):
        self._run()
        mock_fetch = self._run(refetch=True)

        self.assertEqual(mock_fetch.call_count, 1)

    def test_stores_preprocessed_text_not_raw_markup(self):
        self._run()

        self.target.refresh_from_db()
        self.assertNotIn('<table', self.target.raw_content)
        self.assertNotIn('var noise', self.target.raw_content)
        self.assertIn('계약금액 | 1,234,567', self.target.raw_content)

    def test_limit_caps_processed_count(self):
        second = self._make('20260701000004', SelectionState.TARGET)
        mock_fetch = self._run(limit=1)

        self.assertEqual(mock_fetch.call_count, 1)
        second.refresh_from_db()
        self.assertFalse(second.raw_fetched)

    def test_type_filter(self):
        periodic = self._make(
            '20260701000005', SelectionState.TARGET, disclosure_type='정기공시'
        )
        mock_fetch = self._run(disclosure_type='정기공시')

        self.assertEqual(
            [c.args[0] for c in mock_fetch.call_args_list], [periodic.rcept_no]
        )
        self.target.refresh_from_db()
        self.assertFalse(self.target.raw_fetched)

    def test_failure_on_one_continues_with_next(self):
        """건별 실패로 전체가 죽으면 안 된다 — 실패는 기록하고 다음 건을 계속 처리한다."""
        second = self._make('20260701000004', SelectionState.TARGET)

        def flaky(rcept_no):
            if rcept_no == self.target.rcept_no:
                raise DartApiError('900', '원문을 찾을 수 없습니다.')
            return FAKE_HTML_DOCUMENT

        mock_fetch = self._run(side_effect=flaky)

        self.assertEqual(mock_fetch.call_count, 2)
        self.target.refresh_from_db()
        second.refresh_from_db()
        self.assertFalse(self.target.raw_fetched)   # 실패 건은 미확보로 남아 재시도된다
        self.assertTrue(second.raw_fetched)

    def test_invalid_limit_raises(self):
        with self.assertRaises(CommandError):
            self._run(limit=0)


# ---------------------------------------------------------------------------
# 요약 모듈 ↔ 모델 경계면 (disclosures/summarizer.py ↔ DisclosureSummary)
# ---------------------------------------------------------------------------

class SummarySchemaConsistencyTest(TestCase):
    """LLM 출력 스키마와 모델 필드가 어긋나면 저장이 깨진다. 경계면을 고정한다.

    스키마는 `summarizer.py`, 필드는 `models.py`로 소유자가 갈려 있어 한쪽만 바뀌기 쉽다.
    어긋난 채로 실호출하면 LLM 비용을 쓰고 나서 저장 단계에서 터진다 — 그 전에 잡는다.
    """

    def test_importance_enum_matches_model_choices(self):
        schema_enum = summarizer.SUMMARY_JSON_SCHEMA['properties']['importance']['enum']
        self.assertEqual(
            sorted(schema_enum), sorted(DisclosureSummary.Importance.values)
        )

    def test_one_line_max_length_matches_model(self):
        schema_max = summarizer.SUMMARY_JSON_SCHEMA['properties']['one_line']['maxLength']
        field_max = DisclosureSummary._meta.get_field('one_line').max_length
        self.assertEqual(schema_max, field_max)
        # 파이썬 쪽 하드 검증 상수도 같은 값이어야 이중 제약이 성립한다.
        self.assertEqual(summarizer.ONE_LINE_MAX_CHARS, field_max)

    def test_evidence_field_enum_matches_summary_text_fields(self):
        evidence_fields = (
            summarizer.SUMMARY_JSON_SCHEMA['properties']['evidence']
            ['items']['properties']['field']['enum']
        )
        for name in evidence_fields:
            DisclosureSummary._meta.get_field(name)  # 없으면 FieldDoesNotExist

    def test_schema_required_fields_exist_on_model(self):
        for name in summarizer.SUMMARY_JSON_SCHEMA['required']:
            DisclosureSummary._meta.get_field(name)


class SummaryPersistenceTest(TestCase):
    """근거·검증 경고가 모델에 그대로 실려야 QA가 LLM 재호출 없이 재검증할 수 있다."""

    def setUp(self):
        sector = Sector.objects.create(name='반도체', slug='semiconductor')
        company = Company.objects.create(
            sector=sector, corp_code=TRACKED_CORP,
            stock_code='005930', name='삼성전자',
        )
        self.disclosure = Disclosure.objects.create(
            company=company, rcept_no='20260701000001',
            report_name='단일판매ㆍ공급계약체결', disclosure_type='거래소공시',
            filed_at=date(2026, 7, 1), dart_url=dart_viewer_url('20260701000001'),
        )

    def _summary(self, **kwargs):
        defaults = dict(
            disclosure=self.disclosure, one_line='한 줄', easy_explanation='설명',
            why_important='의미', importance=DisclosureSummary.Importance.MEDIUM,
        )
        defaults.update(kwargs)
        return DisclosureSummary.objects.create(**defaults)

    def test_json_fields_default_to_empty_list(self):
        summary = self._summary()
        summary.refresh_from_db()

        self.assertEqual(summary.evidence, [])
        self.assertEqual(summary.review_warnings, [])

    def test_evidence_round_trips(self):
        evidence = [{
            'field': 'one_line', 'claim': '계약금액 1,234,567원',
            'quote': '계약금액 | 1,234,567', 'quote_found': True,
            'numbers_ok': True, 'missing_numbers': [],
        }]
        self._summary(evidence=evidence)

        stored = DisclosureSummary.objects.get(disclosure=self.disclosure)
        self.assertEqual(stored.evidence, evidence)
        self.assertTrue(stored.evidence[0]['quote_found'])

    def test_needs_review_follows_the_type_gate_and_warnings_not_importance(self):
        """5단계에서 검수 게이트가 AI의 `importance` 에서 **공시 유형**으로 옮겨졌다.

        AI 요약을 믿지 못해 하는 검수인데 대상 선정을 그 AI에게 맡기고 있었고, AI가
        중요도를 낮게 잘못 매기면 가장 위험한 요약이 큐에 아예 안 떴다(review_policy.py).
        그래서 `importance` 는 이제 큐에 **아무 영향이 없다** — 유형과 경고만 본다.
        기대값을 바꾼 것이지 조건을 느슨하게 한 것이 아니므로, 유형 축을 함께 고정한다.
        """
        self.assertEqual(self.disclosure.review_category, '')  # 공급계약은 게이트 밖

        summary = self._summary(importance=DisclosureSummary.Importance.HIGH)
        self.assertFalse(summary.needs_review)         # 중요도만 높아서는 큐에 안 들어온다

        summary.review_warnings = ['evidence[0]: 인용문이 원문에서 발견되지 않음']
        self.assertTrue(summary.needs_review)          # 경고 있음 → 검수 필요

        summary.review_warnings = []
        self.assertFalse(summary.needs_review)

        # 유형 게이트에 걸리면 경고가 없고 중요도가 낮아도 사람이 본다.
        summary.importance = DisclosureSummary.Importance.LOW
        self.disclosure.review_category = ReviewCategory.CAPITAL
        self.assertTrue(summary.needs_review)

        summary.is_reviewed = True
        self.assertFalse(summary.needs_review)         # 이미 검수 완료


def _valid_summary_payload(**overrides):
    """스키마를 만족하는 응답 본문(JSON 문자열)을 만든다."""
    payload = {
        'one_line': '삼성전자가 1,234,567원 규모의 공급계약을 체결했다.',
        'easy_explanation': '회사가 제품을 팔기로 계약했다. 금액은 1,234,567원이다. '
                            '계약 상대와 기간은 원문에 적혀 있다.',
        'why_important': '회사의 매출로 이어지는 계약이다.',
        'importance': 'medium',
        'evidence': [{
            'field': 'one_line',
            'claim': '1,234,567원 규모의 공급계약',
            'quote': '계약금액 | 1,234,567',
        }],
    }
    payload.update(overrides)
    return json.dumps(payload, ensure_ascii=False)


#: _call_openai 대역이 돌려주는 (본문, usage, 모델ID) 중 usage 자리.
FAKE_USAGE = {
    'input_tokens': 1500, 'output_tokens': 200, 'cached_tokens': 1400,
    'cache_write_tokens': 0, 'reasoning_tokens': 0, 'total_tokens': 1700,
}


class SummarizerRetryTest(TestCase):
    """OpenAI 실호출 없이 재시도·거부·길이 초과·검증 실패 경로를 고정한다."""

    RAW_TEXT = '단일판매ㆍ공급계약 체결\n계약금액 | 1,234,567\n매출액 대비 | 12.34%'

    def _summarize(self, side_effect, **kwargs):
        with patch(
            'disclosures.summarizer._call_openai', side_effect=side_effect
        ) as mock_call:
            try:
                result = summarizer.summarize_disclosure(
                    company_name='삼성전자', report_name='단일판매ㆍ공급계약체결',
                    filed_at='2026-07-01', rcept_no='20260701000001',
                    raw_text=self.RAW_TEXT, disclosure_type='거래소공시', **kwargs,
                )
            except summarizer.SummarizerError as exc:
                return mock_call, exc
        return mock_call, result

    def test_succeeds_on_first_attempt(self):
        mock_call, result = self._summarize(
            lambda *a: (_valid_summary_payload(), FAKE_USAGE, 'gpt-5.6-luna')
        )

        self.assertEqual(mock_call.call_count, 1)
        self.assertEqual(result['attempts'], 1)
        self.assertEqual(result['importance'], 'medium')
        self.assertEqual(result['model_name'], 'gpt-5.6-luna')
        # 근거의 인용문이 원문에 있으므로 대조를 통과한다.
        self.assertTrue(result['evidence'][0]['quote_found'])
        self.assertEqual(result['unsupported_numbers'], [])

    def test_retries_after_invalid_json_then_succeeds(self):
        responses = [
            ('{깨진 JSON', FAKE_USAGE, 'gpt-5.6-luna'),
            (_valid_summary_payload(), FAKE_USAGE, 'gpt-5.6-luna'),
        ]
        mock_call, result = self._summarize(lambda *a: responses.pop(0))

        self.assertEqual(mock_call.call_count, 2)
        self.assertEqual(result['attempts'], 2)

    def test_refusal_is_not_retried(self):
        """안전 거부는 같은 입력으로 재시도해도 결과가 같다 — 호출을 낭비하지 않는다."""
        mock_call, error = self._summarize(
            summarizer.SummaryRefusedError('안전상의 이유로 거부됨')
        )

        self.assertIsInstance(error, summarizer.SummaryRefusedError)
        self.assertEqual(mock_call.call_count, 1)

    def test_too_large_input_raises_without_calling(self):
        """상한 초과는 호출 전에 걸러야 비용이 발생하지 않는다."""
        mock_call, error = self._summarize(
            lambda *a: (_valid_summary_payload(), FAKE_USAGE, 'gpt-5.6-luna'),
            max_input_tokens=10,
        )

        self.assertIsInstance(error, summarizer.SummaryTooLargeError)
        mock_call.assert_not_called()

    def test_one_line_over_limit_fails_after_retries(self):
        """one_line이 모델 max_length를 넘으면 저장이 깨지므로 하드 검증으로 막는다."""
        too_long = '가' * (summarizer.ONE_LINE_MAX_CHARS + 1)
        mock_call, error = self._summarize(
            lambda *a: (
                _valid_summary_payload(one_line=too_long), FAKE_USAGE, 'gpt-5.6-luna'
            )
        )

        self.assertIsInstance(error, summarizer.SummaryValidationError)
        # 최초 1회 + 재시도 MAX_RETRIES회를 모두 소진한다.
        self.assertEqual(mock_call.call_count, summarizer.MAX_RETRIES + 1)

    def test_missing_evidence_fails_validation(self):
        mock_call, error = self._summarize(
            lambda *a: (_valid_summary_payload(evidence=[]), FAKE_USAGE, 'gpt-5.6-luna')
        )

        self.assertIsInstance(error, summarizer.SummaryValidationError)
        self.assertIn('evidence', str(error))

    def test_unsupported_number_is_recorded_as_warning_not_failure(self):
        """근거 없는 수치는 실패가 아니라 경고로 남겨 검수로 넘긴다."""
        payload = _valid_summary_payload(
            why_important='이 계약은 회사 연매출의 99.9%에 해당한다.'
        )
        _mock_call, result = self._summarize(
            lambda *a: (payload, FAKE_USAGE, 'gpt-5.6-luna')
        )

        self.assertIn('99.9', ' '.join(result['unsupported_numbers']))
        self.assertTrue(result['warnings'])

    def test_fabricated_quote_is_flagged(self):
        payload = _valid_summary_payload(evidence=[{
            'field': 'one_line', 'claim': '계약금액 1,234,567원',
            'quote': '원문에 존재하지 않는 인용 구절',
        }])
        _mock_call, result = self._summarize(
            lambda *a: (payload, FAKE_USAGE, 'gpt-5.6-luna')
        )

        self.assertFalse(result['evidence'][0]['quote_found'])
        self.assertTrue(result['warnings'])

    def test_model_name_fits_model_field(self):
        long_id = 'gpt-' + 'x' * 100
        _mock_call, result = self._summarize(
            lambda *a: (_valid_summary_payload(), FAKE_USAGE, long_id)
        )

        max_length = DisclosureSummary._meta.get_field('model_name').max_length
        self.assertLessEqual(len(result['model_name']), max_length)


class ReferenceNumberTest(TestCase):
    """연도·항목번호를 대조에서 빼되, 금액·비율은 절대 빼지 않는다.

    실측 배경: 경고를 유발한 수치 상위 3개가 `2026`(78회)·`1`(60회)·`2025`(38회)로
    전부 금액이 아니었다. 이 필터가 오탐의 큰 축을 없앤다.
    """

    def _dropped(self, text):
        kept = {raw for raw, _ in summarizer.extract_comparable_numbers(text)}
        allnum = {raw for raw, _ in summarizer.extract_numbers(text)}
        return allnum - kept

    def test_years_and_ordinals_are_dropped(self):
        dropped = self._dropped('2026년 1분기, 제3자배정, 5월 12일')
        self.assertIn('2026', dropped)
        self.assertIn('1', dropped)
        self.assertIn('3', dropped)

    def test_amounts_are_never_dropped(self):
        # 단위가 붙었으면 값이 작아도 금액이다
        self.assertEqual(self._dropped('3조 9,891억'), set())
        self.assertEqual(self._dropped('12억'), set())
        # 쉼표·소수점이 있으면 자릿수 구분·비율이다
        self.assertEqual(self._dropped('1,234,567원 / 지분율 17.33% / 2.44배'), set())

    def test_large_bare_integer_is_kept(self):
        """ORDINAL_MAX를 넘는 맨 정수는 주식수일 수 있으므로 남긴다."""
        self.assertEqual(self._dropped('17790000'), set())


class VerifyQuoteTest(TestCase):
    """인용문 조각 검증 — 표 행을 이어 붙인 정상 인용은 통과, 지어낸 인용은 차단."""

    RAW = (
        '3. 개최목적 | 2026년 1분기 경영실적 발표\n'
        '4. 개최방법 | 대면미팅, One-on-One미팅\n'
        '5. 개최일시 | 2026-05-06\n'
        '6. 주요 설명회내용(요약) | 2026년 1분기 경영실적 설명\n'
    )

    def _verify(self, quote):
        return summarizer.verify_quote(quote, self.RAW)

    def test_contiguous_quote_passes(self):
        self.assertTrue(self._verify('4. 개최방법 | 대면미팅, One-on-One미팅'))

    def test_stitched_rows_pass(self):
        """떨어진 두 행을 이어 붙인 인용 — 각 조각이 원문에 순서대로 있으면 통과한다.

        모델이 5번 항목을 건너뛰고 4번과 6번을 이어 붙인 형태. 할루시네이션이 아니라
        인용 형식 문제이므로 경고를 만들면 안 된다.
        """
        self.assertTrue(self._verify(
            '4. 개최방법 | 대면미팅, One-on-One미팅\n6. 주요 설명회내용(요약) | 2026년 1분기'
        ))

    def test_table_separator_differences_are_absorbed(self):
        """구분자·공백 유무만 다른 인용은 통과한다(모델이 표 구분자를 자주 생략한다)."""
        self.assertTrue(self._verify('5. 개최일시   2026-05-06'))

    # --- 완화해도 여전히 잡혀야 하는 것들 -------------------------------------

    def test_fabricated_fragment_is_rejected(self):
        """조각 하나라도 원문에 없으면 실패한다 — 할루시네이션 탐지력은 유지된다."""
        self.assertFalse(self._verify(
            '4. 개최방법 | 대면미팅\n7. 참가비 | 무료 300,000원'
        ))

    def test_fabricated_number_inside_real_row_is_rejected(self):
        self.assertFalse(self._verify('5. 개최일시 | 2026-05-09'))

    def test_reversed_order_is_rejected(self):
        """원문 순서를 뒤집은 인용은 인과를 왜곡할 수 있으므로 통과시키지 않는다."""
        self.assertFalse(self._verify(
            '6. 주요 설명회내용(요약) | 2026년 1분기 경영실적 설명\n3. 개최목적'
        ))

    def test_too_short_quote_is_undecidable(self):
        """판정할 내용이 없으면 None — 실패로 몰아 경고를 만들지 않는다."""
        self.assertIsNone(self._verify('| |'))

    def test_undecidable_quote_does_not_warn(self):
        checked, warnings = summarizer.verify_evidence(
            [{'field': 'one_line', 'claim': '', 'quote': '| |'}], self.RAW
        )
        self.assertTrue(checked[0]['quote_found'])
        self.assertEqual(warnings, [])


class ScaleToleranceTest(TestCase):
    """표 머리글 단위(`(단위 : 백만원)`)로 자릿수만 어긋난 환산을 정상으로 인정한다."""

    def test_million_won_table_conversion_is_supported(self):
        # 원문 표: 374,629 (단위: 백만원) → 요약: 3,746억 (반올림 표기까지 인정한다)
        quote_values = [value for _, value in summarizer.extract_numbers('매출액 | 374,629')]
        value = summarizer.extract_numbers('3,746억')[0][1]
        self.assertTrue(summarizer._value_supported(value, quote_values, approximate=False))

    def test_thousand_won_table_conversion_is_supported(self):
        quote_values = [v for _, v in summarizer.extract_numbers('63,956,675')]
        value = summarizer.extract_numbers('639억 5,668만')[0][1]
        self.assertTrue(summarizer._value_supported(value, quote_values, approximate=False))

    def test_unrelated_number_is_still_unsupported(self):
        """자릿수 배수가 아닌 값은 여전히 뒷받침되지 않는다."""
        quote_values = [v for _, v in summarizer.extract_numbers('매출액 | 374,629')]
        value = summarizer.extract_numbers('9,999억')[0][1]
        self.assertFalse(summarizer._value_supported(value, quote_values, approximate=True))


class EvidenceScopeTest(TestCase):
    """claim 수치는 자기 인용문이 아니라 **전체 인용문**과 대조한다."""

    RAW = '계약금액 | 1,234,567\n계약기간 | 2026.01.01 ~ 2026.12.31\n매출액 대비 | 12.34%'

    def test_number_from_sibling_quote_is_not_flagged(self):
        """근거 하나가 주장 여러 개를 걸치는 흔한 형태 — 오탐이면 안 된다."""
        checked, _ = summarizer.verify_evidence([
            {'field': 'one_line', 'claim': '계약금액 1,234,567원 (매출 대비 12.34%)',
             'quote': '계약금액 | 1,234,567'},
            {'field': 'why_important', 'claim': '매출액 대비 12.34%',
             'quote': '매출액 대비 | 12.34%'},
        ], self.RAW)

        self.assertTrue(checked[0]['numbers_ok'])
        self.assertEqual(checked[0]['missing_numbers'], [])

    def test_number_absent_from_all_quotes_is_recorded(self):
        checked, _ = summarizer.verify_evidence([
            {'field': 'one_line', 'claim': '계약금액 9,999,999원',
             'quote': '계약금액 | 1,234,567'},
        ], self.RAW)

        self.assertFalse(checked[0]['numbers_ok'])
        self.assertIn('9,999,999', checked[0]['missing_numbers'])

    def test_number_mismatch_does_not_create_warning(self):
        """항목별 수치 판정은 진단용이다. 검수 게이트는 집계 검사(validate_summary)가 맡는다."""
        _checked, warnings = summarizer.verify_evidence([
            {'field': 'one_line', 'claim': '계약금액 9,999,999원',
             'quote': '계약금액 | 1,234,567'},
        ], self.RAW)

        self.assertEqual(warnings, [])


class ReviewWarningBuilderTest(TestCase):
    """생성·재검증 두 경로가 같은 경고를 만들도록 build_review_warnings가 단일 출처다."""

    def test_unsupported_numbers_and_bad_quotes_are_reported(self):
        warnings = summarizer.build_review_warnings({
            'unsupported_numbers': ['9,999억'],
            'evidence': [
                {'quote': '진짜 인용', 'quote_found': True},
                {'quote': '지어낸 인용', 'quote_found': False},
            ],
            'sentence_count': 4,
        })

        self.assertEqual(len(warnings), 2)
        self.assertIn('9,999억', warnings[0])
        self.assertIn('지어낸 인용', warnings[1])

    def test_clean_summary_has_no_warnings(self):
        self.assertEqual(summarizer.build_review_warnings({
            'unsupported_numbers': [],
            'evidence': [{'quote': '진짜 인용', 'quote_found': True}],
            'sentence_count': 4,
        }), [])

    def test_sentence_count_out_of_range_warns(self):
        warnings = summarizer.build_review_warnings({
            'unsupported_numbers': [], 'evidence': [], 'sentence_count': 9,
        })
        self.assertEqual(len(warnings), 1)
        self.assertIn('9문장', warnings[0])


class ReferenceDocSelectionTest(TestCase):
    """대형 참고문서 제외 — 크다는 이유만으로 빼지 않는다는 점이 핵심이다."""

    def test_representative_company_variant_is_excluded(self):
        """집단 전체 계열사 명부(494,378 토큰) — 입력 상한을 넘겨 요약이 실패한다."""
        state, reason = evaluate(
            '공정위공시', '대규모기업집단현황공시[연1회공시및1/4분기용(대표회사)]'
        )
        self.assertEqual(state, SelectionState.EXCLUDED)
        self.assertEqual(reason, ExclusionReason.REFERENCE_DOC)

    def test_correction_tag_does_not_evade_exclusion(self):
        state, reason = evaluate(
            '공정위공시', '[기재정정]대규모기업집단현황공시[연1회공시및1/4분기용(대표회사)]'
        )
        self.assertEqual(state, SelectionState.EXCLUDED)
        self.assertEqual(reason, ExclusionReason.REFERENCE_DOC)

    def test_individual_company_variants_stay_targets(self):
        """같은 공시명이라도 개별회사·동일인용 서식은 6K~16K 토큰이고 요약에 성공했다.

        접두어만 보고 `대규모기업집단현황공시`를 통째로 빼면 정상 요약 4건이 함께 날아간다.
        """
        for title in (
            '대규모기업집단현황공시[연1회공시및1/4분기용(개별회사)]',
            '대규모기업집단현황공시[연1회(동일인용)]',
        ):
            with self.subTest(title=title):
                state, reason = evaluate('공정위공시', title)
                self.assertEqual(state, SelectionState.TARGET)
                self.assertEqual(reason, '')

    def test_governance_report_stays_a_target(self):
        """기업지배구조보고서공시는 41K~103K 토큰으로 크지만 요약에 성공했고
        지배구조는 투자자 관심사라 의도적으로 대상에 남겼다. 같이 빼지 말 것."""
        state, reason = evaluate('거래소공시', '기업지배구조보고서공시')
        self.assertEqual(state, SelectionState.TARGET)
        self.assertEqual(reason, '')

    def test_other_fair_trade_disclosures_stay_targets(self):
        """유형째 빼면 안 된다 — 같은 공정위공시에도 요약 가치가 있는 공시가 있다."""
        state, _ = evaluate('공정위공시', '대규모내부거래관련공시')
        self.assertEqual(state, SelectionState.TARGET)


class RevalidateSummariesCommandTest(TestCase):
    """재검증은 LLM을 부르지 않고 판정만 갱신한다."""

    RAW = (
        '3. 개최목적 | 2026년 1분기 경영실적 발표\n'
        '4. 개최방법 | 대면미팅, One-on-One미팅\n'
        '5. 개최일시 | 2026-05-06\n'
        '6. 주요 설명회내용(요약) | 계약금액 1,234,567원\n'
    )

    def setUp(self):
        sector = Sector.objects.create(name='반도체', slug='semiconductor')
        company = Company.objects.create(
            sector=sector, corp_code=TRACKED_CORP, stock_code='005930', name='삼성전자',
        )
        self.disclosure = Disclosure.objects.create(
            company=company, rcept_no='20260701000002',
            report_name='기업설명회(IR)개최', disclosure_type='거래소공시',
            filed_at=date(2026, 7, 1), dart_url=dart_viewer_url('20260701000002'),
            raw_content=self.RAW, raw_fetched=True,
        )
        # 옛 규칙에서 오탐 경고가 붙은 채 저장된 요약을 재현한다.
        self.summary = DisclosureSummary.objects.create(
            disclosure=self.disclosure,
            one_line='계약금액 1,234,567원 규모의 설명회를 연다.',
            easy_explanation='회사가 설명회를 연다. 금액은 1,234,567원이다. 방법은 대면미팅이다.',
            why_important='투자자 소통 창구다.',
            importance=DisclosureSummary.Importance.MEDIUM,
            evidence=[{
                'field': 'one_line', 'claim': '계약금액 1,234,567원',
                # 4번과 6번을 이어 붙인 인용 — 옛 규칙에서는 미발견으로 판정됐다
                'quote': '4. 개최방법 | 대면미팅, One-on-One미팅\n'
                         '6. 주요 설명회내용(요약) | 계약금액 1,234,567원',
                'quote_found': False, 'numbers_ok': False,
                'missing_numbers': ['1,234,567'],
            }],
            review_warnings=['원문에서 찾지 못한 인용: 4. 개최방법 | 대면미팅'],
        )

    def test_false_positive_warning_is_cleared(self):
        call_command('revalidate_summaries', verbosity=0)

        self.summary.refresh_from_db()
        self.assertEqual(self.summary.review_warnings, [])
        self.assertTrue(self.summary.evidence[0]['quote_found'])

    def test_dry_run_does_not_save(self):
        call_command('revalidate_summaries', '--dry-run', verbosity=0)

        self.summary.refresh_from_db()
        self.assertEqual(
            self.summary.review_warnings,
            ['원문에서 찾지 못한 인용: 4. 개최방법 | 대면미팅'],
        )

    def test_genuine_hallucination_is_still_flagged(self):
        """재검증이 경고를 지우기만 하는 것은 아니다 — 진짜 문제는 다시 붙는다."""
        self.summary.evidence = [{
            'field': 'one_line', 'claim': '계약금액 1,234,567원',
            'quote': '원문에 존재하지 않는 인용 구절입니다',
            'quote_found': True, 'numbers_ok': True, 'missing_numbers': [],
        }]
        self.summary.review_warnings = []
        self.summary.save(update_fields=['evidence', 'review_warnings'])

        call_command('revalidate_summaries', verbosity=0)

        self.summary.refresh_from_db()
        self.assertTrue(self.summary.review_warnings)
        self.assertFalse(self.summary.evidence[0]['quote_found'])

    def test_missing_raw_content_is_skipped(self):
        """원문이 없으면 대조가 불가능하므로 기존 판정을 건드리지 않는다."""
        self.disclosure.raw_content = ''
        self.disclosure.raw_fetched = False
        self.disclosure.save(update_fields=['raw_content', 'raw_fetched'])

        call_command('revalidate_summaries', verbosity=0)

        self.summary.refresh_from_db()
        self.assertEqual(
            self.summary.review_warnings,
            ['원문에서 찾지 못한 인용: 4. 개최방법 | 대면미팅'],
        )

    def test_does_not_call_openai(self):
        with patch.object(summarizer, '_call_openai') as mock_call:
            call_command('revalidate_summaries', verbosity=0)
        mock_call.assert_not_called()


# ---------------------------------------------------------------------------
# 3단계: 웹 조회 화면
# ---------------------------------------------------------------------------

class WebViewTestBase(TestCase):
    """화면 테스트 공용 픽스처. 요약 있는 공시와 없는 공시를 함께 둔다."""

    def setUp(self):
        self.sector = Sector.objects.create(
            name='반도체', slug='semiconductor', description='메모리·파운드리·장비',
        )
        # 종목코드 선행 0이 URL에서 살아남는지 확인하기 위해 '000660'을 쓴다.
        self.samsung = Company.objects.create(
            sector=self.sector, corp_code='00126380', stock_code='005930',
            name='삼성전자', sub_category='메모리',
        )
        self.hynix = Company.objects.create(
            sector=self.sector, corp_code='00164779', stock_code='000660',
            name='SK하이닉스', sub_category='메모리',
        )
        self.high = self._disclosure(
            self.samsung, '20260701000001', '단일판매ㆍ공급계약체결',
            importance=DisclosureSummary.Importance.HIGH,
        )
        self.medium = self._disclosure(
            self.hynix, '20260702000001', '자기주식취득결정',
            importance=DisclosureSummary.Importance.MEDIUM,
        )
        # 요약이 없는 공시 — 어느 화면에도 나오면 안 된다.
        self.unsummarized = Disclosure.objects.create(
            company=self.samsung, rcept_no='20260703000001',
            report_name='임원ㆍ주요주주특정증권등소유상황보고서',
            disclosure_type='지분공시', filed_at=date(2026, 7, 3),
            dart_url=dart_viewer_url('20260703000001'),
            selection_state=SelectionState.EXCLUDED,
            exclusion_reason=ExclusionReason.BLACKLIST,
        )

    def _disclosure(self, company, rcept_no, report_name, *, importance,
                    is_reviewed=False, evidence=None, filed_at=None):
        disclosure = Disclosure.objects.create(
            company=company, rcept_no=rcept_no, report_name=report_name,
            disclosure_type='거래소공시', filed_at=filed_at or date(2026, 7, 1),
            dart_url=dart_viewer_url(rcept_no),
            selection_state=SelectionState.TARGET,
            raw_fetched=True, raw_content='원문',
        )
        DisclosureSummary.objects.create(
            disclosure=disclosure,
            one_line=f'{report_name} 한 줄 요약',
            easy_explanation='첫 문장이다. 둘째 문장이다. 셋째 문장이다.',
            why_important='중요한 이유다.',
            importance=importance, is_reviewed=is_reviewed,
            model_name='gpt-5.6-luna',
            evidence=evidence if evidence is not None else [
                {'field': 'one_line', 'claim': '계약금액 1,234,567원',
                 'quote': '계약금액 | 1,234,567', 'quote_found': True,
                 'numbers_ok': True, 'missing_numbers': []},
            ],
        )
        return disclosure


class ViewsDoNotCallExternalApisTest(WebViewTestBase):
    """PLAN.md 12.1 — 사용자 요청 경로에서 DART·LLM을 호출하지 않는다.

    이 프로젝트에서 가장 깨지기 쉬운 원칙이라 테스트로 고정한다. "원문이 없으면 그때
    가져오자", "요약이 없으면 즉석에서 만들자"는 코드가 들어가면 DART 호출 수와 LLM 비용이
    트래픽에 비례하게 되어 설계 전체가 무너진다.
    """

    def test_views_module_does_not_import_dart_or_summarizer(self):
        import inspect

        from disclosures import views

        source = inspect.getsource(views)
        for forbidden in ('dart', 'summarizer'):
            with self.subTest(module=forbidden):
                self.assertNotIn(f'from .{forbidden} import', source)
                self.assertNotIn(f'from disclosures.{forbidden} import', source)

    def test_rendering_pages_never_calls_openai_or_dart(self):
        urls = [
            reverse('disclosures:sector_list'),
            reverse('disclosures:sector_detail', args=['semiconductor']),
            reverse('disclosures:company_detail', args=['005930']),
            reverse('disclosures:disclosure_detail', args=['20260701000001']),
        ]
        with patch.object(summarizer, '_call_openai') as mock_llm, \
                patch('disclosures.dart.requests.get') as mock_dart:
            for url in urls:
                self.assertEqual(self.client.get(url).status_code, 200)
        mock_llm.assert_not_called()
        mock_dart.assert_not_called()


class PageRenderingTest(WebViewTestBase):
    """4개 화면이 뜨고, 공통 규칙(면책·원문 링크)이 지켜지는지."""

    def _all_urls(self):
        return (
            reverse('disclosures:sector_list'),
            reverse('disclosures:sector_detail', args=['semiconductor']),
            reverse('disclosures:company_detail', args=['005930']),
            reverse('disclosures:disclosure_detail', args=['20260701000001']),
        )

    def test_all_pages_return_200(self):
        for url in self._all_urls():
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)

    def test_disclaimer_is_on_every_page(self):
        """면책 문구 상시 노출(PLAN.md 1.4·5.3). base.html에 있어 전 페이지에 떠야 한다."""
        for url in self._all_urls():
            with self.subTest(url=url):
                content = self.client.get(url).content.decode()
                self.assertIn('투자 자문이 아니며', content)
                self.assertIn('DART 원문', content)

    def test_dart_link_is_shown_on_card_and_detail(self):
        """요약이 보이는 곳에는 반드시 원문 링크가 함께 있어야 한다."""
        expected = dart_viewer_url('20260701000001')
        for url in (
            reverse('disclosures:sector_detail', args=['semiconductor']),
            reverse('disclosures:disclosure_detail', args=['20260701000001']),
        ):
            with self.subTest(url=url):
                self.assertContains(self.client.get(url), expected)

    def test_detail_shows_summary_sections_in_plan_order(self):
        content = self.client.get(
            reverse('disclosures:disclosure_detail', args=['20260701000001'])
        ).content.decode()
        positions = [
            content.index('한 줄 요약'),
            content.index('쉬운 설명'),
            content.index('왜 중요한가'),
            content.index('원문 근거'),
            content.index('원문 확인'),
        ]
        self.assertEqual(positions, sorted(positions))

    def test_leading_zero_stock_code_resolves(self):
        """'000660'이 660으로 잘리면 SK하이닉스 페이지가 404가 된다."""
        response = self.client.get(reverse('disclosures:company_detail', args=['000660']))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'SK하이닉스')

    def test_unknown_slug_and_company_return_404(self):
        self.assertEqual(
            self.client.get(
                reverse('disclosures:sector_detail', args=['none'])).status_code, 404)
        self.assertEqual(
            self.client.get(
                reverse('disclosures:company_detail', args=['999999'])).status_code, 404)

    def test_sector_list_counts_only_summarized_disclosures(self):
        """섹터 카드의 건수는 화면에 실제로 보이는 수와 같아야 한다."""
        response = self.client.get(reverse('disclosures:sector_list'))
        sector = response.context['sectors'][0]
        self.assertEqual(sector.company_count, 2)
        self.assertEqual(sector.summary_count, 2)   # 미요약 1건은 세지 않는다


class ExposurePolicyTest(WebViewTestBase):
    """요약이 있는 공시만 노출한다 — published_disclosures()가 단일 출처."""

    def test_unsummarized_disclosure_is_hidden_from_lists(self):
        for url in (
            reverse('disclosures:sector_detail', args=['semiconductor']),
            reverse('disclosures:company_detail', args=['005930']),
        ):
            with self.subTest(url=url):
                self.assertNotContains(
                    self.client.get(url), self.unsummarized.report_name)

    def test_unsummarized_disclosure_detail_is_404(self):
        self.assertEqual(
            self.client.get(
                reverse('disclosures:disclosure_detail',
                        args=['20260703000001'])).status_code, 404)

    def test_unreviewed_summary_is_shown_with_badge(self):
        """검수분만 노출하면 화면이 비므로 미검수도 노출하되 배지를 단다."""
        self.assertContains(
            self.client.get(
                reverse('disclosures:disclosure_detail', args=['20260701000001'])),
            '미검수')

    def test_reviewed_summary_has_no_badge(self):
        summary = self.high.summary
        summary.is_reviewed = True
        summary.save(update_fields=['is_reviewed'])

        self.assertNotContains(
            self.client.get(
                reverse('disclosures:disclosure_detail', args=['20260701000001'])),
            '미검수')

    def test_accuracy_warning_shows_banner_with_the_numbers(self):
        """수치 오류를 잡고도 화면에 알리지 않으면 사용자가 틀린 값을 그대로 믿는다.

        실제로 SK하이닉스 유상증자 요약이 39조 8,905억을 3조 9,891억으로 10배 잘못 적었고,
        자동 검증은 이를 잡았지만 화면에는 '미검수' 배지만 떠 있었다.
        """
        summary = self.high.summary
        summary.review_warnings = [
            summarizer.UNSUPPORTED_NUMBER_PREFIX + '3조 9,891억, 4,000억'
        ]
        summary.save(update_fields=['review_warnings'])

        response = self.client.get(
            reverse('disclosures:disclosure_detail', args=['20260701000001']))
        self.assertContains(response, '원문과 대조되지 않았습니다')
        self.assertContains(response, '3조 9,891억')
        self.assertContains(response, '4,000억')

    def test_accuracy_warning_shows_badge_in_lists(self):
        """목록에서도 보여야 훑어보는 사용자가 놓치지 않는다."""
        summary = self.high.summary
        summary.review_warnings = [summarizer.UNSUPPORTED_NUMBER_PREFIX + '3조 9,891억']
        summary.save(update_fields=['review_warnings'])

        self.assertContains(
            self.client.get(reverse('disclosures:sector_detail', args=['semiconductor'])),
            '수치 확인 필요')

    def test_style_only_warning_does_not_trigger_banner(self):
        """문장 수는 문체 문제다. 이것까지 배너를 띄우면 경고가 흔해져 무시당한다."""
        summary = self.high.summary
        summary.review_warnings = [
            summarizer.SENTENCE_COUNT_PREFIX + ': 쉬운 설명이 7문장 (권장 3~5문장)'
        ]
        summary.save(update_fields=['review_warnings'])

        response = self.client.get(
            reverse('disclosures:disclosure_detail', args=['20260701000001']))
        self.assertNotContains(response, '원문과 대조되지 않았습니다')
        self.assertNotContains(response, '수치 확인 필요')

    def test_reviewed_summary_has_no_accuracy_banner(self):
        """사람이 확인했으면 배너를 걷는다."""
        summary = self.high.summary
        summary.review_warnings = [summarizer.UNSUPPORTED_NUMBER_PREFIX + '3조 9,891억']
        summary.is_reviewed = True
        summary.save(update_fields=['review_warnings', 'is_reviewed'])

        self.assertNotContains(
            self.client.get(
                reverse('disclosures:disclosure_detail', args=['20260701000001'])),
            '원문과 대조되지 않았습니다')

    def test_unverified_quote_warning_uses_generic_wording(self):
        """수치 목록이 없으면 숫자를 나열하지 않고 일반 문구로 안내한다."""
        summary = self.high.summary
        summary.review_warnings = [
            f'{summarizer.UNVERIFIED_QUOTE_PREFIX} (근거 1번): 어떤 구절'
        ]
        summary.save(update_fields=['review_warnings'])

        response = self.client.get(
            reverse('disclosures:disclosure_detail', args=['20260701000001']))
        self.assertContains(response, '원문과 대조되지 않았습니다')
        self.assertContains(response, '일부를 원문에서 확인하지 못했습니다')

    def test_unverified_quote_is_not_rendered_as_evidence(self):
        """원문에서 확인되지 않은 인용을 근거로 보여주면 신뢰를 떨어뜨린다."""
        summary = self.high.summary
        summary.evidence = [
            {'field': 'one_line', 'claim': '검증된 주장',
             'quote': '원문에 있는 구절', 'quote_found': True},
            {'field': 'one_line', 'claim': '미검증 주장',
             'quote': '원문에서 찾지 못한 구절', 'quote_found': False},
        ]
        summary.save(update_fields=['evidence'])

        response = self.client.get(
            reverse('disclosures:disclosure_detail', args=['20260701000001']))
        self.assertContains(response, '원문에 있는 구절')
        self.assertNotContains(response, '원문에서 찾지 못한 구절')


class FilterAndPaginationTest(WebViewTestBase):
    """필터·페이지네이션 동작과 두 기능의 상호작용."""

    def test_importance_filter_narrows_results(self):
        response = self.client.get(
            reverse('disclosures:sector_detail', args=['semiconductor']),
            {'importance': 'high'})
        self.assertEqual(response.context['total_count'], 1)
        self.assertContains(response, '단일판매ㆍ공급계약체결')
        self.assertNotContains(response, '자기주식취득결정')

    def test_company_filter_narrows_results(self):
        response = self.client.get(
            reverse('disclosures:sector_detail', args=['semiconductor']),
            {'company': '000660'})
        self.assertEqual(response.context['total_count'], 1)
        self.assertContains(response, '자기주식취득결정')

    def test_invalid_importance_is_ignored_not_500(self):
        """잘못된 쿼리스트링으로 서버 오류가 나면 안 된다."""
        response = self.client.get(
            reverse('disclosures:sector_detail', args=['semiconductor']),
            {'importance': '../etc/passwd'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['total_count'], 2)

    def test_pagination_splits_pages(self):
        for i in range(views.PAGE_SIZE):
            self._disclosure(
                self.samsung, f'2026080100{i:04d}', f'추가공시{i}',
                importance=DisclosureSummary.Importance.LOW,
                filed_at=date(2026, 8, 1))

        url = reverse('disclosures:sector_detail', args=['semiconductor'])
        first = self.client.get(url)
        self.assertEqual(first.context['page_obj'].paginator.num_pages, 2)
        self.assertEqual(len(first.context['page_obj']), views.PAGE_SIZE)

        second = self.client.get(url, {'page': 2})
        self.assertEqual(len(second.context['page_obj']), 2)

    def test_filter_survives_pagination_links(self):
        """2페이지로 넘어갈 때 필터가 풀리면 사용자가 다른 목록을 보게 된다."""
        # 필터를 적용한 뒤에도 2페이지가 나오려면 PAGE_SIZE를 '넘겨야' 한다.
        for i in range(views.PAGE_SIZE + 1):
            self._disclosure(
                self.samsung, f'2026080200{i:04d}', f'낮은공시{i}',
                importance=DisclosureSummary.Importance.LOW,
                filed_at=date(2026, 8, 2))

        response = self.client.get(
            reverse('disclosures:sector_detail', args=['semiconductor']),
            {'importance': 'low'})
        self.assertContains(response, 'importance=low')
        self.assertContains(response, 'page=2')


class QueryEfficiencyTest(WebViewTestBase):
    """N+1 방지 — 목록 쿼리 수가 공시 건수에 비례해 늘면 안 된다."""

    def _query_count(self, url):
        from django.db import connection, reset_queries
        from django.test.utils import CaptureQueriesContext

        with CaptureQueriesContext(connection) as ctx:
            self.client.get(url)
        return len(ctx)

    def test_list_query_count_does_not_grow_with_rows(self):
        url = reverse('disclosures:sector_detail', args=['semiconductor'])
        baseline = self._query_count(url)

        for i in range(10):
            self._disclosure(
                self.samsung, f'2026080300{i:04d}', f'추가공시{i}',
                importance=DisclosureSummary.Importance.MEDIUM,
                filed_at=date(2026, 8, 3))

        self.assertEqual(self._query_count(url), baseline)

    def test_detail_query_count_is_bounded(self):
        url = reverse('disclosures:disclosure_detail', args=['20260701000001'])
        self.assertLessEqual(self._query_count(url), 3)


class AccuracyWarningClassificationTest(TestCase):
    """정확성 경고와 문체 경고의 구분 — 배너 조건의 근간."""

    def setUp(self):
        sector = Sector.objects.create(name='반도체', slug='semiconductor')
        company = Company.objects.create(
            sector=sector, corp_code=TRACKED_CORP, stock_code='005930', name='삼성전자')
        disclosure = Disclosure.objects.create(
            company=company, rcept_no='20260801000001', report_name='공시',
            filed_at=date(2026, 8, 1), dart_url=dart_viewer_url('20260801000001'))
        self.summary = DisclosureSummary.objects.create(
            disclosure=disclosure, one_line='요약', easy_explanation='설명',
            why_important='이유', importance=DisclosureSummary.Importance.MEDIUM)

    def _set(self, warnings, is_reviewed=False):
        self.summary.review_warnings = warnings
        self.summary.is_reviewed = is_reviewed
        return self.summary

    def test_unsupported_numbers_are_parsed_out(self):
        summary = self._set([summarizer.UNSUPPORTED_NUMBER_PREFIX + '3조 9,891억, 4,000억'])
        self.assertEqual(summary.unsupported_numbers, ['3조 9,891억', '4,000억'])

    def test_sentence_count_warning_is_not_an_accuracy_warning(self):
        summary = self._set([summarizer.SENTENCE_COUNT_PREFIX + ': 쉬운 설명이 9문장'])
        self.assertEqual(summary.accuracy_warnings, [])
        self.assertEqual(summary.unsupported_numbers, [])

    def test_unverified_quote_is_an_accuracy_warning(self):
        summary = self._set([f'{summarizer.UNVERIFIED_QUOTE_PREFIX} (근거 2번): 구절'])
        self.assertEqual(len(summary.accuracy_warnings), 1)
        self.assertEqual(summary.unsupported_numbers, [])   # 나열할 수치는 없다

    def test_reviewed_summary_reports_no_accuracy_warnings(self):
        summary = self._set(
            [summarizer.UNSUPPORTED_NUMBER_PREFIX + '3조'], is_reviewed=True)
        self.assertEqual(summary.accuracy_warnings, [])

    def test_build_review_warnings_uses_declared_prefixes(self):
        """접두어 상수와 실제 생성 문구가 어긋나면 배너 조건이 조용히 깨진다."""
        warnings = summarizer.build_review_warnings({
            'unsupported_numbers': ['3조 9,891억'],
            'evidence': [{'quote': '지어낸 인용', 'quote_found': False}],
            'sentence_count': 9,
        })
        self.assertEqual(len(warnings), 3)
        self.assertTrue(warnings[0].startswith(summarizer.UNSUPPORTED_NUMBER_PREFIX))
        self.assertTrue(warnings[1].startswith(summarizer.UNVERIFIED_QUOTE_PREFIX))
        self.assertTrue(warnings[2].startswith(summarizer.SENTENCE_COUNT_PREFIX))


# ---------------------------------------------------------------------------
# 4단계: 사람 검수 플로우 (숨김 · 검수 이력 · LLM 원본 보존 · 검수 화면)
# ---------------------------------------------------------------------------

class ReviewWorkflowTestBase(TestCase):
    """검수 플로우 공용 픽스처 — 검수자 계정 1명과 공시/요약 생성 헬퍼."""

    def setUp(self):
        self.sector = Sector.objects.create(name='반도체', slug='semiconductor')
        self.company = Company.objects.create(
            sector=self.sector, corp_code=TRACKED_CORP,
            stock_code='005930', name='삼성전자',
        )
        self.reviewer = get_user_model().objects.create_superuser(
            username='reviewer', email='reviewer@example.com', password='pw',
        )
        self.other_reviewer = get_user_model().objects.create_superuser(
            username='reviewer2', email='reviewer2@example.com', password='pw',
        )
        self.client.force_login(self.reviewer)

    def make_summary(self, seq, *, company=None, report_name=None,
                     filed_at=None, raw_content='공시 원문 · 계약금액 | 1,234,567',
                     review_category='', **summary_kwargs):
        """공시 1건 + 요약 1건을 만든다. seq가 접수번호·제목의 구분자다.

        `review_category` 는 5단계에서 생긴 검수 게이트 판정 결과다(`review_policy.py`).
        실제로는 `apply_selection` 이 제목을 보고 채우지만, 여기서는 축을 직접 지정한다 —
        제목 문자열을 바꿔 가며 게이트를 만들면 테스트가 정책 정규식의 사소한 변경에
        끌려다니게 된다. 정규식 자체는 `ReviewPolicyGateTest` 가 따로 고정한다.
        """
        rcept_no = f'2026070100{seq:04d}'
        disclosure = Disclosure.objects.create(
            company=company or self.company, rcept_no=rcept_no,
            report_name=report_name or f'검수대상공시{seq}',
            disclosure_type='거래소공시', filed_at=filed_at or date(2026, 7, 1),
            dart_url=dart_viewer_url(rcept_no),
            selection_state=SelectionState.TARGET,
            review_category=review_category,
            raw_fetched=bool(raw_content), raw_content=raw_content,
        )
        defaults = dict(
            disclosure=disclosure,
            one_line=f'{disclosure.report_name} 한 줄 요약',
            easy_explanation='첫 문장이다. 둘째 문장이다. 셋째 문장이다.',
            why_important='중요한 이유다.',
            importance=DisclosureSummary.Importance.MEDIUM,
            model_name='gpt-5.6-luna',
        )
        defaults.update(summary_kwargs)
        return DisclosureSummary.objects.create(**defaults)

    # --- admin 조작 헬퍼 -----------------------------------------------------

    def change_url(self, summary):
        return reverse('admin:disclosures_disclosuresummary_change', args=[summary.pk])

    def post_change(self, summary, **overrides):
        """admin 변경 화면에 실제로 POST한다(save_model을 우회하지 않는 유일한 경로).

        값이 None인 override는 폼에서 뺀다 — 체크박스 해제를 표현하기 위한 것이다.
        """
        data = {
            'one_line': summary.one_line,
            'easy_explanation': summary.easy_explanation,
            'why_important': summary.why_important,
            'importance': summary.importance,
            'hidden_reason': summary.hidden_reason,
            '_continue': '저장하고 계속 편집',
        }
        if summary.is_reviewed:
            data['is_reviewed'] = 'on'
        if summary.is_published:
            data['is_published'] = 'on'
        data.update(overrides)
        response = self.client.post(
            self.change_url(summary),
            {key: value for key, value in data.items() if value is not None},
        )
        summary.refresh_from_db()
        return response

    def run_action(self, action, summaries, **extra):
        """admin 목록 화면의 일괄 액션을 실제 POST로 실행한다."""
        data = {
            'action': action,
            '_selected_action': [str(summary.pk) for summary in summaries],
        }
        data.update(extra)
        response = self.client.post(
            reverse('admin:disclosures_disclosuresummary_changelist'), data, follow=True,
        )
        for summary in summaries:
            summary.refresh_from_db()
        return response


class HiddenSummaryExposureTest(ReviewWorkflowTestBase):
    """숨긴 요약은 웹 어디에도 남으면 안 된다.

    00_input.md 2장 3번의 확정 판단 — 빈 껍데기 카드를 남기면 사용자에게 "뭔가 있었는데
    가려졌다"는 잘못된 신호가 된다. 노출 경로가 4개(섹터 피드·기업 타임라인·메인
    하이라이트·상세)라 한 곳만 막아도 나머지로 샌다. 네 경로를 각각 고정한다.
    """

    def setUp(self):
        super().setUp()
        self.visible = self.make_summary(
            1, report_name='노출되는공시', importance=DisclosureSummary.Importance.HIGH)
        self.hidden = self.make_summary(
            2, report_name='숨겨진공시', importance=DisclosureSummary.Importance.HIGH,
            is_published=False, hidden_reason='금액을 10배 잘못 적음',
        )

    def _feed_urls(self):
        return (
            reverse('disclosures:sector_list'),
            reverse('disclosures:sector_detail', args=['semiconductor']),
            reverse('disclosures:company_detail', args=['005930']),
        )

    def test_hidden_summary_is_absent_from_every_list(self):
        for url in self._feed_urls():
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertNotContains(response, '숨겨진공시')
                self.assertNotContains(response, self.hidden.disclosure.rcept_no)

    def test_hidden_summary_is_absent_from_main_highlights(self):
        """하이라이트는 중요도 '높음'만 뽑으므로 숨긴 고중요도 공시가 새기 가장 쉽다."""
        response = self.client.get(reverse('disclosures:sector_list'))
        names = [item.report_name for item in response.context['highlights']]
        self.assertIn('노출되는공시', names)
        self.assertNotIn('숨겨진공시', names)

    def test_hidden_summary_detail_returns_404(self):
        response = self.client.get(
            reverse('disclosures:disclosure_detail', args=[self.hidden.disclosure.rcept_no]))
        self.assertEqual(response.status_code, 404)

    def test_hidden_reason_never_reaches_the_web(self):
        """숨김 사유는 검수자 내부 메모다. 웹에 새면 내리기로 한 내용을 도로 노출한다."""
        for url in self._feed_urls():
            with self.subTest(url=url):
                self.assertNotContains(self.client.get(url), '금액을 10배 잘못 적음')

    def test_restoring_brings_the_summary_back(self):
        """숨김은 삭제가 아니다 — 복구하면 같은 URL이 다시 살아나야 한다."""
        self.run_action('restore_summaries', [self.hidden])

        self.assertTrue(self.hidden.is_published)
        self.assertEqual(
            self.client.get(reverse('disclosures:disclosure_detail',
                                    args=[self.hidden.disclosure.rcept_no])).status_code,
            200,
        )
        self.assertContains(
            self.client.get(reverse('disclosures:sector_detail', args=['semiconductor'])),
            '숨겨진공시')

    def test_published_disclosures_is_the_only_gate(self):
        """노출 정책의 단일 출처(00_input.md 3.3) — 큐리셋 자체가 숨김을 걸러야 한다."""
        self.assertNotIn(
            self.hidden.disclosure, list(views.published_disclosures()))
        self.assertIn(self.visible.disclosure, list(views.published_disclosures()))


class SectorCardCountConsistencyTest(ReviewWorkflowTestBase):
    """섹터 카드의 건수 ↔ 실제 목록 건수.

    backend가 스스로 지목한 구조적 중복이다. `sector_list()`의 summary_count는
    `published_disclosures()`와 별개로 조건을 한 번 더 적었기 때문에, 한쪽만 고치면
    "카드에 12건인데 들어가면 11건"인 불일치가 조용히 생긴다. 두 수를 같은 테스트에서
    비교해 못 박는다.
    """

    def _counts(self):
        card = self.client.get(reverse('disclosures:sector_list')) \
            .context['sectors'][0].summary_count
        feed = self.client.get(reverse('disclosures:sector_detail', args=['semiconductor']))
        return card, feed.context['total_count'], len(feed.context['page_obj'])

    def test_counts_agree_when_nothing_is_hidden(self):
        for seq in range(1, 4):
            self.make_summary(seq)

        card, total, rendered = self._counts()
        self.assertEqual((card, total, rendered), (3, 3, 3))

    def test_hidden_summary_drops_out_of_the_card_count_too(self):
        for seq in range(1, 4):
            self.make_summary(seq)
        hidden = self.make_summary(4, report_name='숨길공시')

        self.run_action('hide_summaries', [hidden])

        card, total, rendered = self._counts()
        self.assertEqual((card, total, rendered), (3, 3, 3))

    def test_unsummarized_disclosure_is_counted_by_neither(self):
        self.make_summary(1)
        Disclosure.objects.create(
            company=self.company, rcept_no='20260701009999', report_name='요약없는공시',
            disclosure_type='지분공시', filed_at=date(2026, 7, 1),
            dart_url=dart_viewer_url('20260701009999'),
        )

        card, total, rendered = self._counts()
        self.assertEqual((card, total, rendered), (1, 1, 1))


class HumanEditedBadgeTest(ReviewWorkflowTestBase):
    """'사람이 검토·수정함' 표시와 '미검수' 배지·정확성 배너의 전환.

    검수의 목적이 배너를 걷는 것이므로, 검수 완료가 화면에 반영되지 않으면 4단계 전체가
    무의미해진다. 카드와 상세 두 곳을 모두 확인한다(한 곳만 고쳐지는 일이 잦다).
    """

    def setUp(self):
        super().setUp()
        self.summary = self.make_summary(
            1, importance=DisclosureSummary.Importance.HIGH,
            review_warnings=[summarizer.UNSUPPORTED_NUMBER_PREFIX + '3조 9,891억'],
        )
        self.detail_url = reverse(
            'disclosures:disclosure_detail', args=[self.summary.disclosure.rcept_no])
        self.feed_url = reverse('disclosures:sector_detail', args=['semiconductor'])

    def test_unreviewed_summary_shows_badge_and_banner(self):
        for url in (self.detail_url, self.feed_url):
            with self.subTest(url=url):
                self.assertContains(self.client.get(url), '미검수')
        self.assertContains(self.client.get(self.detail_url), '원문과 대조되지 않았습니다')
        self.assertContains(self.client.get(self.feed_url), '수치 확인 필요')

    def test_review_removes_badge_and_banner_from_both_screens(self):
        self.run_action('mark_reviewed', [self.summary])

        for url in (self.detail_url, self.feed_url):
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertNotContains(response, '미검수')
                self.assertNotContains(response, '수치 확인 필요')
        self.assertNotContains(
            self.client.get(self.detail_url), '원문과 대조되지 않았습니다')

    def test_disclaimer_survives_review(self):
        """배너가 걷혀도 면책 문구는 남아야 한다(PLAN.md 1.4 · 상시 노출)."""
        self.run_action('mark_reviewed', [self.summary])

        self.assertContains(self.client.get(self.detail_url), '투자 자문이 아니며')

    def test_human_badge_needs_both_edit_and_review(self):
        """수정만 하고 검수 전이면 표시하지 않는다 — 고치다 만 상태를 '검토함'으로
        내보내면 실제보다 강한 신뢰 신호가 된다(00_input.md 3.2)."""
        DisclosureSummary.objects.filter(pk=self.summary.pk).update(edited_by_human=True)

        for url in (self.detail_url, self.feed_url):
            with self.subTest(url=url):
                self.assertNotContains(self.client.get(url), '사람이 검토·수정함')

        DisclosureSummary.objects.filter(pk=self.summary.pk).update(is_reviewed=True)
        for url in (self.detail_url, self.feed_url):
            with self.subTest(url=url):
                self.assertContains(self.client.get(url), '사람이 검토·수정함')

    def test_human_badge_and_unreviewed_badge_never_coexist(self):
        """was_human_edited가 is_reviewed를 포함하므로 상호배타여야 한다."""
        DisclosureSummary.objects.filter(pk=self.summary.pk).update(
            edited_by_human=True, is_reviewed=True)

        body = self.client.get(self.detail_url).content.decode()
        self.assertIn('사람이 검토·수정함', body)
        self.assertNotIn('badge-unreviewed', body)

    def test_review_alone_does_not_claim_human_editing(self):
        """검수만 하고 본문을 안 고쳤으면 '수정함'이라고 말하면 안 된다."""
        self.run_action('mark_reviewed', [self.summary])

        self.assertNotContains(self.client.get(self.detail_url), '사람이 검토·수정함')


class AdminHumanEditTrackingTest(ReviewWorkflowTestBase):
    """`save_model`의 사람 수정 감지와 `llm_original` 1회 기록.

    llm_original은 프롬프트 개선의 유일한 근거다(LLM이 원래 뭐라고 했는지). 두 번째
    수정에서 덮어쓰면 사람이 고친 문장을 'LLM 원본'으로 오인하게 되어 값이 무의미해진다.
    """

    def setUp(self):
        super().setUp()
        self.summary = self.make_summary(1)
        self.llm_text = self.summary.body_snapshot()

    def test_first_body_edit_snapshots_the_llm_output(self):
        self.post_change(self.summary, one_line='사람이 고친 한 줄')

        self.assertTrue(self.summary.edited_by_human)
        self.assertEqual(self.summary.llm_original, self.llm_text)
        self.assertEqual(self.summary.one_line, '사람이 고친 한 줄')

    def test_second_edit_keeps_the_first_snapshot(self):
        self.post_change(self.summary, one_line='1차 수정')
        self.post_change(self.summary, one_line='2차 수정', why_important='이유도 수정')

        self.assertEqual(self.summary.llm_original, self.llm_text)
        self.assertNotIn('1차 수정', self.summary.llm_original.values())

    def test_stale_audit_flags_do_not_resnapshot_human_text(self):
        """가드가 `previous`(DB 값) 기준이어야 하는 이유를 재현한다.

        저장하려는 객체의 `edited_by_human`·`llm_original`이 어떤 경로로든 초기화된 채
        들어와도, 이미 사람 손이 닿은 본문을 'LLM 원본'으로 다시 스냅샷하면 안 된다.
        `obj` 기준으로 판정하면 여기서 1차 수정본이 LLM 원본으로 둔갑한다.
        """
        self.post_change(self.summary, one_line='1차 수정')

        model_admin = DisclosureSummaryAdmin(DisclosureSummary, AdminSite())
        request = RequestFactory().post('/')
        request.user = self.reviewer
        incoming = DisclosureSummary.objects.get(pk=self.summary.pk)
        incoming.one_line = '2차 수정'
        incoming.edited_by_human = False       # 감사 플래그가 초기화된 채 들어온 상황
        model_admin.save_model(request, incoming, None, True)

        self.summary.refresh_from_db()
        self.assertNotEqual(self.summary.llm_original.get('one_line'), '1차 수정')
        self.assertTrue(self.summary.edited_by_human)

    def test_readonly_audit_fields_ignore_posted_values(self):
        """감사 기록을 폼으로 덮어쓸 수 있으면 기록이 아니다."""
        self.post_change(self.summary, one_line='1차 수정')

        self.post_change(
            self.summary, one_line='2차 수정',
            llm_original='{}', edited_by_human='', reviewed_by='', reviewed_at='',
        )

        self.assertEqual(self.summary.llm_original, self.llm_text)
        self.assertTrue(self.summary.edited_by_human)

    def test_non_body_change_is_not_a_human_edit(self):
        """중요도만 바꾼 저장을 '사람이 본문을 고쳤다'로 세면 표시가 거짓이 된다."""
        self.post_change(self.summary, importance=DisclosureSummary.Importance.HIGH)

        self.assertFalse(self.summary.edited_by_human)
        self.assertEqual(self.summary.llm_original, {})
        self.assertEqual(self.summary.importance, DisclosureSummary.Importance.HIGH)

    def test_saving_unchanged_body_is_not_a_human_edit(self):
        self.post_change(self.summary)

        self.assertFalse(self.summary.edited_by_human)
        self.assertEqual(self.summary.llm_original, {})

    def test_all_three_body_fields_are_watched(self):
        """BODY_FIELDS 중 하나라도 감지에서 빠지면 그 필드는 몰래 고칠 수 있게 된다."""
        for index, field in enumerate(DisclosureSummary.BODY_FIELDS, start=10):
            with self.subTest(field=field):
                summary = self.make_summary(index)
                before = summary.body_snapshot()
                self.post_change(summary, **{field: f'{field} 수정본'})
                self.assertTrue(summary.edited_by_human)
                self.assertEqual(summary.llm_original, before)

    def test_marking_reviewed_in_the_form_records_the_reviewer(self):
        self.post_change(self.summary, is_reviewed='on')

        self.assertTrue(self.summary.is_reviewed)
        self.assertEqual(self.summary.reviewed_by, self.reviewer)
        self.assertIsNotNone(self.summary.reviewed_at)

    def test_unmarking_reviewed_clears_the_record(self):
        """검수를 되돌렸는데 검수자가 남아 있으면 '누가 검수했다'는 거짓 이력이 된다."""
        self.post_change(self.summary, is_reviewed='on')
        self.post_change(self.summary, is_reviewed=None)

        self.assertFalse(self.summary.is_reviewed)
        self.assertIsNone(self.summary.reviewed_by)
        self.assertIsNone(self.summary.reviewed_at)

    def test_editing_a_reviewed_summary_keeps_the_original_reviewer(self):
        """검수 상태가 그대로면 검수 시각을 다시 찍지 않는다(재검수와 구분)."""
        self.post_change(self.summary, is_reviewed='on')
        first_reviewed_at = self.summary.reviewed_at

        self.post_change(self.summary, one_line='오탈자 수정')

        self.assertEqual(self.summary.reviewed_at, first_reviewed_at)
        self.assertEqual(self.summary.reviewed_by, self.reviewer)


class AdminBulkActionTest(ReviewWorkflowTestBase):
    """일괄 액션 3종이 남기는 상태와 이력."""

    def setUp(self):
        super().setUp()
        self.first = self.make_summary(
            1, importance=DisclosureSummary.Importance.HIGH,
            review_warnings=[summarizer.UNSUPPORTED_NUMBER_PREFIX + '3조 9,891억'])
        self.second = self.make_summary(2)

    def test_mark_reviewed_stamps_requesting_user_and_time(self):
        self.run_action('mark_reviewed', [self.first, self.second])

        for summary in (self.first, self.second):
            with self.subTest(pk=summary.pk):
                self.assertTrue(summary.is_reviewed)
                self.assertEqual(summary.reviewed_by, self.reviewer)
                self.assertIsNotNone(summary.reviewed_at)
                self.assertFalse(summary.needs_review)

    def test_re_reviewing_records_the_latest_reviewer(self):
        """다시 도장을 찍는 것은 '지금 이 사람이 다시 확인했다'는 뜻이다."""
        self.run_action('mark_reviewed', [self.first])
        first_at = self.first.reviewed_at

        self.client.force_login(self.other_reviewer)
        self.run_action('mark_reviewed', [self.first])

        self.assertEqual(self.first.reviewed_by, self.other_reviewer)
        self.assertGreaterEqual(self.first.reviewed_at, first_at)

    def test_mark_reviewed_does_not_claim_human_editing(self):
        """일괄 검수는 본문을 고친 것이 아니므로 edited_by_human은 그대로여야 한다."""
        self.run_action('mark_reviewed', [self.first])

        self.assertFalse(self.first.edited_by_human)
        self.assertFalse(self.first.was_human_edited)

    def test_hide_fills_only_a_blank_reason(self):
        DisclosureSummary.objects.filter(pk=self.second.pk).update(
            hidden_reason='기존에 적어둔 사유')

        self.run_action('hide_summaries', [self.first, self.second])

        self.assertFalse(self.first.is_published)
        self.assertEqual(self.first.hidden_reason, DEFAULT_HIDDEN_REASON)
        self.second.refresh_from_db()
        self.assertFalse(self.second.is_published)
        self.assertEqual(self.second.hidden_reason, '기존에 적어둔 사유')

    def test_restore_clears_the_reason(self):
        """노출 중인 요약에 숨김 사유가 남아 있으면 다음 검수자가 상태를 오해한다."""
        self.run_action('hide_summaries', [self.first])
        self.run_action('restore_summaries', [self.first])

        self.assertTrue(self.first.is_published)
        self.assertEqual(self.first.hidden_reason, '')

    def test_hide_and_restore_do_not_touch_the_review_record(self):
        """노출 스위치와 검수 이력은 별개다 — 숨겼다고 검수 사실이 사라지면 안 된다."""
        self.run_action('mark_reviewed', [self.first])
        reviewed_at = self.first.reviewed_at

        self.run_action('hide_summaries', [self.first])
        self.run_action('restore_summaries', [self.first])

        self.assertTrue(self.first.is_reviewed)
        self.assertEqual(self.first.reviewed_by, self.reviewer)
        self.assertEqual(self.first.reviewed_at, reviewed_at)

    def test_actions_work_with_select_across(self):
        """'모두 선택'은 정렬식이 걸린 전체 큐리셋에 update를 건다 — 정렬 구현이
        `Case(...)` 식이라 여기서 깨질 수 있어 별도로 고정한다."""
        response = self.run_action(
            'hide_summaries', [self.first], select_across='1', index='0')

        self.assertEqual(response.status_code, 200)
        self.second.refresh_from_db()
        self.assertFalse(self.first.is_published)
        self.assertFalse(self.second.is_published)

    def test_hidden_by_action_disappears_from_the_web(self):
        """액션 → 화면까지 이어지는 경로를 한 번은 끝까지 확인한다."""
        self.run_action('hide_summaries', [self.first])

        self.assertEqual(
            self.client.get(reverse('disclosures:disclosure_detail',
                                    args=[self.first.disclosure.rcept_no])).status_code,
            404,
        )


class NeedsReviewFilterParityTest(ReviewWorkflowTestBase):
    """admin `NeedsReviewFilter`의 SQL 조건 ↔ 모델 `needs_review` property.

    property는 list_filter에 못 올려서 조건을 SQL로 한 번 더 적었다(backend 산출물 7.3).
    한쪽만 고치면 검수 큐가 조용히 어긋난다 — 검수 담당자는 필터 결과를 믿고 일하므로
    큐에서 빠진 요약은 영원히 검수되지 않는다. 두 구현을 전수 대조한다.
    """

    #: (is_reviewed, importance, review_category, review_warnings) 전 조합 36가지.
    #:
    #: 5단계에서 게이트가 `importance == 'high'` 에서 `Disclosure.review_category` 로
    #: 옮겨졌다. 조건이 **FK를 타게 됐으므로** 양쪽이 같이 움직였는지 다시 전수 대조한다.
    #: `importance` 를 축에 남긴 것은 **더 이상 큐에 영향을 주지 않는다는 사실 자체**를
    #: 고정하기 위해서다. 축에서 빼면 조건에 되살아나도 아무도 모른다.
    COMBINATIONS = [
        (reviewed, importance, category, warnings)
        for reviewed in (False, True)
        for importance in DisclosureSummary.Importance.values
        for category in ('', ReviewCategory.CAPITAL)
        for warnings in ([], ['경고 1건'], ['경고 1건', '경고 2건'])
    ]

    def setUp(self):
        super().setUp()
        for seq, (reviewed, importance, category, warnings) in enumerate(
            self.COMBINATIONS, start=1
        ):
            self.make_summary(
                seq, is_reviewed=reviewed, importance=importance,
                review_category=category, review_warnings=warnings,
            )

    def _filtered_pks(self, value):
        response = self.client.get(
            reverse('admin:disclosures_disclosuresummary_changelist'),
            {'needs_review': value},
        )
        self.assertEqual(response.status_code, 200)
        return set(response.context['cl'].queryset.values_list('pk', flat=True))

    def _property_pks(self):
        return {
            summary.pk for summary in DisclosureSummary.objects.all()
            if summary.needs_review
        }

    def test_filter_yes_equals_the_property(self):
        self.assertEqual(self._filtered_pks('yes'), self._property_pks())

    def test_filter_no_is_the_exact_complement(self):
        every_pk = set(DisclosureSummary.objects.values_list('pk', flat=True))
        yes, no = self._filtered_pks('yes'), self._filtered_pks('no')

        self.assertEqual(yes | no, every_pk)     # 어느 쪽에도 안 들어가는 요약이 없어야
        self.assertEqual(yes & no, set())        # 양쪽에 겹치는 요약도 없어야

    def _matching(self, reviewed, category, has_warnings):
        qs = DisclosureSummary.objects.filter(
            is_reviewed=reviewed, disclosure__review_category=category)
        return list(
            qs.exclude(review_warnings=[]) if has_warnings else qs.filter(review_warnings=[])
        )

    def test_boundary_cases_are_classified_the_same_way(self):
        """말로 옮긴 경계 — 조건식이 바뀌면 여기가 먼저 깨진다.

        축이 `importance` 에서 `review_category` 로 바뀌었다. 각 경계마다 **importance
        3종 전부**를 확인하므로, 게이트가 중요도로 되돌아가면 여기서 갈린다.
        """
        expectations = {
            # (검수 완료?, 게이트 유형, 경고 있음?) → 검수 필요한가
            (False, '', False): False,                       # 아무 사유 없음
            (False, '', True): True,                         # 경고만 → 유형 무관
            (False, ReviewCategory.CAPITAL, False): True,    # 유형 게이트만 → 경고 무관
            (False, ReviewCategory.CAPITAL, True): True,     # 둘 다
            (True, ReviewCategory.CAPITAL, False): False,    # 검수 완료
            (True, '', True): False,                         # 검수 완료(경고는 남는다)
        }
        yes = self._filtered_pks('yes')
        for (reviewed, category, has_warnings), expected in expectations.items():
            matched = self._matching(reviewed, category, has_warnings)
            with self.subTest(reviewed=reviewed, category=category,
                              warnings=has_warnings):
                # importance 3종 × (경고 목록 2종 or 1종)이 전부 잡혀야 축이 온전하다.
                self.assertEqual(len(matched), 6 if has_warnings else 3)
                for summary in matched:
                    self.assertEqual(summary.needs_review, expected)
                    self.assertEqual(summary.pk in yes, expected)

    def test_importance_no_longer_moves_the_queue(self):
        """중요도만 다른 두 요약이 같은 판정을 받는지 — 게이트 전환의 핵심 주장이다."""
        for category in ('', ReviewCategory.CAPITAL):
            for has_warnings in (False, True):
                verdicts = {
                    summary.importance: summary.needs_review
                    for summary in self._matching(False, category, has_warnings)
                }
                with self.subTest(category=category, warnings=has_warnings):
                    self.assertEqual(len(set(verdicts.values())), 1, verdicts)

    def test_has_warnings_filter_matches_stored_warnings(self):
        with_warnings = {
            summary.pk for summary in DisclosureSummary.objects.all()
            if summary.review_warnings
        }
        response = self.client.get(
            reverse('admin:disclosures_disclosuresummary_changelist'),
            {'has_warnings': 'yes'})
        self.assertEqual(
            set(response.context['cl'].queryset.values_list('pk', flat=True)),
            with_warnings,
        )


class AdminReviewScreenTest(ReviewWorkflowTestBase):
    """검수 화면(변경 폼 + 원문 대조 패널)이 실제로 뜨고 필요한 것을 보여주는지."""

    def setUp(self):
        super().setUp()
        self.summary = self.make_summary(
            1, importance=DisclosureSummary.Importance.HIGH,
            raw_content='유상증자 결정\n납입금액 | 3조 9,891억\n비율 | 12.34%',
            review_warnings=[
                summarizer.UNSUPPORTED_NUMBER_PREFIX + '3조 9,891억, 4,000억'],
            evidence=[{'field': 'one_line', 'claim': '납입금액 3조 9,891억',
                       'quote': '납입금액 | 3조 9,891억', 'quote_found': True,
                       'numbers_ok': True, 'missing_numbers': []}],
        )

    def test_change_form_renders_the_comparison_panel(self):
        response = self.client.get(self.change_url(self.summary))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="review-panel"')
        self.assertContains(response, '원문 대조')
        self.assertContains(response, self.summary.disclosure.dart_url)
        self.assertContains(response, '납입금액')          # 원문이 패널에 실린다
        self.assertContains(response, 'rp-hit')            # 지목 수치가 하이라이트된다

    def test_panel_flags_numbers_absent_from_the_raw_text(self):
        """'원문에 없음' 칩이 검수의 핵심 신호다 — 4,000억은 원문에 없다."""
        response = self.client.get(self.change_url(self.summary))

        self.assertContains(response, 'rp-chip-missing')
        self.assertContains(response, '원문에 없음')

    def test_panel_shows_evidence_verdicts(self):
        response = self.client.get(self.change_url(self.summary))

        self.assertContains(response, '인용 확인됨')
        self.assertContains(response, '한 줄 요약')       # field 라벨 변환

    def test_review_screen_never_calls_dart_or_openai(self):
        """검수 화면에서 '최신 원문을 가져오자'는 코드가 들어가면 안 된다(PLAN.md 12.1)."""
        with patch.object(summarizer, '_call_openai') as mock_llm, \
                patch('disclosures.dart.requests.get') as mock_dart:
            self.client.get(self.change_url(self.summary))
            self.client.get(reverse('admin:disclosures_disclosuresummary_changelist'))
        mock_llm.assert_not_called()
        mock_dart.assert_not_called()

    def test_changelist_shows_the_dart_link(self):
        response = self.client.get(
            reverse('admin:disclosures_disclosuresummary_changelist'))

        self.assertContains(response, self.summary.disclosure.dart_url)
        self.assertContains(response, 'target="_blank"')

    def test_queue_is_ordered_by_review_priority(self):
        """자동 미게시 → 검수 필수 유형 → 경고 있음 → 접수일 최신.

        5단계에서 1순위 키가 AI의 `importance` 에서 공시 유형 게이트로 바뀌었고,
        자동 미게시가 그 앞에 붙었다. **큐에 들어오는 기준이 유형인데 정렬만 중요도로
        두면 맨 위가 큐의 이유와 어긋난다.** 자동 미게시가 맨 앞인 이유는 그 요약들이
        지금 사용자에게 안 보이고 있어 서비스에 구멍이 나 있기 때문이다.

        `self.summary`(seq=1)는 수치 경고를 달고 있고 importance 가 HIGH 다 —
        중요도가 정렬에 남아 있었다면 맨 위여야 하지만 이제 3순위다.
        """
        auto_hidden = self.make_summary(
            2, filed_at=date(2026, 7, 1), is_published=False,
            hidden_by=DisclosureSummary.HiddenBy.AUTO)
        gated = self.make_summary(
            3, filed_at=date(2026, 7, 1), review_category=ReviewCategory.CAPITAL)
        recent = self.make_summary(4, filed_at=date(2026, 7, 5))
        older = self.make_summary(5, filed_at=date(2026, 7, 3))

        response = self.client.get(
            reverse('admin:disclosures_disclosuresummary_changelist'))

        self.assertEqual(
            list(response.context['cl'].queryset.values_list('pk', flat=True)),
            [auto_hidden.pk, gated.pk, self.summary.pk, recent.pk, older.pk],
        )

    def test_human_hidden_summaries_do_not_jump_the_queue(self):
        """사람이 내린 요약은 '이미 결론이 난 것'이라 맨 앞이 아니다.

        `is_published=False` 하나로 정렬하면 자동 미게시와 구분되지 않는다 —
        검수자가 할 일이 정반대인데 같은 자리에 놓인다.
        """
        human_hidden = self.make_summary(
            2, filed_at=date(2026, 7, 9), is_published=False,
            hidden_by=DisclosureSummary.HiddenBy.HUMAN)

        response = self.client.get(
            reverse('admin:disclosures_disclosuresummary_changelist'))
        order = list(response.context['cl'].queryset.values_list('pk', flat=True))

        self.assertEqual(order[0], self.summary.pk)   # 경고 있는 게시분이 먼저
        self.assertEqual(order[-1], human_hidden.pk)

    def test_dart_link_column_degrades_when_the_url_is_missing(self):
        """링크가 없는 공시에서 목록이 깨지면 검수 큐 전체를 못 연다."""
        blank = self.make_summary(6)
        Disclosure.objects.filter(pk=blank.disclosure_id).update(dart_url='')
        blank.refresh_from_db()

        model_admin = DisclosureSummaryAdmin(DisclosureSummary, AdminSite())
        self.assertEqual(model_admin.dart_link(blank), '-')
        self.assertEqual(
            self.client.get(
                reverse('admin:disclosures_disclosuresummary_changelist')).status_code,
            200,
        )

    def test_has_warnings_no_filter_selects_clean_summaries(self):
        clean = self.make_summary(7)

        response = self.client.get(
            reverse('admin:disclosures_disclosuresummary_changelist'),
            {'has_warnings': 'no'})

        pks = set(response.context['cl'].queryset.values_list('pk', flat=True))
        self.assertEqual(pks, {clean.pk})       # 경고가 붙은 self.summary는 빠진다

    def test_summary_without_raw_content_still_renders(self):
        """원문 미확보 공시에서 패널이 깨지면 검수 화면 자체를 못 연다."""
        bare = self.make_summary(5, raw_content='')

        response = self.client.get(self.change_url(bare))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '원문이 아직 확보되지 않았습니다')

    def test_summary_cannot_be_added_by_hand(self):
        """요약은 요약 파이프라인만 만든다(PLAN.md 11) — 추가 경로 자체가 없어야 한다.

        회귀 배경: `disclosure`가 readonly_fields에 들어가면서 추가 폼에서 빠졌는데 추가
        권한은 열려 있어, 저장을 누르면 disclosure_id가 NULL이라 IntegrityError로 500이
        났다. 권한을 닫아 경로를 없앤 것이 수정이므로 GET·POST 양쪽을 고정한다.
        """
        add_url = reverse('admin:disclosures_disclosuresummary_add')
        self.client.raise_request_exception = False

        self.assertEqual(self.client.get(add_url).status_code, 403)
        self.assertEqual(
            self.client.post(add_url, {
                'one_line': '수동 생성', 'easy_explanation': '설명',
                'why_important': '이유', 'importance': 'medium',
                'hidden_reason': '', '_save': '저장',
            }).status_code,
            403,
        )
        self.assertFalse(DisclosureSummary.objects.filter(one_line='수동 생성').exists())

    def test_changelist_offers_no_add_button(self):
        """권한만 닫고 버튼을 남기면 검수자가 누를 때마다 403을 만난다.

        `addlink` 클래스 자체를 찾으면 안 된다 — 사이드바에 다른 모델(공시·기업·
        섹터·사용자)의 추가 링크가 같은 클래스로 늘 함께 렌더되기 때문에, 요약의
        버튼을 지워도 실패한다. 이 화면이 요약 추가 URL을 어디에도 걸지 않았는지만 본다.
        """
        response = self.client.get(
            reverse('admin:disclosures_disclosuresummary_changelist'))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(
            response, reverse('admin:disclosures_disclosuresummary_add'))

    def test_disclosure_screen_also_refuses_to_add_a_summary(self):
        """인라인과 'AI 요약' 화면의 추가 정책이 같아야 한다 — 한쪽만 막으면 우회로가 된다."""
        model_admin = DisclosureSummaryAdmin(DisclosureSummary, AdminSite())
        inline = DisclosureSummaryInline(Disclosure, AdminSite())
        request = RequestFactory().get('/')
        request.user = self.reviewer

        self.assertFalse(model_admin.has_add_permission(request))
        self.assertFalse(inline.has_add_permission(request))


class HighlightTermsSecurityTest(TestCase):
    """`highlight_terms`의 이스케이프 — 원문은 DART에서 온 외부 데이터다.

    태그를 끼워 넣는 태그이므로 순서를 한 번만 뒤집어도(이스케이프 후 검색, 또는
    조각을 그대로 이어붙임) 원문의 마크업이 admin 페이지에서 그대로 실행된다.
    """

    def test_markup_in_the_raw_text_is_escaped(self):
        result = highlight_terms(
            '<script>alert(1)</script> 납입금액 3조 9,891억 <b>굵게</b>',
            ['3조 9,891억'], 'raw')
        html = str(result['html'])

        self.assertNotIn('<script>', html)
        self.assertIn('&lt;script&gt;', html)
        self.assertNotIn('<b>', html)
        self.assertIn('&lt;b&gt;', html)
        self.assertIn('<mark class="rp-hit"', html)   # 우리가 넣은 마크업만 살아남는다

    def test_ampersand_and_quotes_are_escaped(self):
        html = str(highlight_terms('A & B "인용" \'작은따옴표\'', [], 'raw')['html'])

        self.assertIn('&amp;', html)
        self.assertIn('&quot;', html)
        self.assertNotIn('B "인용"', html)

    def test_escaping_survives_inside_a_highlight(self):
        """매칭된 조각도 이스케이프해야 한다 — <mark> 안이라고 안전한 게 아니다."""
        html = str(highlight_terms('값 <b>1,000</b> 원', ['<b>1,000</b>'], 'raw')['html'])

        self.assertNotIn('<b>', html)
        self.assertIn('&lt;b&gt;1,000&lt;/b&gt;', html)

    def test_search_term_is_escaped_in_the_data_attribute(self):
        """검색어도 결국 경고 문자열에서 온 값이다. 속성에 그대로 박으면 탈출당한다."""
        html = str(highlight_terms('a"onload="alert(1)', ['a"onload="alert(1)'], 'raw')['html'])

        self.assertNotIn('data-term="a"onload=', html)
        self.assertIn('&quot;', html)

    def test_term_broken_by_markup_is_reported_missing_not_mismatched(self):
        """수치가 태그 경계에 걸치면 매칭되지 않아야 하고, 마크업도 깨지면 안 된다."""
        result = highlight_terms('값은 <b>3조</b> 9,891억', ['3조 9,891억'], 'raw')
        html = str(result['html'])

        self.assertEqual(result['total'], 0)
        self.assertEqual(result['missing'], ['3조 9,891억'])
        self.assertNotIn('<mark', html)
        self.assertEqual(html.count('&lt;b&gt;'), 1)   # 원문 조각이 유실되지 않았다

    def test_separator_differences_are_tolerated(self):
        """요약은 `3조 9,891억`, 원문(표 추출)은 `3조9891억`으로 적히는 일이 흔하다."""
        for source in ('3조9891억', '3조 9,891억', '3조  9891 억'.replace(' 억', '억')):
            with self.subTest(source=source):
                self.assertEqual(
                    highlight_terms(source, ['3조 9,891억'], 'raw')['total'], 1)

    def test_unit_characters_are_not_relaxed(self):
        """단위까지 느슨하게 풀면 엉뚱한 곳이 걸려 검수자가 헛걸음한다."""
        result = highlight_terms('3조 9,891만', ['3조 9,891억'], 'raw')

        self.assertEqual(result['total'], 0)
        self.assertEqual(result['missing'], ['3조 9,891억'])

    def test_longer_term_wins_over_its_substring(self):
        """`3조 9,891억`이 `891`에 먼저 먹히면 하이라이트가 엉뚱한 자리에 붙는다."""
        result = highlight_terms('납입금액 3조 9,891억', ['891', '3조 9,891억'], 'raw')

        self.assertEqual(result['total'], 1)
        self.assertIn('>3조 9,891억<', str(result['html']))

    def test_repeated_term_gets_distinct_anchors(self):
        html = str(highlight_terms('1,000억 그리고 1,000억', ['1,000억'], 'raw')['html'])

        self.assertIn('id="raw-0-1"', html)
        self.assertIn('id="raw-0-2"', html)

    def test_separator_only_and_empty_terms_are_ignored(self):
        """구분자만으로 된 검색어는 빈 매칭으로 무한 분할을 일으킨다."""
        result = highlight_terms('본문 1,234', [' ', ',', '', '  ,  '], 'raw')

        self.assertEqual(result['total'], 0)
        self.assertEqual(str(result['html']), '본문 1,234')

    def test_regex_metacharacters_in_terms_are_literal(self):
        result = highlight_terms('비율 12.34%', ['12.34%'], 'raw')
        self.assertEqual(result['total'], 1)
        # 정규식으로 해석되면 `12934%` 같은 문자열도 걸린다
        self.assertEqual(highlight_terms('비율 12934%', ['12.34%'], 'raw')['total'], 0)

    def test_empty_source_is_handled(self):
        for source in ('', None):
            with self.subTest(source=source):
                result = highlight_terms(source, ['3조'], 'raw')
                self.assertEqual(str(result['html']), '')
                self.assertEqual(result['missing'], ['3조'])

    def test_duplicate_terms_are_collapsed(self):
        result = highlight_terms('1,000억', ['1,000억', '1,000억'], 'raw')
        self.assertEqual(len(result['hits']), 1)


class ReviewPanelFilterTest(TestCase):
    """패널 보조 필터 — 잘못 표시하면 검수자가 멀쩡한 근거를 의심하게 된다."""

    def test_has_key_separates_missing_key_from_falsy_value(self):
        """`quote_found`가 없는 옛 근거를 '인용 미발견'으로 표시하면 안 된다.

        옛 근거는 판정을 안 한 것이지 실패한 것이 아니다 — '인용 미검증'으로 가야 한다.
        """
        self.assertTrue(has_key({'quote_found': False}, 'quote_found'))
        self.assertTrue(has_key({'quote_found': True}, 'quote_found'))
        self.assertFalse(has_key({'quote': '구절'}, 'quote_found'))

    def test_has_key_rejects_every_non_dict_value(self):
        """dict가 아닌 입력에는 "키가 있는가"라는 물음 자체가 성립하지 않는다 → False.

        `in`을 그냥 쓰면 컨테이너마다 뜻이 달라진다. 문자열이면 부분 문자열 검사가 되어
        `'quote_found 를 담은 문자열'`이 True가 되고, 리스트면 원소 검사가 되어 값이 아닌
        키 이름이 원소로 들어 있기만 해도 True가 된다. 둘 다 '인용 미검증'으로 가야 할
        옛 형식 근거를 '인용 확인됨/미발견'으로 잘못 단정하는 길이다.
        """
        for value in (
            None, 42, 3.5, True,
            'quote_found 를 담은 문자열', 'quote_found',
            ['quote_found'], ('quote_found',), {'quote_found'},
        ):
            with self.subTest(value=value):
                self.assertFalse(has_key(value, 'quote_found'))

    def test_evidence_field_label_translates_model_field_names(self):
        self.assertEqual(evidence_field_label('one_line'), '한 줄 요약')
        self.assertEqual(evidence_field_label('easy_explanation'), '쉬운 설명')
        self.assertEqual(evidence_field_label('why_important'), '왜 중요한가')

    def test_evidence_field_label_falls_back_instead_of_blanking(self):
        self.assertEqual(evidence_field_label(''), '요약')
        self.assertEqual(evidence_field_label(None), '요약')
        self.assertEqual(evidence_field_label('unknown_field'), 'unknown_field')

    def test_field_labels_cover_every_body_field(self):
        """모델의 본문 필드가 늘면 라벨도 늘어야 한다 — 안 그러면 원시 필드명이 뜬다."""
        for field in DisclosureSummary.BODY_FIELDS:
            with self.subTest(field=field):
                self.assertNotEqual(evidence_field_label(field), field)


class ReviewContractConsistencyTest(TestCase):
    """00_input.md 3장 인터페이스 계약이 모델·admin·템플릿에서 같은 이름으로 살아 있는지.

    Django 템플릿은 없는 속성을 조용히 빈 문자열로 처리한다. 필드 이름이 바뀌면 예외 없이
    화면에서 표시만 사라지므로, 경계면을 테스트로 고정하는 것 말고 방어 수단이 없다.
    """

    TEMPLATE_ROOT = Path(__file__).resolve().parent / 'templates'

    def test_new_fields_match_the_contract_table(self):
        expected = {
            'is_published': ('BooleanField', '노출 여부', True),
            'hidden_reason': ('CharField', '숨김 사유', ''),
            'edited_by_human': ('BooleanField', '사람 수정 여부', False),
            'llm_original': ('JSONField', 'LLM 원본', dict),
        }
        for name, (field_type, verbose_name, default) in expected.items():
            with self.subTest(field=name):
                field = DisclosureSummary._meta.get_field(name)
                self.assertEqual(field.get_internal_type(), field_type)
                self.assertEqual(field.verbose_name, verbose_name)
                self.assertEqual(field.default, default)

    def test_reviewed_at_is_nullable_with_the_contract_label(self):
        """검수 전에는 '시각 없음'이어야 한다 — 기본값을 주면 미검수와 구분이 사라진다."""
        field = DisclosureSummary._meta.get_field('reviewed_at')
        self.assertEqual(field.get_internal_type(), 'DateTimeField')
        self.assertEqual(field.verbose_name, '검수 시각')
        self.assertTrue(field.null)
        self.assertTrue(field.blank)

    def test_hidden_reason_length_matches_the_contract(self):
        self.assertEqual(
            DisclosureSummary._meta.get_field('hidden_reason').max_length, 200)

    def test_reviewed_by_survives_user_deletion(self):
        """검수자 계정이 지워져도 '검수는 되었다'는 사실은 남아야 한다."""
        from django.db.models import SET_NULL

        field = DisclosureSummary._meta.get_field('reviewed_by')
        self.assertEqual(field.verbose_name, '검수자')
        self.assertIs(field.remote_field.on_delete, SET_NULL)
        self.assertTrue(field.null)

    def test_was_human_edited_is_edit_and_review(self):
        """정의가 세 곳(모델·산출물 문서·템플릿)에 흩어져 있어 진리표로 고정한다."""
        summary = DisclosureSummary(one_line='x', easy_explanation='y', why_important='z')
        for edited in (False, True):
            for reviewed in (False, True):
                with self.subTest(edited=edited, reviewed=reviewed):
                    summary.edited_by_human = edited
                    summary.is_reviewed = reviewed
                    self.assertEqual(summary.was_human_edited, edited and reviewed)

    def test_body_snapshot_covers_exactly_the_body_fields(self):
        summary = DisclosureSummary(
            one_line='한 줄', easy_explanation='설명', why_important='이유')

        self.assertEqual(
            summary.body_snapshot(),
            {'one_line': '한 줄', 'easy_explanation': '설명', 'why_important': '이유'},
        )
        self.assertEqual(
            set(summary.body_snapshot()), set(DisclosureSummary.BODY_FIELDS))

    def test_llm_original_uses_the_body_field_keys(self):
        """패널이 `llm_original.one_line` 식으로 직접 읽으므로 키 이름이 계약이다."""
        panel = (self.TEMPLATE_ROOT
                 / 'admin/disclosures/disclosuresummary/_review_panel.html'
                 ).read_text(encoding='utf-8')
        for field in DisclosureSummary.BODY_FIELDS:
            with self.subTest(field=field):
                self.assertIn(f'llm_original.{field}', panel)

    def test_templates_only_reference_existing_summary_attributes(self):
        """템플릿이 `summary.`·`original.`로 읽는 이름이 모델에 실제로 있는지 전수 확인."""
        pattern = re.compile(r'\b(?:summary|original)\.([a-z_][a-z0-9_]*)')
        referenced = set()
        for path in self.TEMPLATE_ROOT.rglob('*.html'):
            referenced.update(pattern.findall(path.read_text(encoding='utf-8')))

        self.assertIn('was_human_edited', referenced)   # 스캔이 실제로 동작하는지
        missing = sorted(
            name for name in referenced if not hasattr(DisclosureSummary, name))
        self.assertEqual(missing, [])

    def test_admin_actions_keep_their_contract_names(self):
        """산출물 문서와 운영 안내가 메서드 이름으로 액션을 지목한다."""
        model_admin = DisclosureSummaryAdmin(DisclosureSummary, AdminSite())
        self.assertEqual(
            list(model_admin.actions),
            ['mark_reviewed', 'hide_summaries', 'restore_summaries'],
        )

    def test_audit_fields_are_read_only_in_admin(self):
        model_admin = DisclosureSummaryAdmin(DisclosureSummary, AdminSite())
        for field in ('edited_by_human', 'llm_original', 'reviewed_by', 'reviewed_at',
                      'evidence', 'review_warnings'):
            with self.subTest(field=field):
                self.assertIn(field, model_admin.readonly_fields)

    def test_body_fields_are_editable_in_admin(self):
        """4단계의 목적 자체 — 검수자가 본문을 고칠 수 없으면 큐가 소진되지 않는다."""
        model_admin = DisclosureSummaryAdmin(DisclosureSummary, AdminSite())
        for field in DisclosureSummary.BODY_FIELDS:
            with self.subTest(field=field):
                self.assertNotIn(field, model_admin.readonly_fields)

    def test_summary_inline_cannot_be_used_to_bypass_save_model(self):
        """인라인 저장은 DisclosureSummaryAdmin.save_model을 타지 않는다.

        여기서 본문을 고칠 수 있으면 사람 수정 감지(edited_by_human·llm_original)가
        통째로 우회되어 감사 기록이 빈다.
        """
        request = RequestFactory().get('/')
        request.user = get_user_model()(is_superuser=True, is_staff=True, is_active=True)
        inline = DisclosureSummaryInline(Disclosure, AdminSite())

        self.assertFalse(inline.has_add_permission(request))
        editable = {
            field.name for field in DisclosureSummary._meta.get_fields()
            if getattr(field, 'editable', False) and not field.auto_created
        }
        # 부모 링크(disclosure)를 뺀 모든 편집 가능 필드가 읽기 전용이어야 한다.
        self.assertEqual(
            sorted(editable - set(inline.readonly_fields) - {'disclosure'}), [])


# ===========================================================================
# 5단계 — 요약 파이프라인 자동 교정
# ===========================================================================


class KoreanUnitFormatTest(TestCase):
    """`units.py` 경계값 — 이번 단계의 표적.

    LLM이 반복해서 틀린 지점은 정확히 하나였다. **쉼표는 3자리로 끊고 만·억·조는 4자리로
    끊는다.** 코드가 하면 영원히 안 틀리지만, 그 코드가 틀리면 이번엔 영원히 틀린다.
    그래서 표기 규칙 전체를 값으로 못 박는다.

    표는 `_workspace/21_backend_units_verification.md` 2.3을 그대로 옮긴 것이고,
    굵게 표시됐던 세 줄(39조·45조·43조)이 실제로 웹에 잘못 나갔던 값이다.
    """

    #: (원 단위 정수, max_units=2 기본 표기, max_units=None 정확 표기)
    CASES = [
        (0, '0 원', '0 원'),
        (1, '1 원', '1 원'),
        (9_999, '9,999 원', '9,999 원'),
        (10_000, '1만 원', '1만 원'),
        (10_001, '1만 1 원', '1만 1 원'),
        (12_345, '1만 2,345 원', '1만 2,345 원'),
        (100_000_000, '1억 원', '1억 원'),
        (1_000_000_000_000, '1조 원', '1조 원'),
        (10_000_000_000_000, '10조 원', '10조 원'),
        # 0인 자리가 생략돼 항이 2개뿐이라도 '10조 1 원'은 사람이 쓰는 말이 아니다.
        # 절사 기준이 '항 개수'가 아니라 '자릿수 폭'이어야 하는 이유가 이 줄이다.
        (10_000_000_000_001, '약 10조 원', '10조 1 원'),
        (100_000_000_000_001, '약 100조 원', '100조 1 원'),
        # 반대로 원래 정보가 적은 값은 폭 안에 다 들어와 절사가 일어나지 않는다.
        (1_000_050_000, '10억 5만 원', '10억 5만 원'),
        (39_890_534_790_000, '약 39조 8,905억 원', '39조 8,905억 3,479만 원'),   # id 128
        (45_453_450_000_000, '약 45조 4,534억 원', '45조 4,534억 5,000만 원'),   # id 81
        (43_140_750_000_000, '약 43조 1,407억 원', '43조 1,407억 5,000만 원'),   # id 107
        (400_000_000_000, '4,000억 원', '4,000억 원'),                           # id 8
        (-39_890_534_790_000, '약 -39조 8,905억 원', '-39조 8,905억 3,479만 원'),
        (123456789012345678901, '약 12,345경 6,789조 원',
         '12,345경 6,789조 123억 4,567만 8,901 원'),
    ]

    #: 웹에 실제로 나간 오답 → 정답. 전부 정확히 10배 어긋나 있었다.
    TEN_FOLD_ERRORS = [
        ('3조 9,891억', 39_890_534_790_000, '약 39조 8,905억 원'),
        ('4조 5,453억', 45_453_450_000_000, '약 45조 4,534억 원'),
        ('4조 3,140억', 43_140_750_000_000, '약 43조 1,407억 원'),
    ]

    def test_boundary_values_format_as_specified(self):
        for value, short, exact in self.CASES:
            with self.subTest(value=value):
                self.assertEqual(units.format_korean_won(value), short)
                self.assertEqual(units.format_korean_won(value, max_units=None), exact)

    def test_the_three_real_errors_are_reproduced_correctly(self):
        """AI가 낸 오답을 같은 파서로 되읽으면 정확히 10배 차이가 나야 한다."""
        for wrong, raw_value, expected in self.TEN_FOLD_ERRORS:
            with self.subTest(wrong=wrong):
                self.assertEqual(units.format_korean_won(raw_value), expected)
                wrong_value = units.parse_korean_amount(wrong)
                self.assertIsNotNone(wrong_value)
                # 오답 ÷ 정답이 정확히 1/10 — 자릿수 끊기에서만 무너졌다는 진단의 근거.
                self.assertAlmostEqual(wrong_value / raw_value, 0.1, places=4)

    def test_exact_notation_round_trips(self):
        """`parse(format(v, max_units=None)) == v` — 표기 로직의 자기 검산."""
        for value, _short, _exact in self.CASES:
            with self.subTest(value=value):
                exact = units.format_korean_won(value, max_units=None)
                self.assertEqual(units.parse_korean_amount(exact), value)

    def test_truncated_notation_never_exceeds_the_value(self):
        """절사는 버림이므로 되읽은 값이 원값을 넘지 않는다(금액을 부풀리지 않는다).

        음수는 절댓값 기준으로 본다 — 버림은 0쪽으로 당기므로 부호를 붙인 채 비교하면
        부등호가 뒤집힌다.
        """
        for value, _short, _exact in self.CASES:
            with self.subTest(value=value):
                parsed = units.parse_korean_amount(units.format_korean_won(value))
                self.assertIsNotNone(parsed)
                self.assertLessEqual(abs(parsed), abs(value))
                self.assertEqual(parsed < 0, value < 0)

    def test_round_trip_holds_across_a_swept_range(self):
        """경계표 밖에서도 성립하는지 — 표에만 맞춘 구현을 걸러낸다."""
        values = [
            v for base in (1, 7, 9999, 10 ** 4, 10 ** 8, 10 ** 12, 10 ** 16)
            for v in (base - 1, base, base + 1, base * 3 + 1234)
            if v >= 0
        ]
        for value in values:
            with self.subTest(value=value):
                exact = units.format_korean_amount(value, max_units=None)
                self.assertEqual(units.parse_korean_amount(exact), value)
                short = units.format_korean_amount(value)
                self.assertLessEqual(units.parse_korean_amount(short), value)

    def test_approximation_mark_appears_exactly_when_digits_are_dropped(self):
        """`약` 은 장식이 아니라 '버린 자리가 있다'는 사실의 표시다."""
        self.assertNotIn(units.APPROX_MARK.strip(), units.format_korean_won(10 ** 13))
        self.assertIn(units.APPROX_MARK.strip(), units.format_korean_won(10 ** 13 + 1))

    def test_split_units_drops_zero_coefficients(self):
        self.assertEqual(units.split_units(45_453_450_000_000),
                         [(12, 45), (8, 4534), (4, 5000)])
        self.assertEqual(units.split_units(10_000), [(4, 1)])
        self.assertEqual(units.split_units(0), [])

    def test_invalid_inputs_are_rejected_loudly(self):
        """조용히 이상한 값을 뱉느니 터지는 게 낫다 — 금액 표기는 되돌릴 수 없다."""
        with self.assertRaises(ValueError):
            units.split_units(-1)
        for bad in (1.0, True, '1', None):
            with self.subTest(bad=bad), self.assertRaises(TypeError):
                units.split_units(bad)
        with self.assertRaises(ValueError):
            units.format_korean_amount(1, max_units=0)
        with self.assertRaises(ValueError):
            units.format_korean_amount(1, max_units=-1)

    def test_parser_takes_whole_notations_only(self):
        """자유 문장에서 긁어내는 일은 `verification.extract_numbers` 몫이다.

        여기서 통짜 일치를 요구하는 이유는 용도가 '코드가 만든 표기를 되읽어 값이
        보존됐는지 확인'하는 것이기 때문이다. 넓게 잡으면 그 확인이 무의미해진다.
        """
        for text, expected in [
            ('약 45조 4,534억 5,000만 원', 45_453_450_000_000),
            ('39,890,534,790,000', 39_890_534_790_000),
            ('4,000억원', 400_000_000_000),
            ('3조9891억', 3_989_100_000_000),   # 공백 없이 붙여 쓴 표기
            ('-1억', -100_000_000),
        ]:
            with self.subTest(text=text):
                self.assertEqual(units.parse_korean_amount(text), expected)

        for text in ('계약금액은 3조 원이다', '', '원', 'abc', '3조 4,000억원어치', None):
            with self.subTest(text=text):
                self.assertIsNone(units.parse_korean_amount(text))

    def test_module_imports_without_django(self):
        """Django 없이 import 된다 — 순수 함수라는 설계 제약 자체를 고정한다.

        여기에 Django 의존이 들어오면 `manage.py` 없이 경계값을 돌릴 수 없게 되고,
        환산 로직이 웹 프레임워크의 수명에 묶인다.
        """
        code = (
            'import sys; sys.path.insert(0, sys.argv[1]); import units; '
            'assert "django" not in sys.modules, "django가 함께 로드됐다"; '
            'print(units.format_korean_won(45453450000000))'
        )
        env = dict(os.environ, PYTHONIOENCODING='utf-8')
        env.pop('DJANGO_SETTINGS_MODULE', None)
        result = subprocess.run(
            [sys.executable, '-c', code, str(Path(__file__).resolve().parent)],
            capture_output=True, text=True, encoding='utf-8', env=env,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), '약 45조 4,534억 원')


class ReviewPolicyGateTest(TestCase):
    """검수 게이트 정책(`review_policy.evaluate`) — 제목만 보고 판정한다.

    이 게이트가 5단계의 핵심 변경이다. AI가 매긴 중요도 대신 **DART가 부여한 제목**이라는
    바꿀 수 없는 사실로 대상을 고른다.
    """

    def test_capital_structure_titles_are_gated(self):
        for name in (
            '주요사항보고서(유상증자결정)', '증권신고서(지분증권)', '투자설명서',
            '증권발행실적보고서', '증권예탁증권(DR)발행결정',
            '유상증자또는주식관련사채등의발행결과(자율공시)',
            '주요사항보고서(전환사채권발행결정)',
        ):
            with self.subTest(name=name):
                self.assertEqual(review_policy.evaluate(name), ReviewCategory.CAPITAL)

    def test_other_categories(self):
        self.assertEqual(review_policy.evaluate('중대재해발생'), ReviewCategory.SAFETY)
        self.assertEqual(review_policy.evaluate('최대주주변경'), ReviewCategory.CONTROL)
        self.assertEqual(
            review_policy.evaluate('회계감사인의감사의견(감사보고서)제출'),
            ReviewCategory.DISTRESS,
        )

    def test_normalization_keeps_corrections_and_spacing_on_the_same_verdict(self):
        """정정 공시가 원 공시와 다른 판정을 받으면 검수 큐가 갈라진다.

        DART 제목에는 정렬용 연속 공백이 섞여 있고 `[기재정정]`·`[발행조건확정]` 같은
        선행 태그가 붙는다. `selection.normalize_title` 을 재사용하는 이유가 이것이다.
        """
        for name in (
            '주요사항보고서(유상증자결정)',
            '[기재정정]주요사항보고서(유상증자결정)',
            '주요사항보고서 (유상증자결정)',
            '주요사항보고서(유상증자결정)   ',
            '[발행조건확정]증권신고서(지분증권)',
        ):
            with self.subTest(name=name):
                self.assertTrue(review_policy.evaluate(name))

    def test_types_deliberately_left_out_stay_out(self):
        """게이트 밖으로 뺀 유형 — 뺀 것이 사고가 아니라 판단이었음을 고정한다.

        `단일판매ㆍ공급계약체결` 제외가 가장 논쟁적이다(backend 8(b)). 정형 서식이라
        위험이 사실상 금액 오류 하나이고 그건 수치 검증기가 담당한다는 논리인데,
        **이 유형에서 실제 오류가 나오면 즉시 게이트에 넣어야 한다.**
        """
        for name in (
            '단일판매ㆍ공급계약체결', '분기보고서(2026.03)',
            '연결재무제표기준영업(잠정)실적(공정공시)', '신규시설투자등',
            '기업설명회(IR)개최', '현금ㆍ현물배당결정',
        ):
            with self.subTest(name=name):
                self.assertEqual(review_policy.evaluate(name), '')

    def test_category_choices_match_the_model_field(self):
        field = Disclosure._meta.get_field('review_category')
        self.assertEqual(
            [value for value, _label in field.choices], ReviewCategory.values)
        self.assertTrue(field.blank)
        self.assertEqual(field.default, '')

    def test_should_regenerate_is_the_single_gate_on_cost(self):
        blocking = [verification.UNSUPPORTED_NUMBER_PREFIX + '3조 9,891억']

        self.assertFalse(should_regenerate([], 0))     # 고칠 게 없으면 안 부른다
        self.assertTrue(should_regenerate(blocking, 0))
        self.assertFalse(should_regenerate(blocking, MAX_REGENERATION_ATTEMPTS))
        self.assertFalse(should_regenerate(blocking, MAX_REGENERATION_ATTEMPTS + 5))


class ApplySelectionReviewGateTest(TestCase):
    """게이트 판정이 **DB에 남는지** — property가 아니라 컬럼이어야 admin 필터가 SQL로 건다."""

    def setUp(self):
        sector = Sector.objects.create(name='반도체', slug='semiconductor')
        self.company = Company.objects.create(
            sector=sector, corp_code=TRACKED_CORP, stock_code='005930', name='삼성전자')

    def _make(self, rcept_no, report_name, **kwargs):
        return Disclosure.objects.create(
            company=self.company, rcept_no=rcept_no, report_name=report_name,
            disclosure_type='거래소공시', filed_at=date(2026, 7, 1),
            dart_url=dart_viewer_url(rcept_no), **kwargs)

    def test_gate_verdict_is_persisted(self):
        gated = self._make('20260701000001', '[기재정정]주요사항보고서(유상증자결정)')
        plain = self._make('20260701000002', '단일판매ㆍ공급계약체결')

        call_command('apply_selection')

        gated.refresh_from_db()
        plain.refresh_from_db()
        self.assertEqual(gated.review_category, ReviewCategory.CAPITAL)
        self.assertEqual(plain.review_category, '')

    def test_already_decided_disclosures_need_force_to_pick_up_the_gate(self):
        """**운영상의 함정을 고정한다.** 이미 선별 판정이 끝난 공시는 `--force` 없이는
        게이트를 다시 받지 않는다.

        `apply_selection` 은 `selection_state=PENDING` 만 훑는다. 마이그레이션 0006 직후나
        게이트 정책에 유형을 새로 추가한 뒤에는 **반드시 `--force`** 로 재판정해야 한다.
        안 하면 새로 넣은 고위험 유형이 기존 데이터에서 조용히 비어 있는다.
        """
        decided = self._make(
            '20260701000003', '주요사항보고서(유상증자결정)',
            selection_state=SelectionState.TARGET)

        call_command('apply_selection')
        decided.refresh_from_db()
        self.assertEqual(decided.review_category, '')      # 건드리지 않는다

        call_command('apply_selection', force=True)
        decided.refresh_from_db()
        self.assertEqual(decided.review_category, ReviewCategory.CAPITAL)


class NumberExtractionTailTest(TestCase):
    """복합 표기의 꼬리 절단 — 오탐 4건(id 13·56·73·131)의 직접 원인이었다.

    `341억 8,587만 4,800원` 의 맨 끝 `4,800` 이 별개 수치로 떨어져 나가 "인용 근거 없는
    수치" 경고가 됐다. **AI가 정확히 맞았는데 경고가 붙던 경우다.**
    """

    def _value(self, text):
        found = verification.extract_numbers(text)
        self.assertEqual(len(found), 1, found)
        return found[0][1]

    def test_trailing_remainder_is_absorbed(self):
        self.assertEqual(self._value('341억 8,587만 4,800원'), 34_185_874_800)     # id 13
        self.assertEqual(self._value('2,882억 1,539만 6,500원'), 288_215_396_500)  # id 131
        self.assertEqual(self._value('1,942만 1,345주'), 19_421_345)               # id 73
        self.assertEqual(self._value('45조 4,534억 5,000만'), 45_453_450_000_000)

    def test_unrelated_neighbours_are_still_separate(self):
        """흡수를 넓히면 이번엔 무관한 두 수치가 하나로 뭉친다 — 반대 방향도 고정한다."""
        self.assertEqual(
            [value for _raw, value in verification.extract_numbers('1,234 5,678')],
            [1234.0, 5678.0],
        )
        # 직전 단위(만=10^4)보다 크거나 같으면 나머지가 아니라 뒤따르는 다른 수치다.
        self.assertEqual(
            [value for _raw, value in verification.extract_numbers('5만 60,000')],
            [50_000.0, 60_000.0],
        )


class ScalePathToleranceTest(TestCase):
    """표 머리글 배수 경로의 허용오차 — 오탐을 줄이면서 진짜 오류를 계속 잡는가(양방향)."""

    def test_approximate_wording_gets_the_same_tolerance_on_the_scale_path(self):
        """id 38: `736`(백만원)=7.36억을 '약 7억'으로 쓴 **정확한** 요약이 오탐이었다.

        직접 대조는 근사에 5%를 허용하는데 배수 경로만 1%를 요구하던 비대칭이 원인이다.
        배수를 곱하는 것과 근사로 반올림하는 것은 서로 독립인 오차다.
        """
        self.assertTrue(verification._value_supported(7e8, [736.0], True))

    def test_without_approximation_the_tight_tolerance_still_applies(self):
        """'약' 없이 단정한 수치에까지 5%를 열어 주면 탐지력을 그냥 내주는 것이다."""
        self.assertFalse(verification._value_supported(7e8, [736.0], False))

    def test_ten_fold_errors_are_still_caught_after_the_widening(self):
        """넓힌 허용오차가 진짜 오류를 통과시키지 않는지 — 이게 양방향의 반대쪽이다."""
        for wrong, raw in (
            (3.9891e12, 39_890_534_790_000.0),   # id 128
            (4.5453e12, 45_453_450_000_000.0),   # id 81
            (4.3140e12, 43_140_750_000_000.0),   # id 107
        ):
            with self.subTest(wrong=wrong):
                self.assertFalse(verification._value_supported(wrong, [raw], True))

    def test_declared_scales_reads_units_embedded_in_the_quote(self):
        """인용문이 머리글을 품고 있으면 좌표 계산 없이 배수를 알 수 있다(프롬프트 v3 §2)."""
        self.assertEqual(
            verification.declared_scales(
                ['(단위 : 억원)\n라. 출자상대방 총출자액 | 4,000']),
            {10 ** 8},
        )
        self.assertEqual(verification.declared_scales(['(단위: 백만원)']), {10 ** 6})
        # 외화·비금액 선언에는 원 배수를 붙이지 않는다.
        self.assertEqual(verification.declared_scales(['(단위 : 조 달러)']), set())
        self.assertEqual(verification.declared_scales(['(단위: 명)', '(단위 : 주)']), set())
        # 콜론이 없으면 산문이다. 이 신호가 없으면 '단위당 원가'가 전부 걸린다.
        self.assertEqual(verification.declared_scales(['3년 단위 주주환원정책']), set())

    def test_declared_scales_only_widens_never_narrows(self):
        """가산 전용이라는 성질 — 이 인자로는 경고가 새로 생길 수 없어야 한다."""
        self.assertFalse(verification._value_supported(999.0, [7.0], False))
        self.assertFalse(
            verification._value_supported(999.0, [7.0], False, {10 ** 8}))

    def test_unit_declarations_reset_the_scale_on_non_money_units(self):
        """비금액 선언을 버리면 앞 표의 배수가 뒤 표로 샌다(analyst W1, id 37)."""
        text = '머리\n(단위: 백만원)\n영업이익 | 37,610,283\n(단위: 시간)\n근로시간 | 40'
        declarations = verification.unit_declarations(text)

        self.assertEqual([scale for _pos, scale in declarations], [10 ** 6, 1])
        first, second = (position for position, _scale in declarations)
        self.assertEqual(verification.scale_at(declarations, first + 10), 10 ** 6)
        self.assertEqual(verification.scale_at(declarations, second + 10), 1)
        # 선언보다 앞에 있는 수치에는 적용하지 않는다(소급 금지).
        self.assertIsNone(verification.scale_at(declarations, 0))


class PublicationBlockingTest(TestCase):
    """무엇이 게시를 막는가 — 수치만 막고 인용·문체는 막지 않는다(양방향)."""

    NUMBER = verification.UNSUPPORTED_NUMBER_PREFIX + '3조 9,891억'
    QUOTE = verification.UNVERIFIED_QUOTE_PREFIX + ' (근거 1번): 합 계 | 4,177'
    STYLE = verification.SENTENCE_COUNT_PREFIX + ': 쉬운 설명이 7문장 (권장 3~5문장)'

    def test_only_numeric_warnings_block(self):
        self.assertEqual(
            verification.blocking_warnings([self.NUMBER, self.QUOTE, self.STYLE]),
            [self.NUMBER],
        )
        self.assertEqual(verification.blocking_warnings([self.QUOTE, self.STYLE]), [])
        self.assertEqual(verification.blocking_warnings([]), [])
        self.assertEqual(verification.blocking_warnings(None), [])

    def test_blocking_and_user_facing_warning_sets_are_deliberately_different(self):
        """'알려야 하는 것'과 '감춰야 하는 것'은 다른 판단이다.

        인용 경고가 정확히 그 차이에 있다 — 사용자에게는 알리되 게시는 막지 않는다.
        두 상수를 같게 만들면 표의 두 행을 이어 붙인 멀쩡한 요약이 대량으로 내려간다.
        """
        self.assertIn(
            verification.UNVERIFIED_QUOTE_PREFIX, verification.ACCURACY_WARNING_PREFIXES)
        self.assertNotIn(
            verification.UNVERIFIED_QUOTE_PREFIX,
            verification.PUBLICATION_BLOCKING_PREFIXES)
        self.assertNotIn(
            verification.SENTENCE_COUNT_PREFIX, verification.ACCURACY_WARNING_PREFIXES)
        self.assertNotIn(
            verification.SENTENCE_COUNT_PREFIX,
            verification.PUBLICATION_BLOCKING_PREFIXES)


class KnownDetectionGapTest(TestCase):
    """**지금 못 잡는 것**을 기록한다. 통과가 정상이라는 뜻이 아니다.

    아래 테스트가 실패하면 미탐이 **고쳐진** 것이다. 그때 기대값을 뒤집고 이 클래스를
    지우면 된다. 고쳐지지 않은 채 조용히 잊히는 것을 막기 위한 장치다.
    """

    def test_item_number_can_still_justify_a_ten_fold_error(self):
        """미탐 id 117 — 검증기가 오류를 못 잡은 게 아니라 **정당화해 줬다**.

        원문 `4. 자금조달의 목적 | 시설자금 (원) | 40,023,070,290,000`(=40조 230억)을
        요약이 `약 4조 원`으로 썼는데 경고가 없었다. 표 항목번호 `4.` 가 값 `4` 로
        추출돼 `4 × 조원 = 4조` 라는 가짜 근거가 됐기 때문이다.

        원인은 `is_reference_number` 의 비대칭이다 — 항목번호를 대조 **대상**에서는
        빼면서 **근거**에서는 빼지 않는다. 근거 쪽에서도 빼는 단순 수정은 id 110에
        새 오탐을 만들므로(꼬리 `5,000만`의 `5`를 잃는다) 헤더 배수 확정이 선행돼야 한다.
        """
        quote_values = [4.0, 40_023_070_290_000.0]   # 항목번호 4 + 진짜 금액
        self.assertTrue(verification._value_supported(4e12, quote_values, True))
        # 대조 '대상'에서는 이미 빠진다 — 이 비대칭이 원인 그 자체다.
        self.assertTrue(verification.is_reference_number('4', 4.0))
        self.assertFalse(verification.is_reference_number('4,000', 4000.0))

    def test_thousand_fold_errors_pass_the_multiplier_menu(self):
        """`_SCALE_MULTIPLIERS` 가 다섯 배수를 무조건 허용한다 — 1,000배 틀려도 통과한다.

        진짜 오류 7토큰이 걸린 것은 순전히 **10배가 목록에 없어서**다.
        좁히려면 헤더 배수 배선이 선행돼야 한다(backend 5.2·5.3).
        `10**5`·`10**1` 을 목록에 넣으면 진짜 오류가 전부 통과하므로 **절대 넣지 말 것**.
        """
        # 같은 근거 값 7,348 에 대해 7.348e9(십억원 표) 와 7.348e15(조원 표)가
        # **둘 다 정답으로 인정된다.** 백만 배 차이인데 검증기는 구별하지 못한다.
        self.assertTrue(verification._value_supported(7.348e9, [7348.0], False))
        self.assertTrue(verification._value_supported(7.348e15, [7348.0], False))
        self.assertNotIn(10 ** 5, verification._SCALE_MULTIPLIERS)
        self.assertNotIn(10 ** 1, verification._SCALE_MULTIPLIERS)


class AmountAnnotationTest(TestCase):
    """`annotate_amounts` — 코드가 읽기 쉬운 표기를 만들어 붙인다.

    A안(코드 사후 치환)의 유일한 위험은 **날짜·접수번호·주식수까지 잘못 환산하는 것**이다.
    앵커를 `원`/`주` 로 끝나는 수에만 걸어 구조적으로 막았다는 주장을 공격적으로 검증한다.
    """

    def _annotate(self, text):
        return summarizer.annotate_amounts({'one_line': text})['one_line']

    def test_amounts_over_the_floor_get_a_readable_notation(self):
        self.assertEqual(
            self._annotate('39,890,534,790,000원을 조달한다'),
            '39,890,534,790,000원(약 39조 8,905억 원)을 조달한다',
        )
        self.assertEqual(
            self._annotate('288,215,396,500원이다'),
            '288,215,396,500원(약 2,882억 1,539만 원)이다',
        )
        self.assertEqual(
            self._annotate('1,152,149,501주다'),
            '1,152,149,501주(약 11억 5,214만 주)다',
        )

    def test_non_amount_numbers_are_structurally_untouched(self):
        """날짜·접수번호·사업자번호·비율은 `원`/`주` 로 끝나지 않아 **불가능**하다."""
        for text in (
            '2026년 07월 13일 접수번호 20260713000123 사업자 104-81-26688',
            '17.33%에서 17.32%로 하락',
            '26,507,100,000 USD 규모',
            '보통주식 1,132,477주식수 기준',           # 주(?!식)
            '390,242백만원',                          # 이미 단위가 붙었다
        ):
            with self.subTest(text=text):
                self.assertEqual(self._annotate(text), text)

    def test_korean_units_already_written_are_not_annotated_again(self):
        """`4,000억원(약 4,000억 원)` 같은 동어반복이 나오면 결함이다."""
        for text in (
            '4,000억원이다', '4,000억 원이다',
            '45조 4,534억 5,000만 원', '3조 9,891억 원',
            '2,882억 1,539만 6,500원',                # 복합 표기의 꼬리(띄어 쓴 경우)
            '2,882억 1,539만6,500원',                 # 붙여 쓴 경우
            '3조 500,000,000원',                      # 하한을 넘는 인위적 꼬리
            '5,000만원', '374,629천원',
        ):
            with self.subTest(text=text):
                self.assertEqual(self._annotate(text), text)

    def test_floor_is_one_hundred_million(self):
        """`285,000원(약 28만 원)` 은 도움이 안 되고 문장만 지저분해진다."""
        self.assertEqual(summarizer.AMOUNT_NOTATION_MIN, 10 ** 8)
        self.assertEqual(self._annotate('285,000원'), '285,000원')
        self.assertEqual(self._annotate('99,999,999원'), '99,999,999원')
        self.assertIn('(', self._annotate('100,000,000원'))

    def test_one_line_gives_up_annotation_rather_than_breaking_the_save(self):
        """병기가 200자를 넘기면 `one_line.max_length` 를 초과해 **저장이 깨진다.**

        읽기 편함보다 저장 성공이 우선이고, 그 금액은 easy_explanation 에서 다시 나온다.
        """
        # 병기 전 195자 → 병기하면 213자. 경계를 정확히 걸치게 만든다.
        long_text = '가' * 175 + ' 45,453,450,000,000원'
        self.assertLessEqual(len(long_text), verification.ONE_LINE_MAX_CHARS)
        result = summarizer.annotate_amounts(
            {'one_line': long_text, 'easy_explanation': long_text, 'why_important': ''})

        self.assertEqual(result['one_line'], long_text)           # 병기 생략
        self.assertIn('(약', result['easy_explanation'])           # 여기서는 병기
        self.assertLessEqual(
            len(result['one_line']),
            DisclosureSummary._meta.get_field('one_line').max_length)

    def test_original_dict_is_not_mutated(self):
        source = {'one_line': '500,000,000원', 'easy_explanation': '500,000,000원',
                  'why_important': '500,000,000원'}
        summarizer.annotate_amounts(source)

        self.assertEqual(source['one_line'], '500,000,000원')

    def test_truncated_notation_is_idempotent(self):
        """`(?!\\s*\\(약)` 가 막는 경로 — 절사가 일어난 표기는 두 번 적용해도 같다."""
        once = self._annotate('45,453,450,000,000원을 조달한다')
        self.assertEqual(self._annotate(once), once)


class AmountAnnotationKnownGapTest(TestCase):
    """**지금 있는 결함**을 기록한다. 아래가 실패하면 결함이 고쳐진 것이다.

    `annotate_amounts` 의 멱등성 장치는 `(?!\\s*\\(약)` 하나뿐이라 **절사가 일어난 표기만**
    막는다. 딱 떨어지는 금액(`500,000,000원` → `(5억 원)`)에는 `약` 이 안 붙으므로
    두 번째 적용에서 그대로 다시 병기된다.

    도달 경로가 실재한다 — 재생성 루프가 `correction_previous=result` 로 **이미 병기된**
    요약을 모델에게 되돌려 주고, 교정 프롬프트는 "지적되지 않은 문장은 한 글자도 바꾸지
    마라"고 지시한다. 모델이 그대로 옮기면 그 출력에 `annotate_amounts` 가 다시 걸린다.
    """

    def test_exact_amounts_are_annotated_twice_when_applied_twice(self):
        once = summarizer.annotate_amounts({'one_line': '500,000,000원을 지급한다'})
        twice = summarizer.annotate_amounts(once)

        self.assertEqual(once['one_line'], '500,000,000원(5억 원)을 지급한다')
        # ⚠ 결함: 멱등이 아니다. 고쳐지면 아래 두 줄이 하나의 assertEqual 이 된다.
        self.assertNotEqual(twice['one_line'], once['one_line'])
        self.assertEqual(twice['one_line'], '500,000,000원(5억 원)(5억 원)을 지급한다')

    def test_original_share_wording_is_misread_as_won(self):
        """`원주`(원주식)를 `원`(통화)으로 오인한다. DR 공시에 실제로 나오는 말이다."""
        self.assertEqual(
            summarizer.annotate_amounts(
                {'one_line': '177,900,000 원주를 예탁했다'})['one_line'],
            '177,900,000 원(1억 7,790만 원)주를 예탁했다',
        )


class CorrectionMessageTest(TestCase):
    """재생성 요청 메시지 — 저장된 경고는 **진단문**이고 모델에게는 **지시문**이 필요하다."""

    def test_empty_warnings_produce_no_message(self):
        """고칠 게 없는데 교정 턴을 붙이면 비용만 나간다."""
        self.assertIsNone(summarizer.build_correction_message([]))
        self.assertIsNone(summarizer.build_correction_message(None))
        self.assertIsNone(summarizer.build_correction_message(['', '   ']))

    def test_each_warning_prefix_gets_its_own_action(self):
        for prefix in (
            verification.UNSUPPORTED_NUMBER_PREFIX,
            verification.UNVERIFIED_QUOTE_PREFIX,
            verification.SENTENCE_COUNT_PREFIX,
        ):
            with self.subTest(prefix=prefix):
                message = summarizer.build_correction_message([prefix + '무엇무엇'])
                self.assertIn(summarizer.CORRECTION_ACTIONS[prefix], message)

    def test_unknown_warning_falls_back_instead_of_blanking(self):
        message = summarizer.build_correction_message(['처음 보는 경고'])
        self.assertIn(summarizer.DEFAULT_CORRECTION_ACTION, message)

    def test_actions_are_keyed_by_the_verification_constants(self):
        """문구가 아니라 상수로 이어야 경고 문구를 고쳐도 조치문이 안 끊긴다."""
        self.assertEqual(
            set(summarizer.CORRECTION_ACTIONS),
            {verification.UNSUPPORTED_NUMBER_PREFIX,
             verification.UNVERIFIED_QUOTE_PREFIX,
             verification.SENTENCE_COUNT_PREFIX},
        )

    def test_schema_only_strips_diagnostic_fields(self):
        """되먹이는 직전 출력에 우리가 붙인 진단 필드가 남으면 strict 스키마와 어긋난다."""
        stripped = summarizer.schema_only({
            'one_line': '한 줄', 'easy_explanation': '설명', 'why_important': '의미',
            'importance': 'high', 'warnings': ['경고'], 'unsupported_numbers': ['3조'],
            'evidence': [{'field': 'one_line', 'claim': '주장', 'quote': '인용',
                          'quote_found': True, 'numbers_ok': False,
                          'missing_numbers': ['3조']}],
        })

        self.assertEqual(
            set(stripped),
            {'one_line', 'easy_explanation', 'why_important', 'importance', 'evidence'})
        self.assertEqual(set(stripped['evidence'][0]), {'field', 'claim', 'quote'})


class CorrectionCachePrefixTest(TestCase):
    """교정 턴이 캐시 접두사를 깨지 않는지 — **깨져도 청구서에만 나타난다.**

    시스템 프롬프트와 원문 user 메시지가 바이트 단위로 같아야 프롬프트 캐시가 접두사
    전체에 적중한다(캐시 입력 단가는 미캐시의 1/10). 21만 토큰짜리 사업보고서 기준
    건당 $0.021 대 $0.21 의 차이라 회귀로 고정할 값이 있다.
    """

    BASE = dict(
        company_name='SK하이닉스', report_name='주요사항보고서(유상증자결정)',
        filed_at='2026-07-01', rcept_no='20260701000001',
        raw_text='유상증자 결정\n계약금액 | 1,234,567', disclosure_type='거래소공시',
    )

    def _capture(self, **kwargs):
        captured = []

        def fake_call(messages, model, max_output_tokens, reasoning_effort):
            captured.append([dict(message) for message in messages])
            return _valid_summary_payload(), FAKE_USAGE, 'gpt-5.6-luna'

        with patch.object(summarizer, '_call_openai', side_effect=fake_call):
            result = summarizer.summarize_disclosure(**self.BASE, **kwargs)
        return captured[0], result

    def test_correction_turn_only_appends_to_the_cached_prefix(self):
        first, result = self._capture()
        warnings = [verification.UNSUPPORTED_NUMBER_PREFIX + '3조 9,891억']
        second, corrected = self._capture(
            correction_warnings=warnings, correction_previous=result)

        self.assertEqual([m['role'] for m in first], ['system', 'user'])
        self.assertEqual([m['role'] for m in second],
                         ['system', 'user', 'assistant', 'user'])
        self.assertEqual(first[0]['content'], second[0]['content'])   # system 바이트 동일
        self.assertEqual(first[1]['content'], second[1]['content'])   # 원문 user 동일
        self.assertFalse(result['corrected'])
        self.assertTrue(corrected['corrected'])

    def test_empty_warnings_add_no_turn_at_all(self):
        messages, _result = self._capture(correction_warnings=[])
        self.assertEqual([m['role'] for m in messages], ['system', 'user'])

    def test_previous_output_is_optional_but_omits_the_assistant_turn(self):
        messages, _result = self._capture(
            correction_warnings=[verification.SENTENCE_COUNT_PREFIX + ': 7문장'])
        self.assertEqual([m['role'] for m in messages], ['system', 'user', 'user'])


# --- 요약 생성 명령 ---------------------------------------------------------
#
# `summarize_disclosures` 는 **돈이 나가는 유일한 경로**인데 4단계까지 테스트가 0건이었다.
# 실호출은 절대 하지 않고 `_call_openai`(HTTP 경계) 만 대역으로 바꿔 명령 전체를 태운다.

#: 원문. 아래 payload 의 인용문이 여기서 발견돼야 quote_found 가 참이 된다.
COMMAND_RAW_TEXT = '단일판매ㆍ공급계약 체결\n계약금액 | 1,234,567\n매출액 대비 | 12.34%'

#: 검증을 통과하지 못하는 payload — one_line 의 9,999,999 는 인용문에 근거가 없다.
#: 수치 경고는 게시를 막으므로 자동 미게시 경로가 켜진다.
def _blocking_summary_payload(number='9,999,999'):
    return _valid_summary_payload(
        one_line=f'삼성전자가 {number}원 규모의 공급계약을 체결했다.')


def _fake_llm(*responses):
    """`_call_openai` 대역. 문자열 하나면 매번 같은 응답을 준다."""
    queue = list(responses)

    def call(messages, model, max_output_tokens, reasoning_effort):
        body = queue.pop(0) if len(queue) > 1 else queue[0]
        return body, dict(FAKE_USAGE), 'gpt-5.6-luna'

    return call


class SummarizeDisclosuresCommandTest(TestCase):
    """요약 생성 명령 — LLM 실호출 없이 저장 결과와 게시 판정을 고정한다."""

    def setUp(self):
        sector = Sector.objects.create(name='반도체', slug='semiconductor')
        self.company = Company.objects.create(
            sector=sector, corp_code=TRACKED_CORP, stock_code='005930', name='삼성전자')
        self.disclosure = Disclosure.objects.create(
            company=self.company, rcept_no='20260701000001',
            report_name='단일판매ㆍ공급계약체결', disclosure_type='거래소공시',
            filed_at=date(2026, 7, 1), dart_url=dart_viewer_url('20260701000001'),
            selection_state=SelectionState.TARGET,
            raw_fetched=True, raw_content=COMMAND_RAW_TEXT,
        )

    def _call(self, *responses, **options):
        with patch.object(
            summarizer, '_call_openai', side_effect=_fake_llm(*responses)
        ) as mock_call:
            call_command('summarize_disclosures', **options)
        return mock_call

    def test_creates_a_summary_from_the_model_output(self):
        mock_call = self._call(_valid_summary_payload())

        self.assertEqual(mock_call.call_count, 1)
        summary = DisclosureSummary.objects.get(disclosure=self.disclosure)
        self.assertEqual(summary.importance, 'medium')
        self.assertEqual(summary.model_name, 'gpt-5.6-luna')
        self.assertEqual(summary.review_warnings, [])
        self.assertTrue(summary.is_published)
        self.assertEqual(summary.hidden_by, '')
        self.assertEqual(summary.regeneration_count, 0)
        self.assertEqual(summary.regeneration_history, [])
        # 검수 여부는 요약을 만드는 쪽에서 정하지 않는다 — 게이트와 경고가 정한다.
        self.assertFalse(summary.is_reviewed)

    def test_dry_run_never_calls_the_model(self):
        """비용 추정만 하는 경로가 실수로 호출을 하면 그대로 청구된다."""
        mock_call = self._call(_valid_summary_payload(), dry_run=True)

        mock_call.assert_not_called()
        self.assertFalse(DisclosureSummary.objects.exists())

    def test_existing_summary_is_not_resummarized(self):
        """공시당 1회 원칙(PLAN.md 11)의 실행 지점 — 여기가 새면 비용이 트래픽에 비례한다."""
        self._call(_valid_summary_payload())
        mock_call = self._call(_valid_summary_payload())

        mock_call.assert_not_called()
        self.assertEqual(DisclosureSummary.objects.count(), 1)

    def test_resummarize_overwrites_in_place(self):
        self._call(_valid_summary_payload())
        self._call(
            _valid_summary_payload(one_line='다시 만든 한 줄 요약이다.'),
            resummarize=True,
        )

        self.assertEqual(DisclosureSummary.objects.count(), 1)
        summary = DisclosureSummary.objects.get()
        self.assertEqual(summary.one_line, '다시 만든 한 줄 요약이다.')

    def test_numeric_warning_hides_the_summary_at_creation(self):
        """검증 결과가 노출에 영향을 주는 지점. 39조를 3조로 적은 요약이 경고를 단 채
        웹에 그대로 떠 있던 문제(00_input.md 2.3)를 막는 곳이다."""
        self._call(_blocking_summary_payload())

        summary = DisclosureSummary.objects.get()
        self.assertFalse(summary.is_published)
        self.assertEqual(summary.hidden_by, DisclosureSummary.HiddenBy.AUTO)
        self.assertEqual(summary.hidden_reason, verification.AUTO_HIDDEN_REASON)
        self.assertTrue(summary.auto_hidden)
        self.assertTrue(any(
            warning.startswith(verification.UNSUPPORTED_NUMBER_PREFIX)
            for warning in summary.review_warnings))

    def test_quote_only_warning_does_not_hide_the_summary(self):
        """인용 형식 문제로 멀쩡한 요약을 내리면 화면이 텅 빈다(양방향 확인)."""
        payload = _valid_summary_payload(evidence=[{
            'field': 'one_line', 'claim': '1,234,567원 규모의 공급계약',
            'quote': '원문에 없는 지어낸 인용문이다 계약금액 | 1,234,567',
        }])
        self._call(payload)

        summary = DisclosureSummary.objects.get()
        self.assertTrue(any(
            warning.startswith(verification.UNVERIFIED_QUOTE_PREFIX)
            for warning in summary.review_warnings))
        self.assertTrue(summary.is_published)
        self.assertEqual(summary.hidden_by, '')

    def test_amount_annotation_reaches_the_stored_body(self):
        """코드 환산이 실제로 저장까지 도달하는지 — 이게 5단계 변경의 사용자 접점이다."""
        payload = _valid_summary_payload(
            one_line='SK하이닉스가 45,453,450,000,000원을 조달한다.',
            easy_explanation='회사가 돈을 모은다. 45,453,450,000,000원이다. 셋째 문장이다.',
            evidence=[{'field': 'one_line', 'claim': '45,453,450,000,000원',
                       'quote': '계약금액 | 1,234,567'}],
        )
        self._call(payload)

        summary = DisclosureSummary.objects.get()
        self.assertIn('(약 45조 4,534억 원)', summary.one_line)
        self.assertIn('(약 45조 4,534억 원)', summary.easy_explanation)

    def test_summarizer_failure_is_recorded_without_killing_the_run(self):
        """건별 실패로 전체가 죽으면 남은 공시가 통째로 밀린다."""
        mock_call = self._call('{깨진 JSON')

        self.assertEqual(mock_call.call_count, summarizer.MAX_RETRIES + 1)
        self.assertFalse(DisclosureSummary.objects.exists())

    def test_invalid_limit_is_rejected_before_any_call(self):
        with self.assertRaises(CommandError):
            call_command('summarize_disclosures', limit=0)

    def test_worst_case_llm_calls_per_disclosure(self):
        """건당 LLM 호출의 **진짜 상한**을 잰다.

        backend 보고서의 '건당 최대 2회'는 `summarize_disclosure` **호출 횟수**이고,
        그 함수 안에는 검증 실패 시 도는 재시도 루프가 따로 있다(`MAX_RETRIES`).
        실제 HTTP 호출 상한은 `(1 + MAX_REGENERATION_ATTEMPTS) × (MAX_RETRIES + 1)` 이다.
        비용 상한을 이 수로 잡아야 한다.
        """
        broken = '{깨진 JSON'
        mock_call = self._call(
            broken, broken, _blocking_summary_payload(),   # 최초: 3번째에 성공(경고 있음)
            broken, broken, broken,                        # 재생성: 3번 다 실패
            regenerate=True,
        )

        expected = (1 + MAX_REGENERATION_ATTEMPTS) * (summarizer.MAX_RETRIES + 1)
        self.assertEqual(mock_call.call_count, expected)
        self.assertEqual(mock_call.call_count, 6)

        summary = DisclosureSummary.objects.get()
        self.assertEqual(summary.regeneration_count, 1)
        self.assertFalse(summary.is_published)
        self.assertIn('error', summary.regeneration_history[0])


# --- 재생성 상한 ------------------------------------------------------------
#
# **무한 재시도 = 무한 비용.** 상한이 이 파이프라인의 유일한 비용 방어선이므로
# `summarize_disclosure` 자체를 대역으로 바꿔 호출 횟수를 직접 센다.

def _regen_result(**overrides):
    """`summarize_disclosure` 가 돌려주는 모양의 dict."""
    result = {
        'one_line': '삼성전자가 1,234,567원 규모의 공급계약을 체결했다.',
        'easy_explanation': '첫 문장이다. 둘째 문장이다. 셋째 문장이다.',
        'why_important': '매출로 이어지는 계약이다.',
        'importance': 'medium',
        'model_name': 'gpt-5.6-luna',
        'evidence': [{'field': 'one_line', 'claim': '계약금액 1,234,567원',
                      'quote': '계약금액 | 1,234,567', 'quote_found': True,
                      'numbers_ok': True, 'missing_numbers': []}],
        'unsupported_numbers': [],
        'sentence_count': 3,
        'warnings': [],
        'usage': dict(FAKE_USAGE),
        'cost_usd': 0.01,
        'attempts': 1,
        'prompt_version': summarizer.PROMPT_VERSION,
        'corrected': False,
    }
    result.update(overrides)
    return result


def _fake_summarize_disclosure(outcomes):
    """`summarize_disclosure` 대역.

    **시그니처가 진짜와 같아야 한다** — 명령이 `inspect.signature` 로 재생성 지원 여부를
    판정하기 때문이다(MagicMock 을 쓰면 키워드가 없다고 판정돼 재생성이 통째로 꺼진다).
    """
    calls = []

    def fake(*, company_name, report_name, filed_at, rcept_no, raw_text,
             disclosure_type='', model=summarizer.DEFAULT_MODEL,
             reasoning_effort=summarizer.DEFAULT_REASONING_EFFORT,
             max_retries=summarizer.MAX_RETRIES,
             max_input_tokens=summarizer.MAX_INPUT_TOKENS,
             correction_warnings=None, correction_previous=None):
        calls.append({'correction_warnings': correction_warnings,
                      'correction_previous': correction_previous})
        outcome = outcomes[min(len(calls) - 1, len(outcomes) - 1)]
        if isinstance(outcome, Exception):
            raise outcome
        return dict(outcome)

    fake.calls = calls
    return fake


class RegenerationLimitTest(TestCase):
    """재생성 루프의 상한 — 이 테스트가 없으면 이 단계는 배포하면 안 된다."""

    BLOCKING = _regen_result(unsupported_numbers=['3조 9,891억'])

    def setUp(self):
        sector = Sector.objects.create(name='반도체', slug='semiconductor')
        company = Company.objects.create(
            sector=sector, corp_code=TRACKED_CORP, stock_code='005930', name='삼성전자')
        self.disclosure = Disclosure.objects.create(
            company=company, rcept_no='20260701000001',
            report_name='단일판매ㆍ공급계약체결', disclosure_type='거래소공시',
            filed_at=date(2026, 7, 1), dart_url=dart_viewer_url('20260701000001'),
            selection_state=SelectionState.TARGET,
            raw_fetched=True, raw_content=COMMAND_RAW_TEXT,
        )

    def _run(self, outcomes, **options):
        fake = _fake_summarize_disclosure(outcomes)
        with patch.object(summarize_command, 'summarize_disclosure', fake):
            call_command('summarize_disclosures', **options)
        return fake

    def test_a_model_that_never_improves_is_called_exactly_twice(self):
        """계속 실패해도 3회째가 없어야 한다. 상한이 없으면 한 건이 LLM을 무한히 부른다."""
        fake = self._run([self.BLOCKING], regenerate=True)

        self.assertEqual(len(fake.calls), 2)                  # 최초 1 + 재생성 1
        self.assertIsNone(fake.calls[0]['correction_warnings'])
        self.assertTrue(fake.calls[1]['correction_warnings'])

        summary = DisclosureSummary.objects.get()
        self.assertEqual(summary.regeneration_count, MAX_REGENERATION_ATTEMPTS)
        self.assertTrue(summary.regeneration_exhausted)
        self.assertFalse(summary.is_published)
        self.assertEqual(summary.hidden_by, DisclosureSummary.HiddenBy.AUTO)
        self.assertEqual(len(summary.regeneration_history), 1)
        self.assertFalse(summary.regeneration_history[0]['resolved'])

    def test_the_constant_is_the_only_place_the_ceiling_lives(self):
        """상수를 올리면 호출 수가 실제로 따라 올라야 단일 출처라고 말할 수 있다.

        호출부마다 상한을 다시 적어 두면 한 곳만 빠뜨려도 비용이 샌다.
        """
        with patch.object(review_policy, 'MAX_REGENERATION_ATTEMPTS', 3):
            fake = self._run([self.BLOCKING], regenerate=True)

        self.assertEqual(len(fake.calls), 4)                  # 최초 1 + 재생성 3
        self.assertEqual(DisclosureSummary.objects.get().regeneration_count, 3)

    def test_regeneration_is_off_unless_explicitly_asked(self):
        """비용이 드는 동작은 명시적으로 켤 때만 돈다."""
        fake = self._run([self.BLOCKING])

        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(DisclosureSummary.objects.get().regeneration_count, 0)

    def test_nothing_blocking_means_no_second_call(self):
        """고칠 게 없는데 부르면 그냥 돈이다."""
        fake = self._run([_regen_result()], regenerate=True)

        self.assertEqual(len(fake.calls), 1)
        self.assertTrue(DisclosureSummary.objects.get().is_published)

    def test_previous_output_is_fed_back_with_the_warnings(self):
        """경고만 넘기면 모델이 자기가 그 값을 어디에 썼는지 몰라 교정이 재작성이 된다."""
        fake = self._run([self.BLOCKING], regenerate=True)

        previous = fake.calls[1]['correction_previous']
        self.assertIsNotNone(previous)
        self.assertEqual(previous['one_line'], self.BLOCKING['one_line'])

    def test_an_exception_during_regeneration_keeps_the_first_summary(self):
        """재생성 실패가 최초 요약까지 버릴 이유는 못 된다 — 미게시로 사람에게 넘어간다."""
        fake = self._run(
            [self.BLOCKING, summarizer.SummaryValidationError('교정 응답이 깨졌다')],
            regenerate=True,
        )

        self.assertEqual(len(fake.calls), 2)                  # 예외가 나도 상한은 지켜진다
        summary = DisclosureSummary.objects.get()
        self.assertEqual(summary.one_line, self.BLOCKING['one_line'])
        self.assertFalse(summary.is_published)
        self.assertIn('error', summary.regeneration_history[0])
        self.assertNotIn('resolved', summary.regeneration_history[0])

    def test_a_successful_correction_publishes_and_records_it(self):
        clean = _regen_result(one_line='교정된 한 줄 요약이다.', corrected=True)
        fake = self._run([self.BLOCKING, clean], regenerate=True)

        self.assertEqual(len(fake.calls), 2)
        summary = DisclosureSummary.objects.get()
        self.assertEqual(summary.one_line, '교정된 한 줄 요약이다.')
        self.assertTrue(summary.is_published)
        self.assertEqual(summary.hidden_by, '')
        self.assertEqual(summary.regeneration_count, 1)
        self.assertTrue(summary.regeneration_history[0]['resolved'])
        self.assertEqual(summary.regeneration_history[0]['remaining_warnings'], [])


class RegenerationKnownGapTest(TestCase):
    """**지금 있는 결함**을 기록한다. 아래가 실패하면 결함이 고쳐진 것이다."""

    def setUp(self):
        sector = Sector.objects.create(name='반도체', slug='semiconductor')
        company = Company.objects.create(
            sector=sector, corp_code=TRACKED_CORP, stock_code='005930', name='삼성전자')
        Disclosure.objects.create(
            company=company, rcept_no='20260701000001',
            report_name='단일판매ㆍ공급계약체결', disclosure_type='거래소공시',
            filed_at=date(2026, 7, 1), dart_url=dart_viewer_url('20260701000001'),
            selection_state=SelectionState.TARGET,
            raw_fetched=True, raw_content=COMMAND_RAW_TEXT,
        )

    def test_a_strictly_worse_correction_is_still_accepted(self):
        """⚠ '교정 결과가 더 나쁘면 되돌린다'는 방어가 **발동할 수 없다.**

        되돌림 조건이 `len(retried_blocking) > len(blocking)` 인데, 게시를 막는 경고는
        `인용 근거 없는 수치: ` **한 종류뿐이고 수치 여러 개를 한 문자열로 합친다.**
        그래서 막는 경고 목록의 길이는 언제나 0 또는 1이고, 재생성은 1일 때만 도니까
        `> 1` 이 성립할 수 없다. 수치가 1개에서 4개로 늘어도 길이는 그대로 1이다.

        고치려면 경고 **문자열 수**가 아니라 `unsupported_numbers` 의 **개수**를 비교해야
        한다. 그때까지 frontend 의 `rolled_back` 배지는 절대 뜨지 않는다.
        """
        before = _regen_result(unsupported_numbers=['3조 9,891억'])
        worse = _regen_result(
            one_line='더 나빠진 요약이다.',
            unsupported_numbers=['3조 9,891억', '4조 5,453억', '1,234', '5,678'],
        )
        fake = _fake_summarize_disclosure([before, worse])
        with patch.object(summarize_command, 'summarize_disclosure', fake):
            call_command('summarize_disclosures', regenerate=True)

        summary = DisclosureSummary.objects.get()
        # ⚠ 결함: 더 나빠진 두 번째 결과가 그대로 저장된다.
        self.assertEqual(summary.one_line, '더 나빠진 요약이다.')
        self.assertNotIn('rolled_back', summary.regeneration_history[0])

    def test_a_successfully_corrected_summary_also_reports_exhausted(self):
        """⚠ `regeneration_exhausted` 가 '성공한 교정'과 '상한 소진'을 구분하지 못한다.

        `regeneration_count >= MAX_REGENERATION_ATTEMPTS` 하나로 판정하므로,
        **한 번에 고쳐져 정상 게시된 요약도 True** 가 된다. frontend 는 이 값을
        "코드가 시도해 실패 — 검수 우선순위 최상위"로 읽는다(24번 보고서 1.1).
        고치려면 상한 소진 판정에 "아직 막는 경고가 남아 있는가"를 함께 봐야 한다.
        """
        before = _regen_result(unsupported_numbers=['3조 9,891억'])
        clean = _regen_result(one_line='교정된 한 줄 요약이다.')
        fake = _fake_summarize_disclosure([before, clean])
        with patch.object(summarize_command, 'summarize_disclosure', fake):
            call_command('summarize_disclosures', regenerate=True)

        summary = DisclosureSummary.objects.get()
        self.assertTrue(summary.is_published)          # 정상 게시된 요약인데
        self.assertTrue(summary.regeneration_exhausted)  # ⚠ '자동 교정 불가'로 읽힌다


class RevalidatePublicationTest(TestCase):
    """재검증이 **기존 140건의 게시 상태까지** 되계산하는지.

    생성 경로만 고치면 이미 저장된 요약은 LLM을 다시 사기 전까지 영원히 그 상태로 남는다.
    이 명령은 LLM을 부르지 않으므로 비용이 0이고 몇 번을 돌려도 안전하다.
    """

    CLEAN_ONE_LINE = '삼성전자가 1,234,567원 규모의 공급계약을 체결했다.'
    BAD_ONE_LINE = '삼성전자가 9,999,999원 규모의 공급계약을 체결했다.'

    def setUp(self):
        sector = Sector.objects.create(name='반도체', slug='semiconductor')
        self.company = Company.objects.create(
            sector=sector, corp_code=TRACKED_CORP, stock_code='005930', name='삼성전자')

    def _summary(self, one_line, **kwargs):
        disclosure = Disclosure.objects.create(
            company=self.company, rcept_no='20260701000001',
            report_name='단일판매ㆍ공급계약체결', disclosure_type='거래소공시',
            filed_at=date(2026, 7, 1), dart_url=dart_viewer_url('20260701000001'),
            selection_state=SelectionState.TARGET,
            raw_fetched=True, raw_content=COMMAND_RAW_TEXT,
        )
        defaults = dict(
            disclosure=disclosure, one_line=one_line,
            easy_explanation='첫 문장이다. 둘째 문장이다. 셋째 문장이다.',
            why_important='매출로 이어지는 계약이다.',
            importance=DisclosureSummary.Importance.MEDIUM,
            evidence=[{'field': 'one_line', 'claim': '계약금액',
                       'quote': '계약금액 | 1,234,567'}],
        )
        defaults.update(kwargs)
        summary = DisclosureSummary.objects.create(**defaults)
        return summary

    def test_a_stored_numeric_error_is_taken_off_the_web(self):
        summary = self._summary(self.BAD_ONE_LINE)

        call_command('revalidate_summaries')

        summary.refresh_from_db()
        self.assertFalse(summary.is_published)
        self.assertEqual(summary.hidden_by, DisclosureSummary.HiddenBy.AUTO)
        self.assertEqual(summary.hidden_reason, verification.AUTO_HIDDEN_REASON)

    def test_dry_run_changes_nothing(self):
        summary = self._summary(self.BAD_ONE_LINE)

        call_command('revalidate_summaries', dry_run=True)

        summary.refresh_from_db()
        self.assertTrue(summary.is_published)
        self.assertEqual(summary.review_warnings, [])

    def test_an_auto_hidden_summary_comes_back_when_the_warning_goes_away(self):
        summary = self._summary(
            self.CLEAN_ONE_LINE, is_published=False,
            hidden_by=DisclosureSummary.HiddenBy.AUTO,
            hidden_reason=verification.AUTO_HIDDEN_REASON,
            review_warnings=[verification.UNSUPPORTED_NUMBER_PREFIX + '9,999,999'],
        )

        call_command('revalidate_summaries')

        summary.refresh_from_db()
        self.assertTrue(summary.is_published)
        self.assertEqual(summary.hidden_by, '')
        self.assertEqual(summary.hidden_reason, '')

    def test_a_human_decision_is_never_overwritten(self):
        """코드가 '경고가 사라졌으니 올리자'고 되돌리면 사람의 판단을 덮어쓴다."""
        summary = self._summary(
            self.CLEAN_ONE_LINE, is_published=False,
            hidden_by=DisclosureSummary.HiddenBy.HUMAN,
            hidden_reason='검수자가 내용이 부정확하다고 판단',
        )

        call_command('revalidate_summaries')

        summary.refresh_from_db()
        self.assertFalse(summary.is_published)
        self.assertEqual(summary.hidden_by, DisclosureSummary.HiddenBy.HUMAN)
        self.assertEqual(summary.hidden_reason, '검수자가 내용이 부정확하다고 판단')

    def test_a_human_published_summary_is_not_auto_hidden_either(self):
        """반대 방향 — 사람이 올려 둔 요약도 hidden_by 가 human 이면 건드리지 않는다."""
        summary = self._summary(
            self.BAD_ONE_LINE, hidden_by=DisclosureSummary.HiddenBy.HUMAN)

        call_command('revalidate_summaries')

        summary.refresh_from_db()
        self.assertTrue(summary.is_published)

    def test_revalidation_is_idempotent(self):
        summary = self._summary(self.BAD_ONE_LINE)

        call_command('revalidate_summaries')
        summary.refresh_from_db()
        first = (summary.review_warnings, summary.is_published, summary.hidden_by)

        call_command('revalidate_summaries')
        summary.refresh_from_db()
        self.assertEqual(
            (summary.review_warnings, summary.is_published, summary.hidden_by), first)

    def test_revalidation_never_calls_the_model(self):
        self._summary(self.BAD_ONE_LINE)

        with patch.object(summarizer, '_call_openai') as mock_call:
            call_command('revalidate_summaries')

        mock_call.assert_not_called()
