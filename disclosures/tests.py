"""1단계 데이터 수집 파이프라인 회귀 테스트.

DART 실호출 없이(네트워크·API 키 불필요) 동작을 고정한다.
seed_companies는 download_corp_codes를, poll_dart는 iter_disclosures를 목으로 대체한다.
"""
from datetime import date
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from disclosures.dart import PBLNTF_TYPES, dart_viewer_url
from disclosures.management.commands.seed_companies import TARGET_COMPANIES
from disclosures.models import Company, Disclosure, Sector


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
