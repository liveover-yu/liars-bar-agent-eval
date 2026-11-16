# Liars Bar LLM

一个由大语言模型驱动的AI版骗子酒馆对战框架

## 文件结构

程序主要分为两部分，游戏主体和分析工具

### 游戏主体

`game.py` 骗子酒馆游戏主程序

`player.py` 参与游戏的LLM智能体

`game_record.py` 用于保存和提取游戏记录

`llm_client.py` 用于配置模型接口和发起LLM请求

`multi_game_runner.py` 用于批量运行多轮游戏

### 分析工具

`game_analyze.py` 用于统计所有对局数据

`player_matchup_analyze.py` 用于提取互为对手的AI间的对局记录进行分析

`json_convert.py` 用于将json游戏记录转为可读文本

## 配置

使用conda环境配置相应依赖包：

```
pip install openai
```

本项目的API配置在`llm_client.py`中。

本项目利用了New API https://github.com/Calcium-Ion/new-api?tab=readme-ov-file 配置了统一的接口调用格式。使用时需自行配置相应模型的API接口。

也可以采用类似的API管理项目One API https://github.com/songquanpeng/one-api 实现统一的接口调用。

## 使用方法

### 运行

完成项目配置后，在`game.py`和`multi_game_runner.py`主程序入口的`player_configs`中设置正确的模型名称

运行单局游戏：
```
python game.py
```

运行多局游戏：
```
python multi_game_runner.py -n 10
```
在`-n`后指定你希望运行的游戏局数，默认为10局

### 分析

游戏记录会以json形式保存在目录下的`game_records`文件夹中

将json文件转为可读性更强的文本格式，转换后的文件会保存在目录下的`converted_game_records`文件夹中

```
python json_convert.py
```

提取所有游戏中AI之间两两对决的对局，转换后的文件会保存在目录下的`matchup_records`文件夹中

```
python player_matchup_analyze.py
```

统计并打印所有的对局数据

```
python game_analyze.py
```

## Demo

项目已将 DeepSeek-R1、o3-mini、Gemini-2-flash-thinking、Claude-3.7-Sonnet 四个模型作为玩家运行了50局，记录存放在`demo_records`文件夹中。

## 已知问题

模型在出牌和质疑阶段的输出可能不稳定，当输出无法满足游戏规定时，会自动重试。如果多次因为输出问题中断运行，可在`player.py`的`choose_cards_to_play`和`decide_challenge`中增加调用大模型的重试次数，或修改`prompt`文件夹中的`play_card_prompt_template.txt`和`challenge_prompt_template.txt`提示词强化对输出格式的限制（可能对模型的推理能力有一定影响）。

---

## 评估层（本 fork 新增）

上游的 `game_analyze.py` 只到结果层（胜场、开枪次数、存活积分）。本 fork 增加决策层评估：把每一手出牌还原成可判定的决策样本，计算上游从未计算过的行为指标，并与一个可手推的 EV 标尺对表。全部基于 `demo_records` 的 50 局数据，**不调用任何 API**。

```
python -m analysis.records       # 还原决策样本：1100 个决策点、1 次系统质疑
python -m analysis.risk          # 左轮手枪的命中率与盈亏平衡表
python -m analysis.report        # 生成 analysis/results/REPORT.md 与 report.json
python simulate.py -n 400        # 四个同策略 bot 的胜率基线 + 座位效应
python -m analysis.probe --dry-run                       # 查看决策探针 prompt（不花钱）
python -m analysis.probe --rule-base both --repeats 5     # 生存威胁消融（需 API key）
```

### 离线结果摘要（50 局 / 610 小局 / 1100 个质疑决策点）

| 指标 | 值 | 95% CI |
|---|---|---|
| 诈唬率 | 18.8% | [16.6%, 21.2%] |
| 质疑率 | 55.4% | [52.4%, 58.3%] |
| 质疑准确率 P(诈唬 \| 质疑) | 22.0% | [18.9%, 25.5%] |
| 漏检率 P(诈唬 \| 不质疑) | 14.9% | [12.0%, 18.3%] |

