"""DART OpenAPI 클라이언트.

모든 함수는 settings.DART_API_KEY 를 사용한다.
공식 가이드: https://opendart.fss.or.kr/guide/main.do?apiGrpCd=DS001
"""
import html
import io
import re
import zipfile
import xml.etree.ElementTree as ET
from datetime import timedelta

import requests
from django.conf import settings

BASE_URL = 'https://opendart.fss.or.kr/api'

# list.json status 코드 중 "정상" 및 "조회 결과 없음"
STATUS_OK = '000'
STATUS_NO_DATA = '013'

# corp_code 없이 list.json을 조회하면 검색기간이 3개월로 제한된다.
#   DART API 오류 [100] corp_code가 없는 경우 검색기간은 3개월만 가능합니다.
# 실측 경계(end_de=20260726 기준): 89일 통과 / 92일 실패 / 95일 실패.
# 따라서 (end_de - bgn_de)를 89일 이하로 유지해야 한다. 정상 폴링(며칠 범위)은 한도에 닿지
# 않지만, 백필·장애 복구로 긴 구간을 훑을 때는 split_date_range()로 분할해야 한다.
MAX_LIST_SPAN_DAYS = 89

# 공시유형(pblntf_ty) 코드 → 명칭.
# list.json 응답 항목에는 공시유형 필드가 없다. pblntf_ty는 요청 필터 파라미터일 뿐이므로,
# 유형별로 나눠 조회해 각 공시에 유형을 태깅한다(유형은 전체 공시를 분할하므로 호출 오버헤드는 작다).
PBLNTF_TYPES = {
    'A': '정기공시',
    'B': '주요사항보고',
    'C': '발행공시',
    'D': '지분공시',
    'E': '기타공시',
    'F': '외부감사관련',
    'G': '펀드공시',
    'H': '자산유동화',
    'I': '거래소공시',
    'J': '공정위공시',
}


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


