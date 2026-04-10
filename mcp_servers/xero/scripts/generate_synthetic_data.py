#!/usr/bin/env python3
"""
Generate synthetic data for Xero MCP Server Phase 2.

This script reads existing Phase 1 data (ContactIDs, AccountIDs, InvoiceIDs) from
synthetic_data.json and generates all Phase 2 entities per BUILD_PLAN_PHASE2.md
Section 9 requirements.

Requirements:
- Update Invoice dates for aging distribution (current, 30-day, 60-day, 90+ day)
- Add Budgets (5+), Journals (20+), BankTransfers (5+), Quotes (10+)
- Add PurchaseOrders (10+), CreditNotes (5+), Prepayments (5+), Overpayments (5+)
- Add Assets (10+), AssetTypes (3-5), Files (20+), Folders (3-5), Associations (10+)
- Add Projects (5+), TimeEntries (15+)
- Add Report data (AgedReceivablesByContact, AgedPayablesByContact, BudgetSummary, ExecutiveSummary)
"""

import json
import uuid
from datetime import datetime, timedelta
from pathlib import Path


def generate_uuid() -> str:
    """Generate a new UUID string."""
    return str(uuid.uuid4())


def xero_date(dt: datetime) -> str:
    """Convert datetime to Xero /Date() format."""
    timestamp_ms = int(dt.timestamp() * 1000)
    return f"/Date({timestamp_ms}+0000)/"


def iso_date(dt: datetime) -> str:
    """Convert datetime to ISO 8601 date string."""
    return dt.strftime("%Y-%m-%dT00:00:00")


def iso_datetime(dt: datetime) -> str:
    """Convert datetime to ISO 8601 datetime string."""
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


