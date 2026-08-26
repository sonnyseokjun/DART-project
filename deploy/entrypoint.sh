#!/bin/sh
# 컨테이너 시작 시 마이그레이션을 먼저 적용한다.
# 쓰기 주체가 하나뿐이라(PLAN.md 9.2) 여러 컨테이너가 동시에 migrate를 돌 걱정이 없다.
# 실패하면 여기서 멈춘다 — 스키마가 안 맞는 채로 서비스가 뜨는 것보다 낫다.
set -e

echo "[entrypoint] migrate 적용"
python manage.py migrate --noinput

echo "[entrypoint] gunicorn 기동"
exec "$@"
