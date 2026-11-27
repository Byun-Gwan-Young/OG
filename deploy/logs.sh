#!/usr/bin/env bash
# 서비스별 최근 로그 보기.
#   bash deploy/logs.sh            # 세 서비스 최근 100줄
#   bash deploy/logs.sh web 300    # web 최근 300줄
#   bash deploy/logs.sh mcp -f     # mcp 로그 계속 보기(Ctrl+C로 끝)
. "$(dirname "${BASH_SOURCE[0]}")/_common.sh"
require_env_file
require_docker

service="${1:-}"
arg="${2:-100}"
if [[ -n "$service" && ! " ${SERVICES[*]} " =~ " ${service} " ]]; then
  die "서비스 이름은 ${SERVICES[*]} 중 하나입니다."
fi
if [[ "$arg" == "-f" ]]; then
  compose logs --no-color --tail 100 -f ${service:+"$service"}
else
  compose logs --no-color --tail "$arg" ${service:+"$service"}
fi
