-- ============================================================================
-- Migration 001: initial schema
-- Distributor Operations Agent — database v0.1
--
-- Source of truth: "Database Design v0.1 — Final" report.
-- Target: Supabase PostgreSQL (PG13+; gen_random_uuid() built-in).
--
-- NOT EXECUTED by this task. Apply via migration runner (future) or psql:
--   psql "$SUPABASE_DB_URL" -f src/app/database/migrations/001_initial_schema.sql
--
-- ----------------------------------------------------------------------------
-- BUSINESS CONSTRAINTS ENFORCED HERE
--   * UUID primary keys, NUMERIC(14,2) money, TIMESTAMPTZ timestamps.
--   * One warehouse (implicit; no warehouse table in v0.1).
--   * Phone globally unique, E.164 format (+country, max 15 digits total).
--   * Product alias globally unique, case-insensitive on lower(btrim(alias)):
--     'Aqua Botol' and 'aqua botol' collide -> rejected.
--   * available_stock = on_hand_stock - reserved_stock (generated, read-only).
--   * Draft orders: no inventory reservation, no credit consumption.
--   * Confirmed orders: reserve inventory, consume credit capacity.
--   * Delivery: on_hand -= qty, reserved -= qty, outstanding_balance += total.
--   * Delivered orders are terminal: no cancellation (status vocabulary +
--     timestamp matrix make inconsistent states unwritable).
--   * order_items.unit_price is a snapshot at order creation; price changes
--     never touch existing orders.
--   * subtotal = quantity * unit_price (generated).
--   * reserved_stock <= on_hand_stock always: available_stock can never go
--     negative, even under buggy application code.
--
-- ----------------------------------------------------------------------------
-- SERVICE-LEVEL VALIDATION (cannot be enforced cheaply in SQL — REQUIRED
-- in repositories/services before executing the transitions):
--   * An order MUST have at least one order_item before confirmation.
--     (No native constraint for "child row count > 0" without triggers that
--     would block multi-statement inserts. Confirm service must reject
--     orders with zero items.)
--   * orders.total_amount must equal SUM(order_items.subtotal); maintained
--     inside the same transaction as item writes.
--
-- ----------------------------------------------------------------------------
-- TRANSACTION REQUIREMENTS for future services (lock discipline):
--   * Confirm:     lock customer, order, inventory. Delivery: same.
--                  Confirmed cancellation: same.
--   * Always lock the customer row FIRST, then inventory rows.
--   * Lock inventory rows in ASCENDING product_id order (deadlock avoidance).
--   * ONE database transaction per state transition, commit-or-rollback-all.
--   * Use guarded status updates, e.g.
--       UPDATE orders SET status='confirmed', confirmed_at=now()
--       WHERE id = $1 AND status = 'draft';
--     Zero rows affected => transition already happened or is illegal.
--     Prevents duplicate confirmation / duplicate delivery effects: second
--     writer's guarded update matches nothing, transaction aborts before
--     stock or balance is touched.
--   * Handle PostgreSQL deadlock SQLSTATE 40P01 (and serialization failure
--     40001) as retryable errors in the service layer.
--   * Parameterized SQL only in repositories (no string interpolation).
-- ============================================================================

-- Order numbers: human-readable 'ORD-00000001'.. sequence (gaps possible on
-- rollback — cosmetic only).
CREATE SEQUENCE order_number_seq START 1;

-- ----------------------------------------------------------------------------
-- 1. price_lists (created BEFORE customers: customers references it)
-- ----------------------------------------------------------------------------
CREATE TABLE price_lists (
    id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    code       text        NOT NULL UNIQUE,
    name       text        NOT NULL,
    is_active  boolean     NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT price_lists_code_not_blank CHECK (length(btrim(code)) > 0),
    CONSTRAINT price_lists_name_not_blank CHECK (length(btrim(name)) > 0)
);

