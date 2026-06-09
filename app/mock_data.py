"""In-memory mock data for the CompassZero demo.

`org_name` is the join key between this data and the Auth0 token —
match the `org_name` you set on each Auth0 Organization to a
COMPANIES[i]["org_name"] entry below.

Resets on every uvicorn restart. Demo only.
"""

from typing import Optional


COMPANIES: list[dict] = [
    {"id": "co_north", "org_name": "northwind-corp", "display_name": "Northwind Corp",
     "budget": 250_000, "spent": 87_500, "currency": "USD"},
    {"id": "co_acme", "org_name": "acme-inc", "display_name": "Acme Inc",
     "budget": 120_000, "spent": 14_200, "currency": "USD"},
    {"id": "co_globex", "org_name": "globex-ltd", "display_name": "Globex Ltd",
     "budget": 400_000, "spent": 213_900, "currency": "EUR"},
]

TRAVEL_AGENTS: list[dict] = [
    {"id": "ag_alex",   "email": "alex@compasszero.com",   "name": "Alex Smith",      "org_name": "northwind-corp"},
    {"id": "ag_brett",  "email": "brett@compasszero.com",  "name": "Brett Lee",       "org_name": "northwind-corp"},
    {"id": "ag_camila", "email": "camila@compasszero.com", "name": "Camila Reyes",    "org_name": "acme-inc"},
    {"id": "ag_dana",   "email": "dana@compasszero.com",   "name": "Dana Park",       "org_name": "globex-ltd"},
]

CUSTOMERS: list[dict] = [
    {"id": "cu_jane",   "email": "jane@northwind.example",  "name": "Jane Doe",        "org_name": "northwind-corp", "agent_id": "ag_alex"},
    {"id": "cu_john",   "email": "john@northwind.example",  "name": "John Wong",       "org_name": "northwind-corp", "agent_id": "ag_alex"},
    {"id": "cu_lin",    "email": "lin@northwind.example",   "name": "Lin Chen",        "org_name": "northwind-corp", "agent_id": "ag_brett"},
    {"id": "cu_marco",  "email": "marco@acme.example",      "name": "Marco Bianchi",   "org_name": "acme-inc",       "agent_id": "ag_camila"},
    {"id": "cu_nadia",  "email": "nadia@acme.example",      "name": "Nadia Petrova",   "org_name": "acme-inc",       "agent_id": "ag_camila"},
    {"id": "cu_oscar",  "email": "oscar@globex.example",    "name": "Oscar Müller",    "org_name": "globex-ltd",     "agent_id": "ag_dana"},
    {"id": "cu_priya",  "email": "priya@globex.example",    "name": "Priya Sharma",    "org_name": "globex-ltd",     "agent_id": "ag_dana"},
    {"id": "cu_quinn",  "email": "quinn@globex.example",    "name": "Quinn O'Hara",    "org_name": "globex-ltd",     "agent_id": "ag_dana"},
]

