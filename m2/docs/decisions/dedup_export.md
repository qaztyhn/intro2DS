> Historical component handoff. See [integration decisions](../INTEGRATION.md) for current behavior, especially no-TeX versions (QA only, no synthetic root). Structural dedup policy remains subject to group review.

# TV4 — Tài liệu quyết định kỹ thuật (Dedup, Export, QA)

Trạng thái chung: **Đề xuất — chờ nhóm xác nhận**. Chưa có dòng code nào của TV4 dựa trên tài liệu này.

Nguồn đã đối chiếu:

- Đặc tả chính thức M2 (trang `statement/parsing`), các mục "Requirement" và "Standardization and Deduplication". Phần đặc tả dưới đây là paraphrase.
- Contract nhóm (`Intro2DS_Project_M2.pdf`), ký hiệu "Contract §x".
- Code thực tế của các thành viên, đọc từ remote ngày 2026-11-15, không chạy:
  - TV1: `origin/feature/m2-gathering` (c8c8e0a)
  - TV2: `origin/feature/m2-hierarchy` (c18469d)
  - TV3: `origin/feature/m2-bibliography` (f46f212)

Nếu contract mâu thuẫn với đặc tả thì đặc tả thắng (Contract §10, nguyên tắc 10).

## 0. Điều TV4 nhận và trả

- **Nhận:** `list[ParsedDocument]` của một publication (mọi version), mỗi `ParsedDocument` có `arxiv_id`, `version`, `root: ParsedNode | None`, `status`, `warnings`. `ParsedNode` có `local_id`, `kind`, `content`, `children` (theo thứ tự xuất hiện). `local_id` chỉ dùng nội bộ, TV4 bỏ qua.
- **Trả:** `elements: list[dict]` và `hierarchy: list[dict]`. Chữ ký `deduplicate_elements` giữ đúng Contract §3.4. Thống kê nằm ở hàm phụ, không đổi chữ ký.
- **Đặc tả ba bảng cuối:**
  - `elements`: `element_id`, `arxiv_id`, `kind`, `content`
  - `hierarchy`: `arxiv_id`, `version`, `element_id`, `parent_id`
  - `bibtex`: theo Contract §2.4
  - Không được thêm cột vào ba bảng này (Contract §2.4).

Ví dụ trong đặc tả (`2310-12345`): mỗi version có một root riêng, `kind=document`, ID `#root1`, `#root2`. Root nằm trong `elements` nhưng chỉ xuất hiện ở cột `parent_id` của `hierarchy`, không bao giờ ở cột `element_id`. Câu `#s1` là con của `#h1` ở version 1 và của `#h2` ở version 2.

### Kết quả đọc code TV2 ảnh hưởng trực tiếp đến các quyết định bên dưới

`kind` hiện TV2 phát ra:

- `document`
- `part`, `chapter`, `section`, `subsection`, `subsubsection`, `paragraph_heading`, `subparagraph_heading`
- `abstract`, `appendix`, `acknowledgements`
- `paragraph`, `sentence`
- `equation`, `figure` (cả bảng lẫn hình)
- `list`, `list_item`

`content` hiện TV2 phát ra:

| kind | `content` hiện tại |
|---|---|
| `document` | tiêu đề bài, hoặc `"Document"` nếu không tìm thấy |
| heading các cấp | chuẩn hóa của tiêu đề |
| `abstract`, `appendix`, `acknowledgements` (môi trường) | hằng số kiểu `"Abstract"`, `"Appendix"` |
| `paragraph` | toàn văn đoạn đã chuẩn hóa |
| `sentence` | văn bản câu |
| `equation`, `figure` | toàn bộ nguồn của khối |
| `list` | ghép `content` của các `list_item` |
| **`list_item`** | **nhãn `\item[...]`, thường rỗng** (không phải văn bản của item) |

Hệ quả: các node cấu trúc **không** có `content` nhận diện được. Đây là lý do khóa dedup ở D2 không thể chỉ là `(kind, content)`.

Về trạng thái:

- `parse_hierarchy` trả `ParsedDocument(root=None, status=<trạng thái gathering>)` khi gathering không phải `ok` hay `partial`, và trả `status="no_tex"` khi `latex` rỗng.
- TV1 gán `status="partial"` mỗi khi gathering có bất kỳ warning nào (`gathering.py`, khoảng dòng 431), nên `partial` sẽ phổ biến.

---

## D1. Root của mỗi version

**Quyết định.** Mỗi version có hợp lệ tạo đúng một element `kind=document`, ID `<paper_key>#root<version>`. Root không bao giờ được dedup, kể cả khi hai version giống hệt. Root có mặt trong `elements`, và chỉ làm `parent_id` trong `hierarchy`. Không có dòng hierarchy nào mà root là `element_id`. `content` của root lấy nguyên từ `ParsedNode.content` của TV2 (tiêu đề bài).

**Đặc tả.** Ví dụ trong mục Requirement: `root1` và `root2` là hai element `document` riêng; hierarchy chỉ dùng chúng làm `parent_id`. Contract §12 mục 3 yêu cầu xác minh quy tắc này bằng test fixture trước khi khóa exporter (Phase 2).

**Ví dụ.** Xem (a) trong D2: `root1`, `root2` là hai dòng `elements`, hai version có hai hierarchy row cấp 1 với `parent_id` lần lượt là `root1` và `root2`.

