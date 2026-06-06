"""Email HITL notifier via SMTP (works with SendGrid, AWS SES, Mailgun, etc.)."""

from __future__ import annotations

import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from agentforge.hitl.approvals import ApprovalRequest
from ui.backend.config import get_settings


async def email_notify(request: ApprovalRequest, reviewer_group: str) -> None:
    """Send an HTML email for a pending HITL approval."""
    settings = get_settings()
    if not settings.smtp_host or not settings.smtp_user:
        return

    subject = f"[AgentForge] Approval Required: {request.action} · {request.agent_id}"
    html = _build_html(request, reviewer_group)

    # Resolve recipient (in production: look up reviewer_group → email list in DB)
    to_addr = settings.smtp_user  # fallback: send to the configured user

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = settings.smtp_from
        msg["To"] = to_addr
        msg.attach(MIMEText(html, "html"))

        with smtplib.SMTP(settings.smtp_host, settings.smtp_port) as server:
            if settings.smtp_tls:
                server.starttls()
            server.login(settings.smtp_user, settings.smtp_password)
            server.sendmail(settings.smtp_from, [to_addr], msg.as_string())
    except Exception:
        pass


def _build_html(request: ApprovalRequest, reviewer_group: str) -> str:
    ctx_rows = "".join(
        f"<tr><td style='padding:6px 12px;color:#6b7280;'>{k}</td>"
        f"<td style='padding:6px 12px;font-weight:600;'>{v}</td></tr>"
        for k, v in (request.context or {}).items()
    )
    console_url = "http://localhost:8000/#hitl"

    return f"""
    <div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;max-width:600px;margin:0 auto;">
      <div style="background:#1a1d27;padding:24px;border-radius:8px 8px 0 0;">
        <h2 style="color:#e8eaf6;margin:0;">⚠️ Agent Approval Required</h2>
      </div>
      <div style="background:#ffffff;padding:24px;border:1px solid #e5e7eb;border-top:none;">
        <table style="width:100%;border-collapse:collapse;margin-bottom:20px;">
          <tr><td style="padding:6px 12px;color:#6b7280;">Agent</td>
              <td style="padding:6px 12px;font-weight:600;font-family:monospace;">{request.agent_id}</td></tr>
          <tr style="background:#f9fafb;"><td style="padding:6px 12px;color:#6b7280;">Action</td>
              <td style="padding:6px 12px;font-weight:600;">{request.action.replace('_',' ')}</td></tr>
          <tr><td style="padding:6px 12px;color:#6b7280;">Resource</td>
              <td style="padding:6px 12px;font-weight:600;">{request.resource}</td></tr>
          <tr style="background:#f9fafb;"><td style="padding:6px 12px;color:#6b7280;">Reviewer Group</td>
              <td style="padding:6px 12px;">{reviewer_group}</td></tr>
          {ctx_rows}
        </table>
        <a href="{console_url}" style="display:inline-block;background:#5c7cfa;color:#fff;padding:12px 24px;
           border-radius:6px;text-decoration:none;font-weight:600;">
          Review in AgentForge Console →
        </a>
      </div>
      <div style="background:#f9fafb;padding:12px 24px;border:1px solid #e5e7eb;border-top:none;
                  border-radius:0 0 8px 8px;font-size:12px;color:#6b7280;">
        Run ID: {request.correlation_id} · Request: {request.request_id[:8]}…
      </div>
    </div>
    """
