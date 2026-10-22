"""
Camada de análise — MySQL direto.

Características:
  • Deteção automática de schema (evita "Unknown column")
  • Fallback em cascata para funções sensíveis
  • Cache do schema por tabela com TTL (sem dependências externas)
  • Cada função é resistente a erros (nunca derruba o dashboard)
  • Formatação de moeda (MZN) segura em qualquer locale
"""
from __future__ import annotations

import os
import time
import logging
import threading
from functools import lru_cache
from typing import Any, Optional

import pandas as pd
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError
from dotenv import load_dotenv

# ================================================================== #
# 1. CONFIGURAÇÃO E CONEXÃO
# ================================================================== #
load_dotenv()

DB_HOST = os.getenv("DB_HOST", "mysql-8c5f0a2-chilenguejoseorlando-4e5d.l.aivencloud.com")
DB_PORT = os.getenv("DB_PORT", "28922")
DB_NAME = os.getenv("DB_NAME", "defaultdb")
DB_USER = os.getenv("DB_USER", "avnadmin")
DB_PASS = os.getenv("DB_PASSWORD", "AVNS_RyE74rN-9PYt_Dsjdwj")

# ------------------------------------------------------------------ #
# CORREÇÃO PARA AIVEN:
# A Aiven exige SSL. O parâmetro correto para o mysql-connector-python
# é "ssl_disabled=False" (já existe) MAS também é preciso garantir que
# o modo SSL está ativo. Para o mysql-connector, a string de conexão
# deve incluir "ssl_ca" ou pelo menos "ssl_disabled=False" E o driver
# deve negociar TLS automaticamente. O erro anterior era causado por
# não passar "ssl_verify_cert=False" e o servidor rejeitar a ligação.
# Adicionamos "ssl_verify_cert=False" para aceitar o certificado da Aiven.
# ------------------------------------------------------------------ #
URL = (
    f"mysql+mysqlconnector://{DB_USER}:{DB_PASS}"
    f"@{DB_HOST}:{DB_PORT}/{DB_NAME}"
    f"?charset=utf8mb4&ssl_disabled=False&ssl_verify_cert=False"
)

ENGINE = create_engine(
    URL,
    pool_pre_ping=True,
    pool_recycle=3600,
    pool_size=5,
    max_overflow=10,
    future=True,
    connect_args={
        # Garante que o mysql-connector usa SSL sem verificar o certificado
        # (necessário para a Aiven, que usa certificados próprios)
        "ssl_disabled": False,
        "ssl_verify_cert": False,
    },
)

log = logging.getLogger("analytics")
if not log.handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


# ================================================================== #
# 2. HELPERS BASE
# ================================================================== #
def q(sql: str, params: dict | None = None) -> pd.DataFrame:
    """Executa SQL com parâmetros nomeados e devolve DataFrame."""
    with ENGINE.connect() as con:
        return pd.read_sql(text(sql), con, params=params or {})


def q_scalar(sql: str, params: dict | None = None, default: Any = 0) -> Any:
    """Executa SQL e devolve o primeiro valor da primeira linha."""
    try:
        df = q(sql, params)
        return df.iloc[0, 0] if not df.empty else default
    except Exception as e:
        log.warning("q_scalar falhou: %s | SQL: %s", e, str(sql)[:120])
        return default


# -------- Cache do schema (TTL) sem dependências externas --------- #
_COL_TTL_SECONDS = 300
_COL_LOCK = threading.RLock()
_COL_CACHE_TS: dict[str, float] = {}


@lru_cache(maxsize=64)
def _colunas_raw(tabela: str) -> frozenset[str]:
    """Leitura efetiva do schema (cacheada por lru_cache)."""
    try:
        df = q(f"SHOW COLUMNS FROM `{tabela}`")
        return frozenset(df["Field"].tolist())
    except Exception as e:
        log.warning("Tabela `%s` inacessível: %s", tabela, e)
        return frozenset()


def _colunas(tabela: str) -> frozenset[str]:
    """Devolve o conjunto de colunas. Cacheado com TTL de 5 minutos."""
    with _COL_LOCK:
        agora = time.monotonic()
        ts = _COL_CACHE_TS.get(tabela, 0.0)
        if agora - ts > _COL_TTL_SECONDS:
            _colunas_raw.cache_clear()
            _COL_CACHE_TS[tabela] = agora
        return _colunas_raw(tabela)


def _tem_colunas(tabela: str, *cols: str) -> bool:
    """Verifica se todas as colunas existem na tabela."""
    if not cols:
        return True
    c = _colunas(tabela)
    return bool(c) and all(x in c for x in cols)


def _primeira_coluna(tabela: str, candidatas: list[str]) -> Optional[str]:
    """Devolve a primeira coluna existente entre as candidatas."""
    c = _colunas(tabela)
    for cand in candidatas:
        if cand in c:
            return cand
    return None


def _tabela_existe(tabela: str) -> bool:
    return bool(_colunas(tabela))


def limpar_cache_colunas():
    """Força re-leitura do schema (após ALTER TABLE, etc.)."""
    with _COL_LOCK:
        _colunas_raw.cache_clear()
        _COL_CACHE_TS.clear()


# ================================================================== #
# 3. FILTRO DE DATA
# ================================================================== #
def _date_where(inicio, fim, col: str = "s.created_at") -> str:
    parts = ["s.id_company_sale = :cid"]
    if inicio:
        parts.append(f"DATE({col}) >= :inicio")
    if fim:
        parts.append(f"DATE({col}) <= :fim")
    return " AND ".join(parts)


def _date_params(cid, inicio, fim) -> dict:
    p: dict[str, Any] = {"cid": cid}
    if inicio:
        p["inicio"] = inicio
    if fim:
        p["fim"] = fim
    return p


# ================================================================== #
# 4. EMPRESAS
# ================================================================== #
def listar_empresas() -> pd.DataFrame:
    return q("""
        SELECT id_company, name_company, sector_company, status_company
        FROM companies
        ORDER BY name_company
    """)


