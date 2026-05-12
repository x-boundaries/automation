# X-Boundaries Automation Dashboard

*Last generated: 2026-05-12 15:27:44 UTC*

## Summary Metrics

- **Total tasks:** 70
- **Done:** 9
- **In Progress:** 6
- **Not Started:** 52
- **Blocked:** 0
- **Parked:** 3
- **Completion:** 12.9%

## Tasks by Status

| Status | Count |
| --- | --- |
| Done | 9 |
| In Progress | 6 |
| Not Started | 52 |
| Parked | 3 |

## Tasks by Category

| Category | Count |
| --- | --- |
| Migration Prep | 13 |
| Data Foundation | 7 |
| Automation | 7 |
| Operations | 8 |
| Retail | 4 |
| Wholesale | 4 |
| Finance | 4 |
| E-commerce | 3 |
| Reporting | 8 |
| Documentation | 3 |
| Stabilisation | 6 |
| Architecture | 3 |

## High-Priority Open Tasks

| No | Category | Task | Status |
| --- | --- | --- | --- |
| 2 | Migration Prep | Confirm AutoCount 2.0 module scope | In Progress |
| 3 | Migration Prep | Confirm 6 migration templates | In Progress |
| 6 | Migration Prep | Prepare Brendan finance data-source call | In Progress |
| 7 | Migration Prep | Prepare stock master list | Not Started |
| 8 | Migration Prep | Prepare customer/debtor master list | Not Started |
| 9 | Migration Prep | Prepare supplier/creditor master list | Not Started |
| 10 | Migration Prep | Prepare Chart of Accounts draft | Not Started |
| 12 | Migration Prep | Confirm POS edge case handling | Not Started |
| 13 | Migration Prep | Confirm opening accounting balances | Not Started |
| 16 | Data Foundation | Create primary product ID concept | In Progress |
| 19 | Automation | Build stock upload generator | Not Started |
| 20 | Automation | Build stock upload exception report | Not Started |
| 21 | Data Foundation | Create data dictionary | Not Started |
| 22 | Data Foundation | Create export archive structure | Not Started |
| 23 | Operations | Create product master cleanup checker | Not Started |
| 24 | Operations | Shipment upload to PO/product update workflow | Not Started |
| 25 | Operations | Create goods-in-transit tracker | Not Started |
| 27 | Operations | Create warehouse GRN scan/discrepancy tracker | Not Started |
| 28 | Operations | Create goods disbursement workflow by channel | Not Started |
| 39 | Finance | Create daily POS/payment reconciliation MVP | Not Started |
| 50 | Reporting | Create current category cleanup/mapping | Not Started |
| 61 | Stabilisation | Create daily post-go-live issue log | Not Started |
| 62 | Stabilisation | Create backup success / restore check alert | Not Started |
| 63 | Stabilisation | Create failed sync tracker | Not Started |
| 65 | Stabilisation | Create POS closing checklist | Not Started |
| 66 | Stabilisation | Create daily sales report archive | Not Started |
| 67 | Architecture | Define read-only automation rule | Not Started |
| 68 | Architecture | Investigate AutoCount export/API/SQL access | Not Started |

## Recently Completed Tasks

