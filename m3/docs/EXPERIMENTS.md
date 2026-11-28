# Thí nghiệm model của TV3 (M3: reference matching)

Tài liệu này mô tả cách chạy phần model (huấn luyện, dự đoán top-5, đánh giá MRR@5) và cách đọc kết quả.
Code nằm ở `m3/src/model.py`, `evaluate.py`, `experiments.py`, `run_tv3.py`.

> **Về số liệu trong tài liệu này.** Mọi con số kèm theo (ví dụ ở mục 6) lấy từ **dữ liệu tổng hợp** do test tạo ra
> (`m3/tests/test_experiments.py::make_design`, 24 bài, 2 thành viên). Chúng chỉ minh họa cách đọc file, **không phải kết quả
> của dự án**. Chưa có số liệu nào chạy trên dữ liệu thật.

## 1. Chạy trên Colab

Chỉ cần CPU, không gọi mạng, không tải model (ràng buộc D9). Thư viện dùng: `pyarrow`, `numpy`, `pandas`,
`scikit-learn`, `lightgbm` (Colab có sẵn). `m3/src/requirements.txt` hiện chưa liệt kê numpy, pandas, scikit-learn, lightgbm
(xem `CONTRACT_PROPOSALS_TV3.md`, mục 3); nếu môi trường thiếu thì:

```bash
pip install -r m3/src/requirements.txt          # pyarrow, pyyaml, ... của nhóm
pip install numpy pandas scikit-learn lightgbm  # phần model của TV3
```

Đặt repo ở thư mục hiện tại (clone hoặc giải nén), rồi chạy:

```bash
python -m m3.src.run_tv3 \
  --input  /content/drive/MyDrive/m3/work \
  --output /content/drive/MyDrive/m3/runs/tv3_r1 \
  --seed 2026 \
  --masked-pairs-dir /content/drive/MyDrive/m3/masked \   # tùy chọn
  --scope all_entries
```

Từ Python (TV4 gọi hàm này ở stage train/predict):

```python
from m3.src.run_tv3 import run
info = run(input_dir="...", output_dir="...", seed=2026, masked_pairs_dir=None, scope="all_entries")
```

Thư mục vào:

```
input_dir/
  pairs.parquet      # 3 cột khóa + cột đặc trưng (số hoặc bool, null được phép)
  labels.parquet
  splits.parquet
  queries.parquet
masked_pairs_dir/    # tùy chọn
  mask_0/pairs.parquet   mask_0.25/pairs.parquet   mask_0.5/pairs.parquet   mask_1/pairs.parquet
```

Thư mục ra (**phải chưa tồn tại hoặc rỗng; nếu không rỗng thì báo `FileExistsError`, không ghi đè**):

```
output_dir/
  predictions.parquet   # schema predictions của contract, đã qua validate_table
  metrics.json          # model được chọn, siêu tham số, MRR valid/test + CI
  experiments.json      # toàn bộ thí nghiệm
  handoff_G4.md         # đường dẫn, số dòng, sha256, lệnh tái tạo, danh sách T3-x
```

Ghi vào một thư mục tạm cạnh `output_dir` rồi đổi tên, nên lỗi giữa chừng không để lại output dở dang. Thiếu file đầu vào thì
`FileNotFoundError`; bảng không hợp lệ thì `ValueError` kèm tên bảng. Mã thoát CLI: 0 thành công, 1 lỗi đầu vào/đầu ra.

`masked_pairs_dir` cần TV1 tính lại đặc trưng với `MaskConfig` (che url/DOI/arXiv ID ở cả train và đánh giá). TV3 không tự che
(xem `CONTRACT_PROPOSALS_TV3.md`, mục 1). Không truyền thì mục `mask` trong `experiments.json` là `skipped` kèm lý do.

## 2. Quy tắc chung

