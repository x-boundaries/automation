# AutoCount 2.0 Automation API Research

Research checked: 2026-06-06 SGT.

This spike is for a local Windows VM where AutoCount Accounting 2.0 and Microsoft SQL Server 2019 are installed on the same machine. The goal is read-only automation first, with dashboards and AI summaries downstream. Facts below are separated from assumptions and recommendations.

## Executive Summary

The best first path for X-Boundaries is a native Windows/.NET extractor running beside AutoCount on the VM. The official AutoCount Programmer wiki presents the on-premise Accounting API as a .NET Framework integration surface for plug-ins, standalone applications, and services. The same Programmer page separately lists Cloud Accounting API documentation, which is a REST API for AutoCount Cloud Accounting, not proof of direct REST access to the local desktop database.

For phase 1, build a read-only extractor against official AutoCount .NET APIs and/or locally approved read-only SQL views. Do not write directly to AutoCount production tables. If read-only SQL is used, constrain it to a dedicated SQL login with `SELECT` only on approved views, then load a separate reporting database.

## Source Access Notes

The standard browsing fetch for `https://wiki.autocountsoft.com/wiki/Programmer#tabber-API_Resources` failed in the browser tool, but direct MediaWiki/HTTP fetches succeeded. No official wiki content was treated as inaccessible after the fallback fetch.

Public sources used:

