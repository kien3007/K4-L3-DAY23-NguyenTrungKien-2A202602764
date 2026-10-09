"""BƯỚC 3c — SINH VIÊN VIẾT. Tự động hoá runbook §4 "Runbook: Region Chính Down".

7 bước trên slide, mỗi bước 1 dòng log có ts. Log này CHÍNH LÀ timeline của postmortem.
  1 xac_nhan_outage          — probe cả 2 region, đừng tin 1 lần fail (dùng nhiều lần
                              hoặc gọi health_checker.probe nếu đã viết xong 3a)
  2 thong_bao_incident       — ts của dòng này là mốc "operator biết tin", LUÔN LUÔN
                              SAU t_outage trong chaos-events (không thể trùng — operator
                              không thể biết ngay giây outage xảy ra). Ghi cả 2 ts vào
                              log để postmortem tính được "độ trễ thông báo".
  3 scale_gpu_pool           — gọi HÀM `failover.failover(...)` MỘT LẦN DUY NHẤT. Hàm
                              đó tự làm đủ 5 bước con (verify/restore/scale/wait/cutover)
                              và tự ghi log riêng vào reports/failover-events.jsonl.
  4 verify_state_replica     — KHÔNG gọi lại failover — chỉ ĐỌC kết quả (vector count +
                              weights ở region phụ) từ dict mà bước 3 trả về, để log vào
                              runbook-run.jsonl cho postmortem đọc 1 chỗ duy nhất.
  5 dns_cutover              — cũng chỉ đọc lại: kết quả cutover có ok hay không.
  6 verify_golden_signals    — 10 request thật vào region phụ: p95 latency + error rate
  7 post_incident            — elapsed_s + lệnh đo RTO

BÁN TỰ ĐỘNG, KHÔNG FULL-AUTO (§4: "failover đầu tiên nên là bán tự động — alert +
1-click confirm — tránh flapping gây failover 2 chiều liên tục"). Mặc định phải hỏi
người vận hành confirm; --auto chỉ dùng trong CI/khi chấm điểm.

Chạy:  python dr/runbook.py --primary a --target b --backend fs
"""
import argparse
import json
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from dr import failover as fo  # noqa: E402

LOG = pathlib.Path("reports/runbook-run.jsonl")
URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}


from dr import health_checker as hc  # noqa: E402


def step(n: int, name: str, **kw):
    """Ghi 1 dòng {ts, iso, step, name, ...} vào LOG và in ra stdout."""
    LOG.parent.mkdir(parents=True, exist_ok=True)
    rec = {
        "ts": time.time(),
        "iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "step": n,
        "name": name,
        **kw,
    }
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")
    print(f"RUNBOOK [Step {n}: {name}]", json.dumps(rec))
    return rec


def confirm(auto: bool, msg: str) -> bool:
    """auto=True -> True; ngược lại hỏi y/N."""
    if auto:
        return True
    try:
        ans = input(f"{msg} [y/N]: ").strip().lower()
        return ans in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        return False


