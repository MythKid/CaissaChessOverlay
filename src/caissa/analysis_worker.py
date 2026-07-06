"""Background worker: fully automatic pipeline.

States:
  SEARCHING - no board locked. Scans the whole (virtual) screen every couple
              of seconds, asks board_finder for candidate regions, and
              validates each with the board reader:
                * a fresh starting position  -> learn pieces + colour, lock;
                * templates already learned and the crop reads as a legal
                  position with good confidence -> lock (mid-game join).
  TRACKING  - a board is locked. Reads the full position every poll, analyses
              with the engine, and surfaces the best move on the user's turn.
              If the board stops being recognisable for a while (window
              closed / moved), it unlocks and returns to SEARCHING.

The finder only proposes; the reader validates. A wrong candidate simply
fails validation and is skipped, which is what makes full-auto safe.
"""
from __future__ import annotations

import os
import time
from datetime import datetime

import cv2
import numpy as np
import chess
from PyQt6.QtCore import QThread, pyqtSignal

from .capture import ScreenCapturer
from .board_reader import (BoardReader, detect_orientation, resolve_board,
                           looks_like_fresh_game)
from .board_finder import find_candidates, overlap_frac
from .board_state import game_phase, describe_move
from .engine import ChessEngine, is_cpu_unsupported
from .engine_locator import resolve_engine
from .config import TEMPLATES_PATH, DEBUG_DIR

SEARCH_INTERVAL = 0.8      # seconds between full-screen scans while searching
LOSS_SECONDS = 6.0         # unrecognisable for this long -> unlock and rescan
MIN_JOIN_CONFIDENCE = 0.12 # template-read confidence needed to lock mid-game

# Cheap frame-diff gate (mean |diff| on a 96x96 grayscale of the region,
# 0..255): below STATIC the frame is "unchanged" and the expensive 64-square
# read is skipped; above CHANGED a running engine search is aborted because
# the position on screen is moving.
FRAME_STATIC_EPS = 0.6
FRAME_CHANGED_EPS = 0.9
FORCE_READ_SECS = 1.0      # do a full read at least this often (loss detection)
ABORT_CHECK_SECS = 0.20    # how often the abort callback peeks at the screen
FAST_CONFIRM_SECS = 0.03   # re-read interval while a move is settling: once a
                           # change is seen, confirm it at this fast rate so the
                           # engine starts almost immediately (steady-state CPU
                           # cost is unchanged - the normal poll_interval still
                           # governs the static screen)