- [AutoCount Programmer wiki](https://wiki.autocountsoft.com/wiki/Programmer)
- [Integration Methods](https://wiki.autocountsoft.com/wiki/Integration_Methods)
- [Initiate UserSession and DBSetting](https://wiki.autocountsoft.com/wiki/Initiate_UserSession_and_DBSetting)
- [AutoCount Accounting 2.1 API](https://wiki.autocountsoft.com/wiki/AutoCount_Accounting_2.1_API)
- [AutoCount Cloud Accounting Integration API docs](https://accounting-api.autocountcloud.com/documentation/)
- [AutoCount Cloud Accounting Swagger](https://accounting-api.autocountcloud.com/swagger/index.html)
- [AutoCount On-The-Go official product page](https://www.autocountsoft.com/pro-aotg.html)
- [Introduction to AOTG API](https://wiki.autocountsoft.com/wiki/Introduction_to_AOTG_API)
- [Begin AutoCount Accounting Integration via AOTG API](https://wiki.autocountsoft.com/wiki/Begin_AutoCount_Accounting_Integration_via_AOTG_API)
- [AOTG API Authenticate](https://wiki.autocountsoft.com/wiki/AOTG_API_Authenticate)
- [API Maintenance support article](https://autocount.freshdesk.com/support/solutions/articles/69000868127-where-to-set-up-api-maintenance-for-autocount-accounting-and-migration-tool-)
- [AutoCount free trial system requirements](https://www.autocountsoft.com/freetrial.html)
- [NuGet AutoCount2](https://www.nuget.org/packages/AutoCount2)
- [NuGet AutoCount2.Accounting](https://www.nuget.org/packages/AutoCount2.Accounting)
- [NuGet AutoCount2.Stock](https://www.nuget.org/packages/AutoCount2.Stock)
- [NuGet AutoCount2.Sales](https://www.nuget.org/packages/AutoCount2.Sales)

## Integration Methods That Appear Available

### On-premise AutoCount Accounting API: verified

The Programmer wiki's API Resources tab lists "AutoCount Accounting API (.Net Framework)" for plug-ins, integration, and applications. The Integration Methods page says AutoCount Accounting is a desktop application and exposes assemblies for programmers to access internal classes and methods.

Verified characteristics:

- The integration project must target Microsoft .NET Framework.
- The wiki says the account book requires the "API Module" license for API access.
- Assemblies can be referenced from the AutoCount application folder or installed via NuGet.
- The default local assembly path is documented as `C:\Program Files\AutoCount\Accounting\`.
- Integration examples cover standalone applications, web services, common data files, Excel/XML import, and plug-ins.
- The API is capable of both read and write operations, so X-Boundaries must deliberately restrict phase 1 usage to reads.

### Plug-ins and scripting: verified

The Programmer page also lists plug-in and report/application scripting tutorials for Accounting 2.0. This is useful if a later integration must run inside the AutoCount desktop process or use UI/report behavior. It is not the recommended first read-only path unless official docs, local sandbox testing, or optional later vendor review show a plug-in is required to access specific reports.

### Read-only SQL views: plausible but requires local validation

AutoCount Accounting uses Microsoft SQL Server, and the current X-Boundaries environment already has SQL Server 2019 on the same VM. Direct SQL reads are operationally simple and low cost, but the table schema and costing logic must be treated as product-owned and version-sensitive. Use SQL only through locally approved read-only views or queries validated against AutoCount reports in a sandbox/local test, and do not write to production AutoCount tables.

### Excel/XML/common data import: verified, but write-oriented

The Integration Methods page describes XML and Excel import approaches. These are not phase 1 extraction methods. They are relevant later for controlled, human-approved write-back or import workflows.

### AOTG Web API: verified as hybrid/cloud-mediated, not a free local REST API

The official wiki has an AOTG API menu and pages. The AOTG introduction describes RESTful API access through an AOTG Server, with an AOTG Client installed on the main computer where the AutoCount Accounting server is located. It also points to `https://aotgapi.autocountcloud.com/swagger`.

Verified AOTG examples include:

- `POST /api/public/v1/TokenAuth/Authenticate`
- `GET /api/public/v1/Debtor`
- `POST /api/public/v1/ARInvoice/GetARInvoiceList`
- Result polling/retrieval through `/api/public/v1/Result/...`

This appears to be an add-on/hybrid cloud path. It may be useful if X-Boundaries already subscribes to AOTG or needs secure remote API access. It is not the lowest-cost first choice for a local VM unless licensing is already included.

### Cloud Accounting REST API: verified cloud-only

The Programmer page separately lists "Cloud Accounting API" with Swagger and documentation links. The Cloud Accounting documentation says the Accounting Integration API exposes functions of the Cloud Accounting web application. It uses `API-Key` and `Key-ID` headers.

The public Swagger lists endpoints such as:

- `POST /{accountBookId}/product/listing`
- `GET|POST|PUT|DELETE /{accountBookId}/product`
- `GET|POST /{accountBookId}/invoice/listing`
- `GET|POST|PUT|DELETE /{accountBookId}/invoice`
- `GET|POST|PUT|DELETE /{accountBookId}/journalentry`
- `GET|POST|PUT|DELETE /{accountBookId}/stocktransfer`

Do not assume these endpoints work against AutoCount Accounting 2.0 desktop/on-premise. Treat them as cloud-only unless official AutoCount documentation or optional later vendor review confirms otherwise.

### API Maintenance: verified as key setup, unclear fit for local read-only

The support article says API Maintenance is reached from the Services page after login, where users can add an API key, choose an API type, and choose companies. This confirms an official API-key administration surface exists. It does not, by itself, prove that X-Boundaries' local desktop AutoCount 2.0 VM has a free REST endpoint available without AOTG or cloud services.

## Relevant NuGet Packages

NuGet package metadata checked on 2026-06-06 showed stable `2.2.29` and prerelease `2.2.30-alpha1` for the core package family listed below. Pin to the AutoCount version installed on the VM; do not blindly use latest if the production AutoCount client is older.

| Package | Relevance |
| --- | --- |
| `AutoCount2` | Core library for AutoCount Accounting version 2. NuGet lists target framework `.NET Framework 4.8`. |
| `AutoCount2.Accounting` | Core accounting logic package. NuGet describes ARAP, GL, Stock, Invoicing, and related logic. Does not contain WinForms controls. |
| `AutoCount2.Accounting.UI` | Accounting UI core. Useful when APIs/reporting require UI startup. Requires DevExpress WinForms. |
| `AutoCount2.MainEntry` | Login/startup flow when the app needs AutoCount login UI. |
| `AutoCount2.ARAP` | AR/AP module APIs. Relevant for debtor, creditor, AR/AP documents, payments, deposits, and aging. |
| `AutoCount2.GL` | General ledger APIs. Relevant for journal entry and cash book examples. |
| `AutoCount2.Stock` | Stock document UI library. Relevant for stock adjustment/issue/transfer examples; may need UI dependencies. |
| `AutoCount2.StockMaint` | Stock maintenance library. Relevant for item master and item group work. |
| `AutoCount2.Sales` | Sales library. NuGet lists namespaces such as sales order, delivery order, invoice, cash sale. Requires DevExpress WinForms. |
| `AutoCount2.Purchase` | Purchase library. Relevant for purchase order and goods received note examples. |
| `AutoCount2.Invoicing` | Shared invoicing package used by sales, purchase, and stock packages. |
| `AutoCount2.Inquiry` / `AutoCount2.FinancialReport` | Potential reporting/inquiry packages; evaluate only if official report APIs are needed. |

## Runtime Assumptions

Verified:

- AutoCount 2.x NuGet packages target `.NET Framework 4.8`.
- The AutoCount 2.1 API page says to use .NET Framework 4.8 for 2.1.5/2.1.8.
- Current AutoCount trial requirements list Windows 10/Windows Server 2016+, SQL Server 2016+, and .NET Framework 4.8+ for the server. SQL Server 2019 satisfies this SQL requirement.
- UI startup paths can require `System.Windows.Forms` and DevExpress WinForms components.
- AutoCount 2.1.5 examples use DevExpress 19.2.10; 2.1.8 notes upgraded DevExpress to 22.2.7. Match the installed AutoCount build.

Assumptions to confirm:

- Exact installed AutoCount Accounting version and build on the VM.
- Whether the API Module is licensed for the production account book.
- Whether official AutoCount APIs support read-only extractor use from a scheduled console app without launching UI.
- Whether report APIs can run unattended under Windows Task Scheduler.

## Public Example Links And Minimal Patterns

The official wiki provides extensive sample code. The patterns below are intentionally minimal and adapted; use the linked pages for complete examples.

### Account book login / connection

Source: [Initiate UserSession and DBSetting](https://wiki.autocountsoft.com/wiki/Initiate_UserSession_and_DBSetting)

```csharp
var db = new AutoCount.Data.DBSetting(DBServerType.SQL2000, serverName, dbName);
var session = new AutoCount.Authentication.UserSession(db);
if (session.Login(userName, password))
{
    new AutoCount.MainEntry.Startup().SubProjectStartup(session);
}
```

Use placeholders only. Store real credentials outside the repository.

### Debtor read by last modified date

Source: [AR Debtor v2](https://wiki.autocountsoft.com/wiki/AR_Debtor_v2)

The page demonstrates `DebtorDataAccess.Create(...)`, a `SearchCriteria`, and a `LastModified` range filter to load selected debtor columns into a `DataTable`. This is directly relevant to incremental read-only extraction.

### Stock item maintenance

Source: [Stock Item v2](https://wiki.autocountsoft.com/wiki/Programmer:Stock_Item_v2)

The page covers stock item master maintenance for AutoCount 2.0. For phase 1, use it as a reference for item fields and supported APIs, not for writes.

### Stock status/report and costing

Sources:

- [Stock Status Report 2](https://wiki.autocountsoft.com/wiki/Programmer:Stock_Status_Report_2)
- [Get Stock Cost v2](https://wiki.autocountsoft.com/wiki/Programmer:Get_Stock_Cost_v2)

These pages are important for inventory dashboards because stock balance and cost are usually business-logic results, not just raw table values.

### Sales and cash sale

Sources:

- [Sales Invoice v2](https://wiki.autocountsoft.com/wiki/Programmer:Sales_Invoice_v2)
- [Cash Sale with Payment v2](https://wiki.autocountsoft.com/wiki/Programmer:Cash_Sale_with_Payment_v2)
- [Search Sales Invoice 2.0-2.2](https://wiki.autocountsoft.com/wiki/Programmer:Search_Sales_Invoice_%2820%29)

These pages cover sales document examples. For read-only phase 1, prioritize listing/search/load patterns over create/save examples.

### Purchase and goods received note

Sources:

- [Purchase Order v2](https://wiki.autocountsoft.com/wiki/Programmer:Purchase_Order_v2)
- [Goods Received Note transfer from Purchase Order v2](https://wiki.autocountsoft.com/wiki/Programmer:Goods_Received_Note_Transfer_from_Purchase_Order_v2)
- [Outstanding Purchase Order 2.0-2.2](https://wiki.autocountsoft.com/wiki/Programmer:Outstanding_Purchase_Order_%2820%29)

These pages are relevant for later purchase/order dashboards and receiving controls.

### AR/AP and GL

Sources:

- [AP Invoice v2](https://wiki.autocountsoft.com/wiki/AP_Invoice_v2)
- [AP Payment v2](https://wiki.autocountsoft.com/wiki/AP_Payment_v2)
- [Journal Entry v2](https://wiki.autocountsoft.com/wiki/Journal_Entry_v2)
- [Cash Book Received Voucher v2](https://wiki.autocountsoft.com/wiki/Cash_Book_Received_Voucher_v2)
- [Cash Book Payment Voucher v2](https://wiki.autocountsoft.com/wiki/Cash_Book_Payment_Voucher_v2)

These pages confirm document APIs exist for accounting flows. Treat all save/post examples as future write-back references only.

## Open Questions And Risks

- Is the AutoCount API Module licensed in the X-Boundaries account book?
- Which exact AutoCount build is installed, and which NuGet package version matches it?
- Can an unattended scheduled process safely call `SubProjectStartup` without UI dependencies?
- Are stock status/cost reports available through stable APIs without opening the desktop UI?
- Which AutoCount data should be extracted via official API vs locally approved SQL view?
- Will the read-only SQL account be allowed to query AutoCount production database views directly?
- Are AOTG or API Maintenance already included in the current license/subscription?
- If AOTG is used, what are the real current base URLs and rate/queue behavior? Some old wiki snippets still show legacy `http://aotg.cloud:8080` examples despite newer notes.
- How should backdated transactions and stock costing recalculation be detected?
- Who approves the list of curated fields that AI may see?

## Recommended First Read-only Integration Path

1. Confirm installed AutoCount build, API Module licensing, and locally approved read surfaces.
2. Create a .NET Framework 4.8 console extractor on the Windows VM.
3. Start with read-only master/reporting data:
   - company/account book metadata,
   - item master,
   - debtor/creditor master,
   - stock status/balance and stock cost,
   - sales invoice/cash sale listing,
   - purchase order/GRN listing,
   - AR/AP open items if exposed cleanly.
4. Write extracted rows only to a separate SQL Server reporting database and audit tables.
5. Use Windows Task Scheduler for daily runs.
6. Build dashboards and AI summary packs from curated `mart` views only.
7. Defer AOTG, Cloud REST API, and write-back until the read side is stable and licensing is confirmed.
