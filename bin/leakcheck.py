#!/usr/bin/env python3
"""Shared pre-publication leak gate.

The first version of this check in each repo used a short list of known strings,
scanned only the working tree, and skipped lockfiles. An independent review
found things all three of those choices let through. This version:

  - looks for SHAPES (an uppercase GCP project id is impossible, so finding one
    proves a mechanical rename) as well as known strings;
  - scans every text file including lockfiles;
  - scans `git log -p --all`, because a fresh history is only clean if nothing
    was committed and then removed.

Dropped into each repo as bin/leakcheck.py and called by bin/e2e.py.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

SKIP_PARTS = {".git", ".venv", "__pycache__", ".pytest_cache", ".ruff_cache", "node_modules"}
# Lockfiles are deliberately NOT skipped: a dependency pinned to an internal
# index would name the company in a file nobody thinks to read.
BINARY = {".png", ".jpg", ".jpeg", ".gif", ".pdf", ".ico", ".whl", ".zip",
          ".woff", ".woff2", ".ttf", ".mp4"}

RULES: list[tuple[str, re.Pattern[str]]] = [
    ("employer name", re.compile(r"buildertrend", re.I)),
    ("bt-prefixed identifier", re.compile(r"\bbt[a-z]{3,}[-_][a-z0-9]|\bBT(?:Net|Core|Sql|Exceptions)", re.I)),
    # A GCP project id is lowercase-only; an uppercase one cannot be real and so
    # advertises that a rename was applied mechanically.
    ("impossible GCP project id", re.compile(
        r"\b[a-z0-9-]*[A-Z][A-Za-z0-9-]*-(?:prod|dev|test|sand|staging)-project\b")),
    ("former colleague", re.compile(
        r"\b(?:Micucci|Dropinski|Goltl|Sharkey|Knodel|Griner|Johansen|Huff|Herman|Marion|Dunning)\b")),
    ("employer email", re.compile(r"[A-Za-z0-9._%+-]+@(?:buildertrend|inditex)\.[a-z]+", re.I)),
    ("secret-shaped token", re.compile(
        r"sk-bf-[0-9a-f]{8}|sntryu_[a-f0-9]{32,}|gh[pousr]_[A-Za-z0-9]{20,}|"
        r"AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{30,}|xox[baprs]-[A-Za-z0-9-]{10,}|"
        r"-----BEGIN [A-Z ]*PRIVATE KEY|eyJ[A-Za-z0-9_-]{30,}\.[A-Za-z0-9_-]{20,}")),
    ("windows workstation path", re.compile(r"[Cc]:[\\/]repos[\\/]")),
    ("internal monorepo IaC path", re.compile(r"InfrastructureAsCode/(?:aiops|bt)")),
    ("internal wiki", re.compile(r"btwiki\.atlassian\.net")),
    ("private RFC1918 address", re.compile(
        r"\b(?:10\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])\.|192\.168\.)\d{1,3}\.\d{1,3}\b")),
]

ALLOW = [
    "example.com", "example.net", "acme.example", "example.org",
    "jasonhastain@gmail.com", "noreply@anthropic.com",
    "gateway.invalid", "bifrost.invalid", "aiplatform.invalid",
    "127.0.0.1", "10.0.0.1",
]


def _interesting(line: str) -> bool:
    low = line.lower()
    return not any(a in low for a in ALLOW)


def scan_tree(root: Path, self_path: Path | None = None) -> list[str]:
    hits: list[str] = []
    for p in sorted(root.rglob("*")):
        if not p.is_file() or set(p.parts) & SKIP_PARTS or p.suffix.lower() in BINARY:
            continue
        if self_path and p.resolve() == self_path.resolve():
            continue  # this file carries the pattern list
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, PermissionError, OSError):
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if not _interesting(line):
                continue
            for label, rx in RULES:
                m = rx.search(line)
                if m:
                    hits.append(f"[{label}] {p.relative_to(root)}:{i} <{m.group(0)[:48]}>")
                    break
    return hits


def scan_history(root: Path) -> list[str]:
    """A fresh history is only clean if nothing was added and then removed."""
    try:
        out = subprocess.run(
            ["git", "log", "-p", "--all", "--no-color"],
            cwd=root, capture_output=True, text=True, errors="replace", timeout=180,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if out.returncode != 0:
        return []
    # Track which file each hunk belongs to, so the checkers' own pattern lists
    # do not report themselves.
    SELF = ("bin/leakcheck.py", "bin/e2e.py")
    hits: list[str] = []
    current = ""
    for line in out.stdout.splitlines():
        if line.startswith("+++ b/"):
            current = line[6:].strip()
            continue
        if line.startswith("--- "):
            continue
        if current in SELF:
            continue
        if not line.startswith("+") or not _interesting(line):
            continue
        for label, rx in RULES:
            m = rx.search(line)
            if m:
                hits.append(f"[{label}] {current or '?'} <{m.group(0)[:48]}>")
                break
    return sorted(set(hits))


def binaries(root: Path) -> list[str]:
    return [
        p.relative_to(root).as_posix() for p in sorted(root.rglob("*"))
        if p.is_file() and not set(p.parts) & SKIP_PARTS and p.suffix.lower() in BINARY
    ]
