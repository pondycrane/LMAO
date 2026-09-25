"""Short in-memory history of Sprout soil-moisture, air-temperature and
air-humidity samples.

The server already receives every Sprout SensorReport.  Keeping the last N
moisture/air values (plus the plant-profile band the node reports) lets it
answer a client with a compact ``DATA`` line that the Cardputer draws as a
chart — without the server needing DuckDB access, which belongs to the ingest
pod.

The line format (parsed by ``cardputer_client/chart.py``) carries three
count-prefixed series — temperature, humidity, then moisture — so lengths may
differ (e.g. a report with a failed SHT30 contact still contributes its
moisture):

    DATA <node8> <dry> <wet> <ct> <t0> ... <tt> <ch> <h0> ... <hh> <cm> <m0> ... <mm>

* ``node8`` — first 8 hex chars of the reporting node id (informational)
* ``dry``/``wet`` — the node's active plant-profile band, integer percent;
  ``-1`` means the node has not reported a band yet
* ``ct``/``ch``/``cm`` — per-series sample counts, then the samples themselves
* air temperature in °C and humidity/moisture in integer percent, OLDEST FIRST

The air series are capped on the wire (``DATA_AIR_MAX_SAMPLES``) because the
server answers the Cardputer with an LXMF ``OPPORTUNISTIC`` packet whose
single-packet content is limited to ``295`` bytes (falling back to
link-based delivery would silently break on the half-duplex LoRa leaf).  Soil
moisture keeps its full ring so the primary chart is not shallower than before.

When the Sprout's SHT30 contact fails (issue #124) the firmware reports soil
moisture without air temperature/humidity, so the air rings stop growing while
the soil ring continues; the DATA line then carries the last-known air values,
and the Cardputer displays them without a staleness marker (a marker would
need an extra wire flag).
"""

DEFAULT_MAXLEN = 30

# The server replies to the Cardputer over LoRa with an LXMF OPPORTUNISTIC
# packet; LXMF's ENCRYPTED_PACKET_MAX_CONTENT is 295 bytes, beyond which it
# silently drops to link-based delivery (unreliable for a half-duplex leaf).
# 30 moisture + 10 temp + 10 humidity samples stay safely under that cap (the
# reply envelope measures ≈254 bytes), so the supplementary air series keep a
# shorter wire history than the soil chart.
DATA_AIR_MAX_SAMPLES = 10

SENSOR_AIR_HUMIDITY = 2
SENSOR_AIR_TEMP = 3
SENSOR_SOIL_MOISTURE = 4
SENSOR_PROFILE_DRY = 10
SENSOR_PROFILE_WET = 11


class SproutHistory:
    """Ring of recent moisture/temperature/humidity samples for one or more nodes."""

    def __init__(self, maxlen=DEFAULT_MAXLEN):
        self._maxlen = int(maxlen)
        self._moisture = []
        self._temp = []
        self._humidity = []
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
        own temperature/humidity reports, whose humidity shares the Sprout's
        sensor_id 2) would adopt the history and stamp the DATA line with its
        own id and pollute the air series.
        """
        node = getattr(sensor_report, "node_id", "") or ""
        moisture_seen = False
        air_temp = None
        air_humidity = None
        dry = None
        wet = None
        for reading in getattr(sensor_report, "readings", None) or []:
            try:
                sensor_id = int(reading.sensor_id)
                value = float(reading.value)
            except (AttributeError, TypeError, ValueError):
                continue
            if sensor_id == SENSOR_SOIL_MOISTURE:
                self._push(self._moisture, value)
                moisture_seen = True
            elif sensor_id == SENSOR_AIR_TEMP:
                air_temp = value
            elif sensor_id == SENSOR_AIR_HUMIDITY:
                air_humidity = value
            elif sensor_id == SENSOR_PROFILE_DRY:
                dry = value
            elif sensor_id == SENSOR_PROFILE_WET:
                wet = value
        if moisture_seen:
            # Air readings only count when the report is a true Sprout report
            # (it carries a moisture sample) — see the ownership rule above.
            if air_temp is not None:
                self._push(self._temp, air_temp)
            if air_humidity is not None:
                self._push(self._humidity, air_humidity)
            if node:
                self._node = node
            if dry is not None:
                self._dry = dry
            if wet is not None:
                self._wet = wet
        return moisture_seen

    def _push(self, ring, value):
        ring.append(value)
        excess = len(ring) - self._maxlen
        if excess > 0:
            del ring[:excess]

    @property
    def samples(self):
        """The soil-moisture series (list property kept for callers/tests)."""
        return list(self._moisture)

    @property
    def temp(self):
        return list(self._temp)

    @property
    def humidity(self):
        return list(self._humidity)

    @property
    def node(self):
        return self._node

    def reset(self):
        self._moisture = []
        self._temp = []
        self._humidity = []
        self._dry = None
        self._wet = None
        self._node = None

    @staticmethod
    def _series_tokens(values, limit):
        """Tokens ``[count, v0, ...]`` for a series, newest samples at the end."""
        if limit is not None and len(values) > limit:
            values = values[-limit:]
        return [str(len(values))] + [str(int(round(v))) for v in values]

    def data_line(self, air_limit=DATA_AIR_MAX_SAMPLES):
        """The DATA line for the next reply, or "" when nothing is known yet.

        ``air_limit`` caps the supplementary air series (temperature,
        humidity); pass ``None`` for a peer that receives the chart over LMAF,
        where the reply is no longer bound by the single-packet content limit
        and the full rings are worth sending.  Soil moisture (the primary
        chart) is never capped.
        """
        if not (self._moisture or self._temp or self._humidity):
            return ""
        node = (self._node or "")[:8]
        dry = -1 if self._dry is None else int(round(self._dry))
        wet = -1 if self._wet is None else int(round(self._wet))
        tokens = [f"DATA {node} {dry} {wet}"]
        tokens += self._series_tokens(self._temp, air_limit)
        tokens += self._series_tokens(self._humidity, air_limit)
        tokens += self._series_tokens(self._moisture, None)
        return " ".join(tokens)
