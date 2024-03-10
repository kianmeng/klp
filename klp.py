#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2024 Dirk Loss
"""
klp: Kool Logfmt Parser

For logs in key=value format (and some others), show only the interesting parts
"""

import argparse
import csv
import dataclasses
import datetime as dt
import errno
import gzip
import json
import os
import pprint
import re
import shutil
import sys
import textwrap
import unittest

# Some modules to make available for filtering and templating
import base64
import collections
import datetime
import hashlib
import itertools
import random
import string

__version__ = "0.48.1"

# Input quotes will be temporarily replaced by sentinel value to simplify parsing
SENTINEL = "\x00"
INPUT_QUOTE = r"\""

# Names of keys our program cares about. Use lowercase keys here.
TS_KEYS = "_ts_delta ts time timestamp at _ts".split()
MSG_KEYS = "msg message".split()
LEVEL_KEYS = "log_level level lvl loglevel severity".split()

# Regular expressions
RE_LOGFMT = re.compile(
    r'([\w.]+)=("[^"]+"|[^ ]+(?= ))'
)  # 'key="value"' OR 'key=value '
RE_EOL_OR_TAB = re.compile(r"\\n|\\t|\\r")
RE_EXTRACT_KEY = re.compile(r"^(\w+)~(.*)")
RE_CLF = re.compile(
    r'(?P<host>\S+) (?P<ident>\S+) (?P<user>\S+) \[(?P<time>[^\]]+)\] "(?P<request>[^"]+)" (?P<status>\d+) (?P<size>\d+)'
)
RE_COMBINED = re.compile(
    r'(?P<host>\S+) (?P<ident>\S+) (?P<user>\S+) \[(?P<time>[^\]]+)\] "(?P<request>[^"]+)" (?P<status>\d+) (?P<size>\d+) "(?P<referrer>[^"]*)" "(?P<agent>[^"]*)"'
)

# ANSI Escape Codes and a short, temporary replacement sentinel that should not occur otherwise in the text
COLOR_CODES = {
    "black": ("\x1b[30m", "\x01"),
    "red": ("\x1b[31m", "\x02"),
    "green": ("\x1b[32m", "\x03"),
    "yellow": ("\x1b[33m", "\x04"),
    "blue": ("\x1b[34m", "\x05"),
    "magenta": ("\x1b[35m", "\x06"),
    "cyan": ("\x1b[36m", "\x07"),
    "white": ("\x1b[37m", "\x08"),
    # Skip 0x09 to 0x0D (not supported by textwrap.wrap)
    "bright_black": ("\x1b[1;30m", "\x0E"),
    "bright_red": ("\x1b[1;31m", "\x0F"),
    "bright_green": ("\x1b[1;32m", "\x10"),
    "bright_yellow": ("\x1b[1;33m", "\x11"),
    "bright_blue": ("\x1b[1;34m", "\x12"),
    "bright_magenta": ("\x1b[1;35m", "\x13"),
    "bright_cyan": ("\x1b[1;36m", "\x14"),
    "bright_white": ("\x1b[1;37m", "\x15"),
    "bold": ("\x1b[1m", "\x16"),
    "off": ("\x1b[0m", "\x17"),
}
COLOR = {key: color for key, (color, _) in COLOR_CODES.items()}
SCOLOR = {key: scolor for key, (_, scolor) in COLOR_CODES.items()}
THEMES = {
    "default": {
        "keys": "green",
        "quotes": "off",
        "message_key": "off",
        "timestamp_key": "off",
        "levels": {
            "trace": "cyan",
            "debug": "bright_cyan",
            "info": "bright_green",
            "notice": "bright_green",
            "warn": "bright_yellow",
            "warning": "bright_yellow",
            "error": "bright_red",
            "err": "bright_red",
            "fatal": "bright_red",
            "panic": "bright_red",
            "alert": "bright_red",
            "crit": "bright_red",
            "emerg": "bright_red",
        },
        "context_prefix": {
            "before": "blue",
            "match": "bright_magenta",
            "after": "blue",
            "fuse_first": "blue",
            "fuse_last": "blue",
        },
        "skipped_marker": {"before": "cyan", "number": "cyan", "after": "cyan"},
    },
    "classic": {
        "keys": "green",
        "quotes": "black",
        "message_key": "bright_black",
        "timestamp_key": "off",
        "levels": {
            "trace": "blue",
            "debug": "cyan",
            "info": "bright_green",
            "notice": "bright_green",
            "warn": "bright_yellow",
            "warning": "bright_yellow",
            "error": "bright_red",
            "err": "bright_red",
            "fatal": "bright_red",
            "panic": "bright_red",
            "alert": "bright_red",
            "crit": "bright_red",
            "emerg": "bright_red",
        },
        "context_prefix": {
            "before": "blue",
            "match": "bright_magenta",
            "after": "blue",
            "fuse_first": "blue",
            "fuse_last": "blue",
        },
        "skipped_marker": {"before": "blue", "number": "black", "after": "blue"},
    },
}

BUILTIN_REGEXES = {
    # https://stackoverflow.com/questions/6038061/regular-expression-to-find-urls-within-a-string
    "url": [
        r"\b(http|ftp|https):\/\/([\w-]+(?:(?:\.[\w_-]+)+))([\w.,@?^=%&:\/~+#-]*[\w@?^=%&\/~+#-])\b"
    ],
    # https://www.regular-expressions.info/email.html
    "email": [r"\b[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}\b"],
    "err": [
        r"(?i)\b(error|err|panic|crit|critical|alert|fatal|emerg|failed|failure|exception)\b"
    ],
    "path": [r"(^|(?<=[^/\w-]))(/[\w-]+)+/?"],
    "ipv4": [
        r"\b(([0-9]|[1-9][0-9]|1[0-9]{2}|2[0-4][0-9]|25[0-5])\.){3}([0-9]|[1-9][0-9]|1[0-9]{2}|2[0-4][0-9]|25[0-5])\b"
    ],
}


EPILOG = f"""
INTERVAL units: us=microseconds/ms=milliseconds/s=seconds/m=minutes/h=hours/d=days/w=weeks"
Highlighted keys: {','.join(TS_KEYS + MSG_KEYS + LEVEL_KEYS)}
"""

terminal_width = shutil.get_terminal_size((80, 24)).columns


def build_globals_dict(modules):
    d = {}
    for module in modules:
        name = module.__name__
        alt_name = "_" + name
        d[name] = d[alt_name] = module
    return d


# Make some modules available for use in filters and templates
EXPORTED_GLOBALS = build_globals_dict(
    [
        base64,
        collections,
        datetime,
        hashlib,
        itertools,
        json,
        pprint,
        random,
        re,
        string,
        textwrap,
    ]
)