TRIPS: list[dict] = [
    {"id": "tr_001", "customer_id": "cu_jane",  "type": "flight", "origin": "JFK", "destination": "LHR", "depart_date": "2026-07-15", "return_date": "2026-07-22", "cost": 1200, "currency": "USD", "status": "booked"},
    {"id": "tr_002", "customer_id": "cu_jane",  "type": "hotel",  "origin": "London", "destination": "London", "depart_date": "2026-07-15", "return_date": "2026-07-22", "cost": 1800, "currency": "USD", "status": "booked"},
    {"id": "tr_003", "customer_id": "cu_john",  "type": "flight", "origin": "SFO", "destination": "NRT", "depart_date": "2026-08-02", "return_date": "2026-08-12", "cost": 1750, "currency": "USD", "status": "booked"},
    {"id": "tr_004", "customer_id": "cu_lin",   "type": "train",  "origin": "PAR", "destination": "AMS", "depart_date": "2026-06-30", "return_date": "2026-07-03", "cost": 240,  "currency": "USD", "status": "completed"},
    {"id": "tr_005", "customer_id": "cu_marco", "type": "flight", "origin": "MXP", "destination": "FCO", "depart_date": "2026-09-04", "return_date": "2026-09-08", "cost": 320,  "currency": "USD", "status": "booked"},
    {"id": "tr_006", "customer_id": "cu_nadia", "type": "flight", "origin": "JFK", "destination": "CDG", "depart_date": "2026-10-11", "return_date": "2026-10-18", "cost": 980,  "currency": "USD", "status": "booked"},
    {"id": "tr_007", "customer_id": "cu_oscar", "type": "flight", "origin": "FRA", "destination": "SIN", "depart_date": "2026-07-20", "return_date": "2026-07-30", "cost": 1450, "currency": "EUR", "status": "booked"},
    {"id": "tr_008", "customer_id": "cu_oscar", "type": "hotel",  "origin": "Singapore", "destination": "Singapore", "depart_date": "2026-07-20", "return_date": "2026-07-30", "cost": 2100, "currency": "EUR", "status": "booked"},
    {"id": "tr_009", "customer_id": "cu_priya", "type": "flight", "origin": "BOM", "destination": "LHR", "depart_date": "2026-08-15", "return_date": "2026-08-25", "cost": 1100, "currency": "EUR", "status": "booked"},
    {"id": "tr_010", "customer_id": "cu_quinn", "type": "flight", "origin": "DUB", "destination": "JFK", "depart_date": "2026-06-10", "return_date": "2026-06-17", "cost": 720,  "currency": "EUR", "status": "completed"},
    {"id": "tr_011", "customer_id": "cu_quinn", "type": "hotel",  "origin": "New York", "destination": "New York", "depart_date": "2026-06-10", "return_date": "2026-06-17", "cost": 2300, "currency": "EUR", "status": "completed"},
    {"id": "tr_012", "customer_id": "cu_jane",  "type": "train",  "origin": "London", "destination": "Edinburgh", "depart_date": "2026-07-19", "return_date": "2026-07-20", "cost": 95,   "currency": "USD", "status": "booked"},
]

EXPERIENCES: list[dict] = [
    {"id": "ex_001", "customer_id": "cu_jane",  "trip_id": "tr_001", "name": "London Eye + Tower bridge tour",   "date": "2026-07-16", "cost": 65,  "location": "London"},
    {"id": "ex_002", "customer_id": "cu_jane",  "trip_id": "tr_001", "name": "West End theatre night",           "date": "2026-07-18", "cost": 110, "location": "London"},
    {"id": "ex_003", "customer_id": "cu_john",  "trip_id": "tr_003", "name": "Tsukiji food tour",                "date": "2026-08-04", "cost": 95,  "location": "Tokyo"},
    {"id": "ex_004", "customer_id": "cu_marco", "trip_id": "tr_005", "name": "Vatican private tour",             "date": "2026-09-06", "cost": 180, "location": "Rome"},
    {"id": "ex_005", "customer_id": "cu_oscar", "trip_id": "tr_007", "name": "Gardens by the Bay evening visit", "date": "2026-07-22", "cost": 40,  "location": "Singapore"},
    {"id": "ex_006", "customer_id": "cu_priya", "trip_id": "tr_009", "name": "Stonehenge day trip",              "date": "2026-08-18", "cost": 85,  "location": "Wiltshire"},
]


# ---------- read helpers ----------


def get_companies(org_name: Optional[str] = None) -> list[dict]:
    if org_name is None:
        return list(COMPANIES)
    return [c for c in COMPANIES if c["org_name"] == org_name]


def get_company(company_id: Optional[str] = None, org_name: Optional[str] = None) -> Optional[dict]:
    if company_id:
        return next((c for c in COMPANIES if c["id"] == company_id), None)
    if org_name:
        return next((c for c in COMPANIES if c["org_name"] == org_name), None)
    return None


def get_agents(org_name: Optional[str] = None) -> list[dict]:
    if org_name is None:
        return list(TRAVEL_AGENTS)
    return [a for a in TRAVEL_AGENTS if a["org_name"] == org_name]


def get_agent(agent_id: str) -> Optional[dict]:
    return next((a for a in TRAVEL_AGENTS if a["id"] == agent_id), None)


def get_customers(org_name: Optional[str] = None, agent_id: Optional[str] = None) -> list[dict]:
    out = list(CUSTOMERS)
    if org_name is not None:
        out = [c for c in out if c["org_name"] == org_name]
    if agent_id is not None:
        out = [c for c in out if c["agent_id"] == agent_id]
    return out


