import random
import json
import re
from collections import Counter
from typing import List, Dict
from llm_client import LLMClient

RULE_BASE_PATH = "prompt/rule_base.txt"
PLAY_CARD_PROMPT_TEMPLATE_PATH = "prompt/play_card_prompt_template.txt"
CHALLENGE_PROMPT_TEMPLATE_PATH = "prompt/challenge_prompt_template.txt"
REFLECT_PROMPT_TEMPLATE_PATH = "prompt/reflect_prompt_template.txt"


class ModelOutputError(ValueError):
    """模型已返回内容，但输出不符合动作契约。"""


def parse_action(content: str, action: str) -> Dict:
    """解析 JSON 动作；不执行任何游戏状态变更。"""
    if not isinstance(content, str):
        raise ModelOutputError("模型内容必须是字符串")
    candidate = content.strip()
    if candidate.startswith("```"):
        match = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", candidate, re.DOTALL | re.IGNORECASE)
        if not match:
            raise ModelOutputError("JSON 代码围栏不完整")
        candidate = match.group(1)
    try:
        result = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise ModelOutputError("JSON 无法解析") from exc
    if not isinstance(result, dict):
        raise ModelOutputError("动作必须是 JSON 对象")
    if action == "play":
        cards = result.get("played_cards")
        if not isinstance(cards, list) or not 1 <= len(cards) <= 3:
            raise ModelOutputError("played_cards 必须是包含 1–3 张牌的列表")
        if not all(isinstance(card, str) for card in cards):
            raise ModelOutputError("每张牌必须是字符串")
        if not all(isinstance(result.get(key), str) for key in ("behavior", "play_reason")):
            raise ModelOutputError("behavior 和 play_reason 必须是字符串")
    elif action == "challenge":
        if type(result.get("was_challenged")) is not bool:
            raise ModelOutputError("was_challenged 必须是布尔值")
        if not isinstance(result.get("challenge_reason"), str):
            raise ModelOutputError("challenge_reason 必须是字符串")
    else:
        raise ValueError(f"未知动作类型: {action}")
    return result


