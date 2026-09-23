#!/usr/bin/env python3
"""Offline tests: no network, no API key, no Gmail.

    uv run --with typesafe-sdk tests/test_offline.py     # or:
    python3 -m unittest discover -s tests -v

The live vocabulary check lives in test_vocabulary.py and needs a real key.
"""

from __future__ import annotations

import logging
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import mail_classify as mc

# The code under test logs warnings for recoverable problems; keep the test
# output readable. Assertions are what prove behaviour, not stderr.
logging.disable(logging.CRITICAL)


# ------------------------------------------------------------------- fixtures


def base_config() -> dict:
    return {
        "secret": {"backend": "env", "var": "TEST_KEY"},
        "model": {"name": "jev-1.13", "base_url": "https://openrouter.ai/api"},
        "classify": {
            "sentinel_label": "a",
            "review_label": "NeedsReview",
            "priority_prefix": "",
            "min_confidence": 0.6,
            "gate_threshold": 0.5,
        },
        "filing": {"Dev": "code", "Finance": "money"},
        "priority": {"P0": "now", "P1": "later"},
        "gates": {"Security": "is it a security event?"},
        "colors": {"Dev": "#16a765", "NeedsAction": "#fce8b3"},
        "questions": {"filing": "f?", "priority": "p?"},
    }


class Ans:
    """Stand-in for a Jev answer object."""

    def __init__(self, **kw):
        self.__dict__.update(kw)


def answers(filing="Dev", fconf=0.95, priority="P1", pconf=0.9, security=0.0):
    return {
        "filing": Ans(choice=filing, confidence=fconf),
        "priority": Ans(choice=priority, confidence=pconf),
        "gate:Security": Ans(noul=security),
    }


# ---------------------------------------------------------------------- tests


class TestDotenv(unittest.TestCase):
    def test_parses_quotes_exports_and_comments(self):
        with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as f:
            f.write("# comment\n")
            f.write("\n")
            f.write("export OPENROUTER_API_KEY=sk-or-plain\n")
            f.write('QUOTED="dq"\n')
            f.write("SINGLE='sq'\n")
            f.write("SPACED =  padded  \n")
            f.write("NOT_A_KEY\n")
            f.write("WITH_EQUALS=a=b\n")
            path = pathlib.Path(f.name)
        try:
            env = mc.parse_dotenv(path)
        finally:
            path.unlink()
        self.assertEqual(env["OPENROUTER_API_KEY"], "sk-or-plain")
        self.assertEqual(env["QUOTED"], "dq")
        self.assertEqual(env["SINGLE"], "sq")
        self.assertEqual(env["SPACED"], "padded")
        self.assertEqual(env["WITH_EQUALS"], "a=b")
        self.assertNotIn("NOT_A_KEY", env)


class TestSecrets(unittest.TestCase):
    def test_env_backend(self):
        cfg = {"secret": {"backend": "env", "var": "MAIL_CLASSIFY_TEST_KEY"}}
        os.environ["MAIL_CLASSIFY_TEST_KEY"] = "from-env"
        try:
            self.assertEqual(mc.resolve_secret(cfg), "from-env")
        finally:
            del os.environ["MAIL_CLASSIFY_TEST_KEY"]

    def test_env_backend_missing_raises(self):
        cfg = {"secret": {"backend": "env", "var": "DEFINITELY_NOT_SET_XYZ"}}
        os.environ.pop("DEFINITELY_NOT_SET_XYZ", None)
        with self.assertRaises(mc.ConfigError):
            mc.resolve_secret(cfg)

    def test_dotenv_backend(self):
        with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as f:
            f.write("TEST_KEY=from-dotenv\n")
            name = f.name
        try:
            cfg = {"secret": {"backend": "dotenv", "path": name, "var": "TEST_KEY"}}
            self.assertEqual(mc.resolve_secret(cfg), "from-dotenv")
        finally:
            pathlib.Path(name).unlink()

    def test_command_backend(self):
        cfg = {"secret": {"backend": "command", "command": ["printf", "from-cmd"]}}
        self.assertEqual(mc.resolve_secret(cfg), "from-cmd")

    def test_command_backend_failure_raises(self):
        cfg = {"secret": {"backend": "command", "command": ["false"]}}
        with self.assertRaises(mc.ConfigError):
            mc.resolve_secret(cfg)

    def test_unknown_backend_raises(self):
        with self.assertRaises(mc.ConfigError):
            mc.resolve_secret({"secret": {"backend": "keyring"}})

    def test_env_override_beats_config(self):
        cfg = {"secret": {"backend": "env", "var": "MAIL_CLASSIFY_TEST_KEY"}}
        os.environ["MAIL_CLASSIFY_TEST_KEY"] = "x"
        os.environ["MAIL_CLASSIFY_SECRET_BACKEND"] = "command"  # pragma: allowlist secret
        os.environ["PATH"] = os.environ.get("PATH", "")
        cfg["secret"]["command"] = ["printf", "override"]
        try:
            self.assertEqual(mc.resolve_secret(cfg), "override")
        finally:
            del os.environ["MAIL_CLASSIFY_TEST_KEY"]
            del os.environ["MAIL_CLASSIFY_SECRET_BACKEND"]

    def test_secret_source_never_leaks_value(self):
        cfg = {"secret": {"backend": "env", "var": "MY_SUPER_SECRET"}}
        # Deliberately fake, and the point of the test is that it is not printed.
        os.environ["MY_SUPER_SECRET"] = "sk-or-do-not-print"  # pragma: allowlist secret
        try:
            self.assertNotIn("sk-or-do-not-print", mc.secret_source(cfg))
        finally:
            del os.environ["MY_SUPER_SECRET"]


