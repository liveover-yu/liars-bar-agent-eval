"""批量跑非 LLM bot 对局，产出上游缺失的两样东西：

1. **胜率基线**：四个完全相同策略的 bot 互相打，胜率应当接近 25%。
   偏离多少，就是纯运气的分布宽度——拿它去比对模型的胜率才有意义。

2. **座位效应**：四个 bot 策略完全一样，唯一差别是座位顺序。如果
   胜率仍显著偏离 25%，那上游 50 局里「DeepSeek 赢 22 局」这类结论
   就可能被座位顺序解释掉一部分（上游座位固定为
   DeepSeek→ChatGPT→Claude→Gemini，从没 counterbalance 过）。

两个实验都不调 API，跑 1000 局只要几秒。
"""

from __future__ import annotations

import argparse
import json
import random
import tempfile
from collections import Counter
from pathlib import Path

import game_record
from baseline_bot import EVBotPlayer
from game import Game


class BotGame(Game):
    """把上游 Game 里的 LLM 玩家换成 EV bot，其余规则一行不改。"""

    def __init__(self, player_configs, seed: int):
        self.rng_seed = seed
        self.seat_names = [c["name"] for c in player_configs]
        self.players = [EVBotPlayer(c["name"]) for c in player_configs]

        for player in self.players:
            player.init_opinions(self.players)

        self.deck = []
        self.target_card = None
        self.current_player_idx = 0
        self.last_shooter_name = None
        self.game_over = False
        self.game_record = game_record.GameRecord()
        self.game_record.save_directory = str(_TMPDIR)
        self.game_record.start_game([p.name for p in self.players])
        self.round_count = 0


_TMPDIR = Path(tempfile.gettempdir()) / "liars_bar_sim_records"


def run_one(seat_order: list[str], seed: int) -> str:
    """跑一局，返回赢家座位名。每局独立设置随机种子。"""
    random.seed(seed)
    configs = [{"name": name, "model": "ev_bot"} for name in seat_order]
    game = BotGame(configs, seed=seed)

    # 与上游 play 一致：随机一个先手
    game.current_player_idx = random.randint(0, len(game.players) - 1)
    # 子弹位置与洗牌都来自同一个种子后的 random 状态
    for player in game.players:
        player.bullet_position = random.randint(0, 5)

    game.deal_cards()
    game.choose_target_card()
    game.start_round_record()
    while not game.game_over:
        game.play_round()
    return game.game_record.winner


def simulate(games: int, seats: list[str], base_seed: int = 0) -> dict:
    wins = Counter()
    seat_stats: dict[str, dict] = {name: {"games": 0, "wins": 0} for name in seats}

    for i in range(games):
        seed = base_seed + i
        winner = run_one(seats, seed)
        wins[winner] += 1

    for name in seats:
        seat_stats[name]["games"] = games
        seat_stats[name]["wins"] = wins[name]

    return {
        "games": games,
        "seats": seats,
        "wins": dict(wins),
        "win_rates": {name: wins[name] / games for name in seats},
        "expected_rate": 1 / len(seats),
        "seat_stats": seat_stats,
    }


def rotate_seats(games: int, names: list[str], base_seed: int = 0) -> dict:
    """座位轮换：每局把座位顺时针转一格，消掉固定座位的系统性偏差。"""
    n = len(names)
    rotated_wins = Counter()
    for i in range(games):
        order = [names[(j + i) % n] for j in range(n)]
        random.seed(base_seed + i)
        rotated_wins[run_one(order, base_seed + i)] += 1
    return {"games": games, "wins": dict(rotated_wins),
            "win_rates": {name: rotated_wins[name] / games for name in names}}


def main() -> None:
    parser = argparse.ArgumentParser(description="bot 基线模拟")
    parser.add_argument("-n", "--games", type=int, default=200)
    parser.add_argument("--out", default="analysis/results/simulation.json")
    args = parser.parse_args()

    _TMPDIR.mkdir(parents=True, exist_ok=True)
    seats = ["Seat_1", "Seat_2", "Seat_3", "Seat_4"]

    fixed = simulate(args.games, seats)
    rotated = rotate_seats(args.games, seats)

    print(f"每席 {args.games} 局")
    print("\n固定座位顺序：")
    for name in seats:
        print(f"  {name}: {fixed['win_rates'][name]:.1%}")
    print(f"\n座位轮换后（同一个 bot 策略，只换位置）：")
    for name in seats:
        print(f"  {name}: {rotated['win_rates'][name]:.1%}")
    print(f"\n理论上限/下限的参照：{1/len(seats):.1%}")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"fixed": fixed, "rotated": rotated},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结果写入 {out}")


if __name__ == "__main__":
    main()
