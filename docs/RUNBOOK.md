# 운영 RUNBOOK — DART 공시 요약 서비스

배포 구성과 그 근거는 **PLAN.md 9.2**에 있다. 이 문서는 "무엇을 어떻게 하는가"만 다룬다.

## 운영 원칙 (먼저 읽을 것)

1. **서버에 SSH로 접속해 파일을 직접 고치지 않는다.**
   모든 설정 변경은 저장소에 커밋 → `git pull` → `docker compose up -d --build`.
   이게 지켜져야 아래 "재구축" 절차가 실제로 성립한다. 서버에만 있는 수정이 하나라도
   생기는 순간 백업은 반쪽이 된다. 예외는 `.env` 하나뿐이다(비밀 값이라 커밋할 수 없다).
2. **복원을 실제로 해본다.** 복원해본 적 없는 백업은 백업이 아니다.
3. **`./data` 마운트를 확인한다.** 빠지면 재배포 때 요약이 통째로 사라진다.

---

## 0. 로컬에서 먼저 확인하기 (AWS 없이)

서버에 올리기 전에 운영 설정(`DEBUG=false`) 그대로 로컬에서 돌려본다.
도메인이 없어 Caddy는 인증서를 못 받으므로 web만 띄운다.

```bash
mkdir -p data && cp db.sqlite3 data/db.sqlite3      # 개발 DB 사본으로 확인
docker compose -f docker-compose.yml -f docker-compose.local.yml up -d --build web
# http://127.0.0.1:8000/

docker compose -f docker-compose.yml -f docker-compose.local.yml exec web python manage.py test disclosures
docker compose -f docker-compose.yml -f docker-compose.local.yml down
```

Caddyfile은 컨테이너를 띄우지 않고도 문법을 검사할 수 있다.

```bash
docker run --rm -v "$PWD/Caddyfile:/etc/caddy/Caddyfile:ro"     -e SITE_DOMAIN=example.duckdns.org -e ADMIN_ALLOWED_IP=203.0.113.7     caddy:2-alpine caddy validate --config /etc/caddy/Caddyfile
```

**2026-08-26 실측 (Windows / Docker Desktop)**

| 항목 | 값 |
|---|---|
| 이미지 크기 | 364MB |
| 컨테이너 메모리 (gunicorn 2워커 유휴) | **107.6MB** |
| 테스트 | 333건 통과 |
| 정적 파일 | 해시 파일명(`style.39e2cff7121e.css`) 정상 서빙 |
| admin IP 제한 | 허용목록 밖 → 404 / 안 → 200 (양쪽 확인) |

PLAN.md 9.2의 상시 메모리 예산 445MB는 Caddy(~30MB)와 OS를 포함한 값이다.

**2026-08-27 실측 (Lightsail 1GB · Ubuntu 24.04 · 배포 직후 유휴)**

| 항목 | 값 |
|---|---|
| 호스트 메모리 | **549MB 사용 / 911MB 가용** (스왑 2.0GB 중 76MB) |
| 컨테이너 | web **113MB** + Caddy **26MB** |
| DB 파일 | 5.8MB (`loaddata`로 새로 채워 로컬 10.7MB보다 조밀하다) |
| 백업 (gzip) | 1.06MB |
| 데이터 | 기업 10 · 공시 963 · 요약 140 (게시중 140) |
| 인증서 | Let's Encrypt, 2026-11-25 만료 |
| 백업 무결성 | `PRAGMA integrity_check` = ok, 건수 일치 확인 |

호스트가 예산보다 약 100MB 무겁다. 컨테이너는 예상대로이므로 원인은 OS 쪽이다.
요약 피크를 더하면 약 706MB(78%)로, 여유는 있으나 예산만큼 넉넉하지는 않다.

**2026-08-28 실측 (cron 무인 완주 1회차)**

| 항목 | 값 |
|---|---|
| 파이프라인 | 07:00 수집 6건 → 07:10 선별 → 07:20 원문 3/4 → 07:30 요약 3건 |
| 요약 비용 | **$0.0274** (건당 $0.0091 · 캐시 적중 56%) |
| 호스트 메모리 | 07:00 **501MB** / 07:30 **491MB** 사용 (가용 409~420MB) |
| 데이터 | 공시 972 · 요약 144 (게시중 144) |
| 백업 | `db_20260828_040001.sqlite3.gz` S3 업로드 성공 |

메모리는 전날 549MB보다 안정됐다. **단 요약 피크는 잡히지 않았다** — 샘플러가
07:30:01에 찍는데 요약 배치가 1분 안에 끝나기 때문이다. 30분 간격으로는 짧은 배치의
피크를 구조적으로 볼 수 없으므로, 확인하려면 요약 창에서만 조밀하게 찍어야 한다(4장).

