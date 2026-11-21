import os, json, time, traceback
import psycopg2, redis
from psycopg2.extras import RealDictCursor, Json

import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# worker 与 backend 共用同一份判定口径；backend 镜像内位于 src/services，worker 镜像内置一份副本
try:
    from moisture_engine import judge
except ImportError:  # pragma: no cover - 仅容器布局差异时走这里
    from src.services.moisture_engine import judge

DB = os.getenv('DATABASE_URL', 'postgresql://bamboo:bamboo_dev_password@db:5432/bamboo')
R = redis.Redis.from_url(os.getenv('REDIS_URL', 'redis://redis:6379/0'), decode_responses=True)
JOB_Q = 'bamboo:moisture-analysis'
CMP_Q = 'bamboo:baseline-comparison'


def db():
    for _ in range(30):
        try:
            return psycopg2.connect(DB)
        except Exception:
            time.sleep(1)
    raise RuntimeError('db unavailable')


def heartbeat():
    try:
        from datetime import datetime, timezone
        os.makedirs('/shared', exist_ok=True)
        open('/shared/worker.heartbeat', 'w').write(datetime.now(timezone.utc).isoformat())
    except Exception:
        pass


def process_job(jid):
    """初检：切换前已领走的任务锚定旧版跑完；排队任务在入队时已写当前版本。"""
    c = db(); c.autocommit = True
    cur = c.cursor(cursor_factory=RealDictCursor)
    cur.execute('SELECT * FROM analysis_jobs WHERE id=%s', (jid,))
    job = cur.fetchone()
    if not job or job['status'] == 'SUCCEEDED':
        c.close(); return
    # 领取即锚定：只有仍在排队的任务会在此刻确认版本；RUNNING 任务保留原版本（切换不影响在途单）
    cur.execute(
        """UPDATE analysis_jobs SET status='RUNNING', claimed_at=COALESCE(claimed_at, now()),
           reference_version_id=COALESCE(reference_version_id,(SELECT id FROM reference_versions WHERE status='ACTIVE'))
           WHERE id=%s AND status IN('QUEUED','RUNNING') RETURNING reference_version_id""",
        (jid,),
    )
    row = cur.fetchone()
    if not row:
        c.close(); return
    version_id = row['reference_version_id']
    cur.execute('SELECT version_code, high_moisture_threshold FROM reference_versions WHERE id=%s', (version_id,))
    version = cur.fetchone()
    if not version:
        cur.execute("UPDATE analysis_jobs SET status='FAILED', error='参考版本不存在', finished_at=now() WHERE id=%s", (jid,))
        c.close(); return
    cur.execute('SELECT moisture_pct FROM moisture_samples WHERE lot_id=%s ORDER BY point_code', (job['lot_id'],))
    readings = [float(x['moisture_pct']) for x in cur.fetchall()]
    cur.close(); c.close()
    try:
        out = judge(version['version_code'], float(version['high_moisture_threshold']), readings)
        c = db(); c.autocommit = True
        cur = c.cursor()
        cur.execute("UPDATE analysis_jobs SET status='SUCCEEDED', result=%s, finished_at=now() WHERE id=%s", (Json(out), jid))
        cur.execute('SELECT id FROM anomaly_lots WHERE lot_id=%s', (job['lot_id'],))
        a = cur.fetchone()
        if out['verdict'] == 'HIGH':
            if a:
                cur.execute("UPDATE anomaly_lots SET status='OPEN',reason=%s,decision_score=%s,standardized_distance=%s,model_checksum=%s WHERE id=%s",
                            ('版本化初检判定为高湿，需窑干复测', out['mean'], out['max'], out['checksum'], a[0]))
            else:
                cur.execute("INSERT INTO anomaly_lots(lot_id,status,reason,decision_score,standardized_distance,model_checksum) VALUES(%s,'OPEN',%s,%s,%s,%s)",
                            (job['lot_id'], '版本化初检判定为高湿，需窑干复测', out['mean'], out['max'], out['checksum']))
            cur.execute("UPDATE bamboo_lots SET status='QUARANTINED' WHERE id=%s", (job['lot_id'],))
        else:
            cur.execute("UPDATE bamboo_lots SET status='CLEARED' WHERE id=%s AND status<>'RELEASED'", (job['lot_id'],))
        c.close()
    except Exception as e:
        c = db(); c.autocommit = True
        c.cursor().execute("UPDATE analysis_jobs SET status='FAILED', error=%s, finished_at=now() WHERE id=%s", (str(e), jid))
        c.close(); traceback.print_exc()