def expand_color_codes(line):
    for _, (color, scolor) in COLOR_CODES.items():
        line = line.replace(scolor, color)
    return line


@dataclasses.dataclass
class Stats:
    # Sets would make this a few percent faster and simplify the implementation.
    # But we want to show items in their original order.
    keys: list
    loglevel_keys: list
    loglevels: list
    num_lines_seen: int
    num_events_shown: int
    first_timestamp: str
    last_timestamp: str
    timespan: str


class StoppedEarly(Exception):
    pass


def timedelta_from(duration):
    pattern = re.compile(r"([-\d.]+)([a-z]+)")
    matches = pattern.findall(duration)
    if not matches:
        raise argparse.ArgumentTypeError(f"Invalid timespec: {duration}")

    unit_map = {
        "us": "microseconds",
        "ms": "microseconds",  # timedelta argument, not description
        "s": "seconds",
        "m": "minutes",
        "h": "hours",
        "d": "days",
        "w": "weeks",
    }

    result = dt.timedelta()
    for value, unit in matches:
        value = float(value)
        if value < 0:
            raise argparse.ArgumentTypeError(
                f"Durations cannot be negative: {duration}"
            )

        if unit in unit_map:
            if unit == "ms":
                value *= 1000  # Convert milliseconds to microseconds
            result += dt.timedelta(**{unit_map[unit]: value})
        else:
            raise argparse.ArgumentTypeError(
                f"Unsupported time unit: '{unit}. Supported units: {', '.join(unit_map.keys())}"
            )

    return result


def format_ts_delta(timedelta):
    if timedelta is None:
        return "unknown"
    # XXX: better heuristics
    s = str(timedelta)
    if not "." in s:
        s += ".000000"
    return s


def add_ts_delta(event, last_ts_datetime):
    ts_datetime = get_timestamp_datetime(event)
    if last_ts_datetime is None:
        delta = ts_datetime
    else:
        delta = ts_datetime - last_ts_datetime
    last_ts_datetime = ts_datetime
    # Add to start of event dict so that delta is displayed first
    new_event = {"_ts_delta": format_ts_delta(delta)}
    new_event.update(event)
    return new_event, last_ts_datetime


def datetime_from(text):
    "Parse date given e.g. in command line arguments (localtime assumed)"
    midnight_today_localtime = (
        dt.datetime.now()
        .astimezone()
        .replace(hour=0, minute=0, second=0, microsecond=0)
    )
    midnight_today_utc = dt.datetime.now(tz=dt.timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )

    if text == "today":
        return midnight_today_localtime
    elif text == "tomorrow":
        return midnight_today_localtime + dt.timedelta(days=1)
    elif text == "yesterday":
        return midnight_today_localtime - dt.timedelta(days=1)
    elif text == "todayZ":
        return midnight_today_utc
    elif text == "tomorrowZ":
        return midnight_today_utc + dt.timedelta(days=1)
    elif text == "yesterdayZ":
        return midnight_today_utc - dt.timedelta(days=1)
    elif text == "now":
        return dt.datetime.now(tz=dt.timezone.utc)
    else:
        datetime = guess_datetime(text, with_args=False)
        if datetime is None:
            raise argparse.ArgumentTypeError(f"Not a valid timestamp: {text}")
        return datetime


datetime_converters = [
    # We don't want external dependencies like dateutil
    # military timezone (Z), very common for our K8s config
    lambda s: dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
        tzinfo=dt.timezone.utc
    ),
    lambda s: dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%S.%fZ").replace(
        tzinfo=dt.timezone.utc
    ),
    lambda s: dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=dt.timezone.utc
    ),
    lambda s: dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%SZ").replace(
        tzinfo=dt.timezone.utc
    ),
    # XXX: datetime does not support nanoseconds, so we just treat them as 000
    lambda s: dt.datetime.strptime(s[:-4] + s[-1:], "%Y-%m-%dT%H:%M:%S.%fZ").replace(
        tzinfo=dt.timezone.utc
    ),
    # usual timezone (+01:00), as we print with -z
    lambda s: dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%S.%f%z"),
    lambda s: dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%S.%f%z"),
    lambda s: dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%S%z"),
    lambda s: dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%S%z"),
    # no timezone given (treat as localtime; for UTC, users could easily add Z)
    lambda s: dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%S.%f").astimezone(),
    lambda s: dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%S.%f").astimezone(),
    lambda s: dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%S").astimezone(),
    lambda s: dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%S").astimezone(),
    # XXX: datetime does not support nanoseconds, so we just treat them as 000
    lambda s: dt.datetime.strptime(
        s[:-4] + s[-1:], "%Y-%m-%dT%H:%M:%S.%f"
    ).astimezone(),
    # NCSA Common Log Format
    lambda s: dt.datetime.strptime(s, "%d/%b/%Y:%H:%M:%S %z").astimezone(),
    # RFC 2822 (date -R)
    lambda s: dt.datetime.strptime(s, "%a, %d %b %Y %H:%M:%S %z").astimezone(),
    # date -u
    lambda s: dt.datetime.strptime(' '.join(s.split()[1:]), "%b %d %H:%M:%S UTC %Y").replace(
        tzinfo=dt.timezone.utc
    ),
    # only date
    lambda s: dt.datetime.strptime(s, "%Y-%m-%d").astimezone(),
    lambda s: dt.datetime.strptime(s, "%Y-%m").astimezone(),
    lambda s: dt.datetime.strptime(s, "%Y").astimezone(),
    lambda s: dt.datetime.strptime(s, '%Y %b %d %H:%M').astimezone(),
    # Assume current year if not given
    lambda s: dt.datetime.strptime(f'{dt.datetime.now().year} {s}', '%Y %b %d %H:%M:%S.%f').astimezone(),
    lambda s: dt.datetime.strptime(f'{dt.datetime.now().year} {s}', '%Y %b %d %H:%M:%S').astimezone(),
    lambda s: dt.datetime.strptime(f'{dt.datetime.now().year} {s}', '%Y %b %d %H:%M').astimezone(),
    # Unix timestamps (seconds since epoch)
    lambda s: dt.datetime.fromtimestamp(float(s)),
    # Nginx timestamps (milliseconds since epoch)
    lambda s: dt.datetime.fromtimestamp(int(s)/1000),
]

dt_conv_order = list(range(len(datetime_converters)))


def guess_datetime(timestamp, with_args=True):
    global dt_conv_order
    datetime = None
    for i in dt_conv_order:
        converter = datetime_converters[i]
        try:
            datetime = converter(timestamp)
            # print("guess", i, timestamp, "as", datetime, datetime.tzname()) # debug
            break
        except (AttributeError, ValueError, TypeError):
            continue
    # Bring found converter to the front so that it's tried first next time
    if not dt_conv_order[0] == i:
        dt_conv_order.insert(0, dt_conv_order.pop(i))
    return datetime


