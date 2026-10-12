"""Plivo telephony cost per call, from Plivo's call records.

Design: docs/spec/call-analysis-design.md §2.4. The hangup callback carries no reliable cost, so
a minute after hangup the call record (calls.get, field total_amount) is read for the call and for
every human leg of a transfer (DialBLegUUID), and the sum is stored. Main process only.
"""

import asyncio
import logging
import os

import call_store

logger = logging.getLogger("plivo_cost")


def _flag(name, default):
    value = os.getenv(name, default).strip()
    if value not in ("0", "1"):
        raise ValueError(f"{name} must be '0' or '1', got {value!r}")
    return value == "1"


PLIVO_COST_ENABLED = _flag("PLIVO_COST_ENABLED", "1")
FETCH_DELAYS_S = (60, 180, 600)   # first look a minute after hangup, then 3 and 10 minutes later

BILLING_FIELDS = ("total_amount", "total_rate", "bill_duration", "billed_duration", "call_duration")

_tasks = set()            # running fetches, held so they are not garbage-collected
_logged_fields = False    # the first record's field names are logged once, to confirm them live


def _get(record, name):
    if isinstance(record, dict):
        return record.get(name)
    return getattr(record, name, None)


def _field_names(record):
    return sorted(record.keys()) if isinstance(record, dict) else sorted(vars(record))


def amount(record):
    """total_amount as a float, or None when the record has none (yet)."""
    if record is None:
        return None
    value = _get(record, "total_amount")
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


async def fetch_cost(db_path, call_uuid, get_call, *, delays=FETCH_DELAYS_S, sleep=asyncio.sleep):
    try:
        return await _fetch_cost(db_path, call_uuid, get_call, delays=delays, sleep=sleep)
    except Exception as e:   # a database error must not die silently in a detached task
        logger.error(f"❌ [PLIVO-COST] call={call_uuid[:8]} fetch crashed: {type(e).__name__}")
        return None


async def _fetch_cost(db_path, call_uuid, get_call, *, delays, sleep):
    global _logged_fields
    tag = call_uuid[:8]
    await call_store.run(db_path, call_store.set_plivo_cost, call_uuid, None, "pending")
    legs = 1
    for delay in delays:
        await sleep(delay)
        uuids = [call_uuid] + await call_store.run(db_path, call_store.b_legs, call_uuid)
        legs = len(uuids)
        amounts = []
        for uuid in uuids:
            try:
                record = await asyncio.to_thread(get_call, uuid)
            except Exception as e:
                logger.warning(f"⚠️ [PLIVO-COST] call={tag} lookup failed: {type(e).__name__}")
                record = None
            if record is not None and not _logged_fields:
                _logged_fields = True
                logger.info(f"💰 [PLIVO-COST] record fields: {_field_names(record)}")
                # Billing values only (no numbers or names), to check the unit/currency live (spec §2.4).
                billing = {name: _get(record, name) for name in BILLING_FIELDS}
                logger.info(f"💰 [PLIVO-COST] record billing: {billing}")
            amounts.append(amount(record))
        if all(value is not None for value in amounts):
            total = sum(amounts)
            await call_store.run(db_path, call_store.set_plivo_cost, call_uuid, total, "ok")
            logger.info(f"💰 [PLIVO-COST] call={tag} status=ok total=${total:.5f} legs={legs}")
            return total
    await call_store.run(db_path, call_store.set_plivo_cost, call_uuid, None, "failed")
    logger.warning(f"💰 [PLIVO-COST] call={tag} status=failed legs={legs}")
    return None


def schedule(db_path, call_uuid, get_call, **kwargs):
    task = asyncio.get_running_loop().create_task(fetch_cost(db_path, call_uuid, get_call, **kwargs))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return task
