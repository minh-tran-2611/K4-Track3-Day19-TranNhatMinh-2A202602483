"""Knowledge Graph (Neo4j) + GraphRAG over two drug-topic knowledge bases.

Contract (fixed — bench_kg.py and the tests rely on it):
    link_entity(name, known)                       -> one of `known` or None          (TODO KG-1)
    build_graph(graph, law_docs, news_docs, llm_fn)   load both KBs into Neo4j      (TODO KG-2)
        every node created from ONE document carries the property `doc_id`
    Neo4jGraph.context(question, doc_ids)         -> list[str] facts               (TODO KG-3)
    GraphRAGAgent.answer(question, top_k)         -> str                           (TODO KG-4)

Everything else in this file is a HINT: one possible ontology (below). Use it as is, change it,
or design your own — your own ontology + report/ONTOLOGY.md earns the bonus (see SUBMISSION.md).

Suggested ontology (Crime is the bridge between the law KB and the news KB):

    (:Article {id, title, law, doc_id})-[:DEFINES]->(:Crime {name})
    (:Article)-[:HAS_CLAUSE]->(:Clause {id, number, penalty, text})-[:MENTIONS]->(:Substance {name})
    (:Case {name, summary, date, doc_id})-[:CHARGED_WITH]->(:Crime)
    (:Case)-[:INVOLVES {amount}]->(:Substance)
    (:Case)-[:LOCATED_IN]->(:Location {name})
    (:Person {name, aliases})-[:INVOLVED_IN {role, sentence, charge}]->(:Case)
"""

from __future__ import annotations

import difflib
import json
import os
import re
from pathlib import Path
from typing import Any, Callable

from .models import Document
from .store import EmbeddingStore

# Canonical substance names: the ones BLHS Chương XX lists, plus common ones in Vietnamese news.
SUBSTANCES = ["Heroine", "Cocaine", "Methamphetamine", "Amphetamine", "MDMA", "XLR-11", "Ketamine",
              "cần sa", "thuốc phiện", "côca"]
CLAUSE_START = re.compile(r"^(\d+)\.\s", re.MULTILINE)
FOOTNOTE = re.compile(r"\[\d+\]")

def load_markdown_docs(folder: str | Path) -> list[Document]:
    """Read crawler output (.md with a flat `key: "value"` front matter) into Documents."""
    docs = []
    for path in sorted(Path(folder).glob("*.md")):
        raw = path.read_text(encoding="utf-8")
        _, front, body = raw.split("---", 2)
        metadata = {k: json.loads(v) for k, v in re.findall(r'^(\w+): (".*")$', front, re.MULTILINE)}
        docs.append(Document(id=metadata.get("doc_id", path.stem), content=body.strip(), metadata=metadata))
    return docs

def normalize_crime(name: str) -> str:
    """'Tội Mua bán trái phép chất ma túy' -> 'mua bán trái phép chất ma túy'."""
    name = re.sub(r"\s+", " ", name.strip().strip("\"'“”").lower())
    return name.removeprefix("tội ").strip()

def link_entity(name: str, known: list[str], normalize: Callable[[str], str] = normalize_crime) -> str | None:
    """Map a free-text mention (e.g. a charge written by a journalist) onto one canonical name in `known`."""
    target = normalize(name or "")
    if not target:
        return None
    by_normalized = {normalize(k): k for k in known}
    if target in by_normalized:
        return by_normalized[target]
    close = difflib.get_close_matches(target, list(by_normalized), n=1, cutoff=0.8)
    return by_normalized[close[0]] if close else None

def find_substances(text: str) -> list[str]:
    lowered = text.lower()
    return [name for name in SUBSTANCES if name.lower() in lowered]

# ----------------------------------------------------------------------------------------------
# HINT — suggested ontology: extraction helpers
# ----------------------------------------------------------------------------------------------

def parse_law_article(doc: Document) -> dict[str, Any]:
    """Deterministic (regex) extraction for one 'Điều' — law text is regular enough to skip the LLM."""
    article_id = doc.metadata["article"]                       # "Điều 251 BLHS"
    title = doc.metadata["title"].split(". ", 1)[-1]           # "Tội mua bán trái phép chất ma túy"
    body = FOOTNOTE.sub("", doc.content)
    starts = list(CLAUSE_START.finditer(body))
    clauses = []
    for index, start in enumerate(starts):
        end = starts[index + 1].start() if index + 1 < len(starts) else len(body)
        text = body[start.start():end].strip()
        first_line = text.splitlines()[0]
        penalty = re.search(r"\bbị ((?:phạt|tù|cảnh cáo).+?)(?::|$)", first_line)
        clauses.append({
            "id": f"{article_id} khoản {start.group(1)}",
            "number": int(start.group(1)),
            "penalty": penalty.group(1).rstrip(".") if penalty else "",
            "text": text,
            "substances": find_substances(text),
        })
    return {
        "id": article_id,
        "law": doc.metadata.get("law", ""),
        "title": title,
        "doc_id": doc.id,
        "crime": normalize_crime(title) if title.startswith("Tội ") else None,
        "clauses": clauses,
    }

