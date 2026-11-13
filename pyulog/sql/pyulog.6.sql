BEGIN;
ALTER TABLE ULogDataset ADD COLUMN UnknownOffset INT;
CREATE TABLE IF NOT EXISTS ULogDatasetTrailingData (
        Id INTEGER PRIMARY KEY AUTOINCREMENT,
        DatasetId INTEGER REFERENCES ULogDataset (Id) ON DELETE CASCADE,
        SampleIndex INT,
        Value BLOB
);
CREATE INDEX IF NOT EXISTS btree_ULogDatasetTrailingData_DatasetId
        ON ULogDatasetTrailingData(DatasetId);
COMMIT;