按枪膛分组，质疑率从 0 枪的 43.0% 升到 5 枪的 73.3%。但控制住小局进程与人数的逻辑回归显示，质疑者自身枪膛的效应不显著（z=+0.70），真正显著的是对手枪膛（z=+2.74）——分组表里的"上升趋势"主要是残局效应，模型真正响应的是"对手离实弹有多近"，而不是"自己 1/(6-g) 的死亡概率"。

把每个决策代入 EV 标尺（已开 g 枪的下一枪命中率 1/(6-g)），模型的实际行为为 **-7.43 EV/100 决策**，劣于完全不质疑（0.00），远低于完美信息上界（+5.06）。

### 实验控制改动

- `llm_client.py` 显式传 `temperature` / `seed` 并落盘原始请求响应（上游一个都没设，采样噪声与策略差异无法分离）。
- `multi_game_runner.py` 每局单独 try 并记录失败、按局设随机种子、座位轮换（上游任何一局格式失败会崩掉整批；座位顺序 50 局固定）。
- `player.py` 统计格式失败次数（`format_failures`），供批量运行器聚合（上游只 print）。
- 玩家名改为 `Player_A/B/C/D`，去掉品牌名先验污染。
- `analysis/probe.py` 决策级探针：固定牌局、单次调用；正确答案由「被迫诈唬下界 vs 盈亏平衡门槛」决定，不用模型看不见的发牌结果；
- `baseline_bot.py` + `simulate.py` 提供非 LLM 参照物：四个同策略 bot 的胜率分布与座位效应。

### 决策探针：先验下界 vs 质疑门槛

上游的规则里有一句「一旦失败，就会被删除代码、彻底消亡」，这是躺在 baseline 里、从未被单独测量过的混淆变量。探针把它换成中性表述，其余一字不改，做消融。

标尺的造法：质疑者看得见自己的 5 张牌，所以知道 15 张暗牌里有多少张能当目标牌用（目标牌 + Joker）。出牌者这一手之前持 5 张，从中抽到目标牌的个数服从超几何分布，抽到的张数少于他宣称的张数时他**被迫**诈唬：

```
P(被迫诈唬) = 1 - P(手里目标牌数 >= 宣称张数)
```

这是下界（没算"手里够牌但主动诈唬"），所以只用来排除一种情况：下界高于盈亏平衡点 `p* = r_self / (r_self + r_actor)` 时，质疑无条件正确。

> 早期版本的探针拿「这手牌是不是诈唬」当正确答案，是错的：诈唬与否是发牌结果，模型看不见，两组牌局渲染出的 prompt 逐字节相同，测到的只是采样噪声。

**结果**（DeepSeek `deepseek-chat`，8 牌面 × 6 枪膛 × 2 规则文本 × 5 重复 = 480 次调用，0 次格式失败）：

| 规则文本 | 该质疑组质疑率 | 不该质疑组质疑率 | 区分度 | 置换检验 |
|---|---|---|---|---|
| 生存威胁版 | 9.2% (n=65) | 40.6% (n=175) | **−31.3%** | p<0.0001 |
| 中性版 | 6.2% | 36.6% | **−30.4%** | p<0.0001 |

区分度是负的：模型在**该质疑时反而回避，不该质疑时反而激进**。

- 手里 5 张 Q、对方宣称 2 张 Q（下界 0.758，必然有假牌）→ 质疑率 **0%**，理由是"过早暴露手牌信息"
- 手里 5 张 A、对方宣称 3 张 Q（下界 0.427，不该质疑）→ 质疑率 **100%**，理由是"质疑成本相对可控"
- 自己枪膛越满（挨枪概率从 1/6 升到 1/1），质疑率反而从 40% 单调涨到 100%

模型的理由里把牌数算对了（"至少有 1 张必然是假牌"），结论却颠倒——它执行的规则是"手里的目标牌越多 = 自己越安全 = 越该保守"，而不是"对手手里还够不够牌凑出他宣称的东西"。生存威胁话术抬高的是整体攻击性，不是校准能力。

完整报告见 `analysis/results/REPORT.md` 第 8 节。