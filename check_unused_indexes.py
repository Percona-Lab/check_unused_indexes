# Copyright 2026 Percona LLC or its affiliates.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

#!/usr/bin/env python3
"""Unused Index Verifier for MySQL Topologies, Aurora Clusters, & Galera/PXC Clusters.

Pass a primary instance alias or host:port string to automatically resolve all downstream replicas
(including Aurora Readers, standard async replicas, circular replication, and Galera/PXC cluster peers),
check sys.schema_unused_indexes across all resolved nodes (excluding FK-backing indexes),
and output ONLY indexes that are unused on EVERY single node in the topology.
"""

import argparse
import logging
import os
import sys
from typing import Dict, List, Optional, Set, Tuple

import pymysql
from pymysql.cursors import DictCursor


class Color:
    _CODES = {
        "RESET": "\033[0m",
        "BOLD": "\033[1m",
        "RED": "\033[91m",
        "GREEN": "\033[92m",
        "YELLOW": "\033[93m",
        "BLUE": "\033[94m",
        "CYAN": "\033[96m",
    }
    ENABLED = True
    RESET = _CODES["RESET"]
    BOLD = _CODES["BOLD"]
    RED = _CODES["RED"]
    GREEN = _CODES["GREEN"]
    YELLOW = _CODES["YELLOW"]
    BLUE = _CODES["BLUE"]
    CYAN = _CODES["CYAN"]

    @classmethod
    def configure(cls, mode: str) -> None:
        enabled = mode == "always"
        if mode == "auto":
            enabled = (
                sys.stdout.isatty()
                and os.environ.get("TERM", "").lower() != "dumb"
                and "NO_COLOR" not in os.environ
            )
        cls.ENABLED = enabled
        for name, code in cls._CODES.items():
            setattr(cls, name, code if enabled else "")


def parse_node_spec(node_str: str) -> Tuple[str, str, int]:
    """Parse node strings formatted as 'alias=ip:port', 'ip:port', or 'ip'."""
    node_str = node_str.strip()
    if "=" in node_str:
        alias, ip_port = node_str.split("=", 1)
        alias = alias.strip()
        ip_port = ip_port.strip()
    else:
        alias = node_str
        ip_port = node_str

    if ":" in ip_port:
        ip, port_str = ip_port.split(":", 1)
        port = int(port_str)
    else:
        ip = ip_port
        port = 3306

    return alias, ip, port


def load_inventory_file(filepath: str) -> Dict[str, Tuple[str, int]]:
    """Load inventory mapping from a file (format: alias=ip:port or ip:port per line)."""
    inventory_map = {}
    if not os.path.isfile(filepath):
        print(f"{Color.RED}Error: Inventory file '{filepath}' not found.{Color.RESET}")
        sys.exit(1)

    with open(filepath, "r", encoding="utf-8") as file:
        for line in file:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            alias, ip, port = parse_node_spec(line)
            inventory_map[alias] = (ip, port)

    return inventory_map


def connect_mysql(ip: str, port: int, cnf_path: str):
    """Establish connection to a MySQL instance."""
    try:
        return pymysql.connect(
            read_default_file=os.path.expanduser(cnf_path),
            host=ip,
            port=port,
            connect_timeout=5,
            cursorclass=DictCursor,
        )
    except pymysql.MySQLError as err:
        print(f"{Color.RED}[Connection Failed] {ip}:{port} -> {err}{Color.RESET}")
        return None


def get_server_metadata(conn) -> dict:
    """Fetch server metadata, replication source pointers, Aurora topology, and PXC/Galera cluster info."""
    with conn.cursor() as cursor:
        cursor.execute("SELECT @@server_id AS server_id, @@port AS port")
        res = cursor.fetchone()

        cursor.execute("SHOW GLOBAL STATUS LIKE 'Uptime'")
        uptime_res = cursor.fetchone()
        uptime = int(uptime_res["Value"]) if uptime_res else 0

        # Galera / PXC Cluster check
        wsrep_cluster_name = ""
        try:
            cursor.execute(
                "SELECT IF(@@wsrep_provider='none', '', @@wsrep_cluster_name) AS cluster_name;"
            )
            c_res = cursor.fetchone()
            if c_res and c_res.get("cluster_name"):
                wsrep_cluster_name = c_res["cluster_name"]
        except pymysql.MySQLError:
            pass

        # AWS Aurora Topology Check
        aurora_nodes = []
        is_aurora = False
        try:
            cursor.execute(
                "SELECT SERVER_ID, SESSION_ID FROM information_schema.replica_host_status"
            )
            rows = cursor.fetchall()
            if rows:
                is_aurora = True
                aurora_nodes = [r["SERVER_ID"] for r in rows if r.get("SERVER_ID")]
        except pymysql.MySQLError:
            pass

        source_host = None
        source_port = None

        for q in ["SHOW REPLICA STATUS", "SHOW SLAVE STATUS"]:
            try:
                cursor.execute(q)
                repl = cursor.fetchone()
                if repl:
                    source_host = repl.get("Master_Host") or repl.get("Source_Host")
                    source_port = repl.get("Master_Port") or repl.get("Source_Port")
                    break
            except pymysql.MySQLError:
                continue

        return {
            "server_id": res["server_id"],
            "port": res["port"],
            "uptime": uptime,
            "source_host": source_host,
            "source_port": source_port,
            "wsrep_cluster_name": wsrep_cluster_name,
            "is_aurora": is_aurora,
            "aurora_nodes": aurora_nodes,
        }