class TestColors(unittest.TestCase):
    def test_light_background_gets_black_text(self):
        c = mc.resolved_colors({"colors": {"X": "#ffffff"}})["X"]
        self.assertEqual(c["textColor"], "#000000")

    def test_dark_background_gets_white_text(self):
        c = mc.resolved_colors({"colors": {"X": "#0b4f30"}})["X"]
        self.assertEqual(c["textColor"], "#ffffff")

    def test_explicit_pair_is_respected(self):
        c = mc.resolved_colors({"colors": {"X": {"bg": "#ffffff", "fg": "#434343"}}})["X"]
        self.assertEqual(c["textColor"], "#434343")

    def test_hex_is_lowercased(self):
        c = mc.resolved_colors({"colors": {"X": "#FFFFFF"}})["X"]
        self.assertEqual(c["backgroundColor"], "#ffffff")

    def test_invalid_hex_rejected(self):
        for bad in ("white", "#fff", "16a765", "#gggggg"):
            with self.assertRaises(mc.ConfigError):
                mc.resolved_colors({"colors": {"X": bad}})

    def test_no_colors_section_is_fine(self):
        self.assertEqual(mc.resolved_colors({}), {})


class TestCleanBody(unittest.TestCase):
    def _msg(self, body: str):
        raw = (
            "From: a@b.c\r\nTo: d@e.f\r\nSubject: s\r\n"
            "Content-Type: text/plain; charset=utf-8\r\n\r\n" + body
        )
        return mc.email.message_from_string(raw, policy=mc.policy.default)

    def test_strips_quoted_reply_on_wrote(self):
        body = (
            "Thanks, will do.\r\n\r\n"
            "On Mon, 1 Sep 2026 at 10:00, Bob <bob@x.com> wrote:\r\n"
            "> original text\r\n"
        )
        self.assertEqual(mc.clean_body(self._msg(body), 500), "Thanks, will do.")

    def test_strips_original_message_separator(self):
        body = "my reply\n\n-----Original Message-----\nold stuff\n"
        self.assertEqual(mc.clean_body(self._msg(body), 500), "my reply")

    def test_drops_quote_lines_but_keeps_the_rest(self):
        body = "line one\n> quoted\nline two\n"
        self.assertEqual(mc.clean_body(self._msg(body), 500), "line one line two")

    def test_collapses_whitespace(self):
        self.assertEqual(mc.clean_body(self._msg("a\n\n\n  b  \t c"), 500), "a b c")

    def test_truncates_to_limit(self):
        self.assertEqual(len(mc.clean_body(self._msg("x" * 500), 40)), 40)

    def test_strips_html(self):
        raw = (
            "From: a@b.c\r\nSubject: s\r\n"
            "Content-Type: text/html; charset=utf-8\r\n\r\n"
            "<html><style>p{color:red}</style><body><p>Hello</p><p>World</p>"
            "<script>evil()</script></body></html>"
        )
        msg = mc.email.message_from_string(raw, policy=mc.policy.default)
        text = mc.clean_body(msg, 500)
        self.assertIn("Hello", text)
        self.assertIn("World", text)
        self.assertNotIn("evil", text)
        self.assertNotIn("color:red", text)

    def test_empty_body_is_empty_string(self):
        self.assertEqual(mc.clean_body(self._msg(""), 500), "")