def now_rfc3339():
    return dt.datetime.now(tz=dt.timezone.utc).isoformat().replace("+00:00", "Z")


def to_datetime(timestamp):
    if timestamp is None:
        raise ValueError(
            "No timestamp found. Cannot filter on timestamp for this log file."
        )
    datetime = guess_datetime(timestamp)
    if datetime is None:
        raise ValueError(
            f"Could not parse timestamp '{timestamp}'. Cannot filter on timestamp for this log file."
        )
    return datetime


def get_timestamp_datetime(event):
    if args.ts_key and args.ts_key in event:
        return to_datetime(event[args.ts_key])
    elif "timestamp" in event:
        return to_datetime(event["timestamp"])
    elif "ts" in event:
        return to_datetime(event["ts"])
    elif "time" in event:
        return to_datetime(event["time"])
    elif "at" in event:
        return to_datetime(event["at"])
    else:
        return None


def format_time(val):
    if args.localtime:
        val = to_datetime(val).astimezone().isoformat(timespec="milliseconds")
    elif args.utc:
        val = (
            to_datetime(val)
            .astimezone(dt.timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )
    return val


def show(event, context_type="", lineno=None):
    event = reorder(event)
    if args.output_format == "default":
        if args.output_template:
            show_by_template(event, args.output_template)
        elif args.output_eval:
            show_by_eval_template(event, args.output_eval)
        else:
            show_default(event, context_type, lineno)
    elif args.output_format == "jsonl":
        show_jsonl(event)
    elif args.output_format == "tsv":
        show_tsv(event)


def unsentinel(s):
    return s.replace(SENTINEL, args.output_quote)


def show_jsonl(event):
    unquoted = {unsentinel(k): unsentinel(v) for k, v in event.items()}
    print(json.dumps(unquoted))


def show_by_template(event, template):
    template = template.replace("\\n", "\n").replace("\\t", "\t")
    try:
        out = template.format(**event)
        print(out)
    except KeyError:
        pass


def show_by_eval_template(event, template):
    template = template.replace("\\n", "\n").replace("\\t", "\t")
    # Find {expression} patterns
    pattern = re.compile(r"\{(.*?)\}")

    # Replace each match with its evaluated result
    def replace_expr(match):
        expr = match.group(1)
        event_plus_underscore = event.copy()
        event_plus_underscore["_"] = event
        try:
            return str(eval(expr, EXPORTED_GLOBALS, event_plus_underscore))
        except Exception as e:
            if args.debug:
                print(f"[Error evaluating '{expr} on {event}': {e}]", file=sys.stderr)
            return ""

    # Replace all expressions in the template
    out = pattern.sub(replace_expr, template)
    if out:
        print(out)


def show_tsv(event):
    cols = []
    for key in args.keys:
        cols.append(unsentinel(event.get(key, "")))
    print("\t".join(cols))


def show_default(event, context_type="", lineno=None):
    colors = THEMES[args.theme]["context_prefix"]
    context_prefix = {
        "before": scolorize("/ ", colors["before"]),
        "match": scolorize("* ", colors["match"]),
        "after": scolorize("\\ ", colors["after"]),
        "fuse_first": scolorize("1 ", colors["fuse_first"]),
        "fuse_last": scolorize(f"{lineno} ", colors["fuse_last"]),
    }.get(context_type, "")

    if args.each_key:
        # One part per item
        parts = [{key: val} for key, val in event.items()]
    else:
        # Only one part
        parts = [event]

    out = []
    for part in parts:
        elems = []
        for key, val in part.items():
            key_lower = key.lower()
            val = unsentinel(val)

            key_color = THEMES[args.theme]["keys"]
            quote_color = THEMES[args.theme]["quotes"]
            val_color = ""
            if key_lower in TS_KEYS:
                val_color = THEMES[args.theme]["timestamp_key"]
                val = format_time(val)
            elif key_lower in LEVEL_KEYS:
                val_color = THEMES[args.theme]["levels"].get(val.lower(), "off")
            elif key_lower in MSG_KEYS:
                val_color = THEMES[args.theme]["message_key"]

            if args.color:
                if args.plain:
                    elems.append(SCOLOR["off"] + scolorize(val, val_color))
                else:
                    elems.append(
                        SCOLOR["off"]
                        + scolorize(key, key_color)
                        + scolorize('="', quote_color)
                        + scolorize(val, val_color)
                        + scolorize('"', quote_color)
                    )
            else:
                if args.plain:
                    elems.append(f"{val}")
                else:
                    elems.append(f'{key}="{val}"')

        text = args.output_sep.join(elems)
        val_lines = RE_EOL_OR_TAB.split(text) if args.expand else [text]
        for v, text in enumerate(val_lines):
            indent = ""
            if context_prefix and v == 0 and part == parts[0]:
                text = context_prefix + text
                context_indent = "  "
            else:
                context_indent = ""
            if args.output_width:
                text = text[: args.output_width]
            if part != parts[0]:
                indent += "  "
            if args.expand and v > 0:
                indent += "    "
            if args.output_wrap:
                for line in textwrap.wrap(
                    text,
                    width=args.output_wrap,
                    initial_indent=indent,
                    subsequent_indent=indent + context_indent + "  ",
                ):
                    if line:
                        out.append(line)
            else:
                out.append(text)
    for line in out:
        if args.color:
            print(expand_color_codes(line))
        else:
            print(line)


def print_err(*args, **kwargs):
    print(*args, file=sys.stderr, **kwargs)


def colorize(text, color):
    if args.color and color:
        return COLOR[color] + text + COLOR["off"]
    else:
        return text


def scolorize(text, color):
    if args.color and color:
        return SCOLOR[color] + text + SCOLOR["off"]
    else:
        return text


def colorize_loglevels(keys):
    return [
        colorize(key, THEMES[args.theme]["levels"].get(key.lower(), "off"))
        for key in keys
    ]


def show_stats(stats):
    if args.color:
        print_err(COLOR["off"], end="")
    colors = THEMES[args.theme]
    seen = stats.num_lines_seen
    shown = stats.num_events_shown
    if seen > 0 and shown > 0:
        percent = f" ({shown/seen:.0%} of { seen } lines seen)"
    else:
        percent = ""
    print_err(f"Events shown: { shown }{percent}")
    if stats.first_timestamp:
        print_err(
            f"Time span shown: { colorize(format_time(stats.first_timestamp), colors['timestamp_key']) } "
            f"to { colorize(format_time(stats.last_timestamp), colors['timestamp_key']) }"
            f" ({ to_datetime(stats.last_timestamp) - to_datetime(stats.first_timestamp) })"
        )
    print_err(f"Keys seen: { ','.join( colorize(key, colors['keys']) for key in stats.keys)}")
    print_err(
        f"Log levels seen: { ','.join(colorize_loglevels(stats.loglevels))}"
        f" (keys: {','.join(stats.loglevel_keys)})"
    )


def show_stats_json(stats):
    print_err(json.dumps(dataclasses.asdict(stats), indent=2))


def update_stats(stats, event):
    global args
    # Don't compute stats when not needed. Improves performance
    if not (args.stats or args.stats_only or args.max_events or args.timespan):
        return stats
    stats.num_events_shown += 1
    # We cannot use sets because they cannot be converted into JSON
    for key in event:
        if key not in stats.keys:
            stats.keys.append(key)
    loglevel_key, loglevel = get_log_level(event)
    if loglevel_key and loglevel_key not in stats.loglevel_keys:
        stats.loglevel_keys.append(loglevel_key)
    if loglevel and loglevel not in stats.loglevels:
        stats.loglevels.append(loglevel)
    ts = get_timestamp_str_or_none(event)
    if ts:
        # FIXME: This string comparison only works for ISO dates
        if stats.first_timestamp == "" or ts < stats.first_timestamp:
            stats.first_timestamp = ts
            if args.timespan is not None:
                temp = to_datetime(stats.first_timestamp) + args.timespan
                if args.to_dt is None:
                    args.to_dt = temp
                else:
                    args.to_dt = min(temp, args.to_dt)
        stats.last_timestamp = ts
    return stats


def get_timestamp_str_or_none(event):
    if args.ts_key:
        return event.get(args.ts_key, None)
    else:
        return (
            event.get("timestamp", None) or event.get("ts", None) or event.get("time", None) or event.get("at", None)
        )


def get_log_level(event):
    for key in LEVEL_KEYS:
        try:
            return key, event[key]
        except KeyError:
            continue
    return None, None


def key_matches(regex, key, event):
    val = event.get(key, "")
    if val and regex.search(val):
        return True
    return False


def matches_python_expr(expr, event):#
    # XXX: Be more intelligent about this
    def unsentinel2(s):
        return s.replace(SENTINEL, '"')
    event = {unsentinel2(k): unsentinel2(v) for k, v in event.items()}
    event_plus_underscore = event.copy()
    event_plus_underscore["_"] = event
    try:
        return eval(expr, EXPORTED_GLOBALS, event_plus_underscore)
    except Exception as e:
        if args.debug:
            print(f"[Error: {e}. event={event}]", file=sys.stderr)
        return False


def visible(line, event):
    if (
        (args.grep_not and any(regex.search(line) for regex in args.grep_not))
        or (args.grep and not any(regex.search(line) for regex in args.grep))
        or (args.where and not matches_python_expr(args.where, event))
    ):
        return False
    if not event:
        return False

    if (
        args.grep_not_by_key
        and any(
            key_matches(regex, key, event)
            for key, regex in args.grep_not_by_key.items()
        )
    ) or (
        args.grep_by_key
        and not any(
            key_matches(regex, key, event) for key, regex in args.grep_by_key.items()
        )
    ):
        return False

    try:
        if args.from_dt is not None or args.to_dt is not None:
            dt_event = get_timestamp_datetime(event)
            if dt_event is not None:
                if args.from_dt is not None and dt_event < args.from_dt:
                    return False
                if args.to_dt is not None and dt_event > args.to_dt:
                    if len(args.files) < 2:
                        raise StoppedEarly
                    return False
    except ValueError as exc:
        print(exc)
        sys.exit(1)

    if args.loglevels or args.loglevels_not:
        _, level = get_log_level(event)
        if level is not None:
            level = level.lower()
            if (args.loglevels_not and level in args.loglevels_not) or (
                args.loglevels and level not in args.loglevels
            ):
                return False
    # Regard events with no selected keys as invisible
    if not reorder(event):
        return False
    return True


def reorder(event):
    """Reorder event keys in the order given on the command-line
    and remove unwanted keys"""
    return {
        key: event[key]
        for key in args.keys or event.keys()
        if key in event and key not in args.keys_not
    }


def parse(line, format):
    if format == "logfmt":
        return parse_logfmt(line)
    elif format == "jsonl":
        return parse_jsonl(line)
    elif format in ["json", "tsv"]:
        # JSON and TSV files have been converted to logfmt
        return parse_logfmt(line)
    elif format == "clf":
        return parse_clf(line)
    elif format == "combined":
        return parse_combined(line)
    else:
        print_err("Unknown input format.")
        exit()


def parse_logfmt(line):
    return {key: val.strip('"') for key, val in RE_LOGFMT.findall(line)}

def parse_jsonl(line):
    # Only handle top-level strings. Everything else is converted into a string
    result = {}
    try:
        # Ignore text before and after JSON object
        json_str = line[line.index('{'):line.rindex('}')+1]
        json_data = json.loads(json_str)
    except (ValueError, json.decoder.JSONDecodeError) as exc:
        if args.debug:
            print_err(line, end="")
            print_err(f"Invalid JSON syntax in the above line:", exc)
            sys.exit(1)
        else: 
            return result
    for key, val in flatten_json(json_data).items():
        if isinstance(val, str):
            result[key] = val
        else:
            result[key] = repr(val)
    return result


def parse_clf(line):
    match = RE_CLF.match(line)
    if match:
        d = match.groupdict()
        if d["size"] == "-":
            d["size"] = "0"
        return d
    else:
        return {}


def parse_combined(line):
    match = RE_COMBINED.match(line)
    if match:
        d = match.groupdict()
        if d["size"] == "-":
            d["size"] = "0"
        return d
    else:
        return {}


def extract_key_regex(spec):
    m = RE_EXTRACT_KEY.match(spec)
    if m:
        return m.group(1), m.group(2)
    return None, spec


def make_regex_dict(specs):
    result = {}
    new_specs = []
    for spec in specs:
        key, regex = extract_key_regex(spec)
        if key:
            result[key] = regex
        else:
            new_specs.append(spec)
    return result, new_specs


def csv_type(text):
    return [] if text is None else text.split(",")


def csv_lower_type(text):
    return csv_type(text.lower())


def builtin_regex(text):
    if "~" in text:
        key, regex = text.split("~", 1)
        prefix = key + "~"
    else:
        regex = text
        prefix = ""
    try:
        return [prefix + b for b in BUILTIN_REGEXES[regex]]
    except KeyError:
        raise argparse.ArgumentTypeError(
            f"'{regex}' is not a builtin regex {list(BUILTIN_REGEXES)}"
        )


def positive_int(value):
    try:
        ivalue = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"Invalid value '{value}'. Must be a positive integer or zero."
        )
    if ivalue < 0:
        raise argparse.ArgumentTypeError(
            f"Invalid value '{value}'. Must be a positive integer or zero."
        )
    return ivalue


