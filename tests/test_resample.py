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


if __name__ == "__main__":
    unittest.main()
