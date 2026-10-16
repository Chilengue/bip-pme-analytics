"""
BIP-PME Analytics Dashboard — versão multi-tenant com autenticação.
Execução:  python app.py  →  http://localhost:5000
Requisitos: flask, pandas, mysql-connector-python, plotly,
            python-dotenv, SQLAlchemy, werkzeug

------------------------------------------------------------------
ÂMBITO DE VISÃO
---------------
  • admin / super_admin  → TODAS as empresas, todas as telas
  • manager              → TODAS as empresas, todas as telas
  • bi_analyst           → TODAS as empresas, só leitura
  • operator             → SÓ a sua empresa
  • external             → SÓ a sua empresa (relatórios)
------------------------------------------------------------------
"""
from __future__ import annotations

import io
import os
import logging
from datetime import datetime, timedelta

from flask import (
    Flask, render_template, request, jsonify,
    Response, abort, session, redirect, url_for,
)
import pandas as pd
import plotly.express as px
import plotly.io as pio

import analytics as A
import auth as Auth


# ================================================================== #
# 00. CONFIGURAÇÃO E REGISTO DE BLUEPRINT
# ================================================================== #
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("bip-pme")

app = Flask(__name__)

app.secret_key = os.getenv("SECRET_KEY", "dev-troca-isto-em-producao")
app.permanent_session_lifetime = timedelta(hours=12)

app.config["JSON_AS_ASCII"] = False
try:
    app.json.ensure_ascii = False
except AttributeError:
    pass

app.register_blueprint(Auth.auth_bp)
app.jinja_env.filters["mzn"] = A.fmt_mzn


# ================================================================== #
# 01. CONTEXTO GLOBAL
# ================================================================== #
@app.context_processor
def _inject_globals():
    def static_v(rel_path: str) -> int:
        try:
            return int(os.path.getmtime(os.path.join(app.static_folder, rel_path)))
        except OSError:
            return 1

    return {
        "static_v": static_v,
        "current_user": {
            "id":        session.get("user_id"),
            "name":      session.get("name"),
            "email":     session.get("email"),
            "role":      session.get("role"),
            "is_super":  session.get("is_super", False),
            "is_global": session.get("is_global", False),
        },
        "current_company": {
            "id":   session.get("company_id"),
            "name": session.get("company_name"),
        },
    }


# ================================================================== #
# 02. CABEÇALHOS ANTI-CACHE
# ================================================================== #
@app.after_request
def _sem_cache_em_debug(resp):
    """Em modo debug, evita cache de JS/CSS durante o desenvolvimento."""
    if app.debug and resp.mimetype in ("text/javascript", "text/css"):
        resp.headers["Cache-Control"] = "no-store, must-revalidate"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
    return resp


@app.after_request
def _no_cache_autenticado(resp):
    """Impede cache de páginas HTML para utilizadores autenticados."""
    if "user_id" in session and resp.mimetype == "text/html":
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
    return resp


# ================================================================== #
# 03. HELPERS DE LAYOUT PLOTLY
# ================================================================== #
def fig_json(fig) -> str:
    return pio.to_json(fig, validate=True, pretty=False)


PALETA = [
    "#6366f1", "#22d3ee", "#f59e0b", "#10b981",
    "#ec4899", "#8b5cf6", "#f43f5e", "#14b8a6",
]

BASE_LAYOUT: dict = dict(
    template="plotly_dark",
    margin=dict(l=60, r=40, t=60, b=60),
    title="",
    title_font=dict(size=16, color="#e2e8f0",
                    family="Inter, system-ui, sans-serif"),
    font=dict(family="Inter, system-ui, sans-serif",
              size=12, color="#cbd5e1"),
    colorway=PALETA,
    paper_bgcolor="rgba(0,0,0,0)",
    plot_bgcolor="rgba(0,0,0,0)",
    hoverlabel=dict(
        bgcolor="#0f172a", bordercolor="#334155",
        font=dict(color="#e2e8f0", size=12,
                  family="Inter, system-ui, sans-serif"),
    ),
    legend=dict(
        bgcolor="rgba(30,41,59,0.85)", bordercolor="#334155",
        borderwidth=1, font=dict(color="#e2e8f0", size=11),
        orientation="h", yanchor="bottom", y=1.02,
        xanchor="right", x=1,
    ),
    xaxis=dict(
        gridcolor="rgba(148,163,184,0.15)",
        linecolor="rgba(148,163,184,0.30)",
        zerolinecolor="rgba(148,163,184,0.20)",
        tickfont=dict(color="#94a3b8", size=11),
        title_font=dict(color="#cbd5e1", size=12),
        automargin=True,
    ),
    yaxis=dict(
        gridcolor="rgba(148,163,184,0.15)",
        linecolor="rgba(148,163,184,0.30)",
        zerolinecolor="rgba(148,163,184,0.20)",
        tickfont=dict(color="#94a3b8", size=11),
        title_font=dict(color="#cbd5e1", size=12),
        automargin=True,
    ),
)


