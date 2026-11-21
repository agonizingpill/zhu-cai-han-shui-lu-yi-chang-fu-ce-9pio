"""参考批次版本、留样对照与发布切换的事务逻辑。"""
import json
from datetime import datetime, timezone

from psycopg2.extras import RealDictCursor, Json

from ..utils.db import conn, one, all_rows
from .moisture_engine import judge, evaluate_gate
from . import queue as q

MIN_REFERENCE_LOTS = 3


# ---------- 参考版本 ----------
def list_versions():
    rows = all_rows(
        """SELECT v.*, count(m.lot_id) member_count
           FROM reference_versions v
           LEFT JOIN reference_version_members m ON m.version_id=v.id
           GROUP BY v.id ORDER BY v.created_at DESC, v.id DESC"""
    )
    return [dict(r) for r in rows]


def get_version(version_id):
    v = one('SELECT * FROM reference_versions WHERE id=%s', (version_id,))
    if not v:
        return None
    members = all_rows(
        """SELECT l.id, l.lot_code, l.origin
           FROM reference_version_members m JOIN bamboo_lots l ON l.id=m.lot_id
           WHERE m.version_id=%s ORDER BY l.lot_code""",
        (version_id,),
    )
    out = dict(v)
    out['members'] = [dict(m) for m in members]
    return out


def create_version(version_code, description, high_moisture_threshold, member_lot_ids, user_id):
    member_ids = sorted({int(x) for x in member_lot_ids})
    if len(member_ids) < MIN_REFERENCE_LOTS:
        raise ValueError(f'参考批次至少 {MIN_REFERENCE_LOTS} 个，当前 {len(member_ids)} 个')
    with conn() as c:
        with c.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute('SELECT id FROM reference_versions WHERE version_code=%s', (version_code,))
            if cur.fetchone():
                raise LookupError('版本号已存在')
            # 留样不允许进入任何参考版本训练集；隔离批次也不能作为正常基线
            cur.execute(
                """SELECT l.id, l.lot_code, l.usage_scope, l.status, count(s.id) pts
                   FROM bamboo_lots l LEFT JOIN moisture_samples s ON s.lot_id=l.id
                   WHERE l.id = ANY(%s) GROUP BY l.id ORDER BY l.id""",
                (member_ids,),
            )
            found = {r['id']: r for r in cur.fetchall()}
            missing = [i for i in member_ids if i not in found]
            if missing:
                raise ValueError(f'批次不存在: {missing}')
            retained = [r['lot_code'] for r in found.values() if r['usage_scope'] == 'RETAINED']
            if retained:
                raise ValueError(f'留样不能作为参考批次: {retained}')
            sparse = [r['lot_code'] for r in found.values() if r['pts'] < 4]
            if sparse:
                raise ValueError(f'参考批次测点不足 4 个: {sparse}')
            cur.execute(
                """INSERT INTO reference_versions(version_code,description,high_moisture_threshold,status,created_by)
                   VALUES(%s,%s,%s,'DRAFT',%s) RETURNING id""",
                (version_code, description, high_moisture_threshold, user_id),
            )
            vid = cur.fetchone()['id']
            cur.executemany(
                'INSERT INTO reference_version_members(version_id,lot_id) VALUES(%s,%s)',
                [(vid, i) for i in member_ids],
            )
    return get_version(vid)


def mark_candidate(version_id):
    with conn() as c:
        with c.cursor() as cur:
            cur.execute("SELECT status FROM reference_versions WHERE id=%s", (version_id,))
            row = cur.fetchone()
            if not row:
                raise LookupError('版本不存在')
            if row[0] == 'ACTIVE':
                raise ValueError('当前生效版本无需标记为候选')
            cur.execute(
                "UPDATE reference_versions SET status='CANDIDATE' WHERE id=%s AND status='DRAFT'",
                (version_id,),
            )


# ---------- 留样 ----------
def list_retained():
    return [dict(r) for r in all_rows(
        """SELECT r.id, r.lot_id, r.manual_verdict, r.note, r.created_at,
                  l.lot_code, l.origin, l.harvest_date, count(s.id) point_count
           FROM retained_samples r
           JOIN bamboo_lots l ON l.id=r.lot_id
           LEFT JOIN moisture_samples s ON s.lot_id=r.lot_id
           GROUP BY r.id, l.id ORDER BY l.lot_code"""
    )]


