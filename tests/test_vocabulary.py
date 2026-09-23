#!/usr/bin/env python3
"""Live vocabulary check: push synthetic edge cases through the real questions.

Requires a working config and API key; skips cleanly when either is absent, so
`python3 -m unittest discover -s tests` passes for contributors without
credentials. It does cost a fraction of a cent (~$0.0007 for 9 cases).

    python3 -m unittest tests.test_vocabulary -v

Each case goes through the same `build_questions` / `plan_labels` path the tool
uses, so this exercises the gates, the review threshold and the sentinel too -
not just the filing Choice.
"""

from __future__ import annotations

import logging
import os
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import mail_classify as mc

logging.disable(logging.CRITICAL)

CONFIG = os.environ.get("MAIL_CLASSIFY_CONFIG", mc.DEFAULT_CONFIG)

# The cases that matter: same sender, very different subjects. A filing rubric
# that files by sender will collapse all of these into one bucket.
CASES = [
    (
        "github-pat-expiry",
        "GitHub <noreply@github.com>",
        "[GitHub] Your fine-grained personal access token is about to expire",
        "Your token fuzo-komodo will expire in 7 days. Generate a new token.",
    ),
    (
        "github-billing",
        "GitHub <noreply@github.com>",
        "Your GitHub receipt for October 2026",
        "Thanks for your payment of $21.00 USD for GitHub Team. Your next billing "
        "date is 1 November 2026. This is a receipt for your records.",
    ),
    (
        "github-payment-failed",
        "GitHub <noreply@github.com>",
        "[GitHub] Payment failed for your account",
        "We were unable to process your payment. Please update your billing "
        "information within 7 days to avoid service interruption.",
    ),
    (
        "github-actions-quota",
        "GitHub <noreply@github.com>",
        "[GitHub] You have used 90% of your included Actions minutes",
        "Your organisation has used 90% of included Actions minutes. Additional "
        "usage will be billed per minute at the end of the cycle.",
    ),
    (
        "github-repo-activity",
        "GitHub <notifications@github.com>",
        "[acme/widgets] Run failed: .github/workflows/ci",
        "The workflow run failed on branch main at commit a1b2c3d.",
    ),
    (
        "aws-invoice",
        "AWS <aws-billing@amazon.com>",
        "Your AWS Invoice for September 2026 is available",
        "Your invoice for account 1234 is now available. Total amount due $482.19.",
    ),
    (
        "stripe-receipt",
        "Stripe <receipts@stripe.com>",
        "Your receipt from Acme Software, Inc. #2417-8821",
        "Receipt for $49.00 paid on 12 September 2026. Visa ending 4242.",
    ),
    # Both are security events on the user's account, from different systems.
    # Neither may pick up a security-alert filing label: Security is a gate, so a
    # second label answering "is this a security event?" will always end up
    # disagreeing with it. Each files by the system it concerns instead.
    (
        "google-security-alert",
        "Google <no-reply@accounts.google.com>",
        "Security alert",
        "New sign-in on Linux. We noticed a new sign-in to your Google Account "
        "on a Linux device. If this was you, you don't need to do anything.",
    ),
    (
        "github-oauth-app",
        "GitHub <noreply@github.com>",
        "[GitHub] A third-party OAuth application has been added to your account",
        "A third-party OAuth application was recently added to your account. "
        "If you did not authorize this, revoke its access in settings.",
    ),
]

# case name -> expectations that must hold
EXPECT = {
    # a security event is marked by the gate, not by a filing label
    "google-security-alert": {"not_filing": {"Alerts"}, "gates": {"Security"}},
    "github-oauth-app": {"not_filing": {"Alerts"}, "gates": {"Security"}},
    "github-pat-expiry": {"not_filing": {"Alerts"}},
    # a code host's billing is Finance, not Dev
    "github-billing": {"filing": {"Finance"}},
    "github-payment-failed": {"filing": {"Finance"}},
}


class TestVocabulary(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.cfg = mc.load_config(CONFIG)
            mc.validate_config(cls.cfg)
        except mc.ConfigError as e:
            raise unittest.SkipTest(f"no usable config at {CONFIG}: {e}") from e
        try:
            key = mc.resolve_secret(cls.cfg)
        except mc.ConfigError as e:
            raise unittest.SkipTest(f"no API key available: {e}") from e
        try:
            from typesafe_sdk import TypeSafeClient
        except ImportError as e:  # pragma: no cover - declared in the PEP 723 header
            raise unittest.SkipTest("typesafe-sdk not installed; run under `uv run`") from e

        cls.questions = mc.build_questions(cls.cfg)
        cls.client = TypeSafeClient(api_key=key, base_url=cls.cfg["model"]["base_url"])
        cls.pfx = cls.cfg["classify"].get("priority_prefix", "")
        cls.sentinel = cls.cfg["classify"]["sentinel_label"]
        cls.allowed = set(cls.cfg["filing"]) | {f"{cls.pfx}{p}" for p in cls.cfg["priority"]}

    def test_edge_cases(self):
        for name, sender, subject, body in CASES:
            with self.subTest(case=name):
                state = {
                    "from": sender,
                    "to": "me@example.com",
                    "subject": subject,
                    "date": "",
                    "body": body,
                }
                res = self.client.system_one(
                    model=self.cfg["model"]["name"], state=state, questions=self.questions
                )
                labels, info = mc.plan_labels(res.answers, self.cfg, self.allowed)

                print(
                    f"\n  {name:22s} -> {info['filing']:10s} "
                    f"{info['priority']:5s} f={info['filing_conf']:.2f} "
                    f"{('+' + '+'.join(info['gates'])) if info['gates'] else ''}"
                )

                # Invariants that must hold whatever the rubrics say.
                self.assertIn(
                    info["filing"],
                    self.allowed | {self.cfg["classify"]["review_label"]},
                    f"{name}: invented filing label {info['filing']!r}",
                )
                self.assertIn(
                    info["priority"],
                    self.allowed,
                    f"{name}: invented priority {info['priority']!r}",
                )
                self.assertEqual(
                    info["rejected"], [], f"{name}: model returned labels outside the vocabulary"
                )
                self.assertIn(
                    self.sentinel,
                    labels,
                    f"{name}: sentinel missing, thread would be reprocessed forever",
                )
                self.assertLessEqual(
                    len(labels & set(self.cfg["filing"])), 1, f"{name}: more than one filing label"
                )

                exp = EXPECT.get(name, {})
                for bad in exp.get("not_filing", ()):
                    self.assertNotEqual(info["filing"], bad, f"{name}: filing must not be {bad!r}")
                for want in exp.get("filing", ()):
                    self.assertEqual(info["filing"], want, f"{name}: expected filing {want!r}")
                for g in exp.get("gates", ()):
                    self.assertIn(g, labels, f"{name}: expected gate {g!r}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
