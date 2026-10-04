# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 hcz1017
"""带状态的重采样（多相 FIR 或 soxr）——**别再用主项目那个自研 resample**。

为什么必须带状态：主项目 `src/audio-processor.ts` 的 resample 每块独立起 FIR（i=0 处索引为负被
丢弃）且 `floor(len/ratio)` 丢块尾余数——AUDIT-2026-09-22 第 174 行记录了它的两个后果：
块边界幅度不连续 + 约 0.1% 时长漂移。桌面助手的采集率往往不是 16k（WASAPI 共享模式通常是
48k），整机音频又是长会话，这个缺陷会被放大，所以这里自己实现一份**跨块保留状态**的版本。

优先级：soxr（若装了，质量与速度最好）→ 内置多相 FIR（numpy，无额外依赖）→ 同率恒等。
两条实测口径（见 tests/test_resample.py）：
  ① 整段 vs 分块结果**逐样本一致**（保证无块边界不连续）；
  ② 正弦频率与幅度保持。
"""


import math

import numpy as np

from ..i18n import t


class ResampleUnavailable(RuntimeError):
    """既没有 soxr 也没有 numpy 时无法重采样（明确报错，绝不静默降级）。"""


class Resampler(object):
    in_rate = 0
    out_rate = 0
    name = "base"

    def process(self, x):
        raise NotImplementedError

    def flush(self):
        return np.zeros(0, dtype=np.float32)


class IdentityResampler(Resampler):
    name = "identity"

    def __init__(self, rate):
        self.in_rate = int(rate)
        self.out_rate = int(rate)

    def process(self, x):
        return np.asarray(x, dtype=np.float32).reshape(-1)


class SoxrResampler(Resampler):
    """python-soxr 的流式重采样（内部保留滤波器状态）。"""

    name = "soxr"

    def __init__(self, in_rate, out_rate, quality="HQ"):
        import soxr  # 局部 import：缺依赖时才失败，且能被 create_resampler 捕获

        self.in_rate = int(in_rate)
        self.out_rate = int(out_rate)
        self._soxr = soxr
        stream_cls = getattr(soxr, "ResampleStream", None)
        if stream_cls is None:
            raise ResampleUnavailable(t("resample.err.noStream"))
        self._stream = stream_cls(in_rate, out_rate, 1, dtype="float32", quality=quality)

    def process(self, x):
        x = np.asarray(x, dtype=np.float32).reshape(-1)
        if x.size == 0:
            return x
        # 坑：不同 soxr 版本的参数名不完全一致（chunk/last vs x/last），这里两种都试一次。
        try:
            y = self._stream.resample_chunk(x, last=False)
        except TypeError:
            y = self._stream.resample_chunk(x)
        return np.asarray(y, dtype=np.float32).reshape(-1)


class PolyphaseResampler(Resampler):
    """有理数比 L/M 的多相 FIR，跨块保留输入尾部与输出序号（无块边界不连续）。

    数学：上采样 L 倍后低通（截止 = 0.5/max(L,M)，归一化到上采样率的奈奎斯特），再抽取 M 倍。
    对输出序号 m：p = (m*M) % L，q = (m*M) // L，y[m] = Σ_j phases[p][j] * x[q-j]，
    phases[p][j] = h[j*L + p]（h 为对称原型滤波器，故 j 的排列方向不影响结果）。
    """

    name = "polyphase"

    def __init__(self, in_rate, out_rate, taps_per_phase=24):
        if in_rate <= 0 or out_rate <= 0:
            raise ValueError(t("resample.err.badRate"))
        g = math.gcd(int(in_rate), int(out_rate))
        self.L = int(out_rate) // g
        self.M = int(in_rate) // g
        self.K = int(taps_per_phase)
        self.in_rate = int(in_rate)
        self.out_rate = int(out_rate)

        n = self.K * self.L
        fc = 0.5 / max(self.L, self.M)
        idx = np.arange(n, dtype=np.float64) - (n - 1) / 2.0
        h = 2.0 * fc * np.sinc(2.0 * fc * idx) * np.hanning(n)
        total = h.sum()
        if total <= 0:
            raise ResampleUnavailable(t("resample.err.designFailed", from_rate=in_rate, to_rate=out_rate))
        # 归一化：每个相位的直流增益 ≈ 1（不然会出现各相位增益不一致 → 周期性的音量抖动）
        h *= self.L / total
        # phases[p][j] = h[j*L + p]
        self._phases = np.ascontiguousarray(h.reshape(self.K, self.L).T.astype(np.float32))
        self._tail = np.zeros(max(self.K - 1, 0), dtype=np.float32)
        self._n_in = 0   # 已消费的输入样本总数
        self._m = 0      # 下一个待产出的输出序号

    def process(self, x):
        x = np.asarray(x, dtype=np.float32).reshape(-1)
        if x.size == 0:
            return x
        k1 = self.K - 1
        # buf 布局：[K-1 个前导零, 前一块尾部(K-1), 本块]；buf[i] 对应全局下标 base+i
        buf = np.concatenate((np.zeros(k1, dtype=np.float32), self._tail, x))
        # buf[0] 对应全局下标 base：buf = [zeros(k1), tail(k1), x] → x[0] 在 buf[2*k1]
        base = self._n_in - 2 * k1
        last_global = self._n_in + x.size - 1
        # 只产出「所需输入样本已到齐」的输出：需 q <= last_global，即 m*M//L <= last_global
        m_max = -(-((last_global + 1) * self.L) // self.M) - 1
        if m_max >= self._m:
            m = np.arange(self._m, m_max + 1, dtype=np.int64)
            t = m * self.M
            q = t // self.L
            p = (t % self.L).astype(np.int64)
            idx = q[:, None] - np.arange(k1, -1, -1, dtype=np.int64)[None, :]
            bi = idx - base
            # 防御：流开头（全局下标为负）读前导零
            np.clip(bi, 0, buf.size - 1, out=bi)
            sel = self._phases[p]
            y = np.einsum("ij,ij->i", sel, buf[bi]).astype(np.float32)
            self._m = int(m_max) + 1
        else:
            y = np.zeros(0, dtype=np.float32)
        if k1:
            self._tail = np.concatenate((self._tail, x))[-k1:]
        self._n_in += int(x.size)
        return y


def create_resampler(in_rate, out_rate, prefer=None):
    """按优先级创建重采样器。prefer 可为 'soxr' / 'polyphase' / 'identity'（测试用）。"""
    in_rate = int(in_rate)
    out_rate = int(out_rate)
    if in_rate == out_rate:
        return IdentityResampler(in_rate)
    order = [prefer] if prefer else ["soxr", "polyphase"]
    last_err = None
    for kind in order:
        try:
            if kind == "soxr":
                return SoxrResampler(in_rate, out_rate)
            if kind == "polyphase":
                return PolyphaseResampler(in_rate, out_rate)
            if kind == "identity":
                return IdentityResampler(in_rate)
            raise ValueError("未知的重采样实现: {!r}".format(kind))
        except ImportError as exc:      # 没装 soxr
            last_err = exc
        except ResampleUnavailable as exc:
            last_err = exc
        except Exception as exc:        # noqa: BLE001 - soxr 版本 API 差异也要能回落
            last_err = exc
    raise ResampleUnavailable(t(
        "resample.err.unavailable", from_rate=in_rate, to_rate=out_rate, detail=last_err
    ))
