"""Continuously log field instruments from serial ports without PuTTY.

The logger writes a lossless acquisition envelope under ``data/``: a PC-local
receipt timestamp plus the exact decoded instrument line. The existing uploader
sends that envelope to S3 Bronze unchanged. Parsing, date normalization, and
science transformations belong to the Silver builder, not this field process.

Only one process can own a serial port. Close PuTTY before starting this logger.
"""

import argparse
import csv
import json
import logging
import os
import re
import signal
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path

try:
    import serial
    from serial.tools import list_ports
except ImportError:  # Allows parser unit tests and --help without pyserial.
    serial = None
    list_ports = None


LOGGER = logging.getLogger("serial-instruments")
DEFAULT_CONFIG_FILE = "config/instruments.json"
LEGACY_CONFIG_FILE = "serial_instruments_config.json"
CONFIG_FILE = os.environ.get("INSTRUMENT_CONFIG", DEFAULT_CONFIG_FILE)
EPOCH_1904 = datetime(1904, 1, 1)

NO2_COLUMNS = [
    "HHMMSS",
    "Concentration",
    "Loss",
    "Pressure",
    "Temperature",
    "Signal",
    "Span",
    "Status",
    "LastBaseline",
    "Timestamp",
    "Instrument_Timestamp",
    "PC_minus_instrument_s",
]
NEPH_COLUMNS = [
    "Date_Time",
    "Scat coefficient",
    "Sample temperature",
    "Enclosure temperature",
    "Relative humidity",
    "Atmospheric pressure",
    "Major State",
    "Minor State",
    "Instrument_Date_Time",
    "PC_minus_instrument_s",
]
LICOR_COLUMNS = [
    "System_Date_(Y-M-D)",
    "System_Time_(h:m:s)",
    "CO2_(umol_mol-1)",
    "H2O_(mmol_mol-1)",
    "H2O_(C)",
    "Cell_Temp_(C)",
    "Cell_Pressure_(kPa)",
    "CO2_Absorption",
    "H2O_Absorption",
    "Input_Voltage_(V)",
    "Raw_CO2",
    "Raw_CO2_Reference",
    "Raw_H2O",
    "Raw_H2O_Reference",
    "Flow_Rate_(L_min-1)",
]

NEPH_RE = re.compile(
    r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),"
    r"\s*(-?[\d.]+),\s*(-?[\d.]+),\s*(-?[\d.]+),\s*(-?[\d.]+),"
    r"\s*(-?[\d.]+),(\d+),(\d+)\s*$"
)

LICOR_XML_FIELDS = [
    "celltemp",
    "cellpres",
    "co2",
    "co2abs",
    "h2o",
    "h2oabs",
    "h2odewpoint",
    "ivolt",
    "raw/co2",
    "raw/co2ref",
    "raw/h2o",
    "raw/h2oref",
    "flowrate",
]


def compact_number(value):
    return format(value, ".12g")


def local_timestamp(moment):
    """Format all logger timestamps consistently, to millisecond precision."""
    return moment.replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def seconds_between(pc_time, instrument_time):
    return round(
        (pc_time.replace(tzinfo=None) - instrument_time.replace(tzinfo=None)).total_seconds(),
        3,
    )


def parse_no2(text, pc_time=None):
    """Parse CAPS data while making PC-local time the reporting timestamp."""
    parts = [part.strip() for part in text.split(",")]
    if len(parts) != 9:
        return None
    try:
        values = [float(part) for part in parts]
        instrument_time = EPOCH_1904 + timedelta(seconds=values[0])
    except (ValueError, OverflowError):
        return None
    if not 2000 <= instrument_time.year <= 2100:
        return None
    captured_at = pc_time or datetime.now().astimezone()
    return [instrument_time.strftime("%H%M%S")] + [
        compact_number(value) for value in values[1:]
    ] + [
        local_timestamp(captured_at),
        local_timestamp(instrument_time),
        compact_number(seconds_between(captured_at, instrument_time)),
    ]


def parse_neph(text, pc_time=None):
    """Parse nephelometer data and retain its unreliable clock separately."""
    match = NEPH_RE.search(text.replace("\x00", ""))
    if not match:
        return None
    try:
        instrument_time = datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    captured_at = pc_time or datetime.now().astimezone()
    # Date_Time is deliberately PC-local time: real captures contain a 12-hour
    # instrument-clock jump and occasional characters glued onto the year. The
    # original value and clock offset remain available for diagnosis.
    return [local_timestamp(captured_at)] + list(match.groups()[1:]) + [
        local_timestamp(instrument_time),
        compact_number(seconds_between(captured_at, instrument_time)),
    ]


