# Runbook 1 trang — Region chính down

Runbook xử lý sự cố khẩn cấp dành cho kỹ sư trực ca (On-Call Engineer) lúc 3h sáng.
Mỗi bước đều có lệnh thực thi trực tiếp dạng copy-paste, tín hiệu xác nhận hoàn thành và phân công vai trò rõ ràng.

| # | Bước | Lệnh (Copy-paste) | Biết là xong khi | Ai làm |
|---|---|---|---|---|
| 1 | Xác nhận outage | `python chaos/kill_region.py status` hoặc `curl -s http://127.0.0.1:8001/readyz` | Region A phản hồi lỗi (ReadTimeout / 503) ≥ 3 lần liên tiếp; Region B phản hồi `/healthz` HTTP 200 OK | On-call SRE |
| 2 | Mở incident + bấm giờ RTO | `python dr/runbook.py --primary a --target b --backend fs --auto` | Thông báo incident được tạo; mốc thời gian `ts_operator_notice` được ghi vào `reports/runbook-run.jsonl` | Incident Commander (IC) / On-call SRE |
| 3 | Restore state ở region phụ | `python state/snapshot.py get --region b --backend fs` | Output JSON có `restored_at`; kiểm tra file database và weights tồn tại ở `state/region-b/` | On-call SRE / Automation script |
| 4 | Scale pool warm→full | `python -c "import pathlib; pathlib.Path('state/region-b/pool_state').write_text('full')"` và kiểm tra `curl -s http://127.0.0.1:8002/readyz` | Endpoint `/readyz` của Region B trả về HTTP 200 OK (`ready: true`, không còn `warming_up`) | On-call SRE / Automation script |
| 5 | DNS/LB cutover | `python -c "import pathlib; pathlib.Path('edge/active_region').write_text('b')"` | Kiểm tra `curl -s http://127.0.0.1:8080/edge/state` trả về `active_region: "b"` | On-call SRE / Automation script |
| 6 | Verify golden signals | `python -c "import httpx; [print(httpx.get('http://127.0.0.1:8080/v1/infer').status_code) for _ in range(10)]"` | 10/10 request trả về HTTP 200 OK với trường `region: "b"`, p95 latency < 200ms, error rate = 0% | On-call SRE |
| 7 | Đo RTO + postmortem | `python tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300` | Output trả về `valid: true` và `rto_verdict: "PASS"`; số liệu được lưu vào `reports/measure-drill-2.json` | Lead SRE & Incident Commander |

---

## Quy trình Rollback (Failover ngược về Region A)

> **CẢNH BÁO QUAN TRỌNG:** Tuyệt đối không thiết lập chế độ tự động rollback hoàn toàn (full-auto rollback). Nếu không có cơ chế ngắt mạch (circuit breaker), khi Region A vừa chập chờn khởi động lại, lưu lượng truy cập đổ dồn về có thể khiến nó sập tiếp, gây ra hiện tượng dao động qua lại liên tục (flapping) giữa 2 vùng, dẫn đến suy giảm nghiêm trọng chất lượng dịch vụ.

### 1. Điều kiện cần và đủ để Rollback:
1. **Region A đã phục hồi hoàn toàn:** Kiểm tra liveness và readiness của Region A liên tục ổn định trong ít nhất 30 phút (`curl http://127.0.0.1:8001/readyz` luôn trả về 200 OK).
2. **Đồng bộ ngược dữ liệu (Reverse Data Sync):** Toàn bộ dữ liệu phát sinh mới tại Region B trong thời gian sự cố phải được sao lưu và replicate ngược về Region A (`state/region-a/vectors.sqlite`), đảm bảo không thất thoát dữ liệu delta và không có xung đột dữ liệu.
3. **Warmup Region A:** GPU pool và cache tại Region A phải được đưa lên trạng thái `full` và sẵn sàng tải trước khi mở cổng traffic.
4. **Cửa sổ bảo trì an toàn:** Thực hiện chuyển mạch DNS vào khung giờ thấp điểm (low-traffic maintenance window) để giảm thiểu rủi ro cho người dùng.

### 2. Thẩm quyền quyết định:
- Quyết định rollback bắt buộc phải có sự phê duyệt chính thức bằng văn bản/tin nhắn từ **Incident Commander (IC)** hoặc **Head of Infrastructure / VP of Engineering**.
- Kỹ sư on-call không được tự ý thực hiện rollback khi chưa có sự xác nhận của chỉ huy sự cố.
