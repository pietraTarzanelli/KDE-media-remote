import asyncio
import subprocess
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
    code = KEYS[name]
    return _run("key", f"{code}:1", f"{code}:0")

def type_text(text: str):
    return _run("type", text)

def mouse_move(dx: int, dy: int):
    return _run("mousemove", "-x", str(dx), "-y", str(dy))

def left_click():
    return _run("click", "0xC0")

def right_click():
    return _run("click", "0xC1")


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
        self.tasks[direction] = asyncio.create_task(self._loop(direction))

    async def stop(self, direction: str | None = None):
        targets = [direction] if direction else list(self.tasks.keys())
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
            held_ms = (loop.time() - started) * 1000
            amount = self.step

            # Dopo 500 ms aumenta lo step: effetto telecomando TV / Fire TV.
            if held_ms >= self.accel_after_ms:
                amount = round(self.step * self.accel_multiplier)

            dx, dy = {
                "up": (0, -amount),
                "down": (0, amount),
                "left": (-amount, 0),
                "right": (amount, 0),
            }[direction]

            mouse_move(dx, dy)
            await asyncio.sleep(self.repeat_ms / 1000)
