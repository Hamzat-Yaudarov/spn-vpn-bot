"""Persistent contest enrollment and a single, uniform draw per campaign."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import re
from secrets import SystemRandom
from urllib.parse import urlsplit

import database as db


MIN_SUBSCRIPTION_DAYS = 45


@dataclass(frozen=True)
class Prize:
    rank: int
    kind: str
    amount: int


PRIZES = tuple(
    Prize(rank, "cash" if rank <= 3 else "subscription", amount)
    for rank, amount in enumerate((20_000, 10_000, 5_000, 365, 180, 90, 30, 30, 30, 30), 1)
)


@dataclass(frozen=True)
class Enrollment:
    status: str
    count: int


@dataclass(frozen=True)
class Place:
    prize: Prize
    tg_id: int | None
    status: str
    reasons: tuple[str, ...]
    subscription_until: datetime | None
    paid_invites: int | None


@dataclass(frozen=True)
class DrawResult:
    campaign: dict
    participant_count: int
    paid_invites: int
    places: tuple[Place, ...]


SCHEMA = """
CREATE TABLE IF NOT EXISTS contests (
    slug TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    starts_at TIMESTAMPTZ NOT NULL,
    ends_at TIMESTAMPTZ NOT NULL CHECK (ends_at > starts_at),
    channel_chat_id TEXT,
    rules_url TEXT,
    published_at TIMESTAMPTZ CHECK (published_at IS NULL OR
        (published_at < starts_at AND rules_url IS NOT NULL AND channel_chat_id IS NOT NULL)),
    drawn_at TIMESTAMPTZ CHECK (drawn_at IS NULL OR (drawn_at >= ends_at AND published_at IS NOT NULL)),
    drawn_participant_count BIGINT,
    drawn_paid_invites BIGINT
);
CREATE TABLE IF NOT EXISTS contest_participants (
    contest_slug TEXT NOT NULL REFERENCES contests(slug),
    tg_id BIGINT NOT NULL CHECK (tg_id > 0),
    joined_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (contest_slug, tg_id)
);
CREATE INDEX IF NOT EXISTS contest_participants_by_user ON contest_participants(tg_id, contest_slug);
CREATE TABLE IF NOT EXISTS contest_subscription_cutoffs (
    contest_slug TEXT NOT NULL,
    tg_id BIGINT NOT NULL,
    subscription_id BIGINT NOT NULL,
    subscription_until TIMESTAMP,
    PRIMARY KEY (contest_slug, subscription_id),
    FOREIGN KEY (contest_slug, tg_id) REFERENCES contest_participants(contest_slug, tg_id)
);
CREATE TABLE IF NOT EXISTS contest_places (
    contest_slug TEXT NOT NULL REFERENCES contests(slug),
    rank INT NOT NULL CHECK (rank BETWEEN 1 AND 10),
    prize_kind TEXT NOT NULL CHECK (prize_kind IN ('cash', 'subscription')),
    prize_amount INT NOT NULL CHECK (prize_amount > 0),
    tg_id BIGINT,
    status TEXT NOT NULL CHECK (status IN ('won', 'forfeited', 'unfilled')),
    reasons TEXT[] NOT NULL,
    subscription_until TIMESTAMP,
    paid_invites BIGINT,
    PRIMARY KEY (contest_slug, rank),
    UNIQUE (contest_slug, tg_id),
    FOREIGN KEY (contest_slug, tg_id) REFERENCES contest_participants(contest_slug, tg_id),
    CHECK ((status = 'unfilled') = (tg_id IS NULL))
);

ALTER TABLE contests ENABLE ROW LEVEL SECURITY;
ALTER TABLE contest_participants ENABLE ROW LEVEL SECURITY;
ALTER TABLE contest_subscription_cutoffs ENABLE ROW LEVEL SECURITY;
ALTER TABLE contest_places ENABLE ROW LEVEL SECURITY;

-- The first change after the deadline preserves the row as it was at the cutoff.
-- A NULL expiration suppresses a new row or an existing ineligible row.
CREATE OR REPLACE FUNCTION contest_keep_cutoff_subscription() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    owner_id BIGINT;
    changed_id BIGINT;
    prior_until TIMESTAMP;
    event_at TIMESTAMPTZ := clock_timestamp();
