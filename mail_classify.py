#!/usr/bin/env python3
"""Label and prioritise Gmail threads with Jev (TypeSafe) over OpenRouter.

Reads mail through himalaya's Gmail backend, asks Jev typed questions about each
*thread*, and applies the answers as Gmail labels.

Pipeline:

    worklist   unclassified messages matching a query, grouped into threads
    fetch      himalaya gmail messages get --format raw -> minimal state
    classify   one Jev request per thread (filing Choice, priority Choice, gates)
    group      bucket threads by the exact label set they need
    apply      himalaya gmail threads modify, one call per thread

A thread is the unit of classification, so every message in a conversation ends
up with the same labels.

Dry-run is the default; pass --apply to write anything.

Examples:

    mail-classify                      # dry-run the inbox
    mail-classify --limit 25           # dry-run 25 threads
    mail-classify --apply              # label for real
    mail-classify --query 'label:inbox' --apply
    mail-classify --check              # validate config and connectivity only
"""

from __future__ import annotations

import argparse
import email
import html
import json
import logging
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from email import policy
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 has no tomllib
    import tomli as tomllib  # type: ignore[no-redef]

__version__ = "0.1.0"

PROG = "mail-classify"
DEFAULT_CONFIG = "~/.config/mail-classify/config.toml"

log = logging.getLogger(PROG)

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_CONFIG = 2

LEVELS = {
    "off": logging.CRITICAL + 1,
    "error": logging.ERROR,
    "warning": logging.WARNING,
    "info": logging.INFO,
    "debug": logging.DEBUG,
}

# Anything the model is allowed to return must appear in the config vocabulary.
# Anything it invents is rejected rather than created.


class ConfigError(Exception):
    """Raised for a config that cannot be used as written."""


# ------------------------------------------------------------------ reporting


_QUIET = False
_COLOR = False

# Only the markers are coloured, never the surrounding text: the output has to
# stay readable when piped into a pager or a log file.
_CODES = {
    "ok": "\033[32m",  # green
    "fail": "\033[31m",  # red
    "warn": "\033[33m",  # yellow
    "reset": "\033[0m",
}


def colorize(text: str, kind: str) -> str:
    """Wrap text in an ANSI colour when stdout is a terminal.

    Honours NO_COLOR (https://no-color.org) and stays plain when output is
    redirected, so a piped report never gains escape sequences.
    """
    if not _COLOR or not text:
        return text
    return f"{_CODES[kind]}{text}{_CODES['reset']}"


def out(msg: str = "") -> None:
    """The human-facing report. Goes to stdout so it can be piped."""
    if not _QUIET:
        print(msg)


def setup_logging(level: str, logfile: str | None) -> None:
    handlers: list[logging.Handler] = []
    if logfile:
        path = Path(os.path.expanduser(logfile))
        path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(path))
        fmt = "%(asctime)s %(levelname)-7s %(message)s"
    else:
        handlers.append(logging.StreamHandler(sys.stderr))
        fmt = "%(levelname)-7s %(message)s"
    if level == "debug":
        fmt = "%(asctime)s %(levelname)-7s %(name)s %(message)s"
    logging.basicConfig(level=LEVELS[level], format=fmt, handlers=handlers, force=True)


# --------------------------------------------------------------------- config


def load_config(path: str) -> dict:
    p = Path(os.path.expanduser(path))
    if not p.is_file():
        raise ConfigError(f"config not found: {p}\nCopy config.example.toml there and edit it.")
    try:
        with p.open("rb") as f:
            return tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{p} is not valid TOML: {e}") from e


def validate_config(cfg: dict) -> None:
    """Fail early and specifically, rather than KeyError-ing mid-run."""
    for table, keys in (
        ("model", ("name", "base_url")),
        ("classify", ("sentinel_label",)),
    ):
        for k in keys:
            if k not in cfg.get(table, {}):
                raise ConfigError(f"missing required key: {table}.{k}")

    if not cfg.get("filing"):
        raise ConfigError("[filing] is empty - the model would have nothing to choose from")
    if not cfg.get("priority"):
        raise ConfigError("[priority] is empty")

    # A label may not appear in two families: the apply would add and remove the
    # same label in one call.
    pfx = cfg["classify"].get("priority_prefix", "")
    families = {
        "filing": set(cfg["filing"]),
        "priority": {f"{pfx}{p}" for p in cfg["priority"]},
        "gates": set(cfg.get("gates", {})),
    }
    keys = list(families)
    for i, a in enumerate(keys):
        for b in keys[i + 1 :]:
            overlap = families[a] & families[b]
            if overlap:
                raise ConfigError(f"label(s) {sorted(overlap)} declared in both [{a}] and [{b}]")


