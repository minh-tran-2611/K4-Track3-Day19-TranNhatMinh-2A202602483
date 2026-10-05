# Báo cáo Day 19 — Flat RAG vs GraphRAG

**Họ tên:** Trần Nhật Minh  **MSSV:** 2A202602483  **Ngày:** 2026-10-05

> Kỳ vọng và thang điểm: `SUBMISSION.md`. Mọi số liệu phải khớp với `ket_qua_benchmark_kg.txt`. Bản thiết kế ontology nộp riêng ở `report/ONTOLOGY.md`.

**Cấu hình chạy:** chat `gemini:gemini-3.5-flash-lite`, embedding `gemini:gemini-embedding-001`, `top_k=3`, `chunk_size=800`, 176 chunk. Ontology tự thiết kế (`KG_ONTOLOGY=own`, mặc định). File của ontology gợi ý để so sánh: `ket_qua_benchmark_kg.hint.txt` (`KG_ONTOLOGY=hint`), cùng provider, cùng model.

## 1. Chi phí (10 điểm)

Từ `ket_qua_benchmark_kg.txt`:

```
== Indexing (one-off)
pipeline  calls    in_tok  out_tok       USD  seconds
flat        176         0        0   0.00000    106.2
graph       196     38399     6756   0.02841    190.7

== Querying (mean per question)
pipeline  recall  judge   in_tok  out_tok       USD  seconds
flat        0.51   1.50      696       72   0.00039     2.06
graph       1.00   2.00     3985      199   0.00169     2.48
```

| Chỉ số | Flat | Graph | Graph / Flat |
| --- | --- | --- | --- |
| Indexing USD | 0.00000 (*) | 0.02841 | — (Flat = 0 do thiếu giá embedding) |
| Indexing giây | 106.2 | 190.7 | ×1.80 |
| Mỗi câu: USD | 0.00039 | 0.00169 | ×4.33 |
| Mỗi câu: giây | 2.06 | 2.48 | ×1.20 |
| Mỗi câu: in_tok | 696 | 3985 | ×5.73 |

(*) Gemini OpenAI-compatible endpoint không trả `usage` cho embedding, và `gemini-embedding-001` không có trong bảng giá `src/llm.py`, nên 176 lần embed được ghi 0 token / 0 USD. Chi phí embed là như nhau cho cả 2 pipeline (Graph dùng lại đúng index đó), nên không ảnh hưởng phần chênh lệch.

**Lưu ý về thời gian:** project dùng gói miễn phí Gemini (15 request/phút). `src/llm.py` tự đợi khi gặp lỗi 429, và thời gian chờ đó **nằm trong** số giây đo bằng đồng hồ tường. Lần chạy này có một lần đợi 40 s trong giai đoạn indexing, nên số giây indexing cao hơn thực tế. Mỗi câu hỏi thì không bị đợi (lần 429 thứ hai rơi vào lời gọi judge, không được tính vào pipeline).

**Chi phí tăng thêm đến từ đâu?**
> Indexing: phần tăng thêm đến **hoàn toàn từ 20 lần gọi LLM trích xuất 20 bài báo** (196 − 176 = 20 lần gọi, 38 399 token vào, 6 756 token ra, 0,028 USD). Phần luật được tách bằng regex nên không tốn token nào. Mỗi câu hỏi: prompt GraphRAG dài gấp ~5,7 lần vì có thêm các dữ kiện graph, chủ yếu là **nguyên văn các khoản luật** (khoản 1 + khoản nặng nhất + khoản khớp ngưỡng khối lượng) và tóm tắt vụ việc. Câu trả lời cũng dài hơn (199 so với 72 token) vì có thêm số Điều/khoản.
>
> Ước tính: tổng chi phí cho N câu hỏi là Flat ≈ 0,00039·N USD, Graph ≈ 0,02841 + 0,00169·N USD. Với 100 câu: 0,04 USD so với 0,20 USD. Graph **không bao giờ hòa vốn về tiền**, vì mỗi câu đều đắt hơn. Cái mua được là độ đúng: trên 4 câu cần nối 2 KB (Q3–Q6), recall trung bình Flat là 0,27, còn Graph là 1,00.
>
> So với ontology gợi ý (`ket_qua_benchmark_kg.hint.txt`): indexing đắt hơn 11% (0,02841 so với 0,02561 USD, do prompt trích xuất dài hơn), nhưng mỗi câu hỏi **rẻ hơn 18%** (0,00169 so với 0,00207 USD) và prompt ngắn hơn 29% (3 985 so với 5 637 token), vì Cypher chọn đúng khoản theo ngưỡng thay vì kéo mọi khoản có nhắc tên chất.

