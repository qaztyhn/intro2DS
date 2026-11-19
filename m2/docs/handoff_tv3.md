# Handoff TV3 — Bibliography Standardization & Profiling

Module: `m2/src/parsing/bibliography.py`, `m2/src/parsing/profiling.py`
Tests: `m2/tests/parsing/test_bibliography.py`, `test_profiling.py`, fixtures trong `m2/tests/parsing/fixtures/`

## 1. Trạng thái so với Definition of Done

| Hạng mục | Trạng thái |
|:--|:--|
| Parse `.bib` và `\bibitem` | Xong, có test |
| Chuẩn hóa fields (title, authors, year, doi, journal, url) | Xong, có test |
| Dedup xuyên version + key mapping | Xong, có test |
| Fixtures `bibliography_bib`, `bibliography_bibitem`, `citation_conflict`, `multi_version` | Xong |
| Script profiling (`scan`, `merge`) | Code và test xong |
| Chạy profiling trên corpus M1 thật của bốn range | **Chưa làm** (xem mục 6) |
| `profiling_summary.json`, `profiling_report.md` | **Chưa tạo** (phụ thuộc mục trên) |
| Parse `.bbl` kiểu `\bibitem` | Code xong, **chưa có dữ liệu** vì M1 không giữ `.bbl` (xem mục 5) |

## 2. API

```python
from src.parsing.bibliography import extract_bibliography, unify_bibliography

extract_bibliography(document: GatheredDocument) -> list[BibEntry]
unify_bibliography(arxiv_id: str, entries_by_version: dict[int, list[BibEntry]]) -> BibliographyResult
```

Hàm phụ có thể dùng lại: `parse_bib(text)`, `parse_bibitems(text)`, `clean_text(s)`, `normalize_entry(entry)`.

`extract_bibliography` đọc (theo thứ tự): các file `.bib` trong `bib_files` (sắp theo tên), các file `.bbl` trong `bib_files`, rồi `\bibitem` trong `document.latex`.
Trong cùng một version, key trùng thì giữ bản đầu và ghi log warning. Hàm không ném exception với input hỏng, chỉ log (`logging`, stage `bib_parse`). Các warning này không nằm trong giá trị trả về, nên TV1 cần cấu hình logging nếu muốn lưu.

## 3. Quy tắc dedup xuyên version (`unify_bibliography`)

Chỉ so khớp chính xác. Đây là lựa chọn thiết kế thận trọng của TV3, không phải yêu cầu của đề bài (ràng buộc "full-text matching, không fuzzy" của đề áp dụng cho dedup elements, không phải bibliography). Hai entry là một nếu (theo thứ tự ưu tiên):
1. cùng `doi`;
2. cùng `eprint` (arXiv id, bỏ hậu tố `vN`);
3. cùng title sau chuẩn hóa (bỏ dấu, chữ thường, chỉ giữ chữ và số), và năm không mâu thuẫn, và họ tác giả đầu không mâu thuẫn;
4. entry `\bibitem` không có title: cùng `raw` sau chuẩn hóa.

Hai entry đều có DOI nhưng khác nhau thì không bao giờ gộp.

- **Canonical key**: key gốc xuất hiện sớm nhất trong cụm mà chưa bị entry khác chiếm. Nếu hai entry khác nội dung cùng key thì entry sau nhận `key_2`, `key_3`, ... kèm warning. Entry không có key thì sinh key dạng `smith2020deep`.
- **Gộp field**: hợp nhất các field không rỗng. Nếu hai version khác giá trị ở một trong 6 field chuẩn, giữ giá trị của version mới nhất và ghi warning.
- **Kết quả**: `entries[i].original_key` chính là canonical key. `fields["source_keys"]` là `{version: [key_cũ, ...]}` để truy vết.
- **Deterministic**: kết quả không phụ thuộc thứ tự key của dict đầu vào (có test).

## 4. Dữ liệu đầu ra

`BibEntry.fields` sau chuẩn hóa luôn có:

| Field | Kiểu | Ghi chú |
|:--|:--|:--|
| `title` | str hoặc None | Đã bỏ markup LaTeX, giữ nguyên `$...$` |
| `authors` | list[str] | Dạng `First Last`, bỏ `others`; `[]` nếu thiếu |
| `year` | int hoặc None | |
| `doi` | str hoặc None | Chữ thường, bỏ tiền tố URL |
| `journal` | str hoặc None | |
| `url` | str hoặc None | |

Các field gốc khác được giữ nguyên (`author` dạng chuỗi, `booktitle`, `month`, `eprint`, `raw`, `label`, `source_keys`, ...). `entry_type` viết thường; `conference` đổi thành `inproceedings`; entry từ `\bibitem` có `entry_type = "misc"`.

Ánh xạ sang bảng `bibtex` (dành cho TV4):

