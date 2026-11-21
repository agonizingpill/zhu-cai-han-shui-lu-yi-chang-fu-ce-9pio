"""含水率初检判定引擎（参考批次版本化）。

口径说明：
- 每个参考批次版本用一个高湿阈值 high_moisture_threshold 表征，初检时
  以留样/批次的多点读数计算 mean/std/max，超过阈值即判 HIGH（疑似高湿异常）。
- 判定完全确定性：同一版本参数 + 同一组读数永远得到同一结论，checksum
  让初检结果可以回溯到“用的是哪版基准、按什么读数算的”。
- 不依赖 numpy / sklearn，便于在对照、worker 与单元测试里复用同一份代码。
"""
import hashlib
import json
import statistics

SEED = 1075


def summarize(readings):
    vals = [float(x) for x in readings]
    if not vals:
        raise ValueError('读数不能为空')
    return {
        'sample_count': len(vals),
        'mean': round(statistics.fmean(vals), 4),
        'std': round(statistics.pstdev(vals), 4) if len(vals) > 1 else 0.0,
        'min': round(min(vals), 4),
        'max': round(max(vals), 4),
    }


def judge(version_code, high_moisture_threshold, readings):
    """按指定参考版本对一组读数做初检判定。"""
    stats = summarize(readings)
    threshold = float(high_moisture_threshold)
    # 均值或任一测点越线都视为高湿：既看整体水平，也不放过局部受潮。
    is_high = stats['mean'] >= threshold or stats['max'] >= threshold
    verdict = 'HIGH' if is_high else 'NORMAL'
    out = {
        'seed': SEED,
        'version_code': version_code,
        'high_moisture_threshold': round(threshold, 2),
        **stats,
        'verdict': verdict,
    }
    out['checksum'] = hashlib.sha256(
        json.dumps(out, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()[:16]
    return out


def evaluate_gate(baseline_verdicts, candidate_verdicts, manual_verdicts):
    """发布闸门：候选少误报且不新增漏报才可发布。

    误报(false positive)：判 HIGH 但人工判 NORMAL。
    漏报(false negative)：判 NORMAL 但人工判 HIGH。
    入参为对齐的 dict，key 为留样标识（如 retained_id 或 lot_code）。
    """
    keys = sorted(set(manual_verdicts))
    if set(baseline_verdicts) != set(keys) or set(candidate_verdicts) != set(keys):
        raise ValueError('两套基准与人工判定必须覆盖同一组留样')

    def confusion(verdicts):
        fp = fn = tp = tn = 0
        mismatches = []
        for k in keys:
            m, v = manual_verdicts[k], verdicts[k]
            if v == 'HIGH' and m == 'NORMAL':
                fp += 1
                mismatches.append({'key': k, 'kind': 'FALSE_POSITIVE', 'manual': m, 'verdict': v})
            elif v == 'NORMAL' and m == 'HIGH':
                fn += 1
                mismatches.append({'key': k, 'kind': 'FALSE_NEGATIVE', 'manual': m, 'verdict': v})
            elif v == 'HIGH' and m == 'HIGH':
                tp += 1
            else:
                tn += 1
        total = len(keys)
        return {
            'false_positive': fp,
            'false_negative': fn,
            'true_positive': tp,
            'true_negative': tn,
            'total': total,
            'false_positive_rate': round(fp / total, 4) if total else 0.0,
            'false_negative_rate': round(fn / total, 4) if total else 0.0,
            'mismatches': mismatches,
        }

    base, cand = confusion(baseline_verdicts), confusion(candidate_verdicts)
    fewer_fp = cand['false_positive'] < base['false_positive']
    no_new_fn = cand['false_negative'] <= base['false_negative']
    passed = fewer_fp and no_new_fn
    if passed:
        reason = (
            f"候选误报 {cand['false_positive']} < 基准 {base['false_positive']}，"
            f"且漏报 {cand['false_negative']} ≤ 基准 {base['false_negative']}，满足发布条件"
        )
    elif not fewer_fp:
        reason = (
            f"候选误报 {cand['false_positive']} 未少于基准 {base['false_positive']}，"
            '不满足“少误报”条件'
        )
    else:
        reason = (
            f"候选新增漏报：候选漏报 {cand['false_negative']} > 基准 {base['false_negative']}，"
            '不满足“不新增漏报”条件'
        )
    return {
        'passed': passed,
        'reason': reason,
        'baseline': base,
        'candidate': cand,
        'rules': {'require_fewer_false_positive': True, 'require_no_new_false_negative': True},
    }
