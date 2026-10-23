# Handover: Baseline B (Hương) -> Linh, Châu, Hiền

Trạng thái: **discovery + download + điều phối đã chạy được và đã kiểm tra**.
Baseline chỉ chạy "từ đầu đến cuối" thật sự khi extract, references, storage, metrics có hàm theo
đúng hợp đồng bên dưới. Hiện các file đó rỗng và được ghi `skipped`.

## 1. Pipeline hoạt động thế nào

`python -m src.main [config.yaml]` xử lý **từng bài một**:

    discover_paper -> download_paper (mọi phiên bản) -> extract_paper -> references_paper -> store_paper
                                                                         (3 hàm cuối là hook của các bạn)
    -> ghi 1 sự kiện "paper" (status + tổng giây của bài)

- Một bài lỗi chỉ được ghi log, run vẫn đi tiếp bài sau.
- Một Throttle dùng chung cho arXiv (>= 3 giây giữa hai request).
- Resume: đặt `run_id` cố định trong config rồi chạy lại. Phần đã `ok` được bỏ qua, phần `failed` được thử lại.
- `run.json` ghi `started_at`, `finished_at`, `run_status` (running/completed/interrupted/crashed),
  `cache_mode` (cold = run mới, warm = resume), `input_ids`, `environment`.

## 2. Dữ liệu một run (thư mục `experiments/<run_id>/`, không commit)

    run.json
    events.jsonl
    papers/<paper_key>/metadata.json                 # discovery: title, authors, dates, versions, status
    papers/<paper_key>/versions/v<N>/manifest.json   # từng phiên bản (schema 1.B trong docx)
    tmp/<paper_key>/v<N>/source.archive              # gói tải về, tạm thời

`paper_key` = ID với `.` thành `-` và `/` thành `__` (vd `2507-01659`). ID gốc luôn nằm trong dữ liệu.
Trạng thái dùng bộ giá trị trong `src/common.py`: pending, running, ok, empty, failed, skipped.
Mã lỗi nằm trong `src/utils/errors.py` (TIMEOUT, HTTP_429, NOT_FOUND, CORRUPT_SOURCE, UNSUPPORTED_FORMAT...) và mọi
module dùng chung một lớp lỗi `CrawlError(message, error_code, status_code=None)`; trong `events.jsonl`, giá trị này được ghi ở cột `http_status`.
Giá trị chưa biết dùng `null`, không dùng 0.

Một dòng `events.jsonl`:
`ts, run_id, arxiv_id, version, stage, status, error_code, http_status, attempts, message, elapsed_seconds`.
`stage` hiện có: pipeline, discovery, download, extract, references, storage, metrics, paper.

## 3. Hợp đồng hook (viết trong `src/<module>.py`)

| File | Chủ | Hàm | Gọi như |
|---|---|---|---|
| `src/extract.py` | Linh | `extract_paper` | `extract_paper(cfg, arxiv_id, events)` |
| `src/references.py` | Châu | `references_paper` | `references_paper(cfg, arxiv_id, events)` |
| `src/storage.py` | Linh | `store_paper` | `store_paper(cfg, arxiv_id, events)` |
| `src/metrics.py` | Hiền | `start_sampler` | `start_sampler(cfg, run_dir)` trả về object có `.stop()` |

Quy tắc chung:
- Idempotent: chạy lại không tạo bản ghi trùng, không ghi đè kết quả tốt bằng kết quả lỗi.
- Ghi log bằng `events.log(stage, status, arxiv_id=..., version=..., error_code=..., elapsed_seconds=...)`.
- Lỗi bất ngờ cứ ném ra: `process_paper` bắt, ghi sự kiện `failed` rồi đi tiếp.
- Đọc `cfg["output_root"] / cfg["run_id"]` để biết thư mục run. Ghi file bằng `write_json_atomic` trong `common.py`.
- Hàm chưa có thì pipeline ghi `skipped`, không báo lỗi.

## 4. Việc của từng người

### Linh: `src/extract.py`, `src/storage.py`
Đọc: `src/downloader/http.py` (hàm `make_manifest`, `detect_format`), `src/common.py`, mục 2 ở trên.
- Đầu vào: `manifest.json` của từng phiên bản, trong đó `archive_path` (tương đối với thư mục run), `source_format` (tar.gz, gz, tar, pdf, unknown).
- Chỉ giữ `.tex`/`.bib`, giữ đường dẫn tương đối, không sửa nội dung. Gợi ý đặt ở `papers/<key>/versions/v<N>/source/`.
- Cập nhật manifest: `extract_status`, `source_files`, `unpacked_total_bytes`, `kept_source_bytes`, `error_code`.
- `pdf` hoặc không có TeX: `extract_status = "empty"`, `error_code = "NO_TEX_SOURCE"`. Định dạng `unknown`: `failed` với `UNSUPPORTED_FORMAT`, **không** coi là rỗng.
- Nên xóa `tmp/.../source.archive` sau khi extract xong để giảm đĩa. Phải ghi `archive_bytes` trước khi xóa (hiện đã ghi sẵn).
- Trong tập thử 20 bài có 1 phiên bản `pdf`, dùng nó để kiểm tra trường hợp rỗng.

### Châu: `src/references.py`, `src/utils/http.py`
Đọc: `src/utils/http.py` (`request_with_retry`, `request_json`, `request_text`, `download_file`), `src/utils/errors.py`, mục 3.
- Gọi Semantic Scholar theo ID bài (không theo phiên bản), lưu **mọi** reference kể cả không có arXiv ID,
  kèm `status`, `fetched_at`, nguồn. Gợi ý: `papers/<key>/references.json`.
