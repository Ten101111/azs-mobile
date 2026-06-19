/*
  KPI query template for GET /api/stations/{ksss}/kpis.

  Expected params:
    %(ksss)s
    %(period_start)s
    %(period_end)s
    %(previous_period_start)s
    %(previous_year_start)s

  Expected output columns:
    revenue, revenue_mom_pct, revenue_yoy_pct
    fuel_volume, fuel_volume_mom_pct, fuel_volume_yoy_pct
    checks, checks_mom_pct, checks_yoy_pct
    avg_check, avg_check_mom_pct, avg_check_yoy_pct

  Replace table and column names after the operational schema is provided.
*/

select
  0::numeric as revenue,
  0::numeric as revenue_mom_pct,
  0::numeric as revenue_yoy_pct,
  0::numeric as fuel_volume,
  0::numeric as fuel_volume_mom_pct,
  0::numeric as fuel_volume_yoy_pct,
  0::numeric as checks,
  0::numeric as checks_mom_pct,
  0::numeric as checks_yoy_pct,
  0::numeric as avg_check,
  0::numeric as avg_check_mom_pct,
  0::numeric as avg_check_yoy_pct
where %(ksss)s is not null
  and %(period_start)s is not null
  and %(period_end)s is not null
  and %(previous_period_start)s is not null
  and %(previous_year_start)s is not null;
