"""Pure-logic tests: plain-English move descriptions, game-phase heuristic,
and the config schema / one-time migration. No engine, no GUI, no display."""
import json
import os
import tempfile

import chess

from caissa.board_state import describe_move, game_phase
from caissa.config import Config, CONFIG_VERSION


# --- describe_move ---------------------------------------------------------
def test_describe_simple_move():
    b = chess.Board()
    assert describe_move(b, chess.Move.from_uci("e2e4")) == "Pawn to e4"


def test_describe_capture():
    b = chess.Board("rnbqkbnr/ppp2ppp/8/3pp3/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq - 0 3")
    assert "takes Pawn on d5" in describe_move(b, chess.Move.from_uci("e4d5"))


def test_describe_check_and_mate():
    b = chess.Board("rnbqkbnr/pppp1ppp/8/4p3/6P1/5P2/PPPPP2P/RNBQKBNR b KQkq - 0 2")
    assert "CHECKMATE" in describe_move(b, chess.Move.from_uci("d8h4"))


def test_describe_castling():
    b = chess.Board("r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1")
    assert describe_move(b, chess.Move.from_uci("e1g1")) == "Castle king-side"


# --- game phase ------------------------------------------------------------
def test_phase_opening_and_endgame():
    assert game_phase(chess.Board()) == "Opening"
    assert game_phase(chess.Board("8/5k2/8/8/8/3K4/4P3/8 w - - 0 1")) == "Endgame"


# --- config migration ------------------------------------------------------
def _load(data):
    p = os.path.join(tempfile.gettempdir(), "caissa_test_cfg.json")
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    try:
        return Config.load(p)
    finally:
        os.remove(p)


def test_config_migrates_old_versions_to_max_defaults():
    cfg = _load({"depth": 16, "change_threshold": 0.12})   # pre-v3
    assert cfg.depth == 30
    assert abs(cfg.think_time - 2.5) < 1e-9
    assert cfg.stability_frames == 1
    assert cfg.version == CONFIG_VERSION


def test_config_respects_current_version():
    cfg = _load({"depth": 22, "think_time": 1.8, "version": CONFIG_VERSION})
    assert cfg.depth == 22                      # not clobbered
    assert abs(cfg.think_time - 1.8) < 1e-9


def test_config_ignores_unknown_keys():
    cfg = _load({"depth": 22, "version": CONFIG_VERSION, "bogus_key": 999})
    assert cfg.depth == 22
    assert not hasattr(cfg, "bogus_key")
