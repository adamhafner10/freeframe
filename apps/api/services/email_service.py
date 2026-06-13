import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import Optional
import boto3
from botocore.exceptions import ClientError
from ..config import settings

# Default socket timeout (seconds) for SMTP operations so the API never hangs
# indefinitely on a dead mail server. Resend/SES SMTP is fast; keep this short
# so login UX stays snappy and failures surface quickly.
SMTP_TIMEOUT_SECONDS = 10


class EmailService:
    """
    Email service that supports both AWS SES and standard SMTP.
    Auto-detects based on mail_provider setting in config.
    """
    
    def __init__(self):
        self.provider = settings.mail_provider
        self.from_address = settings.mail_from_address
        self.from_name = settings.mail_from_name
    
    def _get_ses_client(self):
        """Create AWS SES client."""
        return boto3.client(
            "ses",
            aws_access_key_id=settings.aws_mail_access_key_id,
            aws_secret_access_key=settings.aws_mail_secret_access_key,
            region_name=settings.aws_mail_region,
        )
    
    def _send_via_ses(
        self,
        to_email: str,
        subject: str,
        html_body: str,
        text_body: Optional[str] = None,
        raise_on_error: bool = False,
    ) -> bool:
        """Send email via AWS SES.

        When raise_on_error is True the underlying ClientError propagates so the
        synchronous magic-code path can detect failure; otherwise failures are
        logged and return False (best-effort contract for notifications).
        """
        if not settings.aws_mail_access_key_id or not settings.aws_mail_secret_access_key:
            raise ValueError("AWS SES credentials not configured")

        ses = self._get_ses_client()

        body = {"Html": {"Charset": "UTF-8", "Data": html_body}}
        if text_body:
            body["Text"] = {"Charset": "UTF-8", "Data": text_body}

        try:
            ses.send_email(
                Source=f"{self.from_name} <{self.from_address}>",
                Destination={"ToAddresses": [to_email]},
                Message={
                    "Subject": {"Charset": "UTF-8", "Data": subject},
                    "Body": body,
                },
            )
            return True
        except ClientError as e:
            print(f"SES error: {e.response['Error']['Message']}")
            if raise_on_error:
                raise
            return False
    
    def _send_via_smtp(
        self,
        to_email: str,
        subject: str,
        html_body: str,
        text_body: Optional[str] = None,
        raise_on_error: bool = False,
    ) -> bool:
        """Send email via SMTP server.

        Uses a socket timeout so a dead mail server can't hang the caller.
        When raise_on_error is True, the underlying exception propagates to the
        caller (used by the synchronous magic-code path so the API can detect
        failure and return 503). Otherwise failures are logged and return False
        to preserve the best-effort contract for notification emails.
        """
        if not settings.smtp_host:
            raise ValueError("SMTP host not configured")

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = f"{self.from_name} <{self.from_address}>"
        msg["To"] = to_email

        if text_body:
            msg.attach(MIMEText(text_body, "plain"))
        msg.attach(MIMEText(html_body, "html"))

        server = None
        try:
            if settings.smtp_use_tls:
                server = smtplib.SMTP(
                    settings.smtp_host, settings.smtp_port, timeout=SMTP_TIMEOUT_SECONDS
                )
                server.starttls()
            else:
                server = smtplib.SMTP_SSL(
                    settings.smtp_host, settings.smtp_port, timeout=SMTP_TIMEOUT_SECONDS
                )

            if settings.smtp_user and settings.smtp_password:
                server.login(settings.smtp_user, settings.smtp_password)

            server.sendmail(self.from_address, [to_email], msg.as_string())
            return True
        except Exception as e:
            print(f"SMTP error: {e}")
            if raise_on_error:
                raise
            return False
        finally:
            if server is not None:
                try:
                    server.quit()
                except Exception:
                    pass
    
    def send_email(
        self,
        to_email: str,
        subject: str,
        html_body: str,
        text_body: Optional[str] = None,
    ) -> bool:
        """
        Send email using configured provider (SES or SMTP).
        
        Args:
            to_email: Recipient email address
            subject: Email subject
            html_body: HTML content of the email
            text_body: Optional plain text fallback
            
        Returns:
            True if sent successfully, False otherwise
        """
        if self.provider == "ses":
            return self._send_via_ses(to_email, subject, html_body, text_body)
        elif self.provider == "smtp":
            return self._send_via_smtp(to_email, subject, html_body, text_body)
        else:
            raise ValueError(f"Unknown mail provider: {self.provider}")

    def send_email_sync(
        self,
        to_email: str,
        subject: str,
        html_body: str,
        text_body: Optional[str] = None,
    ) -> bool:
        """Send email synchronously, propagating failures to the caller.

        Unlike send_email (best-effort, returns False on failure), this raises
        if the email cannot be delivered. Used by the magic-code login path so
        the API can return 503 instead of a misleading 200 when mail is down.

        Returns True on success; raises on transport/provider failure.
        """
        if self.provider == "ses":
            ok = self._send_via_ses(to_email, subject, html_body, text_body, raise_on_error=True)
        elif self.provider == "smtp":
            ok = self._send_via_smtp(to_email, subject, html_body, text_body, raise_on_error=True)
        else:
            raise ValueError(f"Unknown mail provider: {self.provider}")
        if not ok:
            raise RuntimeError("Email send reported failure")
        return True

    def check_smtp_health(self) -> bool:
        """Lightweight SMTP reachability probe for health checks.

        Connects, issues NOOP, and quits with a short timeout. Returns True if
        the server responds, False on any failure. Never raises. Only meaningful
        when the SMTP provider is configured; returns False otherwise.
        """
        if not settings.smtp_host:
            return False
        server = None
        try:
            if settings.smtp_use_tls:
                server = smtplib.SMTP(
                    settings.smtp_host, settings.smtp_port, timeout=SMTP_TIMEOUT_SECONDS
                )
                server.starttls()
            else:
                server = smtplib.SMTP_SSL(
                    settings.smtp_host, settings.smtp_port, timeout=SMTP_TIMEOUT_SECONDS
                )
            if settings.smtp_user and settings.smtp_password:
                server.login(settings.smtp_user, settings.smtp_password)
            code, _ = server.noop()
            return 200 <= code < 400
        except Exception as e:
            print(f"SMTP health check failed: {e}")
            return False
        finally:
            if server is not None:
                try:
                    server.quit()
                except Exception:
                    pass

    def send_invite_email(self, to_email: str, inviter_name: str, org_name: str, invite_link: str) -> bool:
        """Send organization invite email."""
        subject = f"You've been invited to join {org_name} on FileStream"
        html_body = f"""
        <html>
        <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
            <h2>You're invited!</h2>
            <p><strong>{inviter_name}</strong> has invited you to join <strong>{org_name}</strong> on FileStream.</p>
            <p>
                <a href="{invite_link}"
                   style="display: inline-block; padding: 12px 24px; background-color: #4F46E5;
                          color: white; text-decoration: none; border-radius: 6px;">
                    Accept Invitation
                </a>
            </p>
            <p style="color: #666; font-size: 14px;">
                If you didn't expect this invitation, you can ignore this email.
            </p>
        </body>
        </html>
        """
        text_body = f"{inviter_name} has invited you to join {org_name} on FileStream. Click here to accept: {invite_link}"
        return self.send_email(to_email, subject, html_body, text_body)

    def send_invite_email_sync(
        self,
        to_email: str,
        inviter_name: str,
        org_name: str,
        invite_link: str,
        team_name: Optional[str] = None,
        expiry_days: int = 7,
    ) -> bool:
        """Send the organization/team invite email synchronously, raising on failure.

        Mirrors the synchronous magic-code path: the invite token lives only in
        this email, there is no resend-on-dispatch, and the API must NOT report
        success unless the email actually went out. We render the same
        templates/email/invite.html the Celery task uses (via render_template) so
        the message stays identical, then send through send_email_sync which
        propagates transport/provider failures to the caller. Raises on failure;
        returns True on success.
        """
        # Local import to avoid a circular import at module load time
        # (email_tasks imports email_service).
        from ..tasks.email_tasks import render_template

        subject = f"You've been invited to join {org_name} on FileStream"
        html_body = render_template(
            "email/invite.html",
            subject=subject,
            inviter_name=inviter_name,
            org_name=org_name,
            team_name=team_name,
            invite_link=invite_link,
            expiry_days=expiry_days,
        )
        text_body = (
            f"{inviter_name} has invited you to join {org_name} on FileStream. "
            f"Accept here: {invite_link}"
        )
        return self.send_email_sync(to_email, subject, html_body, text_body)
    
    def send_comment_notification(
        self, 
        to_email: str, 
        commenter_name: str, 
        asset_name: str, 
        comment_preview: str,
        asset_link: str
    ) -> bool:
        """Send notification when someone comments on an asset."""
        subject = f"New comment on {asset_name}"
        html_body = f"""
        <html>
        <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
            <h2>New Comment</h2>
            <p><strong>{commenter_name}</strong> commented on <strong>{asset_name}</strong>:</p>
            <blockquote style="border-left: 3px solid #4F46E5; padding-left: 12px; color: #555;">
                {comment_preview}
            </blockquote>
            <p>
                <a href="{asset_link}" 
                   style="display: inline-block; padding: 12px 24px; background-color: #4F46E5; 
                          color: white; text-decoration: none; border-radius: 6px;">
                    View Comment
                </a>
            </p>
        </body>
        </html>
        """
        text_body = f"{commenter_name} commented on {asset_name}: {comment_preview}\n\nView: {asset_link}"
        return self.send_email(to_email, subject, html_body, text_body)
    
    def send_mention_notification(
        self,
        to_email: str,
        mentioner_name: str,
        asset_name: str,
        comment_preview: str,
        asset_link: str
    ) -> bool:
        """Send notification when someone mentions a user."""
        subject = f"{mentioner_name} mentioned you on {asset_name}"
        html_body = f"""
        <html>
        <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
            <h2>You were mentioned</h2>
            <p><strong>{mentioner_name}</strong> mentioned you on <strong>{asset_name}</strong>:</p>
            <blockquote style="border-left: 3px solid #4F46E5; padding-left: 12px; color: #555;">
                {comment_preview}
            </blockquote>
            <p>
                <a href="{asset_link}" 
                   style="display: inline-block; padding: 12px 24px; background-color: #4F46E5; 
                          color: white; text-decoration: none; border-radius: 6px;">
                    View Comment
                </a>
            </p>
        </body>
        </html>
        """
        text_body = f"{mentioner_name} mentioned you on {asset_name}: {comment_preview}\n\nView: {asset_link}"
        return self.send_email(to_email, subject, html_body, text_body)
    
    def send_assignment_notification(
        self,
        to_email: str,
        assigner_name: str,
        asset_name: str,
        due_date: Optional[str],
        asset_link: str
    ) -> bool:
        """Send notification when user is assigned to review an asset."""
        due_text = f" (due {due_date})" if due_date else ""
        subject = f"You've been assigned to review {asset_name}{due_text}"
        html_body = f"""
        <html>
        <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
            <h2>New Assignment</h2>
            <p><strong>{assigner_name}</strong> has assigned you to review <strong>{asset_name}</strong>.</p>
            {"<p><strong>Due date:</strong> " + due_date + "</p>" if due_date else ""}
            <p>
                <a href="{asset_link}" 
                   style="display: inline-block; padding: 12px 24px; background-color: #4F46E5; 
                          color: white; text-decoration: none; border-radius: 6px;">
                    Review Asset
                </a>
            </p>
        </body>
        </html>
        """
        text_body = f"{assigner_name} assigned you to review {asset_name}.{' Due: ' + due_date if due_date else ''}\n\nView: {asset_link}"
        return self.send_email(to_email, subject, html_body, text_body)
    
    def send_approval_notification(
        self,
        to_email: str,
        reviewer_name: str,
        asset_name: str,
        status: str,  # "approved" or "rejected"
        note: Optional[str],
        asset_link: str
    ) -> bool:
        """Send notification when an asset is approved or rejected."""
        status_emoji = "✅" if status == "approved" else "❌"
        subject = f"{status_emoji} {asset_name} has been {status}"
        html_body = f"""
        <html>
        <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
            <h2>Asset {status.title()}</h2>
            <p><strong>{reviewer_name}</strong> has <strong>{status}</strong> <strong>{asset_name}</strong>.</p>
            {"<p><strong>Note:</strong> " + note + "</p>" if note else ""}
            <p>
                <a href="{asset_link}" 
                   style="display: inline-block; padding: 12px 24px; background-color: #4F46E5; 
                          color: white; text-decoration: none; border-radius: 6px;">
                    View Asset
                </a>
            </p>
        </body>
        </html>
        """
        text_body = f"{reviewer_name} {status} {asset_name}.{' Note: ' + note if note else ''}\n\nView: {asset_link}"
        return self.send_email(to_email, subject, html_body, text_body)


# Singleton instance
email_service = EmailService()
