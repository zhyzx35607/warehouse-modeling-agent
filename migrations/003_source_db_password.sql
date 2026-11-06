-- ================================================================
-- Wren Modeling Agent — Migration v3
-- Add encrypted_password column to source_db_registry
-- Uses SM4-CBC encryption, key stored in .env SM4_KEY
-- ================================================================

ALTER TABLE source_db_registry
    ADD COLUMN IF NOT EXISTS encrypted_password TEXT NOT NULL DEFAULT '';
