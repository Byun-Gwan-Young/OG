/*
 * 오피스지니 앱 설치 안내 (scripts/pwa_setup.py가 Streamlit 정적 폴더 pwa/install.js로 복사하고 index.html에서 불러온다)
 *
 * Streamlit 화면 코드는 iframe 안에서도 돌 수 있어 beforeinstallprompt를 받지 못하므로 최상위 문서에서 처리한다.
 * - Android·PC 크롬/엣지: beforeinstallprompt를 잡아 화면 아래 "앱 설치" 버튼을 보인다. 누르면 설치 창을 띄운다.
 * - 이미 설치된 상태(display-mode: standalone)이거나 설치가 끝나면(appinstalled) 숨긴다.
 * - iPhone·iPad Safari: 설치 이벤트가 없으므로 "공유 → 홈 화면에 추가" 안내를 한 번 보인다(닫으면 기억).
 * - 사원 홈의 "앱으로 설치하기" 카드 링크(href="#og-install")를 누르면 같은 동작을 한다.
 * - 로그인·회사 등록 입력 화면(.st-key-login_card)이 떠 있는 동안에는 버튼·안내를 띄우지 않는다(입력·버튼을 가리지 않게).
 * - <html>에 상태 클래스를 붙인다: og-standalone, og-can-install, og-ios. 화면 CSS가 이것으로 카드를 숨기거나 바꾼다.
 * 개인정보를 저장하지 않는다. localStorage에는 "iOS 안내를 닫았다"는 표시만 둔다(막혀 있어도 동작).
 */