def flatten(l):
    return [item for sublist in l for item in sublist]


def parse_args():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        add_help=False,
        epilog=EPILOG,
    )
    parser.add_argument(
        "files",
        metavar="FILE",
        nargs="*",
        help="files to read, if empty, stdin is used",
    )

    input = parser.add_argument_group("input options")
    input.add_argument(
        "--input-format",
        "-i",
        choices=["logfmt", "jsonl", "json", "tsv", "tap", "clf", "combined"],
        default="logfmt",
        help="format of the input data. Default: logfmt. tsv needs a header line. json cannot be streamed. tap is from 'linkerd viz tap'. clf is NCSA Common Log Format. combined is Extended Apache",
    )
    input.add_argument(
        "--jsonl-input", "-j", action="store_true", help="input format is JSON Lines"
    )
    input.add_argument(
        "--prefix",
        metavar="STR",
        default="",
        help="prepend given string to each log line before parsing it",
    )

    selection = parser.add_argument_group("event selection options")
    selection.add_argument(
        "--loglevels",
        "-l",
        metavar="LEVELS",
        type=csv_lower_type,
        help="comma-separated names of loglevels to process (case-insensitive). Use -S to show the loglevels available",
    )
    selection.add_argument(
        "--loglevels-not",
        "-L",
        metavar="LEVELS",
        type=csv_lower_type,
        help="comma-separated names of loglevels NOT to process (case-insensitive)",
    )

    grep = parser.add_argument_group(
        "event selection by regular expression (grepping) options"
    )
    grep.add_argument(
        "--grep",
        "-g",
        metavar="REGEX",
        default=[],
        action="append",
        help="only process lines matching the given REGEX. Use '(?i)REGEX' for case-insensitive matches. Use 'key~REGEX' to limit to a specific key. Can be given multiple times. Any of them matching will allow the line to be processed",
    )
    grep.add_argument(
        "--grep-not",
        "-G",
        "-v",
        metavar="REGEX",
        default=[],
        action="append",
        help="don't process lines matching REGEX. Use 'key~REGEX' to limit to a specific key. Can be given multiple times. Any of them matching will exclude the line from processing",
    )
    grep.add_argument(
        "--grep-builtin",
        "-r",
        metavar="REGEX",
        type=builtin_regex,
        default=[],
        action="append",
        help=f"only process lines according to one of the built-in regexes {list(BUILTIN_REGEXES)}. Use 'key~REGEX' to limit to a specific key. Can be given multiple times. Any of them matching will allow the line to be processed",
    )
    grep.add_argument(
        "--grep-builtin-not",
        "-R",
        metavar="REGEX",
        type=builtin_regex,
        default=[],
        action="append",
        help=f"don't process lines according to one of the built-in regexes {list(BUILTIN_REGEXES)}. Use 'key~REGEX' to limit to a specific key. Can be given multiple times. Any of them matching will allow the line to be processed",
    )
    grep.add_argument(
        "--where",
        metavar="EXPR",
        default="",
        help="only process lines where the given Python expression is True",
    )
    grep.add_argument(
        "--before-context",
        "-B",
        type=positive_int,
        default=0,
        metavar="NUM",
        help="print NUM lines of leading context before each matching event",
    )
    grep.add_argument(
        "--after-context",
        "-A",
        type=positive_int,
        default=0,
        metavar="NUM",
        help="print NUM lines of trailing context after each matching event",
    )
    grep.add_argument(
        "--context",
        "-C",
        type=positive_int,
        default=0,
        metavar="NUM",
        help="print NUM lines of  context before and after each matching event",
    )

    time_selection = parser.add_argument_group("event selection by time")
    time_selection.add_argument(
        "--ts-key",
        metavar="KEY",
        help="parse timestamp from KEY",
    )
    time_selection.add_argument(
        "--ts-format",
        metavar="FORMAT",
        help="add FORMAT to the list of timestamp formats to try",
    )
    time_selection.add_argument(
        "--from",
        dest="from_dt",
        type=datetime_from,
        metavar="ISODATETIME",
        help="show only lines for which timestamp is ISODATETIME or later."
        " Also: 'now', 'today', 'yesterday', 'tomorrow' (midnight localtime). Use 'todayZ' etc. for UTC.",
    )
    time_selection.add_argument(
        "--to",
        dest="to_dt",
        type=datetime_from,
        metavar="ISODATETIME",
        help="show only lines for which timestamp is ISODATETIME or earlier."
        " Also: 'now', 'today', 'yesterday', 'tomorrow' (midnight localtime). Use 'todayZ' etc. for UTC.",
    )
    time_selection.add_argument(
        "--since",
        metavar="INTERVAL",
        type=timedelta_from,
        help="show only lines for which timestamp is less than INTERVAL ago.",
    )
    time_selection.add_argument(
        "--until",
        metavar="INTERVAL",
        type=timedelta_from,
        help="show only lines for which timestamp is less than INTERVAL in the future.",
    )
    time_selection.add_argument(
        "--new", "-n", action="store_true", help="only print new lines (--since 0s)"
    )
    time_selection.add_argument(
        "--duration",
        "-d",
        metavar="INTERVAL",
        type=timedelta_from,
        help="show only lines for which timestamp is less than INTERVAL after program start or time given by --from/--since)",
    )
    time_selection.add_argument(
        "--timespan",
        metavar="INTERVAL",
        type=timedelta_from,
        help="show only lines for which timestamp is less than INTERVAL after time of first event",
    )

    output = parser.add_argument_group("output format options")
    output.add_argument(
        "--output-format",
        "-o",
        choices=["default", "jsonl", "tsv"],
        default="default",
        help="format of the output data. Default: default",
    )
    output.add_argument(
        "--jsonl-output", "-J", action="store_true", help="output in JSON Lines format"
    )
    output.add_argument(
        "--max-events",
        "-m",
        metavar="NUM",
        type=int,
        help="maximum number of events to show",
    )
    output.add_argument(
        "--common",
        "-c",
        action="store_true",
        help="show timestamp, log level and message. Activates --plain, --indent and --unicode",
    )
    output.add_argument(
        "--keys",
        "-k",
        metavar="KEYS",
        default=[],
        type=csv_type,
        help="comma-separated names of keys to display (default: all). Use -S to show available keys",
    )
    output.add_argument(
        "--keys-not",
        "-K",
        metavar="KEYS",
        default=[],
        type=csv_type,
        help="comma-separated names of keys NOT to display",
    )
    output.add_argument(
        "--no-plain", "-P", action="store_true", help="don't display values only"
    )
    output.add_argument(
        "--unicode",
        "-u",
        action="store_true",
        help="use unicode symbols to simplify/beautify output by replacing characters",
    )
    output.add_argument(
        "--no-unicode",
        "-U",
        action="store_true",
        help="don't use unicode symbols to simplify/beautify output by replacing characters",
    )

    default_output = parser.add_argument_group("default output format options")
    default_output.add_argument(
        "--plain", "-p", action="store_true", help="display values only"
    )
    default_output.add_argument(
        "--indent",
        action="store_true",
        help="wrap at terminal width and indent subsequent lines",
    )
    default_output.add_argument(
        "--no-indent",
        "-I",
        action="store_true",
        help="don't wrap at terminal width and don't indent subsequent lines",
    )
    default_output.add_argument(
        "--each-key",
        "-e",
        action="store_true",
        help="print each key on a separate line",
    )
    default_output.add_argument(
        "--no-wrap",
        "-W",
        action="store_true",
        help="limit output to terminal width. Text is cut there to prevent wrapping",
    )
    default_output.add_argument(
        "--localtime",
        "-z",
        action="store_true",
        help="output timestamps with local timezone (decreases performance)",
    )
    default_output.add_argument(
        "--utc",
        "-Z",
        action="store_true",
        help="output timestamps with UTC timezone (decreases performance)",
    )
    default_output.add_argument(
        "--no-color",
        action="store_true",
        help="no ANSI colors. Alternatively, set the NO_COLOR environment variable.",
    )
    default_output.add_argument(
        "--color",
        action="store_true",
        help="always use ANSI colors, even when output is not to a TTY (e.g. to a pipe)",
    )
    default_output.add_argument(
        "--theme",
        choices=THEMES.keys(),
        default=list(THEMES.keys())[0],
        help=f"use these color palette and style. Default: {list(THEMES.keys())[0]}",
    )
    default_output.add_argument(
        "--expand",
        "-E",
        action="store_true",
        help=f"split values at \\n and \\t characters and show on separate lines",
    )
    default_output.add_argument(
        "--output-sep",
        metavar="STR",
        default=" ",
        help="string to separate elements",
    )
    default_output.add_argument(
        "--output-template",
        metavar="STR",
        default="",
        help='Python f-string template for output, e.g. "{timestamp} {message}"',
    )
    default_output.add_argument(
        "--output-eval",
        metavar="STR",
        default="",
        help='''Python eval template for output, e.g. "{ts} {level.upper()} {'#'*len(msg)}"''',
    )

    output_special = parser.add_argument_group("special output format options")
    output_special.add_argument(
        "--stats",
        "-s",
        action="store_true",
        help="print statistics to stderr, in addition to regular output",
    )
    output_special.add_argument(
        "--stats-only",
        "-S",
        action="store_true",
        help="print statistics only (to stderr)",
    )
    output_special.add_argument(
        "--levelmap",
        "-M",
        action="store_true",
        help="output log levels only to give a big picture overview",
    )
    output_special.add_argument(
        "--fuse",
        metavar="INTERVAL",
        type=timedelta_from,
        help="for a sequence of events that are separated by less then INTERVAL,"
        " show only the first and last.",
    )
    output_special.add_argument(
        "--debug",
        action="store_true",
        help="print exceptions",
    )

    other = parser.add_argument_group("other options")
    other.add_argument(
        "--selftest",
        action="store_true",
        help="run tests",
    )
    other.add_argument(
        "--version",
        action="version",
        version="%(prog)s v" + __version__,
        help="show version number",
    )
    other.add_argument(
        "-h", "--help", action="help", help="show this help message and exit"
    )

    args = parser.parse_args()

    if sys.stdin.isatty() and not args.files:
        parser.print_usage()
        sys.exit(0)

    args.color = (
        args.color
        or sys.stdout.isatty()
        and not (args.no_color or "NO_COLOR" in os.environ)
    )

    if args.jsonl_input:
        args.input_format = "jsonl"
    if args.jsonl_output:
        args.output_format = "jsonl"

    if args.output_format == "tsv" and not args.keys:
        print_err(
            "TSV format needs explicit list of keys. Use -S to list and copy them, then rerun with -k."
        )
        sys.exit(1)

    args.add_ts = "_ts" in args.keys
    args.add_ts_delta = "_ts_delta" in args.keys
    args.add_line = "_line" in args.keys

    global TS_KEYS
    if args.ts_key:
        # XXX: Don't modify what looks like a constant
        TS_KEYS.append(args.ts_key)

    if args.common:
        args.keys = TS_KEYS + LEVEL_KEYS + MSG_KEYS + args.keys
        args.plain = True
        args.unicode = True
        args.indent = True

    if args.no_plain:
        args.plain = False
    if args.no_unicode:
        args.unicode = False
    if args.no_indent:
        args.indent = False

    if args.localtime and args.utc:
        print_err("Choose either localtime or UTC")
        sys.exit(1)

    if args.input_format == "tap":
        args.prefix += "_type="

    if args.context > 0:
        args.before_context = args.context
        args.after_context = args.context

    args.grep += flatten(args.grep_builtin)
    args.grep_not += flatten(args.grep_builtin_not)

    # Move key=regex args into separate variables
    args.grep_by_key, args.grep = make_regex_dict(args.grep)
    args.grep_not_by_key, args.grep_not = make_regex_dict(args.grep_not)

    # compile regexes to improve performance
    try:
        args.grep = [re.compile(regex) for regex in args.grep]
        args.grep_not = [re.compile(regex) for regex in args.grep_not]
        args.grep_by_key = {
            key: re.compile(regex) for key, regex in args.grep_by_key.items()
        }
        args.grep_not_by_key = {
            key: re.compile(regex) for key, regex in args.grep_not_by_key.items()
        }
    except re.error as exc:
        print_err("Invalid regular expression:", exc)
        sys.exit(1)

    if args.ts_format:
        global datetime_converters
        global dt_conv_order
        # Try user-specified format first
        datetime_converters.insert(0, lambda s: dt.datetime.strptime(s, args.ts_format).astimezone())
        dt_conv_order = list(range(len(datetime_converters)))

    if args.new:
        args.since = timedelta_from("0s")

    if args.since is not None:
        args.from_dt = dt.datetime.now().astimezone() - args.since
    if args.until is not None:
        args.to_dt = dt.datetime.now().astimezone() + args.until
    if args.duration is not None:
        if args.duration < dt.timedelta.resolution:
            print_err("Duration must not be zero. This would not select any data.")
            sys.exit(1)
        if args.from_dt is None:
            args.from_dt = dt.datetime.now(tz=dt.timezone.utc)
        if args.to_dt is None:
            args.to_dt = args.from_dt + args.duration
        else:
            args.to_dt = min(args.from_dt + args.duration, args.to_dt)
    if args.timespan is not None and args.timespan < dt.timedelta.resolution:
        print_err("Time span must not be zero. This would not select any data.")
        sys.exit(1)

    if args.plain or args.output_format in ["jsonl", "tsv"]:
        args.output_quote = '"'
    elif args.unicode:
        args.output_quote = "\u201c"
    else:
        args.output_quote = INPUT_QUOTE

    args.output_width = None
    args.output_wrap = None

    if args.indent and not args.output_wrap:
        args.output_wrap = terminal_width
    if args.no_wrap:
        args.output_wrap = None
        args.output_width = terminal_width
    if args.output_sep:
        args.output_sep = args.output_sep.replace("\\n", "\n").replace("\\t", "\t")
    return args


