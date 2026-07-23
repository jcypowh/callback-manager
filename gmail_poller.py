"""IMAP polling for patient-related emails from Solium AI and Halaxy.

Three real templates are handled:
1. Solium "New message for <practice>" - patient wants a human callback.
2. Solium "New Appointment Booked" - the AI receptionist booked directly.
3. Halaxy "Halaxy online appointment booking" - patient self-booked online.

Each of (2) and (3) is further classified new vs follow-up by scanning the
body text for "follow up" - the email's own heading/subject isn't reliable
for this (a real Solium email was headed "New Appointment Booked" but the
appointment details inside actually said "Follow Up appointment").
"""
import imaplib
import email
import re
from datetime import datetime, timedelta, timezone
from email.header import decode_header

from bs4 import BeautifulSoup

SOLIUM_FROM = 'automations@solium.ai'
HALAXY_FROM = 'noreply@halaxy.com'
WATCHED_SENDERS = (SOLIUM_FROM, HALAXY_FROM)

KNOWN_HOSPITALS = ['Dee Why Endoscopy', 'Mater Hospital', 'East Sydney Private Hospital', 'Northern Beaches Hospital']


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


def _guess_hospital(text):
    for h in KNOWN_HOSPITALS:
        if h.lower() in text.lower():
            return h
    return None


def _appointment_kind(text):
    return 'appointment_followup' if re.search(r'follow[\s-]?up', text, re.IGNORECASE) else 'appointment_new'


def _parse_solium_callback(subject, text):
    """Solium's "New message for X" - patient wants a human to call back."""
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

    message_text = re.split(r'This inbox is not monitored', message_text, flags=re.IGNORECASE)[0]
    kept_lines = [l for l in message_text.splitlines() if not _BOILERPLATE_LINE.match(l.strip())]
    message_text = '\n'.join(kept_lines).strip() or message_text.strip()

    return {
        'source_label': source_label,
        'patient_name': patient_name,
        'phone_number': phone_number,
        'message_text': message_text,
        'intake_source': 'solium',
        'intake_kind': 'callback_request',
    }


def _parse_solium_booking(subject, text):
    """Solium's "New Appointment Booked" - the AI booked one directly."""
    body = re.split(r'This inbox is not monitored', text, flags=re.IGNORECASE)[0]

    name_match = re.search(r'^Patient Name:\s*(.+)$', body, re.IGNORECASE | re.MULTILINE)
    phone_match = re.search(r'^Phone:\s*([+\d][\d \-()]*)', body, re.IGNORECASE | re.MULTILINE)
    details_match = re.search(r'Appointment Details:\s*(.+)', body, re.IGNORECASE | re.DOTALL)

    patient_name = name_match.group(1).strip() if name_match else None
    phone_number = phone_match.group(1).strip() if phone_match else None
    details = details_match.group(1).strip() if details_match else body.strip()

    return {
        'source_label': _guess_hospital(details) or 'Solium AI booking',
        'patient_name': patient_name,
        'phone_number': phone_number,
        'message_text': details,
        'intake_source': 'solium',
        'intake_kind': _appointment_kind(details),
    }


def _parse_halaxy_booking(subject, text):
    """Halaxy's "Halaxy online appointment booking" - patient self-booked."""
    name_match = re.search(r'^Name:\s*(.+)$', text, re.IGNORECASE | re.MULTILINE)
    phone_match = re.search(r'^Phone:\s*([+\d][\d \-()]*)', text, re.IGNORECASE | re.MULTILINE)
    email_match = re.search(r'^Email:\s*(.+)$', text, re.IGNORECASE | re.MULTILINE)
    notes_match = re.search(r'^Notes:\s*(.+)$', text, re.IGNORECASE | re.MULTILINE)
    location_match = re.search(r'^Location:\s*(.+)$', text, re.IGNORECASE | re.MULTILINE)
    datetime_match = re.search(r'^Requested appointment date and time:\s*(.+)$', text, re.IGNORECASE | re.MULTILINE)
    fee_match = re.search(r'^Fee\s*/\s*Service:\s*(.+)$', text, re.IGNORECASE | re.MULTILINE)

    patient_name = name_match.group(1).strip() if name_match else None
    phone_number = phone_match.group(1).strip() if phone_match else None
    location = location_match.group(1).strip() if location_match else ''

    lines = []
    if datetime_match:
        lines.append(f'Requested: {datetime_match.group(1).strip()}')
    if location:
        lines.append(f'Location: {location}')
    if fee_match:
        lines.append(f'Service: {fee_match.group(1).strip()}')
    if email_match:
        lines.append(f'Email: {email_match.group(1).strip()}')
    if notes_match:
        lines.append(f'Notes: {notes_match.group(1).strip()}')
    message_text = '\n'.join(lines) or text

    kind_source = (fee_match.group(1) if fee_match else '') + ' ' + text

    return {
        'source_label': _guess_hospital(location) or _guess_hospital(text) or 'Halaxy booking',
        'patient_name': patient_name,
        'phone_number': phone_number,
        'message_text': message_text,
        'intake_source': 'halaxy',
        'intake_kind': _appointment_kind(kind_source),
    }


def _parse_body(sender, subject, text):
    sender = (sender or '').lower()
    if HALAXY_FROM in sender:
        return _parse_halaxy_booking(subject, text)
    # Solium: "Patient Name:" only appears in the direct-booking template,
    # not the "wants a callback" template (which uses "From:" instead).
    if re.search(r'^Patient Name:', text, re.IGNORECASE | re.MULTILINE):
        return _parse_solium_booking(subject, text)
    return _parse_solium_callback(subject, text)


def fetch_new_patient_emails(gmail_address, app_password, existing_message_ids, days_back=3, folder='INBOX'):
    """Returns a list of dicts (message_id, source_label, patient_name,
    phone_number, message_text, intake_source, intake_kind) for Solium/Halaxy
    emails not already in existing_message_ids.

    `folder` lets this watch a dedicated Gmail label instead of the whole
    inbox (recommended - see admin Settings page for the one-time Gmail
    filter setup). The FROM filter is kept even when scoped to a label, as a
    second check."""
    results = []
    imap = imaplib.IMAP4_SSL('imap.gmail.com')
    try:
        imap.login(gmail_address, app_password)
        status, _ = imap.select(f'"{folder}"' if ' ' in folder else folder)
        if status != 'OK':
            imap.select('INBOX')
        since_date = (datetime.now(timezone.utc) - timedelta(days=days_back)).strftime('%d-%b-%Y')

        seen_uids = set()
        for sender in WATCHED_SENDERS:
            status, data = imap.search(None, f'(FROM "{sender}" SINCE {since_date})')
            if status != 'OK' or not data or not data[0]:
                continue
            for eid in data[0].split():
                if eid in seen_uids:
                    continue
                seen_uids.add(eid)
                status, msg_data = imap.fetch(eid, '(RFC822)')
                if status != 'OK' or not msg_data or msg_data[0] is None:
                    continue
                raw = msg_data[0][1]
                msg = email.message_from_bytes(raw)
                message_id = (msg.get('Message-ID') or '').strip()
                if not message_id or message_id in existing_message_ids:
                    continue
                sender_header = _decode_str(msg.get('From', ''))
                subject = _decode_str(msg.get('Subject', ''))
                body_text = _extract_body_text(msg)
                parsed = _parse_body(sender_header, subject, body_text)
                parsed['message_id'] = message_id
                results.append(parsed)
    finally:
        try:
            imap.logout()
        except Exception:
            pass
    return results
