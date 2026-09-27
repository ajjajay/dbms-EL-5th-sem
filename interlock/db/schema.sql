-- Interlock schema.
--
-- Design document section 7. The central decision is that part, revision, shape
-- and occurrence are four separate things:
--
--   part        durable identity, what a part number names
--   revision    one frozen state of a part
--   shape       one distinct piece of geometry, shared by any number of revisions
--   occurrence  one appearance of a child revision inside a parent, with a pose
--
-- Conflating any two of them is the mistake that makes homemade parts databases
-- collapse. Forty identical bolts are one part row, one shape row, one revision
-- row and forty occurrence rows.
--
-- Units throughout: length mm, mass g, density g/mm^3, angle rad.
-- Written for SQLite; the only non-portable pieces are the STRICT keyword and
-- the trigger bodies, both flagged where they appear.

PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

-- ---------------------------------------------------------------- ownership

CREATE TABLE IF NOT EXISTS team (
    team_id      TEXT PRIMARY KEY,
    name         TEXT NOT NULL UNIQUE,
    contact      TEXT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

-- --------------------------------------------------------------- the commit

-- Append-only. Enforced by trigger below, not by convention.
CREATE TABLE IF NOT EXISTS commit_log (
    commit_id    TEXT PRIMARY KEY,
    author       TEXT NOT NULL,
    team_id      TEXT REFERENCES team(team_id),
    message      TEXT NOT NULL DEFAULT '',
    parent_root  TEXT,                      -- root hash the commit was based on
    new_root     TEXT,                      -- root hash it produced
    verdict      TEXT NOT NULL CHECK (verdict IN ('landed','rejected','pending')),
    reason       TEXT,                      -- populated when verdict = 'rejected'
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_commit_created ON commit_log(created_at);
CREATE INDEX IF NOT EXISTS idx_commit_root    ON commit_log(new_root);

-- ------------------------------------------------------------------- shape

-- Keyed by the strict fingerprint. Deduplicated across the whole database: one
-- row per distinct piece of geometry however many parts or revisions use it.
--
-- The invariant columns are stored as ordinary floats and indexed, not folded
-- into the key. That is what makes the bounded cross-tool match a range query
-- instead of a hash lookup that cannot express "close enough".
CREATE TABLE IF NOT EXISTS shape (
    fingerprint      TEXT PRIMARY KEY,
    -- Two volumes, because they answer different questions and differ by about
    -- half a percent on a cylinder at the working mesh deflection.
    --   volume        from the triangulated mesh. Identity uses this, because
    --                 the whole invariant vector is computed from the same mesh
    --                 and must stay internally consistent (char_length is
    --                 literally volume^(1/3)).
    --   volume_exact  the kernel's own integration over the exact surfaces.
    --                 Mass rollups use this, because a mass budget is an
    --                 integrity constraint and should not carry tessellation
    --                 error. Found by cross-checking against FreeCAD.
    volume           REAL NOT NULL CHECK (volume > 0),
    volume_exact     REAL CHECK (volume_exact IS NULL OR volume_exact > 0),
    area             REAL NOT NULL CHECK (area > 0),
    char_length      REAL NOT NULL CHECK (char_length > 0),
    sphericity       REAL NOT NULL,
    j1               REAL NOT NULL,
    j2               REAL NOT NULL,
    j3               REAL NOT NULL,
    chirality        INTEGER NOT NULL CHECK (chirality IN (-1, 0, 1)),
    frame_stable     INTEGER NOT NULL CHECK (frame_stable IN (0, 1)),
    com_x            REAL NOT NULL,
    com_y            REAL NOT NULL,
    com_z            REAL NOT NULL,
    bbox_dx          REAL NOT NULL,
    bbox_dy          REAL NOT NULL,
    bbox_dz          REAL NOT NULL,
    -- Axis-aligned bounds in the shape's own frame. The extents above are enough
    -- for an envelope check; the corners are needed to place the box in an
    -- assembly for the interference prefilter without opening any geometry.
    bbox_xmin        REAL NOT NULL DEFAULT 0,
    bbox_ymin        REAL NOT NULL DEFAULT 0,
    bbox_zmin        REAL NOT NULL DEFAULT 0,
    bbox_xmax        REAL NOT NULL DEFAULT 0,
    bbox_ymax        REAL NOT NULL DEFAULT 0,
    bbox_zmax        REAL NOT NULL DEFAULT 0,
    n_faces          INTEGER NOT NULL,
    n_edges          INTEGER NOT NULL,
    n_vertices       INTEGER NOT NULL,
    n_shells         INTEGER NOT NULL,
    n_solids         INTEGER NOT NULL,
    face_histogram   TEXT NOT NULL,          -- JSON, surface kind -> count
    blob_path        TEXT,                   -- content-addressed geometry
    source_name      TEXT,
    source_backend   TEXT,
    mesh_triangles   INTEGER,
    notes            TEXT,
    created_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

-- The index that makes bounded matching cheap: candidates are the shapes whose
-- characteristic length falls in a narrow window, and there are almost never
-- more than a handful.
CREATE INDEX IF NOT EXISTS idx_shape_char_length ON shape(char_length);
CREATE INDEX IF NOT EXISTS idx_shape_volume      ON shape(volume);
CREATE INDEX IF NOT EXISTS idx_shape_chirality   ON shape(chirality, char_length);

-- Alternative identities for one shape: a second tool's export that matched
-- within tolerance rather than exactly. Keeping the rejected-but-equivalent
-- fingerprint means the next import of the same file is an immediate hit.
CREATE TABLE IF NOT EXISTS shape_alias (
    alias_fingerprint TEXT PRIMARY KEY,
    fingerprint       TEXT NOT NULL REFERENCES shape(fingerprint) ON DELETE CASCADE,
    char_length_error REAL NOT NULL,
    shape_error       REAL NOT NULL,
    corroborated      INTEGER NOT NULL DEFAULT 0,
    source_name       TEXT,
    created_at        TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_alias_target ON shape_alias(fingerprint);

-- --------------------------------------------------------------- features

-- Geometry reduced to rows at ingest. Nothing downstream opens a solid.
CREATE TABLE IF NOT EXISTS feature (
    feature_id   INTEGER PRIMARY KEY,
    fingerprint  TEXT NOT NULL REFERENCES shape(fingerprint) ON DELETE CASCADE,
    kind         TEXT NOT NULL CHECK (kind IN ('bore','plane','boss','cone')),
    axis_x       REAL, axis_y REAL, axis_z REAL,
    px           REAL, py REAL, pz REAL,
    radius       REAL,
    depth        REAL,
    area         REAL,
    sweep        REAL,
    through      INTEGER DEFAULT 0,       -- passes all the way through the part
    complete     INTEGER DEFAULT 1,       -- angular sweep is a full turn (not a slot)
    chamfered    INTEGER DEFAULT 0,
    counterbores TEXT                       -- JSON array of larger radii
);

CREATE INDEX IF NOT EXISTS idx_feature_shape  ON feature(fingerprint, kind);
-- Interface matching is a range join on radius and a direction comparison.
CREATE INDEX IF NOT EXISTS idx_feature_radius ON feature(kind, radius);

-- ------------------------------------------------------------ part, revision

CREATE TABLE IF NOT EXISTS part (
    part_id      TEXT PRIMARY KEY,
    part_number  TEXT NOT NULL UNIQUE,
    description  TEXT NOT NULL DEFAULT '',
    team_id      TEXT NOT NULL REFERENCES team(team_id),
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_part_team ON part(team_id);

-- A revision is never updated in place; a change makes a new row. That is what
-- keeps the commit log meaningful and the Merkle hashes stable. Enforced by
-- trigger, because a convention nobody enforces is not a guarantee.
CREATE TABLE IF NOT EXISTS revision (
    revision_id    TEXT PRIMARY KEY,
    part_id        TEXT NOT NULL REFERENCES part(part_id),
    revision_index INTEGER NOT NULL,
    fingerprint    TEXT REFERENCES shape(fingerprint),   -- null for pure assemblies
    commit_id      TEXT NOT NULL REFERENCES commit_log(commit_id),
    status         TEXT NOT NULL DEFAULT 'draft'
                   CHECK (status IN ('draft','released','superseded','obsolete')),
    material       TEXT,
    density        REAL CHECK (density IS NULL OR density > 0),
    merkle_hash    TEXT,                    -- hash of this revision's whole subtree
    is_assembly    INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (part_id, revision_index)
);

CREATE INDEX IF NOT EXISTS idx_revision_part   ON revision(part_id, revision_index DESC);
CREATE INDEX IF NOT EXISTS idx_revision_shape  ON revision(fingerprint);
CREATE INDEX IF NOT EXISTS idx_revision_merkle ON revision(merkle_hash);
CREATE INDEX IF NOT EXISTS idx_revision_commit ON revision(commit_id);

-- The mutable pointer the design document never names. Revisions and commits are
-- immutable, so something has to say which revision is current, and that
-- something is what optimistic concurrency actually contends over.
CREATE TABLE IF NOT EXISTS ref (
    ref_name     TEXT PRIMARY KEY,
    revision_id  TEXT NOT NULL REFERENCES revision(revision_id),
    root_hash    TEXT,
    updated_at   TEXT NOT NULL DEFAULT (datetime('now')),
    updated_by   TEXT
);

-- -------------------------------------------------------------- occurrence

-- The assembly graph. Quantity, position and context belong to the appearance,
-- not to the part, which is why this is its own table.
CREATE TABLE IF NOT EXISTS occurrence (
    occurrence_id  INTEGER PRIMARY KEY,
    parent_rev     TEXT NOT NULL REFERENCES revision(revision_id) ON DELETE CASCADE,
    child_rev      TEXT NOT NULL REFERENCES revision(revision_id),
    instance_name  TEXT NOT NULL DEFAULT '',
    quantity       INTEGER NOT NULL DEFAULT 1 CHECK (quantity > 0),
    -- Row-major 4x4 placement of the child relative to the parent.
    m00 REAL NOT NULL, m01 REAL NOT NULL, m02 REAL NOT NULL, m03 REAL NOT NULL,
    m10 REAL NOT NULL, m11 REAL NOT NULL, m12 REAL NOT NULL, m13 REAL NOT NULL,
    m20 REAL NOT NULL, m21 REAL NOT NULL, m22 REAL NOT NULL, m23 REAL NOT NULL,
    transform_key  TEXT NOT NULL,           -- quantised, what the Merkle hash uses
    sort_key       TEXT NOT NULL,           -- makes child order irrelevant
    CHECK (parent_rev <> child_rev)         -- catches the one-hop cycle only
);

-- The two indexes that carry the traversal load, one per direction.
CREATE INDEX IF NOT EXISTS idx_occ_down ON occurrence(parent_rev, child_rev);
CREATE INDEX IF NOT EXISTS idx_occ_up   ON occurrence(child_rev, parent_rev);

-- ------------------------------------------------- contracts and interfaces

-- The public face of a revision. Other teams may depend only on this.
CREATE TABLE IF NOT EXISTS contract (
    contract_id  TEXT PRIMARY KEY,
    revision_id  TEXT NOT NULL UNIQUE REFERENCES revision(revision_id) ON DELETE CASCADE,
    envelope_dx  REAL CHECK (envelope_dx IS NULL OR envelope_dx > 0),
    envelope_dy  REAL CHECK (envelope_dy IS NULL OR envelope_dy > 0),
    envelope_dz  REAL CHECK (envelope_dz IS NULL OR envelope_dz > 0),
    mass_max     REAL CHECK (mass_max IS NULL OR mass_max > 0),
    cg_window    TEXT,                      -- JSON box in the datum frame
    datums       TEXT,                      -- JSON named reference frames
    attributes   TEXT NOT NULL DEFAULT '{}',-- JSON: power_w, thermal_w, material...
    contract_hash TEXT NOT NULL,            -- classification compares this
    provenance   TEXT NOT NULL DEFAULT 'declared'
                 CHECK (provenance IN ('declared','auto_drafted')),
    reviewed     INTEGER NOT NULL DEFAULT 1 CHECK (reviewed IN (0,1)),
    declared_by  TEXT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_contract_hash ON contract(contract_hash);

-- What a revision offers.
CREATE TABLE IF NOT EXISTS interface (
    interface_id  TEXT PRIMARY KEY,
    revision_id   TEXT NOT NULL REFERENCES revision(revision_id) ON DELETE CASCADE,
    name          TEXT NOT NULL,
    kind          TEXT NOT NULL DEFAULT 'bolt_pattern'
                  CHECK (kind IN ('bolt_pattern','shaft','bore','face','port')),
    hole_count    INTEGER,
    hole_radius   REAL,
    -- Seating plane and pattern frame, in the revision's own coordinates.
    ox REAL, oy REAL, oz REAL,
    nx REAL, ny REAL, nz REAL,
    -- Pattern point cloud as JSON, used by the registration step.
    points        TEXT,
    spacings      TEXT,                     -- JSON sorted pairwise distances
    fastener      TEXT,                     -- e.g. 'M6'
    fit_class     TEXT DEFAULT 'clearance'
                  CHECK (fit_class IN ('clearance','close','press','tapped')),
    radius_tol    REAL NOT NULL DEFAULT 0.1,
    spacing_tol   REAL NOT NULL DEFAULT 0.1,
    -- How the interface was located in the geometry (JSON selector). Keeping the
    -- selector rather than only the derived points is what lets a carried-forward
    -- declaration be re-run against changed geometry.
    selector      TEXT,
    -- 'auto_drafted' interfaces were derived from detected hole patterns with no
    -- human declaration, and are labelled unreviewed everywhere they surface.
    source        TEXT NOT NULL DEFAULT 'declared'
                  CHECK (source IN ('declared','auto_drafted')),
    UNIQUE (revision_id, name)
);

CREATE INDEX IF NOT EXISTS idx_interface_rev   ON interface(revision_id);
CREATE INDEX IF NOT EXISTS idx_interface_match ON interface(kind, hole_count, hole_radius);

-- What a parent assembly requires.
CREATE TABLE IF NOT EXISTS socket (
    socket_id     TEXT PRIMARY KEY,
    revision_id   TEXT NOT NULL REFERENCES revision(revision_id) ON DELETE CASCADE,
    name          TEXT NOT NULL,
    kind          TEXT NOT NULL DEFAULT 'bolt_pattern'
                  CHECK (kind IN ('bolt_pattern','shaft','bore','face','port')),
    hole_count    INTEGER,
    hole_radius   REAL,
    radius_tol    REAL NOT NULL DEFAULT 0.2,
    spacing_tol   REAL NOT NULL DEFAULT 0.2,
    ox REAL, oy REAL, oz REAL,
    nx REAL, ny REAL, nz REAL,
    points        TEXT,
    spacings      TEXT,
    fastener      TEXT,
    fit_class     TEXT DEFAULT 'clearance',
    -- Allowance the candidate must fit inside, in the socket's own frame.
    allow_dx      REAL, allow_dy REAL, allow_dz REAL,
    mass_budget   REAL,
    power_budget  REAL,
    thermal_budget REAL,
    -- Which appearance below the socket's assembly this socket is for, as an
    -- instance path ('drive/bracket'). A socket is written in its assembly's
    -- frame and may be filled by something several levels down, so matching has
    -- to compose transforms along this path.
    fills         TEXT,
    interface_name TEXT,                    -- which interface of the filler mates here
    -- How the socket's points were obtained, e.g. read off a sibling's interface
    -- and carried into this frame ('derived'), or written out ('declared').
    derivation    TEXT NOT NULL DEFAULT 'declared',
    derived_from  TEXT,
    UNIQUE (revision_id, name)
);

CREATE INDEX IF NOT EXISTS idx_socket_rev   ON socket(revision_id);
CREATE INDEX IF NOT EXISTS idx_socket_match ON socket(kind, hole_count, hole_radius);

-- A team declaring that it depends on somebody else's contract. This is what
-- turns a contract change into a named list of people to notify.
CREATE TABLE IF NOT EXISTS subscription (
    subscription_id INTEGER PRIMARY KEY,
    team_id         TEXT NOT NULL REFERENCES team(team_id),
    part_id         TEXT NOT NULL REFERENCES part(part_id),
    interface_name  TEXT,                   -- null means the whole contract
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (team_id, part_id, interface_name)
);

CREATE INDEX IF NOT EXISTS idx_subscription_part ON subscription(part_id);

-- ---------------------------------------------------------- tolerance chains

CREATE TABLE IF NOT EXISTS dimension (
    dimension_id TEXT PRIMARY KEY,
    revision_id  TEXT NOT NULL REFERENCES revision(revision_id) ON DELETE CASCADE,
    name         TEXT NOT NULL,
    nominal      REAL NOT NULL,
    tol_plus     REAL NOT NULL CHECK (tol_plus >= 0),
    tol_minus    REAL NOT NULL CHECK (tol_minus >= 0),
    units        TEXT NOT NULL DEFAULT 'mm',
    -- Sigma convention for the statistical total. Without this the RSS number
    -- means nothing, because it depends entirely on what the band represents.
    sigma_span   REAL NOT NULL DEFAULT 3.0 CHECK (sigma_span > 0),
    distribution TEXT NOT NULL DEFAULT 'normal'
                 CHECK (distribution IN ('normal','uniform','triangular')),
    datum_a      TEXT,
    datum_b      TEXT,
    UNIQUE (revision_id, name)
);

CREATE INDEX IF NOT EXISTS idx_dimension_rev ON dimension(revision_id);

CREATE TABLE IF NOT EXISTS chain (
    chain_id     TEXT PRIMARY KEY,
    name         TEXT NOT NULL UNIQUE,
    description  TEXT NOT NULL DEFAULT '',
    target_low   REAL,                      -- the requirement on the accumulated result
    target_high  REAL,
    method       TEXT NOT NULL DEFAULT 'worst_case'
                 CHECK (method IN ('worst_case','rss','monte_carlo')),
    owning_team  TEXT REFERENCES team(team_id),
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS chain_member (
    chain_id     TEXT NOT NULL REFERENCES chain(chain_id) ON DELETE CASCADE,
    seq          INTEGER NOT NULL,
    dimension_id TEXT NOT NULL REFERENCES dimension(dimension_id),
    direction    INTEGER NOT NULL CHECK (direction IN (-1, 1)),
    PRIMARY KEY (chain_id, seq)
);

CREATE INDEX IF NOT EXISTS idx_chain_member_dim ON chain_member(dimension_id);

-- ------------------------------------------------------- validation records

-- Every verdict the pipeline reached, kept so a rejection can be explained after
-- the fact and so the demonstration can show its working.
CREATE TABLE IF NOT EXISTS validation (
    validation_id INTEGER PRIMARY KEY,
    commit_id     TEXT NOT NULL REFERENCES commit_log(commit_id) ON DELETE CASCADE,
    stage         TEXT NOT NULL,
    passed        INTEGER NOT NULL CHECK (passed IN (0, 1)),
    constraint_name TEXT,
    detail        TEXT,
    other_team    TEXT,
    other_part    TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_validation_commit ON validation(commit_id);

-- How disruptive each landed commit was, per section 9's classification table.
CREATE TABLE IF NOT EXISTS change_classification (
    commit_id    TEXT NOT NULL REFERENCES commit_log(commit_id) ON DELETE CASCADE,
    part_id      TEXT NOT NULL REFERENCES part(part_id),
    contract_changed INTEGER NOT NULL CHECK (contract_changed IN (0,1)),
    body_changed     INTEGER NOT NULL CHECK (body_changed IN (0,1)),
    classification   TEXT NOT NULL
                     CHECK (classification IN ('no_op','internal','breaking')),
    PRIMARY KEY (commit_id, part_id)
);

-- Who was told about a breaking change, and whether they acknowledged.
CREATE TABLE IF NOT EXISTS notification (
    notification_id INTEGER PRIMARY KEY,
    commit_id    TEXT NOT NULL REFERENCES commit_log(commit_id) ON DELETE CASCADE,
    team_id      TEXT NOT NULL REFERENCES team(team_id),
    part_id      TEXT NOT NULL REFERENCES part(part_id),
    reason       TEXT NOT NULL,
    acknowledged INTEGER NOT NULL DEFAULT 0,
    acknowledged_by TEXT,
    acknowledged_at TEXT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_notification_team ON notification(team_id, acknowledged);

-- Interference results. Advisory rather than an integrity constraint, and
-- labelled as such: a quadratic check cannot run inside a write transaction, so
-- it cannot honestly be called enforcement.
CREATE TABLE IF NOT EXISTS interference (
    interference_id INTEGER PRIMARY KEY,
    root_rev     TEXT NOT NULL REFERENCES revision(revision_id) ON DELETE CASCADE,
    path_a       TEXT NOT NULL,
    path_b       TEXT NOT NULL,
    kind         TEXT NOT NULL CHECK (kind IN ('clash','clearance')),
    label        TEXT NOT NULL DEFAULT 'advisory',
    depth        REAL,
    volume       REAL,
    checked_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_interference_root ON interference(root_rev);

-- ------------------------------------------------------------------ outbox

-- Events that a commit produced and that belong in the document store: full
-- validation traces, notification detail, natural-language call logs, free-form
-- contract metadata. They are written here, in the same transaction as the commit
-- that caused them, and a single worker drains them into the document store
-- afterwards. That ordering is the whole reason the outbox exists: two stores
-- cannot share one transaction, so the document store is only ever written
-- *after* the SQL commit has landed, and nothing validation reads lives in it.
CREATE TABLE IF NOT EXISTS outbox (
    outbox_id    INTEGER PRIMARY KEY,
    topic        TEXT NOT NULL,
    doc_key      TEXT,
    payload      TEXT NOT NULL,             -- JSON
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    delivered_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_outbox_pending ON outbox(delivered_at, outbox_id);

-- --------------------------------------------------------------- integrity

-- Revisions are immutable. The whole Merkle and commit-log story rests on this,
-- so it is a database rule rather than a habit. Only the merkle hash and the
-- status may move, because both are assigned as a commit lands.
CREATE TRIGGER IF NOT EXISTS revision_is_immutable
BEFORE UPDATE OF part_id, revision_index, fingerprint, commit_id, material, density
ON revision
BEGIN
    SELECT RAISE(ABORT,
        'revision rows are immutable: create a new revision instead of editing one');
END;

-- A landed assembly's structure is part of what its Merkle hash names, so a
-- placement may not be edited in place either. A change is a new revision.
CREATE TRIGGER IF NOT EXISTS occurrence_is_immutable
BEFORE UPDATE OF parent_rev, child_rev, quantity, transform_key, sort_key,
                 m00, m01, m02, m03, m10, m11, m12, m13, m20, m21, m22, m23
ON occurrence
BEGIN
    SELECT RAISE(ABORT,
        'occurrence rows are immutable: create a new parent revision instead');
END;

CREATE TRIGGER IF NOT EXISTS commit_log_is_append_only
BEFORE DELETE ON commit_log
BEGIN
    SELECT RAISE(ABORT, 'the commit log is append-only');
END;

-- An assembly must never contain itself at any depth. A foreign key cannot say
-- this, because the violation may only appear after several hops, so the check
-- is a downward traversal from the proposed child looking for the proposed
-- parent. The depth guard is not optional: without it a cycle that somehow got
-- in would make this query run forever.
CREATE TRIGGER IF NOT EXISTS occurrence_forbids_cycles
BEFORE INSERT ON occurrence
WHEN EXISTS (
    WITH RECURSIVE descendant(rev, depth) AS (
        SELECT NEW.child_rev, 0
        UNION
        SELECT o.child_rev, d.depth + 1
        FROM occurrence o
        JOIN descendant d ON o.parent_rev = d.rev
        WHERE d.depth < 64
    )
    SELECT 1 FROM descendant WHERE rev = NEW.parent_rev
)
BEGIN
    SELECT RAISE(ABORT,
        'cycle rejected: the proposed parent already appears beneath the proposed child');
END;

-- --------------------------------------------------------------- convenience

CREATE VIEW IF NOT EXISTS v_current_revision AS
SELECT r.*
FROM revision r
JOIN (
    SELECT part_id, MAX(revision_index) AS top
    FROM revision
    GROUP BY part_id
) latest ON latest.part_id = r.part_id AND latest.top = r.revision_index;

CREATE VIEW IF NOT EXISTS v_part_mass AS
SELECT
    r.revision_id,
    r.part_id,
    p.part_number,
    p.team_id,
    COALESCE(s.volume_exact, s.volume) AS volume,
    r.density,
    COALESCE(s.volume_exact, s.volume) * COALESCE(r.density, 0.0) AS mass_g
FROM revision r
JOIN part p  ON p.part_id = r.part_id
LEFT JOIN shape s ON s.fingerprint = r.fingerprint;

CREATE VIEW IF NOT EXISTS v_cross_team_chain AS
SELECT
    c.chain_id,
    c.name,
    COUNT(DISTINCT p.team_id) AS team_count,
    GROUP_CONCAT(DISTINCT p.team_id) AS teams
FROM chain c
JOIN chain_member cm ON cm.chain_id = c.chain_id
JOIN dimension d     ON d.dimension_id = cm.dimension_id
JOIN revision r      ON r.revision_id = d.revision_id
JOIN part p          ON p.part_id = r.part_id
GROUP BY c.chain_id, c.name;