**Phương án khác.**

- Root dùng chung giữa các version: không chọn. Mỗi version cần parent riêng để khôi phục cấu trúc riêng từng version.
- Root không có dòng `elements`, chỉ là chuỗi trong `parent_id`: không chọn. Vi phạm yêu cầu `parent_id` phải tham chiếu element hợp lệ (Contract §2.4).

**Hệ quả cho validator.** Root của một version được nhận ra là: element `kind=document` mà ID là `root<N>`, đồng thời là `parent_id` của các dòng hierarchy version N. Version không có dòng hierarchy (xem D8) chỉ nhận ra qua ID.

Trạng thái: **Đề xuất — chờ nhóm xác nhận.**

---

## D2. Khóa nhận diện element (khóa dedup)

**Quyết định.** Phạm vi dedup là một publication, qua mọi version. Không bao giờ dedup giữa hai publication. Chỉ khớp chính xác trên chuỗi `content` đã được TV2 chuẩn hóa. TV4 không normalize thêm, không fuzzy.

Khóa tùy theo `kind`, chia hai nhóm:

| Nhóm | Kind | Khóa | Lý do |
|---|---|---|---|
| **A. Nhận diện bằng nội dung** | `sentence`, `equation`, `figure`, `paragraph` | `(kind, content)` | `content` là toàn văn nên đủ nhận diện. Câu có thể đổi cha giữa các version, đúng ví dụ `#s1` của đặc tả. |
| **B. Nhận diện bằng ngữ cảnh** | mọi kind còn lại (các heading, `abstract`, `appendix`, `acknowledgements`, `list`, `list_item`, kind lạ) | `(kind, content, parent_ref, n)` | `content` của nhóm này không nhận diện được node (xem phần đọc code TV2 ở mục 0). |

Giải thích nhóm B:

- `parent_ref` là `element_id` của node cha, hoặc `None` nếu cha là root. Dùng `None` cho root để heading cấp một vẫn dùng chung được giữa các version, vì mỗi version có một root khác nhau.
- `n` là số thứ tự (từ 0) của node trong số các anh em đã duyệt trước nó có cùng `(kind, content)` dưới cùng một cha. `n` tách các node anh em trùng nhau, ví dụ hai item rỗng hay hai subsection cùng tên trong một section.

Chống chu trình (nhóm A): nếu element khớp khóa đang nằm trên đường tổ tiên của node hiện tại thì tạo element mới (và ghi một warning), không dùng lại. Nhóm B không cần quy tắc này vì `parent_ref` nằm trong khóa nên đã không thể tạo chu trình.

Khóa dedup luôn chứa `kind`, nên hai node khác loại nhưng cùng nội dung không bao giờ gộp. Contract §3.4 và §12 mục 5 yêu cầu xác minh đúng điểm này; ví dụ (b) bên dưới là fixture cho nó.

**Đặc tả.** Nội dung giống hệt sau cleanup thì lưu một lần và dùng chung `element_id` giữa các version (Standardization and Deduplication); không dedup giữa hai publication (Contract §2.4).

**Phương án khác.**

- **(A) `(kind, content)` cho mọi kind (mặc định ban đầu của kế hoạch TV4).** Không chọn. Hỏng ở ví dụ (c) và (d): hai heading trùng tên bị gộp rồi lẫn con của nhau, và các `list`/`list_item` rỗng gộp lại thành một element, tạo chu trình.
- **Chỉ dùng `content`, bỏ `kind`.** Không chọn. Hỏng ở ví dụ (b), xem bên dưới.
- **Khóa Merkle: `(kind, content, ids của con)`.** Không chọn. Đổi một câu thì mọi tổ tiên, kể cả heading, thành element mới, trái với "lưu một lần".
- **Khóa theo đường dẫn đầy đủ từ root cho mọi node.** Không chọn. Câu chuyển từ `h1` sang `h2` giữa hai version (đúng ví dụ của đặc tả) sẽ không còn dùng chung.

**Chi phí của đề xuất.** Heading, list, `list_item` được dùng chung qua version khi toàn bộ chuỗi tổ tiên của nó giữ nguyên. Nếu section cha bị đổi tên ở version sau, các heading con của nó thành element mới dù tiêu đề không đổi. Nếu TV2 sửa `list_item.content` thành toàn văn item (câu hỏi cho TV2 số 2), có thể chuyển `list` và `list_item` sang nhóm A để dedup tốt hơn.

Trạng thái: **Đề xuất — chờ nhóm xác nhận.** Khác với mặc định ban đầu của kế hoạch TV4, cần người dùng/nhóm xác nhận.

### Ví dụ (a): hai version giống nhau trừ một câu

Input (kind: content). Cả hai version có root `document: "Matching paper"`.

| | v1 | v2 |
|---|---|---|
| section | `Introduction` | `Introduction` |
| paragraph | `We study matching. Results are good.` | `We study matching. Results are strong.` |
| sentence | `We study matching.` / `Results are good.` | `We study matching.` / `Results are strong.` |

`elements` (đánh số theo thứ tự gặp lần đầu):

