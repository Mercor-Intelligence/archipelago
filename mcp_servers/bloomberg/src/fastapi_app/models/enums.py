from enum import Enum


class ScreenType(str, Enum):
    """Supported screen types for BEQS requests."""

    EQS = "EQS"
    CUSTOM = "CUSTOM"


class Sector(str, Enum):
    """Supported sector enumeration (based on GICS sectors)."""

    ENERGY = "Energy"
    MATERIALS = "Materials"
    INDUSTRIALS = "Industrials"
    CONSUMER_DISCRETIONARY = "Consumer Discretionary"
    CONSUMER_STAPLES = "Consumer Staples"
    HEALTHCARE = "Health Care"
    FINANCIALS = "Financials"
    INFORMATION_TECHNOLOGY = "Information Technology"
    COMMUNICATION_SERVICES = "Communication Services"
    UTILITIES = "Utilities"
    REAL_ESTATE = "Real Estate"


class Industry(str, Enum):
    """Expanded enumeration of supported industries."""

    SOFTWARE = "Software"
    BANKING = "Banking"
    BIOTECH = "Biotech"
    PHARMACEUTICALS = "Pharmaceuticals"
    OIL_GAS = "Oil and Gas"
    AIRLINES = "Airlines"
    RETAIL = "Retail"
    SEMICONDUCTORS = "Semiconductors"
    TELECOMMUNICATIONS = "Telecommunications"
    ELECTRIC_UTILITIES = "Electric Utilities"
    INSURANCE = "Insurance"
    AUTOMOBILES = "Automobiles"


class PeriodicitySelection(str, Enum):
    """Periodicity selection for historical data."""

    DAILY = "DAILY"
    WEEKLY = "WEEKLY"
    MONTHLY = "MONTHLY"
    QUARTERLY = "QUARTERLY"
    SEMI_ANNUALLY = "SEMI_ANNUALLY"
    YEARLY = "YEARLY"


class PeriodicityAdjustment(str, Enum):
    """Periodicity adjustment options."""

    ACTUAL = "ACTUAL"
    CALENDAR = "CALENDAR"
    FISCAL = "FISCAL"


class NonTradingDayFillOption(str, Enum):
    """Options for handling non-trading days."""

    NON_TRADING_WEEKDAYS = "NON_TRADING_WEEKDAYS"
    ALL_CALENDAR_DAYS = "ALL_CALENDAR_DAYS"
    ACTIVE_DAYS_ONLY = "ACTIVE_DAYS_ONLY"


class NonTradingDayFillMethod(str, Enum):
    """Methods for filling non-trading day data."""

    PREVIOUS_VALUE = "PREVIOUS_VALUE"
    NIL_VALUE = "NIL_VALUE"


class PricingOption(str, Enum):
    """Pricing options."""

    PRICING_OPTION_PRICE = "PRICING_OPTION_PRICE"
    PRICING_OPTION_YIELD = "PRICING_OPTION_YIELD"


class OverrideOption(str, Enum):
    """Override options."""

    OVERRIDE_OPTION_CLOSE = "OVERRIDE_OPTION_CLOSE"
    OVERRIDE_OPTION_GPA = "OVERRIDE_OPTION_GPA"
