# TaxJar Verifier Guide

## Verifier Types

| Type | Purpose | Use When |
|------|---------|----------|
| `TAXJAR_FIELD_CHECK` | Counts, existence, field values | "How many orders?", "Does nexus exist?", "What is the shipping?" |
| `TAXJAR_CALCULATION` | Numerical calculations | "Calculate net sales", "What is tax liability?", "Project Q4 tax" |
| `OUTPUT_LLM` | Qualitative/subjective checks | "Agent explains why...", "Agent recommends...", reasoning quality |

**Note:** Use `OUTPUT_LLM` for criteria that can't be verified numerically (explanations, recommendations, communication quality).

## CSV → Verifier Field Mapping

### Orders CSV

| CSV Column | Verifier Filter | Example |
|------------|-----------------|---------|
| `transaction_id` | `transaction_id` | `"SUN-1001"` |
| `transaction_date` | `from_date`, `to_date`, `quarter` | `"2025-07-07"` |
| `to_zip` | `zip_code` | `"90002"` |
| `to_state` | `state_code` | `"CA"` |
| `to_city` | `city_name` | `"Los Angeles"` |
| `amount` | (auto-summed) | `"4000.00"` |
| `shipping` | `field_name="shipping"` | `"100.00"` |
| `sales_tax` | (auto-summed) | `"380.00"` |
| `exemption_type` | `exemption_type`, `exclude_exempt` | `"wholesale"` |
| `line_items` | Generic field access | `"[]"` |

### Refunds CSV

| CSV Column | Verifier Filter | Example |
|------------|-----------------|---------|
| `transaction_id` | `transaction_id` | `"REF-2001"` |
| `transaction_reference_id` | `order_transaction_id` | `"SUN-1005"` |
| `to_zip` | `zip_code` | `"90002"` |
| `amount` | (auto-summed, subtracted) | `"1000.00"` |
| `sales_tax` | (auto-summed, subtracted) | `"95.00"` |

### Rates CSV

| CSV Column | Verifier Filter | Example |
|------------|-----------------|---------|
| `zip` or `param_zip` | `zip_code` | `"90002"` |
| `combined_rate` | `rate_field`, `tax_rate` | `"0.0950"` |
| `state_rate` | `rate_field` | `"0.0725"` |

### Nexus CSV

| CSV Column | Verifier Filter | Example |
|------------|-----------------|---------|
| `region_code` | `region_code` | `"CA"` |
| `country_code` | - | `"US"` |

---

## TAXJAR_FIELD_CHECK Options

### Counts

| Field Check | Filters | Required Params |
|-------------|---------|-----------------|
| Order Count | date_range, state | - |
| Order Count (Q1/Q2/Q3/Q4) | quarter | year |
| Order Count by ZIP | date_range, zip | zip_code |
| Order Count by City | date_range, city | city_name |
| Transaction Count by State | date_range, state | state_code |
| Refund Count | date_range, state | - |
| Refund Count (Q1/Q2/Q3/Q4) | quarter | year |
| Nexus Region Count | - | - |
| Unique States Count | date_range | - |
| Unique ZIPs Count | date_range, state | - |

### Existence & Lookups

| Field Check | Required Params |
|-------------|-----------------|
| Order Exists | transaction_id |
| Refund Exists | transaction_id |
| Nexus Region Exists | region_code |
| Nexus Region Code | region_index |
| Nexus Region Codes List | - |

### Amount Totals

| Field Check | Filters | Required Params |
|-------------|---------|-----------------|
| Total Orders Amount | date_range, state, exemption | - |
| Total Orders Amount by ZIP | date_range, zip | zip_code |
| Total Orders Amount by City | date_range, city | city_name |
| Total Refunds Amount | date_range, state | - |
| Total Orders Sales Tax | date_range, state, exemption | - |
| Total Orders Sales Tax by ZIP | date_range, zip | zip_code |
| Total Orders Sales Tax by City | date_range, city | city_name |
| Total Refunds Sales Tax | date_range, state | - |

### Field Value Retrieval

| Field Check | Required Params |
|-------------|-----------------|
| Order Field Value | transaction_id, field_name |
| Refund Field Value | transaction_id, field_name |
| Rate for ZIP | zip_code, rate_field |

### Line Item Checks

| Field Check | Required Params |
|-------------|-----------------|
| Line Item Tax Code | transaction_id, tax_code |
| Line Item Count | transaction_id |
| Line Item with Tax Code Count | tax_code |

### Data Integrity

| Field Check | Required Params |
|-------------|-----------------|
| Refund Linked to Order | refund_transaction_id, order_transaction_id |
| Unique Transaction IDs | - |
| Has Required Fields | transaction_id, required_fields |
| Date Sequence Valid | refund_transaction_id |

### Tax Rate Verification

| Field Check | Required Params |
|-------------|-----------------|
| Effective Tax Rate | transaction_id |
| Tax Rate Matches Expected | transaction_id, expected_rate |

---

## TAXJAR_CALCULATION Options

### Basic Sales

| Calculation | Filters | Required Params |
|-------------|---------|-----------------|
| Gross Merchandise Sales | date_range, state, exemption | - |
| Gross Merchandise Sales Including Shipping | date_range, state, exemption | - |
| Net Merchandise Sales | date_range, state, exemption | - |
| Net Taxable Merchandise Sales | date_range, state | - |

### Tax Calculations

| Calculation | Filters | Required Params |
|-------------|---------|-----------------|
| Sales Tax Collected | date_range, state, exemption | - |
| Sales Tax Refunded | date_range, state | - |
| Net Sales Tax Liability | date_range, state, exemption | - |
| Expected Sales Tax | date_range, state | tax_rate |
| Incremental Tax Liability | date_range, state | tax_rate |
| Blended Tax Rate | date_range, state, exemption | - |