| element_id | kind | content | ghi chú |
|---|---|---|---|
| `2310-12345#root1` | document | Matching paper | |
| `2310-12345#root2` | document | Matching paper | root không dedup |
| `2310-12345#h1` | section | Introduction | dùng chung v1, v2 (cùng cha là root, `n=0`) |
| `2310-12345#p1` | paragraph | We study matching. Results are good. | chỉ v1 |
| `2310-12345#p2` | paragraph | We study matching. Results are strong. | chỉ v2, vì toàn văn khác |
| `2310-12345#s1` | sentence | We study matching. | dùng chung |
| `2310-12345#s2` | sentence | Results are good. | chỉ v1 |
| `2310-12345#s3` | sentence | Results are strong. | chỉ v2 |

`hierarchy` (cột `position` chỉ ở bản nội bộ, xem D6):

| version | element_id | parent_id | position |
|---|---|---|---|
| 1 | h1 | root1 | 1 |
| 1 | p1 | h1 | 2 |
| 1 | s1 | p1 | 3 |
| 1 | s2 | p1 | 4 |
| 2 | h1 | root2 | 1 |
| 2 | p2 | h1 | 2 |
| 2 | s1 | p2 | 3 |
| 2 | s3 | p2 | 4 |

Số liệu tính tay: 8 element, 8 dòng hierarchy. Số lần xuất hiện của node không phải root là 4 + 4 = 8, số element không phải root là 6 (D10).

### Ví dụ (b): paragraph chỉ có một câu

Cây v1: `document → section "Intro" → paragraph "We define X." → sentence "We define X."`

| element_id | kind | content |
|---|---|---|
| root1 | document | Matching paper |
| h1 | section | Intro |
| p1 | paragraph | We define X. |
| s1 | sentence | We define X. |

Hierarchy: `(h1, root1)`, `(p1, h1)`, `(s1, p1)`.

Nếu khóa bỏ `kind`, `p1` và `s1` trở thành cùng một element, sinh dòng `(s1, s1)`: self-loop. Vì vậy `kind` bắt buộc nằm trong khóa.

### Ví dụ (c): hai subsection cùng tên "Results" dưới hai section khác nhau (cùng một version)

Cây v1 (bỏ paragraph cho gọn):

```
document
├─ section "Methods"
│   └─ subsection "Results"
│       └─ sentence "A."
└─ section "Analysis"
    └─ subsection "Results"
        └─ sentence "B."
```

**Đề xuất (nhóm B, khóa có `parent_ref`):**

| element_id | kind | content | cha |
|---|---|---|---|
| root1 | document | Matching paper | |
| h1 | section | Methods | root |
| h2 | subsection | Results | h1 |
| s1 | sentence | A. | h2 |
| h3 | section | Analysis | root |
| h4 | subsection | Results | h3 |
| s2 | sentence | B. | h4 |

Hierarchy: `(h1,root1)`, `(h2,h1)`, `(s1,h2)`, `(h3,root1)`, `(h4,h3)`, `(s2,h4)`. Cây khôi phục đúng.

**Nếu dùng `(kind, content)` (phương án A):** hai "Results" là cùng một element `h2`. Hierarchy sẽ có `(h2,h1)`, `(s1,h2)`, `(h3,root1)`, `(h2,h3)`, `(s2,h2)`: `h2` có hai cha và cả `A.` lẫn `B.` đều thành con của `h2`. Đọc lại thì cả hai section đều chứa cả hai câu, tức là mất cấu trúc.

### Ví dụ (d): itemize lồng itemize

Cây v1, theo đúng hình dạng TV2 hiện phát ra (`list` và `list_item` có `content` rỗng):

```
document
└─ section "Lists"
    └─ list ""
        └─ list_item ""
            ├─ paragraph "Outer."  → sentence "Outer."
            └─ list ""
                └─ list_item ""
                    └─ paragraph "Inner."  → sentence "Inner."
```

**Đề xuất:**

| element_id | kind | content | cha |
|---|---|---|---|
| root1 | document | Matching paper | |
| h1 | section | Lists | root |
| l1 | list | (rỗng) | h1 |
| i1 | list_item | (rỗng) | l1 |
| p1 | paragraph | Outer. | i1 |
| s1 | sentence | Outer. | p1 |
| l2 | list | (rỗng) | i1 |
| i2 | list_item | (rỗng) | l2 |
| p2 | paragraph | Inner. | i2 |
| s2 | sentence | Inner. | p2 |

Hierarchy: `(h1,root1)`, `(l1,h1)`, `(i1,l1)`, `(p1,i1)`, `(s1,p1)`, `(l2,i1)`, `(i2,l2)`, `(p2,i2)`, `(s2,p2)`. Không chu trình.

**Nếu dùng `(kind, content)`:** hai `list` rỗng gộp thành `l1` và hai `list_item` rỗng gộp thành `i1`, sinh `(i1,l1)` và `(l1,i1)`: chu trình `l1 → i1 → l1`. Cũng vì vậy mọi item rỗng của cả publication sẽ dồn vào một element.

Vì dedup nhóm B dựa trên vị trí trong cha (`n`), nên nếu v2 chèn thêm một item ở đầu danh sách, các item sau sẽ khớp với item có thứ tự cũ. Cấu trúc từng version vẫn đúng, nhưng "item" trong `elements` không còn là cùng một item theo nghĩa nội dung. Đây là lý do cần TV2 trả nội dung thật (D3).

