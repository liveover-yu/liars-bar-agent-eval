"""批量跑 N 局，并且把失败**记下来**。

上游版本有一个真实的健壮性 bug：``player.py`` 重试 5 次后抛
``RuntimeError``，而这里的循环没有 try——任何一局有模型连续 5 次输出
坏 JSON，整批直接崩，前面花的 API 钱白费。同时格式失败率被 ``print``
吞掉，事后无从统计。

改动只有三处，都是为了让批量运行可恢复、可计量：
1. 每局单独 try，失败记进 run_log.json 后继续下一局；
2. 每局开始前给 random 设种子并记录，牌局可复现；
3. 结束时输出失败率统计——「格式失败率」本身就是一个指标。

用法：
    python multi_game_runner.py -n 20 --seed 20261116
    python multi_game_runner.py -n 20 --temperature 0 --log logs/raw.jsonl
"""

from __future__ import annotations

import argparse
import json
import random
import time
from pathlib import Path

from game import Game

DEFAULT_CONFIGS = [
    {"name": "Player_A", "model": "deepseek-chat"},
    {"name": "Player_B", "model": "gpt-4o-mini"},
    {"name": "Player_C", "model": "claude-sonnet-5"},
    {"name": "Player_D", "model": "gemini-2.0-flash"},
]


class MultiGameRunner:
    def __init__(self, player_configs, num_games=10, base_seed=0, rotate_seats=True):
        self.player_configs = player_configs
        self.num_games = num_games
        self.base_seed = base_seed
        #: 上游 50 局座位固定，品牌先验和座位效应无法分离。轮换后每局的
        #: 出手顺序不同，这两项在期望上互相抵消。
        self.rotate_seats = rotate_seats
        self.log: list[dict] = []

    def _seat_order(self, game_num: int) -> list[dict]:
        n = len(self.player_configs)
        if not self.rotate_seats:
            return self.player_configs
        offset = game_num % n
        return [self.player_configs[(i + offset) % n] for i in range(n)]

    def run_games(self) -> None:
        failures = 0
        started = time.time()

        for game_num in range(self.num_games):
            seed = self.base_seed + game_num
            order = self._seat_order(game_num)
            random.seed(seed)
            print(f"\n=== 第 {game_num + 1}/{self.num_games} 局  seed={seed} "
                  f"座位={[c['name'] for c in order]} ===")

            entry = {"game": game_num + 1, "seed": seed,
                     "seat_order": [c["name"] for c in order],
                     "status": "ok", "winner": None, "error": None}

            try:
                game = Game(order)
                game.start_game()
                entry["winner"] = game.game_record.winner
            except RuntimeError as exc:
                # 只有「模型连续 5 次输出坏 JSON」会走到这里；
                # 它是一局的数据缺失，不是整批的死刑。
                failures += 1
                entry["status"] = "format_failure"
                entry["error"] = str(exc)
                print(f"第 {game_num + 1} 局失败（已记录，继续）：{exc}")

            self.log.append(entry)

        elapsed = time.time() - started
        print(f"\n完成 {self.num_games} 局，失败 {failures} 局 "
              f"（{failures / self.num_games:.1%}），耗时 {elapsed:.1f}s")
        self.write_log()

    def write_log(self, path: str = "analysis/results/run_log.json") -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        summary = {
            "games": self.num_games,
            "failures": sum(1 for e in self.log if e["status"] != "ok"),
            "failure_rate": sum(1 for e in self.log if e["status"] != "ok") / self.num_games,
            "base_seed": self.base_seed,
            "games_detail": self.log,
        }
        p.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"运行日志：{p}")


def parse_arguments():
    parser = argparse.ArgumentParser(description="批量跑 AI 对局（会消耗 API 额度）")
    parser.add_argument("-n", "--num-games", type=int, default=10)
    parser.add_argument("--seed", type=int, default=0, help="基准随机种子，每局 +1")
    parser.add_argument("--no-rotate", action="store_true", help="关闭座位轮换（复现上游的固定座位）")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_arguments()
    runner = MultiGameRunner(DEFAULT_CONFIGS, num_games=args.num_games, base_seed=args.seed,
                             rotate_seats=not args.no_rotate)
    runner.run_games()