def parse_licor(text, pc_time=None):
    """Parse one LI-850 XML element and add the PC date/time it omits."""
    start = text.find("<li850>")
    end = text.rfind("</li850>")
    if start < 0 or end < 0:
        return None
    try:
        root = ET.fromstring(text[start:end + len("</li850>")])
    except ET.ParseError:
        return None
    data = root.find("data")
    if data is None:
        return None
    values = [data.findtext(name) for name in LICOR_XML_FIELDS]
    if any(value is None for value in values):
        return None
    captured_at = pc_time or datetime.now().astimezone()
    # Arrange the XML fields into the same column order as the existing LI-COR
    # tab-delimited exports consumed by the uploader/API.
    by_name = dict(zip(LICOR_XML_FIELDS, (value.strip() for value in values)))
    return [
        captured_at.strftime("%Y-%m-%d"),
        captured_at.strftime("%H:%M:%S"),
        by_name["co2"],
        by_name["h2o"],
        by_name["h2odewpoint"],
        by_name["celltemp"],
        by_name["cellpres"],
        by_name["co2abs"],
        by_name["h2oabs"],
        by_name["ivolt"],
        by_name["raw/co2"],
        by_name["raw/co2ref"],
        by_name["raw/h2o"],
        by_name["raw/h2oref"],
        by_name["flowrate"],
    ]


PARSERS = {
    "no2": (parse_no2, NO2_COLUMNS),
    "neph": (parse_neph, NEPH_COLUMNS),
    "licor": (parse_licor, LICOR_COLUMNS),
}


class DailyTextFile:
    """Append-only text file that rolls over at local midnight."""

    def __init__(self, directory, name, suffix, header=None):
        self.directory = Path(directory)
        self.name = name
        self.suffix = suffix
        self.header = header
        self.day = None
        self.handle = None

    def write(self, when, text):
        day = when.strftime("%Y-%m-%d")
        if day != self.day:
            if self.handle:
                self.handle.close()
            self.directory.mkdir(parents=True, exist_ok=True)
            path = self.directory / f"{self.name}_{day}_{self.suffix}"
            is_new = not path.exists() or path.stat().st_size == 0
            self.handle = path.open("a", encoding="utf-8", newline="")
            if is_new and self.header:
                self.handle.write(self.header + "\n")
                self.handle.flush()
            self.day = day
        self.handle.write(text + "\n")
        self.handle.flush()


