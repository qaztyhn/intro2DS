# D1 OAI-PMH — cách chạy và kiểm chứng

D1 chỉ thay discovery. `src/main.py` chọn OAI-PMH khi `config_name: D1`;
`B` và cấu hình không có `config_name` tiếp tục gọi `main.discover_paper`.
Download, extract, references, storage và resource monitoring dùng code chung.

## Metadata và resume

- Một request logic `GetRecord`, `metadataPrefix=arXivRaw`, cho mỗi bài cần discovery.
- Endpoint: `https://oaipmh.arxiv.org/oai`, theo
  [tài liệu chính thức của arXiv](https://info.arxiv.org/help/oa/index.html).
  `arXivRaw` có lịch sử version. Không dùng OAI `datestamp` làm ngày nộp bài.
- Giữ các trường baseline: `arxiv_id`, `title`, `authors`, `submission_date`,
  `revised_dates`, `venue`, `versions`, `latest_version`, `status`, `error_code`.
- `versions` là danh sách số nguyên liên tiếp từ 1; ngày version chuyển về ISO UTC.
  Không tự thêm version bị thiếu. Lịch sử trùng/gãy, XML hỏng, sai ID hoặc thiếu
  title/authors/ngày v1 được ghi `failed`, `INVALID_RESPONSE`.
- Venue thiếu là `null`. Thiếu ngày revision: giữ danh sách version,
  `revised_dates=null`, cảnh báo trong `events.jsonl`, discovery vẫn `ok` như B.
  Không bịa ngày; `version_history` giữ từng ngày biết được.
- `arXivRaw` cung cấp authors dạng văn bản. Tách dấu phẩy/`and` thông thường,
  giữ dấu phân cách trong ngoặc và lưu `authors_raw`. Tên có dấu phẩy hoặc cách
  ghi đặc biệt vẫn có thể tách chưa chính xác; chưa đối chiếu dữ liệu thật.
- Bản ghi OAI không tồn tại/đã xóa và HTTP 404 được phân loại `NOT_FOUND`.
- Output: `experiments/<run_id>/papers/<paper_key>/metadata.json`.
  `paper_key` dùng quy tắc chung, hỗ trợ cả ID cũ như `hep-th/9901001`.
- Resume chỉ bỏ qua metadata đúng ID, đúng schema, status `ok`, đủ trường và ngày.
  Cache lỗi/thiếu được discovery lại. Cache hợp lệ không bị ghi lại hay đo lại.

## Metrics và giới hạn so sánh

- `discover_paper(..., metrics_logger=None)` tương thích chữ ký baseline.
- Một cặp `started`/`finished` từ `metrics.measure_stage` cho mỗi discovery
  thực hiện, kể cả thất bại. `main` không bọc thêm một measurement discovery.
- `metrics.make_request_hook` tạo `request_id` riêng cho từng `GetRecord`.
  Các retry chung request dùng attempt 1, 2, 3...; bài khác có request ID khác.
- `src/utils/http.py` áp dụng throttle, timeout, retry, Retry-After và backoff.
  Request metrics có attempt, HTTP status, error code và duration; duration request
  không tính throttle/backoff, còn duration stage có tính thời gian chờ như B.
- XML/OAI lỗi trong HTTP 200: request attempt thành công ở tầng HTTP nhưng stage
  thất bại. Mã lỗi nghiệp vụ nằm trong metadata và `events.jsonl`, theo chuẩn chung.
- So sánh thời gian discovery B/D1 trên cùng 40 ID, cùng thiết lập HTTP, cùng
  điều kiện cold/warm. Không trộn lần resume với lần chạy mới.
- **Giới hạn có sẵn của B:** request qua `arxiv.Client` chưa đi qua request hook
  chung; chỉ HTTP fallback lấy abs-page có hook. Do đó `observed_attempt_count`
  của B không đại diện toàn bộ request. Không dùng số này để kết luận trực tiếp
  tổng request B so với D1. Không sửa baseline để thay đổi giới hạn này.
- `configs/baseline.yaml` hiện dùng 20 ID; khi benchmark công bằng cần một config
  B riêng trỏ đến `configs/d1_pilot_ids.txt`, không chỉnh file baseline của nhóm.

## Cấu hình pilot

`configs/d1_pilot.yaml` dùng 40 ID trong `configs/d1_pilot_ids.txt`, khớp lựa chọn
seed 42 bằng `select_benchmark`/`select_pilot` và `configs/benchmark.json`:
10 bài mỗi range. Không cần tải artifact benchmark hay crawl để tạo lại danh sách.

`run_id: D1_pilot_run01` cố định để resume. Đổi sang một run ID D1 mới khi muốn
đo cold run. Output nằm trong `experiments/`; User-Agent dùng contact đã có trong
baseline, delay 3 giây, timeout 30 giây, tối đa 3 retry và backoff cơ sở 2 giây.

## Các lệnh từ thư mục repository

Môi trường `.venv` có các dependency của project; không cần nâng cấp.

```bash
source .venv/bin/activate
python3 -m pytest -q tests/test_main_hooks.py tests/test_metrics_integration.py
python3 -m pytest -q tests/test_oai_pmh.py
python3 -m pytest -q
git diff --check
```

Các test chạy offline, mock request mạng và ghi fixture trong thư mục tạm.
`tests/test_oai_pmh.py` kiểm tra parser, version, schema/storage, resume,
retry/HTTP errors, request metrics và lựa chọn B/D1 trong driver, gồm kiểm tra
discovery chỉ đo một lần. Không sửa kỳ vọng của test chung.

Sau khi thu gọn phạm vi PR: test D1 và hooks/metrics integration có 72 PASS.
Lệnh toàn bộ suite dừng với 2 lỗi collection vì hai test metrics chung trong
`origin/main` còn import `src.common` (đã chuyển sang `src.utils.common`).
Không sửa các test đó trong D1. Để chạy phần thu thập được và vẫn báo đầy đủ lỗi:

```bash
python3 -m pytest -q --continue-on-collection-errors
```

Lần chạy này có 633 PASS, 5 FAIL, 2 lỗi collection và 121 subtests PASS.
Cả 5 FAIL thuộc test
Kaggle có sẵn trên main: authors rỗng; ngày v1 không hợp lệ; snapshot thiếu;
snapshot rỗng; cache thiếu ngày revision. Code Kaggle và các kỳ vọng test giữ
nguyên như `34c46ec`. Các lỗi này cần nhóm phụ trách xử lý riêng.

Chỉ khi muốn chạy pilot mạng thật, thực hiện:

```bash
python3 -m src.main configs/d1_pilot.yaml
```

Chạy lại cùng lệnh sẽ resume `D1_pilot_run01`. Lệnh trên chạy cả pipeline, gồm
download mọi version và gọi Semantic Scholar, không chỉ lấy metadata.

Xem metadata, events và metrics trong `experiments/D1_pilot_run01/`.
Chưa chạy crawl thật trong công việc này; chưa xác nhận khả dụng endpoint,
độ đầy đủ metadata thật hoặc hiệu năng thực tế.

## Phạm vi PR so với origin/main 34c46ec

Branch D1 đã fast-forward từ `0c372df` đến `34c46ec`, không tạo commit mới.
Local `main` không đổi. Code D1 được phục hồi bằng Git stash rồi cập nhật import
`src.utils.common` theo cấu trúc mới. Giữ nguyên module Kaggle và thay đổi throttle
Semantic Scholar của nhóm.

`git stash apply --index` không khớp patch ở `src/main.py`; áp dụng không kèm
`--index` merge ba chiều thành công, giữ cả import mới và lựa chọn discovery D1.
Không có conflict Git chưa xử lý.

Mỗi file có local diff lúc bắt đầu thu gọn được phân loại như sau:

| File | Xử lý/phạm vi |
| --- | --- |
| `src/main.py` | Giữ: chọn discovery D1, truyền metrics logger, giữ nhánh B |
| `src/discovery/oai_pmh.py` | Giữ: parser, resume, metrics và shared HTTP của D1 |
| `configs/d1_pilot.yaml` | Giữ: cấu hình riêng D1 |
| `configs/d1_pilot_ids.txt` | Giữ: 40 ID pilot chung cho D1 |
| `tests/test_oai_pmh.py` | Giữ: test riêng D1 |
| `docs/d1-oai-pmh.md` | Giữ: hướng dẫn và giới hạn của D1 |
| `src/discovery/arxiv_api.py` | Giữ duy nhất `from .. import metrics`: cần để main import được |
| `src/downloader/http.py` | Giữ duy nhất import typing và metrics: cần để main/downloader import và chạy được |
| `README.md` | Khôi phục chính xác `34c46ec`; sửa đường dẫn tài liệu chung không thuộc D1 |
| `src/discovery/kaggle.py` | Khôi phục chính xác `34c46ec`; bỏ mọi sửa logic D2 |
| `tests/test_discovery_metrics.py` | Khôi phục chính xác `34c46ec`; không sửa test chung |
| `tests/test_download_metrics.py` | Khôi phục chính xác `34c46ec`; không sửa test chung |

Hai file ngoài D1 chỉ bổ sung tổng cộng 3 dòng import, không đổi hàm/logic:

- Bản arXiv API trên `34c46ec` dùng `metrics.EventLogger` trong annotation lúc
  định nghĩa hàm nhưng không import `metrics`: import module báo `NameError`.
  `src/main.py` import module này ngay, kể cả khi cấu hình chọn D1.
- Bản downloader trên `34c46ec` thiếu `Dict`, `Any`, `Optional` dùng trong
  annotation, báo `NameError: Dict` khi import. Nó cũng dùng `metrics.EventLogger`,
  `measure_stage` và `make_request_hook` nhưng thiếu import metrics.
  D1 dùng downloader chung nên cần các import này để pipeline khởi động/chạy.
- Đã kiểm chứng lỗi bằng compile/exec nội dung nguyên bản từ Git trong namespace
  riêng, không sửa module thật hay vá trong test. Không dùng sửa logic B/D2
  chỉ để làm test PASS.

Các file/cấu trúc đã có trên main, không phải phần D1 tạo mới: `requirements.txt`
(có `kagglehub==1.0.3`), `src/utils/common.py` (chuyển từ `src/common.py`),
`src/discovery/kaggle.py`, `tests/test_kaggle.py`, `src/utils/http.py` với throttle
Semantic Scholar mới, `src/schema.py`, các module trong `src/stages/`,
`src/tools/demo_m1.py` và các test extract/storage/validation/main-hooks đã đổi
import. `src/requirements.txt` bị xóa bởi main; D1 không tạo lại.
`src/metrics.py`, `src/config.py`, `configs/baseline.yaml` không có local diff.

Bản sao lưu nguyên trạng 11 file nằm ở
`experiments/d1-sync-backup-1kvLsGCj/`; stash D1
`4ba83d4ad910e8a2ab0d9662add1a0b440c706f3` cũng được giữ nguyên. Tài liệu Word,
Zone.Identifier, script patch và tài liệu riêng được đối chiếu byte với backup:
không đổi. Backup không thuộc commit.

Trước khi khôi phục phạm vi, đã sao lưu thêm 17 file và binary patch (cả staged
và worktree) vào `experiments/d1-scope-backup-t0nwBTBx/`. Không xóa backup/stash.

Năm file untracked đã có trước, quyền sở hữu chưa xác nhận:
`apply_d1_patch.py`, `docs/Intro2DS_Project.docx`,
`docs/Intro2DS_Project.docx:Zone.Identifier`, `docs/person2-m1.md`,
`tests/test_m1_integration.py`. Giữ nguyên byte và trạng thái untracked ở lượt
thu gọn này; không đưa vào danh sách stage của D1. Test M1 có trong workspace
nhưng không được tính là file của PR D1.

## Stage file để commit

```bash
git add docs/d1-oai-pmh.md \
  configs/d1_pilot.yaml configs/d1_pilot_ids.txt \
  src/main.py src/discovery/oai_pmh.py src/discovery/arxiv_api.py \
  src/downloader/http.py tests/test_oai_pmh.py
git diff --cached --stat
git diff --cached --check
```

Chỉ stage đúng 8 file trên, gồm 6 file D1 và 2 sửa import bắt buộc đã giải thích.
Index cũ có thể vẫn chứa bản D1 trước khi cập nhật import/tài liệu; cần `git add`
lại các file trên trước commit. Không dùng `git add .`; giữ ngoài commit các
file untracked, dữ liệu crawl, cache và backup. Không tự commit hoặc push.