def register_retained(lot_id, manual_verdict, note):
    with conn() as c:
        with c.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute('SELECT id, usage_scope FROM bamboo_lots WHERE id=%s', (lot_id,))
            lot = cur.fetchone()
            if not lot:
                raise LookupError('批次不存在')
            cur.execute('SELECT count(*) n FROM moisture_samples WHERE lot_id=%s', (lot_id,))
            if cur.fetchone()['n'] < 4:
                raise ValueError('留样至少需要 4 个测点读数')
            cur.execute(
                """INSERT INTO retained_samples(lot_id,manual_verdict,note) VALUES(%s,%s,%s)
                   ON CONFLICT(lot_id) DO UPDATE SET manual_verdict=EXCLUDED.manual_verdict, note=EXCLUDED.note""",
                (lot_id, manual_verdict, note),
            )
            cur.execute("UPDATE bamboo_lots SET usage_scope='RETAINED', manual_verdict=%s WHERE id=%s",
                        (manual_verdict, lot_id))


def _lot_readings(cur, lot_id):
    cur.execute(
        'SELECT point_code, moisture_pct, instrument_id, sampled_at FROM moisture_samples WHERE lot_id=%s ORDER BY point_code',
        (lot_id,),
    )
    return [
        {
            'point_code': r['point_code'],
            'moisture_pct': float(r['moisture_pct']),
            'instrument_id': r['instrument_id'],
            'sampled_at': r['sampled_at'].isoformat() if r['sampled_at'] else None,
        }
        for r in cur.fetchall()
    ]


# ---------- 对照跑 ----------
def list_runs():
    return [dict(r) for r in all_rows(
        """SELECT cr.*, b.version_code baseline_code, c.version_code candidate_code,
                  (SELECT count(*) FROM comparison_items ci WHERE ci.run_id=cr.id) item_total,
                  (SELECT count(*) FROM comparison_items ci WHERE ci.run_id=cr.id AND ci.status='SUCCEEDED') item_done
           FROM comparison_runs cr
           JOIN reference_versions b ON b.id=cr.baseline_version_id
           JOIN reference_versions c ON c.id=cr.candidate_version_id
           ORDER BY cr.created_at DESC, cr.id DESC"""
    )]


def get_run(run_id):
    run = one(
        """SELECT cr.*, b.version_code baseline_code, c.version_code candidate_code
           FROM comparison_runs cr
           JOIN reference_versions b ON b.id=cr.baseline_version_id
           JOIN reference_versions c ON c.id=cr.candidate_version_id
           WHERE cr.id=%s""",
        (run_id,),
    )
    if not run:
        return None
    items = all_rows('SELECT * FROM comparison_items WHERE run_id=%s ORDER BY id', (run_id,))
    out = dict(run)
    out['items'] = [dict(i) for i in items]
    return out


def start_run(run_code, baseline_id, candidate_id, user_id):
    if baseline_id == candidate_id:
        raise ValueError('基准版本与候选版本不能相同')
    with conn() as c:
        with c.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute('SELECT id FROM comparison_runs WHERE run_code=%s', (run_code,))
            if cur.fetchone():
                raise LookupError('对照跑编号已存在')
            cur.execute('SELECT id, version_code, high_moisture_threshold FROM reference_versions WHERE id IN(%s,%s)',
                        (baseline_id, candidate_id))
            versions = {r['id']: r for r in cur.fetchall()}
            if baseline_id not in versions or candidate_id not in versions:
                raise LookupError('参考版本不存在')
            cur.execute('SELECT r.id, r.lot_id, r.manual_verdict, l.lot_code FROM retained_samples r JOIN bamboo_lots l ON l.id=r.lot_id ORDER BY l.lot_code')
            retained = cur.fetchall()
            if len(retained) < 4:
                raise ValueError('留样数量不足，至少需要 4 个留样才能开跑对照')
            snapshot, items = [], []
            for r in retained:
                readings = _lot_readings(cur, r['lot_id'])
                # 留样读数与人工判定以开跑时刻为准：整体快照 + 逐项快照双重固化
                snap = {
                    'retained_id': r['id'], 'lot_id': r['lot_id'], 'lot_code': r['lot_code'],
                    'manual_verdict': r['manual_verdict'], 'readings': readings,
                    'captured_at': datetime.now(timezone.utc).isoformat(),
                }
                snapshot.append(snap)
                items.append((r['id'], r['lot_code'], Json(readings), r['manual_verdict']))
            cur.execute(
                """INSERT INTO comparison_runs(run_code,baseline_version_id,candidate_version_id,status,sample_snapshot,created_by,started_at)
                   VALUES(%s,%s,%s,'PENDING',%s,%s,now()) RETURNING id""",
                (run_code, baseline_id, candidate_id, Json(snapshot), user_id),
            )
            run_id = cur.fetchone()['id']
            cur.executemany(
                """INSERT INTO comparison_items(run_id,retained_id,lot_code,readings_snapshot,manual_verdict)
                   VALUES(%s,%s,%s,%s,%s)""",
                [(run_id, *it) for it in items],
            )
    q.put_comparison(run_id)
    return get_run(run_id)


