import os
import yaml
import logging

try:
    from ruamel.yaml import YAML
except ImportError:
    YAML = None

logger = logging.getLogger(__name__)

# Load configuration from config.yaml
CONFIG_FILE = os.getenv('MUSICBOT_CONFIG_FILE', 'config.yaml')

_yaml_rt = None
if YAML is not None:
    _yaml_rt = YAML()
    _yaml_rt.preserve_quotes = True
    _yaml_rt.width = 4096
    _yaml_rt.indent(mapping=2, sequence=4, offset=2)


def _get_bool(value, default=False):
    """Parse booleans from Python values and common string forms."""
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in {'1', 'true', 'yes', 'on'}
    return bool(value)


def _get_env_bool(name, default=False):
    """Parse boolean from environment variable with fallback default."""
    return _get_bool(os.getenv(name), default)


def _get_env_int(name, default=None):
    """Parse an integer from environment variables with fallback default."""
    raw_value = os.getenv(name)
    if raw_value is None or str(raw_value).strip() == "":
        return default

    try:
        return int(str(raw_value).strip())
    except (TypeError, ValueError):
        logger.warning("Invalid integer value for %s: %r", name, raw_value)
        return default

def load_config():
    """Load configuration from YAML file with sensible defaults."""
    if not os.path.exists(CONFIG_FILE):
        raise FileNotFoundError(
            f"{CONFIG_FILE} not found. Please create a configuration file using config.yaml.example"
        )
    
    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            if _yaml_rt is not None:
                config = _yaml_rt.load(f)
            else:
                config = yaml.safe_load(f)
        if config is None:
            raise ValueError(f"{CONFIG_FILE} is empty")
        return config
    except yaml.YAMLError as e:
        raise ValueError(f"Invalid YAML in {CONFIG_FILE}: {e}")

# Load the configuration
_config = load_config()

# --- Runtime Mode ---
_runtime_cfg = _config.get('runtime', {}) or {}
RUNTIME_MODE = str(os.getenv('MUSICBOT_RUNTIME_MODE', _runtime_cfg.get('mode', 'prod'))).strip().lower()
if RUNTIME_MODE not in {'debug', 'prod'}:
    logger.warning("Invalid runtime.mode '%s' in config/env; falling back to 'prod'", RUNTIME_MODE)
    RUNTIME_MODE = 'prod'

# --- Discord Settings ---
COMMAND_PREFIX = _config.get('discord', {}).get('command_prefix', '!')
TOKEN_ENV_VAR = _config.get('discord', {}).get('token_env_var', 'DISCORD_TOKEN')
CONSOLE_USER_ID = _get_env_int('USER_ID')

# --- Playback Settings ---
DEFAULT_VOLUME = _config.get('playback', {}).get('default_volume', 0.5)
CONNECTION_TIMEOUT = _config.get('playback', {}).get('connection_timeout', 10.0)
CONNECTION_STABILIZE_DELAY = _config.get('playback', {}).get('connection_stabilize_delay', 0.5)
_playback_debug_default = (RUNTIME_MODE == 'debug')
PLAYBACK_DEBUG_METRICS = _get_env_bool(
    'MUSICBOT_PLAYBACK_DEBUG_METRICS',
    _get_bool(_config.get('playback', {}).get('debug_metrics', _playback_debug_default), _playback_debug_default),
)
YT_STREAM_CACHE_TTL_SECONDS = int(_config.get('playback', {}).get('yt_stream_cache_ttl_seconds', 300))
PREBUFFER_SECONDS = float(_config.get('playback', {}).get('prebuffer_seconds', 1.5))
LOG_BUFFER_METRICS = _get_env_bool(
    'MUSICBOT_LOG_BUFFER_METRICS',
    _get_bool(_config.get('playback', {}).get('log_buffer_metrics', False), False),
)
NORMALIZE_AUDIO = _get_bool(_config.get('playback', {}).get('normalize_audio', True), True)
TARGET_LUFS = float(_config.get('playback', {}).get('target_lufs', -16.0))
TRUE_PEAK = float(_config.get('playback', {}).get('true_peak', -1.5))
LOUDNESS_RANGE = float(_config.get('playback', {}).get('loudness_range', 11.0))

# --- Resilience Settings ---
AUTO_RECONNECT = _get_bool(_config.get('resilience', {}).get('auto_reconnect', True), True)
RECONNECT_DELAY_INITIAL = float(_config.get('resilience', {}).get('reconnect_delay_initial', 3.0))
RECONNECT_DELAY_MAX = float(_config.get('resilience', {}).get('reconnect_delay_max', 60.0))
VOICE_RECONNECT_ATTEMPTS = int(_config.get('resilience', {}).get('voice_reconnect_attempts', 10))
VOICE_RECONNECT_INTERVAL = float(_config.get('resilience', {}).get('voice_reconnect_interval', 5.0))
AUTO_RESUME_PLAYBACK = _get_bool(_config.get('resilience', {}).get('auto_resume_playback', True), True)
AUTO_RESUME_MAX_AGE_SECONDS = int(_config.get('resilience', {}).get('auto_resume_max_age_seconds', 300))