NEWS_EXTRACTION_PROMPT = """Bạn trích xuất knowledge graph từ một bài báo tiếng Việt về ma túy.
Chỉ dùng thông tin có trong bài. Trả về JSON đúng dạng:
{{"cases": [{{
  "name": "tên ngắn của vụ việc, ví dụ: Vụ mua bán 36kg ma túy tại TP.HCM",
  "summary": "1-2 câu tóm tắt",
  "date": "ngày xảy ra/xét xử nếu có, dạng YYYY-MM-DD hoặc chuỗi rỗng",
  "location": "tỉnh/thành phố, chuỗi rỗng nếu không rõ",
  "charges": ["tội danh, BẮT BUỘC chọn đúng nguyên văn từ DANH SÁCH TỘI DANH"],
  "substances": [{{"name": "tên chất, dùng tên chuẩn trong DANH SÁCH CHẤT nếu khớp", "amount": "khối lượng nếu có"}}],
  "people": [{{"name": "họ tên", "aliases": ["biệt danh"], "role": "bị cáo|bị can|nghi phạm|người liên quan|cán bộ",
               "charge": "tội danh của người này (từ DANH SÁCH TỘI DANH) hoặc chuỗi rỗng",
               "sentence": "mức án nếu có, ví dụ: tử hình, 8 năm tù"}}]
}}]}}
Bài không nói về vụ việc cụ thể (tuyên truyền, hội nghị...) thì trả về {{"cases": []}}.

DANH SÁCH TỘI DANH: {crimes}
DANH SÁCH CHẤT: {substances}

Tiêu đề: {title}
Nội dung:
{content}"""

def extract_news_cases(doc: Document, llm_fn: Callable[[str], str], known_crimes: list[str]) -> list[dict]:
    """LLM extraction for one news article; charges are re-linked to law-KB crimes in code."""
    prompt = NEWS_EXTRACTION_PROMPT.format(
        crimes="; ".join(known_crimes), substances=", ".join(SUBSTANCES),
        title=doc.metadata.get("title", ""), content=doc.content[:12000],
    )
    try:
        cases = json.loads(llm_fn(prompt)).get("cases", [])
    except (json.JSONDecodeError, AttributeError):
        return []
    for case in cases:
        case["charges"] = sorted({c for c in (link_entity(x, known_crimes) for x in case.get("charges", [])) if c})
        for person in case.get("people", []):
            person["charge"] = link_entity(person.get("charge") or "", known_crimes) or ""
    return cases

# ----------------------------------------------------------------------------------------------
# Own ontology (report/ONTOLOGY.md): thresholds, structured penalties, per-person charges,
# stable case ids, substance synonyms. KG_ONTOLOGY=hint switches back to the suggested ontology.
# ----------------------------------------------------------------------------------------------

def ontology() -> str:
    return os.getenv("KG_ONTOLOGY", "own").lower()

# Canonical substance -> names Vietnamese news uses for it (D4).
SUBSTANCE_ALIASES = {
    "Heroine": ["heroin", "hêrôin", "hàng trắng"],
    "Cocaine": ["cocain", "côcain"],
    "Methamphetamine": ["ma túy đá", "ma tuý đá", "methamphetamin", "meth", "hồng phiến"],
    "Amphetamine": ["amphetamin"],
    "MDMA": ["thuốc lắc", "ecstasy"],
    "Ketamine": ["ketamin", "ke", "khay"],
    "cần sa": ["cỏ mỹ", "bồ đà", "marijuana", "cannabis", "cần sa khô"],
    "thuốc phiện": ["opium", "nha phiến"],
}
_ALIAS_TO_SUBSTANCE = {a: canon for canon, aliases in SUBSTANCE_ALIASES.items() for a in aliases}
_GENERIC_SUBSTANCES = {"ma túy", "ma tuý", "chất ma túy", "chất ma tuý", "ma túy các loại", ""}

def _lower(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())

def link_substance(name: str) -> str | None:
    """Synonym table first, then fuzzy match on canonical names; unknown named drugs keep their own name."""
    key = _lower(name or "")
    if key in _GENERIC_SUBSTANCES:
        return None
    if key in _ALIAS_TO_SUBSTANCE:
        return _ALIAS_TO_SUBSTANCE[key]
    return link_entity(key, SUBSTANCES, normalize=_lower) or name.strip()

