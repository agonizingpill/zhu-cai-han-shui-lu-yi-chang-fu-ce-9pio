"""[已弃用] 早期 OneClassSVM 判定实现，保留仅作历史参考。

当前判定口径统一以 moisture_engine.judge 为准（版本化高湿阈值 + checksum），
worker 与 backend 均不再引用本模块，请勿在新流程中使用。
"""
import hashlib,json,numpy as np
from sklearn.preprocessing import StandardScaler
from sklearn.svm import OneClassSVM
SEED=1075
def vector(vals):
    a=np.array(vals,dtype=float); return [float(a.mean()),float(a.std()),float(a.min()),float(a.max())]
def detect(refs,vals):
    if len(refs)<3: raise ValueError('参考批次不足，至少需要 3 个正常批次')
    scaler=StandardScaler(); rs=scaler.fit_transform(np.array(refs,float)); x=scaler.transform(np.array(vector(vals)).reshape(1,-1))
    model=OneClassSVM(kernel='rbf',gamma='scale',nu=.2); model.fit(rs)
    pred=int(model.predict(x)[0]); score=float(model.decision_function(x)[0]); dist=float(np.linalg.norm(x[0])); abnormal=pred==-1 or dist>=2.25
    out={'seed':SEED,'reference_count':len(refs),'vector':vector(vals),'prediction':pred,'decision_score':round(score,6),'standardized_distance':round(dist,6),'is_anomaly':abnormal}
    out['checksum']=hashlib.sha256(json.dumps(out,sort_keys=True).encode()).hexdigest()[:16]; return out