- **Q (mẫu đánh giá)** = entry **có nhãn** của bài thuộc partition đánh giá. Entry chưa gán nhãn không nằm trong mẫu số.
- **MRR@5** của một entry = 1/rank nếu `groundtruth_arxiv_id` nằm trong `cand_1..cand_5`, ngược lại 0; MRR = trung bình trên Q.
  Ứng viên chỉ lấy trong references của chính bài đó (D8).
- **Chọn siêu tham số và model chỉ trên valid.** Mỗi cấu hình trong lưới được fit trên train và chấm trên valid; chọn MRR valid
  cao nhất, **đồng điểm thì lấy cấu hình đứng trước trong lưới**. Sau đó **test chỉ được chấm một lần** với cấu hình đã chọn.
  Test không bao giờ tham gia chọn lựa.
- `fit_ranker` chỉ nhìn bài `train` (nhãn valid/test không ảnh hưởng hệ số; có test T3-4). Không dùng `groundtruth_arxiv_id`,
  `label_source`, `member_id`, `partition` làm đặc trưng.
- Cột bool ép sang float; null giữ là NaN (SimpleImputer median với LR, LightGBM xử lý NaN sẵn). Cột **toàn null trên tập
  train** bị bỏ và ghi vào `features_dropped_all_null`.
- **Khoảng tin cậy 95%**: bootstrap **theo entry** (1000 lần, `numpy.random.default_rng(seed)`, percentile 2.5/97.5).
- Sổ manual dựng từ `labels` (bài có `label_source == "manual"`).

## 3. Các thí nghiệm trong `experiments.json`

| Khóa | Nội dung |
|---|---|
| `title_baseline` | (a) Xếp thẳng theo `title_similarity` giảm dần (NaN xếp cuối), **không huấn luyện**. |
| `logistic_regression` | (b) `Pipeline(SimpleImputer median, StandardScaler, LogisticRegression(class_weight="balanced", max_iter=1000))`; `C ∈ {0.01, 0.1, 1, 10}` chọn trên valid. |
| `lightgbm_ranker` | (c) `LGBMRanker(objective="lambdarank")`, group theo entry, `n_jobs=1`, `deterministic=True`; lưới 2×2: `n_estimators ∈ {50, 100}` × `num_leaves ∈ {4, 8}`, `min_child_samples=5`; chọn trên valid. |
| `ablation` | (d) Bỏ lần lượt từng nhóm đặc trưng khỏi LR (giữ `C` đã chọn ở `logistic_regression`, không tune lại). |
| `mask` | (e) Che url/DOI/arXiv ID ở 0/25/50/100% — xem mục 4. |
| `random_baseline` | MRR kỳ vọng của xếp hạng ngẫu nhiên (mốc sàn). |
| `candidate_distribution` | (g) Số ứng viên của các bài test. |
| MRR theo pool và thành viên + CI | (f) Nằm trong khối `test` của **mỗi** thí nghiệm. |

- **Nhóm đặc trưng (ablation, kiểm tra che).** Suy từ tên cột, ánh xạ cột → nhóm ghi trong `ablation.feature_groups`:
  chứa `title` → `title`; `author` → `author`; `year` → `year`; `arxiv_id`/`doi`/`url` → `id`; còn lại → `other`. Cột mới do TV1
  thêm tự rơi vào một nhóm. Chỉ nhóm có cột được dùng mới được ablate; nếu bỏ nhóm mà không còn đặc trưng nào thì ghi `skipped`
  kèm lý do.
- **Mốc ngẫu nhiên.** Mỗi entry: `(1/n)·Σ_{k=1..min(5,n)} 1/k`, với n = số ứng viên của entry (entry 0 ứng viên = 0), rồi lấy
  **trung bình trên Q** (không phải trên bài). Có cho valid và test, tách manual/auto. Tính trên bảng `pairs` gốc (không che).
- **Phân bố ứng viên.** Phạm vi: bài partition `test`; số ứng viên của một bài = số `ref_arxiv_id` khác nhau trong `pairs`
  của bài. Gồm `min`, `median`, `max`, `histogram` (các khoảng 0, 1-5, 6-10, 11-20, 21-50, 51-100, 101-200, 201-500, 501+),
  và `per_paper`.