def base_layout_com(**overrides) -> dict:
    """Devolve BASE_LAYOUT com overrides (merge profundo para chaves
    que normalmente precisam: legend, margin, xaxis, yaxis, title)."""
    layout = {**BASE_LAYOUT}
    for k, v in overrides.items():
        if k in ("legend", "margin", "xaxis", "yaxis", "title") and isinstance(v, dict):
            layout[k] = {**(layout.get(k) or {}), **v}
        else:
            layout[k] = v
    return layout


KPIS_DEFAULT: dict = {
    "total_vendas": 0.0, "n_vendas": 0, "total_pago": 0.0,
    "ticket_medio": 0.0, "total_produtos": 0, "stock_total": 0.0,
    "n_clientes": 0,
}


# ================================================================== #
# 04. HELPERS GENÉRICOS
# ================================================================== #
def _safe(fn, default=None):
    try:
        return fn()
    except Exception as e:
        log.exception("Erro em %s: %s", getattr(fn, "__name__", fn), e)
        return default


def _parse_data(s: str | None):
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        log.warning("Data inválida ignorada: %r", s)
        return None


def _periodo_args():
    """Interpreta ?preset / ?inicio / ?fim e devolve
    (inicio_iso, fim_iso, label)."""
    inicio = _parse_data(request.args.get("inicio"))
    fim    = _parse_data(request.args.get("fim"))
    preset = request.args.get("preset")
    hoje   = datetime.now().date()

    if preset == "hoje":
        inicio = fim = hoje
    elif preset == "semana":
        inicio, fim = hoje - timedelta(days=7), hoje
    elif preset == "mes":
        inicio, fim = hoje.replace(day=1), hoje
    elif preset == "ano":
        inicio, fim = hoje.replace(month=1, day=1), hoje

    inicio_s = inicio.isoformat() if inicio else None
    fim_s    = fim.isoformat() if fim else None

    if inicio_s and fim_s:
        label = f"{inicio_s} → {fim_s}"
    elif inicio_s:
        label = f"desde {inicio_s}"
    elif fim_s:
        label = f"até {fim_s}"
    else:
        label = ""

    return inicio_s, fim_s, label


def _regista_grafico(graficos, nome, df, builder):
    """Regista um gráfico em `graficos`, com log detalhado."""
    if df is None:
        log.warning("Gráfico '%s': DataFrame é None — ignorado.", nome)
        return
    if df.empty:
        log.warning("Gráfico '%s': DataFrame vazio — ignorado.", nome)
        return
    try:
        graficos[nome] = fig_json(builder(df))
        log.info("Gráfico '%s': OK (%d linhas).", nome, len(df))
    except Exception as e:
        log.exception("Gráfico '%s': falhou: %s", nome, e)


