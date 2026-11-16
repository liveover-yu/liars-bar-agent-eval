"""统计工具：只依赖标准库 + numpy。

上游全文没有置信区间，这里补上。50 局样本量不大，二项比例的朴素标准误
在比例接近 0 或 1 时会失效，所以用 Wilson 区间；同时局内决策不独立
（同一局的出牌风格相关），所以再给一个按局重采样的 cluster bootstrap。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
import math
import random

import numpy as np


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """二项比例的 Wilson 置信区间。n=0 时返回 (0, 1)。"""
    if n == 0:
        return 0.0, 1.0
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def cluster_bootstrap(
    clusters: Sequence[Sequence[float]],
    stat: Callable[[np.ndarray], float],
    iterations: int = 2000,
    seed: int = 0,
) -> tuple[float, float, float]:
    """按 cluster（这里指一局游戏）有放回重采样，返回 (点估计, 2.5%, 97.5%)。

    同一局里的决策共享牌局风格、对手和洗牌运气，当成独立样本会低估方差。
    """
    pools = [np.asarray(c, dtype=float) for c in clusters if len(c) > 0]
    point = stat(np.concatenate(pools))
    rng = random.Random(seed)
    draws = np.empty(iterations)
    for i in range(iterations):
        picks = [pools[rng.randrange(len(pools))] for _ in pools]
        draws[i] = stat(np.concatenate(picks))
    lo, hi = np.percentile(draws, [2.5, 97.5])
    return float(point), float(lo), float(hi)


def permutation_test(
    a: Sequence[float],
    b: Sequence[float],
    iterations: int = 20000,
    seed: int = 0,
) -> float:
    """两组均值差的置换检验（双侧），返回 p 值。

    比 t 检验稳：这类 0/1 指标严重偏离正态，置换检验不假设分布。
    """
    xa, xb = np.asarray(a, dtype=float), np.asarray(b, dtype=float)
    if len(xa) == 0 or len(xb) == 0:
        return 1.0
    observed = abs(xa.mean() - xb.mean())
    pooled = np.concatenate([xa, xb])
    rng = np.random.default_rng(seed)
    hits = 0
    for _ in range(iterations):
        rng.shuffle(pooled)
        diff = abs(pooled[: len(xa)].mean() - pooled[len(xa) :].mean())
        hits += diff >= observed
    return hits / iterations


def bootstrap_mean_ci(values: Sequence[float], iterations: int = 5000, seed: int = 0):
    arr = np.asarray(values, dtype=float)
    if len(arr) == 0:
        return 0.0, 0.0, 0.0
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(arr), size=(iterations, len(arr)))
    means = arr[idx].mean(axis=1)
    lo, hi = np.percentile(means, [2.5, 97.5])
    return float(arr.mean()), float(lo), float(hi)


def logistic_irls(X: np.ndarray, y: np.ndarray, steps: int = 50):
    """手写 IRLS 逻辑回归，返回 (系数, 标准误)。

    用来自在控制混淆变量：'枪膛越满质疑越多' 可能只是 '越到残局质疑越多'。
    scipy 没装，不必为一次回归装它。
    """
    X = np.asarray(X, dtype=float)
    y = np.asarray(y, dtype=float)
    beta = np.zeros(X.shape[1])
    for _ in range(steps):
        eta = X @ beta
        mu = 1 / (1 + np.exp(-eta))
        w = np.clip(mu * (1 - mu), 1e-9, None)
        z = eta + (y - mu) / w
        XtW = X.T * w
        beta_new = np.linalg.solve(XtW @ X, XtW @ z)
        if np.max(np.abs(beta_new - beta)) < 1e-10:
            beta = beta_new
            break
        beta = beta_new

    eta = X @ beta
    mu = 1 / (1 + np.exp(-eta))
    w = np.clip(mu * (1 - mu), 1e-9, None)
    cov = np.linalg.inv((X.T * w) @ X)
    se = np.sqrt(np.diag(cov))
    return beta, se
