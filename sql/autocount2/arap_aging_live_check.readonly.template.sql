/*
  #321 AR/AP aging export: owner-run live check. READ-ONLY.

  NOT RUN BY THE BUILD. Run once on the AutoCount VM, off-peak and clear of the :30 export, after
  an explicit owner OK (runbook step V3), with sqlcmd variable mode, for example:
      sqlcmd -S <instance> -E -d <book database> -v ExpectedDb="<book database>" -i arap_aging_live_check.readonly.template.sql

  SELECT and aggregates only: counts and sums per table, and per party code for the V3 compare.
  No document numbers, no names, no free-text columns, no writes, no database setting changes.
  The output holds party codes and amounts: keep it private on the VM (mailbox record only).
*/
SET NOCOUNT ON;

IF DB_NAME() <> N'$(ExpectedDb)'
BEGIN
    RAISERROR (N'Wrong database context: stopping before any read.', 16, 1);
    SET NOEXEC ON;
END;

SET LOCK_TIMEOUT 5000;
SET TRANSACTION ISOLATION LEVEL READ COMMITTED;

-- 1. Non-cancelled rows and open rows per source (open = the export's rule for that kind).
SELECT N'ARInvoice' AS SourceTable, COUNT_BIG(*) AS LiveRows,
    SUM(CASE WHEN Outstanding <> 0 THEN 1 ELSE 0 END) AS OpenRows
FROM dbo.ARInvoice WHERE Cancelled = N'F'
UNION ALL SELECT N'ARDN', COUNT_BIG(*), SUM(CASE WHEN Outstanding <> 0 THEN 1 ELSE 0 END) FROM dbo.ARDN WHERE Cancelled = N'F'
UNION ALL SELECT N'ARCN', COUNT_BIG(*), SUM(CASE WHEN NetTotal - KnockOffAmt - RefundAmt <> 0 THEN 1 ELSE 0 END) FROM dbo.ARCN WHERE Cancelled = N'F'
UNION ALL SELECT N'ARPayment', COUNT_BIG(*), SUM(CASE WHEN PaymentAmt - KnockOffAmt - RefundAmt <> 0 THEN 1 ELSE 0 END) FROM dbo.ARPayment WHERE Cancelled = N'F'
UNION ALL SELECT N'ARDeposit', COUNT_BIG(*), SUM(CASE WHEN Outstanding <> 0 THEN 1 ELSE 0 END) FROM dbo.ARDeposit WHERE Cancelled = N'F'
UNION ALL SELECT N'APInvoice', COUNT_BIG(*), SUM(CASE WHEN Outstanding <> 0 THEN 1 ELSE 0 END) FROM dbo.APInvoice WHERE Cancelled = N'F'
UNION ALL SELECT N'APDN', COUNT_BIG(*), SUM(CASE WHEN Outstanding <> 0 THEN 1 ELSE 0 END) FROM dbo.APDN WHERE Cancelled = N'F'
UNION ALL SELECT N'APCN', COUNT_BIG(*), SUM(CASE WHEN NetTotal - KnockOffAmt - RefundAmt <> 0 THEN 1 ELSE 0 END) FROM dbo.APCN WHERE Cancelled = N'F'
UNION ALL SELECT N'APPayment', COUNT_BIG(*), SUM(CASE WHEN PaymentAmt - KnockOffAmt - RefundAmt <> 0 THEN 1 ELSE 0 END) FROM dbo.APPayment WHERE Cancelled = N'F'
UNION ALL SELECT N'APDeposit', COUNT_BIG(*), SUM(CASE WHEN Outstanding <> 0 THEN 1 ELSE 0 END) FROM dbo.APDeposit WHERE Cancelled = N'F';

-- 2. Knock-off target type codes in use (for knockoff_target_types), by knock-off table.
SELECT N'ARPaymentKnockOff' AS KnockOffTable, KnockOffDocType, COUNT_BIG(*) AS KnockOffRows FROM dbo.ARPaymentKnockOff GROUP BY KnockOffDocType
UNION ALL SELECT N'ARCNKnockOff', KnockOffDocType, COUNT_BIG(*) FROM dbo.ARCNKnockOff GROUP BY KnockOffDocType
UNION ALL SELECT N'ARContraKnockOff', KnockOffDocType, COUNT_BIG(*) FROM dbo.ARContraKnockOff GROUP BY KnockOffDocType
UNION ALL SELECT N'APPaymentKnockOff', KnockOffDocType, COUNT_BIG(*) FROM dbo.APPaymentKnockOff GROUP BY KnockOffDocType
UNION ALL SELECT N'APCNKnockOff', KnockOffDocType, COUNT_BIG(*) FROM dbo.APCNKnockOff GROUP BY KnockOffDocType
UNION ALL SELECT N'APContraKnockOff', KnockOffDocType, COUNT_BIG(*) FROM dbo.APContraKnockOff GROUP BY KnockOffDocType;

-- 3. AR invoices whose stored Outstanding differs from NetTotal minus knock-offs (expect 0).
WITH ko AS (
    SELECT k.KnockOffDocKey, SUM(k.Amount + k.DiscountAmt) AS Amt
    FROM dbo.ARPaymentKnockOff AS k INNER JOIN dbo.ARPayment AS p ON p.DocKey = k.DocKey
    WHERE p.Cancelled = N'F' AND k.KnockOffDocType = N'RI' GROUP BY k.KnockOffDocKey
    UNION ALL
    SELECT k.KnockOffDocKey, SUM(k.Amount) FROM dbo.ARCNKnockOff AS k INNER JOIN dbo.ARCN AS c ON c.DocKey = k.DocKey
    WHERE c.Cancelled = N'F' AND k.KnockOffDocType = N'RI' GROUP BY k.KnockOffDocKey
    UNION ALL
    SELECT k.KnockOffDocKey, SUM(k.Amount) FROM dbo.ARContraKnockOff AS k INNER JOIN dbo.ARAPContra AS t ON t.DocKey = k.DocKey
    WHERE t.Cancelled = N'F' AND k.KnockOffDocType = N'RI' GROUP BY k.KnockOffDocKey
),
ko_sum AS (SELECT KnockOffDocKey, SUM(Amt) AS Amt FROM ko GROUP BY KnockOffDocKey)
SELECT
    COUNT_BIG(*) AS LiveInvoices,
    SUM(CASE WHEN i.Outstanding <> i.NetTotal - ISNULL(s.Amt, 0) THEN 1 ELSE 0 END) AS OutstandingMismatches
FROM dbo.ARInvoice AS i
LEFT JOIN ko_sum AS s ON s.KnockOffDocKey = i.DocKey
WHERE i.Cancelled = N'F';

-- 4. ChangeCount rows the export reads (expect 15 rows, one per table).
SELECT COUNT_BIG(*) AS ChangeCountRows, COUNT(DISTINCT TableName) AS DistinctTables
FROM dbo.ChangeCount
WHERE TableName IN (N'ARInvoice', N'ARDN', N'ARCN', N'ARPayment', N'ARDeposit', N'ARRefund', N'ARAPContra',
    N'APInvoice', N'APDN', N'APCN', N'APPayment', N'APDeposit', N'APRefund', N'Debtor', N'Creditor');

-- 5. Per debtor (V3 compare with ar_parties_latest.csv): open debit total and unapplied credit total, local currency.
WITH items AS (
    SELECT DebtorCode AS PartyCode, LocalNetTotal - LocalPaymentAmt AS Debit, CAST(0 AS decimal(19, 2)) AS Credit
    FROM dbo.ARInvoice WHERE Cancelled = N'F' AND Outstanding <> 0
    UNION ALL
    SELECT DebtorCode, CAST(0 AS decimal(19, 2)), -LocalUnappliedAmount
    FROM dbo.ARPayment WHERE Cancelled = N'F' AND PaymentAmt - KnockOffAmt - RefundAmt <> 0
    UNION ALL
    SELECT DebtorCode, CAST(0 AS decimal(19, 2)), -CAST((NetTotal - KnockOffAmt - RefundAmt) * CurrencyRate AS decimal(19, 2))
    FROM dbo.ARCN WHERE Cancelled = N'F' AND NetTotal - KnockOffAmt - RefundAmt <> 0
)
SELECT PartyCode, COUNT_BIG(*) AS OpenItems, SUM(Debit) AS OpenDebitLocal, SUM(Credit) AS UnappliedLocal,
    SUM(Debit) + SUM(Credit) AS OutstandingLocal
FROM items GROUP BY PartyCode ORDER BY PartyCode;

-- 6. Per creditor (V3 compare with ap_parties_latest.csv), the same shape.
WITH items AS (
    SELECT CreditorCode AS PartyCode, LocalNetTotal - LocalPaymentAmt AS Debit, CAST(0 AS decimal(19, 2)) AS Credit
    FROM dbo.APInvoice WHERE Cancelled = N'F' AND Outstanding <> 0
    UNION ALL
    SELECT CreditorCode, CAST(0 AS decimal(19, 2)), -LocalUnappliedAmount
    FROM dbo.APPayment WHERE Cancelled = N'F' AND PaymentAmt - KnockOffAmt - RefundAmt <> 0
    UNION ALL
    SELECT CreditorCode, CAST(0 AS decimal(19, 2)), -CAST((NetTotal - KnockOffAmt - RefundAmt) * CurrencyRate AS decimal(19, 2))
    FROM dbo.APCN WHERE Cancelled = N'F' AND NetTotal - KnockOffAmt - RefundAmt <> 0
)
SELECT PartyCode, COUNT_BIG(*) AS OpenItems, SUM(Debit) AS OpenDebitLocal, SUM(Credit) AS UnappliedLocal,
    SUM(Debit) + SUM(Credit) AS OutstandingLocal
FROM items GROUP BY PartyCode ORDER BY PartyCode;
