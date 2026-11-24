"""Findings and report model shared by all probe checks."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Optional

SEVERITIES = ("info", "low", "medium", "high")
SEVERITY_RANK = {s: i for i, s in enumerate(SEVERITIES)}


@dataclass
class ProbeFinding:
    check: str
    severity: str                 # info | low | medium | high
    url: str
    title: str
    detail: str = ""
    evidence: Optional[str] = None

    def __post_init__(self) -> None:
        if self.severity not in SEVERITY_RANK:
            raise ValueError(f"unknown severity {self.severity!r}")

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ProbeReport:
    target: str
    findings: list[ProbeFinding] = field(default_factory=list)
    stats: dict = field(default_factory=dict)       # check -> {requests, failures, duration_s}
    errors: list[str] = field(default_factory=list)

    def add(self, check: str, severity: str, url: str, title: str,
            detail: str = "", evidence: Optional[str] = None) -> ProbeFinding:
        f = ProbeFinding(check, severity, url, title, detail, evidence)
        self.findings.append(f)
        return f

    def sorted_findings(self) -> list[ProbeFinding]:
        return sorted(self.findings, key=lambda f: (-SEVERITY_RANK[f.severity], f.check, f.url, f.title))

    @property
    def max_severity(self) -> str:
        return max((f.severity for f in self.findings), key=SEVERITY_RANK.get, default="info")

    @property
    def exit_code(self) -> int:
        """0 = nothing above info, 1 = low+ findings, 3 = error."""
        if self.errors:
            return 3
        if any(SEVERITY_RANK[f.severity] >= SEVERITY_RANK["low"] for f in self.findings):
            return 1
        return 0

    def merge(self, other: "ProbeReport") -> None:
        self.findings.extend(other.findings)
        self.stats.update(other.stats)
        self.errors.extend(other.errors)

    def to_dict(self) -> dict:
        return {
            "target": self.target,
            "max_severity": self.max_severity,
            "exit_code": self.exit_code,
            "findings": [f.to_dict() for f in self.sorted_findings()],
            "stats": self.stats,
            "errors": self.errors,
        }

    def render_text(self) -> str:
        lines = [f"esuls-probe report for {self.target}", "=" * 60]
        if not self.findings:
            lines.append("No findings.")
        for f in self.sorted_findings():
            lines.append(f"[{f.severity.upper():6}] ({f.check}) {f.title}")
            lines.append(f"         {f.url}")
            if f.detail:
                lines.append(f"         {f.detail}")
            if f.evidence:
                lines.append(f"         evidence: {f.evidence}")
        counts = {s: sum(1 for f in self.findings if f.severity == s) for s in reversed(SEVERITIES)}
        lines += ["", "Summary: " + ", ".join(f"{n} {s}" for s, n in counts.items())]
        for check, st in self.stats.items():
            lines.append(f"  {check}: {st.get('requests', 0)} requests, "
                         f"{st.get('failures', 0)} failed, {st.get('duration_s', 0)}s")
        for e in self.errors:
            lines.append(f"ERROR: {e}")
        return "\n".join(lines)