---

## 1. 구성 개요

```
        인터넷
          │  443 (HTTPS)
    ┌─────▼──────────────────────────────┐
    │  Lightsail 1GB · Ubuntu 24.04      │
    │                                    │
    │  ┌────────┐   컴포즈 네트워크        │
    │  │ caddy  │──────► web:8000        │
    │  └────────┘        ┌─────────────┐ │
    │   인증서 자동        │ gunicorn ×2 │ │
    │   /admin IP 제한    │  Django     │ │
    │                    └──────┬──────┘ │
    │                           │        │
    │                    ./data/db.sqlite3 (마운트)
    │                           ▲        │
    │  호스트 cron ─────────────┘        │
    │   07:00 수집 → 요약                 │
    │   04:00 backup.sh → S3             │
    └────────────────────────────────────┘
```

호스트 포트로 열리는 것은 22 · 80 · 443뿐이다. gunicorn은 컴포즈 네트워크 안에만 있다.

---

## 2. 최초 구축

### 2.1 Lightsail 인스턴스

- 리전 **서울(ap-northeast-2)**, 블루프린트 **Ubuntu 24.04 LTS**, 플랜 **1GB / 2vCPU / 40GB**
- **고정 IP(Static IP)** 생성 후 인스턴스에 연결 — 재부팅해도 IP가 바뀌지 않는다.
  인스턴스를 지울 때 고정 IP도 함께 해제할 것. 연결이 끊긴 고정 IP는 과금된다.
- 방화벽(네트워킹 탭) 규칙 4개:

  | 애플리케이션 | 프로토콜 | 포트 | 허용 대상 |
  |---|---|---|---|
  | HTTP | TCP | 80 | 전체 (인증서 발급 검증·리다이렉트에 필요하다) |
  | HTTPS | TCP | 443 | 전체 |
  | 사용자 지정 | **UDP** | 443 | 전체 (HTTP/3) |
  | SSH | TCP | 22 | **내 노트북 IP만** (`curl https://checkip.amazonaws.com`) |

  SSH 규칙에서 **`Lightsail 브라우저 SSH/RDP 허용`을 함께 켠다.** 가정용 회선은 IP가
  재할당되므로 언젠가 SSH가 막히는데, 그때 콘솔의 브라우저 SSH로 들어가 규칙을 고칠 수 있다.
  (현재 콘솔은 IPv4/IPv6 방화벽 표가 하나로 통합돼 있다. SSH를 IPv4 주소로 좁히면
  IPv6 우회 경로도 함께 닫히므로 따로 할 일이 없다.)

### 2.2 스왑 2GB

요약 작업 피크가 약 157MB라 1GB로 충분하지만, 안전망으로 둔다.

```bash
sudo fallocate -l 2G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
free -h
```

### 2.3 Docker

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker ubuntu
# 그룹 반영을 위해 재접속
exit
```

### 2.4 AWS CLI (백업용)

```bash
sudo apt-get update && sudo apt-get install -y unzip
curl "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o awscliv2.zip
unzip -q awscliv2.zip && sudo ./aws/install
aws --version
```

자격 증명은 `~/.aws/credentials`가 아니라 **`.env`에 둔다**(`backup.sh`가 읽어서 export한다).
Lightsail은 EC2와 달리 IAM 역할을 붙일 수 없어 액세스 키가 필요하다. 그래서
IAM 사용자 권한은 **백업 버킷 하나에 대한 `s3:PutObject`/`s3:GetObject`/`s3:ListBucket`만** 준다.

### 2.5 S3 버킷

- 버킷 생성(서울 리전), **퍼블릭 액세스 전체 차단**
- 수명 주기 규칙: `db/` 접두사, **30일 후 삭제**
- IAM 사용자 생성 → 위 3개 권한만 담은 인라인 정책 → 액세스 키 발급

### 2.6 도메인 (DuckDNS 임시)

Let's Encrypt는 **IP 주소에는 인증서를 발급하지 않는다.** 도메인 구입 전까지는
[duckdns.org](https://www.duckdns.org)에서 무료 서브도메인을 받아 고정 IP를 가리키게 한다.
나중에 도메인을 사면 `.env`의 `SITE_DOMAIN`과 `DJANGO_ALLOWED_HOSTS`,
`DJANGO_CSRF_TRUSTED_ORIGINS`만 바꾸고 재배포하면 된다.

### 2.7 배포

```bash
git clone <저장소 URL> ~/DART-project
cd ~/DART-project

