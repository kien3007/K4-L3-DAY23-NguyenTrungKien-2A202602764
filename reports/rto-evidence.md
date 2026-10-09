# RTO/RPO Evidence — Lab 23

Quy tắc duy nhất: mỗi con số ở đây phải trỏ được về **một dòng log thật**
(`đường/dẫn.jsonl:số_dòng`). `pytest tests/test_rto_evidence.py` sẽ mở từng file ra kiểm tra.
Con số không có evidence = trượt, bất kể các phần khác.

## 1. Drill 1 — không có DR (baseline)

| Chỉ số | Giá trị | Cách đo | Evidence |
|---|---|---|---|
| t_outage | `2026-10-09T05:36:56` | chaos kill | `chaos/chaos-events.jsonl:10` |
| Request fail đầu tiên | `+0.2s` | dòng `ok:false` đầu tiên sau t_outage | `reports/drill-1-nodr.jsonl:17` |
| Request thành công sau đó | không có | không có dòng `ok:true` nào sau t_outage | `reports/drill-1-nodr.jsonl:32` |
| RTO | `NO_RECOVERY` | `tools/measure_rto.py` | `tools/measure_rto.py` |

## 2. Drill 2 — có DR

| Mốc | +giây từ t_outage | Cách đo | Evidence |
|---|---|---|---|
| t_outage (mốc 0) | 0s | `action:kill` | `chaos/chaos-events.jsonl:13` |
| User thấy lỗi đầu tiên | +0.3s | dòng `ok:false` đầu | `reports/drill-2-withdr.jsonl:25` |
| Health check phát hiện | +15.0s | `to:UNHEALTHY, region:a` | `reports/health-events.jsonl:2` |
| Snapshot restore xong | +19.9s | `step:2_restore_snapshot` | `reports/failover-events.jsonl:2` |
| Region phụ ready | +26.5s | `step:4_wait_ready` | `reports/failover-events.jsonl:4` |
| DNS cutover | +26.5s | `step:5_dns_cutover` | `reports/failover-events.jsonl:5` |
| **RTO đo được** | +28.9s | dòng `ok:true` đầu sau lỗi | `reports/drill-2-withdr.jsonl:39` |

| Chỉ số | Đo được | Mục tiêu (slide §1) | Verdict |
|---|---|---|---|
| RTO — Inference API | `28.9s` | 300s (5 phút) | PASS |
| RPO — Vector DB | `8.05s` / 4 docs | 300s (5 phút) | PASS |

## 3. RTO của tôi gồm những gì (bắt buộc — đây là phần chấm điểm hiểu bài)

| Thành phần | Giây | Nó đến từ đâu | Giảm được bằng cách nào |
|---|---|---|---|
| Health-check detect floor | 15.0s | `interval_s × threshold` trong `reports/health-events.jsonl:2` | Giảm interval từ 5s xuống 2s hoặc threshold từ 3 xuống 2 (đổi lại tăng nguy cơ flapping khi mạng chập chờn) |
| Snapshot restore | 4.9s | `step:2_restore_snapshot` trong `reports/failover-events.jsonl:2` | Tăng tần suất replication hoặc dùng streaming replication thay vì snapshot chu kỳ |
| GPU pool warm-up | 6.6s | `waited_s` ở 4_wait_ready trong `reports/failover-events.jsonl:4` | Duy trì sẵn warm pool hoặc tối ưu hóa model loading và warmup scripts |
| DNS/LB TTL cache | 2.4s | t_recovered − t_cutover (`reports/drill-2-withdr.jsonl:39` trừ `reports/failover-events.jsonl:5`) | Giảm DNS/LB TTL từ 5s xuống 1s-2s (chấp nhận tăng số lượt query DNS lên edge) |
