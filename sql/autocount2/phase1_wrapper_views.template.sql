/*
  AutoCount 2 Phase 1 wrapper-view draft templates only.

  DO NOT execute this file as-is.
  DO NOT treat these surfaces as production-ready.
  DO NOT use these templates to write to AutoCount transaction tables.
  DO NOT treat any wrapper below as selected for Phase 1 extraction.

  These wrapper views must be manually reviewed, adjusted for the real
  AutoCount 2.2 schema, and reconciled against AutoCount UI/report outputs
  before any view is created or granted to an extractor login. Prefer approved
  wrapper views over direct table extraction so downstream extraction does not
  depend on raw vendor table/view names, sign conventions, or document-specific
  quirks.
*/

/*
  Draft stock master wrapper.
  Current evidence favors dbo.Item joined to dbo.ItemUOM: Item carries master
  flags and descriptions; ItemUOM carries UOM and barcode. Reconcile with
  AutoCount stock item listing count, UOM/barcode visibility,
  active/stock-control flags, and modified timestamp behavior before execution.
*/
CREATE OR ALTER VIEW dbo.vw_XB_AC2_StockMaster_Phase1 AS
SELECT
    i.ItemCode,
    i.Description,
    iu.UOM,
    iu.BarCode AS Barcode,
    i.ItemGroup,
    i.ItemBrand,
    i.ItemCategory,
    i.ItemClass,
    i.IsActive,
    i.StockControl,
    i.LastModified
FROM dbo.Item AS i
LEFT JOIN dbo.ItemUOM AS iu
    ON iu.ItemCode = i.ItemCode;
GO

/*
  Draft stock balance wrapper.
  Do not assume one perfect balance source yet:
  - dbo.vItemBalQty includes Location but currently has 1 row.
  - dbo.vItemUOMBalQty has 21,831 rows but no Location.
  - dbo.ItemBatchBalQty includes Location and BatchNo but currently has 1 row.

  This draft keeps dbo.vItemBalQty only as by-location balance evidence because
  it contains Location. It requires UI/report reconciliation before any use,
  especially for zero-balance handling, as-at date semantics, UOM conversion,
  costing, and whether the 1-row result is expected for a not-live AC2 target.
*/
CREATE OR ALTER VIEW dbo.vw_XB_AC2_StockBalance_Phase1 AS
SELECT
    b.ItemCode,
    b.Location,
    b.UOM,
    b.BalQty,
    CAST(NULL AS datetime) AS LastModified
FROM dbo.vItemBalQty AS b;
GO

/*
  Draft stock movement wrapper.
  StockDTL is currently the best movement candidate, but it does not expose
  DocNo. Reconcile DocType/DocKey/DtlKey/DocInfo back to AutoCount stock card
  and document UI/report output before execution.
*/
CREATE OR ALTER VIEW dbo.vw_XB_AC2_StockMovement_Phase1 AS
SELECT
    d.DocType,
    d.DocKey,
    d.DtlKey,
    d.DocInfo,
    d.ItemCode,
    d.DocDate,
    d.Location,
    d.UOM,
    d.BatchNo,
    d.Qty,
    d.Cost,
    d.TotalCost,
    d.LastModified
FROM dbo.StockDTL AS d;
GO

/*
  Draft stock document wrapper.
  Reconcile each contributing header/detail pair against AutoCount document
  listings and stock-card totals before execution. Remove any source that cannot
  be reconciled to avoid double counting.

  Header views provide DocNo, DocDate, Cancelled, and LastModified. Detail views
  generally use UnitCost/SubTotal rather than Cost/TotalCost. Transfer detail
  does not expose Location; preserve header FromLocation/ToLocation and manually
  reconcile transfer sign/location semantics before use. GRN is evidence-only if
  retained: it may overlap purchasing/receiving evidence and must not be
  double-counted.
*/
CREATE OR ALTER VIEW dbo.vw_XB_AC2_StockDocuments_Phase1 AS
SELECT
    'StockAdjustment' AS DocumentType,
    h.DocNo,
    h.DocDate,
    h.Cancelled,
    CAST(NULL AS nvarchar(50)) AS FromLocation,
    CAST(NULL AS nvarchar(50)) AS ToLocation,
    d.ItemCode,
    d.Location,
    d.UOM,
    d.Qty,
    d.UnitCost AS Cost,
    d.SubTotal AS TotalCost,
    h.LastModified
FROM dbo.vStockAdjustment AS h
INNER JOIN dbo.vStockAdjustmentDetail AS d
    ON d.DocKey = h.DocKey
UNION ALL
SELECT
    'StockReceive' AS DocumentType,
    h.DocNo,
    h.DocDate,
    h.Cancelled,
    CAST(NULL AS nvarchar(50)) AS FromLocation,
    CAST(NULL AS nvarchar(50)) AS ToLocation,
    d.ItemCode,
    d.Location,
    d.UOM,
    d.Qty,
    d.UnitCost AS Cost,
    d.SubTotal AS TotalCost,
    h.LastModified
FROM dbo.vStockReceive AS h
INNER JOIN dbo.vStockReceiveDetail AS d
    ON d.DocKey = h.DocKey
UNION ALL
SELECT
    'StockTransfer' AS DocumentType,
    h.DocNo,
    h.DocDate,
    h.Cancelled,
    h.FromLocation,
    h.ToLocation,
    d.ItemCode,
    CAST(NULL AS nvarchar(50)) AS Location,
    d.UOM,
    d.Qty,
    d.UnitCost AS Cost,
    d.SubTotal AS TotalCost,
    h.LastModified
FROM dbo.vStockTransfer AS h
INNER JOIN dbo.vStockTransferDetail AS d
    ON d.DocKey = h.DocKey
UNION ALL
SELECT
    'StockIssue' AS DocumentType,
    h.DocNo,
    h.DocDate,
    h.Cancelled,
    CAST(NULL AS nvarchar(50)) AS FromLocation,
    CAST(NULL AS nvarchar(50)) AS ToLocation,
    d.ItemCode,
    d.Location,
    d.UOM,
    d.Qty,
    d.UnitCost AS Cost,
    d.SubTotal AS TotalCost,
    h.LastModified
FROM dbo.vStockIssue AS h
INNER JOIN dbo.vStockIssueDetail AS d
    ON d.DocKey = h.DocKey
UNION ALL
SELECT
    'StockWriteOff' AS DocumentType,
    h.DocNo,
    h.DocDate,
    h.Cancelled,
    CAST(NULL AS nvarchar(50)) AS FromLocation,
    CAST(NULL AS nvarchar(50)) AS ToLocation,
    d.ItemCode,
    d.Location,
    d.UOM,
    d.Qty,
    d.UnitCost AS Cost,
    d.SubTotal AS TotalCost,
    h.LastModified
FROM dbo.vStockWriteOff AS h
INNER JOIN dbo.vStockWriteOffDetail AS d
    ON d.DocKey = h.DocKey
UNION ALL
SELECT
    'GoodsReceivedNote' AS DocumentType,
    h.DocNo,
    h.DocDate,
    h.Cancelled,
    CAST(NULL AS nvarchar(50)) AS FromLocation,
    CAST(NULL AS nvarchar(50)) AS ToLocation,
    d.ItemCode,
    d.Location,
    COALESCE(d.UserUOM, d.UOM) AS UOM,
    d.Qty,
    d.UnitPrice AS Cost,
    d.SubTotal AS TotalCost,
    h.LastModified
FROM dbo.vGoodsReceivedNote AS h
INNER JOIN dbo.vGoodsReceivedNoteDetail AS d
    ON d.DocKey = h.DocKey;
GO
