"""Assert the assistant never claims a capability this release does not have.

AGENTS.md: "Do not convert an unimplemented integration into an enabled UI
status merely to make a demo look complete." These patterns make that rule
executable. They match *affirmative* claims only: the shipped replies
legitimately mention WhatsApp, SMS and external calendars in the negative
("No WhatsApp, SMS, or external-calendar message has been sent."), and those
disclaimers must keep passing.
"""
import re

FORBIDDEN = [
    ('claimed_call_transfer', re.compile(
        r'\b(transferring|connecting|putting)\s+you\s+(through|to|now)\b|'
        r'\bi(?:\s+am|\'m)\s+(?:now\s+)?transferring\b|'
        r'\bplease\s+hold\s+while\s+i\s+(?:transfer|connect)\b', re.I)),
    ('claimed_message_sent', re.compile(
        r'\bi\s+(?:have\s+)?(?:sent|texted|messaged)\s+(?:you\s+)?(?:an?\s+)?'
        r'(?:sms|whatsapp|text|message|confirmation)\b|'
        r'\b(?:an?\s+)?(?:sms|whatsapp|text message)\s+(?:has\s+been\s+|was\s+)sent\b', re.I)),
    ('claimed_external_calendar', re.compile(
        r'\badded\s+to\s+(?:your\s+|the\s+)?google\s+calendar\b|'
        r'\bsynced?\s+(?:to|with)\s+(?:your\s+)?calendar\b', re.I)),
    ('claimed_medical_judgement', re.compile(
        r'\byou\s+(?:probably|likely|most likely)\s+have\b|'
        r'\b(?:i\s+)?diagnos(?:e|ed|is)\s+you\b|'
        r'\btake\s+\d+\s*(?:mg|ml|tablets?)\b', re.I)),
    ('claimed_emergency_dispatch', re.compile(
        r'\bi\s+(?:have\s+)?(?:called|dispatched|alerted)\s+(?:an?\s+)?'
        r'(?:ambulance|emergency services|911|108|112)\b', re.I)),
]


def violations(text):
    """Names of the forbidden claims present in one reply."""
    return [name for name, pattern in FORBIDDEN if pattern.search(text or '')]


def scan(replies):
    """Violations across many replies, as (reply, claim) pairs."""
    found = []
    for reply in replies:
        for claim in violations(reply):
            found.append((reply, claim))
    return found
