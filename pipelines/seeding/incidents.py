####
## Incident Injection Functions for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

import random
from datetime import date, timedelta
from typing import Dict, List

from pipelines.common.logging import logger


INCIDENT_POOL = [
    "missing_segment",
    "duplicate_orders",
    "cancelled_revenue_leak",
    "fx_rate_spike",
    "late_arriving_batch",
    "missing_latest_day",
]


INCIDENT_PROBABILITIES = {
    "missing_segment": 0.32,
    "duplicate_orders": 0.24,
    "cancelled_revenue_leak": 0.22,
    "fx_rate_spike": 0.20,
    "late_arriving_batch": 0.26,
}


def choose_incidents(
    rng: random.Random,
    forced_incidents: List[str] | None = None,
    max_incidents: int = 2,
) -> List[str]:
    """
    Choose incident names to inject into generated order events.

    Args:
        rng: Random generator used for deterministic incident selection.
        forced_incidents: Optional explicit incident list from CLI/config.
        max_incidents: Maximum number of randomly selected incidents.

    Returns:
        List of incident names understood by INCIDENT_HANDLERS.
    """
    if forced_incidents:
        cleaned = [x.strip() for x in forced_incidents if x.strip()]
        logger.info("Using forced incident selection | incidents=%s", cleaned)

        return cleaned

    # missing_latest_day is mutually exclusive because it removes the target partition.
    if rng.random() < 0.12:
        selected = ["missing_latest_day"]
        logger.info("Random incident selected | incidents=%s", selected)

        return selected

    chosen: List[str] = []

    for name, probability in INCIDENT_PROBABILITIES.items():
        if rng.random() < probability:
            chosen.append(name)

    if not chosen:
        chosen = [rng.choice(INCIDENT_POOL[:-1])]

    selected = chosen[:max_incidents]
    logger.info("Random incidents selected | incidents=%s max_incidents=%d", selected, max_incidents)

    return selected


def apply_missing_segment(
    rows: List[Dict],
    incident_date: date,
    rng: random.Random,
) -> Dict:
    """
    Remove one country/channel segment for the incident date.

    Args:
        rows: Mutable list of raw order dictionaries.
        incident_date: Business date where the segment should be removed.
        rng: Random generator used to pick the affected segment.

    Returns:
        Incident result metadata including affected segment and removed volume.
    """
    target_rows = [row for row in rows if row["order_date"] == incident_date]

    if not target_rows:
        logger.info("Incident skipped | incident=missing_segment dt=%s reason=no_target_rows", incident_date)

        return {"incident": "missing_segment", "applied": False}

    available_segments               = {(row["country"], row["channel"]) for row in target_rows}
    missing_country, missing_channel = rng.choice(list(available_segments))

    removed_rows = [
        row
        for row in rows
        if row["order_date"] == incident_date
        and row["country"] == missing_country
        and row["channel"] == missing_channel
    ]
    removed_revenue = round(sum(row["recognized_revenue_usd"] for row in removed_rows), 2)

    rows[:] = [
        row
        for row in rows
        if not (
            row["order_date"] == incident_date
            and row["country"] == missing_country
            and row["channel"] == missing_channel
        )
    ]

    result = {
        "incident": "missing_segment",
        "applied": True,
        "incident_date": str(incident_date),
        "missing_country": missing_country,
        "missing_channel": missing_channel,
        "removed_orders": len(removed_rows),
        "removed_recognized_revenue_usd": removed_revenue,
    }
    logger.info("Incident applied | result=%s", result)

    return result


def apply_missing_latest_day(
    rows: List[Dict],
    incident_date: date,
) -> Dict:
    """
    Remove all rows for the latest business date.

    Args:
        rows: Mutable list of raw order dictionaries.
        incident_date: Business date to remove.

    Returns:
        Incident result metadata including removed row count.
    """
    before = len(rows)

    rows[:] = [row for row in rows if row["order_date"] != incident_date]
    removed = before - len(rows)

    result = {
        "incident": "missing_latest_day",
        "applied": True,
        "incident_date": str(incident_date),
        "removed_rows": removed,
    }
    logger.info("Incident applied | result=%s", result)

    return result


