"""
Vendored and patched version of textual-terminal (v0.3.0).

Original: https://github.com/mitosch/textual-terminal
License: MIT
Patched for Textual 8.x compatibility (removed DEFAULT_COLORS import).
"""

from __future__ import annotations

import os
import fcntl
import signal
import shlex
import asyncio
from asyncio import Task
import pty
import struct
import termios
import re
from pathlib import Path

import pyte
from pyte.screens import Char

from rich.text import Text
from rich.style import Style
from rich.color import ColorParseError

from textual.widget import Widget
from textual.message import Message
from textual import events
from textual import log


class TerminalPyteScreen(pyte.Screen):
    """Overrides the pyte.Screen class to be used with TERM=linux."""

    def set_margins(self, *args, **kwargs):
        kwargs.pop("private", None)
        return super().set_margins(*args, **kwargs)


class TerminalDisplay:
    """Rich display for the terminal."""

    def __init__(self, lines):
        self.lines = lines

    def __rich_console__(self, _console, _options):
        line: Text
        for line in self.lines:
            yield line


_re_ansi_sequence = re.compile(r"(\x1b\[\??[\d;]*[a-zA-Z])")
DECSET_PREFIX = "\x1b[?"


class Terminal(Widget, can_focus=True, inherit_bindings=False):
    """Terminal textual widget."""

    class NextTabRequest(Message):
        """Posted when the user wants to switch to the next tab."""

    class PrevTabRequest(Message):
        """Posted when the user wants to switch to the previous tab."""

    DEFAULT_CSS = """
    Terminal {
        background: $background;
    }
    """

    def __init__(
        self,
        command: str = "bash",
        cwd: str | None = None,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        self.command = command
        self._cwd = cwd

        # default size, will be adapted on_resize
        self.ncol = 80
        self.nrow = 24
        self.mouse_tracking = False

        # variables used when starting the emulator: self.start()
        self.emulator: TerminalEmulator | None = None
        self.send_queue: asyncio.Queue | None = None
        self.recv_queue: asyncio.Queue | None = None
        self.recv_task: Task | None = None

        self.ctrl_keys = {
            "up": "\x1bOA",
            "down": "\x1bOB",
            "right": "\x1bOC",
            "left": "\x1bOD",
            "home": "\x1bOH",
            "end": "\x1b[F",
            "delete": "\x1b[3~",
            "pageup": "\x1b[5~",
            "pagedown": "\x1b[6~",
            "shift+tab": "\x1b[Z",
            "tab": "\t",
            "enter": "\r",
            "backspace": "\x7f",
            "f1": "\x1bOP",
            "f2": "\x1bOQ",
            "f3": "\x1bOR",
            "f4": "\x1bOS",
            "f5": "\x1b[15~",
            "f6": "\x1b[17~",
            "f7": "\x1b[18~",
            "f8": "\x1b[19~",
            "f9": "\x1b[20~",
            "f10": "\x1b[21~",
            "f11": "\x1b[23~",
            "f12": "\x1b[24~",
            # Ctrl key combos (character is None for these in Textual)
            "ctrl+a": "\x01",
            "ctrl+b": "\x02",
            "ctrl+c": "\x03",
            "ctrl+d": "\x04",
            "ctrl+e": "\x05",
            "ctrl+f": "\x06",
            "ctrl+g": "\x07",
            "ctrl+h": "\x08",
            "ctrl+k": "\x0b",
            "ctrl+l": "\x0c",
            "ctrl+n": "\x0e",
            "ctrl+o": "\x0f",
            "ctrl+p": "\x10",
            "ctrl+r": "\x12",
            "ctrl+s": "\x13",
            "ctrl+u": "\x15",
            "ctrl+w": "\x17",
            "ctrl+z": "\x1a",
        }
        self._display = self.initial_display()
        self._screen = TerminalPyteScreen(self.ncol, self.nrow)
        self.stream = pyte.Stream(self._screen)

        super().__init__(name=name, id=id, classes=classes)

    def start(self) -> None:
        if self.emulator is not None:
            return

        self.emulator = TerminalEmulator(command=self.command, cwd=self._cwd)
        self.emulator.start()
        self.send_queue = self.emulator.recv_queue
        self.recv_queue = self.emulator.send_queue
        self.recv_task = asyncio.create_task(self.recv())

    def stop(self) -> None:
        if self.emulator is None:
            return

        self._display = self.initial_display()

        if self.recv_task is not None:
            self.recv_task.cancel()
            self.recv_task = None

        try:
            self.emulator.stop()
        except Exception:
            pass
        self.emulator = None

    def restart(self, cwd: str | None = None) -> None:
        """Stop and restart the terminal, optionally with a new cwd."""
        self.stop()
        if cwd is not None:
            self._cwd = cwd
        self.start()

    def render(self):
        return self._display

    def check_consume_key(self, key: str, character: str | None) -> bool:
        """Claim all keys when the emulator is running.

        This prevents app-level bindings (j/k/t/q, Ctrl+C quit, etc.)
        from intercepting keystrokes meant for the shell.
        """
        return self.emulator is not None

    async def on_key(self, event: events.Key) -> None:
        if self.emulator is None:
            return

        # Tab switching (Ctrl+PageDown / Ctrl+PageUp)
        if event.key == "ctrl+pagedown":
            event.stop()
            self.post_message(self.NextTabRequest())
            return
        if event.key == "ctrl+pageup":
            event.stop()
            self.post_message(self.PrevTabRequest())
            return

        # Ctrl+T or Escape: close the terminal panel (deferred to avoid
        # cancelling the async task that is currently running)
        if event.key in ("ctrl+t", "escape"):
            event.stop()
            self.app.set_timer(0.05, self.app.action_toggle_terminal)
            return

        event.stop()
        char = self.ctrl_keys.get(event.key) or event.character
        if char:
            await self.send_queue.put(["stdin", char])

    async def on_resize(self, _event: events.Resize) -> None:
        if self.emulator is None:
            return

        self.ncol = self.size.width
        self.nrow = self.size.height
        await self.send_queue.put(["set_size", self.nrow, self.ncol])
        self._screen.resize(self.nrow, self.ncol)

    async def on_click(self, event: events.MouseEvent):
        if self.emulator is None:
            return
        if self.mouse_tracking is False:
            return
        await self.send_queue.put(["click", event.x, event.y, event.button])

    async def on_mouse_scroll_down(self, event: events.MouseScrollDown):
        if self.emulator is None:
            return
        if self.mouse_tracking is False:
            return
        await self.send_queue.put(["scroll", "down", event.x, event.y])

    async def on_mouse_scroll_up(self, event: events.MouseScrollUp):
        if self.emulator is None:
            return
        if self.mouse_tracking is False:
            return
        await self.send_queue.put(["scroll", "up", event.x, event.y])

    async def recv(self):
        try:
            while True:
                message = await self.recv_queue.get()
                cmd = message[0]
                if cmd == "setup":
                    await self.send_queue.put(["set_size", self.nrow, self.ncol])
                elif cmd == "stdout":
                    chars = message[1]

                    for sep_match in re.finditer(_re_ansi_sequence, chars):
                        sequence = sep_match.group(0)
                        if sequence.startswith(DECSET_PREFIX):
                            parameters = sequence.removeprefix(DECSET_PREFIX).split(";")
                            if "1000h" in parameters:
                                self.mouse_tracking = True
                            if "1000l" in parameters:
                                self.mouse_tracking = False

                    try:
                        self.stream.feed(chars)
                    except TypeError as error:
                        log.warning("could not feed:", error)

                    lines = []
                    for y in range(self._screen.lines):
                        line_text = Text()
                        line = self._screen.buffer[y]
                        style_change_pos: int = 0
                        for x in range(self._screen.columns):
                            char: Char = line[x]
                            line_text.append(char.data)

                            if x > 0:
                                last_char = line[x - 1]
                                if not self._char_style_cmp(char, last_char) or x == self._screen.columns - 1:
                                    last_style = self._char_rich_style(last_char)
                                    line_text.stylize(last_style, style_change_pos, x + 1)
                                    style_change_pos = x

                            if (
                                self._screen.cursor.x == x
                                and self._screen.cursor.y == y
                            ):
                                line_text.stylize("reverse", x, x + 1)

                        lines.append(line_text)

                    self._display = TerminalDisplay(lines)
                    self.refresh()

                elif cmd == "disconnect":
                    self.stop()
        except asyncio.CancelledError:
            pass

    def _char_rich_style(self, char: Char) -> Style:
        foreground = self._detect_color(char.fg)
        background = self._detect_color(char.bg)

        try:
            style = Style(
                color=foreground if foreground != "default" else None,
                bgcolor=background if background != "default" else None,
                bold=char.bold,
            )
        except ColorParseError:
            style = None
        return style

    @staticmethod
    def _char_style_cmp(given: Char, other: Char) -> bool:
        return (
            given.fg == other.fg
            and given.bg == other.bg
            and given.bold == other.bold
            and given.italics == other.italics
            and given.underscore == other.underscore
            and given.strikethrough == other.strikethrough
            and given.reverse == other.reverse
            and given.blink == other.blink
        )

    @staticmethod
    def _detect_color(color: str) -> str:
        if color == "brown":
            return "yellow"
        if color == "brightblack":
            return "#808080"
        if re.match("[0-9a-f]{6}", color, re.IGNORECASE):
            return f"#{color}"
        return color

    def initial_display(self) -> TerminalDisplay:
        return TerminalDisplay([Text()])


class TerminalEmulator:
    def __init__(self, command: str, cwd: str | None = None):
        self.ncol = 80
        self.nrow = 24
        self.data_or_disconnect = None
        self.run_task: asyncio.Task | None = None
        self.send_task: asyncio.Task | None = None

        self.fd = self._open_terminal(command=command, cwd=cwd)
        self.p_out = os.fdopen(self.fd, "w+b", 0)
        self.recv_queue = asyncio.Queue()
        self.send_queue = asyncio.Queue()
        self.event = asyncio.Event()

    def start(self):
        self.run_task = asyncio.create_task(self._run())
        self.send_task = asyncio.create_task(self._send_data())

    def stop(self):
        self.run_task.cancel()
        self.send_task.cancel()
        try:
            os.kill(self.pid, signal.SIGTERM)
            os.waitpid(self.pid, 0)
        except (ProcessLookupError, ChildProcessError):
            pass

    def _open_terminal(self, command: str, cwd: str | None = None):
        self.pid, fd = pty.fork()
        if self.pid == 0:
            if cwd:
                os.chdir(cwd)
            argv = shlex.split(command)
            env = {
                **os.environ,
                "TERM": "xterm",
            }
            os.execvpe(argv[0], argv, env)
        return fd

    async def _run(self):
        loop = asyncio.get_running_loop()

        def on_output():
            try:
                self.data_or_disconnect = self.p_out.read(65536).decode()
                self.event.set()
            except UnicodeDecodeError:
                pass
            except Exception:
                loop.remove_reader(self.p_out)
                self.data_or_disconnect = None
                self.event.set()

        loop.add_reader(self.p_out, on_output)
        await self.send_queue.put(["setup", {}])
        try:
            while True:
                msg = await self.recv_queue.get()
                if msg[0] == "stdin":
                    self.p_out.write(msg[1].encode())
                elif msg[0] == "set_size":
                    winsize = struct.pack("HH", msg[1], msg[2])
                    fcntl.ioctl(self.fd, termios.TIOCSWINSZ, winsize)
                elif msg[0] == "click":
                    x = msg[1] + 1
                    y = msg[2] + 1
                    button = msg[3]
                    if button == 1:
                        self.p_out.write(f"\x1b[<0;{x};{y}M".encode())
                        self.p_out.write(f"\x1b[<0;{x};{y}m".encode())
                elif msg[0] == "scroll":
                    x = msg[2] + 1
                    y = msg[3] + 1
                    if msg[1] == "up":
                        self.p_out.write(f"\x1b[<64;{x};{y}M".encode())
                    if msg[1] == "down":
                        self.p_out.write(f"\x1b[<65;{x};{y}M".encode())
        except asyncio.CancelledError:
            pass

    async def _send_data(self):
        try:
            while True:
                await self.event.wait()
                self.event.clear()
                if self.data_or_disconnect is not None:
                    await self.send_queue.put(["stdout", self.data_or_disconnect])
                else:
                    await self.send_queue.put(["disconnect", 1])
        except asyncio.CancelledError:
            pass