def show_skipped_marker(skipped):
    colors = THEMES[args.theme]["skipped_marker"]
    print(
        colorize("\n  [...", colors["before"]),
        colorize(str(skipped), colors["number"]),
        colorize(
            "non-matching input line(s) not shown ...]\n",
            colors["after"],
        ),
        file=sys.stderr,
    )


def colored_levelchar(event):
    _, loglevel = get_log_level(event)
    if loglevel is None:
        return "."
    if args.color:
        return colorize(
            loglevel[0],
            THEMES[args.theme]["levels"].get(loglevel.lower(), "off"),
        )
    else:
        return loglevel[0]

def file_generator(filenames):
    """Yields lines from multiple files, which may be compressed."""
    if not filenames:
        filenames = ["-"]
    for filename in filenames:
        if filename in ["-"]:
            for line in sys.stdin:
                yield line
        elif filename.lower().endswith('.gz'):
            with gzip.open(filename, 'rt') as f:
                for line in f:
                    yield line
        else:
            with open(filename, 'r') as f:
                for line in f:
                    yield line

class MyTests(unittest.TestCase):
    def test_guess_datetime_military_ns(self):
        self.assertEqual(
            guess_datetime("2022-12-30T20:55:00.123000000Z"),
            dt.datetime(2022, 12, 30, 20, 55, 0, 123000, tzinfo=dt.timezone.utc),
        )

    def test_guess_datetime_military_ms(self):
        self.assertEqual(
            guess_datetime("2022-12-30T20:55:00.123Z"),
            dt.datetime(2022, 12, 30, 20, 55, 0, 123000, tzinfo=dt.timezone.utc),
        )

    def test_guess_datetime_military_s(self):
        self.assertEqual(
            guess_datetime("2022-12-30T20:55:00Z"),
            dt.datetime(2022, 12, 30, 20, 55, 0, 0, tzinfo=dt.timezone.utc),
        )

    def test_guess_datetime_no_tz_ns(self):
        self.assertEqual(
            guess_datetime("2022-12-30T20:55:00.123000000"),
            dt.datetime(2022, 12, 30, 20, 55, 0, 123000).astimezone(),
        )

    def test_guess_datetime_no_tz_ms(self):
        self.assertEqual(
            guess_datetime("2022-12-30T20:55:00.123"),
            dt.datetime(2022, 12, 30, 20, 55, 0, 123000).astimezone(),
        )

    def test_guess_datetime_no_tz(self):
        self.assertEqual(
            guess_datetime("2022-12-30T20:55:00"),
            dt.datetime(2022, 12, 30, 20, 55, 0, 0).astimezone(),
        )

    def test_guess_datetime_only_date(self):
        self.assertEqual(
            guess_datetime("2022-12-30"),
            dt.datetime(2022, 12, 30, 0, 0, 0, 0).astimezone(),
        )

    def test_guess_datetime_only_year_month(self):
        self.assertEqual(
            guess_datetime("2022-12"),
            dt.datetime(2022, 12, 1, 0, 0, 0, 0).astimezone(),
        )

    def test_guess_datetime_offset(self):
        self.assertEqual(
            guess_datetime("2022-12-30T22:55:00+02:00"),
            dt.datetime(2022, 12, 30, 20, 55, 0, 0, tzinfo=dt.timezone.utc),
        )

    def test_timedelta_from_microseconds(self):
        self.assertEqual(
            timedelta_from("4us"),
            dt.timedelta(microseconds=4),
        )

    def test_timedelta_from_milliseconds(self):
        self.assertEqual(
            timedelta_from("4ms"),
            dt.timedelta(microseconds=4000),
        )

    def test_timedelta_from_seconds(self):
        self.assertEqual(
            timedelta_from("54s"),
            dt.timedelta(seconds=54),
        )

    def test_timedelta_from_minutes(self):
        self.assertEqual(
            timedelta_from("4m"),
            dt.timedelta(minutes=4),
        )

    def test_timedelta_from_hours(self):
        self.assertEqual(
            timedelta_from("4h"),
            dt.timedelta(hours=4),
        )

    def test_timedelta_from_days(self):
        self.assertEqual(
            timedelta_from("2d"),
            dt.timedelta(days=2),
        )

    def test_timedelta_from_weeks(self):
        self.assertEqual(
            timedelta_from("5w"),
            dt.timedelta(weeks=5),
        )

    def test_timedelta_from_floatseconds(self):
        self.assertEqual(
            timedelta_from("1.567s"),
            dt.timedelta(seconds=1, microseconds=567000),
        )

    def test_timedelta_from_no_unit(self):
        self.assertRaises(
            argparse.ArgumentTypeError,
            timedelta_from,
            "567",
        )

    def test_timedelta_from_no_negatives(self):
        self.assertRaises(
            argparse.ArgumentTypeError,
            timedelta_from,
            "-3s",
        )

    def test_builtin_regexes_compile(self):
        for name, regexes in BUILTIN_REGEXES.items():
            for regex in regexes:
                try:
                    re.compile(regex)
                except re.error as e:
                    self.fail(
                        f"Regex compilation for {name} failed with error: {e.msg}"
                    )


