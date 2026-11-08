/*
  #245 hourly stock balance export: owner-run live check. READ-ONLY.

  NOT RUN BY THE BUILD. The owner runs it once, off-peak, after an explicit OK (runbook step V3),
  with sqlcmd variable mode, for example:
      sqlcmd -S <instance> -E -d <book database> -v ExpectedDb="<book database>" -i stock_balance_hourly_live_check.readonly.template.sql

  SELECT and aggregates only. No item-level rows, no free-text columns, no cost columns, no
  writes, no database setting changes. dbo.ChangeLog is never read.
*/
SET NOCOUNT ON;

IF DB_NAME() <> N'$(ExpectedDb)'
BEGIN
    RAISERROR (N'Wrong database context: stopping before any read.', 16, 1);
    SET NOEXEC ON;
END;

SET LOCK_TIMEOUT 5000;
SET TRANSACTION ISOLATION LEVEL READ COMMITTED;

-- 1. Row counts.
SELECT
    (SELECT COUNT_BIG(*) FROM dbo.ItemBatchBalQty) AS ItemBatchBalQtyRows,
    (SELECT COUNT_BIG(*) FROM dbo.Item) AS ItemRows,
    (SELECT COUNT_BIG(*) FROM dbo.StockDTL) AS StockDTLRows;

-- 2. Per location: rows, non-zero rows, negative rows, SUM(BalQty).
SELECT
    b.Location,
    COUNT_BIG(*) AS BalanceRows,
    SUM(CASE WHEN b.BalQty <> 0 THEN 1 ELSE 0 END) AS NonZeroRows,
    SUM(CASE WHEN b.BalQty < 0 THEN 1 ELSE 0 END) AS NegativeRows,
    SUM(b.BalQty) AS SumBalQty
FROM dbo.ItemBatchBalQty AS b
GROUP BY b.Location
ORDER BY b.Location;

-- 3. Ledger match per ItemCode, UOM, Location, BatchNo: SUM(StockDTL.Qty) vs stored BalQty.
WITH bal AS (
    SELECT ItemCode, UOM, Location, ISNULL(BatchNo, N'') AS BatchNo, ISNULL(BalQty, 0) AS BalQty
    FROM dbo.ItemBatchBalQty
),
led AS (
    SELECT ItemCode, UOM, Location, ISNULL(BatchNo, N'') AS BatchNo, SUM(Qty) AS LedgerQty
    FROM dbo.StockDTL
    GROUP BY ItemCode, UOM, Location, ISNULL(BatchNo, N'')
)
SELECT
    SUM(CASE WHEN bal.ItemCode IS NOT NULL AND led.ItemCode IS NOT NULL AND bal.BalQty = led.LedgerQty THEN 1 ELSE 0 END) AS MatchCount,
    SUM(CASE WHEN bal.ItemCode IS NOT NULL AND led.ItemCode IS NOT NULL AND bal.BalQty <> led.LedgerQty THEN 1 ELSE 0 END) AS MismatchCount,
    SUM(CASE WHEN led.ItemCode IS NULL THEN 1 ELSE 0 END) AS BalanceOnlyCount,
    SUM(CASE WHEN bal.ItemCode IS NULL THEN 1 ELSE 0 END) AS LedgerOnlyCount
FROM bal
FULL OUTER JOIN led
    ON led.ItemCode = bal.ItemCode
    AND led.UOM = bal.UOM
    AND led.Location = bal.Location
    AND led.BatchNo = bal.BatchNo;

-- 4. ChangeCount for the balance table (one index seek).
SELECT TableName, Counter
FROM dbo.ChangeCount
WHERE TableName = N'ItemBatchBalQty';

-- 5. Data and log size (MB).
SELECT
    type_desc AS FileType,
    CAST(SUM(CAST(size AS bigint)) * 8 / 1024.0 AS decimal(18, 1)) AS SizeMB
FROM sys.database_files
GROUP BY type_desc;

-- 6. Columns the item index reads (#245 dashboard v2). Run before pulling that code onto the VM:
--    a NULL means the column is missing and the export would fail as source_error.
SELECT
    COL_LENGTH(N'dbo.Item', N'ItemGroup') AS ItemGroupLength,
    COL_LENGTH(N'dbo.Item', N'ItemBrand') AS ItemBrandLength,
    COL_LENGTH(N'dbo.ItemUOM', N'BarCode') AS BarCodeLength,
    COL_LENGTH(N'dbo.ItemUOM', N'ReOLevel') AS ReOLevelLength,
    COL_LENGTH(N'dbo.ItemUOM', N'MinQty') AS MinQtyLength;

-- 7. Columns the #292 S2 analysis reads (attribute item master read + analysis read). Run before
--    setting analysis.enabled to true: a NULL means the column is missing. The item master read
--    would then fail the hourly run as source_error, and the analysis read would fail as
--    analysis_read_failed.
SELECT
    COL_LENGTH(N'dbo.Item', N'ItemCategory') AS ItemCategoryLength,
    COL_LENGTH(N'dbo.Item', N'ItemClass') AS ItemClassLength,
    COL_LENGTH(N'dbo.Item', N'Desc2') AS Desc2Length,
    COL_LENGTH(N'dbo.Item', N'UDF_StockSize') AS StockSizeLength,
    COL_LENGTH(N'dbo.Item', N'UDF_StockGender') AS StockGenderLength,
    COL_LENGTH(N'dbo.Item', N'UDF_StockColour') AS StockColourLength,
    COL_LENGTH(N'dbo.StockDTL', N'DocType') AS DocTypeLength,
    COL_LENGTH(N'dbo.StockDTL', N'DocKey') AS DocKeyLength,
    COL_LENGTH(N'dbo.IV', N'DocKey') AS IVDocKeyLength,
    COL_LENGTH(N'dbo.IV', N'DebtorCode') AS IVDebtorLength,
    COL_LENGTH(N'dbo.CN', N'DocKey') AS CNDocKeyLength,
    COL_LENGTH(N'dbo.CN', N'DebtorCode') AS CNDebtorLength,
    COL_LENGTH(N'dbo.DO', N'DocKey') AS DODocKeyLength,
    COL_LENGTH(N'dbo.DO', N'DebtorCode') AS DODebtorLength;

-- 8. #292 S2 AC3 reconciliation: units per DocType, location and month for stock-controlled
--    items, to compare with the "reconcile" rows of analysis_sales_daily.json.gz
--    (net_out = -SUM(Qty), qty_in = SUM of positive Qty). Totals only; no item, debtor or
--    document column. Months before the file's day0 are outside the file's window.
SELECT
    d.DocType,
    d.Location,
    CONVERT(char(7), d.DocDate, 126) AS DocMonth,
    -SUM(d.Qty) AS NetOut,
    SUM(CASE WHEN d.Qty > 0 THEN d.Qty ELSE 0 END) AS QtyIn
FROM dbo.StockDTL AS d
JOIN dbo.Item AS i ON i.ItemCode = d.ItemCode
WHERE d.DocType IN (N'OS', N'IV', N'DO', N'CN', N'GR', N'PI')
    AND i.StockControl <> N'F'
    AND d.DocDate < CAST(GETDATE() AS date)
GROUP BY d.DocType, d.Location, CONVERT(char(7), d.DocDate, 126)
ORDER BY d.DocType, d.Location, DocMonth;

SET NOEXEC OFF;
