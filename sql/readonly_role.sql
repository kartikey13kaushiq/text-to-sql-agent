-- SELECT-only role for the Text-to-SQL agent. Even if a generated statement slipped past the
-- static guard, this role cannot write, create objects, or run for longer than the timeout.
--   psql -v db=analytics -v schema=public -v password="'change-me'" -f sql/readonly_role.sql
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'text2sql_ro') THEN
        CREATE ROLE text2sql_ro LOGIN;
    END IF;
END $$;
ALTER ROLE text2sql_ro PASSWORD :password;
ALTER ROLE text2sql_ro SET default_transaction_read_only = on;
ALTER ROLE text2sql_ro SET statement_timeout = '5s';
ALTER ROLE text2sql_ro SET idle_in_transaction_session_timeout = '30s';
ALTER ROLE text2sql_ro CONNECTION LIMIT 10;

REVOKE ALL ON DATABASE :db FROM text2sql_ro;
GRANT CONNECT ON DATABASE :db TO text2sql_ro;
REVOKE CREATE ON SCHEMA :schema FROM PUBLIC;
GRANT USAGE ON SCHEMA :schema TO text2sql_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA :schema TO text2sql_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA :schema GRANT SELECT ON TABLES TO text2sql_ro;