| No | Category | Task | Evidence / Output |
| --- | --- | --- | --- |
| 1 | Migration Prep | Build Mike migration call checklist | mike_call_migration_prep_checklist_v3_sku_mapping.md |
| 4 | Migration Prep | Add stock-in-transit questions | Mike checklist section 6A |
| 5 | Migration Prep | Add parallel run questions | Mike checklist section 7A |
| 14 | Data Foundation | Create master legend concept | xb_master_legend_TEMPLATE_v0_1.xlsx |
| 15 | Data Foundation | Create SKU history approach | SKU_History sheet |
| 17 | Data Foundation | Create new stock input template | xb_new_stock_input_TEMPLATE_v0_1.xlsx |
| 18 | Automation | Create AutoCount stock item copy-paste template | ac2_stock_item_copypaste_TEMPLATE_v0_1.xlsx |
| 57 | Documentation | Create post-migration automation roadmap | docs/post_migration_automation_roadmap.md |
| 58 | Documentation | Create GitHub automation repo | https://github.com/x-boundaries/automation |

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
| 1 | Migration Prep | Build Mike migration call checklist | Create a focused checklist to ask Ingenious what X-Boundaries must prepare before migration day. | Done | High | Minutes |  |  | mike_call_migration_prep_checklist_v3_sku_mapping.md |  |
| 2 | Migration Prep | Confirm AutoCount 2.0 module scope | Confirm stock, accounting, POS, bank recon, AP/AR, price master, multi-location, and import capabilities. | In Progress | High | Minutes |  |  | Mike call |  |
| 3 | Migration Prep | Confirm 6 migration templates | Confirm purpose, mandatory fields, import order, and reversibility for stock, stock open balance, debtor, creditor, AR, and AP templates. | In Progress | High | Minutes |  |  | Mike call |  |
| 4 | Migration Prep | Add stock-in-transit questions | Clarify how paid-but-not-received goods, supplier prepayments, PO, AP, and GRN should be handled. | Done | High | Minutes |  |  | Mike checklist section 6A |  |
| 5 | Migration Prep | Add parallel run questions | Clarify whether AutoCount 1.0 and 2.0 run in parallel, what is keyed twice, and what reports are compared. | Done | High | Minutes |  |  | Mike checklist section 7A |  |
| 6 | Migration Prep | Prepare Brendan finance data-source call | Map Mike's required accounting data to Brendan/finance/accountant sources and owners. | In Progress | High | Minutes |  |  |  |  |
| 7 | Migration Prep | Prepare stock master list | Prepare item/product master data for migration and vendor review. | Not Started | High | Minutes |  |  |  |  |
| 8 | Migration Prep | Prepare customer/debtor master list | Prepare customer/debtor data for migration, especially wholesale, Metro, corporate, and Gebiz customers. | Not Started | High | Minutes |  |  |  |  |
| 9 | Migration Prep | Prepare supplier/creditor master list | Prepare supplier/creditor data including local/overseas suppliers, terms, currency, and contacts. | Not Started | High | Minutes |  |  |  |  |
| 10 | Migration Prep | Prepare Chart of Accounts draft | Coordinate with Brendan/Lik/accountant to prepare accounting buckets before using accounting module. | Not Started | High | Minutes |  |  |  |  |
| 11 | Migration Prep | Prepare channel pricing tiers | Define retail, Shopify, marketplace, wholesale, and other pricing tiers before go-live if needed. | Not Started | Medium | Minutes |  |  |  |  |
| 12 | Migration Prep | Confirm POS edge case handling | Ask Ingenious how refunds, exchanges, mall vouchers, store credits, voids, partial refunds, and split payments are handled. | Not Started | High | Minutes |  |  |  |  |
| 13 | Migration Prep | Confirm opening accounting balances | Clarify what GL opening balance, AR/AP opening, bank/cash, GST, FX, and stock value numbers must be final before go-live. | Not Started | High | Minutes |  |  |  |  |
| 14 | Data Foundation | Create master legend concept | Design external product/SKU mapping legend because AutoCount fields are limited. | Done | High | Derived |  |  | xb_master_legend_TEMPLATE_v0_1.xlsx |  |
| 15 | Data Foundation | Create SKU history approach | Keep old and new SKUs as growing alias rows instead of overwriting SKU values. | Done | High | Minutes |  |  | SKU_History sheet |  |
| 16 | Data Foundation | Create primary product ID concept | Define InternalProductID / PrimarySKU / AutoCountItemCode roles so SKU changes do not break reports. | In Progress | High | Minutes |  |  |  | Need Mike confirmation on AC2 ItemCode change behaviour. |
| 17 | Data Foundation | Create new stock input template | Make stock upload simple: upload guy keys SKU, qty, location, PO/ref, remarks only. | Done | High | Derived |  |  | xb_new_stock_input_TEMPLATE_v0_1.xlsx |  |
| 18 | Automation | Create AutoCount stock item copy-paste template | Create exact 24-column output format matching AutoCount Stock Item import/copy-paste screen. | Done | High | Derived |  |  | ac2_stock_item_copypaste_TEMPLATE_v0_1.xlsx | Need Mike to confirm mandatory fields. |
| 19 | Automation | Build stock upload generator | Generate AutoCount-ready rows from master legend + new stock input file. | Not Started | High | Derived |  |  |  | Start after Mike confirms required fields and quantity workflow. |
| 20 | Automation | Build stock upload exception report | Flag missing SKU, inactive product, invalid qty, invalid location, missing AutoCount item code. | Not Started | High | Derived |  |  |  |  |
| 21 | Data Foundation | Create data dictionary | Document fields from AutoCount, SiteGiant, marketplace exports, and internal templates. | Not Started | High | Derived / Roadmap |  |  |  |  |
| 22 | Data Foundation | Create export archive structure | Set up folders/naming rules for daily/weekly/hourly exports from AutoCount, SiteGiant, marketplaces, and payment rails. | Not Started | High | Derived / Roadmap |  |  |  |  |
| 23 | Operations | Create product master cleanup checker | Check missing brand, category, barcode, UOM, cost, price, duplicate SKU, and inactive items. | Not Started | High | Derived / Roadmap |  |  |  |  |
| 24 | Operations | Shipment upload to PO/product update workflow | Take shipment details and prepare process for PO creation, new product creation, existing stock update, and price-change flag. | Not Started | High | XB new system 2026.xlsx |  |  |  | Backend wishlist item 1. |
| 25 | Operations | Create goods-in-transit tracker | Track PO, supplier payment, shipment ETA, packing list, GRN, landed cost, and closure. | Not Started | High | Minutes / Roadmap |  |  |  |  |
| 26 | Operations | Create pre-order ETA tracker | Track ETA of pre-order stock and alert staff before/after expected arrival. | Not Started | Medium | XB new system 2026.xlsx |  |  |  | Backend wishlist item 6. |
| 27 | Operations | Create warehouse GRN scan/discrepancy tracker | Compare scanned goods received at warehouse against PO and support rectify now / rectify later flow. | Not Started | High | XB new system 2026.xlsx / Roadmap |  |  |  | Backend wishlist item 2. |
| 28 | Operations | Create goods disbursement workflow by channel | Use goods received data to disburse stock to MG, Online, Warehouse, JBM, or other locations. | Not Started | High | XB new system 2026.xlsx |  |  |  | Backend wishlist item 3. |
| 29 | Operations | Create channel receiving acknowledgement tracker | Let channel owners acknowledge goods received and flag quantity differences. | Not Started | Medium | XB new system 2026.xlsx / Roadmap |  |  |  | Backend wishlist item 4. |
| 30 | Operations | Create evergreen replenishment alert | Alert when evergreen item quantity falls below agreed threshold. | Not Started | Medium | XB new system 2026.xlsx / Roadmap |  |  |  | Backend wishlist item 5. |
| 31 | Retail | Retail receive from HQ workflow | Create or document process for retail store to scan goods from HQ and acknowledge receipt/POS update. | Not Started | Medium | XB new system 2026.xlsx |  |  |  | Retail wishlist item 1. |
| 32 | Retail | Retail supplier GRN workflow | Handle retail store receiving directly from suppliers using supplier DO and scanning items received. | Not Started | Medium | XB new system 2026.xlsx |  |  |  | Retail wishlist item 2. |
| 33 | Retail | Membership POS workflow | Support member database, auto discount, points accumulation, and birthday vouchers if POS/system supports it. | Not Started | Medium | XB new system 2026.xlsx |  |  |  | Retail wishlist item 3. |
| 34 | Retail | Membership mandatory fields checker | Ensure member mobile, email, birthday month, postal code, and consent/PDPA fields are captured. | Not Started | Medium | XB new system 2026.xlsx / Roadmap |  |  |  | Retail wishlist item 4. |
| 35 | Wholesale | Wholesale receiving workflow | Create process for wholesale stock receiving/scanning using AutoCount app or agreed workflow. | Not Started | Medium | XB new system 2026.xlsx |  |  |  | Wholesale wishlist item 1. |
| 36 | Wholesale | Wholesale outright buyer invoice workflow | Create invoice flow in AutoCount for outright buyer goods disbursement. | Not Started | Medium | XB new system 2026.xlsx |  |  |  | Wholesale wishlist item 2. |
| 37 | Wholesale | Consignee DO to AR workflow | Create DO out to consignees and push/convert to AR invoice while supporting different consignee stores. | Not Started | Medium | XB new system 2026.xlsx |  |  |  | Wholesale wishlist item 3. |
| 38 | Wholesale | Metro/consignment ageing tracker | Track consignment stock, monthly Metro statement, AR billing, and ageing risk. | Not Started | Medium | Kickoff PDF |  |  |  |  |
| 39 | Finance | Create daily POS/payment reconciliation MVP | Compare POS sales by payment method against bank/card/payment reports and flag exceptions. | Not Started | High | Minutes / Roadmap |  |  |  |  |
| 40 | Finance | Create AR chasing queue | Track unpaid customer invoices, due dates, owners, last chase date, and next follow-up. | Not Started | Medium | Minutes / Roadmap |  |  |  |  |
| 41 | Finance | Create AP invoice inbox tracker | Start with central supplier invoice inbox + tracker before OCR or AutoCount posting automation. | Not Started | Medium | Minutes / Roadmap |  |  |  |  |
| 42 | Finance | Create supplier invoice/expense automation plan | Plan single inbox, document capture, duplicate checks, approval, and AP posting/archiving process. | Not Started | Medium | Minutes / Roadmap |  |  |  |  |
| 43 | E-commerce | Create marketplace payout reconciliation | Match Shopee/Lazada order gross, fees, refunds, vouchers, and net payout. | Not Started | Medium | Kickoff PDF / Roadmap |  |  |  |  |
| 44 | E-commerce | Create SiteGiant sync monitor | Track failed product/order/inventory syncs and create an exception queue. | Not Started | Medium | Kickoff PDF / Roadmap |  |  |  |  |
| 45 | E-commerce | Confirm KrisShop workflow | Confirm SiteGiant compatibility or create manual import/export workaround. | Not Started | Medium | Kickoff PDF / Roadmap |  |  |  |  |
| 46 | Reporting | Create report by vendor | Build reporting view for sales/stock by vendor. | Not Started | Medium | XB new system 2026.xlsx |  |  |  |  |
| 47 | Reporting | Create report by brand | Build reporting view for sales/stock by brand. | Not Started | Medium | XB new system 2026.xlsx |  |  |  |  |
| 48 | Reporting | Create report by category | Build reporting view using cleaned category list and product master mapping. | Not Started | Medium | XB new system 2026.xlsx |  |  |  |  |
| 49 | Reporting | Create sales breakdown report | Track average pieces per transaction and average sales per transaction. | Not Started | Medium | XB new system 2026.xlsx |  |  |  |  |
| 50 | Reporting | Create current category cleanup/mapping | Clean and map existing AutoCount categories like MB Pants, MB Down Jackets, MB Accessories, etc. | Not Started | High | XB new system 2026.xlsx |  |  |  |  |
| 51 | Reporting | Create basic sales/channel dashboard | Show simple sales by channel/outlet/SKU/category after data exports become reliable. | Not Started | Medium | Derived / Roadmap |  |  |  |  |
| 52 | Reporting | Create inventory dashboard | Show stock on hand by SKU/location, dead stock, stock ageing, and replenishment candidates. | Not Started | Medium | Derived / Roadmap |  |  |  |  |
| 53 | Automation | Create price calculator + price change alert | Calculate suggested prices and alert retail/e-commerce/wholesale when approved prices change. | Not Started | Medium | Minutes / Roadmap |  |  |  | Do not auto-update live prices at first. |
| 54 | Automation | Barcode scanning workflow | Support barcode scanning for GRN, receiving, transfer, stocktake, and POS workflows before RFID. | Not Started | Medium | Minutes |  |  |  |  |
| 55 | Automation | RFID pilot | Park RFID until barcode, GRN, transfer, stocktake, hardware, and AutoCount/SiteGiant support are clear. | Parked | Low | Minutes / Roadmap |  |  |  | Not phase 1. |
| 56 | Documentation | Create SOPs for key workflows | Document SKU mapping, stock upload, GRN, stock transfer, POS closing, AP/AR, backup, and returns workflows. | Not Started | Medium | Derived |  |  |  |  |
| 57 | Documentation | Create post-migration automation roadmap | Split out automation work to do after migration stabilises. | Done | High | Roadmap |  |  | docs/post_migration_automation_roadmap.md |  |
| 58 | Documentation | Create GitHub automation repo | Set up private repo to store templates, scripts, docs, schemas, and dummy sample files. | Done | Medium | Derived |  |  | https://github.com/x-boundaries/automation | Do not commit real company data. |
| 59 | Data Foundation | Create weekly achievement update habit | Update this tracker weekly with status, outputs, and proof of work. | In Progress | Medium | Derived |  |  | tracker/work_tracker.csv | This is the canonical tracker path. |
| 60 | Automation | Park AI/direct write-back until data is clean | Keep AI forecast, OCR auto-posting, and direct write-back as later-stage work. | Parked | Low | Derived / Roadmap |  |  |  | Not phase 1 priority. |
| 61 | Stabilisation | Create daily post-go-live issue log | Capture go-live/hypercare issues, owner, severity, status, and resolution notes. | Not Started | High | Roadmap |  |  |  | Phase 0 stabilise first. |
| 62 | Stabilisation | Create backup success / restore check alert | Track backup success and schedule restore-test reminders after go-live. | Not Started | High | Roadmap |  |  |  | Phase 0 stabilise first. |
| 63 | Stabilisation | Create failed sync tracker | Track failed AutoCount/SiteGiant/channel syncs and daily follow-up. | Not Started | High | Roadmap |  |  |  | Phase 0 and SiteGiant monitor. |
| 64 | Stabilisation | Create master data change tracker | Track who changed product, price, customer, supplier, location, or account setup fields. | Not Started | Medium | Roadmap |  |  |  | Start simple: export diff or manual log. |
| 65 | Stabilisation | Create POS closing checklist | Daily outlet closing checklist for POS sales, cash, card, vouchers, refunds, and exceptions. | Not Started | High | Roadmap |  |  |  | Before fancy dashboards. |
| 66 | Stabilisation | Create daily sales report archive | Archive daily sales exports before analysis/dashboard work. | Not Started | High | Roadmap |  |  |  | Read-only automation first. |
| 67 | Architecture | Define read-only automation rule | Document that phase 1 automations collect/export/compare/alert only; no direct write-back until process is stable. | Not Started | High | Roadmap |  |  |  | Important guardrail. |
| 68 | Architecture | Investigate AutoCount export/API/SQL access | Confirm allowed ways to extract data from AutoCount 2.0: export, SQL, API, or scheduled reports. | Not Started | High | Roadmap |  |  |  | Ask Mike after migration basics are clear. |
| 69 | Architecture | Create write-back approval gate | Define rule for when scripts/n8n/API can write to AutoCount/SiteGiant and what human approval is needed. | Parked | Medium | Roadmap |  |  |  | Only after read-only flow works. |
| 70 | Reporting | Generate GitHub dashboard from tracker | Use tracker/work_tracker.csv and source_coverage.csv to build dashboard/README.md automatically. | In Progress | Medium | GitHub repo |  |  | scripts/build_dashboard.py | Jules baseline already exists. |
