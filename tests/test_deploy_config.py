"""AWS EC2 배포 구성(Dockerfile, docker-compose.yml, Caddyfile, deploy/) 구조 테스트. Docker 없이 실행된다."""
from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy"
SCRIPTS = ("_common.sh", "setup_server.sh", "deploy.sh", "logs.sh", "stop.sh")
ENV_KEYS = (
    "DATABASE_URL", "LLM_API_KEY", "LLM_BASE_URL", "OFFICEGENIE_SIGNUP_CODE", "OFFICEGENIE_MCP_PUBLIC_URL",
    "OFFICEGENIE_ENV", "WEB_DOMAIN", "MCP_DOMAIN", "ACME_EMAIL",
)


def _compose() -> dict:
    return yaml.safe_load((ROOT / "docker-compose.yml").read_text("utf-8"))


def _tracked_text_files() -> list[Path]:
    out = subprocess.run(["git", "ls-files", "-co", "--exclude-standard"], cwd=ROOT,
                         capture_output=True, text=True, check=True).stdout.split("\n")
    files = []
    for name in filter(None, out):
        path = ROOT / name
        if path.is_file() and path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".ico", ".webp", ".pdf"}:
            files.append(path)
    return files


# ---------------------------------------------------------------------------
# Render 제거
# ---------------------------------------------------------------------------

def test_render_config_removed():
    assert not (ROOT / "render.yaml").exists()
    needle = "onrender" + ".com"  # 이 파일 자체가 걸리지 않도록 나눠 쓴다
    hits = [str(p.relative_to(ROOT)) for p in _tracked_text_files()
            if needle in p.read_text("utf-8", errors="ignore")]
    assert hits == []


# ---------------------------------------------------------------------------
# docker-compose.yml
# ---------------------------------------------------------------------------

def test_compose_exposes_only_caddy():
    services = _compose()["services"]
    assert set(services) == {"web", "mcp", "caddy"}
    for name, port in (("web", "8501"), ("mcp", "8000")):
        svc = services[name]
        assert "ports" not in svc, f"{name} 포트가 외부에 열려 있습니다"
        assert [str(p) for p in svc["expose"]] == [port]
    published = {str(p).split(":")[0] for p in services["caddy"]["ports"]}
    assert published == {"80", "443"}
    assert not any(str(p).endswith("/udp") for p in services["caddy"]["ports"])  # HTTP/3(UDP) 안 씀


def test_compose_restart_logging_healthchecks():
    services = _compose()["services"]
    for name, svc in services.items():
        assert svc["restart"] == "unless-stopped", name
        assert svc["logging"]["driver"] == "json-file", name
        assert svc["logging"]["options"]["max-size"] and svc["logging"]["options"]["max-file"], name
        assert svc["healthcheck"]["test"], name
    assert "/_stcore/health" in " ".join(services["web"]["healthcheck"]["test"])
    assert "127.0.0.1:8000/health" in " ".join(services["mcp"]["healthcheck"]["test"])


def test_compose_commands_env_and_volumes():
    data = _compose()
    services = data["services"]
    web_cmd = " ".join(services["web"]["command"])
    assert web_cmd.endswith(
        "python scripts/preflight.py && exec streamlit run app.py --server.port 8501 "
        "--server.address 0.0.0.0 --server.headless true")
    assert services["mcp"]["command"] == ["python", "remote_server.py"]
    assert services["mcp"]["environment"]["PORT"] == "8000"
    for name in ("web", "mcp"):
        assert services[name]["env_file"] == ".env.production"
        assert services[name]["build"] == "."
    # Caddy에는 비밀 값 파일을 넘기지 않고 도메인·메일만 넘긴다.
    caddy = services["caddy"]
    assert "env_file" not in caddy
    assert set(caddy["environment"]) == {"WEB_DOMAIN", "MCP_DOMAIN", "ACME_EMAIL"}
    assert "caddy_data:/data" in caddy["volumes"]
    assert any(v.startswith("./Caddyfile:/etc/caddy/Caddyfile") for v in caddy["volumes"])
    assert {"caddy_data", "caddy_config"} <= set(data["volumes"])


