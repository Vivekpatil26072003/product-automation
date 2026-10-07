-- Runtime role used by the API and workers. It is NOT a superuser and does NOT bypass
-- row-level security, so tenant policies are a real second barrier (spec §9).
CREATE ROLE prod_app LOGIN PASSWORD 'prod_app_dev' NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;

-- Isolated database for automated tests.
CREATE DATABASE production_test OWNER prod_owner;

GRANT CONNECT ON DATABASE production TO prod_app;
GRANT CONNECT ON DATABASE production_test TO prod_app;