def substances_in(text: str) -> list[str]:
    """find_substances + synonyms: 'thuốc lắc' in a question -> MDMA."""
    lowered = _lower(text)
    found = set(find_substances(text))
    found |= {canon for alias, canon in _ALIAS_TO_SUBSTANCE.items()
              if len(alias) > 3 and re.search(rf"(?<!\w){re.escape(alias)}(?!\w)", lowered)}
    return sorted(found)

def _number(raw: str) -> float:
    """Vietnamese numbers: '0,1' -> 0.1, '1.000' -> 1000, '9.6' -> 9.6."""
    if "," in raw:
        return float(raw.replace(".", "").replace(",", "."))
    if re.fullmatch(r"\d{1,3}(\.\d{3})+", raw):
        return float(raw.replace(".", ""))
    return float(raw)

_UNIT_GRAMS = {"gam": 1.0, "g": 1.0, "gram": 1.0, "gr": 1.0, "kilôgam": 1000.0, "kilogam": 1000.0, "kg": 1000.0}
_AMOUNT = re.compile(r"(\d+(?:[.,]\d+)*)\s*(kilôgam|kilogam|kg|gam|gram|gr|g)(?!\w)", re.IGNORECASE)

def amount_to_grams(amount: str) -> float | None:
    """'hơn 9,6kg' -> 9600.0; '406g' -> 406.0; '1.000 viên' -> None."""
    match = _AMOUNT.search(amount or "")
    return _number(match.group(1)) * _UNIT_GRAMS[match.group(2).lower()] if match else None

_POINT = re.compile(r"^([a-zđ])\)\s*(.+)$", re.MULTILINE)
_RANGE = re.compile(r"từ (\d+(?:[.,]\d+)?) (gam|kilôgam) đến dưới (\d+(?:[.,]\d+)?) (gam|kilôgam)")
_AT_LEAST = re.compile(r"(\d+(?:[.,]\d+)?) (gam|kilôgam) trở lên")

def parse_thresholds(clause_text: str) -> list[dict]:
    """One row per (point, substance) whose weight band the point states (D1)."""
    rows = []
    for point, line in _POINT.findall(clause_text):
        substances = find_substances(line)
        if not substances:
            continue
        if m := _RANGE.search(line):
            low, high = _number(m.group(1)) * _UNIT_GRAMS[m.group(2)], _number(m.group(3)) * _UNIT_GRAMS[m.group(4)]
        elif m := _AT_LEAST.search(line):
            low, high = _number(m.group(1)) * _UNIT_GRAMS[m.group(2)], None
        else:
            continue
        rows += [{"substance": s, "point": point, "min_g": low, "max_g": high} for s in substances]
    return rows

def parse_penalty(penalty: str) -> dict:
    """'phạt tù 20 năm, tù chung thân hoặc tử hình' -> max_level 'tử hình', severity 100 (D2)."""
    years = [int(n) / (12 if unit == "tháng" else 1) for n, unit in re.findall(r"(\d+) (năm|tháng)", penalty)]
    if "tù" not in penalty:
        years = []
    level = ("tử hình" if "tử hình" in penalty else "chung thân" if "chung thân" in penalty
             else "tù" if years else "")
    severity = {"tử hình": 100, "chung thân": 50}.get(level, max(years, default=0))
    return {"min_years": min(years, default=None), "max_years": max(years, default=None),
            "max_level": level, "severity": severity}

def parse_law_article_v2(doc: Document) -> dict[str, Any]:
    article = parse_law_article(doc)
    for clause in article["clauses"]:
        clause.update(parse_penalty(clause["penalty"]))
        clause["thresholds"] = parse_thresholds(clause["text"])
    return article

NEWS_EXTRACTION_PROMPT_V2 = """Bạn trích xuất knowledge graph từ một bài báo tiếng Việt về ma túy.
Chỉ dùng thông tin có trong bài. Trả về JSON đúng dạng:
{{"cases": [{{
  "name": "tên ngắn của vụ việc, ví dụ: Vụ vận chuyển 9,6kg MDMA qua Nội Bài",
  "summary": "1-2 câu tóm tắt, nêu chất và khối lượng nếu có",
  "date": "ngày xảy ra/xét xử nếu có, dạng YYYY-MM-DD hoặc chuỗi rỗng",
  "stage": "giai đoạn tố tụng MỚI NHẤT bài nhắc tới: bắt giữ|khởi tố|truy tố|sơ thẩm|phúc thẩm|khác",
  "location": "tỉnh/thành phố, chuỗi rỗng nếu không rõ",
  "charges": ["tội danh, BẮT BUỘC chọn đúng nguyên văn từ DANH SÁCH TỘI DANH"],
  "substances": [{{"name": "tên chất, dùng tên chuẩn trong DANH SÁCH CHẤT nếu khớp", "amount": "khối lượng kèm đơn vị như trong bài, ví dụ: hơn 9,6kg"}}],
  "people": [{{"name": "họ tên đầy đủ", "aliases": ["biệt danh, tên giang hồ"],
               "role": "bị cáo|bị can|nghi phạm|người liên quan|cán bộ",
               "charge": "tội danh của RIÊNG người này (từ DANH SÁCH TỘI DANH) hoặc chuỗi rỗng",
               "stage": "giai đoạn tố tụng của người này: bắt giữ|khởi tố|truy tố|sơ thẩm|phúc thẩm|khác",
               "sentence": "mức án nếu đã xử, ví dụ: tử hình, 36 tháng tù; chuỗi rỗng nếu chưa xử"}}]
}}]}}
Nếu bài chỉ mô tả hành vi (ví dụ "cho thuê phòng để sử dụng ma túy", "bán ma túy"), hãy chọn tội danh
tương ứng trong DANH SÁCH TỘI DANH. Mỗi vụ việc khác nhau trong bài là một phần tử của "cases".
Bài không nói về vụ việc cụ thể (tuyên truyền, hội nghị...) thì trả về {{"cases": []}}.

DANH SÁCH TỘI DANH: {crimes}
DANH SÁCH CHẤT: {substances}

Tiêu đề: {title}
Nội dung:
{content}"""

