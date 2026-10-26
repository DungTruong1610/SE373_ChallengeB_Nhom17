# Số liệu đo: Challenge B

- **Model:** DeepSeek qua OpenAI-compatible API (`MODEL_NAME` và `OPENAI_BASE_URL` trong `.env`, không commit). Ngày chạy 2026-10-26.
- **Nguồn số liệu:** `usage` do provider trả về, chụp ở từng model call (`input_tokens`, `output_tokens`, `input_token_details.cache_read`). Token của event compaction chỉ là ước lượng của harness (`count_tokens_approximately`) và chỉ dùng để quyết định khi nào compact.
- **Dữ liệu thô:** `submission/runs/<label>.json` (tổng hợp) và `submission/traces/<label>__turnNN.jsonl` (trace đầy đủ từng lượt).
- **Đáp án đúng:** `python stage-05-context/scripts/gen_logs.py --check`. Hàm này đếm trực tiếp từ 10 file log: 1.575 dòng, 313 dòng ERROR.
- **Chi phí:** tính theo bảng giá giả định "Model A" ở slide 48 (input 5,00, cached input 0,50, output 20,00 USD / 1M token), chỉ để so sánh tương đối. Đây không phải giá DeepSeek.

## B2: Code Mode và native tool calling

Câu hỏi giống nhau cho cả hai lần chạy, mỗi lần là một conversation mới, `COMPACTION=0`:

> Thư mục data/logs/ chứa log hệ thống. Hãy cho biết: (1) top 3 mã lỗi ở các dòng ERROR xuất hiện nhiều nhất, kèm số lần; (2) top 3 endpoint trả status=500 nhiều nhất, kèm số lần. Nêu rõ đã quét bao nhiêu file.

| | Native (`CODE_MODE=0`) | Code Mode (`CODE_MODE=1`) | Tỉ lệ |
|---|---:|---:|---:|
| Model call | 4 | 5 | |
| Tool call mà model sinh ra | 11 (`list_files` ×1, `read_file` ×10) | 4 (`list_files` ×1, `run_python` ×3) | |
| Lời gọi `tools.*` bên trong runtime | 0 | 23 (1 + 11 + 11) | |
| **Tổng input tokens** | **110.910** | **18.073** | **6,1× ít hơn** |
| Input không cache (input − cache_read) | 70.590 | 3.865 | 18,3× |
| **Context lớn nhất (input của 1 call)** | **70.747** | **5.182** | **13,7×** |
| Output tokens | 9.415 | 1.658 | 5,7× |
| Byte dữ liệu tool runtime đã nhận (không vào context) | 0 | 17.835 + 184.978 + 184.978 | |
| Ký tự stdout vào context | (toàn bộ 10 file, ~185 KB) | 951 + 852 + 572 | |
| Chi phí minh họa (slide 48, Model A) | $0,561 | $0,060 | 9,4× |
| Đúng đáp án (top 3 mã lỗi, top 3 endpoint 500) | Đúng | Đúng | |

Input theo từng model call:
- **Native:** 1.601 → 2.049 → 36.513 → 70.747. Context tăng theo mỗi đợt `read_file`; call cuối mang cả 10 file log.
- **Code Mode:** 2.280 → 2.728 → 3.399 → 4.484 → 5.182. Context chỉ tăng thêm khoảng vài trăm token mỗi lượt.

Đáp án đúng: `ERR_DB_TIMEOUT` 98, `ERR_TOKEN_REFRESH_RACE` 74, `ERR_UPSTREAM_502` 52; `/api/auth/refresh` 74, `/api/orders` 70, `/api/checkout` 28.

## B1: Compaction trong một session dài

Cùng 5 lượt chat (`submission/b1_turns.txt`), `CODE_MODE=0` để raw log đi vào context (tạo context bloat thật), ngưỡng `COMPACT_TRIGGER_TOKENS=12000`:

1. Đặt mục tiêu `ERR_TOKEN_REFRESH_RACE` + ràng buộc "giữ API backward compatible", đọc log 11/10.
2. "Đã thử tăng TTL 900s → 1800s ngày 15/10 nhưng không giúp", đọc log 15/10.
3. Đọc log 20/10, so sánh.
4. **Kiểm tra trí nhớ:** "đang điều tra lỗi gì, ràng buộc là gì, đã thử cách nào và kết quả ra sao, bước tiếp theo là gì?"
5. **Kiểm chứng checkpoint (slide 22):** "Làm bước tiếp theo đó đi."