---

## D3. `content` của node cấu trúc (yêu cầu với TV2)

**Quyết định (đề xuất yêu cầu với TV2).**

- **Heading:** chỉ chữ tiêu đề đã chuẩn hóa.
- **Container có văn bản** (`paragraph`, `list`, `list_item`): toàn bộ văn bản đã chuẩn hóa mà nó bao phủ (các con nối bằng một dấu cách).
- **Khối hằng số** (`abstract`, `appendix`, `acknowledgements`): để nguyên như hiện tại; TV4 xử lý bằng khóa ngữ cảnh ở D2.
- TV4 không sửa `content`, không tính lại, không chuẩn hóa thêm.

**Đặc tả.** Đặc tả chỉ nói `content` là nội dung sau cleanup. Contract §2.3 nói `content` đã chuẩn hóa theo quy tắc chung và `children` giữ thứ tự xuất hiện.

**Hiện trạng.** `list_item.content` hiện là nhãn `\item[...]` (xem mục 0), nên chưa thỏa đề xuất này. Với D2 nhóm B, TV4 không hỏng khi `content` rỗng, nhưng dedup `list`/`list_item` kém hiệu quả hơn.

**Phương án khác.** Để TV4 tự tính văn bản bao phủ từ các con: không chọn, vì như vậy TV4 phải biết quy tắc ghép/chuẩn hóa của TV2 và sẽ lệch nhau.

**Chi phí lưu trữ.** Mỗi câu nằm ở `sentence` và lại nằm trong `content` của `paragraph` cha (và `list_item`/`list` nếu có), nên `elements.content` lớn hơn tổng văn bản gốc. TODO: đo bằng benchmark thật, chưa có số.

Trạng thái: **Đề xuất — chờ nhóm xác nhận (chờ TV2).**

---

## D4. `kind` và tiền tố ID

**Quyết định.** Giữ nguyên các `kind` TV2 đang phát ra (danh sách ở mục 0), không đổi tên. Tách `section`, `subsection`, `subsubsection` thay vì một `heading` chung. Ví dụ trong đặc tả dùng `heading`, nhưng cột `kind` không bị đặc tả ràng buộc giá trị; việc tách cấp giúp D2 khỏi gộp nhầm khác cấp.

Bảng tiền tố ID, đặt thành hằng số trong code:

| kind | tiền tố |
|---|---|
| `document` | `root<version>` (D5) |
| `part`, `chapter`, `section`, `subsection`, `subsubsection`, `paragraph_heading`, `subparagraph_heading` | `h` |
| `abstract`, `appendix`, `acknowledgements` | `b` |
| `paragraph` | `p` |
| `sentence` | `s` |
| `equation` | `eq` |
| `figure` | `fig` |
| `list` | `l` |
| `list_item` | `i` |
| kind lạ | `x` kèm warning |

Tiền tố khớp ví dụ đặc tả: `root`, `h`, `s`. Kế hoạch ban đầu của TV4 dùng `s` cho section, nhưng trong đặc tả `s1` là sentence, nên bảng này tránh xung đột đó.

**Phương án khác.** Một tiền tố chung `e` cho mọi kind: không chọn, mất tính đọc được và lệch ví dụ đặc tả.

Trạng thái: **Đề xuất — chờ nhóm xác nhận (chờ TV2 đóng băng danh sách `kind`).**

---

## D5. `element_id`

**Quyết định.** `<paper_key>#<tiền_tố><n>`.

- `paper_key` lấy từ `src.utils.common.paper_key` (M1), không viết lại: `.` → `-`, `/` → `__`. Ví dụ `2310.12345` → `2310-12345`, `hep-th/9901001` → `hep-th__9901001`.
- `n` là bộ đếm riêng cho mỗi tiền tố, trong mỗi publication, tăng theo thứ tự gặp lần đầu: version tăng dần, duyệt DFS pre-order trong version.
- Ngoại lệ là root: `root<version>`, dùng số version thật chứ không dùng bộ đếm. Nhờ vậy `root2` luôn là version 2 kể cả khi version 1 không có root.

**Đặc tả.** ID bắt đầu bằng khóa publication, theo sau là phần phân biệt duy nhất trong publication; ví dụ `2310-12345#s1`, `#h1`, `#root1`.

**Tính chất.** Tất định và append-only: thêm version mới ở cuối chỉ tạo thêm số lớn hơn, không đổi ID đã có. Đổi nội dung hay thêm node ở version trước thì ID các node sau nó có thể dịch. Phải xử lý trọn publication cùng lúc (Contract §10, nguyên tắc 3).

**Phương án khác.**

- Hash nội dung làm ID: không chọn. Dài, khó đọc, và khó giữ duy nhất khi dùng khóa ngữ cảnh ở nhóm B.
- UUID: không chọn, không tất định.

Trạng thái: **Đề xuất — chờ nhóm xác nhận.**

---

## D6. Thứ tự anh em và cột `position`

**Quyết định.** Bảng `hierarchy` cuối chỉ có bốn cột (Contract §2.4: không thêm cột). Để khôi phục thứ tự anh em:

