-- Дирижёр — отдельный сервис. Ядро выдаёт ему ключ (роль conductor) и отдельную схему jobs,
-- которой он владеет сам. Данные ядра Дирижёр только читает.

do $$ begin
  if not exists (select from pg_roles where rolname = 'conductor') then create role conductor login; end if;
end $$;

grant connect on database evactix to conductor;
grant usage on schema public to conductor;
grant select on all tables in schema public to conductor;
alter default privileges in schema public grant select on tables to conductor;

create schema if not exists jobs authorization conductor;
