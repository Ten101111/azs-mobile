/*
  Current fuel stock export for scripts/sync_fuel_stock.py.

  This query runs only on the corporate machine. Individual tank rows are
  deduplicated, normalized and aggregated locally; the public server receives
  only one row per (ksss, canonical fuel).

  Params:
    %(account_date_start)s  inclusive timestamp/date lower bound
    %(account_date_end)s    exclusive timestamp/date upper bound
*/

with azs as (
    select
        enterprise_ksss,
        ksss,
        name,
        npo,
        region_name_rep
    from (
        select
            *,
            dense_rank() over w as rang
        from (
            select distinct
                enterprise_ksss,
                ksss,
                name,
                npo,
                filial_id,
                region_id
            from stg_pet.s_organization org
            where organization_type_id = 1
              and npo in ('ЦНП', 'ЛИКАРД ФРН', 'ЛИКАРД', 'УНП', 'ЮНП', 'СЗНП')
              and (
                  regional_manager is not null
                  or enterprise_ksss = 4111569
                  or enterprise_ksss = 4096186
              )
              and enterprise_ksss is not null
              and ksss is not null
              and isclosed = '0'
        ) t
        window w as (
            partition by enterprise_ksss
            order by filial_id desc
        )
    ) tab
    left join stg_mob_app.region reg
      on tab.region_id = reg.region_id
    left join bds.s_region sreg
      on reg.region_code = sreg.region_code
    where rang = 1
)
select
    pr.account_date::date as account_date,
    azs.ksss::text as ksss,
    pr.ent_name_crc::text as ent_name_crc,
    pr.num_stor::text as num_stor,
    pr.dt_ins,
    sprgn.gds_name as fuel_name,
    pr.oil_tn,
    pr.fact_volume,
    case
        when pr.dead_rest_vol <> 0 and pr.oil_volume <> 0
            then round(pr.dead_rest_vol * pr.oil_tn / pr.oil_volume, 2)
        else 0
    end as dead_rest
from stg_pet.pet_report_9312 pr
left join stg_pet.s_pet_report_9312_ent_name spren
  on pr.ent_name_crc = spren.ent_name_crc
left join stg_pet.s_pet_report_9312_gds_name sprgn
  on pr.gds_name_crc = sprgn.gds_name_crc
left join azs
  on spren.ent_name = azs.name
where pr.account_date >= %(account_date_start)s
  and pr.account_date < %(account_date_end)s
order by azs.ksss, sprgn.gds_name, pr.num_stor;