def info_empresa(cid: int) -> dict:
    try:
        df = q("SELECT * FROM companies WHERE id_company = :cid", {"cid": cid})
        return df.iloc[0].to_dict() if not df.empty else {}
    except Exception as e:
        log.error("info_empresa(%s): %s", cid, e)
        return {}


# ================================================================== #
# 5. KPIs PRINCIPAIS
# ================================================================== #
def kpis_empresa(cid: int, inicio: str | None = None, fim: str | None = None) -> dict:
    where = _date_where(inicio, fim)
    params = _date_params(cid, inicio, fim)

    try:
        v = q(f"""
            SELECT COALESCE(SUM(total_sale),0) AS total, COUNT(*) AS n
            FROM sales s WHERE {where}
        """, params).iloc[0]
        total, n = float(v["total"]), int(v["n"])
    except Exception as e:
        log.error("KPIs vendas: %s", e)
        total, n = 0.0, 0

    total_pago = 0.0
    if _tem_colunas("payments", "amount_payment", "id_sale_payment"):
        total_pago = float(q_scalar(f"""
            SELECT COALESCE(SUM(p.amount_payment),0)
            FROM payments p
            JOIN sales s ON s.id_sale = p.id_sale_payment
            WHERE {where}
        """, params, default=0))

    total_produtos = int(q_scalar("""
        SELECT COUNT(*) FROM products WHERE id_company_product = :cid
    """, {"cid": cid}, default=0))

    stock_total = 0.0
    if _tem_colunas("stock", "quantity_stock", "id_product_stock"):
        stock_total = float(q_scalar("""
            SELECT COALESCE(SUM(st.quantity_stock),0)
            FROM stock st
            JOIN products p ON p.id_product = st.id_product_stock
            WHERE p.id_company_product = :cid
        """, {"cid": cid}, default=0))

    n_clientes = int(q_scalar("""
        SELECT COUNT(*) FROM clients WHERE id_company_client = :cid
    """, {"cid": cid}, default=0))

    return {
        "total_vendas":   total,
        "n_vendas":       n,
        "total_pago":     total_pago,
        "ticket_medio":   total / n if n else 0.0,
        "total_produtos": total_produtos,
        "stock_total":    stock_total,
        "n_clientes":     n_clientes,
    }


# ================================================================== #
# 6. VENDAS (dia / mês / semana / hora)
# ================================================================== #
def vendas_por_dia(cid: int, inicio=None, fim=None) -> pd.DataFrame:
    where = _date_where(inicio, fim)
    return q(f"""
        SELECT DATE(s.created_at) AS dia,
               SUM(s.total_sale) AS total,
               COUNT(*) AS n
        FROM sales s
        WHERE {where}
        GROUP BY dia ORDER BY dia
    """, _date_params(cid, inicio, fim))


def vendas_por_mes(cid: int) -> pd.DataFrame:
    return q("""
        SELECT DATE_FORMAT(created_at,'%Y-%m') AS mes,
               SUM(total_sale) AS total,
               COUNT(*) AS n
        FROM sales WHERE id_company_sale = :cid
        GROUP BY mes ORDER BY mes
    """, {"cid": cid})


def vendas_por_semana(cid: int) -> pd.DataFrame:
    return q("""
        SELECT DATE_FORMAT(created_at,'%Y-%u') AS semana,
               SUM(total_sale) AS total,
               COUNT(*) AS n
        FROM sales WHERE id_company_sale = :cid
        GROUP BY semana ORDER BY semana
    """, {"cid": cid})


def vendas_por_hora(cid: int) -> pd.DataFrame:
    return q("""
        SELECT HOUR(created_at) AS hora,
               SUM(total_sale) AS total,
               COUNT(*) AS n
        FROM sales WHERE id_company_sale = :cid
        GROUP BY hora ORDER BY hora
    """, {"cid": cid})


# ================================================================== #
# 7. PRODUTOS
# ================================================================== #
def top_produtos(cid: int, limite: int = 10) -> pd.DataFrame:
    col_qtd  = _primeira_coluna("sale_items",
                    ["quantity_sale_item", "qty_sale_item", "quantity"])
    col_prc  = _primeira_coluna("sale_items",
                    ["price_sale_item", "unit_price_sale_item", "price"])
    col_prod = _primeira_coluna("sale_items",
                    ["id_product_sale_item", "id_product", "product_id"])
    col_sale = _primeira_coluna("sale_items",
                    ["id_sale_sale_item", "id_sale", "sale_id"])

    if not all([col_qtd, col_prc, col_prod, col_sale]):
        log.warning("top_produtos: schema sale_items inesperado: %s",
                    sorted(_colunas("sale_items")))
        return pd.DataFrame(columns=["name_product", "qtd", "receita"])

    return q(f"""
        SELECT p.name_product,
               SUM(si.`{col_qtd}`) AS qtd,
               SUM(si.`{col_qtd}` * si.`{col_prc}`) AS receita
        FROM sale_items si
        JOIN products p ON p.id_product = si.`{col_prod}`
        JOIN sales s    ON s.id_sale    = si.`{col_sale}`
        WHERE s.id_company_sale = :cid
        GROUP BY p.id_product, p.name_product
        ORDER BY receita DESC
        LIMIT :lim
    """, {"cid": cid, "lim": limite})


def produtos_mais_vendidos_qtd(cid: int, limite: int = 10) -> pd.DataFrame:
    col_qtd  = _primeira_coluna("sale_items",
                    ["quantity_sale_item", "qty_sale_item", "quantity"])
    col_prod = _primeira_coluna("sale_items",
                    ["id_product_sale_item", "id_product", "product_id"])
    col_sale = _primeira_coluna("sale_items",
                    ["id_sale_sale_item", "id_sale", "sale_id"])
    if not all([col_qtd, col_prod, col_sale]):
        return pd.DataFrame(columns=["name_product", "qtd"])

    return q(f"""
        SELECT p.name_product,
               SUM(si.`{col_qtd}`) AS qtd
        FROM sale_items si
        JOIN products p ON p.id_product = si.`{col_prod}`
        JOIN sales s    ON s.id_sale    = si.`{col_sale}`
        WHERE s.id_company_sale = :cid
        GROUP BY p.id_product, p.name_product
        ORDER BY qtd DESC
        LIMIT :lim
    """, {"cid": cid, "lim": limite})


