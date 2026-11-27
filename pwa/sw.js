/*
 * 오피스지니 서비스 워커 (scripts/pwa_setup.py가 Streamlit 정적 폴더 맨 위에 sw.js로 복사한다)
 *
 * - 미리 저장하는 것은 아이콘과 offline.html뿐이다.
 * - 페이지 이동(navigate)은 네트워크 우선, 실패하면 offline.html을 보여 준다. 페이지 응답은 저장하지 않는다.
 * - /_stcore/ 경로(웹소켓·상태 확인·업로드)와 그 밖의 응답은 건드리지 않는다.
 *   화면 데이터에는 개인정보가 있고 실시간 연결이어서 저장하면 안 된다.
 * - 서비스 워커는 HTTPS 또는 localhost에서만 동작한다.
 */
const CACHE = "officegenie-pwa-v1";
const OFFLINE_URL = new URL("pwa/offline.html", self.registration.scope).href;
const PRECACHE = [
  OFFLINE_URL,
  new URL("pwa/icon-192.png", self.registration.scope).href,
  new URL("pwa/icon-512.png", self.registration.scope).href,
  new URL("pwa/icon-maskable-512.png", self.registration.scope).href,
  new URL("pwa/apple-touch-icon.png", self.registration.scope).href,
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE).then((cache) => cache.addAll(PRECACHE)).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((key) => key !== CACHE).map((key) => caches.delete(key))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  if (url.pathname.includes("/_stcore/")) return; // 실시간 연결·API: 절대 가로채거나 저장하지 않는다

  if (request.mode === "navigate") {
    // 네트워크 우선. 응답은 저장하지 않는다(로그인한 사람의 화면일 수 있음).
    event.respondWith(
      fetch(request).catch(() =>
        caches.match(OFFLINE_URL).then((cached) => cached || new Response("오프라인입니다.", {
          status: 503,
          headers: { "Content-Type": "text/plain; charset=utf-8" },
        }))
      )
    );
    return;
  }

  const target = url.origin + url.pathname;
  if (PRECACHE.includes(target)) {
    // 아이콘·오프라인 화면만 저장본을 쓴다.
    event.respondWith(caches.match(target).then((cached) => cached || fetch(request)));
  }
  // 그 밖의 요청은 브라우저 기본 동작(네트워크)에 맡긴다.
});