# ---------------------------------------------------------------------------
# Dockerfile · Caddyfile · ignore 파일
# ---------------------------------------------------------------------------

def test_dockerfile():
    text = (ROOT / "Dockerfile").read_text("utf-8")
    version = (ROOT / ".python-version").read_text().strip()
    assert re.search(rf"^FROM python:{re.escape(version)}-slim\s*$", text, re.M)
    assert re.search(r"^RUN .*pip install -r requirements\.txt", text, re.M)
    assert re.search(r"^RUN python scripts/pwa_setup\.py && python scripts/pwa_setup\.py --check", text, re.M)
    users = re.findall(r"^USER\s+(\S+)", text, re.M)
    assert users and users[-1] not in ("root", "0")
    # pwa_setup은 사용자 전환 전에(패키지 폴더 쓰기) 실행한다.
    assert text.index("pwa_setup.py") < text.index(f"USER {users[-1]}")


def test_caddyfile_routes():
    text = (ROOT / "Caddyfile").read_text("utf-8")
    assert re.search(r"email \{\$ACME_EMAIL\}", text)
    web = re.search(r"\{\$WEB_DOMAIN\} \{(.*?)\n\}", text, re.S).group(1)
    mcp = re.search(r"\{\$MCP_DOMAIN\} \{(.*?)\n\}", text, re.S).group(1)
    assert "reverse_proxy web:8501" in web
    assert "reverse_proxy mcp:8000" in mcp
    # 헤더를 지우거나 덮어쓰는 설정이 없어야 앱이 X-Forwarded-Proto로 https를 판단한다.
    assert "header_up -X-Forwarded" not in text and "tls internal" not in text


def test_caddy_disables_http3_and_clears_alt_svc():
    """보안 그룹이 UDP 443을 막으므로 HTTP/3 광고(Alt-Svc: h3)를 하면 아이콘 폰트 등 나중 요청이 실패한다."""
    text = (ROOT / "Caddyfile").read_text("utf-8")
    assert re.search(r"servers \{\s*protocols h1 h2\s*\}", text)
    assert 'header Alt-Svc "clear"' in text
    common = re.search(r"\(common\) \{(.*?)\n\}", text, re.S).group(1)
    assert "Strict-Transport-Security" in common
    for domain in ("WEB_DOMAIN", "MCP_DOMAIN"):
        block = re.search(r"\{\$" + domain + r"\} \{(.*?)\n\}", text, re.S).group(1)
        assert "import common" in block


def test_env_production_never_committed_or_baked():
    gitignore = (ROOT / ".gitignore").read_text("utf-8").split()
    dockerignore = (ROOT / ".dockerignore").read_text("utf-8").split()
    assert ".env.production" in gitignore
    for entry in (".env.production", ".env", ".venv/", "officegenie.db", "docs/images/", "__pycache__/", ".pytest_cache/"):
        assert entry in dockerignore, entry
    ignored = subprocess.run(["git", "check-ignore", "-q", ".env.production"], cwd=ROOT)
    assert ignored.returncode == 0
    example = (ROOT / "env.production.example").read_text("utf-8")
    keys = re.findall(r"^([A-Z_]+)=", example, re.M)
    assert set(ENV_KEYS) <= set(keys)
    assert re.search(r"^OFFICEGENIE_ENV=production$", example, re.M)
    assert re.search(r"^LLM_API_KEY=$", example, re.M)  # 예시에는 실제 키가 없다


# ---------------------------------------------------------------------------
# deploy/ 스크립트
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", SCRIPTS)
def test_deploy_scripts_parse(name):
    path = DEPLOY / name
    assert path.is_file()
    assert subprocess.run(["bash", "-n", str(path)]).returncode == 0
    if name != "_common.sh":
        assert path.stat().st_mode & stat.S_IXUSR
        assert path.read_text("utf-8").startswith("#!/usr/bin/env bash")


