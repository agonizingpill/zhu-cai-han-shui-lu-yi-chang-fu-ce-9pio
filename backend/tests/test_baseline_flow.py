"""参考版本切换业务流的内存集成测试。

不依赖 Postgres/Redis：用最小内存假库实现 baseline 服务真正用到的游标能力，
端到端验证“开跑快照 → 中断续跑 → 闸门 → 发布/拦截 → 任务版本归属 → 失败回滚”。
"""
import os
import sys
import types
import unittest
from unittest.mock import patch

# backend/ 作为包根：baseline 内部使用相对导入（..utils.db）
BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
WORKSPACE_ROOT = os.path.abspath(os.path.join(BACKEND_ROOT, '..'))
sys.path.insert(0, WORKSPACE_ROOT)

# 本地无 psycopg2 时注入桩模块，仅满足导入；被测逻辑不依赖真实驱动
try:
    import psycopg2  # noqa: F401
except ImportError:
    pg = types.ModuleType('psycopg2')
    extras = types.ModuleType('psycopg2.extras')
    class RealDictCursor: ...
    class Json:
        def __init__(self, obj): self.obj = obj
    extras.RealDictCursor = RealDictCursor
    extras.Json = Json
    pg.extras = extras
    pg.connect = lambda url=None: None  # setUp 中替换为返回 FakeConn 的工厂
    sys.modules['psycopg2'] = pg
    sys.modules['psycopg2.extras'] = extras

try:
    import redis  # noqa: F401
except ImportError:
    redis_stub = types.ModuleType('redis')
    class _Redis:
        @classmethod
        def from_url(cls, *a, **k): return cls()
    redis_stub.Redis = _Redis
    sys.modules['redis'] = redis_stub

from backend.src.services import baseline  # noqa: E402


