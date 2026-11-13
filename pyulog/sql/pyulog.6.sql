BEGIN;
CREATE TABLE IF NOT EXISTS ULogSupplement (
        Id INTEGER PRIMARY KEY AUTOINCREMENT,
        ULogId INT NOT NULL REFERENCES ULog (Id) ON DELETE CASCADE,
        SHA256Sum TEXT NOT NULL,
        StartTimestamp INT,
        LastTimestamp INT,
        Status TEXT NOT NULL,
        Reason TEXT,
        ConflictTopic TEXT,
        ConflictMultiId INT,
        ConflictTimestamp INT,
        AddedDatasetCount INT DEFAULT 0,
        AddedSampleCount INT DEFAULT 0,
        MergedAt TEXT,
        UNIQUE (ULogId, SHA256Sum)
);
CREATE INDEX IF NOT EXISTS btree_ULogSupplement_ULogId ON ULogSupplement(ULogId);
CREATE INDEX IF NOT EXISTS btree_ULogSupplement_SHA256Sum ON ULogSupplement(SHA256Sum);
COMMIT;
