"""
Schedule Service for MusicBot
Handles recurrence expansion and Discord event reminder background tasks.
"""
import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Any
import discord

import db
import settings

logger = logging.getLogger(__name__)


def parse_datetime(dt_str: str) -> datetime:
    """Parse an ISO 8601 string to an aware UTC datetime using standard library."""
    cleaned = dt_str.replace("Z", "+00:00")
    dt = datetime.fromisoformat(cleaned)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def get_upcoming_occurrences(event: Dict[str, Any], window_start: datetime, window_end: datetime) -> List[Dict[str, Any]]:
    """
    Expand occurrences of an event that fall within [window_start, window_end].
    Returns list of dicts with keys: 'key', 'start', 'end', 'event'.
    """
    start_str = event.get("start")
    if not start_str:
        return []

    try:
        dt_start = parse_datetime(start_str)
    except Exception as e:
        logger.warning("Failed to parse start time for event %s: %s", event.get("id"), e)
        return []

    duration = timedelta(minutes=int(event.get("durationMinutes", 60)))
    cancelled = set(event.get("cancelledOccurrences") or [])
    recurrence = event.get("recurrence")

    results = []

    # One-off event
    if not recurrence or not recurrence.get("freq") or recurrence.get("freq") == "none":
        key = dt_start.strftime("%Y-%m-%dT%H:%M:%SZ")
        if key not in cancelled and (window_start <= dt_start <= window_end):
            results.append({
                "key": key,
                "start": dt_start,
                "end": dt_start + duration,
                "event": event,
            })
        return results

    # Recurring event
    freq = recurrence.get("freq")
    interval = max(1, int(recurrence.get("interval", 1)))
    weekdays = recurrence.get("weekdays") or [dt_start.weekday()]
    until_dt = parse_datetime(recurrence["until"]) if recurrence.get("until") else None
    max_count = int(recurrence.get("count", 0)) if recurrence.get("count") else None

    # Step through occurrences from series start up to window_end
    current = dt_start
    generated_count = 0

    # Safety limit to prevent infinite loops
    step_limit = 500
    steps = 0

    while steps < step_limit:
        steps += 1
        if current > window_end:
            break
        if until_dt and current > until_dt:
            break
        if max_count and generated_count >= max_count:
            break

        key = current.strftime("%Y-%m-%dT%H:%M:%SZ")
        is_match = False

        if freq == "daily":
            is_match = True
        elif freq == "weekly":
            # Python weekday(): Monday is 0, Sunday is 6
            # Discord / JS getDay(): Sunday is 0, Monday is 1
            # We support both conventions by checking modulo 7
            js_day = (current.weekday() + 1) % 7
            is_match = (current.weekday() in weekdays) or (js_day in weekdays)
        elif freq == "monthly":
            is_match = True

        if is_match:
            generated_count += 1
            if key not in cancelled and (window_start <= current <= window_end):
                results.append({
                    "key": key,
                    "start": current,
                    "end": current + duration,
                    "event": event,
                })

        # Advance to next candidate
        if freq == "daily":
            current += timedelta(days=interval)
        elif freq == "weekly":
            current += timedelta(days=1)  # scan day-by-day for matching weekdays
        elif freq == "monthly":
            # Rough monthly step: advance ~30 days
            current += timedelta(days=30 * interval)
        else:
            break

    return results


def build_reminder_embed(event: Dict[str, Any], start_dt: datetime, lead_minutes: int = 15, reminder_type: str = "lead") -> discord.Embed:
    """Build a rich Discord embed for an upcoming event reminder or start announcement."""
    title = event.get("title") or "Scheduled Event"
    category = event.get("category", "other").capitalize()
    
    category_emojis = {
        "gaming": "🎮",
        "food": "🍴",
        "outdoor": "📍",
        "other": "📅",
    }
    emoji = category_emojis.get(event.get("category", "other"), "📅")

    if reminder_type == "start":
        embed_title = f"🎉 {emoji} Event Starting Now: {title}"
        default_desc = "This event is starting right now!"
        color = discord.Color.from_rgb(0, 230, 118)
    else:
        embed_title = f"⏰ {emoji} Event Reminder: {title}"
        default_desc = f"This event is starting in about {lead_minutes} minutes!"
        color = discord.Color.from_rgb(0, 200, 255)

    embed = discord.Embed(
        title=embed_title,
        description=event.get("description") or default_desc,
        color=color,
    )

    unix_ts = int(start_dt.timestamp())
    embed.add_field(name="Start Time", value=f"<t:{unix_ts}:F> (<t:{unix_ts}:R>)", inline=True)
    embed.add_field(name="Category", value=f"{emoji} {category}", inline=True)

    loc = event.get("location") or {}
    kind = loc.get("kind")
    if kind == "discord" and loc.get("channelId"):
        embed.add_field(name="Location", value=f"🔊 <#{loc.get('channelId')}>", inline=False)
    elif kind == "address" and loc.get("address"):
        embed.add_field(name="Location", value=f"📍 {loc.get('address')}", inline=False)
    elif kind == "link" and loc.get("url"):
        embed.add_field(name="Location", value=f"🔗 [Join Link]({loc.get('url')})", inline=False)

    if event.get("imageUrl"):
        embed.set_image(url=event["imageUrl"])

    embed.set_footer(text=f"Organized by {event.get('organizerName') or 'Server Member'}")
    return embed