def resume_run(run_id):
    """中断续跑：只要还有未完成项就重新投递；已完成项不会重算。"""
    run = one('SELECT id, status FROM comparison_runs WHERE id=%s', (run_id,))
    if not run:
        raise LookupError('对照跑不存在')
    pending = one(
        "SELECT count(*) n FROM comparison_items WHERE run_id=%s AND status IN('PENDING','RUNNING','FAILED')",
        (run_id,),
    )['n']
    if pending == 0:
        raise ValueError('该对照跑已全部完成，无需续跑')
    with conn() as c:
        with c.cursor() as cur:
            cur.execute("UPDATE comparison_runs SET status='RUNNING', finished_at=NULL WHERE id=%s AND status<>'COMPLETED'", (run_id,))
    q.put_comparison(run_id)
    return get_run(run_id)


def process_comparison(run_id):
    """worker 调用：领取未完成留样，按两版基准各自判定，全部完成后计算闸门。"""
    with conn() as c:
        c.autocommit = False
        with c.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT status FROM comparison_runs WHERE id=%s", (run_id,))
            run_row = cur.fetchone()
            if not run_row:
                return
            if run_row['status'] == 'COMPLETED':
                return
            cur.execute("UPDATE comparison_runs SET status='RUNNING' WHERE id=%s", (run_id,))
            cur.execute(
                'SELECT v.id, v.version_code, v.high_moisture_threshold FROM reference_versions v JOIN comparison_runs cr ON v.id IN(cr.baseline_version_id,cr.candidate_version_id) WHERE cr.id=%s',
                (run_id,),
            )
            versions = {r['id']: dict(r) for r in cur.fetchall()}
            cur.execute(
                """SELECT cr.baseline_version_id, cr.candidate_version_id FROM comparison_runs cr WHERE cr.id=%s""",
                (run_id,),
            )
            pair = cur.fetchone()
            base_v, cand_v = versions[pair['baseline_version_id']], versions[pair['candidate_version_id']]
            cur.execute(
                "SELECT * FROM comparison_items WHERE run_id=%s AND status IN('PENDING','RUNNING','FAILED') ORDER BY id",
                (run_id,),
            )
            todo = cur.fetchall()
            for item in todo:
                cur.execute("UPDATE comparison_items SET status='RUNNING', claimed_at=now(), error=NULL WHERE id=%s", (item['id'],))
                readings = [x['moisture_pct'] for x in item['readings_snapshot']]
                try:
                    base_out = judge(base_v['version_code'], base_v['high_moisture_threshold'], readings)
                    cand_out = judge(cand_v['version_code'], cand_v['high_moisture_threshold'], readings)
                    cur.execute(
                        """UPDATE comparison_items SET status='SUCCEEDED', baseline_verdict=%s, candidate_verdict=%s,
                           baseline_detail=%s, candidate_detail=%s, finished_at=now(), error=NULL WHERE id=%s""",
                        (base_out['verdict'], cand_out['verdict'], Json(base_out), Json(cand_out), item['id']),
                    )
                except Exception as e:  # 单项失败不拖垮整跑，留待续跑重试
                    cur.execute("UPDATE comparison_items SET status='FAILED', error=%s WHERE id=%s", (str(e), item['id']))
            cur.execute(
                "SELECT count(*) n FROM comparison_items WHERE run_id=%s AND status<>'SUCCEEDED'",
                (run_id,),
            )
            unfinished = cur.fetchone()['n']
            if unfinished:
                cur.execute("UPDATE comparison_runs SET status='FAILED' WHERE id=%s", (run_id,))
                c.commit()
                return
            cur.execute('SELECT * FROM comparison_items WHERE run_id=%s ORDER BY id', (run_id,))
            items = cur.fetchall()
            baseline_verdicts = {i['lot_code']: i['baseline_verdict'] for i in items}
            candidate_verdicts = {i['lot_code']: i['candidate_verdict'] for i in items}
            manual = {i['lot_code']: i['manual_verdict'] for i in items}
            metrics = evaluate_gate(baseline_verdicts, candidate_verdicts, manual)
            cur.execute(
                "UPDATE comparison_runs SET status='COMPLETED', gate_passed=%s, gate_reason=%s, metrics=%s, finished_at=now() WHERE id=%s",
                (metrics['passed'], metrics['reason'], Json(metrics), run_id),
            )
            c.commit()


