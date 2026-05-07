-- ##############################################
-- dbt Rowcount Anomaly Test Macro for Agentic Data Quality Triage
-- Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
-- ##############################################

-- --- Defining SQL Objects

{% test rowcount_anomaly(
    model,
    date_column,
    lookback_days=7,
    min_history_days=3,
    lower_ratio=0.50,
    upper_ratio=1.80
) %}

with daily_counts as (

    select
        {{ date_column }}       as dt,
        sum(row_count)          as row_count

    from {{ model }}
    group by {{ date_column }}

),

latest_partition as (

    select max(dt) as current_dt
    from daily_counts

),

current_count as (

    select
        daily_counts.dt,
        daily_counts.row_count

    from daily_counts
    inner join latest_partition
        on daily_counts.dt = latest_partition.current_dt

),

history as (

    select
        count()                 as history_days,
        avg(row_count)          as avg_row_count

    from daily_counts
    cross join latest_partition

    where daily_counts.dt >= subtractDays(latest_partition.current_dt, {{ lookback_days }})
      and daily_counts.dt < latest_partition.current_dt

)

select
    current_count.dt,
    current_count.row_count     as current_row_count,
    history.avg_row_count,
    history.history_days,
    {{ lower_ratio }}           as lower_ratio,
    {{ upper_ratio }}           as upper_ratio

from current_count
cross join history

-- Require enough history before flagging anomalies to avoid noisy first-run failures.
where history.history_days >= {{ min_history_days }}
  and (
        current_count.row_count < history.avg_row_count * {{ lower_ratio }}
     or current_count.row_count > history.avg_row_count * {{ upper_ratio }}
  )

{% endtest %}

