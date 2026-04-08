"""
Google People API wrapper for contact search.
"""

import sys

from .auth import get_people_service


def search_contacts(alias: str, query: str) -> list[dict]:
    """
    Search contacts using Google People API.
    Returns list of contact dicts with name, emails, phones, organization.
    """
    service = get_people_service(alias)

    result = (
        service.people()
        .searchContacts(
            query=query,
            readMask="names,emailAddresses,phoneNumbers,organizations",
        )
        .execute()
    )

    results = result.get("results", [])
    contacts = []

    for item in results:
        person = item.get("person", {})

        # Extract name
        names = person.get("names", [])
        display_name = names[0].get("displayName", "") if names else ""

        # Extract email addresses
        email_addrs = [
            e.get("value", "") for e in person.get("emailAddresses", []) if e.get("value")
        ]

        # Extract phone numbers
        phones = [
            {"number": p.get("value", ""), "type": p.get("type", "")}
            for p in person.get("phoneNumbers", [])
            if p.get("value")
        ]

        # Extract organization
        orgs = person.get("organizations", [])
        org_name = orgs[0].get("name", "") if orgs else ""
        org_title = orgs[0].get("title", "") if orgs else ""

        contacts.append(
            {
                "name": display_name,
                "emails": email_addrs,
                "phones": phones,
                "organization": org_name,
                "title": org_title,
            }
        )

    return contacts
