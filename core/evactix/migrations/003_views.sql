-- Текущее состояние: последняя версия каждой записи (поправки дописываются, старые остаются в истории)

create or replace view plans_current as
select distinct on (tenant, dedup_key) *
from plans
order by tenant, dedup_key, recorded_at desc;

grant select on plans_current to collector, ana;
