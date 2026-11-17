import asyncio
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass

# Linux input-event-codes.h keycodes
KEYS = {
    "esc": 1,
    "backspace": 14,
    "tab": 15,
    "enter": 28,
    "ctrl": 29,
    "a": 30,
    "c": 46,
    "d": 32,
    "f": 33,
    "v": 47,
    "x": 45,
    "alt": 56,
    "space": 57,
    "f4": 62,
    "up": 103,
    "left": 105,
    "right": 106,
    "down": 108,
    "meta": 125,

    # multimedia
    "volume_down": 114,
    "volume_up": 115,
    "power": 116,
    "pause": 119,
    "mute": 113,
    "play_pause": 164,
    "previous": 165,
    "next": 163,
}


def _run(*args: str):
    return subprocess.run(
        ["ydotool", *args],
        check=False,
        capture_output=True,
        text=True,
    )


def key_down(name: str):
    code = KEYS[name]
    return _run("key", f"{code}:1")


def key_up(name: str):
    code = KEYS[name]
    return _run("key", f"{code}:0")


def key_tap(name: str):
    # "home" mostra il desktop su Plasma: e' una combo Meta+D, non un
    # singolo keycode, quindi non passa da KEYS.
    if name == "home":
        return _run(
            "key",
            f"{KEYS['meta']}:1",
            f"{KEYS['d']}:1",
            f"{KEYS['d']}:0",
            f"{KEYS['meta']}:0",
        )

    # Chiude la finestra in primo piano: e' la scorciatoia globale KDE
    # Alt+F4, quindi anche qui serve una combo invece di un keycode.
    if name == "close_window":
        return _run(
            "key",
            f"{KEYS['alt']}:1",
            f"{KEYS['f4']}:1",
            f"{KEYS['f4']}:0",
            f"{KEYS['alt']}:0",
        )

    code = KEYS[name]
    return _run("key", f"{code}:1", f"{code}:0")


def task_switcher_open():
    """Apre il selettore finestre di KDE (Alt+Tab) lasciando Alt premuto.

    Con Alt ancora giu' il menu resta aperto: si naviga con le frecce e
    si conferma con Invio (o si annulla con Esc). Va poi rilasciato con
    task_switcher_close(), altrimenti Alt resterebbe premuto per tutti i
    tasti successivi.

    Alt e Tab non vanno nello stesso invio di ydotool: con una pausa
    breve fra i due eventi KWin apre il tabbox in modo affidabile su
    Plasma (verificato con scripting KWin su Bigscreen).
    """
    _run("key", f"{KEYS['alt']}:1")

    time.sleep(0.04)

    return _run(
        "key",
        f"{KEYS['tab']}:1",
        f"{KEYS['tab']}:0",
    )


def task_switcher_close():
    return _run("key", f"{KEYS['alt']}:0")


def type_text(text: str):
    return _run("type", text)


def mouse_move(dx: int, dy: int):
    return _run("mousemove", "-x", str(dx), "-y", str(dy))


def left_click():
    return _run("click", "0xC0")


def right_click():
    return _run("click", "0xC1")


def scroll_up():
    return _run(
        "mousemove",
        "-w",
        "--",
        "0",
        "1",
    )


def scroll_down():
    return _run(
        "mousemove",
        "-w",
        "--",
        "0",
        "-1",
    )


def middle_click():
    return _run("click", "0xC2")


# ── input raw per la modalità PC ────────────────────────────────

# ydotool click: i valori bassi sono il bottone, 0x40 = down,
# 0x80 = up. Servono entrambi per drag e pressioni prolungate.
BUTTON_CODES = {
    "left": (0x40, 0x80),
    "right": (0x41, 0x81),
    "middle": (0x42, 0x82),
}

MOVE_LIMIT = 1000
SCROLL_LIMIT = 20


