# X-Boundaries Automation Dashboard

*Last generated: 2026-05-12 13:12:49 UTC*

## Summary Metrics

- **Total tasks:** 10
- **Done:** 3
- **In Progress:** 3
- **Not Started:** 4
- **Blocked:** 0
- **Parked:** 0
- **Completion:** 30.0%

## Tasks by Status

| Status | Count |
| --- | --- |
| Done | 3 |
| In Progress | 3 |
| Not Started | 4 |

## Tasks by Category

| Category | Count |
| --- | --- |
| Migration Prep | 3 |
| Data Foundation | 3 |
| Automation | 1 |
| Finance | 1 |
| Operations | 1 |
| Reporting | 1 |

## High-Priority Open Tasks

| No | Category | Task | Status |
| --- | --- | --- | --- |
| 2 | Migration Prep | Confirm AutoCount 2.0 module scope | In Progress |
| 3 | Migration Prep | Confirm 6 migration templates | In Progress |
| 6 | Data Foundation | Create primary product ID concept | In Progress |
| 7 | Automation | Build stock upload generator | Not Started |
| 8 | Finance | Create daily POS/payment reconciliation MVP | Not Started |
| 9 | Operations | Create goods-in-transit tracker | Not Started |

## Recently Completed Tasks

| No | Category | Task | Evidence / Output |
| --- | --- | --- | --- |
| 1 | Migration Prep | Build Mike migration call checklist | mike_call_migration_prep_checklist_v3_sku_mapping.md |
| 4 | Data Foundation | Create master legend concept | xb_master_legend_TEMPLATE_v0_1.xlsx |
| 5 | Data Foundation | Create SKU history approach | SKU_History sheet |

## Source Coverage Summary

| Source | Request / Wishlist Item | Covered in tracker task(s) | Coverage |
| --- | --- | --- | --- |
| Minutes | Stock master list | Prepare stock master list; Product master cleanup checker | Covered |
| Minutes | Customer master list | Prepare customer/debtor master list | Covered |
| Minutes | Supplier master list | Prepare supplier/creditor master list | Covered |
| Minutes | Chart of Accounts | Prepare Chart of Accounts draft; Confirm opening accounting balances | Covered |
| Minutes | Goods in transit | Add stock-in-transit questions; Create goods-in-transit tracker | Covered |
| Minutes | SKU identity / primary key | Create SKU history approach; Create primary product ID concept | Covered |
| Minutes | Migration parallel run | Add parallel run questions | Covered |
| Minutes | Daily reconciliation | Create daily POS/payment reconciliation MVP | Covered |
| Wishlist | New/current products update on AutoCount via shipment upload / PO / price alert | Shipment upload to PO/product update workflow; price calculator + alert | Covered |
| Wishlist | Goods received at warehouse scan vs PO / discrepancy | Create warehouse GRN scan/discrepancy tracker | Covered |
| Wishlist | Goods disbursement to channels | Create goods disbursement workflow by channel | Covered |
| Wishlist | Membership POS / discount / points / birthday voucher | Membership POS workflow | Covered |
| Wishlist | Reports by vendor / brand / category / sales breakdown | Reporting dashboard tasks | Covered |

## Full Tracker

| No | Category | Task | Brief Description / Goal | Status | Priority | Source | Started | Completed | Evidence / Output | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | Migration Prep | Build Mike migration call checklist | Create a focused checklist to ask Ingenious what X-Boundaries must prepare before migration day | Done | High | Minutes |  |  | mike_call_migration_prep_checklist_v3_sku_mapping.md |  |
| 2 | Migration Prep | Confirm AutoCount 2.0 module scope | Confirm stock accounting POS bank recon AP/AR price master multi-location and import capabilities | In Progress | High | Minutes |  |  | Mike call |  |
| 3 | Migration Prep | Confirm 6 migration templates | Confirm purpose mandatory fields import order and reversibility for stock stock open balance debtor creditor AR and AP templates | In Progress | High | Minutes |  |  | Mike call |  |
| 4 | Data Foundation | Create master legend concept | Design external product/SKU mapping legend because AutoCount fields are limited | Done | High | Derived |  |  | xb_master_legend_TEMPLATE_v0_1.xlsx |  |
| 5 | Data Foundation | Create SKU history approach | Keep old and new SKUs as growing alias rows instead of overwriting SKU values | Done | High | Minutes |  |  | SKU_History sheet |  |
| 6 | Data Foundation | Create primary product ID concept | Define InternalProductID PrimarySKU AutoCountItemCode roles so SKU changes do not break reports | In Progress | High | Minutes |  |  |  | Need Mike confirmation on AC2 ItemCode change behaviour. |
| 7 | Automation | Build stock upload generator | Generate AutoCount-ready rows from master legend and new stock input file | Not Started | High | Derived |  |  |  | Start after Mike confirms required fields and quantity workflow. |
| 8 | Finance | Create daily POS/payment reconciliation MVP | Compare POS sales by payment method against bank/card/payment reports and flag exceptions | Not Started | High | Minutes |  |  |  |  |
| 9 | Operations | Create goods-in-transit tracker | Track PO supplier payment shipment ETA packing list GRN landed cost and closure | Not Started | High | Minutes |  |  |  |  |
| 10 | Reporting | Create basic sales/channel dashboard | Show simple sales by channel outlet SKU category after data exports become reliable | Not Started | Medium | Derived |  |  |  |  |