class AnalysisWorker(QThread):
    analysis_ready = pyqtSignal(dict)
    status = pyqtSignal(str)
    error = pyqtSignal(str)

    def __init__(self, config):
        super().__init__()
        self.config = config
        self._running = False
        self._calibrate_requested = False
        self._switch_turn_requested = False
        self._switch_colors_requested = False
        self._refresh_requested = False
        self._debug_shot_requested = False
        self._reinit_engine_requested = False
        self._engine: ChessEngine | None = None
        self._reader = BoardReader()

        # The overlay's OWN window rectangle in global physical pixels.
        # The app displays a chessboard itself (the mini-board), and it is
        # always-on-top, so it appears in every screenshot - without this
        # exclusion the scanner can find and lock onto its own reflection.
        self.exclude_rect = None   # [x, y, w, h] | None (set by the UI)

        # Tracking state.
        self._analyzed = None      # placement we last analysed
        self._prev_turn = None     # side to move we last reported
        self._forced_turn = None   # internal turn override
        self._unseen_hinted = False  # told the user about an unlearned board

    # ------------------------- control API (UI thread) ------------------ #
    def request_calibrate(self):
        self._calibrate_requested = True

    def request_switch_colors(self):
        """User says the board orientation was guessed wrong ('I'm the other
        guy') - flip which side we treat as the user's."""
        self._switch_colors_requested = True

    def request_refresh(self):
        """Forget the current lock and re-find/re-read the board from scratch
        (recovers from any missed move or stale state)."""
        self._refresh_requested = True

    def request_switch_turn(self):
        self._switch_turn_requested = True

    def request_debug_shot(self):
        self._debug_shot_requested = True

    def request_engine_reinit(self):
        self._reinit_engine_requested = True

    def set_exclude_rect(self, rect):
        """Update the overlay's own on-screen rectangle (physical pixels)."""
        self.exclude_rect = rect

    def stop(self):
        self._running = False

    def _is_self(self, region) -> bool:
        """True if `region` (global physical px) overlaps our own window."""
        return (self.exclude_rect is not None
                and overlap_frac(region, self.exclude_rect) > 0.10)

    # ------------------------------ helpers ----------------------------- #
    def _user_color(self):
        return chess.WHITE if self.config.orientation == "white" else chess.BLACK

    @staticmethod
    def _small_gray(img):
        """Tiny grayscale fingerprint of a frame for cheap change detection."""
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        return cv2.resize(gray, (96, 96)).astype(np.float32)

    @staticmethod
    def _frame_diff(a, b) -> float:
        return float(np.mean(np.abs(a - b)))

    def _control_flagged(self) -> bool:
        """Any pending user action that should interrupt a running search."""
        return (not self._running or self._refresh_requested
                or self._switch_colors_requested or self._calibrate_requested
                or self._reinit_engine_requested or self._switch_turn_requested
                or self._debug_shot_requested)

    def _make_abort(self, cap, region, ref_small):
        """Abort callback for a streaming search: stop the engine the moment
        the on-screen position changes or the user presses a button."""
        state = {"t": 0.0}

        def should_abort() -> bool:
            if self._control_flagged():
                return True
            now = time.monotonic()
            if now - state["t"] < ABORT_CHECK_SECS:
                return False
            state["t"] = now
            try:
                img = cap.grab(region)
            except Exception:
                return True
            return self._frame_diff(self._small_gray(img), ref_small) > FRAME_CHANGED_EPS

        return should_abort

    # ------------------------------ main loop --------------------------- #
    def run(self):
        self._running = True
        try:
            cap = ScreenCapturer()
        except Exception as e:
            self.error.emit(f"Screen capture init failed: {e}")
            return

        self._reader.load(TEMPLATES_PATH)
        self._init_engine()

        last_raw = None
        stable = 0
        lost_since = None
        searching_announced = False
        last_small = None          # tiny fingerprint of the previous frame
        last_full_read = 0.0       # monotonic time of the last 64-square read

        while self._running:
            if self._reinit_engine_requested:
                self._reinit_engine_requested = False
                self._init_engine()

            # Refresh: drop the lock and re-find the board from scratch.
            if self._refresh_requested:
                self._refresh_requested = False
                self.config.region = None
                self._analyzed = None
                self._prev_turn = None
                self._forced_turn = None
                last_raw, stable, lost_since = None, 0, None
                searching_announced = False
                self.status.emit("Refreshing - re-finding the board...")

            # Switch colours: the user says they're the other side.
            if self._switch_colors_requested:
                self._switch_colors_requested = False
                self.config.orientation = (
                    "black" if self.config.orientation == "white" else "white")
                self.config.save()
                self._analyzed = None
                self._prev_turn = None
                self._forced_turn = None
                last_raw, stable = None, 0
                side = "White" if self.config.orientation == "white" else "Black"
                self.status.emit(f"Got it - you're {side}. Re-reading the board.")

            # =========================== SEARCHING ========================= #
            if not self.config.region:
                if not searching_announced:
                    searching_announced = True
                    self.status.emit("Looking for a board...")
                try:
                    full, (offx, offy) = cap.grab_full()
                except Exception as e:
                    self.error.emit(f"Screen capture failed: {e}")
                    time.sleep(1.0)
                    continue

                if self._debug_shot_requested:
                    self._debug_shot_requested = False
                    self._save_debug_full(full)

                if self._try_lock(full, offx, offy):
                    searching_announced = False
                    last_raw, stable, lost_since = None, 0, None
                    continue
                time.sleep(SEARCH_INTERVAL)
                continue

            # =========================== TRACKING ========================== #
            searching_announced = False

            # A previously saved region may point at our own window (older
            # versions could lock onto the app's own mini-board) - unlock it.
            if self._is_self(self.config.region):
                self.config.region = None
                self.config.save()
                self.status.emit("Ignoring my own window - searching for the "
                                 "real board...")
                continue

            try:
                img = cap.grab(self.config.region)
            except Exception as e:
                self.error.emit(f"Couldn't read the board area: {e}")
                time.sleep(0.5)
                continue

            if self._debug_shot_requested:
                self._debug_shot_requested = False
                self._save_debug_shot(img)

            if self._calibrate_requested:
                self._calibrate_requested = False
                self._do_calibrate(img)
                last_raw, stable = None, 0
                continue

            # Cheap gate: if the frame is pixel-identical to the last one and
            # we're in a settled, already-analysed state, skip the expensive
            # 64-square read. A full read is still forced every FORCE_READ_SECS
            # so a covered/closed board is detected even on a static screen.
            small = self._small_gray(img)
            now_m = time.monotonic()
            frame_static = (last_small is not None and
                            self._frame_diff(small, last_small) < FRAME_STATIC_EPS)
            last_small = small
            if (frame_static and last_raw is not None
                    and last_raw == self._analyzed
                    and now_m - last_full_read < FORCE_READ_SECS):
                time.sleep(self.config.poll_interval)
                continue
            last_full_read = now_m

            # Auto-setup whenever a fresh game appears (new game / rematch).
            is_start, orient = looks_like_fresh_game(img)
            if is_start and self._auto_setup(img, orient):
                last_raw, stable = None, 0

            if not self._reader.ready:
                # Locked manually on an unseen board mid-game: wait for a start.
                self.status.emit("Watching the board - I'll learn its pieces "
                                 "automatically when a new game starts.")
                time.sleep(0.4)
                continue

            placement, confidence = self._reader.read(img, self.config.orientation)

            # Board unrecognisable (window moved/closed/covered)?
            if confidence < 0.06 and not is_start:
                if lost_since is None:
                    lost_since = time.time()
                elif time.time() - lost_since > LOSS_SECONDS:
                    lost_since = None
                    self.config.region = None      # unlock -> back to searching
                    self.status.emit("Board lost - searching the screen again...")
                    continue
                time.sleep(self.config.poll_interval)
                continue
            lost_since = None

            # Require the read to be identical twice (piece finished moving),
            # confirming at the fast rate so the engine starts sooner.
            if placement != last_raw:
                last_raw, stable = placement, 0
                time.sleep(FAST_CONFIRM_SECS)
                continue
            stable += 1
            if stable < self.config.stability_frames:
                time.sleep(FAST_CONFIRM_SECS)
                continue

            if self._switch_turn_requested:
                self._switch_turn_requested = False
                if self._prev_turn is not None:
                    self._forced_turn = not self._prev_turn
                self._analyzed = None

            if placement == self._analyzed:
                time.sleep(self.config.poll_interval)
                continue

            board, turn = resolve_board(placement, self._analyzed, self._prev_turn,
                                        self._user_color(), self._forced_turn)
            self._forced_turn = None
            if board is None:
                time.sleep(self.config.poll_interval)
                continue

            prev_placement = self._analyzed
            self._analyzed = placement
            self._prev_turn = turn
            # Stream the search; abort instantly if the on-screen position
            # changes (opponent replies / user moves) or a button is pressed.
            abort = self._make_abort(cap, self.config.region, small)
            self._emit_analysis(board, turn, prev_placement, should_abort=abort)
            time.sleep(self.config.poll_interval)

        if self._engine:
            self._engine.close()
        cap.close()

    # --------------------------- locking logic --------------------------- #
    def _try_lock(self, full, offx, offy) -> bool:
        """Validate finder candidates; lock onto the first that passes.

        Real game boards are BIG - small example/diagram boards on article
        pages are ignored via a minimum-size gate, and when several boards
        validate, the largest one wins.
        """
        try:
            candidates = find_candidates(full)
        except Exception:
            return False

        # Minimum size: the board must span a decent fraction of the screen.
        min_side = min(full.shape[0], full.shape[1]) * getattr(
            self.config, "min_board_frac", 0.30)
        candidates = [c for c in candidates if c[0][2] >= min_side]
        # Prefer the biggest board (the actual game) over smaller ones.
        candidates.sort(key=lambda c: -c[0][2])

        for (x, y, w, h), _score in candidates:
            # Never lock onto the overlay's own mini-board.
            if self._is_self([offx + x, offy + y, w, h]):
                continue
            crop = full[y:y + h, x:x + w]

            # Path 1: a fresh game - learn pieces + our colour, lock.
            is_start, orient = looks_like_fresh_game(crop)
            if is_start and self._reader.learn(crop, orient):
                try:
                    self._reader.save(TEMPLATES_PATH)
                except Exception:
                    pass
                if orient != self.config.orientation:
                    self.config.orientation = orient
                self.config.region = [offx + x, offy + y, w, h]
                self.config.save()
                self._analyzed = None
                self._prev_turn = None
                self._unseen_hinted = False
                side = "White" if orient == "white" else "Black"
                self.status.emit(f"Found your board - you're {side}. Game on!")
                self._announce_current(crop)
                return True

            # Path 2: known pieces - lock mid-game if it reads as a legal
            # position with decent confidence (try both orientations).
            if self._reader.ready:
                for orient2 in (self.config.orientation,
                                "black" if self.config.orientation == "white" else "white"):
                    pl, conf = self._reader.read(crop, orient2)
                    if conf < MIN_JOIN_CONFIDENCE:
                        continue
                    user = chess.WHITE if orient2 == "white" else chess.BLACK
                    board, turn = resolve_board(pl, None, None, user, None)
                    if board is None:
                        continue
                    if orient2 != self.config.orientation:
                        self.config.orientation = orient2
                    self.config.region = [offx + x, offy + y, w, h]
                    self.config.save()
                    self._analyzed = pl
                    self._prev_turn = turn
                    self.status.emit("Found your game in progress - reading it.")
                    self._emit_analysis(board, turn, None)
                    return True

        # A big board is visible but none validated: if we've never learned
        # this site's pieces, tell the user what will happen instead of
        # silently searching forever.
        if candidates and not self._reader.ready and not self._unseen_hinted:
            self._unseen_hinted = True
            self.status.emit("I can see a board, but I haven't learned this "
                             "site's pieces yet - I'll set up automatically "
                             "the moment a new game starts.")
        return False

    def _announce_current(self, crop):
        placement, _ = self._reader.read(crop, self.config.orientation)
        board, turn = resolve_board(placement, None, None, self._user_color(), None)
        if board is not None:
            self._analyzed = placement
            self._prev_turn = turn
            self._emit_analysis(board, turn, None)

    # ------------------------------ actions ----------------------------- #
    def _init_engine(self) -> bool:
        path = resolve_engine(self.config.stockfish_path)
        if not path:
            self.error.emit("Chess engine not found. Reinstall the app.")
            return False
        try:
            if self._engine:
                self._engine.close()
            self._engine = ChessEngine(
                path, self.config.depth,
                threads=getattr(self.config, "engine_threads", 0))
            return True
        except Exception as e:
            self._engine = None
            if is_cpu_unsupported(e):
                self.error.emit("This CPU can't run the built-in engine "
                                "(needs AVX2). Pick a non-AVX2 Stockfish "
                                "under Settings → Custom engine.")
            else:
                self.error.emit(f"Couldn't start the chess engine: {e}")
            return False

    def _auto_setup(self, img, orientation) -> bool:
        """Auto-learn from a detected fresh game (new game/rematch while
        locked). Only relearns when needed."""
        orientation_changed = orientation != self.config.orientation
        if orientation_changed:
            self.config.orientation = orientation
            self.config.save()
        if self._reader.ready and not orientation_changed:
            return False
        if not self._reader.learn(img, orientation):
            return False
        try:
            self._reader.save(TEMPLATES_PATH)
        except Exception:
            pass
        self._analyzed = None
        self._prev_turn = None
        self._forced_turn = None
        side = "White" if orientation == "white" else "Black"
        self.status.emit(f"New game detected - you're {side}. Reading the board.")
        return True

    def _do_calibrate(self, img):
        orientation = detect_orientation(img)
        ok, reason = self._reader.validate_start(img, orientation)
        if not ok:
            self.error.emit(reason)
            return
        if orientation != self.config.orientation:
            self.config.orientation = orientation
            self.config.save()
        if not self._reader.learn(img, orientation):
            self.error.emit("That didn't look like a starting position. Set the "
                            "board to a fresh game, then press Calibrate.")
            return
        try:
            self._reader.save(TEMPLATES_PATH)
        except Exception:
            pass
        self._analyzed = None
        self._prev_turn = None
        self._forced_turn = None
        side = "White" if orientation == "white" else "Black"
        self.status.emit(f"Learned this board - you're playing {side}.")
        self._announce_current(img)

    # ------------------------------ debug -------------------------------- #
    def _save_debug_full(self, full):
        try:
            os.makedirs(DEBUG_DIR, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            path = os.path.join(DEBUG_DIR, f"fullscreen_{stamp}.png")
            cv2.imwrite(path, full)
            self.status.emit(f"Saved a full-screen debug image to {path}")
        except Exception as e:
            self.error.emit(f"Couldn't save debug image: {e}")

    def _save_debug_shot(self, img):
        """Save the raw capture plus a copy with the 8x8 grid drawn on top."""
        try:
            os.makedirs(DEBUG_DIR, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            cv2.imwrite(os.path.join(DEBUG_DIR, f"capture_{stamp}.png"), img)

            overlay = img.copy()
            h, w = overlay.shape[:2]
            for i in range(9):
                x, y = round(i * w / 8), round(i * h / 8)
                thick = 2 if i in (0, 8) else 1
                cv2.line(overlay, (x, 0), (x, h), (0, 0, 255), thick)
                cv2.line(overlay, (0, y), (w, y), (0, 0, 255), thick)
            cv2.imwrite(os.path.join(DEBUG_DIR, f"capture_{stamp}_grid.png"), overlay)

            extra = ""
            if self._reader.ready:
                placement, conf = self._reader.read(img, self.config.orientation)
                board, _ = resolve_board(placement, None, None,
                                         self._user_color(), None)
                fen = board.fen() if board else "(not a legal position)"
                extra = f" | read: {fen} | confidence {conf:.2f}"
            self.status.emit(f"Saved debug images to {DEBUG_DIR}{extra}")
        except Exception as e:
            self.error.emit(f"Couldn't save debug image: {e}")

    # ------------------------------ analysis ------------------------------ #
    @staticmethod
    def _guess_last_move(prev_placement, placement):
        """Best-effort from/to for highlighting the move that just happened."""
        if not prev_placement:
            return None
        vacated = [sq for sq in chess.SQUARES
                   if prev_placement.get(sq) and not placement.get(sq)]
        filled = [sq for sq in chess.SQUARES
                  if placement.get(sq) and placement.get(sq) != prev_placement.get(sq)]
        if len(vacated) >= 1 and len(filled) >= 1:
            return chess.square_name(vacated[0]) + chess.square_name(filled[0])
        return None

    def _emit_analysis(self, board, turn, prev_placement, should_abort=None):
        """Streamed, latency-bounded, cancellable analysis.

        The engine's search is streamed live: on the USER'S turn every depth
        improvement is pushed to the UI (first move within ~0.1s, refining
        continuously up to the think-time cap). On the opponent's turn the
        same search runs as a *ponder* - it warms the engine's hash so our
        reply comes back deeper/faster - but only the final eval is shown.
        `should_abort` stops the search the instant the board changes, so
        thinking never blocks move detection.
        """
        user_turn = (turn == self._user_color())
        placement = {sq: (board.piece_at(sq).symbol() if board.piece_at(sq) else None)
                     for sq in chess.SQUARES}
        payload = {
            "fen": board.fen(),
            "phase": game_phase(board),
            "turn": "White" if turn == chess.WHITE else "Black",
            "orientation": self.config.orientation,
            "is_user_turn": user_turn,
            "last_move": self._guess_last_move(prev_placement, placement),
            "best_san": "-", "best_uci": "-", "best_desc": "", "best_piece": None,
            "eval_cp": None, "mate": None, "pv": [], "depth": 0,
            "thinking": False,
            "game_over": board.is_game_over(),
            "result": board.result() if board.is_game_over() else None,
        }

        if board.is_game_over():
            self.analysis_ready.emit(payload)
            return
        if self._engine is None and not self._init_engine():
            self.analysis_ready.emit(payload)
            return

        def apply(res):
            desc = ""
            piece_symbol = None
            if res["best_move"] is not None:
                desc = describe_move(board, res["best_move"])
                piece = board.piece_at(res["best_move"].from_square)
                piece_symbol = piece.symbol() if piece else None
            payload.update({
                "best_san": res["best_san"], "best_uci": res["best_uci"],
                "best_desc": desc, "best_piece": piece_symbol,
                "eval_cp": res["eval_cp"], "mate": res["mate"], "pv": res["pv"],
                "depth": res.get("depth", 0),
            })

        def push_live(res):
            apply(res)
            payload["thinking"] = True
            self.analysis_ready.emit(dict(payload))

        if should_abort is None:
            should_abort = self._control_flagged   # at least honour buttons

        try:
            res = self._engine.analyse_stream(
                board,
                depth=self.config.depth,
                time_limit=getattr(self.config, "think_time", 2.5),
                on_update=push_live if user_turn else None,
                should_abort=should_abort)
            if res is not None:
                apply(res)
            payload["thinking"] = False
            self.analysis_ready.emit(dict(payload))
        except Exception as e:
            self.error.emit(f"Engine hiccup - restarting it: {e}")
            self.analysis_ready.emit(dict(payload))
            # The engine process may have died; drop it so the next analysis
            # relaunches a fresh one instead of failing forever.
            try:
                self._engine.close()
            except Exception:
                pass
            self._engine = None
            # Re-analyse this same position on the next pass.
            self._analyzed = None
