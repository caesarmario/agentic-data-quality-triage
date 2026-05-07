-- ##############################################
-- dbt Reconciliation Test for Daily Orders
-- Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
-- ##############################################

-- --- Defining SQL Objects

with mart_counts as (

    select
        dt,
        country,
        channel,
        sum(row_count)                  as mart_row_count,
        sum(distinct_order_count)        as mart_distinct_order_count

    from {{ ref('fct_orders_daily') }}
    group by
        dt,
        country,
        channel

),

staging_counts as (

    select
        dt,
        country,
        channel,
        count()                         as staging_row_count,
        uniqExact(order_id)             as staging_distinct_order_count

    from {{ ref('stg_orders') }}
    group by
        dt,
        country,
        channel

)

select
    coalesce(mart_counts.dt, staging_counts.dt)             as dt,
    coalesce(mart_counts.country, staging_counts.country)   as country,
    coalesce(mart_counts.channel, staging_counts.channel)   as channel,
    mart_counts.mart_row_count,
    staging_counts.staging_row_count,
    mart_counts.mart_distinct_order_count,
    staging_counts.staging_distinct_order_count

from mart_counts
full outer join staging_counts
    on mart_counts.dt = staging_counts.dt
   and mart_counts.country = staging_counts.country
   and mart_counts.channel = staging_counts.channel

where coalesce(mart_counts.mart_row_count, 0) != coalesce(staging_counts.staging_row_count, 0)
   or coalesce(mart_counts.mart_distinct_order_count, 0) != coalesce(staging_counts.staging_distinct_order_count, 0)

