"""algops.notify — deterministic completion notifications (Slack / email / console).

The "experiment finished" / "benchmark finished" alerts must NOT route through the LLM (it might
forget or hallucinate). They are fired deterministically, deduped per experiment attempt /
benchmark, and degrade to a console log when no channel is configured.
"""

from __future__ import annotations

import smtplib
from abc import ABC, abstractmethod
from email.mime.text import MIMEText
from typing import Any

from algops.core import GoalDirection, MasterSettings, MetricName, Objective, normalize_metrics

# In-process dedup guard (the durable guard is FinalResult.notified in the tracking DB).
_SENT: set[str] = set()


class Notifier(ABC):
    name = "base"

    @abstractmethod
    def send(self, subject: str, body: str) -> str:
        """Send and return an opaque reference (message ts / id / 'console')."""


class ConsoleNotifier(Notifier):
    name = "console"

    def send(self, subject: str, body: str) -> str:
        print(f"\n[ALGOps 알림] {subject}\n{body}\n", flush=True)
        return "console"


class SlackNotifier(Notifier):
    name = "slack"

    def __init__(self, token: str, channel: str) -> None:
        self.token = token
        self.channel = channel

    def send(self, subject: str, body: str) -> str:
        from slack_sdk import WebClient
        from slack_sdk.errors import SlackApiError

        client = WebClient(token=self.token)
        try:
            resp = client.chat_postMessage(channel=self.channel, text=f"*{subject}*\n{body}")
            return str(resp.get("ts", "sent"))
        except SlackApiError as exc:  # honor 429 Retry-After once
            retry_after = exc.response.headers.get("Retry-After") if exc.response else None
            return f"slack_error:{exc.response.get('error') if exc.response else exc}:retry_after={retry_after}"


class EmailNotifier(Notifier):
    name = "email"

    def __init__(self, settings: MasterSettings) -> None:
        self.s = settings

    def send(self, subject: str, body: str) -> str:
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"] = self.s.smtp_user or "algops@localhost"
        msg["To"] = self.s.notify_email_to or ""
        with smtplib.SMTP(self.s.smtp_host, self.s.smtp_port) as server:
            server.starttls()
            if self.s.smtp_user:
                server.login(self.s.smtp_user, self.s.smtp_password or "")
            server.send_message(msg)
        return f"email:{self.s.notify_email_to}"


class McpNotifier(Notifier):  # pragma: no cover - requires a configured MCP bridge endpoint
    """Optional, additive: post an agent-authored summary via an MCP server (Slack/Resend MCP).

    Layered on top of the deterministic notifiers — never the sole 'finished' alert path.
    """

    name = "mcp"

    def __init__(self, server_url: str) -> None:
        self.server_url = server_url

    def send(self, subject: str, body: str) -> str:
        import httpx

        try:
            httpx.post(self.server_url, json={"subject": subject, "text": body}, timeout=10)
            return "mcp:sent"
        except Exception as exc:  # noqa: BLE001
            return f"mcp_error:{exc}"


def build_notifiers(settings: MasterSettings) -> list[Notifier]:
    notifiers: list[Notifier] = [ConsoleNotifier()]
    if settings.slack_bot_token and settings.slack_channel:
        notifiers.append(SlackNotifier(settings.slack_bot_token, settings.slack_channel))
    if settings.smtp_host and settings.notify_email_to:
        notifiers.append(EmailNotifier(settings))
    if settings.mcp_notify and settings.mcp_server_url:
        notifiers.append(McpNotifier(settings.mcp_server_url))
    return notifiers


