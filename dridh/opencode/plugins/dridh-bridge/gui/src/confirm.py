"""Run one Dridh decision command for the desktop app, with the person's typed confirmation.

Dridh asks for a typed confirmation on a terminal (stdin must be a TTY), so scripts and agents cannot approve. The
desktop app has no terminal, so this helper gives the command a pseudo-terminal of its own and relays the prompt:

1. The command runs with stdin and stderr on the pseudo-terminal and stdout on a pipe (its JSON result).
2. When Dridh prints "Type exactly: <phrase>", the helper writes one JSON line to its own stdout:
   {"event": "prompt", "summary": <what Dridh showed>, "expected": <phrase>}.
3. The app shows that in a dialog. The person types the phrase; the app writes their text as one line to this helper's
   stdin, and the helper passes it to Dridh unchanged. Dridh compares it, exactly as on a terminal. An empty stdin
   (the person cancelled) stops the command, and nothing is recorded.
4. When the command ends, the helper writes {"event": "exit", "code", "stdout", "tty"}.

Usage: python3 -I -c "<this file>" '<argv as a JSON array>'
"""

import json
import os
import pty
import re
import select
import signal
import sys
import time

PROMPT = re.compile(r"Type exactly: (.+?)\r?\n")
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
LIMIT = 600  # seconds: a confirmation left open longer is abandoned


def emit(event):
    sys.stdout.write(json.dumps(event) + "\n")
    sys.stdout.flush()


def main():
    argv = json.loads(sys.argv[1])
    out_read, out_write = os.pipe()
    pid, terminal = pty.fork()
    if pid == 0:
        os.close(out_read)
        os.dup2(out_write, 1)
        os.close(out_write)
        try:
            os.execvp(argv[0], argv)
        except OSError as error:
            os.write(2, f"cannot start {argv[0]}: {error}\n".encode())
            os._exit(127)
    os.close(out_write)
    tty, out, prompted = b"", b"", False
    open_fds = [terminal, out_read]
    deadline = time.monotonic() + LIMIT
    while open_fds:
        if time.monotonic() > deadline:
            os.kill(pid, signal.SIGTERM)
            break
        ready, _, _ = select.select(open_fds, [], [], 0.2)
        for fd in ready:
            try:
                chunk = os.read(fd, 65536)
            except OSError:  # the terminal closes with EIO when the command exits
                chunk = b""
            if not chunk:
                open_fds.remove(fd)
                continue
            if fd == out_read:
                out += chunk
                continue
            tty += chunk
            text = ANSI.sub("", tty.decode(errors="replace")).replace("\r", "")
            match = PROMPT.search(text + "\n") if not prompted else None
            if match:
                prompted = True
                emit({"event": "prompt", "summary": text[: match.start()].strip(), "expected": match.group(1).strip()})
                typed = sys.stdin.readline()
                if not typed:
                    os.kill(pid, signal.SIGTERM)
                else:
                    os.write(terminal, typed.rstrip("\n").encode() + b"\n")
    _, status = os.waitpid(pid, 0)
    emit({
        "event": "exit",
        "code": os.waitstatus_to_exitcode(status),
        "stdout": out.decode(errors="replace"),
        "tty": ANSI.sub("", tty.decode(errors="replace")).replace("\r", ""),
    })


main()