def test_setup_server_is_idempotent_by_design():
    text = (DEPLOY / "setup_server.sh").read_text("utf-8")
    assert "docker compose version" in text  # 설치돼 있으면 건너뜀
    assert "usermod -aG docker" in text and "grep -qx docker" in text
    assert "MemTotal" in text and "swapon --show" in text and "grep -q '^/swapfile '" in text
    assert "unattended-upgrades" in text


FAKE_DOCKER = r"""#!/usr/bin/env bash
# 테스트용 가짜 docker. 상태는 FAKE_STATUS_<서비스> 환경변수로 정한다.
echo "docker $*" >> "$FAKE_LOG"
case "$1" in
  info) exit 0 ;;
  image) exit 0 ;;
  inspect) svc="${@: -1}"; svc="${svc#id-}"; var="FAKE_STATUS_${svc}"; echo "${!var:-healthy}"; exit 0 ;;
  compose)
    shift
    while [[ "$1" == --env-file ]]; do shift 2; done
    case "$1" in
      ps) [[ "$2" == "-q" ]] && { echo "id-$3"; exit 0; }; echo "NAME STATUS"; exit 0 ;;
      logs) echo "fake-log-line for ${@: -1}"; exit 0 ;;
      *) exit 0 ;;
    esac ;;
esac
exit 0
"""


@pytest.fixture
def fake_deploy(tmp_path):
    """deploy/와 .env.production만 있는 임시 폴더 + 가짜 docker로 deploy.sh를 실행한다."""
    shutil.copytree(DEPLOY, tmp_path / "deploy")
    env_text = "\n".join(f"{k}=secret-value-{k.lower()}" for k in ENV_KEYS if k != "OFFICEGENIE_ENV")
    (tmp_path / ".env.production").write_text(env_text + "\nOFFICEGENIE_ENV=production\n", "utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(FAKE_DOCKER, "utf-8")
    docker.chmod(0o755)
    log = tmp_path / "docker.log"

    def run(**status):
        env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "FAKE_LOG": str(log),
               "WAIT_SECONDS": "5", **{f"FAKE_STATUS_{k}": v for k, v in status.items()}}
        done = subprocess.run(["bash", str(tmp_path / "deploy" / "deploy.sh"), "--no-pull"],
                              cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60)
        calls = log.read_text("utf-8") if log.exists() else ""
        return done, calls

    return run


def test_deploy_success_builds_starts_waits_and_prunes(fake_deploy):
    done, calls = fake_deploy()
    assert done.returncode == 0, done.stderr
    order = [c for c in ("config --quiet", "build", "up -d", "image prune") if c in calls]
    assert order == ["config --quiet", "build", "up -d", "image prune"]
    assert calls.index("build") < calls.index("up -d") < calls.index("image prune")
    assert "secret-value" not in done.stdout + done.stderr


def test_deploy_failure_shows_50_log_lines_and_stops(fake_deploy):
    done, calls = fake_deploy(mcp="unhealthy")
    assert done.returncode == 1
    assert "mcp" in done.stderr and "fake-log-line for mcp" in done.stderr
    assert "logs --no-color --tail 50 mcp" in calls
    assert "image prune" not in calls  # 실패하면 정리 전에 멈춘다
    assert "secret-value" not in done.stdout + done.stderr


def test_deploy_refuses_missing_values(fake_deploy, tmp_path):
    env_file = tmp_path / ".env.production"
    env_file.write_text(env_file.read_text("utf-8").replace("LLM_API_KEY=secret-value-llm_api_key", "LLM_API_KEY="), "utf-8")
    done, calls = fake_deploy()
    assert done.returncode == 1 and "LLM_API_KEY" in done.stderr
    assert "build" not in calls and "secret-value" not in done.stdout + done.stderr
