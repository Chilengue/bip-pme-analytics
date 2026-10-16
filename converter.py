"""
Converte dump MySQL (bip-pme.sql) para SQLite de forma robusta.
Uso: python converter.py bip-pme.sql
"""
import re
import sys
import os
import sqlite3

SRC = sys.argv[1] if len(sys.argv) > 1 else "bip-pme.sql"
DST = "bip-pme.db"


def parse_dump(text: str):
    """Extrai blocos CREATE TABLE e INSERT INTO."""
    # Limpa comentários -- e /* */
    text = re.sub(r"--[^\n]*", "", text)
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)

    # Extrai CREATE TABLE `nome` ( ... ) ENGINE=...
    tables = []
    for m in re.finditer(
        r"CREATE TABLE\s+`([^`]+)`\s*\((.*?)\)\s*ENGINE",
        text, flags=re.S | re.I,
    ):
        tables.append((m.group(1), m.group(2)))

    # Extrai todos os INSERT INTO `nome` (...) VALUES (...);
    inserts = []
    for m in re.finditer(
        r"INSERT INTO\s+`([^`]+)`\s*\(([^)]+)\)\s*VALUES\s*(.*?);\s*(?=INSERT|$)",
        text, flags=re.S | re.I,
    ):
        nome = m.group(1)
        cols = m.group(2)
        vals_bloco = m.group(3)
        inserts.append((nome, cols, vals_bloco))

    return tables, inserts


def convert_column_def(col: str) -> str:
    """Converte uma definição de coluna MySQL para SQLite."""
    col = col.strip()
    if not col:
        return ""

    # Remove backticks
    col = col.replace("`", "")

    # MySQL tipo → SQLite
    col = re.sub(r"\bBIGINT\b", "INTEGER", col, flags=re.I)
    col = re.sub(r"\bTINYINT\(\d+\)", "INTEGER", col, flags=re.I)
    col = re.sub(r"\bINT\(\d+\)", "INTEGER", col, flags=re.I)
    col = re.sub(r"\bINT\b", "INTEGER", col, flags=re.I)
    col = re.sub(r"\bENUM\([^)]*\)", "TEXT", col, flags=re.I)
    col = re.sub(r"\bDATETIME\b", "TEXT", col, flags=re.I)
    col = re.sub(r"\bDATE\b", "TEXT", col, flags=re.I)
    col = re.sub(r"\bDECIMAL\(\d+,\d+\)", "REAL", col, flags=re.I)
    col = re.sub(r"\bDOUBLE\b", "REAL", col, flags=re.I)
    col = re.sub(r"\bLONGTEXT\b", "TEXT", col, flags=re.I)
    col = re.sub(r"\bMEDIUMTEXT\b", "TEXT", col, flags=re.I)
    col = re.sub(r"\bVARCHAR\(\d+\)", "TEXT", col, flags=re.I)
    col = re.sub(r"\bCHAR\(\d+\)", "TEXT", col, flags=re.I)
    col = re.sub(r"\bJSON\b", "TEXT", col, flags=re.I)
    col = re.sub(r"\bBLOB\b", "BLOB", col, flags=re.I)

    # Remove partes MySQL não suportadas
    col = re.sub(r"CHARACTER SET \w+", "", col, flags=re.I)
    col = re.sub(r"COLLATE \w+", "", col, flags=re.I)
    col = re.sub(r"AUTO_INCREMENT", "", col, flags=re.I)
    col = re.sub(r"UNSIGNED", "", col, flags=re.I)
    col = re.sub(r"DEFAULT\s+NULL", "", col, flags=re.I)
    col = re.sub(r"DEFAULT\s+'[^']*'", "", col, flags=re.I)
    col = re.sub(r"DEFAULT\s+\d+\.?\d*", "", col, flags=re.I)
    col = re.sub(r"ON UPDATE CURRENT_TIMESTAMP", "", col, flags=re.I)
    col = re.sub(r"\bNULL\b", "", col, flags=re.I)

    # Normaliza espaços
    col = re.sub(r"\s+", " ", col).strip()
    return col


