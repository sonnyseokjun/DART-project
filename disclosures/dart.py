"""DART OpenAPI 클라이언트.

모든 함수는 settings.DART_API_KEY 를 사용한다.
공식 가이드: https://opendart.fss.or.kr/guide/main.do?apiGrpCd=DS001
"""
import io
import zipfile
import xml.etree.ElementTree as ET

import requests
from django.conf import settings

BASE_URL = 'https://opendart.fss.or.kr/api'

# list.json status 코드 중 "정상" 및 "조회 결과 없음"
STATUS_OK = '000'
STATUS_NO_DATA = '013'


class DartApiError(Exception):
    def __init__(self, status, message):
        self.status = status
        super().__init__(f'DART API 오류 [{status}] {message}')


def _api_key():
    key = settings.DART_API_KEY
    if not key:
        raise DartApiError('---', 'DART_API_KEY가 설정되지 않았습니다. .env 파일을 확인하세요.')
    return key


def download_corp_codes():
    """corpCode.xml ZIP을 내려받아 [{corp_code, corp_name, stock_code}] 목록으로 반환.

    상장사만 필요하므로 stock_code가 있는 항목만 남긴다.
    """
    resp = requests.get(
        f'{BASE_URL}/corpCode.xml', params={'crtfc_key': _api_key()}, timeout=60
    )
    resp.raise_for_status()
    content_type = resp.headers.get('Content-Type', '')
    if 'xml' in content_type and b'status' in resp.content[:200]:
        root = ET.fromstring(resp.content)
        raise DartApiError(root.findtext('status'), root.findtext('message'))

    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        xml_bytes = zf.read(zf.namelist()[0])

    root = ET.fromstring(xml_bytes)
    result = []
    for corp in root.iter('list'):
        stock_code = (corp.findtext('stock_code') or '').strip()
        if not stock_code:
            continue
        result.append({
            'corp_code': corp.findtext('corp_code').strip(),
            'corp_name': corp.findtext('corp_name').strip(),
            'stock_code': stock_code,
        })
    return result


def list_disclosures(bgn_de, end_de, corp_code=None, page_no=1, page_count=100):
    """list.json 1페이지 조회. 응답 dict를 그대로 반환한다.

    corp_code 없이 호출하면 시장 전체 공시를 날짜 범위로 받는다(확장 전략).
    """
    params = {
        'crtfc_key': _api_key(),
        'bgn_de': bgn_de,
        'end_de': end_de,
        'page_no': page_no,
        'page_count': page_count,
    }
    if corp_code:
        params['corp_code'] = corp_code
    resp = requests.get(f'{BASE_URL}/list.json', params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if data['status'] not in (STATUS_OK, STATUS_NO_DATA):
        raise DartApiError(data['status'], data.get('message', ''))
    return data


def iter_disclosures(bgn_de, end_de, corp_code=None):
    """list.json 전체 페이지를 순회하며 공시 항목을 하나씩 yield."""
    page_no = 1
    while True:
        data = list_disclosures(bgn_de, end_de, corp_code=corp_code, page_no=page_no)
        yield from data.get('list', [])
        if page_no >= int(data.get('total_page', 1)):
            break
        page_no += 1


def fetch_document(rcept_no):
    """document.xml로 공시 원문 XML 텍스트를 반환."""
    resp = requests.get(
        f'{BASE_URL}/document.xml',
        params={'crtfc_key': _api_key(), 'rcept_no': rcept_no},
        timeout=60,
    )
    resp.raise_for_status()
    if resp.content[:2] == b'PK':  # 정상 응답은 ZIP
        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            return zf.read(zf.namelist()[0]).decode('utf-8', errors='replace')
    root = ET.fromstring(resp.content)
    raise DartApiError(root.findtext('status'), root.findtext('message'))


def dart_viewer_url(rcept_no):
    """사용자에게 노출할 DART 원문 열람 URL."""
    return f'https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept_no}'