def run(primary: str, target: str, backend: str, auto: bool) -> dict:
    """7 bước runbook theo §4."""
    t_start = time.time()

    # Bước 1: xac_nhan_outage — chờ và xác nhận alert từ health checker và probe cả 2 region
    # §4: Operator nhận alert từ health check trước khi kích hoạt failover (chống flapping)
    t_wait_hc = time.time()
    hc_detected = False
    while time.time() - t_wait_hc < 25.0:
        if pathlib.Path("reports/health-events.jsonl").exists():
            try:
                hev = [json.loads(l) for l in pathlib.Path("reports/health-events.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
                if any(e.get("event") == "state_change" and e.get("to") == "UNHEALTHY" and e.get("region") == primary for e in hev):
                    hc_detected = True
                    break
            except Exception:
                pass
        time.sleep(0.5)

    p_probes = [hc.probe(primary, timeout=1.5) for _ in range(3)]
    prim_down = all(not ok for ok, _ in p_probes)
    tgt_ok, tgt_reason = hc.probe(target, timeout=1.5)
    step(1, "xac_nhan_outage", primary=primary, primary_down=prim_down,
         health_checker_alerted=hc_detected,
         target=target, target_ready=tgt_ok, target_reason=tgt_reason)

    # Bước 2: thong_bao_incident — ts operator biết tin, so với t_outage
    t_outage = None
    chaos_f = pathlib.Path("chaos/chaos-events.jsonl")
    if chaos_f.exists():
        try:
            kills = [json.loads(l) for l in chaos_f.read_text(encoding="utf-8").splitlines() if l.strip()]
            kills = [e for e in kills if e.get("action") == "kill"]
            if kills:
                t_outage = kills[-1].get("ts")
        except Exception:
            pass

    ts_notice = time.time()
    lag_s = round(ts_notice - t_outage, 2) if t_outage else None
    confirmed = confirm(auto, f"Region {primary} gặp sự cố. Kích hoạt chuyển vùng sang Region {target}?")
    step(2, "thong_bao_incident", primary=primary, target=target,
         t_outage=t_outage, ts_operator_notice=ts_notice,
         notification_lag_s=lag_s, confirmed=confirmed)
    if not confirmed:
        return {"ok": False, "step": 2, "aborted": True, "reason": "operator_declined"}

    # Bước 3: scale_gpu_pool — gọi failover.failover(...) DUY NHẤT 1 LẦN
    fo_res = fo.failover(target=target, backend=backend, wait=60.0)
    step(3, "scale_gpu_pool", target=target, backend=backend, failover_ok=fo_res.get("ok"))
    if not fo_res.get("ok"):
        return {"ok": False, "step": 3, "failover_res": fo_res}

    # Bước 4: verify_state_replica — đọc kết quả replicate từ bước 3
    tgt_state = fo_res.get("target_state", {})
    rpo_s = fo_res.get("rpo_seconds")
    docs_lost = fo_res.get("docs_lost")
    step(4, "verify_state_replica", target=target, target_state=tgt_state,
         rpo_seconds=rpo_s, docs_lost=docs_lost,
         embed_model_version=fo_res.get("embed_model_version"))

    # Bước 5: dns_cutover — đọc lại active_region
    active_path = pathlib.Path("edge/active_region")
    active_reg = active_path.read_text().strip() if active_path.exists() else None
    cutover_ok = (active_reg == target)
    step(5, "dns_cutover", active_region=active_reg, target=target, cutover_ok=cutover_ok)

    # Đợi edge proxy hết TTL cache (5s) và chuyển hẳn sang target
    t_wait_edge = time.time()
    while time.time() - t_wait_edge < 10.0:
        try:
            r = httpx.get("http://127.0.0.1:8080/edge/state", timeout=1.0)
            if r.status_code == 200 and r.json().get("active_region") == target:
                break
        except Exception:
            pass
        time.sleep(0.5)

    # Bước 6: verify_golden_signals — 10 request thật
    latencies = []
    errors = 0
    for _ in range(10):
        t_req = time.time()
        try:
            r = httpx.get("http://127.0.0.1:8080/v1/infer", timeout=3.0)
            lat = round((time.time() - t_req) * 1000, 1)
            latencies.append(lat)
            if r.status_code != 200:
                errors += 1
        except Exception:
            errors += 1
            latencies.append(round((time.time() - t_req) * 1000, 1))
        time.sleep(0.05)

    latencies.sort()
    p95 = latencies[int(0.95 * len(latencies))] if latencies else None
    err_rate = round(errors / 10, 2)
    step(6, "verify_golden_signals", requests=10, errors=errors, error_rate=err_rate, p95_ms=p95)

    # Bước 7: post_incident — elapsed_s + lệnh đo RTO
    elapsed_s = round(time.time() - t_start, 2)
    measure_cmd = "python3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300"
    step(7, "post_incident", elapsed_s=elapsed_s, measure_cmd=measure_cmd)

    return {
        "ok": True,
        "primary": primary,
        "target": target,
        "rpo_seconds": rpo_s,
        "docs_lost": docs_lost,
        "elapsed_s": elapsed_s,
        "golden_signals": {"p95_ms": p95, "error_rate": err_rate},
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--primary", default="a")
    p.add_argument("--target", default="b")
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--auto", action="store_true")
    a = p.parse_args()
    print(json.dumps(run(a.primary, a.target, a.backend, a.auto), indent=2))
