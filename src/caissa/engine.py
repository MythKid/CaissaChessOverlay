"""Stockfish (UCI) integration via python-chess.

Wraps chess.engine.SimpleEngine so the rest of the app never touches the UCI
protocol directly. A single ChessEngine instance is owned by the analysis
worker thread and used only on that thread.

Two analysis modes:
  * analyse()        - classic blocking search (used by the self-test).
  * analyse_stream() - streams the search live: the caller gets a callback on
                       every depth improvement (instant first move, live
                       refinement) and can abort mid-search the moment the
                       on-screen position changes. This is what keeps the app
                       responsive at full strength.
"""
from __future__ import annotations

import os
import subprocess
import time

import chess
import chess.engine

# On Windows, launch the engine subprocess WITHOUT a console window so no
# "cmd" flashes up when the app starts the engine. `creationflags` is a
# Windows-only Popen argument, so on macOS/Linux we don't pass it at all.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_POPEN_ARGS = {"creationflags": _NO_WINDOW} if os.name == "nt" else {}

# Windows STATUS_ILLEGAL_INSTRUCTION (0xC000001D), as both the unsigned exit
# code subprocess reports and the signed form some wrappers surface. This is
# what the bundled AVX2 Stockfish dies with on a CPU that lacks AVX2.
_ILLEGAL_INSTRUCTION_CODES = (3221225501, -1073741795)

CPU_UNSUPPORTED_MSG = (
    "This computer's CPU can't run the built-in chess engine.\n\n"
    "The bundled Stockfish requires AVX2 instructions (Intel CPUs from "
    "~2013, AMD from ~2015). On this machine the engine crashes the moment "
    "it starts, so no move recommendations can be shown.\n\n"
    "Fix: download a non-AVX2 Stockfish build (the \"x86-64\" or "
    "\"x86-64-sse41-popcnt\" download) from stockfishchess.org, then select "
    "it under Settings → Custom engine."
)


def is_cpu_unsupported(exc: BaseException) -> bool:
    """True if the exception looks like the engine dying with an illegal
    instruction - i.e. the binary uses CPU instructions this machine lacks."""
    text = repr(exc)
    return any(str(code) in text for code in _ILLEGAL_INSTRUCTION_CODES)


def health_check(path: str | None) -> tuple[bool, str]:
    """Quickly verify the engine binary actually runs on this machine.

    Runs the engine directly with a plain `uci` handshake (no python-chess,
    no asyncio) so we get the raw process exit code - which lets us tell a
    CPU-incompatibility crash apart from a missing or broken binary.
    Returns (ok, human-readable problem description).
    """
    if not path or not os.path.isfile(path):
        return False, "Chess engine not found. Reinstall the app."
    try:
        proc = subprocess.run(
            [path], input="uci\nquit\n", capture_output=True, text=True,
            timeout=15, **_POPEN_ARGS)
    except subprocess.TimeoutExpired:
        return False, "The chess engine did not respond (timed out)."
    except OSError as e:
        return False, f"The chess engine could not be launched: {e}"
    if proc.returncode in _ILLEGAL_INSTRUCTION_CODES:
        return False, CPU_UNSUPPORTED_MSG
    if "uciok" not in (proc.stdout or ""):
        return False, ("The chess engine started but did not answer "
                       f"correctly (exit code {proc.returncode}).")
    return True, "ok"


