-- ##############################################
-- dbt Mart Model for Daily Orders
-- Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
-- ##############################################

-- --- Defining SQL Objects

{{ config(alias='fct_orders_daily') }}

with orders as (

    select *
    from {{ ref('stg_orders') }}

),

aggregated_orders as (

    select
        dt,
        country,
        channel,
        count()                                                              as row_count,
        count()                                                              as order_count,
        uniqExact(order_id)                                                  as distinct_order_count,
        countIf(status = 'paid')                                             as paid_order_count,
        countIf(status = 'cancelled')                                        as cancelled_order_count,
        countIf(status = 'refunded')                                         as refunded_order_count,
        countIf(status = 'pending')                                          as pending_order_count,
        cast(sum(gross_amount_usd) as Decimal(18, 2))                        as gross_amount_usd,
        cast(sum(discount_usd) as Decimal(18, 2))                            as discount_usd,
        cast(sum(refund_amount_usd) as Decimal(18, 2))                       as refund_amount_usd,
        cast(sum(recognized_revenue_usd) as Decimal(18, 2))                  as recognized_revenue_usd,
        cast(count() - uniqExact(order_id) as UInt64)                        as duplicate_order_count,
        countIf(dateDiff('day', order_ts, ingestion_ts) > 0)                 as late_arriving_count

    from orders
    group by
        dt,
        country,
        channel

),

daily_orders as (

    select
        dt,
        country,
        channel,
        row_count,
        order_count,
        distinct_order_count,
        paid_order_count,
        cancelled_order_count,
        refunded_order_count,
        pending_order_count,
        gross_amount_usd,
        discount_usd,
        refund_amount_usd,
        recognized_revenue_usd,
        cast(
            if(distinct_order_count = 0, 0, gross_amount_usd / distinct_order_count)
            as Decimal(18, 2)
        )                                                                    as aov_usd,
        duplicate_order_count,
        late_arriving_count,
        now64(3)                                                             as updated_at

    from aggregated_orders

)

select *
from daily_orders