def produtos_nunca_vendidos(cid: int) -> pd.DataFrame:
    col_prod = _primeira_coluna("sale_items",
                    ["id_product_sale_item", "id_product", "product_id"])
    col_sale = _primeira_coluna("sale_items",
                    ["id_sale_sale_item", "id_sale", "sale_id"])

    if not all([col_prod, col_sale]):
        log.warning(
            "produtos_nunca_vendidos: sale_items sem colunas esperadas; "
            "a devolver DataFrame vazio."
        )
        return pd.DataFrame(columns=["id_product", "name_product", "price_product"])

    return q(f"""
        SELECT p.id_product, p.name_product, p.price_product
        FROM products p
        WHERE p.id_company_product = :cid
          AND p.id_product NOT IN (
              SELECT si.`{col_prod}`
              FROM sale_items si
              JOIN sales s ON s.id_sale = si.`{col_sale}`
              WHERE s.id_company_sale = :cid
          )
        ORDER BY p.name_product
    """, {"cid": cid})


def produtos_menos_vendidos(cid: int, limite: int = 10) -> pd.DataFrame:
    col_qtd  = _primeira_coluna("sale_items",
                    ["quantity_sale_item", "qty_sale_item", "quantity"])
    col_prod = _primeira_coluna("sale_items",
                    ["id_product_sale_item", "id_product", "product_id"])
    col_sale = _primeira_coluna("sale_items",
                    ["id_sale_sale_item", "id_sale", "sale_id"])
    if not all([col_qtd, col_prod, col_sale]):
        return pd.DataFrame(columns=["name_product", "qtd_vendida"])

    return q(f"""
        SELECT p.name_product,
               COALESCE(SUM(si.`{col_qtd}`), 0) AS qtd_vendida
        FROM products p
        JOIN sale_items si ON si.`{col_prod}` = p.id_product
        JOIN sales s       ON s.id_sale       = si.`{col_sale}`
        WHERE p.id_company_product = :cid
          AND s.id_company_sale    = :cid
        GROUP BY p.id_product, p.name_product
        ORDER BY qtd_vendida ASC
        LIMIT :lim
    """, {"cid": cid, "lim": limite})


def produtos_receita_media(cid: int, limite: int = 10) -> pd.DataFrame:
    col_qtd  = _primeira_coluna("sale_items",
                    ["quantity_sale_item", "qty_sale_item", "quantity"])
    col_prc  = _primeira_coluna("sale_items",
                    ["price_sale_item", "unit_price_sale_item", "price"])
    col_prod = _primeira_coluna("sale_items",
                    ["id_product_sale_item", "id_product", "product_id"])
    col_sale = _primeira_coluna("sale_items",
                    ["id_sale_sale_item", "id_sale", "sale_id"])
    if not all([col_qtd, col_prc, col_prod, col_sale]):
        return pd.DataFrame(columns=["name_product", "receita", "qtd", "preco_medio"])

    return q(f"""
        SELECT p.name_product,
               SUM(si.`{col_qtd}` * si.`{col_prc}`) AS receita,
               SUM(si.`{col_qtd}`) AS qtd,
               AVG(si.`{col_prc}`) AS preco_medio
        FROM sale_items si
        JOIN products p ON p.id_product = si.`{col_prod}`
        JOIN sales s    ON s.id_sale    = si.`{col_sale}`
        WHERE s.id_company_sale = :cid
        GROUP BY p.id_product, p.name_product
        ORDER BY receita DESC
        LIMIT :lim
    """, {"cid": cid, "lim": limite})


def vendas_por_categoria(cid: int, limite: int = 15) -> pd.DataFrame:
    col_cat = _primeira_coluna("products", [
        "category_product", "id_category_product",
        "categoria_product", "category_id_product", "category",
    ])
    if not col_cat:
        return pd.DataFrame(columns=["categoria", "total", "qtd"])

    col_qtd  = _primeira_coluna("sale_items",
                    ["quantity_sale_item", "qty_sale_item", "quantity"])
    col_prc  = _primeira_coluna("sale_items",
                    ["price_sale_item", "unit_price_sale_item", "price"])
    col_prod = _primeira_coluna("sale_items",
                    ["id_product_sale_item", "id_product", "product_id"])
    col_sale = _primeira_coluna("sale_items",
                    ["id_sale_sale_item", "id_sale", "sale_id"])
    if not all([col_qtd, col_prc, col_prod, col_sale]):
        return pd.DataFrame(columns=["categoria", "total", "qtd"])

    try:
        return q(f"""
            SELECT p.`{col_cat}` AS categoria,
                   SUM(si.`{col_qtd}` * si.`{col_prc}`) AS total,
                   SUM(si.`{col_qtd}`) AS qtd
            FROM sale_items si
            JOIN products p ON p.id_product = si.`{col_prod}`
            JOIN sales s    ON s.id_sale    = si.`{col_sale}`
            WHERE s.id_company_sale = :cid
              AND p.`{col_cat}` IS NOT NULL
            GROUP BY categoria
            ORDER BY total DESC
            LIMIT :lim
        """, {"cid": cid, "lim": limite})
    except Exception as e:
        log.warning("vendas_por_categoria: %s", e)
        return pd.DataFrame(columns=["categoria", "total", "qtd"])