def get_unused_indexes(
    conn,
    ignored_schemas: Set[str],
    include_schemas: Optional[Set[str]] = None,
    include_tables: Optional[Set[str]] = None,
) -> List[Tuple[str, str, str]]:
    """Retrieve unused indexes from sys.schema_unused_indexes, strictly excluding FK indexes and applying table/schema filters."""
    unused = []
    where_clauses = []
    params = []

    # Filter out ignored schemas
    if ignored_schemas:
        placeholders = ", ".join(["%s"] * len(ignored_schemas))
        where_clauses.append(f"ui.object_schema NOT IN ({placeholders})")
        params.extend(list(ignored_schemas))

    # Include specific schemas
    if include_schemas:
        placeholders = ", ".join(["%s"] * len(include_schemas))
        where_clauses.append(f"ui.object_schema IN ({placeholders})")
        params.extend(list(include_schemas))

    # Include specific tables
    if include_tables:
        placeholders = ", ".join(["%s"] * len(include_tables))
        where_clauses.append(f"ui.object_name IN ({placeholders})")
        params.extend(list(include_tables))

    # Exclude Foreign Key backing indexes
    where_clauses.append(
        """NOT EXISTS (
            SELECT 1
            FROM information_schema.KEY_COLUMN_USAGE kcu
            JOIN information_schema.TABLE_CONSTRAINTS tc
              ON kcu.CONSTRAINT_SCHEMA = tc.CONSTRAINT_SCHEMA
             AND kcu.CONSTRAINT_NAME   = tc.CONSTRAINT_NAME
             AND kcu.TABLE_NAME        = tc.TABLE_NAME
            JOIN information_schema.STATISTICS s
              ON kcu.TABLE_SCHEMA = s.TABLE_SCHEMA
             AND kcu.TABLE_NAME   = s.TABLE_NAME
             AND kcu.COLUMN_NAME  = s.COLUMN_NAME
            WHERE tc.CONSTRAINT_TYPE = 'FOREIGN KEY'
              AND s.SEQ_IN_INDEX     = 1
              AND ui.object_schema   = s.TABLE_SCHEMA
              AND ui.object_name     = s.TABLE_NAME
              AND ui.index_name      = s.INDEX_NAME
        )"""
    )

    where_sql = " WHERE " + " AND ".join(where_clauses) if where_clauses else ""
    query = f"""
        SELECT ui.object_schema, ui.object_name, ui.index_name
        FROM sys.schema_unused_indexes ui
        {where_sql};
    """

    try:
        with conn.cursor() as cursor:
            cursor.execute(query, tuple(params))
            for row in cursor.fetchall():
                unused.append(
                    (row["object_schema"], row["object_name"], row["index_name"])
                )
    except pymysql.MySQLError as e:
        print(f"{Color.RED}Failed to query sys.schema_unused_indexes: {e}{Color.RESET}")
    return unused