def extract_news_cases_v2(doc: Document, llm_fn: Callable[[str], str], known_crimes: list[str]) -> list[dict]:
    prompt = NEWS_EXTRACTION_PROMPT_V2.format(
        crimes="; ".join(known_crimes), substances=", ".join(SUBSTANCES),
        title=doc.metadata.get("title", ""), content=doc.content[:12000],
    )
    try:
        cases = json.loads(llm_fn(prompt)).get("cases", [])
    except (json.JSONDecodeError, AttributeError):
        return []
    for case in cases:
        case["charges"] = sorted({c for c in (link_entity(x, known_crimes) for x in case.get("charges") or []) if c})
        substances = {}
        for s in case.get("substances") or []:
            name = link_substance(s.get("name") or "")
            if name and name not in substances:
                amount = str(s.get("amount") or "")
                substances[name] = {"name": name, "amount": amount, "amount_g": amount_to_grams(amount)}
        case["substances"] = list(substances.values())
        people = []
        for person in case.get("people") or []:
            name = re.sub(r"\s+", " ", str(person.get("name") or "")).strip()
            if not name:
                continue
            charge = link_entity(person.get("charge") or "", known_crimes) or ""
            people.append({"name": name, "aliases": [a for a in person.get("aliases") or [] if a],
                           "role": person.get("role") or "", "charge": charge,
                           "stage": person.get("stage") or case.get("stage") or "",
                           "sentence": person.get("sentence") or ""})
        case["people"] = people
    return cases

# ----------------------------------------------------------------------------------------------
# Neo4j
# ----------------------------------------------------------------------------------------------