1. Các dòng của `hierarchy.parquet` được ghi theo thứ tự `(arxiv_id, version, position)`, tức DFS pre-order trong mỗi version. Thứ tự dòng chính là thứ tự cấu trúc.
2. Ngoài ra ghi thêm `hierarchy_internal.parquet` có cột `position` (số nguyên, thứ tự pre-order trong version, bắt đầu từ 1, không tính root). File nội bộ này không thuộc schema chính thức và không dùng cho nộp.

**Đặc tả.** Đặc tả cho phép tổ chức dữ liệu nội bộ linh hoạt và M2 không nộp dữ liệu. Contract §12 mục 4 yêu cầu quy tắc này được kiểm thử bằng fixture.

**Rủi ro cần nêu rõ.** Thứ tự dòng trong Parquet không phải là bảo đảm của mô hình quan hệ. Công cụ phía sau có thể xáo trộn. Vì vậy `hierarchy_internal.parquet` mới là nguồn đáng tin; thứ tự dòng chỉ là bản sao thuận tiện.

**Phương án khác.**

- Thêm cột `position` vào bảng cuối: không chọn, vi phạm "không tự thêm cột".
- Mã hóa thứ tự vào `element_id`: không chọn. ID dùng chung giữa các version nên không thể mang vị trí của từng version.

Cần cả nhóm đồng ý có thêm file nội bộ.

Trạng thái: **Đề xuất — chờ nhóm xác nhận.**

---

## D7. Element có nhiều cha và lặp trong cùng một version

**Quyết định.** Cho phép một element có nhiều dòng hierarchy trong cùng version, kể cả hai dòng giống hệt. Không ép tách. Validator đếm và báo warning:

- element có nhiều cha trong cùng version;
- dòng `(arxiv_id, version, element_id, parent_id)` lặp lại.

Ví dụ: paragraph `"Yes. Yes."` có hai con `sentence "Yes."`. Cùng một `s1`, hierarchy có hai dòng `(1, s1, p1)` giống hệt, chỉ phân biệt bằng `position` ở file nội bộ (3 và 4).

Với D2, nhiều cha chỉ có thể xảy ra ở nhóm A (câu, công thức, hình, paragraph lặp lại ở nhiều chỗ). Nhóm B luôn có đúng một cha.

**Đặc tả.** Một element được dùng lại qua nhiều version, và cha có thể khác nhau giữa các version (Contract §2.4). Đặc tả không nói về lặp trong cùng version.

**Phương án khác.**

- Chỉ giữ lần xuất hiện đầu: không chọn, mất nội dung.
- Tạo element mới cho lần lặp (ví dụ `s1`, `s2` cùng nội dung "Yes."): không chọn. Vi phạm "không lưu trùng không cần thiết".

**Lưu ý cho người dùng dữ liệu.** Ai dùng `DISTINCT` trên bốn cột sẽ mất số lần lặp. Nêu rõ trong báo cáo.

Trạng thái: **Đề xuất — chờ nhóm xác nhận.**

---

## D8. Trạng thái version, version không có TeX, chu trình

**Quyết định.**

| `ParsedDocument.status` | `root` | TV4 làm gì |
|---|---|---|
| `ok`, `partial` | có | Xuất elements và hierarchy bình thường. `partial` vẫn xuất; ghi `warnings` vào thống kê. |
| `no_tex` | `None` | Chỉ tạo một element root rỗng `<paper_key>#root<version>` (`kind=document`, `content = "Version <N> root"`, đúng cách đặc tả đặt tên ví dụ), không có dòng hierarchy. Tính vào thống kê `no_tex`. |
| `failed`, `skipped` | `None` | Không xuất gì, nhưng ghi vào thống kê kèm lý do và `warnings`. |
| `ok`/`partial` nhưng `root=None` | `None` | Mâu thuẫn dữ liệu: coi như `failed`, ghi cảnh báo rõ, không im lặng. |

Quy tắc chung:

- Không ghi đè trạng thái M1 bằng trạng thái M2 (Contract §3.6). TV4 chỉ tổng hợp trạng thái nhận được.
- Self-loop hay chu trình trong kết quả dedup là **lỗi nghiêm trọng**: `DedupError` kèm `arxiv_id`, `version`, `element_id`; publication đó đánh dấu `failed` trong thống kê. Không bao giờ xuất ra. Với D2 thì chu trình không thể xảy ra; kiểm tra này là chốt chặn.
- Mỗi lỗi có `arxiv_id`, `version`, `stage`, nguyên nhân (Contract §3.6).

**Đặc tả.** M1 giữ version không có TeX như một bản ghi rỗng; M2 phải ghi nhận là rỗng, không được bỏ qua, để thống kê coverage phản ánh đúng thực tế.

**Điểm bất định lớn nhất.** Đặc tả không nói "ghi nhận rỗng" nghĩa là dòng nào trong ba bảng. Hierarchy không thể có dòng cho version không có root (cột `element_id` và `parent_id` đều non-null), nên cách duy nhất để version đó có mặt trong dữ liệu là một element root rỗng. Phương án thay thế là chỉ ghi trong `qa_report.json` và không đụng ba bảng; phương án này an toàn hơn nhưng version đó biến mất khỏi dữ liệu.

### Ví dụ (e): một version no_tex