def resolve_topology_downstream(
    primary_alias: str, inventory_map: dict, cnf_path: str
) -> dict:
    """Inspect all inventory servers and traverse downstream replication, Aurora clusters, + Galera/PXC clusters."""
    server_details = {}

    for alias, (ip, port) in inventory_map.items():
        conn = connect_mysql(ip, port, cnf_path)
        if conn:
            meta = get_server_metadata(conn)
            conn.close()
            server_details[alias] = {
                "alias": alias,
                "ip": ip,
                "port": port,
                "uptime": meta["uptime"],
                "source_host": meta["source_host"],
                "source_port": meta["source_port"],
                "cluster_name": meta["wsrep_cluster_name"],
                "is_aurora": meta["is_aurora"],
                "aurora_nodes": meta["aurora_nodes"],
            }

    # Match Replication Parents
    for alias, info in server_details.items():
        src_host = info["source_host"]
        src_port = info["source_port"]
        matched_parent = None
        if src_host:
            for p_alias, p_info in server_details.items():
                if p_info["ip"] == src_host and int(p_info["port"]) == int(src_port):
                    matched_parent = p_alias
                    break
        info["parent_alias"] = matched_parent

    resolved_topology = {}
    visited = set()

    def _dfs_downstream(current_alias):
        if current_alias in visited or current_alias not in server_details:
            return
        visited.add(current_alias)
        resolved_topology[current_alias] = server_details[current_alias]

        # 1. Traverse direct standard replication children
        for child_alias, child_info in server_details.items():
            if child_info.get("parent_alias") == current_alias:
                _dfs_downstream(child_alias)

        # 2. If current node is part of a PXC/Galera cluster, pull in all cluster peer nodes
        curr_cluster = server_details[current_alias].get("cluster_name")
        if curr_cluster:
            for peer_alias, peer_info in server_details.items():
                if peer_info.get("cluster_name") == curr_cluster:
                    _dfs_downstream(peer_alias)

        # 3. If current node is an Aurora node, pull in all cluster reader/writer members
        if server_details[current_alias].get("is_aurora"):
            aurora_node_ids = server_details[current_alias].get("aurora_nodes", [])
            for candidate_alias, candidate_info in server_details.items():
                if candidate_alias in visited:
                    continue

                # Check match by server ID or naming convention
                is_aurora_member = (
                    candidate_alias in aurora_node_ids
                    or candidate_info["ip"].split(".")[0] in aurora_node_ids
                    or candidate_alias.startswith(current_alias)
                    or current_alias.startswith(candidate_alias)
                )
                if is_aurora_member:
                    _dfs_downstream(candidate_alias)

    _dfs_downstream(primary_alias)
    return resolved_topology


