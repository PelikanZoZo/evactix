-- Evactix: ядро хранилища. Модель данных — документ «ТЗ ядра Evactix: модель данных».
-- События и планы только дописываются: правка = новая запись со ссылкой supersedes.

create extension if not exists timescaledb;

-- Справочник источников
create table if not exists sources (
    id               text primary key,                 -- 'cbr.fx', 'metno.forecast'
    name             text not null,
    source_class     text not null check (source_class in
                       ('control','automatic','open','paid','corporate','computed')),
    base_confidence  real not null check (base_confidence between 0 and 1),
    license          text not null,                    -- 'CC-BY-4.0', 'public-domain', 'internal-only'
    url              text,
    max_age          interval,                         -- сколько можно молчать, прежде чем Ана поднимет тревогу
    created_at       timestamptz not null default now()
);

-- Справочник метрик: тип значения задан здесь
create table if not exists metrics (
    id           text primary key,                     -- 'fx.rate_rub', 'weather.air_temperature'
    value_type   text not null check (value_type in
                   ('number','money','ratio','category','ordinal','bool3',
                    'cyclic','duration','range','ohlc')),
    unit         text,
    aggregation  text check (aggregation in ('instant','sum','mean','min','max','last')),
    description  text not null,
    created_at   timestamptz not null default now()
);

-- Сущности: ядро знает только ID и тип, реквизиты — в другой системе
create table if not exists entities (
    id          text primary key,                      -- 'currency:USD', 'point:moscow', 'org:<uuid>'
    type        text not null check (type in
                  ('currency','country','point','commodity','index','org','route','warehouse','cargo','region')),
    created_at  timestamptz not null default now()
);

-- События: факты и прогнозы
create table if not exists events (
    id               uuid not null,
    type             text not null,
    schema_version   int  not null default 1,
    kind             text not null check (kind in ('fact','forecast')),
    -- время
    occurred_at      timestamptz not null,
    valid_from       timestamptz,
    valid_to         timestamptz,
    observed_at      timestamptz not null,
    recorded_at      timestamptz not null default now(),
    -- место
    lat              double precision,
    lon              double precision,
    h3               text,
    region           text,
    tz               text,
    -- к чему относится: [{"id":"currency:USD","role":"base"}]
    subject          jsonb not null default '[]',
    -- источник и доверие
    source_id        text not null references sources(id),
    source_class     text not null,
    asserted_by      text not null,
    confidence       real not null check (confidence between 0 and 1),
    verification     text not null default 'claimed' check (verification in ('claimed','corroborated','contradicted')),
    license          text not null,
    -- значение
    metric           text references metrics(id),
    value            jsonb,                            -- {"t":"money","amount":..,"currency":..}
    num              double precision,                 -- главное число значения для быстрых выборок
    aggregation      text,
    period           interval,
    precision        double precision,
    quality          text check (quality in ('measured','computed','estimated','imputed')),
    payload          jsonb not null default '{}',      -- доп. структурированные поля, без свободного текста
    -- связи
    chain_id         uuid,
    obligation_id    uuid,
    plan_id          uuid,
    caused_by        uuid,
    supersedes       uuid,
    raw_ref          text,
    raw_hash         text,
    -- служебное
    tenant           text not null default 'public',
    collector        text not null,
    collector_version text not null,
    dedup_key        text not null,
    content_hash     text not null,
    signature        text not null,
    primary key (id, occurred_at)
);
select create_hypertable('events', by_range('occurred_at', interval '7 days'), if_not_exists => true);

create index if not exists events_dedup_idx   on events (dedup_key, recorded_at desc);
create index if not exists events_source_idx  on events (source_id, recorded_at desc);
create index if not exists events_metric_idx  on events (metric, occurred_at desc);
create index if not exists events_subject_idx on events using gin (subject jsonb_path_ops);
create index if not exists events_h3_idx      on events (h3, occurred_at desc) where h3 is not null;

-- Сжатие старых чанков: экономим диск 40 ГБ
alter table events set (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'source_id',
    timescaledb.compress_orderby = 'occurred_at desc, recorded_at desc'
);
select add_compression_policy('events', interval '30 days', if_not_exists => true);

-- Планы: намерения и обещания, объявленные будущие изменения
create table if not exists plans (
    id               uuid primary key,
    parameter        text not null check (parameter in ('moment','duration','price','ratio','size','category')),
    metric           text references metrics(id),
    value            jsonb,
    window_from      timestamptz,
    window_to        timestamptz,
    tolerance        jsonb,                            -- {"minus":"PT30M","plus":"PT30M"} или {"rel":0.02}
    subject          jsonb not null default '[]',
    chain_id         uuid,
    obligation_id    uuid,
    target_at        timestamptz not null,
    basis            text not null check (basis in ('schedule','contract','tariff','announcement','norm')),
    asserted_by      text not null,
    source_id        text not null references sources(id),
    raw_ref          text,
    raw_hash         text,
    announced_at     timestamptz,
    recorded_at      timestamptz not null default now(),
    revision         int  not null default 1,
    supersedes       uuid,
    status           text not null default 'active' check (status in
                       ('active','changed','cancelled','fulfilled','violated','expired')),
    fulfilled_by     uuid,
    tenant           text not null default 'public',
    collector        text not null,
    dedup_key        text not null,
    content_hash     text not null,
    signature        text not null
);
create index if not exists plans_dedup_idx  on plans (dedup_key, recorded_at desc);
create index if not exists plans_target_idx on plans (target_at, status);

-- Отклонённые валидатором записи: причина и отпечаток, без содержимого
create table if not exists rejects (
    id           bigserial primary key,
    recorded_at  timestamptz not null default now(),
    source_id    text,
    collector    text,
    reason       text not null,
    content_hash text not null
);

-- Журнал запусков сборщиков (для Аны)
create table if not exists collector_runs (
    id            bigserial primary key,
    collector     text not null,
    source_id     text,
    started_at    timestamptz not null,
    finished_at   timestamptz,
    status        text not null check (status in ('ok','error','running')),
    written       int not null default 0,
    skipped       int not null default 0,
    rejected      int not null default 0,
    error         text
);
create index if not exists collector_runs_idx on collector_runs (collector, started_at desc);

-- Последняя подпись цепочки по каждому источнику
create table if not exists chain_heads (
    stream     text primary key,                       -- 'events:<source_id>' / 'plans:<source_id>'
    signature  text not null,
    updated_at timestamptz not null default now()
);
