"""Meta tools for BambooHR MCP server.

Implements:
- bamboo.meta.get_countries: List all supported countries
- bamboo.meta.get_states: List states/provinces for a country
- bamboo.meta.get_list_fields: List all list-type fields with options
- bamboo.meta.get_fields: List all field definitions
- bamboo.meta.get_field_options: Get options for a specific list field
- bamboo.meta.update_field_options: Manage list field options (create, update, archive)

Per BUILD_PLAN sections 3.2.10-3.2.11:
- Read endpoints are accessible to all personas
- Update endpoints require HR Admin only
"""

from typing import Annotated, Any

from constants import get_field_definition
from db import Employee, ListFieldOption, get_session
from mcp_auth import require_scopes, user_has_role
from mcp_schema import GeminiBaseModel as BaseModel
from pydantic import Field
from sqlalchemy import func, select

from .auth_helpers import get_user_context


class ValidationError(Exception):
    """Custom exception for validation errors that trigger rollback."""

    def __init__(self, code: int, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


# Static country data (ISO 3166-1 alpha-2 codes)
# Comprehensive list of commonly used countries
COUNTRIES: list[dict[str, str]] = [
    {"code": "US", "name": "United States"},
    {"code": "CA", "name": "Canada"},
    {"code": "GB", "name": "United Kingdom"},
    {"code": "AU", "name": "Australia"},
    {"code": "DE", "name": "Germany"},
    {"code": "FR", "name": "France"},
    {"code": "JP", "name": "Japan"},
    {"code": "CN", "name": "China"},
    {"code": "IN", "name": "India"},
    {"code": "BR", "name": "Brazil"},
    {"code": "MX", "name": "Mexico"},
    {"code": "ES", "name": "Spain"},
    {"code": "IT", "name": "Italy"},
    {"code": "NL", "name": "Netherlands"},
    {"code": "SE", "name": "Sweden"},
    {"code": "NO", "name": "Norway"},
    {"code": "DK", "name": "Denmark"},
    {"code": "FI", "name": "Finland"},
    {"code": "IE", "name": "Ireland"},
    {"code": "NZ", "name": "New Zealand"},
    {"code": "SG", "name": "Singapore"},
    {"code": "HK", "name": "Hong Kong"},
    {"code": "KR", "name": "South Korea"},
    {"code": "IL", "name": "Israel"},
    {"code": "CH", "name": "Switzerland"},
    {"code": "AT", "name": "Austria"},
    {"code": "BE", "name": "Belgium"},
    {"code": "PL", "name": "Poland"},
    {"code": "PT", "name": "Portugal"},
    {"code": "CZ", "name": "Czech Republic"},
    {"code": "ZA", "name": "South Africa"},
    {"code": "AE", "name": "United Arab Emirates"},
    {"code": "AR", "name": "Argentina"},
    {"code": "CL", "name": "Chile"},
    {"code": "CO", "name": "Colombia"},
    {"code": "PH", "name": "Philippines"},
    {"code": "TH", "name": "Thailand"},
    {"code": "MY", "name": "Malaysia"},
    {"code": "ID", "name": "Indonesia"},
    {"code": "VN", "name": "Vietnam"},
]

# Static state/province data by country code
# Key is country code (uppercase), value is list of states/provinces
STATES_BY_COUNTRY: dict[str, list[dict[str, str]]] = {
    "US": [
        {"code": "AL", "name": "Alabama"},
        {"code": "AK", "name": "Alaska"},
        {"code": "AZ", "name": "Arizona"},
        {"code": "AR", "name": "Arkansas"},
        {"code": "CA", "name": "California"},
        {"code": "CO", "name": "Colorado"},
        {"code": "CT", "name": "Connecticut"},
        {"code": "DE", "name": "Delaware"},
        {"code": "FL", "name": "Florida"},
        {"code": "GA", "name": "Georgia"},
        {"code": "HI", "name": "Hawaii"},
        {"code": "ID", "name": "Idaho"},
        {"code": "IL", "name": "Illinois"},
        {"code": "IN", "name": "Indiana"},
        {"code": "IA", "name": "Iowa"},
        {"code": "KS", "name": "Kansas"},
        {"code": "KY", "name": "Kentucky"},
        {"code": "LA", "name": "Louisiana"},
        {"code": "ME", "name": "Maine"},
        {"code": "MD", "name": "Maryland"},
        {"code": "MA", "name": "Massachusetts"},
        {"code": "MI", "name": "Michigan"},
        {"code": "MN", "name": "Minnesota"},
        {"code": "MS", "name": "Mississippi"},
        {"code": "MO", "name": "Missouri"},
        {"code": "MT", "name": "Montana"},
        {"code": "NE", "name": "Nebraska"},
        {"code": "NV", "name": "Nevada"},
        {"code": "NH", "name": "New Hampshire"},
        {"code": "NJ", "name": "New Jersey"},
        {"code": "NM", "name": "New Mexico"},
        {"code": "NY", "name": "New York"},
        {"code": "NC", "name": "North Carolina"},
        {"code": "ND", "name": "North Dakota"},
        {"code": "OH", "name": "Ohio"},
        {"code": "OK", "name": "Oklahoma"},
        {"code": "OR", "name": "Oregon"},
        {"code": "PA", "name": "Pennsylvania"},
        {"code": "RI", "name": "Rhode Island"},
        {"code": "SC", "name": "South Carolina"},
        {"code": "SD", "name": "South Dakota"},
        {"code": "TN", "name": "Tennessee"},
        {"code": "TX", "name": "Texas"},
        {"code": "UT", "name": "Utah"},
        {"code": "VT", "name": "Vermont"},
        {"code": "VA", "name": "Virginia"},
        {"code": "WA", "name": "Washington"},
        {"code": "WV", "name": "West Virginia"},
        {"code": "WI", "name": "Wisconsin"},
        {"code": "WY", "name": "Wyoming"},
        {"code": "DC", "name": "District of Columbia"},
    ],
    "CA": [
        {"code": "AB", "name": "Alberta"},
        {"code": "BC", "name": "British Columbia"},
        {"code": "MB", "name": "Manitoba"},
        {"code": "NB", "name": "New Brunswick"},
        {"code": "NL", "name": "Newfoundland and Labrador"},
        {"code": "NS", "name": "Nova Scotia"},
        {"code": "NT", "name": "Northwest Territories"},
        {"code": "NU", "name": "Nunavut"},
        {"code": "ON", "name": "Ontario"},
        {"code": "PE", "name": "Prince Edward Island"},
        {"code": "QC", "name": "Quebec"},
        {"code": "SK", "name": "Saskatchewan"},
        {"code": "YT", "name": "Yukon"},
    ],
    "GB": [
        {"code": "ENG", "name": "England"},
        {"code": "SCT", "name": "Scotland"},
        {"code": "WLS", "name": "Wales"},
        {"code": "NIR", "name": "Northern Ireland"},
    ],
    "AU": [
        {"code": "NSW", "name": "New South Wales"},
        {"code": "VIC", "name": "Victoria"},
        {"code": "QLD", "name": "Queensland"},
        {"code": "WA", "name": "Western Australia"},
        {"code": "SA", "name": "South Australia"},
        {"code": "TAS", "name": "Tasmania"},
        {"code": "ACT", "name": "Australian Capital Territory"},
        {"code": "NT", "name": "Northern Territory"},
    ],
    "DE": [
        {"code": "BW", "name": "Baden-Württemberg"},
        {"code": "BY", "name": "Bavaria"},
        {"code": "BE", "name": "Berlin"},
        {"code": "BB", "name": "Brandenburg"},
        {"code": "HB", "name": "Bremen"},
        {"code": "HH", "name": "Hamburg"},
        {"code": "HE", "name": "Hesse"},
        {"code": "MV", "name": "Mecklenburg-Vorpommern"},
        {"code": "NI", "name": "Lower Saxony"},
        {"code": "NW", "name": "North Rhine-Westphalia"},
        {"code": "RP", "name": "Rhineland-Palatinate"},
        {"code": "SL", "name": "Saarland"},
        {"code": "SN", "name": "Saxony"},
        {"code": "ST", "name": "Saxony-Anhalt"},
        {"code": "SH", "name": "Schleswig-Holstein"},
        {"code": "TH", "name": "Thuringia"},
    ],
    "MX": [
        {"code": "AGU", "name": "Aguascalientes"},
        {"code": "BCN", "name": "Baja California"},
        {"code": "BCS", "name": "Baja California Sur"},
        {"code": "CAM", "name": "Campeche"},
        {"code": "CHP", "name": "Chiapas"},
        {"code": "CHH", "name": "Chihuahua"},
        {"code": "COA", "name": "Coahuila"},
        {"code": "COL", "name": "Colima"},
        {"code": "CMX", "name": "Mexico City"},
        {"code": "DUR", "name": "Durango"},
        {"code": "GUA", "name": "Guanajuato"},
        {"code": "GRO", "name": "Guerrero"},
        {"code": "HID", "name": "Hidalgo"},
        {"code": "JAL", "name": "Jalisco"},
        {"code": "MEX", "name": "State of Mexico"},
        {"code": "MIC", "name": "Michoacán"},
        {"code": "MOR", "name": "Morelos"},
        {"code": "NAY", "name": "Nayarit"},
        {"code": "NLE", "name": "Nuevo León"},
        {"code": "OAX", "name": "Oaxaca"},
        {"code": "PUE", "name": "Puebla"},
        {"code": "QUE", "name": "Querétaro"},
        {"code": "ROO", "name": "Quintana Roo"},
        {"code": "SLP", "name": "San Luis Potosí"},
        {"code": "SIN", "name": "Sinaloa"},
        {"code": "SON", "name": "Sonora"},
        {"code": "TAB", "name": "Tabasco"},
        {"code": "TAM", "name": "Tamaulipas"},
        {"code": "TLA", "name": "Tlaxcala"},
        {"code": "VER", "name": "Veracruz"},
        {"code": "YUC", "name": "Yucatán"},
        {"code": "ZAC", "name": "Zacatecas"},
    ],
    "IN": [
        {"code": "AN", "name": "Andaman and Nicobar Islands"},
        {"code": "AP", "name": "Andhra Pradesh"},
        {"code": "AR", "name": "Arunachal Pradesh"},
        {"code": "AS", "name": "Assam"},
        {"code": "BR", "name": "Bihar"},
        {"code": "CH", "name": "Chandigarh"},
        {"code": "CT", "name": "Chhattisgarh"},
        {"code": "DL", "name": "Delhi"},
        {"code": "GA", "name": "Goa"},
        {"code": "GJ", "name": "Gujarat"},
        {"code": "HR", "name": "Haryana"},
        {"code": "HP", "name": "Himachal Pradesh"},
        {"code": "JK", "name": "Jammu and Kashmir"},
        {"code": "JH", "name": "Jharkhand"},
        {"code": "KA", "name": "Karnataka"},
        {"code": "KL", "name": "Kerala"},
        {"code": "MP", "name": "Madhya Pradesh"},
        {"code": "MH", "name": "Maharashtra"},
        {"code": "MN", "name": "Manipur"},
        {"code": "ML", "name": "Meghalaya"},
        {"code": "MZ", "name": "Mizoram"},
        {"code": "NL", "name": "Nagaland"},
        {"code": "OR", "name": "Odisha"},
        {"code": "PB", "name": "Punjab"},
        {"code": "RJ", "name": "Rajasthan"},
        {"code": "SK", "name": "Sikkim"},
        {"code": "TN", "name": "Tamil Nadu"},
        {"code": "TG", "name": "Telangana"},
        {"code": "TR", "name": "Tripura"},
        {"code": "UP", "name": "Uttar Pradesh"},
        {"code": "UK", "name": "Uttarakhand"},
        {"code": "WB", "name": "West Bengal"},
    ],
    "BR": [
        {"code": "AC", "name": "Acre"},
        {"code": "AL", "name": "Alagoas"},
        {"code": "AP", "name": "Amapá"},
        {"code": "AM", "name": "Amazonas"},
        {"code": "BA", "name": "Bahia"},
        {"code": "CE", "name": "Ceará"},
        {"code": "DF", "name": "Federal District"},
        {"code": "ES", "name": "Espírito Santo"},
        {"code": "GO", "name": "Goiás"},
        {"code": "MA", "name": "Maranhão"},
        {"code": "MT", "name": "Mato Grosso"},
        {"code": "MS", "name": "Mato Grosso do Sul"},
        {"code": "MG", "name": "Minas Gerais"},
        {"code": "PA", "name": "Pará"},
        {"code": "PB", "name": "Paraíba"},
        {"code": "PR", "name": "Paraná"},
        {"code": "PE", "name": "Pernambuco"},
        {"code": "PI", "name": "Piauí"},
        {"code": "RJ", "name": "Rio de Janeiro"},
        {"code": "RN", "name": "Rio Grande do Norte"},
        {"code": "RS", "name": "Rio Grande do Sul"},
        {"code": "RO", "name": "Rondônia"},
        {"code": "RR", "name": "Roraima"},
        {"code": "SC", "name": "Santa Catarina"},
        {"code": "SP", "name": "São Paulo"},
        {"code": "SE", "name": "Sergipe"},
        {"code": "TO", "name": "Tocantins"},
    ],
    # Countries without states/provinces (small countries, city-states, etc.)
    # Singapore, Hong Kong, etc. return empty list
}

# Static list field definitions with options
# Per BambooHR API: GET /v1/meta/lists/
LIST_FIELDS: list[dict[str, Any]] = [
    {
        "fieldId": 17,
        "alias": "department",
        "name": "Department",
        "manageable": "yes",
        "multiple": "no",
        "options": [
            {
                "id": 101,
                "name": "Engineering",
                "archived": "no",
                "createdDate": "2020-01-15T10:00:00Z",
                "archivedDate": None,
            },
            {
                "id": 102,
                "name": "Marketing",
                "archived": "no",
                "createdDate": "2020-01-15T10:00:00Z",
                "archivedDate": None,
            },
            {
                "id": 103,
                "name": "Sales",
                "archived": "no",
                "createdDate": "2020-01-15T10:00:00Z",
                "archivedDate": None,
            },
            {
                "id": 104,
                "name": "Human Resources",
                "archived": "no",
                "createdDate": "2020-01-15T10:00:00Z",
                "archivedDate": None,
            },
            {
                "id": 105,
                "name": "Finance",
                "archived": "no",
                "createdDate": "2020-01-15T10:00:00Z",
                "archivedDate": None,
            },
            {
                "id": 106,
                "name": "Operations (Old)",
                "archived": "yes",
                "createdDate": "2020-01-15T10:00:00Z",
                "archivedDate": "2023-06-01T10:00:00Z",
            },
        ],
    },
    {
        "fieldId": 18,
        "alias": "division",
        "name": "Division",
        "manageable": "yes",
        "multiple": "no",
        "options": [
            {
                "id": 201,
                "name": "North America",
                "archived": "no",
                "createdDate": "2020-01-15T10:00:00Z",
                "archivedDate": None,
            },
            {
                "id": 202,
                "name": "Europe",
                "archived": "no",
                "createdDate": "2020-01-15T10:00:00Z",
                "archivedDate": None,
            },
            {
                "id": 203,
                "name": "Asia Pacific",
                "archived": "no",
                "createdDate": "2020-01-15T10:00:00Z",
                "archivedDate": None,
            },
        ],
    },
    {
        "fieldId": 19,
        "alias": "location",
        "name": "Location",
        "manageable": "yes",
        "multiple": "no",
        "options": [
            {
                "id": 301,
                "name": "New York Office",
                "archived": "no",
                "createdDate": "2020-01-15T10:00:00Z",
                "archivedDate": None,
            },
            {
                "id": 302,
                "name": "San Francisco Office",
                "archived": "no",
                "createdDate": "2020-01-15T10:00:00Z",
                "archivedDate": None,
            },
            {
                "id": 303,
                "name": "London Office",
                "archived": "no",
                "createdDate": "2020-01-15T10:00:00Z",
                "archivedDate": None,
            },
            {
                "id": 304,
                "name": "Remote",
                "archived": "no",
                "createdDate": "2021-03-01T10:00:00Z",
                "archivedDate": None,
            },
            {
                "id": 305,
                "name": "Boston Office (Closed)",
                "archived": "yes",
                "createdDate": "2020-01-15T10:00:00Z",
                "archivedDate": "2022-12-31T10:00:00Z",
            },
        ],
    },
    {
        "fieldId": 20,
        "alias": "employmentStatus",
        "name": "Employment Status",
        "manageable": "no",
        "multiple": "no",
        "options": [
            {
                "id": 401,
                "name": "Full-Time",
                "archived": "no",
                "createdDate": "2020-01-15T10:00:00Z",
                "archivedDate": None,
            },
            {
                "id": 402,
                "name": "Part-Time",
                "archived": "no",
                "createdDate": "2020-01-15T10:00:00Z",
                "archivedDate": None,
            },
            {
                "id": 403,
                "name": "Contractor",
                "archived": "no",
                "createdDate": "2020-01-15T10:00:00Z",
                "archivedDate": None,
            },
            {
                "id": 404,
                "name": "Intern",
                "archived": "no",
                "createdDate": "2020-01-15T10:00:00Z",
                "archivedDate": None,
            },
        ],
    },
    {
        "fieldId": 21,
        "alias": "jobTitle",
        "name": "Job Title",
        "manageable": "yes",
        "multiple": "no",
        "options": [
            {
                "id": 501,
                "name": "Software Engineer",
                "archived": "no",
                "createdDate": "2020-01-15T10:00:00Z",
                "archivedDate": None,
            },
            {
                "id": 502,
                "name": "Senior Software Engineer",
                "archived": "no",
                "createdDate": "2020-01-15T10:00:00Z",
                "archivedDate": None,
            },
            {
                "id": 503,
                "name": "Engineering Manager",
                "archived": "no",
                "createdDate": "2020-01-15T10:00:00Z",
                "archivedDate": None,
            },
            {
                "id": 504,
                "name": "Product Manager",
                "archived": "no",
                "createdDate": "2020-01-15T10:00:00Z",
                "archivedDate": None,
            },
            {
                "id": 505,
                "name": "HR Coordinator",
                "archived": "no",
                "createdDate": "2020-01-15T10:00:00Z",
                "archivedDate": None,
            },
            {
                "id": 506,
                "name": "Junior Developer (Legacy)",
                "archived": "yes",
                "createdDate": "2020-01-15T10:00:00Z",
                "archivedDate": "2023-01-15T10:00:00Z",
            },
        ],
    },
]

# Static field definitions
# Per BambooHR API: GET /v1/meta/fields/
FIELD_DEFINITIONS: list[dict[str, Any]] = [
    {"id": 1, "name": "First name", "type": "text", "alias": "firstName"},
    {"id": 2, "name": "Last name", "type": "text", "alias": "lastName"},
    {"id": 3, "name": "Preferred name", "type": "text", "alias": "preferredName"},
    {"id": 4, "name": "Middle name", "type": "text", "alias": "middleName"},
    {"id": 5, "name": "Display name", "type": "text", "alias": "displayName"},
    {"id": 6, "name": "Employee number", "type": "text", "alias": "employeeNumber"},
    {"id": 7, "name": "Work email", "type": "email", "alias": "workEmail"},
    {"id": 8, "name": "Home email", "type": "email", "alias": "homeEmail"},
    {"id": 9, "name": "Work phone", "type": "phone", "alias": "workPhone"},
    {"id": 10, "name": "Work phone extension", "type": "text", "alias": "workPhoneExtension"},
    {"id": 11, "name": "Mobile phone", "type": "phone", "alias": "mobilePhone"},
    {"id": 12, "name": "Hire Date", "type": "date", "alias": "hireDate"},
    {"id": 13, "name": "Termination date", "type": "date", "alias": "terminationDate"},
    {"id": 14, "name": "Date of birth", "type": "date", "alias": "dateOfBirth"},
    {"id": 15, "name": "SSN", "type": "ssn", "alias": "ssn"},
    {"id": 16, "name": "Gender", "type": "gender", "alias": "gender"},
    {"id": 17, "name": "Department", "type": "list"},  # No alias - list field
    {"id": 18, "name": "Division", "type": "list"},  # No alias - list field
    {"id": 19, "name": "Location", "type": "list"},  # No alias - list field
    {"id": 20, "name": "Employment Status", "type": "list"},
    {"id": 21, "name": "Job Title", "type": "list"},
    {"id": 22, "name": "Supervisor", "type": "employee", "alias": "supervisorId"},
    {"id": 23, "name": "Photo", "type": "photo", "alias": "photoUrl"},
    {"id": 24, "name": "Address line 1", "type": "text", "alias": "address1"},
    {"id": 25, "name": "Address line 2", "type": "text", "alias": "address2"},
    {"id": 26, "name": "City", "type": "text", "alias": "city"},
    {"id": 27, "name": "State", "type": "state", "alias": "state"},
    {"id": 28, "name": "Zip code", "type": "text", "alias": "zipcode"},
    {"id": 29, "name": "Country", "type": "country", "alias": "country"},
    {"id": 30, "name": "LinkedIn", "type": "text", "alias": "linkedIn"},
    {"id": 31, "name": "Marital status", "type": "maritalStatus", "alias": "maritalStatus"},
    {"id": 32, "name": "Ethnicity", "type": "text", "alias": "ethnicity"},
    {"id": 33, "name": "Salary", "type": "currency", "alias": "salary"},
    {"id": 34, "name": "Pay rate", "type": "currency", "alias": "payRate"},
    {"id": 35, "name": "Pay per", "type": "text", "alias": "payPer"},
    {"id": 36, "name": "Pay type", "type": "payType", "alias": "payType"},
    {"id": 37, "name": "Pay schedule", "type": "text", "alias": "paySchedule"},
]


async def get_countries() -> dict[str, Any]:
    """List all supported countries."""
    return {"countries": list(COUNTRIES)}


async def get_states(country_code: str) -> dict[str, Any]:
    """List states/provinces for a country."""
    # Normalize country code to uppercase for case-insensitive lookup
    normalized_code = country_code.upper()

    states = STATES_BY_COUNTRY.get(normalized_code, [])
    return {"states": list(states)}


async def get_list_fields() -> list[dict[str, Any]]:
    """Retrieve all list-type fields with their options."""
    # Query all options from database, grouped by field_name
    db_options_by_field: dict[str, list[dict[str, Any]]] = {}

    try:
        async with get_session() as session:
            result = await session.execute(
                select(ListFieldOption).order_by(
                    ListFieldOption.field_name, ListFieldOption.sort_order, ListFieldOption.id
                )
            )
            db_options = result.scalars().all()

            for opt in db_options:
                if opt.field_name not in db_options_by_field:
                    db_options_by_field[opt.field_name] = []
                db_options_by_field[opt.field_name].append(
                    {
                        "id": opt.id,
                        "name": opt.option_value,
                        "archived": "yes" if opt.archived else "no",
                        "createdDate": None,  # DB model doesn't track this
                        "archivedDate": None,  # DB model doesn't track this
                    }
                )
    except Exception:
        # Table doesn't exist or other DB error - use static data only
        pass

    # Build result, using DB options when available, else static options
    result_fields = []
    for field in LIST_FIELDS:
        field_alias = field.get("alias", "")
        field_copy = dict(field)  # Don't modify the original

        # Check if we have DB options for this field
        if field_alias in db_options_by_field:
            field_copy["options"] = db_options_by_field[field_alias]

        result_fields.append(field_copy)

    return result_fields


async def get_fields() -> list[dict[str, Any]]:
    """Retrieve all standard and custom field definitions."""
    return list(FIELD_DEFINITIONS)


async def get_field_options(field_id: str) -> list[dict[str, str]]:
    """Retrieve options for a specific list field."""
    # Normalize field_id for case-insensitive lookup
    normalized_id = field_id.lower()

    # Find the matching list field to get its alias
    matched_field = None
    for field in LIST_FIELDS:
        field_alias = field.get("alias", "").lower()
        field_numeric_id = str(field.get("fieldId", ""))

        if normalized_id == field_alias or field_id == field_numeric_id:
            matched_field = field
            break

    if not matched_field:
        return []

    field_alias = matched_field.get("alias", "")

    # First try to get options from database
    try:
        async with get_session() as session:
            result = await session.execute(
                select(ListFieldOption)
                .where(
                    ListFieldOption.field_name == field_alias,
                    ListFieldOption.archived == False,  # noqa: E712
                )
                .order_by(ListFieldOption.sort_order, ListFieldOption.id)
            )
            db_options = result.scalars().all()

            if db_options:
                # Return DB options
                return [
                    {
                        "id": str(opt.id),
                        "name": opt.option_value,
                    }
                    for opt in db_options
                ]
    except Exception:
        # Table doesn't exist or other DB error - use static data
        pass

    # Fall back to static options if no DB options
    options = []
    for opt in matched_field.get("options", []):
        if opt.get("archived") != "yes":
            options.append(
                {
                    "id": str(opt["id"]),
                    "name": opt["name"],
                }
            )
    return options


class FieldOptionInput(BaseModel):
    """Field option for create, update, or archive operations.

    Uses strict mode to prevent type coercion (e.g., int to str).
    """

    model_config = {"strict": True}

    id: int | None = Field(
        None,
        description="Option ID from database - omit to create new option",
    )
    value: str | None = Field(
        None,
        description="Display value for the option (e.g., 'Engineering', 'Remote Office')",
    )
    archived: str | None = Field(
        None,
        description="Set to 'yes' to archive the option, 'no' to unarchive",
    )


class UpdateFieldOptionsInput(BaseModel):
    """Input for managing list field options (create, update, archive)."""

    list_field_id: str = Field(
        alias="listFieldId",
        description="Field alias like 'department' or 'jobTitle' (not the numeric fieldId)",
    )
    options: list[FieldOptionInput] = Field(
        description="List of options to create, update, or archive",
    )


class FieldOptionOutput(BaseModel):
    """Output model for a field option."""

    id: int
    value: str
    archived: bool | None = None


class UpdateFieldOptionsOutput(BaseModel):
    """Output model for update_field_options."""

    created: list[FieldOptionOutput] = Field(default_factory=list)
    updated: list[FieldOptionOutput] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


@require_scopes("write:metadata")
async def update_field_options(
    list_field_id: Annotated[str, Field(description="Field alias like 'department' or 'jobTitle'")],
    options: list[FieldOptionInput],
) -> dict[str, Any]:
    """Manage list field options (create, update, archive)."""
    # Validate input with Pydantic
    try:
        validated_input = UpdateFieldOptionsInput(
            listFieldId=list_field_id,
            options=options,
        )
    except Exception as e:
        return {
            "error": {
                "code": 422,
                "message": f"Invalid input: {str(e)}",
            }
        }

    # Check HR Admin permission
    _, persona = get_user_context()

    if persona != "hr_admin":
        return {
            "error": {
                "code": 403,
                "message": "Only HR Admin can modify field options",
            }
        }

    try:
        # Validate field exists and is list type (no DB query needed - static data)
        field_def = get_field_definition(list_field_id)

        if not field_def:
            raise ValidationError(404, f"List field '{list_field_id}' not found")

        if field_def.field_type not in ("list", "options"):
            raise ValidationError(422, f"Field '{list_field_id}' is not a list type")

        async with get_session() as session:
            # Check if field is manageable (can be modified by HR Admin)
            # Look up the field in LIST_FIELDS to check manageable property
            field_config = None
            for list_field in LIST_FIELDS:
                alias_match = list_field.get("alias") == list_field_id
                id_match = str(list_field.get("fieldId")) == list_field_id
                if alias_match or id_match:
                    field_config = list_field
                    break

            if field_config and field_config.get("manageable") == "no":
                raise ValidationError(
                    422,
                    f"Field '{list_field_id}' is not manageable and cannot be modified",
                )

            # Process each option
            created_options = []
            updated_options = []
            errors = []

            for option_input in validated_input.options:
                option_id = option_input.id
                option_value = option_input.value
                archived_str = option_input.archived
                was_truncated = False

                # Validate and normalize value
                if option_value is not None:
                    option_value = option_value.strip()
                    if not option_value:
                        raise ValidationError(422, "Option value cannot be empty")
                    # Truncate to 255 characters with warning
                    if len(option_value) > 255:
                        original_length = len(option_value)
                        option_value = option_value[:255]
                        was_truncated = True

                # Validate and normalize archived
                archived_bool = None
                if archived_str is not None:
                    archived_lower = archived_str.lower()
                    if archived_lower not in ("yes", "no"):
                        msg = f"Invalid archived value: must be 'yes' or 'no', got '{archived_str}'"
                        raise ValidationError(422, msg)
                    archived_bool = archived_lower == "yes"

                # Treat None or invalid IDs (<= 0) as create operation
                # Database IDs should always be > 0, so 0 or negative values are invalid
                if option_id is None or option_id <= 0:
                    # CREATE operation
                    if option_value is None:
                        raise ValidationError(422, "Option value is required for create operation")

                    # Check for duplicate value (case-insensitive)
                    stmt = select(ListFieldOption).where(
                        ListFieldOption.field_name == list_field_id,
                        func.lower(ListFieldOption.option_value) == option_value.lower(),
                    )
                    result = await session.execute(stmt)
                    existing = result.scalar_one_or_none()

                    if existing:
                        raise ValidationError(
                            422,
                            f"Option value '{option_value}' already exists for this field",
                        )

                    # Create new option
                    new_option = ListFieldOption(
                        field_name=list_field_id,
                        option_value=option_value,
                        archived=archived_bool if archived_bool is not None else False,
                        sort_order=0,
                    )
                    session.add(new_option)
                    await session.flush()  # Get the generated ID

                    created_option = {
                        "id": new_option.id,
                        "value": new_option.option_value,
                    }
                    if archived_bool is not None:
                        created_option["archived"] = new_option.archived
                    if was_truncated:
                        created_option["warning"] = (
                            f"Option value was truncated to 255 characters (was {original_length})"
                        )
                    created_options.append(created_option)

                else:
                    # UPDATE or ARCHIVE operation
                    # option_id is guaranteed to be > 0 here (handled in CREATE branch above)
                    stmt = select(ListFieldOption).where(
                        ListFieldOption.id == option_id,
                        ListFieldOption.field_name == list_field_id,
                    )
                    result = await session.execute(stmt)
                    existing_option = result.scalar_one_or_none()

                    if not existing_option:
                        raise ValidationError(422, f"Option ID {option_id} not found")

                    # Capture original value for employee usage check (before updating)
                    original_value = existing_option.option_value

                    # Check for duplicate value if updating value (case-insensitive)
                    if option_value is not None and option_value != existing_option.option_value:
                        stmt = select(ListFieldOption).where(
                            ListFieldOption.field_name == list_field_id,
                            func.lower(ListFieldOption.option_value) == option_value.lower(),
                            ListFieldOption.id != option_id,
                        )
                        result = await session.execute(stmt)
                        duplicate = result.scalar_one_or_none()

                        if duplicate:
                            msg = f"Option value '{option_value}' already exists for this field"
                            raise ValidationError(422, msg)

                        existing_option.option_value = option_value

                    # Update archived status
                    if archived_bool is not None:
                        existing_option.archived = archived_bool

                        # Check if option is in use by employees when archiving
                        if archived_bool:
                            # Map field_name to Employee model columns
                            field_column_map = {
                                "department": Employee.department,
                                "jobTitle": Employee.job_title,
                                "location": Employee.location,
                                "division": Employee.division,
                            }

                            # Count employees using the ORIGINAL option value
                            # (not the updated value, if value was changed in same request)
                            employee_count = 0
                            if list_field_id in field_column_map:
                                column = field_column_map[list_field_id]
                                count_stmt = select(func.count()).where(column == original_value)
                                count_result = await session.execute(count_stmt)
                                employee_count = count_result.scalar() or 0

                    await session.flush()

                    updated_option = {
                        "id": existing_option.id,
                        "value": existing_option.option_value,
                    }

                    # Collect warnings
                    warnings = []
                    if archived_bool is not None:
                        updated_option["archived"] = existing_option.archived
                        # Add warning if option is in use
                        if archived_bool and employee_count > 0:
                            warnings.append(f"Option is in use by {employee_count} employee(s)")

                    if was_truncated:
                        warnings.append(
                            f"Option value was truncated to 255 characters (was {original_length})"
                        )

                    if warnings:
                        updated_option["warning"] = "; ".join(warnings)

                    updated_options.append(updated_option)

            # Commit all changes (after processing all options)
            await session.commit()

            return {
                "created": created_options,
                "updated": updated_options,
                "errors": errors,
            }
    except ValidationError as e:
        # ValidationError triggers rollback, return error response
        return {
            "error": {
                "code": e.code,
                "message": e.message,
            }
        }
    except Exception as e:
        # Unexpected errors also trigger rollback
        return {
            "error": {
                "code": 500,
                "message": f"Failed to update field options: {str(e)}",
            }
        }


async def get_users(
    filter_emails_for_non_admins: bool = False,
) -> list[dict]:
    """Get all BambooHR users (employees with active status)."""
    async with get_session() as session:
        # Query active employees only
        stmt = select(Employee).where(Employee.status == "Active")
        result = await session.execute(stmt)
        employees = result.scalars().all()

        # Map to user format
        # Note: In BambooHR, "id" is the user ID (system user account)
        # and "employeeId" is the employee record ID.
        # For our mock system, we generate user IDs as "user_{employee_id}"
        users = []
        for emp in employees:
            user_dict = {
                "id": f"user_{emp.id}",
                "employeeId": str(emp.id),
                "firstName": emp.first_name,
                "lastName": emp.last_name,
            }

            # Include email only if not filtering or user is admin
            # user_has_role returns True if auth disabled
            if not filter_emails_for_non_admins or user_has_role("hr_admin"):
                user_dict["email"] = emp.work_email or ""

            users.append(user_dict)

        return users


__all__ = [
    "get_countries",
    "get_states",
    "get_list_fields",
    "get_fields",
    "get_field_options",
    "update_field_options",
    "get_users",
    "UpdateFieldOptionsInput",
    "UpdateFieldOptionsOutput",
]