def main():
    parser = argparse.ArgumentParser(
        description="Verify unused indexes across primary, all topology replicas (including Aurora Readers), and Galera/PXC cluster nodes."
    )
    parser.add_argument(
        "-p",
        "--primary",
        required=True,
        help="Primary instance alias (if using --inventory) or connection spec 'alias=10.0.0.1:3306' / '10.0.0.1:3306'",
    )
    parser.add_argument(
        "-r",
        "--replicas",
        default=None,
        help="Comma-separated replica connection specs (e.g. 'repl1=10.0.0.2:3306,10.0.0.3:3306')",
    )
    parser.add_argument(
        "-i",
        "--inventory",
        default=None,
        help="Path to inventory text file containing node mappings (format: alias=ip:port)",
    )
    parser.add_argument(
        "-c", "--config", default="~/.my.cnf", help="Path to MySQL config file"
    )
    parser.add_argument(
        "-u",
        "--min-uptime",
        dest="min_uptime_days",
        type=float,
        default=7.0,
        help="Minimum uptime threshold in days to avoid warnings (default: 7, set 0 to disable)",
    )
    parser.add_argument(
        "--action",
        choices=("invisible", "drop", "both"),
        default="invisible",
        help="Generated DDL action: 'invisible' (safest), 'drop', or 'both' (default: invisible)",
    )
    parser.add_argument(
        "--color",
        choices=("auto", "always", "never"),
        default="auto",
        help="Color output mode",
    )
    parser.add_argument(
        "--ignore-schema",
        default="sys,mysql,performance_schema,information_schema",
        help="Comma-separated schemas to ignore",
    )
    parser.add_argument(
        "-s",
        "--include-schema",
        "--schema",
        default=None,
        help="Comma-separated schemas/databases to explicitly target (e.g., db1,db2)",
    )
    parser.add_argument(
        "-t",
        "--include-table",
        "--table",
        default=None,
        help="Comma-separated tables to explicitly target (e.g., users,orders)",
    )
    parser.add_argument(
        "-qs",
        "--quiet-summary",
        action="store_true",
        help="Suppress the Step 3 visual table output and show only DDL statements",
    )
    args = parser.parse_args()

    Color.configure(args.color)
    ignored_schemas = set(args.ignore_schema.split(",")) if args.ignore_schema else set()
    include_schemas = set(args.include_schema.split(",")) if args.include_schema else None
    include_tables = set(args.include_table.split(",")) if args.include_table else None
    recommended_uptime_sec = args.min_uptime_days * 86400

    inventory_map: Dict[str, Tuple[str, int]] = {}

    # 1. Load from inventory file if specified
    if args.inventory:
        inventory_map.update(load_inventory_file(args.inventory))

    # 2. Parse Primary node
    primary_alias = args.primary
    if "=" in args.primary or ":" in args.primary:
        p_alias, p_ip, p_port = parse_node_spec(args.primary)
        inventory_map[p_alias] = (p_ip, p_port)
        primary_alias = p_alias

    # 3. Parse Replicas string if provided
    if args.replicas:
        for replica_spec in args.replicas.split(","):
            if replica_spec.strip():
                r_alias, r_ip, r_port = parse_node_spec(replica_spec)
                inventory_map[r_alias] = (r_ip, r_port)

    # Validate primary exists in loaded nodes
    if primary_alias not in inventory_map:
        print(
            f"{Color.RED}Error: Primary node '{primary_alias}' not found in inventory or CLI arguments.{Color.RESET}"
        )
        sys.exit(1)

    print(
        f"\n{Color.BOLD}{Color.CYAN}=== Step 1: Discovering Topology ==={Color.RESET}"
    )
    print(f"Mapping topology downstream from Primary: {Color.BOLD}{primary_alias}{Color.RESET}...")
    nodes = resolve_topology_downstream(primary_alias, inventory_map, args.config)

    print(f"\nResolved Topology ({len(nodes)} nodes): {', '.join(nodes.keys())}")

    print(
        f"\n{Color.BOLD}{Color.CYAN}=== Step 2: Checking Unused Indexes on Nodes ==={Color.RESET}"
    )

    node_unused_indexes: Dict[str, Set[Tuple[str, str, str]]] = {}
    uptime_warnings = []

    for alias, info in nodes.items():
        role_label = (
            f"{Color.GREEN}[PRIMARY]{Color.RESET}"
            if alias == primary_alias
            else f"{Color.YELLOW}[REPLICA/CLUSTER]{Color.RESET}"
        )
        uptime_days = round(info["uptime"] / 86400, 1)

        print(
            f"Checking {role_label} {Color.BOLD}{alias}{Color.RESET} ({info['ip']}:{info['port']}) - Uptime: {uptime_days} days"
        )

        if recommended_uptime_sec > 0 and info["uptime"] < recommended_uptime_sec:
            uptime_warnings.append((alias, uptime_days))

        conn = connect_mysql(info["ip"], info["port"], args.config)
        if conn:
            unused = set(
                get_unused_indexes(
                    conn,
                    ignored_schemas=ignored_schemas,
                    include_schemas=include_schemas,
                    include_tables=include_tables,
                )
            )
            node_unused_indexes[alias] = unused
            print(f"  └─ Found {len(unused)} unused index candidates on this node.")
            conn.close()

    if uptime_warnings:
        print(
            f"\n{Color.BOLD}{Color.YELLOW}⚠️  UPTIME WARNING (Threshold: {args.min_uptime_days} days):{Color.RESET}"
        )
        for alias, days in uptime_warnings:
            print(
                f"  - Node '{alias}' has been up for only {days} days. Index usage statistics might be incomplete."
            )

    if not node_unused_indexes:
        print(f"\n{Color.RED}No nodes were successfully checked.{Color.RESET}")
        sys.exit(1)

    primary_unused = node_unused_indexes.get(primary_alias, set())
    candidates = set(primary_unused)

    for alias, unused_set in node_unused_indexes.items():
        candidates.intersection_update(unused_set)

    if not candidates:
        print(
            f"\n{Color.GREEN}No unused indexes found across the entire topology.{Color.RESET}\n"
        )
        return

    # Step 3 Output (Suppressed if --quiet-summary / -qs is provided)
    if not args.quiet_summary:
        print(
            f"\n{Color.BOLD}{Color.CYAN}=== Step 3: Intersecting Unused Indexes Across Topology ==={Color.RESET}"
        )
        print(
            f"\n{Color.BOLD}Safe Target Indexes (Unused across ALL {len(node_unused_indexes)} nodes):{Color.RESET}"
        )
        print("=" * 80)
        print(f"{'SCHEMA':<20} | {'TABLE':<30} | {'INDEX NAME':<25}")
        print("-" * 80)
        for schema, table, idx in sorted(list(candidates)):
            print(f"{schema:<20} | {table:<30} | {Color.RED}{idx:<25}{Color.RESET}")

        print("=" * 80)
        print(
            f"\n{Color.BOLD}{Color.GREEN}Total Candidate Indexes: {len(candidates)}{Color.RESET}\n"
        )

    print(f"\n{Color.BOLD}Generated DDL Statements (Execute on Primary):{Color.RESET}")
    print("-" * 80)
    for schema, table, idx in sorted(list(candidates)):
        if args.action in ("invisible", "both"):
            print(f"ALTER TABLE `{schema}`.`{table}` ALTER INDEX `{idx}` INVISIBLE;")
        if args.action in ("drop", "both"):
            print(f"ALTER TABLE `{schema}`.`{table}` DROP INDEX `{idx}`;")
    print("-" * 80 + "\n")


if __name__ == "__main__":
    main()