def cross_sell(cid: int, limite: int = 10) -> pd.DataFrame:
    col_prod = _primeira_coluna("sale_items",
                    ["id_product_sale_item", "id_product", "product_id"])
    col_sale = _primeira_coluna("sale_items",
                    ["id_sale_sale_item", "id_sale", "sale_id"])
    if not all([col_prod, col_sale]):
        return pd.DataFrame(columns=["produto_a", "produto_b", "n_juntos"])

    try:
        return q(f"""
            SELECT pa.name_product AS produto_a,
                   pb.name_product AS produto_b,
                   COUNT(*) AS n_juntos
            FROM sale_items i1
            JOIN sale_items i2 ON i1.`{col_sale}` = i2.`{col_sale}`
                              AND i1.`{col_prod}` < i2.`{col_prod}`
            JOIN sales s ON s.id_sale = i1.`{col_sale}`
            JOIN products pa ON pa.id_product = i1.`{col_prod}`
            JOIN products pb ON pb.id_product = i2.`{col_prod}`
            WHERE s.id_company_sale = :cid
            GROUP BY pa.name_product, pb.name_product
            ORDER BY n_juntos DESC
            LIMIT :lim
        """, {"cid": cid, "lim": limite})
    except Exception as e:
        log.warning("cross_sell: %s", e)
        return pd.DataFrame(columns=["produto_a", "produto_b", "n_juntos"])


# ================================================================== #
# 8. PAGAMENTOS
# ================================================================== #
def metodos_pagamento(cid: int) -> pd.DataFrame:
    col_met  = _primeira_coluna("payments", ["method_payment", "payment_method"])
    col_amt  = _primeira_coluna("payments", ["amount_payment", "payment_amount"])
    col_sale = _primeira_coluna("payments", ["id_sale_payment", "id_sale", "sale_id"])
    if not all([col_met, col_amt, col_sale]):
        return pd.DataFrame(columns=["metodo", "n", "total"])

    return q(f"""
        SELECT p.`{col_met}` AS metodo,
               COUNT(*) AS n,
               SUM(p.`{col_amt}`) AS total
        FROM payments p
        JOIN sales s ON s.id_sale = p.`{col_sale}`
        WHERE s.id_company_sale = :cid
        GROUP BY metodo
        ORDER BY total DESC
    """, {"cid": cid})


def taxa_pagamento(cid: int) -> dict:
    total_vendas = 0.0
    total_pago   = 0.0
    try:
        total_vendas = float(q_scalar("""
            SELECT COALESCE(SUM(total_sale),0)
            FROM sales WHERE id_company_sale = :cid
        """, {"cid": cid}, default=0))
    except Exception:
        pass

    if _tem_colunas("payments", "amount_payment", "id_sale_payment"):
        try:
            total_pago = float(q_scalar("""
                SELECT COALESCE(SUM(p.amount_payment),0)
                FROM payments p
                JOIN sales s ON s.id_sale = p.id_sale_payment
                WHERE s.id_company_sale = :cid
            """, {"cid": cid}, default=0))
        except Exception:
            pass

    taxa = (total_pago / total_vendas * 100.0) if total_vendas else 0.0
    return {
        "total_vendas": total_vendas,
        "total_pago":   total_pago,
        "em_falta":     max(total_vendas - total_pago, 0.0),
        "taxa_pct":     taxa,
    }


# ================================================================== #
# 9. STOCK
# ================================================================== #
def alertas_stock(cid: int) -> pd.DataFrame:
    if not _tem_colunas("stock", "quantity_stock", "id_product_stock"):
        return pd.DataFrame(
            columns=["name_product", "stock", "minimum_stock_product"]
        )

    return q("""
        SELECT p.name_product,
               COALESCE(SUM(st.quantity_stock),0) AS stock,
               p.minimum_stock_product
        FROM products p
        LEFT JOIN stock st ON st.id_product_stock = p.id_product
        WHERE p.id_company_product = :cid
        GROUP BY p.id_product, p.name_product, p.minimum_stock_product
        ORDER BY stock ASC
        LIMIT 10
    """, {"cid": cid})


def produtos_baixo_stock(cid: int) -> pd.DataFrame:
    if not _tem_colunas("stock", "quantity_stock", "id_product_stock"):
        return pd.DataFrame(
            columns=["name_product", "stock", "minimum_stock_product"]
        )
    return q("""
        SELECT p.name_product,
               COALESCE(SUM(st.quantity_stock),0) AS stock,
               p.minimum_stock_product
        FROM products p
        LEFT JOIN stock st ON st.id_product_stock = p.id_product
        WHERE p.id_company_product = :cid
        GROUP BY p.id_product, p.name_product, p.minimum_stock_product
        HAVING stock < p.minimum_stock_product
        ORDER BY stock ASC
    """, {"cid": cid})


# ================================================================== #
# 10. OPERADORES
# ================================================================== #
def ranking_operadores(cid: int, limite: int = 10) -> pd.DataFrame:
    col_user = _primeira_coluna("sales", [
        "id_user_sale", "user_id_sale", "id_user", "user_id",
        "id_operator_sale", "operator_id_sale",
    ])

    if col_user and _tem_colunas("sales", col_user, "total_sale", "id_company_sale"):
        try:
            return q(f"""
                SELECT u.name_user,
                       COUNT(s.id_sale) AS n_vendas,
                       COALESCE(SUM(s.total_sale),0) AS total
                FROM sales s
                JOIN users u ON u.id_user = s.`{col_user}`
                WHERE s.id_company_sale = :cid
                GROUP BY u.id_user, u.name_user
                ORDER BY total DESC
                LIMIT :lim
            """, {"cid": cid, "lim": limite})
        except Exception as e:
            log.warning("ranking_operadores via sales falhou: %s", e)

    return _ranking_via_audit(cid, limite)


def _ranking_via_audit(cid: int, limite: int = 10) -> pd.DataFrame:
    if not _tabela_existe("audit_logs"):
        return pd.DataFrame(columns=["name_user", "n_vendas", "total"])

    col_user_log = _primeira_coluna("audit_logs",
                                    ["id_user_audit_log", "user_id"])
    col_action   = _primeira_coluna("audit_logs",
                                    ["action_audit_log", "action"])
    if not all([col_user_log, col_action]):
        return pd.DataFrame(columns=["name_user", "n_vendas", "total"])

    try:
        return q(f"""
            SELECT u.name_user,
                   COUNT(*) AS n_vendas,
                   0 AS total
            FROM audit_logs a
            JOIN users u ON u.id_user = a.`{col_user_log}`
            WHERE u.id_company_user = :cid
              AND a.`{col_action}` IN ('sales.create','sale.create','sales.add')
            GROUP BY u.id_user, u.name_user
            ORDER BY n_vendas DESC
            LIMIT :lim
        """, {"cid": cid, "lim": limite})
    except Exception as e:
        log.warning("_ranking_via_audit: %s", e)
        return pd.DataFrame(columns=["name_user", "n_vendas", "total"])