Hai run tách nhau từ lượt 5: agent chọn bước tiếp theo khác nhau. Run có compaction đọc 6 log còn lại; run không compaction đọc `src/*.ts`. Vì vậy bảng so sánh chính chỉ dùng **lượt 1–4**, là phần hai run làm cùng việc.

| Lượt 1–4 | Không compaction | Có compaction |
|---|---:|---:|
| Model call của agent | 7 | 7 |
| Số lần compact | 0 | 2 (lượt 2 giữa vòng tool, lượt 3 đầu lượt) |
| **Tổng input tokens của agent** | **105.693** | **68.575** (−35%) |
| **Input của call ở lượt 4** (câu hỏi kiểm tra trí nhớ) | **26.446** | **11.222** (−58%) |
| Input lớn nhất | 26.446 | 23.324 |
| Cache read | 66.176 | 20.864 |
| **Input không cache** | **39.517** | **47.711** (+21%) |
| Output tokens của agent | 9.388 | 5.084 |
| Lời gọi summarizer (input / cached / output) | 0 | 8.223 / 768 / 3.859 |
| Chi phí minh họa khi cache còn ấm (có cache_read như thực tế) | $0,418 | $0,466 (agent $0,351 + summarizer $0,115) |
| Chi phí minh họa nếu cache nguội (không có cache hit) | $0,716 | $0,563 |
| Lượt 4 nêu đúng lỗi, ràng buộc, cách đã thử và kết quả, bước tiếp | Đúng | Đúng |

Input theo từng call (lượt.call: input / cache_read):
- **Không compaction:** 1.1 1.602/1.408 · 1.2 8.397/1.664 · 2.1 9.563/8.192 · 2.2 16.474/9.600 · 3.1 17.958/9.472 · 3.2 25.253/17.920 · 4.1 26.446/17.920 · 5.1 27.318 · 5.2 27.594 · 5.3 28.143 · 5.4 28.782. Tăng đều, chưa bao giờ giảm.
- **Có compaction:** 1.1 1.602 · 1.2 8.899 · 2.1 9.903 · **[compact #1]** 2.2 23.324/1.280 · **[compact #2]** 3.1 **3.162**/1.280 · 3.2 10.463 · 4.1 11.222/10.368 · 5.1 11.796 · **[compact #3]** 5.2 44.351/1.280 · **[compact #4]** 5.3 **7.089**/1.280 · 5.4 14.269.

Các lần compact (token ước lượng của message history, không gồm system prompt và tool schema):

| # | Lượt | Lúc | History trước → sau | Messages | Pre-pass (kept / truncated / dropped) | Summarizer in / out |
|---|---|---|---|---|---|---|
| 1 | 2 | giữa lượt, sau khi đọc 3 log | ~19.488 → ~14.699 | 10 → 6 | 3 / 1 / 0 | 3.140 / 1.934 |
| 2 | 3 | đầu lượt | ~15.296 → ~986 | 8 → 2 | 3 / 3 / 0 | 5.083 / 1.925 |
| 3 | 5 | giữa lượt, sau khi đọc 6 log | ~34.569 → ~28.904 | 15 → 9 | 5 / 1 / 0 | 4.583 / 2.245 |
| 4 | 5 | sau `write_file` | ~29.923 → ~2.133 | 11 → 4 | 1 / 6 / 0 | 6.750 / 2.071 |

Compact #1 và #3 giảm ít vì vòng tool mới nhất (model chưa xem) được giữ nguyên theo thiết kế. Lần compact ngay sau đó (#2, #4) mới đưa history về khoảng 1–2k token.

Kết quả cuối lượt 5 của run có compaction: số `ERR_TOKEN_REFRESH_RACE` theo ngày là 5, 7, 9, 8, 6, 6, 6, 4, 10, 13. Kết quả này **khớp đúng** `refresh_race_by_day` của ground truth.

## Lần chạy đầu của B1: summarizer bị provider từ chối

`b1_v1_toolchoice_failed`: phiên bản đầu dùng `with_structured_output(method="function_calling")`, nên request gửi `tool_choice` bắt buộc. DeepSeek ở thinking mode trả lỗi 400 `Thinking mode does not support this tool_choice`. Harness phát event `compaction_failed` 3 lần, **không compact**, history giữ nguyên và agent vẫn trả lời đúng. Context tăng tới 33.302 token.

Bản hiện tại chuyển sang cách khác: đưa JSON Schema vào prompt, rồi harness tự validate bằng Pydantic.