-- ----------------------------------------------------------------------------
-- 2. customers
-- ----------------------------------------------------------------------------
CREATE TABLE customers (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    code                text          NOT NULL UNIQUE,
    name                text          NOT NULL,
    phone               text          NOT NULL UNIQUE,               -- E.164, globally unique
    address             text,
    credit_limit        numeric(14,2) NOT NULL DEFAULT 0 CHECK (credit_limit >= 0),
    outstanding_balance numeric(14,2) NOT NULL DEFAULT 0 CHECK (outstanding_balance >= 0),
    -- NULL => 'GENERAL' price list fallback (service-level resolution)
    price_list_id       uuid REFERENCES price_lists(id) ON DELETE RESTRICT,
    is_active           boolean       NOT NULL DEFAULT true,
    created_at          timestamptz   NOT NULL DEFAULT now(),
    updated_at          timestamptz   NOT NULL DEFAULT now(),
    CONSTRAINT customers_code_not_blank CHECK (length(btrim(code)) > 0),
    CONSTRAINT customers_name_not_blank CHECK (length(btrim(name)) > 0),
    -- E.164: '+' then country code [1-9], up to 15 digits total
    CONSTRAINT customers_phone_e164 CHECK (phone ~ '^\+[1-9][0-9]{1,14}$')
);

-- ----------------------------------------------------------------------------
-- 3. products
-- ----------------------------------------------------------------------------
CREATE TABLE products (
    id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    sku        text        NOT NULL UNIQUE,
    name       text        NOT NULL,
    unit       text        NOT NULL,                                -- pcs / carton / dus ...
    is_active  boolean     NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT products_sku_not_blank  CHECK (length(btrim(sku)) > 0),
    CONSTRAINT products_name_not_blank CHECK (length(btrim(name)) > 0),
    CONSTRAINT products_unit_not_blank CHECK (length(btrim(unit)) > 0)
);

-- ----------------------------------------------------------------------------
-- 4. product_aliases
-- ----------------------------------------------------------------------------
CREATE TABLE product_aliases (
    id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    product_id uuid NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    alias      text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT product_aliases_alias_not_blank CHECK (length(btrim(alias)) > 0)
);

-- Global case-insensitive alias uniqueness:
-- 'Aqua Botol' and 'aqua botol' are duplicates -> second insert rejected.
CREATE UNIQUE INDEX product_aliases_alias_lower_uniq
    ON product_aliases (lower(btrim(alias)));

-- ----------------------------------------------------------------------------
-- 5. inventory (single warehouse v0.1; one row per product)
-- ----------------------------------------------------------------------------
CREATE TABLE inventory (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    product_id      uuid    NOT NULL UNIQUE REFERENCES products(id) ON DELETE CASCADE,
    on_hand_stock   integer NOT NULL DEFAULT 0 CHECK (on_hand_stock >= 0),
    reserved_stock  integer NOT NULL DEFAULT 0 CHECK (reserved_stock >= 0),
    -- available_stock = on_hand_stock - reserved_stock (read-only, computed)
    available_stock integer GENERATED ALWAYS AS (on_hand_stock - reserved_stock) STORED,
    updated_at      timestamptz NOT NULL DEFAULT now(),
    -- Backstop: reservation can never exceed physical stock, therefore
    -- available_stock can never be negative, even under buggy app code.
    CONSTRAINT inventory_reserved_le_on_hand CHECK (reserved_stock <= on_hand_stock)
);

-- ----------------------------------------------------------------------------
-- 6. product_prices (current price per product per list; order snapshots
--    replace price history in v0.1)
-- ----------------------------------------------------------------------------
CREATE TABLE product_prices (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    price_list_id uuid NOT NULL REFERENCES price_lists(id) ON DELETE CASCADE,
    product_id    uuid NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    unit_price    numeric(14,2) NOT NULL CHECK (unit_price >= 0),
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT product_prices_unique UNIQUE (price_list_id, product_id)
);

