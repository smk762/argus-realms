-- Owners can name their cities; the name stays with the city when it changes hands.
ALTER TABLE hexes ADD COLUMN name text;