`2310.12345` có v1 bình thường, v2 là `no_tex` (M1: `extract_status="empty"`, `error_code="NO_TEX_SOURCE"`), v3 giống v1. v1 và v3 có cây `document → section "Intro" → paragraph "Hello." → sentence "Hello."`.

`elements`:

| element_id | kind | content | ghi chú |
|---|---|---|---|
| 2310-12345#root1 | document | Matching paper | |
| 2310-12345#root2 | document | Version 2 root | root rỗng của version no_tex |
| 2310-12345#root3 | document | Matching paper | |
| 2310-12345#h1 | section | Intro | v1 và v3 dùng chung |
| 2310-12345#p1 | paragraph | Hello. | v1 và v3 dùng chung |
| 2310-12345#s1 | sentence | Hello. | v1 và v3 dùng chung |

`hierarchy`:

| version | element_id | parent_id |
|---|---|---|
| 1 | h1 | root1 |
| 1 | p1 | h1 |
| 1 | s1 | p1 |
| 3 | h1 | root3 |
| 3 | p1 | h1 |
| 3 | s1 | p1 |

Version 2 không có dòng hierarchy. Số liệu tính tay: 6 element, 6 dòng hierarchy; số lần xuất hiện của node không phải root là 3 + 3 = 6, số element không phải root là 3 (D10). QA báo `no_tex = 1` trên tổng 3 version của publication.

Trạng thái: **Đề xuất — chờ nhóm xác nhận.**

---

## D9. Schema Parquet (các chỗ đặc tả không nói rõ)

Đặc tả không nhắc định dạng file; Contract §2.4 và §4 (công việc TV4) yêu cầu Parquet. Các lựa chọn sau là **giả định** của TV4, cần kiểm lại với trang đặc tả trước khi khóa exporter (Phase 4):

| Bảng | Cột | Kiểu Arrow | Nullable | Căn cứ |
|---|---|---|---|---|
| elements | element_id | `string` | không | đặc tả: non-null, unique |
| | arxiv_id | `string` | không | đặc tả: non-null |
| | kind | `dictionary<int32, string>` | có (không ghi null) | đặc tả: dictionary-encoded |
| | content | `string` | có (không ghi null) | đặc tả không nói |
| hierarchy | arxiv_id | `string` | không | đặc tả |
| | version | `int32` | có (không ghi null) | đặc tả chỉ nói "integer" |
| | element_id | `string` | không | đặc tả |
| | parent_id | `string` | không | đặc tả |
| bibtex | arxiv_id | `string` | không | đặc tả |
| | citation_key | `string` | không | đặc tả |
| | entry_type | `dictionary<int32, string>` | có | đặc tả: dictionary-encoded |
| | title, doi, journal, url | `string` | có | đặc tả: không nói |
| | authors | `list<string>` | có | đặc tả |
| | year | `int32` | có | đặc tả: nullable integer |

Quy ước:

- Cột nào đặc tả ghi non-null thì khai báo `nullable=False`. Cột còn lại khai báo nullable, nhưng validator coi null ở `elements.kind`, `elements.content`, `hierarchy.version` là error vì không có nghĩa.
- Chuỗi dùng `string` (không `large_string`).
- Nén cố định (đề xuất `snappy`, mặc định của pyarrow), kích thước row group cố định, không ghi timestamp.
- Chuỗi byte của file phụ thuộc phiên bản `pyarrow` (trường `created_by`). "Tất định" chỉ đúng khi cùng phiên bản pyarrow; benchmark phải ghi lại phiên bản.
- Chuyển `BibliographyResult` sang các dòng `bibtex` do TV4 làm, theo bảng ánh xạ TV3 đã cung cấp (handoff TV3, mục 4); TV3 xác nhận.

**Bổ sung sau khi cài đặt `export.py` (Phase 4).** Những điểm đặc tả và contract không quy định, TV4 đã chọn như sau:

- **Tên file** trong `output_dir`: `elements.parquet`, `hierarchy.parquet` (bốn cột), `bibtex.parquet`, và `hierarchy_internal.parquet` (thêm `position`, `int32`, non-null; chỉ dùng nội bộ, D6). Đặc tả không quy định tên file.
- **Thứ tự dòng:** `elements` theo `element_id` (so sánh chuỗi, nên `h10` đứng trước `h2`); `hierarchy` theo `(arxiv_id, version, position)`; `bibtex` theo `(arxiv_id, citation_key)`.
- **Định dạng cố định:** Parquet 2.6, data page 1.0, nén `snappy`, row group tối đa 1.048.576 dòng, có thống kê, không ghi timestamp. Chuỗi byte giống nhau chỉ đảm bảo khi cùng phiên bản `pyarrow`; môi trường hiện tại dùng `pyarrow 26.0.1`.
- **Kiểm tra đầu vào chặt hơn schema:** `kind`, `content`, `version` bắt buộc có giá trị (schema khai nullable theo đặc tả nhưng TV4 không bao giờ ghi null). Cột lạ bị từ chối. Kiểu Python phải đúng chính xác: `int` (không `bool`, không `float`) cho cột số, `str` cho cột chuỗi, `list`/`tuple` gồm `str` cho `authors`. Lý do: `pyarrow` âm thầm tách chuỗi `"Ada"` thành `['A','d','a']` và cắt `2020.5` thành `2020`.
- **Ghi an toàn:** ghi vào thư mục tạm riêng trong `output_dir`, chỉ đổi tên vào chỗ cũ sau khi cả bốn file đã ghi xong. Lỗi giữa chừng thì output cũ giữ nguyên và thư mục tạm bị xóa. Bước đổi tên từng file là nguyên tử, nhưng bốn lần đổi tên liên tiếp không nguyên tử như một khối; lỗi hệ thống đúng lúc đó có thể để lại hỗn hợp file cũ và mới.
- **Ghi theo từng publication:** `write_part` ghi `parts/<bảng>/<paper_key>.parquet`; `merge_parts` gộp mà chỉ giữ một part trong RAM. Kết quả gộp có cùng các dòng, cùng thứ tự với xuất trực tiếp, nhưng chuỗi byte khác (mỗi part là một row group).
- **`bibtex_rows(arxiv_id, entries)`** chuyển `BibEntry` của TV3 sang dòng `bibtex`; thiếu `authors` thành `[]` theo quy ước TV3, các trường khác thiếu thành null.