class DailyInstrumentRaw:
    """Append raw envelopes to a fixed or date-expanded configured filename."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.day = None
        self.handle = None
        self.writer = None

    def write(self, when, raw_line):
        day = when.strftime("%Y-%m-%d")
        if day != self.day:
            if self.handle:
                self.handle.close()
            directory = Path(self.cfg["output_dir"])
            directory.mkdir(parents=True, exist_ok=True)
            filename = self.cfg["filename"].format(date=day)
            path = directory / filename
            is_new = not path.exists() or path.stat().st_size == 0
            self.handle = path.open("a", encoding="utf-8", newline="")
            self.writer = csv.writer(self.handle, delimiter="\t", lineterminator="\n")
            if is_new:
                self.writer.writerow(["PC_Date_Time", "Raw_Line"])
                self.handle.flush()
            self.day = day
        self.writer.writerow([local_timestamp(when), raw_line])
        self.handle.flush()


def load_config(path=CONFIG_FILE):
    if not os.path.exists(path) and path == DEFAULT_CONFIG_FILE and os.path.exists(LEGACY_CONFIG_FILE):
        LOGGER.warning(
            "Using legacy %s; migrate its serial settings into %s.",
            LEGACY_CONFIG_FILE,
            DEFAULT_CONFIG_FILE,
        )
        path = LEGACY_CONFIG_FILE
    with open(path, encoding="utf-8") as handle:
        payload = json.load(handle)
    return payload


def serial_instrument_configs(config):
    """Return flattened serial settings from unified or legacy configuration."""
    selected = []
    for instrument in config.get("instruments") or []:
        serial_config = instrument.get("serial")
        if serial_config is not None:
            selected.append({
                "id": instrument.get("id"),
                "active": instrument.get("active", True),
                **serial_config,
            })
        elif instrument.get("port"):
            # Temporary compatibility with serial_instruments_config.json.
            selected.append(dict(instrument))
    return selected


def validate_config(config):
    errors = []
    instruments = serial_instrument_configs(config)
    if not isinstance(instruments, list) or not instruments:
        return ["at least one instrument must contain a serial configuration"]
    seen_ports = set()
    required = ("id", "name", "port", "baud", "parser", "output_dir", "filename")
    for index, cfg in enumerate(instruments):
        label = cfg.get("id") or f"instrument #{index + 1}"
        missing = [key for key in required if cfg.get(key) in (None, "")]
        if missing:
            errors.append(f"{label}: missing {', '.join(missing)}")
        if cfg.get("parser") not in PARSERS:
            errors.append(f"{label}: unknown parser {cfg.get('parser')!r}")
        port = str(cfg.get("port", "")).upper()
        if port in seen_ports:
            errors.append(f"{label}: serial port {port} is configured more than once")
        seen_ports.add(port)
        try:
            if int(cfg.get("baud", 0)) <= 0:
                raise ValueError
        except (TypeError, ValueError):
            errors.append(f"{label}: baud must be a positive integer")
    return errors


def list_available_ports():
    """Print the same COM-port identity information shown by Device Manager."""
    if list_ports is None:
        LOGGER.error("pyserial is not installed; run pip install -r requirements.txt")
        return 1
    ports = sorted(list_ports.comports(), key=lambda item: item.device)
    if not ports:
        LOGGER.warning("No serial ports were detected.")
        return 0
    LOGGER.info("Detected %d serial port(s):", len(ports))
    for port in ports:
        LOGGER.info(
            "  %-8s  %s  [%s]",
            port.device,
            port.description or "Unknown device",
            port.hwid or "no hardware ID",
        )
    return 0


def run_instrument(cfg, stop_event, raw_log_dir, reconnect_seconds):
    parser, _columns = PARSERS[cfg["parser"]]
    bronze_file = DailyInstrumentRaw(cfg)
    raw_file = DailyTextFile(
        raw_log_dir, cfg["name"], "raw.log", header="PC_Date_Time\tRaw_Line"
    )
    meta_file = DailyTextFile(
        raw_log_dir, cfg["name"], "meta.txt", header="PC_Date_Time\tUnparsed_Line"
    )

    while not stop_event.is_set():
        try:
            with serial.Serial(
                port=cfg["port"],
                baudrate=int(cfg["baud"]),
                bytesize=8,
                parity="N",
                stopbits=1,
                timeout=1,
                xonxoff=False,
                rtscts=False,
                dsrdtr=False,
            ) as connection:
                LOGGER.info(
                    "[%s] opened %s at %s baud", cfg["id"], cfg["port"], cfg["baud"]
                )
                while not stop_event.is_set():
                    raw = connection.readline()
                    if not raw:
                        continue
                    now = datetime.now().astimezone()
                    text = raw.decode("ascii", errors="replace").rstrip("\r\n")
                    stamp = local_timestamp(now)
                    raw_file.write(now, f"{stamp}\t{text}")
                    if not text:
                        continue
                    # This is the only file watched by the uploader. Preserve
                    # the exact instrument payload; Silver owns all parsing.
                    bronze_file.write(now, text)
                    # Parser use here is diagnostic only and never changes the
                    # Bronze payload. Unrecognized lines go to the local meta log.
                    row = parser(text, now)
                    if row is None:
                        meta_file.write(now, f"{stamp}\t{text}")
        except Exception as exc:
            if stop_event.is_set():
                break
            LOGGER.error(
                "[%s] serial-port problem on %s: %s; retrying in %ss",
                cfg["id"],
                cfg["port"],
                exc,
                reconnect_seconds,
            )
            stop_event.wait(reconnect_seconds)


def run(config):
    if serial is None:
        raise RuntimeError("pyserial is not installed; run pip install -r requirements.txt")
    stop_event = threading.Event()
    settings = config.get("serial_settings") or config
    raw_log_dir = settings.get("raw_log_dir", "serial_logs")
    reconnect_seconds = max(1, int(settings.get("reconnect_seconds", 5)))
    instruments = [
        cfg for cfg in serial_instrument_configs(config) if cfg.get("active", True)
    ]
    if not instruments:
        raise RuntimeError("no active serial instruments are configured")

    def request_stop(_signum=None, _frame=None):
        stop_event.set()

    signal.signal(signal.SIGINT, request_stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, request_stop)

    threads = [
        threading.Thread(
            name=f"serial-{cfg['id']}",
            target=run_instrument,
            args=(cfg, stop_event, raw_log_dir, reconnect_seconds),
        )
        for cfg in instruments
    ]
    for thread in threads:
        thread.start()
    LOGGER.info("Serial logger started for %d instrument(s)", len(threads))
    while any(thread.is_alive() for thread in threads):
        for thread in threads:
            thread.join(timeout=0.5)
    LOGGER.info("Serial logger stopped")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=CONFIG_FILE, help="serial logger JSON config")
    parser.add_argument(
        "--check", action="store_true", help="validate configuration without opening ports"
    )
    parser.add_argument(
        "--list-ports",
        action="store_true",
        help="list detected COM ports and Device Manager descriptions, then exit",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.StreamHandler(), logging.FileHandler("serial_collector.log")],
    )
    if args.list_ports:
        return list_available_ports()
    try:
        config = load_config(args.config)
    except (OSError, json.JSONDecodeError) as exc:
        LOGGER.error("Could not read %s: %s", args.config, exc)
        return 1
    errors = validate_config(config)
    if errors:
        for error in errors:
            LOGGER.error(error)
        return 1
    if serial is None:
        LOGGER.error("pyserial is not installed; run pip install -r requirements.txt")
        return 1
    if args.check:
        active = [
            cfg for cfg in serial_instrument_configs(config) if cfg.get("active", True)
        ]
        LOGGER.info("Serial configuration is valid for %d active instrument(s)", len(active))
        for cfg in active:
            filename = cfg["filename"].format(date=datetime.now().strftime("%Y-%m-%d"))
            destination = Path(cfg["output_dir"]) / filename
            LOGGER.info(
                "[%s] %s at %s baud -> %s",
                cfg["id"], cfg["port"], cfg["baud"], destination,
            )
        return 0
    try:
        run(config)
    except (RuntimeError, ValueError) as exc:
        LOGGER.error("Serial logger could not start: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