## 2. Từng câu hỏi (10 điểm)

| Câu | Loại | Flat recall / judge | Graph recall / judge | Thắng | Vì sao (1 câu) |
| --- | --- | --- | --- | --- | --- |
| Q1 | single-hop-law | 1.00 / 2 | 1.00 / 2 | Hòa | Định nghĩa nằm gọn trong 1 chunk Điều 2 Luật PCMT; graph chỉ thêm số khoản. |
| Q2 | single-hop-news | 1.00 / 2 | 1.00 / 2 | Hòa | Tên 2 bị cáo lãnh án tử hình nằm trong 1 đoạn tin; Flat còn rẻ hơn ×7. |
| Q3 | cross-kb | 0.33 / 1 | 1.00 / 2 | Graph | Flat có "36 tháng" nhưng tự nói "không đủ thông tin" về Điều; Graph đi `Person-ACCUSED_OF->Crime<-DEFINES-Article` tới Điều 251 khoản 1. |
| Q4 | cross-kb | 0.33 / 1 | 1.00 / 2 | Graph | Graph lấy được khoản nặng nhất Điều 255 (khoản 4, chung thân), khoản này không nhắc chất nào nên không có trong chunk tin. |
| Q5 | cross-kb-multi-hop | 0.40 / 1 | 1.00 / 2 | Graph | Cạnh `THRESHOLD` so 9 600 g MDMA với ngưỡng ≥100 g, cho ra đúng điểm b khoản 4 Điều 250. |
| Q6 | aggregation | 0.00 / 2 | 1.00 / 2 | Graph (theo recall) | Graph gom mọi `Case-[:INVOLVES]->MDMA` trên toàn graph; Flat chỉ thấy 3 chunk. Điểm judge 2 của Flat là chấm sai (xem E4). |

**Quy luật:** câu **single-hop** (đáp án nằm trong 1 đoạn, Q1, Q2) thì hai bên hòa, Flat rẻ hơn. Câu cần **nối 2 KB** (Q3, Q4, Q5) thì Flat luôn tự nhận "không đủ thông tin" ở nửa luật, còn Graph đúng đủ. Câu **gom nhiều tài liệu** (Q6) vượt quá top-k của Flat, Graph thắng nhờ truy vấn tổng hợp, nhưng cũng là nơi Graph dễ thêm vụ sai (E5).

So với ontology gợi ý: khác biệt rõ nhất ở **Q4** (gợi ý 0.67 / 1, ontology mới 1.00 / 2, xem E2 và ONTOLOGY.md mục 7).

## 3. Phân tích lỗi (20 điểm)

### Lỗi E2: Thiếu ngữ cảnh luật — câu hỏi "tối đa" bị trả lời bằng khung cơ bản (ontology gợi ý)

- **Hiện tượng:** Với ontology gợi ý, GraphRAG trả lời Q4 rằng mức tù tối đa cho "Hoàng Nato" là **07 năm**, trong khi đúng phải là tù 20 năm hoặc chung thân (Điều 255 khoản 4). Graph có đủ cả 5 khoản của Điều 255.
- **Bằng chứng:** `ket_qua_benchmark_kg.hint.txt`, Q4 graph (recall 0.67, judge 1):

```
- **Mức phạt tù tối đa:** Theo **Điều 255 BLHS - Tội tổ chức sử dụng trái phép chất ma túy (khoản 1)**, ...
  thì bị phạt tù từ 02 năm đến 07 năm. Mức phạt tù tối đa theo điều khoản này là **07 năm**
  (ngữ cảnh không đề cập các khoản nặng hơn của Điều 255).
```