cp .env.example .env
nano .env          # 아래 3.1의 항목을 채운다

mkdir -p data
sudo chown -R 1000:1000 data     # 컨테이너가 uid 1000으로 돈다

sudo mkdir -p /var/log/dart && sudo chown ubuntu:ubuntu /var/log/dart

docker compose up -d --build
docker compose logs -f
```

`https://<도메인>` 접속 확인. 인증서 발급에 10~30초 걸린다.

### 2.8 초기 데이터

개발 DB를 그대로 올린다.

```bash
# 로컬(Windows)에서 — ⚠ `>` 리다이렉션을 쓰면 cp949로 저장돼 한글이 깨진다. 반드시 -o.
./venv/Scripts/python.exe manage.py dumpdata \
    --exclude contenttypes --exclude auth.permission \
    --indent 2 -o seed.json

# 서버로 복사 후
docker compose exec -T web python manage.py loaddata /app/data/seed.json
docker compose exec -T web python manage.py createsuperuser
```

### 2.9 자동화

```bash
crontab deploy/crontab
crontab -l
./deploy/backup.sh          # 첫 백업이 S3에 올라가는지 즉시 확인
```

Lightsail 콘솔 → 인스턴스 → `스냅샷` 탭 → **자동 스냅샷 활성화**.
주기는 고를 수 없다 — **일 1회, 최근 7개 보관** 고정이고 실행 시각만 정한다(UTC 기준).
**UTC 20:00 = 한국시간 05:00**으로 잡는다. 04:00 S3 백업이 끝난 뒤다.

### 2.10 알람

- **AWS Budgets**: 월 $15 임계값 이메일 알림
- **OpenAI 대시보드**: 사용량 한도(hard limit) 설정
- **Lightsail 메트릭**: CPU·네트워크 경보

---

## 3. 설정 값

### 3.1 `.env` 항목

| 키 | 예시 | 설명 |
|---|---|---|
| `DART_API_KEY` | (40자) | DART OpenAPI 인증키 |
| `OPENAI_API_KEY` | `sk-...` | 요약 생성용 |
| `DJANGO_SECRET_KEY` | (50자 이상 난수) | `python -c "from django.core.management.utils import get_random_secret_key as g; print(g())"` |
| `DJANGO_ALLOWED_HOSTS` | `dart-xxx.duckdns.org` | 쉼표 구분 |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | `https://dart-xxx.duckdns.org` | **스킴 포함**. 없으면 admin 로그인이 CSRF로 막힌다 |
| `SITE_DOMAIN` | `dart-xxx.duckdns.org` | Caddy가 인증서를 받을 도메인. **스킴을 붙이지 말 것** |
| `ADMIN_ALLOWED_IP` | `203.0.113.7` | `/admin`을 열어줄 IP. 공백 구분으로 여러 개, CIDR 가능 |
| `S3_BACKUP_BUCKET` | `dart-project-backup` | 백업 버킷 이름 |
| `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` | | 백업 전용 IAM 사용자 |
| `AWS_DEFAULT_REGION` | `ap-northeast-2` | |

`DJANGO_DEBUG` · `DJANGO_BEHIND_HTTPS_PROXY` · `DJANGO_STATIC_MANIFEST` · `DJANGO_DB_PATH`는
`docker-compose.yml`이 고정하므로 `.env`에 적지 않는다.

### ⚠ 도메인 3개 항목의 형식이 서로 다르다

실제 배포에서 여기서 한 번 막혔다(2026-08-27).

| 항목 | 형식 | 비교 대상 |
|---|---|---|
| `SITE_DOMAIN` | `example.com` | Caddy 사이트 주소 |
| `DJANGO_ALLOWED_HOSTS` | `example.com` | HTTP **Host** 헤더 (스킴이 없다) |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | `https://example.com` | **Origin** 헤더 (스킴이 있다) |

`SITE_DOMAIN`에 `http://`를 붙이면 **Caddy가 TLS 없이 HTTP로만 서비스한다.**
오류가 아니라 "그 프로토콜로 고정하라"는 유효한 지시이기 때문에, 조용히
인증서 없는 상태로 뜬다. 증상은 로그의 이 줄이다:

```
"server is listening only on the HTTP port, so no automatic HTTPS will be applied"
```

`DJANGO_ALLOWED_HOSTS`에 스킴을 붙이면 Host 헤더와 영원히 일치하지 않아
모든 요청이 **400**이 된다.

---

## 4. 일상 운영

