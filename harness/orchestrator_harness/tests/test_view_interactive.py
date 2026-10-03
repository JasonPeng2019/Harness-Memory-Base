"""PTY, resize, recovery, and scale qualification for the read-only viewer."""

from __future__ import annotations

import copy
import json
import os
import select
import struct
import subprocess
import sys
import time
import unittest
from pathlib import Path

from orchestrator_harness import view_render, view_term

if os.name != "nt":
    import fcntl
    import termios


HARNESS = Path(__file__).resolve().parents[2]


def _lane(number: int) -> dict:
    return {
        "lane_id": f"lane-{number:03d}", "run_id": f"run-{number:03d}",
        "task": f"repair item {number}", "tone": "live", "label": "Working",
        "steps": ["done", "done", "done", "done", "now", "todo", "todo"],
        "current": 4, "provider": "codex", "model": "gpt-test",
        "elapsed_seconds": float(number), "attempts": 1, "resumed": False,
        "keys": [], "orphaned_keys": [], "detail": "reused plan",
        "memory": {"strategy": "standard", "delivered": 1, "omitted": 0,
                   "sources": ["local"], "plan_branch": "direct_fill"},
    }


def _state(count: int = 2) -> dict:
    return {
        "status": "open", "epoch_id": "e" * 32, "mode": "managed",
        "monitor": {"state": "healthy", "age_seconds": 1.0},
        "run_seconds": 12.0, "keys_held": 0,
        "lanes": [_lane(index) for index in range(count)], "needs_you": [],
    }


@unittest.skipIf(os.name == "nt", "POSIX PTY qualification")
class InteractivePtyTests(unittest.TestCase):
    def _spawn(self, *, explode: bool = False):
        import pty

        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 20, 80, 0, 0))
        before = termios.tcgetattr(slave)
        state = json.dumps(_state())
        script = (
            "import json,sys\n"
            "from orchestrator_harness import view_term,view_render\n"
            f"state=json.loads({state!r})\n"
            + (
                "view_render.render=lambda *a,**k: (_ for _ in ()).throw(RuntimeError('render-fault'))\n"
                if explode else
                "calls={'n':0}\n"
                "def collect():\n"
                " calls['n']+=1\n"
                " if calls['n']==2: raise ValueError('temporary-corrupt-record')\n"
                " return state\n"
            )
            + "view_term.run_interactive(collect if 'collect' in globals() else (lambda:state),out=sys.stdout,depth=0,ascii_only=True,refresh_seconds=0.05)\n"
        )
        env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(HARNESS)}
        child = subprocess.Popen(
            [sys.executable, "-c", script], stdin=slave, stdout=slave, stderr=slave,
            cwd=HARNESS, env=env, close_fds=True,
        )
        return child, master, slave, before

    @staticmethod
    def _read(master: int, child: subprocess.Popen, timeout: float = 5.0) -> bytes:
        deadline = time.monotonic() + timeout
        chunks: list[bytes] = []
        while time.monotonic() < deadline:
            ready, _, _ = select.select([master], [], [], 0.05)
            if ready:
                try:
                    chunks.append(os.read(master, 65536))
                except OSError:
                    break
            if child.poll() is not None and not ready:
                break
        return b"".join(chunks)

    def test_keys_resize_changing_state_and_terminal_restore(self) -> None:
        child, master, slave, before = self._spawn()
        try:
            time.sleep(0.25)
            os.write(master, b"\x1b[B")  # down
            time.sleep(0.12)
            os.write(master, b"\r")      # details
            time.sleep(0.12)
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 32, 120, 0, 0))
            time.sleep(0.2)
            os.write(master, b"q")
            output = self._read(master, child)
            self.assertEqual(0, child.wait(5), output.decode(errors="replace"))
            after = termios.tcgetattr(slave)
            self.assertEqual(before, after)
            self.assertIn(b"\x1b[?1049h", output)
            self.assertIn(b"\x1b[?1049l", output)
            self.assertGreaterEqual(output.count(b"\x1b[2J"), 2)
            self.assertIn(b"lane-001", output)
        finally:
            if child.poll() is None:
                child.terminate()
                child.wait(5)
            os.close(master)
            os.close(slave)

    def test_render_exception_still_restores_terminal_and_leaves_alt_screen(self) -> None:
        child, master, slave, before = self._spawn(explode=True)
        try:
            output = self._read(master, child)
            self.assertNotEqual(0, child.wait(5))
            self.assertEqual(before, termios.tcgetattr(slave))
            self.assertIn(b"render-fault", output)
            self.assertIn(b"\x1b[?1049l", output)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(5)
            os.close(master)
            os.close(slave)


class ViewerScaleAndKeyContractTests(unittest.TestCase):
    def test_large_lane_set_is_bounded_and_read_only(self) -> None:
        state = _state(250)
        before = copy.deepcopy(state)
        started = time.monotonic()
        lines = view_render.render(
            state, 160, 50, selected=249, depth=view_render.NO_COLOR,
            ascii_only=True, details=False,
        )
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertEqual(50, len(lines))
        self.assertTrue(all(view_render.text_width(line) == 160 for line in lines))
        self.assertTrue(any("lane-249" in line for line in lines))
        self.assertEqual(before, state)

    def test_portable_key_mapping_contract(self) -> None:
        self.assertEqual("up", view_term._map_char("k"))
        self.assertEqual("down", view_term._map_char("j"))
        self.assertEqual("enter", view_term._map_char("\r"))
        self.assertEqual("escape", view_term._map_char("\x1b"))
        self.assertEqual("q", view_term._map_char("Q"))
        self.assertIsNone(view_term._map_char("x"))
