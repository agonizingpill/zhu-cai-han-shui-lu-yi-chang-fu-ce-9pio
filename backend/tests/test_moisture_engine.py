"""判定引擎与发布闸门单元测试（纯标准库，不依赖 Postgres/Redis）。

运行：python -m unittest backend.tests.test_moisture_engine -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from backend.src.services.moisture_engine import judge, evaluate_gate  # noqa: E402

# 与 seed 对齐的 10 个留样：R-01..R-06/R-09/R-10 人工 NORMAL，R-07/R-08 人工 HIGH
RETAINED = {
    'R01': ([13.2, 13.5, 13.0, 13.4, 13.1, 13.3], 'NORMAL'),
    'R02': ([13.6, 13.3, 13.7, 13.5, 13.4, 13.2], 'NORMAL'),
    'R03': ([13.8, 14.0, 13.9, 14.1, 13.7, 14.0], 'NORMAL'),
    'R04': ([14.2, 14.5, 14.1, 14.3, 14.0, 14.4], 'NORMAL'),
    'R05': ([12.4, 12.7, 12.5, 12.6, 12.3, 12.8], 'NORMAL'),
    'R06': ([11.9, 12.1, 12.0, 12.2, 11.8, 12.0], 'NORMAL'),
    'R07': ([17.8, 18.2, 17.5, 18.0, 17.9, 18.3], 'HIGH'),
    'R08': ([16.6, 16.9, 16.4, 16.8, 16.5, 17.0], 'HIGH'),
    'R09': ([13.4, 13.6, 13.5, 13.7, 13.3, 13.8], 'NORMAL'),
    'R10': ([12.9, 13.1, 13.0, 13.2, 12.8, 13.0], 'NORMAL'),
}
V1, V2, V3 = 14.5, 15.5, 17.2
MANUAL = {k: v[1] for k, v in RETAINED.items()}


def verdicts(threshold):
    return {k: judge(f'REF-{threshold}', threshold, readings)['verdict'] for k, (readings, _) in RETAINED.items()}


class EngineTest(unittest.TestCase):
    def test_deterministic_checksum(self):
        a = judge('REF-V1', V1, RETAINED['R04'][0])
        b = judge('REF-V1', V1, RETAINED['R04'][0])
        self.assertEqual(a, b)
        self.assertEqual(len(a['checksum']), 16)

    def test_high_when_max_exceeds_even_if_mean_below(self):
        # 单点越线也判 HIGH：局部受潮不漏
        out = judge('REF-V1', 14.5, [12.0, 12.0, 12.0, 15.0])
        self.assertEqual(out['verdict'], 'HIGH')

    def test_normal_below_threshold(self):
        self.assertEqual(judge('REF-V2', V2, RETAINED['R05'][0])['verdict'], 'NORMAL')


class GateTest(unittest.TestCase):
    def test_v2_passes_gate_fewer_fp_no_new_fn(self):
        m = evaluate_gate(verdicts(V1), verdicts(V2), MANUAL)
        self.assertTrue(m['passed'], m['reason'])
        self.assertLess(m['candidate']['false_positive'], m['baseline']['false_positive'])
        self.assertLessEqual(m['candidate']['false_negative'], m['baseline']['false_negative'])
        # 两版都不漏 R07/R08
        self.assertEqual(m['candidate']['false_negative'], 0)
        self.assertEqual(m['baseline']['false_negative'], 0)

    def test_v3_blocked_by_new_false_negative(self):
        m = evaluate_gate(verdicts(V1), verdicts(V3), MANUAL)
        self.assertFalse(m['passed'])
        self.assertIn('漏报', m['reason'])
        # V3 阈值 17.2：R08 均值约 16.7 被漏判
        self.assertEqual(verdicts(V3)['R08'], 'NORMAL')
        self.assertEqual(MANUAL['R08'], 'HIGH')

    def test_gate_requires_identical_sample_set(self):
        with self.assertRaises(ValueError):
            evaluate_gate({'a': 'NORMAL'}, {'a': 'NORMAL', 'b': 'NORMAL'}, {'a': 'NORMAL'})

    def test_equal_fp_does_not_satisfy_fewer_rule(self):
        # 误报数相同（没有“更少”）即便无漏报也不能过闸门
        base = {'a': 'HIGH', 'b': 'NORMAL'}
        cand = {'a': 'HIGH', 'b': 'NORMAL'}
        manual = {'a': 'NORMAL', 'b': 'NORMAL'}
        m = evaluate_gate(base, cand, manual)
        self.assertFalse(m['passed'])
        self.assertIn('未少于', m['reason'])

    def test_confusion_counts(self):
        base = {'a': 'HIGH', 'b': 'NORMAL', 'c': 'HIGH', 'd': 'NORMAL'}
        cand = {'a': 'NORMAL', 'b': 'NORMAL', 'c': 'HIGH', 'd': 'NORMAL'}
        manual = {'a': 'NORMAL', 'b': 'NORMAL', 'c': 'HIGH', 'd': 'HIGH'}
        m = evaluate_gate(base, cand, manual)
        self.assertEqual(m['baseline']['false_positive'], 1)
        self.assertEqual(m['baseline']['false_negative'], 1)
        # 候选把 a 的误报消掉，但 d 的漏报两边都在 → 无新增漏报且少误报 → 通过
        self.assertEqual(m['candidate']['false_positive'], 0)
        self.assertEqual(m['candidate']['false_negative'], 1)
        self.assertTrue(m['passed'])


if __name__ == '__main__':
    unittest.main()
