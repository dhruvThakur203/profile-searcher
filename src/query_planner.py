from dataclasses import dataclass
from itertools import zip_longest

from .models import PersonRecord
from .normalize import normalize_name, normalize_text
from .postal import derive_postal_fragments


FAMILY_CORE = "CORE"
FAMILY_JOINT = "JOINT"
FAMILY_POSTAL = "POSTAL"
FAMILY_OBITUARY = "OBITUARY"
FAMILY_HOLDERS = "HOLDERS"
FAMILY_HOLDER_ADDRESS = "HOLDER_ADDRESS"
# Families whose completion is proven only by the exact query recorded in the
# checkpoint; a bare strategy name from an older plan never counts.
QUERY_VERIFIED_FAMILIES = frozenset({FAMILY_HOLDERS, FAMILY_HOLDER_ADDRESS})


@dataclass
class QueryPlan:
    strategy: str
    query: str
    family: str = FAMILY_CORE
    # Holder field the name came from: 1 = Name of the Holder, 2 = Joint 1, 3 = Joint-2.
    holder: int = 1


def distinctive_address_fragment(*addresses: str) -> str:
    """Drop a leading house number when a street-style line remains."""
    best = ""
    for address in addresses:
        tokens = [token for token in address.replace(",", " ").split() if token]
        if len(tokens) < 3 or not tokens[0].isdigit():
            continue
        fragment = " ".join(tokens[1:])
        if len(fragment) > len(best):
            best = fragment
    return best


def joint_holder_names(person: PersonRecord) -> list[str]:
    """Distinct supplied joint-holder names, excluding copies of the primary."""
    primary = normalize_name(person.name)
    names: list[str] = []
    seen = {primary}
    for value in (person.joint1, person.joint2):
        cleaned = " ".join((value or "").split())
        key = normalize_name(cleaned)
        if not key or key in seen:
            continue
        seen.add(key)
        names.append(cleaned)
    return names


def build_query_plan(person: PersonRecord) -> list[QueryPlan]:
    """Primary-holder core strategies first (unchanged order), then the same
    address variants for each joint holder, then supplementary families
    interleaved so each gets a share of the per-person search budget, then
    holder-name combinations."""
    name = person.name.strip()
    a1 = person.address1.strip()
    a2 = person.address2.strip()
    a3 = person.address3.strip()
    a4 = person.address4.strip()
    country = person.country.strip()
    scrip = person.scrip.strip()

    plans: list[QueryPlan] = []
    seen: set[str] = set()

    def make(
        strategy: str, parts: list[str], family: str, holder: int = 1
    ) -> QueryPlan | None:
        query = " ".join(part.strip() for part in parts if part and part.strip())
        key = normalize_text(query)
        if not key or key in seen:
            return None
        seen.add(key)
        return QueryPlan(strategy, query, family, holder)

    def add(strategy: str, parts: list[str], family: str = FAMILY_CORE, holder: int = 1) -> None:
        plan = make(strategy, parts, family, holder)
        if plan is not None:
            plans.append(plan)

    fragment = distinctive_address_fragment(a1, a2, a3, a4)
    postal = derive_postal_fragments(person)

    if name:
        for strategy, parts in address_query_specs(person, name, fragment):
            add(strategy, parts)
    if name and scrip:
        add("Q5_NAME_SCRIP", [name, scrip])

    # Every other holder gets the same address variants under its own name.
    for position, holder_name in distinct_holders(person):
        if position == 1:
            continue
        suffix = f"_HOLDER{position}"
        specs = address_query_specs(person, holder_name, fragment)
        specs += postal_name_specs(holder_name, postal)
        for strategy, parts in specs:
            add(strategy + suffix, parts, FAMILY_HOLDER_ADDRESS, position)

    joints = joint_holder_names(person)
    location = next((item.location for item in postal if item.location), "") or country
    address_hint = fragment or next((value for value in (a1, a2, a3, a4) if value), "")

    # Plain holder-name pairs/triples live in the HOLDERS family below.
    joint_specs: list[tuple[str, list[str]]] = []
    if name and joints and address_hint:
        joint_specs.append(("Q_JOINT_PRIMARY_JOINT_ADDRESS", [name, joints[0], address_hint]))
    if name and joints and scrip:
        joint_specs.append(("Q_JOINT_PRIMARY_JOINT_SCRIP", [name, joints[0], scrip]))

    postal_specs: list[tuple[str, list[str]]] = []
    for index, item in enumerate(postal[:2], start=1):
        suffix = "" if index == 1 else f"_{index}"
        if name:
            postal_specs.append((f"Q_POSTAL_NAME{suffix}", [name, item.value]))
        if fragment and item.value not in fragment:
            postal_specs.append(
                (f"Q_POSTAL_ADDRESS_FRAGMENT{suffix}", [fragment, item.value])
            )
        if name and item.location:
            postal_specs.append(
                (f"Q_POSTAL_NAME_LOCATION{suffix}", [name, item.location, item.value])
            )

    obituary_specs: list[tuple[str, list[str]]] = []
    if len(normalize_name(name).split()) >= 2:
        obituary_specs.append(("Q_OBIT_NAME_OBITUARY", [name, "obituary"]))
        obituary_specs.append(("Q_OBIT_NAME_CONDOLENCES", [name, "condolences"]))
        if location:
            obituary_specs.append(
                ("Q_OBIT_NAME_LOCATION_OBITUARY", [name, location, "obituary"])
            )
        if joints:
            obituary_specs.append(
                ("Q_OBIT_JOINT_PRIMARY_OBITUARY", [joints[0], name, "obituary"])
            )
        obituary_specs.append(("Q_OBIT_NAME_DEATH", [name, "death"]))
        obituary_specs.append(("Q_OBIT_NAME_MEMORIAL", [name, "memorial"]))

    families = (
        (FAMILY_JOINT, joint_specs),
        (FAMILY_POSTAL, postal_specs),
        (FAMILY_OBITUARY, obituary_specs),
    )
    for row in zip_longest(*(specs for _, specs in families)):
        for (family, _), spec in zip(families, row):
            if spec is None:
                continue
            plan = make(spec[0], spec[1], family)
            if plan is not None:
                plans.append(plan)

    for strategy, parts in holder_combination_specs(person):
        plan = make(strategy, parts, FAMILY_HOLDERS)
        if plan is not None:
            plans.append(plan)
    return plans


