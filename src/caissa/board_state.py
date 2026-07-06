"""Chess game-state helpers: game-phase classification and move detection.

Move detection is the heart of the app. Rather than recognising pieces, we
take the *set of squares that changed on screen* and ask python-chess: "which
of your legal moves would have changed exactly these squares?" This rules-
constrained matching is what lets the overlay follow a game on any platform.
"""
from __future__ import annotations

import chess

# Material values used only for the game-phase heuristic.
PIECE_VALUES = {
    chess.PAWN: 1, chess.KNIGHT: 3, chess.BISHOP: 3,
    chess.ROOK: 5, chess.QUEEN: 9, chess.KING: 0,
}

PIECE_NAMES = {
    chess.PAWN: "Pawn", chess.KNIGHT: "Knight", chess.BISHOP: "Bishop",
    chess.ROOK: "Rook", chess.QUEEN: "Queen", chess.KING: "King",
}


def describe_move(board: chess.Board, move: chess.Move) -> str:
    """Plain-English description of a move, e.g. 'Knight takes Pawn on e5 -
    check!'. Much clearer for non-notation readers than SAN."""
    try:
        if board.is_castling(move):
            kingside = chess.square_file(move.to_square) > chess.square_file(move.from_square)
            text = "Castle king-side" if kingside else "Castle queen-side"
        else:
            piece = board.piece_at(move.from_square)
            name = PIECE_NAMES.get(piece.piece_type, "Piece") if piece else "Piece"
            to_name = chess.square_name(move.to_square)
            if board.is_en_passant(move):
                text = f"{name} takes pawn en passant on {to_name}"
            else:
                target = board.piece_at(move.to_square)
                if target:
                    text = f"{name} takes {PIECE_NAMES.get(target.piece_type, 'piece')} on {to_name}"
                else:
                    text = f"{name} to {to_name}"
            if move.promotion:
                text += f", promote to {PIECE_NAMES.get(move.promotion, 'Queen')}"

        after = board.copy(stack=False)
        after.push(move)
        if after.is_checkmate():
            text += " - CHECKMATE!"
        elif after.is_check():
            text += " - check!"
        return text
    except Exception:
        return ""


def non_pawn_material(board: chess.Board) -> int:
    """Total non-pawn, non-king material on the board (both sides).
    Starting value is 62 (2 x (N+N+B+B+R+R+Q) = 2 x 31)."""
    total = 0
    for pt in (chess.KNIGHT, chess.BISHOP, chess.ROOK, chess.QUEEN):
        count = len(board.pieces(pt, chess.WHITE)) + len(board.pieces(pt, chess.BLACK))
        total += count * PIECE_VALUES[pt]
    return total


def game_phase(board: chess.Board) -> str:
    """Classify the game phase from material and move number.

    Heuristic (documented and easy to tweak):
      * Endgame     - little heavy material left, or queens traded and modest
                      material remaining.
      * Opening     - early moves with almost all material still present.
      * Middlegame  - everything in between.
    """
    npm = non_pawn_material(board)
    queens = (len(board.pieces(chess.QUEEN, chess.WHITE)) +
              len(board.pieces(chess.QUEEN, chess.BLACK)))

    if npm <= 24 or (queens == 0 and npm <= 32):
        return "Endgame"
    if board.fullmove_number <= 10 and npm >= 52:
        return "Opening"
    return "Middlegame"


def squares_involved(board: chess.Board, move: chess.Move) -> set:
    """Every square whose appearance should change when `move` is played.

    Covers the from/to squares plus the special cases of castling (the rook
    also moves) and en passant (the captured pawn is on a different square)."""
    squares = {move.from_square, move.to_square}

    if board.is_castling(move):
        rank = chess.square_rank(move.from_square)
        if chess.square_file(move.to_square) > chess.square_file(move.from_square):
            # King-side: rook h -> f
            squares.update({chess.square(7, rank), chess.square(5, rank)})
        else:
            # Queen-side: rook a -> d
            squares.update({chess.square(0, rank), chess.square(3, rank)})

    if board.is_en_passant(move):
        # Captured pawn sits on the to-file, from-rank.
        squares.add(chess.square(chess.square_file(move.to_square),
                                 chess.square_rank(move.from_square)))
    return squares


def match_move(board: chess.Board, changed: dict):
    """Find the legal move that best explains the set of changed squares.

    `changed` maps square -> change score. Returns (move, score) or (None, ...).

    A candidate is only considered if its from- and to-squares both changed.
    Among candidates we prefer the move whose involved squares changed most
    strongly while leaving the fewest unexplained ("extra") changed squares —
    those typically come from move-highlight rings toggling on/off.
    """
    changed_set = set(changed.keys())
    best, best_score = None, float("-inf")

    for move in board.legal_moves:
        mandatory = {move.from_square, move.to_square}
        if not mandatory.issubset(changed_set):
            continue

        involved = squares_involved(board, move)
        explained = involved & changed_set
        strength = sum(changed.get(sq, 0.0) for sq in explained) / max(len(involved), 1)
        extra = len(changed_set - involved)
        score = strength - 0.04 * extra

        # Default to queen promotion when several promo moves match the same
        # squares (under-promotion can't be told apart by square diffing).
        if move.promotion and move.promotion != chess.QUEEN:
            score -= 0.02

        if score > best_score:
            best, best_score = move, score

    return best, best_score
