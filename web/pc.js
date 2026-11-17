/* Modalità PC: prestito di mouse e tastiera al PC remoto.
 *
 * Il cursore del browser resta visibile e si muove nell'area di
 * destra. Il movimento viene mandato come delta relativo a ydotool:
 * quando il cursore esce dall'area smettiamo di inoltrare mouse e
 * tastiera fisica, ma i toggle CTRL/ALT/META restano premuti sul
 * remoto finché non vengono spenti.
 *
 * I tasti di sistema (Meta, Alt+Tab, ecc.) possono essere intercettati
 * dal SO locale: per questi si usano i toggle della colonna sinistra.
 * Niente fullscreen né Keyboard Lock: la pagina resta in finestra.
 */
(function () {
  "use strict";

  var $ = function (id) {
    return document.getElementById(id);
  };

  var zone = $("lendZone");
  var badge = $("captureBadge");
  var zoneTitle = $("zoneTitle");
  var zoneHint = $("zoneHint");
  var zoneHud = $("zoneHud");
  var zoneFoot = $("zoneFoot");
  var toastEl = $("toast");

  /* Keycode Linux (input-event-codes), non keyCode del browser.
   * F1–F24 sono volutamente esclusi: restano al PC locale, così F5,
   * F11 e F12 continuano a fare quello che devono nel browser. */
  var KEYCODES = {
    Escape: 1,
    Digit1: 2, Digit2: 3, Digit3: 4, Digit4: 5, Digit5: 6,
    Digit6: 7, Digit7: 8, Digit8: 9, Digit9: 10, Digit0: 11,
    Minus: 12, Equal: 13, Backspace: 14, Tab: 15,
    KeyQ: 16, KeyW: 17, KeyE: 18, KeyR: 19, KeyT: 20,
    KeyY: 21, KeyU: 22, KeyI: 23, KeyO: 24, KeyP: 25,
    BracketLeft: 26, BracketRight: 27, Enter: 28, ControlLeft: 29,
    KeyA: 30, KeyS: 31, KeyD: 32, KeyF: 33, KeyG: 34,
    KeyH: 35, KeyJ: 36, KeyK: 37, KeyL: 38,
    Semicolon: 39, Quote: 40, Backquote: 41, ShiftLeft: 42,
    Backslash: 43, KeyZ: 44, KeyX: 45, KeyC: 46, KeyV: 47,
    KeyB: 48, KeyN: 49, KeyM: 50, Comma: 51, Period: 52,
    Slash: 53, ShiftRight: 54, NumpadMultiply: 55,
    AltLeft: 56, Space: 57, CapsLock: 58,
    NumLock: 69, ScrollLock: 70,
    Numpad7: 71, Numpad8: 72, Numpad9: 73, NumpadSubtract: 74,
    Numpad4: 75, Numpad5: 76, Numpad6: 77, NumpadAdd: 78,
    Numpad1: 79, Numpad2: 80, Numpad3: 81, Numpad0: 82,
    NumpadDecimal: 83, IntlBackslash: 86, IntlRo: 89,
    KanaMode: 93, NumpadEnter: 96, ControlRight: 97,
    NumpadDivide: 98, PrintScreen: 99, AltRight: 100,
    Home: 102, ArrowUp: 103, PageUp: 104, ArrowLeft: 105,
    ArrowRight: 106, End: 107, ArrowDown: 108, PageDown: 109,
    Insert: 110, Delete: 111,
    AudioVolumeMute: 113, AudioVolumeDown: 114, AudioVolumeUp: 115,
    Power: 116, NumpadEqual: 117, Pause: 119, NumpadComma: 121,
    Lang1: 122, Lang2: 123, IntlYen: 124, MetaLeft: 125,
    MetaRight: 126, ContextMenu: 127, BrowserStop: 128,
    BrowserBack: 158, BrowserForward: 159,
    MediaTrackNext: 163, MediaPlayPause: 164,
    MediaTrackPrevious: 165, MediaStop: 166,
    Eject: 161, BrowserHome: 172, BrowserRefresh: 173,
    LaunchMail: 155, BrowserFavorites: 156, Help: 138,
    LaunchApplication1: 148, LaunchApplication2: 149,
    MediaSelect: 226, MicMute: 248,
    Sleep: 142, WakeUp: 143,
    BrightnessDown: 224, BrightnessUp: 225
  };

  /* Tasti fisici che corrispondono a un toggle già attivo: non vanno
   * inoltrati come raw, altrimenti il loro keyup spegnerebbe il toggle
   * tenuto dal server. */
  var MODIFIER_BY_CODE = {
    ControlLeft: "ctrl",
    ControlRight: "ctrl",
    AltLeft: "alt",
    AltRight: "alt",
    MetaLeft: "meta",
    MetaRight: "meta"
  };

  var BUTTON_FROM_EVENT = {
    0: "left",
    1: "middle",
    2: "right"
  };

  var settings = {
    mouse_sensitivity: 1.0,
    scroll_px_per_notch: 100,
    allow_web_config_edit: true
  };

  var pointerInside = false;

  var heldKeys = new Set();
  var heldButtons = new Set();
  var heldModifiers = new Set();

  var lastX = null;
  var lastY = null;

  var moveX = 0;
  var moveY = 0;
  var scrollX = 0;
  var scrollY = 0;

  var moveFrame = 0;
  var scrollFrame = 0;
  var toastTimer = 0;

  var conn = window.KMRConnection.create({
    onStatus: function (state) {
      renderStatus(state);

      if (state !== "online") {
        /* il server, quando la connessione cade, rilascia già tutto;
           qui dimentichiamo lo stato locale per non rimandare keyup
           di tasti che non sono più premuti sul remoto */
        heldKeys.clear();
        heldButtons.clear();
        heldModifiers.clear();
        updateModifierButtons();
        updateUi();
      }
    }
  });

  function send(payload) {
    return conn.send(payload);
  }

  function renderStatus(state) {
    var pill = $("connPill");
    var text = $("connTxt");
    var labels = {
      online: "Connesso",
      connecting: "Connessione…",
      offline: "Non connesso",
      stale: "Server bloccato"
    };

    pill.dataset.state = state;
    text.textContent = labels[state] || labels.offline;
  }

  function showToast(text) {
    if (!text) return;

    toastEl.textContent = text;
    toastEl.classList.add("show");

    window.clearTimeout(toastTimer);
    toastTimer = window.setTimeout(function () {
      toastEl.classList.remove("show");
    }, 3400);
  }

  function isEditable(target) {
    if (!(target instanceof Element)) return false;

    return !!target.closest(
      "input, textarea, select, " +
      "[contenteditable=''], [contenteditable='true']"
    );
  }

  function canLend() {
    return (
      pointerInside &&
      document.hasFocus() &&
      document.visibilityState === "visible"
    );
  }

  function modifierLabel() {
    var names = [];

    ["ctrl", "alt", "meta"].forEach(function (name) {
      if (heldModifiers.has(name)) {
        names.push(name.toUpperCase());
      }
    });

    return names.join(" + ");
  }

  function updateModifierButtons() {
    Array.prototype.forEach.call(
      document.querySelectorAll("[data-mod]"),
      function (button) {
        var held = heldModifiers.has(button.dataset.mod);

        button.classList.toggle("held", held);
        button.setAttribute("aria-pressed", held ? "true" : "false");
      }
    );
  }

  function updateUi() {
    var active = canLend();
    var modifiers = modifierLabel();

    zone.classList.toggle("active", active);

    if (!active) {
      badge.textContent = "IN ATTESA";
      zoneTitle.textContent = "AREA INPUT REMOTO";
      zoneHud.textContent = "mouse / scroll / click / tastiera";

      if (modifiers) {
        zoneHint.textContent =
          "Modificatori attivi sul remoto: " + modifiers +
          ". Sposta il cursore nell'area e premi il tasto.";
        zoneFoot.textContent =
          "I toggle restano attivi finché non li spegni / F1–F12 locali";
      } else {
        zoneHint.textContent =
          "Sposta il cursore qui dentro per controllare il PC remoto.";
        zoneFoot.textContent =
          "F1–F12 restano al PC locale / Meta/Alt/Ctrl: usa i toggle";
      }

      return;
    }

    badge.textContent = "MOUSE + TASTI";
    zoneHud.textContent = "mouse / scroll / click / tastiera";
    zoneTitle.textContent = "INPUT AL PC REMOTO";

    if (modifiers) {
      zoneHint.textContent =
        "Mouse, rotella, click e tastiera inoltrati. " +
        "Modificatori attivi sul remoto: " + modifiers + ".";
    } else {
      zoneHint.textContent =
        "Mouse, rotella, click e tastiera inoltrati. " +
        "Per Meta/Alt/Ctrl usa i toggle a sinistra.";
    }

    zoneFoot.textContent =
      "F1–F12 restano al PC locale / Meta/Alt/Ctrl: usa i toggle";
  }

  function cancelMotion() {
    moveX = 0;
    moveY = 0;
    scrollX = 0;
    scrollY = 0;

    if (moveFrame) {
      cancelAnimationFrame(moveFrame);
      moveFrame = 0;
    }

    if (scrollFrame) {
      cancelAnimationFrame(scrollFrame);
      scrollFrame = 0;
    }
  }

  function releaseAll() {
    /* sempre, non solo se il client crede di avere tasti premuti: se
       lo stato locale e quello del server divergono, questo li
       riallinea; il messaggio costa poco ed evita input bloccati */
    send({ action: "release_all" });

    heldKeys.clear();
    heldButtons.clear();
    heldModifiers.clear();
    updateModifierButtons();
    cancelMotion();
    updateUi();
  }

  function releaseTransient() {
    /* uscita dall'area: rilascia mouse e tasti fisici, ma lascia
       premuti i toggle CTRL/ALT/META sul remoto */
    send({ action: "release_transient" });

    heldKeys.clear();
    heldButtons.clear();
    cancelMotion();
  }

  function resetPointerBaseline() {
    lastX = null;
    lastY = null;
  }

  function scheduleMove() {
    if (moveFrame) return;

    moveFrame = requestAnimationFrame(function () {
      moveFrame = 0;

      if (!canLend()) {
        moveX = 0;
        moveY = 0;
        return;
      }

      var dx = Math.trunc(moveX);
      var dy = Math.trunc(moveY);

      if (!dx && !dy) return;

      moveX -= dx;
      moveY -= dy;

      send({ action: "mouse_move", dx: dx, dy: dy });
    });
  }

  function scheduleScroll() {
    if (scrollFrame) return;

    scrollFrame = requestAnimationFrame(function () {
      scrollFrame = 0;

      if (!canLend()) {
        scrollX = 0;
        scrollY = 0;
        return;
      }

      var dx = Math.trunc(scrollX);
      var dy = Math.trunc(scrollY);

      if (!dx && !dy) return;

      scrollX -= dx;
      scrollY -= dy;

      send({ action: "scroll", dx: dx, dy: dy });
    });
  }

  /* ── mouse ─────────────────────────────────────────────────── */

  zone.addEventListener("pointerenter", function (event) {
    pointerInside = true;
    lastX = event.clientX;
    lastY = event.clientY;

    updateUi();
  });

  zone.addEventListener("pointermove", function (event) {
    if (!pointerInside) {
      /* può succedere al primo movimento dopo il caricamento: il
         browser non ha ancora mandato pointerenter */
      pointerInside = true;
      lastX = event.clientX;
      lastY = event.clientY;
      updateUi();
      return;
    }

    if (lastX === null || lastY === null) {
      /* il cursore è rimasto dentro mentre la finestra era in
         background: riparte da qui senza saltare sul remoto */
      lastX = event.clientX;
      lastY = event.clientY;
      return;
    }

    var dx = event.clientX - lastX;
    var dy = event.clientY - lastY;

    lastX = event.clientX;
    lastY = event.clientY;

    if (!canLend() || (!dx && !dy)) return;

    var sensitivity = Number(settings.mouse_sensitivity) || 1;

    moveX += dx * sensitivity;
    moveY += dy * sensitivity;

    scheduleMove();
  });

  zone.addEventListener("pointerleave", function () {
    pointerInside = false;
    resetPointerBaseline();
    releaseTransient();
    updateUi();
  });

  zone.addEventListener("pointerdown", function (event) {
    pointerInside = true;
    lastX = event.clientX;
    lastY = event.clientY;

    if (!canLend()) {
      updateUi();
      return;
    }

    event.preventDefault();
    zone.focus({ preventScroll: true });

    var button = BUTTON_FROM_EVENT[event.button];
    if (!button || heldButtons.has(button)) return;

    heldButtons.add(button);
    send({ action: "mouse_button", button: button, pressed: true });
  });

  window.addEventListener("pointerup", function (event) {
    var button = BUTTON_FROM_EVENT[event.button];

    if (button && heldButtons.has(button)) {
      heldButtons.delete(button);
      send({ action: "mouse_button", button: button, pressed: false });
    }
  }, true);

  window.addEventListener("pointercancel", function () {
    pointerInside = false;
    resetPointerBaseline();
    releaseTransient();
    updateUi();
  });

  zone.addEventListener("contextmenu", function (event) {
    event.preventDefault();
  });

  zone.addEventListener("auxclick", function (event) {
    event.preventDefault();
  });

  zone.addEventListener("wheel", function (event) {
    if (!pointerInside || !canLend()) return;

    event.preventDefault();

    var dx = event.deltaX;
    var dy = event.deltaY;

    /* normalizza in "scatti" di rotella:
       - pixel (Chrome): ~100 px per scatto
       - righe (Firefox): ~3 righe per scatto
       - pagine: 1 pagina = 1 scatto */
    var step = Math.max(
      1,
      Number(settings.scroll_px_per_notch) || 100
    );

    if (event.deltaMode === 1) {
      dx /= 3;
      dy /= 3;
    } else if (event.deltaMode === 0) {
      dx /= step;
      dy /= step;
    }

    if (event.shiftKey && dx === 0) {
      dx = dy;
      dy = 0;
    }

    scrollX += dx;
    scrollY += dy;

    scheduleScroll();
  }, { passive: false });

  /* ── tastiera ──────────────────────────────────────────────── */

  window.addEventListener("keydown", function (event) {
    if (!canLend() || isEditable(event.target)) return;

    var modifier = MODIFIER_BY_CODE[event.code];

    /* il modificatore è già tenuto dal toggle sul remoto: il tasto
       fisico non deve duplicarlo né spegnerlo al keyup */
    if (modifier && heldModifiers.has(modifier)) {
      event.preventDefault();
      event.stopPropagation();
      return;
    }

    var code = KEYCODES[event.code];

    /* niente F1–F24, e niente mapping => il browser resta padrone */
    if (!code) return;

    event.preventDefault();
    event.stopPropagation();

    if (event.repeat || heldKeys.has(event.code)) return;

    heldKeys.add(event.code);
    send({ action: "key_raw", code: code, pressed: true });
  }, true);

  window.addEventListener("keyup", function (event) {
    if (!heldKeys.has(event.code)) return;

    event.preventDefault();
    event.stopPropagation();

    var code = KEYCODES[event.code];

    heldKeys.delete(event.code);

    if (code) {
      send({ action: "key_raw", code: code, pressed: false });
    }
  }, true);

  /* se il focus se ne va (altra finestra, altra app) il server deve
     rilasciare tutto, toggle compresi: il browser non manda keyup di
     tasti persi. Azzeriamo anche pointerInside: mentre la finestra
     non ha il focus il cursore può uscire dall'area senza che noi lo
     sappiamo, quindi al ritorno si riparte solo dopo un movimento. */
  window.addEventListener("blur", function () {
    pointerInside = false;
    resetPointerBaseline();
    releaseAll();
    updateUi();
  });

  window.addEventListener("focus", function () {
    updateUi();
  });

  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState !== "visible") {
      pointerInside = false;
      resetPointerBaseline();
      releaseAll();
    }

    updateUi();
  });

  window.addEventListener("pagehide", function () {
    pointerInside = false;
    resetPointerBaseline();
    releaseAll();
  });

  window.addEventListener("beforeunload", function () {
    releaseAll();
  });

  window.addEventListener("pageshow", function (event) {
    if (event.persisted) {
      pointerInside = false;
      resetPointerBaseline();
      updateUi();
    }
  });

  /* ── app bindings ──────────────────────────────────────────── */

  function renderEmptyBindingSlots() {
    var box = $("bindings");
    box.innerHTML = "";

    for (var slot = 1; slot <= 4; slot++) {
      var button = document.createElement("button");
      button.type = "button";
      button.className = "binding-btn empty";
      button.disabled = true;

      var slotLabel = document.createElement("span");
      slotLabel.className = "binding-slot";
      slotLabel.textContent = "SLOT " + slot;

      var name = document.createElement("span");
      name.className = "binding-name";
      name.textContent = "vuoto";

      button.appendChild(slotLabel);
      button.appendChild(name);
      box.appendChild(button);
    }
  }

  function loadBindings() {
    fetch("/api/bindings")
      .then(function (response) {
        if (!response.ok) throw new Error("bindings");
        return response.json();
      })
      .then(function (data) {
        var bySlot = {};

        data.forEach(function (item) {
          bySlot[Number(item.slot)] = item;
        });

        var box = $("bindings");
        box.innerHTML = "";

        for (var slot = 1; slot <= 4; slot++) {
          var item = bySlot[slot];
          var button = document.createElement("button");
          button.type = "button";
          button.className = "binding-btn";

          if (!item) {
            button.classList.add("empty");
            button.disabled = true;

            var slotLabel = document.createElement("span");
            slotLabel.className = "binding-slot";
            slotLabel.textContent = "SLOT " + slot;

            var emptyName = document.createElement("span");
            emptyName.className = "binding-name";
            emptyName.textContent = "vuoto";

            button.appendChild(slotLabel);
            button.appendChild(emptyName);
            box.appendChild(button);
            continue;
          }

          var label =
            (item.desktop_info && item.desktop_info.name) ||
            item.name ||
            ("Binding " + slot);

          button.title = label;
          button.setAttribute("aria-label", label);

          if (item.has_icon && item.icon_url) {
            var image = document.createElement("img");
            image.alt = "";
            image.src = item.icon_url + "?v=" + Date.now();

            image.addEventListener("error", function (targetImage, targetButton) {
              return function () {
                targetImage.remove();
                targetButton.classList.add("no-icon");
              };
            }(image, button));

            button.appendChild(image);
          } else {
            var fallback = document.createElement("span");
            fallback.className = "binding-fallback";
            fallback.textContent = label.slice(0, 2).toUpperCase();
            button.appendChild(fallback);
          }

          var name = document.createElement("span");
          name.className = "binding-name";
          name.textContent = label;
          button.appendChild(name);

          button.addEventListener("click", function (slotNumber) {
            return function () {
              fetch("/api/binding/" + slotNumber, {
                method: "POST"
              }).catch(function () {});
            };
          }(slot));

          box.appendChild(button);
        }
      })
      .catch(function () {
        renderEmptyBindingSlots();
      });
  }

  /* ── comandi rapidi ────────────────────────────────────────── */

  function loadCommands() {
    fetch("/api/commands")
      .then(function (response) {
        if (!response.ok) throw new Error("commands");
        return response.json();
      })
      .then(function (data) {
        var box = $("commands");
        box.innerHTML = "";

        if (!data.length) {
          var empty = document.createElement("p");
          empty.className = "empty-note";
          empty.textContent = "Nessun comando configurato.";
          box.appendChild(empty);
          return;
        }

        data.forEach(function (command, index) {
          var button = document.createElement("button");
          button.type = "button";
          button.className = "command";
          button.textContent = command.name || ("Comando " + (index + 1));

          button.addEventListener("click", function () {
            fetch("/api/command/" + index, {
              method: "POST"
            }).catch(function () {});
          });

          box.appendChild(button);
        });
      })
      .catch(function () {});
  }

  $("addCommand").addEventListener("submit", function (event) {
    event.preventDefault();

    var form = event.currentTarget;
    var data = new FormData(form);
    var name = String(data.get("name") || "").trim();
    var command = String(data.get("command") || "").trim();

    if (!name || !command) return;

    fetch("/api/commands", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: name, command: command })
    })
      .then(function (response) {
        if (!response.ok) {
          throw new Error("add");
        }

        form.reset();
        loadCommands();
        showToast("Comando aggiunto");
      })
      .catch(function () {
        showToast("Aggiunta non consentita");
      });
  });

  $("commandsToggle").addEventListener("click", function () {
    var scroll = $("commandsScroll");
    var hidden = !scroll.hidden;

    scroll.hidden = hidden;
    this.setAttribute("aria-expanded", hidden ? "false" : "true");
    this.textContent = hidden ? "+" : "−";
  });

  /* ── azioni della colonna sinistra ─────────────────────────── */

  /* I toggle sono composti con gli altri tasti. Prima di un'azione
     standalone (app, comando, HOME, ALT+TAB, ...) invece li
     rilasciamo, per non spedire combinazioni involontarie. */
  document.querySelector(".sidebar").addEventListener(
    "click",
    function (event) {
      var target =
        event.target instanceof Element
          ? event.target.closest("button, a")
          : null;

      if (!target) return;
      if (target.closest(".panel-special")) return;
      if (target.id === "releaseAll") return;

      releaseAll();
    },
    true
  );

  Array.prototype.forEach.call(
    document.querySelectorAll("[data-mod]"),
    function (button) {
      button.addEventListener("click", function () {
        send({
          action: "modifier_toggle",
          key: button.dataset.mod
        });
      });
    }
  );

  Array.prototype.forEach.call(
    document.querySelectorAll("[data-key]"),
    function (button) {
      button.addEventListener("click", function () {
        send({ action: "key", key: button.dataset.key });
      });
    }
  );

  Array.prototype.forEach.call(
    document.querySelectorAll("[data-system]"),
    function (button) {
      button.addEventListener("click", function () {
        send({ action: "system", name: button.dataset.system });
      });
    }
  );

  Array.prototype.forEach.call(
    document.querySelectorAll("[data-switcher]"),
    function (button) {
      button.addEventListener("click", function () {
        send({ action: "switcher" });
        showToast("Selettore finestre: frecce, Invio, Esc");
      });
    }
  );

  $("releaseAll").addEventListener("click", function () {
    releaseAll();
    showToast("Input rilasciato");
  });

  conn.onMessage(function (message) {
    if (!message) return;

    if (message.type === "held_modifiers") {
      heldModifiers.clear();

      (message.keys || []).forEach(function (key) {
        heldModifiers.add(key);
      });

      updateModifierButtons();
      updateUi();
      return;
    }

    if (message.type === "system_result") {
      showToast(
        message.message ||
        (message.ok ? "Fatto" : "Operazione fallita")
      );
    }
  });

  /* ── impostazioni ──────────────────────────────────────────── */

  fetch("/api/pc/settings")
    .then(function (response) {
      if (!response.ok) throw new Error("settings");
      return response.json();
    })
    .then(function (data) {
      if (typeof data.mouse_sensitivity === "number") {
        settings.mouse_sensitivity = data.mouse_sensitivity;
      }

      if (typeof data.scroll_px_per_notch === "number") {
        settings.scroll_px_per_notch = data.scroll_px_per_notch;
      }

      if (data.allow_web_config_edit === false) {
        $("addCommand").hidden = true;
      }
    })
    .catch(function () {});

  updateUi();
  loadBindings();
  loadCommands();
  conn.start();
})();