- `data_summary` đếm bài và entry có nhãn theo partition và pool, để biết mẫu đánh giá lớn cỡ nào.

## 4. Thí nghiệm che và kiểm tra che có tác dụng

Với mỗi mức che có trong `masked_pairs`, **cả LR và LightGBM** được chọn trên valid (cùng lưới như trên) và chấm test một lần;
kết quả nằm ở `mask.levels["<mức>"].logistic_regression` và `.lightgbm_ranker`. Việc che áp dụng cho cả train lẫn đánh giá
vì model fit và dự đoán trên cùng bảng đã che.

Để chắc là che thật sự làm đặc trưng biến mất, mỗi mức ghi:

- `id_columns`: các cột nhóm id có trong bảng pairs đã che; `id_columns_used_by_logistic_regression`: cột nhóm id mà LR còn dùng
  (cột toàn null bị bỏ nên mức 100% sẽ rỗng).
- `id_feature_nonnull_rate` cho `train` và `test`: `rows_with_nonnull_id` (tử số: số dòng pairs có ít nhất một cột nhóm id
  khác null; NaN coi là null), `rows` (mẫu số: tổng số dòng pairs của các bài thuộc partition đó) và `rate` = tử số / mẫu số.
- **Ở mức 1.0, nếu tỉ lệ khác 0 thì `run_experiments` ném `ValueError` kèm tên cột** (che 100% mà vẫn còn giá trị nghĩa là che
  không có tác dụng).
- `mask.warnings`: nếu nhóm id thiếu đặc trưng cho DOI hoặc url. Với đặc trưng hiện tại của TV1, nhóm id chỉ có `arxiv_id_match`,
  nên cảnh báo là: *nhóm id chỉ gồm ['arxiv_id_match']: thí nghiệm che chỉ tác động lên arXiv ID, không tác động lên DOI/url*.

**Giới hạn:** chừng nào `pairs` chưa có cột dùng DOI hay url (`doi_match`, `url_has_arxiv_id` — đề xuất ở mục 9 của
`CONTRACT_PROPOSALS_TV3.md`), thí nghiệm che url/DOI không đo được gì; kết quả che chỉ nói về sự phụ thuộc vào arXiv ID.

## 5. Đọc `experiments.json` và `metrics.json`

Một thí nghiệm có dạng:

```
model, features, features_dropped_all_null, train_counts,
hyperparameters,                     # siêu tham số đã chọn
selection: {rule, grid: [{params, valid_mrr_at_5, valid_entries}], chosen_index},
valid: {mrr_at_5, evaluated_entries},
test:  {mrr_at_5, entries, ci95: {low, high, n_boot, level},
        manual: {mrr_at_5, entries, ci95}, auto: {...},
        by_member: {<member_id>: {mrr_at_5, entries, ci95}}}
```

- `metadata`: seed, `commit` (`git rev-parse HEAD`; `null` nếu không có git, ví dụ khi giải nén zip lên Colab), phiên bản
  thư viện, `n_bootstrap`, `run_at` (thời điểm chạy, phần duy nhất không tất định).
- Một khối MRR/CI có `entries` nhỏ (ví dụ 1–2 entry cho một thành viên) thì CI suy biến hoặc rất rộng; đọc `entries` trước khi
  đọc MRR. `mrr_at_5 = null` nghĩa là không có entry nào trong nhóm đó.
- `metrics.json` (do `run_tv3`) là bản tóm tắt cho **model được dùng để sinh `predictions.parquet`**: `selected_model`
  (tên, lý do, MRR valid của cả hai model), `hyperparameters`, `features_used` / `features_dropped_all_null`, `valid`, `test`
  (chung/manual/auto kèm CI), số dòng predictions, các bài có trong `queries` nhưng không có trong `splits` (bị bỏ), commit,
  phiên bản thư viện. Không có timestamp, nên cùng đầu vào cho cùng nội dung.
