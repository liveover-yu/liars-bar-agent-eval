"""把 demo_records/game_records/*.json 还原成决策级样本。

关键点：一次 PlayAction 就是一次质疑决策的机会——出牌者打了几张、下家要不要质疑。
真实牌面 played_cards 在 json 里有，所以 bluff 是可直接计算的，不是估计值。

不调 API，纯读文件，跑 50 局成本为 0。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json

DECK_SIZE = 20
LEGAL_IN_DECK = 8  # 6 张目标牌 + 2 张 Joker，任意时刻牌堆里的合法牌总数

REPO_ROOT = Path(__file__).resolve().parents[1]
RECORDS_DIR = REPO_ROOT / "demo_records" / "game_records"

SYSTEM = "无"  # 系统自动质疑在 next_player 字段里的占位符


@dataclass(frozen=True)
class Decision:
    """一次「下家是否质疑」的决策，附带你当时能看到的全部信息。"""

    game_id: str
    round_id: int
    target: str
    actor: str
    challenger: str
    played: tuple[str, ...]
    bluff: bool
    challenged: bool
    challenge_ok: bool | None
    actor_gun: int
    challenger_gun: int
    challenger_hand: tuple[str, ...]
    actor_hand_size: int
    round_players: int
    play_index: int

    @property
    def k(self) -> int:
        return len(self.played)

    @property
    def legal_in_challenger_hand(self) -> int:
        return sum(1 for c in self.challenger_hand if c == self.target or c == "Joker")


@dataclass(frozen=True)
class SystemEvent:
    """「只剩一人有牌 → 系统质疑」的边界情况。决策层统计里要单独拿出来。"""

    game_id: str
    round_id: int
    actor: str
    played: tuple[str, ...]
    bluff: bool
    ok: bool


def load_game(path: Path) -> tuple[list[Decision], list[SystemEvent]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    decisions: list[Decision] = []
    systems: list[SystemEvent] = []

    for rnd in data["rounds"]:
        init = {s["player_name"]: s for s in rnd["player_initial_states"]}
        hands = {name: list(s["initial_hand"]) for name, s in init.items()}
        target = rnd["target_card"]

        for idx, play in enumerate(rnd["play_history"]):
            actor = play["player_name"]
            played = tuple(play["played_cards"])
            bluff = any(c != target and c != "Joker" for c in played)
            challenger = play["next_player"]

            if challenger == SYSTEM:
                systems.append(SystemEvent(
                    game_id=data["game_id"], round_id=rnd["round_id"], actor=actor,
                    played=played, bluff=bluff, ok=play["challenge_result"] is True,
                ))
            else:
                decisions.append(Decision(
                    game_id=data["game_id"],
                    round_id=rnd["round_id"],
                    target=target,
                    actor=actor,
                    challenger=challenger,
                    played=played,
                    bluff=bluff,
                    challenged=play["was_challenged"],
                    challenge_ok=play["challenge_result"],
                    actor_gun=init[actor]["current_gun_position"],
                    challenger_gun=init[challenger]["current_gun_position"],
                    challenger_hand=tuple(hands[challenger]),
                    actor_hand_size=len(hands[actor]),
                    round_players=len(rnd["round_players"]),
                    play_index=idx,
                ))

            hands[actor] = list(play["remaining_cards"])

    return decisions, systems


def load_all(records_dir: Path = RECORDS_DIR) -> tuple[list[Decision], list[SystemEvent], dict]:
    paths = sorted(records_dir.glob("*.json"))
    decisions: list[Decision] = []
    systems: list[SystemEvent] = []
    meta = {"games": [], "winners": [], "rounds": 0}

    for path in paths:
        data = json.loads(path.read_text(encoding="utf-8"))
        d, s = load_game(path)
        decisions.extend(d)
        systems.extend(s)
        meta["games"].append(data["game_id"])
        meta["winners"].append(data["winner"])
        meta["rounds"] += len(data["rounds"])

    return decisions, systems, meta


if __name__ == "__main__":
    d, s, m = load_all()
    print(f"games={len(m['games'])} rounds={m['rounds']} decisions={len(d)} system={len(s)}")
    print(f"bluff={sum(x.bluff for x in d)} challenged={sum(x.challenged for x in d)}")
