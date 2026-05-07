-- ##############################################
-- dbt Unique Grain Test for Daily Orders
-- Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
-- ##############################################

-- --- Defining SQL Objects

select
    dt,
    country,
    channel,
    count() as duplicate_rows

from {{ ref('fct_orders_daily') }}

group by
    dt,
    country,
    channel

having count() > 1