- Phân biệt: phản hồi rỗng hợp lệ (`empty`, lý do NO_REFERENCES_RETURNED), NOT_FOUND, lỗi mạng, INVALID_RESPONSE.
- Giới hạn Semantic Scholar riêng (1 request/giây), không dùng chung throttle của arXiv.
- Lớp HTTP chung duy nhất là `src/utils/http.py`: timeout, retry, backoff, throttle trước **mọi** lần thử, hook từng lần
  thử (tham số từ khóa). Lỗi dùng chung `CrawlError` (`src/utils/errors.py`). Không gọi `requests` trực tiếp ở module khác.
- Discovery và download lấy cấu hình HTTP từ `http_options(cfg, throttle, on_attempt=None)` trong `src/config.py`
  (dict tham số cho `request_text` và `download_file`). Cắm hook từng lần thử qua `on_attempt=`.
- Việc còn lại của Châu: hàm `references_paper(cfg, arxiv_id, events)` bọc `crawl_references`, ghi
  `papers/<key>/references.json`, và truyền hook từng lần thử khi gọi `request_json`.

### Hiền: `src/metrics.py`, `configs/benchmark.json`, notebook Colab
Đọc: `src/main.py` (hàm `start_metrics`, `process_paper`), `src/common.py` (`EventLog`, `Stopwatch`), `run.json` mẫu.
- `start_sampler(cfg, run_dir)`: lấy mẫu RAM của tiến trình gốc và tiến trình con (psutil), đĩa của `run_dir`
  (gồm `tmp/`), trả về object có `.stop()`. Ghi mẫu ra file riêng trong `run_dir`.
- Số liệu lấy từ `events.jsonl`:
  - Thời gian từng bài: sự kiện `stage == "paper"` (`elapsed_seconds`, gồm discovery + download + các hook).
  - Tổng thời gian discovery: cộng `elapsed_seconds` của sự kiện `discovery` (không còn `discovery_total`).
  - Thời gian toàn pipeline: sự kiện `pipeline` cuối, hoặc `wall_seconds` trong `run.json`.
  - Tỷ lệ thành công, lỗi theo `error_code`: đếm theo `stage`, `status`, `error_code`.
- Bài `skipped` (đã có từ lần trước) có thời gian gần 0. Báo cáo cold và warm riêng, đọc `cache_mode` trong `run.json`.
- Tập benchmark: bạn chốt danh sách ID và phiên bản kỳ vọng trong `configs/benchmark.json`, rồi tạo config
  riêng (vd `configs/bench_B.yaml`, **không** đặt tên `test*`) trỏ `ids_file` tới file ID đó và dùng `run_id` cố định.
- Cần thêm `psutil`, `pytest` vào `src/requirements.txt` và ghim phiên bản. Kế hoạch dùng Python 3.12 trên Colab.

## 5. Cách chạy và kiểm tra

Cài đặt (một lần):

    git clone https://github.com/qaztyhn/intro2DS.git && cd intro2DS
    python3 -m venv .venv && source .venv/bin/activate
    pip install -r src/requirements.txt

Sửa `user_agent` trong `configs/baseline.yaml` thành email thật của người chạy.

Chạy thử 2 bài (khoảng 20 giây, gọi mạng thật):

    printf '2507.01659\n2507.01660\n' > configs/test_ids_small.txt
    cp configs/baseline.yaml configs/test_small.yaml
    # sửa trong test_small.yaml: run_id: "B_test_small" và ids_file: "configs/test_ids_small.txt"
    python -m src.main configs/test_small.yaml

Kỳ vọng: 2 bài `ok`, 4 phiên bản tải về. Chạy lại cùng lệnh thì cả hai `skipped` và mất chưa tới 1 giây.

Kiểm tra hook của bạn mà không sửa file của ai (ví dụ cho Châu, đổi tên module/hàm cho người khác):

    python - <<'EOF'
    import sys
    import src.references as rf
    def fake(cfg, arxiv_id, events):
        events.log("references", "ok", arxiv_id=arxiv_id, message="fake")
    rf.references_paper = fake
    sys.argv = ["main", "configs/test_small.yaml"]
    from src.main import main
    main()
    EOF
    grep -E '"stage": "(references|paper)"' experiments/B_test_small/events.jsonl | tail

Chạy đầy đủ:

    python -m src.main configs/baseline.yaml      # ids_file mặc định là configs/ids.txt (20 bài)

Đổi `ids_file` sang `configs/ids_full.txt` cho cả 5000 bài (2507.01659 đến 2507.06658). Ước tính từ lần chạy thử:
khoảng 4,5 giây mỗi bài cho discovery và 3,2 giây mỗi phiên bản cho download, tức khoảng 14 giờ chỉ cho B
chưa tính references. Cần đặt `run_id` cố định và lưu `experiments/` ra ổ bền trên Colab để resume.

## 6. Chưa làm / lưu ý

- `extract`, `references`, `storage`, `metrics` chưa có hàm, đang `skipped`. Chưa chạy đầy đủ end-to-end.
- `main.py` còn dùng `EventLog` riêng trong `common.py`. Cần chuyển sang `EventLogger` và `measure_stage` của Hiền,
  và tách pha discovery (cả tập ID) khỏi vòng lặp theo bài để khớp `docs/metrics-integration.md`.
- `tests/` trống, `src/requirements.txt` chưa có psutil/pytest và chưa ghim phiên bản.
- Discovery chỉ ghi phiên bản tại thời điểm tra. Phiên bản mới xuất hiện sau đó chỉ được thấy khi chạy run mới.
- Không commit thư mục `experiments/`, `configs/test*`, `.venv`.
- Trường `attempts` trong `events.jsonl` đang là `null` (`CrawlError` không đếm số lần thử). Số lần thử sẽ được ghi từng lần qua hook của Hiền khi nối metrics.