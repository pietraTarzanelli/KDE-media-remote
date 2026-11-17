/* Connessione WebSocket del telecomando.
 *
 * Il telefono sospende le pagine in background: il socket puo' restare
 * "OPEN" nel JavaScript ma essere gia' morto nel TCP. Per questo non ci
 * si limita a riconnettere dopo onclose:
 *
 * - al ritorno in primo piano si verifica subito il socket con un ping;
 * - dopo un'assenza lunga si preferisce un socket nuovo;
 * - su pagehide/freeze si chiude in modo pulito, cosi' al ritorno non
 *   si trascina dietro una connessione zombie;
 * - un ping senza pong entro il timeout chiude e riconnette subito.
 *
 * I tap discreti (frecce, click, testo) vengono messi in una coda breve
 * mentre la connessione si ristabilisce: un tasto premuto durante la
 * riconnessione non va perso, ma non si accumulano comandi per decine
 * di secondi.
 */
(function () {
  "use strict";

  var QUEUEABLE = new Set([
    "key",
    "mouse_click",
    "right_click",
    "modifier_toggle",
    "text",
  ]);

  var QUEUE_TTL_MS = 1500;
  var QUEUE_MAX = 16;

  var PING_INTERVAL_MS = 10000;
  var PONG_TIMEOUT_MS = 3000;
  var RESUME_PONG_TIMEOUT_MS = 800;

  /* attesa breve quando il server e' davvero irraggiungibile */
  var BACKOFF_MS = [300, 600, 1200, 2500, 5000];

  function registerServiceWorker() {
    if (!("serviceWorker" in navigator)) return;

    var secure =
      location.protocol === "https:" ||
      location.hostname === "localhost" ||
      location.hostname === "127.0.0.1" ||
      location.hostname === "::1" ||
      location.hostname === "[::1]";

    if (!secure) return;

    function register() {
      navigator.serviceWorker.register("/sw.js").catch(function () {
        /* niente HTTPS => il browser non registra: pazienza */
      });
    }

    if (document.readyState === "complete") {
      register();
    } else {
      window.addEventListener("load", register);
    }
  }

  function createConnection(options) {
    var opts = options || {};
    var onStatus =
      typeof opts.onStatus === "function"
        ? opts.onStatus
        : function () {};

    var started = false;
    var suspended = false;
    var socket = null;

    var status = "connecting";
    var retryIndex = 0;
    var retryTimer = null;
    var forceImmediate = false;

    var pingTimer = null;
    var pongTimer = null;
    var lastPongAt = 0;

    var hiddenAt = 0;

    var queue = [];
    var messageHandlers = [];

    function setStatus(next) {
      if (status === next) return;
      status = next;
      onStatus(status);
    }

    function wsUrl() {
      var proto =
        location.protocol === "https:" ? "wss:" : "ws:";
      return proto + "//" + location.host + "/ws";
    }

    function stopHeartbeat() {
      if (pingTimer) {
        clearInterval(pingTimer);
        pingTimer = null;
      }
      if (pongTimer) {
        clearTimeout(pongTimer);
        pongTimer = null;
      }
    }

    function startHeartbeat() {
      stopHeartbeat();

      pingTimer = setInterval(function () {
        if (document.visibilityState !== "visible") return;
        if (!socket || socket.readyState !== WebSocket.OPEN) {
          return;
        }
        if (pongTimer) return;

        sendRaw({ action: "ping" });

        pongTimer = setTimeout(function () {
          pongTimer = null;
          if (socket && socket.readyState === WebSocket.OPEN) {
            forceImmediate = true;
            try {
              socket.close(4000, "pong timeout");
            } catch (e) {}
          }
        }, PONG_TIMEOUT_MS);
      }, PING_INTERVAL_MS);
    }

    function sendRaw(payload) {
      if (!socket || socket.readyState !== WebSocket.OPEN) {
        return false;
      }
      try {
        socket.send(JSON.stringify(payload));
        return true;
      } catch (e) {
        return false;
      }
    }

    function queuePayload(payload) {
      if (!payload || !QUEUEABLE.has(payload.action)) return;

      var now = Date.now();
      queue = queue.filter(function (item) {
        return now - item.at <= QUEUE_TTL_MS;
      });

      if (queue.length >= QUEUE_MAX) queue.shift();
      queue.push({ payload: payload, at: now });
    }

    function flushQueue() {
      if (!queue.length) return;

      var now = Date.now();
      var pending = queue;
      queue = [];

      for (var i = 0; i < pending.length; i++) {
        if (now - pending[i].at > QUEUE_TTL_MS) continue;
        sendRaw(pending[i].payload);
      }
    }

    function scheduleReconnect(immediate) {
      if (retryTimer) return;

      if (immediate) {
        retryTimer = setTimeout(function () {
          retryTimer = null;
          connect(false);
        }, 0);
        return;
      }

      var delay =
        BACKOFF_MS[Math.min(retryIndex, BACKOFF_MS.length - 1)];
      retryIndex += 1;

      retryTimer = setTimeout(function () {
        retryTimer = null;
        connect(false);
      }, delay);
    }

    function connect(force) {
      if (!started) return;
      if (suspended && !force) return;

      forceImmediate = false;

      if (
        socket &&
        (socket.readyState === WebSocket.OPEN ||
          socket.readyState === WebSocket.CONNECTING)
      ) {
        if (!force) return;
        try {
          socket.close(1000, "reconnect");
        } catch (e) {}
        socket = null;
      }

      if (retryTimer) {
        clearTimeout(retryTimer);
        retryTimer = null;
      }

      stopHeartbeat();
      setStatus("connecting");

      var next;
      try {
        next = new WebSocket(wsUrl());
      } catch (e) {
        setStatus("offline");
        scheduleReconnect(false);
        return;
      }

      socket = next;

      next.onopen = function () {
        if (socket !== next) return;

        retryIndex = 0;
        forceImmediate = false;
        lastPongAt = Date.now();
        setStatus("online");

        startHeartbeat();
        sendRaw({ action: "ping" });
        flushQueue();
      };

      next.onmessage = function (event) {
        if (socket !== next) return;

        var msg;
        try {
          msg = JSON.parse(event.data);
        } catch (e) {
          return;
        }

        if (msg && msg.type === "pong") {
          lastPongAt = Date.now();
          if (pongTimer) {
            clearTimeout(pongTimer);
            pongTimer = null;
          }
          return;
        }

        for (var i = 0; i < messageHandlers.length; i++) {
          try {
            messageHandlers[i](msg);
          } catch (e) {
            console.error(e);
          }
        }
      };

      next.onerror = function () {
        /* onclose arriva subito dopo: la riconnessione parte da li' */
      };

      next.onclose = function (event) {
        if (socket !== next) return;
        socket = null;
        stopHeartbeat();
        setStatus("offline");

        if (event && event.code === 1008) {
          /* sessione scaduta o revocata: il login e' l'unica uscita */
          var nextPath =
            location.pathname + location.search;
          location.href =
            "/auth/login?next=" + encodeURIComponent(nextPath);
          return;
        }

        if (suspended) return;
        if (!navigator.onLine) return;

        var immediate = forceImmediate;
        forceImmediate = false;
        scheduleReconnect(immediate);
      };
    }

    function send(payload) {
      if (
        !socket ||
        socket.readyState === WebSocket.CLOSED ||
        socket.readyState === WebSocket.CLOSING
      ) {
        queuePayload(payload);
        connect(false);
        return false;
      }

      if (socket.readyState === WebSocket.CONNECTING) {
        queuePayload(payload);
        return false;
      }

      if (sendRaw(payload)) return true;

      queuePayload(payload);
      return false;
    }

    function pingNow() {
      if (!socket || socket.readyState !== WebSocket.OPEN) {
        connect(false);
        return;
      }

      if (pongTimer) return;

      sendRaw({ action: "ping" });

      pongTimer = setTimeout(function () {
        pongTimer = null;
        if (socket && socket.readyState === WebSocket.OPEN) {
          forceImmediate = true;
          try {
            socket.close(4000, "resume timeout");
          } catch (e) {}
        }
      }, RESUME_PONG_TIMEOUT_MS);
    }

    function onVisible() {
      suspended = false;
      document.documentElement.classList.remove("app-suspended");

      var hiddenFor = hiddenAt ? Date.now() - hiddenAt : 0;
      hiddenAt = 0;

      if (hiddenFor > 2000) {
        /* dopo un'assenza vera un socket nuovo e' piu' affidabile
           di uno che sembra aperto ma non risponde piu' */
        connect(true);
        return;
      }

      if (socket && socket.readyState === WebSocket.OPEN) {
        startHeartbeat();
      }
      pingNow();
    }

    function onHidden() {
      /* In background non si riconnette: se NPM o il telefono chiudono
         il socket, si aspetta il ritorno in primo piano. Altrimenti si
         creerebbe un ciclo di connessioni ogni minuto, con sveglia
         inutile del telefono. */
      suspended = true;
      hiddenAt = Date.now();

      /* niente timer in background: il telefono non deve svegliarsi
         ogni 10 s solo per controllare una pagina che non si vede */
      stopHeartbeat();
    }

    function wireLifecycle() {
      document.addEventListener("visibilitychange", function () {
        if (document.visibilityState === "visible") {
          onVisible();
        } else {
          onHidden();
        }
      });

      window.addEventListener("pageshow", function (event) {
        if (event.persisted) {
          suspended = false;
          connect(true);
          return;
        }
        onVisible();
      });

      window.addEventListener("pagehide", function () {
        suspended = true;
        if (socket) {
          try {
            socket.close(1000, "pagehide");
          } catch (e) {}
        }
      });

      window.addEventListener("focus", function () {
        if (document.visibilityState === "visible") {
          pingNow();
        }
      });

      window.addEventListener("online", function () {
        retryIndex = 0;
        if (document.visibilityState === "visible") {
          suspended = false;
          connect(true);
        } else {
          suspended = true;
        }
      });

      window.addEventListener("offline", function () {
        setStatus("offline");
        if (socket) {
          try {
            socket.close(4001, "offline");
          } catch (e) {}
        }
      });

      /* Page Lifecycle API: supportata soprattutto da Chrome Android.
         freeze/resume sono eventi del document, non del window. */
      document.addEventListener("freeze", function () {
        suspended = true;
        if (socket) {
          try {
            socket.close(1000, "freeze");
          } catch (e) {}
        }
      });

      document.addEventListener("resume", function () {
        suspended = false;

        if (
          socket &&
          (socket.readyState === WebSocket.OPEN ||
            socket.readyState === WebSocket.CONNECTING)
        ) {
          /* spesso resume arriva subito dopo visibilitychange: se la
             connessione e' gia' stata ripristinata, non rifarla */
          if (socket.readyState === WebSocket.OPEN) {
            startHeartbeat();
            pingNow();
          }
          return;
        }

        connect(true);
      });
    }

    return {
      start: function () {
        if (started) return;
        started = true;
        wireLifecycle();
        registerServiceWorker();
        onStatus(status);
        connect(false);
      },

      send: send,

      reconnect: function () {
        retryIndex = 0;
        suspended = false;
        connect(true);
      },

      onMessage: function (handler) {
        if (typeof handler === "function") {
          messageHandlers.push(handler);
        }
      },

      getState: function () {
        return status;
      },
    };
  }

  window.KMRConnection = { create: createConnection };
})();
