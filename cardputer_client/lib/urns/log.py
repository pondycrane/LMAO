# µReticulum Logging
# Lightweight, no threading, stdout only

import time

LOG_NONE = -1
LOG_CRITICAL = 0
LOG_ERROR = 1
LOG_WARNING = 2
LOG_NOTICE = 3
LOG_INFO = 4
LOG_VERBOSE = 5
LOG_DEBUG = 6
LOG_EXTREME = 7

loglevel = LOG_NOTICE

_level_names = {
    0: "CRIT",
    1: "ERR ",
    2: "WARN",
    3: "NOTE",
    4: "INFO",
    5: "VERB",
    6: "DBG ",
    7: "XTRA",
}


# Bounded in-memory ring of recent log lines, for the optional HTTP monitor.
_LOG_RING: list = []
_LOG_RING_MAX = 100


def get_log_ring():
    return _LOG_RING


def log(msg, level=LOG_NOTICE):
    if loglevel >= level:
        # Build log line (best-effort — protect ring buffer from formatting errors)
        try:
            ln = _level_names.get(level, "????")
            line = "[%d][%s] %s" % (time.time(), ln, str(msg))
        except Exception:
            line = "[LOG FORMAT ERROR] " + repr(msg)

        # Console output — the critical path for diagnostics
        try:
            print(line)
        except Exception:
            # If even print fails, try a raw fallback so the user sees something
            try:
                import sys as _sys

                _sys.stdout.write("[LOG FAILURE] " + repr(line) + "\n")
            except Exception:
                pass  # Nothing more we can do

        # Ring buffer is best-effort; never silence console for it
        try:
            _LOG_RING.append(line)
            if len(_LOG_RING) > _LOG_RING_MAX:
                _LOG_RING.pop(0)
        except Exception:
            pass


def set_loglevel(level):
    global loglevel
    loglevel = level


def trace_exception(e):
    import sys

    sys.print_exception(e)