Khoản 4 Điều 255 không nhắc tới chất nào, nên quy tắc lọc "khoản 1 + khoản `MENTIONS` chất mà vụ `INVOLVES`" không bao giờ chọn nó (graph hiện tại, cạnh `THRESHOLD` thay cho `MENTIONS`):

```cypher
MATCH (:Article {id:'Điều 255 BLHS'})-[:HAS_CLAUSE]->(cl)
RETURN cl.number AS k, cl.max_level AS lvl, cl.severity AS sev,
       size([(cl)-[:THRESHOLD]->() | 1]) AS n_thr ORDER BY k
```

```
k=1 lvl='tù'         sev=7.0  n_thr=0
k=2 lvl='tù'         sev=15.0 n_thr=0
k=3 lvl='tù'         sev=20.0 n_thr=0
k=4 lvl='chung thân' sev=50   n_thr=0   <- không nhắc chất nào, bị bỏ sót
k=5 lvl=''           sev=0    n_thr=0
```

- **Nguyên nhân:** nằm ở **Cypher KG-3** và ở **thiết kế ontology**. Ontology gợi ý chỉ lưu `penalty` dạng chuỗi, nên không truy vấn được "khoản nặng nhất". Quy tắc lọc theo chất là một heuristic hợp lý cho câu hỏi "khung nào áp dụng", nhưng sai cho câu hỏi "tối đa bao nhiêu". LLM trả lời trung thực theo ngữ cảnh, nhưng ngữ cảnh thiếu.
- **Đề xuất sửa (đã làm trong ontology mới, D2):** thêm `Clause.max_level` và `Clause.severity` (tách bằng regex từ câu "thì bị phạt …"), và KG-3 luôn thêm khoản có `severity` lớn nhất của mỗi Điều tìm được. Kết quả: Q4 graph tăng lên **recall 1.00 / judge 2** (`ket_qua_benchmark_kg.txt`). Đánh đổi: mỗi Điều thêm ~1 khoản vào prompt. Dù vậy tổng prompt vẫn giảm (5 637 xuống 3 985 token), vì đồng thời bỏ được các khoản chỉ khớp tên chất mà không khớp khối lượng.

### Lỗi E5: LLM lệch với dữ liệu — "100g ma túy tổng hợp" thành "100g MDMA" và sinh khoản luật sai

- **Hiện tượng:** Trong câu trả lời Q4 và Q6 của ontology mới, GraphRAG nhắc tới "Điều 251 BLHS khoản 4 điểm b (khối lượng từ 100g trở lên)" cho chuyên án Hoàng Nato, và liệt kê 2 vụ Hoàng Nato vào danh sách vụ có **MDMA**. Bài báo không hề nói có MDMA 100g.
- **Bằng chứng:** bài gốc `news-100260920221957595.md` dòng 34: *"Tang vật thu giữ gồm hơn 1.000 đầu pod chill chứa ma túy etomidate, **khoảng 100g ma túy tổng hợp các loại**"*. Trong graph:

```cypher
MATCH (k:Case)-[i:INVOLVES]->(s) WHERE k.name CONTAINS 'Nato'
RETURN k.id AS id, s.name AS s, i.amount AS amount
```

```
news-100260920221957595#1  Ketamine         'khoảng 100g'
news-100260920221957595#1  MDMA             'khoảng 100g'       <- cùng 100g gán cho 2 chất
news-100260922111804786#1  Methamphetamine  'khoảng 100g ma túy tổng hợp các loại'
news-100260924095400982#1  etomidate        '10 ống'
news-100260925144412498#1  MDMA             'hơn 1.000 đầu pod chill chứa etomidate cùng lượng lớn ma túy tổng hợp'
```

`ket_qua_benchmark_kg.txt`, Q4 graph: *"Ngoài ra, trong dữ kiện liên quan đến các vụ án ma túy khác của chuyên án có nhắc tới … **Điều 251 BLHS** khoản 4 điểm b (khối lượng từ 100g trở lên)…"*. Q6 graph liệt kê mục 4 và 5 là các vụ Hoàng Nato.