# ================================================================== #
# 05. ROTA INDEX — PAINEL GLOBAL
# ================================================================== #
@app.route("/")
@Auth.login_required
def index():
    """
    Painel global.

      • admin / manager / bi_analyst → comparativo de TODAS as empresas
      • operator / external          → redireciona para a sua empresa
    """
    empresas = Auth.visible_companies()
    global_view = Auth.is_global_viewer()

    inicio, fim, _ = _periodo_args()

    # ---------- Visão local: redireciona para a empresa ----------
    if not global_view and session.get("company_id"):
        return redirect(url_for("empresa", cid=session["company_id"]))

    # ---------- Visão global ----------
    graficos: dict[str, str] = {}
    kpis_global = {
        "total_vendas": 0.0,
        "n_vendas": 0,
        "n_empresas": len(empresas),
        "n_produtos": 0,
        "n_clientes": 0,
    }

    df_comp = _safe(A.comparativo_empresas)
    if df_comp is not None and not df_comp.empty:
        kpis_global["total_vendas"] = float(df_comp["total_vendas"].sum())
        kpis_global["n_vendas"]     = int(df_comp["n_vendas"].sum())

        # ---------- Comparativo (barras por empresa) ----------
        _regista_grafico(
            graficos, "comparativo", df_comp,
            lambda df: px.bar(
                df, x="name_company", y="total_vendas", text="total_vendas",
                title="Vendas por empresa",
                labels={"name_company": "", "total_vendas": "Total (MZN)"},
            ).update_traces(
                marker=dict(
                    color=df["total_vendas"],
                    colorscale=[[0, "#312e81"], [0.5, "#6366f1"], [1, "#22d3ee"]],
                    line=dict(width=0),
                ),
                texttemplate="%{text:,.0f}",
                textposition="outside",
                textfont=dict(color="#cbd5e1", size=11),
                hovertemplate="<b>%{x}</b><br>Total: %{y:,.2f} MZN<extra></extra>",
            ).update_layout(**base_layout_com(showlegend=False)),
        )

        # ---------- Distribuição (pie por empresa) ----------
        df_dist = df_comp[df_comp["total_vendas"] > 0].copy()
        _regista_grafico(
            graficos, "distribuicao", df_dist,
            lambda df: px.pie(
                df, names="name_company", values="total_vendas",
                title="Distribuição de vendas", hole=0.55,
            ).update_traces(
                textposition="inside", textinfo="percent",
                textfont=dict(color="#ffffff", size=11),
                marker=dict(line=dict(color="#0f172a", width=2), colors=PALETA),
                hovertemplate=("<b>%{label}</b><br>Total: %{value:,.2f} MZN"
                               "<br>%{percent}<extra></extra>"),
            ).update_layout(**base_layout_com(
                legend=dict(orientation="v", yanchor="middle", y=0.5,
                            xanchor="left", x=1.02,
                            bgcolor="rgba(0,0,0,0)",
                            font=dict(color="#cbd5e1", size=11)),
                margin=dict(l=20, r=180, t=60, b=40),
            )),
        )

    return render_template(
        "dashboard.html",
        empresas=empresas,
        empresa=None,
        kpis=kpis_global,
        graficos=graficos,
        periodo_label="Todo o histórico",
        inicio=inicio or "",
        fim=fim or "",
    )