class FakeCursor:
    def __init__(self, db):
        self.db = db
        self._rows = None
        self._idx = 0
        self.rowcount = 0

    # --- 仅实现被测代码用到的 SQL 语义，按表分派 ---
    @staticmethod
    def _unwrap(x):
        return x.obj if isinstance(x, Json) else x

    @classmethod
    def _unwrap_item(cls, i):
        d = dict(i)
        d['readings_snapshot'] = cls._unwrap(d['readings_snapshot'])
        return d

    def execute(self, sql, params=()):
        s = ' '.join(sql.split())
        p = params
        if s.startswith('SELECT id FROM comparison_runs WHERE run_code'):
            self._rows = [r for r in self.db['runs'] if r['run_code'] == p[0]]
        elif s.startswith('SELECT id, status FROM comparison_runs WHERE id'):
            self._rows = [{'id': r['id'], 'status': r['status']} for r in self.db['runs'] if r['id'] == p[0]]
        elif s.startswith('SELECT status FROM comparison_runs WHERE id'):
            self._rows = [{'status': r['status']} for r in self.db['runs'] if r['id'] == p[0]]
        elif s.startswith('SELECT v.id, v.version_code, v.high_moisture_threshold FROM reference_versions v JOIN comparison_runs'):
            # process_comparison：按对照跑关联出基准/候选两个版本
            run = next(r for r in self.db['runs'] if r['id'] == p[0])
            ids = {run['baseline_version_id'], run['candidate_version_id']}
            self._rows = [dict(v) for v in self.db['versions'] if v['id'] in ids]
        elif s.startswith('SELECT id, version_code, high_moisture_threshold FROM reference_versions WHERE id IN'):
            ids = set(p)
            self._rows = [dict(v) for v in self.db['versions'] if v['id'] in ids]
        elif s.startswith('SELECT r.id, r.lot_id, r.manual_verdict, l.lot_code FROM retained_samples'):
            self._rows = [dict(r) for r in self.db['retained']]
        elif s.startswith('SELECT point_code, moisture_pct, instrument_id, sampled_at FROM moisture_samples'):
            self._rows = [dict(x) for x in self.db['readings'].get(p[0], [])]
        elif s.startswith('INSERT INTO comparison_runs'):
            rid = max([r['id'] for r in self.db['runs']], default=0) + 1
            self.db['runs'].append({'id': rid, 'run_code': p[0], 'baseline_version_id': p[1],
                                    'candidate_version_id': p[2], 'status': 'PENDING',
                                    'sample_snapshot': self._unwrap(p[3]), 'gate_passed': None, 'gate_reason': None, 'metrics': None})
            self._rows = [{'id': rid}]
        elif s.startswith('INSERT INTO comparison_items'):
            iid = max([i['id'] for i in self.db['items']], default=0) + 1
            self.db['items'].append({'id': iid, 'run_id': p[0], 'retained_id': p[1], 'lot_code': p[2],
                                     'readings_snapshot': self._unwrap(p[3]), 'manual_verdict': p[4],
                                     'baseline_verdict': None, 'candidate_verdict': None, 'status': 'PENDING', 'error': None})
            self._rows = []
        elif s.startswith("UPDATE comparison_runs SET status='RUNNING'"):
            for r in self.db['runs']:
                if r['id'] == p[0] and r['status'] != 'COMPLETED':
                    r['status'] = 'RUNNING'
            self._rows = []
        elif s.startswith('SELECT status FROM comparison_runs WHERE id'):
            self._rows = [{'status': r['status']} for r in self.db['runs'] if r['id'] == p[0]]
        elif s.startswith('SELECT cr.baseline_version_id, cr.candidate_version_id FROM comparison_runs'):
            run = next(r for r in self.db['runs'] if r['id'] == p[0])
            self._rows = [{'baseline_version_id': run['baseline_version_id'], 'candidate_version_id': run['candidate_version_id']}]
        elif s.startswith("SELECT * FROM comparison_items WHERE run_id") and 'status IN' in s:
            run_id = p[0]
            self._rows = [self._unwrap_item(i) for i in self.db['items'] if i['run_id'] == run_id and i['status'] in ('PENDING', 'RUNNING', 'FAILED')]
        elif s.startswith("UPDATE comparison_items SET status='RUNNING'"):
            iid = p[0]
            it = next(i for i in self.db['items'] if i['id'] == iid)
            it['status'] = 'RUNNING'
            self._rows = []
        elif s.startswith("UPDATE comparison_items SET status='SUCCEEDED'"):
            # 参数顺序：verdict, verdict, detail, detail, id
            item = next(i for i in self.db['items'] if i['id'] == p[4])
            item.update({'status': 'SUCCEEDED', 'baseline_verdict': p[0], 'candidate_verdict': p[1],
                         'baseline_detail': p[2], 'candidate_detail': p[3]})
            self._rows = []
        elif s.startswith("SELECT count(*) n FROM comparison_items WHERE run_id") and "status<>'SUCCEEDED'" in s:
            run_id = p[0]
            self._rows = [{'n': sum(1 for i in self.db['items'] if i['run_id'] == run_id and i['status'] != 'SUCCEEDED')}]
        elif s.startswith('SELECT * FROM comparison_items WHERE run_id'):
            self._rows = [self._unwrap_item(i) for i in self.db['items'] if i['run_id'] == p[0]]
        elif s.startswith("UPDATE comparison_runs SET status='COMPLETED'"):
            run = next(r for r in self.db['runs'] if r['id'] == p[3])
            run.update({'status': 'COMPLETED', 'gate_passed': p[0], 'gate_reason': p[1], 'metrics': self._unwrap(p[2])})
            self._rows = []
        elif s.startswith("UPDATE comparison_runs SET status='FAILED'"):
            next(r for r in self.db['runs'] if r['id'] == p[0])['status'] = 'FAILED'
            self._rows = []
        elif s.startswith("SELECT count(*) n FROM comparison_items WHERE run_id"):
            run_id = p[0]
            self._rows = [{'n': sum(1 for i in self.db['items'] if i['run_id'] == run_id and i['status'] in ('PENDING', 'RUNNING', 'FAILED'))}]
        elif s.startswith("UPDATE comparison_runs SET status='RUNNING', finished_at=NULL"):
            next(r for r in self.db['runs'] if r['id'] == p[0] and r['status'] != 'COMPLETED')['status'] = 'RUNNING'
            self._rows = []
        elif s.startswith('SELECT cr.*, b.version_code baseline_code, c.version_code candidate_code'):
            run = next(r for r in self.db['runs'] if r['id'] == p[0])
            vmap = {v['id']: v for v in self.db['versions']}
            self._rows = [dict(run, baseline_code=vmap[run['baseline_version_id']]['version_code'],
                               candidate_code=vmap[run['candidate_version_id']]['version_code'])]
        elif s.startswith('SELECT * FROM comparison_runs WHERE id'):
            self._rows = [dict(r) for r in self.db['runs'] if r['id'] == p[0]]
        elif s.startswith("SELECT id FROM reference_versions WHERE status='ACTIVE'"):
            # release_candidate 用普通游标 active_row[0]
            self._rows = [(v['id'],) for v in self.db['versions'] if v['status'] == 'ACTIVE']
        elif s.startswith("UPDATE analysis_jobs SET reference_version_id"):
            # 只改仍在排队且未锚定的任务：在途任务保持旧版
            for j in self.db['jobs']:
                if j['reference_version_id'] is None and j['status'] == 'QUEUED':
                    j['reference_version_id'] = p[0]
            self._rows = []
        elif s.startswith("UPDATE reference_versions SET status='ARCHIVED'"):
            next(v for v in self.db['versions'] if v['id'] == p[0])['status'] = 'ARCHIVED'
            self._rows = []
        elif s.startswith("UPDATE reference_versions SET status='ACTIVE'"):
            next(v for v in self.db['versions'] if v['id'] == p[0])['status'] = 'ACTIVE'
            self._rows = []
        elif s.startswith('INSERT INTO baseline_releases'):
            rid = max([r['id'] for r in self.db['releases']], default=0) + 1
            if "'ACTIVATED'" in s or "'REJECTED'" in s or "'ROLLED_BACK'" in s:
                # decision 为 SQL 字面量：参数 (run_id, from_id, to_id, reason, gate_passed, decided_by)
                decision = next(d for d in ('ACTIVATED', 'REJECTED', 'ROLLED_BACK') if "'" + d + "'" in s)
                reason, gate = p[3], p[4]
            else:
                # decision 参数化（helper 独立事务）：(run_id, from_id, to_id, decision, reason, gate_passed, decided_by)
                decision, reason, gate = p[3], p[4], p[5]
            self.db['releases'].append({'id': rid, 'decision': decision, 'reason': reason, 'gate_passed': gate})
            self._rows = []
        else:
            raise AssertionError('未实现的SQL: ' + s)
        self._idx = 0
        return self

    def executemany(self, sql, seq):
        for params in seq:
            self.execute(sql, params)

    def fetchone(self):
        if self._rows and self._idx < len(self._rows):
            row = self._rows[self._idx]
            self._idx += 1
            return row
        return None

    def fetchall(self):
        rows = self._rows or []
        self._rows = []
        return rows

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False


