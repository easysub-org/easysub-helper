# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 hcz1017
"""重采样测试。

两条核心口径（对应 AUDIT-2026-09-22 第 174 行记录的主项目缺陷）：
  ① **块不变性**：整段输入 vs 任意分块输入，输出必须逐样本一致
     —— 主项目旧实现每块独立起 FIR，块边界幅度不连续、44.1k 每块丢 ~2.25 样本；
  ② **频率/幅度保持**：1 kHz 正弦 48k→16k 后仍是 1 kHz、幅度不变；
  ③ **样本数精确**：10 秒 48k→16k 的累计输出与理论值偏差 ≤1（无时长漂移）。
"""

import math
import unittest

import numpy as np

from easysub_helper.audio.resample import (
    IdentityResampler,
    PolyphaseResampler,
    ResampleUnavailable,
    SoxrResampler,
    create_resampler,
)


def sine(rate, freq, seconds, amp=0.5):
    n = int(rate * seconds)
    t = np.arange(n, dtype=np.float64) / float(rate)
    return (amp * np.sin(2.0 * math.pi * freq * t)).astype(np.float32)


def peak_freq(x, rate):
    spec = np.abs(np.fft.rfft(x * np.hanning(x.size)))
    k = int(np.argmax(spec))
    return k * float(rate) / float(x.size)


class PolyphaseTest(unittest.TestCase):
    CASES = ((48000, 16000), (44100, 16000), (32000, 16000), (22050, 16000), (8000, 16000))

    def test_frequency_and_amplitude_preserved(self):
        for in_rate, out_rate in self.CASES:
            res = PolyphaseResampler(in_rate, out_rate)
            x = sine(in_rate, 1000.0, 1.0)
            y = res.process(x)
            core = y[int(0.15 * out_rate):int(0.85 * out_rate)]
            self.assertGreater(core.size, 100, "{}->{} 输出太短".format(in_rate, out_rate))
            self.assertAlmostEqual(peak_freq(core, out_rate), 1000.0, delta=20.0)
            self.assertAlmostEqual(float(np.max(np.abs(core))), 0.5, delta=0.08)

    def test_output_length_is_exact(self):
        for in_rate, out_rate in self.CASES:
            res = PolyphaseResampler(in_rate, out_rate)
            x = sine(in_rate, 1000.0, 2.0)
            y = res.process(x)
            expected = int(round(x.size * out_rate / float(in_rate)))
            self.assertLessEqual(abs(int(y.size) - expected), 1,
                                 "{}->{}: got {} want ~{}".format(in_rate, out_rate, y.size, expected))
            self.assertEqual(y.dtype, np.float32)

    def test_block_invariance(self):
        x = sine(48000, 997.0, 0.5)
        whole = PolyphaseResampler(48000, 16000).process(x)
        for chunk in (160, 960, 4000):
            res = PolyphaseResampler(48000, 16000)
            parts = [res.process(x[i:i + chunk]) for i in range(0, x.size, chunk)]
            joined = np.concatenate(parts)
            self.assertEqual(int(whole.size), int(joined.size),
                             "分块 {} 样本时输出长度不一致".format(chunk))
            np.testing.assert_allclose(whole, joined, atol=1e-5,
                                       err_msg="分块 {} 时块边界不连续".format(chunk))

    def test_no_amplitude_step_at_block_boundary(self):
        """逐帧看包络：块边界处不应出现台阶（旧实现每 60ms 削掉首 1~3 样本）。"""
        rate_in, rate_out, block = 48000, 16000, 960   # 20ms
        res = PolyphaseResampler(rate_in, rate_out)
        x = sine(rate_in, 440.0, 1.0)
        env = []
        for i in range(0, x.size, block):
            out = res.process(x[i:i + block])
            if out.size:
                env.append(float(np.max(np.abs(out))))
        self.assertGreater(len(env), 10)
        env = np.asarray(env)
        self.assertLess(float(env.std() / env.mean()), 0.02, "块间包络抖动过大（块边界不连续）")

    def test_silence_stays_silent(self):
        res = PolyphaseResampler(48000, 16000)
        y = res.process(np.zeros(4800, dtype=np.float32))
        self.assertEqual(float(np.max(np.abs(y))), 0.0)


class StreamStartTest(unittest.TestCase):
    """流开头的行为：负下标被 clip 成 buf[0]（恰是前导零区）。

    独立审查提醒：这个"恰好无差"依赖 buf 的布局——将来若有人改掉"开头即前导零"的前提，
    clip 会把越界读变成重复读且**静默**。这里把当前行为钉住：同一信号"一次性喂"与"分小块喂"
    的输出必须逐样本一致（含开头 23 个过渡样本），布局一旦变化这里先红。
    """

    def _signal(self, n=8000):
        import math
        return [math.sin(2 * math.pi * 220 * i / 48000.0) for i in range(n)]

    def test_stream_start_is_block_invariant(self):
        from easysub_helper.audio.resample import create_resampler
        sig = self._signal()
        whole = create_resampler(48000, 16000).process(np.asarray(sig, dtype=np.float32))

        r = create_resampler(48000, 16000)
        parts = []
        for i in range(0, len(sig), 333):                      # 故意用不整除的块
            parts.append(r.process(np.asarray(sig[i:i + 333], dtype=np.float32)))
        chunked = np.concatenate(parts) if parts else np.zeros(0, dtype=np.float32)

        self.assertEqual(whole.size, chunked.size)
        # 全段一致（含开头的过渡样本），最大误差按 float32 精度容忍
        diff = np.max(np.abs(whole - chunked)) if whole.size else 0.0
        self.assertLess(float(diff), 1e-6, "开头样本不一致：clip 语义或缓冲布局变了")


