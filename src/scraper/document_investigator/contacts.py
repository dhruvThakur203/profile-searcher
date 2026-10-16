from __future__ import annotations

import re

from ..phones import PHONE_PATTERN, blocked_digit_strings, phone_key, plausible_phone
from ..result_parser import EMAIL_PATTERN
from .models import ContactEvidence, EvidenceGroup, ShareholderTarget, SourceDocument


CONTACT_HINT = re.compile(
    r"(?i)\b(phone|mobile|telephone|tel|email|e-mail|contact)\b"
)


def extract_contacts_for_groups(
    target: ShareholderTarget,
    document: SourceDocument,
    groups: list[EvidenceGroup],
) -> list[ContactEvidence]:
    """Extract phones/emails only from accepted evidence-group text.

    Numbers that appear only in unrelated document regions are ignored.
    """
    blocked = blocked_digit_strings(
        target.name,
        target.address1,
        target.address2,
        target.address3,
        target.address4,
        target.pincode,
    )
    contacts: list[ContactEvidence] = []
    seen: set[tuple[str, str]] = set()

    for group in groups:
        region = group.text or ""
        # Prefer labeled contact lines inside the group.
        for match in CONTACT_HINT.finditer(region):
            window_start = max(0, match.start() - 20)
            window_end = min(len(region), match.end() + 80)
            window = region[window_start:window_end]
            contacts.extend(
                _contacts_from_text(
                    window,
                    document=document,
                    group=group,
                    blocked=blocked,
                    seen=seen,
                    require_hint=False,
                )
            )
        # Also allow clearly formatted contacts inside the same record text.
        contacts.extend(
            _contacts_from_text(
                region,
                document=document,
                group=group,
                blocked=blocked,
                seen=seen,
                require_hint=True,
            )
        )
    return contacts


def _contacts_from_text(
    text: str,
    *,
    document: SourceDocument,
    group: EvidenceGroup,
    blocked: set[str],
    seen: set[tuple[str, str]],
    require_hint: bool,
) -> list[ContactEvidence]:
    found: list[ContactEvidence] = []
    if require_hint and not CONTACT_HINT.search(text):
        # Still allow emails in the record without a label; phones need a hint
        # when scanning the whole group to avoid random folio-like numbers.
        for match in EMAIL_PATTERN.finditer(text):
            email = match.group(0)
            key = ("email", email.casefold())
            if key in seen:
                continue
            seen.add(key)
            found.append(
                ContactEvidence(
                    type="email",
                    value=email,
                    normalized_value=email.casefold(),
                    source_url=document.source_url,
                    context=_clip(text, match.start(), match.end()),
                    page_number=group.page_number,
                    group_id=group.group_id,
                )
            )
        return found

    for match in EMAIL_PATTERN.finditer(text):
        email = match.group(0)
        key = ("email", email.casefold())
        if key in seen:
            continue
        seen.add(key)
        found.append(
            ContactEvidence(
                type="email",
                value=email,
                normalized_value=email.casefold(),
                source_url=document.source_url,
                context=_clip(text, match.start(), match.end()),
                page_number=group.page_number,
                group_id=group.group_id,
            )
        )

    for match in PHONE_PATTERN.finditer(text):
        display = plausible_phone(match.group(1), blocked)
        if display is None:
            continue
        key = ("phone", phone_key(display))
        if key in seen:
            continue
        # When require_hint is True, only keep phones near a contact word.
        if require_hint:
            local = text[max(0, match.start() - 40) : match.end() + 10]
            if not CONTACT_HINT.search(local):
                continue
        seen.add(key)
        found.append(
            ContactEvidence(
                type="phone",
                value=display,
                normalized_value=phone_key(display),
                source_url=document.source_url,
                context=_clip(text, match.start(), match.end()),
                page_number=group.page_number,
                group_id=group.group_id,
            )
        )
    return found


def _clip(text: str, start: int, end: int, radius: int = 60) -> str:
    left = max(0, start - radius)
    right = min(len(text), end + radius)
    return " ".join(text[left:right].split())
