import os, sys, time, json
sys.path.insert(0, '/app')
from src.utils.db import conn
from src.utils.security import hp

if os.getenv('SEED_DEMO', 'true').lower() != 'true':
    raise SystemExit(0)

schema = open('/app/src/models/schema.sql').read()

# 普通参考批次（训练集）：全部在目标含水率窗内，留样永不进入这些批次
TRAINING_LOTS = [
    # (批次号, 产地, 采伐日, 毛重, 目标, 6 个测点读数)
    ('BZ-T01-20261115', '浙江安吉', '2026-11-15', 880, 14, [11.4, 11.8, 12.0, 11.6, 11.9, 11.5]),
    ('BZ-T02-20261115', '浙江安吉', '2026-11-15', 760, 14, [12.3, 12.1, 12.5, 12.2, 12.0, 12.4]),
    ('BZ-T03-20261116', '福建建瓯', '2026-11-16', 900, 15, [13.1, 13.4, 13.0, 13.2, 13.5, 13.3]),
    ('BZ-T04-20261116', '江西奉新', '2026-11-16', 820, 14, [10.9, 11.1, 11.3, 11.0, 11.2, 11.0]),
    ('BZ-T05-20261117', '湖南桃江', '2026-11-17', 950, 15, [12.8, 13.0, 12.6, 12.9, 13.1, 12.7]),
    ('BZ-T06-20261117', '浙江龙泉', '2026-11-17', 700, 14, [11.7, 11.9, 12.1, 11.8, 12.0, 11.6]),
    ('BZ-T07-20261118', '浙江安吉', '2026-11-18', 870, 14, [12.0, 12.2, 11.9, 12.1, 12.3, 12.0]),
    ('BZ-T08-20261118', '福建建瓯', '2026-11-18', 730, 15, [13.5, 13.2, 13.4, 13.1, 13.3, 13.0]),
]

# 留样（不参加任何训练）：读数与人工判定在对照开跑时快照固定
# 人工 NORMAL=合格，HIGH=高湿；R-07/R-08 为高湿留样（漏报考核项）
RETAINED = [
    ('BZ-R01-20261119', '浙江安吉', '2026-11-19', 690, 14, [13.2, 13.5, 13.0, 13.4, 13.1, 13.3], 'NORMAL', '整体接近上限，人工复核合格'),
    ('BZ-R02-20261119', '江西奉新', '2026-11-19', 810, 14, [13.6, 13.3, 13.7, 13.5, 13.4, 13.2], 'NORMAL', '均值偏高但未越线'),
    ('BZ-R03-20261119', '湖南桃江', '2026-11-19', 760, 15, [13.8, 14.0, 13.9, 14.1, 13.7, 14.0], 'NORMAL', '边界合格批'),
    ('BZ-R04-20261120', '浙江龙泉', '2026-11-20', 920, 14, [14.2, 14.5, 14.1, 14.3, 14.0, 14.4], 'NORMAL', '人工判合格：旧版误报高发区'),
    ('BZ-R05-20261120', '浙江安吉', '2026-11-20', 650, 14, [12.4, 12.7, 12.5, 12.6, 12.3, 12.8], 'NORMAL', '稳定合格批'),
    ('BZ-R06-20261120', '福建建瓯', '2026-11-20', 780, 15, [11.9, 12.1, 12.0, 12.2, 11.8, 12.0], 'NORMAL', '稳定合格批'),
    ('BZ-R07-20261120', '江西奉新', '2026-11-20', 890, 14, [17.8, 18.2, 17.5, 18.0, 17.9, 18.3], 'HIGH', '明显高湿，两版都应判出'),
    ('BZ-R08-20261121', '湖南桃江', '2026-11-21', 840, 14, [16.6, 16.9, 16.4, 16.8, 16.5, 17.0], 'HIGH', '中高湿：V3 阈值过松会漏报'),
    ('BZ-R09-20261121', '浙江龙泉', '2026-11-21', 710, 14, [13.4, 13.6, 13.5, 13.7, 13.3, 13.8], 'NORMAL', '合格批，V3 不误报但会漏掉 R-08'),
    ('BZ-R10-20261121', '浙江安吉', '2026-11-21', 900, 15, [12.9, 13.1, 13.0, 13.2, 12.8, 13.0], 'NORMAL', '稳定合格批'),
]

# 三版基准：V1 旧版（阈值严、误报多）；V2 候选（放松阈值，少误报且不新增漏报）；V3 过松（会漏报，闸门应拦截）
VERSIONS = [
    ('REF-V1-20261101', 'V1 旧版基准：14.5% 高湿阈值，误报偏多', 14.5, 'ACTIVE', [1, 2, 3, 4, 5, 6]),
    ('REF-V2-20261115', 'V2 候选基准：15.5% 高湿阈值，留样对照少误报无新增漏报', 15.5, 'CANDIDATE', [3, 4, 5, 6, 7, 8]),
    ('REF-V3-20261118', 'V3 过松候选：17.2% 高湿阈值，演示漏报被闸门拦截', 17.2, 'DRAFT', [1, 3, 5, 7]),
]


def ensure_lot(cur, code, origin, day, kg, target, readings, usage, manual):
    cur.execute("SELECT id FROM bamboo_lots WHERE lot_code=%s", (code,))
    row = cur.fetchone()
    if row:
        lid = row[0]
    else:
        cur.execute(
            "INSERT INTO bamboo_lots(lot_code,origin,harvest_date,species,weight_kg,target_moisture,status,operator_id,usage_scope,manual_verdict) VALUES(%s,%s,%s,'毛竹',%s,%s,'SAMPLED',1,%s,%s) RETURNING id",
            (code, origin, day, kg, target, usage, manual))
        lid = cur.fetchone()[0]
    for i, v in enumerate(readings, 1):
        cur.execute("INSERT INTO moisture_samples(lot_id,point_code,moisture_pct,instrument_id) VALUES(%s,%s,%s,'MT-07') ON CONFLICT(lot_id,point_code) DO NOTHING", (lid, f'P-{i:02d}', v))
    return lid