-- ----------------------------------------------------------------------------
-- 7. orders
-- ----------------------------------------------------------------------------
CREATE TABLE orders (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    order_number text NOT NULL UNIQUE
                 DEFAULT 'ORD-' || lpad(nextval('order_number_seq')::text, 8, '0'),
    customer_id  uuid NOT NULL REFERENCES customers(id) ON DELETE RESTRICT,
    status       text NOT NULL DEFAULT 'draft'
                 CONSTRAINT orders_status_allowed
                     CHECK (status IN ('draft', 'confirmed', 'cancelled', 'delivered')),
    total_amount numeric(14,2) NOT NULL DEFAULT 0 CHECK (total_amount >= 0),
    notes        text,
    confirmed_at timestamptz,
    cancelled_at timestamptz,
    delivered_at timestamptz,
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now(),
    -- Status/timestamp matrix:
    --   draft      : no timestamps at all
    --   confirmed  : confirmed_at only
    --   cancelled  : cancelled_at set; confirmed_at free (NULL if cancelled
    --                as draft, populated if cancelled after confirmation);
    --                delivered_at always NULL (delivered orders terminal)
    --   delivered  : confirmed_at AND delivered_at, no cancelled_at
    CONSTRAINT orders_status_timestamps CHECK (
        (status = 'draft'     AND confirmed_at IS NULL AND cancelled_at IS NULL AND delivered_at IS NULL)
     OR (status = 'confirmed' AND confirmed_at IS NOT NULL AND cancelled_at IS NULL AND delivered_at IS NULL)
     OR (status = 'cancelled' AND cancelled_at IS NOT NULL AND delivered_at IS NULL)
     OR (status = 'delivered' AND confirmed_at IS NOT NULL AND delivered_at IS NOT NULL AND cancelled_at IS NULL)
    ),
    CONSTRAINT orders_delivered_after_confirmed
        CHECK (delivered_at IS NULL OR delivered_at >= confirmed_at)
);

-- ----------------------------------------------------------------------------
-- 8. order_items (unit_price = snapshot at order creation)
-- ----------------------------------------------------------------------------
CREATE TABLE order_items (
    id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    order_id   uuid NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    product_id uuid NOT NULL REFERENCES products(id) ON DELETE RESTRICT,
    quantity   integer NOT NULL CHECK (quantity > 0),
    unit_price numeric(14,2) NOT NULL CHECK (unit_price >= 0),      -- snapshot [C]
    subtotal   numeric(14,2) GENERATED ALWAYS AS (quantity * unit_price) STORED
               CHECK (subtotal >= 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT order_items_unique UNIQUE (order_id, product_id)
);

-- ----------------------------------------------------------------------------
-- Indexes for search / FK support.
-- (UNIQUE constraints already create indexes on: customers.code, customers.phone,
--  products.sku, price_lists.code, inventory.product_id, orders.order_number,
--  product_prices(price_list_id, product_id), order_items(order_id, product_id).)
-- ----------------------------------------------------------------------------
CREATE INDEX customers_name_lower_idx    ON customers (lower(name));            -- name search
CREATE INDEX customers_price_list_idx    ON customers (price_list_id);          -- FK
CREATE INDEX products_name_lower_idx     ON products (lower(name));             -- name search
CREATE INDEX product_aliases_product_idx ON product_aliases (product_id);       -- FK
CREATE INDEX product_prices_product_idx  ON product_prices (product_id);        -- FK
CREATE INDEX orders_customer_status_idx  ON orders (customer_id, status);       -- credit exposure SUM
CREATE INDEX orders_status_created_idx   ON orders (status, created_at DESC);   -- listing
CREATE INDEX order_items_product_idx     ON order_items (product_id);           -- FK

-- ----------------------------------------------------------------------------
-- updated_at maintenance (all mutable tables)
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_customers_upd      BEFORE UPDATE ON customers      FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_products_upd       BEFORE UPDATE ON products       FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_inventory_upd      BEFORE UPDATE ON inventory      FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_price_lists_upd    BEFORE UPDATE ON price_lists    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_product_prices_upd BEFORE UPDATE ON product_prices FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_orders_upd         BEFORE UPDATE ON orders         FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ----------------------------------------------------------------------------
-- Seed: GENERAL price list (system row, not dummy business data).
-- Required for customers.price_list_id NULL fallback.
-- ON CONFLICT guard: idempotent if re-applied.
-- ----------------------------------------------------------------------------
INSERT INTO price_lists (code, name) VALUES ('GENERAL', 'Default price list')
ON CONFLICT (code) DO NOTHING;
