"""
auth.py — Autenticação e isolamento multi-tenant para BIP-PME Analytics.

Perfis e âmbito de visão:
  • ADMIN / SUPER_ADMIN  → vê TODAS as empresas, todas as telas
  • MANAGER              → vê TODAS as empresas, todas as telas
  • BI_ANALYST           → vê TODAS as empresas, só leitura
  • OPERATOR             → vê SÓ a sua empresa (análise local)
  • EXTERNAL             → vê SÓ a sua empresa (relatórios)

Login aceita:
  • email + password
  • access_code_user (BIP-XXXX-XXXX) sem password (para operadores)

Compatibilidade de hashes:
  • bcrypt PHP  → $2y$  (convertido automaticamente para $2b$)
  • bcrypt      → $2a$ / $2b$
  • werkzeug    → pbkdf2:sha256 / scrypt

Requisitos extra:  pip install bcrypt
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from functools import wraps

from flask import (
    session, redirect, url_for, request, abort,
    render_template, flash, Blueprint,
)
from werkzeug.security import check_password_hash, generate_password_hash
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

# bcrypt é opcional — só precisas se tiveres hashes $2y$ / $2a$ / $2b$
try:
    import bcrypt as _bcrypt
    _HAS_BCRYPT = True
except ImportError:
    _bcrypt = None
    _HAS_BCRYPT = False

import analytics as A

log = logging.getLogger("bip-pme.auth")


# ================================================================== #
# 01. CONSTANTES DE PERFIL
# ================================================================== #
ROLE_SUPER_ADMIN  = "super_admin"
ROLE_MANAGER      = "manager"
ROLE_OPERATOR     = "operator"
ROLE_BI           = "bi_analyst"
ROLE_EXTERNAL     = "external"
ROLE_ADMIN_LEGACY = "admin"          # compatibilidade com o dump

ROLES_VALIDOS = {
    ROLE_SUPER_ADMIN, ROLE_MANAGER, ROLE_OPERATOR,
    ROLE_BI, ROLE_EXTERNAL, ROLE_ADMIN_LEGACY,
}

ROLES_ANALISE_GLOBAL = {
    ROLE_ADMIN_LEGACY, ROLE_SUPER_ADMIN, ROLE_MANAGER, ROLE_BI,
}

ROLES_ANALISE_LOCAL = {ROLE_OPERATOR, ROLE_EXTERNAL}
ROLES_ANALISE = ROLES_ANALISE_GLOBAL | ROLES_ANALISE_LOCAL

ROLES_FINANCEIRO = {
    ROLE_ADMIN_LEGACY, ROLE_SUPER_ADMIN, ROLE_MANAGER, ROLE_BI,
}

ROLES_ESCRITA = {
    ROLE_ADMIN_LEGACY, ROLE_SUPER_ADMIN, ROLE_MANAGER, ROLE_OPERATOR,
}

ROLES_GERIR_USERS = {
    ROLE_ADMIN_LEGACY, ROLE_SUPER_ADMIN, ROLE_MANAGER,
}


# ================================================================== #
# 02. VERIFICAÇÃO DE PASSWORD
# ================================================================== #
def _verify_password(stored_hash: str | None, plain: str | None) -> bool:
    """
    Verifica password aceitando bcrypt (PHP/Python) e werkzeug.
    """
    if not stored_hash or not plain:
        return False

    stored_hash = stored_hash.strip()

    # ---------- bcrypt (PHP $2y$, $2a$, $2b$) ----------
    if stored_hash.startswith(("$2y$", "$2a$", "$2b$")):
        if not _HAS_BCRYPT:
            log.error("Hash bcrypt detectado mas o módulo `bcrypt` não está instalado. Corre: pip install bcrypt")
            return False

        hash_check = stored_hash
        if hash_check.startswith("$2y$"):
            hash_check = "$2b$" + hash_check[4:]

        try:
            ok = _bcrypt.checkpw(plain.encode("utf-8"), hash_check.encode("utf-8"))
            if not ok:
                log.info("Tentativa de login falhou: password incorreta (bcrypt).")
            return ok
        except (ValueError, TypeError) as e:
            log.warning("bcrypt.checkpw falhou: %s", e)
            return False

    # ---------- werkzeug ----------
    if stored_hash.startswith(("pbkdf2:", "scrypt:", "$pbkdf2$")):
        try:
            ok = check_password_hash(stored_hash, plain)
            if not ok:
                log.info("Tentativa de login falhou: password incorreta (werkzeug).")
            return ok
        except (ValueError, TypeError) as e:
            log.warning("check_password_hash falhou: %s", e)
            return False

    # ---------- fallback (só debug) ----------
    log.warning("Hash não reconhecido — a comparar em plaintext (INSEGURO)")
    return stored_hash == plain


def _hash_password(plain: str) -> str:
    """Gera hash compatível com werkzeug (pbkdf2:sha256)."""
    return generate_password_hash(plain, method="pbkdf2:sha256")


# ================================================================== #
# 03. ENGINE / QUERIES RÁPIDAS
# ================================================================== #
def _engine():
    return A.ENGINE


def _q(sql: str, params: dict | None = None) -> list[dict]:
    with _engine().connect() as con:
        rows = con.execute(text(sql), params or {}).mappings().all()
        return [dict(r) for r in rows]


def _q1(sql: str, params: dict | None = None) -> dict | None:
    rows = _q(sql, params)
    return rows[0] if rows else None


# ================================================================== #
# 04. CARREGAR UTILIZADORES
# ================================================================== #
def get_user_by_email(email: str) -> dict | None:
    if not email:
        return None
    return _q1("""
        SELECT u.*, c.name_company, c.status_company,
               c.modules_enabled_company, c.activation_code_company
        FROM users u
        LEFT JOIN companies c ON c.id_company = u.id_company_user
        WHERE u.email_user = :email
          AND u.status_user = 'active'
        LIMIT 1
    """, {"email": email.strip().lower()})


def get_user_by_access_code(code: str) -> dict | None:
    if not code:
        return None
    return _q1("""
        SELECT u.*, c.name_company, c.status_company,
               c.modules_enabled_company, c.activation_code_company
        FROM users u
        LEFT JOIN companies c ON c.id_company = u.id_company_user
        WHERE u.access_code_user = :code
          AND u.status_user = 'active'
        LIMIT 1
    """, {"code": code.strip().upper()})


def get_user_by_id(uid: int) -> dict | None:
    return _q1("""
        SELECT u.*, c.name_company, c.status_company,
               c.modules_enabled_company, c.activation_code_company
        FROM users u
        LEFT JOIN companies c ON c.id_company = u.id_company_user
        WHERE u.id_user = :uid
        LIMIT 1
    """, {"uid": uid})


# ================================================================== #
# 05. HELPERS DE SESSÃO
# ================================================================== #
def _modulos(user: dict) -> list[str]:
    raw = user.get("modules_enabled_company")
    if not raw:
        return ["*"] if _is_super(user) else []
    try:
        return json.loads(raw) if isinstance(raw, str) else list(raw)
    except (ValueError, TypeError):
        return []


def _is_super(user: dict) -> bool:
    if user.get("is_super_admin"):
        return True
    return user.get("role_user") in (ROLE_SUPER_ADMIN, ROLE_ADMIN_LEGACY)


def login_user(user: dict) -> None:
    """Coloca o utilizador na sessão."""
    session.clear()
    session.permanent = True
    session["user_id"]      = int(user["id_user"])
    session["company_id"]   = int(user["id_company_user"]) if user.get("id_company_user") else None
    session["role"]         = user.get("role_user", ROLE_OPERATOR)
    session["name"]         = user.get("name_user", "")
    session["email"]        = user.get("email_user") or ""
    session["company_name"] = user.get("name_company") or "—"
    session["is_super"]     = _is_super(user)
    session["is_global"]    = (session["is_super"]
                               or session["role"] in ROLES_ANALISE_GLOBAL)
    session["modules"]      = _modulos(user)
    session["logged_in_at"] = datetime.utcnow().isoformat()

    # atualiza last_login
    try:
        with _engine().begin() as con:
            con.execute(text("""
                UPDATE users SET last_login_user = NOW()
                WHERE id_user = :uid
            """), {"uid": user["id_user"]})
    except SQLAlchemyError as e:
        log.warning("Não foi possível atualizar last_login: %s", e)


def logout_user() -> None:
    session.clear()


# ================================================================== #
# 06. DECORATORS
# ================================================================== #
def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            if request.path.startswith("/api/"):
                return {"error": "unauthorized"}, 401
            return redirect(url_for("auth.login", next=request.path))
        return f(*args, **kwargs)
    return wrapper


def role_required(*roles):
    """Exige que o utilizador tenha um dos roles indicados."""
    def deco(f):
        @wraps(f)
        @login_required
        def wrapper(*args, **kwargs):
            if session.get("is_super"):
                return f(*args, **kwargs)
            if session.get("role") not in roles:
                abort(403, description="Sem permissão para esta área.")
            return f(*args, **kwargs)
        return wrapper
    return deco


def global_view_required(f):
    """Exige que o utilizador tenha visão global (admin/manager/bi)."""
    @wraps(f)
    @login_required
    def wrapper(*args, **kwargs):
        if not is_global_viewer():
            abort(403, description="Sem permissão para visão multi-empresa.")
        return f(*args, **kwargs)
    return wrapper


def module_required(module: str):
    """Bloqueia se o módulo não estiver ativo na empresa (super admin passa sempre)."""
    def deco(f):
        @wraps(f)
        @login_required
        def wrapper(*args, **kwargs):
            if session.get("is_super"):
                return f(*args, **kwargs)
            mods = session.get("modules") or []
            if "*" not in mods and module not in mods:
                abort(403, description=f"Módulo '{module}' não está ativo.")
            return f(*args, **kwargs)
        return wrapper
    return deco


# ================================================================== #
# 07. ISOLAMENTO POR EMPRESA
# ================================================================== #
def current_company_id() -> int | None:
    return session.get("company_id")


def is_global_viewer() -> bool:
    if session.get("is_super"):
        return True
    if session.get("is_global"):
        return True
    return session.get("role") in ROLES_ANALISE_GLOBAL


def ensure_company_access(cid: int) -> None:
    if is_global_viewer():
        return
    if cid != session.get("company_id"):
        log.warning(
            "Acesso negado: user %s (role=%s) tentou ver empresa %s "
            "(pertence a %s)",
            session.get("user_id"), session.get("role"),
            cid, session.get("company_id"),
        )
        abort(403, description="Não tens acesso a esta empresa.")


def visible_companies() -> list[dict]:
    if is_global_viewer():
        try:
            return A.listar_empresas().to_dict("records")
        except Exception as e:
            log.exception("visible_companies (global): %s", e)
            return []

    cid = session.get("company_id")
    if not cid:
        return []
    try:
        df = A.q(
            "SELECT id_company, name_company, sector_company, status_company "
            "FROM companies WHERE id_company = :cid",
            {"cid": cid},
        )
        return df.to_dict("records")
    except Exception as e:
        log.exception("visible_companies (local): %s", e)
        return []


# ================================================================== #
# 08. AUDITORIA
# ================================================================== #
def audit(action: str, resource: str = "", payload: dict | None = None) -> None:
    try:
        with _engine().begin() as con:
            con.execute(text("""
                INSERT INTO audit_logs
                  (id_user_audit_log, action_audit_log, resource_audit_log,
                   ip_audit_log, payload_audit_log, created_at)
                VALUES (:uid, :act, :res, :ip, :pl, NOW())
            """), {
                "uid": session.get("user_id"),
                "act": action,
                "res": resource,
                "ip":  request.remote_addr if request else None,
                "pl":  json.dumps(payload, ensure_ascii=False) if payload else None,
            })
    except SQLAlchemyError as e:
        log.warning("audit falhou: %s", e)


# ================================================================== #
# 09. BLUEPRINT DE AUTENTICAÇÃO
# ================================================================== #
auth_bp = Blueprint("auth", __name__)


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if "user_id" in session:
        return redirect(url_for("index"))

    erro = None
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        pwd   = request.form.get("password") or ""
        code  = (request.form.get("access_code") or "").strip().upper()

        user = None

        # 1. Tenta via código de acesso (se preenchido)
        if code:
            user = get_user_by_access_code(code)
            if not user:
                log.info("Login falhou: código de acesso '%s' não encontrado ou inativo.", code)
                erro = "Código de acesso inválido."
        
        # 2. Tenta via email + password (se preenchidos)
        if not user and email and pwd:
            user = get_user_by_email(email)
            if not user:
                log.info("Login falhou: email '%s' não encontrado ou inativo.", email)
                erro = "Credenciais inválidas (email não encontrado)."
            elif not user.get("password_user"):
                log.info("Login falhou: user '%s' não tem password definida.", email)
                erro = "Esta conta não tem password. Usa o código de acesso."
            elif not _verify_password(user["password_user"], pwd):
                erro = "Credenciais inválidas (password incorreta)."

        # 3. Se não preencheu nada de útil
        if not code and not (email and pwd):
            erro = "Preenche email+password ou o código de acesso."

        # 4. Validações finais de empresa e login
        if user and not erro:
            if user.get("status_company") and user["status_company"] != "active":
                erro = "A empresa está inativa. Contacta o suporte."
            else:
                login_user(user)
                audit("auth.login", f"users/{user['id_user']}")
                log.info("Login com sucesso: %s (role=%s)", user.get("email_user"), user.get("role_user"))
                
                nxt = request.args.get("next") or request.form.get("next")
                if nxt and not nxt.startswith("/"):
                    nxt = None
                return redirect(nxt or url_for("index"))

    status = 401 if erro else 200
    return render_template("login.html", erro=erro), status


@auth_bp.route("/logout")
def logout():
    uid = session.get("user_id")
    if uid:
        audit("auth.logout", f"users/{uid}")
    logout_user()
    return redirect(url_for("auth.login"))


@auth_bp.route("/perfil")
@login_required
def perfil():
    user = get_user_by_id(session["user_id"]) or {}
    user.pop("password_user", None)
    return render_template("perfil.html", u=user)


@auth_bp.route("/perfil/password", methods=["POST"])
@login_required
def alterar_password():
    atual = request.form.get("atual", "")
    nova  = request.form.get("nova", "")

    if len(nova) < 6:
        flash("A nova password deve ter pelo menos 6 caracteres.", "erro")
        return redirect(url_for("auth.perfil"))

    user = get_user_by_id(session["user_id"])
    if not user:
        abort(404)

    if user.get("password_user"):
        if not _verify_password(user["password_user"], atual):
            flash("Password atual incorreta.", "erro")
            return redirect(url_for("auth.perfil"))

    novo_hash = _hash_password(nova)
    try:
        with _engine().begin() as con:
            con.execute(text("""
                UPDATE users SET password_user = :h, updated_at = NOW()
                WHERE id_user = :uid
            """), {"h": novo_hash, "uid": session["user_id"]})
        audit("users.password_change", f"users/{session['user_id']}")
        flash("Password atualizada com sucesso.", "ok")
    except SQLAlchemyError as e:
        log.exception("alterar_password: %s", e)
        flash("Erro ao atualizar password.", "erro")

    return redirect(url_for("auth.perfil"))