# ================================================================== #
# 06. ROTA EMPRESA — DASHBOARD DETALHADO
# ================================================================== #
@app.route("/empresa/<int:cid>")
@Auth.login_required
def empresa(cid):
    Auth.ensure_company_access(cid)          # valida global vs local
    inicio, fim, label = _periodo_args()

    empresas = Auth.visible_companies()
    info = _safe(lambda: A.info_empresa(cid), default={}) or {}
    if not info:
        abort(404, description=f"Empresa {cid} não encontrada")

    kpis = _safe(
        lambda: A.kpis_empresa(cid, inicio, fim),
        default=dict(KPIS_DEFAULT),
    ) or dict(KPIS_DEFAULT)

    pico          = _safe(lambda: A.mes_de_pico(cid), default={}) or {}
    recomendacoes = _safe(lambda: A.recomendacoes(cid), default=[]) or []

    graficos: dict[str, str] = {}

    # ---------- Vendas por dia ----------
    _regista_grafico(
        graficos, "vendas_dia",
        _safe(lambda: A.vendas_por_dia(cid, inicio, fim)),
        lambda df: px.area(
            df, x="dia", y="total", markers=True,
            title="Evolução diária de vendas",
            labels={"dia": "Data", "total": "Total (MZN)"},
        ).update_traces(
            line=dict(width=3, color="#6366f1", shape="spline"),
            fillcolor="rgba(99,102,241,0.18)",
            marker=dict(size=8, color="#6366f1",
                        line=dict(width=2, color="#0f172a")),
            hovertemplate="<b>%{x}</b><br>Total: %{y:,.2f} MZN<extra></extra>",
        ).update_layout(**base_layout_com(showlegend=False)),
    )

    # ---------- Vendas por mês ----------
    _regista_grafico(
        graficos, "vendas_mes",
        _safe(lambda: A.vendas_por_mes(cid)),
        lambda df: px.bar(
            df, x="mes", y="total", text="total",
            title="Vendas por mês",
            labels={"mes": "Mês", "total": "Total (MZN)"},
        ).update_traces(
            marker=dict(
                color=df["total"],
                colorscale=[[0, "#312e81"], [0.5, "#6366f1"], [1, "#22d3ee"]],
                line=dict(width=0),
            ),
            texttemplate="%{text:,.0f}",
            textposition="outside",
            textfont=dict(color="#cbd5e1", size=11),
            hovertemplate="<b>%{x}</b><br>Total: %{y:,.2f} MZN<extra></extra>",
        ).update_layout(**base_layout_com(showlegend=False)),
    )

    # ---------- Vendas por dia da semana ----------
    _regista_grafico(
        graficos, "vendas_dia_semana",
        _safe(lambda: A.vendas_por_dia_semana(cid)),
        lambda df: px.bar(
            df, x="dia_semana", y="total", text="total",
            title="Vendas por dia da semana",
            labels={"dia_semana": "", "total": "Total (MZN)"},
        ).update_traces(
            marker=dict(
                color=df["total"],
                colorscale=[[0, "#1e3a8a"], [0.5, "#6366f1"], [1, "#22d3ee"]],
                line=dict(width=0),
            ),
            texttemplate="%{text:,.0f}",
            textposition="outside",
            textfont=dict(color="#cbd5e1", size=11),
            hovertemplate="<b>%{x}</b><br>Total: %{y:,.2f} MZN<extra></extra>",
        ).update_layout(**base_layout_com(showlegend=False)),
    )

    # ---------- Vendas por hora ----------
    _regista_grafico(
        graficos, "vendas_hora",
        _safe(lambda: A.vendas_por_hora(cid)),
        lambda df: px.bar(
            df, x="hora", y="total", text="total",
            title="Vendas por hora",
            labels={"hora": "Hora do dia", "total": "Total (MZN)"},
        ).update_traces(
            marker=dict(
                color=df["total"],
                colorscale=[[0, "#312e81"], [0.5, "#6366f1"], [1, "#22d3ee"]],
                line=dict(width=0),
            ),
            texttemplate="%{text:,.0f}",
            textposition="outside",
            textfont=dict(color="#cbd5e1", size=11),
            hovertemplate="<b>%{x}h</b><br>Total: %{y:,.2f} MZN<extra></extra>",
        ).update_layout(**base_layout_com(showlegend=False)),
    )

    # ---------- Top produtos (pie) ----------
    _regista_grafico(
        graficos, "top_produtos",
        _safe(lambda: A.top_produtos(cid, 8)),
        lambda df: px.pie(
            df, names="name_product", values="receita",
            title="Top produtos por receita", hole=0.55,
        ).update_traces(
            textposition="inside", textinfo="percent",
            textfont=dict(color="#ffffff", size=11),
            marker=dict(line=dict(color="#0f172a", width=2), colors=PALETA),
            hovertemplate=("<b>%{label}</b><br>Receita: %{value:,.2f} MZN"
                           "<br>%{percent}<extra></extra>"),
        ).update_layout(**base_layout_com(
            legend=dict(orientation="v", yanchor="middle", y=0.5,
                        xanchor="left", x=1.02,
                        bgcolor="rgba(0,0,0,0)",
                        font=dict(color="#cbd5e1", size=11)),
            margin=dict(l=20, r=180, t=60, b=40),
        )),
    )

    # ---------- Top produtos (quantidade) ----------
    _regista_grafico(
        graficos, "top_qtd",
        _safe(lambda: A.produtos_mais_vendidos_qtd(cid, 10)),
        lambda df: px.bar(
            df.sort_values("qtd", ascending=True),
            x="qtd", y="name_product", orientation="h",
            title="Top produtos por quantidade",
            labels={"qtd": "Quantidade", "name_product": ""},
            text="qtd",
        ).update_traces(
            marker=dict(
                color=df.sort_values("qtd", ascending=True)["qtd"],
                colorscale=[[0, "#0e7490"], [1, "#22d3ee"]],
                line=dict(width=0),
            ),
            texttemplate="%{text:,.0f}", textposition="outside",
            textfont=dict(color="#cbd5e1", size=11),
            hovertemplate="<b>%{y}</b><br>Quantidade: %{x}<extra></extra>",
        ).update_layout(**base_layout_com(
            showlegend=False, margin=dict(l=180, r=60, t=60, b=60),
        )),
    )

    # ---------- Menos vendidos ----------
    df_menos = _safe(lambda: A.produtos_menos_vendidos(cid, 10))
    menos_vendidos = df_menos.to_dict("records") if df_menos is not None else []

    _regista_grafico(
        graficos, "menos_vendidos", df_menos,
        lambda df: px.bar(
            df.sort_values("qtd_vendida", ascending=True),
            x="qtd_vendida", y="name_product", orientation="h",
            title="Produtos menos vendidos",
            labels={"qtd_vendida": "Quantidade", "name_product": ""},
            text="qtd_vendida",
        ).update_traces(
            marker=dict(
                color=df.sort_values("qtd_vendida", ascending=True)["qtd_vendida"],
                colorscale=[[0, "#7c2d12"], [1, "#f59e0b"]],
                line=dict(width=0),
            ),
            texttemplate="%{text:,.0f}", textposition="outside",
            textfont=dict(color="#cbd5e1", size=11),
            hovertemplate="<b>%{y}</b><br>Vendidos: %{x}<extra></extra>",
        ).update_layout(**base_layout_com(
            showlegend=False, margin=dict(l=180, r=60, t=60, b=60),
        )),
    )

    # ---------- Pagamentos (roles financeiros) ----------
    if session.get("is_super") or session.get("role") in Auth.ROLES_FINANCEIRO:
        _regista_grafico(
            graficos, "pagamentos",
            _safe(lambda: A.metodos_pagamento(cid)),
            lambda df: px.pie(
                df, names="metodo", values="total",
                title="Formas de pagamento", hole=0.55,
            ).update_traces(
                textposition="inside", textinfo="percent",
                textfont=dict(color="#ffffff", size=11),
                marker=dict(line=dict(color="#0f172a", width=2), colors=PALETA),
                hovertemplate=("<b>%{label}</b><br>Total: %{value:,.2f} MZN"
                               "<br>%{percent}<extra></extra>"),
            ).update_layout(**base_layout_com(
                legend=dict(orientation="v", yanchor="middle", y=0.5,
                            xanchor="left", x=1.02,
                            bgcolor="rgba(0,0,0,0)",
                            font=dict(color="#cbd5e1", size=11)),
                margin=dict(l=20, r=180, t=60, b=40),
            )),
        )

    # ---------- Stock ----------
    _regista_grafico(
        graficos, "stock",
        _safe(lambda: A.alertas_stock(cid)),
        lambda df: px.bar(
            df.sort_values("stock", ascending=True),
            x="stock", y="name_product", orientation="h",
            title="Stock atual — mais críticos",
            labels={"stock": "Unidades", "name_product": ""},
            text="stock",
        ).update_traces(
            marker=dict(
                color=df.sort_values("stock", ascending=True)["stock"],
                colorscale=[[0, "#ef4444"], [0.5, "#f59e0b"], [1, "#10b981"]],
                line=dict(width=0),
            ),
            texttemplate="%{text:,.0f}", textposition="outside",
            textfont=dict(color="#cbd5e1", size=11),
            hovertemplate="<b>%{y}</b><br>Stock: %{x}<extra></extra>",
        ).update_layout(**base_layout_com(
            showlegend=False, margin=dict(l=180, r=60, t=60, b=60),
        )),
    )

    # ---------- Operadores ----------
    _regista_grafico(
        graficos, "operadores",
        _safe(lambda: A.ranking_operadores(cid)),
        lambda df: px.bar(
            df.sort_values("total", ascending=True),
            x="total", y="name_user", orientation="h",
            title="Ranking de operadores",
            labels={"total": "Receita (MZN)", "name_user": ""},
            text="total",
        ).update_traces(
            marker=dict(
                color=df.sort_values("total", ascending=True)["total"],
                colorscale=[[0, "#4c1d95"], [0.5, "#8b5cf6"], [1, "#22d3ee"]],
                line=dict(width=0),
            ),
            texttemplate="%{text:,.0f}", textposition="outside",
            textfont=dict(color="#cbd5e1", size=11),
            hovertemplate="<b>%{y}</b><br>Receita: %{x:,.2f} MZN<extra></extra>",
        ).update_layout(**base_layout_com(
            showlegend=False, margin=dict(l=180, r=60, t=60, b=60),
        )),
    )

    # ---------- Top clientes ----------
    df_cli = _safe(lambda: A.vendas_por_cliente(cid, 10))
    top_clientes = df_cli.to_dict("records") if df_cli is not None else []

    _regista_grafico(
        graficos, "top_clientes", df_cli,
        lambda df: px.bar(
            df.sort_values("total", ascending=True),
            x="total", y="name_client", orientation="h",
            title="Top clientes por receita",
            labels={"total": "Receita (MZN)", "name_client": ""},
            text="total",
        ).update_traces(
            marker=dict(
                color=df.sort_values("total", ascending=True)["total"],
                colorscale=[[0, "#065f46"], [0.5, "#10b981"], [1, "#22d3ee"]],
                line=dict(width=0),
            ),
            texttemplate="%{text:,.0f}", textposition="outside",
            textfont=dict(color="#cbd5e1", size=11),
            hovertemplate="<b>%{y}</b><br>Receita: %{x:,.2f} MZN<extra></extra>",
        ).update_layout(**base_layout_com(
            showlegend=False, margin=dict(l=180, r=60, t=60, b=60),
        )),
    )

    # ---------- Nunca vendidos ----------
    nunca_vendidos = _safe(
        lambda: A.produtos_nunca_vendidos(cid).to_dict("records"),
        default=[],
    ) or []

    # ---------- Log final ----------
    todas_chaves = [
        "vendas_dia", "vendas_mes", "vendas_dia_semana", "vendas_hora",
        "top_produtos", "top_qtd", "menos_vendidos", "pagamentos",
        "stock", "operadores", "top_clientes",
    ]
    registados = set(graficos.keys())
    omitidos   = [k for k in todas_chaves if k not in registados]

    log.info(
        "Empresa %s (user=%s role=%s global=%s): gráficos=%s | omitidos=%s | recs=%d",
        cid, session.get("user_id"), session.get("role"),
        session.get("is_global"), sorted(registados),
        omitidos or "—", len(recomendacoes),
    )

    return render_template(
        "empresa.html",
        empresas=empresas,
        empresa=info,
        kpis=kpis,
        graficos=graficos,
        nunca_vendidos=nunca_vendidos,
        menos_vendidos=menos_vendidos,
        top_clientes=top_clientes,
        pico=pico,
        recomendacoes=recomendacoes,
        periodo_label=label or "Todo o histórico",
        inicio=inicio or "",
        fim=fim or "",
    )