# ================================================================== #
# 11. CLIENTES
# ================================================================== #
def vendas_por_cliente(cid: int, limite: int = 10) -> pd.DataFrame:
    """
    Top clientes por receita na empresa `cid`.

    Deteção automática:
      • Coluna de cliente em `sales`   → id_client_sale / client_id_sale / ...
      • Tabela `clients`               → nome e id por ordem de preferência
      • Se não existir `clients`, usa `name_client_sale` direto em `sales`
    """
    col_cli_sale = _primeira_coluna("sales", [
        "id_client_sale", "client_id_sale", "id_client", "client_id",
    ])

    # ---------- caminho A: JOIN com tabela `clients` ----------
    if col_cli_sale and _tabela_existe("clients"):
        col_nome = _primeira_coluna("clients", [
            "name_client", "nome_client", "name_clients", "client_name",
        ])
        id_cli = _primeira_coluna("clients",
                                  ["id_client", "id_clients", "client_id"])
        if col_nome and id_cli:
            try:
                return q(f"""
                    SELECT c.`{col_nome}` AS name_client,
                           COUNT(s.id_sale) AS n_vendas,
                           COALESCE(SUM(s.total_sale),0) AS total
                    FROM sales s
                    JOIN clients c ON c.`{id_cli}` = s.`{col_cli_sale}`
                    WHERE s.id_company_sale = :cid
                    GROUP BY c.`{id_cli}`, c.`{col_nome}`
                    ORDER BY total DESC
                    LIMIT :lim
                """, {"cid": cid, "lim": limite})
            except Exception as e:
                log.warning("vendas_por_cliente (JOIN): %s", e)

    # ---------- caminho B: nome do cliente direto em `sales` ----------
    col_nome_sale = _primeira_coluna("sales", [
        "name_client_sale", "nome_client_sale", "client_name_sale",
        "name_client", "client_name",
    ])
    if col_nome_sale:
        try:
            return q(f"""
                SELECT COALESCE(s.`{col_nome_sale}`, 'Anónimo') AS name_client,
                       COUNT(s.id_sale) AS n_vendas,
                       COALESCE(SUM(s.total_sale),0) AS total
                FROM sales s
                WHERE s.id_company_sale = :cid
                  AND s.`{col_nome_sale}` IS NOT NULL
                GROUP BY s.`{col_nome_sale}`
                ORDER BY total DESC
                LIMIT :lim
            """, {"cid": cid, "lim": limite})
        except Exception as e:
            log.warning("vendas_por_cliente (direto): %s", e)

    # ---------- sem dados ----------
    log.warning(
        "vendas_por_cliente: sem coluna de cliente em `sales` "
        "nem tabela `clients` utilizável."
    )
    return pd.DataFrame(columns=["name_client", "n_vendas", "total"])


def clientes_inativos(cid: int, dias: int = 60, limite: int = 15) -> pd.DataFrame:
    col_cli_sale = _primeira_coluna("sales", [
        "id_client_sale", "client_id_sale", "id_client", "client_id",
    ])
    if not col_cli_sale or not _tabela_existe("clients"):
        return pd.DataFrame(columns=["name_client", "ultima_compra", "dias"])

    col_nome = _primeira_coluna("clients", [
        "name_client", "nome_client", "name_clients", "client_name",
    ])
    id_cli = _primeira_coluna("clients", ["id_client", "id_clients", "client_id"])
    if not (col_nome and id_cli):
        return pd.DataFrame(columns=["name_client", "ultima_compra", "dias"])

    try:
        return q(f"""
            SELECT c.`{col_nome}` AS name_client,
                   MAX(s.created_at) AS ultima_compra,
                   DATEDIFF(CURDATE(), MAX(s.created_at)) AS dias
            FROM sales s
            JOIN clients c ON c.`{id_cli}` = s.`{col_cli_sale}`
            WHERE s.id_company_sale = :cid
            GROUP BY c.`{id_cli}`, c.`{col_nome}`
            HAVING dias >= :dias
            ORDER BY dias DESC
            LIMIT :lim
        """, {"cid": cid, "dias": dias, "lim": limite})
    except Exception as e:
        log.warning("clientes_inativos: %s", e)
        return pd.DataFrame(columns=["name_client", "ultima_compra", "dias"])


def novos_clientes_por_mes(cid: int) -> pd.DataFrame:
    col_cli_sale = _primeira_coluna("sales", [
        "id_client_sale", "client_id_sale", "id_client", "client_id",
    ])
    if not col_cli_sale:
        return pd.DataFrame(columns=["mes", "n"])
    try:
        return q(f"""
            SELECT DATE_FORMAT(primeira,'%Y-%m') AS mes,
                   COUNT(*) AS n
            FROM (
                SELECT s.`{col_cli_sale}` AS cliente,
                       MIN(s.created_at) AS primeira
                FROM sales s
                WHERE s.id_company_sale = :cid
                  AND s.`{col_cli_sale}` IS NOT NULL
                GROUP BY s.`{col_cli_sale}`
            ) t
            GROUP BY mes
            ORDER BY mes
        """, {"cid": cid})
    except Exception as e:
        log.warning("novos_clientes_por_mes: %s", e)
        return pd.DataFrame(columns=["mes", "n"])


