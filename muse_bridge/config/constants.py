"""Internal constants and default configuration values.

Users do NOT need to modify this file directly. Any value defined here
can be overridden via user configuration (`config.json`) if desired.
"""

# Muse Gateway Defaults
DEFAULT_MUSE_HOST = "hatch.metaaivm.com"
DEFAULT_MUSE_TIMEZONE = "Asia/Shanghai"
DEFAULT_MUSE_DIRECT = True
DEFAULT_MUSE_REPLY_MODE = "push"  # "push" (subscribe event stream) or "poll"

# Egress IP Detection Services
EGRESS_IP_SERVICES = (
    "https://api.ipify.org",
    "https://ifconfig.me/ip",
    "https://checkip.amazonaws.com",
    "https://icanhazip.com",
)

# QQ Bot Media & Payload Limits (Bytes)
DEFAULT_MAX_IMAGE_BYTES = 8 * 1024 * 1024         # 8 MiB (QQ API image cap)
DEFAULT_MAX_FILE_BYTES = 25 * 1024 * 1024        # 25 MiB (Muse file cap)
DEFAULT_MAX_VIDEO_BYTES = 100 * 1024 * 1024      # 100 MiB (Muse video cap)
DEFAULT_MAX_VIDEO_UPLOAD_BYTES = 10 * 1024 * 1024 # 10 MiB (QQ API video cap)
DEFAULT_FILE_REF_THRESHOLD_BYTES = 2 * 1024 * 1024 # 2 MiB (Threshold for upload_file vs inline)

# QQ Bot Message Chunking & Budget
DEFAULT_CHUNK_CHARS = 1200
DEFAULT_MAX_CHUNKS = 8
DEFAULT_MAX_ACTIVE_CHUNKS = 2

# Network Timeouts & Intervals (Seconds)
DEFAULT_GATEWAY_DEDUP_S = 120
DEFAULT_DOWNLOAD_TIMEOUT_S = 60
DEFAULT_PUSH_TIMEOUT_S = 120
DEFAULT_PUSH_GRACE_S = 2
DEFAULT_POLL_TIMEOUT_S = 180
DEFAULT_POLL_INTERVAL_S = 5
DEFAULT_POLL_QUIET_POLLS = 2

# Push Subscriber & In-flight Timing
DEFAULT_IN_FLIGHT_TTL_S = 300
DEFAULT_PROCESSING_TTL_S = 300
DEFAULT_RECHECK_SLEEP_S = 5

# Network Specific Workarounds
BLOCKED_DOWNLOAD_HOST = "multimedia.nt.qq.com.cn"

# Startup Catch-Up Defaults (proactive push on restart)
DEFAULT_CATCH_UP_ENABLED = False
DEFAULT_CATCH_UP_WINDOW_S = 6 * 3600  # 6 hours
DEFAULT_CATCH_UP_MAX_MESSAGES = 5

# Administrator Permission Defaults
DEFAULT_ADMIN_ONLY = True

# WebSocket & HTTP Client Protocol Defaults
DEFAULT_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36"
)
