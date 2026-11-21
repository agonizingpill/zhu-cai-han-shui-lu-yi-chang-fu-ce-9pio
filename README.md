# 竹材含水率异常复测

面向安吉青篁竹材集散中心质量组的入库、含水率异常隔离、窑干复测与出库放行工作台。系统使用多点采样建立批次湿度画像，通过 `StandardScaler + OneClassSVM` 识别偏离正常基线的批次，并把异常批次带入可追溯的窑位排程和主管放行流程。

## 真实场景

模拟客户日均 30–50 批毛竹入库。仓管在纸质批次卡写产地和采伐日，检验员使用 MT-07 手持仪在 Excel 中登记 4–6 个测点，窑炉班组通过微信群协调窑位。痛点是测点缺失后仍可能被手工判为正常，异常批次隔离和窑位重排缺少统一证据，主管盖章时难以回看复测后的最后读数。

上游输入为入库过磅、产地/采伐日和 MT-07 测点记录。本系统替代手工湿度判断、异常隔离和窑位沟通。下游为窑炉班组和出库主管，消费窑位排程、复测结论和出库放行记录。

## 功能模块

- 竹材入库 `BambooLot`：批次号、产地、采伐日、树种、毛重与目标含水率。
- 含水率采样 `MoistureSample`：每个点位只能录入一次，少于 4 点时禁止建模。
- 异常批次 `AnomalyLot`：持久化判定结论、均值/最大值、版本 checksum 和隔离状态。
- 窑干复测 `KilnRecheck`：记录窑位时间窗，冲突时创建 `RESCHEDULED` 版本并保留原排程。
- 出库放行 `ReleaseDecision`：只有 supervisor 能盖章，异常复测未闭环时后端拒绝放行。
- 参考批次版本 `ReferenceVersion`：每版基准带高湿阈值与参考批次成员，同一时刻只有一个 `ACTIVE`，版本状态走 `DRAFT → CANDIDATE → ACTIVE/ARCHIVED`。
- 留样 `RetainedSample`：独立于训练集，带人工判定 `NORMAL/HIGH`；留样禁止进入任何参考版本。
- 对照跑 `ComparisonRun`：基准版与候选版用**同一组留样**对照，读数与人工判定在开跑瞬间快照固定；中断可续跑，只重算未完成项。
- 发布闸门与回滚 `BaselineRelease`：候选必须**误报更少且不新增漏报**才能发布；闸门拦截记 `REJECTED`，切换异常记 `ROLLED_BACK` 且旧版保持 `ACTIVE`。

## 参考批次切换流程（发布闸门）

1. 主管登记留样与人工判定（留样不参与任何训练）。
2. 主管创建候选参考版本并标记 `CANDIDATE`。
3. 主管发起对照跑：系统对全部留样固化读数 + 人工判定快照，向 worker 投递对照任务，两版基准各判一次。
4. worker 逐项写入 `comparison_items`；中途失败可在页面点“续跑”，已完成项不重算。
5. 全部完成后按混淆矩阵计算闸门：误报(FP)=判 HIGH 人工 NORMAL，漏报(FN)=判 NORMAL 人工 HIGH。仅当 `候选 FP < 基准 FP` 且 `候选 FN ≤ 基准 FN` 时 `gate_passed=true`。
6. 主管点“发布切换”：闸门不过则记 `REJECTED` 且旧版不变；通过则在单事务内归档旧版、激活候选。切换中任何异常整体回滚并独立记录 `ROLLED_BACK`，旧版保持 `ACTIVE`。
7. 版本切换对在途初检的影响：切换前已被 worker 领走（`RUNNING`）的初检按旧版跑完；仍在 `QUEUED` 且未锚定的排队任务改跟新版。每张初检单 `analysis_jobs.reference_version_id` 都记录实际使用版本，可在“基准对照”页回溯。

## 判定口径

判定引擎 `backend/src/services/moisture_engine.py` 为纯标准库实现，backend 与 worker 共用同一份代码（worker 镜像内置副本）。按参考版本的高湿阈值，对一组测点读数计算 `mean/std/min/max`，均值或任一测点越过阈值即判 `HIGH`，并生成 16 位 checksum 保证结果可回溯、确定性可复现。发布闸门的混淆矩阵与判定逻辑同样集中在该模块，单元测试直接覆盖。


## 快速启动

```bash
cp .env.example .env
docker compose config --quiet
docker compose up -d --build
```

访问 `http://localhost:18274`。容器内 seed 会自动幂等执行，也可运行 `docker compose exec backend python scripts/seed_demo.py` 复核。

## 演示账号

| 账号 | 密码 | 角色 |
|---|---|---|
| operator | Bamboo#1075 | 录入、采样、送模型、排程、回写复测 |
| supervisor | Bamboo#1075 | 包含 operator 权限，可主管放行 |
| viewer | Bamboo#1075 | 查看批次、异常、放行记录 |

## 枚举位置

后端枚举在 `backend/src/constants/enums.py`，前端枚举在 `frontend/src/constants/enums.js`。具体状态前置条件在 `backend/src/main.py` 的入库、采样、分析、排程、复测和放行路由中。

## 技术栈与目录

| 层 | 技术 |
|---|---|
| frontend | Nginx + 原生 HTML/CSS/ES modules |
| backend | Python 3.11 + FastAPI + psycopg2 |
| worker | Python 3.11 + Redis BRPOP + scikit-learn |
| db | PostgreSQL 16 |
| queue | Redis 7 |

领域代码位于 `backend/src/main.py`、`backend/src/services/baseline.py`、`backend/src/services/moisture_engine.py`、`backend/src/models/schema.sql` 和 `frontend/src/app.js`。seed 位于 `backend/scripts/seed_demo.py`，预置 8 个普通参考批次、10 个留样（含 2 个高湿）与三版基准（V1 生效 / V2 可过闸门 / V3 演示漏报被拦截）。

## 单元与业务流测试

测试不依赖 Postgres/Redis，纯标准库即可运行：

```bash
python3 -I -m unittest discover -s backend/tests -v
```

- `test_moisture_engine.py`：判定确定性、阈值口径、发布闸门（少误报、无新增漏报、误报持平不放行、混淆矩阵）。
- `test_baseline_flow.py`：用内存假库端到端验证开跑快照、中断续跑幂等、闸门拦截保旧版、切换失败回滚、在途/排队初检的版本归属。

## License

Internal demonstration project, MIT-style use for evaluation.
