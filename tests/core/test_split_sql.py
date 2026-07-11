"""Unit tests for ``split_sql`` (offline, no database).

Guards the root cause fixed in the "large schema / import fixes" milestone: a phpMyAdmin / mysqldump
file interleaves a comment block before every ``CREATE TABLE``. The old splitter left those comments
glued to the statement, and the apply step skipped anything starting with ``--`` — so every table was
silently dropped and the import failed on the first ``ALTER TABLE``. ``split_sql`` must now strip
comments and respect quoting so each real statement survives intact.
"""

from __future__ import annotations

from app.core.importer import schema_statements, split_sql


def test_strips_line_and_block_comments_keeping_statements():
    sql = """
    -- phpMyAdmin SQL Dump
    -- version 5.2.1
    /*!40101 SET NAMES utf8mb4 */;

    -- --------------------------------------------------------
    --
    -- Table structure for table `users`
    --
    CREATE TABLE `users` (`id` int NOT NULL);  # trailing mysql comment
    """
    stmts = split_sql(sql)
    # Comment blocks (incl. the version-gated /*! ... */ pragma) are removed entirely, not glued to the
    # CREATE; only the one real statement survives.
    assert len(stmts) == 1
    assert stmts[0].startswith("CREATE TABLE `users`")
    assert "--" not in stmts[0] and "#" not in stmts[0]


def test_semicolon_inside_string_literal_does_not_split():
    sql = "INSERT INTO `t` (`note`) VALUES ('a; b; c'); CREATE TABLE `x` (`id` int);"
    stmts = split_sql(sql)
    assert len(stmts) == 2
    assert stmts[0] == "INSERT INTO `t` (`note`) VALUES ('a; b; c')"
    assert stmts[1] == "CREATE TABLE `x` (`id` int)"


def test_double_quote_and_doubled_quote_escapes():
    # A ';' inside a double-quoted string, and a doubled '' escape, must not split.
    sql = """SET SQL_MODE = "NO;AUTO";
             INSERT INTO `t` (`a`) VALUES ('O''Brien; Jr');"""
    stmts = split_sql(sql)
    assert len(stmts) == 2
    assert stmts[0] == 'SET SQL_MODE = "NO;AUTO"'
    assert stmts[1] == "INSERT INTO `t` (`a`) VALUES ('O''Brien; Jr')"


def test_backtick_identifier_with_semicolon():
    sql = "CREATE TABLE `we;ird` (`id` int); SELECT 1;"
    stmts = split_sql(sql)
    assert stmts[0] == "CREATE TABLE `we;ird` (`id` int)"
    assert stmts[1] == "SELECT 1"


def test_postgres_dollar_quoting_is_preserved():
    sql = "CREATE FUNCTION f() RETURNS int AS $$ BEGIN RETURN 1; END; $$ LANGUAGE plpgsql; SELECT 1;"
    stmts = split_sql(sql)
    assert len(stmts) == 2
    assert stmts[0].startswith("CREATE FUNCTION f()") and "RETURN 1;" in stmts[0]
    assert stmts[1] == "SELECT 1"


def test_double_dash_without_space_is_not_a_comment():
    # `a--b` (no space after --) is an operator context, not a SQL line comment.
    sql = "SELECT 1--no comment here\n;"
    stmts = split_sql(sql)
    assert stmts == ["SELECT 1--no comment here"]


def test_no_statement_starts_with_a_comment():
    """The exact failure mode: nothing returned should begin with a comment marker."""
    sql = "-- header\n-- more\nCREATE TABLE `a` (`id` int);\n-- mid\nALTER TABLE `a` ADD KEY(`id`);"
    stmts = split_sql(sql)
    assert all(not s.lstrip().startswith(("--", "#", "/*")) for s in stmts)
    assert [s.split("(")[0].strip() for s in stmts] == ["CREATE TABLE `a`", "ALTER TABLE `a` ADD KEY"]


# --- schema_statements: a shadow import applies only DDL, never the data dump (bug §2) -------------


def test_schema_statements_drops_data_keeps_ddl():
    """The "Lost connection" root cause: a real dump's huge INSERTs blow max_allowed_packet. A shadow
    import only needs structure, so INSERT/REPLACE/LOCK/transaction control are dropped; DDL stays."""
    dump = """
        CREATE TABLE `users` (`id` int NOT NULL AUTO_INCREMENT, `email` varchar(255), PRIMARY KEY (`id`));
        LOCK TABLES `users` WRITE;
        INSERT INTO `users` (`id`, `email`) VALUES (1, 'a@example.com'), (2, 'b@example.com');
        REPLACE INTO `users` VALUES (3, 'c@example.com');
        UNLOCK TABLES;
        ALTER TABLE `users` ADD UNIQUE KEY `email` (`email`);
    """
    kept = schema_statements(split_sql(dump))
    kinds = [s.split("(")[0].split()[0].upper() for s in kept]
    assert kinds == ["CREATE", "ALTER"]                      # only the two DDL statements survive
    assert not any(s.upper().lstrip().startswith(("INSERT", "REPLACE", "LOCK", "UNLOCK")) for s in kept)


def test_schema_statements_drops_a_huge_insert_but_keeps_the_table():
    """A multi-megabyte INSERT (the literal trigger of 'Lost connection during query') is dropped, so
    the table is still created from the dump and the import can finish."""
    big_values = ", ".join(f"({i}, 'x{i}')" for i in range(50_000))  # a large single INSERT
    dump = (
        "CREATE TABLE `t` (`id` int, `v` varchar(50));\n"
        f"INSERT INTO `t` (`id`, `v`) VALUES {big_values};\n"
    )
    kept = schema_statements(split_sql(dump))
    assert len(kept) == 1 and kept[0].startswith("CREATE TABLE `t`")