def do_tests():
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromTestCase(MyTests)
    runner = unittest.TextTestRunner()
    result = runner.run(suite)
    return result


def flatten_json(json_data, separator="."):
    flattened = {}

    def flatten(x, name=""):
        if type(x) is dict:
            for a in x:
                flatten(x[a], name + a + separator)
        elif type(x) is list:
            i = 0
            for a in x:
                flatten(a, name + str(i) + separator)
                i += 1
        else:
            flattened[name[:-1]] = x

    flatten(json_data)
    return flattened


def read_json_from_input(filename):
    if filename in ["-", None]:
        data = json.load(sys.stdin)
    else:
        if filename.lower().endswith('.gz'):
            with gzip.open(filename, 'rt') as f:
                data = json.load(f)
        else:
            with open(filename, "r") as f:
                data = json.load(f)
    return data


def lines_from_jsonfiles(filenames):
    out = []

    def build_line(flat):
        line = ""
        for k, v in flat.items():
            if isinstance(v, str):
                line += f'{k}="{v}" '
            else:
                line += f"{k}={v} "
        return line

    if not filenames:
        filenames = ["-"]
    for filename in filenames:
        data = read_json_from_input(filename)
        if type(data) is list:
            for elem in data:
                flat = flatten_json(elem)
                line = build_line(flat)
                out.append(line)
        else:
            flat = flatten_json(data)
            line = build_line(flat)
            out.append(line)
    return out


