import time
from datetime import datetime, timezone, timedelta
import pytest
from fastapi.testclient import TestClient

import db
import api
import schedule_service

client = TestClient(api.app)

def setup_module():
    db.init_db()

def test_schedule_db_and_api():
    guild_id = "test-guild-123"
    user_id = "test-user-456"

    # 1. Test DB creation with category
    event_data = {
        "title": "Community Gaming Night",
        "description": "Chill multiplayer and discussion",
        "category": "gaming",
        "start": "2026-11-01T20:00",
        "durationMinutes": 90,
        "recurrence": {
            "freq": "weekly",
            "interval": 1,
            "weekdays": [2, 4],
        },
        "location": {
            "kind": "discord",
            "channelId": "ch-1",
            "channelName": "general-voice",
        },
        "links": [{"label": "Playlist", "url": "https://youtube.com"}],
        "inviteEveryone": True,
        "inviteeIds": [],
    }

    event = db.create_schedule_event(guild_id, event_data, user_id, "Test Organizer", None)
    assert event is not None
    assert event["title"] == "Community Gaming Night"
    assert event["category"] == "gaming"
    assert event["guildId"] == guild_id
    assert event["organizerId"] == user_id
    assert event["seriesResponses"].get(user_id) == "accepted"

    event_id = event["id"]

    # 2. Test DB retrieval
    events = db.get_schedule_events(guild_id)
    assert len(events) >= 1
    found = [e for e in events if e["id"] == event_id]
    assert len(found) == 1
    assert found[0]["category"] == "gaming"

    # 3. Test RSVP
    respondent_id = "responder-789"
    updated = db.respond_schedule_event(event_id, respondent_id, "2026-11-03", "tentative", scope="occurrence")
    assert updated["occurrenceResponses"].get("2026-11-03", {}).get(respondent_id) == "tentative"

    # Series response clears individual occurrence override
    updated_series = db.respond_schedule_event(event_id, respondent_id, "2026-11-03", "accepted", scope="series")
    assert updated_series["seriesResponses"].get(respondent_id) == "accepted"
    assert respondent_id not in updated_series["occurrenceResponses"].get("2026-11-03", {})

    # 4. Test Update category
    patch = {"title": "Updated Gaming Jam", "category": "food", "durationMinutes": 120}
    updated_event = db.update_schedule_event(event_id, patch, user_id)
    assert updated_event["title"] == "Updated Gaming Jam"
    assert updated_event["category"] == "food"
    assert updated_event["durationMinutes"] == 120

    # Unauthorized update throws PermissionError
    with pytest.raises(PermissionError):
        db.update_schedule_event(event_id, patch, "unauthorized-user", is_admin=False)

    # 5. Cancel single occurrence
    db.delete_schedule_event(event_id, user_id, occurrence="2026-11-03")
    after_cancel = db.get_schedule_event(event_id)
    assert "2026-11-03" in after_cancel["cancelledOccurrences"]

    # 6. Test Reminder Service expansion & DB tracking
    now = datetime.now(timezone.utc)
    reminder_event_data = {
        "title": "Upcoming Dinner",
        "category": "food",
        "start": (now + timedelta(minutes=10)).isoformat(),
        "durationMinutes": 60,
        "inviteEveryone": True,
    }
    rem_event = db.create_schedule_event(guild_id, reminder_event_data, user_id, "Host")
    rem_id = rem_event["id"]
    try:
        upcoming = schedule_service.get_upcoming_occurrences(
            rem_event,
            now - timedelta(minutes=1),
            now + timedelta(minutes=15)
        )
        assert len(upcoming) == 1
        occ_key = upcoming[0]["key"]

        # Default reminder_type="lead"
        assert not db.is_occurrence_reminded(rem_id, occ_key, "lead")
        db.mark_occurrence_reminded(rem_id, occ_key, "lead")
        assert db.is_occurrence_reminded(rem_id, occ_key, "lead")

        # Starting-now reminder_type="start" is tracked independently
        assert not db.is_occurrence_reminded(rem_id, occ_key, "start")
        db.mark_occurrence_reminded(rem_id, occ_key, "start")
        assert db.is_occurrence_reminded(rem_id, occ_key, "start")
    finally:
        db.delete_schedule_event(rem_id, user_id)

    # 7. Delete whole event
    deleted = db.delete_schedule_event(event_id, user_id)
    assert deleted is True
    assert db.get_schedule_event(event_id) is None