def process_comparison(run_id):
    """对照跑：逐项判定、可续跑。与 backend 的 baseline.process_comparison 同构，worker 独立可跑。"""
    c = db(); c.autocommit = False
    cur = c.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute('SELECT status FROM comparison_runs WHERE id=%s', (run_id,))
        run_row = cur.fetchone()
        if not run_row or run_row['status'] == 'COMPLETED':
            c.rollback(); c.close(); return
        cur.execute("UPDATE comparison_runs SET status='RUNNING' WHERE id=%s", (run_id,))
        cur.execute(
            """SELECT v.id, v.version_code, v.high_moisture_threshold
               FROM comparison_runs cr JOIN reference_versions v ON v.id IN(cr.baseline_version_id, cr.candidate_version_id)
               WHERE cr.id=%s""", (run_id,))
        versions = {r['id']: dict(r) for r in cur.fetchall()}
        cur.execute('SELECT baseline_version_id, candidate_version_id FROM comparison_runs WHERE id=%s', (run_id,))
        pair = cur.fetchone()
        base_v, cand_v = versions[pair['baseline_version_id']], versions[pair['candidate_version_id']]
        cur.execute("SELECT * FROM comparison_items WHERE run_id=%s AND status IN('PENDING','RUNNING','FAILED') ORDER BY id", (run_id,))
        for item in cur.fetchall():
            cur.execute("UPDATE comparison_items SET status='RUNNING', claimed_at=now(), error=NULL WHERE id=%s", (item['id'],))
            readings = [x['moisture_pct'] for x in item['readings_snapshot']]
            try:
                base_out = judge(base_v['version_code'], float(base_v['high_moisture_threshold']), readings)
                cand_out = judge(cand_v['version_code'], float(cand_v['high_moisture_threshold']), readings)
                cur.execute(
                    """UPDATE comparison_items SET status='SUCCEEDED', baseline_verdict=%s, candidate_verdict=%s,
                       baseline_detail=%s, candidate_detail=%s, finished_at=now(), error=NULL WHERE id=%s""",
                    (base_out['verdict'], cand_out['verdict'], Json(base_out), Json(cand_out), item['id']))
            except Exception as e:
                cur.execute("UPDATE comparison_items SET status='FAILED', error=%s WHERE id=%s", (str(e), item['id']))
        cur.execute("SELECT count(*) n FROM comparison_items WHERE run_id=%s AND status<>'SUCCEEDED'", (run_id,))
        if cur.fetchone()['n']:
            cur.execute("UPDATE comparison_runs SET status='FAILED' WHERE id=%s", (run_id,))
            c.commit(); c.close(); return
        cur.execute('SELECT * FROM comparison_items WHERE run_id=%s ORDER BY id', (run_id,))
        items = cur.fetchall()
        manual = {i['lot_code']: i['manual_verdict'] for i in items}
        bv = {i['lot_code']: i['baseline_verdict'] for i in items}
        cv = {i['lot_code']: i['candidate_verdict'] for i in items}
        try:
            from moisture_engine import evaluate_gate
        except ImportError:  # pragma: no cover
            from src.services.moisture_engine import evaluate_gate
        metrics = evaluate_gate(bv, cv, manual)
        cur.execute("UPDATE comparison_runs SET status='COMPLETED', gate_passed=%s, gate_reason=%s, metrics=%s, finished_at=now() WHERE id=%s",
                    (metrics['passed'], metrics['reason'], Json(metrics), run_id))
        c.commit(); c.close()
    except Exception:
        c.rollback(); c.close(); raise


def recover_stale():
    """启动/定时回收崩溃残留：RUNNING 的初检与 FAILED 的对照可被重新投递续跑。"""
    c = db(); c.autocommit = True
    cur = c.cursor()
    cur.execute("UPDATE analysis_jobs SET status='QUEUED', claimed_at=NULL WHERE status='RUNNING'")
    cur.close(); c.close()
    # 重新入队所有 QUEUED 任务（幂等：process_job 对 SUCCEEDED 直接跳过）
    c = db(); c.autocommit = True
    cur = c.cursor()
    cur.execute("SELECT id FROM analysis_jobs WHERE status='QUEUED' ORDER BY id")
    for (jid,) in cur.fetchall():
        R.rpush(JOB_Q, json.dumps({'job_id': jid}))
    cur.execute("SELECT id FROM comparison_runs WHERE status IN('PENDING','RUNNING','FAILED') ORDER BY id")
    for (rid,) in cur.fetchall():
        cur.execute("SELECT count(*) FROM comparison_items WHERE run_id=%s AND status<>'SUCCEEDED'", (rid,))
        if cur.fetchone()[0]:
            R.rpush(CMP_Q, json.dumps({'run_id': rid}))
    cur.close(); c.close()
    return stale_jobs


heartbeat()
recover_stale()
while True:
    heartbeat()
    # 两条队列轮询，短超时保证对照与初检都能及时被处理
    item = R.blpop(JOB_Q, timeout=2)
    if item:
        try:
            process_job(json.loads(item[1])['job_id'])
        except Exception:
            traceback.print_exc()
        continue
    item = R.blpop(CMP_Q, timeout=2)
    if item:
        try:
            process_comparison(json.loads(item[1])['run_id'])
        except Exception:
            traceback.print_exc()
