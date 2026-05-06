-- ##############################################
-- dbt Freshness Test Macro for Agentic Data Quality Triage
-- Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
-- ##############################################

{% test freshness_by_date(model, date_column, max_days_old=7) %}

with latest_partition as (

    select
        count()                 as row_count,
        max({{ date_column }})  as max_dt

    from {{ model }}

)

select
    row_count,
    max_dt,
    today()                    as evaluated_on,
    {{ max_days_old }}          as max_days_old

from latest_partition

-- Empty tables and stale tables should both fail this test.
where row_count = 0
   or max_dt < subtractDays(today(), {{ max_days_old }})

{% endtest %}

