#!/usr/bin/env bash
# EC2(Ubuntu 24.04) 처음 한 번: Docker·Compose 설치, docker 그룹, 스왑(메모리 2GB 이하일 때 2GB), 자동 보안 업데이트.
# 여러 번 실행해도 이미 된 것은 건너뛴다.
#   bash deploy/setup_server.sh
set -euo pipefail

if [[ "$(id -u)" -eq 0 ]]; then
  echo "[오류] root가 아닌 일반 사용자(ubuntu)로 실행하세요. 필요한 곳에서만 sudo를 씁니다." >&2
  exit 1
fi
. /etc/os-release
if [[ "${ID:-}" != "ubuntu" ]]; then
  echo "[경고] Ubuntu용 스크립트입니다(현재: ${PRETTY_NAME:-알 수 없음}). 계속합니다." >&2
fi

step() { echo; echo "== $* =="; }

step "기본 패키지"
sudo apt-get update -y
sudo apt-get install -y ca-certificates curl git gnupg unattended-upgrades

step "Docker Engine + Compose 플러그인"
if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  echo "이미 설치됨: $(docker --version)"
else
  sudo install -m 0755 -d /etc/apt/keyrings
  if [[ ! -f /etc/apt/keyrings/docker.asc ]]; then
    sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
    sudo chmod a+r /etc/apt/keyrings/docker.asc
  fi
  arch="$(dpkg --print-architecture)"
  echo "deb [arch=${arch} signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" \
    | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
  sudo apt-get update -y
  sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi
sudo systemctl enable --now docker

step "docker 그룹"
if id -nG "$USER" | tr ' ' '\n' | grep -qx docker; then
  echo "$USER 는 이미 docker 그룹입니다."
else
  sudo usermod -aG docker "$USER"
  echo "$USER 를 docker 그룹에 추가했습니다. 로그아웃 후 다시 접속해야 적용됩니다."
fi

step "스왑"
mem_kb="$(awk '/MemTotal/ {print $2}' /proc/meminfo)"
if [[ "$mem_kb" -gt $((2 * 1024 * 1024)) ]]; then
  echo "메모리가 2GB보다 커서 스왑을 만들지 않습니다."
elif [[ -n "$(swapon --show --noheadings)" ]]; then
  echo "이미 스왑이 있습니다: $(swapon --show --noheadings | awk '{print $1, $3}' | tr '\n' ' ')"
else
  sudo fallocate -l 2G /swapfile || sudo dd if=/dev/zero of=/swapfile bs=1M count=2048
  sudo chmod 600 /swapfile
  sudo mkswap /swapfile
  sudo swapon /swapfile
  grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
  echo 'vm.swappiness=10' | sudo tee /etc/sysctl.d/99-officegenie-swap.conf >/dev/null
  sudo sysctl -q -p /etc/sysctl.d/99-officegenie-swap.conf
  echo "2GB 스왑(/swapfile)을 만들었습니다."
fi

step "자동 보안 업데이트"
sudo tee /etc/apt/apt.conf.d/20auto-upgrades >/dev/null <<'CONF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
CONF
sudo systemctl enable --now unattended-upgrades
echo "unattended-upgrades 켜짐"

echo
echo "준비 완료. docker 그룹을 방금 추가했다면 로그아웃 후 다시 접속한 뒤:"
echo "  git clone <저장소 주소> officegenie && cd officegenie"
echo "  cp env.production.example .env.production && chmod 600 .env.production && nano .env.production"
echo "  bash deploy/deploy.sh"