class Player:
    def __init__(self, name: str, model_name: str, llm_client=None):
        """初始化玩家

        Args:
            name: 玩家名称
            model_name: 使用的LLM模型名称
            llm_client: 共用的 LLMClient（批量运行时传入同一个，避免每个玩家
                各建一个连接池；探针里也靠它注入固定 temperature/seed）
        """
        self.name = name
        self.hand = []
        self.alive = True
        self.bullet_position = random.randint(0, 5)
        self.current_bullet_position = 0
        self.opinions = {}

        # LLM相关初始化
        self.llm_client = llm_client or LLMClient()
        self.model_name = model_name

        #: 格式失败统计。上游只 print 掉，事后无从统计；这里留痕，
        #: 「格式失败率」本身是一个要报告的指标，不是噪音。
        self.format_failures = {"choose_cards_to_play": 0, "decide_challenge": 0}

    def _read_file(self, filepath: str) -> str:
        """读取文件内容"""
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                return f.read().strip()
        except Exception as e:
            print(f"读取文件 {filepath} 失败: {str(e)}")
            return ""

    def print_status(self) -> None:
        """打印玩家状态"""
        print(f"{self.name} - 手牌: {', '.join(self.hand)} - "
              f"子弹位置: {self.bullet_position} - 当前弹舱位置: {self.current_bullet_position}")
        
    def init_opinions(self, other_players: List["Player"]) -> None:
        """初始化对其他玩家的看法
        
        Args:
            other_players: 其他玩家列表
        """
        self.opinions = {
            player.name: "还不了解这个玩家"
            for player in other_players
            if player.name != self.name
        }

    def choose_cards_to_play(self,
                        round_base_info: str,
                        round_action_info: str,
                        play_decision_info: str) -> Dict:
        """
        玩家选择出牌
        
        Args:
            round_base_info: 轮次基础信息
            round_action_info: 轮次操作信息
            play_decision_info: 出牌决策信息
            
        Returns:
            tuple: (结果字典, 推理内容)
            - 结果字典包含played_cards, behavior和play_reason
            - 推理内容为LLM的原始推理过程
        """
        # 读取规则和模板
        rules = self._read_file(RULE_BASE_PATH)
        template = self._read_file(PLAY_CARD_PROMPT_TEMPLATE_PATH)
        
        # 准备当前手牌信息
        current_cards = ", ".join(self.hand)
        
        # 填充模板
        prompt = template.format(
            rules=rules,
            self_name=self.name,
            round_base_info=round_base_info,
            round_action_info=round_action_info,
            play_decision_info=play_decision_info,
            current_cards=current_cards
        )
        
        # 网络、鉴权、限流错误由客户端/调用方治理，不归为格式失败。
        for attempt in range(5):
            messages = [{"role": "user", "content": prompt}]
            content, reasoning_content = self.llm_client.chat(messages, model=self.model_name)
            try:
                result = parse_action(content, "play")
                if Counter(result["played_cards"]) - Counter(self.hand):
                    raise ModelOutputError("出牌多重集超出当前手牌")
                remaining = list(self.hand)
                for card in result["played_cards"]:
                    remaining.remove(card)
            except ModelOutputError as exc:
                self.format_failures["choose_cards_to_play"] += 1
                print(f"尝试 {attempt+1} 动作无效: {exc}")
                continue
            # 所有字段及数量验证成功后才提交，失败重试不消耗手牌。
            self.hand[:] = remaining
            return result, reasoning_content
        raise RuntimeError(f"玩家 {self.name} 的choose_cards_to_play方法在多次尝试后失败")

    def decide_challenge(self,
                        round_base_info: str,
                        round_action_info: str,
                        challenge_decision_info: str,
                        challenging_player_performance: str,
                        extra_hint: str) -> bool:
        """
        玩家决定是否对上一位玩家的出牌进行质疑
        
        Args:

            round_base_info: 轮次基础信息
            round_action_info: 轮次操作信息
            challenge_decision_info: 质疑决策信息
            challenging_player_performance: 被质疑玩家的表现描述
            extra_hint: 额外提示信息
            
        Returns:
            tuple: (result, reasoning_content)
            - result: 包含was_challenged和challenge_reason的字典
            - reasoning_content: LLM的原始推理过程
        """
        # 读取规则和模板
        rules = self._read_file(RULE_BASE_PATH)
        template = self._read_file(CHALLENGE_PROMPT_TEMPLATE_PATH)
        self_hand = f"你现在的手牌是: {', '.join(self.hand)}"
        
        # 填充模板
        prompt = template.format(
            rules=rules,
            self_name=self.name,
            round_base_info=round_base_info,
            round_action_info=round_action_info,
            self_hand=self_hand,
            challenge_decision_info=challenge_decision_info,
            challenging_player_performance=challenging_player_performance,
            extra_hint=extra_hint
        )
        
        # 尝试获取有效的JSON响应，最多重试五次
        for attempt in range(5):
            # 每次都发送相同的原始prompt
            messages = [
                {"role": "user", "content": prompt}
            ]
            
            content, reasoning_content = self.llm_client.chat(messages, model=self.model_name)
            try:
                result = parse_action(content, "challenge")
            except ModelOutputError as exc:
                self.format_failures["decide_challenge"] += 1
                print(f"尝试 {attempt+1} 动作无效: {exc}")
                continue
            return result, reasoning_content
        raise RuntimeError(f"玩家 {self.name} 的decide_challenge方法在多次尝试后失败")

    def reflect(self, alive_players: List[str], round_base_info: str, round_action_info: str, round_result: str) -> None:
        """
        玩家在轮次结束后对其他存活玩家进行反思，更新对他们的印象
        
        Args:
            alive_players: 还存活的玩家名称列表
            round_base_info: 轮次基础信息
            round_action_info: 轮次操作信息
            round_result: 轮次结果
        """
        # 读取反思模板
        template = self._read_file(REFLECT_PROMPT_TEMPLATE_PATH)
        
        # 读取规则
        rules = self._read_file(RULE_BASE_PATH)
        
        # 对每个存活的玩家进行反思和印象更新（排除自己）
        for player_name in alive_players:
            # 跳过对自己的反思
            if player_name == self.name:
                continue
            
            # 获取此前对该玩家的印象
            previous_opinion = self.opinions.get(player_name, "还不了解这个玩家")
            
            # 填充模板
            prompt = template.format(
                rules=rules,
                self_name=self.name,
                round_base_info=round_base_info,
                round_action_info=round_action_info,
                round_result=round_result,
                player=player_name,
                previous_opinion=previous_opinion
            )
            
            # 向LLM请求分析
            messages = [
                {"role": "user", "content": prompt}
            ]
            
            try:
                content, _ = self.llm_client.chat(messages, model=self.model_name)
                
                # 更新对该玩家的印象
                self.opinions[player_name] = content.strip()
                print(f"{self.name} 更新了对 {player_name} 的印象")
                
            except Exception as e:
                print(f"反思玩家 {player_name} 时出错: {str(e)}")

    def process_penalty(self) -> bool:
        """处理惩罚"""
        print(f"玩家 {self.name} 执行射击惩罚：")
        self.print_status()
        if self.bullet_position == self.current_bullet_position:
            print(f"{self.name} 中枪死亡！")
            self.alive = False
        else:
            print(f"{self.name} 幸免于难！")
        self.current_bullet_position = (self.current_bullet_position + 1) % 6
        return self.alive
