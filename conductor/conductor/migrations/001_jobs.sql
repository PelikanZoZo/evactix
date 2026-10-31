-- Дирижёр: схема jobs, владелец — роль conductor. Ничего не удаляется: отмена — статус.

create table if not exists jobs.recipients (
    id          bigserial primary key,
    kind        text not null check (kind in ('telegram', 'webhook')),
    name        text not null,
    address     text not null,                     -- chat_id или URL
    secret      text,                              -- для подписи webhook
    active      boolean not null default true,
    created_at  timestamptz not null default now(),
    created_by  text not null,
    unique (kind, address)
);

create table if not exists jobs.jobs (
    id            bigserial primary key,
    title         text not null,
    instruction   text,                            -- для «выполнить»: что сделать (внутреннее)
    message       text,                            -- для «напомнить»: что отправить дословно
    action        text not null check (action in ('remind', 'do', 'builtin')),
    handler       text,                            -- для «встроенного»: 'collect:cbr.fx'
    -- автор
    author_type   text not null check (author_type in ('user', 'agent', 'ana', 'system', 'service')),
    author_id     text,
    author_name   text not null,
    source        text not null check (source in ('telegram', 'console', 'agent', 'system', 'api')),
    parent_job_id bigint references jobs.jobs(id),
    -- расписание
    schedule      text not null check (schedule in ('once', 'cron', 'interval', 'plan')),
    run_at        timestamptz,
    cron          text,
    interval_min  int,
    plan_source   text,                            -- запуск от плана: источник и ключ плана в ядре
    plan_key      text,
    plan_offset_min int,                           -- минус — до планового срока, плюс — после
    plan_target_at timestamptz,                    -- срок плана на момент расчёта
    until_at      timestamptz,
    max_runs      int,
    -- исполнение
    next_run_at   timestamptz,
    last_run_at   timestamptz,
    run_count     int not null default 0,
    fail_streak   int not null default 0,
    exec_status   text not null default 'scheduled' check (exec_status in
                    ('scheduled', 'running', 'succeeded', 'failed', 'retrying', 'skipped')),
    last_result   text,
    lock_until    timestamptz,
    -- получатели: [{"id": 1}] или [{"all": true}]
    recipients    jsonb not null default '[]',
    status        text not null default 'active' check (status in ('active', 'done', 'cancelled')),
    system_key    text unique,
    version       int not null default 1,
    created_at    timestamptz not null default now(),
    updated_at    timestamptz not null default now(),
    cancelled_by  text,
    cancelled_at  timestamptz
);
create index if not exists jobs_due_idx on jobs.jobs (next_run_at) where status = 'active';

create table if not exists jobs.history (
    id         bigserial primary key,
    job_id     bigint not null references jobs.jobs(id),
    version    int not null,
    action     text not null,                      -- create / update / cancel / restore / close
    by_name    text not null,
    source     text not null,
    diff       jsonb not null default '{}',        -- {"поле": [было, стало]}
    at         timestamptz not null default now()
);

create table if not exists jobs.runs (
    id          bigserial primary key,
    job_id      bigint not null references jobs.jobs(id),
    started_at  timestamptz not null default now(),
    finished_at timestamptz,
    status      text not null,                     -- succeeded / failed / skipped
    manual      boolean not null default false,
    result      text,
    sent        int not null default 0
);
create index if not exists runs_job_idx on jobs.runs (job_id, started_at desc);

-- Очередь отправки: всё, что уходит наружу
create table if not exists jobs.outbox (
    id           bigserial primary key,
    recipient_id bigint references jobs.recipients(id),
    chat_id      text,                             -- ответ в разговоре без получателя из списка
    job_id       bigint references jobs.jobs(id),
    text         text not null,
    payload      jsonb,
    status       text not null default 'pending' check (status in ('pending', 'sent', 'failed')),
    attempts     int not null default 0,
    next_try_at  timestamptz not null default now(),
    last_error   text,
    created_at   timestamptz not null default now(),
    sent_at      timestamptz
);
create index if not exists outbox_pending_idx on jobs.outbox (next_try_at) where status = 'pending';

-- Команды другим сервисам (сборщикам): Дирижёр ставит, исполнитель отмечает
create table if not exists jobs.commands (
    id          bigserial primary key,
    target      text not null,                     -- 'collector'
    name        text not null,                     -- 'cbr.fx'
    job_id      bigint references jobs.jobs(id),
    status      text not null default 'pending' check (status in ('pending', 'running', 'done', 'failed')),
    created_at  timestamptz not null default now(),
    taken_at    timestamptz,
    finished_at timestamptz,
    result      text
);
create index if not exists commands_pending_idx on jobs.commands (target, created_at) where status = 'pending';

-- Разговоры в Telegram: короткая память диалога и смещение чтения
create table if not exists jobs.chat_log (
    id       bigserial primary key,
    chat_id  text not null,
    role     text not null check (role in ('user', 'assistant')),
    text     text not null,
    at       timestamptz not null default now()
);
create index if not exists chat_log_idx on jobs.chat_log (chat_id, at desc);

create table if not exists jobs.kv (
    key        text primary key,
    value      jsonb not null,
    updated_at timestamptz not null default now()
);

-- Пульс Дирижёра — для Аны и сборщиков
create table if not exists jobs.heartbeat (
    id      int primary key default 1 check (id = 1),
    at      timestamptz not null default now(),
    version text
);

-- Доступы: Ана видит всё, сборщики — свои команды и пульс
grant usage on schema jobs to ana, collector;
grant select on all tables in schema jobs to ana;
alter default privileges in schema jobs grant select on tables to ana;
grant select, update (status, taken_at, finished_at, result) on jobs.commands to collector;
grant select on jobs.heartbeat to collector;