- **Chọn model để sinh predictions**: LR hay LightGBM theo MRR valid; chỉ khi LightGBM **cao hơn hẳn** mới chọn nó, hòa thì chọn LR
  vì đơn giản hơn. `run_tv3` fit lại model đã chọn, rồi kiểm tra MRR test tính lại từ `predictions.parquet` đúng bằng số trong
  `experiments.json` (lệch thì dừng với `RuntimeError`).
- Cùng đầu vào, cùng seed, cùng phiên bản thư viện thì `predictions.parquet` giống hệt từng byte (có test).

## 6. Ví dụ đầu ra (dữ liệu tổng hợp, chỉ minh họa)

Chạy CLI trên dữ liệu tổng hợp của test (24 bài; 13 entry test có nhãn), kèm 4 mức che giả lập:

```
Model: lightgbm_ranker (MRR valid: lightgbm_ranker 0.8958 > logistic_regression 0.8750)
MRR@5 test = 0.8269 trên 13 entry (CI95 0.6923–0.9615)
```

Các con số này do dữ liệu giả lập do chính test sinh ra, **không có ý nghĩa khoa học**; không dùng chúng trong báo cáo.

## 7. Lưu ý khi phân tích kết quả thật

1. **Baseline tiêu đề có thể ≈ 1.0.** Entry BibTeX thường có sẵn tiêu đề (và url/ID/DOI) khớp reference, nên xếp theo
   `title_similarity` đã gần đúng hết. Khi đó model không thể hiện lợi thế rõ trên MRR chung, và hai phân tích quan trọng hơn là
   **thí nghiệm che** (model còn đúng khi mất url/DOI/ID?) và **ablation** (đặc trưng nào thực sự cần?). Luôn so với `random_baseline`.
2. **Test chỉ khoảng 8 paper**, mẫu đánh giá chỉ vài chục entry, nên phương sai lớn và CI rộng. Bootstrap ở mức entry bỏ qua việc
   các entry trong cùng một bài tương quan nhau, nên CI **thực tế có thể còn rộng hơn** con số báo cáo. Chênh lệch giữa các model
   nhỏ hơn bề rộng CI thì không nên diễn giải là khác biệt thật.
3. **MRR trên bài gán tay đáng tin hơn bài tự động.** Luật gán nhãn tự động dùng đặc trưng tương tự đặc trưng của model, nên
   MRR trên nhãn tự động có thể cao do tính vòng tròn. Báo cáo ưu tiên `test.manual`; `test.auto` chỉ để tham khảo. Mỗi nhóm cũng có ít
   entry hơn tổng.
4. **Thí nghiệm che bị giới hạn bởi đặc trưng có sẵn** (mục 4): không có cột DOI/url thì chỉ đo được tác động của arXiv ID.
5. **Hòa điểm.** Khi nhiều ứng viên cùng điểm, `ref_arxiv_id` nhỏ hơn đứng trước. Điều này có thể làm MRR hơi cao hoặc thấp một cách
   hệ thống nếu đặc trưng không phân biệt được các ứng viên; `candidate_distribution` và `random_baseline` giúp nhìn ra.
6. **Ablation giữ nguyên `C`** đã chọn cho mô hình đầy đủ và không tune lại; chênh lệch nhỏ có thể do chưa tune.
7. Chưa có kết quả nào trên dữ liệu thật tại thời điểm viết tài liệu này; mọi nhận định ở mục này là hướng dẫn đọc, không phải kết luận.

## 8. Kiểm thử

```bash
python -m pytest m3/tests -q
python -I m3/src/contracts.py
```

Test của TV3: `test_evaluate.py` (T3-1, T3-2, đối chiếu tuyệt đối với `contracts.evaluate_mrr_at_5`), `test_model.py`
(T3-3, T3-4, T3-5, T3-7), `test_offline.py` (T3-6), `test_experiments.py` (T3-8, che), `test_integration_tv3.py`
(fixture → 0.75), `test_run_tv3.py` (end-to-end, chặn socket).
