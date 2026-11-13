BEGIN;
-- Every raw INFO_MULTIPLE block is stored separately, including blocks that
-- could not be reconciled into a complete logical value. SeriesIndex is the
-- block order across the whole log and is what a logical value references.
CREATE TABLE IF NOT EXISTS ULogMessageInfoMultipleBlock (
        Id INTEGER PRIMARY KEY AUTOINCREMENT,
        SeriesIndex INTEGER,
        Key TEXT,
        Typename TEXT,
        IsContinued INTEGER,
        Value BLOB,
        ULogId INT REFERENCES ULog (Id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS btree_ULogMessageInfoMultipleBlock_ULogId
    ON ULogMessageInfoMultipleBlock(ULogId);

-- Links each reconciled list element back to the raw block it was built from.
-- Rows written by older schema versions leave this column NULL.
ALTER TABLE ULogMessageInfoMultipleListElement ADD COLUMN BlockSeriesIndex INTEGER;
COMMIT;