class SyntheticDataGenerator:
    """Generator for Phase 2 synthetic data."""

    def __init__(self, existing_data: dict):
        self.data = existing_data
        self.today = datetime(2025, 12, 6)  # Current date for aging calculations

        # Extract existing IDs for referential integrity
        self.contact_ids = [c["ContactID"] for c in self.data.get("Contacts", [])]
        self.account_ids = [a["AccountID"] for a in self.data.get("Accounts", [])]
        self.invoice_ids = [i["InvoiceID"] for i in self.data.get("Invoices", [])]

        # Get bank account IDs specifically
        self.bank_account_ids = [
            a["AccountID"] for a in self.data.get("Accounts", []) if a.get("Type") == "BANK"
        ]

        # Store contacts by type for proper assignment
        self.customer_contacts = [
            c
            for c in self.data.get("Contacts", [])
            if c.get("IsCustomer", False) and c.get("ContactStatus") == "ACTIVE"
        ]
        self.supplier_contacts = [
            c
            for c in self.data.get("Contacts", [])
            if c.get("IsSupplier", False) and c.get("ContactStatus") == "ACTIVE"
        ]

        # Generated IDs for cross-referencing
        self.folder_ids = []
        self.file_ids = []
        self.project_ids = []
        self.asset_type_ids = []
        self.quote_ids = []
        self.po_ids = []
        self.prepayment_ids = []
        self.overpayment_ids = []

    def update_invoice_dates_for_aging(self):
        """Update invoice dates to create aging distribution over 6+ months."""
        self.data.get("Invoices", [])

        # Create new invoices with proper aging buckets
        aging_invoices = []

        # Current (0-30 days overdue) - 3 invoices
        for i in range(3):
            days_ago = 10 + (i * 7)  # 10, 17, 24 days ago
            invoice_date = self.today - timedelta(days=days_ago + 30)
            due_date = self.today - timedelta(days=days_ago)

            contact = self.customer_contacts[i % len(self.customer_contacts)]
            aging_invoices.append(
                {
                    "Type": "ACCREC",
                    "InvoiceID": generate_uuid(),
                    "InvoiceNumber": f"INV-AGE-{1001 + i}",
                    "Status": "AUTHORISED",
                    "DateString": iso_date(invoice_date),
                    "Date": xero_date(invoice_date),
                    "DueDateString": iso_date(due_date),
                    "DueDate": xero_date(due_date),
                    "CurrencyCode": "USD",
                    "SubTotal": 1500.0 + (i * 250),
                    "TotalTax": 0.0,
                    "Total": 1500.0 + (i * 250),
                    "AmountDue": 1500.0 + (i * 250),
                    "AmountPaid": 0.0,
                    "AmountCredited": 0.0,
                    "Contact": {"ContactID": contact["ContactID"], "Name": contact["Name"]},
                    "LineItems": [
                        {
                            "Description": f"Professional Services - Current Aging {i + 1}",
                            "Quantity": 1,
                            "UnitAmount": 1500.0 + (i * 250),
                            "LineAmount": 1500.0 + (i * 250),
                            "AccountCode": "200",
                            "TaxType": "NONE",
                            "TaxAmount": 0.0,
                        }
                    ],
                }
            )

        # 30-60 days overdue - 3 invoices
        for i in range(3):
            days_ago = 35 + (i * 8)  # 35, 43, 51 days ago
            invoice_date = self.today - timedelta(days=days_ago + 30)
            due_date = self.today - timedelta(days=days_ago)

            contact = self.customer_contacts[i % len(self.customer_contacts)]
            aging_invoices.append(
                {
                    "Type": "ACCREC",
                    "InvoiceID": generate_uuid(),
                    "InvoiceNumber": f"INV-AGE-{1004 + i}",
                    "Status": "AUTHORISED",
                    "DateString": iso_date(invoice_date),
                    "Date": xero_date(invoice_date),
                    "DueDateString": iso_date(due_date),
                    "DueDate": xero_date(due_date),
                    "CurrencyCode": "USD",
                    "SubTotal": 2200.0 + (i * 300),
                    "TotalTax": 0.0,
                    "Total": 2200.0 + (i * 300),
                    "AmountDue": 2200.0 + (i * 300),
                    "AmountPaid": 0.0,
                    "AmountCredited": 0.0,
                    "Contact": {"ContactID": contact["ContactID"], "Name": contact["Name"]},
                    "LineItems": [
                        {
                            "Description": f"Consulting Services - 30-60 Day Aging {i + 1}",
                            "Quantity": 1,
                            "UnitAmount": 2200.0 + (i * 300),
                            "LineAmount": 2200.0 + (i * 300),
                            "AccountCode": "200",
                            "TaxType": "NONE",
                            "TaxAmount": 0.0,
                        }
                    ],
                }
            )

        # 60-90 days overdue - 2 invoices
        for i in range(2):
            days_ago = 65 + (i * 12)  # 65, 77 days ago
            invoice_date = self.today - timedelta(days=days_ago + 30)
            due_date = self.today - timedelta(days=days_ago)

            contact = self.customer_contacts[i % len(self.customer_contacts)]
            aging_invoices.append(
                {
                    "Type": "ACCREC",
                    "InvoiceID": generate_uuid(),
                    "InvoiceNumber": f"INV-AGE-{1007 + i}",
                    "Status": "AUTHORISED",
                    "DateString": iso_date(invoice_date),
                    "Date": xero_date(invoice_date),
                    "DueDateString": iso_date(due_date),
                    "DueDate": xero_date(due_date),
                    "CurrencyCode": "USD",
                    "SubTotal": 3500.0 + (i * 500),
                    "TotalTax": 0.0,
                    "Total": 3500.0 + (i * 500),
                    "AmountDue": 3500.0 + (i * 500),
                    "AmountPaid": 0.0,
                    "AmountCredited": 0.0,
                    "Contact": {"ContactID": contact["ContactID"], "Name": contact["Name"]},
                    "LineItems": [
                        {
                            "Description": f"Project Work - 60-90 Day Aging {i + 1}",
                            "Quantity": 1,
                            "UnitAmount": 3500.0 + (i * 500),
                            "LineAmount": 3500.0 + (i * 500),
                            "AccountCode": "200",
                            "TaxType": "NONE",
                            "TaxAmount": 0.0,
                        }
                    ],
                }
            )

        # 90+ days overdue - 3 invoices
        for i in range(3):
            days_ago = 95 + (i * 30)  # 95, 125, 155 days ago
            invoice_date = self.today - timedelta(days=days_ago + 30)
            due_date = self.today - timedelta(days=days_ago)

            contact = self.customer_contacts[i % len(self.customer_contacts)]
            aging_invoices.append(
                {
                    "Type": "ACCREC",
                    "InvoiceID": generate_uuid(),
                    "InvoiceNumber": f"INV-AGE-{1009 + i}",
                    "Status": "AUTHORISED",
                    "DateString": iso_date(invoice_date),
                    "Date": xero_date(invoice_date),
                    "DueDateString": iso_date(due_date),
                    "DueDate": xero_date(due_date),
                    "CurrencyCode": "USD",
                    "SubTotal": 4500.0 + (i * 750),
                    "TotalTax": 0.0,
                    "Total": 4500.0 + (i * 750),
                    "AmountDue": 4500.0 + (i * 750),
                    "AmountPaid": 0.0,
                    "AmountCredited": 0.0,
                    "Contact": {"ContactID": contact["ContactID"], "Name": contact["Name"]},
                    "LineItems": [
                        {
                            "Description": f"Legacy Services - 90+ Day Aging {i + 1}",
                            "Quantity": 1,
                            "UnitAmount": 4500.0 + (i * 750),
                            "LineAmount": 4500.0 + (i * 750),
                            "AccountCode": "200",
                            "TaxType": "NONE",
                            "TaxAmount": 0.0,
                        }
                    ],
                }
            )

        # Add aged ACCPAY (Bills) for payables aging
        aged_bills = []
        supplier = (
            self.supplier_contacts[0] if self.supplier_contacts else self.customer_contacts[0]
        )

        # Current bills (0-30 days)
        for i in range(2):
            days_ago = 15 + (i * 10)
            invoice_date = self.today - timedelta(days=days_ago + 30)
            due_date = self.today - timedelta(days=days_ago)

            aged_bills.append(
                {
                    "Type": "ACCPAY",
                    "InvoiceID": generate_uuid(),
                    "InvoiceNumber": f"BILL-AGE-{101 + i}",
                    "Status": "AUTHORISED",
                    "DateString": iso_date(invoice_date),
                    "Date": xero_date(invoice_date),
                    "DueDateString": iso_date(due_date),
                    "DueDate": xero_date(due_date),
                    "CurrencyCode": "USD",
                    "SubTotal": 800.0 + (i * 200),
                    "TotalTax": 0.0,
                    "Total": 800.0 + (i * 200),
                    "AmountDue": 800.0 + (i * 200),
                    "AmountPaid": 0.0,
                    "AmountCredited": 0.0,
                    "Contact": {"ContactID": supplier["ContactID"], "Name": supplier["Name"]},
                    "LineItems": [
                        {
                            "Description": f"Supplies - Current {i + 1}",
                            "Quantity": 1,
                            "UnitAmount": 800.0 + (i * 200),
                            "LineAmount": 800.0 + (i * 200),
                            "AccountCode": "400",
                            "TaxType": "NONE",
                            "TaxAmount": 0.0,
                        }
                    ],
                }
            )

        # 30-60 day bills
        for i in range(2):
            days_ago = 40 + (i * 10)
            invoice_date = self.today - timedelta(days=days_ago + 30)
            due_date = self.today - timedelta(days=days_ago)

            aged_bills.append(
                {
                    "Type": "ACCPAY",
                    "InvoiceID": generate_uuid(),
                    "InvoiceNumber": f"BILL-AGE-{103 + i}",
                    "Status": "AUTHORISED",
                    "DateString": iso_date(invoice_date),
                    "Date": xero_date(invoice_date),
                    "DueDateString": iso_date(due_date),
                    "DueDate": xero_date(due_date),
                    "CurrencyCode": "USD",
                    "SubTotal": 1200.0 + (i * 300),
                    "TotalTax": 0.0,
                    "Total": 1200.0 + (i * 300),
                    "AmountDue": 1200.0 + (i * 300),
                    "AmountPaid": 0.0,
                    "AmountCredited": 0.0,
                    "Contact": {"ContactID": supplier["ContactID"], "Name": supplier["Name"]},
                    "LineItems": [
                        {
                            "Description": f"Equipment Rental - 30-60 Day {i + 1}",
                            "Quantity": 1,
                            "UnitAmount": 1200.0 + (i * 300),
                            "LineAmount": 1200.0 + (i * 300),
                            "AccountCode": "400",
                            "TaxType": "NONE",
                            "TaxAmount": 0.0,
                        }
                    ],
                }
            )

        # 90+ day bills
        for i in range(2):
            days_ago = 100 + (i * 30)
            invoice_date = self.today - timedelta(days=days_ago + 30)
            due_date = self.today - timedelta(days=days_ago)

            aged_bills.append(
                {
                    "Type": "ACCPAY",
                    "InvoiceID": generate_uuid(),
                    "InvoiceNumber": f"BILL-AGE-{105 + i}",
                    "Status": "AUTHORISED",
                    "DateString": iso_date(invoice_date),
                    "Date": xero_date(invoice_date),
                    "DueDateString": iso_date(due_date),
                    "DueDate": xero_date(due_date),
                    "CurrencyCode": "USD",
                    "SubTotal": 2500.0 + (i * 500),
                    "TotalTax": 0.0,
                    "Total": 2500.0 + (i * 500),
                    "AmountDue": 2500.0 + (i * 500),
                    "AmountPaid": 0.0,
                    "AmountCredited": 0.0,
                    "Contact": {"ContactID": supplier["ContactID"], "Name": supplier["Name"]},
                    "LineItems": [
                        {
                            "Description": f"Professional Services - 90+ Day {i + 1}",
                            "Quantity": 1,
                            "UnitAmount": 2500.0 + (i * 500),
                            "LineAmount": 2500.0 + (i * 500),
                            "AccountCode": "400",
                            "TaxType": "NONE",
                            "TaxAmount": 0.0,
                        }
                    ],
                }
            )

        # Append new aging invoices to existing invoices
        self.data["Invoices"].extend(aging_invoices)
        self.data["Invoices"].extend(aged_bills)

        # Update invoice_ids for cross-referencing
        self.invoice_ids = [i["InvoiceID"] for i in self.data["Invoices"]]

    def generate_budgets(self):
        """Generate 5+ budgets with tracking categories."""
        budgets = []

        tracking_categories = [
            {"Name": "Region", "Option": "North", "TrackingCategoryID": generate_uuid()},
            {"Name": "Region", "Option": "South", "TrackingCategoryID": generate_uuid()},
            {"Name": "Region", "Option": "East", "TrackingCategoryID": generate_uuid()},
            {"Name": "Department", "Option": "Sales", "TrackingCategoryID": generate_uuid()},
            {"Name": "Department", "Option": "Marketing", "TrackingCategoryID": generate_uuid()},
        ]

        budget_descriptions = [
            "Q1 2025 Regional Budget - North",
            "Q1 2025 Regional Budget - South",
            "Annual Marketing Budget 2025",
            "Sales Department FY2025",
            "Operations Budget 2025",
            "R&D Investment Budget 2025",
        ]

        for i, desc in enumerate(budget_descriptions):
            budget_id = generate_uuid()
            budget_date = self.today - timedelta(days=30 * (i + 1))

            # Generate budget lines for 12 months
            budget_lines = []
            for month in range(12):
                month_date = datetime(2025, month + 1, 1)
                budget_lines.append(
                    {
                        "AccountID": self.account_ids[i % len(self.account_ids)],
                        "AccountCode": f"{200 + (i * 10)}",
                        "Period": month_date.strftime("%Y-%m-%dT00:00:00"),
                        "Amount": 10000.0 + (i * 1000) + (month * 500),
                    }
                )

            budgets.append(
                {
                    "BudgetID": budget_id,
                    "Type": "TRACKING" if i < 4 else "OVERALL",
                    "Description": desc,
                    "UpdatedDateUTC": iso_datetime(budget_date),
                    "Tracking": [tracking_categories[i % len(tracking_categories)]]
                    if i < 4
                    else [],
                    "BudgetLines": budget_lines,
                }
            )

        self.data["Budgets"] = budgets

    def generate_journals(self):
        """Generate 20+ journals with balanced debit/credit lines."""
        journals = []

        journal_types = [
            ("ACCRUAL", "Month-end accrual"),
            ("CASHREC", "Cash receipt"),
            ("CASHPAID", "Cash payment"),
            ("MANUAL", "Manual adjustment"),
            ("MANJOURNAL", "Manual journal entry"),
        ]

        for i in range(22):
            journal_id = generate_uuid()
            journal_date = self.today - timedelta(days=i * 5)
            source_type, description = journal_types[i % len(journal_types)]

            # Create balanced journal lines (debits = credits)
            amount = 1000.0 + (i * 150)
            journal_lines = [
                {
                    "JournalLineID": generate_uuid(),
                    "AccountID": self.account_ids[0],
                    "AccountCode": "090",
                    "AccountType": "BANK",
                    "AccountName": "Business Bank Account",
                    "Description": f"{description} - Debit",
                    "NetAmount": amount,
                    "GrossAmount": amount,
                    "TaxAmount": 0.0,
                    "TaxType": "NONE",
                    "TaxName": "No Tax",
                },
                {
                    "JournalLineID": generate_uuid(),
                    "AccountID": self.account_ids[1]
                    if len(self.account_ids) > 1
                    else self.account_ids[0],
                    "AccountCode": "200",
                    "AccountType": "REVENUE",
                    "AccountName": "Sales",
                    "Description": f"{description} - Credit",
                    "NetAmount": -amount,
                    "GrossAmount": -amount,
                    "TaxAmount": 0.0,
                    "TaxType": "NONE",
                    "TaxName": "No Tax",
                },
            ]

            journals.append(
                {
                    "JournalID": journal_id,
                    "JournalNumber": 1000 + i,
                    "JournalDate": iso_date(journal_date),
                    "CreatedDateUTC": xero_date(journal_date),
                    "Reference": f"JNL-{1000 + i}",
                    "SourceID": generate_uuid(),
                    "SourceType": source_type,
                    "JournalLines": journal_lines,
                }
            )

        self.data["Journals"] = journals

    def generate_bank_transfers(self):
        """Generate 5+ bank transfers between accounts."""
        bank_transfers = []

        # Ensure we have at least 2 bank accounts
        if len(self.bank_account_ids) < 2:
            # Add a second bank account if needed
            self.bank_account_ids.append(self.account_ids[0])

        transfer_descriptions = [
            "Transfer to savings",
            "Operating account funding",
            "Payroll account transfer",
            "Investment account deposit",
            "Emergency fund allocation",
            "Quarterly tax reserve",
        ]

        for i in range(6):
            transfer_id = generate_uuid()
            transfer_date = self.today - timedelta(days=i * 12)
            amount = 5000.0 + (i * 2500)

            from_account_id = self.bank_account_ids[0]
            to_account_id = self.bank_account_ids[1 % len(self.bank_account_ids)]

            bank_transfers.append(
                {
                    "BankTransferID": transfer_id,
                    "CreatedDateUTC": xero_date(transfer_date),
                    "Date": iso_date(transfer_date),
                    "Amount": amount,
                    "FromBankAccount": {
                        "AccountID": from_account_id,
                        "Code": "090",
                        "Name": "Business Bank Account",
                    },
                    "ToBankAccount": {
                        "AccountID": to_account_id,
                        "Code": "091",
                        "Name": "Test Bank Account",
                    },
                    "FromBankTransactionID": generate_uuid(),
                    "ToBankTransactionID": generate_uuid(),
                    "FromIsReconciled": i % 2 == 0,
                    "ToIsReconciled": i % 3 == 0,
                    "Reference": transfer_descriptions[i],
                }
            )

        self.data["BankTransfers"] = bank_transfers

    def generate_quotes(self):
        """Generate 10+ quotes with various statuses."""
        quotes = []
        statuses = ["DRAFT", "SENT", "ACCEPTED", "DECLINED", "INVOICED"]

        quote_items = [
            ("Website Development", 5000.0),
            ("Mobile App Design", 8000.0),
            ("SEO Optimization Package", 2500.0),
            ("Cloud Migration Services", 15000.0),
            ("Security Audit", 3500.0),
            ("Data Analytics Setup", 7500.0),
            ("CRM Implementation", 12000.0),
            ("Training Program", 4000.0),
            ("Support Contract", 6000.0),
            ("Custom Integration", 9000.0),
            ("Performance Optimization", 3000.0),
            ("Compliance Review", 5500.0),
        ]

        for i, (item_desc, base_amount) in enumerate(quote_items):
            quote_id = generate_uuid()
            self.quote_ids.append(quote_id)

            quote_date = self.today - timedelta(days=i * 8)
            expiry_date = quote_date + timedelta(days=30)
            contact = self.customer_contacts[i % len(self.customer_contacts)]
            status = statuses[i % len(statuses)]

            quotes.append(
                {
                    "QuoteID": quote_id,
                    "QuoteNumber": f"QU-{1001 + i}",
                    "Reference": f"REF-Q{i + 1}",
                    "Status": status,
                    "Contact": {"ContactID": contact["ContactID"], "Name": contact["Name"]},
                    "Date": iso_date(quote_date),
                    "ExpiryDate": iso_date(expiry_date),
                    "LineItems": [
                        {
                            "LineItemID": generate_uuid(),
                            "Description": item_desc,
                            "Quantity": 1,
                            "UnitAmount": base_amount,
                            "LineAmount": base_amount,
                            "AccountCode": "200",
                            "TaxType": "NONE",
                            "TaxAmount": 0.0,
                        }
                    ],
                    "SubTotal": base_amount,
                    "TotalTax": 0.0,
                    "Total": base_amount,
                    "CurrencyCode": "USD",
                    "Title": f"Quote for {item_desc}",
                    "Summary": f"Professional services quote for {item_desc.lower()}",
                    "Terms": "Payment due within 30 days of acceptance",
                    "UpdatedDateUTC": xero_date(quote_date),
                }
            )

        self.data["Quotes"] = quotes

    def generate_purchase_orders(self):
        """Generate 10+ purchase orders with various statuses."""
        purchase_orders = []
        statuses = ["DRAFT", "SUBMITTED", "AUTHORISED", "BILLED", "DELETED"]

        po_items = [
            ("Office Furniture", 3500.0),
            ("Computer Equipment", 8500.0),
            ("Software Licenses", 2000.0),
            ("Office Supplies", 500.0),
            ("Marketing Materials", 1500.0),
            ("Server Hardware", 12000.0),
            ("Networking Equipment", 4500.0),
            ("Security Systems", 6000.0),
            ("Printing Services", 800.0),
            ("Consulting Services", 5000.0),
            ("Training Materials", 1200.0),
            ("Maintenance Supplies", 750.0),
        ]

        supplier = (
            self.supplier_contacts[0] if self.supplier_contacts else self.customer_contacts[0]
        )

        for i, (item_desc, base_amount) in enumerate(po_items):
            po_id = generate_uuid()
            self.po_ids.append(po_id)

            po_date = self.today - timedelta(days=i * 7)
            delivery_date = po_date + timedelta(days=14)
            status = statuses[i % len(statuses)]

            purchase_orders.append(
                {
                    "PurchaseOrderID": po_id,
                    "PurchaseOrderNumber": f"PO-{2001 + i}",
                    "Status": status,
                    "Contact": {"ContactID": supplier["ContactID"], "Name": supplier["Name"]},
                    "Date": iso_date(po_date),
                    "DeliveryDate": iso_date(delivery_date),
                    "LineItems": [
                        {
                            "LineItemID": generate_uuid(),
                            "Description": item_desc,
                            "Quantity": 1,
                            "UnitAmount": base_amount,
                            "LineAmount": base_amount,
                            "AccountCode": "400",
                            "TaxType": "NONE",
                            "TaxAmount": 0.0,
                        }
                    ],
                    "SubTotal": base_amount,
                    "TotalTax": 0.0,
                    "Total": base_amount,
                    "CurrencyCode": "USD",
                    "Reference": f"Vendor Ref {i + 1}",
                    "AttentionTo": "Purchasing Department",
                    "DeliveryInstructions": "Deliver to main office",
                    "LineAmountTypes": "Exclusive",
                    "UpdatedDateUTC": xero_date(po_date),
                }
            )

        self.data["PurchaseOrders"] = purchase_orders

    def generate_credit_notes(self):
        """Generate 5+ credit notes (ACCRECCREDIT and ACCPAYCREDIT)."""
        credit_notes = []

        # Customer credit notes (ACCRECCREDIT)
        for i in range(3):
            cn_id = generate_uuid()
            cn_date = self.today - timedelta(days=i * 20)
            contact = self.customer_contacts[i % len(self.customer_contacts)]
            amount = 250.0 + (i * 100)

            credit_notes.append(
                {
                    "CreditNoteID": cn_id,
                    "CreditNoteNumber": f"CN-{3001 + i}",
                    "Type": "ACCRECCREDIT",
                    "Status": "AUTHORISED" if i < 2 else "PAID",
                    "Contact": {"ContactID": contact["ContactID"], "Name": contact["Name"]},
                    "Date": iso_date(cn_date),
                    "LineItems": [
                        {
                            "LineItemID": generate_uuid(),
                            "Description": f"Credit for returned goods {i + 1}",
                            "Quantity": 1,
                            "UnitAmount": amount,
                            "LineAmount": amount,
                            "AccountCode": "200",
                            "TaxType": "NONE",
                            "TaxAmount": 0.0,
                        }
                    ],
                    "LineAmountTypes": "Exclusive",
                    "SubTotal": amount,
                    "TotalTax": 0.0,
                    "Total": amount,
                    "RemainingCredit": amount if i < 2 else 0.0,
                    "CurrencyCode": "USD",
                    "Reference": f"Return #{i + 1}",
                    "UpdatedDateUTC": xero_date(cn_date),
                }
            )

        # Supplier credit notes (ACCPAYCREDIT)
        supplier = (
            self.supplier_contacts[0] if self.supplier_contacts else self.customer_contacts[0]
        )
        for i in range(3):
            cn_id = generate_uuid()
            cn_date = self.today - timedelta(days=i * 25)
            amount = 500.0 + (i * 150)

            credit_notes.append(
                {
                    "CreditNoteID": cn_id,
                    "CreditNoteNumber": f"CN-{3004 + i}",
                    "Type": "ACCPAYCREDIT",
                    "Status": "AUTHORISED",
                    "Contact": {"ContactID": supplier["ContactID"], "Name": supplier["Name"]},
                    "Date": iso_date(cn_date),
                    "LineItems": [
                        {
                            "LineItemID": generate_uuid(),
                            "Description": f"Supplier credit adjustment {i + 1}",
                            "Quantity": 1,
                            "UnitAmount": amount,
                            "LineAmount": amount,
                            "AccountCode": "400",
                            "TaxType": "NONE",
                            "TaxAmount": 0.0,
                        }
                    ],
                    "LineAmountTypes": "Exclusive",
                    "SubTotal": amount,
                    "TotalTax": 0.0,
                    "Total": amount,
                    "RemainingCredit": amount,
                    "CurrencyCode": "USD",
                    "Reference": f"Supplier Credit #{i + 1}",
                    "UpdatedDateUTC": xero_date(cn_date),
                }
            )

        self.data["CreditNotes"] = credit_notes

    def generate_prepayments(self):
        """Generate 5+ prepayments with allocations."""
        prepayments = []

        # Customer prepayments (RECEIVE-PREPAYMENT)
        for i in range(3):
            prepayment_id = generate_uuid()
            self.prepayment_ids.append(prepayment_id)

            pp_date = self.today - timedelta(days=i * 15)
            contact = self.customer_contacts[i % len(self.customer_contacts)]
            amount = 1000.0 + (i * 500)

            # Create allocation if we have invoices
            allocations = []
            if i < len(self.invoice_ids) and i > 0:
                allocations.append(
                    {
                        "AllocationID": generate_uuid(),
                        "Invoice": {"InvoiceID": self.invoice_ids[i], "InvoiceNumber": f"INV-{i}"},
                        "Amount": amount * 0.5,
                        "Date": iso_date(pp_date),
                    }
                )

            prepayments.append(
                {
                    "PrepaymentID": prepayment_id,
                    "Type": "RECEIVE-PREPAYMENT",
                    "Status": "AUTHORISED",
                    "Contact": {"ContactID": contact["ContactID"], "Name": contact["Name"]},
                    "Date": iso_date(pp_date),
                    "LineItems": [
                        {
                            "LineItemID": generate_uuid(),
                            "Description": f"Customer prepayment {i + 1}",
                            "Quantity": 1,
                            "UnitAmount": amount,
                            "LineAmount": amount,
                            "AccountCode": "200",
                            "TaxType": "NONE",
                            "TaxAmount": 0.0,
                        }
                    ],
                    "LineAmountTypes": "Exclusive",
                    "SubTotal": amount,
                    "TotalTax": 0.0,
                    "Total": amount,
                    "RemainingCredit": amount * 0.5 if allocations else amount,
                    "CurrencyCode": "USD",
                    "Allocations": allocations,
                    "Reference": f"Prepay-{i + 1}",
                    "UpdatedDateUTC": xero_date(pp_date),
                    "HasAttachments": False,
                }
            )

        # Supplier prepayments (SPEND-PREPAYMENT)
        supplier = (
            self.supplier_contacts[0] if self.supplier_contacts else self.customer_contacts[0]
        )
        for i in range(3):
            prepayment_id = generate_uuid()
            self.prepayment_ids.append(prepayment_id)

            pp_date = self.today - timedelta(days=i * 18)
            amount = 750.0 + (i * 250)

            prepayments.append(
                {
                    "PrepaymentID": prepayment_id,
                    "Type": "SPEND-PREPAYMENT",
                    "Status": "AUTHORISED",
                    "Contact": {"ContactID": supplier["ContactID"], "Name": supplier["Name"]},
                    "Date": iso_date(pp_date),
                    "LineItems": [
                        {
                            "LineItemID": generate_uuid(),
                            "Description": f"Supplier deposit {i + 1}",
                            "Quantity": 1,
                            "UnitAmount": amount,
                            "LineAmount": amount,
                            "AccountCode": "400",
                            "TaxType": "NONE",
                            "TaxAmount": 0.0,
                        }
                    ],
                    "LineAmountTypes": "Exclusive",
                    "SubTotal": amount,
                    "TotalTax": 0.0,
                    "Total": amount,
                    "RemainingCredit": amount,
                    "CurrencyCode": "USD",
                    "Allocations": [],
                    "Reference": f"Deposit-{i + 1}",
                    "UpdatedDateUTC": xero_date(pp_date),
                    "HasAttachments": False,
                }
            )

        self.data["Prepayments"] = prepayments

    def generate_overpayments(self):
        """Generate 5+ overpayments with allocations."""
        overpayments = []

        # Customer overpayments (RECEIVE-OVERPAYMENT)
        for i in range(3):
            overpayment_id = generate_uuid()
            self.overpayment_ids.append(overpayment_id)

            op_date = self.today - timedelta(days=i * 22)
            contact = self.customer_contacts[i % len(self.customer_contacts)]
            amount = 150.0 + (i * 75)

            # Create allocation for some
            allocations = []
            if i > 0 and i < len(self.invoice_ids):
                allocations.append(
                    {
                        "AllocationID": generate_uuid(),
                        "Invoice": {"InvoiceID": self.invoice_ids[i], "InvoiceNumber": f"INV-{i}"},
                        "Amount": amount * 0.6,
                        "Date": iso_date(op_date),
                    }
                )

            overpayments.append(
                {
                    "OverpaymentID": overpayment_id,
                    "Type": "RECEIVE-OVERPAYMENT",
                    "Status": "AUTHORISED",
                    "Contact": {"ContactID": contact["ContactID"], "Name": contact["Name"]},
                    "Date": iso_date(op_date),
                    "LineItems": [
                        {
                            "LineItemID": generate_uuid(),
                            "Description": f"Customer overpayment {i + 1}",
                            "Quantity": 1,
                            "UnitAmount": amount,
                            "LineAmount": amount,
                            "AccountCode": "800",
                            "TaxType": "NONE",
                            "TaxAmount": 0.0,
                        }
                    ],
                    "LineAmountTypes": "Exclusive",
                    "SubTotal": amount,
                    "TotalTax": 0.0,
                    "Total": amount,
                    "RemainingCredit": amount * 0.4 if allocations else amount,
                    "CurrencyCode": "USD",
                    "Allocations": allocations,
                    "UpdatedDateUTC": xero_date(op_date),
                    "HasAttachments": False,
                }
            )

        # Supplier overpayments (SPEND-OVERPAYMENT)
        supplier = (
            self.supplier_contacts[0] if self.supplier_contacts else self.customer_contacts[0]
        )
        for i in range(3):
            overpayment_id = generate_uuid()
            self.overpayment_ids.append(overpayment_id)

            op_date = self.today - timedelta(days=i * 28)
            amount = 200.0 + (i * 100)

            overpayments.append(
                {
                    "OverpaymentID": overpayment_id,
                    "Type": "SPEND-OVERPAYMENT",
                    "Status": "AUTHORISED",
                    "Contact": {"ContactID": supplier["ContactID"], "Name": supplier["Name"]},
                    "Date": iso_date(op_date),
                    "LineItems": [
                        {
                            "LineItemID": generate_uuid(),
                            "Description": f"Supplier overpayment {i + 1}",
                            "Quantity": 1,
                            "UnitAmount": amount,
                            "LineAmount": amount,
                            "AccountCode": "610",
                            "TaxType": "NONE",
                            "TaxAmount": 0.0,
                        }
                    ],
                    "LineAmountTypes": "Exclusive",
                    "SubTotal": amount,
                    "TotalTax": 0.0,
                    "Total": amount,
                    "RemainingCredit": amount,
                    "CurrencyCode": "USD",
                    "Allocations": [],
                    "UpdatedDateUTC": xero_date(op_date),
                    "HasAttachments": False,
                }
            )

        self.data["Overpayments"] = overpayments

    def generate_asset_types(self):
        """Generate 3-5 asset types with depreciation settings."""
        asset_types = []

        type_configs = [
            ("Computer Equipment", "StraightLine", 3, 0.0),
            ("Office Furniture", "DiminishingValue100", 10, 0.0),
            ("Vehicles", "DiminishingValue150", 5, 10000.0),
            ("Building Improvements", "StraightLine", 15, 0.0),
            ("Software", "StraightLine", 2, 0.0),
        ]

        for i, (name, method, life_years, residual) in enumerate(type_configs):
            type_id = generate_uuid()
            self.asset_type_ids.append(type_id)

            asset_types.append(
                {
                    "assetTypeId": type_id,
                    "assetTypeName": name,
                    "fixedAssetAccountId": self.account_ids[0],
                    "depreciationExpenseAccountId": self.account_ids[1]
                    if len(self.account_ids) > 1
                    else self.account_ids[0],
                    "accumulatedDepreciationAccountId": self.account_ids[0],
                    "bookDepreciationSetting": {
                        "depreciationMethod": method,
                        "averagingMethod": "ActualDays",
                        "depreciationRate": round(100.0 / life_years, 2),
                        "effectiveLifeYears": life_years,
                        "depreciationCalculationMethod": "None",
                        "residualValue": residual,
                    },
                    "locks": i + 1,  # Number of assets using this type
                }
            )

        self.data["AssetTypes"] = asset_types

    def generate_assets(self):
        """Generate 10+ fixed assets in various statuses."""
        assets = []
        statuses = ["Draft", "Registered", "Disposed"]

        asset_items = [
            ("Dell XPS Laptop", "Computer Equipment", 1500.0),
            ("MacBook Pro 16", "Computer Equipment", 2500.0),
            ("Conference Table", "Office Furniture", 800.0),
            ("Executive Desk", "Office Furniture", 1200.0),
            ("Herman Miller Chairs (Set)", "Office Furniture", 3500.0),
            ("Company Van", "Vehicles", 35000.0),
            ("Server Rack", "Computer Equipment", 5000.0),
            ("Office Renovation", "Building Improvements", 25000.0),
            ("Adobe Creative Suite", "Software", 600.0),
            ("Microsoft 365 Licenses", "Software", 1200.0),
            ("Backup Generator", "Computer Equipment", 8000.0),
            ("Security Camera System", "Computer Equipment", 2000.0),
        ]

        for i, (name, _type_name, price) in enumerate(asset_items):
            asset_id = generate_uuid()
            purchase_date = self.today - timedelta(days=180 + (i * 30))
            status = statuses[i % 3]

            # Find matching asset type
            asset_type_id = (
                self.asset_type_ids[i % len(self.asset_type_ids)]
                if self.asset_type_ids
                else generate_uuid()
            )

            # Calculate book value based on status and age
            months_old = (self.today - purchase_date).days / 30
            depreciation_rate = 0.1  # Simplified 10% annual
            book_value = price * (1 - (depreciation_rate * months_old / 12))
            book_value = max(0, book_value)

            asset_data = {
                "assetId": asset_id,
                "assetName": name,
                "assetNumber": f"FA-{4001 + i}",
                "purchaseDate": iso_date(purchase_date),
                "purchasePrice": price,
                "disposalPrice": 0.0,
                "assetStatus": status,
                "assetTypeId": asset_type_id,
                "accountingBookValue": round(book_value, 2),
                "canRollback": status == "Registered",
                "bookDepreciationSetting": {
                    "depreciationMethod": "StraightLine",
                    "depreciationRate": 10.0,
                    "effectiveLifeYears": 10,
                },
                "bookDepreciationDetail": {
                    "currentCapitalGain": 0.0,
                    "currentGainLoss": 0.0,
                    "depreciationStartDate": iso_date(purchase_date),
                    "costLimit": price,
                    "residualValue": 0.0,
                    "priorAccumDepreciationAmount": round(price - book_value, 2),
                    "currentAccumDepreciationAmount": round(price - book_value, 2),
                },
            }

            if status == "Disposed":
                disposal_date = self.today - timedelta(days=i * 10)
                asset_data["disposalDate"] = iso_date(disposal_date)
                asset_data["disposalPrice"] = round(book_value * 0.3, 2)

            if i % 3 == 0:
                asset_data["serialNumber"] = f"SN-{10000 + i}"
                asset_data["warrantyExpiryDate"] = iso_date(purchase_date + timedelta(days=365))

            if i % 4 == 0:
                asset_data["description"] = f"Asset description for {name}"

            assets.append(asset_data)

        self.data["Assets"] = assets

    def generate_folders(self):
        """Generate 3-5 folders including Inbox."""
        folders = []

        folder_configs = [
            ("Inbox", True, "inbox@files.xero.com"),
            ("Invoices", False, None),
            ("Receipts", False, None),
            ("Contracts", False, None),
            ("Reports", False, None),
        ]

        for i, (name, is_inbox, email) in enumerate(folder_configs):
            folder_id = generate_uuid()
            self.folder_ids.append(folder_id)

            folder_data = {
                "Id": folder_id,
                "Name": name,
                "FileCount": 4 + i,  # Will be updated after files are generated
                "IsInbox": is_inbox,
            }

            if email:
                folder_data["Email"] = email

            folders.append(folder_data)

        self.data["Folders"] = folders

    def generate_files(self):
        """Generate 20+ files with various mime types."""
        files = []

        file_configs = [
            ("invoice_001.pdf", "application/pdf", 125000),
            ("receipt_feb_2025.pdf", "application/pdf", 45000),
            ("contract_signed.pdf", "application/pdf", 890000),
            (
                "expense_report_q1.xlsx",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                56000,
            ),
            (
                "budget_2025.xlsx",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                78000,
            ),
            (
                "meeting_notes.docx",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                34000,
            ),
            (
                "project_proposal.docx",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                120000,
            ),
            ("company_logo.png", "image/png", 25000),
            ("product_photo_1.jpg", "image/jpeg", 450000),
            ("product_photo_2.jpg", "image/jpeg", 380000),
            ("bank_statement_nov.pdf", "application/pdf", 67000),
            ("bank_statement_oct.pdf", "application/pdf", 72000),
            ("tax_return_2024.pdf", "application/pdf", 234000),
            ("audit_report.pdf", "application/pdf", 567000),
            ("employee_handbook.pdf", "application/pdf", 1200000),
            ("quarterly_report_q3.pdf", "application/pdf", 345000),
            ("vendor_agreement.pdf", "application/pdf", 189000),
            ("insurance_certificate.pdf", "application/pdf", 156000),
            ("lease_agreement.pdf", "application/pdf", 278000),
            ("payroll_summary.csv", "text/csv", 12000),
            ("inventory_list.csv", "text/csv", 8500),
            ("customer_contacts.csv", "text/csv", 15000),
        ]

        for i, (name, mime_type, size) in enumerate(file_configs):
            file_id = generate_uuid()
            self.file_ids.append(file_id)

            created_date = self.today - timedelta(days=i * 5)
            folder_id = (
                self.folder_ids[i % len(self.folder_ids)] if self.folder_ids else generate_uuid()
            )

            files.append(
                {
                    "Id": file_id,
                    "Name": name,
                    "MimeType": mime_type,
                    "Size": size,
                    "CreatedDateUtc": iso_datetime(created_date),
                    "UpdatedDateUtc": iso_datetime(created_date + timedelta(hours=i)),
                    "FolderId": folder_id,
                    "User": {
                        "Id": generate_uuid(),
                        "Name": "Test User",
                        "FirstName": "Test",
                        "LastName": "User",
                        "Email": "test@example.com",
                    },
                }
            )

        self.data["Files"] = files

        # Update folder file counts
        for folder in self.data["Folders"]:
            folder["FileCount"] = sum(1 for f in files if f["FolderId"] == folder["Id"])

    def generate_associations(self):
        """Generate 10+ file-to-object associations."""
        associations = []

        object_types = [
            ("Invoice", "Account"),
            ("Contact", "Contact"),
            ("CreditNote", "Account"),
            ("Quote", "Account"),
            ("PurchaseOrder", "Account"),
        ]

        # Associate files with invoices, contacts, etc.
        for i in range(12):
            file_id = self.file_ids[i % len(self.file_ids)] if self.file_ids else generate_uuid()
            object_type, object_group = object_types[i % len(object_types)]

            # Get appropriate object ID based on type
            if object_type == "Invoice":
                object_id = (
                    self.invoice_ids[i % len(self.invoice_ids)]
                    if self.invoice_ids
                    else generate_uuid()
                )
            elif object_type == "Contact":
                object_id = (
                    self.contact_ids[i % len(self.contact_ids)]
                    if self.contact_ids
                    else generate_uuid()
                )
            elif object_type == "Quote":
                object_id = (
                    self.quote_ids[i % len(self.quote_ids)] if self.quote_ids else generate_uuid()
                )
            elif object_type == "PurchaseOrder":
                object_id = self.po_ids[i % len(self.po_ids)] if self.po_ids else generate_uuid()
            else:
                object_id = generate_uuid()

            associations.append(
                {
                    "FileId": file_id,
                    "ObjectId": object_id,
                    "ObjectType": object_type,
                    "ObjectGroup": object_group,
                }
            )

        self.data["Associations"] = associations

    def generate_projects(self):
        """Generate 5+ projects with various statuses."""
        projects = []
        statuses = ["INPROGRESS", "CLOSED"]

        project_configs = [
            ("Website Redesign", 24000, 480),
            ("Mobile App Development", 85000, 1200),
            ("CRM Implementation", 45000, 600),
            ("Data Migration", 18000, 240),
            ("Security Audit", 12000, 160),
            ("Cloud Infrastructure Setup", 35000, 420),
        ]

        for i, (name, estimate, minutes) in enumerate(project_configs):
            project_id = generate_uuid()
            self.project_ids.append(project_id)

            contact = self.customer_contacts[i % len(self.customer_contacts)]
            status = statuses[i % 2]
            deadline = self.today + timedelta(days=30 * (i + 1))

            # Calculate amounts
            task_amount = estimate * 0.6 if status == "INPROGRESS" else estimate
            expense_amount = estimate * 0.1
            invoiced = task_amount * 0.5 if status == "INPROGRESS" else task_amount

            projects.append(
                {
                    "projectId": project_id,
                    "contactId": contact["ContactID"],
                    "name": name,
                    "currencyCode": "USD",
                    "minutesLogged": minutes if status == "CLOSED" else int(minutes * 0.7),
                    "totalTaskAmount": {"currency": "USD", "value": task_amount},
                    "totalExpenseAmount": {"currency": "USD", "value": expense_amount},
                    "minutesToBeInvoiced": 0 if status == "CLOSED" else int(minutes * 0.3),
                    "estimate": {"currency": "USD", "value": estimate},
                    "status": status,
                    "deadlineUtc": iso_datetime(deadline),
                    "totalInvoiced": {"currency": "USD", "value": invoiced},
                    "totalToBeInvoiced": {"currency": "USD", "value": task_amount - invoiced},
                    "deposit": {"currency": "USD", "value": estimate * 0.2},
                }
            )

        self.data["Projects"] = projects

    def generate_time_entries(self):
        """Generate 15+ time entries across projects."""
        time_entries = []

        user_id = generate_uuid()
        task_ids = [generate_uuid() for _ in range(5)]

        descriptions = [
            "Initial project planning",
            "Requirements gathering",
            "Design mockups",
            "Frontend development",
            "Backend API implementation",
            "Database schema design",
            "Unit testing",
            "Integration testing",
            "Code review",
            "Documentation",
            "Client meeting",
            "Bug fixes",
            "Performance optimization",
            "Deployment preparation",
            "User training",
            "Support and maintenance",
        ]

        statuses = ["ACTIVE", "LOCKED", "INVOICED"]

        for i in range(18):
            entry_date = self.today - timedelta(days=i * 2)
            project_id = (
                self.project_ids[i % len(self.project_ids)] if self.project_ids else generate_uuid()
            )

            time_entries.append(
                {
                    "timeEntryId": generate_uuid(),
                    "userId": user_id,
                    "projectId": project_id,
                    "taskId": task_ids[i % len(task_ids)],
                    "dateUtc": iso_datetime(entry_date),
                    "duration": 60 + (i * 15),  # 60-330 minutes
                    "description": descriptions[i % len(descriptions)],
                    "status": statuses[i % len(statuses)],
                }
            )

        self.data["TimeEntries"] = time_entries

    def generate_reports(self):
        """Generate pre-computed report data for aging, budget, and executive summary."""
        reports = self.data.get("Reports", {})

        # Aged Receivables By Contact reports
        aged_receivables = {}
        for contact in self.customer_contacts:
            contact_id = contact["ContactID"]
            contact_name = contact["Name"]

            # Find receivables for this contact
            contact_invoices = [
                inv
                for inv in self.data.get("Invoices", [])
                if inv.get("Type") == "ACCREC"
                and inv.get("Contact", {}).get("ContactID") == contact_id
                and inv.get("Status") in ["AUTHORISED", "SUBMITTED"]
                and inv.get("AmountDue", 0) > 0
            ]

            if contact_invoices:
                rows = [
                    {
                        "RowType": "Header",
                        "Cells": [
                            {"Value": "Date"},
                            {"Value": "Reference"},
                            {"Value": "Due Date"},
                            {"Value": ""},
                            {"Value": "Total"},
                            {"Value": "Paid"},
                            {"Value": "Credited"},
                            {"Value": "Due"},
                        ],
                    }
                ]

                section_rows = []
                total_due = 0.0

                for inv in contact_invoices:
                    total_due += inv.get("AmountDue", 0)
                    section_rows.append(
                        {
                            "RowType": "Row",
                            "Cells": [
                                {"Value": inv.get("DateString", "")},
                                {"Value": inv.get("InvoiceNumber", "")},
                                {"Value": inv.get("DueDateString", "")},
                                {"Value": ""},
                                {"Value": f"{inv.get('Total', 0):.2f}"},
                                {"Value": f"{inv.get('AmountPaid', 0):.2f}"},
                                {"Value": f"{inv.get('AmountCredited', 0):.2f}"},
                                {"Value": f"{inv.get('AmountDue', 0):.2f}"},
                            ],
                        }
                    )

                rows.append({"RowType": "Section", "Title": contact_name, "Rows": section_rows})

                rows.append(
                    {
                        "RowType": "SummaryRow",
                        "Cells": [
                            {"Value": "Total"},
                            {"Value": ""},
                            {"Value": ""},
                            {"Value": ""},
                            {"Value": ""},
                            {"Value": ""},
                            {"Value": ""},
                            {"Value": f"{total_due:.2f}"},
                        ],
                    }
                )

                aged_receivables[contact_id] = {
                    "ReportID": "AgedReceivablesByContact",
                    "ReportName": "Aged Receivables By Contact",
                    "ReportType": "AgedReceivablesByContact",
                    "ReportTitles": [
                        "Aged Receivables",
                        contact_name,
                        f"As at {self.today.strftime('%d %B %Y')}",
                    ],
                    "ReportDate": self.today.strftime("%d %B %Y"),
                    "UpdatedDateUTC": xero_date(self.today),
                    "Rows": rows,
                }

        reports["AgedReceivablesByContact"] = aged_receivables

        # Aged Payables By Contact reports
        aged_payables = {}
        for contact in (
            self.supplier_contacts if self.supplier_contacts else self.customer_contacts[:2]
        ):
            contact_id = contact["ContactID"]
            contact_name = contact["Name"]

            # Find payables for this contact
            contact_bills = [
                inv
                for inv in self.data.get("Invoices", [])
                if inv.get("Type") == "ACCPAY"
                and inv.get("Contact", {}).get("ContactID") == contact_id
                and inv.get("Status") in ["AUTHORISED", "SUBMITTED"]
                and inv.get("AmountDue", 0) > 0
            ]

            if contact_bills:
                rows = [
                    {
                        "RowType": "Header",
                        "Cells": [
                            {"Value": "Date"},
                            {"Value": "Reference"},
                            {"Value": "Due Date"},
                            {"Value": ""},
                            {"Value": "Total"},
                            {"Value": "Paid"},
                            {"Value": "Credited"},
                            {"Value": "Due"},
                        ],
                    }
                ]

                section_rows = []
                total_due = 0.0

                for inv in contact_bills:
                    total_due += inv.get("AmountDue", 0)
                    section_rows.append(
                        {
                            "RowType": "Row",
                            "Cells": [
                                {"Value": inv.get("DateString", "")},
                                {"Value": inv.get("InvoiceNumber", "")},
                                {"Value": inv.get("DueDateString", "")},
                                {"Value": ""},
                                {"Value": f"{inv.get('Total', 0):.2f}"},
                                {"Value": f"{inv.get('AmountPaid', 0):.2f}"},
                                {"Value": f"{inv.get('AmountCredited', 0):.2f}"},
                                {"Value": f"{inv.get('AmountDue', 0):.2f}"},
                            ],
                        }
                    )

                rows.append({"RowType": "Section", "Title": contact_name, "Rows": section_rows})

                rows.append(
                    {
                        "RowType": "SummaryRow",
                        "Cells": [
                            {"Value": "Total"},
                            {"Value": ""},
                            {"Value": ""},
                            {"Value": ""},
                            {"Value": ""},
                            {"Value": ""},
                            {"Value": ""},
                            {"Value": f"{total_due:.2f}"},
                        ],
                    }
                )

                aged_payables[contact_id] = {
                    "ReportID": "AgedPayablesByContact",
                    "ReportName": "Aged Payables By Contact",
                    "ReportType": "AgedPayablesByContact",
                    "ReportTitles": [
                        "Aged Payables",
                        contact_name,
                        f"As at {self.today.strftime('%d %B %Y')}",
                    ],
                    "ReportDate": self.today.strftime("%d %B %Y"),
                    "UpdatedDateUTC": xero_date(self.today),
                    "Rows": rows,
                }

        reports["AgedPayablesByContact"] = aged_payables

        # Budget Summary Report (12 months)
        budget_rows = [
            {
                "RowType": "Header",
                "Cells": [
                    {"Value": "Account"},
                    {"Value": "Budget"},
                    {"Value": "Actual"},
                    {"Value": "Variance"},
                ],
            }
        ]

        budget_accounts = [
            ("Sales Revenue", 120000.0, 115000.0),
            ("Service Revenue", 80000.0, 85000.0),
            ("Cost of Goods Sold", 45000.0, 48000.0),
            ("Salaries", 60000.0, 62000.0),
            ("Rent", 24000.0, 24000.0),
            ("Utilities", 6000.0, 5500.0),
            ("Marketing", 15000.0, 18000.0),
            ("Office Supplies", 3000.0, 2800.0),
            ("Professional Fees", 8000.0, 7500.0),
            ("Depreciation", 12000.0, 12000.0),
        ]

        for account, budget, actual in budget_accounts:
            variance = actual - budget
            budget_rows.append(
                {
                    "RowType": "Row",
                    "Cells": [
                        {"Value": account},
                        {"Value": f"{budget:.2f}"},
                        {"Value": f"{actual:.2f}"},
                        {"Value": f"{variance:.2f}"},
                    ],
                }
            )

        total_budget = sum(b for _, b, _ in budget_accounts)
        total_actual = sum(a for _, _, a in budget_accounts)

        budget_rows.append(
            {
                "RowType": "SummaryRow",
                "Cells": [
                    {"Value": "Total"},
                    {"Value": f"{total_budget:.2f}"},
                    {"Value": f"{total_actual:.2f}"},
                    {"Value": f"{total_actual - total_budget:.2f}"},
                ],
            }
        )

        reports["BudgetSummary"] = {
            "ReportID": "BudgetSummary",
            "ReportName": "Budget Summary",
            "ReportType": "BudgetSummary",
            "ReportTitles": [
                "Budget Summary",
                "Demo Company (US)",
                f"For the year ended {self.today.strftime('%d %B %Y')}",
            ],
            "ReportDate": self.today.strftime("%d %B %Y"),
            "UpdatedDateUTC": xero_date(self.today),
            "Rows": budget_rows,
        }

        # Executive Summary Report
        reports["ExecutiveSummary"] = {
            "ReportID": "ExecutiveSummary",
            "ReportName": "Executive Summary",
            "ReportType": "ExecutiveSummary",
            "ReportTitles": [
                "Executive Summary",
                "Demo Company (US)",
                f"As at {self.today.strftime('%d %B %Y')}",
            ],
            "ReportDate": self.today.strftime("%d %B %Y"),
            "UpdatedDateUTC": xero_date(self.today),
            "Rows": [
                {
                    "RowType": "Section",
                    "Title": "Cash",
                    "Rows": [
                        {
                            "RowType": "Row",
                            "Cells": [{"Value": "Cash in Bank"}, {"Value": "125000.00"}],
                        },
                        {
                            "RowType": "Row",
                            "Cells": [{"Value": "Cash in Hand"}, {"Value": "5000.00"}],
                        },
                        {
                            "RowType": "SummaryRow",
                            "Cells": [{"Value": "Total Cash"}, {"Value": "130000.00"}],
                        },
                    ],
                },
                {
                    "RowType": "Section",
                    "Title": "Receivables",
                    "Rows": [
                        {
                            "RowType": "Row",
                            "Cells": [{"Value": "Accounts Receivable"}, {"Value": "45000.00"}],
                        },
                        {
                            "RowType": "Row",
                            "Cells": [{"Value": "Average Days Outstanding"}, {"Value": "32"}],
                        },
                    ],
                },
                {
                    "RowType": "Section",
                    "Title": "Payables",
                    "Rows": [
                        {
                            "RowType": "Row",
                            "Cells": [{"Value": "Accounts Payable"}, {"Value": "28000.00"}],
                        },
                        {
                            "RowType": "Row",
                            "Cells": [{"Value": "Average Days Outstanding"}, {"Value": "25"}],
                        },
                    ],
                },
                {
                    "RowType": "Section",
                    "Title": "Profitability",
                    "Rows": [
                        {"RowType": "Row", "Cells": [{"Value": "Income"}, {"Value": "200000.00"}]},
                        {
                            "RowType": "Row",
                            "Cells": [{"Value": "Expenses"}, {"Value": "155000.00"}],
                        },
                        {
                            "RowType": "SummaryRow",
                            "Cells": [{"Value": "Net Profit"}, {"Value": "45000.00"}],
                        },
                    ],
                },
                {
                    "RowType": "Section",
                    "Title": "Income Trend",
                    "Rows": [
                        {
                            "RowType": "Row",
                            "Cells": [{"Value": "This Month"}, {"Value": "18500.00"}],
                        },
                        {
                            "RowType": "Row",
                            "Cells": [{"Value": "Last Month"}, {"Value": "17200.00"}],
                        },
                        {"RowType": "Row", "Cells": [{"Value": "Change"}, {"Value": "7.6%"}]},
                    ],
                },
            ],
        }

        self.data["Reports"] = reports

    def generate_all(self):
        """Generate all Phase 2 synthetic data."""
        print("Updating invoice dates for aging distribution...")
        self.update_invoice_dates_for_aging()

        print("Generating budgets...")
        self.generate_budgets()

        print("Generating journals...")
        self.generate_journals()

        print("Generating bank transfers...")
        self.generate_bank_transfers()

        print("Generating quotes...")
        self.generate_quotes()

        print("Generating purchase orders...")
        self.generate_purchase_orders()

        print("Generating credit notes...")
        self.generate_credit_notes()

        print("Generating prepayments...")
        self.generate_prepayments()

        print("Generating overpayments...")
        self.generate_overpayments()

        print("Generating asset types...")
        self.generate_asset_types()

        print("Generating assets...")
        self.generate_assets()

        print("Generating folders...")
        self.generate_folders()

        print("Generating files...")
        self.generate_files()

        print("Generating associations...")
        self.generate_associations()

        print("Generating projects...")
        self.generate_projects()

        print("Generating time entries...")
        self.generate_time_entries()

        print("Generating reports...")
        self.generate_reports()

        return self.data


