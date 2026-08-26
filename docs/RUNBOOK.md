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
- **고정 IP(Static IP)** 생성 후 인스턴스에 연결 — 재부팅해도 IP가 바뀌지 않는다
- 방화벽(네트워킹 탭): **22는 내 IP만**, 80·443은 전체 허용

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

Lightsail 콘솔 → 스냅샷 → **자동 스냅샷 활성화**(주 1회 상당, 최근 4개 보관).

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
| `SITE_DOMAIN` | `dart-xxx.duckdns.org` | Caddy가 인증서를 받을 도메인 |
| `ADMIN_ALLOWED_IP` | `203.0.113.7` | `/admin`을 열어줄 IP. 공백 구분으로 여러 개, CIDR 가능 |
| `S3_BACKUP_BUCKET` | `dart-project-backup` | 백업 버킷 이름 |
| `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` | | 백업 전용 IAM 사용자 |
| `AWS_DEFAULT_REGION` | `ap-northeast-2` | |

`DJANGO_DEBUG` · `DJANGO_BEHIND_HTTPS_PROXY` · `DJANGO_STATIC_MANIFEST` · `DJANGO_DB_PATH`는
`docker-compose.yml`이 고정하므로 `.env`에 적지 않는다.

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
| 컨테이너 상태 | `docker compose ps` |

**요약 생성은 돈이 나간다.** cron이 `--limit 20`으로 돌린다. 수동 실행할 때도
반드시 `--limit`을 붙이고, 붙이기 전에 대상 건수를 먼저 확인한다.

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