# ================================================================== #
# 07. ÁREA ADMIN
# ================================================================== #
@app.route("/admin/utilizadores")
@Auth.role_required(Auth.ROLE_MANAGER)
def admin_utilizadores():
    cid = session.get("company_id")
    if Auth.is_global_viewer():
        try:
            df = A.q("""
                SELECT u.id_user, u.name_user, u.email_user, u.phone_user,
                       u.role_user, u.status_user, u.access_code_user,
                       u.id_company_user, c.name_company,
                       u.last_login_user, u.created_at
                FROM users u
                LEFT JOIN companies c ON c.id_company = u.id_company_user
                ORDER BY u.created_at DESC
                LIMIT 500
            """)
            users = df.to_dict("records")
        except Exception as e:
            log.exception("admin_utilizadores: %s", e)
            users = []
    else:
        try:
            df = A.q("""
                SELECT u.id_user, u.name_user, u.email_user, u.phone_user,
                       u.role_user, u.status_user, u.access_code_user,
                       u.id_company_user, c.name_company,
                       u.last_login_user, u.created_at
                FROM users u
                LEFT JOIN companies c ON c.id_company = u.id_company_user
                WHERE u.id_company_user = :cid
                ORDER BY u.created_at DESC
            """, {"cid": cid})
            users = df.to_dict("records")
        except Exception as e:
            log.exception("admin_utilizadores (empresa): %s", e)
            users = []

    return render_template("admin_users.html", users=users)


