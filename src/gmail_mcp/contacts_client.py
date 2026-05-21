"""Google People API wrapper for contact search."""

from .auth import get_people_service


def search_contacts(user_id: str, alias: str, query: str) -> list[dict]:
    service = get_people_service(user_id, alias)

    result = (
        service.people()
        .searchContacts(
            query=query,
            readMask="names,emailAddresses,phoneNumbers,organizations",
        )
        .execute()
    )

    contacts = []
    for item in result.get("results", []):
        person = item.get("person", {})

        names = person.get("names", [])
        display_name = names[0].get("displayName", "") if names else ""

        email_addrs = [
            e.get("value", "") for e in person.get("emailAddresses", []) if e.get("value")
        ]

        phones = [
            {"number": p.get("value", ""), "type": p.get("type", "")}
            for p in person.get("phoneNumbers", [])
            if p.get("value")
        ]

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
