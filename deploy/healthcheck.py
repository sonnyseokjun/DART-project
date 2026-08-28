"""컨테이너 헬스체크.

127.0.0.1로 접속하되 Host 헤더는 SITE_DOMAIN으로 보낸다.
루프백을 ALLOWED_HOSTS에 추가하는 방법도 있지만, 그러면 Host 헤더 검사를
그만큼 헐겁게 만드는 셈이라 택하지 않았다. 헬스체크 쪽을 맞추는 게 옳다.
"""
import os
import sys
import urllib.request

host = os.environ.get('SITE_DOMAIN') or '127.0.0.1'
req = urllib.request.Request('http://127.0.0.1:8000/', headers={'Host': host})

try:
    with urllib.request.urlopen(req, timeout=5) as res:
        sys.exit(0 if res.status < 500 else 1)
except Exception as exc:
    print(f'헬스체크 실패: {exc}', file=sys.stderr)
    sys.exit(1)