def priority_names(cfg: dict) -> set[str]:
    pfx = cfg["classify"].get("priority_prefix", "")
    return {f"{pfx}{p}" for p in cfg["priority"]}


# -------------------------------------------------------------------- secrets


def parse_dotenv(path: Path) -> dict[str, str]:
    """Minimal .env: KEY=value, optional `export`, # comments, quoted values."""
    out_: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:]
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        out_[k.strip()] = v.strip().strip("'\"")
    return out_


def resolve_secret(cfg: dict) -> str:
    """env | dotenv | command. `command` covers secret-tool, pass, op, anything."""
    s = cfg.get("secret", {})
    backend = os.environ.get("MAIL_CLASSIFY_SECRET_BACKEND") or s.get("backend", "env")
    var = s.get("var", "OPENROUTER_API_KEY")
    log.debug("resolving secret %s via backend %r", var, backend)

    if backend == "env":
        val = os.environ.get(var)
    elif backend == "dotenv":
        p = Path(os.path.expanduser(s.get("path", "~/.config/mail-classify/.env")))
        if not p.is_file():
            raise ConfigError(f"secret: dotenv file not found: {p}")
        val = parse_dotenv(p).get(var)
    elif backend == "command":
        cmd = s.get("command")
        if not cmd:
            raise ConfigError('secret: backend = "command" requires a `command`')
        r = subprocess.run(cmd if isinstance(cmd, list) else [cmd], capture_output=True, text=True)
        if r.returncode:
            raise ConfigError(f"secret: command failed: {' '.join(cmd)}\n{r.stderr.strip()}")
        val = r.stdout.strip()
    else:
        raise ConfigError(f"secret: unknown backend {backend!r} (env | dotenv | command)")

    if not val:
        raise ConfigError(f"secret: no value for {var} via backend {backend!r}")
    return val


def secret_source(cfg: dict) -> str:
    """Human description of where the key comes from, for --check. Never the value."""
    s = cfg.get("secret", {})
    backend = os.environ.get("MAIL_CLASSIFY_SECRET_BACKEND") or s.get("backend", "env")
    if backend == "dotenv":
        return f"dotenv {s.get('path', '~/.config/mail-classify/.env')}"
    if backend == "command":
        return f"command {' '.join(s.get('command', []))}"
    return f"env {s.get('var', 'OPENROUTER_API_KEY')}"


# ------------------------------------------------------------------- himalaya


def himalaya(*args: str, check: bool = True) -> str:
    log.debug("himalaya %s", " ".join(args))
    try:
        r = subprocess.run(["himalaya", *args], capture_output=True, text=True)
    except FileNotFoundError as e:
        raise ConfigError(
            "himalaya not found on PATH. Install it and configure the Gmail backend."
        ) from e
    if check and r.returncode:
        raise ConfigError(f"himalaya {' '.join(args)} failed:\n{r.stderr.strip()}")
    return r.stdout


def label_map() -> dict[str, str]:
    out_ = himalaya("gmail", "labels", "list", "--json")
    return {lab["name"]: lab["id"] for lab in json.loads(out_).get("labels", [])}


def worklist(query: str, limit: int) -> list[tuple[str, str]]:
    """(message id, thread id) pairs, newest first."""
    out_ = himalaya("gmail", "messages", "list", "-q", query, "--max-results", str(limit), "--json")
    return [(i["id"], i["threadId"]) for i in json.loads(out_).get("ids", [])]


def thread_targets(items: list[tuple[str, str]]) -> dict[str, str]:
    """Collapse (message id, thread id) pairs to {thread id: newest message id}.

    The newest matching message in a thread is the state we classify the whole
    conversation from, since the API returns messages newest first.
    """
    threads: dict[str, str] = {}
    for mid, tid in items:
        threads.setdefault(tid, mid)
    return threads