# ================================================================== #
# 08. APIs JSON
# ================================================================== #
@app.route("/api/empresas")
@Auth.login_required
def api_empresas():
    return jsonify(Auth.visible_companies())


@app.route("/api/comparativo")
@Auth.login_required
def api_comparativo():
    if not Auth.is_global_viewer():
        cid = session.get("company_id")
        df = _safe(lambda: A.q("""
            SELECT c.id_company, c.name_company,
                   COALESCE(SUM(s.total_sale),0) AS total_vendas,
                   COUNT(s.id_sale) AS n_vendas
            FROM companies c
            LEFT JOIN sales s ON s.id_company_sale = c.id_company
            WHERE c.id_company = :cid
            GROUP BY c.id_company, c.name_company
        """, {"cid": cid}))
    else:
        df = _safe(A.comparativo_empresas)

    if df is None or df.empty:
        return jsonify([])
    return jsonify(df.to_dict("records"))


@app.route("/api/empresa/<int:cid>/kpis")
@Auth.login_required
def api_kpis(cid):
    Auth.ensure_company_access(cid)
    inicio, fim, _ = _periodo_args()
    return jsonify(
        _safe(lambda: A.kpis_empresa(cid, inicio, fim),
              default=dict(KPIS_DEFAULT))
        or dict(KPIS_DEFAULT)
    )


