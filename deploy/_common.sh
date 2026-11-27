# deploy/*.sh 공통: 저장소 루트로 이동하고 docker compose 명령을 정한다(직접 실행하지 않는다).
# 모든 compose 명령은 .env.production을 --env-file로 읽는다(Caddy 도메인 치환용). 값은 출력하지 않는다.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"
ENV_FILE="$ROOT_DIR/.env.production"
SERVICES=(web mcp caddy)

die() { echo "[오류] $*" >&2; exit 1; }

require_env_file() {
  [[ -f "$ENV_FILE" ]] || die ".env.production이 없습니다. cp env.production.example .env.production 후 값을 채우세요."
}

require_docker() {
  command -v docker >/dev/null 2>&1 || die "docker가 없습니다. 먼저 deploy/setup_server.sh를 실행하세요."
  if ! docker info >/dev/null 2>&1; then
    die "docker에 접근할 수 없습니다. setup_server.sh 실행 후 로그아웃했다가 다시 접속하세요(또는 newgrp docker)."
  fi
}

compose() { docker compose --env-file "$ENV_FILE" "$@"; }