class TestParseMessage(unittest.TestCase):
    def test_returns_only_the_fields_questions_need(self):
        raw = (
            "Received: by 1.2.3.4 with SMTP id abc\r\n"
            "ARC-Seal: i=1; a=rsa-sha256; b=verylongbase64\r\n"
            "DKIM-Signature: v=1; b=morebase64\r\n"
            "From: GitHub <noreply@github.com>\r\n"
            "To: me@example.com\r\n"
            "Subject: A thing happened\r\n"
            "Date: Tue, 22 Sep 2026 16:12:15 -0700\r\n\r\n"
            "Body text here.\r\n"
        )
        s = mc.parse_message(raw, 500)
        self.assertEqual(s["from"], "GitHub <noreply@github.com>")
        self.assertEqual(s["subject"], "A thing happened")
        self.assertEqual(s["body"], "Body text here.")
        # the noisy headers must not survive into the state
        self.assertEqual(set(s), {"from", "to", "subject", "date", "body"})
        self.assertNotIn("ARC-Seal", str(s))


class TestThreadTargets(unittest.TestCase):
    def test_groups_by_thread_keeping_newest(self):
        items = [("m1", "t1"), ("m2", "t1"), ("m3", "t2"), ("m4", "t1")]
        self.assertEqual(mc.thread_targets(items), {"t1": "m1", "t2": "m3"})

    def test_empty(self):
        self.assertEqual(mc.thread_targets([]), {})

    def test_distinct_threads_all_kept(self):
        items = [("m1", "t1"), ("m2", "t2"), ("m3", "t3")]
        self.assertEqual(len(mc.thread_targets(items)), 3)


class TestValidateConfig(unittest.TestCase):
    def test_base_config_is_valid(self):
        mc.validate_config(base_config())

    def test_missing_model_key(self):
        cfg = base_config()
        del cfg["model"]["base_url"]
        with self.assertRaises(mc.ConfigError):
            mc.validate_config(cfg)

    def test_empty_filing(self):
        cfg = base_config()
        cfg["filing"] = {}
        with self.assertRaises(mc.ConfigError):
            mc.validate_config(cfg)

    def test_empty_priority(self):
        cfg = base_config()
        cfg["priority"] = {}
        with self.assertRaises(mc.ConfigError):
            mc.validate_config(cfg)

    def test_label_declared_twice(self):
        cfg = base_config()
        cfg["filing"]["P0"] = "clashes with priority"
        with self.assertRaises(mc.ConfigError):
            mc.validate_config(cfg)

    def test_gate_clashing_with_filing(self):
        cfg = base_config()
        cfg["gates"]["Dev"] = "clashes with filing"
        with self.assertRaises(mc.ConfigError):
            mc.validate_config(cfg)


class TestPriorityNames(unittest.TestCase):
    def test_bare_names(self):
        self.assertEqual(mc.priority_names(base_config()), {"P0", "P1"})

    def test_prefixed_names(self):
        cfg = base_config()
        cfg["classify"]["priority_prefix"] = "Priority/"
        self.assertEqual(mc.priority_names(cfg), {"Priority/P0", "Priority/P1"})


class TestBuildQuestions(unittest.TestCase):
    def test_shape(self):
        q = mc.build_questions(base_config())
        self.assertEqual(q["filing"]["type"], "choice")
        self.assertEqual(q["filing"]["criteria"], {"Dev": "code", "Finance": "money"})
        self.assertEqual(q["priority"]["type"], "choice")
        self.assertEqual(q["gate:Security"]["type"], "noul")
        self.assertIn("instructions", q["gate:Security"])

    def test_no_gates_section(self):
        cfg = base_config()
        del cfg["gates"]
        self.assertEqual(set(mc.build_questions(cfg)), {"filing", "priority"})


