# 오피스지니 웹(app.py)과 원격 MCP 서버(remote_server.py)가 함께 쓰는 이미지.
# 실행 명령만 docker-compose.yml에서 다르게 준다. Python 버전은 .python-version과 같게 둔다.
FROM python:3.15.2-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# 의존성만 먼저 설치해 코드가 바뀌어도 이 층은 캐시를 쓴다.
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# PWA 파일을 설치된 streamlit 정적 폴더에 넣고(패키지 폴더라 root일 때 실행), 적용됐는지 확인한다.
RUN python scripts/pwa_setup.py && python scripts/pwa_setup.py --check

# root가 아닌 사용자로 실행한다. 코드(/app)는 root 소유로 두어 실행 사용자는 읽기만 한다.
# 앱은 실행 중 파일에 데이터를 쓰지 않는다(모든 상태는 DB). 홈 폴더는 streamlit 설정용.
RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --home-dir /home/app --create-home --shell /usr/sbin/nologin app
USER app

EXPOSE 8501 8000
# 기본 명령은 웹. MCP 서비스는 docker-compose.yml에서 python remote_server.py로 바꾼다.
CMD ["sh", "-c", "python scripts/preflight.py && exec streamlit run app.py --server.port 8501 --server.address 0.0.0.0 --server.headless true"]