def get_customer(customer_id: str) -> Optional[dict]:
    return next((c for c in CUSTOMERS if c["id"] == customer_id), None)


def get_trips(
    customer_id: Optional[str] = None,
    org_name: Optional[str] = None,
) -> list[dict]:
    out = list(TRIPS)
    if customer_id is not None:
        out = [t for t in out if t["customer_id"] == customer_id]
    if org_name is not None:
        customer_ids_in_org = {c["id"] for c in CUSTOMERS if c["org_name"] == org_name}
        out = [t for t in out if t["customer_id"] in customer_ids_in_org]
    return out


def get_trip(trip_id: str) -> Optional[dict]:
    return next((t for t in TRIPS if t["id"] == trip_id), None)


def get_experiences_for_trip(trip_id: str) -> list[dict]:
    return [e for e in EXPERIENCES if e["trip_id"] == trip_id]


def get_experiences(customer_id: Optional[str] = None) -> list[dict]:
    out = list(EXPERIENCES)
    if customer_id is not None:
        out = [e for e in out if e["customer_id"] == customer_id]
    return out


# ---------- write helpers (used by chat tools) ----------


def _next_id(prefix: str, collection: list[dict]) -> str:
    n = len(collection) + 1
    return f"{prefix}{n:03d}"


def add_trip(
    customer_id: str,
    type: str,
    origin: str,
    destination: str,
    depart_date: str,
    return_date: str,
    cost: float,
    currency: str = "USD",
) -> dict:
    if not get_customer(customer_id):
        raise ValueError(f"Unknown customer_id: {customer_id}")
    trip = {
        "id": _next_id("tr_", TRIPS),
        "customer_id": customer_id,
        "type": type,
        "origin": origin,
        "destination": destination,
        "depart_date": depart_date,
        "return_date": return_date,
        "cost": float(cost),
        "currency": currency,
        "status": "booked",
    }
    TRIPS.append(trip)
    customer = get_customer(customer_id)
    if customer:
        company = get_company(org_name=customer["org_name"])
        if company:
            company["spent"] = company.get("spent", 0) + trip["cost"]
    return trip


def add_experience(
    customer_id: str,
    name: str,
    date: str,
    cost: float,
    trip_id: str = "",
    location: str = "",
) -> dict:
    if not get_customer(customer_id):
        raise ValueError(f"Unknown customer_id: {customer_id}")
    # trip_id is optional — standalone activities (e.g. a one-off
    # cooking class) don't have to be attached to a flight/trip.
    if trip_id and not get_trip(trip_id):
        raise ValueError(f"Unknown trip_id: {trip_id}")
    experience = {
        "id": _next_id("ex_", EXPERIENCES),
        "customer_id": customer_id,
        "trip_id": trip_id,
        "name": name,
        "date": date,
        "cost": float(cost),
        "location": location,
    }
    EXPERIENCES.append(experience)
    customer = get_customer(customer_id)
    if customer:
        company = get_company(org_name=customer["org_name"])
        if company:
            company["spent"] = company.get("spent", 0) + experience["cost"]
    return experience


def add_company(org_name: str, display_name: str, budget: float, currency: str = "USD") -> dict:
    if any(c["org_name"] == org_name for c in COMPANIES):
        raise ValueError(f"Company with org_name {org_name} already exists")
    company = {
        "id": _next_id("co_", COMPANIES),
        "org_name": org_name,
        "display_name": display_name,
        "budget": float(budget),
        "spent": 0.0,
        "currency": currency,
    }
    COMPANIES.append(company)
    return company


def add_customer(name: str, email: str, org_name: str, agent_id: Optional[str] = None) -> dict:
    if not get_company(org_name=org_name):
        raise ValueError(f"Unknown org_name: {org_name}")
    if agent_id and not get_agent(agent_id):
        raise ValueError(f"Unknown agent_id: {agent_id}")
    customer = {
        "id": _next_id("cu_", CUSTOMERS),
        "email": email,
        "name": name,
        "org_name": org_name,
        "agent_id": agent_id or "",
    }
    CUSTOMERS.append(customer)
    return customer
