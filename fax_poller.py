"""IMAP polling for VOIP.net fax-to-email notifications.

Each fax arrives as a plain notification email ("You have a new fax, received
from <number> at <datetime>.") with exactly one PDF attachment - the scanned
fax itself. Unlike the Solium/Halaxy poller, there's no attempt to classify
the content here - a human previews the PDF and files it (see app.py's
fax_inbox routes)."""
import imaplib
import email
import re
from datetime import datetime, timedelta, timezone
from email.header import decode_header

FAX_FROM = 'do_not_reply@au.voipcloud.online'


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


def _extract_body_text(msg):
    for part in (msg.walk() if msg.is_multipart() else [msg]):
        if part.get_content_type() == 'text/plain':
            payload = part.get_payload(decode=True) or b''
            charset = part.get_content_charset() or 'utf-8'
            try:
                return payload.decode(charset, errors='replace')
            except (LookupError, TypeError):
                return payload.decode('utf-8', errors='replace')
    return ''


def _extract_pdf_attachment(msg):
    for part in (msg.walk() if msg.is_multipart() else []):
        filename = part.get_filename()
        if filename and filename.lower().endswith('.pdf'):
            return part.get_payload(decode=True)
    return None


def fetch_new_faxes(gmail_address, app_password, existing_message_ids, days_back=7, folder='INBOX'):
    """Returns a list of dicts (message_id, from_number, received_at, pdf_bytes)
    for new fax notifications not already in existing_message_ids."""
    results = []
    imap = imaplib.IMAP4_SSL('imap.gmail.com')
    try:
        imap.login(gmail_address, app_password)
        status, _ = imap.select(f'"{folder}"' if ' ' in folder else folder)
        if status != 'OK':
            imap.select('INBOX')
        since_date = (datetime.now(timezone.utc) - timedelta(days=days_back)).strftime('%d-%b-%Y')
        status, data = imap.search(None, f'(FROM "{FAX_FROM}" SINCE {since_date})')
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
            pdf_bytes = _extract_pdf_attachment(msg)
            if not pdf_bytes:
                continue
            body_text = _extract_body_text(msg)
            number_match = re.search(r'received from\s+(\S+)\s+at', body_text, re.IGNORECASE)
            from_number = number_match.group(1).strip() if number_match else None
            results.append({
                'message_id': message_id,
                'from_number': from_number,
                'pdf_bytes': pdf_bytes,
            })
    finally:
        try:
            imap.logout()
        except Exception:
            pass
    return results
