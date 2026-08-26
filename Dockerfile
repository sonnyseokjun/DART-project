# 서울 리전 Lightsail(Ubuntu 24.04, x86_64)에서 돌린다.
# 개발 머신이 ARM(Apple Silicon)이면 빌드 시 --platform linux/amd64를 붙일 것.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Seoul

WORKDIR /app

# tzdata: TIME_ZONE='Asia/Seoul'을 쓰므로 필요하다. slim 이미지에는 없다.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

# 의존성을 먼저 복사해 레이어 캐시를 살린다 — 코드만 바뀌면 pip를 다시 돌리지 않는다.
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

COPY . .

# 정적 파일을 빌드 시점에 모은다. 템플릿이 없는 파일을 참조하면 여기서 깨지므로
# 배포 후가 아니라 빌드 중에 알 수 있다.
# SECRET_KEY는 빌드 전용 더미다 — 이미지에 남아도 런타임에는 .env 값으로 덮인다.
RUN DJANGO_STATIC_MANIFEST=true \
    DJANGO_SECRET_KEY=build-time-only-not-used-at-runtime \
    python manage.py collectstatic --noinput

# 루트로 돌리지 않는다. 볼륨으로 마운트할 /app/data를 미리 만들어 소유권을 준다
# (마운트 시점에 없으면 도커가 root 소유로 만들어 쓰기가 막힌다).
# chmod: Windows에서 개발하면 git이 실행 권한을 기록하지 않는다(core.filemode=false).
# 이미지 안에서 다시 붙여야 엔트리포인트가 돈다.
RUN useradd --create-home --uid 1000 app \
    && mkdir -p /app/data \
    && chmod +x /app/deploy/entrypoint.sh \
    && chown -R app:app /app
USER app

EXPOSE 8000

# migrate를 먼저 돌리고 gunicorn을 exec한다.
ENTRYPOINT ["/app/deploy/entrypoint.sh"]

# 워커 2개: 실측 웹 요청 경로 메모리가 45MB라 1GB 인스턴스에 넉넉히 들어간다(PLAN.md 9.2).
# 로그는 stdout/stderr로 보내 docker logs로 본다.
CMD ["gunicorn", "config.wsgi:application", \
     "--bind", "0.0.0.0:8000", \
     "--workers", "2", \
     "--timeout", "60", \
     "--access-logfile", "-", \
     "--error-logfile", "-"]
