"""A real interactive terminal for the side panel: a login shell in a pseudo-terminal.
macOS/Linux use the standard pty module; Windows uses ConPTY through pywinpty."""

from __future__ import annotations

import codecs
import os
import secrets
import shutil
import threading


class Terminal:
    def __init__(self, cwd: str, on_output, on_exit, cols: int = 100, rows: int = 30):
        self.id = secrets.token_hex(4)
        self.on_output, self.on_exit = on_output, on_exit
        self.alive = True
        if os.name == "nt":
            from winpty import PtyProcess

            shell = shutil.which("pwsh") or shutil.which("powershell") or "cmd.exe"
            self.proc = PtyProcess.spawn(shell, cwd=cwd, dimensions=(rows, cols))
            threading.Thread(target=self._read_windows, daemon=True).start()
        else:
            import pty

            shell = os.environ.get("SHELL") or shutil.which("zsh") or shutil.which("bash") or "/bin/sh"
            pid, fd = pty.fork()
            if pid == 0:  # child: become the shell
                try:
                    os.chdir(cwd)
                except OSError:
                    pass
                env = {**os.environ, "TERM": "xterm-256color", "COLORTERM": "truecolor"}
                os.execvpe(shell, [shell, "-l"], env)
            self.pid, self.fd = pid, fd
            self.resize(cols, rows)
            threading.Thread(target=self._read_unix, daemon=True).start()

    def _read_unix(self):
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        while self.alive:
            try:
                data = os.read(self.fd, 65536)
            except OSError:
                break
            if not data:
                break
            self.on_output(self.id, decoder.decode(data))
        self._finish()

    def _read_windows(self):
        while self.alive:
            try:
                data = self.proc.read(65536)
            except EOFError:
                break
            except Exception:
                if not self.proc.isalive():
                    break
                continue
            if data:
                self.on_output(self.id, data)
        self._finish()

    def _finish(self):
        if self.alive:
            self.alive = False
            self.on_exit(self.id)

    def write(self, data: str):
        if not self.alive:
            return
        if os.name == "nt":
            self.proc.write(data)
        else:
            os.write(self.fd, data.encode())

    def resize(self, cols: int, rows: int):
        cols, rows = max(20, min(int(cols), 500)), max(5, min(int(rows), 200))
        if os.name == "nt":
            self.proc.setwinsize(rows, cols)
        else:
            import fcntl
            import struct
            import termios

            fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    def close(self):
        self.alive = False
        try:
            if os.name == "nt":
                self.proc.terminate(force=True)
            else:
                import signal

                os.kill(self.pid, signal.SIGHUP)
                os.close(self.fd)
                os.waitpid(self.pid, os.WNOHANG)
        except Exception:
            pass