for attempt in range(30):
    try:
        with conn() as c:
            with c.cursor() as cur:
                cur.execute(schema)
                for u, n, r in [('operator', '林竹', 'operator'), ('supervisor', '周窑', 'supervisor'), ('viewer', '沈库', 'viewer')]:
                    cur.execute('INSERT INTO users(username,display_name,role,password_hash) VALUES(%s,%s,%s,%s) ON CONFLICT(username) DO NOTHING', (u, n, r, hp('Bamboo#1075')))
                # 既有演示批次（保持幂等）
                demo_lots = [('BZ-601-20261118', '浙江安吉', '2026-11-18', 860, 14), ('BZ-602-20261119', '浙江安吉', '2026-11-19', 910, 14), ('BZ-603-20261119', '福建建瓯', '2026-11-19', 780, 15), ('BZ-604-20261120', '江西奉新', '2026-11-20', 1020, 14), ('BZ-605-20261120', '湖南桃江', '2026-11-20', 640, 15), ('BZ-606-20261120', '浙江龙泉', '2026-11-20', 720, 14)]
                for code, origin, day, kg, target in demo_lots:
                    cur.execute("INSERT INTO bamboo_lots(lot_code,origin,harvest_date,species,weight_kg,target_moisture,status,operator_id) VALUES(%s,%s,%s,'毛竹',%s,%s,'RECEIVED',1) ON CONFLICT(lot_code) DO NOTHING", (code, origin, day, kg, target))
                demo_vals = {'BZ-601-20261118': [10.8, 11.2, 10.9, 11.0, 11.1, 10.7], 'BZ-602-20261119': [11.3, 11.0, 11.2, 11.1, 10.9, 11.2], 'BZ-603-20261119': [12.2, 12.0, 12.4, 12.1, 12.3, 12.0], 'BZ-604-20261120': [21.0, 22.8, 23.5, 24.1, 22.2, 23.0], 'BZ-605-20261120': [20.2, 21.1], 'BZ-606-20261120': [19.4, 20.1, 21.5, 20.7, 19.9, 20.6]}
                for code, vs in demo_vals.items():
                    cur.execute('SELECT id FROM bamboo_lots WHERE lot_code=%s', (code,)); lid = cur.fetchone()[0]
                    for i, v in enumerate(vs, 1):
                        cur.execute("INSERT INTO moisture_samples(lot_id,point_code,moisture_pct,instrument_id) VALUES(%s,%s,%s,'MT-07') ON CONFLICT(lot_id,point_code) DO NOTHING", (lid, f'P-{i:02d}', v))
                    cur.execute("UPDATE bamboo_lots SET status='SAMPLED' WHERE id=%s", (lid,))
                for code, reason in [('BZ-604-20261120', 'seed异常批次，等待窑干复测'), ('BZ-606-20261120', '待模型确认的高湿批次')]:
                    cur.execute('SELECT id FROM bamboo_lots WHERE lot_code=%s', (code,)); lid = cur.fetchone()[0]
                    cur.execute("INSERT INTO anomaly_lots(lot_id,status,reason) VALUES(%s,'OPEN',%s) ON CONFLICT(lot_id) DO NOTHING", (lid, reason))
                    cur.execute("UPDATE bamboo_lots SET status='QUARANTINED' WHERE id=%s", (lid,))

                training_ids = []
                for code, origin, day, kg, target, readings in TRAINING_LOTS:
                    training_ids.append(ensure_lot(cur, code, origin, day, kg, target, readings, 'NORMAL', None))

                retained_ids = []
                for code, origin, day, kg, target, readings, manual, note in RETAINED:
                    lid = ensure_lot(cur, code, origin, day, kg, target, readings, 'RETAINED', manual)
                    cur.execute("INSERT INTO retained_samples(lot_id,manual_verdict,note) VALUES(%s,%s,%s) ON CONFLICT(lot_id) DO UPDATE SET manual_verdict=EXCLUDED.manual_verdict, note=EXCLUDED.note", (lid, manual, note))
                    retained_ids.append(lid)

                for vcode, desc, threshold, status, member_idx in VERSIONS:
                    cur.execute('SELECT id FROM reference_versions WHERE version_code=%s', (vcode,))
                    row = cur.fetchone()
                    if row:
                        vid = row[0]
                    else:
                        cur.execute('INSERT INTO reference_versions(version_code,description,high_moisture_threshold,status,created_by) VALUES(%s,%s,%s,%s,1) RETURNING id', (vcode, desc, threshold, status))
                        vid = cur.fetchone()[0]
                    member_ids = [training_ids[i - 1] for i in member_idx]
                    cur.executemany('INSERT INTO reference_version_members(version_id,lot_id) VALUES(%s,%s) ON CONFLICT DO NOTHING', [(vid, m) for m in member_ids])
                # 保证只有 V1 是 ACTIVE（幂等重跑时收敛状态）
                cur.execute("UPDATE reference_versions SET status='ACTIVE', activated_at=COALESCE(activated_at, now()) WHERE version_code='REF-V1-20261101'")
        print('seed_demo: idempotent seed complete')
        break
    except Exception:
        if attempt == 29:
            raise
        time.sleep(1)
