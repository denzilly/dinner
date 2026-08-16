-- Phase 6: learned ingredient -> Picnic product mappings.
--
-- Picnic's search ranking cannot be trusted (the top hit for "olijfolie" is an
-- olive oil *spray*), so nothing is ever auto-selected. Each row here is a
-- human decision, made once and reused: either "this ingredient is that
-- product" or "never buy this via Picnic".
--
-- Keyed on ingredient_id rather than on a recipe line, because the grocery list
-- aggregates to ingredients before anything is bought -- "500 g rundergehakt"
-- from three different recipes is one purchase, and one decision.

CREATE TABLE IF NOT EXISTS picnic_products (
    ingredient_id    INTEGER PRIMARY KEY REFERENCES ingredients (id) ON DELETE CASCADE,

    -- 'mapped'  -- product_id and the pack_covers_* pair are set
    -- 'never'   -- deliberately not bought via Picnic (butcher, market, pantry)
    decision         TEXT NOT NULL,

    product_id       TEXT,
    -- Kept so a substituted or renamed product is visible rather than silent:
    -- ids get reused, and the name is what a human would notice changing.
    product_name     TEXT,

    -- How much of this ingredient ONE pack covers, in the *recipe's* units
    -- rather than Picnic's. This is what lets one division serve every case,
    -- including Picnic selling rookworst by weight while a recipe counts
    -- sausages: parse.py's units are deliberately non-interchangeable across
    -- dimensions, so a human resolves that once here and the code never has to.
    pack_covers_qty  REAL,
    pack_covers_unit TEXT,

    -- Picnic's own free-text quantity ("300 gram", "3 stuks"), kept only for
    -- diagnosing a bad mapping later. Never used for arithmetic.
    picnic_unit_text TEXT,

    confirmed_at     TEXT NOT NULL,

    CHECK (decision IN ('mapped', 'never')),
    CHECK (decision = 'never'
           OR (product_id IS NOT NULL
               AND pack_covers_qty > 0
               AND pack_covers_unit IS NOT NULL))
);