Trạng thái: **Đề xuất — chờ nhóm xác nhận.**

---

## D10. Thống kê dedup và mẫu số

**Quyết định.**

- Tỷ lệ dedup = `1 - unique_non_root / occurrences_non_root`.
  - Tử số: số element không phải root.
  - Mẫu số: tổng số lần xuất hiện của node không phải root, qua mọi version `ok`/`partial`.
  - Root không dedup nên loại khỏi cả hai vế.
- Mọi tỷ lệ trong `qa_report.json` có dạng `{numerator, denominator, denominator_definition, value}` (Contract §7.3). Không dùng số version có TeX làm mẫu số rồi gọi là tỷ lệ trên toàn corpus.
- Hàm phụ trả về: `total_occurrences`, `unique_elements`, `elements_by_kind`, `multi_parent_count`, `warnings` theo version và trạng thái từng version.

Ví dụ số liệu: (a) cho tỷ lệ `1 - 6/8 = 0,25`; (e) cho `1 - 3/6 = 0,5`. Cả hai tính tay, chưa đo trên dữ liệu thật.

Trạng thái: **Đề xuất — chờ nhóm xác nhận.**

---

## Câu hỏi cho các thành viên

### TV2 (kind, content)

1. Danh sách `kind` ở mục 0 có chính thức không (ví dụ `list` / `list_item` / `paragraph_heading`)? Mong TV2 công bố một hằng số dùng chung và đóng băng trước khi TV4 làm Phase 3.
2. `list_item.content` hiện là nhãn `\item[...]` (thường rỗng) và `list.content` ghép từ các nhãn đó. Có thể đổi thành toàn văn mà node bao phủ không (D3)?
3. `abstract`, `appendix`, `acknowledgements` đang có `content` hằng số. TV2 có muốn đổi không, hay cứ giữ để TV4 xử lý bằng khóa ngữ cảnh?
4. `figure` dùng cho cả hình lẫn bảng. Có cần tách `table` không?
5. Root `content` là tiêu đề bài. TV2 có bảo đảm khác rỗng không, hay TV4 cần dự phòng?
6. Cấu trúc LaTeX không parse được hoàn toàn (Contract §12 mục 7): TV2 đưa vào `content` hay bỏ? TV4 chỉ nhận những gì có trong cây.

### TV1 (trạng thái version)

1. Skeleton ở Contract §3.5 chỉ truyền `doc.status == "ok"` sang TV4. TV1 lại gán `partial` mỗi khi có warning, nên bỏ `partial` sẽ làm mất nhiều version. Đề nghị TV1 truyền mọi version (kể cả `partial`, `no_tex`, `failed`, `skipped`) vào `deduplicate_elements`; TV2 đã trả `ParsedDocument(root=None, status=...)` nên không cần đổi contract.
2. Số liệu QA về root detection và include resolution: TV1 lấy từ đâu? Đề nghị quy ước mã cảnh báo trong `GatheredDocument.warnings` để TV4 đếm.
3. TV4 gọi hàm thống kê phụ cùng `deduplicate_elements` để lấy trạng thái từng version. TV1 xác nhận pipeline có chỗ gọi.

### TV3 (bibliography)

1. Số entry trước dedup (tổng `extract_bibliography` theo version) và sau dedup (`len(entries)`): ai gọi và gửi cho TV4 để ghi vào `qa_report.json`?
2. Citation coverage: TV4 tính theo `\cite{...}` còn trong `content` so với `bibtex.citation_key`. TV3 xác nhận `key_mapping` đã áp dụng đầy đủ trước khi TV2 chuẩn hóa.

### Cả nhóm

1. Đồng ý thêm `hierarchy_internal.parquet` có cột `position` (D6).
2. Layout thư mục: branch TV1 dùng `m2/src/parsing/` và `m2/tests/parsing/`, branch TV2 và TV3 dùng `m2/src/parsing/` và `m2/tests/parsing/`, còn `origin/main` đã tái cấu trúc thành `m1/` và chưa có `m2/`. Cần chốt một layout; TV4 sẽ đặt file theo layout đó.
3. Đồng ý khóa dedup hai nhóm ở D2 thay cho `(kind, content)` thuần.
4. Đồng ý ghi nhận version `no_tex` bằng một element root rỗng (D8).
5. `src/requirements.txt` (đặc tả yêu cầu) cần thêm `pyarrow`, `pandas`, `psutil`.

