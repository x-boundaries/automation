/*
  #245 hourly stock balance export: owner-run live check. READ-ONLY.

  NOT RUN BY THE BUILD. The owner runs it once, off-peak, after an explicit OK (runbook step V3),
  with sqlcmd variable mode, for example:
      sqlcmd -S <instance> -E -v ExpectedDb="<book database>" -i stock_balance_hourly_live_check.readonly.template.sql

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

SET NOEXEC OFF;