class SoxrTest(unittest.TestCase):
    def test_if_available(self):
        try:
            res = SoxrResampler(48000, 16000)
        except (ImportError, ResampleUnavailable) as exc:
            self.skipTest("soxr 不可用: {}".format(exc))
        x = sine(48000, 1000.0, 1.0)
        y = res.process(x)
        core = y[int(0.15 * 16000):int(0.85 * 16000)]
        self.assertAlmostEqual(peak_freq(core, 16000), 1000.0, delta=20.0)
        self.assertAlmostEqual(float(np.max(np.abs(core))), 0.5, delta=0.08)


class FactoryTest(unittest.TestCase):
    def test_same_rate_is_identity(self):
        res = create_resampler(16000, 16000)
        self.assertIsInstance(res, IdentityResampler)
        x = sine(16000, 1000.0, 0.1)
        self.assertTrue(np.array_equal(res.process(x), x))

    def test_forced_polyphase(self):
        res = create_resampler(44100, 16000, prefer="polyphase")
        self.assertIsInstance(res, PolyphaseResampler)

    def test_unknown_preference_raises(self):
        self.assertRaises(ResampleUnavailable, create_resampler, 44100, 16000, "nope")

    def test_bad_rate_raises(self):
        self.assertRaises(ValueError, PolyphaseResampler, 0, 16000)


class PolyphaseAgainstReferenceTest(unittest.TestCase):
    """与「上采样 → 低通卷积 → 抽取」朴素基准**逐样本**比对。

    坑（独立审查抓到的 blocker，这个测试就是补它）：以前只断言单音的峰值频率（±20Hz）
    与幅度（±0.08）——**相位错位不改变单音的峰值频率**，所以 44100/22050/11025/8000
    这类 L>1 的非整数比下"相位内抽头顺序颠倒"造成的严重失真（rms 误差 0.04~0.18、
    频谱里冒出 6900Hz 镜像杂散）一直没被发现；48000/32000→16000 因为 L=1 恰好正确，
    所以按 48k 写的用例全绿。这里用**双音**信号直接和朴素基准比：任何相位/对齐错误
    都会立刻放大成明显误差。

    基准刻意不共用实现的任何对齐逻辑：只从相位表还原原型 h（h[j*L+p] = phases[p][j]），
    然后老老实实上采样、卷积、每 M 个取一个；卷积走 FFT 以免长信号拖慢测试。
    """

    CASES = (
        (48000, 16000),   # L=1：两种写法重合（历史用例只覆盖了这类）
        (44100, 16000),   # L=160, M=441 ← 曾经严重失真
        (32000, 16000),
        (22050, 16000),   # L=320
        (11025, 16000),   # L=640
        (8000, 16000),    # L=2
        (16000, 44100),   # 反向（上采样到设备率）
    )

    @staticmethod
    def _reference(x, res):
        L, M, K = res.L, res.M, res.K
        h = np.zeros(K * L, dtype=np.float64)
        for p in range(L):
            h[p::L] = res._phases[p]          # 还原原型：h[j*L + p] = phases[p][j]
        up = np.zeros(x.size * L, dtype=np.float64)
        up[::L] = x.astype(np.float64)
        n = 1
        while n < up.size + h.size - 1:
            n *= 2
        full = np.fft.irfft(np.fft.rfft(up, n) * np.fft.rfft(h, n), n)
        n_out = (x.size * L) // M + 2
        return full[np.arange(n_out, dtype=np.int64) * M]

    def test_matches_naive_reference(self):
        for in_rate, out_rate in self.CASES:
            with self.subTest(rate="{}->{}".format(in_rate, out_rate)):
                n = in_rate // 2                      # 0.5 秒足够看出镜像杂散
                t = np.arange(n, dtype=np.float64) / float(in_rate)
                x = (0.3 * np.sin(2.0 * math.pi * 1000.0 * t)
                     + 0.2 * np.sin(2.0 * math.pi * 3000.0 * t)).astype(np.float32)
                res = PolyphaseResampler(in_rate, out_rate)
                y = res.process(x).astype(np.float64)
                ref = self._reference(x, res)
                k = min(y.size, ref.size)
                self.assertGreater(k, 1000, "输出太短，测不出对齐问题")
                rms = float(np.sqrt(np.mean((y[:k] - ref[:k]) ** 2)))
                self.assertLess(
                    rms, 1e-5,
                    "{}->{} 与朴素基准不符（rms={:.3e}）：多相相位/对齐错了".format(in_rate, out_rate, rms))

    def test_no_mirror_spur_on_dual_tone(self):
        """双音纯净度：正确的多相输出里 1k/3k 之外不应有强杂散（相位错位会造镜像）。"""
        in_rate = 44100
        n = in_rate
        t = np.arange(n, dtype=np.float64) / float(in_rate)
        x = (0.3 * np.sin(2.0 * math.pi * 1000.0 * t)
             + 0.2 * np.sin(2.0 * math.pi * 3000.0 * t)).astype(np.float32)
        y = PolyphaseResampler(in_rate, 16000).process(x)
        core = y[2000:6000]
        spec = np.abs(np.fft.rfft(core * np.hanning(core.size)))
        freqs = np.fft.rfftfreq(core.size, d=1.0 / 16000.0)
        # 1k/3k 附近各取一个带宽，其它位置的最大值必须低 30dB 以上
        keep = np.zeros_like(spec, dtype=bool)
        for f0 in (1000.0, 3000.0):
            keep |= np.abs(freqs - f0) < 120.0
        peak = float(spec[keep].max())
        spur = float(np.delete(spec, np.where(keep)[0]).max())
        self.assertGreater(peak / max(spur, 1e-12), 31.6,   # 30 dB
                           "通带外出现镜像杂散（相位错位的典型症状）")


if __name__ == "__main__":
    unittest.main()