def lines_from_tsvfiles(filenames):
    if not filenames:
        filenames = ["-"]
    for filename in filenames:
        if filename in ["-", None]:
            f = sys.stdin
        elif filename.lower().endswith(".gz"):
            f = gzip.open(filename, "rt")
        else:
            f = open(filename, "r")
        reader = csv.reader(f, delimiter="\t")
        # TODO: Support files without header
        headers = next(reader)
        for row in reader:
            line = " ".join(f'{key}="{value}"' for key, value in zip(headers, row))
            yield line


def main():
    global args
    interrupted = False
    stats = Stats([], [], [], 0, 0, "", "", "")
    try:
        args = parse_args()
        if args.selftest:
            if do_tests().wasSuccessful():
                sys.exit(0)
            else:
                sys.exit(1)
        if args.output_format == "tsv":
            # Header
            print("\t".join(args.keys))
        show_context = (
            args.context or args.before_context or args.after_context
        ) and not args.stats_only
        before_context = collections.deque(maxlen=args.before_context)
        after_context_num = 0
        last_ts_datetime = None
        ts_delta = 0
        skipped = 0
        fuse_skipped = 0
        colored_levelline = []
        levelchars = 0
        formatted_time = ""
        len_formatted_time = 0
        fuse_maybe_last = None
        if args.input_format == "json":
            lines = lines_from_jsonfiles(args.files)
        elif args.input_format == "tsv":
            lines = lines_from_tsvfiles(args.files)
        else:
            lines = file_generator(args.files)
        for line in lines:
            stats.num_lines_seen += 1
            # quoted double quotes would break our parser
            # replace with a symbol that shouldn't occur otherwise
            line = line.replace(INPUT_QUOTE, SENTINEL)
            # Do whole-line matches here to prevent parsing if line is not included
            if (
                args.grep_not and any(regex.search(line) for regex in args.grep_not)
            ) or (args.grep and not any(regex.search(line) for regex in args.grep)):
                if after_context_num == 0:
                    if args.max_events and stats.num_events_shown >= args.max_events:
                        raise StoppedEarly
                    # Only skip if there are no after_context lines to be printed
                    if len(before_context) == args.before_context:
                        skipped += 1
                    before_context.append(line)
                    continue

            if args.add_line:
                event = {"_line": line.rstrip(), "_ts": now_rfc3339()}
            else:
                if args.add_ts:
                    line = line + f' _ts="{now_rfc3339()}"'
                if args.prefix:
                    line = args.prefix + line
                event = parse(line, args.input_format)

            if visible(line, event):
                # breakpoint()
                if args.fuse is not None:
                    ts_datetime = get_timestamp_datetime(event)
                    if ts_datetime is None:
                        # timestamp unknown: ignore event
                        if fuse_maybe_last:
                            show(fuse_maybe_last)
                        continue
                    elif last_ts_datetime is None:
                        # first block ever: show
                        last_ts_datetime = ts_datetime
                    else:
                        ts_delta = ts_datetime - last_ts_datetime
                        last_ts_datetime = ts_datetime
                        if ts_delta < args.fuse:
                            # old block, ignore for now, but save for later
                            # (will have to be printed if it was the last in block)
                            fuse_maybe_last = event
                            fuse_skipped += 1
                            continue
                        else:
                            # new block: show last event from last block first
                            if fuse_maybe_last:
                                show(
                                    fuse_maybe_last,
                                    "fuse_last",
                                    lineno=1 + fuse_skipped + 1,
                                )
                                print("", file=sys.stderr)
                if show_context and skipped > 0 and args.output_format == "default":
                    show_skipped_marker(skipped)
                skipped = 0
                if args.max_events and stats.num_events_shown >= args.max_events:
                    raise StoppedEarly
                if args.add_ts_delta:
                    event, last_ts_datetime = add_ts_delta(event, last_ts_datetime)
                if args.levelmap and not args.stats_only:
                    levelchars += 1
                    if levelchars == 1:
                        ts = get_timestamp_str_or_none(event)
                        if ts:
                            formatted_time = format_time(ts)
                            # Cache value for more performance
                            len_formatted_time = len(formatted_time)
                            if args.color:
                                print(
                                    colorize(
                                        formatted_time,
                                        THEMES[args.theme]["timestamp_key"],
                                    ),
                                    end=" ",
                                )
                            else:
                                print(formatted_time, end=" ")
                    elif len_formatted_time + 1 + levelchars + 1 == terminal_width:
                        print("".join(colored_levelline))
                        colored_levelline = []
                        levelchars = 0
                    colored_levelline.append(colored_levelchar(event))
                    stats = update_stats(stats, event)
                    continue
                if not args.stats_only:
                    for before_line in before_context:
                        before_event = parse(before_line, args.input_format)
                        show(before_event, "before")
                        stats = update_stats(stats, before_event)
                    before_context.clear()
                    after_context_num = args.after_context
                    if args.fuse:
                        show(event, "fuse_first")
                        fuse_skipped = 0
                    elif args.context or args.before_context or args.after_context:
                        show(event, "match")
                    else:
                        show(event)
                stats = update_stats(stats, event)
            elif after_context_num > 0:
                if show_context and skipped > 0 and args.output_format == "default":
                    show_skipped_marker(skipped)
                skipped = 0
                after_context_num -= 1
                show(event, "after")
                stats = update_stats(stats, event)
            else:
                if len(before_context) == args.before_context:
                    skipped += 1
                before_context.append(line)
        skipped += len(before_context)
        if show_context and skipped > 0 and args.output_format == "default":
            show_skipped_marker(skipped)
        if colored_levelline:
            print("".join(colored_levelline))
        if fuse_maybe_last:
            show(fuse_maybe_last, "fuse_last", lineno=1 + fuse_skipped + 1)

    except FileNotFoundError as exc:
        print(exc)
        sys.exit(1)
    except IOError as exc:
        # Ignore broken pipe errors (e.g. caused by piping our output to head)
        if exc.errno == errno.EPIPE:
            pass
        else:
            raise
    except StoppedEarly:
        pass
    except KeyboardInterrupt:
        interrupted = True
        sys.stdout.flush()

    # Print stats even after CTRL-C or early stop
    if args.stats or args.stats_only:
        # New line only after ^C
        if interrupted:
            print_err()
        if args.output_format == "jsonl":
            show_stats_json(stats)
        else:
            show_stats(stats)


if __name__ == "__main__":
    main()