def list_disclosures(bgn_de, end_de, corp_code=None, pblntf_ty=None, page_no=1,
                     page_count=100, sort=None, sort_mth=None):
    """list.json 1페이지 조회. 응답 dict를 그대로 반환한다.

    corp_code 없이 호출하면 시장 전체 공시를 날짜 범위로 받는다(확장 전략).
    pblntf_ty(PBLNTF_TYPES의 코드)를 주면 해당 공시유형만 조회한다.
    sort/sort_mth는 정렬 기준·방향이다(`date`/`desc` 등). 생략하면 DART 기본값을 따른다.
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
    if pblntf_ty:
        params['pblntf_ty'] = pblntf_ty
    if sort:
        params['sort'] = sort
    if sort_mth:
        params['sort_mth'] = sort_mth
    resp = requests.get(f'{BASE_URL}/list.json', params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if data['status'] not in (STATUS_OK, STATUS_NO_DATA):
        raise DartApiError(data['status'], data.get('message', ''))
    return data


def iter_disclosures(bgn_de, end_de, corp_code=None, pblntf_ty=None):
    """list.json 전체 페이지를 순회하며 공시 항목을 하나씩 yield."""
    page_no = 1
    while True:
        data = list_disclosures(
            bgn_de, end_de, corp_code=corp_code, pblntf_ty=pblntf_ty, page_no=page_no
        )
        yield from data.get('list', [])
        if page_no >= int(data.get('total_page', 1) or 1):
            break
        page_no += 1


#: 감지용 1회 조회로 훑는 최신 공시 건수. list.json page_count 상한이 100이다.
DETECT_PAGE_COUNT = 100


def latest_disclosures(bgn_de, end_de, page_count=DETECT_PAGE_COUNT):
    """가장 최근 공시 page_count건만 **1회 호출로** 받아 리스트로 반환한다.

    유형별로 나눠 훑는 iter_disclosures()와 달리 페이지네이션도 유형 분할도 하지 않는다.
    "새로 올라온 게 있는가"만 알면 되는 감지 경로(`poll_dart --detect`)를 위한 것이다 —
    2일 창을 유형별로 전부 훑으면 30회 넘게 부르지만, 이쪽은 언제나 1회다.

    정렬을 명시하는 이유: DART 기본 응답도 최신순이지만(2026-09-01 실측) 문서에 보장이
    없다. 오름차순으로 뒤집히면 이 함수는 **가장 오래된 100건**을 보게 되어 신규를
    영영 못 잡는다. 조용히 틀리는 종류의 실패라 명시적으로 못박는다.

    ## 놓칠 수 있는 경우

    호출 사이에 시장 전체 공시가 page_count건을 넘어서면 창 밖으로 밀려난 신규는 보이지
    않는다. 시장 전체가 하루 약 1,100건(2026-09-01 실측)이라 1분 주기에서는 여유가 크지만,
    폴링이 몇 시간 멈췄다 재개하면 발생할 수 있다. **하루 1회 도는 전체 폴링이 최종
    안전망**이므로 이 함수만으로 수집 완결성을 보장하지 않는다.
    """
    data = list_disclosures(
        bgn_de, end_de, page_no=1, page_count=page_count,
        sort='date', sort_mth='desc',
    )
    return data.get('list', [])


def split_date_range(bgn, end, max_span_days=MAX_LIST_SPAN_DAYS):
    """[bgn, end] 구간(date)을 검색기간 한도 이하의 창으로 분할해 [(bgn, end), ...]을 반환.

    각 창의 종료일이 다음 창의 시작일이 되어 **경계일 하루가 두 창에 겹쳐** 조회된다.
    list.json은 bgn_de·end_de를 모두 포함해 조회하므로 창을 딱 붙여 나눠도(다음 창을
    종료일+1일부터 시작) 이론상 누락은 없다. 그럼에도 하루를 겹치는 이유는, 경계 계산이
    하루라도 어긋나면 그 날짜의 공시가 영구히 누락되는 반면, 중복 조회는 `rcept_no` unique
    제약과 `get_or_create` 덕에 아무 부작용이 없기 때문이다(비용 = 창 수 - 1일치 재조회).
    """
    if bgn > end:
        raise ValueError(f'시작일({bgn})이 종료일({end})보다 늦습니다.')
    if max_span_days < 1:
        raise ValueError('max_span_days는 1 이상이어야 합니다.')

    chunks = []
    chunk_bgn = bgn
    while True:
        chunk_end = min(chunk_bgn + timedelta(days=max_span_days), end)
        chunks.append((chunk_bgn, chunk_end))
        if chunk_end >= end:
            return chunks
        chunk_bgn = chunk_end  # 경계일을 겹쳐 누락을 원천 차단


def fetch_document(rcept_no):
    """document.xml로 공시 원문 텍스트를 반환.

    **인코딩은 UTF-8이다(실측 확인).** 다만 함정이 있다: 거래소공시류 HTML 원문은 헤더에
    `<meta ... charset=euc-kr>`로 **잘못 선언**하지만 실제 바이트는 UTF-8이다. 선언된
    charset을 믿고 EUC-KR로 디코딩하면 한글이 전부 깨진다. 선언을 무시하고 UTF-8로 고정한다.

    반환 포맷은 두 가지다(is_dart_xml()로 구분):
      - 거래소공시류: `<html>` HTML 문서
      - 정기공시·주요사항보고류: `<DOCUMENT>` DART 전용 XML (dart4.xsd)
    둘 다 preprocess_document()가 처리한다.
    """
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


# --- 원문 전처리 --------------------------------------------------------
#
# LLM 입력 토큰을 줄이는 것이 목적이다. 마크업이 원문의 90% 이상을 차지한다
# (분기보고서 실측: 4,318,370자 → 태그 제거 후 394,333자).
# 표의 수치는 요약의 숫자 대조에 반드시 필요하므로 셀 구분자를 남긴 채 텍스트로 보존한다.

# 내용이 통째로 버려져야 하는 요소(스크립트·스타일·주석)
_DROP_BLOCK_RE = re.compile(
    r'<(script|style)\b[^>]*>.*?</\1>|<!--.*?-->', re.S | re.I
)
# 표 셀 종료 → 구분자. DART XML은 TD/TH 외에 TU(단위값)·TE(추출값) 셀을 쓴다.
_CELL_END_RE = re.compile(r'</(td|th|tu|te)\s*>', re.I)
# 줄이 바뀌어야 하는 블록 요소
_LINE_BREAK_RE = re.compile(
    r'</(tr|p|div|table|title|section-[123]|cover-title|li|h[1-6])\s*>'
    r'|<(br|pgbrk)\b[^>]*/?>',
    re.I,
)
_TAG_RE = re.compile(r'<[^>]+>')
# 줄바꿈 포함 모든 공백(전각·비단절 공백 포함)
_WHITESPACE_RE = re.compile(r'[\s 　]+')
# 줄 양 끝에 남은 셀 구분자·공백
_TRIM_SEPARATOR_RE = re.compile(r'^[|\s]+|[|\s]+$')
_SPACES_RE = re.compile(r'[ \t 　]+')
_BLANK_LINES_RE = re.compile(r'\n{3,}')
# 표에서 빈 셀만 남은 줄(`| | |`)은 정보가 없다
_EMPTY_ROW_RE = re.compile(r'^[|\s]*$')


def is_dart_xml(text):
    """DART 전용 XML(`<DOCUMENT>`) 원문인지. 아니면 HTML 원문으로 본다."""
    return '<DOCUMENT' in text[:2000].upper()


def strip_markup(text):
    """HTML·DART XML 공통 태그 제거 + 공백 정규화.

    표는 셀을 ` | `로, 행을 줄바꿈으로 바꿔 텍스트 표로 남긴다. 셀을 그냥 이어붙이면
    `1,234`와 `5,678`이 `1,2345,678`로 붙어 숫자 대조가 불가능해진다.
    """
    text = _DROP_BLOCK_RE.sub(' ', text)
    # 원문의 줄바꿈은 마크업 들여쓰기일 뿐이라 의미가 없다. 먼저 모두 공백으로 눌러야
    # `<TD>값</TD>\n<TE>값</TE>` 같은 태그 사이 줄바꿈이 표의 한 행을 쪼개지 않는다.
    # 이후 문서 구조는 오직 태그로만 복원한다.
    text = _WHITESPACE_RE.sub(' ', text)
    text = _CELL_END_RE.sub(' | ', text)
    text = _LINE_BREAK_RE.sub('\n', text)
    text = _TAG_RE.sub('', text)
    text = html.unescape(text)

    lines = []
    for line in text.split('\n'):
        line = _TRIM_SEPARATOR_RE.sub('', _SPACES_RE.sub(' ', line))
        if not line or _EMPTY_ROW_RE.match(line):
            continue
        lines.append(line)
    return _BLANK_LINES_RE.sub('\n\n', '\n'.join(lines))


#: 정기공시(분기·사업보고서)에서 남길 섹션.
#: {SECTION-1 제목 접두어: None(통째로) | (SECTION-2 제목 접두어, ...)}
#:
#: 선정 근거 — 분기보고서 표본(20260515002181, 삼성전자) 실측 4,318,370자 중
#: `III. 재무에 관한 사항`이 2,938,451자(68%), `VIII. 임원 및 직원 등`이 858,393자(20%)다.
#: 앞은 연결·별도 재무제표 주석, 뒤는 임직원 명부/보수 표로, 일반인용 요약에 쓰이지 않으면서
#: 토큰만 잡아먹는다. 반대로 요약에 필요한 "무슨 사업을 얼마나 팔아 얼마를 벌었나"는
#: 아래 여섯 갈래에 모여 있다.
#: 버리는 것: 회사 개요·연혁, 주주 현황, 임원 및 직원 명부, 계열회사 현황, 재무제표 주석, 상세표.
#: 전부 상용구·명부성 자료라 요약에 쓰이지 않으면서 토큰의 대부분을 차지한다.
PERIODIC_KEY_SECTIONS = {
    'III.재무에관한사항': (
        '1.요약재무정보',                    # 매출·영업이익·자산·부채 요약표 (주석 제외)
        '6.배당에관한사항',                  # 주주 환원
    ),
    'II.사업의내용': (
        '1.사업의개요',                      # 사업 구조 서술
        '4.매출및수주상황',                  # 매출 구성·수주 잔고
        '2.주요제품및서비스',                # 제품군·가격 추이
        # 원가 구조·설비 투자. 비용만 보면 뺄 후보로 보이지만(이 섹션 하나가 정기공시
        # 건당 7,530 → 11,424토큰) 빼지 말 것. 반도체는 설비 투자와 원가 구조가 실적
        # 변동의 핵심 동인이고, 추적 대상 10곳이 전부 반도체다. 생산능력·가동률·원재료
        # 가격 추이가 없으면 요약의 "왜 중요한가"를 원문 근거로 쓸 수 없어, 모델이
        # 근거 없이 추측할 위험이 오히려 커진다. 비용 차이는 141건 총액의 3%다.
        '3.원재료및생산설비',
    ),
    'IV.이사의경영진단및분석의견': None,      # 경영진의 실적 해설 (2.1K)
}

#: 발행공시(증권신고서·투자설명서)에서 남길 섹션.
#:
#: 선정 근거 — 증권신고서 표본(20260624000511, SK하이닉스 유상증자) 실측 4,157,032자.
#: 이 서식은 **제1부(모집·매출에 관한 사항) + 제2부(발행인에 관한 사항)** 구조이고,
#: 제2부가 사업보고서 전체를 그대로 반복한다(제2부만 3,400,000자 이상, `III. 재무에 관한 사항`
#: 1,607,147자). 증권신고서 요약에서 독자가 알아야 할 것은 "얼마를, 어떤 조건으로,
#: 무엇에 쓰려고 발행하나"와 "무엇이 위험한가"이고, 그건 전부 제1부에 있다. 제2부는 버린다.
#:
#: 로마 숫자가 제1부·제2부에서 중복되지만(`I. 모집 또는 매출에 관한 일반사항` vs
#: `I. 회사의 개요`), 제목 전체로 접두어 매칭하므로 서로 구분된다.
OFFERING_KEY_SECTIONS = {
    '1.핵심투자위험': None,                  # 금감원이 요구하는 평이체 위험 요약 (43K)
    'I.모집또는매출에관한일반사항': None,      # 공모 규모·방법·가격 결정 (40K)
    'V.자금의사용목적': None,                # 조달 자금의 용처 (98K)
}

#: 공시유형 → 섹션 맵. 여기 없는 유형은 원문 전체를 정제한다.
KEY_SECTIONS_BY_TYPE = {
    '정기공시': PERIODIC_KEY_SECTIONS,
    '발행공시': OFFERING_KEY_SECTIONS,
}

_SECTION_RE_CACHE = {}


def _section_re(level):
    if level not in _SECTION_RE_CACHE:
        _SECTION_RE_CACHE[level] = re.compile(
            rf'<SECTION-{level}\b[^>]*>(.*?)</SECTION-{level}>', re.S | re.I
        )
    return _SECTION_RE_CACHE[level]


_TITLE_RE = re.compile(r'<TITLE\b[^>]*>(.*?)</TITLE>', re.S | re.I)
_NORMALIZE_RE = re.compile(r'\s+')


def _section_title(section_xml):
    """섹션 첫 `<TITLE>`의 텍스트를 공백 제거해 반환(제목 비교용)."""
    match = _TITLE_RE.search(section_xml)
    if not match:
        return ''
    return _NORMALIZE_RE.sub('', html.unescape(_TAG_RE.sub('', match.group(1))))


def extract_key_sections(xml_text, key_sections):
    """`<DOCUMENT>` XML에서 요약에 필요한 섹션만 골라 이어붙인다.

    분기보고서·증권신고서는 정제해도 20만~40만 토큰대라 통짜로 LLM에 넣을 수 없다
    (일반 공시 수백 건분). key_sections에 열거한 SECTION-1/SECTION-2만 남겨
    규모를 두 자릿수 배로 줄인다.

    해당 섹션을 하나도 찾지 못하면(형식이 다른 보고서) None을 반환한다 —
    호출자는 원문 전체 정제로 안전하게 되돌아갈 수 있다.
    """
    picked = []
    for section in _section_re(1).findall(xml_text):
        title = _section_title(section)
        matched = next(
            (k for k in key_sections if title.startswith(k)), None
        )
        if matched is None:
            continue
        sub_prefixes = key_sections[matched]
        if sub_prefixes is None:
            picked.append(section)
            continue
        for subsection in _section_re(2).findall(section):
            if _section_title(subsection).startswith(tuple(sub_prefixes)):
                picked.append(subsection)
    return '\n'.join(picked) if picked else None


def preprocess_document(raw_text, disclosure_type=None):
    """원문 텍스트를 LLM 입력용 평문으로 정제한다.

    disclosure_type이 KEY_SECTIONS_BY_TYPE에 있는 대형 서식(정기공시·발행공시)이면
    핵심 섹션만 먼저 추린 뒤 태그를 제거한다. 나머지 유형은 원문 전체를 정제한다.
    """
    key_sections = KEY_SECTIONS_BY_TYPE.get(disclosure_type)
    if key_sections and is_dart_xml(raw_text):
        sections = extract_key_sections(raw_text, key_sections)
        if sections is not None:
            raw_text = sections
    return strip_markup(raw_text)