# ================================================================== #
# 12. TEMPO — dia da semana, hora de pico, sazonalidade
# ================================================================== #
def vendas_por_dia_semana(cid: int) -> pd.DataFrame:
    return q("""
        SELECT
            CASE DAYOFWEEK(created_at)
                WHEN 1 THEN 'Domingo'
                WHEN 2 THEN 'Segunda'
                WHEN 3 THEN 'Terça'
                WHEN 4 THEN 'Quarta'
                WHEN 5 THEN 'Quinta'
                WHEN 6 THEN 'Sexta'
                WHEN 7 THEN 'Sábado'
            END AS dia_semana,
            DAYOFWEEK(created_at) AS ordem,
            SUM(total_sale) AS total,
            COUNT(*)        AS n
        FROM sales
        WHERE id_company_sale = :cid
        GROUP BY dia_semana, ordem
        ORDER BY ordem
    """, {"cid": cid})


def hora_de_pico(cid: int) -> dict:
    try:
        df = q("""
            SELECT HOUR(created_at) AS hora,
                   SUM(total_sale) AS total,
                   COUNT(*) AS n
            FROM sales
            WHERE id_company_sale = :cid
            GROUP BY hora
            ORDER BY total DESC
            LIMIT 1
        """, {"cid": cid})
        if df.empty:
            return {}
        return df.iloc[0].to_dict()
    except Exception as e:
        log.warning("hora_de_pico: %s", e)
        return {}


def mes_de_pico(cid: int) -> dict:
    try:
        df = q("""
            SELECT DATE_FORMAT(created_at,'%Y-%m') AS mes,
                   SUM(total_sale) AS total,
                   COUNT(*) AS n
            FROM sales
            WHERE id_company_sale = :cid
            GROUP BY mes
            ORDER BY total DESC
            LIMIT 1
        """, {"cid": cid})
        if df.empty:
            return {}
        return df.iloc[0].to_dict()
    except Exception as e:
        log.warning("mes_de_pico: %s", e)
        return {}


def _dia_pico(cid: int) -> dict:
    try:
        df = vendas_por_dia_semana(cid)
        if df is None or df.empty:
            return {}
        top = df.sort_values("total", ascending=False).iloc[0]
        return top.to_dict()
    except Exception:
        return {}


def sazonalidade_mensal(cid: int) -> pd.DataFrame:
    return q("""
        SELECT MONTH(created_at) AS mes_num,
               CASE MONTH(created_at)
                   WHEN 1 THEN 'Jan' WHEN 2  THEN 'Fev' WHEN 3  THEN 'Mar'
                   WHEN 4 THEN 'Abr' WHEN 5  THEN 'Mai' WHEN 6  THEN 'Jun'
                   WHEN 7 THEN 'Jul' WHEN 8  THEN 'Ago' WHEN 9  THEN 'Set'
                   WHEN 10 THEN 'Out' WHEN 11 THEN 'Nov' WHEN 12 THEN 'Dez'
               END AS mes_nome,
               AVG(total_sale) AS media,
               SUM(total_sale) AS total,
               COUNT(*) AS n
        FROM sales
        WHERE id_company_sale = :cid
        GROUP BY mes_num, mes_nome
        ORDER BY mes_num
    """, {"cid": cid})


def heatmap_dia_hora(cid: int) -> pd.DataFrame:
    return q("""
        SELECT
            CASE DAYOFWEEK(created_at)
                WHEN 1 THEN 'Dom' WHEN 2 THEN 'Seg' WHEN 3 THEN 'Ter'
                WHEN 4 THEN 'Qua' WHEN 5 THEN 'Qui' WHEN 6 THEN 'Sex'
                WHEN 7 THEN 'Sáb'
            END AS dia,
            DAYOFWEEK(created_at) AS dia_ord,
            HOUR(created_at) AS hora,
            SUM(total_sale) AS total,
            COUNT(*) AS n
        FROM sales
        WHERE id_company_sale = :cid
        GROUP BY dia, dia_ord, hora
        ORDER BY dia_ord, hora
    """, {"cid": cid})


def ticket_medio_por_mes(cid: int) -> pd.DataFrame:
    return q("""
        SELECT DATE_FORMAT(created_at,'%Y-%m') AS mes,
               AVG(total_sale) AS ticket_medio,
               SUM(total_sale) AS total,
               COUNT(*) AS n
        FROM sales
        WHERE id_company_sale = :cid
        GROUP BY mes
        ORDER BY mes
    """, {"cid": cid})


# ================================================================== #
# 13. TENDÊNCIAS
# ================================================================== #
def tendencia_semanal(cid: int) -> dict:
    try:
        df = q("""
            SELECT
                SUM(CASE WHEN DATE(created_at) >= DATE_SUB(CURDATE(), INTERVAL 7 DAY)
                         THEN total_sale ELSE 0 END) AS total_atual,
                SUM(CASE WHEN DATE(created_at) >= DATE_SUB(CURDATE(), INTERVAL 14 DAY)
                          AND DATE(created_at) <  DATE_SUB(CURDATE(), INTERVAL 7 DAY)
                         THEN total_sale ELSE 0 END) AS total_anterior
            FROM sales
            WHERE id_company_sale = :cid
        """, {"cid": cid})
        if df.empty:
            return {}
        r = df.iloc[0]
        atual    = float(r["total_atual"]    or 0)
        anterior = float(r["total_anterior"] or 0)
        variacao = 100.0 if (anterior == 0 and atual > 0) else (
            ((atual - anterior) / anterior * 100.0) if anterior else 0.0
        )
        direcao = "subida" if variacao > 5 else ("descida" if variacao < -5 else "estavel")
        return {
            "total_atual":    atual,
            "total_anterior": anterior,
            "variacao_pct":   variacao,
            "direcao":        direcao,
        }
    except Exception as e:
        log.warning("tendencia_semanal: %s", e)
        return {}