# ---------- 发布 / 切换 ----------
def release_candidate(run_id, user_id):
    """通过闸门才允许切换；任何一步失败整体回滚，旧版保持 ACTIVE。"""
    run = one('SELECT * FROM comparison_runs WHERE id=%s', (run_id,))
    if not run:
        raise LookupError('对照跑不存在')
    if run['status'] != 'COMPLETED':
        raise ValueError('对照跑尚未完成，不能发布')
    if not run['gate_passed']:
        # 闸门不通过：登记 REJECTED，旧版保持不变
        with conn() as c:
            with c.cursor() as cur:
                cur.execute(
                    """INSERT INTO baseline_releases(run_id,from_version_id,to_version_id,decision,reason,gate_passed,decided_by)
                       VALUES(%s,%s,%s,'REJECTED',%s,%s,%s)""",
                    (run_id, run['baseline_version_id'], run['candidate_version_id'], run['gate_reason'], False, user_id),
                )
        raise PermissionError('发布闸门未通过：' + run['gate_reason'])
    with conn() as c:
        with c.cursor() as cur:
            cur.execute("SELECT id FROM reference_versions WHERE status='ACTIVE'")
            active_row = cur.fetchone()
            old_id = active_row[0] if active_row else None
            if old_id != run['baseline_version_id']:
                raise ValueError('对照基准已不是当前生效版本，请重新开跑对照')
            try:
                # 切换前已领走（RUNNING/SUCCEEDED/FAILED）的初检锚定旧版，排队中的 QUEUED 任务改跟新版
                cur.execute(
                    'UPDATE analysis_jobs SET reference_version_id=%s WHERE reference_version_id IS NULL AND status=%s',
                    (run['candidate_version_id'], 'QUEUED'),
                )
                cur.execute("UPDATE reference_versions SET status='ARCHIVED', activated_at=NULL WHERE id=%s", (old_id,))
                cur.execute(
                    "UPDATE reference_versions SET status='ACTIVE', activated_at=now() WHERE id=%s",
                    (run['candidate_version_id'],),
                )
                # 其余候选/草稿不动；老 ACTIVE 已归档，保证同一时刻只有一个 ACTIVE
                cur.execute(
                    """INSERT INTO baseline_releases(run_id,from_version_id,to_version_id,decision,reason,gate_passed,decided_by)
                       VALUES(%s,%s,%s,'ACTIVATED',%s,%s,%s)""",
                    (run_id, old_id, run['candidate_version_id'], run['gate_reason'], True, user_id),
                )
            except Exception:
                # 切换事务即将整体回滚；回滚记录必须独立提交，否则会被同一事务的 rollback 一并撤销
                _record_release(run_id, old_id, run['candidate_version_id'], 'ROLLED_BACK', '切换失败，已保留旧版', False, user_id)
                raise
    return get_run(run_id)


def _record_release(run_id, from_id, to_id, decision, reason, gate_passed, user_id):
    """在独立事务里写发布/回滚审计，确保不被外层业务事务回滚掉。"""
    with conn() as c:
        with c.cursor() as cur:
            cur.execute(
                """INSERT INTO baseline_releases(run_id,from_version_id,to_version_id,decision,reason,gate_passed,decided_by)
                   VALUES(%s,%s,%s,%s,%s,%s,%s)""",
                (run_id, from_id, to_id, decision, reason, gate_passed, user_id),
            )


def list_releases():
    return [dict(r) for r in all_rows(
        """SELECT br.*, f.version_code from_code, t.version_code to_code, u.display_name decided_by_name
           FROM baseline_releases br
           LEFT JOIN reference_versions f ON f.id=br.from_version_id
           JOIN reference_versions t ON t.id=br.to_version_id
           LEFT JOIN users u ON u.id=br.decided_by
           ORDER BY br.created_at DESC, br.id DESC"""
    )]


def active_version():
    return one('SELECT * FROM reference_versions WHERE status=%s ORDER BY activated_at DESC NULLS LAST LIMIT 1', ('ACTIVE',))
