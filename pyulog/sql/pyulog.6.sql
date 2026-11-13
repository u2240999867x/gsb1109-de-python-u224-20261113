BEGIN;
CREATE TABLE IF NOT EXISTS Device (
        Id INTEGER PRIMARY KEY AUTOINCREMENT,
        Serial TEXT UNIQUE,
        Topic TEXT,
        Notes TEXT
);

CREATE TABLE IF NOT EXISTS DeviceReplacement (
        Id INTEGER PRIMARY KEY AUTOINCREMENT,
        Timestamp INT,
        Topic TEXT,
        Instance INT,
        OldSerial TEXT,
        NewSerial TEXT,
        Note TEXT
);

CREATE TABLE IF NOT EXISTS ULogDeviceSegment (
        Id INTEGER PRIMARY KEY AUTOINCREMENT,
        DatasetName TEXT,
        MultiId INT,
        StartTimestamp INT,
        EndTimestamp INT,
        Serial TEXT,
        Confirmed BOOLEAN,
        Candidates TEXT,
        Rationale TEXT,
        ULogId INT REFERENCES ULog (Id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS btree_ULogDeviceSegment_ULogId ON ULogDeviceSegment(ULogId);
COMMIT;
