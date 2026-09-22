(function () {
  var RESET_QUERY = "reset";
  var CHECK_DELAY_MS = 7000;

  function queryHas(name) {
    return new RegExp("(?:^|[?&])" + name + "(?:=|&|$)").test(window.location.search);
  }

  function ping(eventName) {
    try {
      var img = new Image();
      img.src = "/api/health?boot=" + encodeURIComponent(eventName) + "&t=" + Date.now();
    } catch (_error) {
      // Best-effort diagnostic only.
    }
  }

  function clearBrowserState() {
    var tasks = [];

    try {
      if ("serviceWorker" in navigator) {
        tasks.push(
          navigator.serviceWorker.getRegistrations().then(function (registrations) {
            return Promise.all(
              registrations.map(function (registration) {
                return registration.unregister();
              })
            );
          })
        );
      }
    } catch (_error) {
      // Continue with cache cleanup.
    }

    try {
      if ("caches" in window) {
        tasks.push(
          caches.keys().then(function (keys) {
            return Promise.all(
              keys.map(function (key) {
                return caches.delete(key);
              })
            );
          })
        );
      }
    } catch (_error) {
      // Continue with reload.
    }

    Promise.all(tasks.map(function (task) {
      return task.catch(function () {});
    })).then(function () {
      window.location.replace("/?fresh=" + Date.now());
    });
  }

  function showFallback(reason) {
    var root = document.getElementById("root");
    if (!root || root.children.length > 0 || root.textContent.trim()) return;

    var wrap = document.createElement("main");
    wrap.style.cssText = [
      "font-family: Tahoma, Segoe UI, Arial, sans-serif",
      "min-height: 100vh",
      "display: flex",
      "align-items: center",
      "justify-content: center",
      "padding: 24px",
      "background: #f7f8fa",
      "color: #111"
    ].join(";");

    var panel = document.createElement("section");
    panel.style.cssText = [
      "max-width: 520px",
      "width: 100%",
      "border: 1px solid #d9d9d9",
      "border-radius: 8px",
      "background: #fff",
      "padding: 24px",
      "box-shadow: 0 12px 32px rgba(0,0,0,.08)"
    ].join(";");

    var title = document.createElement("h1");
    title.textContent = "Приложение не запустилось";
    title.style.cssText = "margin:0 0 12px;font-size:22px;line-height:1.25;color:#c00000";

    var text = document.createElement("p");
    text.textContent =
      "Safari или браузер на iPad мог оставить старый PWA-кэш. Сбросьте кэш приложения и откройте сайт заново.";
    text.style.cssText = "margin:0 0 18px;font-size:15px;line-height:1.5;color:#333";

    var small = document.createElement("p");
    small.textContent = reason || "";
    small.style.cssText = "margin:0 0 18px;font-size:12px;line-height:1.45;color:#777";

    var button = document.createElement("button");
    button.type = "button";
    button.textContent = "Сбросить кэш приложения";
    button.style.cssText = [
      "width:100%",
      "border:0",
      "border-radius:6px",
      "background:#c00000",
      "color:#fff",
      "font:700 15px Tahoma, Segoe UI, Arial, sans-serif",
      "padding:13px 16px"
    ].join(";");
    button.addEventListener("click", clearBrowserState);

    panel.appendChild(title);
    panel.appendChild(text);
    panel.appendChild(small);
    panel.appendChild(button);
    wrap.appendChild(panel);
    root.appendChild(wrap);
  }

  window.AZS_CLEAR_APP_CACHE = clearBrowserState;
  ping("boot-check-loaded");

  if (queryHas(RESET_QUERY)) {
    ping("reset-requested");
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", clearBrowserState, { once: true });
    } else {
      clearBrowserState();
    }
    return;
  }

  window.addEventListener("error", function (event) {
    ping("window-error");
    setTimeout(function () {
      showFallback(event && event.message ? event.message : "Ошибка загрузки скрипта.");
    }, 0);
  });

  window.addEventListener("unhandledrejection", function () {
    ping("unhandled-rejection");
  });

  setTimeout(function () {
    showFallback("React не смонтировал интерфейс за " + Math.round(CHECK_DELAY_MS / 1000) + " секунд.");
  }, CHECK_DELAY_MS);
})();