def address_query_specs(
    person: PersonRecord, holder_name: str, fragment: str
) -> list[tuple[str, list[str]]]:
    """Name + address variants, in priority order, for one holder name."""
    a1 = person.address1.strip()
    a2 = person.address2.strip()
    a3 = person.address3.strip()
    a4 = person.address4.strip()
    pincode = person.pincode.strip()
    country = person.country.strip()
    specs: list[tuple[str, list[str]]] = [
        ("Q1_FULL_NAME_FULL_ADDRESS", [holder_name, a1, a2, a3, a4, pincode])
    ]
    if a1 and a2:
        specs.append(("Q_NAME_ADDRESS1_ADDRESS2", [holder_name, a1, a2]))
    if a1:
        specs.append(("Q2_NAME_ADDRESS1", [holder_name, a1]))
    if a2:
        specs.append(("Q3_NAME_ADDRESS2", [holder_name, a2]))
    if a3:
        specs.append(("Q_NAME_ADDRESS3", [holder_name, a3]))
    if a4:
        specs.append(("Q_NAME_ADDRESS4", [holder_name, a4]))
    if fragment:
        specs.append(("Q_NAME_ADDRESS_FRAGMENT", [holder_name, fragment]))
    if pincode:
        specs.append(("Q_NAME_PINCODE", [holder_name, pincode]))
    if country:
        specs.append(("Q4_NAME_COUNTRY", [holder_name, country]))
    return specs


def postal_name_specs(holder_name: str, postal) -> list[tuple[str, list[str]]]:
    specs: list[tuple[str, list[str]]] = []
    for index, item in enumerate(postal[:2], start=1):
        suffix = "" if index == 1 else f"_{index}"
        specs.append((f"Q_POSTAL_NAME{suffix}", [holder_name, item.value]))
        if item.location:
            specs.append(
                (f"Q_POSTAL_NAME_LOCATION{suffix}", [holder_name, item.location, item.value])
            )
    return specs


def distinct_holders(person: PersonRecord) -> list[tuple[int, str]]:
    """(field position, original full name) for each distinct populated holder.

    Positions: 1 = Name of the Holder, 2 = Joint 1, 3 = Joint-2. A holder that
    normalizes to an earlier one is the same holder and is dropped.
    """
    holders: list[tuple[int, str]] = []
    seen: set[str] = set()
    for position, value in enumerate((person.name, person.joint1, person.joint2), start=1):
        cleaned = " ".join((value or "").split())
        key = normalize_name(cleaned)
        if not key or key in seen:
            continue
        seen.add(key)
        holders.append((position, cleaned))
    return holders


def holder_name_for_plan(person: PersonRecord, plan: QueryPlan) -> str:
    """Display name for the holder associated with a planned query."""
    return dict(distinct_holders(person)).get(plan.holder, person.name.strip())


def holder_combination_specs(person: PersonRecord) -> list[tuple[str, list[str]]]:
    """Every unordered pair of distinct holders, plus all three when present."""
    holders = dict(distinct_holders(person))
    specs: list[tuple[str, list[str]]] = []
    for first, second in ((1, 2), (2, 3), (3, 1)):
        if first in holders and second in holders:
            specs.append(
                (f"Q_HOLDER_PAIR_{first}_{second}", [holders[first], holders[second]])
            )
    if len(holders) == 3:
        specs.append(("Q_ALL_HOLDERS", [holders[1], holders[2], holders[3]]))
    return specs
