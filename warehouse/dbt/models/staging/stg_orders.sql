-- ##############################################
-- dbt Staging Model for Orders
-- Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
-- ##############################################

-- --- Defining SQL Objects

{{ config(alias='stg_orders') }}

with source_orders as (

    select
        dt,
        order_id,
        order_date,
        order_ts,
        ingestion_ts,
        customer_id,
        country,
        channel,
        status,
        currency,
        gross_amount_local,
        fx_rate_to_usd,
        gross_amount_usd,
        discount_usd,
        refund_amount_usd,
        recognized_revenue_usd,
        source_system,
        is_test,
        business_date_version,
        incident_scenario

    from {{ source('raw', 'raw_orders') }}

    where dt between toDate('{{ var("start_dt") }}') and toDate('{{ var("end_dt") }}')
      and is_test = 0

),

typed_orders as (

    select
        cast(dt as Date)                                                       as dt,
        cast(order_id as String)                                               as order_id,
        cast(order_date as Date)                                               as order_date,
        cast(order_ts as DateTime64(3, 'UTC'))                                 as order_ts,
        cast(ingestion_ts as DateTime64(3, 'UTC'))                             as ingestion_ts,
        cast(customer_id as String)                                            as customer_id,
        cast(country as LowCardinality(String))                                as country,
        cast(channel as LowCardinality(String))                                as channel,
        cast(status as LowCardinality(String))                                 as status,
        cast(currency as LowCardinality(String))                               as currency,
        cast(gross_amount_local as Decimal(18, 2))                             as gross_amount_local,
        cast(fx_rate_to_usd as Decimal(18, 6))                                 as fx_rate_to_usd,
        cast(gross_amount_usd as Decimal(18, 2))                               as gross_amount_usd,
        cast(discount_usd as Decimal(18, 2))                                   as discount_usd,
        cast(refund_amount_usd as Decimal(18, 2))                              as refund_amount_usd,
        cast(recognized_revenue_usd as Decimal(18, 2))                         as recognized_revenue_usd,
        cast(gross_amount_usd - discount_usd - refund_amount_usd as Decimal(18, 2)) as net_amount_usd,
        cast(source_system as LowCardinality(String))                          as source_system,
        cast(is_test as UInt8)                                                 as is_test,
        cast(business_date_version as UInt32)                                  as business_date_version,
        cast(incident_scenario as String)                                      as incident_scenario,
        now64(3)                                                              as transformed_at

    from source_orders

)

select *
from typed_orders

