# Postmortem — DR Drill Lab 23

Theo đúng template §4 "Sau Failover: Blameless Postmortem". Blameless: câu hỏi là
"hệ thống/process nào cho phép chuyện này", không phải "ai làm sai".

## 1. Timeline (mọi dòng phải có evidence path:line)

| ISO time | Sự kiện | Evidence |
|---|---|---|
| `2026-10-09T05:40:34` | Region A gặp sự cố mạng netblock (outage bắt đầu) | `chaos/chaos-events.jsonl:13` |
| `2026-10-09T05:40:34.947Z` | User đầu tiên bị ảnh hưởng (HTTP 503 ReadTimeout) | `reports/drill-2-withdr.jsonl:25` |
| `2026-10-09T05:40:49Z` | Health check phát hiện 3 lần fail liên tiếp, đổi trạng thái sang UNHEALTHY | `reports/health-events.jsonl:2` |
| `2026-10-09T05:40:54Z` | Operator nhận cảnh báo và xác nhận kích hoạt chuyển vùng | `reports/runbook-run.jsonl:2` |
| `2026-10-09T05:40:54Z` | Snapshot restore hoàn tất ở Region B (RPO 8.05s, 4 docs lost) | `reports/failover-events.jsonl:2` |
| `2026-10-09T05:41:01Z` | Region B hoàn tất warmup GPU pool và sẵn sàng phục vụ (/readyz trả về 200) | `reports/failover-events.jsonl:4` |
| `2026-10-09T05:41:01Z` | DNS cutover hoàn tất, trỏ active_region sang 'b' | `reports/failover-events.jsonl:5` |
| `2026-10-09T05:41:03.539Z` | Resolved — Request đầu tiên thành công từ Region B sau outage (+28.9s) | `reports/drill-2-withdr.jsonl:39` |

## 2. RTO/RPO đo được vs mục tiêu — gap ở bước nào?

- RTO mục tiêu: 300s · đo được: `28.9s` · gap: `271.1s` (nhanh hơn mục tiêu 271.1s, đạt SLA)
- RPO mục tiêu: 300s · đo được: `8.05s` (`4` doc bị mất) · gap: `291.95s` (nhanh hơn mục tiêu 291.95s, đạt SLA)
- **Bước tốn nhiều giây nhất:** `Health check detection floor` (15.0s, chiếm 51.9% tổng RTO). Nguyên nhân là do cơ chế chống flapping yêu cầu 3 lần probe thất bại liên tiếp với chu kỳ 5s (`interval_s=5.0 × threshold=3`). Bước tiếp theo là GPU pool warm-up (6.56s, chiếm 22.7% RTO) do thời gian khởi tạo tài nguyên phục vụ suy luận.

## 3. Root cause (5 whys)

1. **Why 1:** Tại sao người dùng gặp lỗi 503 trong 28.9s?
   *Trả lời:* Vì Region A bị cô lập mạng (netblock) và hệ thống mất thời gian phát hiện sự cố, khôi phục dữ liệu ở Region B, làm ấm GPU pool và cập nhật DNS.
2. **Why 2:** Tại sao mất 15s hệ thống mới nhận biết được Region A gặp sự cố?
   *Trả lời:* Vì health checker cấu hình chu kỳ thăm dò 5s và ngưỡng 3 lần lỗi liên tiếp nhằm loại bỏ báo động giả (flapping) khi mạng chập chờn tạm thời.
3. **Why 3:** Tại sao Region B không thể nhận traffic ngay tức thì?
   *Trả lời:* Vì Region B duy trì ở trạng thái warm standby để tiết kiệm chi phí hạ tầng; khi kích hoạt cần thời gian nạp mô hình/vector DB và làm ấm GPU pool (mất ~6.6s).
4. **Why 4:** Tại sao có 4 documents bị mất (RPO 8.05s)?
   *Trả lời:* Vì tiến trình replicate sao chép dữ liệu định kỳ mỗi 30s (`every=30s`) lên kho lưu trữ snapshot thay vì đồng bộ thời gian thực theo từng transaction.
