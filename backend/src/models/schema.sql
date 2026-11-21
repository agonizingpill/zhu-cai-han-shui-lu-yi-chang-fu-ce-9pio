CREATE TABLE IF NOT EXISTS users(id SERIAL PRIMARY KEY,username TEXT UNIQUE NOT NULL,display_name TEXT NOT NULL,role TEXT NOT NULL,password_hash TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS bamboo_lots(id SERIAL PRIMARY KEY,lot_code TEXT UNIQUE NOT NULL,origin TEXT NOT NULL,harvest_date DATE NOT NULL,species TEXT NOT NULL,weight_kg NUMERIC(10,2) NOT NULL CHECK(weight_kg>0),target_moisture NUMERIC(5,2) NOT NULL,status TEXT NOT NULL DEFAULT 'RECEIVED',received_at TIMESTAMPTZ NOT NULL DEFAULT now(),operator_id INT REFERENCES users(id));
ALTER TABLE bamboo_lots ADD COLUMN IF NOT EXISTS usage_scope TEXT NOT NULL DEFAULT 'NORMAL';
ALTER TABLE bamboo_lots ADD COLUMN IF NOT EXISTS manual_verdict TEXT;
CREATE TABLE IF NOT EXISTS moisture_samples(id SERIAL PRIMARY KEY,lot_id INT REFERENCES bamboo_lots(id) ON DELETE CASCADE,point_code TEXT NOT NULL,moisture_pct NUMERIC(6,2) NOT NULL CHECK(moisture_pct BETWEEN 0 AND 100),instrument_id TEXT NOT NULL,sampled_at TIMESTAMPTZ NOT NULL DEFAULT now(),UNIQUE(lot_id,point_code));
CREATE TABLE IF NOT EXISTS anomaly_lots(id SERIAL PRIMARY KEY,lot_id INT UNIQUE REFERENCES bamboo_lots(id) ON DELETE CASCADE,status TEXT NOT NULL DEFAULT 'OPEN',reason TEXT NOT NULL,decision_score NUMERIC(10,6),standardized_distance NUMERIC(10,6),model_checksum TEXT,detected_at TIMESTAMPTZ DEFAULT now(),resolved_at TIMESTAMPTZ);
CREATE TABLE IF NOT EXISTS kiln_rechecks(id SERIAL PRIMARY KEY,anomaly_id INT REFERENCES anomaly_lots(id) ON DELETE CASCADE,kiln_code TEXT NOT NULL,scheduled_start TIMESTAMPTZ NOT NULL,scheduled_end TIMESTAMPTZ NOT NULL,status TEXT NOT NULL DEFAULT 'SCHEDULED',pre_moisture NUMERIC(6,2),post_moisture NUMERIC(6,2),result TEXT,notes TEXT,previous_recheck_id INT REFERENCES kiln_rechecks(id),created_by INT REFERENCES users(id));
CREATE TABLE IF NOT EXISTS release_decisions(id SERIAL PRIMARY KEY,lot_id INT REFERENCES bamboo_lots(id) ON DELETE CASCADE,anomaly_id INT REFERENCES anomaly_lots(id),decision TEXT NOT NULL,reason TEXT NOT NULL,decided_by INT REFERENCES users(id),decided_at TIMESTAMPTZ DEFAULT now());
CREATE TABLE IF NOT EXISTS analysis_jobs(id SERIAL PRIMARY KEY,lot_id INT REFERENCES bamboo_lots(id) ON DELETE CASCADE,status TEXT NOT NULL DEFAULT 'QUEUED',input_summary JSONB NOT NULL,result JSONB,error TEXT,created_at TIMESTAMPTZ DEFAULT now(),finished_at TIMESTAMPTZ);
ALTER TABLE analysis_jobs ADD COLUMN IF NOT EXISTS reference_version_id INT;
ALTER TABLE analysis_jobs ADD COLUMN IF NOT EXISTS claimed_at TIMESTAMPTZ;
CREATE TABLE IF NOT EXISTS audit_logs(id SERIAL PRIMARY KEY,action TEXT NOT NULL,path TEXT,method TEXT,status_code INT,created_at TIMESTAMPTZ DEFAULT now());

-- 参考批次版本：同一时刻至多一个 ACTIVE，切换走“候选 -> 发布 -> 激活/回滚”的可追溯流程
CREATE TABLE IF NOT EXISTS reference_versions(
  id SERIAL PRIMARY KEY,
  version_code TEXT UNIQUE NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  high_moisture_threshold NUMERIC(6,2) NOT NULL CHECK(high_moisture_threshold BETWEEN 0 AND 100),
  status TEXT NOT NULL DEFAULT 'DRAFT',
  created_by INT REFERENCES users(id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  activated_at TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS reference_version_members(
  version_id INT NOT NULL REFERENCES reference_versions(id) ON DELETE CASCADE,
  lot_id INT NOT NULL REFERENCES bamboo_lots(id) ON DELETE CASCADE,
  PRIMARY KEY(version_id,lot_id)
);

-- 留样：不允许进入任何参考版本训练集；人工判定在对照开跑时被快照固定
CREATE TABLE IF NOT EXISTS retained_samples(
  id SERIAL PRIMARY KEY,
  lot_id INT UNIQUE NOT NULL REFERENCES bamboo_lots(id) ON DELETE CASCADE,
  usage_scope TEXT NOT NULL DEFAULT 'RETAINED',
  manual_verdict TEXT NOT NULL CHECK(manual_verdict IN ('NORMAL','HIGH')),
  note TEXT NOT NULL DEFAULT '',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 对照跑：两版基准用同一组留样；读数与人工判定以开跑时刻快照为准，中断后只续跑未完成项
CREATE TABLE IF NOT EXISTS comparison_runs(
  id SERIAL PRIMARY KEY,
  run_code TEXT UNIQUE NOT NULL,
  baseline_version_id INT NOT NULL REFERENCES reference_versions(id),
  candidate_version_id INT NOT NULL REFERENCES reference_versions(id),
  status TEXT NOT NULL DEFAULT 'PENDING',
  sample_snapshot JSONB NOT NULL,
  gate_passed BOOLEAN,
  gate_reason TEXT,
  metrics JSONB,
  created_by INT REFERENCES users(id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  started_at TIMESTAMPTZ,
  finished_at TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS comparison_items(
  id SERIAL PRIMARY KEY,
  run_id INT NOT NULL REFERENCES comparison_runs(id) ON DELETE CASCADE,
  retained_id INT NOT NULL REFERENCES retained_samples(id),
  lot_code TEXT NOT NULL,
  readings_snapshot JSONB NOT NULL,
  manual_verdict TEXT NOT NULL,
  baseline_verdict TEXT,
  candidate_verdict TEXT,
  baseline_detail JSONB,
  candidate_detail JSONB,
  status TEXT NOT NULL DEFAULT 'PENDING',
  error TEXT,
  claimed_at TIMESTAMPTZ,
  finished_at TIMESTAMPTZ,
  UNIQUE(run_id,retained_id)
);
CREATE INDEX IF NOT EXISTS idx_comparison_items_status ON comparison_items(status);

-- 发布记录：闸门不通过或激活失败时回滚，旧版保持 ACTIVE
CREATE TABLE IF NOT EXISTS baseline_releases(
  id SERIAL PRIMARY KEY,
  run_id INT REFERENCES comparison_runs(id),
  from_version_id INT REFERENCES reference_versions(id),
  to_version_id INT NOT NULL REFERENCES reference_versions(id),
  decision TEXT NOT NULL CHECK(decision IN ('ACTIVATED','REJECTED','ROLLED_BACK')),
  reason TEXT NOT NULL,
  gate_passed BOOLEAN NOT NULL,
  decided_by INT REFERENCES users(id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
