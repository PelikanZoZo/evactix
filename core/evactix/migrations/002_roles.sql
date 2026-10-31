-- Роли: у сборщиков и у Аны свои ключи и свои права.
-- Пароли выставляет migrate.py из переменных окружения.

do $$ begin
  if not exists (select from pg_roles where rolname = 'collector') then create role collector login; end if;
  if not exists (select from pg_roles where rolname = 'ana')       then create role ana login; end if;
end $$;

grant connect on database evactix to collector, ana;
grant usage on schema public to collector, ana;

-- Сборщики: читают справочники, дописывают события и планы. Ни update, ни delete.
grant select on sources, metrics, entities, events, plans, chain_heads, collector_runs to collector;
grant insert on sources, metrics, entities, events, plans, rejects, collector_runs to collector;
grant update (finished_at, status, written, skipped, rejected, error) on collector_runs to collector;
grant insert, update on chain_heads to collector;
grant update (name, source_class, base_confidence, license, url, max_age) on sources to collector;
grant update (value_type, unit, aggregation, description) on metrics to collector;
grant usage on sequence rejects_id_seq, collector_runs_id_seq to collector;

-- Ана — отдельный проект рядом с ядром. Ядро только выдаёт ей ключ на чтение;
-- своё состояние Ана хранит у себя.
grant select on all tables in schema public to ana;
alter default privileges in schema public grant select on tables to ana;
