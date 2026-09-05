"""IMAP polling for referral letters that arrive as a PDF email attachment
rather than an actual fax - e.g. Dr Tu forwarding or receiving a referral
letter, GP portal letters, "BPS Letter" style attachments. Uses the SAME
mailbox/credentials as fax_poller.py (the gastroenterologyjtu@gmail.com
account) but looks at everything else in the inbox instead of just the
VOIP.net fax notifications.

Unlike the Solium/Halaxy/fax pollers, there's no single sender to filter on
here - subjects and senders are wildly inconsistent (GP letterheads, referral
services, "BPS Letter", etc.). So the rule is deliberately broad: any email
not from an already-handled automated sender, with a PDF attachment, gets
caught and handed to a human to file under a patient name - better to catch
a few irrelevant PDFs than lose a referral."""
import imaplib
import email
from datetime import datetime, timedelta, timezone
from email.header import decode_header

# Senders already handled by their own poller - never re-catch these here.
EXCLUDED_SENDERS = ('do_not_reply@au.voipcloud.online', 'automations@solium.ai', 'noreply@halaxy.com')


def _decode_str(value):
    if not value:
        return ''
    parts = decode_header(value)
    out = []
    for text, enc in parts:
        if isinstance(text, bytes):
            out.append(text.decode(enc or 'utf-8', errors='replace'))
        else:
            out.append(text)
    return ''.join(out)


def _extract_pdf_attachment(msg):
    for part in (msg.walk() if msg.is_multipart() else []):
        filename = part.get_filename()
        if filename:
            filename = _decode_str(filename)
            if filename.lower().endswith('.pdf'):
                return filename, part.get_payload(decode=True)
    return None, None


def fetch_new_referral_emails(gmail_address, app_password, existing_message_ids, days_back=30, folder='INBOX'):
    """Returns a list of dicts (message_id, from_address, subject, filename,
    pdf_bytes) for emails with a PDF attachment, not already in
    existing_message_ids and not from an already-handled automated sender."""
    results = []
    imap = imaplib.IMAP4_SSL('imap.gmail.com')
    try:
        imap.login(gmail_address, app_password)
        status, _ = imap.select(f'"{folder}"' if ' ' in folder else folder)
        if status != 'OK':
            imap.select('INBOX')
        since_date = (datetime.now(timezone.utc) - timedelta(days=days_back)).strftime('%d-%b-%Y')
        status, data = imap.search(None, f'(SINCE {since_date})')
        if status != 'OK' or not data or not data[0]:
            return results
        for eid in data[0].split():
            status, msg_data = imap.fetch(eid, '(RFC822)')
            if status != 'OK' or not msg_data or msg_data[0] is None:
                continue
            raw = msg_data[0][1]
            msg = email.message_from_bytes(raw)
            message_id = (msg.get('Message-ID') or '').strip()
            if not message_id or message_id in existing_message_ids:
                continue
            sender = _decode_str(msg.get('From', ''))
            if any(excluded in sender.lower() for excluded in EXCLUDED_SENDERS):
                continue
            filename, pdf_bytes = _extract_pdf_attachment(msg)
            if not pdf_bytes:
                continue
            subject = _decode_str(msg.get('Subject', ''))
            results.append({
                'message_id': message_id,
                'from_address': sender,
                'subject': subject,
                'filename': filename,
                'pdf_bytes': pdf_bytes,
            })
    finally:
        try:
            imap.logout()
        except Exception:
            pass
    return results