BEGIN
    IF NOT EXISTS (SELECT 1 FROM contests WHERE published_at IS NOT NULL
                   AND ends_at <= event_at AND drawn_at IS NULL) THEN
        RETURN NULL;
    END IF;
    IF TG_OP = 'INSERT' THEN
        owner_id := NEW.tg_id;
        changed_id := NEW.id;
    ELSE
        owner_id := OLD.tg_id;
        changed_id := OLD.id;
        IF OLD.is_active AND OLD.remnawave_uuid IS NOT NULL AND
           ((OLD.generation = 'v2' AND OLD.is_visible) OR OLD.legacy_readonly) THEN
            prior_until := OLD.subscription_until;
        END IF;
    END IF;

    -- A join accepted just before the cutoff may still be committing.
    PERFORM pg_advisory_xact_lock_shared(hashtext('contest-enrollment'), hashint8(owner_id));
    INSERT INTO contest_subscription_cutoffs (contest_slug, tg_id, subscription_id, subscription_until)
    SELECT c.slug, owner_id, changed_id, prior_until
    FROM contest_participants AS p JOIN contests AS c ON c.slug = p.contest_slug
    WHERE p.tg_id = owner_id AND c.published_at IS NOT NULL
      AND c.ends_at <= event_at AND c.drawn_at IS NULL
    ON CONFLICT (contest_slug, subscription_id) DO NOTHING;
    RETURN NULL;
END;
$$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'contest_keep_cutoff_subscription'
                   AND tgrelid = 'subscriptions'::regclass) THEN
        CREATE TRIGGER contest_keep_cutoff_subscription
        AFTER INSERT OR DELETE OR UPDATE OF subscription_until, is_active, remnawave_uuid,
            generation, is_visible, legacy_readonly ON subscriptions
        FOR EACH ROW EXECUTE FUNCTION contest_keep_cutoff_subscription();
    END IF;