def format_completion_message(
    *,
    project_name: str,
    experiment_name: str,
    goal_text: str,
    stop_reason: str | None,
    optimal_params: dict[str, Any],
    metrics: Any,
    target_metric: MetricName | str,
    target_value: float | None,
    direction: GoalDirection | str,
    objective: Objective | None = None,
    changed_params: dict[str, Any] | None = None,
    algorithm_id: str | None = None,
) -> tuple[str, str]:
    m = normalize_metrics(metrics)
    tm = target_metric.value if isinstance(target_metric, MetricName) else str(target_metric)
    obj = objective or Objective(metric=tm, direction=GoalDirection(direction), target=target_value)
    subject = f"실험 완료: {project_name} / {experiment_name}"
    params = ", ".join(f"{k}={v}" for k, v in sorted(optimal_params.items())) or "(없음)"
    body_lines = [
        f"프로젝트: {project_name}" + (f"  (알고리즘 {algorithm_id})" if algorithm_id else ""),
        f"실험: {experiment_name}",
        f"목표: {goal_text or '-'}  ({obj.describe()})",
        f"종료 사유: {stop_reason or '-'}",
        f"최적 파라미터: {params}",
    ]
    if changed_params:
        body_lines.append("기본값 대비 변경: " + ", ".join(
            f"{k} {c.get('old')}→{c.get('new')}" for k, c in changed_params.items()))
    if m:
        shown = [obj.metric, *[c.metric for c in obj.constraints]]
        shown += [n for n in ("precision", "recall", "f1", "fp_rate", "fn_rate") if n not in shown]
        body_lines.append("성능: " + ", ".join(f"{n}={m[n]:.4f}" for n in shown if n in m))
    return subject, "\n".join(body_lines)


def _send_all(settings: MasterSettings, subject: str, body: str) -> dict[str, str]:
    refs: dict[str, str] = {}
    for notifier in build_notifiers(settings):
        try:
            refs[notifier.name] = notifier.send(subject, body)
        except Exception as exc:  # notification failure must never crash a workflow
            refs[notifier.name] = f"error:{exc}"
    return refs


def notify_experiment_complete(
    settings: MasterSettings,
    *,
    dedup_key: str,
    project_name: str,
    experiment_name: str,
    goal_text: str,
    stop_reason: str | None,
    optimal_params: dict[str, Any],
    metrics: Any,
    target_metric: MetricName | str,
    target_value: float | None,
    direction: GoalDirection | str,
    objective: Objective | None = None,
    changed_params: dict[str, Any] | None = None,
    algorithm_id: str | None = None,
) -> dict[str, str]:
    """Fire the completion alert once. Returns {notifier_name: ref}. Idempotent per dedup_key."""
    if dedup_key in _SENT:
        return {"deduped": dedup_key}
    subject, body = format_completion_message(
        project_name=project_name,
        experiment_name=experiment_name,
        goal_text=goal_text,
        stop_reason=stop_reason,
        optimal_params=optimal_params,
        metrics=metrics,
        target_metric=target_metric,
        target_value=target_value,
        direction=direction,
        objective=objective,
        changed_params=changed_params,
        algorithm_id=algorithm_id,
    )
    refs = _send_all(settings, subject, body)
    _SENT.add(dedup_key)
    return refs


def format_benchmark_message(*, name: str, status: str, summary: dict[str, int],
                             failing: list[dict[str, Any]], url: str | None = None) -> tuple[str, str]:
    icon = "✅" if status == "passed" else "❌"
    subject = f"{icon} 벤치마크 {status}: {name}"
    counts = ", ".join(f"{k}={v}" for k, v in summary.items() if v) or "셀 없음"
    lines = [f"벤치마크: {name}", f"결과: {counts}"]
    for cell in failing[:20]:
        lines.append(f"- [{cell.get('verdict')}] {cell.get('project')}/{cell.get('algorithm_id')}"
                     f"@{cell.get('suite_id')}: {cell.get('detail', '')}")
    if len(failing) > 20:
        lines.append(f"... 외 {len(failing) - 20}건")
    if url:
        lines.append(url)
    return subject, "\n".join(lines)


def notify_benchmark_complete(settings: MasterSettings, *, dedup_key: str, name: str, status: str,
                              summary: dict[str, int], failing: list[dict[str, Any]]) -> dict[str, str]:
    if dedup_key in _SENT:
        return {"deduped": dedup_key}
    subject, body = format_benchmark_message(name=name, status=status, summary=summary, failing=failing)
    refs = _send_all(settings, subject, body)
    _SENT.add(dedup_key)
    return refs


__all__ = [
    "Notifier",
    "ConsoleNotifier",
    "SlackNotifier",
    "EmailNotifier",
    "build_notifiers",
    "format_completion_message",
    "notify_experiment_complete",
    "format_benchmark_message",
    "notify_benchmark_complete",
]