| Cột `bibtex` | Nguồn |
|:--|:--|
| `arxiv_id` | `BibliographyResult.arxiv_id` |
| `citation_key` | `entry.original_key` (đã là canonical) |
| `entry_type` | `entry.entry_type` |
| `title`, `year`, `doi`, `journal`, `url` | `entry.fields[...]` |
| `authors` | `entry.fields["authors"]` |

Các field khác không đưa vào bảng.

## 5. Ghi chú cho các thành viên khác

**TV1 (gathering, pipeline)**
- Gọi `unify_bibliography` chỉ với các version `status == "ok"`. Version no_tex hoặc failed không có mặt trong `key_mapping`, nên dùng `key_mapping.get(version, {})`.
- `key_mapping[v]` không chứa key rỗng và không chứa key được `\cite` nhưng không được định nghĩa ở đâu cả.
- `extract_bibliography` cũng đọc `\bibitem` trong `document.latex`, nên `latex` cần giữ nguyên các môi trường `thebibliography` (TV2 sẽ tự loại chúng khỏi hierarchy).
- **Vấn đề `.bbl` (cần quyết định)**: M1 chỉ giữ `.tex` và `.bib` (whitelist ở `m1/src/stages/extract.py` dòng 125 và 270, `m1/src/stages/validation.py` dòng 90-91, `m1/src/tools/benchmark.py` dòng 401). Các bài chỉ có `.bbl` (không `.bib`, không `\bibitem` trong `.tex`) hiện không còn nguồn bibliography. Thầy đã trả lời nên thử convert ngược `.bbl` về `.bib`, nếu không được thì giữ `.bbl` và ghi chú. Parser `.bbl` (`parse_bibitems`) đã có. Nếu quyết định giữ `.bbl`: (a) sửa whitelist M1 ở 4 chỗ và test liên quan; (b) giải nén lại từ archive gốc (nếu archive còn; nếu không thì cần hỏi thầy vì tải lại nằm ngoài phạm vi M2); (c) `gather_source` đưa `.bbl` vào `bib_files`.

**TV2 (hierarchy)**
- `citation_mapping` truyền vào `parse_hierarchy` là `key_mapping[version]`. Key không có trong mapping thì giữ nguyên.
- Khóa so khớp phân biệt chữ hoa/thường ở mapping; việc chống trùng key của TV3 thì không phân biệt (so bằng `casefold`).

**TV4 (export)**
- Cần xác nhận cách xuất `doi`, `journal`, `url` khi thiếu: hiện là `None`; đề bài ghi kiểu string. Chọn `""` hay null theo schema chính thức.
- `year` là int hoặc None, khớp với cột nullable integer.

## 6. Việc còn lại (chưa làm)

Chạy profiling trên corpus M1 thật. Mỗi thành viên chạy trên máy có corpus của mình, từ thư mục `m2/`:

```bash
python -m src.parsing.profiling scan --run-dir <run_dir> --ids-file <ids_file> --label tv1 --out profiling_tv1.json
```

(`--label` lần lượt là `tv1`, `tv2`, `tv3`, `tv4`; `<run_dir>` là thư mục chứa `papers/`.) TV3 gom bốn file JSON và chạy:

```bash
python -m src.parsing.profiling merge --inputs profiling_tv1.json profiling_tv2.json profiling_tv3.json profiling_tv4.json --out-json profiling_summary.json --out-md profiling_report.md
```

Chỉ commit `profiling_summary.json` và `profiling_report.md`. Không commit các file `profiling_tv*.json` (có danh sách từng version, khá lớn). Các chỉ số liên quan `.bbl` (`bbl_present`, `bbl_only_no_bib`, `bib_cmd_no_recoverable_source`) sẽ cho biết quy mô thiệt hại của vấn đề ở mục 5.

## 7. Giới hạn đã biết

- `.bbl` kiểu biblatex/biber (`\entry{...}`) chưa được parse, chỉ được đếm trong profiling.
- Entry từ `\bibitem` không có `\newblock` chỉ tách được các field nhận diện bằng regex (`doi`, `url`, `eprint`, `year`); `title` và `authors` rỗng, thông tin còn lại nằm trong `raw`.
- Với `\bibitem` có `\newblock`, `journal` lấy nội dung `\emph{}` nếu có; nếu không thì lấy cả khối thứ 3, có thể lẫn volume/trang.
- Tên tác giả giả định dạng `Last, First` hoặc `First Last`; tên tổ chức viết trong `{...}` được giữ nguyên.
- Dedup chỉ so khớp chính xác (lựa chọn thiết kế, xem mục 9), nên các entry giống nhau nhưng khác cách viết title sẽ không được gộp.
- Profiling dùng regex trên `.tex`, chỉ mang tính sơ bộ, không thay thế kết quả parser.

## 8. Chạy test

```bash
cd m2 && python -m pytest tests/parsing -q
```

cat >> m2/docs/handoff_tv3.md << 'EOF'

## 9. Quyết định thiết kế

