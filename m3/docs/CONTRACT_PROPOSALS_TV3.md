# Đề xuất thay đổi contract (TV3)

Ghi chú của TV3 về chỗ contract bản 6 thiếu hoặc chưa quy định. Chưa có thay đổi nào được áp dụng vào
`m3/src/contracts.py`; đây chỉ là đề xuất để nhóm quyết định.

## 1. Thí nghiệm che url/DOI/arXiv ID cần pairs tính lại (quan trọng)

**Vấn đề.** `run_experiments(*, pairs, labels, splits, seed, output_dir)` chỉ nhận `pairs` đã tính sẵn. Che url/DOI/arXiv ID
(`MaskConfig`) phải tính lại đặc trưng từ bibtex và references (`build_pair_batch(..., mask=MaskConfig)` của TV1), nên TV3
không thể tự che từ bảng `pairs` đã có.

**Cách TV3 đã làm (không đổi contract).** Thêm tham số tùy chọn:

```python
run_experiments(*, pairs, labels, splits, seed, output_dir,
                masked_pairs: dict[float, pa.Table] | None = None) -> Path
```

- Khóa là tỉ lệ che (kỳ vọng 0.0, 0.25, 0.5, 1.0); giá trị là bảng `pairs` đã tính lại với `MaskConfig(fraction=...)`.
- Mỗi bảng phải qua `validate_table("pairs", ...)`, cùng tập entry/ứng viên với `pairs` gốc. Việc che áp dụng cho cả train
  lẫn đánh giá vì model được fit và dự đoán trên cùng bảng đã che.
- Không truyền `masked_pairs` thì `experiments.json` ghi `"mask": {"status": "skipped", "reason": "..."}`.

**Đề xuất.** Người sở hữu `pipeline.py` (hoặc TV1) sinh `masked_pairs` bằng `MaskConfig(fraction=f, seed=...)` cho
f ∈ {0, 0.25, 0.5, 1} rồi truyền vào `run_experiments`. Nếu muốn ghi vào contract: thêm chữ ký trên vào mô tả
`run_experiments` ở tài liệu phân công. Mặc định cột `title_similarity` không bị che, nên baseline chỉ tiêu đề không đổi theo
mức che.

## 2. Bài có trong `queries` nhưng không có trong `splits`

`contracts.fixture()` có bài `2507.00002` (query `q4`) không nằm trong `splits`, và `validate_splits` không đòi mọi bài
phải có split. Contract không nói `predict_top5` phải làm gì.

**Cách TV3 đã làm.** Bỏ bài đó khỏi `predictions` và gọi `warnings.warn` kèm danh sách bài bị bỏ. Không ném lỗi, vì
ném lỗi sẽ làm hỏng chuỗi fixture chính thức.

**Đề xuất.** Ghi vào contract: "bài không có trong `splits` bị bỏ khỏi `predictions`".

## 3. Phụ thuộc cần thêm vào `m3/src/requirements.txt`

TV3 không sửa file này (không thuộc phạm vi). Cần thêm:

```
numpy
pandas
scikit-learn
lightgbm
```

Đã kiểm thử với scikit-learn 1.9.1, lightgbm 4.7.0, numpy 2.5.4, pandas 3.1.1, pyarrow 26.0.1, Python 3.12.
Các gói này có sẵn trên Colab.

## 4. Quy ước cột đặc trưng trong `pairs`

Contract cho phép thêm cột đặc trưng sau 3 cột khóa nhưng không nêu kiểu hay tên cấm. `model.py` đang quy ước:

- Đặc trưng là cột số hoặc bool (null được phép, bool ép sang float, null thành NaN). Cột kiểu khác bị bỏ khi
  `features=None` (và báo lỗi nếu được chỉ định rõ).
- Cột toàn null trên tập train bị bỏ khỏi model và ghi lại trong `features_dropped_`.
- Tên cột `groundtruth_arxiv_id`, `label_source`, `member_id`, `partition` bị cấm làm đặc trưng; có trong `pairs` thì
  `fit_ranker(features=None)` ném `ValueError`.

**Đề xuất.** Ghi các quy ước này vào mô tả bảng `pairs` của contract để TV1 biết khi thêm cột mới. Nhóm đặc trưng cho
ablation suy từ tên cột (title / author / year / arxiv_id, doi, url / other), nên TV1 nên đặt tên cột có các tiền tố này.

## 5. Hai bản cài đặt `evaluate_mrr_at_5`

`contracts.evaluate_mrr_at_5` và `m3/src/evaluate.py` cùng tồn tại. Hai bản được đối chiếu tuyệt đối (so bằng `==`)
trên fixture và 300 bộ dữ liệu ngẫu nhiên có seed (có và không có `manual`), gồm cả thông báo lỗi. `evaluate.py` chặt hơn một
điểm: validate cả `predictions` (khóa trùng, partition ngoài `train/valid/test`) và `manual`. Với dữ liệu hợp lệ, kết quả
giống hệt.

**Đề xuất.** Giữ bản trong contract làm tham chiếu và để `evaluate.py` là bản dùng trong pipeline; giữ test đối chiếu.

## 6. `validate_table("predictions")` không kiểm tra nhất quán với `splits`

Cột `partition` của `predictions` chỉ được kiểm tra thuộc `{train, valid, test}`, không đối chiếu với `splits`. TV3 luôn
sinh `partition` từ `splits`, và `evaluate_mrr_at_5` lọc theo `splits` chứ không theo cột `partition` của `predictions`.

**Đề xuất.** Thêm kiểm tra này vào `validate_table`/hàm riêng, hoặc ghi vào contract rằng `splits` là nguồn chân lý.

## 7. Fixture chung không đủ cho chuỗi fit → predict → evaluate

`contracts.fixture()` chỉ có bài test, cột đặc trưng đều null và không có bài train, nên `fit_ranker` trên fixture nguyên
bản báo `ValueError` (không có mẫu nào). Test tích hợp của TV3 thêm 2 bài train dựng bằng `pa.table` trong file test.

**Đề xuất.** Nếu nhóm muốn một fixture tích hợp dùng chung, thêm vào contract một hàm kiểu `fixture_with_train()`.

## 8. `scope="labeled_only"` và entry có nhãn nhưng không có trong `queries`

`predict_top5` đi từ `queries`. Entry có trong `labels` nhưng không có trong `queries` sẽ không có dòng dự đoán;
nếu bài thuộc test thì `evaluate_mrr_at_5` báo `ValueError` kèm khóa. Contract không quy định `predict_top5` phải báo sớm.
Nếu muốn lỗi xuất hiện sớm hơn thì đây là chỗ cần quyết định.