END;
$$;
"""


async def initialize(conn):
    await conn.execute(SCHEMA)


def _validate_rules_url(url):
    if url is None:
        return
    if not isinstance(url, str) or len(url) > 2048 or any(char.isspace() for char in url):
        raise ValueError("Укажите HTTPS-ссылку на публичные правила.")
    try:
        parsed = urlsplit(url)
        if parsed.scheme == "https" and parsed.hostname and parsed.username is None and parsed.password is None:
            return
    except ValueError:
        pass
    raise ValueError("Укажите HTTPS-ссылку на публичные правила.")


def _channel_reference(value):
    reference = str(value).strip()
    if re.fullmatch(r"@[a-zA-Z][a-zA-Z0-9_]{4,31}", reference):
        return reference
    if re.fullmatch(r"-100[0-9]{4,13}", reference) and int(reference) > -(2**52):
        return reference
    raise ValueError("Укажите @имя канала или его числовой ID, начинающийся с -100.")


async def create_campaign(slug, title, starts_at, ends_at, *, channel_chat_id=None, rules_url=None):
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,39}", slug):
        raise ValueError("Идентификатор конкурса должен содержать от 1 до 40 символов a–z, 0–9, _ или -.")
    title = title.strip()
    if not 1 <= len(title) <= 120:
        raise ValueError("Укажите название конкурса до 120 символов.")
    if (starts_at.utcoffset() is None or ends_at.utcoffset() is None
            or ends_at.astimezone(timezone.utc) <= starts_at.astimezone(timezone.utc)):
        raise ValueError("Укажите время начала и окончания с часовым поясом, в порядке возрастания.")
    if channel_chat_id is not None:
        channel_chat_id = _channel_reference(channel_chat_id)
    _validate_rules_url(rules_url)
    row = await db.db_execute("""
        INSERT INTO contests (slug, title, starts_at, ends_at, channel_chat_id, rules_url)
        VALUES ($1, $2, $3, $4, $5, $6)
        ON CONFLICT (slug) DO NOTHING RETURNING *
    """, (slug, title, starts_at, ends_at, channel_chat_id, rules_url), fetch_one=True)
    if row is None:
        raise ValueError("Такой идентификатор конкурса уже существует.")
    return row


async def get_campaign(slug):
    return await db.db_execute("SELECT * FROM contests WHERE slug = $1", (slug,), fetch_one=True)


async def publish_campaign(slug, *, resolved_channel_chat_id=None):
    pool = await db.get_pool()
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(hashtext('contest-publish'))")
        campaign = await conn.fetchrow("SELECT * FROM contests WHERE slug = $1 FOR UPDATE", slug)
        if campaign is None:
            raise ValueError("Конкурс не найден.")
        if campaign["published_at"] is not None:
            return campaign
        if campaign["rules_url"] is None:
            raise ValueError("Перед публикацией добавьте ссылку на публичные правила.")
        configured_channel = campaign["channel_chat_id"]
        if configured_channel is None:
            raise ValueError("Перед публикацией укажите канал конкурса.")
        channel_id = configured_channel if resolved_channel_chat_id is None else _channel_reference(resolved_channel_chat_id)
        if not channel_id.startswith("-"):
            raise ValueError("Перед публикацией подтвердите числовой ID канала.")
        if configured_channel.startswith("-") and channel_id != configured_channel:
            raise ValueError("Подтверждённый ID не совпадает с указанным каналом конкурса.")
        if await conn.fetchval("""
            SELECT EXISTS (SELECT 1 FROM contests WHERE published_at IS NOT NULL
                           AND starts_at < $2 AND ends_at > $1)
        """, campaign["starts_at"], campaign["ends_at"]):
            raise ValueError("На это время уже запланирован опубликованный конкурс.")
        published = await conn.fetchrow("""
            WITH stamp AS (SELECT clock_timestamp() AS at)
            UPDATE contests SET published_at = stamp.at, channel_chat_id = $2 FROM stamp
            WHERE slug = $1 AND stamp.at < starts_at RETURNING contests.*
        """, slug, channel_id)
        if published is None:
            raise ValueError("Публикация доступна до начала конкурса.")
        return published


async def get_current_campaign():
    return await db.db_execute("""
        SELECT * FROM contests WHERE published_at IS NOT NULL AND drawn_at IS NULL
          AND starts_at <= statement_timestamp() AND ends_at > statement_timestamp()
        ORDER BY starts_at DESC LIMIT 1
    """, fetch_one=True)


COUNT_QUERY = "SELECT COUNT(*) FROM contest_participants WHERE contest_slug = $1"


async def count_participants(slug):
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval(COUNT_QUERY, slug)


async def enroll(slug, tg_id):
    pool = await db.get_pool()
    async with pool.acquire() as conn, conn.transaction():
        campaign = await conn.fetchrow("SELECT * FROM contests WHERE slug = $1 FOR SHARE", slug)
        if campaign is None or campaign["published_at"] is None:
            return Enrollment("not_found", 0)
        if type(tg_id) is not int or not 0 < tg_id <= 9_223_372_036_854_775_807:
            return Enrollment("unknown_user", await conn.fetchval(COUNT_QUERY, slug))
        await conn.execute("SELECT pg_advisory_xact_lock(hashtext('contest-enrollment'), hashint8($1))", tg_id)
        now = await conn.fetchval("SELECT clock_timestamp()")
        if not campaign["starts_at"] <= now < campaign["ends_at"]:
            status = "closed"
        elif not await conn.fetchval("SELECT EXISTS (SELECT 1 FROM users WHERE tg_id = $1)", tg_id):
            status = "unknown_user"
        else:
            inserted = await conn.fetchval("""
                INSERT INTO contest_participants (contest_slug, tg_id, joined_at)
                VALUES ($1, $2, $3) ON CONFLICT (contest_slug, tg_id) DO NOTHING RETURNING TRUE
            """, slug, tg_id, now)
            status = "joined" if inserted else "already_joined"
        return Enrollment(status, await conn.fetchval(COUNT_QUERY, slug))


DRAW_EVIDENCE = """
WITH enrolled AS (
    SELECT tg_id FROM contest_participants WHERE contest_slug = $1
), cutoff_subscriptions AS (
    SELECT s.tg_id, s.subscription_until
    FROM subscriptions AS s JOIN enrolled AS e ON e.tg_id = s.tg_id
    WHERE s.is_active AND s.remnawave_uuid IS NOT NULL
      AND ((s.generation = 'v2' AND s.is_visible) OR s.legacy_readonly)
      AND NOT EXISTS (
          SELECT 1 FROM contest_subscription_cutoffs AS old
          WHERE old.contest_slug = $1 AND old.subscription_id = s.id
      )
    UNION ALL
    SELECT tg_id, subscription_until FROM contest_subscription_cutoffs WHERE contest_slug = $1
), expirations AS (
    SELECT tg_id, MAX(subscription_until) AS subscription_until FROM cutoff_subscriptions GROUP BY tg_id
), paid_invites AS (
    SELECT settlement.referrer_id, COUNT(DISTINCT settlement.payer_id) AS paid_invites
    FROM referral_payment_settlements AS settlement
    JOIN enrolled AS e ON e.tg_id = settlement.referrer_id
    JOIN users AS friend ON friend.tg_id = settlement.payer_id AND friend.referrer_id = settlement.referrer_id
    WHERE settlement.qualified AND settlement.is_first_purchase
      AND friend.created_at >= $2 AND friend.created_at < $3
      AND settlement.created_at >= $2 AND settlement.created_at < $3
      AND NOT EXISTS (
          SELECT 1 FROM referral_confirmed_refunds AS refund
          WHERE refund.payment_id = settlement.payment_id AND refund.created_at < $3
      )
    GROUP BY settlement.referrer_id
)
SELECT e.tg_id, expiry.subscription_until, COALESCE(invite.paid_invites, 0) AS paid_invites
FROM enrolled AS e LEFT JOIN expirations AS expiry ON expiry.tg_id = e.tg_id
LEFT JOIN paid_invites AS invite ON invite.referrer_id = e.tg_id
"""


def _select_places(participants, deadline):
    # Sample from all entrants before testing either requirement. A place is final
    # even if the selected participant is ineligible.
    selected = SystemRandom().sample(participants, min(len(participants), len(PRIZES)))
    minimum_expiry = deadline + timedelta(days=MIN_SUBSCRIPTION_DAYS)
    total_invites = sum(entry["paid_invites"] for entry in participants)
    places = []
    for index, prize in enumerate(PRIZES):
        if index >= len(selected):
            places.append(Place(prize, None, "unfilled", (), None, None))
            continue
        entry = selected[index]
        expiry = entry["subscription_until"]
        reasons = []
        if expiry is None or expiry < minimum_expiry:
            reasons.append("subscription_short")
        if not entry["paid_invites"]:
            reasons.append("no_paid_invite")
        places.append(Place(prize, entry["tg_id"], "forfeited" if reasons else "won",
                            tuple(reasons), expiry, entry["paid_invites"]))
    return total_invites, tuple(places)


async def _get_result(conn, campaign):
    rows = await conn.fetch("SELECT * FROM contest_places WHERE contest_slug = $1 ORDER BY rank", campaign["slug"])
    places = tuple(
        Place(Prize(row["rank"], row["prize_kind"], row["prize_amount"]), row["tg_id"], row["status"],
              tuple(row["reasons"]), row["subscription_until"], row["paid_invites"])
        for row in rows
    )
    return DrawResult(dict(campaign), campaign["drawn_participant_count"], campaign["drawn_paid_invites"], places)


async def get_draw_result(slug):
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        campaign = await conn.fetchrow("SELECT * FROM contests WHERE slug = $1 AND drawn_at IS NOT NULL", slug)
        return await _get_result(conn, campaign) if campaign else None


async def draw(slug):
    pool = await db.get_pool()
    async with pool.acquire() as conn, conn.transaction():
        campaign = await conn.fetchrow("SELECT * FROM contests WHERE slug = $1 FOR UPDATE", slug)
        if campaign is None or campaign["published_at"] is None:
            raise ValueError("Опубликованный конкурс не найден.")
        if campaign["drawn_at"] is not None:
            return await _get_result(conn, campaign)
        now = await conn.fetchval("SELECT clock_timestamp()")
        if now < campaign["ends_at"]:
            raise ValueError("Жеребьёвка доступна после окончания конкурса.")
        # Business tables use naive UTC timestamps. Query evidence together so a
        # concurrent update and its cutoff record appear in the same MVCC view.
        start = campaign["starts_at"].astimezone(timezone.utc).replace(tzinfo=None)
        deadline = campaign["ends_at"].astimezone(timezone.utc).replace(tzinfo=None)
        participants = await conn.fetch(DRAW_EVIDENCE, slug, start, deadline)
        paid_invites, places = _select_places(participants, deadline)
        await conn.executemany("""
            INSERT INTO contest_places (contest_slug, rank, prize_kind, prize_amount, tg_id,
                                        status, reasons, subscription_until, paid_invites)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
        """, [(slug, place.prize.rank, place.prize.kind, place.prize.amount, place.tg_id,
               place.status, place.reasons, place.subscription_until, place.paid_invites) for place in places])
        saved = await conn.fetchrow("""
            UPDATE contests SET drawn_at = clock_timestamp(), drawn_participant_count = $2, drawn_paid_invites = $3
            WHERE slug = $1 RETURNING *
        """, slug, len(participants), paid_invites)
        return DrawResult(dict(saved), len(participants), paid_invites, places)