class Neo4jGraph:
    """Thin wrapper over the official neo4j driver."""

    def __init__(self, uri: str, user: str, password: str) -> None:
        from neo4j import GraphDatabase

        self.driver = GraphDatabase.driver(uri, auth=(user, password), notifications_min_severity="OFF")
        self.driver.verify_connectivity()

    def close(self) -> None:
        self.driver.close()

    def run(self, cypher: str, **params: Any) -> list[dict]:
        records, _, _ = self.driver.execute_query(cypher, params)
        return [record.data() for record in records]

    def reset(self) -> None:
        """Delete every node, relationship and constraint (bench_kg.py calls this before build_graph)."""
        self.run("MATCH (n) DETACH DELETE n")
        for row in self.run("SHOW CONSTRAINTS YIELD name RETURN name"):
            self.run(f"DROP CONSTRAINT `{row['name']}` IF EXISTS")

    def stats(self) -> dict[str, int]:
        nodes = self.run("MATCH (n) RETURN count(n) AS n")[0]["n"]
        rels = self.run("MATCH ()-[r]->() RETURN count(r) AS n")[0]["n"]
        return {"nodes": nodes, "relationships": rels}

    def seed_facts(self, question: str, doc_ids: list[str], skip_labels: tuple[str, ...] = (),
                   limit: int = 60) -> tuple[list[str], list[str]]:
        """Ontology-independent first step: seed nodes + their 1-hop edges as text facts.

        Seeds = nodes whose `doc_id` is in doc_ids, or whose `name`/`aliases` appear in the question.
        Returns (seed elementIds, facts). Nodes with a label in skip_labels are left out of the facts.
        """
        seeds = self.run(
            """
            MATCH (n)
            WHERE n.doc_id IN $doc_ids
               OR (n.name IS :: STRING AND size(n.name) >= 3 AND toLower($q) CONTAINS toLower(n.name))
               OR any(a IN coalesce(n.aliases, []) WHERE size(a) >= 3 AND toLower($q) CONTAINS toLower(a))
            RETURN elementId(n) AS id
            """,
            q=question, doc_ids=doc_ids,
        )
        seed_ids = [row["id"] for row in seeds]
        edges = self.run(
            """
            MATCH (s)-[r]-(m)
            WHERE elementId(s) IN $ids
              AND none(l IN labels(s) + labels(m) WHERE l IN $skip)
            WITH DISTINCT r LIMIT $limit
            WITH startNode(r) AS a, r, endNode(r) AS b
            RETURN labels(a)[0] AS a_label, coalesce(a.name, a.id) AS a_name, type(r) AS rel,
                   properties(r) AS props, labels(b)[0] AS b_label, coalesce(b.name, b.id) AS b_name
            """,
            ids=seed_ids, skip=list(skip_labels), limit=limit,
        )
        facts = []
        for e in edges:
            props = ", ".join(f"{k}: {v}" for k, v in e["props"].items() if v)
            facts.append(f"({e['a_label']}: {e['a_name']}) -[{e['rel']}{' {' + props + '}' if props else ''}]-> "
                         f"({e['b_label']}: {e['b_name']})")
        return seed_ids, facts

    # ---------------------------------------------------------------- HINT — suggested ontology: writes

    def suggested_constraints(self) -> None:
        for label, key in [("Article", "id"), ("Clause", "id"), ("Crime", "name"), ("Case", "name"),
                           ("Substance", "name"), ("Person", "name"), ("Location", "name")]:
            self.run(f"CREATE CONSTRAINT IF NOT EXISTS FOR (n:{label}) REQUIRE n.{key} IS UNIQUE")

    def add_law_article(self, article: dict) -> None:
        self.run(
            """
            MERGE (a:Article {id: $id}) SET a.title = $title, a.law = $law, a.doc_id = $doc_id
            FOREACH (crime IN CASE WHEN $crime IS NULL THEN [] ELSE [$crime] END |
                MERGE (c:Crime {name: crime}) MERGE (a)-[:DEFINES]->(c))
            WITH a
            UNWIND $clauses AS clause
            MERGE (cl:Clause {id: clause.id})
              SET cl.number = clause.number, cl.penalty = clause.penalty, cl.text = clause.text, cl.doc_id = $doc_id
            MERGE (a)-[:HAS_CLAUSE]->(cl)
            FOREACH (s IN clause.substances | MERGE (sub:Substance {name: s}) MERGE (cl)-[:MENTIONS]->(sub))
            """,
            **article,
        )

    def add_news_case(self, case: dict, doc: Document) -> None:
        self.run(
            """
            MERGE (k:Case {name: $name})
              SET k.summary = $summary, k.date = $date, k.doc_id = $doc_id, k.source_title = $title
            FOREACH (loc IN CASE WHEN $location = '' THEN [] ELSE [$location] END |
                MERGE (l:Location {name: loc}) MERGE (k)-[:LOCATED_IN]->(l))
            FOREACH (crime IN $charges | MERGE (c:Crime {name: crime}) MERGE (k)-[:CHARGED_WITH]->(c))
            FOREACH (s IN $substances | MERGE (sub:Substance {name: s.name}) MERGE (k)-[r:INVOLVES]->(sub)
                SET r.amount = s.amount)
            FOREACH (p IN $people | MERGE (person:Person {name: p.name})
                SET person.aliases = coalesce(p.aliases, [])
                MERGE (person)-[r:INVOLVED_IN]->(k) SET r.role = p.role, r.charge = p.charge, r.sentence = p.sentence)
            """,
            name=case.get("name") or doc.metadata.get("title", doc.id),
            summary=case.get("summary", ""), date=case.get("date", ""), location=case.get("location", ""),
            charges=case.get("charges", []), people=[p for p in case.get("people", []) if p.get("name")],
            substances=[s for s in case.get("substances", []) if s.get("name")],
            doc_id=doc.id, title=doc.metadata.get("title", ""),
        )

    # ---------------------------------------------------------------- own ontology: writes

    def own_constraints(self) -> None:
        for label, key in [("Article", "id"), ("Clause", "id"), ("Crime", "name"), ("Case", "id"),
                           ("Substance", "name"), ("Person", "name"), ("Location", "name")]:
            self.run(f"CREATE CONSTRAINT IF NOT EXISTS FOR (n:{label}) REQUIRE n.{key} IS UNIQUE")

    def add_law_article_v2(self, article: dict) -> None:
        self.run(
            """
            MERGE (a:Article {id: $id}) SET a.title = $title, a.law = $law, a.doc_id = $doc_id
            FOREACH (crime IN CASE WHEN $crime IS NULL THEN [] ELSE [$crime] END |
                MERGE (c:Crime {name: crime}) MERGE (a)-[:DEFINES]->(c))
            WITH a
            UNWIND $clauses AS clause
            MERGE (cl:Clause {id: clause.id})
              SET cl.number = clause.number, cl.penalty = clause.penalty, cl.text = clause.text,
                  cl.min_years = clause.min_years, cl.max_years = clause.max_years,
                  cl.max_level = clause.max_level, cl.severity = clause.severity, cl.doc_id = $doc_id
            MERGE (a)-[:HAS_CLAUSE]->(cl)
            FOREACH (t IN clause.thresholds | MERGE (sub:Substance {name: t.substance})
                MERGE (cl)-[r:THRESHOLD {point: t.point}]->(sub) SET r.min_g = t.min_g, r.max_g = t.max_g)
            """,
            **article,
        )

    def add_news_case_v2(self, case: dict, doc: Document, index: int) -> None:
        case_id = f"{doc.id}#{index}"
        self.run(
            """
            MERGE (k:Case {id: $id})
              SET k.name = $name, k.summary = $summary, k.date = $date, k.stage = $stage,
                  k.doc_id = $doc_id, k.source_title = $title
            FOREACH (loc IN CASE WHEN $location = '' THEN [] ELSE [$location] END |
                MERGE (l:Location {name: loc}) MERGE (k)-[:LOCATED_IN]->(l))
            FOREACH (crime IN $charges | MERGE (c:Crime {name: crime}) MERGE (k)-[:CHARGED_WITH]->(c))
            FOREACH (s IN $substances | MERGE (sub:Substance {name: s.name}) MERGE (k)-[r:INVOLVES]->(sub)
                SET r.amount = s.amount, r.amount_g = s.amount_g)
            FOREACH (p IN $people |
                MERGE (person:Person {name: p.name})
                SET person.aliases = reduce(acc = coalesce(person.aliases, []), a IN p.aliases |
                                            CASE WHEN a IN acc THEN acc ELSE acc + a END)
                MERGE (person)-[r:INVOLVED_IN]->(k) SET r.role = p.role
                FOREACH (crime IN CASE WHEN p.charge = '' THEN [] ELSE [p.charge] END |
                    MERGE (c:Crime {name: crime})
                    MERGE (person)-[a:ACCUSED_OF {case_id: $id}]->(c) SET a.stage = p.stage, a.sentence = p.sentence))
            """,
            id=case_id, name=case.get("name") or doc.metadata.get("title", doc.id),
            summary=case.get("summary", ""), date=case.get("date", ""), stage=case.get("stage", ""),
            location=case.get("location") or "", charges=case.get("charges", []),
            substances=case.get("substances", []), people=case.get("people", []),
            doc_id=doc.id, title=doc.metadata.get("title", ""),
        )

    def set_substance_aliases(self) -> None:
        self.run("UNWIND $rows AS row MATCH (s:Substance {name: row.name}) SET s.aliases = row.aliases",
                 rows=[{"name": k, "aliases": v} for k, v in SUBSTANCE_ALIASES.items()])

    # ---------------------------------------------------------------- KG-3

    def context(self, question: str, doc_ids: list[str], max_facts: int = 60) -> list[str]:
        """Graph facts for a question: the legal basis of every case reached first, then seeds + 1 hop."""
        if ontology() == "hint":
            return self._context_hint(question, doc_ids, max_facts)
        return self._context_own(question, doc_ids, max_facts)

    @staticmethod
    def _clause_fact(row: dict) -> str:
        return f"[{row['aid']} - {row['title']}] khoản {row['number']}: {row['text']}"

    @staticmethod
    def _merge_facts(*groups: list[str], limit: int) -> list[str]:
        return list(dict.fromkeys(f for group in groups for f in group))[:limit]

    def _seed_cases(self, seed_ids: list[str]) -> list[dict]:
        return self.run(
            """
            MATCH (k:Case)
            WHERE elementId(k) IN $ids OR EXISTS { MATCH (s)--(k) WHERE elementId(s) IN $ids }
            RETURN DISTINCT elementId(k) AS id, k.name AS name, k.summary AS summary,
                   k.stage AS stage, k.date AS date
            """,
            ids=seed_ids,
        )

    def _context_hint(self, question: str, doc_ids: list[str], max_facts: int) -> list[str]:
        seed_ids, seed_facts = self.seed_facts(question, doc_ids)
        cases = self._seed_cases(seed_ids)
        facts = [f"Vụ việc '{c['name']}': {c['summary']}" for c in cases]
        rows = self.run(
            """
            MATCH (k:Case)-[:CHARGED_WITH]->(:Crime)<-[:DEFINES]-(a:Article)-[:HAS_CLAUSE]->(cl:Clause)
            WHERE elementId(k) IN $cids
              AND (cl.number = 1 OR EXISTS { (k)-[:INVOLVES]->(:Substance)<-[:MENTIONS]-(cl) })
            RETURN DISTINCT a.id AS aid, a.title AS title, cl.number AS number, cl.text AS text
            ORDER BY aid, number
            """,
            cids=[c["id"] for c in cases],
        )
        numbers = re.findall(r"[Đđ]iều (\d+)", question)
        if numbers:
            rows += self.run(
                """
                MATCH (a:Article)-[:HAS_CLAUSE]->(cl:Clause)
                WHERE any(n IN $nums WHERE a.id STARTS WITH 'Điều ' + n + ' ')
                  AND (cl.number = 1 OR EXISTS { (cl)-[:MENTIONS]->(s:Substance) WHERE s.name IN $subs })
                RETURN a.id AS aid, a.title AS title, cl.number AS number, cl.text AS text
                ORDER BY aid, number
                """,
                nums=numbers, subs=find_substances(question),
            )
        facts += [self._clause_fact(r) for r in rows]
        return self._merge_facts(facts, seed_facts, limit=max_facts)

    def _context_own(self, question: str, doc_ids: list[str], max_facts: int) -> list[str]:
        # Clause nodes are reached on purpose below; as raw 1-hop seed edges they only add noise.
        seed_ids, seed_facts = self.seed_facts(question, doc_ids, skip_labels=("Clause",))
        cases = self._seed_cases(seed_ids)
        case_ids = [c["id"] for c in cases]
        facts = [f"Vụ việc '{c['name']}' (giai đoạn: {c['stage'] or '?'}, ngày: {c['date'] or '?'}): {c['summary']}"
                 for c in cases]

        # Who is accused of what, per person (D3).
        for r in self.run(
            """
            MATCH (p:Person)-[i:INVOLVED_IN]->(k:Case) WHERE elementId(k) IN $cids
            OPTIONAL MATCH (p)-[a:ACCUSED_OF {case_id: k.id}]->(c:Crime)
            RETURN p.name AS person, p.aliases AS aliases, i.role AS role, c.name AS crime,
                   a.stage AS stage, a.sentence AS sentence
            """,
            cids=case_ids,
        ):
            alias = f" (biệt danh: {', '.join(r['aliases'])})" if r["aliases"] else ""
            crime = f" — tội {r['crime']} [{r['stage'] or '?'}]" if r["crime"] else ""
            sentence = f" — mức án: {r['sentence']}" if r["sentence"] else ""
            facts.append(f"{r['person']}{alias}: {r['role'] or 'liên quan'}{crime}{sentence}")

        # Weight band -> the exact clause (D1).
        threshold_rows = self.run(
            """
            MATCH (k:Case)-[i:INVOLVES]->(s:Substance)<-[t:THRESHOLD]-(cl:Clause)<-[:HAS_CLAUSE]-(a:Article)
            WHERE elementId(k) IN $cids AND i.amount_g IS NOT NULL
              AND i.amount_g >= t.min_g AND (t.max_g IS NULL OR i.amount_g < t.max_g)
              AND EXISTS { (a)-[:DEFINES]->(:Crime)<-[:CHARGED_WITH|ACCUSED_OF]-(x) WHERE x = k OR (x)-[:INVOLVED_IN]->(k) }
            RETURN DISTINCT k.name AS case, s.name AS substance, i.amount AS amount, t.point AS point,
                   t.min_g AS min_g, t.max_g AS max_g, a.id AS aid, a.title AS title, cl.number AS number, cl.text AS text
            ORDER BY aid, number
            """,
            cids=case_ids,
        )
        for r in threshold_rows:
            band = f"từ {r['min_g']:g} g" + (f" đến dưới {r['max_g']:g} g" if r["max_g"] is not None else " trở lên")
            facts.append(f"Vụ '{r['case']}': {r['substance']} {r['amount']} thuộc {r['aid']} khoản {r['number']} "
                         f"điểm {r['point']} ({band})")

        # Basic + most severe clause of every article reached from the cases or their people (D2).
        rows = self.run(
            """
            MATCH (k:Case) WHERE elementId(k) IN $cids
            MATCH (k)-[:CHARGED_WITH]->(c:Crime)
            RETURN DISTINCT c.name AS crime
            UNION
            MATCH (p:Person)-[:INVOLVED_IN]->(k:Case) WHERE elementId(k) IN $cids
            MATCH (p)-[:ACCUSED_OF {case_id: k.id}]->(c:Crime)
            RETURN DISTINCT c.name AS crime
            """,
            cids=case_ids,
        )
        article_filter = "(a)-[:DEFINES]->(:Crime {name: crime})"
        clause_rows = self._basic_and_max_clauses([r["crime"] for r in rows], article_filter, "crime")
        clause_rows += threshold_rows

        # Articles named in the question: basic + most severe + clauses with a band for a named substance.
        numbers = re.findall(r"[Đđ]iều (\d+)", question)
        substances = substances_in(question)
        if numbers:
            clause_rows += self._basic_and_max_clauses(
                numbers, "a.id STARTS WITH 'Điều ' + n + ' '", "n")
            clause_rows += self.run(
                """
                MATCH (a:Article)-[:HAS_CLAUSE]->(cl:Clause)-[:THRESHOLD]->(s:Substance)
                WHERE any(n IN $nums WHERE a.id STARTS WITH 'Điều ' + n + ' ') AND s.name IN $subs
                RETURN DISTINCT a.id AS aid, a.title AS title, cl.number AS number, cl.text AS text
                ORDER BY aid, number
                """,
                nums=numbers, subs=substances,
            )
        facts += [self._clause_fact(r) for r in sorted(clause_rows, key=lambda r: (r["aid"], r["number"]))]

        # Aggregation over a named substance: every case that involves it, not only the seeds (Q6).
        for r in self.run(
            """
            MATCH (k:Case)-[i:INVOLVES]->(s:Substance) WHERE s.name IN $subs
            RETURN s.name AS substance, k.name AS case, k.summary AS summary, i.amount AS amount
            ORDER BY case LIMIT 20
            """,
            subs=substances,
        ):
            facts.append(f"Vụ việc '{r['case']}' có {r['substance']}"
                         f"{' (' + r['amount'] + ')' if r['amount'] else ''}: {r['summary']}")
        return self._merge_facts(facts, seed_facts, limit=max_facts)

    def _basic_and_max_clauses(self, keys: list[str], article_filter: str, var: str) -> list[dict]:
        """Clause 1 and the most severe clause of every article matching article_filter for some key."""
        if not keys:
            return []
        return self.run(
            f"""
            UNWIND $keys AS {var}
            MATCH (a:Article)-[:HAS_CLAUSE]->(cl:Clause) WHERE {article_filter}
            WITH a, cl ORDER BY cl.severity DESC, cl.number DESC
            WITH a, collect(cl) AS clauses
            WITH a, [c IN clauses WHERE c.number = 1] + [clauses[0]] AS picked
            UNWIND picked AS cl
            RETURN DISTINCT a.id AS aid, a.title AS title, cl.number AS number, cl.text AS text
            """,
            keys=keys,
        )

