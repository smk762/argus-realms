-- Veteran units (bots): kills since the unit last fell make it stronger and set its bounty.
ALTER TABLE units ADD COLUMN kills integer NOT NULL DEFAULT 0;
ALTER TABLE units ADD COLUMN veteran boolean NOT NULL DEFAULT false;