async def check_and_send_reminders(bot: discord.Client) -> int:
    """
    Check all active events and send reminders for:
    1. Advance heads-up (default 15 minutes before start)
    2. Starting now announcement (when event start time arrives)
    Returns count of sent reminders.
    """
    if not bot or not bot.is_ready():
        return 0

    lead_minutes = getattr(settings, "SCHEDULE_REMINDER_MINUTES", 15)
    announcement_name = getattr(settings, "SCHEDULE_ANNOUNCEMENT_CHANNEL", "general").strip().lower()

    now = datetime.now(timezone.utc)
    # Check window: from 2 minutes in past (to catch just-starting events) up to lead_minutes in future
    window_start = now - timedelta(minutes=2)
    window_end = now + timedelta(minutes=lead_minutes, seconds=45)

    try:
        events = db.get_all_active_schedule_events()
    except Exception as e:
        logger.error("[Schedule] Failed to fetch events from DB: %s", e)
        return 0

    sent_count = 0

    for ev in events:
        event_id = ev.get("id")
        if not event_id:
            continue

        occurrences = get_upcoming_occurrences(ev, window_start, window_end)
        for occ in occurrences:
            occ_key = occ["key"]
            start_dt = occ["start"]

            # Evaluate reminder phases to dispatch
            reminders_to_send = []

            # Phase 1: Advance reminder (lead time, e.g. 15 minutes before)
            # Fired when the event starts more than 2 minutes in the future
            if start_dt > now + timedelta(minutes=2):
                if not db.is_occurrence_reminded(event_id, occ_key, reminder_type="lead"):
                    reminders_to_send.append("lead")

            # Phase 2: Starting now announcement
            # Fired when start_dt is within [-2 min, +1 min] of now
            if (now - timedelta(minutes=2)) <= start_dt <= (now + timedelta(minutes=1)):
                if not db.is_occurrence_reminded(event_id, occ_key, reminder_type="start"):
                    reminders_to_send.append("start")

            for r_type in reminders_to_send:
                embed = build_reminder_embed(ev, start_dt, lead_minutes, reminder_type=r_type)
                guild_id_str = str(ev.get("guildId", ""))
                
                try:
                    guild = bot.get_guild(int(guild_id_str))
                except (ValueError, TypeError):
                    guild = None

                if not guild:
                    continue

                if r_type == "start":
                    channel_content = "🎉 **Event starting now!**"
                    dm_content = f"🎉 **Starting Now**: Your scheduled event '{ev.get('title')}' is starting right now!"
                else:
                    channel_content = f"📢 **Upcoming Event in {lead_minutes} minutes!**"
                    dm_content = f"⏰ **Reminder**: Your scheduled event '{ev.get('title')}' starts in {lead_minutes} minutes!"

                # Case A: Server-wide event -> announcement channel
                if ev.get("inviteEveryone", True):
                    target_channel = None
                    # 1. Look for channel named announcement_name
                    for ch in guild.text_channels:
                        if ch.name.lower() == announcement_name and ch.permissions_for(guild.me).send_messages:
                            target_channel = ch
                            break

                    # 2. Fall back to system channel or first writable channel
                    if not target_channel and guild.system_channel and guild.system_channel.permissions_for(guild.me).send_messages:
                        target_channel = guild.system_channel

                    if not target_channel:
                        for ch in guild.text_channels:
                            if ch.permissions_for(guild.me).send_messages:
                                target_channel = ch
                                break

                    if target_channel:
                        try:
                            await target_channel.send(
                                content=channel_content,
                                embed=embed,
                            )
                            db.mark_occurrence_reminded(event_id, occ_key, reminder_type=r_type)
                            sent_count += 1
                            logger.info(
                                "[Schedule] Sent %s reminder for event %s (%s) to #%s in guild %s",
                                r_type, event_id, ev.get("title"), target_channel.name, guild.name
                            )
                        except Exception as e:
                            logger.warning("[Schedule] Failed to send reminder to channel: %s", e)

                # Case B: Individual invites -> DM recipients
                else:
                    recipients = set()
                    if ev.get("organizerId"):
                        recipients.add(str(ev["organizerId"]))
                    for uid in ev.get("inviteeIds") or []:
                        recipients.add(str(uid))

                    # Filter out anyone who declined
                    series_resp = ev.get("seriesResponses") or {}
                    occ_resp = (ev.get("occurrenceResponses") or {}).get(occ_key, {})

                    active_recipients = []
                    for uid in recipients:
                        status = occ_resp.get(uid) or series_resp.get(uid)
                        if status != "declined":
                            active_recipients.append(uid)

                    for uid in active_recipients:
                        try:
                            user = bot.get_user(int(uid)) or await bot.fetch_user(int(uid))
                            if user and not user.bot:
                                await user.send(
                                    content=dm_content,
                                    embed=embed,
                                )
                        except Exception as e:
                            logger.debug("[Schedule] Could not send DM to user %s: %s", uid, e)

                    db.mark_occurrence_reminded(event_id, occ_key, reminder_type=r_type)
                    sent_count += 1
                    logger.info(
                        "[Schedule] Sent %s DM reminders for event %s (%s) to %d invitees",
                        r_type, event_id, ev.get("title"), len(active_recipients)
                    )

    return sent_count


async def run_schedule_reminder_loop(bot: discord.Client, check_interval_seconds: int = 30):
    """Background loop that periodically runs check_and_send_reminders."""
    logger.info("[Schedule] Starting background reminder loop (interval: %ds)", check_interval_seconds)
    while not bot.is_closed():
        try:
            await check_and_send_reminders(bot)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error("[Schedule] Error in reminder loop: %s", e, exc_info=True)

        try:
            await asyncio.sleep(check_interval_seconds)
        except asyncio.CancelledError:
            break