class TestPlanLabels(unittest.TestCase):
    def setUp(self):
        self.cfg = base_config()
        self.allowed = {"Dev", "Finance", "P0", "P1"}

    def plan(self, **kw):
        return mc.plan_labels(answers(**kw), self.cfg, self.allowed)

    def test_confident_filing_is_not_reviewed(self):
        labels, info = self.plan(fconf=0.95)
        self.assertEqual(labels, {"Dev", "P1", "a"})
        self.assertEqual(info["filing"], "Dev")

    def test_low_filing_confidence_goes_to_review(self):
        labels, _ = self.plan(fconf=0.59)
        self.assertIn("NeedsReview", labels)
        self.assertIn("Dev", labels)  # still filed, just also flagged

    def test_low_priority_confidence_does_not_trigger_review(self):
        # the deliberate asymmetry: review is for filing only
        labels, _ = self.plan(fconf=0.95, pconf=0.05)
        self.assertNotIn("NeedsReview", labels)

    def test_gate_fires_above_threshold(self):
        labels, info = self.plan(security=0.9)
        self.assertIn("Security", labels)
        self.assertEqual(info["gates"], ["Security"])

    def test_gate_stays_quiet_below_threshold(self):
        labels, _ = self.plan(security=0.4)
        self.assertNotIn("Security", labels)

    def test_gate_boundary_is_inclusive(self):
        labels, _ = self.plan(security=0.5)
        self.assertIn("Security", labels)

    def test_invented_filing_label_is_rejected(self):
        labels, info = self.plan(filing="Marketing")
        self.assertEqual(labels, {"NeedsReview", "a"})
        self.assertEqual(info["rejected"], ["Marketing"])
        # and crucially, it is never silently added
        self.assertNotIn("Marketing", labels)

    def test_invented_priority_label_is_rejected(self):
        labels, info = self.plan(priority="P9")
        self.assertEqual(labels, {"NeedsReview", "a"})
        self.assertEqual(info["rejected"], ["P9"])

    def test_sentinel_always_present(self):
        for kw in ({}, {"fconf": 0.1}, {"filing": "Nope"}, {"security": 1.0}):
            labels, _ = self.plan(**kw)
            self.assertIn("a", labels)

    def test_exactly_one_filing_and_one_priority(self):
        labels, _ = self.plan(security=0.9)
        self.assertEqual(len(labels & {"Dev", "Finance"}), 1)
        self.assertEqual(len(labels & {"P0", "P1"}), 1)

    def test_prefixed_priority_is_used(self):
        cfg = base_config()
        cfg["classify"]["priority_prefix"] = "Priority/"
        labels, info = mc.plan_labels(answers(), cfg, {"Dev", "Finance", "Priority/P1"})
        self.assertIn("Priority/P1", labels)
        self.assertEqual(info["priority"], "Priority/P1")

    def test_missing_gate_key_does_not_crash(self):
        cfg = base_config()
        cfg["gates"]["Extra"] = "another gate"
        labels, _ = mc.plan_labels(
            {
                "filing": Ans(choice="Dev", confidence=0.9),
                "priority": Ans(choice="P1", confidence=0.9),
            },
            cfg,
            self.allowed,
        )
        self.assertNotIn("Extra", labels)


class TestCli(unittest.TestCase):
    def test_version(self):
        r = subprocess.run(
            [sys.executable, str(pathlib.Path(mc.__file__)), "--version"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(r.returncode, 0)
        self.assertIn(mc.__version__, r.stdout)

    def test_help_mentions_key_flags(self):
        r = subprocess.run(
            [sys.executable, str(pathlib.Path(mc.__file__)), "--help"],
            capture_output=True,
            text=True,
        )
        for flag in ("--apply", "--dry-run", "--check", "--limit", "--log-level"):
            self.assertIn(flag, r.stdout)

    def test_missing_config_exits_2(self):
        r = subprocess.run(
            [
                sys.executable,
                str(pathlib.Path(mc.__file__)),
                "-c",
                "/tmp/definitely-not-here-9137.toml",
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(r.returncode, mc.EXIT_CONFIG)

    def test_mutually_exclusive_flags(self):
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as f:
            f.write(
                "[model]\nname='m'\nbase_url='u'\n[classify]\nsentinel_label='a'\n"
                "[filing]\nDev='d'\n[priority]\nP0='p'\n[questions]\nfiling='f'\npriority='q'\n"
            )
            name = f.name
        try:
            r = subprocess.run(
                [
                    sys.executable,
                    str(pathlib.Path(mc.__file__)),
                    "-c",
                    name,
                    "--dry-run",
                    "--apply",
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(r.returncode, mc.EXIT_CONFIG)
        finally:
            pathlib.Path(name).unlink()


if __name__ == "__main__":
    unittest.main(verbosity=2)