- **Nguyên nhân:** nằm ở **prompt trích xuất** và ở **chính thiết kế D1** của mình. (1) LLM biến "ma túy tổng hợp các loại" (không nêu tên chất) thành các chất cụ thể (MDMA, Ketamine, Methamphetamine), và chép cùng một khối lượng tổng cho từng chất. (2) Ontology mới tin vào `amount_g` để so ngưỡng: 100 g rơi đúng ngưỡng "MDMA ≥ 100 g", nên Cypher sinh ra dữ kiện "thuộc khoản 4". Một lỗi trích xuất nhỏ biến thành một dữ kiện pháp lý sai trông rất chắc chắn. (3) LLM trả lời thì đưa dữ kiện này vào dù câu hỏi không cần. Với ontology gợi ý, lỗi này ít nguy hiểm hơn vì không có ngưỡng để khớp sai.
- **Đề xuất sửa:**
  - Prompt trích xuất: thêm quy tắc "chỉ ghi tên chất nếu bài nêu đích danh; 'ma túy tổng hợp', 'các loại' thì để `name` rỗng", và "khối lượng chung cho nhiều chất thì không chia cho từng chất". Rẻ, không tốn thêm lời gọi.
  - Trong code: chỉ tính `amount_g` khi chuỗi `amount` không chứa "các loại / tổng hợp / tổng", và khi một vụ có ≥ 2 chất cùng chuỗi `amount` thì đặt `amount_g = null`. Đánh đổi: có thể bỏ mất vài ngưỡng đúng.
  - Trong KG-3: chỉ đưa dữ kiện ngưỡng của các vụ là seed trực tiếp, không đưa của các vụ "hàng xóm".

### Lỗi E1: Cầu nối gãy — 3 `Case` không có `CHARGED_WITH`

- **Hiện tượng:** 3 trên 15 vụ không nối sang được KB luật.
- **Bằng chứng:**

```cypher
MATCH (k:Case) WHERE NOT (k)-[:CHARGED_WITH]->() RETURN k.id AS id, k.name AS name
```

```
news-100260918220613301#1  Vụ bắt giữ Nguyễn Minh Đức
news-100260926112415229#1  Vụ tông cảnh sát giao thông tại An Giang
news-100261002184934505#1  Vụ triệt phá chuyên án A3-626p
```

Mở bài gốc theo `doc_id`:
  - `news-100260926112415229`: tài xế bị khởi tố về **"chống người thi hành công vụ"**, chỉ *sử dụng* ma túy. Tội này không nằm trong Chương XX BLHS, nên **không nối là đúng**: graph không có Điều luật để nối.
  - `news-100260918220613301` ("Công nhân nói không với ma túy…") là **bài tuyên truyền**. "Vụ bắt giữ Nguyễn Minh Đức" lấy từ dòng 42 (*"Công an tỉnh Ninh Bình vừa bắt giữ khẩn cấp Nguyễn Minh Đức, tức 'Đức Cộng' - một giang hồ mạng…"*), là một đoạn tin liên quan mà crawler lấy kèm vào cuối bài, không nói gì về ma túy. Đây là **lỗi crawl + lỗi trích xuất**: vụ này không nên tồn tại.
  - `news-100261002184934505` ("Bộ đội Biên phòng… hội nghị") là **bài hội nghị**. "Chuyên án A3-626p" là chuyên án **mua bán người** được trao thưởng, không phải vụ ma túy. Prompt yêu cầu trả `{"cases": []}` cho loại bài này, nhưng LLM vẫn tạo "vụ".