### Projections

| Calculation | Filters | Required Params |
|-------------|---------|-----------------|
| Projected Merchandise Sales | date_range, state, exemption | growth_rate |
| Projected Tax Liability | date_range, state, exemption | growth_rate, tax_rate |
| Projected Tax Using Blended Rate | date_range, state, exemption | growth_rate |

### Corrected Calculations

| Calculation | Filters | Required Params |
|-------------|---------|-----------------|
| Corrected Net Taxable Sales | date_range, state | - |
| Corrected Tax Liability | date_range, state | - |
| Corrected Blended Tax Rate | date_range, state | - |
| Projected Corrected Merchandise Sales | date_range, state | growth_rate |
| Projected Tax Using Corrected Blended Rate | date_range, state | growth_rate |

### ZIP/City Calculations

| Calculation | Filters | Required Params |
|-------------|---------|-----------------|
| Net Taxable Sales by ZIP | date_range | zip_code |
| Net Taxable Sales by City | date_range | city_name |
| Sales Tax Collected by ZIP | date_range | zip_code |
| Sales Tax Collected by City | date_range | city_name |
| Net Sales Tax by ZIP | date_range | zip_code |
| Net Sales Tax by City | date_range | city_name |

### Rate Corrections

| Calculation | Filters | Required Params |
|-------------|---------|-----------------|
| Corrected Tax with Rate | date_range | zip_code, corrected_rate |
| Additional Tax to Remit | date_range | zip_code, corrected_rate |
| Total Corrected Tax Liability | date_range, state | - |

### Tax Verification

| Calculation | Filters | Required Params |
|-------------|---------|-----------------|
| Tax Calculation Match | date_range, state | expected_rate |
| Tax Discrepancy Total | date_range, state | expected_rate |

### Jurisdiction Breakdown

| Calculation | Filters |
|-------------|---------|
| State Tax Component | date_range, state |
| County Tax Component | date_range, state |
| City Tax Component | date_range, state |
| Special District Tax Component | date_range, state |

### Economic Nexus

| Calculation | Filters | Required Params |
|-------------|---------|-----------------|
| Economic Nexus Sales Check | date_range | state_code, threshold |
| Economic Nexus Transaction Count Check | date_range | state_code, threshold |

### Filing

| Calculation | Filters | Required Params |
|-------------|---------|-----------------|
| Filing Period Sales | date_range, state | - |
| Filing Period Tax Due | date_range, state | - |
| Filing States Count | date_range | - |

### Rate Validation

| Calculation | Filters | Required Params |
|-------------|---------|-----------------|
| Average Effective Rate | date_range, state | - |
| Rate Variance | date_range, state | expected_rate |

---

## Example Configurations

### Field Check: Order Count

```json
{
  "field_to_check": "Order Count (Q3)",
  "year": 2025,
  "expected_value": "6"
}
```

### Field Check: Order Field Value

```json
{
  "field_to_check": "Order Field Value",
  "transaction_id": "SUN-1001",
  "field_name": "shipping",
  "expected_value": "100.00"
}
```

### Field Check: Line Item Tax Code

```json
{
  "field_to_check": "Line Item Tax Code",
  "transaction_id": "ORD-001",
  "tax_code": "20010",
  "expected_value": "true"
}
```

### Field Check: Refund Linked

```json
{
  "field_to_check": "Refund Linked to Order",
  "refund_transaction_id": "REF-2001",
  "order_transaction_id": "SUN-1005",
  "expected_value": "true"
}
```

### Calculation: Net Taxable by ZIP

```json
{
  "calculation_type": "Net Taxable Sales by ZIP",
  "zip_code": "90002",
  "quarter": "3",
  "year": 2025,
  "expected_value": 14500.00
}
```

### Calculation: Corrected Tax with Rate

```json
{
  "calculation_type": "Corrected Tax with Rate",
  "zip_code": "90002",
  "corrected_rate": 0.0975,
  "quarter": "3",
  "year": 2025,
  "expected_value": 1413.75
}
```

### Calculation: Economic Nexus Check

```json
{
  "calculation_type": "Economic Nexus Sales Check",
  "state_code": "CA",
  "threshold": 500000,
  "quarter": "3",
  "year": 2025,
  "expected_value": 32400.00
}
```

### Calculation: Tax Discrepancy

```json
{
  "calculation_type": "Tax Discrepancy Total",
  "expected_rate": 0.10,
  "quarter": "3",
  "year": 2025,
  "expected_value": 47.00
}
```

---

## Key Formulas

| Concept | Formula |
|---------|---------|
| Net Taxable Sales | Orders.amount - Refunds.amount (excluding exempt) |
| Net Sales Tax | Orders.sales_tax - Refunds.sales_tax |
| Blended Rate | Net Sales Tax ÷ Net Taxable Sales |
| Corrected Net Taxable | All Orders.amount - Refunds.amount (ignore exemption) |
| Corrected Tax | Original Tax + (Exempt Amount × Blended Rate) - Refunded Tax |
| Projected Sales | Base × (1 + growth_rate) |
| Projected Tax | Projected Sales × tax_rate |
| Incremental Liability | Expected Tax - Actual Tax Collected |
| Additional Tax to Remit | (Net Taxable × corrected_rate) - Original Net Tax |

---

## Troubleshooting

| Issue | Check |
|-------|-------|
| Wrong count | Date filtering - Q3 is Jul 1 - Sep 30 |
| Tax mismatch | exemption_type values in CSV |
| Blended rate errors | Ensure taxable orders have non-zero sales_tax |
| Projection errors | Base period matches growth calculation |
| ZIP not found | Try both `zip` and `param_zip` columns |
