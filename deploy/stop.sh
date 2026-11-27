#!/usr/bin/env bash
# 전체 중지(컨테이너 삭제). 인증서 볼륨(caddy_data)과 DB(Neon)는 그대로 남는다.
# 다시 켜려면 bash deploy/deploy.sh --no-pull
. "$(dirname "${BASH_SOURCE[0]}")/_common.sh"
require_env_file
require_docker
compose down
echo "모든 서비스를 중지했습니다."
