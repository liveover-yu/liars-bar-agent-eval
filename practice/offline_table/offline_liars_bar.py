"""骗子酒馆 —— 纯 Python 离线牌桌（阶段 0.2 作业，已补完）。

不调用 LLM，键盘输入，四人玩到只剩一人。

规则来源以 liarsbar.net 与 Steam 规则指南为准，和上游 game.py 的
handle_system_challenge 对齐：

1. 有人被处罚 → 开枪 → 无论中弹与否，本小局结束，重新发牌。
2. 有人出完手牌 → 本小局不结束，后面轮到他的回合跳过。
3. 轮到某人出牌时，若其他存活玩家手牌都已打空 → 他的剩余手牌视为
   自动打出，并受到系统质疑（不再询问）。
4. 只剩一个存活玩家 → 游戏结束。
"""

import random


SEATS = 5          # 每人手牌上限
CHAMBERS = 6       # 左轮仓位数


class Player:
    def __init__(self, name, rng):
        self.name = name
        self.hand = []
        self.alive = True
        self.bullet_position = rng.randint(0, CHAMBERS - 1)
        self.current_bullet_position = 0

    def print_status(self):
        print(
            f"{self.name}: 手牌 {self.hand} | 存活 {self.alive} | "
            f"弹舱 {self.current_bullet_position}/{CHAMBERS}"
        )

    def shoot(self):
        """开一枪。返回 'dead' 或 'blank'，转轮前进一格。"""
        hit = self.current_bullet_position == self.bullet_position
        self.current_bullet_position = (self.current_bullet_position + 1) % CHAMBERS
        if hit:
            self.alive = False
            print(f"{self.name} 中枪死亡")
            return "dead"
        print(f"{self.name} 空枪，侥幸存活")
        return "blank"


def create_deck(rng):
    deck = ["Q"] * 6 + ["K"] * 6 + ["A"] * 6 + ["Joker"] * 2
    rng.shuffle(deck)
    return deck


def choose_target_card(rng):
    return rng.choice(["Q", "K", "A"])


def is_valid_play(cards, target_card):
    return all(card == target_card or card == "Joker" for card in cards)


def normalize_card(token):
    token = token.strip()
    if token.lower() == "joker":
        return "Joker"
    return token.upper()


def choose_cards_to_play(player):
    """读键盘，返回这一手打出的牌。校验都在输入层，状态层不管这些。"""
    while True:
        print(f"{player.name} 的手牌：{player.hand}")
        raw = input(f"请输入 {player.name} 要出的牌（空格分隔，1-3 张）：").split()
        cards = [normalize_card(t) for t in raw]

        if not 1 <= len(cards) <= 3:
            print("只能出 1 到 3 张牌")
            continue

        remaining = player.hand.copy()
        for card in cards:
            if card not in remaining:
                print(f"{card} 不在你的手牌里")
                break
            remaining.remove(card)
        else:
            for card in cards:
                player.hand.remove(card)
            return cards


def ask_challenge(player):
    while True:
        answer = input(f"{player.name} 是否质疑？(y/n) ").strip().lower()
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        print("请输入 y 或 n")


def players_with_cards(players):
    return [p for p in players if p.alive and p.hand]


def next_player_with_cards(players, start_idx):
    """从 start_idx 之后找下一个存活且有牌的玩家。找不到返回 None。"""
    for offset in range(1, len(players) + 1):
        candidate = players[(start_idx + offset) % len(players)]
        if candidate.alive and candidate.hand:
            return players.index(candidate)
    return None


def deal_cards(players, rng):
    deck = create_deck(rng)
    for player in players:
        player.hand.clear()
    for _ in range(SEATS):
        for player in players:
            if player.alive:
                player.hand.append(deck.pop())


def start_new_round(players, rng, start_idx=None, first=None):
    """发新牌、选新目标牌，决定这一小局谁先出。

    规则 1 说开枪后本小局结束、重新发牌，但没有规定谁先出；上游的做法是
    上一枪的开枪者先出（活着的话），否则顺延。这里保持一致。
    """
    deal_cards(players, rng)
    target = choose_target_card(rng)
    print(f"\n=== 新一小局，目标牌：{target} ===")

    if first is not None and first.alive and first.hand:
        return players.index(first), target
    start = 0 if start_idx is None else start_idx
    idx = next_player_with_cards(players, start)
    return idx, target


def resolve_challenge(actor, challenger, cards, target):
    """判定质疑，返回需要挨枪的人（没人挨枪时返回 None）。

    challenger 为 None 表示系统自动质疑：质疑失败时无人开枪，直接重新发牌。
    """
    valid = is_valid_play(cards, target)
    print(f"真实牌面：{cards}（目标牌 {target}）")
    if valid:
        if challenger is None:
            print("系统质疑失败，无人开枪")
            return None
        print(f"质疑失败，{challenger.name} 受罚")
        return challenger
    print(f"质疑成功，{actor.name} 受罚")
    return actor


def run_turn(players, actor_idx, target):
    """跑一个回合。返回 (受罚者, 下一局从谁开始, 本小局是否结束)。"""
    actor = players[actor_idx]

    # 规则 3：其他存活玩家都没牌了 → 自动出完，系统质疑
    others = [p for p in players if p is not actor and p.alive]
    if others and all(not p.hand for p in others):
        cards = actor.hand.copy()
        actor.hand.clear()
        print(f"{actor.name} 是最后一个有牌的玩家，剩余手牌 {cards} 视为自动打出，系统质疑")
        return resolve_challenge(actor, None, cards, target), actor_idx, True

    cards = choose_cards_to_play(actor)
    print(f"{actor.name} 打出了 {len(cards)} 张牌")

    next_idx = next_player_with_cards(players, actor_idx)
    if next_idx is None:
        print("没有其他持牌玩家，本小局直接结束")
        return None, actor_idx, True
    challenger = players[next_idx]

    if ask_challenge(challenger):
        return resolve_challenge(actor, challenger, cards, target), actor_idx, True

    print(f"{challenger.name} 没有质疑，轮到 {challenger.name} 出牌")
    return None, next_idx, False


def play_game(player_names, rng):
    players = [Player(name, rng) for name in player_names]
    actor_idx, target = start_new_round(players, rng, first=players[0])

    while True:
        actor_idx, target = (
            (actor_idx, target)
            if players[actor_idx].alive and players[actor_idx].hand
            else start_new_round(players, rng, start_idx=actor_idx)
        )

        print(f"\n--- 轮到 {players[actor_idx].name} 出牌，目标牌 {target} ---")
        for p in players:
            if p.alive:
                p.print_status()

        penalty, next_idx, round_over = run_turn(players, actor_idx, target)

        if penalty is not None:
            penalty.shoot()

        alive = [p for p in players if p.alive]
        if len(alive) == 1:
            print(f"\n{alive[0].name} 是最后的幸存者，获胜！")
            return alive[0].name

        if round_over:
            # 规则 1：开枪后本小局结束，重新发牌，由开枪者先出
            actor_idx, target = start_new_round(players, rng, start_idx=next_idx)
        else:
            actor_idx = next_idx


def main():
    rng = random.Random()
    names = ["P1", "P2", "P3", "P4"]
    print("骗子酒馆（命令行版）")
    print(f"玩家：{', '.join(names)}")
    winner = play_game(names, rng)
    print(f"胜者：{winner}")


if __name__ == "__main__":
    main()