import copy


class FakeConn:
    def __init__(self, db):
        self.db = db
        self.autocommit = False
        # 进入事务时快照，rollback 时还原，模拟真实事务原子性
        self._snapshot = copy.deepcopy(db)

    def cursor(self, cursor_factory=None):
        return FakeCursor(self.db)

    def commit(self):
        self._snapshot = None

    def rollback(self):
        if self._snapshot is not None:
            # 审计表由独立事务提交，不随业务事务回滚而撤销
            durable_releases = self.db.get('releases')
            self.db.clear()
            self.db.update(copy.deepcopy(self._snapshot))
            if durable_releases is not None and 'releases' in self._snapshot:
                # 保留独立事务已提交的发布/回滚记录
                self.db['releases'] = copy.deepcopy(durable_releases)

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.commit()
        else:
            self.rollback()
        self.close()
        return False


class BaselineFlowTest(unittest.TestCase):
    def setUp(self):
        self.db = {
            'versions': [
                {'id': 1, 'version_code': 'V1', 'high_moisture_threshold': 14.5, 'status': 'ACTIVE'},
                {'id': 2, 'version_code': 'V2', 'high_moisture_threshold': 15.5, 'status': 'CANDIDATE'},
                {'id': 3, 'version_code': 'V3', 'high_moisture_threshold': 17.2, 'status': 'DRAFT'},
            ],
            'retained': [
                {'id': 1, 'lot_id': 101, 'lot_code': 'R01', 'manual_verdict': 'NORMAL'},
                {'id': 2, 'lot_id': 102, 'lot_code': 'R04', 'manual_verdict': 'NORMAL'},
                {'id': 3, 'lot_id': 103, 'lot_code': 'R07', 'manual_verdict': 'HIGH'},
                {'id': 4, 'lot_id': 104, 'lot_code': 'R08', 'manual_verdict': 'HIGH'},
            ],
            'readings': {
                # R01/R04 读数越过 V1 阈值 14.5 但低于 V2 阈值 15.5 → V1 误报、V2 不误报
                101: [{'point_code': 'P1', 'moisture_pct': 14.8, 'instrument_id': 'MT-07', 'sampled_at': None}],
                102: [{'point_code': 'P1', 'moisture_pct': 15.1, 'instrument_id': 'MT-07', 'sampled_at': None}],
                # R07/R08 高湿：R08 均值 16.7 低于 V3 阈值 17.2 → V3 漏报
                103: [{'point_code': 'P1', 'moisture_pct': 18.0, 'instrument_id': 'MT-07', 'sampled_at': None}],
                104: [{'point_code': 'P1', 'moisture_pct': 16.7, 'instrument_id': 'MT-07', 'sampled_at': None}],
            },
            'runs': [], 'items': [], 'releases': [],
            # j1 已领走（在途，锚定 V1）；j2 排队未锚定；j3 排队已锚定 V1
            'jobs': [
                {'id': 1, 'status': 'RUNNING', 'reference_version_id': 1},
                {'id': 2, 'status': 'QUEUED', 'reference_version_id': None},
                {'id': 3, 'status': 'QUEUED', 'reference_version_id': 1},
            ],
        }

    def _patch_conn(self):
        import psycopg2
        # 让 utils.db.conn 与 one/all_rows 内部的 psycopg2.connect 都拿到同一个假连接工厂
        return patch.object(psycopg2, 'connect', lambda url=None: FakeConn(self.db))

    def test_full_flow_snapshot_resume_gate_publish_and_pin(self):
        enqueued = []
        with self._patch_conn(), patch.object(baseline.q, 'put_comparison', lambda rid: enqueued.append(rid)):
            run = baseline.start_run('CMP-1', 1, 2, user_id=9)
            rid = run['id']
            self.assertEqual(enqueued, [rid])
            # 开跑即快照：4 个留样、读数与人工判定都固化
            self.assertEqual(len(self.db['runs'][0]['sample_snapshot']), 4)
            self.assertEqual(len(self.db['items']), 4)

            # 模拟 worker 处理前两项后中断：把后两项留在 PENDING
            baseline.process_comparison(rid)
            # 正常情况下一次处理全部完成
            self.assertEqual(self.db['runs'][0]['status'], 'COMPLETED')
            self.assertTrue(self.db['runs'][0]['gate_passed'])

            # 逐项口径：V1 对 R01/R04 误报，V2 不误报；R07 两版都判 HIGH（无漏报）
            by_code = {i['lot_code']: i for i in self.db['items']}
            self.assertEqual(by_code['R01']['baseline_verdict'], 'HIGH')
            self.assertEqual(by_code['R01']['candidate_verdict'], 'NORMAL')
            self.assertEqual(by_code['R07']['candidate_verdict'], 'HIGH')

            # 发布：闸门通过 → 切换；在途 j1 保持 V1，未锚定排队 j2 跟 V2
            baseline.release_candidate(rid, user_id=9)
            self.assertEqual({v['id']: v['status'] for v in self.db['versions']}, {1: 'ARCHIVED', 2: 'ACTIVE', 3: 'DRAFT'})
            jobs = {j['id']: j for j in self.db['jobs']}
            self.assertEqual(jobs[1]['reference_version_id'], 1)   # 切换前领走，照旧跑完
            self.assertEqual(jobs[2]['reference_version_id'], 2)   # 排队跟新版
            self.assertEqual(jobs[3]['reference_version_id'], 1)   # 已锚定旧版的排队单不被改写
            self.assertEqual(self.db['releases'][-1]['decision'], 'ACTIVATED')

    def test_resume_only_picks_unfinished_and_is_idempotent(self):
        with self._patch_conn(), patch.object(baseline.q, 'put_comparison', lambda rid: None):
            run = baseline.start_run('CMP-2', 1, 2, user_id=9)
            rid = run['id']
            # 人为制造中断：最后一项改回 PENDING，run 置 FAILED
            baseline.process_comparison(rid)
            self.db['items'][-1]['status'] = 'PENDING'
            self.db['items'][-1]['candidate_verdict'] = None
            self.db['runs'][0]['status'] = 'FAILED'
            requeued = []
            with patch.object(baseline.q, 'put_comparison', lambda x: requeued.append(x)):
                baseline.resume_run(rid)
            self.assertEqual(requeued, [rid])
            # 续跑幂等：已完成项不重算，未完成项补齐，最终 COMPLETED
            baseline.process_comparison(rid)
            self.assertEqual(self.db['runs'][0]['status'], 'COMPLETED')
            self.assertTrue(all(i['status'] == 'SUCCEEDED' for i in self.db['items']))

    def test_gate_reject_keeps_old_version(self):
        with self._patch_conn(), patch.object(baseline.q, 'put_comparison', lambda rid: None):
            # V3 阈值 17.2 会漏掉 R08（人工 HIGH）→ 闸门拦截
            run = baseline.start_run('CMP-3', 1, 3, user_id=9)
            rid = run['id']
            baseline.process_comparison(rid)
            self.assertFalse(self.db['runs'][0]['gate_passed'])
            with self.assertRaises(PermissionError):
                baseline.release_candidate(rid, user_id=9)
            # 旧版保持 ACTIVE，留下 REJECTED 记录
            self.assertEqual(next(v for v in self.db['versions'] if v['id'] == 1)['status'], 'ACTIVE')
            self.assertEqual(self.db['releases'][-1]['decision'], 'REJECTED')

    def test_switch_failure_rolls_back_and_keeps_old(self):
        with self._patch_conn(), patch.object(baseline.q, 'put_comparison', lambda rid: None):
            run = baseline.start_run('CMP-4', 1, 2, user_id=9)
            rid = run['id']
            baseline.process_comparison(rid)
            # 激活新版时数据库故障 → 整体回滚，旧版仍 ACTIVE，记录 ROLLED_BACK
            original = FakeCursor.execute

            def exploding(self2, sql, params=()):
                if "UPDATE reference_versions SET status='ACTIVE'" in ' '.join(sql.split()):
                    raise RuntimeError('simulated db failure during switch')
                return original(self2, sql, params)

            with patch.object(FakeCursor, 'execute', exploding):
                with self.assertRaises(RuntimeError):
                    baseline.release_candidate(rid, user_id=9)
            self.assertEqual(next(v for v in self.db['versions'] if v['id'] == 1)['status'], 'ACTIVE')
            self.assertEqual(self.db['releases'][-1]['decision'], 'ROLLED_BACK')


if __name__ == '__main__':
    unittest.main()