---

## D11. Quy ước đo benchmark (Phase 6)

Đặc tả yêu cầu: thời gian chạy end-to-end của parser, thời gian trung bình mỗi publication, RAM tối đa, dung lượng output; chạy chính thức trên Google Colab chỉ CPU. Các quy ước sau do TV4 chọn (đặc tả không quy định):

- **`end_to_end`** = xử lý (pipeline của TV1, hoặc chỉ dedup ở chế độ `tv4_only`) + ghi part từng publication + gộp bảng. **Không** gồm validation và tính checksum; hai bước này đo thành stage riêng.
- **Thời gian:** `time.perf_counter`, cộng dồn qua nhiều lần vào cùng một stage.
- **RAM tối đa:** luồng nền lấy mẫu RSS của tiến trình và các tiến trình con mỗi khoảng 50 ms (dùng lại `process_tree_rss_bytes` của M1, nên cùng định nghĩa với benchmark M1). Lấy mẫu có thể bỏ lỡ đỉnh ngắn hơn khoảng lấy mẫu, nên `process_lifetime_max_rss_bytes` (`ru_maxrss`) được ghi kèm để đối chiếu. Thiếu `psutil` thì dùng `ru_maxrss` và đánh dấu `peak_rss_source`.
- **Hai chế độ:** `pipeline` (gọi `process_publication` của TV1) và `tv4_only` (chỉ dedup, export, merge trên tài liệu do người gọi đưa vào, `data_source = caller_documents`). Kết quả luôn ghi rõ chế độ; số của `tv4_only` không được trình bày như số của cả pipeline.
- **Tỷ lệ:** mọi tỷ lệ có `numerator`, `denominator`, `denominator_definition`, `value`. `versions_per_second` chỉ đếm version đã xuất (không tính version `failed`/`skipped`); `mean_seconds_per_publication` chia cho số publication đã thử (kể cả lỗi).
- **Không ghi đè, không cache:** mỗi lần chạy cần một nhãn mới; thư mục làm việc `<output_dir>/<label>` và file `benchmark_<label>.json` đều không được tồn tại trước. Nhãn trùng thì báo `FileExistsError`.
- **So sánh hai lần chạy:** chỉ hợp lệ khi cùng hash danh sách ID, cùng chế độ, cùng môi trường (nền tảng, phiên bản Python, số CPU, RAM, phiên bản `pyarrow`) và output giống hệt từng byte; `check_comparable` liệt kê các lý do không so sánh được.
- **Quyền riêng tư:** file kết quả không chứa đường dẫn tuyệt đối hay tên người dùng, chỉ tên thư mục chạy và tên file danh sách ID.
- **Chưa có số liệu:** TODO. Chưa chạy trên dữ liệu M1 thật và chưa chạy trên Colab; mọi số liệu thử nghiệm đến nay (nếu có) lấy từ dữ liệu tổng hợp.

---

## D12. Trạng thái từng version truyền sang QA (Phase 7)

`qa_report.build_qa_report(output_dir, version_status, benchmark_path)` cần mỗi version pipeline đã xử lý một dòng. Đề xuất gửi TV1 xác nhận (hàm `qa_report.status_row(gathered, parsed, has_tex=..., error=...)` dựng sẵn dòng này từ `GatheredDocument` và `ParsedDocument` để TV1 dùng hoặc thay thế):

| Khóa | Kiểu | Ý nghĩa |
|---|---|---|
| `arxiv_id`, `version` | str, int ≥ 1 | định danh; mỗi cặp chỉ xuất hiện một lần |
| `status` | `ok` / `partial` / `no_tex` / `failed` / `skipped` | trạng thái cuối cùng của version (Contract §3.6) |
| `has_tex` | bool hoặc None | nguồn M1 có TeX hay không |
| `root_found` | bool hoặc None | gathering xác định được root |
| `include_problems` | int hoặc None | số cảnh báo include chưa giải quyết, vòng lặp, đường dẫn không an toàn |
| `stage`, `reason` | str hoặc None | nơi dừng và nguyên nhân của version `failed`/`skipped` |

Quy ước tính tỷ lệ trong báo cáo (Contract §7.3):

- Mẫu số của mọi tỷ lệ theo trạng thái là **tất cả version có dòng trong `version_status`**, có hay không có TeX.
- `ok_among_versions_with_tex` dùng mẫu số là số version có TeX và định nghĩa của nó ghi rõ đây không phải tỷ lệ thành công trên toàn corpus.
- Số liệu chưa cung cấp (ví dụ số entry bibtex trước dedup của TV3, hoặc `include_problems` chưa biết) được ghi `null` kèm giải thích, không suy diễn.

**Phát hiện khi chạy trên dữ liệu M1 thật (lượt pilot trên run M1 `A_PIPELINE_pilot_24120085_01`, 40 publication, chi tiết trong mô tả PR):** skeleton ở Contract §3.5 chỉ chuyển sang bước sau những version có `status == "ok"`. Ở pilot có 8 trên 41 version xuất được ở trạng thái `partial` (cả tám do cảnh báo `MISSING_DEPENDENCY` của gathering); lọc theo `ok` sẽ làm mất chúng.