def main():
    """Main entry point."""
    # Determine paths
    script_dir = Path(__file__).parent
    project_root = script_dir.parent
    data_file = project_root / "mcp_servers" / "xero" / "data" / "synthetic_data.json"

    print(f"Reading existing data from {data_file}...")

    # Read existing data
    with open(data_file) as f:
        existing_data = json.load(f)

    # Generate Phase 2 data
    generator = SyntheticDataGenerator(existing_data)
    updated_data = generator.generate_all()

    # Write updated data
    print(f"Writing updated data to {data_file}...")
    with open(data_file, "w") as f:
        json.dump(updated_data, f, indent=2)

    # Print summary
    print("\n=== Summary ===")
    print(f"Accounts: {len(updated_data.get('Accounts', []))}")
    print(f"Contacts: {len(updated_data.get('Contacts', []))}")
    print(f"Invoices: {len(updated_data.get('Invoices', []))}")
    print(f"Budgets: {len(updated_data.get('Budgets', []))}")
    print(f"Journals: {len(updated_data.get('Journals', []))}")
    print(f"BankTransfers: {len(updated_data.get('BankTransfers', []))}")
    print(f"Quotes: {len(updated_data.get('Quotes', []))}")
    print(f"PurchaseOrders: {len(updated_data.get('PurchaseOrders', []))}")
    print(f"CreditNotes: {len(updated_data.get('CreditNotes', []))}")
    print(f"Prepayments: {len(updated_data.get('Prepayments', []))}")
    print(f"Overpayments: {len(updated_data.get('Overpayments', []))}")
    print(f"AssetTypes: {len(updated_data.get('AssetTypes', []))}")
    print(f"Assets: {len(updated_data.get('Assets', []))}")
    print(f"Folders: {len(updated_data.get('Folders', []))}")
    print(f"Files: {len(updated_data.get('Files', []))}")
    print(f"Associations: {len(updated_data.get('Associations', []))}")
    print(f"Projects: {len(updated_data.get('Projects', []))}")
    print(f"TimeEntries: {len(updated_data.get('TimeEntries', []))}")
    print(f"Reports: {list(updated_data.get('Reports', {}).keys())}")

    print("\nDone!")


if __name__ == "__main__":
    main()