1. **Hội nghị vào `booktitle`.** Với entry `\bibitem`, khối thứ 3 bắt đầu bằng `In` hoặc chứa `conference`/`workshop`/`symposium`/... được lưu vào `booktitle`. `journal` để `None` nếu không có tên tạp chí. Đây là giới hạn của schema (bảng `bibtex` không có cột `booktitle`), không phải lỗi parser. `booktitle` vẫn nằm trong `BibEntry.fields` nhưng không được đưa vào bảng.
2. **Tên tác giả kiểu Vancouver** (`Smith J, Doe AB`) chỉ được chuyển thành `J. Smith`, `A. B. Doe` khi có ít nhất hai tác giả và tất cả đều khớp mẫu. Trường hợp không đủ điều kiện giữ nguyên để tránh chuẩn hóa sai (ví dụ tên tổ chức `Google AI`).
3. **Dedup chỉ so khớp chính xác** là lựa chọn thận trọng của TV3. Đánh đổi: không gộp nhầm, nhưng bỏ sót các entry đổi cách viết title.
4. **Key BibTeX không phân biệt hoa/thường.** `extract_bibliography` coi `Smith2020` và `smith2020` trong cùng version là trùng (giữ entry đầu, ghi warning). Entry không có key không bao giờ bị coi là trùng. TV2 nên tra `key_mapping` bằng `build_key_resolver(mapping)` (khớp chính xác trước, rồi không phân biệt hoa/thường, trả `None` nếu mơ hồ).
5. **`crossref`** được xử lý trong `extract_bibliography`: field thiếu của entry con được lấy từ entry cha; `title` của cha không bao giờ thay `title` của con, mà thành `booktitle` cho `inproceedings`/`incollection`/`inbook`/`conference`.
6. **`@string`** được thu thập ở lượt đầu nên có thể định nghĩa sau khi dùng. Entry `@...` nằm trên dòng bắt đầu bằng `%` bị bỏ qua (theo cách của biber); `%` trong giá trị field và trong `\url{}`/`\href{}` được giữ nguyên.
7. **`et al.` và `others`** bị loại khỏi `authors`; field `authors_truncated = True` ghi nhận danh sách bị rút gọn.
EOF8. **Gộp khi thiếu năm.** Nếu một bên thiếu `year`, chỉ gộp khi danh sách tác giả đầy đủ (đã chuẩn hóa) giống nhau và title có từ 3 từ trở lên. Các cặp bị chặn bởi quy tắc này không được báo ở đâu (chẩn đoán "có thể trùng" chỉ chạy khi cả hai có năm). Không gộp nghĩa là chưa đủ bằng chứng, không phải chắc chắn khác nhau.
9. **Chẩn đoán "có thể trùng"** dùng hệ số chồng lấn (số từ chung / số từ của title ngắn hơn) >= 0.8, cùng năm, cùng họ tác giả đầu, mỗi title có ít nhất 2 từ; không bỏ stopword. Chỉ ghi warning, không bao giờ gộp. Title ngắn là tập con của title dài luôn cho điểm 1.0 nên có thể ồn; ngưỡng sẽ chỉnh sau khi chạy dữ liệu thật.
10. **Macro tạp chí** (`\apj`, `\mnras`, ...): bảng lấy từ danh sách macro của ADS/AASTeX (https://ui.adsabs.harvard.edu/help/actions/journal-macros). Chỉ thay khi toàn bộ giá trị `journal` là một macro; chuỗi viết tắt thông thường (`ApJ`) không bị đụng. Macro không có trong bảng giữ nguyên. Tên macro đã gặp (đã thay hay chưa) lưu ở `fields["journal_macro"]`; macro chưa biết là entry có `journal` vẫn bắt đầu bằng `\`. Các macro `azh`, `apspr`, `zap`, `physscr`, `pre` chưa được đối chiếu với nguồn.
11. **`year_raw`**: khi `year`/`date` có giá trị nhưng không đọc được năm (`to appear`, `n.d.`), giá trị gốc lưu ở `fields["year_raw"]`. **DOI trong `note`** chỉ nhận khi có tiền tố rõ (`doi:`, `doi.org/`, `\doi{`) và entry chưa có DOI riêng; ghi `doi_source = "note"` và không dùng làm khóa gộp.
12. **`parse_mode`** của entry `\bibitem`: `newblock` (tách theo `\newblock`), `quoted` (tách theo title trong dấu nháy, kiểu IEEE, độ chính xác cao) hoặc `raw_only` (chỉ có `raw` và các field nhận diện bằng regex). Mục "Giới hạn đã biết" ở trên về `\bibitem` không có `\newblock` nay chỉ còn đúng cho nhóm `raw_only`; QA nên đếm tỷ lệ ba nhóm này.
13. **Chưa parse** bibliography viết bằng `\item`, `\harvarditem`, `\reference` và `.bbl` kiểu biblatex. Profiling đo các chỉ số `item_list_bibliography`, `harvarditem_present`, `reference_cmd_present`, `unparsed_format_candidate`, `bbl_biblatex_style`; quyết định có viết parser hay không dựa trên các tỷ lệ này khi chạy dữ liệu thật.