@app.route("/api/empresa/<int:cid>/produtos")
@Auth.login_required
def api_produtos(cid):
    Auth.ensure_company_access(cid)
    df = _safe(lambda: A.top_produtos(cid, 20))
    return jsonify(df.to_dict("records") if df is not None else [])


@app.route("/api/empresa/<int:cid>/nunca_vendidos")
@Auth.login_required
def api_nunca_vendidos(cid):
    Auth.ensure_company_access(cid)
    df = _safe(lambda: A.produtos_nunca_vendidos(cid))
    return jsonify(df.to_dict("records") if df is not None else [])


# ================================================================== #
# 09. EXPORT CSV
# ================================================================== #
def _csv_response(df: pd.DataFrame, nome: str) -> Response:
    if df is None or df.empty:
        abort(404)
    buf = io.StringIO()
    df.to_csv(buf, index=False, sep=";")
    payload = "\ufeff" + buf.getvalue()
    return Response(
        payload,
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{nome}.csv"'},
    )


@app.route("/export/empresa/<int:cid>/vendas.csv")
@Auth.login_required
def export_vendas(cid):
    Auth.ensure_company_access(cid)
    inicio, fim, _ = _periodo_args()
    df = _safe(lambda: A.vendas_por_dia(cid, inicio, fim))
    return _csv_response(df, f"vendas_empresa_{cid}")


@app.route("/export/empresa/<int:cid>/produtos.csv")
@Auth.login_required
def export_produtos(cid):
    Auth.ensure_company_access(cid)
    df = _safe(lambda: A.top_produtos(cid, 500))
    return _csv_response(df, f"produtos_empresa_{cid}")


@app.route("/export/comparativo.csv")
@Auth.global_view_required
def export_comparativo():
    df = _safe(A.comparativo_empresas)
    return _csv_response(df, "comparativo_empresas")


# ================================================================== #
# 10. HANDLERS DE ERRO
# ================================================================== #
@app.errorhandler(403)
def forbidden(e):
    msg = str(getattr(e, "description", "Sem permissão"))
    role = session.get("role")
    if role == Auth.ROLE_OPERATOR:
        msg = ("A tua conta é de operador e só tem acesso à tua empresa. "
               "Contacta o administrador para acesso multi-empresa.")
    elif role == Auth.ROLE_EXTERNAL:
        msg = "A tua conta é externa e só tem acesso a relatórios."
    return render_template(
        "erro.html", codigo=403, mensagem=msg,
        empresas=Auth.visible_companies() if "user_id" in session else [],
    ), 403


@app.errorhandler(404)
def not_found(e):
    return render_template(
        "erro.html", codigo=404,
        mensagem=str(getattr(e, "description", "Página não encontrada")),
        empresas=Auth.visible_companies() if "user_id" in session else [],
    ), 404


@app.errorhandler(500)
def server_error(e):
    log.exception("Erro 500: %s", e)
    return render_template(
        "erro.html", codigo=500,
        mensagem="Erro interno no servidor.",
        empresas=Auth.visible_companies() if "user_id" in session else [],
    ), 500


# ================================================================== #
# 11. ENTRYPOINT
# ================================================================== #
if __name__ == "__main__":
    log.info("A iniciar BIP-PME Analytics...")

    use_reload = os.getenv("FLASK_USE_RELOADER", "0") == "1"

    app.run(
        debug=True,
        host="0.0.0.0",
        port=5000,
        use_reloader=use_reload,
        threaded=True,
    )