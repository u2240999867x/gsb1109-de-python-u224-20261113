BEGIN;
-- Per-sample storage of data points, enabling lazy time-window queries
-- without loading (or growing memory with) the records outside the window.
-- Rows share the SeriesIndex ordering with the packed ValueArray BLOB in
-- ULogField, so the original time order and same-timestamp duplicate rows
-- are preserved per dataset instance and field.
CREATE TABLE IF NOT EXISTS ULogFieldSample (
        DatasetId INTEGER NOT NULL REFERENCES ULogDataset (Id) ON DELETE CASCADE,
        TopicName TEXT NOT NULL,
        SeriesIndex INTEGER NOT NULL,
        Timestamp INT NOT NULL,
        Value BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS btree_ULogFieldSample_DatasetId_Timestamp
    ON ULogFieldSample (DatasetId, Timestamp);
CREATE INDEX IF NOT EXISTS btree_ULogFieldSample_DatasetId_Field_Index
    ON ULogFieldSample (DatasetId, TopicName, SeriesIndex);
COMMIT;