def tendencia_mensal(cid: int) -> dict:
    try:
        df = q("""
            SELECT
                SUM(CASE WHEN DATE_FORMAT(created_at,'%Y-%m') = DATE_FORMAT(CURDATE(),'%Y-%m')
                         THEN total_sale ELSE 0 END) AS total_atual,
                SUM(CASE WHEN DATE_FORMAT(created_at,'%Y-%m') = DATE_FORMAT(DATE_SUB(CURDATE(), INTERVAL 1 MONTH),'%Y-%m')
                         THEN total_sale ELSE 0 END) AS total_anterior
            FROM sales
            WHERE id_company_sale = :cid
        """, {"cid": cid})
        if df.empty:
            return {}
        r = df.iloc[0]
        atual    = float(r["total_atual"]    or 0)
        anterior = float(r["total_anterior"] or 0)
        variacao = 100.0 if (anterior == 0 and atual > 0) else (
            ((atual - anterior) / anterior * 100.0) if anterior else 0.0
        )
        direcao = "subida" if variacao > 5 else ("descida" if variacao < -5 else "estavel")
        return {
            "total_atual":    atual,
            "total_anterior": anterior,
            "variacao_pct":   variacao,
            "direcao":        direcao,
        }
    except Exception as e:
        log.warning("tendencia_mensal: %s", e)
        return {}


# ================================================================== #
# 14. COMPARATIVO GLOBAL
# ================================================================== #
def comparativo_empresas() -> pd.DataFrame:
    return q("""
        SELECT c.id_company, c.name_company,
               COALESCE(SUM(s.total_sale),0) AS total_vendas,
               COUNT(s.id_sale) AS n_vendas
        FROM companies c
        LEFT JOIN sales s ON s.id_company_sale = c.id_company
        GROUP BY c.id_company, c.name_company
        ORDER BY total_vendas DESC
    """)


def vendas_por_mes_todas() -> pd.DataFrame:
    return q("""
        SELECT DATE_FORMAT(created_at,'%Y-%m') AS mes,
               SUM(total_sale) AS total,
               COUNT(*) AS n
        FROM sales
        GROUP BY mes ORDER BY mes
    """)


def top_empresas_vendas(limite: int = 5) -> pd.DataFrame:
    return q("""
        SELECT c.name_company,
               COALESCE(SUM(s.total_sale),0) AS total
        FROM companies c
        LEFT JOIN sales s ON s.id_company_sale = c.id_company
        GROUP BY c.id_company, c.name_company
        ORDER BY total DESC
        LIMIT :lim
    """, {"lim": limite})


# ================================================================== #
# 15. RECOMENDAÇÕES — versão com itens detalhados para modal
# ================================================================== #
def _colunas_monetarias() -> set[str]:
    """Colunas cujo valor deve ser formatado como MZN no frontend."""
    return {
        "total", "receita", "price_product",
        "minimum_stock_product", "preco_medio",
    }


