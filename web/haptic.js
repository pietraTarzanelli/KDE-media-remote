/* Feedback aptico su iOS.
 *
 * Storia breve: fino a iOS 26.4 bastava creare uno <input type="checkbox"
 * switch> e chiamare label.click() da script per far suonare il Taptic
 * Engine. Da iOS 26.5 WebKit accetta l'haptic solo se il dito tocca
 * fisicamente il controllo, quindi quella via e' morta.
 *
 * Qui non "triggeriamo" piu' niente: mettiamo uno switch nativo vero,
 * invisibile, SOPRA ogni pulsante. Il dito tocca il controllo (il sistema
 * suona il tick) e il tap risale al pulsante, che continua a fare il suo
 * lavoro senza modifiche.
 *
 * Limiti che restano: un solo tipo di tick (niente intensita' ne'
 * success/error), e solo su un tap reale: un evento arrivato dal server non
 * puo' far vibrare il telefono.
 */
(function () {
  "use strict";

  var STORAGE_KEY = "kmr.haptic";
  var CLASS = "kmr-haptic-switch";
  var PRESSED = "kmr-pressed";
  var OFF_CLASS = "kmr-haptic-off";

  var ua = navigator.userAgent || "";
  var isIOS =
    /iPad|iPhone|iPod/.test(ua) ||
    (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);

  var enabled = true;
  try {
    enabled = localStorage.getItem(STORAGE_KEY) !== "0";
  } catch (err) {}

  function hasSwitch(el) {
    for (var i = 0; i < el.children.length; i++) {
      if (el.children[i].classList.contains(CLASS)) return true;
    }
    return false;
  }

  /* Aggiunge l'overlay a un elemento interattivo gia' nel DOM.
   * Va chiamato dopo l'inserimento, cosi' getComputedStyle() e' valido.
   * Un elemento con l'attributo data-no-haptic resta senza tick: e' cosi'
   * che si escludono singoli pulsanti senza toccare la logica JS. */
  function attach(el) {
    if (
      !isIOS ||
      !el ||
      el.nodeType !== 1 ||
      el.hasAttribute("data-no-haptic") ||
      hasSwitch(el)
    ) return false;

    try {
      var style = getComputedStyle(el);

      /* position:relative non sposta nulla ma serve da riferimento per
       * l'overlay. I tasti del D-pad sono gia' posizionati in assoluto. */
      if (style.position === "static") el.style.position = "relative";

      var input = document.createElement("input");
      input.type = "checkbox";
      input.setAttribute("switch", "");
      input.classList.add(CLASS);
      input.tabIndex = -1;
      input.setAttribute("aria-hidden", "true");

      /* clip-path ritaglia anche l'area che riceve il tocco: senza,
       * resterebbero angoli attivi fuori dalla forma del tasto. */
      var radius = style.borderRadius;
      if (radius && radius !== "0px") {
        input.style.clipPath = "inset(0 round " + radius + ")";
      }

      /* Il tap cade sullo switch, non sul pulsante: :active non parte.
       * Questa classe riproduce l'effetto "premuto" (vedi haptic.css). */
      input.addEventListener("pointerdown", function () {
        el.classList.add(PRESSED);
      }, { passive: true });

      var release = function () {
        el.classList.remove(PRESSED);
      };
      input.addEventListener("pointerup", release);
      input.addEventListener("pointercancel", release);
      input.addEventListener("pointerleave", release);

      el.appendChild(input);
      return true;
    } catch (err) {
      return false;
    }
  }

  function attachAll(selector, root) {
    var list = (root || document).querySelectorAll(selector);
    for (var i = 0; i < list.length; i++) attach(list[i]);
  }

  /* true quando il pointerdown e' partito dallo switch: in quel caso il
   * chiamante non deve fare preventDefault(), altrimenti annulla il toggle
   * nativo e quindi l'haptic. */
  function fromSwitch(event) {
    var target = event && event.target;
    return !!(target && target.classList &&
      target.classList.contains(CLASS));
  }

  function applyEnabled() {
    if (document.body) {
      document.body.classList.toggle(OFF_CLASS, !enabled);
    }
  }

  function setEnabled(value) {
    enabled = !!value;
    try {
      localStorage.setItem(STORAGE_KEY, enabled ? "1" : "0");
    } catch (err) {}
    applyEnabled();
    return enabled;
  }

  function isEnabled() {
    return enabled;
  }

  window.KMRHaptic = {
    available: isIOS,
    attach: attach,
    attachAll: attachAll,
    fromSwitch: fromSwitch,
    setEnabled: setEnabled,
    isEnabled: isEnabled
  };

  applyEnabled();
})();