# ---------------------------------------------------------------------------------------------- KG-2

def build_graph(graph: Neo4jGraph, law_docs: list[Document], news_docs: list[Document],
                llm_fn: Callable[..., str]) -> None:
    """Load both KBs into an empty graph. llm_fn(prompt, json_mode=False) -> str (metered OpenAI chat)."""
    json_llm = lambda prompt: llm_fn(prompt, json_mode=True)
    if ontology() == "hint":
        graph.suggested_constraints()
        articles = [parse_law_article(d) for d in law_docs]
        for article in articles:
            graph.add_law_article(article)
        crimes = [a["crime"] for a in articles if a["crime"]]
        for doc in news_docs:
            for case in extract_news_cases(doc, json_llm, crimes):
                graph.add_news_case(case, doc)
        return

    graph.own_constraints()
    articles = [parse_law_article_v2(d) for d in law_docs]
    for article in articles:
        graph.add_law_article_v2(article)
    crimes = [a["crime"] for a in articles if a["crime"]]
    for doc in news_docs:
        for index, case in enumerate(extract_news_cases_v2(doc, json_llm, crimes), start=1):
            graph.add_news_case_v2(case, doc, index)
    graph.set_substance_aliases()

# ---------------------------------------------------------------------------------------------- KG-4

GRAPH_PROMPT = """Trả lời câu hỏi chỉ dựa trên ngữ cảnh (đoạn văn bản và dữ kiện từ knowledge graph).
Nêu rõ số Điều luật khi có. Nếu ngữ cảnh không đủ, nói không đủ thông tin.

Dữ kiện knowledge graph:
{facts}

Đoạn văn bản:
{chunks}

Câu hỏi: {question}
Trả lời:"""

class GraphRAGAgent:
    """Hybrid GraphRAG: the same vector top-k as flat RAG, plus facts expanded from the graph."""

    def __init__(self, store: EmbeddingStore, graph: Neo4jGraph, llm_fn: Callable[[str], str]) -> None:
        self.store = store
        self.graph = graph
        self.llm_fn = llm_fn

    def answer(self, question: str, top_k: int = 3) -> str:
        chunks = self.store.search(question, top_k=top_k)
        doc_ids = list(dict.fromkeys(chunk["metadata"]["doc_id"] for chunk in chunks))
        facts = self.graph.context(question, doc_ids)
        prompt = GRAPH_PROMPT.format(
            facts="\n".join(f"- {fact}" for fact in facts) or "- (không có)",
            chunks="\n\n".join(f"[{i}] {chunk['content']}" for i, chunk in enumerate(chunks, start=1)),
            question=question,
        )
        return self.llm_fn(prompt)