def apply_duplicate_orders(
    rows: List[Dict],
    incident_date: date,
    rng: random.Random,
) -> Dict:
    """
    Duplicate a sample of orders for the incident date.

    Args:
        rows: Mutable list of raw order dictionaries.
        incident_date: Business date where duplicates should be added.
        rng: Random generator used for sample selection and ingestion lag.

    Returns:
        Incident result metadata including duplicated row count.
    """
    candidates = [row for row in rows if row["order_date"] == incident_date]

    if not candidates:
        logger.info("Incident skipped | incident=duplicate_orders dt=%s reason=no_candidates", incident_date)

        return {"incident": "duplicate_orders", "applied": False}

    dup_count = max(1, int(len(candidates) * rng.uniform(0.03, 0.07)))
    sampled   = rng.sample(candidates, min(dup_count, len(candidates)))

    duplicated = []

    for row in sampled:
        cloned                 = dict(row)
        cloned["ingestion_ts"] = row["ingestion_ts"] + timedelta(minutes=rng.randint(1, 15))

        duplicated.append(cloned)

    rows.extend(duplicated)

    result = {
        "incident": "duplicate_orders",
        "applied": True,
        "incident_date": str(incident_date),
        "duplicated_orders": len(duplicated),
    }
    logger.info("Incident applied | result=%s", result)

    return result


def apply_cancelled_revenue_leak(
    rows: List[Dict],
    incident_date: date,
    rng: random.Random,
) -> Dict:
    """
    Make some cancelled/pending orders incorrectly contribute recognized revenue.

    Args:
        rows: Mutable list of raw order dictionaries.
        incident_date: Business date where revenue leakage should be injected.
        rng: Random generator used to sample affected rows.

    Returns:
        Incident result metadata including affected rows and leaked revenue.
    """
    candidates = [
        row
        for row in rows
        if row["order_date"] == incident_date
        and row["status"] in {"cancelled", "pending"}
    ]

    if not candidates:
        logger.info("Incident skipped | incident=cancelled_revenue_leak dt=%s reason=no_candidates", incident_date)

        return {"incident": "cancelled_revenue_leak", "applied": False}

    affected_count = max(1, int(len(candidates) * rng.uniform(0.18, 0.35)))
    sampled        = rng.sample(candidates, min(affected_count, len(candidates)))
    leaked_revenue = 0.0

    # Revenue leakage mutates business logic fields while keeping raw order identity stable.
    for row in sampled:
        leaked = max(row["gross_amount_usd"] - row["discount_usd"], 0.0)

        row["recognized_revenue_usd"] = round(leaked, 2)
        leaked_revenue               += row["recognized_revenue_usd"]

    result = {
        "incident": "cancelled_revenue_leak",
        "applied": True,
        "incident_date": str(incident_date),
        "affected_rows": len(sampled),
        "leaked_revenue_usd": round(leaked_revenue, 2),
    }
    logger.info("Incident applied | result=%s", result)

    return result


def apply_fx_rate_spike(
    rows: List[Dict],
    incident_date: date,
    rng: random.Random,
) -> Dict:
    """
    Inflate FX rates for a sample of one country's orders.

    Args:
        rows: Mutable list of raw order dictionaries.
        incident_date: Business date where FX spike should be injected.
        rng: Random generator used to pick country, rows, and multiplier.

    Returns:
        Incident result metadata including country, affected rows, and multiplier.
    """
    target_rows = [row for row in rows if row["order_date"] == incident_date]

    if not target_rows:
        logger.info("Incident skipped | incident=fx_rate_spike dt=%s reason=no_target_rows", incident_date)

        return {"incident": "fx_rate_spike", "applied": False}

    country      = rng.choice(sorted({row["country"] for row in target_rows}))
    country_rows = [row for row in target_rows if row["country"] == country]

    if not country_rows:
        logger.info(
            "Incident skipped | incident=fx_rate_spike dt=%s country=%s reason=no_country_rows",
            incident_date,
            country,
        )

        return {"incident": "fx_rate_spike", "applied": False}

    sample_size = max(1, int(len(country_rows) * rng.uniform(0.25, 0.45)))
    sampled     = rng.sample(country_rows, min(sample_size, len(country_rows)))
    multiplier  = rng.uniform(4.0, 6.0)

    # Recompute USD metrics after mutating FX so downstream anomalies are internally consistent.
    for row in sampled:
        row["fx_rate_to_usd"]     = round(row["fx_rate_to_usd"] * multiplier, 6)
        row["gross_amount_usd"]   = round(row["gross_amount_local"] * row["fx_rate_to_usd"], 2)

        if row["status"] == "paid":
            row["recognized_revenue_usd"] = round(max(row["gross_amount_usd"] - row["discount_usd"], 0.0), 2)

        elif row["status"] == "refunded":
            row["refund_amount_usd"]      = round(max(row["gross_amount_usd"] - row["discount_usd"], 0.0), 2)
            row["recognized_revenue_usd"] = 0.0

    result = {
        "incident": "fx_rate_spike",
        "applied": True,
        "incident_date": str(incident_date),
        "country": country,
        "affected_rows": len(sampled),
        "fx_multiplier": round(multiplier, 2),
    }
    logger.info("Incident applied | result=%s", result)

    return result


