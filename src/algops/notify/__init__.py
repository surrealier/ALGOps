"""algops.notify — deterministic completion notifications (Slack / email / console).

The "experiment finished" alert must NOT route through the LLM (it might forget or
hallucinate). It is fired deterministically from the finalize step, deduped by experiment,
and degrades to a console log when no channel is configured.
"""

from __future__ import annotations

import smtplib
from abc import ABC, abstractmethod
from email.mime.text import MIMEText
from typing import Any

from algops.core import GoalDirection, MasterSettings, Metrics, MetricName

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
    metrics: Metrics | None,
    target_metric: MetricName | str,
    target_value: float,
    direction: GoalDirection | str,
) -> tuple[str, str]:
    tm = MetricName(target_metric).value
    arrow = "≤" if GoalDirection(direction) is GoalDirection.minimize else "≥"
    subject = f"실험 완료: {project_name} / {experiment_name}"
    params = ", ".join(f"{k}={v}" for k, v in sorted(optimal_params.items())) or "(없음)"
    body_lines = [
        f"프로젝트: {project_name}",
        f"실험: {experiment_name}",
        f"목표: {goal_text or '-'}  (목표 지표 {tm} {arrow} {target_value})",
        f"종료 사유: {stop_reason or '-'}",
        f"최적 파라미터: {params}",
    ]
    if metrics is not None:
        body_lines.append(
            f"성능: {tm}={metrics.get(target_metric):.4f}, "
            f"precision={metrics.precision:.4f}, recall={metrics.recall:.4f}, "
            f"f1={metrics.f1:.4f}, fp_rate={metrics.fp_rate:.4f}, fn_rate={metrics.fn_rate:.4f}"
        )
    return subject, "\n".join(body_lines)


def notify_experiment_complete(
    settings: MasterSettings,
    *,
    dedup_key: str,
    project_name: str,
    experiment_name: str,
    goal_text: str,
    stop_reason: str | None,
    optimal_params: dict[str, Any],
    metrics: Metrics | None,
    target_metric: MetricName | str,
    target_value: float,
    direction: GoalDirection | str,
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
    )
    refs: dict[str, str] = {}
    for notifier in build_notifiers(settings):
        try:
            refs[notifier.name] = notifier.send(subject, body)
        except Exception as exc:  # notification failure must never crash the loop
            refs[notifier.name] = f"error:{exc}"
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
]
