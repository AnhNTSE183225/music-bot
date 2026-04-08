import os
import yaml
import logging

logger = logging.getLogger(__name__)

# Load configuration from config.yaml
CONFIG_FILE = 'config.yaml'


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

def load_config():
    """Load configuration from YAML file with sensible defaults."""
    if not os.path.exists(CONFIG_FILE):
        raise FileNotFoundError(
            f"{CONFIG_FILE} not found. Please create a configuration file using config.yaml.example"
        )
    
    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
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

# --- Media and Storage ---
MEDIA_FOLDER = _config.get('storage', {}).get('media_folder', 'media')
LOG_FILE = os.getenv('MUSICBOT_LOG_FILE', _config.get('storage', {}).get('log_file', 'musicbot.log'))
_default_log_level = 'DEBUG' if RUNTIME_MODE == 'debug' else 'INFO'
LOG_LEVEL = os.getenv('MUSICBOT_LOG_LEVEL', _config.get('storage', {}).get('log_level', _default_log_level))

# --- Message Settings ---
DISCORD_MESSAGE_CHAR_LIMIT = _config.get('message', {}).get('embed_char_limit', 2000)
MESSAGE_BUFFER = _config.get('message', {}).get('embed_buffer', 100)

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
    
    options = {
        'format': ytdl_cfg.get('format', 'bestaudio/best'),
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
    
    # Handle extractor_args if present
    if 'extractor_args' in ytdl_cfg and ytdl_cfg['extractor_args']:
        extractor_args = {}
        for extractor, args_list in ytdl_cfg['extractor_args'].items():
            if isinstance(args_list, list):
                # Convert list of "key=value" strings to dict
                extractor_args[extractor] = {}
                for arg in args_list:
                    if '=' in arg:
                        key, value = arg.split('=', 1)
                        extractor_args[extractor][key] = value
            else:
                extractor_args[extractor] = args_list
        options['extractor_args'] = {'youtube': [f'player_client={extractor_args.get("youtube", {}).get("player_client", "android")}']}
    
    return options

# --- FFmpeg Options ---
def get_ffmpeg_options():
    """Build FFmpeg options from config."""
    ffmpeg_cfg = _config.get('ffmpeg', {})

    return {
        'before_options': ffmpeg_cfg.get('before_options', '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5'),
        'options': ffmpeg_cfg.get('audio_only', '-vn'),
    }

# Legacy compatibility - pre-compute these
YTDL_OPTIONS = get_ytdl_options()
FFMPEG_OPTIONS = get_ffmpeg_options()
YT_BLACKLIST_PATTERNS = get_blacklist_patterns()

# --- Permissions and Role Rules ---
def get_permissions_config():
    """Get permissions configuration from config."""
    return _config.get('permissions', {}) or {}


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
        'min_votes': vote_cfg.get('min_votes', 2),
        'same_channel_only': vote_cfg.get('same_channel_only', True),
        'force_vote_for_admin': vote_cfg.get('force_vote_for_admin', False),
    }