def build_sqlite_create(nome: str, body: str) -> str:
    """Reconstrói CREATE TABLE para SQLite."""
    linhas = []
    depth = 0
    buf = ""
    for ch in body:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            linhas.append(buf.strip())
            buf = ""
        else:
            buf += ch
    if buf.strip():
        linhas.append(buf.strip())

    cols_sql = []
    pk_cols = []  # para PRIMARY KEY composta

    for linha in linhas:
        if not linha:
            continue

        # Ignora chaves/índices
        if re.match(r"^\s*PRIMARY KEY", linha, re.I):
            continue
        if re.match(r"^\s*KEY\b", linha, re.I):
            continue
        if re.match(r"^\s*UNIQUE KEY\b", linha, re.I):
            continue
        if re.match(r"^\s*CONSTRAINT\b", linha, re.I):
            continue
        if linha.startswith("`") and '"' not in linha and "'" not in linha:
            # linha de coluna
            col_name_m = re.match(r"`([^`]+)`", linha)
            if not col_name_m:
                continue
            col_name = col_name_m.group(1)
            definicao = convert_column_def(linha)
            # Detecta se é a chave primária
            if re.search(r"PRIMARY KEY", linha, re.I):
                definicao = re.sub(r"PRIMARY KEY", "PRIMARY KEY AUTOINCREMENT", definicao, flags=re.I)
                # Evita duplicar AUTOINCREMENT
                definicao = definicao.replace("AUTOINCREMENT AUTOINCREMENT", "AUTOINCREMENT")
                # Remove AUTOINCREMENT se não for INTEGER
                if "INTEGER" not in definicao.upper():
                    definicao = definicao.replace("AUTOINCREMENT", "")
            cols_sql.append(definicao)

    if not cols_sql:
        return ""

    return f"CREATE TABLE IF NOT EXISTS `{nome}` (\n  " + ",\n  ".join(cols_sql) + "\n);"


def clean_values(bloco: str) -> str:
    """Limpa bloco de VALUES para SQLite."""
    # MySQL escapa strings com \' → SQLite usa ''
    bloco = bloco.replace("\\'", "''")
    # Remove espaço após vírgulas entre tuplas fica ok
    return bloco.strip()


def main():
    if os.path.exists(DST):
        os.remove(DST)

    print(f"→ Lendo {SRC}...")
    with open(SRC, "r", encoding="utf-8") as f:
        raw = f.read()

    tables, inserts = parse_dump(raw)
    print(f"→ {len(tables)} tabelas, {len(inserts)} blocos INSERT detectados")

    con = sqlite3.connect(DST)
    con.execute("PRAGMA foreign_keys = OFF")
    ok, fail = 0, 0

    for nome, body in tables:
        ddl = build_sqlite_create(nome, body)
        if not ddl:
            print(f"  ⚠ DDL vazio: {nome}")
            fail += 1
            continue
        try:
            con.execute(ddl)
            ok += 1
        except sqlite3.Error as e:
            print(f"  ✗ CREATE {nome}: {e}")
            fail += 1

    for nome, cols, vals in inserts:
        stmt = f"INSERT OR IGNORE INTO `{nome}` ({cols}) VALUES {clean_values(vals)}"
        try:
            con.executescript(stmt)
        except sqlite3.Error as e:
            print(f"  ✗ INSERT {nome}: {str(e)[:120]}")
            fail += 1

    con.commit()

    print(f"\n✔ {ok} tabelas criadas · {fail} falhas")
    # Lista tabelas finais
    cur = con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    print("\nTabelas no banco:")
    for (n,) in cur:
        cnt = con.execute(f"SELECT COUNT(*) FROM `{n}`").fetchone()[0]
        print(f"  · {n}: {cnt} linhas")
    con.close()


if __name__ == "__main__":
    main()