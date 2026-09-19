"""Short in-memory history of Sprout soil-moisture samples.

The server already receives every Sprout SensorReport.  Keeping the last N
moisture values (plus the plant-profile band the node reports) lets it answer a
client with a compact ``DATA`` line that the Cardputer draws as a chart —
without the server needing DuckDB access, which belongs to the ingest pod.

The line format (parsed by ``cardputer_client/chart.py``):

    DATA <node8> <dry> <wet> <v0> <v1> ... <vn>

Values are integer percent, oldest first; ``-1`` for a band the node has not
reported yet.
"""

DEFAULT_MAXLEN = 30

SENSOR_SOIL_MOISTURE = 4
SENSOR_PROFILE_DRY = 10
SENSOR_PROFILE_WET = 11


class SproutHistory:
    """Ring of recent moisture samples for one or more nodes."""

    def __init__(self, maxlen=DEFAULT_MAXLEN):
        self._maxlen = int(maxlen)
        self._samples = []
        self._dry = None
        self._wet = None
        self._node = None

    def update(self, sensor_report):
        """Fold one SensorReport into the history.  Returns True if it held data.

        ``sensor_report`` is a protobuf SensorReport (anything exposing
        ``node_id`` and ``readings`` with ``sensor_id``/``value`` works).
        Unknown ids are ignored, so new node sensors cannot break this.

        A report only contributes if it actually carries a soil-moisture
        sample — otherwise every other node on the mesh (e.g. the Cardputer's
        own temperature/humidity reports) would adopt the history and stamp the
        DATA line with its own id.
        """
        node = getattr(sensor_report, "node_id", "") or ""
        moisture_seen = False
        dry = None
        wet = None
        for reading in getattr(sensor_report, "readings", None) or []:
            try:
                sensor_id = int(reading.sensor_id)
                value = float(reading.value)
            except (AttributeError, TypeError, ValueError):
                continue
            if sensor_id == SENSOR_SOIL_MOISTURE:
                self._push(value)
                moisture_seen = True
            elif sensor_id == SENSOR_PROFILE_DRY:
                dry = value
            elif sensor_id == SENSOR_PROFILE_WET:
                wet = value
        if moisture_seen:
            if node:
                self._node = node
            if dry is not None:
                self._dry = dry
            if wet is not None:
                self._wet = wet
        return moisture_seen

    def _push(self, value):
        self._samples.append(value)
        excess = len(self._samples) - self._maxlen
        if excess > 0:
            del self._samples[:excess]

    @property
    def samples(self):
        return list(self._samples)

    @property
    def node(self):
        return self._node

    def reset(self):
        self._samples = []
        self._dry = None
        self._wet = None
        self._node = None

    def data_line(self):
        """The DATA line for the next reply, or "" when nothing is known yet."""
        if not self._samples:
            return ""
        node = (self._node or "")[:8]
        dry = -1 if self._dry is None else int(round(self._dry))
        wet = -1 if self._wet is None else int(round(self._wet))
        values = " ".join(str(int(round(v))) for v in self._samples)
        return f"DATA {node} {dry} {wet} {values}"