# --- Media and Storage ---
MEDIA_FOLDER = _config.get('storage', {}).get('media_folder', 'media')
LOG_FILE = os.getenv('MUSICBOT_LOG_FILE', _config.get('storage', {}).get('log_file', 'musicbot.log'))
_default_log_level = 'DEBUG' if RUNTIME_MODE == 'debug' else 'INFO'
LOG_LEVEL = os.getenv('MUSICBOT_LOG_LEVEL', _config.get('storage', {}).get('log_level', _default_log_level))
STATE_FILE = os.getenv('MUSICBOT_STATE_FILE', _config.get('storage', {}).get('state_file', 'queue_state.json'))

# --- Message Settings ---
DISCORD_MESSAGE_CHAR_LIMIT = _config.get('message', {}).get('embed_char_limit', 2000)
MESSAGE_BUFFER = _config.get('message', {}).get('embed_buffer', 100)
QUEUE_ITEMS_PER_PAGE = int(_config.get('message', {}).get('queue_items_per_page', 10))
QUEUE_PAGINATOR_TIMEOUT = int(_config.get('message', {}).get('queue_paginator_timeout', 120))

# --- YouTube Playlist & Search Settings ---
DETECT_PLAYLISTS = _get_bool(_config.get('youtube', {}).get('detect_playlists', False), False)
PLAYLIST_CONFIRMATION_TIMEOUT = int(_config.get('youtube', {}).get('playlist_confirmation_timeout', 60))
MAX_PLAYLIST_ITEMS = int(_config.get('youtube', {}).get('max_playlist_items', 200))
YOUTUBE_SEARCH_PROVIDER = str(_config.get('youtube', {}).get('search_provider', 'youtube_music')).strip().lower()

# --- YouTube Blacklist Patterns ---
def get_blacklist_patterns():
    """Get YouTube blacklist regex patterns from config."""
    patterns = _config.get('youtube', {}).get('blacklist_patterns', [])
    if patterns is None:
        patterns = []
    return patterns

# --- YouTube / YTDL Options ---
def get_ytdl_options():
    """Build yt-dlp format options from config."""
    ytdl_cfg = _config.get('ytdl_options', {})
    
    DEFAULT_FORMAT = "ba[acodec=opus][abr<=96]/ba[abr<=96]/ba[acodec=opus][abr<=128]/bestaudio/best"
    options = {
        'format': ytdl_cfg.get('format', DEFAULT_FORMAT),
        'outtmpl': '%(extractor)s-%(id)s-%(title)s.%(ext)s',
        'restrictfilenames': True,
        'noplaylist': ytdl_cfg.get('noplaylist', True),
        'nocheckcertificate': True,
        'ignoreerrors': False,
        'logtostderr': False,
        'quiet': ytdl_cfg.get('quiet', True),
        'no_warnings': ytdl_cfg.get('no_warnings', True),
        'default_search': ytdl_cfg.get('default_search', 'auto'),
        'source_address': ytdl_cfg.get('source_address', '0.0.0.0'),
        'geo_bypass': ytdl_cfg.get('geo_bypass', True),
    }
    
    # Support cookies file if specified in config, environment, or present in project root
    cookie_file = ytdl_cfg.get('cookiefile') or os.getenv('YTDL_COOKIEFILE')
    if not cookie_file and os.path.exists('cookies.txt'):
        cookie_file = 'cookies.txt'
    if cookie_file and os.path.exists(cookie_file):
        options['cookiefile'] = cookie_file

    # Handle extractor_args (convert config strings into yt-dlp Python API dictionary structure)
    formatted_extractor_args = {}
    if 'extractor_args' in ytdl_cfg and ytdl_cfg['extractor_args']:
        for extractor, args in ytdl_cfg['extractor_args'].items():
            formatted_extractor_args[extractor] = {}
            if isinstance(args, list):
                for item in args:
                    if '=' in item:
                        k, v = item.split('=', 1)
                        vals = [x.strip() for x in v.split(',') if x.strip()]
                        formatted_extractor_args[extractor][k.strip()] = vals
                    else:
                        formatted_extractor_args[extractor][item.strip()] = []
            elif isinstance(args, dict):
                for k, v in args.items():
                    if isinstance(v, list):
                        formatted_extractor_args[extractor][k] = v
                    elif isinstance(v, str):
                        formatted_extractor_args[extractor][k] = [x.strip() for x in v.split(',') if x.strip()]
                    else:
                        formatted_extractor_args[extractor][k] = [v]

    # Ensure youtube player_client defaults to mobile clients (android, ios) to bypass web bot checks
    if 'youtube' not in formatted_extractor_args:
        formatted_extractor_args['youtube'] = {}
    if 'player_client' not in formatted_extractor_args['youtube']:
        formatted_extractor_args['youtube']['player_client'] = ['android', 'ios']

    options['extractor_args'] = formatted_extractor_args
    options['js_runtimes'] = {'node': {}}

    return options

