/*
  AutoCount 2 Phase 1 wrapper-view draft templates only.

  DO NOT execute this file as-is.
  DO NOT treat these surfaces as production-ready.
  DO NOT use these templates to write to AutoCount transaction tables.

  These wrapper views must be reviewed, adjusted for the real AutoCount 2.2
  schema, and reconciled against AutoCount UI/report outputs before any view is
  created or granted to an extractor login. Prefer approved wrapper views over
  direct table extraction so downstream extraction does not depend on raw vendor
  table/view names, sign conventions, or document-specific quirks.
*/

/*
  Draft stock master wrapper.
  Reconcile with AutoCount stock item listing count, UOM/barcode visibility,
  active/stock-control flags, and modified timestamp behavior before execution.
*/
CREATE OR ALTER VIEW dbo.vw_XB_AC2_StockMaster_Phase1 AS
SELECT
    i.ItemCode,
    iu.UOM,
    iu.BarCode AS Barcode,
    i.ItemGroup,
    i.ItemBrand,
    i.ItemCategory,
    i.ItemClass,
    i.IsActive,
    i.StockControl,
    i.LastModified
FROM dbo.vItem AS i
LEFT JOIN dbo.vItemUOM AS iu
    ON iu.ItemCode = i.ItemCode;
GO

/*
  Draft stock balance wrapper.
  Reconcile with AutoCount stock balance/status report by item, UOM, location,
  batch handling, zero-balance rules, and report date filters before execution.
*/
CREATE OR ALTER VIEW dbo.vw_XB_AC2_StockBalance_Phase1 AS
SELECT
    b.ItemCode,
    b.Location,
    b.UOM,
    b.BalQty,
    b.LastModified
FROM dbo.vItemUOMBalQty AS b;
GO

/*
  Draft stock movement wrapper.
  Reconcile with AutoCount stock card/movement report, including quantity signs,
  cost semantics, posting/cancellation rules, and the two StockDTL rows dated
  2026-06-04 before execution.
*/
CREATE OR ALTER VIEW dbo.vw_XB_AC2_StockMovement_Phase1 AS
SELECT
    d.ItemCode,
    d.DocDate,
    d.DocNo,
    d.Location,
    d.UOM,
    d.Qty,
    d.Cost,
    d.TotalCost
FROM dbo.StockDTL AS d;
GO

/*
  Draft stock document wrapper.
  Reconcile each contributing header/detail pair against AutoCount document
  listings and stock-card totals before execution. Remove any source that cannot
  be reconciled to avoid double counting.
*/
CREATE OR ALTER VIEW dbo.vw_XB_AC2_StockDocuments_Phase1 AS
SELECT
    'StockAdjustment' AS DocumentType,
    h.DocDate,
    h.DocNo,
    d.ItemCode,
    d.Location,
    d.UOM,
    d.Qty,
    d.Cost,
    d.TotalCost,
    h.Cancelled
FROM dbo.vStockAdjustment AS h
INNER JOIN dbo.vStockAdjustmentDetail AS d
    ON d.DocKey = h.DocKey
UNION ALL
SELECT
    'StockReceive' AS DocumentType,
    h.DocDate,
    h.DocNo,
    d.ItemCode,
    d.Location,
    d.UOM,
    d.Qty,
    d.Cost,
    d.TotalCost,
    h.Cancelled
FROM dbo.vStockReceive AS h
INNER JOIN dbo.vStockReceiveDetail AS d
    ON d.DocKey = h.DocKey
UNION ALL
SELECT
    'StockTransfer' AS DocumentType,
    h.DocDate,
    h.DocNo,
    d.ItemCode,
    d.Location,
    d.UOM,
    d.Qty,
    d.Cost,
    d.TotalCost,
    h.Cancelled
FROM dbo.vStockTransfer AS h
INNER JOIN dbo.vStockTransferDetail AS d
    ON d.DocKey = h.DocKey
UNION ALL
SELECT
    'StockIssue' AS DocumentType,
    h.DocDate,
    h.DocNo,
    d.ItemCode,
    d.Location,
    d.UOM,
    d.Qty,
    d.Cost,
    d.TotalCost,
    h.Cancelled
FROM dbo.vStockIssue AS h
INNER JOIN dbo.vStockIssueDetail AS d
    ON d.DocKey = h.DocKey
UNION ALL
SELECT
    'StockWriteOff' AS DocumentType,
    h.DocDate,
    h.DocNo,
    d.ItemCode,
    d.Location,
    d.UOM,
    d.Qty,
    d.Cost,
    d.TotalCost,
    h.Cancelled
FROM dbo.vStockWriteOff AS h
INNER JOIN dbo.vStockWriteOffDetail AS d
    ON d.DocKey = h.DocKey;
GO