- **Nguyên nhân:** **crawl** (lấy cả phần tin liên quan cuối bài), **prompt trích xuất** (LLM không tuân thủ quy tắc bỏ qua bài không có vụ cụ thể), và giới hạn của **phạm vi KB luật** (chỉ có Chương XX) cho trường hợp An Giang. Cùng nguyên nhân crawl cũng làm bài về Lê Minh Thành (`news-100260918080821054`) sinh thêm một vụ Cái Quang Huy (dòng 92 là tóm tắt một tin khác).
- **Đề xuất sửa:** (1) Trong code, bỏ những `Case` không có `charges` **và** không có người nào có `charge`. Như vậy vừa xóa được vụ giả, vừa không mất vụ thật nào trong benchmark. Không tốn thêm token. (2) Muốn giữ vụ An Giang, có thể thêm node `Crime` "ngoài phạm vi" để ghi nhận tội không có Điều luật trong KB. Graph to hơn một chút nhưng không còn "cầu gãy câm".

### Lỗi E4: Phép đo sai — Q6 Flat: recall 0.00 nhưng judge 2

- **Hiện tượng:** Câu Q6 của Flat RAG bị recall chấm 0 điểm nhưng judge chấm điểm tối đa.
- **Bằng chứng:** `ket_qua_benchmark_kg.txt`:

```
--- Q6 [aggregation] flat recall=0.00 judge=2 2.03s
1. Vụ việc [1]: ... MDMA (khối lượng gần 4,3kg) liên quan đến Đạt và Huy.
2. Vụ việc [2]: Công an bắt quả tang Thành mang 5 viên nén màu trắng ... MDMA.
3. Vụ việc [3]: ... MDMA (khối lượng hơn 5,3kg).
```

`must_include` của Q6 là `["Cái Quang Huy", "Lê Minh Thành", "Pháp y tâm thần"]`; đáp án chuẩn gồm 3 vụ: Cái Quang Huy, Lê Minh Thành, Viện Pháp y tâm thần.

- **Phân tích, bên nào đúng:** **cả hai đều sai một phần.**
  - **Recall quá khắt khe:** câu trả lời có nhắc vụ Huy và vụ Thành (chỉ gọi tên ngắn "Huy", "Thành"), nhưng không khớp chuỗi họ tên đầy đủ nên mất trọn điểm. Đúng ra phải được khoảng 2/3.
  - **Judge quá dễ dãi:** câu trả lời **thiếu hẳn vụ Viện Pháp y tâm thần**, và vụ 1 với vụ 3 thực ra là cùng một vụ Cái Quang Huy (2 lần vận chuyển) bị kể thành 2 vụ. Theo thang "2 = đúng và đủ các ý chính" thì phải là 1. Ngược lại, ở lần chạy ontology gợi ý, Flat Q6 có cùng recall 0.00 nhưng judge chấm 1, nghĩa là **judge không ổn định giữa các lần chạy**.
- **Nguyên nhân:** **phép đo**. `keyword_recall` so khớp chuỗi con nguyên văn. Judge là cùng một LLM rẻ (`gemini-3.5-flash-lite`) chấm với temperature 0 nhưng vẫn dao động, và có xu hướng thưởng cho câu trả lời "trông đầy đủ".
- **Đề xuất sửa:** (1) Recall: cho mỗi từ khóa một danh sách biến thể (`["Cái Quang Huy", "Huy"]`) hoặc so khớp sau khi bỏ dấu. Đánh đổi: dễ đếm nhầm hơn. (2) Judge: đưa `must_include` vào prompt chấm và yêu cầu liệt kê từng ý có/thiếu trước khi cho điểm. Dùng model mạnh hơn hoặc chấm 3 lần lấy trung vị, tốn thêm khoảng 3 lần chi phí judge. Kết luận về Q6 trong báo cáo này vì vậy dựa trên **việc đọc câu trả lời**, không dựa trên một con số duy nhất.

## 4. Kết luận (5 điểm)

Khi nào nên dùng KG, khi nào Flat RAG là đủ?