def get_playlist_ytdl_options():
    """Build yt-dlp format options for extracting playlist metadata."""
    options = get_ytdl_options()
    options['extract_flat'] = 'in_playlist'
    options['noplaylist'] = False
    options['playlistend'] = MAX_PLAYLIST_ITEMS
    options['ignoreerrors'] = True
    return options

def get_search_ytdl_options():
    """Build yt-dlp format options for extracting search result metadata efficiently."""
    options = get_ytdl_options()
    options['extract_flat'] = 'in_playlist'
    options['noplaylist'] = False
    options['playlistend'] = 10
    options['ignoreerrors'] = True
    return options

# --- FFmpeg Options ---
def get_audio_filter_arg():
    """Build FFmpeg audio filter (-af) arguments for loudness normalization if enabled."""
    playback_cfg = _config.get('playback', {}) or {}
    if _get_bool(playback_cfg.get('normalize_audio', True), True):
        lufs = playback_cfg.get('target_lufs', -16.0)
        tp = playback_cfg.get('true_peak', -1.5)
        lra = playback_cfg.get('loudness_range', 11.0)
        return f"-af loudnorm=I={lufs}:TP={tp}:LRA={lra} "
    return ""

def get_ffmpeg_options():
    """Build FFmpeg options for remote network streams."""
    ffmpeg_cfg = _config.get('ffmpeg', {})
    filter_arg = get_audio_filter_arg()
    base_audio = ffmpeg_cfg.get('audio_only', '-vn -ar 48000 -ac 2')

    return {
        'before_options': ffmpeg_cfg.get('before_options', '-v warning -reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -thread_queue_size 1024 -fflags +genpts'),
        'options': f"{filter_arg}{base_audio}".strip(),
    }

def get_local_ffmpeg_options():
    """Build FFmpeg options for local media files (with audio filters, without network reconnect flags)."""
    ffmpeg_cfg = _config.get('ffmpeg', {})
    filter_arg = get_audio_filter_arg()
    base_audio = ffmpeg_cfg.get('audio_only', '-vn -ar 48000 -ac 2')

    return {
        'options': f"{filter_arg}{base_audio}".strip(),
    }

# Legacy compatibility - pre-compute these
YTDL_OPTIONS = get_ytdl_options()
PLAYLIST_YTDL_OPTIONS = get_playlist_ytdl_options()
SEARCH_YTDL_OPTIONS = get_search_ytdl_options()
FFMPEG_OPTIONS = get_ffmpeg_options()
LOCAL_FFMPEG_OPTIONS = get_local_ffmpeg_options()
YT_BLACKLIST_PATTERNS = get_blacklist_patterns()

def save_config():
    """Save the current global _config to config.yaml."""
    try:
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            if _yaml_rt is not None:
                _yaml_rt.dump(_config, f)
            else:
                yaml.safe_dump(_config, f, default_flow_style=False, sort_keys=False, allow_unicode=True)
        return True
    except Exception as e:
        logger.error(f"Failed to save config: {e}")
        return False


def update_blocked_user_ids(user_ids_list):
    """Update blocked_user_ids in the config and save."""
    if 'permissions' not in _config:
        _config['permissions'] = {}
    _config['permissions']['blocked_user_ids'] = list(user_ids_list)
    return save_config()


def update_allowed_user_ids(user_ids_list_or_none):
    """Update allowed_user_ids in the config and save.

    Accepts:
      - None: disable whitelist mode
      - Iterable: explicit whitelist values
    """
    if 'permissions' not in _config:
        _config['permissions'] = {}

    if user_ids_list_or_none is None:
        _config['permissions']['allowed_user_ids'] = None
    else:
        _config['permissions']['allowed_user_ids'] = list(user_ids_list_or_none)

    return save_config()


def update_blacklist_patterns(patterns_list):
    """Update blacklist_patterns in the config and save."""
    if 'youtube' not in _config:
        _config['youtube'] = {}
    _config['youtube']['blacklist_patterns'] = list(patterns_list)
    return save_config()


