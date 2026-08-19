# Unused Index Verifier for MySQL Topologies

A safety-first Python tool designed to detect unused indexes across complex MySQL database topologies—including **AWS Aurora Clusters**, **Standard Asynchronous Replicas**, **Circular Replication**, and **Galera / Percona XtraDB Clusters (PXC)**.

Dropping an index based on statistics from a single instance is dangerous: an index might be idle on your Primary, but actively used for heavy reporting on a Read Replica or an Aurora Reader. `check_unused_indexes.py` queries `sys.schema_unused_indexes` on **every single node** in your downstream topology, filters out foreign key dependencies, and outputs **only indexes that are 100% unused everywhere**.

---

## Key Features

* **Topology Auto-Discovery:** Supply your pool of database nodes via CLI or inventory file. Point the script at your Primary, and it automatically inspects and traverses the downstream replication trees, Aurora reader nodes, and Galera/PXC cluster peers to select only the relevant servers in that primary's topology.
* **Cross-Topology Consensus:** Ensure an index is flagged only if it is unused on **all** discovered instances.
* **Foreign Key Protection:** Automatically queries `information_schema` to exclude indexes backing foreign key constraints (preventing accidental FK constraint drops/locking).
* **Uptime Safety Warning:** Warns you if any node in the topology has an uptime below a threshold (default: 7 days) to prevent false positives from newly restarted instances.
* **Safe DDL Generation:** Generates `ALTER INDEX ... INVISIBLE` statements by default (allowing easy rollback before permanent removal), with options for `DROP INDEX`.
* **Flexible Filtering:** Target specific schemas/tables or exclude internal system schemas (`sys`, `mysql`, `performance_schema`, `information_schema`).

---

## Requirements

* **Python:** 3.6+
* **Dependencies:** `pymysql`
* **MySQL Privileges:** Read access to `sys.schema_unused_indexes`, `information_schema`, and `SHOW REPLICA/SLAVE STATUS`.

### Installation

1. Clone the repository:
   ```bash
   git clone https://github.com/arunjitha/check_unused_indexes.git
   cd check_unused_indexes

2. Configure your MySQL credentials in ~/.my.cnf:

```
Ini, TOML
[client]
user=your_db_user
password=your_db_password
```

Usage1.

1. Simple Single Host / Direct Replica CLI
Pass your primary instance directly via the CLI:

``` Bash
python3 check_unused_indexes.py \
  --primary "primary=10.0.0.1:3306" \
  --replicas "repl1=10.0.0.2:3306,repl2=10.0.0.3:3306"
```

2. Using an Inventory File (Recommended for Large Topologies)Create an inventory.txt mapping file:

```Ini, TOML

# inventory.txt
db-primary=10.0.0.10:3306
db-replica-1=10.0.0.11:3306
db-replica-2=10.0.0.12:3306
db-analytics=10.0.0.13:3306
```

Run the script specifying your primary node alias from the file:

```Bash
python3 check_unused_indexes.py \
  --inventory inventory.txt \
  --primary db-primary
```
5. Targeting Specific Schemas or TablesRestrict checks to particular databases or tables:

```Bash
python3 check_unused_indexes.py \
  --primary "10.0.0.1:3306" \
  --include-schema "production_db,orders_db" \
  --include-table "users,orders"
```


Step 1: Discovering Topology ===

```
Mapping topology downstream from Primary: db-primary...
Resolved Topology (3 nodes): db-primary, db-replica-1, db-replica-2

=== Step 2: Checking Unused Indexes on Nodes ===
Checking [PRIMARY] db-primary (10.0.0.10:3306) - Uptime: 42.1 days
  └─ Found 12 unused index candidates on this node.
Checking [REPLICA/CLUSTER] db-replica-1 (10.0.0.11:3306) - Uptime: 42.1 days
  └─ Found 8 unused index candidates on this node.
Checking [REPLICA/CLUSTER] db-replica-2 (10.0.0.12:3306) - Uptime: 14.0 days
  └─ Found 5 unused index candidates on this node.

=== Step 3: Intersecting Unused Indexes Across Topology ===

Safe Target Indexes (Unused across ALL 3 nodes):
================================================================================
SCHEMA               | TABLE                          | INDEX NAME               
--------------------------------------------------------------------------------
app_db               | users                          | idx_created_at           
app_db               | orders                         | idx_legacy_status        
================================================================================

Total Candidate Indexes: 2

Generated DDL Statements (Execute on Primary):
--------------------------------------------------------------------------------
ALTER TABLE `app_db`.`users` ALTER INDEX `idx_created_at` INVISIBLE;
ALTER TABLE `app_db`.`orders` ALTER INDEX `idx_legacy_status` INVISIBLE;
--------------------------------------------------------------------------------
```
Safety RecommendationsAlways use ALTER INDEX ... INVISIBLE first. Mark indexes as invisible for a few days to ensure no unexpected queries degrade in performance before dropping them permanently.Verify Uptime. MySQL resets index statistics upon service restart. Ensure all servers have been running long enough to capture peak application workload cycles (e.g., weekly batch jobs).


   
