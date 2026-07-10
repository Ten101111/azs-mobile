/*
  Daily KPI export for scripts/sync_kpi_metrics.py.

  Purpose:
    Export only aggregated station/day numbers from DWH to the public site.
    Raw checks, products, clients, cards, and transaction rows must not leave
    the corporate network.

  Hard lower bound:
    Data before 2024-01-01 is intentionally ignored.

  Params provided by the sync script:
    %(period)s        text, YYYY-MM
    %(period_start)s  date, already max(requested month start, min_date)
    %(period_end)s    date, first day of next month
    %(min_date)s      date, default 2024-01-01

  Result columns:
    metric_date       date/text YYYY-MM-DD
    ksss              station identifier matching the site station catalog
    revenue           revenue in rubles for the day
    revenue_ntu       NTU revenue used to calculate the average check
    fuel_volume       fuel volume in liters for the day
    checks            checks/receipts count for the day
    checks_ntu        NTU checks used to calculate the average check
    avg_check         daily NTU average check (diagnostic/fallback value)
*/

with params as (
  select
    greatest(%(period_start)s::date, %(min_date)s::date, date '2024-01-01') as period_start,
    %(period_end)s::date as period_end
),
ntu_goods as (
  select distinct gds_ksss
  from dds.s_gds_ksss_category
  where product_name_2 in (
    'Продовольственные товары',
    'Продукция кафе',
    'Непродовольственные товары'
  )
),
aggregated as (
  select
    so.ksss::text as ksss,
    pr.account_date::date as metric_date,
    sum(pr.amount_payment) as revenue,
    sum(pr.amount_payment) filter (where ng.gds_ksss is not null) as revenue_ntu,
    sum(pr.volume) as fuel_volume,
    count(distinct (pr.cheque_id, pr.cashregister_id, pr.cheque_number))
      filter (where pr.cheque_type = 0)
      - count(distinct (pr.cheque_id, pr.cashregister_id, pr.cheque_number))
      filter (where pr.cheque_type = 1) as checks,
    count(distinct (pr.cheque_id, pr.cashregister_id, pr.cheque_number))
      filter (where pr.cheque_type = 0 and ng.gds_ksss is not null)
      - count(distinct (pr.cheque_id, pr.cashregister_id, pr.cheque_number))
      filter (where pr.cheque_type = 1 and ng.gds_ksss is not null) as checks_ntu
  from stg_pet.pet_retail pr
  left join stg_pet.s_organization so
    on so.organization_id = pr.organization_id
  left join ntu_goods ng
    on ng.gds_ksss = pr.goods_ksss
  join params p on true
  where pr.account_date >= p.period_start
    and pr.account_date < p.period_end
    and so.ksss is not null
    and so.ksss::text <> ''
  group by so.ksss, pr.account_date::date
)
select
  metric_date,
  ksss,
  coalesce(revenue, 0) as revenue,
  coalesce(revenue_ntu, 0) as revenue_ntu,
  coalesce(fuel_volume, 0) as fuel_volume,
  coalesce(checks, 0) as checks,
  coalesce(checks_ntu, 0) as checks_ntu,
  round(revenue_ntu / nullif(checks_ntu, 0), 0) as avg_check
from aggregated
order by ksss, metric_date;
