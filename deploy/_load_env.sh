# .env에서 필요한 값만 읽어 export 한다. 다른 스크립트가 source 해서 쓴다.
#
# `set -a; source .env`를 쓰면 안 된다. .env의 값은 따옴표 없이 적히는데
# DJANGO_SECRET_KEY에는 ( ) $ # & * 같은 문자가 들어간다. 셸이 이걸 문법으로
# 해석해서, 운이 좋으면 값이 잘리고 나쁘면 문법 오류로 스크립트가 죽는다.
# 여기서는 =의 첫 등장만 기준으로 잘라 값을 그대로 넘긴다 — 셸 확장이 없다.
#
# 읽는 키를 화이트리스트로 제한한 것도 의도적이다. API 키를 백업 프로세스의
# 환경에 올릴 이유가 없다.

_load_env() {
    local file="${1:-.env}"
    local line key value

    [ -f "$file" ] || { echo "$file 이 없습니다" >&2; return 1; }

    while IFS= read -r line || [ -n "$line" ]; do
        case "$line" in
            '#'*|'') continue ;;
        esac
        key=${line%%=*}
        value=${line#*=}
        value=${value%$'\r'}    # Windows에서 편집된 .env 대비
        case "$key" in
            S3_BACKUP_BUCKET|AWS_ACCESS_KEY_ID|AWS_SECRET_ACCESS_KEY|AWS_DEFAULT_REGION)
                export "$key=$value"
                ;;
        esac
    done < "$file"
}
