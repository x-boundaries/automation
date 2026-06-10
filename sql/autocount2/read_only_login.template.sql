/*
  AutoCount 2 read-only login/user grant template only.

  DO NOT execute this file as-is.
  Replace placeholders, review with the DBA/operator, and run only after Phase 1
  wrapper views have reconciled against AutoCount UI/report outputs.

  This template intentionally grants SELECT only to approved wrapper views. It
  does not grant write/admin roles and does not write to AutoCount transaction
  tables.
*/

/* Option A: map an existing Windows account. */
-- USE [master];
-- IF NOT EXISTS (SELECT 1 FROM sys.server_principals WHERE name = N'DOMAIN\svc_autocount_readonly')
--     CREATE LOGIN [DOMAIN\svc_autocount_readonly] FROM WINDOWS;
-- GO
-- USE [AED_XBOUNDARIES];
-- IF NOT EXISTS (SELECT 1 FROM sys.database_principals WHERE name = N'DOMAIN\svc_autocount_readonly')
--     CREATE USER [DOMAIN\svc_autocount_readonly] FOR LOGIN [DOMAIN\svc_autocount_readonly];
-- GO

/* Option B: create a SQL login with a placeholder password. */
-- USE [master];
-- IF NOT EXISTS (SELECT 1 FROM sys.server_principals WHERE name = N'xb_autocount_readonly')
--     CREATE LOGIN [xb_autocount_readonly]
--     WITH PASSWORD = N'<REPLACE_WITH_STRONG_PASSWORD_STORED_OUTSIDE_GIT>',
--          CHECK_POLICY = ON,
--          CHECK_EXPIRATION = ON;
-- GO
-- USE [AED_XBOUNDARIES];
-- IF NOT EXISTS (SELECT 1 FROM sys.database_principals WHERE name = N'xb_autocount_readonly')
--     CREATE USER [xb_autocount_readonly] FOR LOGIN [xb_autocount_readonly];
-- GO

/* Deny/avoid elevated database roles for the chosen user. Review before use. */
-- ALTER ROLE db_owner DROP MEMBER [xb_autocount_readonly];
-- ALTER ROLE db_datawriter DROP MEMBER [xb_autocount_readonly];
-- ALTER ROLE db_ddladmin DROP MEMBER [xb_autocount_readonly];
-- ALTER ROLE db_securityadmin DROP MEMBER [xb_autocount_readonly];
-- ALTER ROLE db_accessadmin DROP MEMBER [xb_autocount_readonly];
-- ALTER ROLE db_backupoperator DROP MEMBER [xb_autocount_readonly];

/* Grant SELECT only after these wrapper views are approved by reconciliation. */
-- GRANT SELECT ON OBJECT::dbo.vw_XB_AC2_StockMaster_Phase1 TO [xb_autocount_readonly];
-- GRANT SELECT ON OBJECT::dbo.vw_XB_AC2_StockBalance_Phase1 TO [xb_autocount_readonly];
-- GRANT SELECT ON OBJECT::dbo.vw_XB_AC2_StockMovement_Phase1 TO [xb_autocount_readonly];
-- GRANT SELECT ON OBJECT::dbo.vw_XB_AC2_StockDocuments_Phase1 TO [xb_autocount_readonly];

/* Validation query ideas for the operator; do not paste credentials in results. */
-- SELECT USER_NAME() AS database_user_name;
-- SELECT IS_ROLEMEMBER('db_owner') AS is_db_owner,
--        IS_ROLEMEMBER('db_datawriter') AS is_db_datawriter,
--        IS_ROLEMEMBER('db_datareader') AS is_db_datareader;
