-- Паспорт установки: версия Evactix и выкатки. Пишет migrate.py, читают все (в том числе Ана).
create table if not exists system_info (
    key        text primary key,
    value      text not null,
    updated_at timestamptz not null default now()
);
grant select on system_info to collector, ana;
