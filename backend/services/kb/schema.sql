PRAGMA journal_mode=WAL;
PRAGMA busy_timeout=5000;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS meta(
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS documents(
  doc_id TEXT PRIMARY KEY,
  topic TEXT NOT NULL DEFAULT '_inbox',
  source_type TEXT NOT NULL DEFAULT '',
  title TEXT NOT NULL DEFAULT '',
  abstract TEXT NOT NULL DEFAULT '',
  authors TEXT NOT NULL DEFAULT '',
  year TEXT NOT NULL DEFAULT '',
  venue TEXT NOT NULL DEFAULT '',
  citations INTEGER,
  doi TEXT NOT NULL DEFAULT '',
  source_uri TEXT NOT NULL DEFAULT '',
  stage TEXT NOT NULL DEFAULT 'found',
  best_tier INTEGER NOT NULL DEFAULT 0,
  tags TEXT NOT NULL DEFAULT '[]',
  org_marks TEXT NOT NULL DEFAULT '[]',
  arxiv_cats TEXT NOT NULL DEFAULT '[]',
  status TEXT NOT NULL DEFAULT 'active',
  first_seen_at TEXT,
  updated_at TEXT,
  CHECK(best_tier BETWEEN 0 AND 3),
  CHECK(stage IN ('found','downloaded','converted','translated','condensed'))
);
CREATE INDEX IF NOT EXISTS idx_doc_topic ON documents(topic, source_type);
CREATE INDEX IF NOT EXISTS idx_doc_tier ON documents(best_tier);
CREATE INDEX IF NOT EXISTS idx_doc_stage ON documents(stage);

CREATE TABLE IF NOT EXISTS versions(
  version_id INTEGER PRIMARY KEY,
  doc_id TEXT NOT NULL REFERENCES documents(doc_id) ON DELETE CASCADE,
  tier INTEGER NOT NULL CHECK(tier BETWEEN 0 AND 3),
  kind TEXT NOT NULL,
  path TEXT NOT NULL,
  content_hash TEXT,
  char_count INTEGER,
  updated_at TEXT,
  UNIQUE(doc_id, kind)
);
CREATE INDEX IF NOT EXISTS idx_ver_doc ON versions(doc_id);

CREATE TABLE IF NOT EXISTS vocab(
  topic TEXT NOT NULL,
  facet TEXT NOT NULL DEFAULT 'topic',
  canonical TEXT NOT NULL,
  aliases TEXT NOT NULL DEFAULT '[]',
  status TEXT NOT NULL DEFAULT 'candidate',
  PRIMARY KEY(topic, canonical)
);

CREATE TABLE IF NOT EXISTS batches(
  round_id TEXT PRIMARY KEY,
  topic TEXT,
  label TEXT,
  status TEXT NOT NULL DEFAULT 'open',
  created_at TEXT,
  confirmed_at TEXT
);

CREATE TABLE IF NOT EXISTS batch_items(
  round_id TEXT NOT NULL REFERENCES batches(round_id) ON DELETE CASCADE,
  doc_id TEXT NOT NULL,
  action TEXT NOT NULL DEFAULT 'import',
  PRIMARY KEY(round_id, doc_id)
);

CREATE VIRTUAL TABLE IF NOT EXISTS kb_fts USING fts5(
  doc_id UNINDEXED,
  title_t,
  abstract_t,
  tags_t,
  body_t,
  tokenize='unicode61'
);

-- M4 追加（schema_version=2）：chunk manifest。
-- 向量容器 A（sqlite-vec vec0）由 db.py 在扩展加载成功后创建：
--   CREATE VIRTUAL TABLE IF NOT EXISTS kb_vec USING vec0(
--     chunk_id INTEGER PRIMARY KEY, embedding float[1024]);
-- 容器 B（FAISS IndexIDMap2）只保留 vector.py 接口，主容器见 02 §10 M0 结论。
CREATE TABLE IF NOT EXISTS chunks(
  chunk_id INTEGER PRIMARY KEY,
  version_id INTEGER NOT NULL REFERENCES versions(version_id) ON DELETE CASCADE,
  doc_id TEXT NOT NULL,
  chunk_index INTEGER NOT NULL,
  text TEXT NOT NULL,
  content_hash TEXT NOT NULL,   -- sha256(text) 前 16 位
  model_id TEXT,                -- 如 bge-m3@local-1024；NULL=未嵌入
  embedded_at TEXT,
  UNIQUE(version_id, chunk_index)
);
CREATE INDEX IF NOT EXISTS idx_chunk_doc ON chunks(doc_id);
CREATE INDEX IF NOT EXISTS idx_chunk_version ON chunks(version_id);