def _clamp_int(value, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return max(low, min(high, number))


def _valid_keycode(value):
    try:
        code = int(value)
    except (TypeError, ValueError):
        return None
    if 1 <= code <= 255:
        return code
    return None


def _execute_pump_item(kind: str, args):
    if kind == "key":
        code = _valid_keycode(args[0])
        if code is None:
            return
        pressed = 1 if args[1] else 0
        _run("key", f"{code}:{pressed}")
        return

    if kind == "button":
        code = _clamp_int(args[0], 0, 0xFF)
        _run("click", hex(code))
        return

    dx = _clamp_int(args[0], -MOVE_LIMIT, MOVE_LIMIT)
    dy = _clamp_int(args[1], -MOVE_LIMIT, MOVE_LIMIT)

    if kind == "move":
        if dx or dy:
            _run(
                "mousemove",
                "-x",
                str(dx),
                "-y",
                str(dy),
            )

    elif kind == "scroll":
        # Il browser usa delta positivi verso il basso; ydotool vuole
        # valori positivi verso l'alto.
        dx = _clamp_int(dx, -SCROLL_LIMIT, SCROLL_LIMIT)
        dy = _clamp_int(dy, -SCROLL_LIMIT, SCROLL_LIMIT)
        if dx or dy:
            _run(
                "mousemove",
                "-w",
                "--",
                str(dx),
                str(-dy),
            )


_pump_error_shown = False


def _run_pump_item(kind: str, args):
    """Esegue un comando del pump senza far morire il thread.

    Se ydotool manca o il daemon non risponde, un'eccezione nel thread
    lascerebbe la coda piena per sempre: la registriamo una volta e
    continuiamo a svuotare gli eventi successivi.
    """
    global _pump_error_shown

    try:
        _execute_pump_item(kind, args)
    except Exception as exc:  # noqa: BLE001
        if not _pump_error_shown:
            _pump_error_shown = True
            print(
                f"[input] ydotool non disponibile: {exc}",
                file=sys.stderr,
                flush=True,
            )


class InputPump:
    """Esegue i comandi ydotool fuori dal loop asyncio.

    Movimento e scroll arrivano dal browser a raffica. Il pump tiene
    il primo evento per una finestra breve e somma quelli che arrivano
    nello stesso intervallo, così non si avvia un processo ydotool per
    ogni pixel. Tasti e bottoni non vengono accorpati e restano in
    ordine.
    """

    def __init__(self, flush_ms: int = 12):
        self._queue = deque()
        self._cv = threading.Condition()
        self._closed = False
        self._flush_s = max(0.0, flush_ms / 1000.0)
        self._thread = threading.Thread(
            target=self._run,
            name="kmr-input-pump",
            daemon=True,
        )
        self._thread.start()

    def submit(self, kind: str, *args) -> bool:
        with self._cv:
            if self._closed:
                return False
            self._queue.append((kind, args))
            self._cv.notify()
            return True

    def close(self):
        with self._cv:
            self._closed = True
            self._cv.notify()

    def _next(self):
        with self._cv:
            while not self._queue and not self._closed:
                self._cv.wait()
            if not self._queue:
                return None
            return self._queue.popleft()

    def _run(self):
        while True:
            item = self._next()
            if item is None:
                return

            kind, args = item

            if kind not in ("move", "scroll"):
                _run_pump_item(kind, args)
                continue

            dx = _clamp_int(args[0], -MOVE_LIMIT, MOVE_LIMIT)
            dy = _clamp_int(args[1], -MOVE_LIMIT, MOVE_LIMIT)
            deadline = time.monotonic() + self._flush_s

            # Accorpa solo gli eventi dello stesso tipo che sono in
            # testa alla coda. Se arriva un tasto o un bottone, non lo
            # si scavalca: l'ordine con click e pressioni deve restare
            # quello inviato dal client.
            while True:
                with self._cv:
                    while (
                        self._queue
                        and self._queue[0][0] == kind
                    ):
                        next_args = self._queue.popleft()[1]
                        dx += _clamp_int(
                            next_args[0],
                            -MOVE_LIMIT,
                            MOVE_LIMIT,
                        )
                        dy += _clamp_int(
                            next_args[1],
                            -MOVE_LIMIT,
                            MOVE_LIMIT,
                        )

                    remaining = deadline - time.monotonic()
                    if self._closed or remaining <= 0:
                        break

                    if self._queue:
                        break

                    self._cv.wait(timeout=remaining)

            _run_pump_item(kind, (dx, dy))


_pump = None
_pump_lock = threading.Lock()


def input_pump() -> InputPump:
    global _pump

    with _pump_lock:
        if _pump is None:
            _pump = InputPump()

    return _pump


def queue_raw_key(code, pressed: bool) -> bool:
    """Invia un keycode Linux raw (1..255) come down/up."""
    code = _valid_keycode(code)
    if code is None:
        return False
    return input_pump().submit(
        "key",
        code,
        bool(pressed),
    )


def queue_mouse_move(dx, dy) -> bool:
    return input_pump().submit(
        "move",
        _clamp_int(dx, -MOVE_LIMIT, MOVE_LIMIT),
        _clamp_int(dy, -MOVE_LIMIT, MOVE_LIMIT),
    )


def queue_mouse_button(button: str, pressed: bool) -> bool:
    codes = BUTTON_CODES.get(button)
    if not codes:
        return False

    return input_pump().submit(
        "button",
        codes[0] if pressed else codes[1],
    )


def queue_scroll(dx, dy) -> bool:
    return input_pump().submit(
        "scroll",
        _clamp_int(dx, -MOVE_LIMIT, MOVE_LIMIT),
        _clamp_int(dy, -MOVE_LIMIT, MOVE_LIMIT),
    )


@dataclass
class KeyRepeater:
    """Ripete una pressione di tasti finche' resta premuta.

    Usato per le frecce del D-pad: un tap breve equivale a una singola
    pressione, tenendo premuto invece la ripete con velocita' che
    cresce piano (intervallo * accel_factor per ciclo, floored a
    min_repeat_ms) per non far scattare il cursore del video.

    La ripetizione non parte subito: dopo il primo tap aspetta
    hold_delay_ms, cosi' un appoggio accidentale del dito resta una
    singola pressione e non fa scorrere due elementi.
    """

    hold_delay_ms: int = 400
    repeat_ms: int = 150
    min_repeat_ms: int = 70
    accel_after_ms: int = 600
    accel_factor: float = 0.85

    def __post_init__(self):
        self.tasks: dict[str, asyncio.Task] = {}

    async def start(self, key: str):
        await self.stop(key)
        self.tasks[key] = asyncio.create_task(
            self._loop(key)
        )

    async def stop(self, key: str | None = None):
        targets = (
            [key]
            if key
            else list(self.tasks.keys())
        )

        for k in targets:
            task = self.tasks.pop(k, None)

            if task:
                task.cancel()

                try:
                    await task
                except asyncio.CancelledError:
                    pass

    async def _loop(self, key: str):
        loop = asyncio.get_running_loop()

        # il primo tap esce subito: un tocco breve resta una singola
        # pressione, come prima di introdurre il ripetitore
        key_tap(key)

        # la ripetizione attende che la pressione sia "tenuta" davvero
        await asyncio.sleep(
            self.hold_delay_ms / 1000
        )

        started = loop.time()
        interval_ms = float(self.repeat_ms)

        while True:
            await asyncio.sleep(
                interval_ms / 1000
            )

            key_tap(key)

            held_ms = (
                loop.time() - started
            ) * 1000

            if held_ms >= self.accel_after_ms:
                interval_ms = max(
                    self.min_repeat_ms,
                    interval_ms * self.accel_factor,
                )


@dataclass
class MouseRepeater:
    step: int = 22
    repeat_ms: int = 70
    accel_after_ms: int = 500
    accel_multiplier: float = 2.2

    def __post_init__(self):
        self.tasks: dict[str, asyncio.Task] = {}

    async def start(self, direction: str):
        await self.stop(direction)
        self.tasks[direction] = asyncio.create_task(
            self._loop(direction)
        )

    async def stop(self, direction: str | None = None):
        targets = (
            [direction]
            if direction
            else list(self.tasks.keys())
        )

        for d in targets:
            task = self.tasks.pop(d, None)

            if task:
                task.cancel()

                try:
                    await task
                except asyncio.CancelledError:
                    pass

    async def _loop(self, direction: str):
        loop = asyncio.get_running_loop()
        started = loop.time()

        while True:
            held_ms = (
                loop.time() - started
            ) * 1000

            amount = self.step

            if held_ms >= self.accel_after_ms:
                amount = round(
                    self.step *
                    self.accel_multiplier
                )

            dx, dy = {
                "up": (0, -amount),
                "down": (0, amount),
                "left": (-amount, 0),
                "right": (amount, 0),
            }[direction]

            mouse_move(dx, dy)

            await asyncio.sleep(
                self.repeat_ms / 1000
            )
