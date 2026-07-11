"""IMAP polling for Solium AI patient-callback emails.

Solium forwards a "New message for <practice>" email with a "From:" (patient name)
and "Incoming Number:" (phone) label somewhere in the body. The exact template
hasn't been verified against a live sample yet, so parsing is defensive: if the
labels aren't found, the full message text is kept and patient_name/phone_number
are left blank rather than dropping the email.
"""
import imaplib
import email
import re
from datetime import datetime, timedelta, timezone
from email.header import decode_header

from bs4 import BeautifulSoup

SOLIUM_FROM = 'automations@solium.ai'


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
    html_part = None
    plain_part = None
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            if ctype == 'text/html' and html_part is None:
                html_part = part
            elif ctype == 'text/plain' and plain_part is None:
                plain_part = part
    else:
        if msg.get_content_type() == 'text/html':
            html_part = msg
        else:
            plain_part = msg

    part = html_part or plain_part
    if part is None:
        return ''
    payload = part.get_payload(decode=True) or b''
    charset = part.get_content_charset() or 'utf-8'
    try:
        text = payload.decode(charset, errors='replace')
    except (LookupError, TypeError):
        text = payload.decode('utf-8', errors='replace')

    if part is html_part:
        soup = BeautifulSoup(text, 'html.parser')
        text = soup.get_text(separator='\n')

    lines = [line.strip() for line in text.splitlines()]
    lines = [line for line in lines if line]
    return '\n'.join(lines)


_BOILERPLATE_LINE = re.compile(
    r'^(New message( for .+| from .+)?\.?|You received a new patient message\.)$',
    re.IGNORECASE,
)


def _parse_body(subject, text):
    # Solium's real subject is just "<name> has left a message" - the practice
    # name lives in the body ("New message for <practice>"), not the subject.
    source_match = re.search(r'New message for\s+([^\n.]+)', text, re.IGNORECASE)
    if not source_match:
        source_match = re.search(r'New message for\s+(.+)', subject, re.IGNORECASE)
    source_label = source_match.group(1).strip() if source_match else subject.strip()

    from_match = re.search(r'^From:\s*(.+)$', text, re.IGNORECASE | re.MULTILINE)
    number_match = re.search(r'^Incoming Number:\s*([+\d][\d \-()]*)', text, re.IGNORECASE | re.MULTILINE)

    patient_name = from_match.group(1).strip() if from_match else None
    phone_number = number_match.group(1).strip() if number_match else None

    message_text = text
    if from_match or number_match:
        remainder = text
        if from_match:
            remainder = remainder.replace(from_match.group(0), '')
        if number_match:
            remainder = remainder.replace(number_match.group(0), '')
        remainder = '\n'.join(line for line in remainder.splitlines() if line.strip())
        message_text = remainder.strip() or text

    # Drop Solium's boilerplate header lines and the "not monitored" footer -
    # keep just the actual call summary and any collected callback number.
    message_text = re.split(r'This inbox is not monitored', message_text, flags=re.IGNORECASE)[0]
    kept_lines = [l for l in message_text.splitlines() if not _BOILERPLATE_LINE.match(l.strip())]
    message_text = '\n'.join(kept_lines).strip() or message_text.strip()

    return {
        'source_label': source_label,
        'patient_name': patient_name,
        'phone_number': phone_number,
        'message_text': message_text,
    }


def fetch_new_solium_emails(gmail_address, app_password, existing_message_ids, days_back=3, folder='INBOX'):
    """Returns a list of dicts (message_id, source_label, patient_name, phone_number,
    message_text) for Solium emails not already in existing_message_ids.

    `folder` lets this watch a dedicated Gmail label instead of the whole inbox
    (recommended - see admin Settings page for the one-time Gmail filter setup).
    The FROM filter is kept even when scoped to a label, as a second check."""
    results = []
    imap = imaplib.IMAP4_SSL('imap.gmail.com')
    try:
        imap.login(gmail_address, app_password)
        status, _ = imap.select(f'"{folder}"' if ' ' in folder else folder)
        if status != 'OK':
            imap.select('INBOX')
        since_date = (datetime.now(timezone.utc) - timedelta(days=days_back)).strftime('%d-%b-%Y')
        status, data = imap.search(None, f'(FROM "{SOLIUM_FROM}" SINCE {since_date})')
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
            subject = _decode_str(msg.get('Subject', ''))
            body_text = _extract_body_text(msg)
            parsed = _parse_body(subject, body_text)
            parsed['message_id'] = message_id
            results.append(parsed)
    finally:
        try:
            imap.logout()
        except Exception:
            pass
    return results