# ------------------------------------------------------------------ gmail api


def gmail_token(cfg: dict) -> str | None:
    """Bearer token for the Gmail REST API. Only needed for label colours."""
    spec = cfg.get("gmail", {}).get("token", {}).get("command")
    if not spec:
        return None
    r = subprocess.run(spec, capture_output=True, text=True)
    if r.returncode or not r.stdout.strip():
        log.warning("gmail token command failed, skipping label colours: %s", r.stderr.strip())
        return None
    return r.stdout.strip()


def _luminance(hexcolor: str) -> float:
    r, g, b = (int(hexcolor[i : i + 2], 16) / 255 for i in (1, 3, 5))
    f = lambda c: c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4  # noqa: E731
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)


def resolved_colors(cfg: dict) -> dict[str, dict]:
    """[colors]: `label = "#rrggbb"`, optionally `label = { bg = .., fg = .. }`.

    Gmail requires both textColor and backgroundColor, from a fixed palette.
    Text defaults to white or black by luminance, so light backgrounds stay
    readable without specifying a pair for every label.
    """
    out_: dict[str, dict] = {}
    for label, spec in cfg.get("colors", {}).items():
        if isinstance(spec, str):
            bg, fg = spec, None
        else:
            bg, fg = spec["bg"], spec.get("fg")
        if not re.fullmatch(r"#[0-9a-fA-F]{6}", bg):
            raise ConfigError(f"colors.{label}: {bg!r} is not a #rrggbb hex value")
        out_[label] = {
            "backgroundColor": bg.lower(),
            "textColor": (fg or ("#ffffff" if _luminance(bg) < 0.4 else "#000000")).lower(),
        }
    return out_