def apply_late_arriving_batch(
    rows: List[Dict],
    incident_date: date,
    rng: random.Random,
) -> Dict:
    """
    Delay ingestion timestamps for a country/channel batch.

    Args:
        rows: Mutable list of raw order dictionaries.
        incident_date: Business date where late-arriving data should be injected.
        rng: Random generator used to pick segment and sample affected rows.

    Returns:
        Incident result metadata including segment and late row count.
    """
    target_rows = [row for row in rows if row["order_date"] == incident_date]

    if not target_rows:
        logger.info("Incident skipped | incident=late_arriving_batch dt=%s reason=no_target_rows", incident_date)

        return {"incident": "late_arriving_batch", "applied": False}

    available_segments = {(row["country"], row["channel"]) for row in target_rows}
    country, channel   = rng.choice(list(available_segments))

    segment_rows = [
        row
        for row in target_rows
        if row["country"] == country and row["channel"] == channel
    ]

    if not segment_rows:
        logger.info(
            "Incident skipped | incident=late_arriving_batch dt=%s country=%s channel=%s reason=no_segment_rows",
            incident_date,
            country,
            channel,
        )

        return {"incident": "late_arriving_batch", "applied": False}

    sample_size = max(1, int(len(segment_rows) * rng.uniform(0.45, 0.75)))
    sampled     = rng.sample(segment_rows, min(sample_size, len(segment_rows)))

    for row in sampled:
        row["ingestion_ts"] = row["order_ts"] + timedelta(days=1, hours=rng.randint(5, 10))

    result = {
        "incident": "late_arriving_batch",
        "applied": True,
        "incident_date": str(incident_date),
        "country": country,
        "channel": channel,
        "late_rows": len(sampled),
    }
    logger.info("Incident applied | result=%s", result)

    return result


INCIDENT_HANDLERS = {
    "missing_segment": apply_missing_segment,
    "missing_latest_day": apply_missing_latest_day,
    "duplicate_orders": apply_duplicate_orders,
    "cancelled_revenue_leak": apply_cancelled_revenue_leak,
    "fx_rate_spike": apply_fx_rate_spike,
    "late_arriving_batch": apply_late_arriving_batch,
}


def apply_incidents(
    rows: List[Dict],
    incident_date: date,
    incidents: List[str],
    rng: random.Random,
) -> List[Dict]:
    """
    Apply one or more named incident handlers to generated rows.

    Args:
        rows: Mutable list of raw order dictionaries.
        incident_date: Business date targeted by incident injection.
        incidents: Incident names to apply in order.
        rng: Random generator used by incident handlers.

    Returns:
        List of incident result metadata dictionaries.

    Raises:
        KeyError: If an incident name is not registered in INCIDENT_HANDLERS.
    """
    results: List[Dict] = []

    logger.info("Applying incidents | dt=%s incidents=%s input_rows=%d", incident_date, incidents, len(rows))

    for incident in incidents:
        handler = INCIDENT_HANDLERS[incident]

        if incident == "missing_latest_day":
            result = handler(rows, incident_date)
        else:
            result = handler(rows, incident_date, rng)

        results.append(result)

    logger.info("Incidents applied | dt=%s output_rows=%d results=%s", incident_date, len(rows), results)

    return results
