-- =====================================================================
-- meta-search DB schema (SQLite 3.45+, 단일 파일: data/meta_search.sqlite)
-- 설명: meta-search 최종 구현 계획서.md §3
-- 연결 직후 항상 실행:  PRAGMA foreign_keys = ON; PRAGMA journal_mode = WAL; PRAGMA busy_timeout = 30000;
-- =====================================================================

-- ---------------------------------------------------------------------
-- 1. 카테고리와 객관 속성 표준(taxonomy)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS categories (
    category_id      TEXT PRIMARY KEY
                     CHECK (category_id GLOB '[a-z]*' AND category_id NOT GLOB '*[^a-z0-9_]*'),
    display_name     TEXT NOT NULL,                       -- 예: '기계식 키보드'
    taxonomy_status  TEXT NOT NULL DEFAULT 'provisional'
                     CHECK (taxonomy_status IN ('provisional', 'normalized')),
    taxonomy_version INTEGER NOT NULL DEFAULT 0,          -- 객관 속성 표준이 바뀔 때마다 +1 (정보용)
    created_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

-- 카테고리별 표준 객관 속성. tier(core/extra)의 유일한 출처(Single Source of Truth).
CREATE TABLE IF NOT EXISTS category_attributes (
    category_id  TEXT NOT NULL REFERENCES categories(category_id) ON DELETE CASCADE,
    key          TEXT NOT NULL CHECK (key GLOB '[a-z]*' AND key NOT GLOB '*[^a-z0-9_]*'),
    display_name TEXT NOT NULL,                           -- 예: '키 배열'
    definition   TEXT NOT NULL DEFAULT '',
    tier         TEXT NOT NULL CHECK (tier IN ('core', 'extra')),
    value_type   TEXT NOT NULL CHECK (value_type IN ('string', 'number', 'boolean', 'string_list')),
    unit         TEXT,                                    -- value_type='number'일 때만 사용 (예: 'g', 'mm')
    reason       TEXT NOT NULL DEFAULT '',                -- 분류 근거 (정규화 결과)
    PRIMARY KEY (category_id, key),
    CHECK (unit IS NULL OR value_type = 'number')
);

-- ---------------------------------------------------------------------
-- 2. 상품과 객관 속성 값
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS products (
    product_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    category_id    TEXT NOT NULL REFERENCES categories(category_id),
    name           TEXT NOT NULL,                         -- 페이지 표기 상품명
    brand          TEXT NOT NULL,
    model          TEXT NOT NULL,                         -- 모델명/모델 코드
    model_key      TEXT NOT NULL,                         -- 중복 판별 키 = normalize(brand + ' ' + model)
    source_url     TEXT NOT NULL,                         -- 등록 요청 URL
    details_md     TEXT NOT NULL DEFAULT '',              -- 상세 설명 Markdown 원문 (검색·발췌용)
    raw_attributes TEXT NOT NULL DEFAULT '[]'
                   CHECK (json_valid(raw_attributes) AND json_type(raw_attributes) = 'array'),
                                                          -- 최초 추출 원본 속성 목록 (추적용, 정규화로 변경하지 않음)
    status         TEXT NOT NULL DEFAULT 'collecting'
                   CHECK (status IN ('collecting', 'ready', 'failed')),
    created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE (category_id, model_key)
);

-- 정규화된 객관 속성 값 (EAV). tier는 category_attributes 조인으로 얻는다.
CREATE TABLE IF NOT EXISTS product_attributes (
    product_id  INTEGER NOT NULL REFERENCES products(product_id) ON DELETE CASCADE,
    category_id TEXT NOT NULL,
    key         TEXT NOT NULL,
    value_json  TEXT NOT NULL CHECK (json_valid(value_json)),   -- "TKL" | 1200 | true | ["usb_c","bluetooth"]
    source      TEXT NOT NULL CHECK (source IN ('page', 'research')),
    PRIMARY KEY (product_id, key),
    FOREIGN KEY (category_id, key)
        REFERENCES category_attributes(category_id, key)
        ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED
);

-- ---------------------------------------------------------------------
-- 3. 리뷰와 체감 속성 (Opinion Units)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS reviews (
    review_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id   INTEGER NOT NULL REFERENCES products(product_id) ON DELETE CASCADE,
    body         TEXT NOT NULL,
    rating       REAL,                                    -- 페이지 표기 별점 (없으면 NULL)
    written_at   TEXT,                                    -- 페이지 표기 작성일 (ISO 날짜, 없으면 NULL)
    collected_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    cloud_synced INTEGER NOT NULL DEFAULT 0 CHECK (cloud_synced IN (0, 1))
);
CREATE INDEX IF NOT EXISTS idx_reviews_product ON reviews(product_id);

-- Lambda가 반환한 최신 군집 결과. 이전 표를 덮어쓴다.
CREATE TABLE IF NOT EXISTS aspect_clusters (
    aspect_cluster_id TEXT PRIMARY KEY,                   -- 'ac_' + sha1(category_id, label)[:10]
    category_id       TEXT NOT NULL REFERENCES categories(category_id) ON DELETE CASCADE,
    label             TEXT NOT NULL,                      -- 대표 표현 (구성원 중 하나)
    UNIQUE (category_id, label)
);

CREATE TABLE IF NOT EXISTS state_clusters (
    state_cluster_id  TEXT PRIMARY KEY,                   -- 'sc_' + sha1(aspect_cluster_id, label)[:10]
    aspect_cluster_id TEXT NOT NULL REFERENCES aspect_clusters(aspect_cluster_id) ON DELETE CASCADE,
    label             TEXT NOT NULL,
    UNIQUE (aspect_cluster_id, label)
);

CREATE TABLE IF NOT EXISTS opinion_units (
    unit_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    review_id         INTEGER NOT NULL REFERENCES reviews(review_id) ON DELETE CASCADE,
    product_id        INTEGER NOT NULL REFERENCES products(product_id) ON DELETE CASCADE,  -- 조회 편의용 비정규화
    category_id       TEXT NOT NULL,                       -- 조회 편의용 비정규화
    raw_aspect        TEXT NOT NULL,
    raw_status        TEXT,                                -- 상태 없는 평가는 NULL
    excerpt           TEXT NOT NULL,                       -- 리뷰 원문의 연속 부분 문자열
    opinion           TEXT NOT NULL,
    sentiment         TEXT NOT NULL CHECK (sentiment IN ('positive', 'negative', 'mixed', 'neutral', 'unknown')),
    -- 원래 표현 ID: 텍스트에서 결정적으로 계산 → 재군집화해도 변하지 않음 (검색 조건에 사용)
    aspect_variant_id TEXT NOT NULL,                       -- 'av_' + sha1(category_id, norm(raw_aspect))[:10]
    state_variant_id  TEXT,                                -- 'sv_' + sha1(category_id, norm(raw_status))[:10], raw_status NULL이면 NULL
    -- 군집 ID: 재군집화 때마다 갱신 (표시·탐색용)
    aspect_cluster_id TEXT REFERENCES aspect_clusters(aspect_cluster_id) ON DELETE SET NULL,
    state_cluster_id  TEXT REFERENCES state_clusters(state_cluster_id) ON DELETE SET NULL,
    cloud_synced      INTEGER NOT NULL DEFAULT 0 CHECK (cloud_synced IN (0, 1))
);
CREATE INDEX IF NOT EXISTS idx_units_product_aspect ON opinion_units(product_id, aspect_variant_id);
CREATE INDEX IF NOT EXISTS idx_units_category_aspect ON opinion_units(category_id, aspect_variant_id);
CREATE INDEX IF NOT EXISTS idx_units_review ON opinion_units(review_id);

-- 로컬에서는 표현의 exact-match만 수행한다. 미매칭 군집 ID는 NULL이다.
CREATE TABLE IF NOT EXISTS exact_aspect_matches (
    category_id TEXT NOT NULL REFERENCES categories(category_id) ON DELETE CASCADE,
    raw_aspect TEXT NOT NULL,
    aspect_cluster_id TEXT NOT NULL REFERENCES aspect_clusters(aspect_cluster_id) ON DELETE CASCADE,
    PRIMARY KEY (category_id, raw_aspect)
);
CREATE TABLE IF NOT EXISTS exact_state_matches (
    category_id TEXT NOT NULL REFERENCES categories(category_id) ON DELETE CASCADE,
    aspect_cluster_id TEXT NOT NULL REFERENCES aspect_clusters(aspect_cluster_id) ON DELETE CASCADE,
    raw_status TEXT NOT NULL,
    state_cluster_id TEXT NOT NULL REFERENCES state_clusters(state_cluster_id) ON DELETE CASCADE,
    PRIMARY KEY (category_id, aspect_cluster_id, raw_status)
);

-- ---------------------------------------------------------------------
-- 4. 판매 속성 (Offers)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS offers (
    offer_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id      INTEGER NOT NULL REFERENCES products(product_id) ON DELETE CASCADE,
    seller          TEXT NOT NULL,                         -- 판매자/판매 업체
    retailer        TEXT NOT NULL,                         -- 판매 사이트 (예: 쿠팡)
    url             TEXT NOT NULL,
    price           INTEGER NOT NULL CHECK (price >= 0),   -- 상품 가격 (배송비 제외, 통화 최소 단위 아님: KRW는 원)
    currency        TEXT NOT NULL DEFAULT 'KRW' CHECK (length(currency) = 3),
    shipping_fee    INTEGER CHECK (shipping_fee IS NULL OR shipping_fee >= 0),  -- 0=무료, NULL=미확인
    shipping_days   INTEGER CHECK (shipping_days IS NULL OR shipping_days >= 0), -- 예상 도착까지 일수, NULL=미확인
    availability    TEXT NOT NULL
                    CHECK (availability IN ('available', 'sold_out', 'discontinued', 'unknown')),
    last_checked_at TEXT NOT NULL,                         -- ISO8601 UTC
    UNIQUE (product_id, url)
);
CREATE INDEX IF NOT EXISTS idx_offers_product ON offers(product_id);

-- ---------------------------------------------------------------------
-- 5. 등록 작업 (Jobs)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS jobs (
    job_id            TEXT PRIMARY KEY,                    -- uuid4 hex 앞 12자리
    url               TEXT NOT NULL,
    category_hint     TEXT,                                -- 사용자가 준 카테고리 힌트 (선택)
    status            TEXT NOT NULL DEFAULT 'queued'
                      CHECK (status IN ('queued', 'running', 'done', 'failed')),
    stage             TEXT NOT NULL DEFAULT 'queued',      -- 'collect_page' | 'extract_objective' | 'parallel' | 'finalize' | 'done'
    product_id        INTEGER REFERENCES products(product_id) ON DELETE SET NULL,
    is_update         INTEGER NOT NULL DEFAULT 0 CHECK (is_update IN (0, 1)),  -- 기존 상품 재등록 여부
    objective_status  TEXT NOT NULL DEFAULT 'pending'
                      CHECK (objective_status  IN ('pending', 'running', 'done', 'skipped', 'failed')),
    experience_status TEXT NOT NULL DEFAULT 'pending'
                      CHECK (experience_status IN ('pending', 'running', 'done', 'skipped', 'failed')),
    sales_status      TEXT NOT NULL DEFAULT 'pending'
                      CHECK (sales_status      IN ('pending', 'running', 'done', 'skipped', 'failed')),
    warnings          TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(warnings)),  -- 문자열 배열
    error             TEXT,
    created_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    started_at        TEXT,
    finished_at       TEXT
);
