"""데이터 수집·선별·원문 확보 파이프라인 회귀 테스트.

DART 실호출 없이(네트워크·API 키 불필요) 동작을 고정한다.
seed_companies는 download_corp_codes를, poll_dart는 iter_disclosures를,
fetch_documents는 fetch_document를 목으로 대체한다.
"""
import json
from datetime import date, timedelta
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.urls import reverse

from disclosures.dart import (
    MAX_LIST_SPAN_DAYS, OFFERING_KEY_SECTIONS, PBLNTF_TYPES, PERIODIC_KEY_SECTIONS,
    DartApiError, dart_viewer_url, extract_key_sections, is_dart_xml,
    preprocess_document, split_date_range, strip_markup,
)
from disclosures.management.commands.seed_companies import TARGET_COMPANIES
from disclosures.models import Company, Disclosure, DisclosureSummary, Sector
from disclosures import summarizer, views
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

    def test_needs_review_flags_high_importance_and_warnings(self):
        summary = self._summary(importance=DisclosureSummary.Importance.HIGH)
        self.assertTrue(summary.needs_review)          # 중요도 높음 → 검수 게이트

        summary.importance = DisclosureSummary.Importance.LOW
        summary.review_warnings = ['evidence[0]: 인용문이 원문에서 발견되지 않음']
        self.assertTrue(summary.needs_review)          # 경고 있음 → 검수 필요

        summary.review_warnings = []
        self.assertFalse(summary.needs_review)         # 경고 없고 중요도 낮음

        summary.importance = DisclosureSummary.Importance.HIGH
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
