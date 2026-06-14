import re


def is_youtube_link(query):
    """Return True when the query is a direct YouTube URL."""
    query = (query or "").strip().lower()
    return bool(re.match(r'^(https?://)?([a-z0-9-]+\.)?(youtube\.com|youtu\.be)/', query))


def is_probable_url(query):
    """Return True when input looks like an HTTP(S) or www URL."""
    query = (query or "").strip().lower()
    return bool(re.match(r'^(https?://|www\.)', query))


def normalize_yt_search_term(query):
    """For search terms, just return the stripped query."""
    stripped_query = (query or "").strip()
    return stripped_query, False
