#!/usr/bin/env bash
# 업데이트할 때마다: git pull → 이미지 빌드 → 컨테이너 재시작 → 헬스체크 대기 → 오래된 이미지 정리.
# 실패하면 그 서비스 로그 마지막 50줄을 보여 주고 멈춘다. DB 주소·키 값은 출력하지 않는다.
#   bash deploy/deploy.sh            # 보통
#   bash deploy/deploy.sh --no-pull  # 저장소를 당기지 않고 지금 코드로 배포
. "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

PULL=1
[[ "${1:-}" == "--no-pull" ]] && PULL=0
WAIT_SECONDS="${WAIT_SECONDS:-240}"

require_env_file
require_docker

# 필수 값이 비어 있지 않은지만 확인한다(값은 출력하지 않음).
missing=()
for key in DATABASE_URL LLM_API_KEY LLM_BASE_URL OFFICEGENIE_MCP_PUBLIC_URL OFFICEGENIE_ENV WEB_DOMAIN MCP_DOMAIN ACME_EMAIL; do
  grep -Eq "^${key}=.+" "$ENV_FILE" || missing+=("$key")
done
[[ ${#missing[@]} -eq 0 ]] || die ".env.production에 값이 비어 있습니다: ${missing[*]}"
grep -Eq '^OFFICEGENIE_ENV=production\s*$' "$ENV_FILE" || die "OFFICEGENIE_ENV=production 이어야 합니다."
chmod 600 "$ENV_FILE" 2>/dev/null || true

if [[ $PULL -eq 1 ]]; then
  echo "== git pull =="
  git pull --ff-only
fi
echo "== 커밋: $(git log --oneline -1 2>/dev/null || echo 알 수 없음) =="

echo "== 설정 확인 =="
compose config --quiet || die "docker-compose.yml 또는 .env.production 형식이 올바르지 않습니다."

echo "== 이미지 빌드 =="
compose build

echo "== 컨테이너 시작 =="
compose up -d --remove-orphans

status_of() {
  local id
  id="$(compose ps -q "$1")"
  [[ -n "$id" ]] || { echo "missing"; return; }
  docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$id"
}

show_failure() {
  echo
  echo "[실패] $1 서비스가 정상 상태가 되지 않았습니다 (상태: $2). 최근 로그 50줄:" >&2
  compose logs --no-color --tail 50 "$1" >&2 || true
  exit 1
}

echo "== 헬스체크 대기 (최대 ${WAIT_SECONDS}초) =="
deadline=$((SECONDS + WAIT_SECONDS))
for svc in "${SERVICES[@]}"; do
  while true; do
    st="$(status_of "$svc")"
    case "$st" in
      healthy) echo "  $svc: 정상"; break ;;
      unhealthy|exited|dead|missing) show_failure "$svc" "$st" ;;
    esac
    (( SECONDS < deadline )) || show_failure "$svc" "$st"
    sleep 3
  done
done

echo "== 오래된 이미지 정리 =="
docker image prune -f >/dev/null
compose ps
echo
echo "배포 완료. 내 PC에서 점검: python scripts/deploy_check.py https://<WEB_DOMAIN> https://<MCP_DOMAIN>"