(function () {
  "use strict";
  if (window.__ogInstallLoaded) return;
  window.__ogInstallLoaded = true;

  var doc = document;
  var root = doc.documentElement;
  var DISMISS_KEY = "officegenie:ios-install-hint-dismissed";
  var deferred = null;

  function store(kind) {
    try { return window[kind]; } catch (e) { return null; }
  }
  function getFlag(kind, key) {
    try { var s = store(kind); return s ? s.getItem(key) : null; } catch (e) { return null; }
  }
  function setFlag(kind, key) {
    try { var s = store(kind); if (s) s.setItem(key, "1"); } catch (e) { /* 저장 불가(사생활 보호 모드 등) */ }
  }

  function isStandalone() {
    try {
      if (window.matchMedia && window.matchMedia("(display-mode: standalone)").matches) return true;
    } catch (e) { /* 무시 */ }
    return window.navigator.standalone === true;
  }
  var ua = window.navigator.userAgent || "";
  // iPadOS Safari는 "Macintosh"로 보고하므로 터치 + Safari(크롬·엣지·파이어폭스 제외)일 때만 iPad로 본다.
  var isIPadSafari = /Macintosh/.test(ua) && "ontouchend" in doc && /Safari/.test(ua) &&
    !/Chrome|Chromium|CriOS|Edg|Firefox|FxiOS/.test(ua);
  var isIOS = /iPhone|iPad|iPod/.test(ua) || isIPadSafari;
  var isIOSSafari = isIOS && /Safari/.test(ua) && !/CriOS|FxiOS|EdgiOS|KAKAOTALK|NAVER|Line\//.test(ua);

  function setState() {
    root.classList.toggle("og-standalone", isStandalone());
    root.classList.toggle("og-can-install", !!deferred && !isStandalone());
    root.classList.toggle("og-ios", isIOS);
  }

  var STYLE = [
    ".og-install-pill{position:fixed;left:max(16px,env(safe-area-inset-left));z-index:100000;",
    "bottom:calc(24px + env(safe-area-inset-bottom));display:none;align-items:center;gap:4px;",
    "font:600 14px/1 Inter,'Noto Sans KR','Apple SD Gothic Neo',sans-serif;color:#fff;background:#3868BE;",
    "border:0;border-radius:999px;box-shadow:0 8px 24px rgba(32,45,64,.18);padding:0 6px 0 16px;min-height:48px}",
    ".og-install-pill.og-show{display:inline-flex}",
    ".og-install-pill button{font:inherit;color:inherit;background:none;border:0;cursor:pointer;min-height:44px;",
    "display:inline-flex;align-items:center;gap:8px;padding:0}",
    ".og-install-pill .og-x{width:36px;justify-content:center;border-radius:50%;opacity:.85}",
    ".og-install-pill .og-x:hover{background:rgba(255,255,255,.16)}",
    ".og-install-pill button:focus-visible,.og-ios-hint button:focus-visible{outline:2px solid #fff;outline-offset:2px}",
    ".og-ios-hint{position:fixed;left:16px;right:16px;z-index:100000;bottom:calc(16px + env(safe-area-inset-bottom));",
    "display:none;gap:12px;align-items:flex-start;background:#fff;color:#202D40;border:1px solid #E2E7EE;",
    "border-radius:16px;box-shadow:0 12px 32px rgba(32,45,64,.18);padding:16px;",
    "font:400 14px/1.55 Inter,'Noto Sans KR','Apple SD Gothic Neo',sans-serif;max-width:460px;margin:0 auto}",
    ".og-ios-hint.og-show{display:flex}",
    ".og-ios-hint strong{display:block;font-weight:700;margin-bottom:2px}",
    ".og-ios-hint .og-icon{flex:0 0 40px;height:40px;border-radius:10px;background:#EAF1FC;color:#3868BE;",
    "display:grid;place-items:center}",
    ".og-ios-hint .og-x{flex:0 0 36px;height:36px;border:0;background:none;color:#67758A;border-radius:50%;cursor:pointer}",
    ".og-ios-hint .og-x:hover{background:#F5F7FA}",
    ".og-share{display:inline-block;vertical-align:-3px;color:#3868BE}",
    /* 모바일: 아래 탭바(약 64px)와 오른쪽 아래 챗봇 버튼(56px)을 가리지 않게 그 위로 올린다 */
    "@media (max-width:767px){.og-install-pill{bottom:calc(84px + env(safe-area-inset-bottom))}",
    ".og-ios-hint{bottom:calc(156px + env(safe-area-inset-bottom))}}",
    /* PC: 오른쪽 아래 챗봇 버튼(64px)을 피한다 */
    "@media (min-width:768px){.og-ios-hint{right:auto;left:24px;bottom:calc(24px + env(safe-area-inset-bottom))}}",
    "@media print{.og-install-pill,.og-ios-hint{display:none!important}}"
  ].join("");

  var ICON_DOWNLOAD = '<svg width="20" height="20" viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" ' +
    'stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 4v11"/><path d="m7 10 5 5 5-5"/><path d="M5 20h14"/></svg>';
  var ICON_CLOSE = '<svg width="18" height="18" viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" ' +
    'stroke-width="2" stroke-linecap="round"><path d="M6 6l12 12M18 6 6 18"/></svg>';
  var ICON_SHARE = '<svg class="og-share" width="18" height="18" viewBox="0 0 24 24" aria-hidden="true" fill="none" ' +
    'stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v12"/>' +
    '<path d="m8 7 4-4 4 4"/><path d="M6 11H5a1 1 0 0 0-1 1v8a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-8a1 1 0 0 0-1-1h-1"/></svg>';

  var pill = null;
  var hint = null;

  function build() {
    if (pill || !doc.body) return;
    var style = doc.createElement("style");
    style.textContent = STYLE;
    doc.head.appendChild(style);

    pill = doc.createElement("div");
    pill.className = "og-install-pill";
    pill.setAttribute("role", "region");
    pill.setAttribute("aria-label", "앱 설치");
    pill.innerHTML = '<button type="button" class="og-go">' + ICON_DOWNLOAD + '<span>앱 설치</span></button>' +
      '<button type="button" class="og-x" aria-label="앱 설치 안내 닫기">' + ICON_CLOSE + "</button>";
    pill.querySelector(".og-go").addEventListener("click", promptInstall);
    pill.querySelector(".og-x").addEventListener("click", function () {
      setFlag("sessionStorage", "officegenie:install-pill-closed");
      pill.classList.remove("og-show");
    });
    doc.body.appendChild(pill);

    hint = doc.createElement("div");
    hint.className = "og-ios-hint";
    hint.setAttribute("role", "dialog");
    hint.setAttribute("aria-label", "홈 화면에 추가 안내");
    hint.innerHTML = '<div class="og-icon">' + ICON_DOWNLOAD + "</div>" +
      "<div><strong>오피스지니를 앱처럼 쓰기</strong>Safari 아래쪽 공유 버튼 " + ICON_SHARE +
      " 을 누르고 <b>홈 화면에 추가</b>를 고르세요.</div>" +
      '<button type="button" class="og-x" aria-label="안내 닫기">' + ICON_CLOSE + "</button>";
    hint.querySelector(".og-x").addEventListener("click", function () {
      setFlag("localStorage", DISMISS_KEY);
      hint.classList.remove("og-show");
    });
    doc.body.appendChild(hint);
    refresh();
    // Streamlit은 화면을 페이지 이동 없이 바꾸므로(로그인 → 홈 등) 가끔 다시 확인한다.
    setInterval(refresh, 1500);
  }

  function onEntryForm() {
    return !!doc.querySelector(".st-key-login_card");
  }

  function refresh() {
    setState();
    if (!pill) return;
    var standalone = isStandalone();
    var blocked = onEntryForm();
    pill.classList.toggle("og-show", !!deferred && !standalone && !blocked &&
      !getFlag("sessionStorage", "officegenie:install-pill-closed"));
    var showHint = isIOSSafari && !standalone && !blocked && !getFlag("localStorage", DISMISS_KEY);
    if (showHint && !hint.classList.contains("og-show")) {
      hint.classList.add("og-show");
    }
    if (!showHint) hint.classList.remove("og-show");
  }

  function showGuide() {
    // 설치 창을 띄울 수 없는 브라우저: iPhone이면 안내 카드를, 그 밖에는 메뉴 안내를 보인다.
    if (isIOS) {
      if (hint) hint.classList.add("og-show");
      return;
    }
    window.alert("브라우저 메뉴(⋮)에서 '앱 설치' 또는 '홈 화면에 추가'를 눌러 주세요.\n" +
      "크롬·엣지에서 https 주소로 접속해야 설치할 수 있습니다.");
  }

  function promptInstall() {
    if (!deferred) { showGuide(); return; }
    var evt = deferred;
    deferred = null;
    try {
      evt.prompt();
      evt.userChoice.then(refresh, refresh);
    } catch (e) {
      showGuide();
    }
    refresh();
  }

  window.addEventListener("beforeinstallprompt", function (event) {
    event.preventDefault();
    deferred = event;
    refresh();
  });
  window.addEventListener("appinstalled", function () {
    deferred = null;
    if (pill) pill.classList.remove("og-show");
    refresh();
  });
  try {
    window.matchMedia("(display-mode: standalone)").addEventListener("change", refresh);
  } catch (e) { /* 오래된 브라우저 */ }

  // 사원 홈 카드 등 화면 안의 링크: <a href="#og-install">
  doc.addEventListener("click", function (event) {
    var link = event.target && event.target.closest ? event.target.closest('a[href="#og-install"]') : null;
    if (!link) return;
    event.preventDefault();
    promptInstall();
  });

  // 아이콘 글꼴(Material Symbols)을 끝내 받지 못하면 <html>에 표시를 붙여 아이콘 이름 글자를 감춘다(화면 CSS).
  function watchIconFont() {
    if (!doc.fonts || !doc.fonts.load) return;
    var spec = '24px "Material Symbols Rounded"';
    var mark = function () {
      var ok = false;
      try { ok = doc.fonts.check(spec); } catch (e) { ok = true; }
      root.classList.toggle("og-icon-font-missing", !ok);
    };
    setTimeout(function () {
      doc.fonts.load(spec).then(mark, mark);
    }, 6000);
  }

  setState();
  window.addEventListener("load", watchIconFont);
  if (doc.readyState === "loading") doc.addEventListener("DOMContentLoaded", build);
  else build();
  window.officegenieInstall = { prompt: promptInstall, refresh: refresh };
})();