def set_label_colors(cfg: dict, ids: dict[str, str]) -> None:
    """himalaya's `gmail labels update` only sets the name, so colours go
    straight to the REST API."""
    colors = resolved_colors(cfg)
    if not colors:
        return
    token = gmail_token(cfg)
    if not token:
        return

    for name, color in sorted(colors.items()):
        if name not in ids:
            log.debug("colour %s: label does not exist, skipping", name)
            continue
        req = urllib.request.Request(
            f"https://gmail.googleapis.com/gmail/v1/users/me/labels/{ids[name]}",
            data=json.dumps({"color": color}).encode(),
            method="PATCH",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        try:
            urllib.request.urlopen(req, timeout=60).read()
            log.info("colour %-18s %s on %s", name, color["backgroundColor"], color["textColor"])
        except urllib.error.HTTPError as e:
            detail = e.read().decode()[:160]
            log.warning("colour %s FAILED: HTTP %s %s", name, e.code, detail)
        except Exception as e:  # network hiccup must not lose the labels
            log.warning("colour %s FAILED: %s", name, e)


# --------------------------------------------------------------------- fetch


QUOTE_HEAD = re.compile(r"^On .{5,80}\bwrote:$", re.I)
QUOTE_SEP = re.compile(r"^-{2,}\s*(original|forwarded) message\s*-{2,}", re.I)


def clean_body(msg, limit: int) -> str:
    """Plain text, quoted replies and HTML boilerplate stripped, then truncated.

    Jev's context is 32k for state plus the longest question, and accuracy falls
    as the state grows with unrelated detail, so reply chains are dropped.
    """
    part = msg.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    try:
        text = part.get_content()
    except Exception:
        return ""
    if part.get_content_subtype() == "html":
        text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", text, flags=re.S | re.I)
        text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    lines: list[str] = []
    for raw in text.splitlines():
        s = raw.strip()
        if s.startswith(">"):
            continue
        if QUOTE_HEAD.match(s) or QUOTE_SEP.match(s):
            break
        lines.append(s)
    return re.sub(r"\s+", " ", " ".join(lines)).strip()[:limit]


def parse_message(raw: str, body_chars: int) -> dict:
    """Only the fields the questions need - not the raw RFC 5322 with its
    Received/ARC/DKIM headers, which are pure distractor tokens."""
    msg = email.message_from_string(raw, policy=policy.default)
    return {
        "from": str(msg.get("From", "")),
        "to": str(msg.get("To", "")),
        "subject": str(msg.get("Subject", "")),
        "date": str(msg.get("Date", "")),
        "body": clean_body(msg, body_chars),
    }


def fetch(mid: str, body_chars: int) -> dict:
    raw = himalaya("gmail", "messages", "get", mid, "--format", "raw")
    return parse_message(raw, body_chars)


# ------------------------------------------------------------------------ jev


def build_questions(cfg: dict) -> dict:
    q = cfg["questions"]
    qs = {
        "filing": {
            "type": "choice",
            "instructions": q["filing"],
            "criteria": cfg["filing"],
        },
        "priority": {
            "type": "choice",
            "instructions": q["priority"],
            "criteria": cfg["priority"],
        },
    }
    # Orthogonal yes/no gates. Each true gate becomes its own label, so a thread
    # can be "from GitHub" AND "a security event" at once, instead of the filing
    # Choice having to arbitrate between two things that aren't exclusive.
    for label, instructions in cfg.get("gates", {}).items():
        qs[f"gate:{label}"] = {"type": "noul", "instructions": instructions}
    return qs


def field(ans, name, default=None):
    """Answers come back as objects; tolerate dicts too."""
    if isinstance(ans, dict):
        return ans.get(name, default)
    return getattr(ans, name, default)


def answer(ans, key):
    """Fetch one answer by question name, tolerating a missing question rather
    than crashing the whole run over one absent gate."""
    try:
        return ans[key]
    except (KeyError, TypeError, AttributeError):
        log.warning("no answer returned for question %r", key)
        return None


def plan_labels(ans, cfg: dict, allowed: set[str]) -> tuple[set[str], dict]:
    """Turn one thread's answers into the label set it should carry.

    Returns (labels, summary). Any label the model invented is rejected and the
    thread goes to review - the tool never creates a label from model output.
    """
    cl = cfg["classify"]
    review = cl["review_label"]
    sentinel = cl["sentinel_label"]
    gates = cfg.get("gates", {})
    gate_min = float(cl.get("gate_threshold", 0.5))
    min_conf = float(cl.get("min_confidence", 0.6))
    pfx = cl.get("priority_prefix", "")

    filing_ans = answer(ans, "filing")
    prio_ans = answer(ans, "priority")
    filing = field(filing_ans, "choice")
    priority = pfx + str(field(prio_ans, "choice"))
    filing_conf = field(filing_ans, "confidence", 0.0) or 0.0
    prio_conf = field(prio_ans, "confidence", 0.0) or 0.0

    rejected = [v for v in (filing, priority) if v not in allowed]
    if rejected:
        labels = {review, sentinel}
        filing = review
    else:
        labels = {filing, priority, sentinel}
        # Only filing gates review. A misprioritised thread is cheap to notice
        # and fix; a misfiled one is never found again, and urgency judgements
        # are inherently fuzzier than categorical ones.
        if filing_conf < min_conf:
            labels.add(review)

    fired = []
    for g in gates:
        val = field(answer(ans, f"gate:{g}"), "noul", 0.0) or 0.0
        if val >= gate_min:
            labels.add(g)
            fired.append(g)

    return labels, {
        "filing": filing,
        "priority": priority,
        "filing_conf": filing_conf,
        "prio_conf": prio_conf,
        "gates": fired,
        "rejected": rejected,
    }


# ----------------------------------------------------------------------- run


def run(cfg: dict, args: argparse.Namespace) -> int:
    cl = cfg["classify"]
    model = args.model or cfg["model"]["name"]
    base_url = cfg["model"]["base_url"]

    sentinel = cl["sentinel_label"]
    review = cl["review_label"]
    gates = cfg.get("gates", {})
    filing_names = set(cfg["filing"])
    prio_names = priority_names(cfg)
    required = filing_names | prio_names | set(gates) | {sentinel, review}
    # The model may only ever pick from these; anything else is rejected.
    allowed = filing_names | prio_names

    query = args.query or f"in:inbox -label:{sentinel}"

    have = label_map()
    if args.apply:
        for name in sorted(required - set(have)):
            out(f"creating label {name!r}")
            himalaya("gmail", "labels", "create", name)
        have = label_map()
        set_label_colors(cfg, have)
    else:
        missing = sorted(required - set(have))
        if missing:
            out(f"[dry-run] would create {len(missing)} labels: {', '.join(missing)}")

    items = worklist(query, args.limit)
    # Classify the THREAD, not the message. A conversation is the unit of
    # meaning: per-message classification gives near-identical siblings different
    # labels, and Gmail shows the union on the thread row anyway.
    threads = thread_targets(items)

    tag = "" if args.apply else " [DRY RUN]"
    out(f"{len(threads)} thread(s) / {len(items)} message(s) via {query!r} -> {model}{tag}\n")
    if not threads:
        return EXIT_OK

    try:
        from typesafe_sdk import TypeSafeClient
    except ImportError as e:  # pragma: no cover - dependency is declared inline
        raise ConfigError(
            "typesafe-sdk is not installed. Run this script with `uv run`, "
            "or `pip install typesafe-sdk`."
        ) from e

    questions = build_questions(cfg)
    client = TypeSafeClient(api_key=resolve_secret(cfg), base_url=base_url)

    buckets: dict[frozenset, list[str]] = defaultdict(list)
    total_cost = 0.0
    failures = 0
    started = time.monotonic()

    for i, (tid, mid) in enumerate(threads.items(), 1):
        try:
            state = fetch(mid, int(cl.get("body_chars", 2000)))
            result = client.system_one(model=model, state=state, questions=questions)
        except Exception as e:  # one bad thread must not kill the run
            failures += 1
            log.warning("[%d/%d] %s FAILED: %s", i, len(threads), tid, e)
            continue

        try:
            total_cost += result.raw_http_response.json().get("usage", {}).get("cost") or 0.0
        except Exception:
            pass

        labels, info = plan_labels(result.answers, cfg, allowed)
        buckets[frozenset(labels)].append(tid)

        if info["rejected"]:
            log.warning(
                "[%d/%d] %s rejected unknown label(s): %s", i, len(threads), tid, info["rejected"]
            )
        if review in labels:
            log.debug("[%d/%d] %s -> review", i, len(threads), tid)

        gate_s = ("+" + "+".join(info["gates"])) if info["gates"] else ""
        flag = colorize(" REVIEW", "warn") if review in labels else ""
        out(
            f"  [{i}/{len(threads)}] {state['subject'][:48]:48s} "
            f"-> {info['filing']:10s} {info['priority']:5s} "
            f"f={info['filing_conf']:.2f} p={info['prio_conf']:.2f}{gate_s}{flag}"
        )

    elapsed = time.monotonic() - started
    log.debug("%d model calls in %.1fs", len(threads) - failures, elapsed)

    fails = colorize(f"{failures} failed", "fail") if failures else "0 failed"
    out(
        f"\n{len(threads) - failures} classified, {len(buckets)} distinct label set(s), "
        f"{fails}, ${total_cost:.4f}\n"
    )
    for target_set, members in buckets.items():
        out(f"  {len(members):4d}  {', '.join(sorted(target_set))}")

    if not args.apply:
        out("\n[dry-run] nothing written. Re-run with --apply.")
        return EXIT_ERROR if failures else EXIT_OK

    # threads modify applies to every message in the thread, so a conversation
    # ends up with one label set. It is additive, so removing every managed label
    # the thread should NOT have keeps this an idempotent set rather than an
    # accumulating union.
    managed = filing_names | prio_names | set(gates) | {sentinel, review}
    for target_set, members in buckets.items():
        add = [f"--add-label={have[n]}" for n in sorted(target_set)]
        drop = [f"--remove-label={have[n]}" for n in sorted(managed - target_set) if n in have]
        for tid in members:
            himalaya("gmail", "threads", "modify", tid, *add, *drop)

    applied = sum(len(m) for m in buckets.values())
    out(f"\napplied labels to {applied} thread(s).")
    if failures:
        log.error(
            "%d thread(s) failed and were left unmarked; the next run will retry them", failures
        )
        return EXIT_ERROR
    return EXIT_OK


def check(cfg: dict, args: argparse.Namespace) -> int:
    """Validate config, credentials and connectivity without calling the model."""
    problems = 0

    def report(ok: bool, label: str, detail: str = "") -> None:
        nonlocal problems
        mark = colorize("OK" if ok else "FAIL", "ok" if ok else "fail")
        if not ok:
            problems += 1
        out(f"  [{mark}] {label}{(' - ' + detail) if detail else ''}")

    out(f"{PROG} {__version__}\n")

    cl = cfg["classify"]
    filing = set(cfg["filing"])
    prio = priority_names(cfg)
    gates = set(cfg.get("gates", {}))
    out(f"model        {cfg['model']['name']} via {cfg['model']['base_url']}")
    out(f"vocabulary   {len(filing)} filing, {len(prio)} priority, {len(gates)} gate")
    out(f"sentinel     {cl['sentinel_label']!r}   review {cl['review_label']!r}")
    out(f"thresholds   min_confidence={cl.get('min_confidence')} gate={cl.get('gate_threshold')}\n")

    report(True, "secret backend", secret_source(cfg))
    try:
        resolve_secret(cfg)
        report(True, "secret resolves")
    except ConfigError as e:
        report(False, "secret resolves", str(e).splitlines()[0])

    try:
        have = label_map()
        report(True, "himalaya gmail backend", f"{len(have)} labels visible")
        required = filing | prio | gates | {cl["sentinel_label"], cl["review_label"]}
        missing = sorted(required - set(have))
        report(
            not missing, "vocabulary labels exist", "will be created on --apply" if missing else ""
        )
    except ConfigError as e:
        report(False, "himalaya gmail backend", str(e).splitlines()[0])
        return EXIT_CONFIG

    if cfg.get("gmail", {}).get("token", {}).get("command"):
        report(gmail_token(cfg) is not None, "gmail api token (for label colours)")
    else:
        report(True, "gmail api token", "not configured, colours will be skipped")

    q = args.query or f"in:inbox -label:{cl['sentinel_label']}"
    try:
        n = len(worklist(q, args.limit))
        report(True, "worklist query", f"{n} message(s) match {q!r}")
    except ConfigError as e:
        report(False, "worklist query", str(e).splitlines()[0])

    out("")
    if problems:
        out(f"{problems} problem(s) found.")
        return EXIT_CONFIG
    out("All checks passed.")
    return EXIT_OK


# ----------------------------------------------------------------------- cli


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog=PROG,
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("-V", "--version", action="version", version=f"{PROG} {__version__}")
    ap.add_argument(
        "-c",
        "--config",
        default=DEFAULT_CONFIG,
        metavar="PATH",
        help="config file (default: %(default)s)",
    )
    ap.add_argument(
        "-n",
        "--limit",
        type=int,
        default=200,
        metavar="N",
        help="max messages to consider this run (default: %(default)s)",
    )
    ap.add_argument(
        "-q",
        "--query",
        metavar="QUERY",
        help="override the worklist Gmail query (default: 'in:inbox -label:<sentinel>')",
    )
    ap.add_argument("--model", help="override model.name from the config")
    ap.add_argument(
        "--check",
        action="store_true",
        help="validate config, credentials and connectivity, then exit",
    )
    ap.add_argument("--apply", action="store_true", help="write labels (default is a dry run)")
    ap.add_argument(
        "--dry-run", action="store_true", help="explicitly do not write anything (the default)"
    )
    ap.add_argument(
        "--quiet",
        action="store_true",
        help="suppress the report on stdout; errors still go to stderr",
    )
    ap.add_argument(
        "--log-level",
        default="info",
        choices=sorted(LEVELS),
        metavar="LEVEL",
        help=f"one of {', '.join(sorted(LEVELS))} (default: %(default)s)",
    )
    ap.add_argument("--log-file", metavar="PATH", help="append logs to PATH instead of stderr")
    return ap


def main(argv: list[str] | None = None) -> int:
    global _QUIET, _COLOR
    args = build_parser().parse_args(argv)
    _QUIET = args.quiet
    # Colour only when a human is watching. NO_COLOR (any value) forces it off.
    _COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None
    setup_logging(args.log_level, args.log_file)

    if args.dry_run and args.apply:
        log.error("--dry-run and --apply are mutually exclusive")
        return EXIT_CONFIG

    try:
        cfg = load_config(args.config)
        validate_config(cfg)
    except ConfigError as e:
        log.error("%s", e)
        return EXIT_CONFIG

    if args.check:
        return check(cfg, args)

    try:
        return run(cfg, args)
    except ConfigError as e:
        log.error("%s", e)
        return EXIT_CONFIG
    except KeyboardInterrupt:
        log.warning("interrupted")
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