> **Flat RAG là đủ** khi đáp án nằm gọn trong một đoạn văn: Q1 và Q2 hai bên đều đạt recall 1.00 / judge 2, nhưng Flat rẻ hơn khoảng 4 lần mỗi câu (0,00039 so với 0,00169 USD) và không tốn chi phí dựng graph (0,028 USD, khoảng 85 giây gọi LLM cho 20 bài).
>
> **Nên dùng KG** khi câu hỏi phải **nối các nguồn có cấu trúc khác nhau qua một khóa chung**: ở đây là "người trong tin, tội danh, Điều/khoản trong luật". Trên Q3–Q5, Flat chỉ đạt recall 0,33–0,40 và luôn tự nhận "không đủ thông tin" ở nửa luật; GraphRAG đạt 1.00 / 2 cả ba câu. KG cũng thắng ở câu **tổng hợp** (Q6: recall 0.00 lên 1.00), vì top-k cố định của Flat không thể gom đủ các vụ.
>
> **Điều kiện để KG đáng tiền:** (1) có một loại thực thể cầu nối mà cả hai nguồn nhắc tới và chuẩn hóa được (tội danh); (2) có ít nhất một nguồn đủ đều để trích xuất bằng regex (luật), nên graph rẻ và ổn định; (3) tỉ lệ câu hỏi multi-hop đủ lớn. Trong bộ 6 câu này là 4/6, và chênh lệch độ đúng (judge trung bình 1,50 lên 2,00) đáng giá hơn mức chi phí gấp khoảng 4 lần mỗi câu.
>
> **Ontology quyết định phần lớn kết quả:** cùng dữ liệu và model, chỉ đổi ontology (ngưỡng khối lượng, mức phạt có cấu trúc, tội theo người) đã nâng recall Graph từ 0,94 lên 1,00, judge từ 1,83 lên 2,00, và giảm 29% token mỗi câu. Đổi lại, graph càng "thông minh" thì lỗi trích xuất càng thành lỗi pháp lý trông rất chắc chắn (E5), nên vẫn phải đọc từng câu trả lời chứ không chỉ nhìn con số trung bình.

## 5. Tự kiểm (5 điểm)

```
$ python -m pytest tests/ -q
................................................                         [100%]
48 passed in 0.14s

$ python bench_kg.py --check
[OK] Dữ liệu: 18 điều luật, 20 bài báo
[OK] KG-1 link_entity
[OK] Neo4j kết nối được
[provider] chat = gemini:gemini-3.5-flash-lite | embedding = gemini:gemini-embedding-001
[OK] KG-2 build_graph: 148 node / 351 cạnh, đường xuyên 2 KB dài 2 cạnh
[OK] KG-3 context: 26 dữ kiện, có Điều 251
[OK] KG-4 GraphRAGAgent.answer
[OK] Chi phí check: 1 lần gọi LLM, $0.00279. Graph nhỏ (luật + 1 bài) vẫn còn trong Neo4j để bạn xem; chạy --judge để dựng graph đầy đủ.
```

(Trên Windows có bật Smart App Control nên `pytest.exe` bị chặn; dùng `python -m pytest`, kết quả tương đương.)

Ảnh Neo4j: `report/img/kg_count.png`, `report/img/kg_cross_kb.png`, `report/img/kg_my_case.png`.
Người đã chọn cho `kg_my_case.png`: **Cái Quang Huy**

## Vấn đề gặp phải (không tính điểm)

- `py -3.11` không chạy được vì máy chỉ có Python 3.12. Đã dùng venv Python 3.12; mọi gói trong `requirements.txt` chạy bình thường.
- Smart App Control chặn `pytest.exe` ("An Application Control policy has blocked this file"). Đã gọi qua `python -m pytest`.
- `gemini-2.5-flash-lite` trả 404 ("no longer available to new users"). Đã đặt `GEMINI_CHAT_MODEL=gemini-3.5-flash-lite` trong `.env` và thêm giá (0,30 / 2,50 USD mỗi 1M token) vào `PRICES_PER_M` trong `src/llm.py`.
- Gói miễn phí Gemini giới hạn 15 request/phút nên benchmark bị `429 RESOURCE_EXHAUSTED`. Đã thêm cơ chế tự đợi theo "retry in Xs" rồi thử lại (`MeteredLLM._retrying` trong `src/llm.py`). Thời gian đợi có nằm trong số giây đo được (xem mục 1).