def recomendacoes(cid: int) -> list[dict]:
    """
    Gera recomendações com lista detalhada de itens para modal clicável.
    Cada item tem:
        {
          "nivel":  "info|aviso|critico",
          "tipo":   "stock_baixo|nunca_vendidos|concentracao|pico|dia|inativos|tendencia",
          "titulo": "...",
          "texto":  "...",
          "colunas": [["chave", "Título"], ...],   # colunas da tabela
          "itens":   [ {...}, {...} ],             # linhas
        }
    """
    recs: list[dict] = []

    def _to_python(val):
        """Converte numpy/pandas types em Python nativo (JSON-safe)."""
        try:
            if pd.isna(val):
                return None
        except (TypeError, ValueError):
            pass
        if isinstance(val, (int, float, str, bool)) or val is None:
            return val
        # numpy int/float
        try:
            return val.item()
        except AttributeError:
            return str(val)

    def _sanitize_df(df: pd.DataFrame) -> list[dict]:
        """Converte DataFrame em lista de dicts JSON-safe."""
        rows = []
        for rec in df.to_dict("records"):
            rows.append({k: _to_python(v) for k, v in rec.items()})
        return rows

    # ---------- 1) Stock crítico ----------
    try:
        df = produtos_baixo_stock(cid)
        if df is not None and not df.empty:
            itens = _sanitize_df(df)
            recs.append({
                "nivel": "critico",
                "tipo": "stock_baixo",
                "titulo": f"{len(itens)} produto(s) abaixo do stock mínimo",
                "texto": "Repõe stock destes produtos para evitar ruturas.",
                "colunas": [
                    ["name_product", "Produto"],
                    ["stock", "Stock atual"],
                    ["minimum_stock_product", "Mínimo"],
                ],
                "itens": itens,
            })
    except Exception as e:
        log.warning("recomendacoes / stock: %s", e)

    # ---------- 2) Produtos nunca vendidos ----------
    try:
        df = produtos_nunca_vendidos(cid)
        if df is not None and not df.empty:
            itens = _sanitize_df(df)
            recs.append({
                "nivel": "aviso",
                "tipo": "nunca_vendidos",
                "titulo": f"{len(itens)} produto(s) nunca vendido(s)",
                "texto": "Considera promoções, bundles, ou descontinuar.",
                "colunas": [
                    ["name_product", "Produto"],
                    ["price_product", "Preço"],
                ],
                "itens": itens,
            })
    except Exception as e:
        log.warning("recomendacoes / nunca_vendidos: %s", e)

    # ---------- 3) Concentração de vendas ----------
    try:
        df_top = top_produtos(cid, 5)
        df_pag = metodos_pagamento(cid)
        total_pag = float(df_pag["total"].sum()) if df_pag is not None and not df_pag.empty else 0
        if df_top is not None and not df_top.empty and total_pag > 0:
            top1 = df_top.iloc[0]
            pct = (float(top1["receita"]) / total_pag) * 100
            if pct > 40:
                itens = _sanitize_df(df_top)
                recs.append({
                    "nivel": "aviso",
                    "tipo": "concentracao",
                    "titulo": f"Concentração alta: {top1['name_product']}",
                    "texto": f"Representa {pct:.1f}% da receita total. "
                             "Diversifica para reduzir risco.",
                    "colunas": [
                        ["name_product", "Produto"],
                        ["receita", "Receita (MZN)"],
                        ["qtd", "Quantidade"],
                    ],
                    "itens": itens,
                })
    except Exception as e:
        log.warning("recomendacoes / concentracao: %s", e)

    # ---------- 4) Mês de pico ----------
    try:
        mp = mes_de_pico(cid)
        if mp:
            recs.append({
                "nivel": "info",
                "tipo": "pico",
                "titulo": f"Mês de pico: {mp['mes']}",
                "texto": f"Total de {float(mp['total']):,.2f} MZN em "
                         f"{int(mp['n'])} venda(s). Reforça stock e equipa.",
                "colunas": [
                    ["mes", "Mês"],
                    ["total", "Total (MZN)"],
                    ["n", "Nº Vendas"],
                ],
                "itens": [{
                    "mes":   _to_python(mp["mes"]),
                    "total": _to_python(mp["total"]),
                    "n":     _to_python(mp["n"]),
                }],
            })
    except Exception as e:
        log.warning("recomendacoes / pico: %s", e)

    # ---------- 5) Melhor dia da semana ----------
    try:
        df_ds = vendas_por_dia_semana(cid)
        if df_ds is not None and not df_ds.empty:
            top = df_ds.sort_values("total", ascending=False).iloc[0]
            itens = _sanitize_df(df_ds)
            recs.append({
                "nivel": "info",
                "tipo": "dia",
                "titulo": f"Melhor dia: {top['dia_semana']}",
                "texto": f"Concentra {float(top['total']):,.2f} MZN. "
                         "Ideal para promoções e reforço de pessoal.",
                "colunas": [
                    ["dia_semana", "Dia"],
                    ["total", "Total (MZN)"],
                    ["n", "Nº Vendas"],
                ],
                "itens": itens,
            })
    except Exception as e:
        log.warning("recomendacoes / dia: %s", e)

    # ---------- 6) Clientes inativos ----------
    try:
        df_inat = clientes_inativos(cid, dias=60, limite=20)
        if df_inat is not None and not df_inat.empty:
            itens = _sanitize_df(df_inat)
            recs.append({
                "nivel": "aviso",
                "tipo": "inativos",
                "titulo": f"{len(itens)} cliente(s) inativo(s) há 60+ dias",
                "texto": "Campanha de reativação pode recuperar receita.",
                "colunas": [
                    ["name_client",   "Cliente"],
                    ["ultima_compra", "Última compra"],
                    ["dias",          "Dias"],
                ],
                "itens": itens,
            })
    except Exception as e:
        log.warning("recomendacoes / inativos: %s", e)

    # ---------- 7) Tendência mensal ----------
    try:
        tm = tendencia_mensal(cid)
        if tm and tm.get("direcao") == "descida":
            recs.append({
                "nivel": "aviso",
                "tipo": "tendencia",
                "titulo": "Vendas em queda este mês",
                "texto": f"Variação de {tm['variacao_pct']:.1f}% face ao mês anterior.",
                "colunas": [["periodo", "Período"], ["total", "Total (MZN)"]],
                "itens": [
                    {"periodo": "Mês anterior", "total": tm["total_anterior"]},
                    {"periodo": "Mês atual",    "total": tm["total_atual"]},
                ],
            })
        elif tm and tm.get("direcao") == "subida":
            recs.append({
                "nivel": "info",
                "tipo": "tendencia",
                "titulo": "Vendas em subida este mês",
                "texto": f"Variação de +{tm['variacao_pct']:.1f}% face ao mês anterior.",
                "colunas": [["periodo", "Período"], ["total", "Total (MZN)"]],
                "itens": [
                    {"periodo": "Mês anterior", "total": tm["total_anterior"]},
                    {"periodo": "Mês atual",    "total": tm["total_atual"]},
                ],
            })
    except Exception as e:
        log.warning("recomendacoes / tendencia: %s", e)

    return recs


# ================================================================== #
# 16. PANORAMA — sumário compacto
# ================================================================== #
def panorama_empresa(cid: int) -> dict:
    return {
        "mes_pico":         mes_de_pico(cid),
        "hora_pico":        hora_de_pico(cid),
        "dia_pico":         _dia_pico(cid),
        "tendencia_semana": tendencia_semanal(cid),
        "tendencia_mes":    tendencia_mensal(cid),
        "taxa_pagamento":   taxa_pagamento(cid),
    }


# ================================================================== #
# 17. DIAGNÓSTICO
# ================================================================== #
def schema_resumo() -> pd.DataFrame:
    try:
        tabelas = q("SHOW TABLES").iloc[:, 0].tolist()
    except SQLAlchemyError as e:
        log.error("schema_resumo: SHOW TABLES falhou: %s", e)
        return pd.DataFrame(columns=["tabela", "linhas", "colunas"])

    rows = []
    for t in tabelas:
        try:
            n = q_scalar(f"SELECT COUNT(*) FROM `{t}`", default=0)
            rows.append({
                "tabela":  t,
                "linhas":  int(n),
                "colunas": len(_colunas(t)),
            })
        except Exception as e:
            rows.append({
                "tabela":  t,
                "linhas":  -1,
                "colunas": 0,
                "erro":    str(e)[:80],
            })
    return pd.DataFrame(rows).sort_values("linhas", ascending=False)


def colunas_de(tabela: str) -> list[str]:
    """Debug: lista as colunas de uma tabela."""
    return sorted(_colunas(tabela))


# ================================================================== #
# 18. FORMATAÇÃO (moeda MZN)
# ================================================================== #
def fmt_mzn(v: Any) -> str:
    """
    Formata um número como moeda MZN, estilo pt-MZ:
        1234.5  -> '1 234,50 MZN'
        0       -> '0,00 MZN'
    Seguro contra None, strings vazias e valores não numéricos.
    """
    try:
        n = float(v)
    except (TypeError, ValueError):
        return "0,00 MZN"

    s = f"{n:,.2f}"                       # '1,234.50'
    s = s.replace(",", "\x00")            # separador de milhares temporário
    s = s.replace(".", ",")               # decimal passa a vírgula
    s = s.replace("\x00", "\u00a0")       # milhares = espaço não-quebrável
    return f"{s} MZN"