def update_normalize_audio(enabled: bool):
    """Update normalize_audio in the config, recompute options, and save."""
    if 'playback' not in _config:
        _config['playback'] = {}
    _config['playback']['normalize_audio'] = bool(enabled)
    global NORMALIZE_AUDIO, FFMPEG_OPTIONS, LOCAL_FFMPEG_OPTIONS
    NORMALIZE_AUDIO = bool(enabled)
    FFMPEG_OPTIONS = get_ffmpeg_options()
    LOCAL_FFMPEG_OPTIONS = get_local_ffmpeg_options()
    return save_config()


# --- Permissions and Role Rules ---
def get_permissions_config():
    """Get permissions configuration from config."""
    return _config.get('permissions', {}) or {}


def validate_permissions_identity_lists():
    """Fail fast if allow/block user list config has invalid types."""
    permissions_cfg = get_permissions_config()

    blocked_raw = permissions_cfg.get('blocked_user_ids', [])
    if blocked_raw is None:
        blocked_raw = []
    if not isinstance(blocked_raw, list):
        raise ValueError("permissions.blocked_user_ids must be a list")

    allowed_raw = permissions_cfg.get('allowed_user_ids', None)
    if allowed_raw is not None and not isinstance(allowed_raw, list):
        raise ValueError("permissions.allowed_user_ids must be null or a list")


def get_blocked_user_ids():
    """Get blocked user IDs from config as a set of ints."""
    permissions_cfg = get_permissions_config()
    raw_ids = permissions_cfg.get('blocked_user_ids', []) or []

    blocked_ids = set()
    for raw_id in raw_ids:
        try:
            blocked_ids.add(int(raw_id))
        except (TypeError, ValueError):
            logger.warning("Ignoring invalid blocked_user_ids entry: %r", raw_id)

    return blocked_ids


def get_allowed_user_ids():
    """Get allowed user IDs from config.

    Returns:
      - None: whitelist disabled
      - set[int]: whitelist entries (may be empty)
    """
    permissions_cfg = get_permissions_config()
    raw_ids = permissions_cfg.get('allowed_user_ids', None)

    if raw_ids is None:
        return None

    allowed_ids = set()
    for raw_id in raw_ids:
        try:
            allowed_ids.add(int(raw_id))
        except (TypeError, ValueError):
            logger.warning("Ignoring invalid allowed_user_ids entry: %r", raw_id)

    return allowed_ids


def is_whitelist_enabled():
    """Return True only when whitelist has at least one explicit user ID."""
    allowed_ids = get_allowed_user_ids()
    return bool(allowed_ids)


def get_command_permission_mode(command_name):
    """Get mode for a command. Modes: open, admin_only, vote_if_non_admin."""
    commands_cfg = get_permissions_config().get('commands', {}) or {}
    command_cfg = commands_cfg.get(command_name, {}) or {}
    return command_cfg.get('mode', 'open')


def get_skip_vote_config():
    """Get vote settings for skip command with defaults."""
    commands_cfg = get_permissions_config().get('commands', {}) or {}
    skip_cfg = commands_cfg.get('skip', {}) or {}
    vote_cfg = skip_cfg.get('vote', {}) or {}

    return {
        'threshold_type': vote_cfg.get('threshold_type', 'ratio'),
        'threshold_value': vote_cfg.get('threshold_value', 0.5),
        'min_votes': vote_cfg.get('min_votes', 1),
        'same_channel_only': vote_cfg.get('same_channel_only', True),
        'force_vote_for_admin': vote_cfg.get('force_vote_for_admin', False),
    }


validate_permissions_identity_lists()

# --- API & Web UI Settings ---
API_HOST = os.getenv('MUSICBOT_API_HOST', _config.get('api', {}).get('host', '0.0.0.0'))
API_PORT = int(os.getenv('MUSICBOT_API_PORT', _config.get('api', {}).get('port', 8000)))
FRONTEND_URL = os.getenv('MUSICBOT_FRONTEND_URL', _config.get('api', {}).get('frontend_url', 'http://localhost:3000'))
DISCORD_REDIRECT_URI = os.getenv('MUSICBOT_DISCORD_REDIRECT_URI', _config.get('api', {}).get('discord_redirect_uri', 'http://localhost:8000/callback'))
DISCORD_CLIENT_ID = os.getenv('DISCORD_CLIENT_ID', _config.get('api', {}).get('client_id'))
DISCORD_CLIENT_SECRET = os.getenv('DISCORD_CLIENT_SECRET', _config.get('api', {}).get('client_secret'))
CORS_ORIGINS = _config.get('api', {}).get('cors_origins', [])
EMPTY_VOICE_LEAVE_DELAY_SECONDS = int(os.getenv('MUSICBOT_EMPTY_VOICE_LEAVE_DELAY_SECONDS', _config.get('playback', {}).get('empty_voice_leave_delay_seconds', 10)))