| 하는 일 | 명령 |
|---|---|
| 재배포 | `git pull && docker compose up -d --build` |
| 로그 | `docker compose logs -f web` / `docker compose logs -f caddy` |
| 파이프라인 수동 실행 | `docker compose exec web python manage.py poll_dart --days 3` |
| 재검증 (무료) | `docker compose exec web python manage.py revalidate_summaries` |
| 백업 즉시 실행 | `./deploy/backup.sh` |
| 메모리 확인 | `free -h && docker stats --no-stream` |
| 메모리 추이 | `tail -40 /var/log/dart/mem.log` (30분 간격) |
| 요약 배치 피크 | `grep -A1 "$(date -I)T07:3" /var/log/dart/mem_peak.log` |
| 컨테이너 상태 | `docker compose ps` |
| 디스크 여유 | `df -h /` · `docker system df` |

**요약 생성은 돈이 나간다.** cron이 `--limit 20`으로 돌린다. 수동 실행할 때도
반드시 `--limit`을 붙이고, 붙이기 전에 대상 건수를 먼저 확인한다.

**메모리 로그가 둘인 이유.** `mem.log`는 30분 간격의 상시 추이고, `mem_peak.log`는
요약 배치가 도는 07:30~07:33만 10초 간격으로 찍은 것이다. 배치가 1분 안에 끝나기 때문에
30분 샘플러로는 이 서비스의 1순위 리스크(메모리 초과)가 발생하는 바로 그 순간을 볼 수 없다.

---

## 5. 장애 대응

### 사이트가 안 열린다
```bash
docker compose ps                  # 컨테이너가 떠 있나
docker compose logs --tail 100 web
docker compose logs --tail 100 caddy
free -h                            # OOM으로 죽었나
```
메모리가 원인이면 gunicorn 워커를 1로 줄이거나(`Dockerfile`의 `--workers`) 2GB 플랜으로 올린다.

### HTTPS 인증서 오류
```bash
docker compose logs caddy | grep -i "certificate\|acme"
```
DuckDNS의 A 레코드가 고정 IP를 가리키는지, 방화벽 80·443이 열려 있는지 확인.
**인증서 볼륨(`caddy_data`)을 지우고 재시도하지 말 것** — Let's Encrypt는 같은 도메인
중복 발급을 주당 5회로 제한한다.

### admin에 못 들어간다
1. 접속 IP가 바뀌었는지 확인(가정용 회선은 바뀐다) → `.env`의 `ADMIN_ALLOWED_IP` 수정 후
   `docker compose up -d caddy`
2. 로그인 폼에서 CSRF 오류 → `DJANGO_CSRF_TRUSTED_ORIGINS`에 `https://` 스킴이 붙어 있는지 확인

### DB가 잠긴다 (`database is locked`)
요약 배치와 admin 저장이 겹친 경우다. `timeout=20`으로 대부분 흡수되지만 반복되면
PLAN.md 9.2의 **PostgreSQL 전환 트리거**에 해당하는지 검토한다.

### DB 복원
```bash
./deploy/restore.sh                                  # 최근 백업
./deploy/restore.sh db_20260826_040000.sqlite3.gz    # 특정 시점
```
이전 DB는 `./data/db.sqlite3.before_<시각>`에 남으므로 되돌릴 수 있다.

---

## 6. 인스턴스를 통째로 잃었을 때 (재구축)

**1순위는 스냅샷 복원이 아니라 "저장소 + S3 백업으로 재구축"이다.** 그래야 서버에만
존재하는 상태가 없다는 것이 매번 검증된다. 스냅샷은 재구축이 막혔을 때의 안전망이다.

1. 새 Lightsail 인스턴스 생성 (2.1~2.5 반복)
2. 고정 IP를 **새 인스턴스로 재연결** — DNS를 건드릴 필요가 없다
3. `git clone` → `.env` 작성 → `docker compose up -d --build`
4. `./deploy/restore.sh` 로 최근 백업 복원
5. `crontab deploy/crontab`

최대 데이터 손실은 **24시간**(백업 주기)이다. 그 사이의 요약은 파이프라인을 다시 돌리면
재생성되고, 비용은 건당 $0.023이다.

---

## 7. EC2로 이전할 때 (7단계)

Celery 워커를 별도 프로세스로 분리하는 시점에 검토한다(PLAN.md 9.2 전환 트리거 1번).

- Lightsail 스냅샷 → **EC2로 내보내기**가 공식 지원된다
- 다만 6번 재구축 절차를 EC2에서 그대로 밟는 편이 깨끗하다. 이전이 곧 재구축 리허설이다
- EC2에서는 IAM **역할**을 인스턴스에 붙일 수 있으므로 `.env`의 AWS 액세스 키를 없앤다
- 이 시점에 PostgreSQL 전환도 함께 판단한다