5. **Why 5:** Nếu đây là sự cố thật trong thực tế, bước nào trong runbook có nguy cơ thất bại cao nhất?
   *Trả lời:* Bước `2_restore_snapshot` và `4_wait_ready`. Nếu phiên bản embedding model ở bản snapshot không tương thích với phiên bản đang phục vụ hoặc Region B thiếu quota/vRAM GPU, Region B sẽ không thể vượt qua bài kiểm tra `/readyz`, dẫn đến toàn bộ quy trình chuyển vùng bị hủy (abort) để tránh lỗi 503 kép.

## 4. Action items (có owner + deadline)

| # | Action | Owner | Deadline | Giảm RTO/RPO bao nhiêu giây |
|---|---|---|---|---|
| 1 | Tinh chỉnh health check xuống interval 3s, threshold 3 để giảm detection floor từ 15s xuống 9s | SRE Team | 2026-10-25 | Giảm 6.0s RTO |
| 2 | Nâng cấp cơ chế sao lưu sang Continuous Streaming Replication (WAL/CDC) thay vì batch snapshot 30s | Data Platform Team | 2026-11-10 | Giảm RPO từ 8.05s xuống < 1.0s (0 docs lost) |
| 3 | Tối ưu hóa pipeline nạp model weights và pre-warm cache tại Region B | AI Serving Team | 2026-11-20 | Giảm 3.0s RTO |
| 4 | Giảm TTL tại Edge proxy/DNS từ 5s xuống 2s để tăng tốc độ hội tụ định tuyến | Network Team | 2026-10-30 | Giảm 2.0s RTO |

## 5. Ba câu hỏi bắt buộc trả lời

1. **`interval × threshold` của bạn là bao nhiêu giây? Nó chiếm bao nhiêu % RTO?**
   - Giá trị: `5.0s × 3 = 15.0s`.
   - Tỷ lệ: Với RTO thực tế đo được là `28.9s`, bước này chiếm **51.9%** tổng thời gian gián đoạn dịch vụ. Đây là phần cố định không thể tránh khỏi trong kiến trúc pull-based health check.

2. **Nếu hạ interval xuống 1s, RTO giảm mấy giây — và bạn trả giá gì (§4 flapping)?**
   - RTO giảm: Nếu giảm interval xuống 1s với threshold 3, detection floor giảm từ 15s xuống 3s, giúp rút ngắn RTO đi **12.0s**.
   - Cái giá phải trả (Flapping): Nguy cơ chuyển vùng nhầm (false-positive failover) tăng vọt. Chỉ cần một đợt nghẽn mạng ngắn hạn kéo dài 2-3s (như router khởi động lại, packet drop thoáng qua), hệ thống sẽ nhầm là sập region và kích hoạt chuyển vùng. Việc đổi vùng liên tục giữa 2 region sẽ làm đứt kết nối người dùng, lãng phí chi phí scale GPU pool và gây quá tải hệ thống.

3. **Nếu outage kéo dài 6 giờ và region chính mất dữ liệu vĩnh viễn, `docs_lost` của bạn có nghĩa gì với khách hàng?**
   - Ý nghĩa: `docs_lost = 4 docs` biểu thị 4 tài liệu/giao dịch mà khách hàng đã gửi lên trong khoảng thời gian 8.05s trước khi Region A sụp đổ, dù khách hàng có thể đã nhận được HTTP 200 tại thời điểm đó nhưng dữ liệu chưa kịp đưa vào snapshot lưu trữ.
   - Hệ quả: Các tài liệu này bị mất vĩnh viễn nếu Region A không thể phục hồi ổ đĩa. Khách hàng sẽ bị thiếu hóa đơn, bản ghi hoặc tài liệu tri thức khi truy vấn tìm kiếm.
   - Hướng xử lý: Cần đối soát từ access log/message queue ở tầng Gateway để replay lại 4 bản ghi này vào Region B, đồng thời gửi thông báo tới người dùng liên quan để đối soát nếu có sai lệch.