def _total_ram_mb() -> int:
    """Total physical RAM in MB (best effort; assumes 8 GB if unknown)."""
    if os.name == "nt":
        try:
            import ctypes

            class _MemoryStatusEx(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_uint32),
                            ("dwMemoryLoad", ctypes.c_uint32),
                            ("ullTotalPhys", ctypes.c_uint64),
                            ("ullAvailPhys", ctypes.c_uint64),
                            ("ullTotalPageFile", ctypes.c_uint64),
                            ("ullAvailPageFile", ctypes.c_uint64),
                            ("ullTotalVirtual", ctypes.c_uint64),
                            ("ullAvailVirtual", ctypes.c_uint64),
                            ("ullAvailExtendedVirtual", ctypes.c_uint64)]

            stat = _MemoryStatusEx()
            stat.dwLength = ctypes.sizeof(stat)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
                return int(stat.ullTotalPhys // (1024 * 1024))
        except Exception:
            pass
    else:
        try:
            pages = os.sysconf("SC_PHYS_PAGES")
            page_size = os.sysconf("SC_PAGE_SIZE")
            return int(pages * page_size // (1024 * 1024))
        except (ValueError, OSError, AttributeError):
            pass
    return 8192


def _auto_hash_mb() -> int:
    """Engine hash table size: an eighth of system RAM, clamped to
    [256, 1024] MB. A bigger hash means positions searched while pondering
    the opponent's turn are still in memory when it's our move - the reply
    search starts deeper instead of from scratch."""
    return max(256, min(1024, _total_ram_mb() // 8))


class _WinJob:
    """Windows Job Object with KILL_ON_JOB_CLOSE: any engine process assigned
    to it is killed by the OS the moment our process dies - even on a Task
    Manager force-kill or a crash, where Python cleanup never runs. Prevents
    orphaned stockfish.exe processes."""

    def __init__(self):
        import ctypes

        k32 = ctypes.windll.kernel32
        self._k32 = k32
        self.handle = k32.CreateJobObjectW(None, None)
        if not self.handle:
            raise OSError("CreateJobObjectW failed")

        class BASIC(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                        ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", ctypes.c_uint32),
                        ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t),
                        ("ActiveProcessLimit", ctypes.c_uint32),
                        ("Affinity", ctypes.c_size_t),
                        ("PriorityClass", ctypes.c_uint32),
                        ("SchedulingClass", ctypes.c_uint32)]

        class IO(ctypes.Structure):
            _fields_ = [(n, ctypes.c_uint64) for n in
                        ("ReadOperationCount", "WriteOperationCount",
                         "OtherOperationCount", "ReadTransferCount",
                         "WriteTransferCount", "OtherTransferCount")]

        class EXTENDED(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BASIC),
                        ("IoInfo", IO),
                        ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]

        info = EXTENDED()
        info.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
        ok = k32.SetInformationJobObject(
            self.handle, 9,  # JobObjectExtendedLimitInformation
            ctypes.byref(info), ctypes.sizeof(info))
        if not ok:
            raise OSError("SetInformationJobObject failed")

    def assign(self, pid: int) -> bool:
        PROCESS_SET_QUOTA = 0x0100
        PROCESS_TERMINATE = 0x0001
        h = self._k32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE,
                                  False, int(pid))
        if not h:
            return False
        ok = bool(self._k32.AssignProcessToJobObject(self.handle, h))
        self._k32.CloseHandle(h)
        return ok


_JOB = None   # module-level singleton; the handle must live as long as we do


def _job() -> "_WinJob | None":
    global _JOB
    if os.name != "nt":
        return None
    if _JOB is None:
        try:
            _JOB = _WinJob()
        except Exception:
            _JOB = False   # tried and failed - don't retry every launch
    return _JOB or None


def default_threads() -> int:
    """Automatic core count: half the CPU cores (polite to the foreground
    game/browser), at least 1."""
    return max(1, (os.cpu_count() or 2) // 2)


class ChessEngine:
    def __init__(self, path: str, depth: int = 30, threads: int = 0):
        self.path = path
        self.depth = depth
        # threads: 0 = automatic (half cores); otherwise clamp to [1, cpu].
        self.threads = threads
        self._engine: chess.engine.SimpleEngine | None = None
        self.open()

    def open(self):
        """Launch the Stockfish process (hidden). Raises if the path is invalid."""
        self._engine = chess.engine.SimpleEngine.popen_uci(
            self.path, **_POPEN_ARGS)
        # Tie the engine's lifetime to ours at the OS level (Windows), so a
        # crash or force-kill of the app can never leave an orphaned engine.
        try:
            job = _job()
            pid = self._engine.transport.get_pid()
            if job is not None and pid:
                job.assign(pid)
        except Exception:
            pass
        cpu = os.cpu_count() or 2
        n = self.threads if self.threads and self.threads > 0 else default_threads()
        n = max(1, min(int(n), cpu))
        try:
            self._engine.configure({"Threads": n, "Hash": _auto_hash_mb()})
        except Exception:
            pass

    def set_depth(self, depth: int):
        self.depth = depth

    # ------------------------------------------------------------------ #
    def _info_to_result(self, board: chess.Board, info: dict) -> dict:
        """Convert one UCI info line into the app's result dict."""
        white_score = info["score"].white()
        mate = white_score.mate()
        cp = white_score.score(mate_score=100000)

        pv = info.get("pv", [])
        best_move = pv[0] if pv else None
        try:
            best_san = board.san(best_move) if best_move else "-"
        except Exception:
            best_san = "-"

        san_line, tmp = [], board.copy(stack=False)
        for mv in pv[:6]:
            try:
                san_line.append(tmp.san(mv))
                tmp.push(mv)
            except Exception:
                break

        return {
            "best_move": best_move,
            "best_san": best_san,
            "best_uci": best_move.uci() if best_move else "-",
            "eval_cp": cp,
            "mate": mate,
            "pv": san_line,
            "depth": int(info.get("depth", 0)),
        }

    # ------------------------------------------------------------------ #
    def analyse(self, board: chess.Board, depth: int | None = None,
                time_limit: float | None = None) -> dict:
        """Blocking search; stops at depth OR time, whichever comes first."""
        limit = chess.engine.Limit(depth=depth if depth is not None else self.depth,
                                   time=time_limit)
        info = self._engine.analyse(board, limit)
        return self._info_to_result(board, info)

    def analyse_stream(self, board: chess.Board, depth: int | None = None,
                       time_limit: float | None = None,
                       on_update=None, should_abort=None,
                       min_emit_gap: float = 0.15) -> dict | None:
        """Stream the search live.

        * on_update(result_dict) fires on each depth improvement - immediately
          for shallow depths, throttled to `min_emit_gap` afterwards.
        * should_abort() is checked on every engine info line; returning True
          stops the search at once (e.g. the on-screen position changed).

        Returns the best result seen, or None if aborted before any line.
        """
        limit = chess.engine.Limit(depth=depth if depth is not None else self.depth,
                                   time=time_limit)
        best = None
        last_depth = 0
        last_emit = 0.0

        with self._engine.analysis(board, limit) as analysis:
            for info in analysis:
                if should_abort is not None and should_abort():
                    analysis.stop()
                    break
                if "pv" not in info or "score" not in info:
                    continue
                best = self._info_to_result(board, info)
                d = best["depth"]
                now = time.monotonic()
                if (on_update is not None and d > last_depth
                        and (d <= 12 or now - last_emit >= min_emit_gap)):
                    last_depth = d
                    last_emit = now
                    on_update(best)
        return best

    def close(self):
        try:
            if self._engine:
                self._engine.quit()
        except Exception:
            pass
        self._engine = None
