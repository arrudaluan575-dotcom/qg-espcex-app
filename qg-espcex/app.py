from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import tempfile
import time
import uuid
import html
import datetime
import sqlite3
import threading
import secrets as py_secrets
import urllib.request
from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import plotly.io as pio
import random

# =============================================================================
# SEGURANÇA LOCAL
# =============================================================================
_USERS_STORE_PATH = Path("usuarios.json")
_ITERATIONS = 600_000
_ATTEMPT_FILE = Path(".qg_login_attempts.json")
_USER_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,31}$")
_SQLITE_CONNECT_LOCAL = sqlite3.connect


def _qg_database_url() -> str:
    """Lê a conexão PostgreSQL apenas de Secrets ou de uma variável de ambiente."""
    try:
        valor = st.secrets.get("QG_DATABASE_URL", "")
    except Exception:
        valor = ""
    return str(valor or os.environ.get("QG_DATABASE_URL", "")).strip()


def _qg_cloud_enabled() -> bool:
    return bool(_qg_database_url())


@st.cache_resource(show_spinner=False)
def _qg_initialize_cloud_storage(database_url: str) -> bool:
    """Cria as tabelas de contas, progresso, tentativas, sessões e snapshot SQLite."""
    import psycopg

    with psycopg.connect(database_url, connect_timeout=8) as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS qg_users (
            username TEXT PRIMARY KEY, payload TEXT NOT NULL, updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS qg_user_data (
            username TEXT PRIMARY KEY, payload TEXT NOT NULL, updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS qg_login_attempts (
            attempt_key TEXT PRIMARY KEY, payload TEXT NOT NULL, updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS qg_auth_sessions (
            session_hash TEXT PRIMARY KEY, username TEXT NOT NULL, expires_at TIMESTAMPTZ NOT NULL,
            revoked_at TIMESTAMPTZ, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS qg_auth_sessions_username_idx ON qg_auth_sessions (username)")
        conn.execute("""CREATE TABLE IF NOT EXISTS qg_sqlite_snapshot (
            snapshot_id SMALLINT PRIMARY KEY CHECK (snapshot_id = 1),
            payload BYTEA NOT NULL, version BIGINT NOT NULL, updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )""")
    return True


def _qg_pg_connection():
    """Abre a base configurada e garante que o esquema do QG existe."""
    database_url = _qg_database_url()
    if not database_url:
        raise RuntimeError("QG_DATABASE_URL não foi configurada.")
    _qg_initialize_cloud_storage(database_url)
    import psycopg
    return psycopg.connect(database_url, connect_timeout=8)


def _qg_json_or_default(texto, padrao):
    try:
        valor = json.loads(texto)
        return valor
    except (TypeError, ValueError, json.JSONDecodeError):
        return padrao


def _qg_proteger_senha_legada(conta: dict) -> dict:
    """Converte senhas em texto puro de cadastros antigos para PBKDF2 antes da migração."""
    protegida = dict(conta)
    senha = str(protegida.get("senha", ""))
    e_hash_sha256 = len(senha) == 64 and all(c in "0123456789abcdef" for c in senha.casefold())
    if senha and not senha.startswith("pbkdf2_sha256$") and not e_hash_sha256:
        protegida["senha"] = hash_senha(senha)
    return protegida


@st.cache_resource(show_spinner=False)
def _qg_sqlite_runtime_state():
    """Mantém um único arquivo SQLite local sincronizado e serializado por processo."""
    return {"lock": threading.RLock(), "ready": False, "database_url": "", "version": 0, "force_upload": False}


def _qg_digest_file(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _qg_write_local_sqlite_snapshot(path: Path, payload: bytes) -> None:
    """Restaura atomicamente o snapshot cloud antes de abrir conexões SQLite."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".qg-cloud-db-", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as arquivo:
            arquivo.write(payload)
            arquivo.flush()
            os.fsync(arquivo.fileno())
        for sufixo in ("-wal", "-shm"):
            Path(str(path) + sufixo).unlink(missing_ok=True)
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def _qg_load_sqlite_snapshot(path: Path, database_url: str) -> int:
    """Carrega o banco compartilhado completo do PostgreSQL na primeira conexão."""
    with _qg_pg_connection() as conn:
        row = conn.execute("SELECT version, payload FROM qg_sqlite_snapshot WHERE snapshot_id = 1").fetchone()
    if row:
        _qg_write_local_sqlite_snapshot(path, bytes(row[1]))
        return int(row[0])
    return 0


def _qg_upload_sqlite_snapshot(path: Path, state: dict) -> None:
    """Persiste as tabelas SQLite existentes sem reescrever os módulos que as usam."""
    if not path.is_file():
        return
    payload = path.read_bytes()
    with _qg_pg_connection() as conn:
        # Um bloqueio transacional evita que dois processos sobrescrevam o mesmo snapshot ao mesmo tempo.
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (894784853,))
        row = conn.execute("SELECT version FROM qg_sqlite_snapshot WHERE snapshot_id = 1 FOR UPDATE").fetchone()
        versao_remota = int(row[0]) if row else 0
        if versao_remota != int(state.get("version", 0)):
            # Protege dados de outra instância: sinaliza para buscar a versão atual antes da próxima operação.
            state["ready"] = False
            state["force_upload"] = False
            raise RuntimeError("O banco foi alterado por outra instância. Atualize a página e repita a operação.")
        nova_versao = versao_remota + 1
        conn.execute("""INSERT INTO qg_sqlite_snapshot (snapshot_id, payload, version, updated_at)
            VALUES (1, %s, %s, NOW())
            ON CONFLICT (snapshot_id) DO UPDATE
            SET payload = EXCLUDED.payload, version = EXCLUDED.version, updated_at = NOW()""",
            (payload, nova_versao))
    state["version"] = nova_versao
    state["force_upload"] = False


class _QGCloudSQLiteConnection(sqlite3.Connection):
    """Conexão SQLite compatível com pandas que sincroniza o arquivo ao fechar."""
    def _qg_attach(self, state: dict, path: Path) -> None:
        self._qg_state = state
        self._qg_path = path
        self._qg_changes_before = self.total_changes
        self._qg_schema_before = self.execute("PRAGMA schema_version").fetchone()[0]
        self._qg_cloud_closed = False

    def close(self) -> None:
        if getattr(self, "_qg_cloud_closed", True):
            return
        state = self._qg_state
        try:
            schema_after = self.execute("PRAGMA schema_version").fetchone()[0]
            database_alterado = (
                self.total_changes != self._qg_changes_before
                or schema_after != self._qg_schema_before
                or state.get("force_upload")
            )
            super().close()
            if database_alterado:
                _qg_upload_sqlite_snapshot(self._qg_path, state)
        finally:
            self._qg_cloud_closed = True
            state["lock"].release()

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


def _qg_sqlite_connect(database=":memory:", *args, **kwargs):
    """Usa PostgreSQL como armazenamento durável do SQLite compartilhado quando configurado."""
    if not _qg_cloud_enabled() or str(database) == ":memory:" or Path(str(database)).name != "dados_qg.db":
        return _SQLITE_CONNECT_LOCAL(database, *args, **kwargs)

    state = _qg_sqlite_runtime_state()
    state["lock"].acquire()
    try:
        database_url = _qg_database_url()
        path = Path(str(database)).resolve()
        if not state["ready"] or state["database_url"] != database_url:
            state["version"] = _qg_load_sqlite_snapshot(path, database_url)
            state["database_url"] = database_url
            state["ready"] = True
            state["force_upload"] = state["version"] == 0
        kwargs.pop("factory", None)
        connection = _SQLITE_CONNECT_LOCAL(
            database, *args, factory=_QGCloudSQLiteConnection, **kwargs
        )
        connection._qg_attach(state, path)
        return connection
    except Exception:
        state["lock"].release()
        raise


# Redireciona apenas conexões para dados_qg.db; arquivos temporários e bancos :memory: seguem SQLite local.
sqlite3.connect = _qg_sqlite_connect


def normalizar_usuario(valor: str) -> str:
    usuario = str(valor or "").strip().casefold()
    return usuario if _USER_RE.fullmatch(usuario) else ""

def senha_forte(senha: str) -> bool:
    return len(senha) >= 12 and any(c.isalpha() for c in senha) and any(c.isdigit() for c in senha)

def hash_senha(senha: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", senha.encode("utf-8"), salt, _ITERATIONS)
    return f"pbkdf2_sha256${_ITERATIONS}${salt.hex()}${digest.hex()}"

def verificar_senha(senha: str, armazenada: str) -> bool:
    """Valida hashes novos e hashes SHA-256 legados, para migração no login."""
    try:
        if armazenada.startswith("pbkdf2_sha256$"):
            _, rounds, salt_hex, digest_hex = armazenada.split("$", 3)
            digest = hashlib.pbkdf2_hmac("sha256", senha.encode("utf-8"), bytes.fromhex(salt_hex), int(rounds))
            return hmac.compare_digest(digest.hex(), digest_hex)
        # Compatibilidade com contas existentes; o app troca para PBKDF2 após login.
        return hmac.compare_digest(hashlib.sha256(senha.encode("utf-8")).hexdigest(), armazenada)
    except (ValueError, TypeError):
        return False

def carregar_usuarios() -> dict:
    if _qg_cloud_enabled():
        with _qg_pg_connection() as conn:
            rows = conn.execute("SELECT username, payload FROM qg_users ORDER BY username").fetchall()
        if not rows:
            # Migra automaticamente cadastros legados locais na primeira inicialização da base remota.
            try:
                legado = json.loads(_USERS_STORE_PATH.read_text(encoding="utf-8"))
                if isinstance(legado, dict) and legado:
                    migrados = {
                        normalizar_usuario(username): _qg_proteger_senha_legada(conta)
                        for username, conta in legado.items()
                        if normalizar_usuario(username) and isinstance(conta, dict)
                    }
                    salvar_usuarios(migrados)
                    return migrados
            except (OSError, json.JSONDecodeError):
                pass
        usuarios = {}
        for username, payload in rows:
            usuario = _qg_json_or_default(payload, {})
            if isinstance(usuario, dict):
                usuarios[str(username)] = usuario
        return usuarios
    try:
        data = json.loads(_USERS_STORE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def carregar_usuario(username: str) -> dict:
    """Busca apenas o cadastro necessário para login/perfil, sem carregar hashes de todas as contas."""
    username = normalizar_usuario(username)
    if not username:
        return {}
    if _qg_cloud_enabled():
        with _qg_pg_connection() as conn:
            row = conn.execute("SELECT payload FROM qg_users WHERE username = %s", (username,)).fetchone()
        if row:
            valor = _qg_json_or_default(row[0], {})
            return valor if isinstance(valor, dict) else {}
        # Migração sob demanda do usuário correspondente, caso a instalação ainda tenha usuarios.json.
        try:
            legados = json.loads(_USERS_STORE_PATH.read_text(encoding="utf-8"))
            conta = legados.get(username, {}) if isinstance(legados, dict) else {}
            if isinstance(conta, dict) and conta:
                conta = _qg_proteger_senha_legada(conta)
                salvar_usuarios({username: conta})
                return conta
        except (OSError, json.JSONDecodeError):
            pass
        return {}
    usuarios = carregar_usuarios()
    conta = usuarios.get(username, {})
    return conta if isinstance(conta, dict) else {}

def salvar_usuarios(usuarios: dict) -> None:
    if _qg_cloud_enabled():
        with _qg_pg_connection() as conn:
            for username, payload in usuarios.items():
                username_limpo = normalizar_usuario(username)
                if not username_limpo or not isinstance(payload, dict):
                    continue
                conn.execute("""INSERT INTO qg_users (username, payload, updated_at)
                    VALUES (%s, %s, NOW())
                    ON CONFLICT (username) DO UPDATE
                    SET payload = EXCLUDED.payload, updated_at = NOW()""",
                    (username_limpo, json.dumps(payload, ensure_ascii=False)))
        return
    _USERS_STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix="usuarios-", suffix=".tmp", dir=str(_USERS_STORE_PATH.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(usuarios, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_name, _USERS_STORE_PATH)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)

def verificar_credencial(valor: str, armazenada: str) -> bool:
    if armazenada.startswith("pbkdf2_sha256$"):
        return verificar_senha(valor, armazenada)
    if len(armazenada) == 64 and all(c in "0123456789abcdef" for c in armazenada.casefold()):
        return verificar_senha(valor, armazenada)
    return hmac.compare_digest(str(valor).encode("utf-8"), str(armazenada).encode("utf-8"))

def tempo_bloqueio(chave: str) -> int:
    if _qg_cloud_enabled():
        with _qg_pg_connection() as conn:
            row = conn.execute("SELECT payload FROM qg_login_attempts WHERE attempt_key = %s", (str(chave),)).fetchone()
        estado_item = _qg_json_or_default(row[0], {}) if row else {}
        try:
            ate = float(estado_item.get("bloqueado_ate", 0))
            return max(0, int(ate - time.time() + 0.999))
        except (TypeError, ValueError, AttributeError):
            return 0
    try:
        estado = json.loads(_ATTEMPT_FILE.read_text(encoding="utf-8"))
        item = estado.get(str(chave), {})
        ate = float(item.get("bloqueado_ate", 0))
        return max(0, int(ate - time.time() + 0.999))
    except (OSError, ValueError, json.JSONDecodeError, AttributeError):
        return 0

def registrar_tentativa(chave: str, sucesso: bool) -> None:
    if _qg_cloud_enabled():
        chave = str(chave)
        with _qg_pg_connection() as conn:
            # Serializa tentativas da mesma conta, inclusive se mais de uma sessão tentar ao mesmo tempo.
            conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (chave,))
            row = conn.execute(
                "SELECT payload FROM qg_login_attempts WHERE attempt_key = %s FOR UPDATE", (chave,)
            ).fetchone()
            item = _qg_json_or_default(row[0], {}) if row else {}
            agora = time.time()
            if sucesso:
                conn.execute("DELETE FROM qg_login_attempts WHERE attempt_key = %s", (chave,))
                return
            tentativas = [float(t) for t in item.get("tentativas", []) if agora - float(t) < 600]
            tentativas.append(agora)
            proximo = {"tentativas": tentativas, "bloqueado_ate": agora + 90 if len(tentativas) >= 5 else 0}
            if proximo["bloqueado_ate"]:
                proximo["tentativas"] = []
            conn.execute("""INSERT INTO qg_login_attempts (attempt_key, payload, updated_at)
                VALUES (%s, %s, NOW())
                ON CONFLICT (attempt_key) DO UPDATE
                SET payload = EXCLUDED.payload, updated_at = NOW()""",
                (chave, json.dumps(proximo)))
        return
    try:
        estado = json.loads(_ATTEMPT_FILE.read_text(encoding="utf-8"))
        if not isinstance(estado, dict):
            estado = {}
    except (OSError, json.JSONDecodeError):
        estado = {}
    agora = time.time()
    item = estado.get(str(chave), {})
    if sucesso:
        estado.pop(str(chave), None)
    else:
        tentativas = [float(t) for t in item.get("tentativas", []) if agora - float(t) < 600]
        tentativas.append(agora)
        item = {"tentativas": tentativas, "bloqueado_ate": agora + 90 if len(tentativas) >= 5 else 0}
        if item["bloqueado_ate"]:
            item["tentativas"] = []
        estado[str(chave)] = item
    fd, temp_name = tempfile.mkstemp(prefix="qg-security-", suffix=".tmp", dir=str(_ATTEMPT_FILE.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(estado, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_name, _ATTEMPT_FILE)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)

# =============================================================================
# MEMÓRIA PERSISTENTE E REVISÃO ESPAÇADA
# =============================================================================
DATA_DIR = Path("dados_usuarios")
SCHEMA_VERSION = 3
INTERVALOS_REVISAO = (1, 3, 7, 14, 30)

def _arquivo_usuario(username: str) -> Path:
    user_id = normalizar_usuario(username)
    if not user_id:
        raise ValueError("Identificador de usuário inválido.")
    base = DATA_DIR.resolve()
    path = (DATA_DIR / f"dados_espcex_{user_id}.json").resolve()
    if path.parent != base:
        raise ValueError("Caminho de dados inválido.")
    return path

def _padrao(edital_factory):
    hoje = datetime.date.today()
    return {
        "schema_version": SCHEMA_VERSION, "sessoes": [], "erros": [], "redacoes": [],
        "taf": [], "tarefas_casa": [],
        "efa_ciclo": {"modo": "Em Casa", "inicio_ciclo": str(hoje)},
        "edital": edital_factory(), "simulados": [], "pomodoro_ciclos_totais": 0,
        "data_prova": str(hoje + datetime.timedelta(days=320)), "memorias": [],
    }

def carregar_dados_usuario(username: str, edital_factory) -> dict:
    path = _arquivo_usuario(username)
    dados = _padrao(edital_factory)
    fonte = path
    legado = Path(f"dados_espcex_{normalizar_usuario(username)}.json")
    migrar_para_nuvem = False
    if _qg_cloud_enabled():
        with _qg_pg_connection() as conn:
            row = conn.execute("SELECT payload FROM qg_user_data WHERE username = %s", (normalizar_usuario(username),)).fetchone()
        if row:
            carregados = _qg_json_or_default(row[0], {})
            if isinstance(carregados, dict):
                dados.update(carregados)
        else:
            if not path.exists() and legado.is_file() and legado.resolve().parent == Path.cwd().resolve():
                fonte = legado
            try:
                carregados = json.loads(fonte.read_text(encoding="utf-8"))
                if isinstance(carregados, dict):
                    dados.update(carregados)
                    migrar_para_nuvem = fonte.is_file()
            except (OSError, json.JSONDecodeError):
                pass
    else:
        if not path.exists() and legado.is_file() and legado.resolve().parent == Path.cwd().resolve():
            fonte = legado
        try:
            carregados = json.loads(fonte.read_text(encoding="utf-8"))
            if isinstance(carregados, dict):
                dados.update(carregados)
        except (OSError, json.JSONDecodeError):
            pass
    for chave, valor in _padrao(edital_factory).items():
        dados.setdefault(chave, valor)
    dados["schema_version"] = SCHEMA_VERSION
    if not isinstance(dados.get("memorias"), list):
        dados["memorias"] = []
    for item in dados["memorias"]:
        if isinstance(item, dict):
            item.setdefault("id", uuid.uuid4().hex[:12])
            item.setdefault("materia", "Geral")
            item.setdefault("tipo", "Anotação")
            item.setdefault("tags", [])
            item.setdefault("prioridade", "Média")
            item.setdefault("repeticoes", 0)
            item.setdefault("acertos", 0)
            item.setdefault("falhas", 0)
            item.setdefault("revisao_em", str(datetime.date.today()))
            item.setdefault("fixada", False)
    if migrar_para_nuvem:
        salvar_dados_usuario(username, dados)
    return dados

def salvar_dados_usuario(username: str, dados: dict) -> None:
    path = _arquivo_usuario(username)
    payload = dict(dados)
    payload["schema_version"] = SCHEMA_VERSION
    payload["atualizado_em"] = datetime.datetime.now().isoformat(timespec="seconds")
    if _qg_cloud_enabled():
        with _qg_pg_connection() as conn:
            conn.execute("""INSERT INTO qg_user_data (username, payload, updated_at)
                VALUES (%s, %s, NOW())
                ON CONFLICT (username) DO UPDATE
                SET payload = EXCLUDED.payload, updated_at = NOW()""",
                (normalizar_usuario(username), json.dumps(payload, ensure_ascii=False)))
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f"{path.stem}-", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def _qg_hash_session(token: str) -> str:
    """Armazena somente o hash do identificador de sessão no banco remoto."""
    return hashlib.sha256(str(token).encode("utf-8")).hexdigest()


def _qg_criar_sessao(username: str) -> str:
    """Emite um token aleatório que fica apenas na sessão privada do Streamlit."""
    token = py_secrets.token_urlsafe(32)
    if _qg_cloud_enabled():
        with _qg_pg_connection() as conn:
            conn.execute("""INSERT INTO qg_auth_sessions (session_hash, username, expires_at)
                VALUES (%s, %s, NOW() + INTERVAL '8 hours')""",
                (_qg_hash_session(token), normalizar_usuario(username)))
    return token


def _qg_renovar_sessao(token: str, username: str) -> bool:
    """Renova a sessão ativa por oito horas após uma interação do usuário."""
    if not token:
        return False
    if not _qg_cloud_enabled():
        return True
    with _qg_pg_connection() as conn:
        row = conn.execute("""UPDATE qg_auth_sessions
            SET expires_at = NOW() + INTERVAL '8 hours'
            WHERE session_hash = %s AND username = %s AND revoked_at IS NULL AND expires_at > NOW()
            RETURNING session_hash""",
            (_qg_hash_session(token), normalizar_usuario(username))).fetchone()
    return bool(row)


def _qg_revogar_sessao(token: str) -> None:
    """Revoga a sessão no PostgreSQL durante logout ou expiração por inatividade."""
    if token and _qg_cloud_enabled():
        with _qg_pg_connection() as conn:
            conn.execute("UPDATE qg_auth_sessions SET revoked_at = NOW() WHERE session_hash = %s AND revoked_at IS NULL",
                         (_qg_hash_session(token),))


def _qg_revogar_sessoes_usuario(username: str) -> None:
    """Invalida sessões antigas depois de uma redefinição de senha."""
    if username and _qg_cloud_enabled():
        with _qg_pg_connection() as conn:
            conn.execute("UPDATE qg_auth_sessions SET revoked_at = NOW() WHERE username = %s AND revoked_at IS NULL",
                         (normalizar_usuario(username),))


def _qg_revogar_todas_sessoes() -> None:
    """Ação administrativa para encerrar todas as sessões de cadetes na nuvem."""
    if _qg_cloud_enabled():
        with _qg_pg_connection() as conn:
            conn.execute("UPDATE qg_auth_sessions SET revoked_at = NOW() WHERE revoked_at IS NULL")


def _qg_persistir_sqlite_restaurado() -> None:
    """Publica imediatamente no PostgreSQL um SQLite restaurado pelo administrador."""
    if not _qg_cloud_enabled():
        return
    state = _qg_sqlite_runtime_state()
    state["lock"].acquire()
    try:
        state["ready"] = True
        state["database_url"] = _qg_database_url()
        _qg_upload_sqlite_snapshot(Path("dados_qg.db").resolve(), state)
    finally:
        state["lock"].release()

def listar_memorias(username: str, limite: int = 100, materia: str | None = None,
                    busca: str = "", apenas_revisar: bool = False) -> list[dict]:
    dados = carregar_dados_usuario(username, lambda: [])
    itens = [x for x in dados.get("memorias", []) if isinstance(x, dict)]
    hoje = datetime.date.today()
    if materia and materia != "Todas":
        itens = [x for x in itens if x.get("materia", "Geral") == materia]
    termo = busca.strip().casefold()
    if termo:
        itens = [x for x in itens if termo in (str(x.get("texto", "")) + " " + str(x.get("materia", "")) + " " + " ".join(x.get("tags", []))).casefold()]
    if apenas_revisar:
        itens = [x for x in itens if _data(x.get("revisao_em")) <= hoje]
    itens.sort(key=lambda x: (not bool(x.get("fixada")), _data(x.get("revisao_em")), x.get("criada_em", "")), reverse=False)
    return itens[:max(1, min(int(limite), 500))]

def _data(valor) -> datetime.date:
    try:
        return datetime.date.fromisoformat(str(valor)[:10])
    except (TypeError, ValueError):
        return datetime.date.today()

def adicionar_memoria(username: str, texto: str, materia: str = "Geral", tipo: str = "Anotação",
                      tags: list[str] | None = None, prioridade: str = "Média") -> str:
    texto = str(texto).strip()
    if not texto or len(texto) > 3000:
        raise ValueError("A anotação deve ter entre 1 e 3000 caracteres.")
    materia = str(materia).strip()[:80] or "Geral"
    tags_limpas = list(dict.fromkeys(str(x).strip().lstrip("#")[:32] for x in (tags or []) if str(x).strip()))[:12]
    dados = carregar_dados_usuario(username, lambda: [])
    memoria_id = uuid.uuid4().hex[:12]
    dados.setdefault("memorias", []).append({
        "id": memoria_id, "texto": texto, "materia": materia, "tipo": str(tipo)[:40],
        "tags": tags_limpas, "prioridade": prioridade if prioridade in {"Baixa", "Média", "Alta"} else "Média",
        "criada_em": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "revisao_em": str(datetime.date.today()), "ultima_revisao": "", "repeticoes": 0,
        "acertos": 0, "falhas": 0, "fixada": False,
    })
    salvar_dados_usuario(username, dados)
    return memoria_id

def registrar_revisao(username: str, memoria_id: str, lembrou: bool) -> None:
    dados = carregar_dados_usuario(username, lambda: [])
    hoje = datetime.date.today()
    for item in dados.get("memorias", []):
        if isinstance(item, dict) and item.get("id") == memoria_id:
            item["ultima_revisao"] = str(hoje)
            if lembrou:
                item["acertos"] = int(item.get("acertos", 0)) + 1
                item["repeticoes"] = min(int(item.get("repeticoes", 0)) + 1, len(INTERVALOS_REVISAO))
                intervalo = INTERVALOS_REVISAO[item["repeticoes"] - 1]
            else:
                item["falhas"] = int(item.get("falhas", 0)) + 1
                item["repeticoes"] = 0
                intervalo = 1
            item["revisao_em"] = str(hoje + datetime.timedelta(days=intervalo))
            break
    salvar_dados_usuario(username, dados)

def alternar_memoria_fixada(username: str, memoria_id: str) -> None:
    dados = carregar_dados_usuario(username, lambda: [])
    for item in dados.get("memorias", []):
        if isinstance(item, dict) and item.get("id") == memoria_id:
            item["fixada"] = not bool(item.get("fixada", False))
            break
    salvar_dados_usuario(username, dados)

def remover_memoria(username: str, memoria_id: str) -> None:
    dados = carregar_dados_usuario(username, lambda: [])
    dados["memorias"] = [x for x in dados.get("memorias", []) if not isinstance(x, dict) or x.get("id") != memoria_id]
    salvar_dados_usuario(username, dados)

def estatisticas_memoria(username: str) -> dict:
    dados = carregar_dados_usuario(username, lambda: [])
    itens = [x for x in dados.get("memorias", []) if isinstance(x, dict)]
    hoje = datetime.date.today()
    acertos = sum(int(x.get("acertos", 0)) for x in itens)
    falhas = sum(int(x.get("falhas", 0)) for x in itens)
    revisoes = acertos + falhas
    return {
        "total": len(itens),
        "para_revisar": sum(_data(x.get("revisao_em")) <= hoje for x in itens),
        "fixadas": sum(bool(x.get("fixada")) for x in itens),
        "taxa_acerto": round(acertos / revisoes * 100, 1) if revisoes else 0.0,
        "por_materia": {m: sum(x.get("materia", "Geral") == m for x in itens) for m in sorted({x.get("materia", "Geral") for x in itens})},
    }

# =============================================================================
# SETUP PLOTLY & PREFS
# =============================================================================
pio.templates["qg_enterprise"] = go.layout.Template(layout=go.Layout(
    paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
    font={"family": "Inter, Manrope, sans-serif", "color": "#dbe9f7", "size": 12},
    title={"font": {"size": 18, "color": "#f1f7ff"}, "x": 0.02, "xanchor": "left"},
    margin={"t": 54, "r": 24, "b": 44, "l": 48},
    hoverlabel={"bgcolor": "#0b1725", "bordercolor": "rgba(99,229,220,.4)", "font": {"color": "#eef5ff", "family": "Inter, sans-serif"}},
    colorway=["#59e1d2", "#5d91ff", "#a88cff", "#f3bf62", "#ff7c9c", "#73d6a5"],
    xaxis={"gridcolor": "rgba(141,181,212,.10)", "zerolinecolor": "rgba(141,181,212,.18)", "linecolor": "rgba(141,181,212,.18)", "tickfont": {"color": "#9aacc0"}},
    yaxis={"gridcolor": "rgba(141,181,212,.10)", "zerolinecolor": "rgba(141,181,212,.18)", "linecolor": "rgba(141,181,212,.18)", "tickfont": {"color": "#9aacc0"}},
    legend={"bgcolor": "rgba(0,0,0,0)", "font": {"color": "#b8c9d9"}},
))
pio.templates.default = "qg_enterprise"

def verificar_bloqueio_manutencao():
    try:
        conn = sqlite3.connect("dados_qg.db")
        c = conn.cursor()
        c.execute("SELECT valor FROM config_qg WHERE chave = 'modo_manutencao'")
        res = c.fetchone()
        conn.close()
        status_manutencao = res[0] if res else "Desativado"
        if status_manutencao == "Ativado":
            if not st.session_state.get("admin_autenticado", False):
                st.error("🚨 **SISTEMA EM MANUTENÇÃO PROGRAMADA**")
                st.warning("""
                O QG EsPCEx está passando por atualizações e otimizações de segurança.
                Apenas o Administrador possui permissão para acessar o sistema no momento.
                """)
                st.stop()
    except Exception:
        pass

def registrar_acesso_usuario(nome_usuario):
    try:
        conn = sqlite3.connect("dados_qg.db")
        c = conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS usuarios_qg (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nome_usuario TEXT UNIQUE,
                data_cadastro TEXT,
                ultimo_acesso TEXT
            )
        """)
        agora = datetime.datetime.now().strftime("%d/%m/%Y %H:%M:%S")
        c.execute("SELECT id FROM usuarios_qg WHERE nome_usuario = ?", (nome_usuario,))
        row = c.fetchone()
        if row:
            c.execute("UPDATE usuarios_qg SET ultimo_acesso = ? WHERE id = ?", (agora, row[0]))
        else:
            c.execute("INSERT INTO usuarios_qg (nome_usuario, data_cadastro, ultimo_acesso) VALUES (?, ?, ?)", (nome_usuario, agora, agora))
        conn.commit()
        conn.close()
    except Exception:
        pass

# =============================================================================
# CONFIGURAÇÃO DE PÁGINA
# =============================================================================
st.set_page_config(
    page_title="QG EsPCEx - Preparação de Cadetes",
    page_icon="⚔️",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Verifica a conexão antes de autenticar ou ler dados: com Secrets configurado, o app nunca cai silenciosamente no armazenamento temporário.
_qg_url_inicial = _qg_database_url()
if _qg_url_inicial:
    try:
        _qg_initialize_cloud_storage(_qg_url_inicial)
    except Exception:
        st.error("Não foi possível conectar ao banco persistente. Confira QG_DATABASE_URL em Secrets e a disponibilidade do PostgreSQL.")
        st.stop()

verificar_bloqueio_manutencao()

# Tema 3D global: CSS é aplicado pela página; o JS só adiciona inclinação leve aos cartões.
st.markdown("""<style>
:root {
  color-scheme: dark;
  --qg-bg: #07101b;
  --qg-panel: rgba(12, 27, 43, .88);
  --qg-panel-raised: rgba(18, 39, 59, .94);
  --qg-line: rgba(143, 190, 220, .20);
  --qg-text: #eff7ff;
  --qg-muted: #9db2c7;
  --qg-cyan: #63e5dc;
  --qg-blue: #78a5ff;
  --qg-violet: #b09aff;
  --qg-gold: #f3bf62;
  --qg-shadow: 0 22px 55px rgba(0, 0, 0, .38);
}

* { box-sizing: border-box; }
.stApp {
  color: var(--qg-text);
  background:
    radial-gradient(ellipse at 12% 0%, rgba(27, 112, 139, .18), transparent 36%),
    radial-gradient(ellipse at 88% 13%, rgba(73, 80, 163, .20), transparent 35%),
    linear-gradient(145deg, #050b14 0%, #081523 46%, #0c1022 100%) !important;
  font-family: Inter, Manrope, ui-sans-serif, system-ui, sans-serif;
  isolation: isolate;
  overflow-x: clip;
  overflow-y: visible;
}
.stApp::before, .stApp::after {
  content: "";
  position: fixed;
  inset: -35%;
  z-index: -1;
  pointer-events: none;
  border-radius: 50%;
  filter: blur(2px);
}
.stApp::before {
  background: radial-gradient(ellipse at 30% 45%, rgba(42, 175, 184, .10), transparent 46%),
              radial-gradient(ellipse at 68% 58%, rgba(97, 111, 227, .12), transparent 43%);
  animation: qgAurora 34s ease-in-out infinite alternate;
}
.stApp::after {
  inset: auto -15% -48%;
  height: 72vh;
  background: radial-gradient(ellipse, rgba(62, 108, 188, .14), transparent 68%);
  animation: qgDrift 17s ease-in-out infinite alternate;
}
@keyframes qgAurora {
  0% { transform: rotate(-8deg) scale(.94) translate3d(-1%, 0, 0); }
  100% { transform: rotate(8deg) scale(1.10) translate3d(2%, 1%, 0); }
}
@keyframes qgDrift {
  from { transform: translate3d(-3%, 0, 0); opacity: .55; }
  to { transform: translate3d(3%, -5%, 0); opacity: 1; }
}

[data-testid="stAppViewContainer"] { background: transparent; }
/* Mantém a área principal do Streamlit como superfície de rolagem vertical da página. */
[data-testid="stMain"] {
  overflow-x:hidden !important;
  overflow-y:auto !important;
  min-height:0;
  scroll-behavior:smooth;
  -webkit-overflow-scrolling:touch;
}
.main .block-container { max-width: 1680px; padding-top: 2rem; padding-bottom: 4rem; }
[data-testid="stHeader"] { background: rgba(5, 11, 20, .58); backdrop-filter: blur(16px); }
[data-testid="stSidebar"] {
  background: linear-gradient(180deg, rgba(8, 19, 32, .97), rgba(5, 10, 19, .98));
  border-right: 1px solid rgba(99, 229, 220, .16);
  box-shadow: 18px 0 55px rgba(0, 0, 0, .32);
}
[data-testid="stSidebar"] > div:first-child { background: transparent; }
[data-testid="stSidebar"] h1, [data-testid="stSidebar"] h2, [data-testid="stSidebar"] h3 { color: #eaf7ff; }
[data-testid="stSidebar"] [data-testid="stMetric"] { margin: .35rem 0; }

/* Superfícies em vidro e profundidade */
[data-testid="stVerticalBlockBorderWrapper"] > div,
[data-testid="stExpander"],
[data-testid="stForm"],
[data-testid="stTabs"] {
  border: 1px solid var(--qg-line) !important;
  border-radius: 19px !important;
  background: linear-gradient(145deg, rgba(17, 39, 59, .70), rgba(8, 20, 34, .80)) !important;
  box-shadow: 0 14px 36px rgba(0, 0, 0, .22), inset 0 1px rgba(255, 255, 255, .035);
  backdrop-filter: blur(14px);
  -webkit-backdrop-filter: blur(14px);
  transform-style: preserve-3d;
  transition: transform .28s ease, box-shadow .28s ease, border-color .28s ease;
}
[data-testid="stVerticalBlockBorderWrapper"] > div:hover,
[data-testid="stExpander"]:hover,
[data-testid="stForm"]:hover {
  transform: perspective(1200px) translateY(-2px) rotateX(.35deg);
  border-color: rgba(99, 229, 220, .34) !important;
  box-shadow: 0 23px 48px rgba(0, 0, 0, .30), 0 0 26px rgba(99, 229, 220, .055);
}
[data-testid="stTabs"] { padding: 13px 16px 17px; }
[data-testid="stTabs"] [role="tablist"] {
  gap: 8px;
  border-bottom: 1px solid rgba(143, 190, 220, .13);
}
[data-testid="stTabs"] [role="tab"] {
  border-radius: 11px 11px 6px 6px;
  transition: transform .2s ease, background .2s ease, color .2s ease;
}
[data-testid="stTabs"] [role="tab"]:hover { transform: translateY(-2px); background: rgba(99, 229, 220, .08); }
[data-testid="stTabs"] [aria-selected="true"] { color: var(--qg-cyan) !important; }

/* Botões com extrusão 3D */
.stButton > button, .stFormSubmitButton > button, [data-testid="stDownloadButton"] > button {
  min-height: 2.65rem;
  border: 1px solid rgba(99, 229, 220, .30);
  border-radius: 12px;
  color: #ecfbff;
  background: linear-gradient(145deg, #173d52, #10273e 60%, #182444);
  box-shadow: 0 5px 0 #071522, 0 11px 24px rgba(0, 0, 0, .28), inset 0 1px rgba(255, 255, 255, .12);
  transform: perspective(700px) translateZ(0);
  transform-style: preserve-3d;
  transition: transform .18s ease, box-shadow .18s ease, border-color .18s ease, filter .18s ease;
}
.stButton > button:hover, .stFormSubmitButton > button:hover, [data-testid="stDownloadButton"] > button:hover {
  transform: perspective(700px) translateY(-3px) rotateX(3deg);
  border-color: rgba(99, 229, 220, .68);
  filter: brightness(1.12);
  box-shadow: 0 8px 0 #071522, 0 17px 30px rgba(14, 151, 166, .20), 0 0 20px rgba(99, 229, 220, .12);
}
.stButton > button:active, .stFormSubmitButton > button:active, [data-testid="stDownloadButton"] > button:active {
  transform: perspective(700px) translateY(3px);
  box-shadow: 0 1px 0 #071522, 0 4px 10px rgba(0, 0, 0, .30);
}
.stButton > button[kind="primary"], .stFormSubmitButton > button[kind="primary"] {
  border-color: rgba(123, 241, 227, .78);
  background: linear-gradient(115deg, #117e82, #2369a3 58%, #494b9b);
  box-shadow: 0 5px 0 #084b5b, 0 14px 28px rgba(37, 160, 190, .22), inset 0 1px rgba(255,255,255,.20);
}

/* Campos elevados, foco luminoso e seletores */
[data-baseweb="input"] > div, [data-baseweb="select"] > div, [data-baseweb="textarea"] > div,
textarea, input {
  border-radius: 12px !important;
  border-color: rgba(112, 191, 218, .24) !important;
  background: rgba(5, 16, 27, .82) !important;
  box-shadow: inset 0 3px 9px rgba(0, 0, 0, .34), 0 2px 7px rgba(0, 0, 0, .12) !important;
  transition: border-color .2s ease, box-shadow .2s ease, transform .2s ease;
}
[data-baseweb="input"] > div:focus-within, [data-baseweb="select"] > div:focus-within,
textarea:focus, input:focus {
  border-color: var(--qg-cyan) !important;
  box-shadow: inset 0 2px 5px rgba(0, 0, 0, .30), 0 0 0 2px rgba(99, 229, 220, .12), 0 0 19px rgba(99, 229, 220, .18) !important;
  transform: translateY(-1px);
}

/* Métricas, alertas, progresso e tabelas */
[data-testid="stMetric"] {
  position: relative;
  overflow: hidden;
  padding: 16px 17px;
  border: 1px solid rgba(99, 229, 220, .20);
  border-radius: 17px;
  background: linear-gradient(145deg, rgba(19, 45, 65, .94), rgba(9, 22, 37, .96));
  box-shadow: 0 13px 28px rgba(0, 0, 0, .26), inset 0 1px rgba(255, 255, 255, .05);
  transform-style: preserve-3d;
  transition: transform .24s ease, box-shadow .24s ease, border-color .24s ease;
}
[data-testid="stMetric"]::after {
  content: ""; position: absolute; left: 12%; right: 12%; bottom: 0; height: 1px;
  background: linear-gradient(90deg, transparent, var(--qg-cyan), var(--qg-blue), transparent); opacity: .72;
}
[data-testid="stMetric"]:hover { transform: perspective(900px) translateY(-4px) rotateX(2deg) rotateY(-1deg); border-color: rgba(99,229,220,.48); box-shadow: 0 22px 42px rgba(0,0,0,.34), 0 0 24px rgba(99,229,220,.10); }
[data-testid="stMetricValue"] { color: #f4fbff; font-weight: 800; text-shadow: 0 0 18px rgba(99,229,220,.13); }
[data-testid="stMetricLabel"] { color: var(--qg-muted); }
[data-testid="stAlert"] { border-radius: 14px; box-shadow: 0 12px 26px rgba(0,0,0,.17); backdrop-filter: blur(12px); }
[data-testid="stProgress"] > div > div { border-radius: 999px; background: linear-gradient(90deg, var(--qg-cyan), var(--qg-blue), var(--qg-violet)); box-shadow: 0 0 18px rgba(99,229,220,.25); }
[data-testid="stDataFrame"], [data-testid="stTable"] { border: 1px solid rgba(143,190,220,.16); border-radius: 14px; overflow: hidden; box-shadow: 0 15px 32px rgba(0,0,0,.22); }

/* Cartões de rotina */
.routine-card {
  position: relative; height: 100%; min-height: 178px; overflow: hidden; padding: 21px;
  border: 1px solid rgba(99,229,220,.23); border-radius: 17px;
  background: linear-gradient(145deg, rgba(18,43,65,.94), rgba(7,19,33,.98));
  box-shadow: 9px 12px 25px rgba(0,0,0,.34), inset 1px 1px rgba(255,255,255,.07), inset -1px -1px rgba(0,0,0,.24);
  transform: perspective(900px) rotateX(1deg); transform-style: preserve-3d;
  transition: transform .3s ease, box-shadow .3s ease, border-color .3s ease;
}
.routine-card::after { content:""; position:absolute; width:180px; height:180px; right:-85px; top:-105px; border-radius:50%; background:rgba(99,229,220,.12); filter:blur(4px); }
.routine-card:hover { transform: perspective(900px) translateY(-6px) rotateX(3deg) rotateY(-2deg); border-color:rgba(99,229,220,.52); box-shadow:14px 22px 38px rgba(0,0,0,.38),0 0 25px rgba(99,229,220,.11); }
.routine-card h4 { position:relative; color:var(--qg-cyan); margin-top:0; font-weight:800; text-shadow:0 0 15px rgba(99,229,220,.25); }
.routine-card b { position:relative; color:#f1f7ff; }

/* Cabeçalhos 3D e cubo */
.qg-hero {
  position:relative; display:flex; align-items:center; gap:24px; overflow:hidden;
  margin:0 0 25px; padding:25px 30px; min-height:150px;
  border:1px solid rgba(99,229,220,.28); border-radius:23px;
  background:radial-gradient(circle at 86% 12%,rgba(94,113,241,.24),transparent 34%),linear-gradient(115deg,rgba(10,31,49,.98),rgba(19,39,69,.94) 60%,rgba(31,30,61,.95));
  box-shadow:0 23px 54px rgba(0,0,0,.39),inset 0 1px rgba(255,255,255,.10);
  transform:perspective(1200px) rotateX(.7deg); transform-style:preserve-3d;
}
.qg-hero::after { content:""; position:absolute; left:7%; right:7%; bottom:0; height:2px; background:linear-gradient(90deg,transparent,var(--qg-cyan),var(--qg-blue),var(--qg-violet),transparent); animation:qgGlow 2.8s ease-in-out infinite alternate; }
@keyframes qgGlow { from { opacity:.48; filter:drop-shadow(0 0 2px var(--qg-cyan)); } to { opacity:1; filter:drop-shadow(0 0 10px var(--qg-cyan)); } }
.qg-hero-copy { min-width:0; flex:1; }
.qg-hero-kicker { color:var(--qg-cyan); font:700 .7rem ui-monospace,monospace; letter-spacing:.17em; text-transform:uppercase; }
.qg-hero-title { margin:.35rem 0; color:#f5f9ff; font-size:clamp(1.35rem,3vw,2rem); font-weight:850; letter-spacing:-.04em; text-shadow:0 8px 25px rgba(0,0,0,.45); }
.qg-hero-subtitle { color:#a9bdd0; font-size:.9rem; line-height:1.5; }
.qg-cube-wrap { width:66px; height:66px; flex:none; perspective:600px; }
.qg-cube { position:relative; width:100%; height:100%; transform-style:preserve-3d; animation:qgCube 14s linear infinite; }
.qg-cube-face { position:absolute; inset:0; display:grid; place-items:center; border:1px solid rgba(99,229,220,.78); border-radius:11px; color:var(--qg-cyan); background:rgba(10,35,52,.74); box-shadow:inset 0 0 18px rgba(99,229,220,.13),0 0 18px rgba(99,229,220,.15); font-size:1.2rem; font-weight:850; backface-visibility:hidden; }
.qg-cube-front { transform:translateZ(33px); }
.qg-cube-back { transform:rotateY(180deg) translateZ(33px); }
.qg-cube-right { transform:rotateY(90deg) translateZ(33px); }
.qg-cube-left { transform:rotateY(-90deg) translateZ(33px); }
.qg-cube-top { transform:rotateX(90deg) translateZ(33px); }
.qg-cube-bottom { transform:rotateX(-90deg) translateZ(33px); }
@keyframes qgCube { to { transform:rotateX(360deg) rotateY(360deg); } }
.qg-live { flex:none; padding:8px 11px; border:1px solid rgba(99,229,220,.23); border-radius:999px; color:#c9f5ee; background:rgba(23,93,89,.20); font:700 .62rem ui-monospace,monospace; letter-spacing:.12em; }
.qg-live::before { content:""; display:inline-block; width:7px; height:7px; margin-right:7px; border-radius:50%; background:var(--qg-cyan); box-shadow:0 0 11px var(--qg-cyan); animation:qgPulse 1.7s ease-in-out infinite; }
@keyframes qgPulse { 50% { opacity:.48; transform:scale(.75); } }

/* Acesso: palco holográfico e cubo 3D com mais presença */
.qg-hero-auth {
  isolation:isolate; min-height:238px; padding:34px 38px; gap:30px;
  border-color:rgba(106,246,231,.58);
  background:radial-gradient(ellipse at 17% 8%,rgba(67,230,218,.22),transparent 42%),radial-gradient(ellipse at 90% 50%,rgba(126,101,255,.27),transparent 41%),linear-gradient(118deg,rgba(6,23,39,.99),rgba(12,42,67,.98) 50%,rgba(27,23,59,.99));
  box-shadow:0 32px 82px rgba(0,0,0,.56),inset 0 2px rgba(255,255,255,.16),inset 0 -3px rgba(0,0,0,.33),0 0 48px rgba(73,221,223,.13);
  animation:qgAuthHover 6.5s ease-in-out infinite;
}
.qg-hero-auth::before {
  opacity:.48;
  background-image:radial-gradient(circle at var(--qg-light-x,82%) var(--qg-light-y,48%),rgba(100,245,230,.22),transparent 34%),linear-gradient(rgba(132,202,229,.10) 1px,transparent 1px),linear-gradient(90deg,rgba(132,202,229,.10) 1px,transparent 1px);
  background-size:auto,30px 30px,30px 30px;
  mask-image:linear-gradient(112deg,black 4%,rgba(0,0,0,.75) 50%,transparent 92%);
}
.qg-hero-auth::after { height:3px; left:4%; right:4%; box-shadow:0 0 18px rgba(99,229,220,.7); }
.qg-hero-auth .qg-cube-wrap { position:relative; z-index:2; width:94px; height:94px; perspective:1000px; filter:drop-shadow(0 0 22px rgba(67,238,226,.48)); }
.qg-hero-auth .qg-cube { animation:qgAuthCube 8s linear infinite; }
.qg-hero-auth .qg-cube-face {
  border:1.5px solid rgba(129,255,240,.92); border-radius:14px;
  color:#c6fff8; background:linear-gradient(145deg,rgba(20,102,117,.84),rgba(22,35,75,.91));
  box-shadow:inset 0 0 28px rgba(87,242,229,.28),0 0 26px rgba(68,226,231,.30),0 0 54px rgba(76,153,255,.12);
  font-size:1.5rem; text-shadow:0 0 14px rgba(99,229,220,.7);
}
.qg-hero-auth .qg-cube-front { transform:translateZ(47px); }
.qg-hero-auth .qg-cube-back { transform:rotateY(180deg) translateZ(47px); }
.qg-hero-auth .qg-cube-right { transform:rotateY(90deg) translateZ(47px); }
.qg-hero-auth .qg-cube-left { transform:rotateY(-90deg) translateZ(47px); }
.qg-hero-auth .qg-cube-top { transform:rotateX(90deg) translateZ(47px); }
.qg-hero-auth .qg-cube-bottom { transform:rotateX(-90deg) translateZ(47px); }
.qg-hero-auth .qg-hero-copy { position:relative; z-index:2; transform:translateZ(28px); }
.qg-hero-auth .qg-hero-kicker { text-shadow:0 0 16px rgba(99,229,220,.35); }
.qg-hero-auth .qg-hero-title {
  font-size:clamp(1.8rem,4vw,2.8rem); letter-spacing:-.045em;
  text-shadow:0 1px 0 #4b8990,0 3px 0 #23516a,0 6px 0 #132c49,0 12px 28px rgba(0,0,0,.62),0 0 34px rgba(99,229,220,.30);
}
.qg-hero-auth .qg-hero-subtitle { max-width:520px; font-size:1rem; color:#c1d2e3; text-shadow:0 3px 12px rgba(0,0,0,.5); }
.qg-hero-auth .qg-live { position:relative; z-index:2; padding:10px 15px; box-shadow:0 8px 24px rgba(0,0,0,.25),inset 0 1px rgba(255,255,255,.13),0 0 20px rgba(99,229,220,.10); }
.qg-auth-orbits {
  position:absolute; z-index:1; top:50%; right:8%; width:250px; height:250px; pointer-events:none;
  border:1px solid rgba(99,229,220,.23); border-radius:50%;
  box-shadow:0 0 0 18px rgba(99,229,220,.025),0 0 0 39px rgba(120,165,255,.035),inset 0 0 35px rgba(99,229,220,.06);
  transform:translate3d(var(--qg-orbit-x,0px),calc(-50% + var(--qg-orbit-y,0px)),0) rotateX(66deg) rotateZ(-24deg);
  animation:qgOrbitSpin 24s linear infinite;
}
.qg-auth-orbits::before,.qg-auth-orbits::after,.qg-auth-orbits i {
  content:""; position:absolute; inset:17px; border:1px solid rgba(120,165,255,.30); border-radius:50%;
}
.qg-auth-orbits::before { transform:rotateY(55deg) rotateZ(30deg); border-color:rgba(99,229,220,.32); }
.qg-auth-orbits::after { inset:37px; transform:rotateY(-58deg) rotateZ(-31deg); border-color:rgba(176,154,255,.33); }
.qg-auth-orbits i:nth-child(1) { inset:0; transform:rotateY(74deg); border-style:dashed; border-color:rgba(99,229,220,.28); }
.qg-auth-orbits i:nth-child(2) { inset:27px; transform:rotateX(56deg) rotateZ(40deg); border-color:rgba(120,165,255,.30); }
.qg-auth-orbits i:nth-child(3) { inset:58px; transform:rotateY(52deg) rotateX(-28deg); border-color:rgba(176,154,255,.34); }
.qg-auth-orbits span { position:absolute; top:13%; left:22%; width:8px; height:8px; border-radius:50%; background:#8bfff0; box-shadow:0 0 15px 4px rgba(99,229,220,.58); }
@keyframes qgAuthCube { 0% { transform:rotateX(-18deg) rotateY(0) rotateZ(0); } 100% { transform:rotateX(342deg) rotateY(360deg) rotateZ(360deg); } }
@keyframes qgOrbitSpin { from { rotate:0deg; } to { rotate:360deg; } }
@keyframes qgAuthHover { 0%,100% { translate:0 0; } 50% { translate:0 -4px; } }
.qg-hero-auth + div { position:relative; z-index:1; }

/* Cartões da tela de inicialização: apresentam áreas do QG sem depender de login ou dados do usuário. */
.qg-start-overview { position:relative; margin:0 0 19px; padding:20px; overflow:hidden; border:1px solid rgba(105,220,225,.24); border-radius:20px; background:radial-gradient(ellipse at 8% 0%,rgba(72,205,202,.11),transparent 42%),linear-gradient(145deg,rgba(11,27,44,.95),rgba(9,17,34,.97) 58%,rgba(22,19,47,.94)); box-shadow:0 20px 46px rgba(0,0,0,.30),inset 0 1px rgba(255,255,255,.08); }
.qg-start-overview::before { content:""; position:absolute; inset:0; pointer-events:none; opacity:.26; background-image:linear-gradient(rgba(132,202,229,.10) 1px,transparent 1px),linear-gradient(90deg,rgba(132,202,229,.10) 1px,transparent 1px); background-size:28px 28px; mask-image:linear-gradient(110deg,black,transparent 78%); }
.qg-start-heading { position:relative; z-index:1; margin:0 0 14px; }
.qg-start-kicker { color:#75e5df; font:700 .65rem ui-monospace,monospace; letter-spacing:.17em; text-transform:uppercase; }
.qg-start-heading h2 { margin:5px 0 4px; color:#f2f7ff; font-size:1.08rem; font-weight:850; letter-spacing:-.025em; }
.qg-start-heading p { margin:0; color:#9fb4c9; font-size:.79rem; line-height:1.55; }
.qg-start-grid { position:relative; z-index:1; display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:12px; perspective:1100px; }
.qg-start-card { position:relative; min-height:142px; overflow:hidden; padding:15px; border:1px solid rgba(118,191,214,.21); border-radius:15px; background:linear-gradient(145deg,rgba(21,49,70,.92),rgba(9,22,39,.97)); box-shadow:7px 10px 18px rgba(0,0,0,.28),inset 1px 1px rgba(255,255,255,.08),inset -1px -1px rgba(0,0,0,.25); transform:perspective(900px) rotateX(2deg) translateZ(0); transform-style:preserve-3d; transition:transform .24s ease,border-color .24s ease,box-shadow .24s ease; }
.qg-start-card::before { content:""; position:absolute; inset:0; pointer-events:none; background:linear-gradient(118deg,transparent 18%,rgba(255,255,255,.09) 39%,transparent 59%); transform:translateX(-125%); transition:transform .75s ease; }
.qg-start-card:hover { border-color:rgba(99,229,220,.53); box-shadow:12px 17px 27px rgba(0,0,0,.38),0 0 23px rgba(99,229,220,.11),inset 1px 1px rgba(255,255,255,.11); }
.qg-start-card:hover::before { transform:translateX(125%); }
.qg-start-card-top { display:flex; align-items:center; justify-content:space-between; gap:8px; margin-bottom:10px; }
.qg-start-icon { display:grid; place-items:center; width:34px; height:34px; border:1px solid rgba(99,229,220,.32); border-radius:11px; color:#b8fff4; background:linear-gradient(145deg,rgba(31,137,145,.54),rgba(43,53,117,.72)); box-shadow:0 5px 0 rgba(3,12,24,.72),0 0 18px rgba(99,229,220,.12),inset 0 1px rgba(255,255,255,.24); font-size:1rem; transform:translateZ(17px) rotate(-4deg); }
.qg-start-index { color:#66849c; font:700 .58rem ui-monospace,monospace; letter-spacing:.12em; }
.qg-start-card h3 { position:relative; margin:0 0 5px; color:#edf7ff; font-size:.79rem; font-weight:800; transform:translateZ(10px); }
.qg-start-card p { position:relative; margin:0; color:#9db1c5; font-size:.68rem; line-height:1.5; transform:translateZ(6px); }
.qg-start-foot { position:relative; z-index:1; display:flex; align-items:flex-start; gap:9px; margin-top:13px; padding:10px 12px; border:1px solid rgba(99,229,220,.15); border-radius:12px; color:#aec4d6; background:rgba(7,19,32,.62); font-size:.69rem; line-height:1.5; }
.qg-start-foot strong { flex:none; color:#a7f7ee; }

/* Em telas estreitas, os cartões passam para duas e depois uma coluna; todos permanecem visíveis. */
@media (max-width:900px) { .qg-start-grid { grid-template-columns:repeat(2,minmax(0,1fr)); } }
@media (max-width:700px) {
  .qg-hero-auth { flex-wrap:wrap; align-items:center; }
  .qg-hero-auth .qg-hero-copy { flex:1 1 58%; min-width:190px; }
  .qg-hero-auth .qg-live { display:inline-flex; margin-left:auto; padding:8px 10px; font-size:.55rem; }
  .qg-start-overview { padding:15px; }
  .qg-start-grid { gap:9px; }
  .qg-start-card { min-height:132px; padding:12px; }
}
@media (max-width:460px) {
  .qg-hero-auth .qg-hero-copy { flex-basis:100%; min-width:0; }
  .qg-hero-auth .qg-live { margin-left:75px; }
  .qg-start-grid { grid-template-columns:minmax(0,1fr); }
  .qg-start-card { min-height:unset; }
}
@media (prefers-reduced-motion:reduce) { .qg-start-card,.qg-start-card::before { transition:none !important; } }

@media (max-width:700px) {
  .qg-hero-auth { min-height:178px; padding:22px 18px; gap:17px; }
  .qg-hero-auth .qg-cube-wrap { width:58px; height:58px; }
  .qg-hero-auth .qg-cube-front { transform:translateZ(29px); }
  .qg-hero-auth .qg-cube-back { transform:rotateY(180deg) translateZ(29px); }
  .qg-hero-auth .qg-cube-right { transform:rotateY(90deg) translateZ(29px); }
  .qg-hero-auth .qg-cube-left { transform:rotateY(-90deg) translateZ(29px); }
  .qg-hero-auth .qg-cube-top { transform:rotateX(90deg) translateZ(29px); }
  .qg-hero-auth .qg-cube-bottom { transform:rotateX(-90deg) translateZ(29px); }
  .qg-auth-orbits { right:-26px; width:170px; height:170px; opacity:.72; }
}
@media (prefers-reduced-motion:reduce) { .qg-hero-auth,.qg-auth-orbits { animation:none !important; } }

hr { border-color:rgba(143,190,220,.15) !important; }
.stMarkdown h1, .stMarkdown h2, .stMarkdown h3, .stMarkdown h4 { color:#eef7ff; text-shadow:0 5px 18px rgba(0,0,0,.28); }
.stCaption, [data-testid="stCaptionContainer"] { color:#9db2c7; }
[data-testid="stRadio"] label, [data-testid="stCheckbox"] label { transition:transform .18s ease, color .18s ease; }
[data-testid="stRadio"] label:hover, [data-testid="stCheckbox"] label:hover { color:#dcfffb; transform:translateX(2px); }
[data-testid="stSidebar"] [data-testid="stRadio"] label:hover { background:rgba(99,229,220,.08); border-radius:10px; }
.qg-sidebar-brand { display:flex; align-items:center; gap:10px; margin:2px 0 12px; padding:2px 1px 13px; border-bottom:1px solid rgba(126,185,211,.13); }
.qg-sidebar-brand-mark { position:relative; display:grid; place-items:center; width:43px; height:43px; flex:none; border:1px solid rgba(99,229,220,.72); border-radius:13px; color:#d8fffb; background:linear-gradient(145deg,#177c85,#193f62 58%,#403b81); box-shadow:5px 6px 0 rgba(2,9,17,.76),0 0 23px rgba(99,229,220,.24),inset 0 1px rgba(255,255,255,.35); transform:perspective(400px) rotateY(-12deg) rotateX(6deg); font-weight:900; letter-spacing:.04em; }
.qg-sidebar-brand-copy { min-width:0; }
.qg-sidebar-brand-copy strong { display:block; color:#f2f8ff; font-size:.82rem; letter-spacing:.09em; }
.qg-sidebar-brand-copy span { display:block; margin-top:3px; color:#7f9ab0; font:600 .56rem ui-monospace,monospace; letter-spacing:.14em; text-transform:uppercase; }
.qg-sidebar-profile { position:relative; display:flex; align-items:center; gap:10px; margin:0 0 9px; padding:10px; overflow:hidden; border:1px solid rgba(99,229,220,.26); border-radius:15px; background:radial-gradient(ellipse at 100% 0,rgba(120,165,255,.17),transparent 48%),linear-gradient(145deg,rgba(18,49,69,.94),rgba(8,22,37,.97)); box-shadow:0 10px 22px rgba(0,0,0,.28),inset 0 1px rgba(255,255,255,.09),inset 0 -2px rgba(0,0,0,.26); transform:perspective(800px) rotateY(-2deg); transform-style:preserve-3d; transition:transform .24s ease,box-shadow .24s ease,border-color .24s ease; }
.qg-sidebar-profile:hover { border-color:rgba(99,229,220,.58); box-shadow:0 15px 29px rgba(0,0,0,.36),0 0 20px rgba(99,229,220,.11),inset 0 1px rgba(255,255,255,.12); }
.qg-sidebar-avatar { display:grid; place-items:center; width:38px; height:38px; flex:none; border:1px solid rgba(197,255,248,.75); border-radius:12px; color:#edfffc; background:linear-gradient(145deg,#45d8c8,#4d84cd 58%,#8c70d4); box-shadow:0 5px 0 #0a2434,0 8px 17px rgba(54,180,203,.28),inset 0 1px rgba(255,255,255,.42); transform:translateZ(14px); font-size:1.15rem; font-weight:850; }
.qg-sidebar-profile-copy { min-width:0; flex:1; }
.qg-sidebar-profile-copy small { display:block; color:#77ded7; font:700 .55rem ui-monospace,monospace; letter-spacing:.12em; text-transform:uppercase; }
.qg-sidebar-profile-copy strong { display:block; overflow:hidden; margin-top:3px; color:#f2f7ff; font-size:.78rem; text-overflow:ellipsis; white-space:nowrap; }
.qg-sidebar-profile-copy span { display:block; overflow:hidden; margin-top:2px; color:#8da5ba; font-size:.63rem; text-overflow:ellipsis; white-space:nowrap; }
.qg-sidebar-online { width:7px; height:7px; flex:none; border-radius:50%; background:#62e5bd; box-shadow:0 0 12px #62e5bd; }
.qg-sidebar-action-note { margin:-3px 0 5px; color:#91a9be; font-size:.64rem; line-height:1.45; }
[data-testid="stSidebar"] [data-testid="stRadio"] { max-height:min(54vh,510px); overflow-y:auto; overflow-x:hidden; overscroll-behavior:contain; scrollbar-width:thin; scrollbar-color:rgba(99,229,220,.52) rgba(12,26,40,.5); padding:3px 5px 8px 2px; border:1px solid rgba(99,229,220,.16); border-radius:14px; background:linear-gradient(145deg,rgba(9,25,40,.68),rgba(6,16,28,.75)); box-shadow:inset 0 1px rgba(255,255,255,.035),0 9px 18px rgba(0,0,0,.14); }
[data-testid="stSidebar"] [data-testid="stRadio"]::-webkit-scrollbar { width:8px; }
[data-testid="stSidebar"] [data-testid="stRadio"]::-webkit-scrollbar-track { border-radius:8px; background:rgba(11,25,38,.62); }
[data-testid="stSidebar"] [data-testid="stRadio"]::-webkit-scrollbar-thumb { border:2px solid rgba(11,25,38,.8); border-radius:8px; background:linear-gradient(180deg,#59e1d2,#5d91ff); }
.st-key-qg_open_profile > button { min-height:2.25rem !important; padding:.35rem .6rem !important; font-size:.76rem !important; }
.qg-profile-hero { min-height:172px; }
.qg-profile-orb { display:grid; place-items:center; width:88px; height:88px; flex:none; border:1px solid rgba(194,255,247,.72); border-radius:28px; color:#fff; background:linear-gradient(145deg,#37cdbd,#4e7bc4 57%,#7864cb); box-shadow:0 12px 0 rgba(4,17,29,.78),0 23px 38px rgba(0,0,0,.34),0 0 36px rgba(99,229,220,.26),inset 0 2px rgba(255,255,255,.44); transform:perspective(800px) rotateY(-14deg) rotateX(9deg) translateZ(22px); font-size:2.1rem; }
.qg-profile-hero .qg-hero-copy { transform:translateZ(30px); }
.insight-box {
  padding:16px 18px; border:1px solid rgba(99,229,220,.24); border-left:3px solid var(--qg-cyan);
  border-radius:14px; background:linear-gradient(135deg,rgba(13,45,59,.86),rgba(14,25,47,.91));
  box-shadow:0 15px 32px rgba(0,0,0,.25), inset 0 1px rgba(255,255,255,.05);
}
[data-testid="stToast"] { border:1px solid rgba(99,229,220,.26); border-radius:13px; box-shadow:0 18px 40px rgba(0,0,0,.32); backdrop-filter:blur(15px); }

/* Camada visual 3D reforçada: perspectiva, neon, profundidade e reflexos */
.main .block-container { perspective:1700px; perspective-origin:50% 0; }
[data-testid="stAppViewContainer"] { position:relative; z-index:1; }
.stApp::before {
  background:
    radial-gradient(ellipse at 18% 24%, rgba(34, 218, 204, .16), transparent 36%),
    radial-gradient(ellipse at 82% 22%, rgba(93, 117, 255, .18), transparent 38%),
    radial-gradient(ellipse at 55% 78%, rgba(160, 105, 255, .10), transparent 42%);
  animation:qgAurora 22s ease-in-out infinite alternate;
}
.stApp::after { height:82vh; filter:blur(10px); animation:qgDrift 12s ease-in-out infinite alternate; }
[data-testid="stVerticalBlockBorderWrapper"] > div,
[data-testid="stExpander"], [data-testid="stForm"], [data-testid="stTabs"],
[data-testid="stPlotlyChart"], [data-testid="stDataFrame"] {
  transform-style:preserve-3d;
  transition:transform .32s cubic-bezier(.2,.75,.25,1), box-shadow .32s ease, border-color .32s ease, filter .32s ease;
}
[data-testid="stVerticalBlockBorderWrapper"] > div:hover,
[data-testid="stExpander"]:hover, [data-testid="stForm"]:hover,
[data-testid="stPlotlyChart"]:hover, [data-testid="stDataFrame"]:hover {
  transform:perspective(1100px) translateY(-4px) rotateX(1.25deg);
  border-color:rgba(99,229,220,.45) !important;
  box-shadow:0 25px 52px rgba(0,0,0,.35),0 0 28px rgba(99,229,220,.10),inset 0 1px rgba(255,255,255,.08);
}
[data-testid="stMetric"] {
  background:radial-gradient(ellipse at 18% 0%,rgba(99,229,220,.15),transparent 55%),linear-gradient(145deg,rgba(21,52,76,.97),rgba(8,20,36,.99));
  box-shadow:0 17px 32px rgba(0,0,0,.34),inset 0 2px rgba(255,255,255,.09),inset 0 -2px rgba(0,0,0,.24);
}
[data-testid="stMetricValue"] { background:linear-gradient(100deg,#f5ffff 8%,#7cf5e6 47%,#a8c3ff 90%); -webkit-background-clip:text; background-clip:text; -webkit-text-fill-color:transparent; filter:drop-shadow(0 0 12px rgba(99,229,220,.18)); }
.routine-card {
  background:radial-gradient(ellipse at 90% 0%,rgba(120,165,255,.15),transparent 47%),linear-gradient(145deg,rgba(20,53,77,.98),rgba(6,17,31,.99));
  box-shadow:12px 16px 30px rgba(0,0,0,.43),inset 2px 2px rgba(255,255,255,.09),inset -2px -2px rgba(0,0,0,.30),0 0 22px rgba(99,229,220,.06);
}
.routine-card::before {
  content:""; position:absolute; inset:0; pointer-events:none; border-radius:inherit;
  background:linear-gradient(118deg,transparent 18%,rgba(255,255,255,.075) 37%,transparent 55%);
  transform:translateX(-115%); transition:transform .8s ease;
}
.routine-card:hover::before { transform:translateX(115%); }
.qg-hero {
  min-height:166px; border-color:rgba(99,229,220,.46);
  background:radial-gradient(ellipse at 8% 0%,rgba(99,229,220,.20),transparent 42%),radial-gradient(ellipse at 96% 12%,rgba(127,108,255,.24),transparent 40%),linear-gradient(118deg,rgba(8,29,48,.99),rgba(15,42,72,.97) 55%,rgba(27,25,59,.98));
  box-shadow:0 28px 66px rgba(0,0,0,.48),inset 0 2px rgba(255,255,255,.14),inset 0 -3px rgba(0,0,0,.25),0 0 38px rgba(66,202,220,.09);
  animation:qgHeroFloat 7s ease-in-out infinite;
}
.qg-hero::before {
  content:""; position:absolute; inset:0; pointer-events:none; opacity:.32;
  background-image:linear-gradient(rgba(132,202,229,.10) 1px,transparent 1px),linear-gradient(90deg,rgba(132,202,229,.10) 1px,transparent 1px);
  background-size:34px 34px; mask-image:linear-gradient(115deg,black,transparent 74%);
}
.qg-hero-title { text-shadow:0 0 24px rgba(99,229,220,.23),0 10px 28px rgba(0,0,0,.55); }
.qg-cube-wrap { filter:drop-shadow(0 0 16px rgba(99,229,220,.30)); }
.qg-cube { animation:qgCube 9s linear infinite; }
.qg-cube-face { border-width:1.5px; background:linear-gradient(145deg,rgba(23,87,104,.82),rgba(18,28,62,.88)); box-shadow:inset 0 0 24px rgba(99,229,220,.24),0 0 24px rgba(99,229,220,.22); }
@keyframes qgHeroFloat { 0%,100% { transform:perspective(1200px) rotateX(.7deg) translateY(0); } 50% { transform:perspective(1200px) rotateX(.7deg) translateY(-3px); } }
.stButton > button, .stFormSubmitButton > button, [data-testid="stDownloadButton"] > button {
  position:relative; overflow:hidden; text-shadow:0 1px 8px rgba(255,255,255,.18);
  box-shadow:0 6px 0 #061321,0 14px 28px rgba(0,0,0,.38),inset 0 1px rgba(255,255,255,.20),inset 0 -2px rgba(0,0,0,.18);
}
.stButton > button::before, .stFormSubmitButton > button::before, [data-testid="stDownloadButton"] > button::before {
  content:""; position:absolute; inset:0; pointer-events:none; border-radius:inherit;
  background:linear-gradient(112deg,transparent 20%,rgba(255,255,255,.21) 48%,transparent 68%);
  transform:translateX(-125%); transition:transform .65s ease;
}
.stButton > button:hover::before, .stFormSubmitButton > button:hover::before, [data-testid="stDownloadButton"] > button:hover::before { transform:translateX(125%); }
.stButton > button[kind="primary"], .stFormSubmitButton > button[kind="primary"] {
  background:linear-gradient(112deg,#0fa99e,#2476c2 56%,#704ec4);
  box-shadow:0 6px 0 #07515e,0 17px 34px rgba(50,166,208,.30),inset 0 1px rgba(255,255,255,.28),inset 0 -2px rgba(0,0,0,.16);
}
[data-testid="stSidebar"] { background:radial-gradient(ellipse at 0% 0%,rgba(26,129,143,.16),transparent 42%),linear-gradient(180deg,rgba(7,19,32,.98),rgba(4,8,17,.99)); box-shadow:22px 0 70px rgba(0,0,0,.46),inset -1px 0 rgba(99,229,220,.13); }
[data-testid="stSidebar"] [data-testid="stRadio"] label { padding:5px 8px; border:1px solid transparent; border-radius:11px; transition:all .24s ease; }
[data-testid="stSidebar"] [data-testid="stRadio"] label:hover { transform:translateX(5px) perspective(500px) rotateY(-3deg); border-color:rgba(99,229,220,.22); box-shadow:5px 7px 16px rgba(0,0,0,.22),inset 0 0 15px rgba(99,229,220,.07); }
[data-testid="stPlotlyChart"] { border-radius:17px; filter:drop-shadow(0 12px 20px rgba(0,0,0,.18)); }
@media (max-width:700px) { .qg-hero { min-height:135px; } .main .block-container { perspective:none; } }
@media (prefers-reduced-motion:reduce) { .qg-hero { animation:none; } }
@media (max-width:700px) {
  .main .block-container { padding:1.1rem .85rem 2.5rem; }
  [data-testid="stTabs"] { padding:8px; }
  /* Reorganiza os cabeçalhos em telas estreitas e mantém seus selos de status visíveis. */
  .qg-hero { flex-wrap:wrap; gap:13px; padding:18px 16px; min-height:125px; }
  .qg-hero-copy { flex:1 1 58%; min-width:0; }
  .qg-cube-wrap { width:48px; height:48px; }
  .qg-cube-front { transform:translateZ(24px); }
  .qg-cube-back { transform:rotateY(180deg) translateZ(24px); }
  .qg-cube-right { transform:rotateY(90deg) translateZ(24px); }
  .qg-cube-left { transform:rotateY(-90deg) translateZ(24px); }
  .qg-cube-top { transform:rotateX(90deg) translateZ(24px); }
  .qg-cube-bottom { transform:rotateX(-90deg) translateZ(24px); }
  .qg-live { display:inline-flex; }
}
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after { animation-duration:.01ms !important; animation-iteration-count:1 !important; scroll-behavior:auto !important; transition-duration:.01ms !important; }
}
</style>""", unsafe_allow_html=True)
components.html("""<script>
(() => {
  try {
    const doc = window.parent.document;
    const root = doc.documentElement;
    if (root.dataset.qgTiltInstalled === "1") return;
    root.dataset.qgTiltInstalled = "1";

    // Partículas leves no fundo da aplicação; o canvas nunca captura cliques.
    const installParticles = () => {
      const app = doc.querySelector(".stApp");
      if (!app || app.querySelector(".qg-particle-field")) return;
      const canvas = doc.createElement("canvas");
      canvas.className = "qg-particle-field";
      canvas.setAttribute("aria-hidden", "true");
      canvas.style.cssText = "position:fixed;inset:0;width:100vw;height:100vh;z-index:0;pointer-events:none;opacity:.72";
      app.prepend(canvas);
      const context = canvas.getContext("2d", { alpha: true });
      if (!context) return;
      const motionPreference = window.matchMedia("(prefers-reduced-motion: reduce)");
      const colors = ["99,229,220", "120,165,255", "176,154,255"];
      let width = 0, height = 0, points = [], frame = 0, previousFrame = 0;
      const resize = () => {
        const ratio = Math.min(window.devicePixelRatio || 1, 1.5);
        width = window.innerWidth;
        height = window.innerHeight;
        canvas.width = Math.round(width * ratio);
        canvas.height = Math.round(height * ratio);
        context.setTransform(ratio, 0, 0, ratio, 0, 0);
        const count = Math.min(64, Math.max(22, Math.round(width / 25)));
        points = Array.from({ length: count }, () => ({
          x: Math.random() * width, y: Math.random() * height,
          vx: (Math.random() - .5) * .22, vy: (Math.random() - .5) * .22,
          radius: .7 + Math.random() * 1.5, color: colors[Math.floor(Math.random() * colors.length)],
        }));
        paint(0, false);
      };
      const paint = (time, animate) => {
        context.clearRect(0, 0, width, height);
        for (let i = 0; i < points.length; i++) {
          const point = points[i];
          if (animate) {
            point.x += point.vx; point.y += point.vy;
            if (point.x < -8 || point.x > width + 8) point.vx *= -1;
            if (point.y < -8 || point.y > height + 8) point.vy *= -1;
          }
          const pulse = animate ? .46 + (Math.sin(time / 1000 + i) + 1) * .19 : .66;
          context.beginPath();
          context.arc(point.x, point.y, point.radius, 0, Math.PI * 2);
          context.fillStyle = `rgba(${point.color},${pulse})`;
          context.shadowBlur = 10;
          context.shadowColor = `rgba(${point.color},.55)`;
          context.fill();
          context.shadowBlur = 0;
          for (let j = i + 1; j < points.length; j++) {
            const other = points[j];
            const dx = point.x - other.x, dy = point.y - other.y;
            const distance = Math.sqrt(dx * dx + dy * dy);
            if (distance < 112) {
              context.beginPath();
              context.moveTo(point.x, point.y); context.lineTo(other.x, other.y);
              context.strokeStyle = `rgba(111,190,218,${(1 - distance / 112) * .095})`;
              context.lineWidth = .7; context.stroke();
            }
          }
        }
      };
      const tick = (time) => {
        if (doc.hidden || motionPreference.matches) return;
        if (time - previousFrame >= 33) { paint(time, true); previousFrame = time; }
        frame = window.requestAnimationFrame(tick);
      };
      resize();
      window.addEventListener("resize", resize, { passive: true });
      doc.addEventListener("visibilitychange", () => {
        if (doc.hidden) window.cancelAnimationFrame(frame);
        else if (!motionPreference.matches) frame = window.requestAnimationFrame(tick);
      });
      if (!motionPreference.matches) frame = window.requestAnimationFrame(tick);
    };
    installParticles();

    const attachTilt = () => {
      doc.querySelectorAll('.qg-hero-auth').forEach((hero) => {
        if (hero.dataset.qgParallax === "1") return;
        hero.dataset.qgParallax = "1";
        hero.addEventListener("pointermove", (event) => {
          const box = hero.getBoundingClientRect();
          const x = (event.clientX - box.left) / Math.max(box.width, 1);
          const y = (event.clientY - box.top) / Math.max(box.height, 1);
          hero.style.setProperty("--qg-light-x", `${Math.round(x * 100)}%`);
          hero.style.setProperty("--qg-light-y", `${Math.round(y * 100)}%`);
          hero.style.setProperty("--qg-orbit-x", `${((x - .5) * 18).toFixed(1)}px`);
          hero.style.setProperty("--qg-orbit-y", `${((y - .5) * 14).toFixed(1)}px`);
        }, { passive: true });
        hero.addEventListener("pointerleave", () => {
          ["--qg-light-x", "--qg-light-y", "--qg-orbit-x", "--qg-orbit-y"].forEach((name) => hero.style.removeProperty(name));
        }, { passive: true });
      });

      // Dá inclinação 3D discreta aos cartões ao acompanhar o ponteiro, sem alterar seu conteúdo ou funcionamento.
      doc.querySelectorAll('[data-testid="stMetric"], .routine-card, .qg-sidebar-profile, .qg-sidebar-brand-mark, .qg-start-card').forEach((card) => {
        if (card.dataset.qgTilt === "1") return;
        card.dataset.qgTilt = "1";
        let scheduled = false;
        let lastEvent = null;
        card.addEventListener("pointermove", (event) => {
          lastEvent = event;
          if (scheduled) return;
          scheduled = true;
          requestAnimationFrame(() => {
            scheduled = false;
            if (!lastEvent) return;
            const box = card.getBoundingClientRect();
            const x = (lastEvent.clientX - box.left) / Math.max(box.width, 1) - .5;
            const y = (lastEvent.clientY - box.top) / Math.max(box.height, 1) - .5;
            // Reduz o ângulo do cartão: mantém o relevo visual sem inclinar demais as letras.
            card.style.transform = `perspective(1200px) rotateX(${(-y * 1.4).toFixed(2)}deg) rotateY(${(x * 1.6).toFixed(2)}deg) translateY(-2px)`;
          });
        }, { passive: true });
        card.addEventListener("pointerleave", () => { card.style.transform = ""; }, { passive: true });
      });
    };
    attachTilt();
    let scanPending = false;
    new MutationObserver(() => {
      if (scanPending) return;
      scanPending = true;
      requestAnimationFrame(() => {
        scanPending = false;
        attachTilt();
      });
    }).observe(doc.body, { childList: true, subtree: true });
  } catch (_error) {
    // As animações CSS continuam ativas caso o navegador isole o iframe.
  }
})();
</script>""", height=0, scrolling=False)

# =============================================================================
# CONSTANTES E LISTAS
# =============================================================================
MATERIAS_TECNICAS_EFA = [
    "Plano de Estudo (PEF) / Caderno da Realidade",
    "Projeto Profissional do Jovem (PPJ)",
    "Agricultura (Cafeicultura, Olericultura e Culturas)",
    "Zootecnia (Manejo e Produção Animal)",
    "Agroecologia, SAFs e Meio Ambiente",
    "Solos e Manejo do Solo",
    "Agroindústria e Processamento de Alimentos",
    "Administração Rural e Cooperativismo",
    "Mecanização e Construções Rurais",
    "Extensão Rural e Desenvolvimento Sustentável",
    "Língua Portuguesa e Redação",
    "Matemática",
    "Biologia",
    "Química",
    "Física",
    "Geografia e Questão Agrária",
    "História",
    "Sociologia e Filosofia",
    "Inglês"
]

def get_edital_espcex():
    return [
        {"Dia": "1º Dia", "Matéria": "Português", "Tópico": "Compreensão e Interpretação de Texto", "Status": "Não Iniciado", "Prioridade": "Alta"},
        {"Matéria": "Português", "Dia": "1º Dia", "Tópico": "Morfologia e Classes Gramaticais", "Status": "Não Iniciado", "Prioridade": "Alta"},
        {"Matéria": "Português", "Dia": "1º Dia", "Tópico": "Sintaxe da Oração e do Período", "Status": "Não Iniciado", "Prioridade": "Alta"},
        {"Matéria": "Português", "Dia": "1º Dia", "Tópico": "Concordância, Regência e Crase", "Status": "Não Iniciado", "Prioridade": "Alta"},
        {"Matéria": "Português", "Dia": "1º Dia", "Tópico": "Pontuação e Ortografia Oficial", "Status": "Não Iniciado", "Prioridade": "Média"},
        {"Matéria": "Português", "Dia": "1º Dia", "Tópico": "Literatura Brasileira (Barroco ao Modernismo)", "Status": "Não Iniciado", "Prioridade": "Alta"},
        
        {"Matéria": "Redação", "Dia": "1º Dia", "Tópico": "Estrutura Dissertativo-Argumentativa Modelo EsPCEx", "Status": "Não Iniciado", "Prioridade": "Alta"},
        {"Matéria": "Redação", "Dia": "1º Dia", "Tópico": "Elaboração de Tese e Projeto de Texto", "Status": "Não Iniciado", "Prioridade": "Alta"},
        {"Matéria": "Redação", "Dia": "1º Dia", "Tópico": "Desenvolvimento e Argumentação Lógica", "Status": "Não Iniciado", "Prioridade": "Alta"},
        {"Matéria": "Redação", "Dia": "1º Dia", "Tópico": "Conclusão e Fechamento das Ideias", "Status": "Não Iniciado", "Prioridade": "Alta"},

        {"Matéria": "Física", "Dia": "1º Dia", "Tópico": "Cinemática Escalar e Vetorial", "Status": "Não Iniciado", "Prioridade": "Alta"},
        {"Matéria": "Física", "Dia": "1º Dia", "Tópico": "Leis de Newton e Dinâmica", "Status": "Não Iniciado", "Prioridade": "Alta"},
        {"Matéria": "Física", "Dia": "1º Dia", "Tópico": "Trabalho, Energia e Potência", "Status": "Não Iniciado", "Prioridade": "Alta"},
        {"Matéria": "Física", "Dia": "1º Dia", "Tópico": "Termologia, Calorimetria e Gases", "Status": "Não Iniciado", "Prioridade": "Alta"},
        {"Matéria": "Física", "Dia": "1º Dia", "Tópico": "Eletrostática e Eletrodinâmica", "Status": "Não Iniciado", "Prioridade": "Alta"},

        {"Matéria": "Química", "Dia": "1º Dia", "Tópico": "Estrutura Atômica e Tabela Periódica", "Status": "Não Iniciado", "Prioridade": "Alta"},
        {"Matéria": "Química", "Dia": "1º Dia", "Tópico": "Ligações Químicas e Funções Inorgânicas", "Status": "Não Iniciado", "Prioridade": "Alta"},
        {"Matéria": "Química", "Dia": "1º Dia", "Tópico": "Reações Químicas e Estequiometria", "Status": "Não Iniciado", "Prioridade": "Alta"},
        {"Matéria": "Química", "Dia": "1º Dia", "Tópico": "Química Orgânica", "Status": "Não Iniciado", "Prioridade": "Alta"},

        {"Matéria": "Matemática", "Dia": "2º Dia", "Tópico": "Conjuntos, Relações e Funções", "Status": "Não Iniciado", "Prioridade": "Alta"},
        {"Matéria": "Matemática", "Dia": "2º Dia", "Tópico": "Função Exponencial e Logarítmica", "Status": "Não Iniciado", "Prioridade": "Alta"},
        {"Matéria": "Matemática", "Dia": "2º Dia", "Tópico": "Trigonometria", "Status": "Não Iniciado", "Prioridade": "Alta"},
        {"Matéria": "Matemática", "Dia": "2º Dia", "Tópico": "Geometria Plana", "Status": "Não Iniciado", "Prioridade": "Alta"},
        {"Matéria": "Matemática", "Dia": "2º Dia", "Tópico": "Geometria Espacial", "Status": "Não Iniciado", "Prioridade": "Alta"},
        {"Matéria": "Matemática", "Dia": "2º Dia", "Tópico": "Geometria Analítica", "Status": "Não Iniciado", "Prioridade": "Alta"},

        {"Matéria": "Geografia", "Dia": "2º Dia", "Tópico": "Geografia Física do Brasil", "Status": "Não Iniciado", "Prioridade": "Alta"},
        {"Matéria": "Geografia", "Dia": "2º Dia", "Tópico": "Demografia e População", "Status": "Não Iniciado", "Prioridade": "Alta"},
        {"Matéria": "Geografia", "Dia": "2º Dia", "Tópico": "Espaço Agropecuário e Questão Agrária", "Status": "Não Iniciado", "Prioridade": "Alta"},
        {"Matéria": "Geografia", "Dia": "2º Dia", "Tópico": "Geopolítica Global", "Status": "Não Iniciado", "Prioridade": "Alta"},

        {"Matéria": "História", "Dia": "2º Dia", "Tópico": "Brasil Colônia e Império", "Status": "Não Iniciado", "Prioridade": "Alta"},
        {"Matéria": "História", "Dia": "2º Dia", "Tópico": "República e Era Vargas", "Status": "Não Iniciado", "Prioridade": "Alta"},
        {"Matéria": "História", "Dia": "2º Dia", "Tópico": "História Geral: Guerras Mundiais e Guerra Fria", "Status": "Não Iniciado", "Prioridade": "Alta"},

        {"Matéria": "Inglês", "Dia": "2º Dia", "Tópico": "Compreensão de Textos Táticos", "Status": "Não Iniciado", "Prioridade": "Alta"},
        {"Matéria": "Inglês", "Dia": "2º Dia", "Tópico": "Gramática Aplicada", "Status": "Não Iniciado", "Prioridade": "Alta"}
    ]

# =============================================================================
# TELA DE AUTENTICAÇÃO E LOGIN
# =============================================================================
if "usuario_logado" not in st.session_state:
    st.session_state.usuario_logado = None

if st.session_state.usuario_logado is not None:
    agora_sessao = time.time()
    ultima_atividade = st.session_state.get("auth_ultima_atividade", agora_sessao)
    token_sessao_atual = st.session_state.get("auth_session_token", "")
    sessao_valida = agora_sessao - ultima_atividade <= 8 * 60 * 60
    if sessao_valida:
        try:
            sessao_valida = _qg_renovar_sessao(token_sessao_atual, st.session_state.usuario_logado)
        except Exception:
            sessao_valida = False
    if not sessao_valida:
        try:
            _qg_revogar_sessao(token_sessao_atual)
        except Exception:
            pass
        st.session_state.usuario_logado = None
        st.session_state.pop("nome_exibicao", None)
        st.session_state.pop("auth_ultima_atividade", None)
        st.session_state.pop("auth_session_token", None)
        st.session_state.login_tentativas = 0
    else:
        st.session_state.auth_ultima_atividade = agora_sessao

if st.session_state.usuario_logado is None:
    # Tela de inicialização pública: apresenta identidade, visão dos módulos e dicas de acesso antes dos formulários.
    st.markdown("""
    <section class="qg-hero qg-hero-auth">
      <div class="qg-auth-orbits" aria-hidden="true"><i></i><i></i><i></i><span></span></div>
      <div class="qg-cube-wrap"><div class="qg-cube">
        <div class="qg-cube-face qg-cube-front">QG</div><div class="qg-cube-face qg-cube-back">🛡️</div>
        <div class="qg-cube-face qg-cube-right">⚔️</div><div class="qg-cube-face qg-cube-left">🎯</div>
        <div class="qg-cube-face qg-cube-top">✦</div><div class="qg-cube-face qg-cube-bottom">✦</div>
      </div></div>
      <div class="qg-hero-copy"><div class="qg-hero-kicker">QG EsPCEx · acesso seguro</div>
        <div class="qg-hero-title">Central de preparação</div>
        <div class="qg-hero-subtitle">Entre na sua conta ou crie seu perfil para continuar.</div>
      </div><div class="qg-live">SISTEMA ATIVO</div>
    </section>
    """, unsafe_allow_html=True)

    # Cartões informativos fixos: resumem os recursos principais sem consultar ou expor dados pessoais.
    # Para editar a apresentação, ajuste os títulos e descrições abaixo; o CSS qg-start-* controla cores, relevo e resposta móvel.
    st.markdown("""
    <section class="qg-start-overview" aria-label="Apresentação dos recursos do QG EsPCEx">
      <header class="qg-start-heading">
        <div class="qg-start-kicker">Central de preparação · visão geral</div>
        <h2>Seu próximo passo começa aqui</h2>
        <p>Entre para abrir sua área pessoal. Estes são alguns dos recursos disponíveis no QG.</p>
      </header>
      <div class="qg-start-grid">
        <article class="qg-start-card">
          <div class="qg-start-card-top"><span class="qg-start-icon" aria-hidden="true">📚</span><span class="qg-start-index">01 / ESTUDO</span></div>
          <h3>Estudo organizado</h3>
          <p>Plano semanal, edital verticalizado, questões e simulados em um só lugar.</p>
        </article>
        <article class="qg-start-card">
          <div class="qg-start-card-top"><span class="qg-start-icon" aria-hidden="true">🎯</span><span class="qg-start-index">02 / ROTINA</span></div>
          <h3>Rotina e foco</h3>
          <p>Missões diárias, cronômetro de foco e acompanhamento da preparação física.</p>
        </article>
        <article class="qg-start-card">
          <div class="qg-start-card-top"><span class="qg-start-icon" aria-hidden="true">🧠</span><span class="qg-start-index">03 / MEMÓRIA</span></div>
          <h3>Revisão contínua</h3>
          <p>Guarde anotações, registre erros e programe revisões espaçadas.</p>
        </article>
        <article class="qg-start-card">
          <div class="qg-start-card-top"><span class="qg-start-icon" aria-hidden="true">🛡️</span><span class="qg-start-index">04 / PERFIL</span></div>
          <h3>Seu espaço pessoal</h3>
          <p>Personalize o painel e acompanhe seus registros depois de entrar na conta.</p>
        </article>
      </div>
      <div class="qg-start-foot"><strong>COMO COMEÇAR</strong><span>Use <b>Entrar na Conta</b> se já tem cadastro. No primeiro acesso, abra <b>Criar Nova Conta</b> e escolha uma senha forte.</span></div>
    </section>
    """, unsafe_allow_html=True)

    st.info(
        "Primeiro acesso? Abra **Criar Nova Conta**. O usuário deve ter de 3 a 32 caracteres; "
        "a senha precisa ter ao menos 12 caracteres, com letras e números. "
        "As credenciais não são salvas no navegador; a sessão termina após oito horas sem atividade."
    )
    if _qg_cloud_enabled():
        st.success("Armazenamento PostgreSQL conectado: contas, progresso, tentativas e sessões são persistidos na nuvem.")
    else:
        st.warning("Persistência em nuvem ainda não configurada. Defina QG_DATABASE_URL em Secrets; no modo local, os dados podem sumir após uma reinicialização do servidor.")

    tab_entrar, tab_cadastrar = st.tabs(["🔐 Entrar na Conta", "📝 Criar Nova Conta"])

    with tab_entrar:
        st.subheader("Login do Cadete")
        login_user = st.text_input("Utilizador / Nome de Guerra", key="input_login_user")
        login_pass = st.text_input("Senha", type="password", key="input_login_pass")

        if st.button("🔑 Entrar no QG"):
            u_clean = normalizar_usuario(login_user)
            chave_login = u_clean or "__login_invalido__"
            espera = tempo_bloqueio(chave_login)
            if espera:
                st.error(f"Muitas tentativas. Aguarde {espera}s antes de tentar novamente.")
            else:
                conta_login = carregar_usuario(u_clean)
                if conta_login and verificar_senha(login_pass, conta_login.get("senha", "")):
                    registrar_tentativa(chave_login, True)
                    if not str(conta_login.get("senha", "")).startswith("pbkdf2_sha256$"):
                        conta_login["senha"] = hash_senha(login_pass)
                        salvar_usuarios({u_clean: conta_login})
                    
                    st.session_state.usuario_logado = u_clean
                    st.session_state.nome_exibicao = conta_login.get("nome", login_user)
                    st.session_state.auth_session_token = _qg_criar_sessao(u_clean)
                    st.session_state.auth_ultima_atividade = time.time()
                    st.success(f"Bem-vindo, Cadete {st.session_state.nome_exibicao}!")
                    st.rerun()
                else:
                    registrar_tentativa(chave_login, False)
                    st.error("Utilizador ou senha incorretos.")

        with st.expander("🔑 Esqueceu sua senha?"):
            st.caption("Redefina sua senha com seu utilizador e a chave de recuperação do responsável pelo sistema.")
            try:
                chave_recuperacao_configurada = str(st.secrets.get("QG_PASSWORD_RESET_KEY", "")).strip()
            except Exception:
                chave_recuperacao_configurada = ""
            if not chave_recuperacao_configurada:
                chave_recuperacao_configurada = os.environ.get("QG_PASSWORD_RESET_KEY", "").strip()
    
            recuperacao_ativa = len(chave_recuperacao_configurada) >= 32
            if not recuperacao_ativa:
                st.info("A recuperação ainda não foi ativada. Configure QG_PASSWORD_RESET_KEY em Secrets.")
    
            with st.form("form_recuperacao_senha", clear_on_submit=True):
                usuario_recuperacao = st.text_input("Utilizador / Nome de Guerra", key="recuperacao_usuario")
                chave_recuperacao = st.text_input("Chave de recuperação", type="password", key="recuperacao_chave")
                nova_senha_recuperacao = st.text_input("Nova senha", type="password", key="recuperacao_nova_senha")
                confirmar_senha_recuperacao = st.text_input("Confirmar nova senha", type="password", key="recuperacao_confirmar_senha")
                solicitar_recuperacao = st.form_submit_button(
                    "Redefinir senha", type="primary", use_container_width=True,
                    disabled=not recuperacao_ativa,
                )
    
            if solicitar_recuperacao:
                usuario_recuperacao_limpo = normalizar_usuario(usuario_recuperacao)
                chave_tentativa_recuperacao = f"recuperacao:{usuario_recuperacao_limpo or '__invalido__'}"
                espera_recuperacao = tempo_bloqueio(chave_tentativa_recuperacao)
                if espera_recuperacao:
                    st.error(f"Muitas tentativas. Aguarde {espera_recuperacao}s antes de tentar novamente.")
                elif not senha_forte(nova_senha_recuperacao):
                    st.error("Use uma senha com pelo menos 12 caracteres, incluindo letras e números.")
                elif nova_senha_recuperacao != confirmar_senha_recuperacao:
                    st.error("As senhas não coincidem.")
                else:
                    conta_recuperacao = carregar_usuario(usuario_recuperacao_limpo)
                    chave_informada_ok = hmac.compare_digest(
                        str(chave_recuperacao).encode("utf-8"),
                        chave_recuperacao_configurada.encode("utf-8"),
                    )
                    if isinstance(conta_recuperacao, dict) and chave_informada_ok:
                        conta_recuperacao["senha"] = hash_senha(nova_senha_recuperacao)
                        salvar_usuarios({usuario_recuperacao_limpo: conta_recuperacao})
                        _qg_revogar_sessoes_usuario(usuario_recuperacao_limpo)
                        registrar_tentativa(chave_tentativa_recuperacao, True)
                        st.success("Senha redefinida. Agora você já pode entrar com a nova senha.")
                    else:
                        registrar_tentativa(chave_tentativa_recuperacao, False)
                        st.error("Não foi possível validar a conta e a chave de recuperação.")
    
    with tab_cadastrar:
        st.subheader("Cadastro de Novo Cadete")
        cad_nome = st.text_input("Nome Completo / Guerra", key="input_cad_nome")
        cad_user = st.text_input("Nome de Utilizador (3–32 caracteres: letras, números, . _ -)", key="input_cad_user")
        cad_pass = st.text_input("Criar Senha", type="password", key="input_cad_pass")
        cad_conf = st.text_input("Confirmar Senha", type="password", key="input_cad_conf")

        if st.button("📝 Criar Minha Conta"):
            u_clean = normalizar_usuario(cad_user)
            if not u_clean or not cad_pass:
                st.warning("Preencha todos os campos.")
            elif len(u_clean) < 3:
                st.error("O nome de usuário precisa ter ao menos 3 caracteres.")
            elif carregar_usuario(u_clean):
                st.error("Nome de utilizador já existe.")
            elif not senha_forte(cad_pass):
                st.error("Use uma senha com pelo menos 12 caracteres, incluindo letras e números.")
            elif cad_pass != cad_conf:
                st.error("Senhas não coincidem.")
            else:
                nova_conta = {
                    "nome": cad_nome if cad_nome else cad_user,
                    "senha": hash_senha(cad_pass),
                    "criado_em": str(datetime.date.today())
                }
                salvar_usuarios({u_clean: nova_conta})
                st.success("Conta criada! Acesse a aba 'Entrar na Conta'.")

    st.stop()

# =============================================================================
# ÁREA LOGADA
# =============================================================================
user_atual = st.session_state.usuario_logado
db = carregar_dados_usuario(user_atual, get_edital_espcex)
nome_cadete = st.session_state.get("nome_exibicao", user_atual)

if "acesso_plataforma_notificado" not in st.session_state:
    st.session_state["acesso_plataforma_notificado"] = True
    registrar_acesso_usuario(user_atual)

def _eventos_proximos(dias=7):
    try:
        conn = sqlite3.connect("dados_qg.db")
        exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='calendario_qg'").fetchone()
        if not exists:
            conn.close()
            return []
        rows = conn.execute("SELECT data_evento, hora_evento, titulo, categoria, prioridade, status FROM calendario_qg WHERE COALESCE(status, 'Pendente') != 'Concluído' ORDER BY data_evento, hora_evento LIMIT 100").fetchall()
        conn.close()
        hoje_local = datetime.date.today()
        limite = hoje_local + datetime.timedelta(days=dias)
        saida = []
        for row in rows:
            try:
                data_evt = datetime.date.fromisoformat(str(row[0])[:10])
            except ValueError:
                continue
            if data_evt <= limite:
                saida.append({"data": str(row[0]), "hora": str(row[1] or ""), "titulo": str(row[2] or "Evento"), "categoria": str(row[3] or "Geral"), "prioridade": str(row[4] or "Média"), "status": str(row[5] or "Pendente"), "atrasado": data_evt < hoje_local})
        return saida[:20]
    except sqlite3.Error:
        return []

eventos_proximos = _eventos_proximos()

QG_MODULOS_NAVEGACAO = [
    "📊 Painel & Rotina Diária", "🧩 Quiz Diário Adaptativo", "🎯 Missões Diárias",
    "🎮 Mini Jogo de Pontos", "🎛️ Personalizar Painel", "👋 Guia Inicial",
    "📲 Instalar como App", "🆕 Novidades do QG", "🦉 Portal Estratégia Militares", "👤 Meu Perfil",
    "🏋️ Treino TAF + Spotify", "🌾 Alternância EFA & AMSP (15/15)", "⏱️ Pomodoro Tático (com Alarme)",
    "✍️ Módulo de Redação", "📋 Edital Verticalizado EsPCEx", "📝 Registro de Questões",
    "📕 Caderno de Bizus & Erros", "🎯 Simulados (1º e 2º Dia)", "📊 Visualizador de Planilhas",
    "🎲 Sorteador de Temas & Textos Base", "🏃 Módulo 13: Pacer TAF", "🥗 Nutrição & Calorias",
    "🌾 Calc. Agropecuária", "📌 Kanban EFA", "📌 Módulo 10 - Quadro Tático (Kanban)",
    "🔥 Habit Tracker", "📅 Cronograma Dinâmico", "🔗 Hub de Links",
    "📖 Diário de Bordo", "🎧 Player de Áudio", "⚙️ Configurações do QG",
    "📅 Calendário Tático", "💰 Controlo Financeiro", "🧠 Memória Avançada",
    "🗓️ Plano Semanal Automático", "💾 Backup e Restauração", "🎯 Modo Foco",
    "🛡️ Módulo 24 · Central Administrativa",
]

QG_ADMIN_MODULOS_INTERNOS = [
    "🛡️ Módulo 24 · Ajustes Privados", "🎮 Admin · Discord Bot", "📈 Admin · Relatórios",
    "📥 Admin · Importação", "💻 Admin · Console SQL", "📜 Admin · Logs",
    "📡 Admin · Telemetria", "🛠️ Admin · Manutenção", "🗄️ Admin · Banco de Dados",
    "🐞 Admin · Diagnóstico & Bugs", "🔐 Admin · Segurança", "🧠 Admin · Inteligência do Efetivo",
    "🎯 Admin · Campanhas QG",
]
QG_ROTAS_ADMIN_LEGADAS = set(["🛡️ Módulo 24 - Painel do Administrador", "🛡️ Módulo 24 · Ajustes Privados"] + QG_ADMIN_MODULOS_INTERNOS)


def _salvar_check_missao(username, data_iso, tarefa_id, widget_key):
    estado = carregar_dados_usuario(username, get_edital_espcex)
    missoes = estado.get("missoes_diarias", {})
    if not isinstance(missoes, dict): missoes = {}
    estado["missoes_diarias"] = missoes
    dia = missoes.get(data_iso, {})
    if not isinstance(dia, dict): dia = {}
    missoes[data_iso] = dia
    dia[tarefa_id] = bool(st.session_state.get(widget_key, False))
    dia["atualizado_em"] = datetime.datetime.now().isoformat(timespec="seconds")
    for chave_antiga in sorted(missoes)[:-90]:
        missoes.pop(chave_antiga, None)
    salvar_dados_usuario(username, estado)

_QG_MISSOES_SEMENTES = [
    ("Manhã", "Rotina matinal", 5, 5, [
        "Começar no horário planejado", "Beber água ao iniciar o dia", "Organizar a mesa de estudos",
        "Conferir a agenda e os compromissos", "Separar o material da primeira matéria",
        "Revisar as três prioridades do dia", "Fazer a mobilidade leve prevista na sua rotina",
        "Abrir o plano de estudos antes das notificações", "Preparar o uniforme ou material necessário",
        "Registrar seu nível de foco para começar",
    ]),
    ("Manhã", "Arranque de estudos", 15, 10, [
        "Revisar cinco cartões de memória", "Ler o resumo do assunto prioritário",
        "Resolver três questões antes de consultar a teoria", "Estudar um tópico por 15 minutos sem interrupção",
        "Recitar de memória os pontos da última aula", "Revisar uma fórmula e explicar quando usá-la",
        "Fazer uma leitura curta de atualidades", "Escrever uma meta objetiva para o primeiro bloco",
        "Conferir uma revisão vencida no quiz", "Registrar uma dúvida para investigar no estudo de hoje",
    ]),
    ("Dia", "Estudo e foco", 25, 15, [
        "Concluir um bloco de estudo de 25 minutos", "Estudar um tópico difícil em etapas menores",
        "Explicar um conceito em voz alta sem olhar o material", "Montar um mapa curto de um assunto",
        "Comparar dois conceitos parecidos e anotar a diferença", "Fazer uma pausa planejada entre blocos",
        "Fechar distrações durante um bloco de foco", "Registrar o tempo real de estudo no diário",
        "Transformar uma dúvida em pergunta de revisão", "Concluir o próximo passo definido no plano semanal",
    ]),
    ("Dia", "Questões e simulados", 20, 15, [
        "Resolver cinco questões da matéria prioritária", "Corrigir uma questão errada e explicar o motivo",
        "Resolver questões sem consultar o gabarito antes", "Anotar o tema de cada erro do bloco",
        "Refazer uma questão que errou anteriormente", "Completar um mini bloco cronometrado de questões",
        "Conferir se entendeu cada alternativa de uma questão", "Resolver uma questão de interpretação de texto",
        "Registrar acertos e tentativas no controle de questões", "Separar duas questões para revisar amanhã",
    ]),
    ("Dia", "Memória e revisão", 15, 12, [
        "Revisar cartões que vencem hoje", "Fazer recordação ativa de um assunto sem consulta",
        "Revisar um erro do Caderno de Bizus", "Criar um cartão para um conceito importante",
        "Revisitar uma matéria estudada há sete dias", "Resumir um tópico em cinco linhas de memória",
        "Explicar a solução de uma questão para si mesmo", "Revisar uma anotação fixada na memória",
        "Marcar o que ainda precisa de nova explicação", "Agendar a próxima revisão de um assunto consolidado",
    ]),
    ("Dia", "Conteúdo e redação", 30, 18, [
        "Escrever uma introdução de redação", "Produzir um parágrafo com tese e argumento",
        "Revisar a clareza de um texto próprio", "Estudar um tópico do edital verticalizado",
        "Relacionar um conteúdo a um exemplo concreto", "Reescrever uma resposta com mais precisão",
        "Ler um texto e identificar a ideia central", "Registrar repertório para uma redação futura",
        "Revisar uma regra gramatical com exemplos", "Atualizar o progresso de um item do edital",
    ]),
    ("Dia", "TAF planejado", 30, 20, [
        "Cumprir somente o treino de TAF previsto no seu plano", "Registrar o treino realizado no histórico do TAF",
        "Conferir o material necessário para o próximo treino", "Fazer o aquecimento que já faz parte da sua orientação",
        "Respeitar o dia de descanso previsto no planejamento", "Anotar como se sentiu durante o treino planejado",
        "Revisar uma meta física definida com seu orientador", "Conferir a hidratação e o material antes do treino",
        "Atualizar uma marca do TAF sem alterar o plano de treino", "Preparar a agenda para a próxima sessão de TAF",
    ]),
    ("Dia", "Bem-estar e constância", 10, 8, [
        "Fazer uma pausa longe da tela", "Organizar o espaço após o bloco de estudos",
        "Fazer uma caminhada leve se ela estiver na sua rotina", "Manter o horário de descanso que você planejou",
        "Preparar água e material para o próximo bloco", "Anotar uma pequena vitória do dia",
        "Pedir ajuda ou registrar uma dúvida que travou seu estudo", "Fazer uma pausa breve entre matérias",
        "Desconectar de notificações durante o período planejado", "Reconhecer uma tarefa concluída sem aumentar a carga de hoje",
    ]),
    ("Dia", "Organização e planejamento", 15, 10, [
        "Atualizar o plano semanal com o que foi concluído", "Escolher a matéria prioritária para o próximo bloco",
        "Separar materiais por matéria", "Conferir os compromissos da semana",
        "Transformar uma meta ampla em uma tarefa pequena", "Remover uma tarefa duplicada da lista de hoje",
        "Anotar o próximo passo antes de encerrar o estudo", "Conferir revisões pendentes no painel",
        "Preparar uma lista curta para o próximo dia", "Revisar se o plano está compatível com o tempo disponível",
    ]),
    ("Noite", "Fechamento do dia", 10, 8, [
        "Marcar as missões concluídas de hoje", "Registrar o principal aprendizado do dia",
        "Escolher a primeira tarefa de amanhã", "Revisar uma anotação curta antes de encerrar",
        "Guardar materiais e deixar o espaço organizado", "Anotar uma dificuldade para retomar amanhã",
        "Conferir o progresso da semana sem se comparar com outras pessoas", "Registrar o estudo realizado no Diário de Bordo",
        "Verificar se há revisão programada para amanhã", "Encerrar o planejamento e respeitar o horário de descanso",
    ]),
]

QG_MISSOES = []
_qg_indice_missao = 0
for _periodo, _categoria, _minutos, _pontos, _titulos in _QG_MISSOES_SEMENTES:
    for _titulo in _titulos:
        _qg_indice_missao += 1
        QG_MISSOES.append({
            "id": f"M{_qg_indice_missao:03d}", "periodo": _periodo,
            "categoria": _categoria, "minutos": _minutos,
            "pontos": _pontos, "titulo": _titulo,
        })
QG_MISSOES_POR_ID = {missao["id"]: missao for missao in QG_MISSOES}

def _render_missao_diaria():
    hoje = datetime.date.today().isoformat()
    missao_config = db.get("config_missoes_diarias", {})
    if not isinstance(missao_config, dict): missao_config = {}
    tarefas_padrao = {
        "taf": "Cumprir o treino de TAF que você planejou",
        "estudo": "Concluir o bloco principal de estudo",
        "revisao": "Fazer a revisão programada",
        "diario": "Registrar a evolução no Diário de Bordo",
    }
    ativas = missao_config.get("ativas", list(tarefas_padrao))
    if not isinstance(ativas, list): ativas = list(tarefas_padrao)
    tarefas = [(chave, titulo) for chave, titulo in tarefas_padrao.items() if chave in ativas]
    extras = missao_config.get("extras", [])
    if isinstance(extras, list):
        tarefas.extend((f"extra_{hashlib.sha256(str(titulo).strip().casefold().encode('utf-8')).hexdigest()[:8]}", str(titulo)[:100]) for titulo in extras if str(titulo).strip())

    planos_catalogo = db.get("missoes_catalogo_por_dia", {})
    ids_catalogo_hoje = planos_catalogo.get(hoje, []) if isinstance(planos_catalogo, dict) else []
    if isinstance(ids_catalogo_hoje, list):
        for missao_id in ids_catalogo_hoje:
            missao = QG_MISSOES_POR_ID.get(str(missao_id))
            if missao:
                tarefas.append((f"catalogo_{missao['id']}", missao["titulo"]))

    missoes_salvas = db.get("missoes_diarias", {})
    estado_dia = missoes_salvas.get(hoje, {}) if isinstance(missoes_salvas, dict) else {}
    if not isinstance(estado_dia, dict): estado_dia = {}

    with st.container(border=True):
        st.markdown("#### 🏅 Missão diária · TAF e rotina")
        st.caption("Marque as atividades que você planejou para hoje. O progresso é salvo na sua conta.")
        descanso_key = f"missao_{user_atual}_{hoje}_descanso_taf"
        if descanso_key not in st.session_state:
            st.session_state[descanso_key] = bool(estado_dia.get("descanso_taf", False))
        st.checkbox(
            "Hoje é um dia sem treino de TAF planejado",
            key=descanso_key, on_change=_salvar_check_missao,
            args=(user_atual, hoje, "descanso_taf", descanso_key),
        )

        total = concluidas = 0
        colunas = st.columns(2)
        for indice, (tarefa_id, titulo) in enumerate(tarefas):
            if tarefa_id == "taf" and st.session_state[descanso_key]:
                continue
            total += 1
            widget_key = f"missao_{user_atual}_{hoje}_{tarefa_id}"
            if widget_key not in st.session_state:
                st.session_state[widget_key] = bool(estado_dia.get(tarefa_id, False))
            col = colunas[indice % 2]
            col.checkbox(titulo, key=widget_key, on_change=_salvar_check_missao, args=(user_atual, hoje, tarefa_id, widget_key))
            concluidas += int(bool(st.session_state[widget_key]))
        if total:
            st.progress(concluidas / total, text=f"Progresso de hoje: {concluidas} de {total} missões")
        else:
            st.info("Escolha as missões diárias em Personalizar Painel.")

st.markdown(f"""
<section class="qg-hero">
  <div class="qg-cube-wrap"><div class="qg-cube">
    <div class="qg-cube-face qg-cube-front">QG</div><div class="qg-cube-face qg-cube-back">🛡️</div>
    <div class="qg-cube-face qg-cube-right">⚔️</div><div class="qg-cube-face qg-cube-left">🎯</div>
    <div class="qg-cube-face qg-cube-top">✦</div><div class="qg-cube-face qg-cube-bottom">✦</div>
  </div></div>
  <div class="qg-hero-copy"><div class="qg-hero-kicker">QG EsPCEx · centro de performance</div>
    <div class="qg-hero-title">Olá, {html.escape(str(nome_cadete))}</div>
    <div class="qg-hero-subtitle">Estudos, rotina e evolução em uma única plataforma · {datetime.date.today().strftime('%d/%m/%Y')}</div>
  </div><div class="qg-live">OPERAÇÕES ONLINE</div>
</section>
""", unsafe_allow_html=True)

# =============================================================================
# BARRA LATERAL (SIDEBAR)
# =============================================================================
def _navegar_sidebar_qg(modulo: str) -> None:
    if modulo in QG_MODULOS_NAVEGACAO:
        st.session_state["qg_nav_menu"] = modulo

perfil_sidebar = carregar_usuario(user_atual)
if not isinstance(perfil_sidebar, dict):
    perfil_sidebar = {}
avatar_sidebar = str(perfil_sidebar.get("avatar", "⚔️"))
if avatar_sidebar not in {"⚔️", "🛡️", "🎖️", "🦅", "🔰", "✦"}:
    avatar_sidebar = (str(nome_cadete).strip()[:1] or "U").upper()
nome_sidebar_seguro = html.escape(str(nome_cadete))
usuario_sidebar_seguro = html.escape(str(user_atual))
st.sidebar.markdown(f"""
<div class="qg-sidebar-brand">
  <div class="qg-sidebar-brand-mark">QG</div>
  <div class="qg-sidebar-brand-copy"><strong>QG EsPCEx</strong><span>Central de preparação</span></div>
</div>
<div class="qg-sidebar-profile">
  <div class="qg-sidebar-avatar">{html.escape(avatar_sidebar)}</div>
  <div class="qg-sidebar-profile-copy"><small>Perfil do cadete</small><strong>{nome_sidebar_seguro}</strong><span>@{usuario_sidebar_seguro}</span></div>
  <i class="qg-sidebar-online" aria-label="sessão ativa"></i>
</div>
""", unsafe_allow_html=True)
if st.sidebar.button("👤 Abrir meu perfil", key="qg_open_profile", use_container_width=True):
    st.session_state["qg_nav_menu"] = "👤 Meu Perfil"
    st.rerun()

if st.sidebar.button("🚪 Encerrar sessão", use_container_width=True):
    try:
        _qg_revogar_sessao(st.session_state.get("auth_session_token", ""))
    except Exception:
        pass
    st.session_state.usuario_logado = None
    st.session_state.pop("nome_exibicao", None)
    st.session_state.pop("auth_ultima_atividade", None)
    st.session_state.pop("auth_session_token", None)
    st.session_state["admin_autenticado"] = False
    st.session_state.pop("admin_ultima_atividade", None)
    st.rerun()

st.sidebar.markdown("---")

if st.session_state.get("qg_nav_menu") in QG_ROTAS_ADMIN_LEGADAS:
    st.session_state["qg_nav_menu"] = "🛡️ Módulo 24 · Central Administrativa"
modulos_qg = QG_MODULOS_NAVEGACAO
if st.session_state.get("qg_nav_menu") not in modulos_qg:
    st.session_state["qg_nav_menu"] = "📊 Painel & Rotina Diária"

st.sidebar.subheader("Módulos do QG")
busca_modulo_qg = st.sidebar.text_input("Buscar módulo", placeholder="Digite para localizar...", key="qg_nav_search", label_visibility="collapsed")
menu_atual_qg = st.session_state.get("qg_nav_menu", "📊 Painel & Rotina Diária")
modulos_filtrados_qg = [m for m in modulos_qg if busca_modulo_qg.strip().casefold() in m.casefold()]

if busca_modulo_qg.strip() and not modulos_filtrados_qg:
    st.sidebar.caption("Nenhum módulo encontrado. A tela atual continua aberta.")
    modulos_filtrados_qg = [menu_atual_qg]
elif menu_atual_qg not in modulos_filtrados_qg:
    modulos_filtrados_qg = [menu_atual_qg] + modulos_filtrados_qg

menu = st.sidebar.radio("Central de Operações", modulos_filtrados_qg, key="qg_nav_menu", label_visibility="collapsed")

# Cabeçalho compartilhado inspirado no Módulo 14: painel animado, identificação da plataforma e relógio do navegador.
# Ele dá às telas autenticadas a mesma abertura visual sem trocar os controles ou os dados próprios de cada módulo.
components.html("""
<div class="qg-enterprise-header" role="banner" aria-label="QG EsPCEx Enterprise">
  <div class="qg-enterprise-grid" aria-hidden="true"></div>
  <div class="qg-enterprise-orbit qg-enterprise-orbit-a" aria-hidden="true"></div>
  <div class="qg-enterprise-orbit qg-enterprise-orbit-b" aria-hidden="true"></div>
  <div class="qg-enterprise-content">
    <div class="qg-enterprise-brand"><span class="qg-enterprise-mark">QG</span><span>QG ESPCEX ENTERPRISE</span></div>
    <div class="qg-enterprise-subtitle">Núcleo de preparação · estudos e performance</div>
    <div class="qg-enterprise-clock" id="qg-enterprise-clock" aria-label="Horário local">⏱️ --:--:-- · SISTEMA ATIVO</div>
  </div>
</div>
<style>
/* Painel comum do Módulo 14: fundo em gradiente animado, relevo nas bordas e leitura centralizada. */
*{box-sizing:border-box}
html,body{width:100%;height:100%;margin:0;padding:0;overflow:hidden;background:transparent;color:#fff;font-family:system-ui,-apple-system,"Segoe UI",Roboto,Arial,sans-serif}
.qg-enterprise-header{position:relative;isolation:isolate;display:grid;place-items:center;width:100%;height:150px;overflow:hidden;border:1px solid rgba(0,201,255,.38);border-radius:17px;background:linear-gradient(115deg,#0b1b29,#17323d 38%,#21423b 70%,#0e202d);box-shadow:0 18px 42px rgba(0,0,0,.34),inset 0 1px rgba(255,255,255,.15),inset 0 -2px rgba(0,0,0,.25);}
.qg-enterprise-header:before{content:"";position:absolute;inset:-45%;z-index:-2;background:linear-gradient(270deg,rgba(0,201,255,.12),rgba(146,254,157,.12),rgba(0,201,255,.12));background-size:200% 200%;animation:qgEnterpriseGradient 10s ease-in-out infinite;}
.qg-enterprise-header:after{content:"";position:absolute;left:7%;right:7%;bottom:0;height:2px;background:linear-gradient(90deg,transparent,#00c9ff,#92fe9d,#00c9ff,transparent);box-shadow:0 0 15px rgba(0,201,255,.65);animation:qgEnterpriseGlow 3s ease-in-out infinite alternate;}
.qg-enterprise-grid{position:absolute;inset:0;z-index:-1;opacity:.22;background-image:linear-gradient(rgba(178,234,239,.12) 1px,transparent 1px),linear-gradient(90deg,rgba(178,234,239,.12) 1px,transparent 1px);background-size:30px 30px;mask-image:linear-gradient(105deg,black,transparent 90%)}
.qg-enterprise-orbit{position:absolute;z-index:-1;top:50%;width:190px;height:190px;border:1px solid rgba(0,201,255,.20);border-radius:50%;box-shadow:0 0 0 15px rgba(0,201,255,.025),inset 0 0 25px rgba(0,201,255,.06);transform:translateY(-50%) rotateX(66deg);pointer-events:none}
.qg-enterprise-orbit-a{left:-58px;animation:qgEnterpriseOrbit 22s linear infinite}
.qg-enterprise-orbit-b{right:-58px;border-color:rgba(146,254,157,.23);transform:translateY(-50%) rotateY(62deg) rotateX(55deg);animation:qgEnterpriseOrbit 28s linear infinite reverse}
.qg-enterprise-content{position:relative;z-index:1;padding:10px 18px;text-align:center}
.qg-enterprise-brand{display:flex;align-items:center;justify-content:center;gap:10px;color:#f7fdff;font-size:clamp(1.05rem,2.8vw,1.55rem);font-weight:850;letter-spacing:.10em}
.qg-enterprise-mark{display:grid;place-items:center;width:36px;height:36px;border:1px solid rgba(146,254,157,.72);border-radius:11px;color:#e8fff0;background:linear-gradient(145deg,rgba(0,201,255,.32),rgba(146,254,157,.23));box-shadow:0 5px 0 rgba(0,0,0,.35),0 0 18px rgba(0,201,255,.22),inset 0 1px rgba(255,255,255,.32);font-size:.78rem;letter-spacing:.02em}
.qg-enterprise-subtitle{margin-top:7px;color:#aaf4be;font-size:.72rem;font-weight:700;letter-spacing:.17em;text-transform:uppercase}
.qg-enterprise-clock{display:inline-block;margin-top:11px;padding:6px 13px;border:1px solid rgba(146,254,157,.29);border-radius:999px;color:#e8f7ff;background:rgba(2,11,18,.46);box-shadow:inset 0 0 12px rgba(0,0,0,.38);font:700 .68rem ui-monospace,SFMono-Regular,Consolas,monospace;letter-spacing:.08em;font-variant-numeric:tabular-nums}
@keyframes qgEnterpriseGradient{0%,100%{transform:translateX(-5%) rotate(-3deg);opacity:.5}50%{transform:translateX(5%) rotate(3deg);opacity:1}}
@keyframes qgEnterpriseGlow{0%{opacity:.55}100%{opacity:1;filter:drop-shadow(0 0 8px #00c9ff)}}
@keyframes qgEnterpriseOrbit{to{rotate:360deg}}
@media(max-width:560px){.qg-enterprise-header{height:146px;border-radius:14px}.qg-enterprise-content{padding:8px}.qg-enterprise-brand{gap:7px;letter-spacing:.055em}.qg-enterprise-mark{width:31px;height:31px}.qg-enterprise-subtitle{font-size:.61rem;letter-spacing:.11em}.qg-enterprise-clock{font-size:.62rem}}
@media(prefers-reduced-motion:reduce){.qg-enterprise-header:before,.qg-enterprise-header:after,.qg-enterprise-orbit{animation:none!important}}
</style>
<script>
// Atualiza o relógio dentro do cabeçalho sem provocar reruns do Streamlit.
(()=>{const clock=document.getElementById("qg-enterprise-clock");if(!clock)return;const render=()=>{clock.textContent="⏱️ "+new Date().toLocaleTimeString("pt-BR")+" · SISTEMA ATIVO"};render();window.setInterval(render,1000)})();
</script>
""", height=160, scrolling=False)

# Navegação rápida para celular: replica a seleção atual do menu lateral em um seletor compacto.
def _navegar_mobile_qg() -> None:
    modulo = st.session_state.get("qg_mobile_nav_selection")
    if modulo in QG_MODULOS_NAVEGACAO:
        st.session_state["qg_nav_menu"] = modulo

if st.session_state.get("qg_mobile_nav_selection") != menu:
    st.session_state["qg_mobile_nav_selection"] = menu
with st.container(key="qg_mobile_navigation"):
    st.selectbox(
        "Acessar módulo",
        options=QG_MODULOS_NAVEGACAO,
        index=QG_MODULOS_NAVEGACAO.index(menu),
        key="qg_mobile_nav_selection",
        on_change=_navegar_mobile_qg,
        label_visibility="visible",
    )

st.sidebar.button(
    "🔐 Acessar área administrativa", key="qg_admin_access", use_container_width=True,
    help="A senha master será solicitada antes de abrir a central.",
    on_click=_navegar_sidebar_qg, args=("🛡️ Módulo 24 · Central Administrativa",),
)
st.sidebar.markdown('<div class="qg-sidebar-action-note">🔐 A Central Administrativa, no fim da lista, solicita a senha master antes de abrir.</div>', unsafe_allow_html=True)

st.sidebar.markdown("---")
with st.sidebar.expander(f"🔔 Agenda · {len(eventos_proximos)} lembretes", expanded=any(e["atrasado"] for e in eventos_proximos)):
    if eventos_proximos:
        for evento in eventos_proximos[:8]:
            data_legivel = datetime.date.fromisoformat(evento["data"][:10]).strftime("%d/%m")
            prefixo = "⚠️ Atrasado" if evento["atrasado"] else f"📅 {data_legivel}"
            st.markdown(f"**{prefixo} · {evento['titulo']}**")
            st.caption(f"{evento['hora']} · {evento['categoria']} · {evento['prioridade']}")
    else:
        st.caption("Sem eventos pendentes para os próximos 7 dias.")

with st.sidebar.expander("🧠 Memória de estudo", expanded=False):
    with st.form("form_memoria_estudo", clear_on_submit=True):
        nota_memoria = st.text_area("Anotação que deseja guardar", max_chars=1000, placeholder="Ex.: revisar cinemática...")
        if st.form_submit_button("Guardar anotação", use_container_width=True):
            if nota_memoria.strip():
                adicionar_memoria(user_atual, nota_memoria.strip())
                st.toast("Anotação guardada.", icon="🧠")
                st.rerun()
    memorias = listar_memorias(user_atual, limite=5)
    if memorias:
        st.caption("Anotações recentes")
        for item in memorias:
            st.markdown(f"- {item['texto']}  ")
            st.caption(item['criada_em'])
    else:
        st.caption("Suas anotações persistentes aparecerão aqui.")

# Ciclo EFA
efa_info = db.get("efa_ciclo", {"modo": "Em Casa", "inicio_ciclo": str(datetime.date.today())})
try: dt_inicio = datetime.datetime.strptime(efa_info["inicio_ciclo"], "%Y-%m-%d").date()
except Exception: dt_inicio = datetime.date.today()
dias_decorridos = (datetime.date.today() - dt_inicio).days + 1
dia_no_ciclo = min(max(dias_decorridos, 1), 15)

st.sidebar.markdown(f"**Sessão EFA:** `{efa_info['modo']}`")
st.sidebar.markdown(f"**Progresso EFA:** `Dia {dia_no_ciclo} de 15`")

st.sidebar.markdown("---")
st.sidebar.subheader("⏳ Contagem Regressiva EsPCEx")
data_prova_str = db.get("data_prova", str(datetime.date.today()))
try: data_prova = datetime.datetime.strptime(data_prova_str, "%Y-%m-%d").date()
except ValueError: data_prova = datetime.date.today()

nova_data = st.sidebar.date_input("Data da Prova", data_prova)
if nova_data != data_prova:
    db["data_prova"] = str(nova_data)
    salvar_dados_usuario(user_atual, db)
    st.rerun()

dias_restantes = (nova_data - datetime.date.today()).days
st.sidebar.metric("Dias até a Prova", f"{dias_restantes} dias")
st.sidebar.markdown(f"**Ciclos Pomodoro:** `{db.get('pomodoro_ciclos_totais', 0)}`")

# -----------------------------------------------------------------------------
# UTILITÁRIOS DAS TELAS
# -----------------------------------------------------------------------------
def _navegar_qg(modulo: str) -> None:
    """Seleciona uma tela já existente no menu lateral."""
    if modulo in QG_MODULOS_NAVEGACAO:
        st.session_state["qg_nav_menu"] = modulo


def _carregar_campanha_missoes_qg() -> dict:
    """Lê a campanha do SQLite e devolve um objeto vazio se ainda não existir."""
    conn = None
    try:
        conn = sqlite3.connect("dados_qg.db", timeout=5)
        row = conn.execute(
            "SELECT valor FROM config_qg WHERE chave = ?",
            ("campanha_missoes_qg",),
        ).fetchone()
        campanha = json.loads(row[0]) if row and row[0] else {}
        return campanha if isinstance(campanha, dict) else {}
    except (sqlite3.Error, json.JSONDecodeError, TypeError):
        return {}
    finally:
        if conn is not None:
            conn.close()


# -----------------------------------------------------------------------------
# MÓDULO 1: Painel & Cronograma da Rotina Diária
# -----------------------------------------------------------------------------
# O tema 3D compartilhado foi aplicado na inicialização do aplicativo.

# -----------------------------------------------------------------------------
# LÓGICA DA INTERFACE - PAINEL PRINCIPAL
# -----------------------------------------------------------------------------
if menu == "👤 Meu Perfil":
    conta_perfil = carregar_usuario(user_atual)
    if not isinstance(conta_perfil, dict):
        conta_perfil = {}
    opcoes_avatar_perfil = ["⚔️", "🛡️", "🎖️", "🦅", "🔰", "✦"]
    avatar_salvo_perfil = str(conta_perfil.get("avatar", "⚔️"))
    if avatar_salvo_perfil not in opcoes_avatar_perfil:
        avatar_salvo_perfil = "⚔️"
    nome_perfil = str(conta_perfil.get("nome", nome_cadete))

    st.markdown(f"""
    <section class="qg-hero qg-profile-hero">
      <div class="qg-profile-orb">{html.escape(avatar_salvo_perfil)}</div>
      <div class="qg-hero-copy"><div class="qg-hero-kicker">QG EsPCEx · identidade do cadete</div>
        <div class="qg-hero-title">Meu perfil</div>
        <div class="qg-hero-subtitle">Personalize sua identificação e acompanhe seu progresso em um só lugar.</div>
      </div><div class="qg-live">CONTA ATIVA</div>
    </section>
    """, unsafe_allow_html=True)

    sessoes_perfil = db.get("sessoes", [])
    sessoes_perfil = [item for item in sessoes_perfil if isinstance(item, dict)] if isinstance(sessoes_perfil, list) else []
    try:
        horas_perfil = sum(float(item.get("Horas", 0) or 0) for item in sessoes_perfil)
    except (TypeError, ValueError):
        horas_perfil = 0.0
    cards_perfil = db.get("quiz_cards", [])
    taf_perfil = db.get("taf", [])
    p1, p2, p3, p4 = st.columns(4)
    p1.metric("Horas registradas", f"{horas_perfil:.1f} h")
    p2.metric("Sessões de estudo", len(sessoes_perfil))
    p3.metric("Questões próprias", len(cards_perfil) if isinstance(cards_perfil, list) else 0)
    p4.metric("Registros de TAF", len(taf_perfil) if isinstance(taf_perfil, list) else 0)

    st.markdown("#### Identificação")
    with st.container(border=True):
        with st.form("qg_editar_perfil"):
            nome_perfil_novo = st.text_input("Nome exibido no QG", value=nome_perfil, max_chars=60)
            avatar_perfil_novo = st.selectbox(
                "Emblema do perfil", opcoes_avatar_perfil,
                index=opcoes_avatar_perfil.index(avatar_salvo_perfil),
                help="O emblema aparece no cartão de perfil da barra lateral.",
            )
            salvar_perfil = st.form_submit_button("Salvar meu perfil", type="primary", use_container_width=True)
        if salvar_perfil:
            if not nome_perfil_novo.strip():
                st.error("Digite um nome para exibir no perfil.")
            else:
                conta_perfil["nome"] = nome_perfil_novo.strip()
                conta_perfil["avatar"] = avatar_perfil_novo
                salvar_usuarios({user_atual: conta_perfil})
                st.session_state["nome_exibicao"] = nome_perfil_novo.strip()
                st.toast("Perfil atualizado.", icon="✅")
                st.rerun()

    with st.container(border=True):
        st.markdown("#### 🔐 Área administrativa")
        st.caption("O acesso à Central Administrativa continua protegido pela senha master da conta proprietária.")
        st.button(
            "Abrir a área protegida", type="primary", use_container_width=True,
            on_click=_navegar_qg, args=("🛡️ Módulo 24 · Central Administrativa",),
        )

elif menu == "📊 Painel & Rotina Diária":
    st.title(f"📊 QG EsPCEx - Painel de Prontidão de {nome_cadete}")
    st.caption("Visão integrada dos estudos noturnos, treino físico da madrugada, trabalho no campo e realização da AMSP.")
    
    if not db.get("guia_inicial_concluido", False):
        with st.container():
            st.info("Primeiro acesso? O Guia Inicial ajuda a configurar o painel, o TAF e suas metas.")
            st.button("👋 Abrir Guia Inicial", on_click=_navegar_qg, args=("👋 Guia Inicial",))

    modo_efa = efa_info["modo"]

    # Extração de dados
    df_sessoes = pd.DataFrame(db["sessoes"])
    df_red = pd.DataFrame(db.get("redacoes", []))
    df_taf = pd.DataFrame(db.get("taf", []))
    amsp_pendentes = [t for t in db.get("tarefas_casa", []) if not t.get("concluida", False)]

    total_horas = df_sessoes["Horas"].sum() if not df_sessoes.empty else 0.0
    total_questoes = df_sessoes["Questões"].sum() if not df_sessoes.empty else 0
    total_acertos = df_sessoes["Acertos"].sum() if not df_sessoes.empty else 0
    taxa_acerto = (total_acertos / total_questoes * 100) if total_questoes > 0 else 0.0

    painel_config = db.get("painel_config", {})
    if not isinstance(painel_config, dict):
        painel_config = {}
        
    opcoes_indicadores = [
        "Horas de estudo", "Questões respondidas", "Taxa de acerto", "Treinos TAF",
        "Dias até a prova", "AMSP pendentes", "Revisões pendentes", "Progresso do edital",
    ]
    indicadores_salvos = painel_config.get("indicadores", opcoes_indicadores[:5])
    if not isinstance(indicadores_salvos, list):
        indicadores_salvos = opcoes_indicadores[:5]
        
    with st.expander("🎛️ Personalizar indicadores do painel", expanded=False):
        with st.form("form_indicadores_painel"):
            indicadores_escolhidos = st.multiselect(
                "Escolha quais cartões aparecem no topo",
                opcoes_indicadores, default=[x for x in indicadores_salvos if x in opcoes_indicadores],
                help="A seleção fica salva na sua conta."
            )
            if st.form_submit_button("Salvar meu painel", type="primary", use_container_width=True):
                db["painel_config"] = {**painel_config, "indicadores": indicadores_escolhidos}
                salvar_dados_usuario(user_atual, db)
                st.rerun()

    revisoes_pendentes_qtd = len(listar_memorias(user_atual, limite=1000, apenas_revisar=True))
    itens_edital = [item for item in db.get("edital", []) if isinstance(item, dict)]
    itens_edital_concluidos = sum(str(item.get("Status", "")).casefold() in {"revisado / dominado", "dominado"} for item in itens_edital)
    progresso_edital_pct = round(itens_edital_concluidos / len(itens_edital) * 100) if itens_edital else 0
    
    dados_indicadores = {
        "Horas de estudo": ("⏱️ Horas de estudo", f"{total_horas:.1f} h"),
        "Questões respondidas": ("📝 Questões respondidas", f"{total_questoes}"),
        "Taxa de acerto": ("🎯 Taxa de acerto", f"{taxa_acerto:.1f}%"),
        "Treinos TAF": ("🏋️ Treinos TAF", str(len(df_taf))),
        "Dias até a prova": ("📆 Dias até a prova", str(dias_restantes)),
        "AMSP pendentes": ("📚 AMSP pendentes", str(len(amsp_pendentes))),
        "Revisões pendentes": ("🧠 Revisões pendentes", str(revisoes_pendentes_qtd)),
        "Progresso do edital": ("📋 Progresso do edital", f"{progresso_edital_pct}%"),
    }
    
    indicadores_ativos = [item for item in indicadores_salvos if item in dados_indicadores]
    
    st.markdown("<br>", unsafe_allow_html=True)
    if indicadores_ativos:
        metric_cols = st.columns(min(len(indicadores_ativos), 6))
        for indice, item in enumerate(indicadores_ativos):
            rotulo, valor = dados_indicadores[item]
            metric_cols[indice % len(metric_cols)].metric(rotulo, valor)
    else:
        st.info("Selecione pelo menos um indicador no cartão de personalização acima.")

    hoje_iso = str(datetime.date.today())
    plano_atual = db.get("plano_semanal", {})
    tarefas_hoje = [t for t in plano_atual.get("tarefas", []) if t.get("data") == hoje_iso and not t.get("concluida", False)]
    revisoes_hoje = listar_memorias(user_atual, limite=100, apenas_revisar=True)
    eventos_hoje = [e for e in eventos_proximos if e.get("data", "")[:10] == hoje_iso]
    materias_fracas = []
    
    if not df_sessoes.empty and {"Matéria", "Questões", "Acertos"}.issubset(df_sessoes.columns):
        resumo_materia = df_sessoes.groupby("Matéria")[["Questões", "Acertos"]].sum()
        resumo_materia = resumo_materia[resumo_materia["Questões"] > 0]
        if not resumo_materia.empty:
            resumo_materia["Aproveitamento"] = resumo_materia["Acertos"] / resumo_materia["Questões"]
            materias_fracas = list(resumo_materia.sort_values("Aproveitamento").head(2).index)

    st.markdown("<br>", unsafe_allow_html=True)
    with st.container():
        st.markdown("#### ✦ Missão do dia")
        st.caption(datetime.date.today().strftime("%A, %d/%m/%Y") + " · construída a partir do seu plano, revisões e calendário")
        missao_cols = st.columns(3)
        missao_idx = 0
        
        if tarefas_hoje:
            tarefa = tarefas_hoje[0]
            missao_cols[missao_idx].markdown("**🎯 Plano semanal**")
            missao_cols[missao_idx].write(f"{tarefa.get('materia', 'Estudo')}: {tarefa.get('atividade', '')}")
            missao_idx += 1
        if revisoes_hoje:
            item_revisao = revisoes_hoje[0]
            missao_cols[missao_idx].markdown("**🧠 Revisão espaçada**")
            missao_cols[missao_idx].write(f"Revisar {item_revisao.get('materia', 'Geral')}: {item_revisao.get('texto', '')[:100]}")
            missao_idx += 1
        if eventos_hoje and missao_idx < 3:
            missao_cols[missao_idx].markdown("**📅 Compromisso de hoje**")
            missao_cols[missao_idx].write(f"{eventos_hoje[0]['hora']} · {eventos_hoje[0]['titulo']}")
            missao_idx += 1
        if materias_fracas and missao_idx < 3:
            missao_cols[missao_idx].markdown("**⚠️ Foco recomendado**")
            missao_cols[missao_idx].write("Resolver questões de " + ", ".join(materias_fracas))
            missao_idx += 1
        if missao_idx == 0:
            missao_cols[0].write("Gere seu plano semanal, faça uma revisão ou registre a primeira sessão para montar uma missão personalizada.")

    _render_missao_diaria()

    st.markdown("---")
    st.subheader(f"📅 Estrutura da Sua Rotina - Período [{modo_efa.upper()}]")

    # Renderização Condicional da Rotina com Divs Estilizadas
    if modo_efa == "Em Casa":
        r1, r2, r3, r4 = st.columns(4)
        with r1:
            st.markdown("""
            <div class="routine-card">
            <h4>🌅 05:00 - 06:15</h4>
            <b>Treino TAF + SPOTIFY</b><br><br>
            <span style="color:#94a3b8; font-size:0.9em;">Corrida, Flexão, Abdominal e Barra Fixa ao som de Marchas do Exército / Funk TAF.</span>
            </div>
            """, unsafe_allow_html=True)
        with r2:
            st.markdown("""
            <div class="routine-card">
            <h4>🚜 06:30 - 17:00</h4>
            <b>Trabalho no Campo</b><br><br>
            <span style="color:#94a3b8; font-size:0.9em;">Atividades agrícolas e braçais da família na alternância.</span>
            </div>
            """, unsafe_allow_html=True)
        with r3:
            st.markdown("""
            <div class="routine-card">
            <h4>📖 17:00 - 18:30</h4>
            <b>Descanso & AMSP</b><br><br>
            <span style="color:#94a3b8; font-size:0.9em;">Pausa de recuperação e resolução da AMSP da EFA.</span>
            </div>
            """, unsafe_allow_html=True)
        with r4:
            st.markdown("""
            <div class="routine-card">
            <h4>📚 18:30 - 22:30</h4>
            <b>Estudos Noturnos</b><br><br>
            <span style="color:#94a3b8; font-size:0.9em;">Blocos de Pomodoro e plataforma Estratégia Militares.</span>
            </div>
            """, unsafe_allow_html=True)
    else:
        r1, r2, r3 = st.columns(3)
        with r1:
            st.markdown("""
            <div class="routine-card">
            <h4>🌅 05:00 - 06:15</h4>
            <b>Treino TAF Madrugada</b><br><br>
            <span style="color:#94a3b8; font-size:0.9em;">Treino de aptidão física militar com áudio motivacional.</span>
            </div>
            """, unsafe_allow_html=True)
        with r2:
            st.markdown("""
            <div class="routine-card">
            <h4>🏫 07:00 - 17:00</h4>
            <b>Aulas Presenciais EFA</b><br><br>
            <span style="color:#94a3b8; font-size:0.9em;">Disciplinas teóricas e práticas da Escola Família Agrícola.</span>
            </div>
            """, unsafe_allow_html=True)
        with r3:
            st.markdown("""
            <div class="routine-card">
            <h4>📚 18:30 - 22:30</h4>
            <b>QG Noturno EsPCEx</b><br><br>
            <span style="color:#94a3b8; font-size:0.9em;">Revisão teórica, LDI, BQEM e simulados do Estratégia.</span>
            </div>
            """, unsafe_allow_html=True)

    st.markdown("---")

    # -----------------------------------------------------------------------------
    # GRÁFICOS MELHORADOS COM ASPECTO 3D
    # -----------------------------------------------------------------------------
    col_g1, col_g2 = st.columns(2)
    
    with col_g1:
        st.subheader("📈 Rendimento em Questões")
        if not df_sessoes.empty:
            df_mat = df_sessoes.groupby("Matéria")[["Acertos", "Questões"]].sum().reset_index()
            df_mat["Erros"] = df_mat["Questões"] - df_mat["Acertos"]
            
            # Gráfico de Barras Estilizado Simulando 3D no Plotly
            fig_mat = go.Figure()
            fig_mat.add_trace(go.Bar(
                x=df_mat["Matéria"], y=df_mat["Acertos"],
                name='Acertos',
                marker=dict(
                    color='#10b981',
                    line=dict(color='#059669', width=2)
                )
            ))
            fig_mat.add_trace(go.Bar(
                x=df_mat["Matéria"], y=df_mat["Erros"],
                name='Erros',
                marker=dict(
                    color='#ef4444',
                    line=dict(color='#b91c1c', width=2)
                )
            ))
            fig_mat.update_layout(
                barmode='stack',
                paper_bgcolor="rgba(0,0,0,0)",
                plot_bgcolor="rgba(0,0,0,0)",
                font=dict(color="#e2e8f0"),
                margin=dict(l=20, r=20, t=30, b=20),
                legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1)
            )
            # Adicionando grid mais suave para dar profundidade
            fig_mat.update_yaxes(showgrid=True, gridwidth=1, gridcolor='rgba(255,255,255,0.1)')
            st.plotly_chart(fig_mat, use_container_width=True)
        else:
            st.info("Nenhuma bateria de questões registrada.")

    with col_g2:
        st.subheader("📋 Progresso do Edital")
        df_edital = pd.DataFrame(db.get("edital", []))
        
        if not df_edital.empty:
            df_status = df_edital["Status"].value_counts().reset_index()
            df_status.columns = ["Status", "Quantidade"]
            
            cores_map = {
                "Não Iniciado": "#334155", 
                "Teoria Lida": "#f59e0b", 
                "Exercícios Feitos": "#3b82f6", 
                "Revisado / Dominado": "#10b981"
            }
            
            # Prepara os dados para o JavaScript do Highcharts 3D
            data_for_hc = []
            for _, row in df_status.iterrows():
                # O item "Revisado / Dominado" fica levemente destacado
                is_sliced = row["Status"] == "Revisado / Dominado"
                data_for_hc.append({
                    "name": row["Status"],
                    "y": int(row["Quantidade"]),
                    "color": cores_map.get(row["Status"], "#888888"),
                    "sliced": is_sliced,
                    "selected": is_sliced
                })
            
            # Injeta o Highcharts 3D via HTML/JS (Gráfico de Rosca/Donut Premium)
            hc_html = f"""
            <html>
            <head>
                <script src="https://code.highcharts.com/highcharts.js"></script>
                <script src="https://code.highcharts.com/highcharts-3d.js"></script>
                <style>
                    body {{ 
                        background-color: transparent; 
                        margin: 0; 
                        padding: 0; 
                        font-family: 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; 
                    }}
                    #container {{ height: 350px; width: 100%; }}
                </style>
            </head>
            <body>
                <div id="container"></div>
                <script>
                    Highcharts.chart('container', {{
                        chart: {{
                            type: 'pie',
                            options3d: {{
                                enabled: true,
                                alpha: 45,
                                beta: 0
                            }},
                            backgroundColor: 'transparent'
                        }},
                        title: {{
                            text: null
                        }},
                        tooltip: {{
                            pointFormat: '<b>{{point.percentage:.1f}}%</b><br>({{point.y}} itens)',
                            backgroundColor: 'rgba(15, 23, 42, 0.9)',
                            style: {{ color: '#e2e8f0' }},
                            borderWidth: 1,
                            borderColor: '#334155'
                        }},
                        plotOptions: {{
                            pie: {{
                                allowPointSelect: true,
                                cursor: 'pointer',
                                depth: 45,
                                innerSize: '30%', /* Cria o efeito Donut 3D */
                                dataLabels: {{
                                    enabled: true,
                                    format: '<b>{{point.name}}</b>',
                                    color: '#e2e8f0',
                                    style: {{ 
                                        textOutline: 'none', 
                                        fontSize: '13px',
                                        fontWeight: '600'
                                    }}
                                }}
                            }}
                        }},
                        series: [{{
                            name: 'Progresso',
                            data: {json.dumps(data_for_hc)}
                        }}],
                        credits: {{
                            enabled: false
                        }}
                    }});
                </script>
            </body>
            </html>
            """
            
            components.html(hc_html, height=360)
        else:
            st.info("Edital não importado ainda.")
            
# -----------------------------------------------------------------------------
# PERSONALIZAÇÃO DO PAINEL
# -----------------------------------------------------------------------------
elif menu == "🎛️ Personalizar Painel":
    st.title("🎛️ Personalizar Painel")
    st.caption("Escolha os indicadores e as tarefas que aparecem na sua rotina diária.")
    
    painel_config = db.get("painel_config", {})
    missao_config = db.get("config_missoes_diarias", {})
    if not isinstance(painel_config, dict): painel_config = {}
    if not isinstance(missao_config, dict): missao_config = {}
    
    indicadores_disponiveis = [
        "Horas de estudo", "Questões respondidas", "Taxa de acerto", "Treinos TAF",
        "Dias até a prova", "AMSP pendentes", "Revisões pendentes", "Progresso do edital",
    ]
    missoes_disponiveis = {
        "taf": "Treino TAF planejado",
        "estudo": "Bloco principal de estudo",
        "revisao": "Revisão programada",
        "diario": "Registro no Diário de Bordo",
    }
    
    missoes_atuais = missao_config.get("ativas", list(missoes_disponiveis))
    if not isinstance(missoes_atuais, list): missoes_atuais = list(missoes_disponiveis)
    extras_atuais = missao_config.get("extras", [])
    if not isinstance(extras_atuais, list): extras_atuais = []

    with st.container():
        st.markdown("#### Sua visão de acompanhamento")
        with st.form("form_personalizar_painel_completo"):
            indicadores_personalizados = st.multiselect(
                "Cartões de indicadores", indicadores_disponiveis,
                default=[x for x in painel_config.get("indicadores", indicadores_disponiveis[:5]) if x in indicadores_disponiveis],
                help="Você pode escolher e reordenar os indicadores do topo do painel."
            )
            missoes_personalizadas = st.multiselect(
                "Missões que aparecem todo dia", list(missoes_disponiveis),
                format_func=lambda chave: missoes_disponiveis[chave],
                default=[x for x in missoes_atuais if x in missoes_disponiveis]
            )
            outras_missoes_texto = st.text_area(
                "Outras missões diárias (uma por linha)",
                value="\n".join(str(x) for x in extras_atuais[:5]), max_chars=500,
                placeholder="Ex.: Separar material para amanhã\nConferir agenda da semana"
            )
            if st.form_submit_button("Salvar personalização", type="primary", use_container_width=True):
                extras_limpos = list(dict.fromkeys(linha.strip()[:100] for linha in outras_missoes_texto.splitlines() if linha.strip()))[:5]
                db["painel_config"] = {**painel_config, "indicadores": indicadores_personalizados}
                db["config_missoes_diarias"] = {"ativas": missoes_personalizadas, "extras": extras_limpos}
                salvar_dados_usuario(user_atual, db)
                st.success("Seu painel e as missões diárias foram salvos.")
                st.rerun()

    with st.expander("Prévia dos indicadores", expanded=True):
        if indicadores_personalizados:
            st.caption("Os cartões escolhidos aparecerão no topo do Painel & Rotina Diária.")
            st.markdown("**Missão diária:** TAF planejado, estudo, revisão, diário e tarefas extras que você ativou.")
        else:
            st.info("Nenhum indicador selecionado. Você ainda verá as missões e gráficos do painel.")
            
    st.button("Voltar ao painel", on_click=_navegar_qg, args=("📊 Painel & Rotina Diária",))
   
# -----------------------------------------------------------------------------
# MÓDULO 2: Portal Estratégia Militares
# -----------------------------------------------------------------------------
elif menu == "🦉 Portal Estratégia Militares":
    st.title("🦉 Portal do Aluno - Estratégia Militares")
    st.caption("Acesso direto à plataforma para estudos, LDI, Banco de Questões (BQEM) e videoaulas.")

    st.markdown("""
    <div style="background-color: #1a2318; padding: 15px; border-radius: 8px; border-left: 5px solid #0056b3; margin-bottom: 20px;">
        <h4>📱 Acesso Rápido Mobile / Desktop</h4>
        <p>Acesse a Área do Aluno do Estratégia Militares no seu smartphone ou computador:</p>
        <a href="https://militares.estrategia.com" target="_blank" style="background-color: #0056b3; color: white; padding: 10px 20px; border-radius: 6px; text-decoration: none; font-weight: bold; display: inline-block; margin-right: 10px;">
            🚀 Abrir Plataforma EM (Nova Aba)
        </a>
        <a href="https://perfil.estrategia.com/login" target="_blank" style="background-color: #2e4d25; color: white; padding: 10px 20px; border-radius: 6px; text-decoration: none; font-weight: bold; display: inline-block;">
            🔐 Entrar na Conta (Login EM)
        </a>
    </div>
    """, unsafe_allow_html=True)

    st.markdown("---")
    st.subheader("🌐 Visualização em Tela da Plataforma")
    components.iframe("https://militares.estrategia.com", height=650, scrolling=True)

# -----------------------------------------------------------------------------
# MÓDULO 3: Treino TAF + Player de Música Spotify
# -----------------------------------------------------------------------------
elif menu == "🏋️ Treino TAF + Spotify":
    st.title("🏋️ Treino TAF + Spotify")
    st.caption("Registre seus resultados, acompanhe a evolução e abra o player de áudio na mesma tela.")

    registros_taf = db.get("taf", [])
    if not isinstance(registros_taf, list):
        registros_taf = []
    registros_taf = [item for item in registros_taf if isinstance(item, dict)]
    db["taf"] = registros_taf

    aba_registro_taf, aba_evolucao_taf, aba_spotify_taf = st.tabs(
        ["📝 Registrar resultado", "📈 Minha evolução", "🎧 Spotify"]
    )

    with aba_registro_taf:
        st.markdown("#### Bateria TAF")
        with st.form("form_treino_taf_usuario", clear_on_submit=True):
            data_treino_taf = st.date_input("Data do registro", datetime.date.today())
            taf_col1, taf_col2, taf_col3, taf_col4 = st.columns(4)
            corrida_12min = taf_col1.number_input(
                "Corrida em 12 min (metros)", min_value=0, max_value=6000, value=0, step=50
            )
            barra_reps = taf_col2.number_input(
                "Barra fixa (repetições)", min_value=0, max_value=100, value=0, step=1
            )
            abdominal_reps = taf_col3.number_input(
                "Abdominal em 1 min", min_value=0, max_value=200, value=0, step=1
            )
            flexao_reps = taf_col4.number_input(
                "Flexões (repetições)", min_value=0, max_value=200, value=0, step=1
            )
            taf_col5, taf_col6 = st.columns([1, 2])
            natacao_segundos = taf_col5.number_input(
                "Natação 50 m (segundos; 0 se não registrou)", min_value=0, max_value=900, value=0, step=1
            )
            observacao_taf = taf_col6.text_input("Observação (opcional)", max_chars=240)
            salvar_taf = st.form_submit_button("💾 Salvar resultado", type="primary", use_container_width=True)

        if salvar_taf:
            if not any((corrida_12min, barra_reps, abdominal_reps, flexao_reps, natacao_segundos)):
                st.warning("Informe pelo menos um resultado antes de salvar.")
            else:
                registros_taf.append({
                    "id": uuid.uuid4().hex[:12],
                    "Data": data_treino_taf.isoformat(),
                    "Corrida 12 min (m)": int(corrida_12min),
                    "Barra fixa (reps)": int(barra_reps),
                    "Abdominal (reps/min)": int(abdominal_reps),
                    "Flexões (reps)": int(flexao_reps),
                    "Natação 50 m (s)": int(natacao_segundos) if natacao_segundos else "",
                    "Observação": observacao_taf.strip(),
                })
                db["taf"] = registros_taf
                salvar_dados_usuario(user_atual, db)
                st.toast("Resultado do TAF salvo no seu perfil.", icon="🏋️")
                st.rerun()

        st.button(
            "Abrir Pacer TAF avançado →",
            on_click=_navegar_qg,
            args=("🏃 Módulo 13: Pacer TAF",),
        )

    with aba_evolucao_taf:
        if not registros_taf:
            st.info("Ainda não há resultados registrados. Use a primeira aba para salvar sua bateria.")
        else:
            registros_ordenados_taf = sorted(
                registros_taf,
                key=lambda item: str(item.get("Data", item.get("data", ""))),
                reverse=True,
            )
            df_historico_taf = pd.DataFrame(registros_ordenados_taf)
            ultima_taf = registros_ordenados_taf[0]
            corrida_valores_taf = [
                int(item.get("Corrida 12 min (m)", item.get("corrida_12min_m", 0)) or 0)
                for item in registros_taf
            ]
            barra_valores_taf = [
                int(item.get("Barra fixa (reps)", item.get("barra_reps", 0)) or 0)
                for item in registros_taf
            ]
            taf_m1, taf_m2, taf_m3 = st.columns(3)
            taf_m1.metric("Registros", len(registros_taf))
            taf_m2.metric("Melhor corrida · 12 min", f"{max(corrida_valores_taf, default=0)} m")
            taf_m3.metric("Melhor barra fixa", f"{max(barra_valores_taf, default=0)} reps")

            datas_corrida_taf = []
            for item in registros_taf:
                distancia = int(item.get("Corrida 12 min (m)", item.get("corrida_12min_m", 0)) or 0)
                data_item = str(item.get("Data", item.get("data", "")))
                if distancia > 0 and data_item:
                    datas_corrida_taf.append({"Data": data_item, "Distância (m)": distancia})
            if datas_corrida_taf:
                st.markdown("#### Evolução da corrida")
                fig_corrida_taf = px.line(
                    pd.DataFrame(datas_corrida_taf).sort_values("Data"),
                    x="Data", y="Distância (m)", markers=True,
                    template="qg_enterprise",
                )
                st.plotly_chart(fig_corrida_taf, use_container_width=True)
            st.markdown("#### Histórico salvo")
            st.dataframe(df_historico_taf, use_container_width=True, hide_index=True)

    with aba_spotify_taf:
        st.markdown("#### 🎵 Player de treino")
        st.caption("O player é fornecido pelo Spotify e pode solicitar autenticação ou abrir o aplicativo Spotify.")
        components.iframe(
            "https://open.spotify.com/embed/playlist/37i9dQZF1DX76Wlfdnj7bg?utm_source=generator&theme=0",
            height=430,
            scrolling=True,
        )

# -----------------------------------------------------------------------------
# MÓDULO 4: Alternância EFA & AMSP (15/15) - Disciplinas da EFA
# -----------------------------------------------------------------------------
elif menu == "🌾 Alternância EFA & AMSP (15/15)":
    import sqlite3
    import datetime
    import pandas as pd

    st.title("🌾 Gestão da Alternância EFA & AMSP")
    st.caption("Controle dos ciclos de 15 dias e gestão da AMSP focada nas matérias da EFA.")

    # 1. Garante a criação das tabelas no SQLite local
    def init_db_efa():
        conn = sqlite3.connect("dados_qg.db")
        c = conn.cursor()
        
        # Tabela de controle do ciclo (Em Casa / Na Escola)
        c.execute('''
            CREATE TABLE IF NOT EXISTS efa_ciclo (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                modo TEXT,
                inicio_ciclo TEXT
            )
        ''')

        # Tabela de tarefas AMSP
        c.execute('''
            CREATE TABLE IF NOT EXISTS tarefas_amsp (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                disciplina TEXT,
                titulo TEXT,
                categoria TEXT,
                prazo TEXT,
                prioridade TEXT,
                detalhes TEXT,
                concluida INTEGER DEFAULT 0,
                criada_em TEXT
            )
        ''')
        
        conn.commit()
        conn.close()

    init_db_efa()

    # Fallback para a lista de matérias técnicas da EFA
    MATERIAS_EFA = globals().get("MATERIAS_TECNICAS_EFA", [
        "Plano de Estudo (PEF) / Caderno da Realidade",
        "Projeto Profissional do Jovem (PPJ)",
        "Agricultura (Cafeicultura, Olericultura e Culturas)",
        "Zootecnia (Manejo e Produção Animal)",
        "Agroecologia, SAFs e Meio Ambiente",
        "Solos e Manejo do Solo"
    ])

    # 2. Carrega o estado atual do ciclo a partir do SQLite
    conn = sqlite3.connect("dados_qg.db")
    c = conn.cursor()
    c.execute("SELECT modo, inicio_ciclo FROM efa_ciclo ORDER BY id DESC LIMIT 1")
    ciclo_atual = c.fetchone()
    conn.close()

    if ciclo_atual:
        modo_atual, inicio_ciclo_str = ciclo_atual
    else:
        modo_atual = "Em Casa"
        inicio_ciclo_str = str(datetime.date.today())

    # 3. Configuração do Ciclo Atual
    with st.expander("⚙️ Configurar Ciclo Atual de Alternância", expanded=True):
        col_c1, col_c2, col_c3 = st.columns(3)
        modo_selecionado = col_c1.selectbox("Período Ativo do Ciclo", ["Em Casa", "Na Escola"], index=0 if modo_atual == "Em Casa" else 1)
        
        try:
            dt_init_val = datetime.datetime.strptime(inicio_ciclo_str, "%Y-%m-%d").date()
        except Exception:
            dt_init_val = datetime.date.today()

        data_inicio_input = col_c2.date_input("Data de Início dos 15 Dias", dt_init_val)

        if col_c3.button("💾 Atualizar Estado do Ciclo"):
            conn = sqlite3.connect("dados_qg.db")
            c = conn.cursor()
            c.execute("INSERT INTO efa_ciclo (modo, inicio_ciclo) VALUES (?, ?)", (modo_selecionado, str(data_inicio_input)))
            conn.commit()
            conn.close()
            st.success("Estado do ciclo EFA salvo com sucesso!")
            st.rerun()

    st.markdown("---")

    # 4. Modo EM CASA - Central de AMSP
    if modo_selecionado == "Em Casa":
        st.subheader("🏠 Período EM CASA - Central da AMSP (Atividades da EFA)")
        st.info("💡 Registre aqui as pesquisas do Caderno da Realidade, PEF e tarefas de todas as matérias da EFA para o horário das 17h00 - 18h30.")

        with st.form("form_tarefa_casa", clear_on_submit=True):
            st.markdown("#### ➕ Cadastrar Nova AMSP por Matéria da EFA")
            
            col_tf1, col_tf2 = st.columns(2)
            disciplina_amsp = col_tf1.selectbox("Matéria da EFA", MATERIAS_EFA)
            titulo_tf = col_tf2.text_input("Título / Tema da AMSP (Ex: Diagnóstico de Pragas do Café / Análise de Solo)")

            col_tf3, col_tf4 = st.columns(2)
            categoria_tf = col_tf3.selectbox("Tipo de Atividade AMSP", [
                "Caderno da Realidade / Pesquisa de Campo",
                "Plano de Estudo Familiar (PEF)",
                "Lista de Exercícios / Questões da Matéria",
                "Experimento Prático / Trabalho no Campo",
                "Relatório de Observação / Entrevista"
            ])
            prioridade_tf = col_tf4.selectbox("Prioridade", ["Alta", "Média", "Baixa"])

            prazo_tf = st.date_input("Prazo de Entrega na EFA", datetime.date.today() + datetime.timedelta(days=7))
            detalhes_tf = st.text_area("Descrição / Questões da AMSP")

            if st.form_submit_button("📌 Salvar AMSP"):
                if titulo_tf.strip():
                    conn = sqlite3.connect("dados_qg.db")
                    c = conn.cursor()
                    c.execute('''
                        INSERT INTO tarefas_amsp (disciplina, titulo, categoria, prazo, prioridade, detalhes, concluida, criada_em)
                        VALUES (?, ?, ?, ?, ?, ?, 0, ?)
                    ''', (disciplina_amsp, titulo_tf, categoria_tf, str(prazo_tf), prioridade_tf, detalhes_tf, str(datetime.date.today())))
                    conn.commit()
                    conn.close()
                    st.success("AMSP cadastrada com sucesso no banco de dados local!")
                    st.rerun()
                else:
                    st.warning("Preencha o título da AMSP antes de salvar.")

        st.markdown("---")
        st.subheader("📋 Painel de AMSP (Filtro por Matéria da EFA)")

        # Carregar tarefas da base de dados local de forma segura
        conn = sqlite3.connect("dados_qg.db")
        c = conn.cursor()
        c.execute("SELECT id, disciplina, titulo, categoria, prazo, prioridade, detalhes, concluida FROM tarefas_amsp ORDER BY id DESC")
        rows = c.fetchall()
        conn.close()

        # Estruturação para dicionário explícito
        tarefas_list = [
            {
                "id": r[0],
                "disciplina": r[1],
                "titulo": r[2],
                "categoria": r[3],
                "prazo": r[4],
                "prioridade": r[5],
                "detalhes": r[6],
                "concluida": bool(r[7])
            }
            for r in rows
        ]

        if not tarefas_list:
            st.info("Nenhuma AMSP cadastrada até o momento.")
        else:
            filtro_disc = st.selectbox("Filtrar AMSP por Matéria da EFA", ["Todas as Matérias"] + MATERIAS_EFA)
            
            # Filtro por disciplina
            tarefas_exibir = tarefas_list if filtro_disc == "Todas as Matérias" else [t for t in tarefas_list if t["disciplina"] == filtro_disc]

            p_tab1, p_tab2 = st.tabs(["⏳ AMSP Pendentes", "✅ AMSP Concluídas"])

            with p_tab1:
                pendentes = [t for t in tarefas_exibir if not t["concluida"]]
                if not pendentes:
                    st.success("🎉 Nenhuma AMSP pendente para a seleção atual!")
                else:
                    for t in pendentes:
                        with st.container():
                            st.markdown(f"### 🌾 [{t['disciplina']}] - {t['titulo']}")
                            st.markdown(f"**Tipo:** `{t['categoria']}` | **Prazo:** `{t['prazo']}` | **Prioridade:** `{t['prioridade']}`")
                            st.write(t["detalhes"])

                            if st.button(f"✅ Marcar AMSP Concluída #{t['id']}", key=f"btn_tf_{t['id']}"):
                                conn = sqlite3.connect("dados_qg.db")
                                c = conn.cursor()
                                c.execute("UPDATE tarefas_amsp SET concluida = 1 WHERE id = ?", (t["id"],))
                                conn.commit()
                                conn.close()
                                st.success("AMSP concluída!")
                                st.rerun()
                            st.markdown("---")

            with p_tab2:
                concluidas = [t for t in tarefas_exibir if t["concluida"]]
                if not concluidas:
                    st.info("Nenhuma AMSP concluída nesta seleção.")
                else:
                    for t in concluidas:
                        st.markdown(f"~~**[{t['disciplina']}]** - {t['titulo']} ({t['categoria']})~~ ✅ Concluída")

    # 5. Modo NA ESCOLA
    else:
        st.subheader("🏫 Período NA ESCOLA - Aulas Presenciais")
        st.success("ℹ️ **Módulo de tarefas AMSP de casa ocultado durante o período presencial na EFA.**")
        st.markdown("""
        Durante os **15 dias de permanência na EFA**:
        - 📖 Acompanhamento direto de aulas teóricas e práticas com professores e monitores;
        - 🤝 Vivência comunitária e trabalhos práticos nos setores de produção;
        - 📚 Liberação da noite para focar nos blocos de Pomodoro e LDI da EsPCEx.
        """)
# -----------------------------------------------------------------------------
# MÓDULO 5: Pomodoro Tático (Com Alarme Sonoro)
# -----------------------------------------------------------------------------
elif menu == "⏱️ Pomodoro Tático (com Alarme)":
    def tocar_alarme_sonoro():
        components.html("""
            <script>
            const audioCtx = new (window.AudioContext || window.webkitAudioContext)();
            function playBeep(freq, duration) {
                const osc = audioCtx.createOscillator();
                const gain = audioCtx.createGain();
                osc.type = "sine";
                osc.frequency.value = freq;
                gain.gain.setValueAtTime(0.15, audioCtx.currentTime);
                osc.connect(gain);
                gain.connect(audioCtx.destination);
                osc.start();
                osc.stop(audioCtx.currentTime + duration);
            }
            playBeep(880, 0.25);
            setTimeout(() => playBeep(1046.5, 0.4), 300);
            </script>
        """, height=0)

    st.title("⏱️ Pomodoro Noturno com Alarme Sonoro")
    st.caption("Temporizador para os blocos de estudo da noite. Ao chegar ao zero, um alarme sonoro soará automaticamente.")

    col_config1, col_config2, col_config3 = st.columns(3)
    tempo_foco = col_config1.number_input("🎯 Tempo de Foco (Minutos)", min_value=5, max_value=120, value=30, step=5)
    tempo_descanso_curto = col_config2.number_input("☕ Descanso Curto (Minutos)", min_value=1, max_value=30, value=5, step=1)
    tempo_descanso_longo = col_config3.number_input("🏕️ Descanso Longo (Minutos)", min_value=5, max_value=60, value=15, step=5)

    st.markdown("---")

    if "pomodoro_estado" not in st.session_state:
        st.session_state.pomodoro_estado = "PARADO"
    if "tempo_restante_seg" not in st.session_state:
        st.session_state.tempo_restante_seg = tempo_foco * 60

    col_btn1, col_btn2, col_btn3 = st.columns(3)

    if col_btn1.button("🚀 Iniciar Bloco de Foco Noturno"):
        st.session_state.pomodoro_estado = "FOCO"
        st.session_state.tempo_restante_seg = tempo_foco * 60

    if col_btn2.button("☕ Iniciar Descanso"):
        st.session_state.pomodoro_estado = "DESCANSO"
        st.session_state.tempo_restante_seg = tempo_descanso_curto * 60

    if col_btn3.button("🛑 Interromper / Resetar"):
        st.session_state.pomodoro_estado = "PARADO"
        st.session_state.tempo_restante_seg = tempo_foco * 60

    st.markdown("---")

    ph_timer = st.empty()
    ph_status = st.empty()

    if st.session_state.pomodoro_estado in ["FOCO", "DESCANSO"]:
        estado_atual = st.session_state.pomodoro_estado
        cor_header = "#4caf50" if estado_atual == "FOCO" else "#ff9800"
        icone = "⚔️ FOCO NOTURNO ATIVO" if estado_atual == "FOCO" else "☕ DESCANSO / RECOVERY"

        ph_status.markdown(f"<h3 style='text-align: center; color: {cor_header};'>{icone}</h3>", unsafe_allow_html=True)

        for seg in range(st.session_state.tempo_restante_seg, -1, -1):
            if st.session_state.pomodoro_estado == "PARADO":
                break
            
            m, s = divmod(seg, 60)
            ph_timer.markdown(f"<h1 style='text-align: center; font-size: 90px; color: {cor_header}; font-family: monospace;'>{m:02d}:{s:02d}</h1>", unsafe_allow_html=True)
            time.sleep(1)

        if st.session_state.pomodoro_estado != "PARADO":
            tocar_alarme_sonoro()
            st.balloons()

            if estado_atual == "FOCO":
                st.success("🔔 ALARME: Bloco de Foco Concluído! +1 Ciclo Noturno Registrado.")
                db["pomodoro_ciclos_totais"] = db.get("pomodoro_ciclos_totais", 0) + 1
                salvar_dados_usuario(user_atual, db)
            else:
                st.info("🔔 ALARME: Descanso finalizado! Hora do próximo bloco.")
            st.session_state.pomodoro_estado = "PARADO"
    else:
        m, s = divmod(tempo_foco * 60, 60)
        ph_timer.markdown(f"<h1 style='text-align: center; font-size: 90px; color: #888888; font-family: monospace;'>{m:02d}:{s:02d}</h1>", unsafe_allow_html=True)
        ph_status.markdown("<h4 style='text-align: center; color: #888888;'>Aguardando início da sessão</h4>", unsafe_allow_html=True)

# -----------------------------------------------------------------------------
# MÓDULO 6: Redação
# -----------------------------------------------------------------------------
elif menu == "✍️ Módulo de Redação":
    st.title("✍️ Oficina de Redação - Padrão EsPCEx")
    st.caption("Treino contínuo da produção textual dissertativa-argumentativa.")

    df_red = pd.DataFrame(db.get("redacoes", []))
    total_redacoes = len(df_red)
    media_redacao = df_red["Nota"].mean() if not df_red.empty else 0.0

    col_r1, col_r2 = st.columns(2)
    col_r1.metric("✍️ Redações Produzidas", f"{total_redacoes}")
    col_r2.metric("📊 Média das Notas", f"{media_redacao:.1f} / 100")

    st.markdown("---")
    aba_cadastro, aba_historico, aba_repertorio = st.tabs(["➕ Cadastrar Redação", "📜 Histórico & Evolução", "💡 Repertórios"])

    with aba_cadastro:
        with st.form("form_redacao", clear_on_submit=True):
            c_t1, c_t2 = st.columns(2)
            tema = c_t1.text_input("Tema da Redação")
            data_red = c_t2.date_input("Data de Produção", datetime.date.today())

            c_e1, c_e2 = st.columns(2)
            eixo = c_e1.selectbox("Eixo Temático", ["Defesa / Geopolítica", "Sociedade / Cidadania", "Tecnologia / Segurança", "Meio Ambiente / Amazônia", "Outros"])
            nota = c_e2.number_input("Nota (0 a 100)", 0.0, 100.0, 80.0, step=5.0)

            repertorio = st.text_area("Repertórios Socioculturais Utilizados")
            pontos = st.text_area("Pontos a Melhorar")

            if st.form_submit_button("⚔️ Registrar Redação"):
                nova_r = {
                    "Data": str(data_red),
                    "Tema": tema,
                    "Eixo": eixo,
                    "Nota": float(nota),
                    "Repertório": repertorio,
                    "Pontos de Melhoria": pontos
                }
                db["redacoes"].append(nova_r)
                salvar_dados_usuario(user_atual, db)
                st.success("Redação registrada!")
                st.rerun()

    with aba_historico:
        if not df_red.empty:
            fig_red = px.line(df_red, x="Tema", y="Nota", markers=True, title="Evolução de Notas")
            fig_red.add_hline(y=50.0, line_dash="dash", line_color="red", annotation_text="Mínimo 50 pts")
            fig_red.update_layout(paper_bgcolor="rgba(0,0,0,0)", font_color="#e0e6ed")
            st.plotly_chart(fig_red, use_container_width=True)
            st.dataframe(df_red, use_container_width=True)
        else:
            st.info("Nenhuma redação cadastrada ainda.")

    with aba_repertorio:
        st.markdown("""
        - 📜 **Artigo 142 da CF/88:** Papel constitucional das Forças Armadas.
        - 🌲 **Projeto Calha Norte:** Soberania e proteção das fronteiras da Amazônia.
        - 🧠 **Zygmunt Bauman:** Modernidade Líquida e relações interpessoais.
        """)

# -----------------------------------------------------------------------------
# MÓDULO 7: Edital Verticalizado (com Chave Dinâmica)
# -----------------------------------------------------------------------------
elif menu == "📋 Edital Verticalizado EsPCEx":
    import sqlite3
    import pandas as pd

    st.title("📋 Edital Verticalizado EsPCEx")

    # 1. Garante a criação da tabela no SQLite local
    def init_db_edital():
        conn = sqlite3.connect("dados_qg.db")
        c = conn.cursor()
        c.execute('''
            CREATE TABLE IF NOT EXISTS edital_verticalizado (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dia TEXT,
                materia TEXT,
                topico TEXT UNIQUE,
                status TEXT DEFAULT 'Não Iniciado',
                prioridade TEXT DEFAULT 'Média'
            )
        ''')
        conn.commit()
        conn.close()

    init_db_edital()

    # 2. Migra/Popula os dados iniciais caso o banco local esteja vazio
    conn = sqlite3.connect("dados_qg.db")
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM edital_verticalizado")
    total_registos = c.fetchone()[0]

    if total_registos == 0 and "db" in globals() and isinstance(db, dict) and "edital" in db and db["edital"]:
        for item in db["edital"]:
            c.execute('''
                INSERT OR IGNORE INTO edital_verticalizado (dia, materia, topico, status, prioridade)
                VALUES (?, ?, ?, ?, ?)
            ''', (
                item.get("Dia", "1º Dia"),
                item.get("Matéria", "Geral"),
                item.get("Tópico", "Tópico de Estudo"),
                item.get("Status", "Não Iniciado"),
                item.get("Prioridade", "Média")
            ))
        conn.commit()
    conn.close()

    # 3. Carrega os dados atualizados do banco SQLite local
    conn = sqlite3.connect("dados_qg.db")
    try:
        df_edital = pd.read_sql_query('''
            SELECT 
                id,
                dia AS "Dia",
                materia AS "Matéria",
                topico AS "Tópico",
                status AS "Status",
                prioridade AS "Prioridade"
            FROM edital_verticalizado
            ORDER BY id ASC
        ''', conn)
    except Exception:
        df_edital = pd.DataFrame(columns=["id", "Dia", "Matéria", "Tópico", "Status", "Prioridade"])
    finally:
        conn.close()

    if df_edital.empty:
        st.info("Nenhum tópico de edital encontrado na base de dados.")
    else:
        # Filtros de visualização
        col_f1, col_f2 = st.columns(2)
        dias_disponiveis = ["Todos"] + sorted(list(df_edital["Dia"].unique()))
        materias_disponiveis = ["Todas"] + sorted(list(df_edital["Matéria"].unique()))

        dia_f = col_f1.selectbox("Dia da Prova", dias_disponiveis)
        mat_f = col_f2.selectbox("Disciplina", materias_disponiveis)

        df_exib = df_edital.copy()
        if dia_f != "Todos":
            df_exib = df_exib[df_exib["Dia"] == dia_f]
        if mat_f != "Todas":
            df_exib = df_exib[df_exib["Matéria"] == mat_f]

        # Tabela editável (oculta a coluna id para o utilizador)
        edited_df = st.data_editor(
            df_exib,
            column_config={
                "id": None,  # Oculta a coluna ID técnica
                "Status": st.column_config.SelectboxColumn(
                    "Status",
                    options=["Não Iniciado", "Teoria Lida", "Exercícios Feitos", "Revisado / Dominado"],
                    required=True
                ),
                "Prioridade": st.column_config.SelectboxColumn(
                    "Relevância",
                    options=["Baixa", "Média", "Alta"],
                    required=True
                )
            },
            disabled=["Dia", "Matéria", "Tópico"],  # Impede a edição dos nomes dos tópicos por engano
            use_container_width=True,
            key=f"edital_edit_{dia_f}_{mat_f}"
        )

        # 4. Salvar as alterações diretamente no SQLite
        if st.button("💾 Salvar Edital"):
            novos_dados = edited_df.to_dict("records")
            
            conn = sqlite3.connect("dados_qg.db")
            c = conn.cursor()
            for row in novos_dados:
                c.execute('''
                    UPDATE edital_verticalizado
                    SET status = ?, prioridade = ?
                    WHERE id = ?
                ''', (row["Status"], row["Prioridade"], row["id"]))
            conn.commit()
            conn.close()

            st.success("✅ Edital atualizado com sucesso na base de dados local!")
            st.rerun()

# -----------------------------------------------------------------------------
# MÓDULO 8: Registro de Questões
# -----------------------------------------------------------------------------
elif menu == "📝 Registro de Questões":
    import sqlite3
    import datetime
    import pandas as pd

    st.title("📝 Registro de Baterias de Questões")

    # 1. Garante a criação da tabela no SQLite local
    def init_db_questoes():
        conn = sqlite3.connect("dados_qg.db")
        c = conn.cursor()
        c.execute('''
            CREATE TABLE IF NOT EXISTS baterias_questoes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                data TEXT,
                materia TEXT,
                topico TEXT,
                questoes INTEGER,
                acertos INTEGER,
                horas REAL
            )
        ''')
        conn.commit()
        conn.close()

    init_db_questoes()

    # 2. Carrega as matérias e tópicos do edital local do SQLite (ou usa lista padrão como fallback)
    materias_disponiveis = ["Português", "Matemática", "História", "Geografia", "Inglês", "Física", "Química"]
    df_edital_local = pd.DataFrame()

    conn = sqlite3.connect("dados_qg.db")
    try:
        df_edital_local = pd.read_sql_query("SELECT materia AS 'Matéria', topico AS 'Tópico' FROM edital_verticalizado", conn)
    except Exception:
        pass
    finally:
        conn.close()

    if not df_edital_local.empty and "Matéria" in df_edital_local.columns:
        materias_disponiveis = sorted(list(df_edital_local["Matéria"].dropna().unique()))
    elif "db" in globals() and isinstance(db, dict) and "edital" in db and db["edital"]:
        try:
            df_e = pd.DataFrame(db["edital"])
            if "Matéria" in df_e.columns:
                materias_disponiveis = sorted(list(df_e["Matéria"].dropna().unique()))
                df_edital_local = df_e
        except Exception:
            pass

    # 3. Formulário para registrar bateria de questões
    with st.form("form_q", clear_on_submit=True):
        col1, col2 = st.columns(2)
        data_q = col1.date_input("Data", datetime.date.today())
        materia_q = col2.selectbox("Matéria", materias_disponiveis)

        topicos_mat = []
        if not df_edital_local.empty and "Matéria" in df_edital_local.columns and "Tópico" in df_edital_local.columns:
            topicos_mat = df_edital_local[df_edital_local["Matéria"] == materia_q]["Tópico"].dropna().tolist()

        topico_q = st.selectbox("Assunto", topicos_mat if topicos_mat else ["Geral"])

        col3, col4, col5 = st.columns(3)
        qtd_q = col3.number_input("Total Questões", min_value=1, value=20)
        qtd_a = col4.number_input("Acertos", min_value=0, max_value=int(qtd_q), value=15)
        tempo_h = col5.number_input("Horas Dedicadas", min_value=0.1, max_value=10.0, value=1.0, step=0.1)

        if st.form_submit_button("⚔️ Salvar Bateria"):
            conn = sqlite3.connect("dados_qg.db")
            c = conn.cursor()
            c.execute('''
                INSERT INTO baterias_questoes (data, materia, topico, questoes, acertos, horas)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (str(data_q), materia_q, topico_q, int(qtd_q), int(qtd_a), float(tempo_h)))
            conn.commit()
            conn.close()

            st.success("⚔️ Bateria registrada com sucesso no banco de dados local!")
            st.rerun()

    st.markdown("---")

    # 4. Histórico de Baterias Salvas no SQLite
    conn = sqlite3.connect("dados_qg.db")
    try:
        df_sessoes = pd.read_sql_query('''
            SELECT 
                data AS "Data",
                materia AS "Matéria",
                topico AS "Tópico",
                questoes AS "Questões",
                acertos AS "Acertos",
                horas AS "Horas"
            FROM baterias_questoes
            ORDER BY id DESC
        ''', conn)
    except Exception:
        df_sessoes = pd.DataFrame()
    finally:
        conn.close()

    if not df_sessoes.empty:
        st.dataframe(df_sessoes, use_container_width=True)
    else:
        st.info("Nenhuma bateria de questões registrada até o momento.")
# -----------------------------------------------------------------------------
# MÓDULO 9: Caderno de Bizus & Erros
# -----------------------------------------------------------------------------
elif "bizu" in menu.lower() or "erros" in menu.lower():
    import sqlite3
    import datetime
    import pandas as pd

    st.title("📕 Caderno de Bizus Táticos e Erros")
    st.caption("Central de registo de erros, pegadinhas e correções táticas para revisão.")

    # 1. Garante a criação da tabela no SQLite local
    def init_db_erros():
        conn = sqlite3.connect("dados_qg.db")
        c = conn.cursor()
        c.execute('''
            CREATE TABLE IF NOT EXISTS caderno_erros (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                data TEXT,
                materia TEXT,
                assunto TEXT,
                motivo TEXT,
                bisu TEXT,
                sanado INTEGER DEFAULT 0
            )
        ''')
        conn.commit()
        conn.close()

    init_db_erros()

    # Obter matérias cadastradas no edital local do SQLite ou lista de reserva
    materias_padrao = ["Português", "Matemática", "História", "Geografia", "Inglês", "Física", "Química", "EFA / Técnica"]
    conn = sqlite3.connect("dados_qg.db")
    try:
        df_edital = pd.read_sql_query("SELECT DISTINCT materia AS 'Matéria' FROM edital_verticalizado", conn)
        if not df_edital.empty and "Matéria" in df_edital.columns:
            materias_padrao = sorted(df_edital["Matéria"].dropna().tolist())
    except Exception:
        pass
    finally:
        conn.close()

    # 2. Formulário para cadastrar novo Bisu
    with st.expander("➕ Cadastrar Novo Bisu", expanded=True):
        with st.form("form_e", clear_on_submit=True):
            c1, c2 = st.columns(2)
            mat_e = c1.selectbox("Matéria", materias_padrao)
            assunto_e = c2.text_input("Assunto / Questão")

            motivo = st.text_area("Qual foi a pegadinha / erro?")
            bisu = st.text_area("Bisu de Correção Tática")

            if st.form_submit_button("📌 Salvar Bisu"):
                if assunto_e.strip() or motivo.strip() or bisu.strip():
                    conn = sqlite3.connect("dados_qg.db")
                    c = conn.cursor()
                    c.execute('''
                        INSERT INTO caderno_erros (data, materia, assunto, motivo, bisu, sanado)
                        VALUES (?, ?, ?, ?, ?, 0)
                    ''', (str(datetime.date.today()), mat_e, assunto_e, motivo, bisu))
                    conn.commit()
                    conn.close()
                    st.success("📌 Bisu registrado com sucesso na base de dados local!")
                    st.rerun()
                else:
                    st.warning("Preencha as informações do bisu antes de salvar.")

    st.markdown("---")

    # 3. Exibição dos bizus pendentes na base de dados SQLite
    conn = sqlite3.connect("dados_qg.db")
    c = conn.cursor()
    c.execute("SELECT id, data, materia, assunto, motivo, bisu FROM caderno_erros WHERE sanado = 0 ORDER BY id DESC")
    erros_pendentes = c.fetchall()
    conn.close()

    if not erros_pendentes:
        st.success("🎉 Nenhum bisu pendente de revisão!")
    else:
        for err in erros_pendentes:
            err_id, err_data, err_mat, err_assunto, err_motivo, err_bisu = err
            st.markdown(f"### ⚔️ [{err_mat}] - {err_assunto}")
            st.markdown(f"**Causa:** {err_motivo}")
            st.info(f"💡 **BISU:** {err_bisu}")
            
            if st.button(f"✅ Marcar como Sanado #{err_id}", key=f"btn_sanar_bisu_{err_id}"):
                conn = sqlite3.connect("dados_qg.db")
                c = conn.cursor()
                c.execute("UPDATE caderno_erros SET sanado = 1 WHERE id = ?", (err_id,))
                conn.commit()
                conn.close()
                st.success("✅ Bisu marcado como sanado!")
                st.rerun()
            st.markdown("---")
# -----------------------------------------------------------------------------
# MÓDULO 10: Simulados
# -----------------------------------------------------------------------------
elif menu == "📌 Módulo 10 - Quadro Tático (Kanban)":
    import sqlite3
    import datetime
    import pandas as pd

    st.title("📌 Módulo 10 - Quadro Tático (Kanban)")
    st.caption("Organização tática de tarefas e metas divididas por colunas de status.")

    # 1. Cria a tabela no SQLite local se não existir
    def init_db_kanban():
        conn = sqlite3.connect("dados_qg.db")
        c = conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS kanban_tarefas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                titulo TEXT,
                categoria TEXT,
                prioridade TEXT,
                status TEXT DEFAULT 'A Fazer',
                data_criacao TEXT
            )
        """)
        conn.commit()
        conn.close()

    init_db_kanban()

    # 2. Formulário para adicionar nova tarefa
    with st.expander("➕ Nova Tarefa Tática", expanded=False):
        with st.form("form_kanban", clear_on_submit=True):
            col_k1, col_k2 = st.columns(2)
            titulo_k = col_k1.text_input("Título / Descrição da Tarefa")
            categoria_k = col_k2.selectbox("Categoria", ["EsPCEx / Estudos", "EFA / AMSP", "TAF", "Geral"])

            col_k3, col_k4 = st.columns(2)
            prioridade_k = col_k3.selectbox("Prioridade", ["Alta", "Média", "Baixa"])
            status_k = col_k4.selectbox("Coluna Inicial", ["A Fazer", "Em Andamento", "Concluído"])

            if st.form_submit_button("📌 Salvar Tarefa"):
                if titulo_k.strip():
                    conn = sqlite3.connect("dados_qg.db")
                    c = conn.cursor()
                    c.execute("""
                        INSERT INTO kanban_tarefas (titulo, categoria, prioridade, status, data_criacao)
                        VALUES (?, ?, ?, ?, ?)
                    """, (titulo_k, categoria_k, prioridade_k, status_k, str(datetime.date.today())))
                    conn.commit()
                    conn.close()
                    st.success("Tarefa registrada no Quadro Tático!")
                    st.rerun()
                else:
                    st.warning("Preencha a descrição da tarefa antes de salvar.")

    st.markdown("---")

    # 3. Leitura dos dados salvos no SQLite
    conn = sqlite3.connect("dados_qg.db")
    try:
        df_kanban = pd.read_sql_query("SELECT id, titulo, categoria, prioridade, status FROM kanban_tarefas", conn)
    except Exception:
        df_kanban = pd.DataFrame()
    finally:
        conn.close()

    # Layout das 3 colunas Kanban
    col_afazer, col_andamento, col_concluido = st.columns(3)

    colunas_map = [
        ("📝 A Fazer", "A Fazer", col_afazer),
        ("⚡ Em Andamento", "Em Andamento", col_andamento),
        ("✅ Concluído", "Concluído", col_concluido)
    ]

    for nome_coluna, status_alvo, col_streamlit in colunas_map:
        with col_streamlit:
            st.subheader(nome_coluna)
            if not df_kanban.empty:
                sub_df = df_kanban[df_kanban["status"] == status_alvo]
                if sub_df.empty:
                    st.caption("Nenhuma tarefa nesta coluna.")
                else:
                    for _, row in sub_df.iterrows():
                        t_id = int(row["id"])
                        with st.expander(f"[{row['categoria']}] {row['titulo']}", expanded=True):
                            st.caption(f"Prioridade: **{row['prioridade']}**")
                            
                            c_m1, c_m2, c_m3 = st.columns(3)
                            
                            # Mover para a esquerda
                            if status_alvo != "A Fazer" and c_m1.button("⬅️", key=f"btn_left_{t_id}"):
                                novo_st = "A Fazer" if status_alvo == "Em Andamento" else "Em Andamento"
                                conn = sqlite3.connect("dados_qg.db")
                                c = conn.cursor()
                                c.execute("UPDATE kanban_tarefas SET status = ? WHERE id = ?", (novo_st, t_id))
                                conn.commit()
                                conn.close()
                                st.rerun()

                            # Excluir tarefa
                            if c_m2.button("🗑️", key=f"btn_del_{t_id}"):
                                conn = sqlite3.connect("dados_qg.db")
                                c = conn.cursor()
                                c.execute("DELETE FROM kanban_tarefas WHERE id = ?", (t_id,))
                                conn.commit()
                                conn.close()
                                st.rerun()

                            # Mover para a direita
                            if status_alvo != "Concluído" and c_m3.button("➡️", key=f"btn_right_{t_id}"):
                                novo_st = "Em Andamento" if status_alvo == "A Fazer" else "Concluído"
                                conn = sqlite3.connect("dados_qg.db")
                                c = conn.cursor()
                                c.execute("UPDATE kanban_tarefas SET status = ? WHERE id = ?", (novo_st, t_id))
                                conn.commit()
                                conn.close()
                                st.rerun()
            else:
                st.caption("Nenhuma tarefa registrada.")
        # -----------------------------------------------------------------------------
# MÓDULO 11: Cronograma Integrado EFA & EsPCEx
# -----------------------------------------------------------------------------
elif menu == "📊 Visualizador de Planilhas":
    st.title("📊 Cronograma Integrado EFA, AMSP & EsPCEx")
    st.caption("Planilha oficial de planeamento carregada diretamente do repositório.")

    NOME_DO_ARQUIVO = "Plano_de_Estudos_EFA_Status_Automatico.xlsx"

    try:
        # Lê a planilha ignorando o cabeçalho decorativo superior (skiprows=3)
        df_plano = pd.read_excel(NOME_DO_ARQUIVO, skiprows=3)
        df_plano = df_plano.dropna(how='all')

        # Resumo e Métricas das Atividades
        total_atividades = len(df_plano)
        concluidas = len(df_plano[df_plano["Status"] == "Concluído"])
        em_andamento = len(df_plano[df_plano["Status"] == "Em Andamento"])
        pendentes = len(df_plano[df_plano["Status"] == "Pendente"])

        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Total de Blocos", total_atividades)
        m2.metric("Concluídos ✅", concluidas)
        m3.metric("Em Andamento ⏳", em_andamento)
        m4.metric("Pendentes 📌", pendentes)

        st.markdown("---")

        # Filtro de navegação por Semana (Escola EFA vs Propriedade/Casa)
        semanas_opcoes = ["Todas as Semanas"] + list(df_plano["Semana"].dropna().unique())
        semana_selecionada = st.selectbox("🔍 Filtrar por Período da Alternância:", semanas_opcoes)

        if semana_selecionada != "Todas as Semanas":
            df_exibicao = df_plano[df_plano["Semana"] == semana_selecionada]
        else:
            df_exibicao = df_plano

        # Exibição da Tabela
        st.dataframe(df_exibicao, use_container_width=True, hide_index=True)

    except FileNotFoundError:
        st.error(f"O ficheiro '{NOME_DO_ARQUIVO}' não foi encontrado no GitHub. Confirme se enviou o ficheiro com este nome exato.")
    except Exception as e:
        st.error(f"Erro ao carregar a planilha: {e}")
     # ====================================================
# MÓDULO 12: SORTEADOR DE TEMAS & TEXTOS BASE (DUPLO)
# ====================================================
elif menu == "🎲 Sorteador de Temas & Textos Base":

    st.header("🎲 Módulo 12: Sorteador Tático de Temas & Textos Motivadores")
    st.markdown("Sorteie propostas completas de redação com **Textos Base Motivadores (Texto I e Texto II)**, eixos temáticos e repertórios para treino.")

    if "banco_temas" not in st.session_state:
        st.session_state.banco_temas = [
            # ----------------------------------------------------
            # EIXO 1: DEFESA NACIONAL & ESTADO
            # ----------------------------------------------------
            {
                "id": 1,
                "tema": "O impacto da inteligência artificial e da automação na segurança e soberania nacional",
                "eixo": "Defesa Nacional & Estado",
                "origem": "Simulado EsPCEx",
                "texto_base_1": "TEXTO I: A transformação digital redefiniu o conceito de defesa estratégica. Drones autônomos, guerra cibernética e processamento de dados em massa tornaram-se pilares do poder militar moderno. O controle sobre tecnologias críticas não é apenas uma questão comercial, mas de sobrevivência e autonomia das Forças Armadas frente às ameaças externas.",
                "texto_base_2": "TEXTO II: Segundo a Estratégia Nacional de Defesa, a independência tecnológica é pressuposto para a garantia da soberania. Países que dependem exclusivamente de insumos e softwares estrangeiros para seus sistemas de controle e comando ficam suscetíveis a bloqueios e vulnerabilidades em cenários de conflito internacional.",
                "repertorios": ["Art. 142 da Constituição Federal", "Conceito de Guerra Híbrida", "Thomas Hobbes (O Leviatã)"]
            },
            {
                "id": 2,
                "tema": "A importância do controle de fronteiras no combate ao crime organizado transnacional",
                "eixo": "Defesa Nacional & Estado",
                "origem": "Prova EsPCEx",
                "texto_base_1": "TEXTO I: O Brasil possui mais de 16 mil quilômetros de fronteira terrestre com dez países. A vastidão do território impõe desafios monumentais no combate ao tráfico de armas, drogas e ao contrabando. A atuação conjunta entre o Exército, a Polícia Federal e agências internacionais é indispensável para conter a expansão de facções criminosas.",
                "texto_base_2": "TEXTO II: Operações interagências em zonas fronteiriças demonstram que a presença ostensiva do Estado inibe a instalação de ilícitos e protege a economia nacional. A integração de dados de inteligência e o monitoramento por radar são vetores indispensáveis nessa salvaguarda.",
                "repertorios": ["Max Weber (Monopólio da Violência Legítima)", "Operações Interagências", "Estratégia Nacional de Defesa"]
            },
            {
                "id": 3,
                "tema": "A atuação das Forças Armadas em missões de garantia da lei e da ordem (GLO) e ajuda humanitária",
                "eixo": "Defesa Nacional & Estado",
                "origem": "Simulado EsPCEx",
                "texto_base_1": "TEXTO I: Para além da defesa contra agressões estrangeiras, o Exército Brasileiro é frequentemente acionado em operações de apoio à população vulnerável, socorro em desastres naturais e apoio suplementar à segurança pública. Essa dupla vocação reforça a integração entre a caserna e a sociedade civil.",
                "texto_base_2": "TEXTO II: O emprego de tropas federais em missões de paz ou socorro humanitário exige preparo técnico, neutralidade e respeito rigoroso aos Direitos Humanos, consolidando a imagem do Exército como instituição de Estado garantidora da estabilidade e do bem-estar social.",
                "repertorios": ["Princípio da Dignidade da Pessoa Humana", "Art. 144 da CF/88", "Ação Própria de Estado"]
            },
            {
                "id": 4,
                "tema": "O papel da indústria nacional de defesa no desenvolvimento tecnológico e econômico do país",
                "eixo": "Defesa Nacional & Estado",
                "origem": "Simulado EsPCEx",
                "texto_base_1": "TEXTO I: A Base Industrial de Defesa (BID) gera empregos de alta qualificação e impulsiona a inovação científica em setores como aeroespacial, bélico e de telecomunicações. Investir em tecnologia militar própria reduz a dependência de potências estrangeiras.",
                "texto_base_2": "TEXTO II: Projetos estratégicos, como o desenvolvimento de submarinos e caças em parceria com a indústria nacional, transbordam conhecimento para o setor civil, estimulando patentes e fortalecendo a matriz industrial e econômica do país.",
                "repertorios": ["Soberania Tecnológica", "Celso Furtado (Desenvolvimento Econômico)", "Tríplice Hélice de Inovação"]
            },
            {
                "id": 5,
                "tema": "Desafios para a vigilância e proteção da Amazônia Azul e dos recursos marítimos nacionais",
                "eixo": "Defesa Nacional & Estado",
                "origem": "Simulado EsPCEx",
                "texto_base_1": "TEXTO I: A Amazônia Azul compreende cerca de 4,5 milhões de km² de área marítima dotada de imensas reservas petrolíferas (Pré-Sal) e biodiversidade marinha. Garantir a patrulha e a soberania sobre essas águas exige investimentos constantes na força naval.",
                "texto_base_2": "TEXTO II: Mais de 95% do comércio exterior brasileiro trafega por vias marítimas. A salvaguarda do tráfego aquaviário e das plataformas continentais é condição direta para a segurança alimentar, energética e comercial da nação.",
                "repertorios": ["Convenção das Nações Unidas sobre o Direito do Mar", "Conceito de Amazônia Azul", "Soberania Econômica"]
            },
            {
                "id": 6,
                "tema": "O serviço militar obrigatório como instrumento de formação cidadã e coesão social",
                "eixo": "Defesa Nacional & Estado",
                "origem": "Simulado EsPCEx",
                "texto_base_1": "TEXTO I: O serviço militar reúne jovens de diversas origens sociais, étnicas e regionais, proporcionando um ambiente de igualdade de deveres e companheirismo sob o mesmo fardamento.",
                "texto_base_2": "TEXTO II: Além do treinamento militar, a incorporação estimula princípios de disciplina, hierarquia, respeito às leis e civismo, atuando como porta de entrada para a qualificação profissional e a consciência social.",
                "repertorios": ["Civismo e Patriotismo", "Émile Durkheim (Fato Social)", "Art. 143 da CF/88"]
            },
            {
                "id": 7,
                "tema": "A diplomacia militar e a cooperação regional na América do Sul",
                "eixo": "Defesa Nacional & Estado",
                "origem": "Simulado EsPCEx",
                "texto_base_1": "TEXTO I: A construção de um entorno regional pacífico passa por exercícios militares conjuntos, intercâmbio em academias de formação e operações combinadas de salvamento na América do Sul.",
                "texto_base_2": "TEXTO II: A confiança mútua entre Forças Armadas vizinhas previne corridas armamentistas desnecessárias e fortalece blocos de negociação internacional no combate a ameaças comuns.",
                "repertorios": ["Carta das Nações Unidas", "Princípio da Não Intervenção", "Conselho de Defesa Sul-Americano"]
            },

            # ----------------------------------------------------
            # EIXO 2: CIBERSEGURANÇA & TECNOLOGIA
            # ----------------------------------------------------
            {
                "id": 8,
                "tema": "A vulnerabilidade das infraestruturas críticas nacionais diante de ataques cibernéticos",
                "eixo": "Cibersegurança & Tecnologia",
                "origem": "Simulado EsPCEx",
                "texto_base_1": "TEXTO I: Redes elétricas, sistemas bancários, abastecimento de água e redes de telecomunicação dependem diretamente de sistemas digitais. Ataques promovidos por hackers ou grupos estatais estrangeiros podem paralisar serviços essenciais e comprometer a segurança da nação.",
                "texto_base_2": "TEXTO II: A criação do Comando de Defesa Cibernética é resposta à necessidade de blindar dados estratégicos do Estado. No entanto, a formação continuada de especialistas e o uso de criptografia própria são urgentes diante de invasões sofisticadas.",
                "repertorios": ["Comando de Defesa Cibernética (ComDCiber)", "Zygmunt Bauman (Sociedade de Risco)", "Marco Civil da Internet"]
            },
            {
                "id": 9,
                "tema": "A desinformação e as fake news como ameaças à estabilidade democrática e social",
                "eixo": "Cibersegurança & Tecnologia",
                "origem": "Prova EsPCEx",
                "texto_base_1": "TEXTO I: A difusão em massa de notícias falsas por algoritmos e redes sociais manipula a opinião pública, corrói a confiança nas instituições legítimas e acirra a polarização social. Garantir a liberdade de expressão sem permitir o caos informativo é um dilema central.",
                "texto_base_2": "TEXTO II: A desinformação em momentos de crise sanitária ou eleitoral compromete a coesão do tecido social. A educação de mídias e a responsabilização jurídica das plataformas são caminhos fundamentais de enfrentamento.",
                "repertorios": ["Hannah Arendt (Verdade e Política)", "Guy Debord (A Sociedade do Espetáculo)", "LGPD"]
            },
            {
                "id": 10,
                "tema": "Os limites éticos do desenvolvimento e uso da inteligência artificial na sociedade",
                "eixo": "Cibersegurança & Tecnologia",
                "origem": "Simulado EsPCEx",
                "texto_base_1": "TEXTO I: O avanço acelerado dos algoritmos preditivos levanta questionamentos sobre privacidade, viés discriminatório e perda de postos de trabalho. A regulamentação da inteligência artificial busca equilibrar a inovação com a preservação dos direitos humanos.",
                "texto_base_2": "TEXTO II: Sistemas autônomos de tomada de decisão não possuem discernimento moral. Delegar escolhas de vida, justiça ou defesa a máquinas sem supervisão humana direta fere o princípio da responsabilidade ética.",
                "repertorios": ["Immanuel Kant (Humanidade como Fim)", "Nick Bostrom (Superinteligência)", "Direito à Privacidade"]
            },
            {
                "id": 11,
                "tema": "O impacto da hiperconexão na saúde mental e nas relações interpessoais da juventude",
                "eixo": "Cibersegurança & Tecnologia",
                "origem": "Simulado EsPCEx",
                "texto_base_1": "TEXTO I: O uso excessivo de telas e redes sociais tem gerado isolamento, ansiedade e dependência digital entre adolescentes. A busca por validação virtual contínua substitui a convivência presencial e afeta diretamente a capacidade de concentração.",
                "texto_base_2": "TEXTO II: A cultura do imediatismo gerada pelas notificações em tempo real fragiliza a paciência necessária para o estudo profundo e para a construção de relacionamentos interpessoais sólidos e resilientes.",
                "repertorios": ["Byung-Chul Han (A Sociedade do Cansaço)", "Conceito de Solidão Digital", "OMS (Saúde Mental)"]
            },
            {
                "id": 12,
                "tema": "A inclusão digital como fator determinante para o combate às desigualdades sociais",
                "eixo": "Cibersegurança & Tecnologia",
                "origem": "Simulado EsPCEx",
                "texto_base_1": "TEXTO I: O acesso à internet de alta velocidade e a dispositivos computacionais deixou de ser luxo para se tornar requisito de cidadania, educação e oportunidade de trabalho. A exclusão digital perpetua o ciclo de pobreza em regiões periféricas e rurais.",
                "texto_base_2": "TEXTO II: A alfabetização digital deve caminhar junto à infraestrutura de redes. Sem capacitação para usar as ferramentas com senso crítico e produtividade, o hiato social entre classes continuará a se expandir.",
                "repertorios": ["Gilberto Dimenstein (O Cidadão de Papel)", "Teoria do Capital Cultural (Bourdieu)", "Art. 205 da CF/88"]
            },
            {
                "id": 13,
                "tema": "A proteção de dados pessoais e o direito à privacidade na era da informação",
                "eixo": "Cibersegurança & Tecnologia",
                "origem": "Simulado EsPCEx",
                "texto_base_1": "TEXTO I: A rastreabilidade contínua de hábitos, preferências e dados biométricos transforma o indivíduo em produto mercadológico na economia de dados.",
                "texto_base_2": "TEXTO II: Legislações como a LGPD representam marcos de proteção civil, exigindo consentimento explícito e transparência de empresas e do governo no manuseio de informações sensíveis.",
                "repertorios": ["LGPD", "Panóptico de Jeremy Bentham", "Direito Fundamental à Privacidade"]
            },

            # ----------------------------------------------------
            # EIXO 3: MEIO AMBIENTE & SUSTENTABILIDADE
            # ----------------------------------------------------
            {
                "id": 14,
                "tema": "O desafio do combate aos crimes ambientais e a preservação da Amazônia Legal",
                "eixo": "Meio Ambiente & Sustentabilidade",
                "origem": "Simulado EsPCEx",
                "texto_base_1": "TEXTO I: O desmatamento ilegal, o garimpo clandestino e a grilagem de terras ameaçam a biodiversidade e o equilíbrio climático global. A presença das Forças Armadas e dos órgãos de fiscalização é vital para sufocar o crime organizado ambiental.",
                "texto_base_2": "TEXTO II: A bioeconomia sustentável e o fortalecimento de pesquisas científicas locais oferecem alternativas de desenvolvimento econômico digno para as populações tradicionais da floresta sem destruição.",
                "repertorios": ["Hans Jonas (O Princípio Responsabilidade)", "Art. 225 da CF/88", "Soberania Ambiental"]
            },
            {
                "id": 15,
                "tema": "A transição energética e o papel do Brasil na matriz limpa mundial",
                "eixo": "Meio Ambiente & Sustentabilidade",
                "origem": "Simulado EsPCEx",
                "texto_base_1": "TEXTO I: Frente ao aquecimento global, a substituição de combustíveis fósseis por fontes renováveis é urgente. O Brasil destaca-se mundialmente por sua matriz energética diversificada e limpa, podendo liderar a economia verde internacional.",
                "texto_base_2": "TEXTO II: O investimento em biocombustíveis, energia eólica, solar e hidrogênio verde atrai capital estrangeiro e reduz a pegada de carbono do setor produtivo e de transportes.",
                "repertorios": ["Acordo de Paris", "Conceito de Desenvolvimento Sustentável", "Agenda 2030 da ONU"]
            },
            {
                "id": 16,
                "tema": "A gestão dos recursos hídricos e a segurança alimentar diante das mudanças climáticas",
                "eixo": "Meio Ambiente & Sustentabilidade",
                "origem": "Simulado EsPCEx",
                "texto_base_1": "TEXTO I: Eventos climáticos extremos, como secas prolongadas e enchentes devastadoras, impactam diretamente a produção agrícola e o abastecimento de água. A sustentabilidade no campo e a preservação dos mananciais são cruciais.",
                "texto_base_2": "TEXTO II: A tecnologia de irrigação eficiente, o reflorestamento de matas ciliares e o combate ao desperdício são alavancas para assegurar a comida na mesa sem esgotar as bacias hidrográficas.",
                "repertorios": ["Josué de Castro (Geopolítica da Fome)", "Código Florestal Brasileiro", "Embrapa e Tecnologia Agrícola"]
            },
            {
                "id": 17,
                "tema": "O dilema entre o crescimento econômico e a conservação do patrimônio natural",
                "eixo": "Meio Ambiente & Sustentabilidade",
                "origem": "Simulado EsPCEx",
                "texto_base_1": "TEXTO I: O avanço das fronteiras agrícolas e industriais é essencial para a geração de empregos. Contudo, a exploração desenfreada sem manejo sustentável esgota os recursos naturais e compromete a qualidade de vida das gerações futuras.",
                "texto_base_2": "TEXTO II: O conceito de desenvolvimento sustentável prova que preservação e rentabilidade não são antagônicos, desde que pautados por licenciamento sério e compensação ambiental.",
                "repertorios": ["Relatório Brundtland (1987)", "Desenvolvimento Sustentável", "Paul Crutzen (Antropoceno)"]
            },
            {
                "id": 18,
                "tema": "A gestão de resíduos sólidos e a responsabilidade socioambiental nas cidades brasileiras",
                "eixo": "Meio Ambiente & Sustentabilidade",
                "origem": "Simulado EsPCEx",
                "texto_base_1": "TEXTO I: O descarte inadequado do lixo, a proliferação de lixões clandestinos e o baixo índice de reciclagem geram impactos severos à saúde pública e ao meio ambiente urbano. A logística reversa e o apoio aos catadores são medidas urgentes.",
                "texto_base_2": "TEXTO II: A transição para a economia circular exige engajamento do consumidor, incentivos fiscais para embalagens biodegradáveis e coleta seletiva eficiente nas periferias.",
                "repertorios": ["Política Nacional de Resíduos Sólidos", "Conceito de Economia Circular", "Saneamento Básico como Direito"]
            },
            {
                "id": 19,
                "tema": "O papel da agricultura de precisão e das agrotecnologias na preservação ambiental",
                "eixo": "Meio Ambiente & Sustentabilidade",
                "origem": "Simulado EsPCEx",
                "texto_base_1": "TEXTO I: O uso de drones, sensores de solo e mapeamento por satélite otimiza a aplicação de insumos e adubos, reduzindo o desperdício e protegendo o lençol freático.",
                "texto_base_2": "TEXTO II: Produzir mais na mesma área reduz a pressão sobre a abertura de novas vegetações nativas, aliando produtividade no campo e proteção biológica.",
                "repertorios": ["Inovação Agrícola", "Código Florestal", "Sustentabilidade no Campo"]
            },

            # ----------------------------------------------------
            # EIXO 4: CIDADANIA & ÉTICA
            # ----------------------------------------------------
            {
                "id": 20,
                "tema": "A relevância do civismo e dos valores éticos na formação da juventude brasileira",
                "eixo": "Cidadania & Ética",
                "origem": "Prova EsPCEx",
                "texto_base_1": "TEXTO I: O civismo, o respeito aos símbolos nacionais e a consciência dos deveres sociais são fundamentos da coesão de um país. Estimular valores como disciplina, responsabilidade coletiva e patriotismo fortalece a democracia.",
                "texto_base_2": "TEXTO II: Cidadania plena envolve não apenas usufruir de direitos, mas praticar ativamente a ética no cotidiano, respeitando as leis e colaborando para a melhoria da comunidade.",
                "repertorios": ["Immanuel Kant (Imperativo Categórico)", "Émile Durkheim (Fato Social e Moral)", "LDB - Lei de Diretrizes e Bases"]
            },
            {
                "id": 21,
                "tema": "O papel da educação no combate à intolerância e à cultura da violência na sociedade",
                "eixo": "Cidadania & Ética",
                "origem": "Simulado EsPCEx",
                "texto_base_1": "TEXTO I: O aumento de episódios de discurso de ódio e agressões interpessoais evidencia a necessidade de uma formação educacional pautada no diálogo, na empatia e no cumprimento das leis. A escola deve ser um espaço de mediação de conflitos.",
                "texto_base_2": "TEXTO II: Educar para a cultura da paz exige desconstruir preconceitos e formar indivíduos conscientes do valor do respeito mútuo e da solução pacífica de divergências.",
                "repertorios": ["Paulo Freire (Educação como Prática da Liberdade)", "Declaração Universal dos Direitos Humanos", "Conceito de Alteridade"]
            },
            {
                "id": 22,
                "tema": "A importância do voluntariado e do espírito de solidariedade em momentos de crise nacional",
                "eixo": "Cidadania & Ética",
                "origem": "Simulado EsPCEx",
                "texto_base_1": "TEXTO I: Durante grandes calamidades públicas e crises sociais, a mobilização da sociedade civil em ações comunitárias e doações complementa a atuação estatal. A cultura do voluntariado demonstra a força da empatia coletiva.",
                "texto_base_2": "TEXTO II: Engajar-se voluntariamente fortalece os laços comunitários e desenvolve o senso de responsabilidade social, transformando o cidadão em agente direto de mudança.",
                "repertorios": ["Zygmunt Bauman (Comunidade)", "Princípio da Solidariedade Constitucional", "Ação Social Organizacional"]
            },
            {
                "id": 23,
                "tema": "A valorização do trabalho e o desafio do primeiro emprego para os jovens no Brasil",
                "eixo": "Cidadania & Ética",
                "origem": "Simulado EsPCEx",
                "texto_base_1": "TEXTO I: A transição da escola para o mercado de trabalho é marcada por exigências crescentes de qualificação técnica e experiência prévia. Programas de aprendizagem, ensino técnico e incentivos governamentais são fundamentais.",
                "texto_base_2": "TEXTO II: O trabalho formal na juventude é vetor de dignidade e autonomia, prevenindo a ociosidade e fornecendo perspectivas reais de crescimento social e profissional.",
                "repertorios": ["Lei do Aprendiz", "Constituição Federal (Direito ao Trabalho)", "Teoria do Capital Humano"]
            },
            {
                "id": 24,
                "tema": "A preservação da memória histórica e da identidade cultural como pilares de uma nação",
                "eixo": "Cidadania & Ética",
                "origem": "Simulado EsPCEx",
                "texto_base_1": "TEXTO I: O conhecimento da história, das tradições e dos heróis nacionais permite compreender o presente e projetar o futuro. Um país que desvaloriza seu patrimônio cultural corre o risco de perder sua identidade e coesão social.",
                "texto_base_2": "TEXTO II: Preservar acervos, monumentos e datas históricas não é apego ao passado, mas compromisso com a continuidade dos valores e símbolos que unificam o povo.",
                "repertorios": ["Patrimônio Histórico Nacional (IPHAN)", "Marc Bloch (A Apologia da História)", "Identidade Nacional (Ernest Renan)"]
            },
            {
                "id": 25,
                "tema": "A ética no serviço público como garantia de eficiência e respeito ao cidadão",
                "eixo": "Cidadania & Ética",
                "origem": "Simulado EsPCEx",
                "texto_base_1": "TEXTO I: Servidores públicos investidos de autoridade devem agir estritamente pautados pela impessoalidade, moralidade e transparência em seus atos administrativos.",
                "texto_base_2": "TEXTO II: A eficiência na prestação de serviços essenciais como saúde, segurança e educação é o reflexo direto de um corpo funcional comprometido com a ética pública.",
                "repertorios": ["Art. 37 da Constituição Federal (LIMPE)", "Aristóteles (Ética a Nicômaco)", "Transparência Pública"]
            }
        ]

    if "tema_sorteado" not in st.session_state:
        st.session_state.tema_sorteado = None

    c_eixo, c_origem, c_btn = st.columns([2, 2, 1.5])

    with c_eixo:
        eixo_filtro = st.selectbox(
            "Filtrar Eixo Temático:",
            ["Todos os Eixos", "Defesa Nacional & Estado", "Cibersegurança & Tecnologia", "Meio Ambiente & Sustentabilidade", "Cidadania & Ética"]
        )

    with c_origem:
        origem_filtro = st.selectbox(
            "Filtrar Origem:",
            ["Todas as Origens", "Prova EsPCEx", "Simulado EsPCEx"]
        )

    with c_btn:
        st.write(" ")
        st.write(" ")
        if st.button("🎲 Sortear Tema Agora", use_container_width=True):
            lista_filtrada = st.session_state.banco_temas
            if eixo_filtro != "Todos os Eixos":
                lista_filtrada = [t for t in lista_filtrada if t["eixo"] == eixo_filtro]
            if origem_filtro != "Todas as Origens":
                lista_filtrada = [t for t in lista_filtrada if t["origem"] == origem_filtro]

            if lista_filtrada:
                st.session_state.tema_sorteado = random.choice(lista_filtrada)
            else:
                st.warning("Nenhum tema encontrado com estes filtros.")

    if st.session_state.tema_sorteado:
        t = st.session_state.tema_sorteado
        st.markdown("---")
        st.subheader("📌 Proposta de Redação Sorteada")

        st.info(f"### 📝 Proposta: \"{t['tema']}\"")
        st.write(f"**Eixo:** {t['eixo']} | **Origem:** {t['origem']} | **ID:** #{t['id']}")

        with st.expander("📜 Textos Base Motivadores (Clique para ler)", expanded=True):
            st.markdown(f"**TEXTO I**\n\n{t['texto_base_1']}")
            st.markdown("---")
            st.markdown(f"**TEXTO II**\n\n{t['texto_base_2']}")
            st.caption("A partir da leitura dos textos motivadores e dos seus conhecimentos construídos ao longo de sua formação, redija um texto dissertativo-argumentativo na modalidade escrita culta da língua portuguesa.")

        st.markdown("#### 💡 Repertórios Coringa Sugeridos:")
        for rep in t["repertorios"]:
            st.success(f"📌 {rep}")

    st.markdown("---")
    with st.expander("➕ Cadastrar Novo Tema com Textos Base ao Banco"):
        with st.form("form_novo_tema_completo"):
            n_tema = st.text_input("Título da Proposta de Redação")
            n_eixo = st.selectbox("Eixo Temático", ["Defesa Nacional & Estado", "Cibersegurança & Tecnologia", "Meio Ambiente & Sustentabilidade", "Cidadania & Ética"])
            n_origem = st.text_input("Origem (Ex: Prova 2024, Simulado 05)", value="Simulado EsPCEx")
            n_texto_1 = st.text_area("Texto Base I (Motivador 1)")
            n_texto_2 = st.text_area("Texto Base II (Motivador 2)")
            n_reps = st.text_input("Repertórios Sugeridos (separados por vírgula)")

            btn_salvar_tema = st.form_submit_button("Salvar Tema no Banco")

            if btn_salvar_tema and n_tema and n_texto_1 and n_texto_2:
                lista_r = [r.strip() for r in n_reps.split(",") if r.strip()]
                st.session_state.banco_temas.append({
                    "id": len(st.session_state.banco_temas) + 1,
                    "tema": n_tema,
                    "eixo": n_eixo,
                    "origem": n_origem,
                    "texto_base_1": n_texto_1,
                    "texto_base_2": n_texto_2,
                    "repertorios": lista_r if lista_r else ["Repertório Geral do Eixo"]
                })
                st.success("Novo tema cadastrado com sucesso no Módulo 12!")
                st.rerun()

# ====================================================
# MÓDULO 13: PACER TAF
# ====================================================
elif menu == "🏃 Módulo 13: Pacer TAF":
    
    import pandas as pd
    import numpy as np
    import plotly.graph_objects as go
    import datetime

    # ==============================================================================
    # MOTOR DE RENDERIZAÇÃO: VELOCÍMETRO TÁTICO (GAUGE CHART UNIFICADO)
    # ==============================================================================
    def criar_velocimetro(valor, max_valor, titulo, cor_barra, limiar=None):
        fig = go.Figure(go.Indicator(
            mode="gauge+number",
            value=valor,
            title={'text': titulo, 'font': {'color': '#a1aabf', 'size': 16, 'family': 'sans-serif'}},
            number={'font': {'size': 45, 'color': cor_barra, 'family': 'Courier New'}},
            gauge={
                'shape': "angular",
                'axis': {'range': [0, max_valor], 'tickwidth': 1, 'tickcolor': "#4b5563"},
                'bar': {'color': cor_barra, 'thickness': 0.35},
                'bgcolor': "#0d1117",
                'borderwidth': 0,
                'steps': [
                    {'range': [0, max_valor*0.5], 'color': '#111827'}, 
                    {'range': [max_valor*0.5, max_valor], 'color': '#1f2937'}
                ]
            }
        ))
        if limiar:
            fig.data[0].gauge.threshold = {'line': {'color': "#ffffff", 'width': 3}, 'thickness': 0.75, 'value': limiar}
        
        fig.update_layout(
            paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)", 
            height=280, margin=dict(l=20, r=20, t=50, b=20)
        )
        return fig

    # ==============================================================================
    # 1. FRONTEND: CSS ENTERPRISE 3D + SPOTIFY ULTRA PREMIUM
    # ==============================================================================
    st.markdown("""
        <style>
        .main { background-color: #0d1117; } 
        @keyframes smoothAppear { 0% { opacity: 0; transform: translateY(50px) scale(0.98); } 100% { opacity: 1; transform: translateY(0) scale(1); } }
        .hero-banner-3d { perspective: 1500px; margin-bottom: 40px; animation: smoothAppear 1.2s cubic-bezier(0.23, 1, 0.32, 1) forwards; }
        .hero-content { background: linear-gradient(135deg, rgba(15, 23, 42, 0.95) 0%, rgba(5, 8, 15, 0.98) 100%); border: 1px solid rgba(0, 242, 254, 0.2); border-top: 3px solid #00f2fe; border-bottom: 3px solid #00f2fe; border-radius: 20px; padding: 40px 30px; text-align: center; transform-style: preserve-3d; transform: rotateX(4deg); box-shadow: 0 30px 60px rgba(0,0,0,0.8), inset 0 0 40px rgba(0, 242, 254, 0.05); transition: all 0.6s cubic-bezier(0.23, 1, 0.32, 1); }
        .hero-content:hover { transform: rotateX(0deg) translateZ(40px); box-shadow: 0 40px 80px rgba(0,242,254,0.15), inset 0 0 60px rgba(0, 242, 254, 0.1); border-color: #00ff88; }
        .hero-google-badge { display: inline-block; background: rgba(255,255,255,0.03); border: 1px solid rgba(255,255,255,0.1); padding: 8px 20px; border-radius: 30px; font-size: 0.85rem; color: #ffffff; text-shadow: 1px 1px 2px #000; transform: translateZ(50px); margin-bottom: 15px; box-shadow: 0 5px 15px rgba(0,0,0,0.5); }
        .hero-google-badge span { background: linear-gradient(90deg, #4285F4, #EA4335, #FBBC05, #34A853); -webkit-background-clip: text; -webkit-text-fill-color: transparent; font-weight: 900; letter-spacing: 1px; }
        .hero-title { color: #ffffff; font-size: 3.8rem; font-weight: 900; letter-spacing: 6px; margin: 0 0 5px 0; transform: translateZ(90px); line-height: 1.1; text-shadow: 1px 1px 0 #005c7a, 2px 2px 0 #004d66, 3px 3px 0 #003d52, 4px 4px 0 #002e3d, 5px 5px 0 #001f29, 6px 6px 15px rgba(0, 242, 254, 0.5); }
        .hero-subtitle { color: #00f2fe; font-size: 1.1rem; font-weight: 900; letter-spacing: 3px; text-transform: uppercase; margin-bottom: 25px; transform: translateZ(60px); text-shadow: 1px 1px 0 #004d66, 2px 2px 0 #002e3d; }
        .hero-status { font-family: 'Courier New', monospace; color: #fff; font-size: 1rem; font-weight: bold; background: #000; display: inline-block; padding: 8px 25px; border-radius: 20px; border: 1px solid #333; transform: translateZ(50px); box-shadow: 2px 2px 0 #222; }
        .hero-meanings { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 15px; margin-top: 35px; transform: translateZ(30px); }
        .meaning-item { background: rgba(255, 255, 255, 0.04); border-left: 3px solid #3a3f4e; padding: 15px; font-size: 0.9rem; color: #e2e8f0; text-shadow: 1px 1px 2px #000; text-align: left; border-radius: 6px; transition: all 0.3s; }
        .meaning-item b { color: #00f2fe; font-weight: 900; letter-spacing: 1.5px; font-size: 1rem; display: block; margin-bottom: 5px; text-shadow: 1px 1px 0 #004d66, 2px 2px 0 #002e3d;}
        .meaning-item:hover { background: rgba(0, 242, 254, 0.1); border-left-color: #00f2fe; transform: translateX(10px) translateZ(10px); color: #fff; }
        div[data-baseweb="tab-list"] { border-bottom: none; gap: 10px; flex-wrap: wrap; justify-content: center; margin-bottom: 20px; }
        div[data-baseweb="tab"] { background: #15181e; color: #a1aabf; font-weight: 900; font-size: 1rem; padding: 12px 25px; border-radius: 8px; border: 1px solid #222; transition: all 0.3s; }
        div[aria-selected="true"] { background: linear-gradient(180deg, rgba(0, 242, 254, 0.15), transparent) !important; color: #fff !important; border: 1px solid #00f2fe !important; box-shadow: 0 0 20px rgba(0,242,254,0.3) !important; text-shadow: 1px 1px 2px #000; }
        
        /* --- ESTILOS DO SPOTIFY ULTRA PREMIUM --- */
        .spotify-ultra-container {
            position: relative;
            background: linear-gradient(135deg, rgba(29, 185, 84, 0.1) 0%, rgba(5, 8, 15, 0.95) 100%);
            border: 1px solid rgba(29, 185, 84, 0.3);
            border-top: 3px solid #1DB954;
            border-radius: 24px;
            padding: 40px;
            box-shadow: 0 20px 60px rgba(0,0,0,0.9), inset 0 0 80px rgba(29, 185, 84, 0.05);
            text-align: center;
            overflow: hidden;
            animation: smoothAppear 1s cubic-bezier(0.23, 1, 0.32, 1) forwards;
        }
        .spotify-ultra-container::before {
            content: ''; position: absolute; top: -50%; left: -50%; width: 200%; height: 200%;
            background: radial-gradient(circle, rgba(29, 185, 84, 0.15) 0%, transparent 50%);
            animation: rotateGlow 12s linear infinite; z-index: 0; pointer-events: none;
        }
        @keyframes rotateGlow { 0% { transform: rotate(0deg); } 100% { transform: rotate(360deg); } }
        .spotify-ultra-content { position: relative; z-index: 1; }
        
        .spotify-title {
            color: #fff; font-size: 2.8rem; font-weight: 900; letter-spacing: 4px; margin: 0 0 15px 0;
            text-shadow: 0 0 20px rgba(29, 185, 84, 0.6), 2px 2px 0px #000;
            display: flex; justify-content: center; align-items: center; gap: 15px;
        }
        .spotify-title span { color: #1DB954; }
        .spotify-desc { color: #b3b3b3; font-size: 1.1rem; margin-bottom: 35px; font-weight: 800; letter-spacing: 1px; }
        
        .eq-container { display: flex; align-items: flex-end; justify-content: center; height: 35px; gap: 4px; margin-bottom: 25px; }
        .eq-bar { width: 6px; background: #1DB954; border-radius: 4px; animation: equalize 1s ease-in-out infinite alternate; box-shadow: 0 0 10px #1DB954; }
        .eq-bar:nth-child(1) { height: 15px; animation-delay: 0.1s; }
        .eq-bar:nth-child(2) { height: 30px; animation-delay: 0.3s; }
        .eq-bar:nth-child(3) { height: 20px; animation-delay: 0.0s; }
        .eq-bar:nth-child(4) { height: 35px; animation-delay: 0.4s; }
        .eq-bar:nth-child(5) { height: 10px; animation-delay: 0.2s; }
        @keyframes equalize { 0% { height: 8px; } 100% { height: 35px; } }
        
        .spotify-iframe-wrapper {
            border-radius: 20px; overflow: hidden; border: 2px solid rgba(29, 185, 84, 0.4);
            box-shadow: 0 15px 50px rgba(0,0,0,0.9), 0 0 40px rgba(29, 185, 84, 0.2); background: #000;
            transition: all 0.4s; position: relative; z-index: 2;
        }
        .spotify-iframe-wrapper:hover { transform: scale(1.01) translateZ(20px); border-color: #1DB954; box-shadow: 0 20px 60px rgba(0,0,0,0.9), 0 0 60px rgba(29, 185, 84, 0.4); }
        </style>
    """, unsafe_allow_html=True)

    agora = datetime.datetime.now().strftime("%H:%M:%S")
    st.markdown(f"""
        <div class="hero-banner-3d">
            <div class="hero-content">
                <div class="hero-google-badge">Operação suportada pelo <span>Google AI Plus</span></div>
                <h1 class="hero-title">NÚCLEO DE INTELIGÊNCIA</h1>
                <div class="hero-subtitle">Módulo de Preparação Física e Tática — EsPCEx</div>
                <div class="hero-status"><span style="color:#00ff88;">●</span> OPERADOR: ARRUDA | ⏱️ {agora} | SENSOR DE PROFUNDIDADE ATIVO</div>
                <div class="hero-meanings">
                    <div class="meaning-item"><b>⛰️ BIOMETRIA OCUPACIONAL</b> Cálculo de bateria residual baseada no desgaste do trabalho rural em montanha.</div>
                    <div class="meaning-item"><b>🎯 PACER VETORIAL</b> Simulação matemática de ritmo balístico para a prova de corrida de 12 minutos.</div>
                    <div class="meaning-item"><b>💪 TRAÇÃO & CORE</b> Projeção neuromuscular de fadiga para superação das provas de barra fixa e abdominal.</div>
                    <div class="meaning-item"><b>🏋️ MECÂNICA E CARGA</b> Análise de tonelagem física e carga relativa corporal nas flexões de braço.</div>
                    <div class="meaning-item"><b>🌊 NATAÇÃO TÁTICA</b> Monitoramento de resistência hidrodinâmica e tempo subaquático (50 metros).</div>
                    <div class="meaning-item"><b>🫀 ZONAS VAM</b> Cálculo avançado de limiar anaeróbico e consumo máximo de oxigênio (VO2 Máx).</div>
                    <div class="meaning-item"><b>🎒 MARCHA TÁTICA</b> Simulação topográfica e desgaste metabólico de progressão com mochila na selva.</div>
                    <div class="meaning-item"><b>🛡️ CLEARANCE ESPCEX</b> Sistema gamificado de Níveis de Acesso Tático e matriz de prontidão global.</div>
                    <div class="meaning-item"><b>🎵 ÁUDIO TFM</b> Player nativo Spotify integrado na interface de inteligência.</div>
                </div>
            </div>
        </div>
    """, unsafe_allow_html=True)

    tabs = st.tabs(["⛰️ Ocupacional", "🏃 Pacer", "💪 Tração", "🏋️ Mecânica", "🏊 Natação", "⚡ VAM", "🎒 Marcha", "🛡️ Clearance", "🎵 Áudio TFM"])

    # ==============================================================================
    # ABA 1: BIOMETRIA OCUPACIONAL
    # ==============================================================================
    with tabs[0]:
        col_o1, col_o2 = st.columns([1, 2.5])
        with col_o1:
            horas_roca = st.slider("Horas Trabalhadas (Café)", 0.0, 12.0, 8.0, 0.5)
            inclinacao_terreno = st.slider("Inclinação da Montanha (Graus)", 0, 45, 20)
            fator_esforco = 1.0 + (inclinacao_terreno / 100)
            bateria_drenada = min((horas_roca / 10.0) * fator_esforco * 100, 95)
            bateria_residual = 100 - bateria_drenada
            cor_velocimetro = "#00ff88"
            if bateria_residual < 40: cor_velocimetro = "#00f2fe"
            if bateria_residual < 15: cor_velocimetro = "#ff2b6d"
        with col_o2:
            st.plotly_chart(criar_velocimetro(bateria_residual, 100, "🔋 BATERIA RESIDUAL (%)", cor_velocimetro), use_container_width=True)
            
        st.markdown("---")
        x_m = np.linspace(-5, 5, 40); y_m = np.linspace(-5, 5, 40); X_m, Y_m = np.meshgrid(x_m, y_m)
        Z_m = np.exp(-(X_m**2 + Y_m**2)/10) * (inclinacao_terreno * 2) + np.random.normal(0, 0.5, X_m.shape)
        fig_b = go.Figure(data=[go.Surface(z=Z_m, x=X_m, y=Y_m, colorscale='Viridis', opacity=0.95, lighting=dict(roughness=0.9))])
        fig_b.update_layout(scene=dict(xaxis_title='Vetor X', yaxis_title='Vetor Y', zaxis_title='Altímetro (m)', xaxis=dict(gridcolor="#1e2430", backgroundcolor="#0d1117"), yaxis=dict(gridcolor="#1e2430", backgroundcolor="#0d1117"), zaxis=dict(gridcolor="#1e2430", backgroundcolor="#0d1117"), camera=dict(eye=dict(x=1.8, y=-1.8, z=0.5))), margin=dict(l=0, r=0, b=0, t=0), paper_bgcolor="#0d1117", plot_bgcolor="#0d1117", height=500)
        st.plotly_chart(fig_b, use_container_width=True, theme=None)

    # ==============================================================================
    # ABA 2: CORRIDA PACER
    # ==============================================================================
    with tabs[1]:
        col1, col2 = st.columns([1, 2.5])
        with col1:
            distancia_meta = st.number_input("Meta de Distância (m)", 1000, 4000, 2450, 50, key="c1")
            tamanho_pista = st.number_input("Tamanho da Pista (m)", 100, 1000, 400, 50, key="c2")
            vel_media_kmh = (distancia_meta / 1000) / (12.0 / 60)
        with col2:
            st.plotly_chart(criar_velocimetro(vel_media_kmh, 22, "🔥 VELOCIDADE MÉDIA (KM/H)", "#00f2fe", 12.25), use_container_width=True)
            
        st.markdown("---")
        sx = np.arange(1, 9); dy = min(2000, distancia_meta - 450) + (((distancia_meta + 100 - min(2000, distancia_meta - 450)) / 7) * (sx - 1)); vz = (dy / 1000) / (12 / 60)
        fig_p = go.Figure(data=[go.Scatter3d(x=sx, y=dy, z=vz, mode='lines+markers', marker=dict(size=12, color=vz, colorscale='Teal', opacity=1.0, line=dict(color='white', width=2)), line=dict(color='rgba(0, 242, 254, 0.8)', width=8))])
        fig_p.update_layout(scene=dict(xaxis=dict(gridcolor="#1e2430", backgroundcolor="#0d1117"), yaxis=dict(gridcolor="#1e2430", backgroundcolor="#0d1117"), zaxis=dict(gridcolor="#1e2430", backgroundcolor="#0d1117"), camera=dict(eye=dict(x=1.8, y=-1.8, z=0.5))), margin=dict(l=0, r=0, b=0, t=0), paper_bgcolor="#0d1117", plot_bgcolor="#0d1117", height=500)
        st.plotly_chart(fig_p, use_container_width=True, theme=None)

    # ==============================================================================
    # ABA 3: BARRA E ABDOMINAL
    # ==============================================================================
    with tabs[2]:
        col_f1, col_f2 = st.columns(2)
        with col_f1: 
            barras = st.number_input("Barras Máximas Realizadas", 0, 30, 8)
            st.plotly_chart(criar_velocimetro(barras, 20, "💪 BARRAS (REPS)", "#00ff88", 3), use_container_width=True)
        with col_f2: 
            abdominais = st.number_input("Abdominais (1 min)", 0, 100, 45)
            st.plotly_chart(criar_velocimetro(abdominais, 80, "🛡️ ABDOMINAIS (REPS)", "#00f2fe", 30), use_container_width=True)

        st.markdown("---")
        xf, yf = np.meshgrid(np.linspace(1, 6, 20), np.linspace(1, 8, 20)); zf = np.clip(15 + (xf ** 1.9) * 6 - (np.log(yf + 1) * 12), 0, 100)
        fig_f = go.Figure(data=[go.Surface(z=zf, x=xf, y=yf, colorscale='Teal', opacity=1.0, lighting=dict(roughness=0.1, specular=1.5))])
        fig_f.update_layout(scene=dict(xaxis=dict(gridcolor="#1e2430", backgroundcolor="#0d1117"), yaxis=dict(gridcolor="#1e2430", backgroundcolor="#0d1117"), zaxis=dict(gridcolor="#1e2430", backgroundcolor="#0d1117")), margin=dict(l=0, r=0, b=0, t=0), paper_bgcolor="#0d1117", plot_bgcolor="#0d1117", height=400)
        st.plotly_chart(fig_f, use_container_width=True, theme=None)

    # ==============================================================================
    # ABA 4: MECÂNICA
    # ==============================================================================
    with tabs[3]:
        col_flex1, col_flex2 = st.columns([1, 2.5])
        with col_flex1: 
            flexoes = st.number_input("Flexões de Braço Realizadas", 0, 80, 25)
            peso_flex = st.number_input("Seu Peso Corporal (kg)", 40.0, 120.0, 68.0)
            tonelagem = ((peso_flex * 0.64) * flexoes) / 1000
        with col_flex2:
            st.plotly_chart(criar_velocimetro(tonelagem, 5, "💥 TONELAGEM MOVIMENTADA (TONS)", "#ff2b6d"), use_container_width=True)

        st.markdown("---")
        reps_x = np.arange(1, flexoes + 1) if flexoes > 0 else np.array([0]); cadencia_y = np.linspace(1, 1.5, len(reps_x)); esforco_z = reps_x * cadencia_y
        fig_fl = go.Figure(data=[go.Scatter3d(x=reps_x, y=cadencia_y, z=esforco_z, mode='markers', marker=dict(size=10, color=esforco_z, colorscale='Electric', opacity=0.9, symbol='diamond'))])
        fig_fl.update_layout(scene=dict(xaxis=dict(gridcolor="#1e2430", backgroundcolor="#0d1117"), yaxis=dict(gridcolor="#1e2430", backgroundcolor="#0d1117"), zaxis=dict(gridcolor="#1e2430", backgroundcolor="#0d1117")), margin=dict(l=0, r=0, b=0, t=0), paper_bgcolor="#0d1117", plot_bgcolor="#0d1117", height=400)
        st.plotly_chart(fig_fl, use_container_width=True, theme=None)

    # ==============================================================================
    # ABA 5: NATAÇÃO
    # ==============================================================================
    with tabs[4]:
        col_n1, col_n2 = st.columns([1, 2.5])
        with col_n1:
            tempo_natacao = st.slider("Tempo em Natação 50m (Segundos)", 20, 120, 50)
        with col_n2:
            cor_natacao = "#00ff88" if tempo_natacao <= 55 else "#ff2b6d"
            st.plotly_chart(criar_velocimetro(tempo_natacao, 120, "⏱️ TEMPO (SEGUNDOS)", cor_natacao, 55), use_container_width=True)

        st.markdown("---")
        xn, yn = np.meshgrid(np.linspace(0, 10, 40), np.linspace(0, 10, 40)); zn = np.sin(xn + (tempo_natacao/10)) * np.cos(yn) * (tempo_natacao/15) 
        fig_n = go.Figure(data=[go.Surface(z=zn, x=xn, y=yn, colorscale='Blues', opacity=0.9, lighting=dict(specular=2.0))])
        fig_n.update_layout(scene=dict(xaxis=dict(showgrid=False, backgroundcolor="#0d1117"), yaxis=dict(showgrid=False, backgroundcolor="#0d1117"), zaxis=dict(gridcolor="#1e2430", backgroundcolor="#0d1117")), margin=dict(l=0, r=0, b=0, t=0), paper_bgcolor="#0d1117", plot_bgcolor="#0d1117", height=400)
        st.plotly_chart(fig_n, use_container_width=True, theme=None)

    # ==============================================================================
    # ABA 6: ZONAS VAM
    # ==============================================================================
    with tabs[5]:
        col_v1, col_v2 = st.columns([1, 2.5])
        with col_v1:
            vam = st.slider("Sua Velocidade Aeróbica Máxima (km/h)", 10.0, 22.0, 15.0, 0.5)
            limiar = vam * 0.85
        with col_v2:
            st.plotly_chart(criar_velocimetro(limiar, 25, "🫀 LIMIAR ANAERÓBICO (KM/H)", "#ff2b6d", 15), use_container_width=True)

        st.markdown("---")
        xv, yv = np.meshgrid(np.linspace(8, 20, 30), np.linspace(0, 15, 30)); zv = (0.2 * (xv * 16.6667)) + (0.9 * (xv * 16.6667) * (yv/100)) + 3.5
        fig_v = go.Figure(data=[go.Surface(z=zv, x=xv, y=yv, colorscale='Inferno', opacity=1.0)])
        fig_v.update_layout(scene=dict(xaxis=dict(gridcolor="#1e2430", backgroundcolor="#0d1117"), yaxis=dict(gridcolor="#1e2430", backgroundcolor="#0d1117"), zaxis=dict(gridcolor="#1e2430", backgroundcolor="#0d1117")), margin=dict(l=0, r=0, b=0, t=0), paper_bgcolor="#0d1117", plot_bgcolor="#0d1117", height=400)
        st.plotly_chart(fig_v, use_container_width=True, theme=None)

    # ==============================================================================
    # ABA 7: MARCHA TÁTICA
    # ==============================================================================
    with tabs[6]:
        col_m1, col_m2 = st.columns([1, 2.5])
        with col_m1:
            dist_marcha = st.slider("Distância da Marcha (km)", 4.0, 20.0, 8.0, 0.5)
            carga_mochila = st.slider("Peso da Mochila (kg)", 5.0, 30.0, 15.0, 1.0)
            carga_tatica = dist_marcha * carga_mochila
        with col_m2:
            st.plotly_chart(criar_velocimetro(carga_tatica, 600, "🎒 CARGA TÁTICA (KG/KM)", "#FFD700"), use_container_width=True)
            
        st.markdown("---")
        xt, yt = np.meshgrid(np.linspace(0, dist_marcha, 40), np.linspace(-2, 2, 40)); zt = np.sin(xt * 1.5) * np.cos(yt * 2) * (carga_mochila / 10) + np.random.normal(0, 0.1, xt.shape)
        fig_m = go.Figure(data=[go.Surface(z=zt, x=xt, y=yt, colorscale='Earth', opacity=1.0, lighting=dict(roughness=0.8, specular=0.2))])
        fig_m.update_layout(scene=dict(xaxis=dict(gridcolor="#1e2430", backgroundcolor="#0d1117"), yaxis=dict(gridcolor="#1e2430", backgroundcolor="#0d1117"), zaxis=dict(gridcolor="#1e2430", backgroundcolor="#0d1117"), camera=dict(eye=dict(x=1.5, y=-1.5, z=0.8))), margin=dict(l=0, r=0, b=0, t=0), paper_bgcolor="#0d1117", plot_bgcolor="#0d1117", height=400)
        st.plotly_chart(fig_m, use_container_width=True, theme=None)

    # ==============================================================================
    # ABA 8: SISTEMA DE CLEARANCE ESPCEX
    # ==============================================================================
    with tabs[7]:
        col_t1, col_t2 = st.columns([1, 2.5])
        with col_t1:
            t_cor = st.slider("Corrida 12min (m)", 1500, 3200, 2200, 50, key="t1")
            t_bar = st.slider("Barra (reps)", 0, 15, 2, key="t2")
            t_nat = st.slider("Natação 50m (s)", 20, 120, 60, key="t3")
            t_abd = st.slider("Abdominal (reps)", 0, 60, 25, key="t4")
            t_flx = st.slider("Flexões (reps)", 0, 40, 15, key="t5")

        p_cor = min((t_cor / 2450) * 100, 100); p_bar = min((t_bar / 3) * 100, 100); p_nat = min((55 / t_nat) * 100, 100); p_abd = min((t_abd / 30) * 100, 100); p_flx = min((t_flx / 21) * 100, 100)
        prontidao_global = (p_cor + p_bar + p_nat + p_abd + p_flx) / 5
        clearance_cor_grafico = 'Reds'; cor_vel = "#ff4b4b"
        if prontidao_global >= 50: clearance_cor_grafico = 'Blues'; cor_vel = "#00f2fe"
        if prontidao_global >= 80: clearance_cor_grafico = 'aggrnyl'; cor_vel = "#00ff88"
        if prontidao_global >= 95: clearance_cor_grafico = 'Wistia'; cor_vel = "#FFD700"
        with col_t2:
            st.plotly_chart(criar_velocimetro(prontidao_global, 100, "🎯 ÍNDICE DE PRONTIDÃO GLOBAL (%)", cor_vel, 80), use_container_width=True)

        st.markdown("---")
        u = np.linspace(0, 2 * np.pi, 120); v = np.linspace(0, 2 * np.pi, 60); U, V = np.meshgrid(u, v)
        X_t = (4 + 1 * np.cos(V)) * np.cos(U); Y_t = (4 + 1 * np.cos(V)) * np.sin(U); Z_t = 1 * np.sin(V)
        Matriz_Cores = np.where(U <= ((prontidao_global/100) * 2 * np.pi), 1, 0)
        fig_d = go.Figure(data=[go.Surface(x=X_t, y=Y_t, z=Z_t, surfacecolor=Matriz_Cores, colorscale=clearance_cor_grafico, cmin=0, cmax=1, showscale=False, opacity=1.0, lighting=dict(ambient=0.5, diffuse=0.8, specular=1.5, roughness=0.1), contours=dict(x=dict(show=False), y=dict(show=False), z=dict(show=False)))])
        fig_d.update_layout(scene=dict(xaxis=dict(showgrid=False, showticklabels=False, backgroundcolor="#0d1117", title=""), yaxis=dict(showgrid=False, showticklabels=False, backgroundcolor="#0d1117", title=""), zaxis=dict(showgrid=False, showticklabels=False, backgroundcolor="#0d1117", title=""), camera=dict(eye=dict(x=0, y=-1.5, z=1.8))), margin=dict(l=0, r=0, b=0, t=0), paper_bgcolor="#0d1117", plot_bgcolor="#0d1117", height=450)
        st.plotly_chart(fig_d, use_container_width=True, theme=None)

   # ==============================================================================
    # ABA 9: ÁUDIO TFM (SPOTIFY NATIVO ISOLADO)
    # ==============================================================================
    with tabs[8]:
        import streamlit.components.v1 as components
        
        codigo_spotify = """
        <style>
        body { 
            background-color: transparent; 
            margin: 0; 
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; 
        }
        .spotify-ultra-container {
            background: linear-gradient(135deg, rgba(29, 185, 84, 0.1) 0%, rgba(5, 8, 15, 0.95) 100%);
            border: 1px solid rgba(29, 185, 84, 0.3);
            border-top: 3px solid #1DB954;
            border-radius: 24px;
            padding: 30px;
            text-align: center;
        }
        .spotify-title { 
            color: #fff; 
            font-size: 2.2rem; 
            font-weight: 900; 
            margin: 0 0 10px 0; 
            text-shadow: 0 0 20px rgba(29, 185, 84, 0.6); 
        }
        .spotify-title span { color: #1DB954; }
        .spotify-desc { color: #b3b3b3; font-size: 1rem; margin-bottom: 25px; }
        .spotify-iframe-wrapper { 
            border-radius: 20px; 
            overflow: hidden; 
            border: 2px solid rgba(29, 185, 84, 0.4); 
            box-shadow: 0 15px 50px rgba(0,0,0,0.9); 
        }
        </style>

        <div class="spotify-ultra-container">
            <h2 class="spotify-title"><span>🎧</span> SPOTIFY TÁTICO</h2>
            <p class="spotify-desc">Sincronização Ativa. Faça o login direto no terminal abaixo para aceder à sua biblioteca sem sair do sistema.</p>
            <div class="spotify-iframe-wrapper">
                <iframe src="https://open.spotify.com/embed/playlist/37i9dQZF1DX76Wlfdnj7bg?utm_source=generator&theme=0" width="100%" height="700" frameBorder="0" allowfullscreen="" allow="autoplay; clipboard-write; encrypted-media; fullscreen; picture-in-picture" loading="lazy"></iframe>
            </div>
        </div>
        """
        
        # Carrega o HTML de forma isolada e segura, prevenindo o erro do React
        components.html(codigo_spotify, height=900, scrolling=False)
   
# ====================================================
# MÓDULO 14: NUTRIÇÃO & CALORIAS
# ====================================================
elif menu == "🥗 Nutrição & Calorias":
    # Importações necessárias para o funcionamento do módulo
    from zoneinfo import ZoneInfo # Para lidar com fusos horários reais
    import datetime # Para manipular datas e horas dos registros
    import re # Expressões regulares para formatar textos (slugs)
    import time # Para criar efeitos de atraso (loading/spinners)
    import pandas as pd # Para manipular e filtrar dados em tabelas
    import numpy as np # Para cálculos matemáticos complexos
    import plotly.express as px # Gráficos rápidos do Plotly
    import plotly.graph_objects as go # Gráficos altamente customizáveis (Gauge, Donut 3D)
    import streamlit.components.v1 as components # Para injetar HTML e JS puro

    # =====================================================================
    # 🌟 1. CSS INJECTIONS: ANIMAÇÕES E VISUAL CYBERPUNK
    # =====================================================================
    # Este bloco injeta CSS diretamente na página do Streamlit para sobrescrever
    # o design padrão e adicionar animações de flutuação, brilho neon e gradientes.
    st.markdown("""
        <style>
        /* Fade-in: Faz a página inteira aparecer suavemente de baixo para cima */
        @keyframes fadeInScale {
            0% { opacity: 0; transform: scale(0.95); }
            100% { opacity: 1; transform: scale(1); }
        }
        
        /* Gradient Shift: Move o fundo colorido do título em loop contínuo */
        @keyframes gradientShift {
            0% { background-position: 0% 50%; }
            50% { background-position: 100% 50%; }
            100% { background-position: 0% 50%; }
        }
        
        /* Float Effect: Faz os cartões de métricas subirem e descerem suavemente */
        @keyframes floatEffect {
            0% { transform: translateY(0px); }
            50% { transform: translateY(-8px); }
            100% { transform: translateY(0px); }
        }
        
        /* Neon Pulse: Faz as bordas e sombras piscarem com luz neon (Ciano e Verde) */
        @keyframes neonPulse {
            0% { box-shadow: 0 0 10px rgba(0, 201, 255, 0.2); }
            50% { box-shadow: 0 0 25px rgba(0, 201, 255, 0.6), 0 0 10px rgba(146, 254, 157, 0.4); }
            100% { box-shadow: 0 0 10px rgba(0, 201, 255, 0.2); }
        }

        /* Aplica o Fade-in no container principal do app */
        .block-container { animation: fadeInScale 0.8s ease-out; }

        /* Estilização das Métricas (Cartões com números grandes) */
        [data-testid="stMetric"] {
            background: rgba(20, 25, 35, 0.6) !important; /* Fundo escuro transparente */
            backdrop-filter: blur(10px); /* Efeito de vidro borrado */
            border: 1px solid rgba(0, 201, 255, 0.2);
            border-radius: 15px;
            padding: 15px !important;
            animation: floatEffect 4s ease-in-out infinite; /* Efeito flutuante */
            transition: all 0.3s ease;
        }
        
        /* Efeito ao passar o mouse nas métricas (salto e brilho) */
        [data-testid="stMetric"]:hover {
            transform: scale(1.05) translateY(-5px);
            border-color: #92FE9D;
            box-shadow: 0 10px 30px rgba(146, 254, 157, 0.3);
        }

        /* Estilização avançada para todos os botões do módulo */
        .stButton>button {
            transition: all 0.3s ease !important;
            border: 1px solid rgba(0, 201, 255, 0.4) !important;
            background: linear-gradient(45deg, rgba(0,0,0,0.4), rgba(0,201,255,0.1)) !important;
            border-radius: 10px !important;
        }
        .stButton>button:hover {
            transform: scale(1.05) !important;
            background: rgba(0, 201, 255, 0.2) !important;
            box-shadow: 0 0 15px rgba(0, 201, 255, 0.6) !important;
            border-color: #00C9FF !important;
        }
        
        /* Efeito hover nos gráficos Plotly para dar sensação 3D ao passar o mouse */
        [data-testid="stGraph"] { transition: transform 0.4s ease, filter 0.4s ease; }
        [data-testid="stGraph"]:hover {
            transform: scale(1.02);
            filter: drop-shadow(0px 10px 20px rgba(0, 201, 255, 0.2));
        }

        /* Título com texto em gradiente animado */
        .premium-title {
            background: linear-gradient(270deg, #00C9FF, #92FE9D, #00C9FF);
            background-size: 200% 200%;
            animation: gradientShift 4s ease infinite;
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            font-weight: 900;
            text-transform: uppercase;
            letter-spacing: 2px;
        }
        </style>
    """, unsafe_allow_html=True)

    # O cabeçalho animado e o relógio são compartilhados por todas as páginas acima do conteúdo do módulo.
    st.markdown("<h2 class='premium-title'>🥗 Painel Nutricional</h2>", unsafe_allow_html=True)

    # =====================================================================
    # 🗃️ 3. BASE DE DADOS LOCAL DE ALIMENTOS
    # =====================================================================
    # Lista de dicionários contendo a tabela nutricional padrão para cálculos.
    QG_CATALOGO_ALIMENTOS = [
        {"Alimento": "Arroz branco cozido", "Grupo": "Grãos", "Porção comum (g)": 150, "kcal/100 g": 128, "Proteína (g)": 2.5, "Carboidratos (g)": 28.1, "Gorduras (g)": 0.2},
        {"Alimento": "Feijão carioca cozido", "Grupo": "Leguminosas", "Porção comum (g)": 100, "kcal/100 g": 76, "Proteína (g)": 4.8, "Carboidratos (g)": 13.6, "Gorduras (g)": 0.5},
        {"Alimento": "Peito de frango grelhado", "Grupo": "Proteínas", "Porção comum (g)": 100, "kcal/100 g": 159, "Proteína (g)": 32.0, "Carboidratos (g)": 0.0, "Gorduras (g)": 2.5},
        {"Alimento": "Ovo cozido", "Grupo": "Proteínas", "Porção comum (g)": 50, "kcal/100 g": 146, "Proteína (g)": 13.3, "Carboidratos (g)": 0.6, "Gorduras (g)": 9.5},
        {"Alimento": "Carne bovina magra grelhada", "Grupo": "Proteínas", "Porção comum (g)": 100, "kcal/100 g": 219, "Proteína (g)": 35.9, "Carboidratos (g)": 0.0, "Gorduras (g)": 7.3},
        {"Alimento": "Leite integral", "Grupo": "Laticínios", "Porção comum (g)": 200, "kcal/100 g": 61, "Proteína (g)": 3.2, "Carboidratos (g)": 4.7, "Gorduras (g)": 3.3},
        {"Alimento": "Queijo minas frescal", "Grupo": "Laticínios", "Porção comum (g)": 30, "kcal/100 g": 264, "Proteína (g)": 17.4, "Carboidratos (g)": 3.2, "Gorduras (g)": 20.2},
        {"Alimento": "Aveia em flocos", "Grupo": "Grãos", "Porção comum (g)": 30, "kcal/100 g": 394, "Proteína (g)": 13.9, "Carboidratos (g)": 66.6, "Gorduras (g)": 8.5},
        {"Alimento": "Pão francês", "Grupo": "Grãos", "Porção comum (g)": 50, "kcal/100 g": 300, "Proteína (g)": 8.0, "Carboidratos (g)": 58.6, "Gorduras (g)": 3.1},
        {"Alimento": "Tapioca (beiju simples)", "Grupo": "Grãos", "Porção comum (g)": 60, "kcal/100 g": 288, "Proteína (g)": 0.2, "Carboidratos (g)": 71.9, "Gorduras (g)": 0.1},
        {"Alimento": "Batata-doce cozida", "Grupo": "Tubérculos", "Porção comum (g)": 100, "kcal/100 g": 77, "Proteína (g)": 0.6, "Carboidratos (g)": 18.4, "Gorduras (g)": 0.1},
        {"Alimento": "Batata inglesa cozida", "Grupo": "Tubérculos", "Porção comum (g)": 100, "kcal/100 g": 52, "Proteína (g)": 1.2, "Carboidratos (g)": 11.9, "Gorduras (g)": 0.1},
        {"Alimento": "Banana-prata", "Grupo": "Frutas", "Porção comum (g)": 80, "kcal/100 g": 98, "Proteína (g)": 1.3, "Carboidratos (g)": 26.0, "Gorduras (g)": 0.1},
        {"Alimento": "Maçã", "Grupo": "Frutas", "Porção comum (g)": 130, "kcal/100 g": 56, "Proteína (g)": 0.3, "Carboidratos (g)": 15.2, "Gorduras (g)": 0.0},
        {"Alimento": "Tomate", "Grupo": "Verduras e legumes", "Porção comum (g)": 100, "kcal/100 g": 15, "Proteína (g)": 1.1, "Carboidratos (g)": 3.1, "Gorduras (g)": 0.2},
        {"Alimento": "Alface", "Grupo": "Verduras e legumes", "Porção comum (g)": 50, "kcal/100 g": 11, "Proteína (g)": 1.3, "Carboidratos (g)": 1.7, "Gorduras (g)": 0.2},
        {"Alimento": "Cenoura crua", "Grupo": "Verduras e legumes", "Porção comum (g)": 80, "kcal/100 g": 34, "Proteína (g)": 1.3, "Carboidratos (g)": 7.7, "Gorduras (g)": 0.2},
    ]
    # Cria um dicionário rápido para buscar os alimentos pelo nome
    QG_ALIMENTO_POR_NOME = {item["Alimento"]: item for item in QG_CATALOGO_ALIMENTOS}

    # =====================================================================
    # 📑 4. DECLARAÇÃO DAS ABAS PRINCIPAIS
    # =====================================================================
    tab_estimativas, tab_catalogo, tab_planejador, tab_hidratacao = st.tabs([
        "📊 Motor Corporal", "🥑 Banco de Dados", "🍽️ Sintetizador", "💧 Centro Hídrico"
    ])

    # =====================================================================
    # ⚙️ ABA 1: MOTOR CORPORAL (CÁLCULOS METABÓLICOS)
    # =====================================================================
    with tab_estimativas:
        st.subheader("⚙️ Calibração de Metabolismo")
        
        # Formulários de entrada das variáveis corporais do usuário
        c_peso, c_altura, c_idade = st.columns(3)
        peso = c_peso.number_input("Peso atual (kg)", min_value=40.0, max_value=150.0, value=68.0, step=0.5, key="nutri_peso")
        altura = c_altura.number_input("Altura (cm)", min_value=130, max_value=220, value=175, key="nutri_altura")
        idade = c_idade.number_input("Idade", min_value=14, max_value=60, value=17, key="nutri_idade")

        c_roca, c_taf = st.columns(2)
        nivel_roca = c_roca.selectbox("Intensidade na Roça", ["Leve / Interno", "Moderado (Manejo/Café)", "Pesado (Capina/Carga)"], key="nutri_trabalho")
        treino_taf = c_taf.selectbox("Treino TAF de hoje", ["Nenhum / Descanso", "Corrida Moderada", "Treino Intenso (Corrida + Barra + Abdominal)"], key="nutri_treino")

        # --- ALGORITMO DE GASTO CALÓRICO ---
        # 1. TMB (Taxa Metabólica Basal) - Fórmula de Mifflin-St Jeor para homens
        tmb = (10 * peso) + (6.25 * altura) - (5 * idade) + 5
        
        # 2. Multiplicador de Atividade (Trabalho na roça)
        fator_roca_val = (tmb * 1.2) - tmb if "Leve" in nivel_roca else ((tmb * 1.45) - tmb if "Moderado" in nivel_roca else (tmb * 1.7) - tmb)
        
        # 3. Adicional de Atividade Física (Treino TAF)
        fator_taf_val = 0 if "Nenhum" in treino_taf else (250 if "Moderada" in treino_taf else 450)
        
        # 4. Gasto Diário Total e Metas de Água e Macros
        gasto_total = tmb + fator_roca_val + fator_taf_val
        agua_ml = peso * 40 # Recomenda-se ~40ml de água por kg

        # Renderiza os blocos fluantes com os resultados
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("🔥 Gasto Total", f"{int(gasto_total)} kcal")
        m2.metric("💧 Hidratação Base", f"{agua_ml / 1000:.2f} L")
        m3.metric("🥩 Meta Proteína", f"{int(peso * 1.8)} g")
        m4.metric("🍚 Meta Carbo", f"{int(peso * 5.0)} g")

        st.markdown("---")
        
        # --- GRÁFICO 1: Rosquinha de Distribuição Energética ---
        fig_donut_gasto = go.Figure(data=[go.Pie(
            labels=['Metabolismo Basal', 'Trabalho Físico', 'Treino TAF'], 
            values=[tmb, fator_roca_val, fator_taf_val], 
            hole=0.65, # Deixa o meio vazado criando a rosquinha
            marker=dict(colors=['#00C9FF', '#92FE9D', '#FF2A6D'], line=dict(color='rgba(0,0,0,0)', width=2)),
            textinfo='percent', textfont=dict(size=14, color='white', family="Arial Black")
        )])
        fig_donut_gasto.update_layout(
            template="plotly_dark", paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
            margin=dict(l=0, r=0, b=0, t=10), height=350,
            showlegend=True, legend=dict(orientation="h", yanchor="bottom", y=-0.3, xanchor="center", x=0.5)
        )
        # Adiciona a anotação central com texto iluminado
        fig_donut_gasto.add_annotation(text=f"<span style='font-size:32px; font-weight:900; color:#92FE9D;'>{gasto_total:.0f}</span><br>KCAL", x=0.5, y=0.5, showarrow=False)
        st.plotly_chart(fig_donut_gasto, use_container_width=True)

    # =====================================================================
    # 🥑 ABA 2: BANCO DE DADOS (CATÁLOGO FILTRÁVEL)
    # =====================================================================
    with tab_catalogo:
        st.subheader("🗄️ Matriz de Ingredientes")
        df_alimentos = pd.DataFrame(QG_CATALOGO_ALIMENTOS)
        
        # Lógica de filtros do DataFrame (por grupo ou pesquisa de texto)
        filtro_col1, filtro_col2 = st.columns([1, 1.5])
        grupos_alimentos = ["Todos"] + sorted(df_alimentos["Grupo"].unique().tolist())
        grupo_escolhido = filtro_col1.selectbox("Grupo", grupos_alimentos, key="qg_nutri_grupo")
        busca_alimento = filtro_col2.text_input("Buscar", placeholder="Ex.: arroz", key="qg_nutri_busca")
        
        df_filtrado = df_alimentos.copy()
        if grupo_escolhido != "Todos": 
            df_filtrado = df_filtrado[df_filtrado["Grupo"] == grupo_escolhido]
        if busca_alimento.strip(): 
            df_filtrado = df_filtrado[df_filtrado["Alimento"].str.contains(busca_alimento.strip(), case=False, na=False)]
            
        st.dataframe(df_filtrado, use_container_width=True, hide_index=True)

    # =====================================================================
    # 🍽️ ABA 3: SINTETIZADOR (MONTAR REFEIÇÃO)
    # =====================================================================
    with tab_planejador:
        st.subheader("⚛️ Sintetizador Quântico")
        
        # Seleção múltipla para montar o prato
        ingredientes_disponiveis = st.multiselect("Ingredientes", options=list(QG_ALIMENTO_POR_NOME), key=f"qg_planejador_ingredientes_{user_atual}")

        if ingredientes_disponiveis:
            quantidades_planejadas = {}
            colunas_ingredientes = st.columns(3)
            
            # Gera campos de gramas (Inputs) para cada alimento selecionado dinamicamente
            for i, nome in enumerate(ingredientes_disponiveis):
                item = QG_ALIMENTO_POR_NOME[nome]
                slug = re.sub(r"[^a-z0-9]+", "_", nome.casefold()).strip("_") # Cria ID único sem espaços
                quantidades_planejadas[nome] = colunas_ingredientes[i % 3].number_input(f"{nome} (g)", min_value=1, value=int(item["Porção comum (g)"]), step=10, key=f"g_{slug}")

            # Processa e calcula os totais macro de cada alimento baseado nas gramas escolhidas
            linhas_prato = []
            for nome, gramas in quantidades_planejadas.items():
                item = QG_ALIMENTO_POR_NOME[nome]
                f = gramas / 100 # Fator de proporção (pois o banco está em base 100g)
                linhas_prato.append({
                    "Ingrediente": nome, "Quantidade (g)": gramas, "Energia (kcal)": item["kcal/100 g"] * f, 
                    "Proteína (g)": item["Proteína (g)"] * f, "Carbo (g)": item["Carboidratos (g)"] * f, "Gordura (g)": item["Gorduras (g)"] * f,
                })
            
            # Converte as linhas processadas num DataFrame e plota na tela
            df_prato = pd.DataFrame(linhas_prato)
            st.dataframe(df_prato.round(1), use_container_width=True, hide_index=True)
        else:
            st.info("Aguardando seleção de ingredientes...")

    # =====================================================================
    # 💧 ABA 4: CENTRO DE CONTROLE HÍDRICO (DB, GRÁFICOS E IA)
    # =====================================================================
    with tab_hidratacao:
        st.subheader("💧 Hub de Monitoramento Hídrico")
        
        # Controle de fuso horário confiável
        try: fuso_hidratacao = ZoneInfo("America/Sao_Paulo")
        except: fuso_hidratacao = datetime.timezone(datetime.timedelta(hours=-3))

        # --- CONEXÃO COM O BANCO DE DADOS LOCAL DO USUÁRIO ---
        hidratacao_qg = db.get("nutricao_hidratacao", {})
        if not isinstance(hidratacao_qg, dict): hidratacao_qg = {}
        # Filtra os dados existentes para garantir segurança e evitar corrompimento
        registros_hidratacao = [item for item in hidratacao_qg.get("registros", []) if isinstance(item, dict) and str(item.get("ml", "")).isdigit()]
        
        # Função Auxiliar 1: Conversão de datas gravadas como String (ISO) de volta para Datetime
        def _qg_data_hidratacao(registro):
            try:
                dt = datetime.datetime.fromisoformat(str(registro.get("data_hora", "")).replace("Z", "+00:00"))
                if dt.tzinfo is None: dt = dt.replace(tzinfo=fuso_hidratacao)
                return dt.astimezone(fuso_hidratacao)
            except: return None

        # Função Auxiliar 2: Insere o log de água na lista, limita a 1500 logs, e salva no banco de dados principal
        def _qg_registrar_hidratacao(volume_ml):
            registros_hidratacao.append({"data_hora": datetime.datetime.now(fuso_hidratacao).isoformat(timespec="seconds"), "ml": int(volume_ml), "origem": "Sistema"})
            hidratacao_qg["registros"] = registros_hidratacao[-1500:] # Mantém apenas as últimas 1500 entradas para salvar memória
            db["nutricao_hidratacao"] = hidratacao_qg
            salvar_dados_usuario(user_atual, db) # Persiste os dados (Requer que a função 'salvar_dados_usuario' exista globalmente no seu app)
            st.toast(f"Fluído reabastecido! +{volume_ml}ml", icon="🌊")

        # Puxa a meta diária salva no DB ou calcula via fórmula de Peso
        meta_salva_ml = hidratacao_qg.get("meta_ml", int(round(agua_ml)))
        
        # --- CÁLCULOS DE ESTATÍSTICA DE ÁGUA (HOJE E SEMANA) ---
        hoje = datetime.datetime.now(fuso_hidratacao).date()
        
        # Filtra apenas registros que aconteceram na data de hoje
        reg_hoje = [(r, _qg_data_hidratacao(r)) for r in registros_hidratacao if _qg_data_hidratacao(r) and _qg_data_hidratacao(r).date() == hoje]
        total_hoje = sum(int(r["ml"]) for r, _ in reg_hoje) # Soma as MLs de hoje
        
        falta_agua = max(0, meta_salva_ml - total_hoje)
        progresso = min(1.0, total_hoje / max(1, meta_salva_ml)) # Garante que não divida por zero e fique entre 0% e 100%

        # Calcula a soma diária pros últimos 7 dias para traçar histórico
        datas_semana = [hoje - datetime.timedelta(days=d) for d in range(6, -1, -1)]
        totais_semana = {d: 0 for d in datas_semana}
        for r in registros_hidratacao:
            d = _qg_data_hidratacao(r)
            if d and d.date() in totais_semana: totais_semana[d.date()] += int(r["ml"])
        
        media_7d = sum(totais_semana.values()) / 7 # Média dos 7 dias
        
        # --- STATUS DINÂMICO E DIAGNÓSTICO (ESTILO JOGO) ---
        if progresso >= 1: status_cor, status_txt = "#92FE9D", "Excelência (Meta Atingida!)"
        elif progresso >= 0.6: status_cor, status_txt = "#00C9FF", "Adequado (Continue bebendo)"
        else: status_cor, status_txt = "#FF2A6D", "Alerta Crítico (Risco)"

        # 1. PAINEL SUPERIOR: Métricas em Cartões Flutuantes (Hover effects definidos via CSS lá no topo)
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Tanque Atual", f"{total_hoje} ml")
        m2.metric("Faltante", f"{falta_agua} ml")
        m3.metric("Média Semanal", f"{int(media_7d)} ml")
        m4.metric("Status do Corpo", status_txt)

        st.markdown("---")
        
        # 2. CONTROLES: Botoeira de Entrada e Calibração de Metas
        col_botoes, col_config = st.columns([2, 1])
        with col_botoes:
            st.markdown("#### ⚡ Injetar Fluído")
            btn1, btn2, btn3, btn4 = st.columns(4)
            # O st.rerun recarrega a interface pra mostrar o dado atualizado imediatamente. Time.sleep dá tempo do toast aparecer.
            if btn1.button("🥛 200 ml", use_container_width=True): _qg_registrar_hidratacao(200); time.sleep(0.5); st.rerun()
            if btn2.button("🥤 300 ml", use_container_width=True): _qg_registrar_hidratacao(300); time.sleep(0.5); st.rerun()
            if btn3.button("💧 500 ml", use_container_width=True): _qg_registrar_hidratacao(500); time.sleep(0.5); st.rerun()
            if btn4.button("🚰 750 ml", use_container_width=True): _qg_registrar_hidratacao(750); time.sleep(0.5); st.rerun()
        
        with col_config:
            with st.expander("⚙️ Calibrar Meta Hídrica"):
                nova_meta = st.number_input("Nova Meta (ml)", min_value=500, max_value=10000, value=meta_salva_ml, step=100)
                if st.button("Salvar Meta", use_container_width=True):
                    hidratacao_qg["meta_ml"] = int(nova_meta)
                    db["nutricao_hidratacao"] = hidratacao_qg
                    salvar_dados_usuario(user_atual, db)
                    st.toast("Diretriz Hídrica Atualizada!", icon="🎯")
                    time.sleep(0.5); st.rerun()

        st.markdown("---")
        
        # 3. GRÁFICOS VISUAIS (VELOCÍMETRO E HISTÓRICO)
        graf1, graf2 = st.columns(2)
        
        with graf1:
            st.markdown("#### 🚀 Velocímetro de Abastecimento")
            # --- GRÁFICO 2: Gauge (Velocímetro) - Mostra progresso diário contra a meta.
            # O 'steps' cria as zonas de cores (vermelho no começo, ciano no meio, verde no final).
            fig_gauge = go.Figure(go.Indicator(
                mode = "gauge+number+delta",
                value = total_hoje,
                domain = {'x': [0, 1], 'y': [0, 1]},
                delta = {'reference': meta_salva_ml, 'position': "top", 'decreasing': {'color': '#FF2A6D'}, 'increasing': {'color': '#92FE9D'}},
                number = {'font': {'size': 40, 'color': status_cor}},
                gauge = {
                    'axis': {'range': [None, meta_salva_ml], 'tickwidth': 1, 'tickcolor': "white"},
                    'bar': {'color': status_cor, 'thickness': 0.7},
                    'bgcolor': "rgba(255,255,255,0.05)",
                    'borderwidth': 0,
                    'steps': [
                        {'range': [0, meta_salva_ml*0.4], 'color': "rgba(255, 42, 109, 0.2)"},
                        {'range': [meta_salva_ml*0.4, meta_salva_ml*0.8], 'color': "rgba(0, 201, 255, 0.2)"},
                        {'range': [meta_salva_ml*0.8, meta_salva_ml*1.5], 'color': "rgba(146, 254, 157, 0.2)"}
                    ],
                    'threshold': {'line': {'color': "white", 'width': 4}, 'thickness': 1, 'value': meta_salva_ml}
                }
            ))
            fig_gauge.update_layout(template="plotly_dark", paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)", height=350, margin=dict(l=20, r=20, b=20, t=20))
            st.plotly_chart(fig_gauge, use_container_width=True)

        with graf2:
            st.markdown("#### 📊 Histórico de 7 Dias")
            # --- GRÁFICO 3: Barras Plotly Express ---
            df_semana = pd.DataFrame({"Dia": [d.strftime("%d/%m") for d in datas_semana], "Volume": [totais_semana[d] for d in datas_semana]})
            
            # Gera gráfico de barras dinâmico e desenha uma linha pontilhada verde marcando a Meta Salva.
            fig_barras = px.bar(df_semana, x="Dia", y="Volume", text="Volume", color="Volume", color_continuous_scale="Blues")
            fig_barras.add_hline(y=meta_salva_ml, line_dash="dash", line_color="#92FE9D", annotation_text=" META", annotation_position="top right", annotation_font_color="#92FE9D")
            fig_barras.update_traces(textposition='outside', marker_line_color='rgba(0,0,0,0)', marker_line_width=0)
            fig_barras.update_layout(
                template="plotly_dark", paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)", height=350, margin=dict(l=0, r=0, b=0, t=10),
                xaxis=dict(showgrid=False), yaxis=dict(showgrid=True, gridcolor="rgba(255,255,255,0.1)"), coloraxis_showscale=False
            )
            st.plotly_chart(fig_barras, use_container_width=True)

        st.markdown("---")
        
        # 4. INTELIGÊNCIA ARTIFICIAL E RESUMO DIÁRIO
        col_diag, col_historico = st.columns([1.5, 1])
        
        with col_diag:
            st.markdown("#### 🧠 Inteligência Hídrica")
            # Texto dinâmico: se altera de acordo com a seleção que o usuário fez na aba 1 (Treino TAF)
            if "Intenso" in treino_taf:
                dica_taf = "⚠️ **Atenção:** Você tem Treino TAF Intenso hoje. Adicione pelo menos 500ml extras durante/após a atividade para repor eletrólitos."
            elif "Moderada" in treino_taf:
                dica_taf = "🏃‍♂️ **Dica:** Corrida leve hoje! Beba água cerca de 30 mins antes do treino para melhorar a performance."
            else:
                dica_taf = "🔋 **Recuperação:** Hoje é dia de descanso. Mantenha o fluxo constante para recuperar os músculos."
                
            st.info(f"**Análise de Perfil:** Para o seu peso configurado de {peso}kg, a base fisiológica de hidratação é {agua_ml}ml diários. {dica_taf}")

        with col_historico:
            st.markdown("#### 📜 Últimas Entradas")
            # Mostra apenas as 5 últimas bebidas de hoje
            if reg_hoje:
                linhas = [{"Hora": dt.strftime("%H:%M:%S"), "Injeção": f"{r['ml']} ml"} for r, dt in sorted(reg_hoje, key=lambda p: p[1], reverse=True)[:5]]
                st.dataframe(pd.DataFrame(linhas), use_container_width=True, hide_index=True)
            else:
                st.caption("Nenhuma atividade registrada no sistema hoje.")

# ====================================================
# MÓDULO 15: CALC. AGROPECUÁRIA
# ====================================================
elif menu == "🌾 Calc. Agropecuária":
    st.header("🌾 Módulo 15: Calculadora Técnica Agropecuária")
    st.markdown("Apoio prático para tarefas da EFA, calibração de dosagens e estimativas de café.")

    subtab1, subtab2, subtab3 = st.tabs(["📐 Conversor de Áreas", "🧪 Dosagem de Adubação", "☕ Estimativa de Cafeicultura"])

    with subtab1:
        st.subheader("Conversão de Medidas de Área")
        ha_input = st.number_input("Área em Hectares (ha)", min_value=0.1, value=1.0, step=0.5)
        m2_calc = ha_input * 10000
        alq_mg = ha_input / 4.8
        col_a1, col_a2 = st.columns(2)
        col_a1.metric("Área em Metros Quadrados", f"{m2_calc:,.0f} m²")
        col_a2.metric("Alqueires Mineiros (4,8 ha)", f"{alq_mg:.2f} alq")

    with subtab2:
        st.subheader("Cálculo de Adubação por Talhão")
        pes_alvo = st.number_input("Número de Pés de Café no Talhão", min_value=100, value=2000, step=100)
        dose_g = st.number_input("Dosagem Recomendada por Planta (gramas)", min_value=10, value=150, step=10)
        total_kg = (pes_alvo * dose_g) / 1000
        sacos_50kg = total_kg / 50
        st.success(f"**Total de Adubo Necessário:** {total_kg:.1f} kg ({sacos_50kg:.1f} sacos de 50kg)")

    with subtab3:
        st.subheader("Estimativa de Produtividade do Cafezal")
        total_pes = st.number_input("Total de Plantas em Produção", min_value=100, value=3000, step=500)
        litros_por_pe = st.number_input("Média Estimada de Litros por Pé", min_value=0.5, value=4.0, step=0.5)
        litros_totais = total_pes * litros_por_pe
        sacas_estimadas = litros_totais / 480
        st.info(f"**Estimativa da Colheita:** {litros_totais:,.0f} Litros ➔ **~{sacas_estimadas:.1f} Sacas Beneficiadas (60kg)**")


# ====================================================
# MÓDULO 16: KANBAN EFA
# ====================================================
elif menu == "📌 Kanban EFA":
    st.header("📌 Módulo 16: Quadro Kanban de Trabalhos & Pesquisas EFA")
    st.markdown("Gestão visual de projetos escolares, maquetes e relatórios agrícolas.")

    if "kanban_tasks" not in st.session_state:
        st.session_state.kanban_tasks = [
            {"id": 1, "titulo": "Relatório de Solo e Adubação", "status": "A Fazer"},
            {"id": 2, "titulo": "Pesquisa sobre Cooperativismo", "status": "Em Andamento"},
            {"id": 3, "titulo": "Apresentação AMSP 01", "status": "Entregue"}
        ]

    with st.expander("➕ Adicionar Novo Projeto/Trabalho"):
        novo_proj = st.text_input("Título do Trabalho")
        if st.button("Cadastrar no Kanban") and novo_proj:
            novo_id = len(st.session_state.kanban_tasks) + 1
            st.session_state.kanban_tasks.append({"id": novo_id, "titulo": novo_proj, "status": "A Fazer"})
            st.rerun()

    k_fazer, k_and, k_ent = st.columns(3)

    with k_fazer:
        st.subheader("🔴 A Fazer")
        for item in list(st.session_state.kanban_tasks):
            if item["status"] == "A Fazer":
                with st.expander(f"📌 {item['titulo']}"):
                    if st.button("Mover ➔ Em Andamento", key=f"kan_and_{item['id']}"):
                        item["status"] = "Em Andamento"
                        st.rerun()

    with k_and:
        st.subheader("🟡 Em Andamento")
        for item in list(st.session_state.kanban_tasks):
            if item["status"] == "Em Andamento":
                with st.expander(f"⏳ {item['titulo']}"):
                    if st.button("Mover ➔ Entregue", key=f"kan_ent_{item['id']}"):
                        item["status"] = "Entregue"
                        st.rerun()

    with k_ent:
        st.subheader("🟢 Entregue")
        for item in list(st.session_state.kanban_tasks):
            if item["status"] == "Entregue":
                with st.expander(f"✅ {item['titulo']}"):
                    st.caption("Concluído e arquivado")


# ====================================================
# MÓDULO 17: HABIT TRACKER
# ====================================================
elif menu == "🔥 Habit Tracker":
    st.header("🔥 Módulo 17: Habit Tracker & Disciplina Militar")
    st.markdown("Acompanhe o cumprimento diário das tarefas fundamentais para a aprovação.")

    if "streak_dias" not in st.session_state:
        st.session_state.streak_dias = 12

    st.subheader(f"🔥 Sequência Atual: **{st.session_state.streak_dias} Dias Consecutivos de Foco!**")

    st.markdown("### Checklist do Dia")
    h1 = st.checkbox("🎯 Meta de Questões Cumprida (BQEM)")
    h2 = st.checkbox("🏃 Treino do TAF Realizado (Corrida / Flexão / Abdominal)")
    h3 = st.checkbox("📖 Leitura do LDI / Teoria")
    h4 = st.checkbox("🧠 Anotações e Revisão NIP")
    h5 = st.checkbox("📑 Diário de Campo / AMSP EFA em dia")

    total_cumprido = sum([h1, h2, h3, h4, h5])
    progresso = total_cumprido / 5.0

    st.progress(progresso)
    st.write(f"**Progresso Diário:** {total_cumprido} de 5 metas concluídas ({int(progresso * 100)}%)")

    if total_cumprido == 5:
        st.balloons()
        st.success("Excelente! Missão cumprida no dia de hoje. Mantenha o padrão!")


# ====================================================
# MINI JOGO DE PONTOS: DESAFIOS, NÍVEIS E CONQUISTAS
# ====================================================
elif menu == "🎮 Mini Jogo de Pontos":
    from zoneinfo import ZoneInfo

    st.header("🎮 Mini Jogo de Pontos")
    st.markdown("Complete um desafio, ganhe XP, suba de nível e desbloqueie conquistas. Seu progresso fica salvo na sua conta.")

    QG_DESAFIOS_STAFF = [
        {"id": "flexoes", "nome": "Flexões", "unidade": "repetições", "descricao": "Registre a variação de flexão adequada ao seu momento."},
        {"id": "agachamentos", "nome": "Agachamentos", "unidade": "repetições", "descricao": "Faça apenas o que for confortável para você."},
        {"id": "abdominais", "nome": "Abdominais", "unidade": "repetições", "descricao": "Registre uma série curta, sem competir com outras pessoas."},
        {"id": "caminhada", "nome": "Caminhada leve", "unidade": "minutos", "descricao": "Uma pausa ativa no ritmo que você escolher."},
        {"id": "mobilidade", "nome": "Mobilidade ou alongamento", "unidade": "minutos", "descricao": "Movimente-se com leveza, sem forçar amplitude."},
    ]
    STAFF_DESAFIOS_POR_ID = {item["id"]: item for item in QG_DESAFIOS_STAFF}
    QG_MISSOES_BASE_STAFF = [
        ("caminhada", "Caminhada leve", [3, 4, 5, 6, 8], "minutos", "Caminhe por {q} minutos em ritmo confortável."),
        ("marcha", "Marcha leve no lugar", [20, 30, 40, 50, 60], "segundos", "Marche no lugar por {q} segundos, sem acelerar."),
        ("flexao_parede", "Flexão na parede", [3, 4, 5, 6, 8], "repetições", "Faça {q} flexões na parede, com movimento controlado."),
        ("sentar_levantar", "Sentar e levantar da cadeira", [3, 4, 5, 6, 8], "repetições", "Faça {q} repetições usando uma cadeira firme como referência."),
        ("panturrilha", "Elevação de panturrilha com apoio", [4, 5, 6, 8, 10], "repetições", "Faça {q} elevações segurando em um apoio estável."),
        ("ponte", "Ponte de glúteos", [3, 4, 5, 6, 8], "repetições", "Faça {q} repetições lentas, somente se for confortável."),
        ("bird_dog", "Bird-dog adaptado", [2, 3, 4, 5, 6], "por lado", "Faça {q} movimentos controlados de cada lado, com apoio confortável."),
        ("dead_bug", "Dead bug adaptado", [2, 3, 4, 5, 6], "por lado", "Faça {q} movimentos controlados de cada lado, sem prender a respiração."),
        ("joelho_sentado", "Elevação de joelho sentado", [4, 5, 6, 8, 10], "por lado", "Eleve cada joelho {q} vezes, sentado em uma cadeira firme."),
        ("toque_degrau", "Toque alternado em degrau baixo", [20, 30, 40, 50, 60], "segundos", "Alterne toques em um degrau baixo por {q} segundos, com apoio próximo."),
        ("mobilidade_ombros", "Mobilidade suave de ombros", [20, 30, 40, 50, 60], "segundos", "Faça movimentos confortáveis de ombros por {q} segundos."),
        ("rotacao_tronco", "Rotação suave de tronco", [2, 3, 4, 5, 6], "por lado", "Faça {q} rotações pequenas e confortáveis para cada lado."),
        ("mobilidade_tornozelo", "Mobilidade de tornozelos", [2, 3, 4, 5, 6], "por lado", "Faça {q} movimentos circulares suaves com cada tornozelo."),
        ("alongamento_panturrilha", "Alongamento de panturrilha", [10, 15, 20, 25, 30], "segundos por lado", "Alongue com leveza por {q} segundos de cada lado, sem forçar."),
        ("alongamento_peitoral", "Alongamento peitoral na parede", [10, 15, 20, 25, 30], "segundos", "Mantenha um alongamento confortável por {q} segundos, sem dor."),
        ("respiracao", "Respiração tranquila", [1, 2, 3, 4, 5], "minutos", "Faça respirações tranquilas por {q} minuto(s), sem prender o ar."),
        ("equilibrio", "Equilíbrio com apoio próximo", [5, 8, 10, 12, 15], "segundos por lado", "Mantenha-se em equilíbrio por {q} segundos de cada lado, perto de um apoio."),
        ("bracos_sem_carga", "Elevação confortável de braços", [4, 5, 6, 8, 10], "repetições", "Eleve os braços sem carga por {q} repetições, em amplitude confortável."),
        ("extensao_joelho", "Extensão de joelho sentado", [4, 5, 6, 8, 10], "por lado", "Estenda cada joelho {q} vezes, sentado e sem pressa."),
        ("passos_laterais", "Passos laterais leves", [20, 30, 40, 50, 60], "segundos", "Faça passos laterais curtos por {q} segundos, com espaço livre."),
    ]
    QG_MISSOES_DIARIAS_STAFF = []
    for prefixo_missao_staff, nome_missao_staff, metas_missao_staff, unidade_missao_staff, descricao_missao_staff in QG_MISSOES_BASE_STAFF:
        for meta_missao_staff in metas_missao_staff:
            QG_MISSOES_DIARIAS_STAFF.append({
                "id": f"{prefixo_missao_staff}_{meta_missao_staff}",
                "nome": f"{nome_missao_staff} · {meta_missao_staff} {unidade_missao_staff}",
                "descricao": descricao_missao_staff.format(q=meta_missao_staff),
            })
    MISSOES_STAFF_POR_ID = {item["id"]: item for item in QG_MISSOES_DIARIAS_STAFF}
    try:
        fuso_staff = ZoneInfo("America/Sao_Paulo")
    except Exception:
        fuso_staff = datetime.timezone(datetime.timedelta(hours=-3))
    hoje_staff = datetime.datetime.now(fuso_staff).date()
    data_staff_hoje = hoje_staff.isoformat()
    estado_staff = db.get("desafios_staff", {})
    if not isinstance(estado_staff, dict):
        estado_staff = {}
    registros_staff = estado_staff.get("registros", [])
    if not isinstance(registros_staff, list):
        registros_staff = []

    registros_staff_validos = []
    for registro_staff in registros_staff:
        if not isinstance(registro_staff, dict):
            continue
        try:
            data_registro_staff = datetime.date.fromisoformat(str(registro_staff.get("data", ""))[:10]).isoformat()
            atividade_id_staff = str(registro_staff.get("atividade", ""))
            pontos_registro_staff = int(registro_staff.get("pontos", 0))
            quantidade_registro_staff = float(registro_staff.get("quantidade", 0))
        except (TypeError, ValueError, OverflowError):
            continue
        if atividade_id_staff not in STAFF_DESAFIOS_POR_ID or not 1 <= pontos_registro_staff <= 30 or not 0 < quantidade_registro_staff <= 1000:
            continue
        registro_limpo_staff = {
            "data": data_registro_staff,
            "atividade": atividade_id_staff,
            "quantidade": quantidade_registro_staff,
            "pontos": pontos_registro_staff,
            "hora": str(registro_staff.get("hora", ""))[:8],
        }
        registros_staff_validos.append(registro_limpo_staff)
    registros_staff = registros_staff_validos

    missoes_diarias_staff = estado_staff.get("missoes_diarias", {})
    if not isinstance(missoes_diarias_staff, dict):
        missoes_diarias_staff = {}
    missoes_diarias_staff_validas = {}
    for data_missao_staff, estado_missao_staff in missoes_diarias_staff.items():
        if not isinstance(estado_missao_staff, dict):
            continue
        try:
            data_missao_staff = datetime.date.fromisoformat(str(data_missao_staff)[:10]).isoformat()
        except ValueError:
            continue
        desafio_id_staff = str(estado_missao_staff.get("desafio_id", ""))
        if desafio_id_staff not in MISSOES_STAFF_POR_ID:
            continue
        missoes_diarias_staff_validas[data_missao_staff] = {
            "desafio_id": desafio_id_staff,
            "concluida": estado_missao_staff.get("concluida") is True,
            "atualizado_em": str(estado_missao_staff.get("atualizado_em", ""))[:25],
        }
    missoes_diarias_staff = missoes_diarias_staff_validas
    estado_staff["missoes_diarias"] = missoes_diarias_staff
    indice_missao_staff = (hoje_staff - datetime.date(2025, 1, 1)).days % len(QG_MISSOES_DIARIAS_STAFF)
    missao_dia_staff = QG_MISSOES_DIARIAS_STAFF[indice_missao_staff]
    status_missao_hoje_staff = missoes_diarias_staff.get(data_staff_hoje, {})
    missao_hoje_concluida_staff = (
        status_missao_hoje_staff.get("desafio_id") == missao_dia_staff["id"]
        and status_missao_hoje_staff.get("concluida") is True
    )
    missoes_concluidas_staff = {
        data_missao for data_missao, estado_missao in missoes_diarias_staff.items()
        if estado_missao.get("concluida") is True
    }
    pontos_totais_staff = sum(int(registro["pontos"]) for registro in registros_staff) + 10 * len(missoes_concluidas_staff)
    total_desafios_staff = len(registros_staff) + len(missoes_concluidas_staff)
    dias_staff_com_atividade = {registro["data"] for registro in registros_staff} | missoes_concluidas_staff
    pontos_staff_hoje = sum(int(registro["pontos"]) for registro in registros_staff if registro["data"] == data_staff_hoje) + (10 if missao_hoje_concluida_staff else 0)
    sequencia_staff = 0
    cursor_staff = hoje_staff
    if cursor_staff.isoformat() not in dias_staff_com_atividade:
        cursor_staff -= datetime.timedelta(days=1)
    while cursor_staff.isoformat() in dias_staff_com_atividade:
        sequencia_staff += 1
        cursor_staff -= datetime.timedelta(days=1)

    niveis_staff = [(0, "Recruta"), (50, "Cadete"), (150, "Operador"), (300, "Veterano"), (600, "Elite"), (1000, "Lenda")]
    nivel_atual_staff = max((nivel for nivel in niveis_staff if pontos_totais_staff >= nivel[0]), key=lambda nivel: nivel[0])
    proximo_nivel_staff = next((nivel for nivel in niveis_staff if nivel[0] > pontos_totais_staff), None)
    if proximo_nivel_staff:
        inicio_nivel_staff = nivel_atual_staff[0]
        progresso_nivel_staff = (pontos_totais_staff - inicio_nivel_staff) / max(1, proximo_nivel_staff[0] - inicio_nivel_staff)
        destino_nivel_staff = f"Próximo nível · {proximo_nivel_staff[1]} ({proximo_nivel_staff[0]} XP)"
    else:
        progresso_nivel_staff = 1.0
        destino_nivel_staff = "Nível máximo · Lenda"
    progresso_nivel_staff = max(0.0, min(1.0, progresso_nivel_staff))

    chave_flash_staff = f"staff_desafio_flash_{user_atual}"
    flash_desafio_staff = st.session_state.pop(chave_flash_staff, None)
    if flash_desafio_staff:
        st.balloons()
        st.success(flash_desafio_staff)

    # Os pontos são pessoais. A missão do dia é única para a data e só rende XP quando marcada como concluída.
    circunferencia_staff = 282.743
    deslocamento_staff = circunferencia_staff * (1.0 - progresso_nivel_staff)
    hud_staff = f"""
    <section class="qg-staff-hud" aria-label="Resumo pessoal de desafios">
      <div class="qg-staff-hud-copy">
        <div class="qg-staff-kicker">QG · MINI JOGO</div>
        <div class="qg-staff-title">Seu progresso de equipe</div>
        <div class="qg-staff-rank">{html.escape(nivel_atual_staff[1])} <span>·</span> {html.escape(destino_nivel_staff)}</div>
        <div class="qg-staff-meta"><span>🔥 {sequencia_staff} dias de sequência</span><span>🎯 {len(QG_MISSOES_DIARIAS_STAFF)} desafios no catálogo</span></div>
      </div>
      <div class="qg-staff-ring-wrap">
        <svg class="qg-staff-ring" viewBox="0 0 120 120" role="img" aria-label="Progresso para o próximo nível">
          <circle class="qg-staff-ring-track" cx="60" cy="60" r="45"></circle>
          <circle class="qg-staff-ring-progress" cx="60" cy="60" r="45" style="stroke-dasharray:{circunferencia_staff:.3f};stroke-dashoffset:{deslocamento_staff:.3f}"></circle>
          <text x="60" y="56" class="qg-staff-xp-value"><tspan id="qg-staff-points">0</tspan></text>
          <text x="60" y="74" class="qg-staff-xp-label">PONTOS</text>
        </svg>
      </div>
    </section>
    <style>
      html,body{{margin:0;padding:0;background:transparent;color:#edf5ff;font-family:Inter,'Segoe UI',sans-serif}}
      .qg-staff-hud{{position:relative;isolation:isolate;overflow:hidden;display:flex;align-items:center;justify-content:space-between;gap:20px;min-height:174px;padding:22px 26px;border:1px solid rgba(99,229,220,.24);border-radius:22px;background:radial-gradient(ellipse at 92% 4%,rgba(90,115,255,.24),transparent 34%),linear-gradient(120deg,rgba(11,34,50,.98),rgba(19,31,59,.96));box-shadow:0 18px 50px rgba(0,0,0,.22),inset 0 1px rgba(255,255,255,.05)}}
      .qg-staff-hud:before{{content:'';position:absolute;z-index:-1;width:300px;height:300px;right:5%;top:-215px;border:1px solid rgba(99,229,220,.15);border-radius:50%;box-shadow:0 0 0 24px rgba(99,229,220,.025),0 0 0 52px rgba(120,165,255,.025);animation:staff-orbit 28s linear infinite}}
      .qg-staff-hud:after{{content:'';position:absolute;inset:auto 0 0;height:2px;background:linear-gradient(90deg,transparent,#63e5dc,#78a5ff,#b09aff,transparent);opacity:.8}}
      .qg-staff-hud-copy{{min-width:0}}.qg-staff-kicker{{color:#63e5dc;font:750 10px 'DM Mono',monospace;letter-spacing:.2em}}
      .qg-staff-title{{margin:9px 0 5px;color:#f4f8ff;font-size:clamp(20px,3vw,28px);font-weight:850;letter-spacing:-.045em}}
      .qg-staff-rank{{color:#c6daea;font-size:13px;font-weight:700}}.qg-staff-rank span{{padding:0 5px;color:#63e5dc}}
      .qg-staff-meta{{display:flex;flex-wrap:wrap;gap:9px;margin-top:14px}}.qg-staff-meta span{{padding:7px 10px;border:1px solid rgba(151,184,214,.16);border-radius:999px;color:#a9bed1;background:rgba(5,16,29,.4);font:650 10px 'DM Mono',monospace}}
      .qg-staff-ring-wrap{{flex:0 0 128px;display:grid;place-items:center}}.qg-staff-ring{{width:126px;height:126px;overflow:visible;transform:rotate(-90deg)}}
      .qg-staff-ring circle{{fill:none;stroke-width:8}}.qg-staff-ring-track{{stroke:rgba(158,187,211,.13)}}.qg-staff-ring-progress{{stroke:#63e5dc;stroke-linecap:round;filter:drop-shadow(0 0 7px rgba(99,229,220,.65));transition:stroke-dashoffset 1.1s cubic-bezier(.2,.75,.2,1)}}
      .qg-staff-xp-value{{fill:#f4ffff;font:850 22px 'DM Mono',monospace;text-anchor:middle;dominant-baseline:middle;transform:rotate(90deg);transform-origin:60px 60px}}
      .qg-staff-xp-label{{fill:#91a9bd;font:750 7px 'DM Mono',monospace;letter-spacing:1.2px;text-anchor:middle;transform:rotate(90deg);transform-origin:60px 60px}}
      @keyframes staff-orbit{{to{{transform:rotate(360deg)}}}}
      @media(max-width:540px){{.qg-staff-hud{{align-items:flex-start;padding:17px;gap:8px}}.qg-staff-ring-wrap{{flex-basis:96px}}.qg-staff-ring{{width:94px;height:94px}}.qg-staff-meta{{gap:6px}}.qg-staff-meta span{{font-size:9px;padding:6px 8px}}}}
      @media(prefers-reduced-motion:reduce){{*,*:before,*:after{{animation:none!important;transition:none!important}}}}
    </style>
    <script>
      (function(){{const node=document.getElementById('qg-staff-points');if(!node)return;const finalValue={pontos_totais_staff};const start=performance.now();const duration=900;function frame(now){{const t=Math.min(1,(now-start)/duration);const eased=1-Math.pow(1-t,3);node.textContent=Math.round(finalValue*eased).toLocaleString('pt-BR');if(t<1)requestAnimationFrame(frame)}}requestAnimationFrame(frame)}})();
    </script>
    """
    with st.container(border=True):
        st.markdown("#### ⚡ Seu progresso no Mini Jogo")
        resumo_topo_staff_1, resumo_topo_staff_2, resumo_topo_staff_3 = st.columns(3)
        resumo_topo_staff_1.metric("Pontuação", f"{pontos_totais_staff:,} XP")
        resumo_topo_staff_2.metric("Patente", nivel_atual_staff[1])
        resumo_topo_staff_3.metric("Sequência", f"{sequencia_staff} dias")
        st.progress(progresso_nivel_staff, text=destino_nivel_staff)

    aba_desafio_staff, aba_conquistas_staff, aba_recompensas_staff = st.tabs([
        "🎯 Missão diária", "🏅 Conquistas", "🎁 Ideias de recompensa"
    ])

    with aba_desafio_staff:
        progresso_visual_missao_staff = int(round((indice_missao_staff + 1) / len(QG_MISSOES_DIARIAS_STAFF) * 100))
        card_missao_3d_staff = """
        <section class="qg-mission3d" aria-label="Missão diária do QG">
          <div class="qg-mission-copy">
            <div class="qg-mission-kicker"><span class="qg-live-dot"></span> MISSÃO DO DIA <span class="qg-mission-index">__INDEX__/100</span></div>
            <div class="qg-mission-date">DESAFIO SELECIONADO PELO QG · __DATE__</div>
            <h2>__TITLE__</h2>
            <p class="qg-mission-desc">__DESCRIPTION__</p>
            <div class="qg-mission-reward">
              <span class="qg-xp-pill">⚡ +10 XP</span>
              <span class="qg-reward-note">ao marcar como concluída</span>
            </div>
            <div class="qg-catalog-row"><span>CATÁLOGO ROTATIVO</span><span>__PROGRESS__%</span></div>
            <div class="qg-catalog-track"><span style="width:__PROGRESS__%"></span></div>
          </div>
          <div class="qg-mission-scene" aria-hidden="true">
            <div class="qg-scene-halo"></div>
            <div class="qg-orbit qg-orbit-a"></div>
            <div class="qg-orbit qg-orbit-b"></div>
            <div class="qg-cube qg-cube-a">
              <i class="front">✦</i><i class="back"></i><i class="right"></i><i class="left"></i><i class="top"></i><i class="bottom"></i>
            </div>
            <div class="qg-cube qg-cube-b">
              <i class="front">+</i><i class="back"></i><i class="right"></i><i class="left"></i><i class="top"></i><i class="bottom"></i>
            </div>
            <div class="qg-medal-model">
              <div class="qg-ribbon qg-ribbon-left"></div>
              <div class="qg-ribbon qg-ribbon-right"></div>
              <div class="qg-medal-edge"></div>
              <div class="qg-medal-face"><span>★</span><small>QG</small></div>
            </div>
            <div class="qg-scene-caption">MISSÃO · XP · EVOLUÇÃO</div>
          </div>
        </section>
        <style>
          * { box-sizing:border-box }
          html,body { margin:0; padding:0; background:transparent; color:#eef5ff; font-family:Inter,'Segoe UI',sans-serif }
          .qg-mission3d {
            position:relative; isolation:isolate; overflow:hidden; min-height:276px; display:grid;
            grid-template-columns:minmax(0,1fr) 235px; align-items:center; gap:14px;
            padding:25px 30px; border:1px solid rgba(109,231,222,.22); border-radius:24px;
            background:
              radial-gradient(ellipse at 80% 48%,rgba(67,118,255,.20),transparent 34%),
              radial-gradient(ellipse at 3% 0%,rgba(44,224,204,.10),transparent 40%),
              linear-gradient(120deg,#0b1b2a 0%,#102438 48%,#141d38 100%);
            box-shadow:0 22px 60px rgba(0,0,0,.23),inset 0 1px rgba(255,255,255,.06);
          }
          .qg-mission3d:before {
            content:''; position:absolute; inset:0; z-index:-1; opacity:.18; pointer-events:none;
            background-image:linear-gradient(rgba(160,205,220,.12) 1px,transparent 1px),linear-gradient(90deg,rgba(160,205,220,.12) 1px,transparent 1px);
            background-size:34px 34px; mask-image:linear-gradient(90deg,#000,transparent 85%);
          }
          .qg-mission3d:after { content:''; position:absolute; bottom:0; left:7%; right:7%; height:1px; background:linear-gradient(90deg,transparent,#63e5dc,#83a8ff,transparent); opacity:.8 }
          .qg-mission-copy { min-width:0; position:relative; z-index:2 }
          .qg-mission-kicker { display:flex; align-items:center; gap:9px; color:#71e6dd; font:800 10px 'DM Mono',monospace; letter-spacing:.16em }
          .qg-live-dot { width:7px; height:7px; border-radius:50%; background:#63e5dc; box-shadow:0 0 12px #63e5dc; animation:qg-pulse 2s ease-in-out infinite }
          .qg-mission-index { margin-left:auto; color:#9db3c9; letter-spacing:.08em }
          .qg-mission-date { margin-top:14px; color:#829bb2; font:700 9px 'DM Mono',monospace; letter-spacing:.12em }
          .qg-mission3d h2 { margin:9px 0 8px; max-width:600px; color:#f4f8ff; font-size:clamp(21px,3vw,31px); line-height:1.12; letter-spacing:-.045em; font-weight:850 }
          .qg-mission-desc { margin:0; max-width:560px; color:#b7c8d8; font-size:13px; line-height:1.55 }
          .qg-mission-reward { display:flex; align-items:center; gap:10px; margin-top:16px }
          .qg-xp-pill { display:inline-flex; align-items:center; gap:6px; padding:8px 12px; border:1px solid rgba(255,205,113,.36); border-radius:999px; color:#ffe2a3; background:linear-gradient(135deg,rgba(255,181,65,.18),rgba(255,222,151,.07)); font:800 11px 'DM Mono',monospace; box-shadow:0 0 22px rgba(255,187,79,.08) }
          .qg-reward-note { color:#95aabe; font-size:11px }
          .qg-catalog-row { display:flex; justify-content:space-between; max-width:440px; margin-top:17px; color:#8199af; font:700 9px 'DM Mono',monospace; letter-spacing:.10em }
          .qg-catalog-track { max-width:440px; height:4px; margin-top:7px; overflow:hidden; border-radius:8px; background:rgba(158,191,216,.13) }
          .qg-catalog-track span { display:block; height:100%; border-radius:inherit; background:linear-gradient(90deg,#58dfd2,#83a8ff); box-shadow:0 0 12px rgba(91,225,215,.55) }
          .qg-mission-scene { position:relative; min-height:220px; perspective:800px; transform-style:preserve-3d }
          .qg-scene-halo { position:absolute; width:176px; height:176px; top:18px; left:29px; border-radius:50%; background:radial-gradient(circle,rgba(75,210,227,.17),rgba(75,210,227,.035) 52%,transparent 72%); filter:blur(2px) }
          .qg-orbit { position:absolute; top:82px; left:32px; width:170px; height:64px; border:1px solid rgba(112,227,224,.27); border-radius:50%; transform-style:preserve-3d }
          .qg-orbit-a { transform:rotate(-24deg) rotateX(64deg); animation:qg-orbit 12s linear infinite }
          .qg-orbit-b { width:148px; height:52px; top:89px; left:43px; border-color:rgba(146,164,255,.28); transform:rotate(35deg) rotateX(66deg); animation:qg-orbit 16s linear infinite reverse }
          .qg-medal-model { position:absolute; top:37px; left:83px; width:100px; height:132px; transform-style:preserve-3d; animation:qg-medal-float 4.5s ease-in-out infinite }
          .qg-ribbon { position:absolute; top:0; width:27px; height:52px; background:linear-gradient(90deg,#9a263d,#ff6175 48%,#9a263d); clip-path:polygon(0 0,100% 0,100% 100%,50% 78%,0 100%); filter:drop-shadow(0 7px 8px rgba(0,0,0,.28)); transform:translateZ(-12px) }
          .qg-ribbon-left { left:26px; transform:translateZ(-12px) rotate(-10deg) }
          .qg-ribbon-right { right:26px; background:linear-gradient(90deg,#2652a1,#6fb7ff 48%,#2652a1); transform:translateZ(-13px) rotate(10deg) }
          .qg-medal-edge { position:absolute; left:15px; top:38px; width:70px; height:70px; border-radius:50%; background:conic-gradient(from 20deg,#9b5c16,#ffe391,#be771d,#fff0b4,#9b5c16); box-shadow:0 13px 28px rgba(0,0,0,.35),0 0 30px rgba(255,192,84,.22); transform:translateZ(2px) }
          .qg-medal-face { position:absolute; left:21px; top:44px; width:58px; height:58px; display:flex; flex-direction:column; align-items:center; justify-content:center; border:1px solid rgba(255,247,202,.9); border-radius:50%; color:#fff4c9; background:radial-gradient(circle at 35% 28%,#ffd978,#c9872d 58%,#784417); box-shadow:inset 0 0 0 4px rgba(255,245,198,.19),inset 0 0 12px rgba(96,45,8,.36); transform:translateZ(12px) }
          .qg-medal-face span { font-size:21px; line-height:1; text-shadow:0 2px 5px rgba(75,38,0,.45) }
          .qg-medal-face small { margin-top:2px; font:800 7px 'DM Mono',monospace; letter-spacing:.12em }
          .qg-cube { --size:46px; position:absolute; width:var(--size); height:var(--size); transform-style:preserve-3d; animation:qg-cube-spin 13s linear infinite }
          .qg-cube i { position:absolute; inset:0; display:grid; place-items:center; border:1px solid rgba(127,236,231,.55); color:#c7fff7; background:linear-gradient(135deg,rgba(75,231,219,.42),rgba(61,100,181,.45)); box-shadow:inset 0 0 16px rgba(103,231,224,.16); font-style:normal; font-size:20px; backface-visibility:visible }
          .qg-cube .front { transform:translateZ(calc(var(--size) / 2)) }
          .qg-cube .back { transform:rotateY(180deg) translateZ(calc(var(--size) / 2)) }
          .qg-cube .right { transform:rotateY(90deg) translateZ(calc(var(--size) / 2)) }
          .qg-cube .left { transform:rotateY(-90deg) translateZ(calc(var(--size) / 2)) }
          .qg-cube .top { transform:rotateX(90deg) translateZ(calc(var(--size) / 2)) }
          .qg-cube .bottom { transform:rotateX(-90deg) translateZ(calc(var(--size) / 2)) }
          .qg-cube-a { top:44px; left:14px; --size:38px; animation-duration:15s }
          .qg-cube-b { right:4px; bottom:42px; --size:29px; animation-duration:11s; animation-direction:reverse }
          .qg-cube-b i { border-color:rgba(190,158,255,.54); color:#eee2ff; background:linear-gradient(135deg,rgba(163,117,255,.42),rgba(70,88,177,.43)) }
          .qg-scene-caption { position:absolute; bottom:3px; left:0; right:0; text-align:center; color:#7691a9; font:700 8px 'DM Mono',monospace; letter-spacing:.17em }
          @keyframes qg-medal-float { 0%,100% { transform:translateY(0) rotateY(-12deg) rotateX(5deg) } 50% { transform:translateY(-9px) rotateY(12deg) rotateX(-3deg) } }
          @keyframes qg-cube-spin { from { transform:rotateX(-20deg) rotateY(0) rotateZ(5deg) } to { transform:rotateX(340deg) rotateY(360deg) rotateZ(365deg) } }
          @keyframes qg-orbit { from { rotate:0deg } to { rotate:360deg } }
          @keyframes qg-pulse { 0%,100% { opacity:1; box-shadow:0 0 12px #63e5dc } 50% { opacity:.45; box-shadow:0 0 4px #63e5dc } }
          @media(max-width:620px) {
            .qg-mission3d { grid-template-columns:1fr; gap:0; padding:21px 20px 12px }
            .qg-mission-scene { min-height:154px; width:220px; max-width:100%; justify-self:center; transform:scale(.78); transform-origin:top center; margin-bottom:-34px }
            .qg-mission-index { font-size:8px }
            .qg-mission-desc { font-size:12px }
          }
          @media(prefers-reduced-motion:reduce) { *,*:before,*:after { animation:none!important; scroll-behavior:auto!important } }
        </style>
        """
        card_missao_3d_staff = (
            card_missao_3d_staff
            .replace("__INDEX__", str(indice_missao_staff + 1))
            .replace("__DATE__", hoje_staff.strftime("%d/%m/%Y"))
            .replace("__TITLE__", html.escape(missao_dia_staff["nome"]))
            .replace("__DESCRIPTION__", html.escape(missao_dia_staff["descricao"]))
            .replace("__PROGRESS__", str(progresso_visual_missao_staff))
        )
        arte_3d_staff = """
        <div class="qg3d-stage" aria-label="Medalha e cubos tridimensionais">
          <div class="qg3d-halo"></div>
          <div class="qg3d-orbit qg3d-orbit-a"></div>
          <div class="qg3d-orbit qg3d-orbit-b"></div>
          <div class="qg3d-cube qg3d-cube-cyan">
            <i class="front"></i><i class="back"></i><i class="right"></i><i class="left"></i><i class="top"></i><i class="bottom"></i>
          </div>
          <div class="qg3d-cube qg3d-cube-violet">
            <i class="front"></i><i class="back"></i><i class="right"></i><i class="left"></i><i class="top"></i><i class="bottom"></i>
          </div>
          <div class="qg3d-medal">
            <div class="qg3d-ribbon qg3d-ribbon-red"></div>
            <div class="qg3d-ribbon qg3d-ribbon-blue"></div>
            <div class="qg3d-coin-edge"></div>
            <div class="qg3d-coin-face"><span class="qg3d-star"></span></div>
          </div>
        </div>
        <style>
          html,body { margin:0; padding:0; background:transparent }
          .qg3d-stage {
            position:relative; isolation:isolate; overflow:hidden; height:252px; width:100%;
            border:1px solid rgba(109,231,222,.22); border-radius:22px; perspective:850px;
            background:radial-gradient(ellipse at 50% 48%,rgba(69,137,211,.22),transparent 58%),linear-gradient(145deg,#0b1c2c,#111a31);
            box-shadow:0 18px 45px rgba(0,0,0,.2),inset 0 1px rgba(255,255,255,.05);
          }
          .qg3d-halo { position:absolute; width:174px; height:174px; top:33px; left:calc(50% - 87px); border-radius:50%; background:radial-gradient(circle,rgba(75,210,227,.17),rgba(75,210,227,.035) 55%,transparent 74%) }
          .qg3d-orbit { position:absolute; top:96px; left:calc(50% - 92px); width:184px; height:65px; border:1px solid rgba(112,227,224,.31); border-radius:50%; transform-style:preserve-3d }
          .qg3d-orbit-a { transform:rotate(-24deg) rotateX(64deg); animation:qg3d-orbit 14s linear infinite }
          .qg3d-orbit-b { width:154px; height:52px; top:102px; left:calc(50% - 77px); border-color:rgba(146,164,255,.34); transform:rotate(35deg) rotateX(66deg); animation:qg3d-orbit 18s linear infinite reverse }
          .qg3d-medal { position:absolute; top:42px; left:calc(50% - 50px); width:100px; height:140px; transform-style:preserve-3d; animation:qg3d-float 4.5s ease-in-out infinite }
          .qg3d-ribbon { position:absolute; top:0; width:27px; height:55px; clip-path:polygon(0 0,100% 0,100% 100%,50% 78%,0 100%); filter:drop-shadow(0 7px 8px rgba(0,0,0,.28)) }
          .qg3d-ribbon-red { left:25px; background:linear-gradient(90deg,#9a263d,#ff6175 48%,#9a263d); transform:translateZ(-12px) rotate(-10deg) }
          .qg3d-ribbon-blue { right:25px; background:linear-gradient(90deg,#2652a1,#6fb7ff 48%,#2652a1); transform:translateZ(-13px) rotate(10deg) }
          .qg3d-coin-edge { position:absolute; left:15px; top:41px; width:70px; height:70px; border-radius:50%; background:conic-gradient(from 20deg,#9b5c16,#ffe391,#be771d,#fff0b4,#9b5c16); box-shadow:0 13px 28px rgba(0,0,0,.35),0 0 30px rgba(255,192,84,.22); transform:translateZ(2px) }
          .qg3d-coin-face { position:absolute; left:21px; top:47px; width:58px; height:58px; display:grid; place-items:center; border:1px solid rgba(255,247,202,.9); border-radius:50%; background:radial-gradient(circle at 35% 28%,#ffd978,#c9872d 58%,#784417); box-shadow:inset 0 0 0 4px rgba(255,245,198,.19),inset 0 0 12px rgba(96,45,8,.36); transform:translateZ(12px) }
          .qg3d-star { display:block; width:27px; height:27px; background:linear-gradient(135deg,#fff7c9,#ffe48a); clip-path:polygon(50% 0,61% 35%,98% 35%,68% 57%,79% 94%,50% 72%,21% 94%,32% 57%,2% 35%,39% 35%); filter:drop-shadow(0 2px 4px rgba(75,38,0,.45)) }
          .qg3d-cube { --s:42px; position:absolute; width:var(--s); height:var(--s); transform-style:preserve-3d; animation:qg3d-cube-spin 15s linear infinite }
          .qg3d-cube i { position:absolute; inset:0; border:1px solid rgba(127,236,231,.6); background:linear-gradient(135deg,rgba(75,231,219,.42),rgba(61,100,181,.48)); box-shadow:inset 0 0 18px rgba(103,231,224,.18); backface-visibility:visible }
          .qg3d-cube .front { transform:translateZ(calc(var(--s) / 2)) }
          .qg3d-cube .back { transform:rotateY(180deg) translateZ(calc(var(--s) / 2)) }
          .qg3d-cube .right { transform:rotateY(90deg) translateZ(calc(var(--s) / 2)) }
          .qg3d-cube .left { transform:rotateY(-90deg) translateZ(calc(var(--s) / 2)) }
          .qg3d-cube .top { transform:rotateX(90deg) translateZ(calc(var(--s) / 2)) }
          .qg3d-cube .bottom { transform:rotateX(-90deg) translateZ(calc(var(--s) / 2)) }
          .qg3d-cube-cyan { top:50px; left:calc(50% - 110px); --s:38px; animation-duration:17s }
          .qg3d-cube-violet { right:calc(50% - 123px); bottom:41px; --s:29px; border-color:rgba(190,158,255,.6); animation-duration:13s; animation-direction:reverse }
          .qg3d-cube-violet i { border-color:rgba(190,158,255,.58); background:linear-gradient(135deg,rgba(163,117,255,.42),rgba(70,88,177,.48)) }
          @keyframes qg3d-float { 0%,100% { transform:translateY(0) rotateY(-12deg) rotateX(5deg) } 50% { transform:translateY(-8px) rotateY(12deg) rotateX(-3deg) } }
          @keyframes qg3d-cube-spin { from { transform:rotateX(-20deg) rotateY(0) rotateZ(5deg) } to { transform:rotateX(340deg) rotateY(360deg) rotateZ(365deg) } }
          @keyframes qg3d-orbit { from { rotate:0deg } to { rotate:360deg } }
          @media(max-width:640px) { .qg3d-stage { height:220px } }
          @media(prefers-reduced-motion:reduce) { *,*:before,*:after { animation:none!important } }
        </style>
        """
        col_detalhes_missao_staff, col_modelos_3d_staff = st.columns([1.55, 1], gap="large")
        with col_detalhes_missao_staff:
            with st.container(border=True):
                st.caption(f"MISSÃO DO DIA · {indice_missao_staff + 1}/100 · {hoje_staff.strftime('%d/%m/%Y')}")
                st.subheader(missao_dia_staff["nome"])
                st.write(missao_dia_staff["descricao"])
                st.metric("Recompensa da missão", "+10 XP")
                st.progress(
                    (indice_missao_staff + 1) / len(QG_MISSOES_DIARIAS_STAFF),
                    text=f"Catálogo · {indice_missao_staff + 1} de {len(QG_MISSOES_DIARIAS_STAFF)}",
                )
                st.caption("O QG seleciona o desafio do dia para todos. Adapte a atividade ao seu condicionamento.")
        with col_modelos_3d_staff:
            st.markdown("#### Medalha da missão")
            components.html(arte_3d_staff, height=260, scrolling=False)
        st.caption("Pare se sentir dor ou mal-estar.")
        st.markdown("#### Sua resposta · +10 XP")
        st.caption("Marque se concluiu. Os pontos são registrados uma única vez neste dia.")

        chave_resposta_missao_staff = f"staff_resposta_missao_{user_atual}_{data_staff_hoje}"
        if chave_resposta_missao_staff not in st.session_state:
            st.session_state[chave_resposta_missao_staff] = (
                "Concluída" if missao_hoje_concluida_staff else "Não concluída"
            )
        with st.form(f"qg_form_missao_staff_{user_atual}_{data_staff_hoje}"):
            resposta_missao_staff = st.radio(
                "A missão foi concluída?",
                ["Não concluída", "Concluída"],
                key=chave_resposta_missao_staff,
                horizontal=True,
            )
            salvar_resposta_missao_staff = st.form_submit_button(
                "💾 Salvar resultado", type="primary", use_container_width=True
            )

        if salvar_resposta_missao_staff:
            concluida_missao_staff = resposta_missao_staff == "Concluída"
            missoes_diarias_staff[data_staff_hoje] = {
                "desafio_id": missao_dia_staff["id"],
                "concluida": concluida_missao_staff,
                "atualizado_em": datetime.datetime.now(fuso_staff).isoformat(timespec="seconds"),
            }
            estado_staff["missoes_diarias"] = missoes_diarias_staff
            estado_staff["registros"] = registros_staff[-1500:]
            db["desafios_staff"] = estado_staff
            salvar_dados_usuario(user_atual, db)
            st.session_state[chave_flash_staff] = (
                "Missão concluída! Você recebeu 10 XP."
                if concluida_missao_staff else "Resultado salvo. Nenhum ponto foi adicionado."
            )
            st.rerun()

        if missao_hoje_concluida_staff:
            st.success("✅ Missão concluída · +10 XP")
        else:
            st.info("Missão aguardando seu registro. Não há penalidade por não concluir.")

        dias_historico_staff = [hoje_staff - datetime.timedelta(days=i) for i in range(13, -1, -1)]
        pontos_historico_staff = {
            dia.isoformat(): (
                sum(int(r["pontos"]) for r in registros_staff if r["data"] == dia.isoformat())
                + (10 if dia.isoformat() in missoes_concluidas_staff else 0)
            )
            for dia in dias_historico_staff
        }
        df_historico_staff = pd.DataFrame({
            "Dia": [dia.strftime("%d/%m") for dia in dias_historico_staff],
            "Pontos": [pontos_historico_staff[dia.isoformat()] for dia in dias_historico_staff],
        })
        st.markdown("#### Sua atividade · últimos 14 dias")
        grafico_staff = px.bar(df_historico_staff, x="Dia", y="Pontos", text="Pontos")
        grafico_staff.update_traces(marker_color="#78a5ff", textposition="outside", cliponaxis=False)
        grafico_staff.update_layout(
            height=280, margin=dict(l=8, r=8, t=12, b=8),
            paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
            font=dict(color="#dceafb"), xaxis_title=None, yaxis_title="XP",
            yaxis=dict(gridcolor="rgba(151,184,214,.12)", rangemode="tozero"),
        )
        st.plotly_chart(grafico_staff, use_container_width=True, config={"displayModeBar": False})

    with aba_conquistas_staff:
        st.subheader("Coleção de conquistas")
        conquistas_staff = [
            ("🌱", "Primeiro passo", total_desafios_staff >= 1, "Conclua seu primeiro desafio"),
            ("🔥", "Constância · 3 dias", sequencia_staff >= 3, "Registre atividade em 3 dias seguidos"),
            ("⚡", "Uma semana firme", sequencia_staff >= 7, "Registre atividade em 7 dias seguidos"),
            ("🎖️", "Centurião", pontos_totais_staff >= 100, "Acumule 100 XP"),
            ("🏆", "Veterano de equipe", total_desafios_staff >= 25, "Conclua 25 desafios"),
            ("💠", "Lenda do QG", pontos_totais_staff >= 1000, "Acumule 1.000 XP"),
        ]
        colunas_conquistas_staff = st.columns(3)
        for indice_conquista, (icone_conquista, nome_conquista, desbloqueada, regra_conquista) in enumerate(conquistas_staff):
            with colunas_conquistas_staff[indice_conquista % 3]:
                with st.container(border=True):
                    estado_conquista = "DESBLOQUEADA" if desbloqueada else "BLOQUEADA"
                    st.markdown(f"### {icone_conquista}")
                    st.markdown(f"**{nome_conquista}**")
                    st.caption(regra_conquista)
                    if desbloqueada:
                        st.success(estado_conquista)
                    else:
                        st.caption(f"🔒 {estado_conquista}")

        st.markdown("#### Seu resumo")
        resumo_staff_1, resumo_staff_2, resumo_staff_3 = st.columns(3)
        resumo_staff_1.metric("Pontos totais", f"{pontos_totais_staff:,} XP")
        resumo_staff_2.metric("Desafios registrados", total_desafios_staff)
        resumo_staff_3.metric("Sequência atual", f"{sequencia_staff} dias")
        st.caption("O placar é privado por conta neste primeiro formato; não há comparação pública entre usuários.")

    with aba_recompensas_staff:
        st.subheader("Ideias de recompensa")
        st.caption("Os pontos são fictícios e as recompensas ainda não são resgatáveis. Escolha o que faz sentido para sua equipe antes de ativar qualquer troca.")
        ideias_recompensa_staff = [
            ("🏅 Distintivos digitais", "Títulos e emblemas no perfil, liberados por marcos de XP."),
            ("🎨 Personalização do QG", "Temas, molduras de avatar ou cores especiais para o perfil."),
            ("📣 Mural de reconhecimento", "Destaque semanal opcional para quem completou missões, com consentimento."),
            ("🎵 Escolha da trilha", "Escolher a música ambiente ou o tema de uma atividade coletiva."),
            ("🎯 Criar o desafio da semana", "O staff escolhe uma missão leve para a equipe, sem competição por volume."),
            ("📜 Certificado simbólico", "Certificado digital por constância, participação ou marcos de equipe."),
        ]
        col_recompensas_staff = st.columns(2)
        for indice_recompensa, (titulo_recompensa, descricao_recompensa) in enumerate(ideias_recompensa_staff):
            with col_recompensas_staff[indice_recompensa % 2]:
                with st.container(border=True):
                    st.markdown(f"#### {titulo_recompensa}")
                    st.write(descricao_recompensa)
        st.info("Sugestão: começar com distintivos digitais e personalização do perfil; são recompensas simples, transparentes e sem custo.")
        st.caption("Os desafios são auto-registrados e devem ser adaptados à capacidade de cada pessoa. Faça pausas e não continue se sentir dor ou mal-estar.")


# ====================================================
# MÓDULO 18: CRONOGRAMA DINÂMICO
# ====================================================
elif menu == "📅 Cronograma Dinâmico":
    st.header("📅 Módulo 18: Cronograma Semanal Dinâmico")
    st.markdown("Alterne instantaneamente a visualização do plano entre a **Semana A (Na Escola)** e a **Semana B (Em Casa)**.")

    modo_semana = st.radio("Selecione o Contexto Atual:", ["Semana A (Escola EFA)", "Semana B (Propriedade / Casa)"], horizontal=True)

    if modo_semana == "Semana A (Escola EFA)":
        st.info("🏫 **Foco da Semana A:** Blocos noturnos concentrados (21h30 às 22h30) após o período letivo na EFA.")
        grade_a = [
            {"Dia": "Segunda-feira", "Horário": "21h30 - 22h30", "Matéria": "Matemática Básica", "Foco": "LDI Cap. 01 + 10 questões"},
            {"Dia": "Terça-feira", "Horário": "21h30 - 22h30", "Matéria": "Gramática / Português", "Foco": "Classes de Palavras + NIP"},
            {"Dia": "Quarta-feira", "Horário": "21h30 - 22h30", "Matéria": "Física", "Foco": "Conversão de Unidades e Vetores"},
            {"Dia": "Quinta-feira", "Horário": "21h30 - 22h30", "Matéria": "Química", "Foco": "Estados da Matéria"},
            {"Dia": "Sexta-feira", "Horário": "21h30 - 22h30", "Matéria": "História do Brasil", "Foco": "Período Pré-Colonial"},
            {"Dia": "Sábado", "Horário": "14h00 - 16h00", "Matéria": "Revisão + Redação", "Foco": "Estrutura de Parágrafo"},
            {"Dia": "Domingo", "Horário": "09h00 - 10h00", "Matéria": "Planejamento", "Foco": "Download de PDFs para a semana"}
        ]
        st.table(pd.DataFrame(grade_a))

    else:
        st.success("🏡 **Foco da Semana B:** Janelas ampliadas pós-trabalho na roça (18h30 às 21h30).")
        grade_b = [
            {"Dia": "Segunda-feira", "Horário": "18h30 - 21h30", "Matéria": "Matemática + Gramática", "Foco": "Porcentagem / Regra de Três + Pronomes"},
            {"Dia": "Terça-feira", "Horário": "18h30 - 21h30", "Matéria": "Física + Química", "Foco": "Movimento Uniforme + Tabela Periódica"},
            {"Dia": "Quarta-feira", "Horário": "18h30 - 21h30", "Matéria": "Matemática + Geografia", "Foco": "Equações + Cartografia e Fusos"},
            {"Dia": "Quinta-feira", "Horário": "18h30 - 21h30", "Matéria": "Física/Química + Inglês", "Foco": "Exercícios de Fixação BQEM"},
            {"Dia": "Sexta-feira", "Horário": "18h30 - 21h30", "Matéria": "Português + História", "Foco": "Sintaxe + Ciclo do Açúcar"},
            {"Dia": "Sábado", "Horário": "08h00 - 16h30", "Matéria": "Redação Prática + Caderno Erros", "Foco": "Texto Completo + Refazer Erradas"},
            {"Dia": "Domingo", "Horário": "09h00 - 10h30", "Matéria": "Planejamento Alternância", "Foco": "Organizar PDFs e Malhar TAF"}
        ]
        st.table(pd.DataFrame(grade_b))


# ====================================================
# MÓDULO 19: HUB DE LINKS
# ====================================================
elif menu == "🔗 Hub de Links":
    st.header("🔗 Módulo 19: Hub de Links Táticos & Atalhos Rápidos")
    st.markdown("Acesso direto às plataformas de estudo, materiais e sistemas com um único clique.")

    c_l1, c_l2 = st.columns(2)

    with c_l1:
        st.subheader("🎖️ EsPCEx & Estratégia Militares")
        st.link_button("🌐 Plataforma Estratégia Militares", "https://militar.estrategiaconcursos.com.br/", use_container_width=True)
        st.link_button("🎯 Banco de Questões (BQEM)", "https://questoes.estrategiaconcursos.com.br/", use_container_width=True)
        st.link_button("📜 Site Oficial da EsPCEx", "http://www.espcex.eb.mil.br/", use_container_width=True)

    with c_l2:
        st.subheader("🌾 EFA & Ferramentas Pessoais")
        st.link_button("📁 Google Drive - Documentos EFA", "https://drive.google.com/", use_container_width=True)
        st.link_button("💻 Repositório GitHub (qg-espcex-app)", "https://github.com/arrudaluan575-dotcom/qg-espcex-app", use_container_width=True)
        st.link_button("📊 Streamlit Cloud Dashboard", "https://share.streamlit.io/", use_container_width=True)


# ====================================================
# MÓDULO 20: DIÁRIO DE BORDO
# ====================================================
elif menu == "🎯 Simulados (1º e 2º Dia)":
    import sqlite3
    import datetime
    import pandas as pd

    st.title("🎯 Simulados EsPCEx (1º e 2º Dia)")

    # 1. Garante a criação da tabela no SQLite local
    def init_db_simulados():
        conn = sqlite3.connect("dados_qg.db")
        c = conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS simulados (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nome TEXT,
                data TEXT,
                portugues INTEGER,
                fisica INTEGER,
                quimica INTEGER,
                redacao REAL,
                matematica INTEGER,
                geografia INTEGER,
                historia INTEGER,
                ingles INTEGER,
                tot_objetiva INTEGER,
                aprovado TEXT
            )
        """)
        conn.commit()
        conn.close()

    init_db_simulados()

    # 2. Formulário para cadastro e avaliação do simulado
    with st.expander("➕ Lançar Simulado", expanded=True):
        with st.form("form_sim", clear_on_submit=True):
            nome_sim = st.text_input("Nome do Simulado (Ex: 1º Simulado Inédito EsPCEx 2026)")
            data_sim = st.date_input("Data do Simulado", datetime.date.today())

            st.markdown("#### 1º DIA DE PROVA")
            c1a, c1b, c1c, c1d = st.columns(4)
            p_port = c1a.number_input("Português (0 a 20 q)", 0, 20, 14)
            p_fis = c1b.number_input("Física (0 a 12 q)", 0, 12, 7)
            p_qui = c1c.number_input("Química (0 a 12 q)", 0, 12, 8)
            p_red = c1d.number_input("Redação (0 a 100)", 0.0, 100.0, 80.0, step=5.0)

            st.markdown("#### 2º DIA DE PROVA")
            c2a, c2b, c2c, c2d = st.columns(4)
            p_mat = c2a.number_input("Matemática (0 a 20 q)", 0, 20, 13)
            p_geo = c2b.number_input("Geografia (0 a 12 q)", 0, 12, 8)
            p_hist = c2c.number_input("História (0 a 12 q)", 0, 12, 9)
            p_ing = c2d.number_input("Inglês (0 a 12 q)", 0, 12, 10)

            if st.form_submit_button("🏆 Avaliar Desempenho"):
                minimos = {
                    "Português": p_port >= 10, "Física": p_fis >= 6, "Química": p_qui >= 6,
                    "Matemática": p_mat >= 10, "Geografia": p_geo >= 6, "História": p_hist >= 6,
                    "Inglês": p_ing >= 6, "Redação": p_red >= 50.0
                }
                tot_obj = p_port + p_fis + p_qui + p_mat + p_geo + p_hist + p_ing
                aprovado_bool = all(minimos.values())
                aprovado_str = "✅ SIM" if aprovado_bool else "❌ NÃO"

                # Insere o simulado no SQLite local
                conn = sqlite3.connect("dados_qg.db")
                c = conn.cursor()
                sql_insert = """
                    INSERT INTO simulados (
                        nome, data, portugues, fisica, quimica, redacao, 
                        matematica, geografia, historia, ingles, tot_objetiva, aprovado
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """
                c.execute(sql_insert, (
                    nome_sim if nome_sim.strip() else "Simulado Sem Nome",
                    str(data_sim), int(p_port), int(p_fis), int(p_qui), float(p_red),
                    int(p_mat), int(p_geo), int(p_hist), int(p_ing), int(tot_obj), aprovado_str
                ))
                conn.commit()
                conn.close()

                if aprovado_bool:
                    st.success(f"🎉 APROVADO NOS MÍNIMOS! {tot_obj}/100 acertos e Redação {p_red}.")
                else:
                    reprov = [m for m, ok in minimos.items() if not ok]
                    st.error(f"⚠️ Mínimo não atingido em: {', '.join(reprov)}")
                st.rerun()

    st.markdown("---")

    # 3. Exibição da tabela de simulados registrados no SQLite
    conn = sqlite3.connect("dados_qg.db")
    try:
        sql_select = "SELECT nome AS 'Nome', data AS 'Data', tot_objetiva AS 'tot_obj', redacao AS 'Redação', aprovado AS 'Mínimos Atingidos' FROM simulados ORDER BY id DESC"
        df_sim_raw = pd.read_sql_query(sql_select, conn)
    except Exception:
        df_sim_raw = pd.DataFrame()
    finally:
        conn.close()

    if not df_sim_raw.empty:
        # Formata a coluna de objetiva para exibir 'XX/100 (XX.0%)'
        df_sim_raw["Objetiva"] = df_sim_raw["tot_obj"].apply(lambda x: f"{x}/100 ({x:.1f}%)")
        df_exibicao = df_sim_raw[["Nome", "Data", "Objetiva", "Redação", "Mínimos Atingidos"]]
        
        st.dataframe(df_exibicao, use_container_width=True)
    else:
        st.info("Nenhum simulado registrado até o momento.")
# ====================================================
# MÓDULO 20B: DIÁRIO DE BORDO
# ====================================================
elif menu == "📖 Diário de Bordo":
    st.title("📖 Diário de Bordo")
    st.caption("Registre sua evolução, aprendizados e próximos passos. Seu rascunho é salvo automaticamente na sua conta.")
    diario = db.setdefault("diario_bordo", [])
    if not isinstance(diario, list):
        diario = []
        db["diario_bordo"] = diario

    horas_total_diario = sum(float(item.get("horas", 0) or 0) for item in diario if isinstance(item, dict))
    datas_diario = {str(item.get("data", "")) for item in diario if isinstance(item, dict) and item.get("data")}
    dm1, dm2, dm3 = st.columns(3)
    dm1.metric("Entradas registradas", len(diario))
    dm2.metric("Horas documentadas", f"{horas_total_diario:.1f} h")
    dm3.metric("Dias com registro", len(datas_diario), help=f"Registro mais recente: {max(datas_diario) if datas_diario else '—'}")

    draft_saved = db.get("diario_rascunho", {})
    if not isinstance(draft_saved, dict):
        draft_saved = {}
    draft_prefix = f"diario_rascunho_{user_atual}_"
    draft_defaults = {
        "data": draft_saved.get("data", str(datetime.date.today())),
        "materia": draft_saved.get("materia", ""), "horas": float(draft_saved.get("horas", 1.0) or 1.0),
        "resumo": draft_saved.get("resumo", ""), "aprendizado": draft_saved.get("aprendizado", ""),
        "desafio": draft_saved.get("desafio", ""), "proximo": draft_saved.get("proximo_passo", ""),
        "foco": int(draft_saved.get("foco", 3) or 3),
    }
    for campo, valor in draft_defaults.items():
        chave = draft_prefix + campo
        if chave not in st.session_state:
            if campo == "data":
                try:
                    valor = datetime.date.fromisoformat(str(valor)[:10])
                except ValueError:
                    valor = datetime.date.today()
            st.session_state[chave] = valor

    def _persistir_rascunho_diario():
        estado_fresco = carregar_dados_usuario(user_atual, get_edital_espcex)
        estado_fresco["diario_rascunho"] = {
            "data": str(st.session_state[draft_prefix + "data"]),
            "materia": st.session_state[draft_prefix + "materia"],
            "horas": float(st.session_state[draft_prefix + "horas"]),
            "resumo": st.session_state[draft_prefix + "resumo"],
            "aprendizado": st.session_state[draft_prefix + "aprendizado"],
            "desafio": st.session_state[draft_prefix + "desafio"],
            "proximo_passo": st.session_state[draft_prefix + "proximo"],
            "foco": int(st.session_state[draft_prefix + "foco"]),
            "salvo_em": datetime.datetime.now().isoformat(timespec="seconds"),
        }
        salvar_dados_usuario(user_atual, estado_fresco)

    def _registrar_diario_callback():
        estado_fresco = carregar_dados_usuario(user_atual, get_edital_espcex)
        resumo = str(st.session_state[draft_prefix + "resumo"]).strip()
        aprendizado = str(st.session_state[draft_prefix + "aprendizado"]).strip()
        if not resumo and not aprendizado:
            st.session_state["diario_feedback"] = "Preencha o resumo ou o principal aprendizado antes de salvar."
            return
        lista_diario = estado_fresco.setdefault("diario_bordo", [])
        lista_diario.append({
            "id": uuid.uuid4().hex[:12], "data": str(st.session_state[draft_prefix + "data"]),
            "materia": str(st.session_state[draft_prefix + "materia"]).strip() or "Estudo geral",
            "horas": float(st.session_state[draft_prefix + "horas"]), "resumo": resumo,
            "aprendizado": aprendizado, "desafio": str(st.session_state[draft_prefix + "desafio"]).strip(),
            "proximo_passo": str(st.session_state[draft_prefix + "proximo"]).strip(),
            "foco": int(st.session_state[draft_prefix + "foco"]),
            "criado_em": datetime.datetime.now().isoformat(timespec="minutes"),
        })
        estado_fresco["diario_bordo"] = lista_diario[-1000:]
        estado_fresco["diario_rascunho"] = {}
        salvar_dados_usuario(user_atual, estado_fresco)
        st.session_state["diario_feedback"] = "Entrada salva no Diário de Bordo."
        st.session_state[draft_prefix + "data"] = datetime.date.today()
        st.session_state[draft_prefix + "materia"] = ""
        st.session_state[draft_prefix + "horas"] = 1.0
        st.session_state[draft_prefix + "resumo"] = ""
        st.session_state[draft_prefix + "aprendizado"] = ""
        st.session_state[draft_prefix + "desafio"] = ""
        st.session_state[draft_prefix + "proximo"] = ""
        st.session_state[draft_prefix + "foco"] = 3

    feedback_diario = st.session_state.pop("diario_feedback", None)
    if feedback_diario:
        st.success(feedback_diario) if feedback_diario.startswith("Entrada salva") else st.warning(feedback_diario)

    with st.container(border=True):
        st.markdown("#### ＋ Nova entrada · salvamento automático")
        st.caption("O rascunho é salvo enquanto você preenche e continua disponível se navegar para outra tela.")
        dr1, dr2, dr3 = st.columns([1, 2, 1])
        st.date_input("Data", key=draft_prefix + "data", on_change=_persistir_rascunho_diario)
        st.text_input("Matéria / atividade", placeholder="Ex.: Física, redação ou TAF", key=draft_prefix + "materia", on_change=_persistir_rascunho_diario)
        st.number_input("Horas", min_value=0.0, max_value=24.0, step=0.5, key=draft_prefix + "horas", on_change=_persistir_rascunho_diario)
        st.text_area("O que você estudou ou realizou?", max_chars=2000, key=draft_prefix + "resumo", on_change=_persistir_rascunho_diario)
        dr4, dr5 = st.columns(2)
        dr4.text_area("Principal aprendizado", max_chars=1200, key=draft_prefix + "aprendizado", on_change=_persistir_rascunho_diario)
        dr5.text_area("Dificuldade ou ponto para revisar", max_chars=1200, key=draft_prefix + "desafio", on_change=_persistir_rascunho_diario)
        st.text_input("Próximo passo", placeholder="Ex.: resolver 20 questões de cinemática", key=draft_prefix + "proximo", on_change=_persistir_rascunho_diario)
        st.slider("Como foi seu foco hoje?", 1, 5, key=draft_prefix + "foco", help="1 = baixo · 5 = excelente", on_change=_persistir_rascunho_diario)
        st.button("Salvar entrada no diário", type="primary", use_container_width=True, on_click=_registrar_diario_callback)

    # Reread after a possible autosave callback so the list reflects the stored state.
    diario = db.get("diario_bordo", [])
    entradas_diario = sorted([x for x in diario if isinstance(x, dict)], key=lambda item: (str(item.get("data", "")), str(item.get("criado_em", ""))), reverse=True)
    if entradas_diario:
        st.markdown("### Ritmo de estudo")
        linhas_grafico = [{"Data": item.get("data"), "Horas": float(item.get("horas", 0) or 0), "Matéria": item.get("materia", "Estudo geral")} for item in reversed(entradas_diario[-30:])]
        df_diario = pd.DataFrame(linhas_grafico)
        if not df_diario.empty:
            fig_diario = px.bar(df_diario, x="Data", y="Horas", color="Matéria", barmode="stack", title="Horas registradas · últimos 30 registros")
            fig_diario.update_layout(height=320, margin=dict(t=50, b=15, l=10, r=10), xaxis_title="", yaxis_title="Horas")
            st.plotly_chart(fig_diario, use_container_width=True)
        st.markdown("### Histórico recente")
        busca_diario = st.text_input("Filtrar por matéria ou conteúdo", key="filtro_diario_bordo")
        termo_diario = busca_diario.casefold().strip()
        visiveis_diario = [item for item in entradas_diario if not termo_diario or termo_diario in " ".join(str(item.get(k, "")) for k in ("materia", "resumo", "aprendizado", "desafio", "proximo_passo")).casefold()]
        for item in visiveis_diario:
            with st.container(border=True):
                cc1, cc2 = st.columns([5, 1])
                cc1.markdown(f"**{item.get('data', '')} · {item.get('materia', 'Estudo geral')}**")
                cc2.metric("Horas", f"{float(item.get('horas', 0) or 0):g}")
                if item.get("resumo"):
                    st.write(item["resumo"])
                sub1, sub2 = st.columns(2)
                if item.get("aprendizado"):
                    sub1.markdown("**Aprendizado**")
                    sub1.write(item["aprendizado"])
                if item.get("desafio"):
                    sub2.markdown("**Para revisar**")
                    sub2.write(item["desafio"])
                if item.get("proximo_passo"):
                    st.caption(f"Próximo passo: {item['proximo_passo']}")
                st.caption(f"Foco percebido: {item.get('foco', 3)}/5")
                if st.button("Excluir entrada", key=f"apagar_diario_{item.get('id', item.get('criado_em', 'sem-id'))}"):
                    db["diario_bordo"] = [x for x in diario if x is not item]
                    salvar_dados_usuario(user_atual, db)
                    st.rerun()
    else:
        st.info("Seu diário ainda está vazio. Preencha uma entrada; o rascunho salva automaticamente e o botão registra no histórico.")

# ====================================================
# MÓDULO 21: PLAYER DE ÁUDIO
# ====================================================
elif menu == "🎧 Player de Áudio":
    st.header("🎧 Módulo 21: Central de Áudio Tático")
    st.markdown("Músicas de marcha para os treinos do TAF e frequências para concentração profunda.")

    p_col1, p_col2 = st.columns(2)

    with p_col1:
        st.subheader("🎺 Canções e Marchas Militares (TAF)")
        st.markdown("**Hino da Infantaria**")
        st.audio("https://www.soundhelix.com/examples/mp3/SoundHelix-Song-1.mp3")
        st.markdown("**Fibra de Herói**")
        st.audio("https://www.soundhelix.com/examples/mp3/SoundHelix-Song-2.mp3")

    with p_col2:
        st.subheader("🧠 Áudios para Estudo e Foco Profundo")
        st.markdown("**Ruído Branco / Foco LDI**")
        st.audio("https://www.soundhelix.com/examples/mp3/SoundHelix-Song-3.mp3")
        st.markdown("**Ondas Binaurais (Alfa - Leitura NIP)**")
        st.audio("https://www.soundhelix.com/examples/mp3/SoundHelix-Song-4.mp3")
# ====================================================
# #MODULO_22_CENTRAL_DE_CONFIGURACOES
# ====================================================
elif "módulo 22" in menu.lower() or "configurações" in menu.lower():
    import sqlite3
    import datetime
    import os
    import json
    import pandas as pd
    import streamlit as st

    # Hashtag de identificação tática na interface
    st.markdown("### `#Modulo22_Configuracoes_QG`")
    st.title("⚙️ Módulo 22 - Central de Configurações & Painel de Controle")
    st.caption("Gerenciamento integral de identidade tática, parâmetros de estudo, segurança de acesso e banco de dados local.")

    # -----------------------------------------------------------------------------
    # 1. INFRAESTRUTURA DE CONFIGURAÇÕES NO SQLITE
    # -----------------------------------------------------------------------------
    def init_db_config():
        conn = sqlite3.connect("dados_qg.db")
        c = conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS config_qg (
                chave TEXT PRIMARY KEY,
                valor TEXT
            )
        """)
        conn.commit()
        conn.close()

    init_db_config()

    def set_config(chave, valor):
        conn = sqlite3.connect("dados_qg.db")
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO config_qg (chave, valor) VALUES (?, ?)", (chave, str(valor)))
        conn.commit()
        conn.close()

    def get_config(chave, padrao=""):
        conn = sqlite3.connect("dados_qg.db")
        c = conn.cursor()
        c.execute("SELECT valor FROM config_qg WHERE chave = ?", (chave,))
        res = c.fetchone()
        conn.close()
        return res[0] if res else padrao

    def get_all_configs():
        conn = sqlite3.connect("dados_qg.db")
        c = conn.cursor()
        c.execute("SELECT chave, valor FROM config_qg")
        rows = c.fetchall()
        conn.close()
        return dict(rows)

    # -----------------------------------------------------------------------------
    # 2. NAVEGAÇÃO POR ABAS PROFISSIONAIS
    # -----------------------------------------------------------------------------
    aba_perfil, aba_parametros, aba_seguranca, aba_banco, aba_manutencao = st.tabs([
        "🎖️ Perfil & Identidade",
        "🎯 Parâmetros & Metas",
        "🔒 Acesso & Autenticação",
        "💾 Banco de Dados & Backup",
        "🛠️ Ferramentas & Restauração"
    ])

    # -----------------------------------------------------------------------------
    # ABA 1: PERFIL & IDENTIDADE TÁTICA
    # -----------------------------------------------------------------------------
    with aba_perfil:
        st.subheader("🎖️ Perfil do Cadete & Personalização Tática")
        st.caption("Edite os dados principais exibidos no cabeçalho e relatórios da plataforma.")

        with st.form("form_perfil_master"):
            col_p1, col_p2 = st.columns(2)
            nome_guerra = col_p1.text_input("Nome de Guerra", value=get_config("nome_guerra", "Arruda"))
            nome_completo = col_p2.text_input("Nome Completo", value=get_config("nome_completo", "Luan de Cristo Arruda"))

            col_p3, col_p4 = st.columns(2)
            arma_interesse = col_p3.selectbox(
                "Arma / Especialização de Interesse (EsPCEx)",
                ["Infantaria (Selva)", "Cavalaria", "Artilharia", "Engenharia", "Comunicações", "Material Bélico / Intendência"],
                index=0
            )
            instituicao_efa = col_p4.text_input("Instituição EFA", value=get_config("instituicao_efa", "Escola Família Agrícola Margarida Alves"))

            lema_tatico = st.text_input("Lema Tático / Frase Motivacional", value=get_config("lema_tatico", "A vitória ama a preparação!"))
            
            st.markdown("---")
            if st.form_submit_button("💾 Salvar Alterações do Perfil", use_container_width=True):
                set_config("nome_guerra", nome_guerra)
                set_config("nome_completo", nome_completo)
                set_config("arma_interesse", arma_interesse)
                set_config("instituicao_efa", instituicao_efa)
                set_config("lema_tatico", lema_tatico)
                st.success("✅ Perfil tático atualizado com sucesso!")
                st.rerun()

    # -----------------------------------------------------------------------------
    # ABA 2: PARÂMETROS DE ESTUDO E TAF
    # -----------------------------------------------------------------------------
    with aba_parametros:
        st.subheader("🎯 Metas Globais do Sistema e Índices do TAF")
        
        with st.form("form_parametros_qg"):
            st.markdown("##### ⏱️ Metas Diárias de Estudos & Pomodoro")
            c_m1, c_m2, c_m3 = st.columns(3)
            meta_pomodoros = c_m1.number_input("Meta Diária de Pomodoros", min_value=1, max_value=20, value=int(get_config("meta_pomodoros", "8")))
            tempo_pomodoro = c_m2.number_input("Duração do Bloco (Minutos)", min_value=15, max_value=60, value=int(get_config("tempo_pomodoro", "25")))
            tempo_descanso = c_m3.number_input("Pausa Curta (Minutos)", min_value=3, max_value=15, value=int(get_config("tempo_descanso", "5")))

            st.markdown("---")
            st.markdown("##### 🏋️‍♂️ Índices Mínimos do TAF EsPCEx")
            c_t1, c_t2, c_t3, c_t4 = st.columns(4)
            taf_corrida = c_t1.number_input("Corrida 12 min (m)", min_value=2000, value=int(get_config("taf_corrida", "2450")), step=50)
            taf_flexao = c_t2.number_input("Flexões de Braço", min_value=10, value=int(get_config("taf_flexao", "21")))
            taf_abdominal = c_t3.number_input("Abdominais (1 min)", min_value=15, value=int(get_config("taf_abdominal", "30")))
            taf_barra = c_t4.number_input("Barra Fixa", min_value=1, value=int(get_config("taf_barra", "3")))

            if st.form_submit_button("⚙️ Atualizar Parâmetros do Sistema", use_container_width=True):
                set_config("meta_pomodoros", meta_pomodoros)
                set_config("tempo_pomodoro", tempo_pomodoro)
                set_config("tempo_descanso", tempo_descanso)
                set_config("taf_corrida", taf_corrida)
                set_config("taf_flexao", taf_flexao)
                set_config("taf_abdominal", taf_abdominal)
                set_config("taf_barra", taf_barra)
                st.success("✅ Parâmetros globais atualizados!")
                st.rerun()

    # -----------------------------------------------------------------------------
    # ABA 3: SEGURANÇA, CREDENCIAIS E CONEXÕES
    # -----------------------------------------------------------------------------
    with aba_seguranca:
        st.subheader("🔒 Segurança da Conta & Controle de Acesso")
        
        with st.form("form_seguranca_senha"):
            st.markdown("##### 🔑 Alterar Senha de Acesso ao QG")
            col_s1, col_s2 = st.columns(2)
            nova_senha = col_s1.text_input("Nova Senha", type="password")
            confirma_senha = col_s2.text_input("Confirmar Nova Senha", type="password")

            if st.form_submit_button("🔒 Salvar Nova Senha"):
                if nova_senha.strip() != "" and nova_senha == confirma_senha:
                    set_config("senha_acesso", nova_senha)
                    st.success("✅ Senha alterada com sucesso! Utilize-a no próximo login.")
                else:
                    st.error("As senhas informadas não coincidem ou estão vazias.")

        st.markdown("---")
        st.markdown("##### 📱 Gestão de sessões ativas")
        st.caption("Revoga sessões em nuvem e limpa registros de autenticação legados:")

        if st.button("🧹 Revogar Todas as Sessões Ativas", key="btn_clear_sessions_master", use_container_width=True):
            try:
                _qg_revogar_todas_sessoes()
                try:
                    with sqlite3.connect("dados_qg.db") as conn:
                        conn.execute("DELETE FROM sessoes_usuario")
                except sqlite3.Error:
                    pass
                st.success("✅ As sessões remotas foram revogadas. Cada cadete precisará autenticar novamente.")
            except Exception as e:
                st.error(f"Erro ao limpar sessões: {e}")
            st.rerun()

    # -----------------------------------------------------------------------------
    # ABA 4: BANCO DE DADOS & BACKUPS
    # -----------------------------------------------------------------------------
    with aba_banco:
        st.subheader("💾 Gestão da Base de Dados Local (`dados_qg.db`)")
        st.caption("Efetue o download dos seus dados ou inspecione o uso do armazenamento SQLite.")

        col_b1, col_b2 = st.columns(2)

        with col_b1:
            st.markdown("##### 📥 Exportar Backup Completo")
            if os.path.exists("dados_qg.db"):
                with open("dados_qg.db", "rb") as fp:
                    st.download_button(
                        label="📦 Baixar Banco SQLite (.db)",
                        data=fp,
                        file_name=f"backup_qg_espcex_{datetime.date.today()}.db",
                        mime="application/x-sqlite3",
                        use_container_width=True
                    )

        with col_b2:
            st.markdown("##### 📄 Exportar Configurações em JSON")
            all_configs = get_all_configs()
            json_str = json.dumps(all_configs, indent=4, ensure_ascii=False)
            st.download_button(
                label="📄 Baixar Arquivo JSON de Configurações",
                data=json_str,
                file_name=f"configuracoes_qg_{datetime.date.today()}.json",
                mime="application/json",
                use_container_width=True
            )

        st.markdown("---")
        st.markdown("##### 📊 Diagnóstico de Tabelas e Registros Salvos")

        conn = sqlite3.connect("dados_qg.db")
        c = conn.cursor()
        c.execute("SELECT name FROM sqlite_master WHERE type='table';")
        tabelas = [r[0] for r in c.fetchall() if r[0] != "sqlite_sequence"]

        diag_data = []
        for tb in tabelas:
            try:
                c.execute(f"SELECT COUNT(*) FROM {tb}")
                count = c.fetchone()[0]
                diag_data.append({"Módulo / Tabela": tb, "Total de Registros": count, "Status": "🟢 Ativo"})
            except Exception:
                diag_data.append({"Módulo / Tabela": tb, "Total de Registros": 0, "Status": "🔴 Erro"})
        conn.close()

        if diag_data:
            st.dataframe(pd.DataFrame(diag_data), use_container_width=True)

    # -----------------------------------------------------------------------------
    # ABA 5: FERRAMENTAS & RESETAÇÃO SELETIVA
    # -----------------------------------------------------------------------------
    with aba_manutencao:
        st.subheader("🛠️ Ferramentas de Manutenção Avançada")
        st.warning("⚠️ **Atenção:** As ações abaixo alteram diretamente os dados das tabelas locais.")

        with st.expander("🗑️ Limpeza Seletiva de Módulos (Zerar Dados)"):
            st.caption("Selecione qual módulo deseja resetar individualmente sem afetar os outros:")

            c_r1, c_r2, c_r3 = st.columns(3)

            if c_r1.button("🔥 Zerar Simulados", use_container_width=True):
                conn = sqlite3.connect("dados_qg.db"); c = conn.cursor()
                c.execute("DELETE FROM simulados"); conn.commit(); conn.close()
                st.success("Tabela de simulados zerada!")
                st.rerun()

            if c_r2.button("🔥 Zerar Questões", use_container_width=True):
                conn = sqlite3.connect("dados_qg.db"); c = conn.cursor()
                c.execute("DELETE FROM baterias_questoes"); conn.commit(); conn.close()
                st.success("Histórico de questões zerado!")
                st.rerun()

            if c_r3.button("🔥 Zerar Caderno de Bizus", use_container_width=True):
                conn = sqlite3.connect("dados_qg.db"); c = conn.cursor()
                c.execute("DELETE FROM caderno_erros"); conn.commit(); conn.close()
                st.success("Caderno de bizus zerado!")
                st.rerun()

        st.markdown("---")
        st.markdown("##### 🧹 Otimização do Banco de Dados (VACUUM)")
        if st.button("⚡ Executar Otimização e Compactação do SQLite", use_container_width=True):
            conn = sqlite3.connect("dados_qg.db")
            conn.execute("VACUUM")
            conn.close()
            st.success("✅ Banco de dados otimizado com sucesso!")
# ====================================================
 # #MODULO_23_CALENDARIO_TATICO
# ====================================================
elif "módulo 23" in menu.lower() or "calendário" in menu.lower():
    import sqlite3
    import datetime
    import pandas as pd
    import streamlit as st

    st.markdown("### `#Modulo23_Calendario_Tatico`")
    st.title("📅 Módulo 23 - Calendário Tático & Agenda de Eventos")
    st.caption("Planejamento cronológico de provas, alternâncias EFA, simulados, TAF e compromissos operacionais.")

    # -----------------------------------------------------------------------------
    # 1. INFRAESTRUTURA DE BANCO DE DADOS LOCAL (SQLITE)
    # -----------------------------------------------------------------------------
    def init_db_calendario():
        conn = sqlite3.connect("dados_qg.db")
        c = conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS calendario_qg (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                data_evento TEXT NOT NULL,
                hora_evento TEXT,
                titulo TEXT NOT NULL,
                categoria TEXT NOT NULL,
                descricao TEXT,
                prioridade TEXT DEFAULT 'Média',
                status TEXT DEFAULT 'Pendente'
            )
        """)
        conn.commit()
        conn.close()

    init_db_calendario()

    def adicionar_evento(data_e, hora_e, titulo, categoria, desc, prioridade):
        conn = sqlite3.connect("dados_qg.db")
        c = conn.cursor()
        c.execute("""
            INSERT INTO calendario_qg (data_evento, hora_evento, titulo, categoria, descricao, prioridade, status)
            VALUES (?, ?, ?, ?, ?, ?, 'Pendente')
        """, (str(data_e), str(hora_e), titulo, categoria, desc, prioridade))
        conn.commit()
        conn.close()

    def atualizar_status_evento(id_evento, novo_status):
        conn = sqlite3.connect("dados_qg.db")
        c = conn.cursor()
        c.execute("UPDATE calendario_qg SET status = ? WHERE id = ?", (novo_status, id_evento))
        conn.commit()
        conn.close()

    def deletar_evento(id_evento):
        conn = sqlite3.connect("dados_qg.db")
        c = conn.cursor()
        c.execute("DELETE FROM calendario_qg WHERE id = ?", (id_evento,))
        conn.commit()
        conn.close()

    def carregar_eventos():
        conn = sqlite3.connect("dados_qg.db")
        df = pd.read_sql_query("SELECT * FROM calendario_qg ORDER BY data_evento ASC, hora_evento ASC", conn)
        conn.close()
        return df

    # -----------------------------------------------------------------------------
    # 2. NAVEGAÇÃO POR ABAS
    # -----------------------------------------------------------------------------
    aba_agenda, aba_novo, aba_gestao = st.tabs([
        "📋 Agenda & Próximos Eventos",
        "➕ Cadastrar Novo Compromisso",
        "🛠️ Gerenciar & Exportar"
    ])

    # -----------------------------------------------------------------------------
    # ABA 1: AGENDA & PRÓXIMOS EVENTOS
    # -----------------------------------------------------------------------------
    with aba_agenda:
        df_evt = carregar_eventos()
        hoje_str = str(datetime.date.today())

        # Métricas Rápidas
        tot_eventos = len(df_evt) if not df_evt.empty else 0
        tot_pendentes = len(df_evt[df_evt['status'] == 'Pendente']) if not df_evt.empty else 0
        tot_futuros = len(df_evt[df_evt['data_evento'] >= hoje_str]) if not df_evt.empty else 0

        c_m1, c_m2, c_m3 = st.columns(3)
        c_m1.metric("Total de Registros", tot_eventos)
        c_m2.metric("Eventos Pendentes", tot_pendentes)
        c_m3.metric("Próximos Compromissos", tot_futuros)

        st.markdown("---")
        st.subheader("📆 Cronograma Operacional")

        if df_evt.empty:
            st.info("Nenhum evento registrado no calendário tático. Acesse a aba **'Cadastrar Novo Compromisso'** para adicionar.")
        else:
            col_f1, col_f2 = st.columns(2)
            categorias_disponiveis = df_evt['categoria'].unique().tolist()
            cat_filtro = col_f1.multiselect("Filtrar por Categoria", options=categorias_disponiveis, default=categorias_disponiveis)
            status_filtro = col_f2.radio("Filtrar Status", ["Todos", "Pendente", "Concluído"], horizontal=True)

            df_filtrado = df_evt[df_evt['categoria'].isin(cat_filtro)]
            if status_filtro != "Todos":
                df_filtrado = df_filtrado[df_filtrado['status'] == status_filtro]

            if df_filtrado.empty:
                st.warning("Nenhum evento corresponde aos filtros selecionados.")
            else:
                for idx, row in df_filtrado.iterrows():
                    cor_prioridade = "🔴" if row['prioridade'] == "Alta" else ("🟡" if row['prioridade'] == "Média" else "🟢")
                    status_icone = "✅" if row['status'] == "Concluído" else "⏳"
                    
                    titulo_expander = f"{status_icone} {row['data_evento']} ({row['hora_evento']}) — {row['titulo']} [{row['categoria']}] {cor_prioridade}"
                    
                    with st.expander(titulo_expander):
                        st.write(f"**Descrição / Detalhes:** {row['descricao'] if row['descricao'] else 'Sem detalhes adicionais.'}")
                        st.write(f"**Prioridade:** {row['prioridade']} | **Status:** `{row['status']}`")
                        
                        c_a1, c_a2, _ = st.columns([1, 1, 2])
                        if row['status'] == "Pendente":
                            if c_a1.button("✅ Marcar Concluído", key=f"btn_concluir_{row['id']}"):
                                atualizar_status_evento(row['id'], "Concluído")
                                st.success("Evento concluído!")
                                st.rerun()
                        else:
                            if c_a1.button("🔄 Reabrir Evento", key=f"btn_reabrir_{row['id']}"):
                                atualizar_status_evento(row['id'], "Pendente")
                                st.info("Evento reaberto!")
                                st.rerun()
                                
                        if c_a2.button("🗑️ Excluir", key=f"btn_del_{row['id']}"):
                            deletar_evento(row['id'])
                            st.warning("Evento removido!")
                            st.rerun()

    # -----------------------------------------------------------------------------
    # ABA 2: CADASTRAR NOVO COMPROMISSO
    # -----------------------------------------------------------------------------
    with aba_novo:
        st.subheader("➕ Novo Evento no Calendário")
        
        with st.form("form_novo_evento_qg", clear_on_submit=True):
            col_e1, col_e2 = st.columns(2)
            titulo_evt = col_e1.text_input("Título do Compromisso / Evento *", placeholder="Ex: Simulado EsPCEx 1º Dia")
            categoria_evt = col_e2.selectbox(
                "Categoria *",
                [
                    "🎯 Concurso / Prova (EsPCEx)",
                    "🌾 EFA / AMSP (Alternância / Relatórios)",
                    "📚 Simulado / Bateria de Questões",
                    "🏋️ TAF / Teste Físico",
                    "📌 Compromisso Geral / Estudo"
                ]
            )

            col_d1, col_d2, col_d3 = st.columns(3)
            data_evt = col_d1.date_input("Data do Evento", datetime.date.today())
            hora_evt = col_d2.time_input("Horário", datetime.time(8, 0))
            prioridade_evt = col_d3.selectbox("Prioridade", ["Baixa", "Média", "Alta"], index=1)

            descricao_evt = st.text_area("Descrição / Detalhes Táticos", placeholder="Ex: Revisão geral de Química e Física. Verificar documentos.")

            if st.form_submit_button("💾 Salvar Evento no Calendário", use_container_width=True):
                if titulo_evt.strip() == "":
                    st.error("O título do compromisso é obrigatório!")
                else:
                    adicionar_evento(
                        data_e=data_evt,
                        hora_e=hora_evt.strftime("%H:%M"),
                        titulo=titulo_evt,
                        categoria=categoria_evt,
                        desc=descricao_evt,
                        prioridade=prioridade_evt
                    )
                    st.success(f"✅ Compromisso '{titulo_evt}' agendado para {data_evt} com sucesso!")
                    st.rerun()

    # -----------------------------------------------------------------------------
    # ABA 3: GERENCIAR & EXPORTAR
    # -----------------------------------------------------------------------------
    with aba_gestao:
        st.subheader("🛠️ Tabela Geral do Calendário (`calendario_qg`)")
        df_completo = carregar_eventos()
        
        if not df_completo.empty:
            st.dataframe(df_completo, use_container_width=True)
            
            csv_data = df_completo.to_csv(index=False).encode('utf-8')
            st.download_button(
                label="📥 Baixar Calendário Completo (.csv)",
                data=csv_data,
                file_name=f"calendario_tatico_qg_{datetime.date.today()}.csv",
                mime="text/csv",
                use_container_width=True
            )
        else:
            st.info("Nenhum dado cadastrado para exportação.")
#==========================================================================
# #MODULO_24_PAINEL_ADMINISTRADOR_EXPANDIDO
#===========================================================================
# #MODULO_24_PAINEL_ADMINISTRADOR_ULTIMATE_PREMIUM
elif "módulo 24" in menu.lower():
    import platform
    import sys
    import plotly.express as px
    
    st.markdown("### `#Area_Administrativa_Restrita`")
    titulo_pagina_admin = "Módulo 24 · Central Administrativa"
    st.title(titulo_pagina_admin)
    st.caption("Central privada da conta proprietária para operação, dados, manutenção, segurança e inteligência do QG.")
    st.divider()

    # -----------------------------------------------------------------------------
    # 1. INFRAESTRUTURA DE BANCO DE DADOS, LOGS E DISPAROS
    # -----------------------------------------------------------------------------
    def init_db_admin_completo():
        conn = sqlite3.connect("dados_qg.db")
        c = conn.cursor()
        c.execute("CREATE TABLE IF NOT EXISTS config_qg (chave TEXT PRIMARY KEY, valor TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS logs_sistema (id INTEGER PRIMARY KEY AUTOINCREMENT, data_hora TEXT NOT NULL, acao TEXT NOT NULL, detalhe TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS usuarios_qg (id INTEGER PRIMARY KEY AUTOINCREMENT, nome_usuario TEXT UNIQUE, data_cadastro TEXT, ultimo_acesso TEXT)")
        conn.commit()
        conn.close()

    init_db_admin_completo()

    def get_config_adm(chave, padrao=""):
        try:
            conn = sqlite3.connect("dados_qg.db")
            c = conn.cursor()
            c.execute("SELECT valor FROM config_qg WHERE chave = ?", (chave,))
            res = c.fetchone()
            conn.close()
            return res[0] if res and res[0].strip() != "" else padrao
        except Exception: return padrao

    def set_config_adm(chave, valor):
        conn = sqlite3.connect("dados_qg.db")
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO config_qg (chave, valor) VALUES (?, ?)", (chave, str(valor)))
        conn.commit()
        conn.close()

    def registrar_log(acao, detalhe=""):
        try:
            conn = sqlite3.connect("dados_qg.db")
            c = conn.cursor()
            c.execute("INSERT INTO logs_sistema (data_hora, acao, detalhe) VALUES (?, ?, ?)",
                      (datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), acao, detalhe))
            conn.commit()
            conn.close()
        except Exception: pass

    def get_total_registros_adm(tabela):
        try:
            conn = sqlite3.connect("dados_qg.db")
            c = conn.cursor()
            c.execute(f"SELECT COUNT(*) FROM {tabela}")
            res = c.fetchone()[0]
            conn.close()
            return res
        except Exception: return 0

    def enviar_discord_webhook(webhook_url, titulo, mensagem, cor=0x2e7d32, campos=None):
        try:
            if not webhook_url: return False, "Webhook não configurado."
            payload = {
                "username": "Servidor Tático — QG EsPCEx", "avatar_url": "https://i.imgur.com/4M34hi2.png",
                "embeds": [{"title": titulo, "description": mensagem, "color": cor, "fields": campos if campos else [], "footer": {"text": "QG EsPCEx | Monitoramento Ativo"}, "timestamp": datetime.datetime.utcnow().isoformat() + "Z"}]
            }
            req = urllib.request.Request(webhook_url, data=json.dumps(payload).encode('utf-8'), headers={'Content-Type': 'application/json', 'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=10) as response: return True, "Enviado com sucesso"
        except Exception as e: return False, str(e)

    # -----------------------------------------------------------------------------
    # 2. AUTENTICAÇÃO SEGURA MASTER (DESIGN EM CARTÃO)
    # -----------------------------------------------------------------------------
    usuarios_admin = carregar_usuarios()
    primeiro_usuario_admin = next(iter(usuarios_admin), "")
    admin_owner = str(get_config_adm("admin_owner", "") or "").strip()
    if not admin_owner and primeiro_usuario_admin:
        admin_owner = primeiro_usuario_admin
        set_config_adm("admin_owner", admin_owner)

    pin_admin_salvo = str(get_config_adm("senha_admin", "") or "").strip()
    if pin_admin_salvo == "1911":
        pin_admin_salvo = ""
    pin_admin_salvo = pin_admin_salvo or os.environ.get("QG_ADMIN_PIN", "").strip()
    pin_temporario = get_config_adm("admin_pin_temporario", "0") == "1"
    if pin_temporario:
        # Migra a antiga credencial pública de primeiro acesso e exige uma nova senha privada.
        pin_admin_salvo = os.environ.get("QG_ADMIN_PIN", "").strip()
        set_config_adm("senha_admin", "")
        set_config_adm("admin_pin_temporario", "0")
        pin_temporario = False

    if "admin_autenticado" not in st.session_state: st.session_state["admin_autenticado"] = False
    if st.session_state["admin_autenticado"] and user_atual != admin_owner:
        st.session_state["admin_autenticado"] = False
        st.session_state.pop("admin_ultima_atividade", None)
    try:
        timeout_admin_minutos = max(5, min(120, int(get_config_adm("admin_timeout_minutes", "20"))))
    except (TypeError, ValueError):
        timeout_admin_minutos = 20

    if st.session_state["admin_autenticado"]:
        if time.time() - st.session_state.get("admin_ultima_atividade", time.time()) > timeout_admin_minutos * 60:
            st.session_state["admin_autenticado"] = False
            st.session_state.pop("admin_ultima_atividade", None)
            st.warning("A sessão administrativa expirou por inatividade.")
        else:
            st.session_state["admin_ultima_atividade"] = time.time()

    if not st.session_state["admin_autenticado"]:
        col_espaco1, col_login, col_espaco2 = st.columns([1, 2, 1])
        with col_login:
            st.warning("🛡️ ZONA RESTRITA · Acesso exclusivo à conta proprietária")
            if not admin_owner or user_atual != admin_owner:
                st.error("Somente a conta que criou o primeiro cadastro do QG pode acessar o painel administrativo.")
            elif not pin_admin_salvo:
                st.info("Primeiro acesso: crie uma senha master privada para proteger esta área.")
                with st.form("form_configurar_pin_admin"):
                    pin_nova_master = st.text_input("Criar senha master", type="password", help="Use pelo menos 12 caracteres, incluindo letras e números.")
                    pin_confirma_master = st.text_input("Confirmar senha master", type="password")
                    if st.form_submit_button("Criar senha e abrir a área", type="primary", use_container_width=True):
                        if not senha_forte(pin_nova_master):
                            st.error("Use pelo menos 12 caracteres, incluindo letras e números.")
                        elif pin_nova_master != pin_confirma_master:
                            st.error("As senhas não coincidem.")
                        else:
                            set_config_adm("senha_admin", hash_senha(pin_nova_master))
                            set_config_adm("admin_pin_temporario", "0")
                            set_config_adm("admin_owner", user_atual)
                            set_config_adm("data_criacao_senha_admin", datetime.datetime.now().strftime("%d/%m/%Y %H:%M:%S"))
                            registrar_log("CONFIG_ADMIN", "Senha master privada criada pela conta proprietária.")
                            st.session_state["admin_autenticado"] = True
                            st.session_state["admin_ultima_atividade"] = time.time()
                            st.rerun()
            else:
                with st.form("form_login_admin_master"):
                    pin_digitado = st.text_input("🔑 Sua senha administrativa", type="password")
                    if st.form_submit_button("🔓 Entrar no painel", use_container_width=True):
                        pin_clean = pin_digitado.strip()
                        espera_admin = tempo_bloqueio("admin-master")
                        if espera_admin:
                            st.error(f"Muitas tentativas. Aguarde {espera_admin}s.")
                        elif verificar_credencial(pin_clean, pin_admin_salvo):
                            registrar_tentativa("admin-master", True)
                            st.session_state["admin_autenticado"] = True
                            st.session_state["admin_ultima_atividade"] = time.time()
                            set_config_adm("senha_admin", hash_senha(pin_clean))
                            set_config_adm("admin_owner", user_atual)
                            registrar_log("LOGIN_ADMIN", "Acesso autorizado à conta proprietária.")
                            st.rerun()
                        else:
                            registrar_tentativa("admin-master", False)
                            registrar_log("FALHA_LOGIN_ADMIN", "Tentativa de acesso administrativo não autorizada.")
                            st.error("Senha incorreta.")

    # -----------------------------------------------------------------------------
    # 3. MÓDULOS PRIVADOS DESBLOQUEADOS
    # -----------------------------------------------------------------------------
    else:
        col_adm_h1, col_adm_h2 = st.columns([3, 1])
        col_adm_h1.success(f"🟢 **CONEXÃO SEGURA ESTABELECIDA** — Sessão protegida por {timeout_admin_minutos} min de inatividade")
        if col_adm_h2.button("🔒 Encerrar Sessão", type="primary", use_container_width=True):
            st.session_state["admin_autenticado"] = False
            st.session_state.pop("admin_ultima_atividade", None)
            registrar_log("LOGOUT_ADMIN", "Sessão encerrada de forma segura")
            st.rerun()

        st.markdown("""
        <section class="qg-admin-console-hero">
          <div class="qg-admin-console-mark">24</div>
          <div class="qg-admin-console-copy">
            <div class="qg-admin-console-kicker">QG · CENTRAL DE CONTROLE</div>
            <div class="qg-admin-console-title">Central de operação administrativa</div>
            <div class="qg-admin-console-subtitle">Acesse relatórios, dados, manutenção, segurança e inteligência nesta área protegida.</div>
          </div>
          <div class="qg-admin-console-status"><span></span> PROPRIETÁRIO AUTENTICADO</div>
        </section>
        """, unsafe_allow_html=True)

        components.html("""
        <div class="qg-admin-clock">
          <span class="qg-admin-clock-label"><i></i> HORÁRIO OPERACIONAL · BRASÍLIA</span>
          <strong id="qg-admin-clock-value">--:--:--</strong>
          <span class="qg-admin-clock-date" id="qg-admin-clock-date">SINCRONIZANDO DATA</span>
        </div>
        <style>
          html,body{margin:0;padding:0;background:transparent;color:#eaf4ff;font-family:Inter,'Segoe UI',sans-serif}
          .qg-admin-clock{display:flex;align-items:center;gap:12px;min-height:34px;padding:0 4px;color:#dceafa}
          .qg-admin-clock-label{display:flex;align-items:center;gap:7px;color:#79a0b7;font:700 9px 'DM Mono',monospace;letter-spacing:.1em}
          .qg-admin-clock-label i{width:6px;height:6px;border-radius:50%;background:#63e5dc;box-shadow:0 0 12px #63e5dc;animation:clock-pulse 1.8s ease-in-out infinite}
          .qg-admin-clock strong{color:#eaffff;font:750 15px 'DM Mono',monospace;letter-spacing:.08em;text-shadow:0 0 15px rgba(99,229,220,.22)}
          .qg-admin-clock-date{color:#8299ae;font:600 9px 'DM Mono',monospace;letter-spacing:.08em}
          @keyframes clock-pulse{50%{opacity:.45;box-shadow:0 0 4px #63e5dc}}
          @media(max-width:560px){.qg-admin-clock{gap:8px;flex-wrap:wrap}.qg-admin-clock-label,.qg-admin-clock-date{font-size:8px}}
          @media(prefers-reduced-motion:reduce){*,*:before,*:after{animation:none!important}}
        </style>
        <script>
          (function(){
            const timeNode=document.getElementById('qg-admin-clock-value');
            const dateNode=document.getElementById('qg-admin-clock-date');
            if(!timeNode||!dateNode)return;
            const locale='pt-BR';
            const optionsTime={timeZone:'America/Sao_Paulo',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false};
            const optionsDate={timeZone:'America/Sao_Paulo',weekday:'short',day:'2-digit',month:'short',year:'numeric'};
            function updateClock(){
              const now=new Date();
              timeNode.textContent=new Intl.DateTimeFormat(locale,optionsTime).format(now);
              dateNode.textContent=new Intl.DateTimeFormat(locale,optionsDate).format(now).toUpperCase();
            }
            updateClock();
            window.setInterval(updateClock,1000);
          })();
        </script>
        """, height=42, scrolling=False)

        if st.session_state.get("qg_admin_ferramenta") not in QG_ADMIN_MODULOS_INTERNOS:
            st.session_state["qg_admin_ferramenta"] = QG_ADMIN_MODULOS_INTERNOS[0]
        menu = st.radio(
            "Ferramentas do Módulo 24",
            options=QG_ADMIN_MODULOS_INTERNOS,
            horizontal=True,
            format_func=lambda ferramenta: (
                ferramenta.split("Admin ·", 1)[-1].strip()
                if "Admin ·" in ferramenta
                else ferramenta.split("·", 1)[-1].strip()
            ),
            key="qg_admin_ferramenta",
        )
        st.divider()

        if menu == "🛡️ Módulo 24 · Ajustes Privados":
            st.caption("Parâmetros globais de funcionamento, editáveis somente pela conta proprietária.")
            st.info(f"Conta proprietária: **{html.escape(admin_owner or 'não configurada')}**")
            with st.form("form_ajustes_privados_qg"):
                timeout_admin_novo = st.number_input(
                    "Encerrar sessão administrativa após inatividade (minutos)",
                    min_value=5, max_value=120, step=5, value=timeout_admin_minutos,
                    help="O novo limite passa a valer nas próximas verificações da sessão.",
                )
                try:
                    limite_importacao_atual = max(1, min(100, int(get_config_adm("limite_importacao_mb", "20"))))
                except (TypeError, ValueError):
                    limite_importacao_atual = 20
                try:
                    limite_backup_atual = max(10, min(250, int(get_config_adm("limite_backup_mb", "100"))))
                except (TypeError, ValueError):
                    limite_backup_atual = 100
                limite_importacao_novo = st.number_input(
                    "Tamanho máximo para importações (MB)", min_value=1, max_value=100,
                    step=1, value=limite_importacao_atual,
                )
                limite_backup_novo = st.number_input(
                    "Tamanho máximo para restaurar backup do banco (MB)", min_value=10, max_value=250,
                    step=10, value=limite_backup_atual,
                )
                if st.form_submit_button("💾 Salvar ajustes privados", type="primary", use_container_width=True):
                    set_config_adm("admin_timeout_minutes", int(timeout_admin_novo))
                    set_config_adm("limite_importacao_mb", int(limite_importacao_novo))
                    set_config_adm("limite_backup_mb", int(limite_backup_novo))
                    registrar_log("AJUSTES_PRIVADOS", "Parâmetros de sessão e limites de arquivo atualizados.")
                    st.success("Ajustes privados atualizados.")
                    st.rerun()
            st.caption("Os limites de importação e restauração são aplicados nos módulos correspondentes.")

        # =========================================================================
        # ABA 1: DISCORD BOT
        # =========================================================================
        if menu == "🎮 Admin · Discord Bot":
            st.subheader("🎮 Integração Tática com Discord")
            webhook_atual = get_config_adm("discord_webhook_url", "").strip()
            
            with st.expander("⚙️ Configurar Webhook URL", expanded=not bool(webhook_atual)):
                with st.form("form_config_discord"):
                    url_discord_input = st.text_input("Endereço do Webhook", value=webhook_atual, type="password")
                    if st.form_submit_button("💾 Salvar Integração", use_container_width=True):
                        set_config_adm("discord_webhook_url", url_discord_input.strip())
                        st.success("✅ Servidor sincronizado!")
                        st.rerun()
            
            st.divider()
            col_d1, col_d2 = st.columns(2)
            
            with col_d1:
                st.markdown("#### 📡 Transmissão Manual")
                titulo_custom = st.text_input("Título", value="🎖️ Atualização do QG")
                msg_custom = st.text_area("Mensagem", value="Operação de manutenção concluída.")
                cor_custom = st.color_picker("Cor da Etiqueta", "#1E88E5")
                if st.button("📤 Disparar Alerta", type="primary", use_container_width=True):
                    if webhook_atual:
                        enviar_discord_webhook(webhook_atual, titulo_custom, msg_custom, cor=int(cor_custom.replace("#", ""), 16))
                        st.toast("✅ Transmissão enviada para a base!")
                    else: st.error("Configure o Webhook!")
            
            with col_d2:
                st.markdown("#### 🚨 Simulações de Defesa")
                st.info("Utilize estes botões para testar a comunicação do servidor.")
                if st.button("🔔 Testar Alerta de Novo Registo", use_container_width=True):
                    enviar_discord_webhook(webhook_atual, "👤 NOVO CADETE", "Teste de comunicação bem-sucedido.", 0x2E7D32)
                if st.button("🧨 Simular Falha Crítica", use_container_width=True):
                    enviar_discord_webhook(webhook_atual, "🚨 FALHA CRÍTICA (TESTE)", "Simulação de queda do banco de dados.", 0xFF0000)

        # =========================================================================
        # ABA 2: RELATÓRIOS DÍNAMICOS
        # =========================================================================
        if menu == "📈 Admin · Relatórios":
            st.subheader("📈 Boletim Tático de Desempenho")
            
            c_rel1, c_rel2 = st.columns(2)
            total_users = get_total_registros_adm('usuarios_qg')
            total_logs = get_total_registros_adm('logs_sistema')
            
            with c_rel1:
                st.metric("Total de Cadetes (Efetivo)", total_users)
            with c_rel2:
                st.metric("Ações Auditadas (Eventos)", total_logs)

            resumo_relatorio_adm = pd.DataFrame([
                {"Indicador": "Cadetes cadastrados", "Valor": total_users},
                {"Indicador": "Ações auditadas", "Valor": total_logs},
                {"Indicador": "Gerado em", "Valor": datetime.datetime.now().strftime("%d/%m/%Y %H:%M:%S")},
            ])
            st.download_button(
                "📥 Exportar relatório resumido (CSV)",
                data=resumo_relatorio_adm.to_csv(index=False).encode("utf-8-sig"),
                file_name=f"relatorio_administrativo_{datetime.date.today()}.csv",
                mime="text/csv",
                use_container_width=True,
            )
            
            st.divider()
            
            # Gerador de Gráfico Visual usando Plotly
            try:
                conn = sqlite3.connect("dados_qg.db")
                df_logs = pd.read_sql_query("SELECT acao FROM logs_sistema LIMIT 200", conn)
                conn.close()
                if not df_logs.empty:
                    st.markdown("#### 📊 Distribuição de Eventos no Sistema")
                    fig = px.pie(df_logs, names='acao', hole=0.4, color_discrete_sequence=px.colors.sequential.Greens_r)
                    fig.update_layout(margin=dict(t=0, b=0, l=0, r=0), paper_bgcolor="rgba(0,0,0,0)")
                    st.plotly_chart(fig, use_container_width=True)
            except: pass

        # =========================================================================
        # ABA 3: CARGA MASSIVA (DESIGN APRIMORADO)
        # =========================================================================
        if menu == "📥 Admin · Importação":
            st.subheader("📥 Central de Importação de Dados")
            try:
                conn = sqlite3.connect("dados_qg.db")
                c = conn.cursor()
                c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT IN ('sqlite_sequence', 'config_qg', 'logs_sistema', 'usuarios_qg');")
                tabelas = [r[0] for r in c.fetchall()]
                conn.close()
                
                if tabelas:
                    tabela_destino = st.selectbox("📂 Selecione o Banco de Destino:", tabelas)
                    arquivo_upload = st.file_uploader("Submeta o ficheiro CSV ou Excel", type=["csv", "xlsx"])
                    
                    if arquivo_upload:
                        try:
                            limite_importacao_mb = max(1, min(100, int(get_config_adm("limite_importacao_mb", "20"))))
                        except (TypeError, ValueError):
                            limite_importacao_mb = 20
                        if arquivo_upload.size > limite_importacao_mb * 1024 * 1024:
                            raise ValueError(f"O arquivo excede o limite configurado de {limite_importacao_mb} MB.")
                        with st.spinner("Analisando dados..."):
                            df_import = pd.read_csv(arquivo_upload) if arquivo_upload.name.endswith(".csv") else pd.read_excel(arquivo_upload)
                            st.markdown("#### 🔎 Editor e Pré-visualização")
                            st.caption("Altere os dados diretamente na tabela abaixo antes de confirmar a gravação.")
                            df_editado = st.data_editor(df_import, use_container_width=True, num_rows="dynamic")
                        
                        if st.button("🚀 Injetar Dados no Banco", type="primary", use_container_width=True):
                            conn = sqlite3.connect("dados_qg.db")
                            df_editado.to_sql(tabela_destino, conn, if_exists="append", index=False)
                            conn.close()
                            registrar_log("CARGA_MASSIVA", f"{len(df_editado)} registros em {tabela_destino}")
                            st.success(f"✅ {len(df_editado)} linhas fundidas com sucesso em `{tabela_destino}`!")
                else: st.info("Não existem tabelas operacionais disponíveis para importação.")
            except Exception as e: st.error(f"Erro na importação: {e}")

        # =========================================================================
        # ABA 4: SQL CONSOLE
        # =========================================================================
        if menu == "💻 Admin · Console SQL":
            st.subheader("💻 Terminal SQL Avançado")
            st.caption("Acesso direto de baixo nível ao banco SQLite.")
            
            snippet = st.selectbox("⚡ Snippets de Comando Rápido:", [
                "Comando Livre", "Listar Todas as Tabelas", "Ver Configurações", "Limpar Logs (Manter 100)"
            ])
            
            dict_snippets = {
                "Comando Livre": "",
                "Listar Todas as Tabelas": "SELECT name FROM sqlite_master WHERE type='table';",
                "Ver Configurações": "SELECT * FROM config_qg;",
                "Limpar Logs (Manter 100)": "DELETE FROM logs_sistema WHERE id NOT IN (SELECT id FROM logs_sistema ORDER BY id DESC LIMIT 100);"
            }
            
            query_sql = st.text_area("Instrução SQL:", value=dict_snippets[snippet], height=120)
            
            if st.button("⚙️ Processar Query", type="primary", use_container_width=True):
                if query_sql.strip():
                    try:
                        conn = sqlite3.connect("dados_qg.db")
                        if query_sql.strip().upper().startswith("SELECT") or query_sql.strip().upper().startswith("PRAGMA"):
                            df_res = pd.read_sql_query(query_sql, conn)
                            st.dataframe(df_res, use_container_width=True)
                        else:
                            c = conn.cursor()
                            c.execute(query_sql)
                            conn.commit()
                            st.success(f"✅ Query executada. Linhas modificadas: {c.rowcount}")
                            registrar_log("SQL_CONSOLE", "Comando de alteração executado.")
                        conn.close()
                    except Exception as e_sql: st.error(f"Erro de Sintaxe SQL: {e_sql}")

        # =========================================================================
        # ABA 5: LOGS INTERATIVOS COM CORES
        # =========================================================================
        if menu == "📜 Admin · Logs":
            st.subheader("📜 Registo de Auditoria Global")
            try:
                conn = sqlite3.connect("dados_qg.db")
                df_logs = pd.read_sql_query("SELECT id as ID, data_hora as Data, acao as Evento, detalhe as Detalhe FROM logs_sistema ORDER BY id DESC", conn)
                conn.close()
                
                if not df_logs.empty:
                    col_l1, col_l2 = st.columns([1, 2])
                    filtro_acao = col_l1.multiselect("Filtrar Evento:", df_logs['Evento'].unique())
                    busca_log = col_l2.text_input("🔍 Procurar no detalhe do log:")
                    
                    if filtro_acao: df_logs = df_logs[df_logs['Evento'].isin(filtro_acao)]
                    if busca_log: df_logs = df_logs[df_logs['Detalhe'].str.contains(busca_log, case=False, na=False)]
                    
                    st.dataframe(df_logs, use_container_width=True, hide_index=True)
                    st.download_button(
                        "📥 Exportar logs filtrados (CSV)",
                        data=df_logs.to_csv(index=False).encode("utf-8-sig"),
                        file_name=f"logs_qg_{datetime.date.today()}.csv",
                        mime="text/csv",
                        use_container_width=True,
                    )
                else: st.info("Sem registos de auditoria até ao momento.")
            except: pass

        # =========================================================================
        # ABA 6: TELEMETRIA
        # =========================================================================
        if menu == "📡 Admin · Telemetria":
            st.subheader("📊 Telemetria e Saúde do Servidor")
            c_t1, c_t2, c_t3 = st.columns(3)
            
            db_size = round(os.path.getsize('dados_qg.db') / 1024, 2) if os.path.exists("dados_qg.db") else 0
            c_t1.metric("📦 Carga do Banco (SQLite)", f"{db_size} KB")
            c_t2.metric("🐍 Núcleo Python", sys.version.split(' ')[0])
            c_t3.metric("🖥️ Sistema Operativo", platform.system())
            
            st.progress(min(db_size / 50000, 1.0), text="Uso estimado do limite seguro do SQLite (50MB)")

        # =========================================================================
        # ABA 7: MODO DE MANUTENÇÃO (DESIGN DE EMERGÊNCIA)
        # =========================================================================
        if menu == "🛠️ Admin · Manutenção":
            st.subheader("🚨 Protocolo de Defesa e Isolamento")
            st.caption("Corta instantaneamente o acesso a todo o efetivo.")
            
            status_manutencao = get_config_adm("modo_manutencao", "Desativado")
            
            if status_manutencao == "Ativado":
                st.error("🔴 **ISOLAMENTO ATIVO!** O sistema está completamente inacessível para não-administradores.", icon="⛔")
            else:
                st.success("🟢 **SISTEMA ONLINE.** Tráfego e acesso normais.", icon="✅")

            st.divider()
            col_lock1, col_lock2 = st.columns(2)
            if col_lock1.button("🔒 ATIVAR PROTOCOLO DE MANUTENÇÃO", type="primary", use_container_width=True):
                set_config_adm("modo_manutencao", "Ativado")
                registrar_log("MODO_MANUTENCAO", "Ativado")
                st.rerun()
            if col_lock2.button("🔓 CANCELAR PROTOCOLO (LIBERAR SITE)", use_container_width=True):
                set_config_adm("modo_manutencao", "Desativado")
                registrar_log("MODO_MANUTENCAO", "Desativado")
                st.rerun()

        # =========================================================================
        # ABA 8: GERENCIADOR E BACKUP
        # =========================================================================
        if menu == "🗄️ Admin · Banco de Dados":
            st.subheader("📂 Backup e restauração segura do banco")
            caminho_db = Path("dados_qg.db")

            def _snapshot_banco_admin():
                """Gera uma cópia consistente do SQLite, incluindo transações em WAL."""
                fd_snapshot, nome_snapshot = tempfile.mkstemp(prefix="qg-backup-", suffix=".db")
                os.close(fd_snapshot)
                origem = destino = None
                try:
                    origem = sqlite3.connect(str(caminho_db), timeout=15)
                    destino = sqlite3.connect(nome_snapshot, timeout=15)
                    origem.backup(destino)
                    destino.close()
                    destino = None
                    origem.close()
                    origem = None
                    return Path(nome_snapshot).read_bytes()
                finally:
                    if destino is not None:
                        destino.close()
                    if origem is not None:
                        origem.close()
                    try:
                        os.unlink(nome_snapshot)
                    except OSError:
                        pass

            if caminho_db.is_file():
                try:
                    snapshot_atual = _snapshot_banco_admin()
                    st.download_button(
                        label="📥 Exportar cópia consistente do banco",
                        data=snapshot_atual,
                        file_name=f"Backup_QG_EsPCEx_{datetime.date.today()}.db",
                        mime="application/octet-stream", type="primary", use_container_width=True,
                    )
                except (sqlite3.Error, OSError) as erro_snapshot:
                    st.error(f"Não foi possível criar o backup agora: {erro_snapshot}")

            with st.expander("♻️ Restaurar banco administrativo", expanded=False):
                st.warning("A restauração substitui os dados compartilhados do painel. Antes da troca, o app salva automaticamente uma cópia do banco atual na pasta backups_qg.")
                arquivo_db = st.file_uploader("Selecione um backup SQLite (.db)", type=["db", "sqlite", "sqlite3"], key="upload_backup_sqlite_admin")
                confirmar_db = st.checkbox("Entendo que a restauração substituirá o banco compartilhado.", key="confirmar_restore_db_admin")
                frase_restore = st.text_input("Digite RESTAURAR para confirmar", key="frase_restore_db_admin")

                if st.button("Restaurar banco compartilhado", type="primary", disabled=not (arquivo_db and confirmar_db and frase_restore.strip().upper() == "RESTAURAR"), use_container_width=True):
                    tmp_restore = None
                    try:
                        dados_upload = arquivo_db.getvalue()
                        try:
                            limite_backup_mb = max(10, min(250, int(get_config_adm("limite_backup_mb", "100"))))
                        except (TypeError, ValueError):
                            limite_backup_mb = 100
                        if not dados_upload or len(dados_upload) > limite_backup_mb * 1024 * 1024:
                            raise ValueError(f"O arquivo está vazio ou excede o limite configurado de {limite_backup_mb} MB.")

                        fd_restore, nome_restore = tempfile.mkstemp(prefix=".qg-restore-", suffix=".db", dir=str(caminho_db.resolve().parent))
                        tmp_restore = Path(nome_restore)
                        with os.fdopen(fd_restore, "wb") as arquivo_tmp:
                            arquivo_tmp.write(dados_upload)
                            arquivo_tmp.flush()
                            os.fsync(arquivo_tmp.fileno())

                        banco_validacao = sqlite3.connect(str(tmp_restore), timeout=15)
                        try:
                            integridade = banco_validacao.execute("PRAGMA integrity_check").fetchone()
                            tabelas = {linha[0] for linha in banco_validacao.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                            obrigatorias = {"config_qg", "usuarios_qg", "logs_sistema"}
                            if not integridade or integridade[0] != "ok":
                                raise ValueError("O SQLite informou que o arquivo está corrompido.")
                            if not obrigatorias.issubset(tabelas):
                                raise ValueError("Este arquivo não parece ser um backup válido do banco do QG EsPCEx.")
                            colunas_necessarias = {
                                "config_qg": {"chave", "valor"},
                                "usuarios_qg": {"id", "nome_usuario", "data_cadastro", "ultimo_acesso"},
                                "logs_sistema": {"id", "data_hora", "acao", "detalhe"},
                            }
                            for nome_tabela, colunas in colunas_necessarias.items():
                                colunas_arquivo = {linha[1] for linha in banco_validacao.execute(f"PRAGMA table_info({nome_tabela})")}
                                if not colunas.issubset(colunas_arquivo):
                                    raise ValueError("O arquivo tem uma estrutura incompatível com o QG EsPCEx.")

                            # Mantém a credencial e a conta proprietária atuais mesmo ao restaurar dados antigos.
                            conn_atual = sqlite3.connect(str(caminho_db), timeout=15)
                            try:
                                seguranca_atual = dict(conn_atual.execute(
                                "SELECT chave, valor FROM config_qg WHERE chave IN ('senha_admin', 'admin_owner', 'admin_pin_temporario', 'data_criacao_senha_admin', 'admin_timeout_minutes', 'limite_importacao_mb', 'limite_backup_mb')"
                                ).fetchall())
                            finally:
                                conn_atual.close()
                            for chave_segura in ("senha_admin", "admin_owner", "admin_pin_temporario", "data_criacao_senha_admin", "admin_timeout_minutes", "limite_importacao_mb", "limite_backup_mb"):
                                if chave_segura in seguranca_atual:
                                    banco_validacao.execute("INSERT OR REPLACE INTO config_qg (chave, valor) VALUES (?, ?)", (chave_segura, seguranca_atual[chave_segura]))
                                else:
                                    banco_validacao.execute("DELETE FROM config_qg WHERE chave = ?", (chave_segura,))
                            banco_validacao.commit()
                            banco_validacao.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                            banco_validacao.execute("PRAGMA journal_mode=DELETE")
                        finally:
                            banco_validacao.close()

                        for sufixo in ("-wal", "-shm"):
                            sidecar_temp = Path(str(tmp_restore) + sufixo)
                            if sidecar_temp.exists():
                                sidecar_temp.unlink()

                        snapshot_anterior = _snapshot_banco_admin()
                        pasta_backups = Path("backups_qg")
                        pasta_backups.mkdir(parents=True, exist_ok=True)
                        nome_backup_anterior = pasta_backups / f"dados_qg_pre_restore_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}.db"
                        nome_backup_anterior.write_bytes(snapshot_anterior)

                        conn_checkpoint = sqlite3.connect(str(caminho_db), timeout=15)
                        try:
                            conn_checkpoint.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                        finally:
                            conn_checkpoint.close()
                        for sufixo in ("-wal", "-shm"):
                            sidecar = Path(str(caminho_db) + sufixo)
                            if sidecar.exists():
                                sidecar.unlink()
                        os.replace(tmp_restore, caminho_db)
                        tmp_restore = None
                        _qg_persistir_sqlite_restaurado()
                        registrar_log("DB_RESTAURADO", f"Banco restaurado pelo proprietário; cópia anterior: {nome_backup_anterior.name}")
                        st.success(f"Banco restaurado. Cópia anterior salva em {nome_backup_anterior}.")
                        st.rerun()
                    except (OSError, sqlite3.Error, ValueError) as erro_restore:
                        st.error(f"Restauração cancelada: {erro_restore}")
                    finally:
                        if tmp_restore is not None:
                            try:
                                tmp_restore.unlink(missing_ok=True)
                            except OSError:
                                pass
            
            with st.expander("Ver ficheiros do servidor local"):
                lista_arquivos = [f for f in os.listdir(".") if os.path.isfile(f)]
                df_files = pd.DataFrame({"Ficheiro": lista_arquivos, "Tamanho (KB)": [round(os.path.getsize(f)/1024, 2) for f in lista_arquivos]})
                st.dataframe(df_files, hide_index=True, use_container_width=True)

        # =========================================================================
        # ABA 9: SEGURANÇA AVANÇADA (PIN MASTER CORRIGIDO)
        # =========================================================================
        if menu == "🔐 Admin · Segurança":
            st.subheader("🔑 Gestão de Credenciais Master")
            st.info(f"📅 **Última Modificação de Credencial:** `{get_config_adm('data_criacao_senha_admin', 'Padrão Inicial')}`")
            segundos_bloqueio_admin = tempo_bloqueio("admin-master")
            if segundos_bloqueio_admin:
                st.warning(f"Proteção contra tentativas ativa. Novo acesso em {segundos_bloqueio_admin} segundos.")
            else:
                st.success("Proteção contra tentativas excessivas está ativa; não há bloqueio temporário no momento.")

            with st.expander("⚙️ Política de Redefinição", expanded=True):
                exigir_pin_antigo_salvo = get_config_adm("exigir_pin_antigo", "Sim") == "Sim"
                exigir_pin_antigo = st.toggle("🔒 Obrigatório inserir o PIN atual para permitir a troca", value=exigir_pin_antigo_salvo)
                
                status_exigir_str = "Sim" if exigir_pin_antigo else "Não"
                if status_exigir_str != get_config_adm("exigir_pin_antigo", "Sim"):
                    set_config_adm("exigir_pin_antigo", status_exigir_str)
                    st.toast("Política de segurança atualizada!")

            st.markdown("#### 📝 Atualizar Código de Acesso")
            with st.form("form_alterar_pin_master"):
                pin_antigo_input = st.text_input("🔑 PIN Atual", type="password") if exigir_pin_antigo else ""
                col_p1, col_p2 = st.columns(2)
                novo_pin_input = col_p1.text_input("🆕 Nova Credencial", type="password")
                confirma_pin_input = col_p2.text_input("✔️ Confirmar Credencial", type="password")

                if st.form_submit_button("💾 Validar e Alterar Segurança", type="primary", use_container_width=True):
                    pin_antigo_correto = str(get_config_adm("senha_admin", "") or os.environ.get("QG_ADMIN_PIN", "")).strip()
                    
                    if exigir_pin_antigo and not verificar_credencial(pin_antigo_input.strip(), pin_antigo_correto):
                        st.error("❌ O PIN Atual está incorreto. Acesso negado.")
                    elif not novo_pin_input.strip() or novo_pin_input.strip() != confirma_pin_input.strip():
                        st.error("❌ As novas credenciais não coincidem ou são inválidas.")
                    else:
                        dh = datetime.datetime.now().strftime("%d/%m/%Y %H:%M:%S")
                        set_config_adm("senha_admin", hash_senha(novo_pin_input.strip()))
                        set_config_adm("admin_pin_temporario", "0")
                        set_config_adm("admin_owner", user_atual)
                        set_config_adm("data_criacao_senha_admin", dh)
                        registrar_log("ALTERACAO_PIN", "Credencial master substituída.")
                        st.success("✅ **Credencial atualizada com sucesso!** O sistema só autorizará esta nova senha a partir de agora.")
                        time.sleep(1.5)
                        st.rerun()

            with st.expander("Revogar logins persistentes de todas as contas", expanded=False):
                st.warning("Esta ação desconecta as contas que marcaram para permanecer conectadas.")
                confirmar_revogacao_sessoes = st.checkbox("Confirmo revogar todas as sessões persistentes", key="confirmar_revogacao_sessoes_admin")
                if st.button("🧹 Revogar sessões persistentes", key="revogar_sessoes_admin", disabled=not confirmar_revogacao_sessoes):
                    try:
                        _qg_revogar_todas_sessoes()
                        try:
                            with sqlite3.connect("dados_qg.db", timeout=5) as conn:
                                conn.execute("DELETE FROM sessoes_usuario")
                        except sqlite3.Error:
                            pass
                        registrar_log("SESSOES_REVOGADAS", "O proprietário revogou as sessões persistentes.")
                        st.success("Sessões persistentes revogadas.")
                        st.rerun()
                    except Exception as erro_sessoes:
                        st.error(f"Não foi possível revogar as sessões: {erro_sessoes}")

        # =========================================================================
        # ABA 10: INTELIGÊNCIA DO EFETIVO (GRÁFICOS E TABELAS)
        # =========================================================================
        if menu == "🧠 Admin · Inteligência do Efetivo":
            st.subheader("👥 Inteligência e Radar do Efetivo")
            try:
                conn = sqlite3.connect("dados_qg.db")
                df_users = pd.read_sql_query("SELECT id, nome_usuario as 'Nome/Guerra', data_cadastro as 'Alistamento', ultimo_acesso as 'Último Acesso' FROM usuarios_qg ORDER BY id DESC", conn)
                conn.close()
                
                total_users = len(df_users)
                hoje_str = datetime.datetime.now().strftime("%d/%m/%Y")

                if total_users > 0:
                    c1, c2, c3 = st.columns(3)
                    c1.metric("🎖️ Efetivo Total", total_users)
                    c2.metric("🔥 Movimentação Hoje", len(df_users[df_users['Último Acesso'].str.contains(hoje_str, na=False)]))
                    c3.metric("✨ Novatos Hoje", len(df_users[df_users['Alistamento'].str.contains(hoje_str, na=False)]))

                    st.divider()
                    
                    busca = st.text_input("🔍 Localizar Cadete:")
                    df_ex = df_users[df_users["Nome/Guerra"].str.contains(busca, case=False, na=False)] if busca else df_users
                    st.dataframe(df_ex, use_container_width=True, hide_index=True)

                    with st.expander("🛠️ Ações de Correção e Baixa (Zona de Perigo)"):
                        col_b1, col_b2 = st.columns(2)
                        
                        with col_b1:
                            st.info("Editar ou Apagar Cadete")
                            with st.form("form_edicao_user"):
                                alvo = st.selectbox("Alvo:", df_users["Nome/Guerra"].tolist())
                                novo_nome = st.text_input("Corrigir Nome (Opcional):")
                                if st.form_submit_button("✏️ Renomear") and novo_nome.strip():
                                    conn = sqlite3.connect("dados_qg.db")
                                    conn.cursor().execute("UPDATE usuarios_qg SET nome_usuario = ? WHERE nome_usuario = ?", (novo_nome.strip(), alvo))
                                    conn.commit()
                                    conn.close()
                                    st.success("Nome atualizado!")
                                    st.rerun()
                                    
                                if st.form_submit_button("❌ Excluir Registo", type="primary"):
                                    conn = sqlite3.connect("dados_qg.db")
                                    conn.cursor().execute("DELETE FROM usuarios_qg WHERE nome_usuario = ?", (alvo,))
                                    conn.commit()
                                    conn.close()
                                    st.success(f"{alvo} apagado.")
                                    st.rerun()

                        with col_b2:
                            st.error("Reset Total (Wipeout)")
                            with st.form("form_wipeout"):
                                check = st.checkbox("Confirmo o expurgo de TODO o efetivo.")
                                if st.form_submit_button("💥 APAGAR TODOS", type="primary") and check:
                                    conn = sqlite3.connect("dados_qg.db")
                                    conn.cursor().execute("DELETE FROM usuarios_qg")
                                    conn.commit()
                                    conn.close()
                                    st.success("Base limpa!")
                                    st.rerun()
                else: st.info("O radar não detetou nenhum cadete. Aguarde o primeiro acesso.")
            except Exception as e: st.error(f"Falha ao carregar inteligência: {e}")

        # =========================================================================
        # ABA 11: DESTAQUE DE MISSÃO E COMUNICADO PARA A ÁREA DO QUIZ
        # =========================================================================
        if menu == "🎯 Admin · Campanhas QG":
            st.subheader("🎯 Missão em destaque e comunicado do QG")
            st.caption("Publique um desafio do catálogo e uma mensagem curta que aparecerão na área do Quiz para os usuários.")

            campanha_atual = _carregar_campanha_missoes_qg()
            opcoes_campanha = {
                f"{missao['id']} · {missao['periodo']} · {missao['titulo']} · {missao['pontos']} pts": missao["id"]
                for missao in QG_MISSOES
            }
            id_campanha_atual = str(campanha_atual.get("missao_id", "M001"))
            rotulos_campanha = list(opcoes_campanha)
            hoje_campanha_admin = datetime.date.today()
            try:
                inicio_campanha_padrao = datetime.date.fromisoformat(str(campanha_atual.get("inicio_em", hoje_campanha_admin.isoformat())))
            except ValueError:
                inicio_campanha_padrao = hoje_campanha_admin
            try:
                fim_campanha_padrao = datetime.date.fromisoformat(str(campanha_atual.get("fim_em", (hoje_campanha_admin + datetime.timedelta(days=7)).isoformat())))
            except ValueError:
                fim_campanha_padrao = hoje_campanha_admin + datetime.timedelta(days=7)
            indice_campanha = next(
                (i for i, rotulo in enumerate(rotulos_campanha) if opcoes_campanha[rotulo] == id_campanha_atual),
                0,
            )

            with st.container(border=True):
                with st.form("form_campanha_missoes_qg"):
                    rotulo_escolhido = st.selectbox(
                        "Missão para destacar no Quiz",
                        options=rotulos_campanha,
                        index=indice_campanha,
                    )
                    aviso_campanha = st.text_area(
                        "Comunicado para os usuários",
                        value=str(campanha_atual.get("aviso", ""))[:600],
                        max_chars=600,
                        placeholder="Ex.: Nesta semana, priorize revisões curtas e mantenha o treino conforme seu planejamento.",
                    )
                    data_inicio_col, data_fim_col = st.columns(2)
                    inicio_campanha = data_inicio_col.date_input("Começa em", value=inicio_campanha_padrao)
                    fim_campanha = data_fim_col.date_input("Termina em", value=fim_campanha_padrao)
                    publicar_campanha = st.checkbox(
                        "Exibir o destaque e o comunicado na área do Quiz",
                        value=bool(campanha_atual.get("ativa", False)),
                    )
                    if st.form_submit_button("💾 Publicar campanha", type="primary", use_container_width=True):
                        if fim_campanha < inicio_campanha:
                            st.error("A data final precisa ser igual ou posterior à data inicial.")
                        else:
                            campanha_nova = {
                                "missao_id": opcoes_campanha[rotulo_escolhido],
                                "aviso": aviso_campanha.strip(),
                                "ativa": bool(publicar_campanha),
                                "inicio_em": inicio_campanha.isoformat(),
                                "fim_em": fim_campanha.isoformat(),
                                "publicada_em": datetime.datetime.now().isoformat(timespec="seconds"),
                                "publicada_por": user_atual,
                            }
                            set_config_adm("campanha_missoes_qg", json.dumps(campanha_nova, ensure_ascii=False))
                            registrar_log("CAMPANHA_MISSOES", f"Campanha {'publicada' if publicar_campanha else 'salva como rascunho'}; missão {campanha_nova['missao_id']}.")
                            st.success("Campanha atualizada. Ela aparece no Quiz durante o período configurado, quando estiver ativada.")
                            st.rerun()

            if campanha_atual.get("ativa"):
                missao_preview = QG_MISSOES_POR_ID.get(str(campanha_atual.get("missao_id", "")))
                st.success("🟢 Campanha ativa")
                if missao_preview:
                    st.markdown(f"**Missão:** {missao_preview['titulo']} · {missao_preview['pontos']} pontos")
                st.caption(f"Vigência: {campanha_atual.get('inicio_em', 'hoje')} até {campanha_atual.get('fim_em', 'sem data final')}")
                if campanha_atual.get("aviso"):
                    st.info(str(campanha_atual["aviso"]))
            else:
                st.info("Nenhum comunicado está publicado no momento.")

            if st.button("🧹 Retirar campanha do Quiz", key="limpar_campanha_missoes_qg"):
                set_config_adm("campanha_missoes_qg", "{}")
                registrar_log("CAMPANHA_MISSOES_REMOVIDA", "O proprietário retirou o destaque e o comunicado do Quiz.")
                st.toast("Campanha removida da área do Quiz.", icon="✅")
                st.rerun()

            try:
                with sqlite3.connect("dados_qg.db", timeout=5) as conn:
                    historico_campanhas_df = pd.read_sql_query(
                        "SELECT data_hora AS Data, acao AS Evento, detalhe AS Detalhe FROM logs_sistema WHERE acao LIKE 'CAMPANHA_MISSOES%' ORDER BY id DESC LIMIT 20",
                        conn,
                    )
                if not historico_campanhas_df.empty:
                    st.markdown("#### 🧾 Últimas alterações")
                    st.dataframe(historico_campanhas_df, use_container_width=True, hide_index=True)
            except (sqlite3.Error, pd.errors.DatabaseError):
                st.caption("O histórico de campanhas aparecerá depois da primeira publicação.")

        if menu == "🐞 Admin · Diagnóstico & Bugs":
            st.caption("Verificações de integridade e registro privado de problemas para análise do proprietário.")
            st.info("Não inclua senhas, tokens, dados pessoais sensíveis ou chaves nos relatos.")

            diagnosticos_admin = []
            try:
                with sqlite3.connect("dados_qg.db", timeout=5) as conn:
                    integridade_db = conn.execute("PRAGMA integrity_check").fetchone()
                    tabelas_db = {linha[0] for linha in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                diagnosticos_admin.extend([
                    {"Verificação": "Integridade SQLite", "Resultado": "OK" if integridade_db and integridade_db[0] == "ok" else "Revisar"},
                    {"Verificação": "Tabela de configurações", "Resultado": "Disponível" if "config_qg" in tabelas_db else "Ausente"},
                    {"Verificação": "Tabela de usuários administrativos", "Resultado": "Disponível" if "usuarios_qg" in tabelas_db else "Ausente"},
                    {"Verificação": "Tabela de auditoria", "Resultado": "Disponível" if "logs_sistema" in tabelas_db else "Ausente"},
                ])
            except sqlite3.Error as erro_diag:
                diagnosticos_admin.append({"Verificação": "Integridade SQLite", "Resultado": f"Erro: {erro_diag}"})
            st.dataframe(pd.DataFrame(diagnosticos_admin), use_container_width=True, hide_index=True)

            with st.form("form_relato_bug_admin", clear_on_submit=True):
                titulo_bug = st.text_input("Resumo do problema", max_chars=120)
                severidade_bug = st.selectbox("Impacto", ["Baixo", "Médio", "Alto", "Crítico"])
                detalhe_bug = st.text_area("O que aconteceu?", max_chars=1500, height=140)
                if st.form_submit_button("📝 Registrar relato privado", type="primary", use_container_width=True):
                    if not titulo_bug.strip() or not detalhe_bug.strip():
                        st.error("Informe um resumo e descreva o problema.")
                    else:
                        registrar_log(
                            "BUG_REPORT",
                            f"conta={user_atual}; impacto={severidade_bug}; resumo={titulo_bug.strip()[:120]}; relato={detalhe_bug.strip()[:1500]}",
                        )
                        st.success("Relato registrado no log administrativo privado.")
                        st.rerun()

            try:
                with sqlite3.connect("dados_qg.db", timeout=5) as conn:
                    relatos_bug_df = pd.read_sql_query(
                        "SELECT data_hora AS Data, detalhe AS Relato FROM logs_sistema WHERE acao = 'BUG_REPORT' ORDER BY id DESC LIMIT 50",
                        conn,
                    )
                st.markdown("#### Relatos recentes")
                if relatos_bug_df.empty:
                    st.caption("Ainda não há relatos registrados.")
                else:
                    st.dataframe(relatos_bug_df, use_container_width=True, hide_index=True)
            except (sqlite3.Error, pd.errors.DatabaseError) as erro_bug_log:
                st.error(f"Não foi possível carregar os relatos: {erro_bug_log}")
# ===========================================================================
  # #MODULO_25_CONTROLE_FINANCEIRO_PREMIUM_ULTIMATE
   # =========================================================================
 # #MODULO_25_CONTROLE_FINANCEIRO_NIVEL_MAXIMO
elif "módulo 25" in menu.lower() or "finanças" in menu.lower() or "financeiro" in menu.lower():
    import sqlite3
    import pandas as pd
    import datetime
    import time
    import plotly.express as px
    import streamlit as st

    # --- CSS Personalizado Premium ---
    st.markdown("""
        <style>
        .stMetric {
            background-color: #121212;
            padding: 15px;
            border-radius: 8px;
            border-left: 5px solid #4CAF50;
            box-shadow: 0px 4px 6px rgba(0,0,0,0.3);
        }
        .metric-red { border-left: 5px solid #F44336 !important; }
        .insight-box {
            background-color: #1a2318;
            border: 1px solid #2e4d25;
            padding: 15px;
            border-radius: 8px;
            color: #e0e6ed;
            font-size: 14px;
        }
        </style>
    """, unsafe_allow_html=True)

    st.markdown("### `#Modulo25_Comando_Financeiro`")
    st.title("💸 Inteligência Financeira AgroLar")
    st.caption("Visão tática e auditoria do seu capital. Controle de receitas, roça e setup.")
    st.divider()

    # -----------------------------------------------------------------------------
    # 1. INFRAESTRUTURA DE BANCO DE DADOS E METAS
    # -----------------------------------------------------------------------------
    def init_db_financas_pessoais():
        conn = sqlite3.connect("dados_qg.db")
        c = conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS financas_pessoais (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fluxo TEXT NOT NULL,       
                tipo TEXT NOT NULL,        
                categoria TEXT NOT NULL,
                descricao TEXT NOT NULL,
                valor REAL NOT NULL,
                data_registo TEXT NOT NULL
            )
        """)
        # Tabela para guardar o limite de gastos do mês
        c.execute("""
            CREATE TABLE IF NOT EXISTS financas_metas (
                mes_ano TEXT PRIMARY KEY,
                limite_gastos REAL NOT NULL
            )
        """)
        conn.commit()
        conn.close()

    init_db_financas_pessoais()

    def carregar_financas():
        conn = sqlite3.connect("dados_qg.db")
        df = pd.read_sql_query("SELECT * FROM financas_pessoais ORDER BY id DESC", conn)
        conn.close()
        return df

    def get_meta_mes(mes_ano):
        try:
            conn = sqlite3.connect("dados_qg.db")
            c = conn.cursor()
            c.execute("SELECT limite_gastos FROM financas_metas WHERE mes_ano = ?", (mes_ano,))
            res = c.fetchone()
            conn.close()
            return res[0] if res else 0.0
        except: return 0.0

    df_fin = carregar_financas()
    hoje = datetime.datetime.now()
    
    # Processamento de Datas no Pandas para Filtros
    if not df_fin.empty:
        df_fin['Data_Real'] = pd.to_datetime(df_fin['data_registo'], format="%d/%m/%Y %H:%M:%S")
        df_fin['Mes_Ano'] = df_fin['Data_Real'].dt.strftime("%m/%Y")
        df_fin['Dia'] = df_fin['Data_Real'].dt.strftime("%d/%m")
        meses_disponiveis = df_fin['Mes_Ano'].unique().tolist()
    else:
        meses_disponiveis = []

    # -----------------------------------------------------------------------------
    # 2. INTERFACE DE COMANDO (ABAS)
    # -----------------------------------------------------------------------------
    aba_dash, aba_receitas, aba_despesas, aba_metas, aba_auditoria = st.tabs([
        "📊 Dashboard", 
        "📥 Receitas", 
        "📤 Despesas", 
        "🎯 Metas & Planejamento",
        "📜 Extrato Geral"
    ])

    # =========================================================================
    # ABA 1: DASHBOARD DE INTELIGÊNCIA VISUAL COM FILTROS
    # =========================================================================
    with aba_dash:
        col_f1, col_f2 = st.columns([1, 3])
        with col_f1:
            filtro_mes = st.selectbox("📅 Período de Análise:", ["Todos os Tempos"] + meses_disponiveis)
        
        # Filtra os dados conforme o mês selecionado
        if filtro_mes == "Todos os Tempos" or df_fin.empty:
            df_dash = df_fin
        else:
            df_dash = df_fin[df_fin['Mes_Ano'] == filtro_mes]

        # Recalcula as métricas para o mês filtrado
        total_rec = df_dash[df_dash['fluxo'] == 'Entrada']['valor'].sum() if not df_dash.empty else 0
        total_fix = df_dash[(df_dash['fluxo'] == 'Saida') & (df_dash['tipo'] == 'Fixo')]['valor'].sum() if not df_dash.empty else 0
        total_var = df_dash[(df_dash['fluxo'] == 'Saida') & (df_dash['tipo'] == 'Variável')]['valor'].sum() if not df_dash.empty else 0
        total_desp = total_fix + total_var
        saldo_dash = total_rec - total_desp
        perc_gasto = (total_desp / total_rec * 100) if total_rec > 0 else 0

        st.divider()

        # Painel de Métricas Superior
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("💵 Receitas", f"R$ {total_rec:,.2f}")
        c2.metric("🏢 Custos Fixos", f"R$ {total_fix:,.2f}")
        c3.metric("🎒 Custos Variáveis", f"R$ {total_var:,.2f}")
        
        if saldo_dash >= 0:
            c4.metric("🟢 SALDO LIVRE", f"R$ {saldo_dash:,.2f}")
        else:
            c4.metric("🔴 SALDO NEGATIVO", f"R$ {saldo_dash:,.2f}")

        st.markdown("<br>", unsafe_allow_html=True)

        if not df_dash.empty and total_desp > 0:
            # INSIGHTS GERADOS POR CÓDIGO (INTELIGÊNCIA)
            df_saidas = df_dash[df_dash['fluxo'] == 'Saida']
            if not df_saidas.empty:
                maior_gasto = df_saidas.groupby('categoria')['valor'].sum().idxmax()
                valor_maior_gasto = df_saidas.groupby('categoria')['valor'].sum().max()
                
                texto_insight = f"💡 **Resumo da Inteligência:** Neste período, o seu maior gargalo financeiro foi com **{maior_gasto}** (R$ {valor_maior_gasto:.2f}). "
                if perc_gasto < 50:
                    texto_insight += "Excelente disciplina! Você comprometeu menos da metade da sua renda. Capacidade de investimento alta."
                elif perc_gasto < 80:
                    texto_insight += "Seu saldo está no controle, mas atenção aos gastos variáveis para sobrar mais no final do mês."
                else:
                    texto_insight += "⚠️ **Alerta Vermelho:** Você está gastando quase tudo o que ganha. Segure as despesas não essenciais!"
                
                st.markdown(f'<div class="insight-box">{texto_insight}</div><br>', unsafe_allow_html=True)

            col_graf1, col_graf2 = st.columns([1, 2])
            
            with col_graf1:
                st.markdown("**Saúde Financeira (Consumo %)**")
                fig_gauge = go.Figure(go.Indicator(
                    mode = "gauge+number",
                    value = perc_gasto,
                    number = {'suffix': "%"},
                    domain = {'x': [0, 1], 'y': [0, 1]},
                    gauge = {
                        'axis': {'range': [None, 100], 'tickwidth': 1},
                        'bar': {'color': "rgba(0,0,0,0)"}, 
                        'steps': [
                            {'range': [0, 50], 'color': "#4CAF50"},
                            {'range': [50, 80], 'color': "#FFEB3B"},
                            {'range': [80, 100], 'color': "#F44336"}
                        ],
                        'threshold': {'line': {'color': "white", 'width': 3}, 'thickness': 0.75, 'value': perc_gasto}
                    }))
                fig_gauge.update_layout(margin=dict(t=10, b=10, l=10, r=10), paper_bgcolor="rgba(0,0,0,0)", height=200)
                st.plotly_chart(fig_gauge, use_container_width=True)

            with col_graf2:
                st.markdown("**Fluxo de Saídas por Categoria**")
                df_cat = df_saidas.groupby('categoria')['valor'].sum().reset_index().sort_values(by='valor', ascending=True)
                fig_bar = px.bar(df_cat, x='valor', y='categoria', orientation='h', text='valor', color='valor', color_continuous_scale="Reds")
                fig_bar.update_traces(texttemplate='R$ %{text:.2f}', textposition='outside')
                fig_bar.update_layout(margin=dict(t=0, b=0, l=0, r=0), paper_bgcolor="rgba(0,0,0,0)", coloraxis_showscale=False)
                st.plotly_chart(fig_bar, use_container_width=True)

            # Timeline de gastos (Gráfico de Linha/Barra de evolução)
            st.markdown("**Evolução de Gastos Diários (Timeline)**")
            df_timeline = df_saidas.groupby('Dia')['valor'].sum().reset_index()
            fig_time = px.line(df_timeline, x='Dia', y='valor', markers=True, line_shape='spline')
            fig_time.update_traces(line_color="#FF9800", marker=dict(size=8, color="white"))
            fig_time.update_layout(margin=dict(t=10, b=10, l=10, r=10), paper_bgcolor="rgba(0,0,0,0)", yaxis_title="Reais (R$)")
            st.plotly_chart(fig_time, use_container_width=True)
            
        else:
            st.info("Nenhuma movimentação registrada no período selecionado.")

    # =========================================================================
    # ABA 2 e 3: LANÇAMENTOS COM DESIGN OTIMIZADO
    # =========================================================================
    with aba_receitas:
        st.subheader("📥 Injetar Capital")
        with st.container(border=True):
            with st.form("form_receitas_max"):
                col_r1, col_r2 = st.columns(2)
                cat_receita = col_r1.selectbox("Origem:", ["Salário Mensal", "Empreitada/Roça", "Vendas Hardware", "Mesada", "Outros"])
                valor_receita = col_r1.number_input("Valor (R$):", min_value=0.0, step=50.0, format="%.2f")
                desc_receita = col_r2.text_input("Descrição:", placeholder="Detalhes da entrada...")
                
                if st.form_submit_button("💰 Processar Receita", type="primary", use_container_width=True):
                    if desc_receita and valor_receita > 0:
                        conn = sqlite3.connect("dados_qg.db")
                        conn.cursor().execute("INSERT INTO financas_pessoais (fluxo, tipo, categoria, descricao, valor, data_registo) VALUES (?, ?, ?, ?, ?, ?)",
                            ("Entrada", "Receita", cat_receita, desc_receita.strip(), valor_receita, hoje.strftime("%d/%m/%Y %H:%M:%S")))
                        conn.commit()
                        conn.close()
                        st.balloons()
                        st.toast("Sucesso!", icon="🎉")
                        st.success(f"✅ R$ {valor_receita:.2f} adicionados ao seu saldo!")
                        time.sleep(1.2)
                        st.rerun()
                    else: st.error("Preencha a descrição e valor.")

    with aba_despesas:
        st.subheader("📤 Registrar Despesa")
        with st.container(border=True):
            tipo_despesa = st.radio("Classificação:", ["🏢 Custo Fixo (Assinaturas, Mensalidades)", "🎒 Custo Variável (Alimentação, Lazer)"], horizontal=True)
            tipo_bd = "Fixo" if "Fixo" in tipo_despesa else "Variável"

            with st.form("form_despesas_max"):
                col_d1, col_d2 = st.columns(2)
                if tipo_bd == "Fixo":
                    cat_despesa = col_d1.selectbox("Categoria:", ["Internet/Celular", "Estratégia EsPCEx", "Luz/Água", "Assinaturas Diversas"])
                else:
                    cat_despesa = col_d1.selectbox("Categoria:", ["Alimentação/Lanches", "Setup (Hardware/Periféricos)", "Combustível/Transporte", "Lazer", "Material de Estudo", "Imprevistos"])
                
                valor_despesa = col_d1.number_input("Valor (R$):", min_value=0.0, step=10.0, format="%.2f")
                desc_despesa = col_d2.text_input("Descrição:", placeholder="Com o que você gastou?")

                if st.form_submit_button("💸 Registrar Saída", type="primary", use_container_width=True):
                    if desc_despesa and valor_despesa > 0:
                        conn = sqlite3.connect("dados_qg.db")
                        conn.cursor().execute("INSERT INTO financas_pessoais (fluxo, tipo, categoria, descricao, valor, data_registo) VALUES (?, ?, ?, ?, ?, ?)",
                            ("Saida", tipo_bd, cat_despesa, desc_despesa.strip(), valor_despesa, hoje.strftime("%d/%m/%Y %H:%M:%S")))
                        conn.commit()
                        conn.close()
                        st.toast("Gasto computado!", icon="💸")
                        st.success(f"✅ Débito de R$ {valor_despesa:.2f} registrado.")
                        time.sleep(1.2)
                        st.rerun()
                    else: st.error("Preencha corretamente.")

    # =========================================================================
    # ABA 4: METAS DE GASTOS (NOVIDADE!)
    # =========================================================================
    with aba_metas:
        st.subheader("🎯 Metas e Planejamento de Gastos")
        st.caption("Defina um limite de dinheiro que você se permite gastar por mês.")
        
        mes_atual_str = hoje.strftime("%m/%Y")
        meta_atual = get_meta_mes(mes_atual_str)
        
        # Filtra apenas despesas do mês atual
        gastos_mes_atual = 0.0
        if not df_fin.empty:
            df_mes = df_fin[(df_fin['Mes_Ano'] == mes_atual_str) & (df_fin['fluxo'] == 'Saida')]
            gastos_mes_atual = df_mes['valor'].sum() if not df_mes.empty else 0.0

        with st.container(border=True):
            col_m1, col_m2 = st.columns(2)
            with col_m1:
                nova_meta = st.number_input("Definir Limite de Gastos (R$):", value=meta_atual, step=50.0)
                if st.button("💾 Salvar Meta do Mês", use_container_width=True):
                    conn = sqlite3.connect("dados_qg.db")
                    conn.cursor().execute("INSERT OR REPLACE INTO financas_metas (mes_ano, limite_gastos) VALUES (?, ?)", (mes_atual_str, nova_meta))
                    conn.commit()
                    conn.close()
                    st.toast("Meta atualizada!", icon="🎯")
                    time.sleep(0.5)
                    st.rerun()
            
            with col_m2:
                if meta_atual > 0:
                    perc_meta = min(100, int((gastos_mes_atual / meta_atual) * 100))
                    st.markdown(f"**Progresso do Limite ({mes_atual_str})**")
                    st.progress(perc_meta / 100, text=f"Usado: {perc_meta}% (R$ {gastos_mes_atual:.2f} de R$ {meta_atual:.2f})")
                    
                    if perc_meta >= 100:
                        st.error("🚨 Você estourou o orçamento planejado para este mês!")
                    elif perc_meta >= 80:
                        st.warning("⚠️ Atenção! Você está perto de atingir o limite de gastos.")
                    else:
                        st.success("🟢 Tudo no controle. Orçamento folgado.")
                else:
                    st.info("Defina uma meta ao lado para ativar o monitoramento.")

    # =========================================================================
    # ABA 5: EXTRATO COMPLETO
    # =========================================================================
    with aba_auditoria:
        st.subheader("📜 Extrato Geral e Auditoria")
        if not df_fin.empty:
            df_exibicao = df_fin.copy()
            df_exibicao['Valor'] = df_exibicao['valor'].apply(lambda x: f"R$ {x:,.2f}")
            df_exibicao = df_exibicao.rename(columns={'data_registo': 'Data', 'fluxo': 'Tipo', 'categoria': 'Categoria', 'descricao': 'Descrição'})
            
            def cor_linha(row):
                if row['Tipo'] == 'Entrada': return ['background-color: rgba(76, 175, 80, 0.15)'] * len(row)
                else: return ['background-color: rgba(244, 67, 54, 0.15)'] * len(row)
            
            st.dataframe(df_exibicao[["id", "Data", "Tipo", "Categoria", "Descrição", "Valor"]].style.apply(cor_linha, axis=1), use_container_width=True, hide_index=True)

            st.divider()
            col_aud1, col_aud2 = st.columns(2)
            
            with col_aud1:
                with st.expander("🗑️ Excluir Transação"):
                    with st.form("form_exclusao"):
                        id_apagar = st.selectbox("ID da transação:", df_fin['id'].tolist())
                        if st.form_submit_button("❌ Excluir Registo", type="primary"):
                            conn = sqlite3.connect("dados_qg.db")
                            conn.cursor().execute("DELETE FROM financas_pessoais WHERE id = ?", (id_apagar,))
                            conn.commit()
                            conn.close()
                            st.toast("Excluído!", icon="🗑️")
                            time.sleep(0.5)
                            st.rerun()
                            
            with col_aud2:
                with st.expander("📥 Exportar Relatório Oficial"):
                    csv_export = df_fin.to_csv(index=False).encode('utf-8')
                    st.download_button(label="📥 Baixar Excel / CSV", data=csv_export, file_name=f"Extrato_AgroLar_{datetime.date.today()}.csv", mime="text/csv", use_container_width=True)
        else:
            st.info("O sistema está pronto para receber lançamentos.")

elif menu == "🧠 Memória Avançada":
    st.title("🧠 Memória de Aprendizado")
    st.caption("Central de conhecimento pessoal com revisão espaçada, etiquetas e acompanhamento de retenção.")

    stats_memoria = estatisticas_memoria(user_atual)
    total_memorias = stats_memoria["total"]
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Registros", total_memorias)
    m2.metric("Revisões pendentes", stats_memoria["para_revisar"])
    m3.metric("Fixadas", stats_memoria["fixadas"])
    m4.metric("Retenção", f"{stats_memoria['taxa_acerto']:.1f}%")

    with st.expander("＋ Adicionar conhecimento", expanded=total_memorias == 0):
        with st.form("form_nova_memoria_avancada", clear_on_submit=True):
            mc1, mc2, mc3 = st.columns(3)
            materia_mem = mc1.text_input("Matéria ou projeto", placeholder="Ex.: Física")
            tipo_mem = mc2.selectbox("Tipo", ["Conceito", "Fórmula", "Erro recorrente", "Estratégia", "Anotação"])
            prioridade_mem = mc3.selectbox("Prioridade", ["Baixa", "Média", "Alta"], index=1)
            texto_mem = st.text_area("O que você quer lembrar?", max_chars=3000, placeholder="Registre uma ideia, regra, dúvida ou ponto de revisão.")
            tags_mem = st.text_input("Etiquetas", placeholder="Ex.: cinemática, revisão, prova (separadas por vírgula)")
            if st.form_submit_button("Guardar na memória", type="primary", use_container_width=True):
                if texto_mem.strip():
                    adicionar_memoria(
                        user_atual, texto_mem, materia=materia_mem or "Geral", tipo=tipo_mem,
                        tags=[tag.strip() for tag in tags_mem.split(",") if tag.strip()], prioridade=prioridade_mem,
                    )
                    st.toast("Conhecimento salvo. A primeira revisão já está agendada.", icon="🧠")
                    st.rerun()
                else:
                    st.warning("Escreva uma anotação antes de salvar.")

    aba_revisao, aba_biblioteca, aba_insights = st.tabs(["🔁 Revisão de hoje", "📚 Biblioteca", "📈 Insights"])
    with aba_revisao:
        pendentes = listar_memorias(user_atual, limite=500, apenas_revisar=True)
        if not pendentes:
            st.success("Sua fila está em dia. As próximas revisões aparecerão aqui quando vencerem.")
        else:
            st.markdown(f"**{len(pendentes)} itens** aguardando revisão. Leia a anotação, tente recordar e depois marque o resultado.")
            for item in pendentes:
                with st.container(border=True):
                    top1, top2 = st.columns([5, 1])
                    top1.markdown(f"**{item.get('materia', 'Geral')} · {item.get('tipo', 'Anotação')}**")
                    top2.caption(f"Prioridade {item.get('prioridade', 'Média')}")
                    st.write(item.get("texto", ""))
                    etiquetas = item.get("tags", [])
                    if etiquetas:
                        st.caption("  ·  ".join(f"#{tag}" for tag in etiquetas))
                    st.caption(f"Revisões corretas: {item.get('acertos', 0)}  ·  Próxima revisão: {item.get('revisao_em', 'hoje')}")
                    b1, b2, b3 = st.columns([1, 1, 4])
                    if b1.button("Lembrei", key=f"mem_ok_{item['id']}", use_container_width=True):
                        registrar_revisao(user_atual, item["id"], True)
                        st.toast("Boa. Próxima revisão espaçada agendada.", icon="✅")
                        st.rerun()
                    if b2.button("Reestudar", key=f"mem_retry_{item['id']}", use_container_width=True):
                        registrar_revisao(user_atual, item["id"], False)
                        st.toast("Anotação devolvida para uma revisão próxima.", icon="🔁")
                        st.rerun()
    with aba_biblioteca:
        registros = listar_memorias(user_atual, limite=500)
        if not registros:
            st.info("Sua biblioteca está vazia. Adicione uma anotação para começar.")
        else:
            materias = ["Todas"] + sorted({x.get("materia", "Geral") for x in registros})
            f1, f2 = st.columns([2, 1])
            filtro_busca = f1.text_input("Pesquisar por texto ou etiqueta", key="busca_memoria")
            filtro_materia = f2.selectbox("Matéria", materias, key="filtro_materia_memoria")
            encontrados = listar_memorias(user_atual, limite=500, materia=filtro_materia, busca=filtro_busca)
            for item in encontrados:
                with st.container(border=True):
                    c1, c2 = st.columns([5, 1])
                    c1.markdown(f"**{item.get('materia', 'Geral')} · {item.get('tipo', 'Anotação')}**")
                    c2.caption("📌 Fixada" if item.get("fixada") else f"Revisar {item.get('revisao_em', 'hoje')}")
                    st.write(item.get("texto", ""))
                    if item.get("tags"):
                        st.caption("  ·  ".join(f"#{tag}" for tag in item["tags"]))
                    p1, p2 = st.columns([1, 5])
                    if p1.button("Desafixar" if item.get("fixada") else "Fixar", key=f"mem_pin_{item['id']}"):
                        alternar_memoria_fixada(user_atual, item["id"])
                        st.rerun()
                    with p2.expander("Opções"):
                        if st.button("Excluir anotação", key=f"mem_del_{item['id']}"):
                            remover_memoria(user_atual, item["id"])
                            st.toast("Anotação removida.")
                            st.rerun()
    with aba_insights:
        if not stats_memoria["por_materia"]:
            st.info("Os gráficos aparecem quando você adicionar conhecimentos à biblioteca.")
        else:
            df_memoria = pd.DataFrame([{"Matéria": materia, "Registros": quantidade} for materia, quantidade in stats_memoria["por_materia"].items()])
            g1, g2 = st.columns(2)
            with g1:
                st.markdown("#### Distribuição por matéria")
                fig_memoria = px.bar(df_memoria.sort_values("Registros"), x="Registros", y="Matéria", orientation="h", text="Registros")
                fig_memoria.update_traces(marker_color="#59e1d2", textposition="outside", cliponaxis=False)
                st.plotly_chart(fig_memoria, use_container_width=True)
            with g2:
                st.markdown("#### Ritmo de retenção")
                fig_retencao = go.Figure(go.Indicator(
                    mode="gauge+number", value=stats_memoria["taxa_acerto"], number={"suffix": "%"},
                    title={"text": "Acertos nas revisões"},
                    gauge={"axis": {"range": [0, 100]}, "bar": {"color": "#59e1d2"},
                           "bgcolor": "rgba(0,0,0,0)", "borderwidth": 0,
                           "steps": [{"range": [0, 50], "color": "rgba(255,124,156,.15)"},
                                     {"range": [50, 80], "color": "rgba(243,191,98,.15)"},
                                     {"range": [80, 100], "color": "rgba(89,225,210,.15)"}]}
                ))
                fig_retencao.update_layout(height=280, margin=dict(t=40, b=15, l=20, r=20))
                st.plotly_chart(fig_retencao, use_container_width=True)

elif menu == "🗓️ Plano Semanal Automático":
    st.title("🗓️ Plano Semanal Automático")
    st.caption("Distribui blocos conforme seu desempenho, tópicos prioritários do edital e revisões vencidas. Ao recalcular, preserva as tarefas concluídas que ainda coincidirem.")
    dias_semana = ["Segunda", "Terça", "Quarta", "Quinta", "Sexta", "Sábado", "Domingo"]
    plano_salvo = db.get("plano_semanal", {})
    with st.form("form_gerar_plano_semanal"):
        p1, p2 = st.columns([1, 2])
        horas_dia = p1.slider("Horas de estudo por dia", 1, 8, int(plano_salvo.get("horas_dia", 3)))
        dias_disponiveis = p2.multiselect("Dias disponíveis", dias_semana, default=plano_salvo.get("dias", dias_semana[:6]))
        gerar_plano = st.form_submit_button("Recalcular meu plano semanal", type="primary", use_container_width=True, disabled=not dias_disponiveis)

    if gerar_plano:
        edital = [x for x in db.get("edital", []) if isinstance(x, dict) and x.get("Matéria")]
        materias = sorted({str(x.get("Matéria")) for x in edital})
        desempenho = {}
        for sessao in db.get("sessoes", []):
            if not isinstance(sessao, dict):
                continue
            materia = str(sessao.get("Matéria", "")).strip()
            if materia:
                stats = desempenho.setdefault(materia, [0.0, 0.0])
                stats[0] += float(sessao.get("Questões", 0) or 0)
                stats[1] += float(sessao.get("Acertos", 0) or 0)
        revisoes_vencidas = listar_memorias(user_atual, limite=500, apenas_revisar=True)
        revisoes_por_materia = {}
        for nota_revisao in revisoes_vencidas:
            materia_revisao = str(nota_revisao.get("materia", "Geral")).strip() or "Geral"
            revisoes_por_materia[materia_revisao] = revisoes_por_materia.get(materia_revisao, 0) + 1
        erros_por_materia = {}
        erros_usuario = db.get("erros", [])
        if isinstance(erros_usuario, list):
            for erro_usuario in erros_usuario:
                if isinstance(erro_usuario, dict):
                    materia_erro = str(erro_usuario.get("Matéria", erro_usuario.get("materia", ""))).strip()
                    if materia_erro:
                        erros_por_materia[materia_erro] = erros_por_materia.get(materia_erro, 0) + 1

        ranking = []
        for materia in materias:
            temas = [x for x in edital if str(x.get("Matéria")) == materia]
            pendentes = [x for x in temas if x.get("Status") not in {"Revisado / Dominado", "Dominado"}]
            total = len(temas) or 1
            altas = sum(x.get("Prioridade") == "Alta" for x in pendentes)
            questoes, acertos = desempenho.get(materia, (0.0, 0.0))
            aproveitamento = acertos / questoes if questoes else 0.5
            pendencias_revisao = revisoes_por_materia.get(materia, 0)
            pendencias_erros = erros_por_materia.get(materia, 0)
            score = (
                1 + (1 - aproveitamento) * 4
                + (len(pendentes) / total) * 2
                + altas / total
                + min(1.5, pendencias_revisao * 0.35)
                + min(1.2, pendencias_erros * 0.3)
            )
            temas_priorizados = sorted(
                pendentes or temas,
                key=lambda tema: (tema.get("Prioridade") != "Alta", tema.get("Status") == "Revisado / Dominado"),
            )
            ranking.append((score, materia, temas_priorizados))
        ranking.sort(key=lambda x: x[0], reverse=True)
        if not ranking:
            ranking = [(1.0, "Estudo geral", [])]

        today = datetime.date.today()
        tarefas_plano = []
        due_notes = revisoes_vencidas
        slots_por_dia = max(1, min(6, int(horas_dia)))
        topic_cursors = {materia: 0 for _, materia, _ in ranking}
        blocos_por_materia = {materia: 0 for _, materia, _ in ranking}
        revisao_cursor = 0
        concluidas_antes = {
            (str(t.get("data", "")), str(t.get("materia", "")), str(t.get("atividade", "")))
            for t in plano_salvo.get("tarefas", [])
            if isinstance(t, dict) and t.get("concluida")
        }
        for dia_nome in dias_disponiveis:
            weekday_index = dias_semana.index(dia_nome)
            offset = (weekday_index - today.weekday()) % 7
            data_plano = today + datetime.timedelta(days=offset)
            for bloco in range(slots_por_dia):
                if bloco == 0 and revisao_cursor < len(due_notes):
                    nota_revisao = due_notes[revisao_cursor]
                    revisao_cursor += 1
                    atividade = f"Revisão espaçada · {str(nota_revisao.get('texto', 'anotação'))[:90]}"
                    materia = "Revisão"
                else:
                    # Seleção ponderada: matérias prioritárias recebem mais blocos, sem excluir as demais.
                    _, materia, temas = max(
                        ranking,
                        key=lambda item: item[0] / (1 + blocos_por_materia[item[1]] * 0.65),
                    )
                    blocos_por_materia[materia] += 1
                    cursor = topic_cursors[materia]
                    if temas:
                        tema = temas[cursor % len(temas)].get("Tópico", materia)
                        atividade = ("Teoria e resumo" if cursor % 2 == 0 else "Questões e análise de erros") + f" · {tema}"
                        topic_cursors[materia] += 1
                    else:
                        atividade = "Revisão de conteúdo e resolução de questões"
                tarefa_nova = {
                    "data": str(data_plano), "dia": dia_nome, "bloco": bloco + 1,
                    "materia": materia, "atividade": atividade, "minutos": 50, "concluida": False,
                }
                assinatura_tarefa = (tarefa_nova["data"], tarefa_nova["materia"], tarefa_nova["atividade"])
                tarefa_nova["concluida"] = assinatura_tarefa in concluidas_antes
                tarefas_plano.append(tarefa_nova)
        db["plano_semanal"] = {
            "id_plano": uuid.uuid4().hex[:10],
            "gerado_em": datetime.datetime.now().isoformat(timespec="minutes"),
            "horas_dia": int(horas_dia), "dias": dias_disponiveis, "tarefas": tarefas_plano,
        }
        salvar_dados_usuario(user_atual, db)
        st.toast("Plano semanal criado e salvo.", icon="🗓️")
        st.rerun()

    plano_salvo = db.get("plano_semanal", {})
    tarefas_salvas = plano_salvo.get("tarefas", [])
    if not tarefas_salvas:
        st.info("Escolha seus dias e horas disponíveis acima para gerar seu primeiro plano.")
    else:
        concluidas = sum(bool(x.get("concluida")) for x in tarefas_salvas)
        st.progress(concluidas / len(tarefas_salvas), text=f"Progresso semanal: {concluidas} de {len(tarefas_salvas)} blocos concluídos")
        st.caption(f"Plano gerado em {plano_salvo.get('gerado_em', '—')} · blocos de 50 minutos")
        tarefas_alteradas = False
        por_data = {}
        for item in tarefas_salvas:
            por_data.setdefault(item.get("data", ""), []).append(item)
        for data_chave, itens_dia in sorted(por_data.items()):
            with st.container(border=True):
                try:
                    data_titulo = datetime.date.fromisoformat(data_chave).strftime("%A · %d/%m")
                except ValueError:
                    data_titulo = data_chave
                st.markdown(f"#### {data_titulo}")
                for item in itens_dia:
                    chave_geracao = str(plano_salvo.get("id_plano", plano_salvo.get("gerado_em", "plano"))).replace(":", "-").replace(" ", "_")
                    key_check = f"plano_{chave_geracao}_{data_chave}_{item.get('bloco', 0)}"
                    marcado = st.checkbox(
                        f"{item.get('materia', 'Estudo')} · {item.get('atividade', '')}",
                        value=bool(item.get("concluida", False)), key=key_check,
                    )
                    if marcado != bool(item.get("concluida", False)):
                        item["concluida"] = marcado
                        tarefas_alteradas = True
        if tarefas_alteradas:
            db["plano_semanal"] = plano_salvo
            salvar_dados_usuario(user_atual, db)

elif menu == "💾 Backup e Restauração":
    st.title("💾 Backup e Restauração")
    st.caption("Exporte e recupere seu progresso individual, sem incluir senhas nem dados de outras contas.")
    backup_usuario = {
        "aplicativo": "QG EsPCEx", "versao": 1, "usuario": user_atual,
        "exportado_em": datetime.datetime.now().isoformat(timespec="seconds"), "dados": db,
    }
    backup_bytes = json.dumps(backup_usuario, ensure_ascii=False, indent=2).encode("utf-8")
    st.download_button(
        "⬇️ Baixar backup pessoal (.json)", data=backup_bytes,
        file_name=f"backup_qg_{user_atual}_{datetime.date.today()}.json",
        mime="application/json", type="primary", use_container_width=True,
    )
    st.caption("Este arquivo contém seu progresso, edital, diário, plano, memória e preferências de estudo. Não contém credenciais ou o banco administrativo compartilhado.")
    st.divider()
    arquivo_backup = st.file_uploader("Selecione um backup pessoal do QG", type=["json"], key="upload_backup_pessoal")
    confirmar_backup = st.checkbox("Confirmo que quero substituir meus dados pessoais pelo conteúdo do backup.", key="confirmar_backup_pessoal")
    if st.button("Restaurar backup pessoal", disabled=not (arquivo_backup and confirmar_backup), use_container_width=True):
        try:
            conteudo_backup = arquivo_backup.getvalue()
            if len(conteudo_backup) > 5 * 1024 * 1024:
                raise ValueError("O arquivo excede o limite de 5 MB.")
            objeto_backup = json.loads(conteudo_backup.decode("utf-8"))
            if objeto_backup.get("aplicativo") != "QG EsPCEx" or objeto_backup.get("usuario") != user_atual:
                raise ValueError("O backup não pertence a esta conta do QG.")
            dados_backup = objeto_backup.get("dados")
            if not isinstance(dados_backup, dict):
                raise ValueError("Formato de backup inválido.")
            dados_atualizados = carregar_dados_usuario(user_atual, get_edital_espcex)
            dados_atualizados.update(dados_backup)
            salvar_dados_usuario(user_atual, dados_atualizados)
            st.success("Backup pessoal restaurado. A página será atualizada.")
            st.rerun()
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as erro_backup:
            st.error(f"Não foi possível restaurar: {erro_backup}")

elif menu == "🎯 Modo Foco":
    st.markdown("""
    <style>
    .qg-focus-hero{position:relative;overflow:hidden;margin:2px 0 18px;padding:25px 27px;border:1px solid rgba(99,229,220,.22);border-radius:22px;background:radial-gradient(circle at 87% 10%,rgba(91,140,255,.24),transparent 31%),linear-gradient(115deg,#0b1d2c,#122841 62%,#191d37);box-shadow:0 20px 55px rgba(0,0,0,.23)}
    .qg-focus-hero:after{content:"";position:absolute;left:0;right:0;bottom:0;height:2px;background:linear-gradient(90deg,transparent,#59e1d2,#5d91ff,#a88cff,transparent)}
    .qg-focus-kicker{color:#64dfd5;font:700 .69rem 'DM Mono',monospace;letter-spacing:.17em;text-transform:uppercase}
    .qg-focus-title{margin:7px 0 4px;color:#f4f8ff;font-size:clamp(1.45rem,3vw,2.05rem);font-weight:850;letter-spacing:-.05em}
    .qg-focus-copy{max-width:720px;color:#a9bbce;font-size:.92rem;line-height:1.6}
    .qg-focus-tip{padding:12px 15px;border:1px solid rgba(99,229,220,.14);border-radius:13px;color:#b4c8da;background:rgba(10,26,41,.68)}
    @media(max-width:640px){.qg-focus-hero{padding:19px;border-radius:17px}.qg-focus-copy{font-size:.84rem}}
    </style>
    <section class="qg-focus-hero"><div class="qg-focus-kicker">QG · ambiente de concentração</div><div class="qg-focus-title">🎯 Modo Foco</div><div class="qg-focus-copy">Um bloco por vez, com objetivo definido, cronômetro visual e registro automático ao concluir. O som é leve e pode ser ativado no próprio cronômetro.</div></section>
    """, unsafe_allow_html=True)
    foco = db.get("modo_foco", {"status": "parado"})
    if not isinstance(foco, dict):
        foco = {"status": "parado"}
    status_foco = foco.get("status", "parado")
    ativo_foco = status_foco == "ativo"
    pausado_foco = status_foco == "pausado"
    tarefa_foco = str(foco.get("tarefa", ""))
    st.markdown("#### " + (tarefa_foco or "Escolha o objetivo do próximo bloco"))

    if not (ativo_foco or pausado_foco):
        with st.form("form_iniciar_modo_foco"):
            objetivo_foco = st.text_input("Tarefa", placeholder="Ex.: 30 questões de Física")
            duracao_foco = st.slider("Duração", 10, 120, 50, step=5, format="%d min")
            iniciar_foco = st.form_submit_button("Iniciar bloco", type="primary", use_container_width=True)
        if iniciar_foco and objetivo_foco.strip():
            agora_foco = time.time()
            db["modo_foco"] = {
                "status": "ativo", "tarefa": objetivo_foco.strip(), "duracao_seg": int(duracao_foco * 60),
                "inicio_epoch": agora_foco, "fim_epoch": agora_foco + duracao_foco * 60,
                "restante_seg": int(duracao_foco * 60),
                "som_evento": uuid.uuid4().hex, "som_tipo": "inicio",
            }
            salvar_dados_usuario(user_atual, db)
            st.rerun()
    else:
        if ativo_foco:
            restante_foco = max(0, int(float(foco.get("fim_epoch", time.time())) - time.time()))
        else:
            restante_foco = max(0, int(foco.get("restante_seg", 0)))
        tempo_limite_ms = int(float(foco.get("fim_epoch", 0)) * 1000) if ativo_foco else 0
        restante_inicial = restante_foco
        duracao_total_foco = max(1, int(foco.get("duracao_seg", 0) or 1))
        evento_som_foco = re.sub(r"[^a-fA-F0-9]", "", str(foco.get("som_evento", "")))[:64]
        tipo_som_foco = "pausa" if foco.get("som_tipo") == "pausa" else "inicio"
        estado_visual_foco = "FOCO EM ANDAMENTO" if ativo_foco else "BLOCO PAUSADO"
        js_timer = """<main class="focus-shell">
  <div class="focus-top"><span class="focus-kicker">QG · foco profundo</span><span class="focus-live"><i></i> __STATUS__</span></div>
  <div class="focus-task">__TASK__</div>
  <div class="focus-clock-wrap">
    <svg class="focus-ring" viewBox="0 0 220 220" aria-hidden="true">
      <defs><linearGradient id="focusGradient" x1="0%" y1="0%" x2="100%" y2="100%"><stop offset="0%" stop-color="#59e1d2"></stop><stop offset="55%" stop-color="#62a8ff"></stop><stop offset="100%" stop-color="#a88cff"></stop></linearGradient></defs>
      <circle class="focus-ring-track" cx="110" cy="110" r="96"></circle>
      <circle id="focus-ring-progress" class="focus-ring-progress" cx="110" cy="110" r="96"></circle>
    </svg>
    <div id="focus-clock" class="focus-clock">00:00</div>
  </div>
  <div id="focus-clock-caption" class="focus-clock-caption">__STATUS__ · __TOTAL_MIN__ MIN</div>
  <div class="focus-bottom"><span id="focus-audio-hint">Sinal sonoro leve · opcional</span><button id="focus-audio-enable" type="button">🔊 Ativar som</button></div>
</main>
<style>
*{box-sizing:border-box}body{margin:0;background:transparent;color:#eef5ff;font-family:Inter,ui-sans-serif,system-ui,-apple-system,Segoe UI,sans-serif}
.focus-shell{position:relative;overflow:hidden;min-height:300px;padding:19px 22px 16px;border:1px solid rgba(120,190,214,.22);border-radius:22px;background:radial-gradient(circle at 50% 47%,rgba(44,120,147,.16),transparent 37%),linear-gradient(145deg,#0b1927,#101f32 58%,#141b31);box-shadow:0 20px 50px rgba(0,0,0,.26),inset 0 1px rgba(255,255,255,.035)}
.focus-shell:before{content:"";position:absolute;inset:-80px auto auto 60%;width:220px;height:220px;border:1px solid rgba(89,225,210,.08);border-radius:50%;box-shadow:0 0 0 24px rgba(89,225,210,.025),0 0 0 52px rgba(89,225,210,.018);pointer-events:none}
.focus-top,.focus-bottom{position:relative;display:flex;align-items:center;justify-content:space-between;gap:12px}.focus-kicker{color:#73ddd6;font:700 10px ui-monospace,monospace;letter-spacing:.17em;text-transform:uppercase}.focus-live{display:inline-flex;align-items:center;gap:7px;padding:6px 9px;border:1px solid rgba(112,214,192,.17);border-radius:999px;color:#c4e5df;background:rgba(27,91,83,.18);font:700 9px ui-monospace,monospace;letter-spacing:.08em}.focus-live i{width:6px;height:6px;border-radius:50%;background:#59e1d2;box-shadow:0 0 10px #59e1d2;animation:focusPulse 1.8s ease-in-out infinite}
.focus-task{position:relative;max-width:700px;margin:15px auto 0;text-align:center;color:#eaf2fc;font-size:14px;font-weight:650;line-height:1.4}
.focus-clock-wrap{position:relative;width:204px;height:204px;margin:1px auto -5px}.focus-ring{width:100%;height:100%;transform:rotate(-90deg);overflow:visible}.focus-ring-track{fill:none;stroke:rgba(147,176,201,.13);stroke-width:5}.focus-ring-progress{fill:none;stroke:url(#focusGradient);stroke-width:5;stroke-linecap:round;stroke-dasharray:603.19;stroke-dashoffset:0;filter:drop-shadow(0 0 6px rgba(89,225,210,.48));transition:stroke-dashoffset .35s linear,stroke .35s ease}
.focus-clock{position:absolute;inset:0;display:grid;place-items:center;color:#f5faff;font:800 47px ui-monospace,SFMono-Regular,Consolas,monospace;letter-spacing:-.07em;text-shadow:0 0 28px rgba(89,225,210,.17);font-variant-numeric:tabular-nums}.focus-clock-caption{text-align:center;color:#91a9bf;font:700 9px ui-monospace,monospace;letter-spacing:.13em}
.focus-bottom{margin-top:17px;padding-top:13px;border-top:1px solid rgba(147,176,201,.1);color:#8198ae;font-size:10px}.focus-bottom button{padding:7px 10px;border:1px solid rgba(99,229,220,.24);border-radius:9px;color:#d9fbf6;background:rgba(36,105,108,.2);font-size:10px;font-weight:700;cursor:pointer;transition:all .18s ease}.focus-bottom button:hover{border-color:rgba(99,229,220,.65);background:rgba(36,105,108,.38)}
@keyframes focusPulse{50%{opacity:.45;box-shadow:0 0 3px #59e1d2}}@media(max-width:480px){.focus-shell{padding:16px 14px;border-radius:18px}.focus-clock-wrap{width:184px;height:184px}.focus-clock{font-size:41px}.focus-task{font-size:12px}}@media(prefers-reduced-motion:reduce){*,*:before,*:after{animation-duration:.01ms!important;animation-iteration-count:1!important;transition-duration:.01ms!important}}
</style>
<script>
const focusEndMs=__END_MS__, focusInitial=__LEFT__, focusActive=__ACTIVE__, focusTotal=__TOTAL__;
const soundEventId="__SOUND_EVENT__", soundKind="__SOUND_KIND__", soundPreferenceKey="qg_focus_sound_enabled";
let focusAudio=null, pendingFocusTone=null;
const clockEl=document.getElementById("focus-clock"), ringEl=document.getElementById("focus-ring-progress"), captionEl=document.getElementById("focus-clock-caption"), hintEl=document.getElementById("focus-audio-hint"), soundButton=document.getElementById("focus-audio-enable");
const ringLength=2*Math.PI*96;
function storageRead(key){try{return window.localStorage.getItem(key)}catch(_error){return null}}
function storageWrite(key,value){try{window.localStorage.setItem(key,value)}catch(_error){}}
function tone(kind){
  if(!focusAudio||focusAudio.state!=="running"){pendingFocusTone=kind;hintEl.textContent="Toque em Ativar som para liberar o sinal";return}
  const notes=kind==="pausa"?[440]:[660,880];
  notes.forEach((frequency,index)=>{const start=focusAudio.currentTime+index*.12;const oscillator=focusAudio.createOscillator();const gain=focusAudio.createGain();oscillator.type="sine";oscillator.frequency.setValueAtTime(frequency,start);gain.gain.setValueAtTime(.0001,start);gain.gain.exponentialRampToValueAtTime(.045,start+.025);gain.gain.exponentialRampToValueAtTime(.0001,start+.17);oscillator.connect(gain);gain.connect(focusAudio.destination);oscillator.start(start);oscillator.stop(start+.18)});
  if(soundEventId)storageWrite("qg_focus_sound_seen_"+soundEventId,"1");
  pendingFocusTone=null;hintEl.textContent="Som leve ativado";
}
async function unlockFocusAudio(){
  try{const AudioAPI=window.AudioContext||window.webkitAudioContext;if(!AudioAPI){hintEl.textContent="Áudio não suportado neste navegador";return}if(!focusAudio)focusAudio=new AudioAPI();await focusAudio.resume();storageWrite(soundPreferenceKey,"1");soundButton.textContent="✓ Som ativado";if(pendingFocusTone)tone(pendingFocusTone);else hintEl.textContent="Som leve ativado"}catch(_error){hintEl.textContent="O navegador bloqueou o áudio; tente ativar novamente"}
}
soundButton.addEventListener("click",unlockFocusAudio);
function updateFocusClock(){let left=focusActive?Math.max(0,Math.ceil((focusEndMs-Date.now())/1000)):focusInitial;const minutes=Math.floor(left/60),seconds=left%60;clockEl.textContent=String(minutes).padStart(2,"0")+":"+String(seconds).padStart(2,"0");const ratio=focusTotal?Math.max(0,Math.min(1,left/focusTotal)):0;ringEl.style.strokeDasharray=ringLength;ringEl.style.strokeDashoffset=ringLength*(1-ratio);if(left===0){ringEl.style.stroke="#f3bf62";captionEl.textContent="BLOCO CONCLUÍDO · VOLTE AO APP PARA REGISTRAR";clockEl.style.color="#f3bf62"}}
updateFocusClock();window.setInterval(updateFocusClock,250);
if(soundEventId&&storageRead("qg_focus_sound_seen_"+soundEventId)!=="1"){
  pendingFocusTone=soundKind;
  if(storageRead(soundPreferenceKey)==="1")unlockFocusAudio();
}
</script>"""
        js_timer = (
            js_timer.replace("__STATUS__", estado_visual_foco)
            .replace("__TASK__", html.escape(tarefa_foco or "Sessão de estudo"))
            .replace("__TOTAL_MIN__", str(duracao_total_foco // 60))
            .replace("__END_MS__", str(tempo_limite_ms))
            .replace("__LEFT__", str(restante_inicial))
            .replace("__ACTIVE__", str(ativo_foco).lower())
            .replace("__TOTAL__", str(duracao_total_foco))
            .replace("__SOUND_EVENT__", evento_som_foco)
            .replace("__SOUND_KIND__", tipo_som_foco)
        )
        components.html(js_timer, height=335, scrolling=False)
        st.caption(f"Duração planejada: {int(foco.get('duracao_seg', 0)) // 60} min")
        f1, f2, f3 = st.columns(3)
        if ativo_foco and f1.button("Pausar", use_container_width=True):
            foco["restante_seg"] = restante_foco
            foco["status"] = "pausado"
            foco["som_evento"] = uuid.uuid4().hex
            foco["som_tipo"] = "pausa"
            db["modo_foco"] = foco
            salvar_dados_usuario(user_atual, db)
            st.rerun()
        if pausado_foco and f1.button("Retomar", type="primary", use_container_width=True):
            foco["fim_epoch"] = time.time() + restante_foco
            foco["status"] = "ativo"
            foco["som_evento"] = uuid.uuid4().hex
            foco["som_tipo"] = "inicio"
            db["modo_foco"] = foco
            salvar_dados_usuario(user_atual, db)
            st.rerun()
        if f2.button("Encerrar bloco", use_container_width=True):
            foco["status"] = "encerrado"
            db["modo_foco"] = foco
            salvar_dados_usuario(user_atual, db)
            st.rerun()
        if ativo_foco and restante_foco > 0 and f3.button("Atualizar status", use_container_width=True):
            st.rerun()
        if restante_foco == 0 and status_foco == "ativo":
            if f3.button("Registrar sessão concluída", type="primary", use_container_width=True):
                sessoes_focus = db.setdefault("sessoes", [])
                sessoes_focus.append({
                    "Data": str(datetime.date.today()), "Matéria": tarefa_foco,
                    "Horas": round(float(foco.get("duracao_seg", 0)) / 3600, 2),
                    "Questões": 0, "Acertos": 0, "Origem": "Modo Foco",
                })
                db["pomodoro_ciclos_totais"] = int(db.get("pomodoro_ciclos_totais", 0)) + 1
                db["modo_foco"] = {"status": "parado"}
                salvar_dados_usuario(user_atual, db)
                st.success("Bloco registrado no seu histórico de estudos.")
                st.rerun()
        elif status_foco == "encerrado":
            st.info("Bloco encerrado. Ele não foi contado como sessão concluída.")
            if st.button("Preparar novo bloco", use_container_width=True):
                db["modo_foco"] = {"status": "parado"}
                salvar_dados_usuario(user_atual, db)
                st.rerun()

elif menu == "👋 Guia Inicial":
    st.title("👋 Guia Inicial do QG")
    st.caption("Um roteiro rápido para configurar o ambiente e começar a usar seus módulos.")
    guia_concluido = bool(db.get("guia_inicial_concluido", False))
    st.progress(1.0 if guia_concluido else 0.0, text="Guia concluído" if guia_concluido else "Primeiros passos · você pode voltar a este guia quando quiser")
    passos_guia = [
        ("01", "Configure sua rotina", "Defina a data da prova, o período EFA e os indicadores que deseja acompanhar no painel."),
        ("02", "Monte seu plano", "Use o Plano Semanal Automático para organizar blocos de estudo e revisões."),
        ("03", "Registre suas metas físicas", "Use o módulo TAF para acompanhar o treino que você planejou e o seu histórico."),
        ("04", "Ative a memória de estudo", "Crie cartões no Quiz Diário Adaptativo ou guarde anotações para revisões futuras."),
        ("05", "Personalize as missões", "Escolha tarefas de TAF, estudo, revisão e outras atividades na tela Personalizar Painel."),
    ]
    for numero, titulo, descricao in passos_guia:
        with st.container(border=True):
            a, b = st.columns([1, 8])
            a.markdown(f"### `{numero}`")
            b.markdown(f"#### {titulo}")
            b.write(descricao)
    g1, g2, g3 = st.columns(3)
    g1.button("🎛️ Personalizar painel", on_click=_navegar_qg, args=("🎛️ Personalizar Painel",), use_container_width=True)
    g2.button("🧩 Abrir quiz", on_click=_navegar_qg, args=("🧩 Quiz Diário Adaptativo",), use_container_width=True)
    if not guia_concluido:
        if g3.button("Concluir guia", type="primary", use_container_width=True):
            db["guia_inicial_concluido"] = True
            salvar_dados_usuario(user_atual, db)
            st.toast("Guia inicial concluído.", icon="✅")
            st.rerun()
    else:
        g3.success("Guia concluído")

elif menu == "📲 Instalar como App":
    st.title("📲 QG no seu dispositivo")
    st.caption("Hospedagem detectada: Streamlit Community Cloud. Use o navegador para criar um atalho de acesso rápido.")

    status_cloud_1, status_cloud_2, status_cloud_3 = st.columns(3)
    status_cloud_1.metric("Hospedagem", "Community Cloud")
    status_cloud_2.metric("Conexão HTTPS", "Disponível")
    status_cloud_3.metric("Atalho no dispositivo", "Compatível")

    inst_android, inst_ios, inst_desktop = st.columns(3)
    with inst_android:
        with st.container(border=True):
            st.markdown("#### 🤖 Android")
            st.markdown("1. Abra o endereço do QG no Chrome.\n2. Toque em **⋮**.\n3. Selecione **Adicionar à tela inicial** ou **Instalar app**, quando disponível.\n4. Confirme para criar o ícone.")
    with inst_ios:
        with st.container(border=True):
            st.markdown("#### 🍎 iPhone e iPad")
            st.markdown("1. Abra o endereço do QG no Safari.\n2. Toque em **Compartilhar**.\n3. Escolha **Adicionar à Tela de Início**.\n4. Confirme o nome e toque em **Adicionar**.")
    with inst_desktop:
        with st.container(border=True):
            st.markdown("#### 💻 Computador")
            st.markdown("1. Abra o QG no Chrome ou Edge.\n2. Abra o menu do navegador.\n3. Use **Criar atalho** ou **Instalar este site como aplicativo**, se aparecer.\n4. Ative a abertura em janela própria, se disponível.")

    st.info("Esse atalho abre o QG pelo navegador e facilita o acesso. No Community Cloud, o app Streamlit não controla o manifesto nem o service worker na raiz do site; por isso, a instalação como PWA nativa pode não aparecer. Uma PWA completa exige uma camada web externa configurada para servir esses arquivos no escopo correto.")
    with st.expander("🔐 Persistência do login e da memória no Community Cloud"):
        if _qg_cloud_enabled():
            st.success("PostgreSQL conectado. Cadastros, progresso, sessões e o banco administrativo são sincronizados com a base persistente configurada em Secrets.")
        else:
            st.warning("A persistência PostgreSQL ainda não está configurada. Sem QG_DATABASE_URL em Secrets, os arquivos locais podem não sobreviver a reinicializações ou novas instâncias do Community Cloud.")
        st.caption("Não coloque senha de banco, token ou chave privada no código nem em um repositório público.")
    st.button("Voltar ao painel", on_click=_navegar_qg, args=("📊 Painel & Rotina Diária",))

elif menu == "🆕 Novidades do QG":
    st.title("🆕 Novidades do QG")
    st.caption("Recursos disponíveis nesta versão da plataforma.")
    novidades_qg = [
        ("🧩 Quiz Diário Adaptativo", "Uma sessão diária de até 20 perguntas, com cartões priorizados pelo histórico e tópicos do edital para recordação ativa."),
        ("🎛️ Painel personalizável", "Escolha os indicadores do topo e as tarefas que entram na sua missão diária."),
        ("🏅 Missões diárias", "Acesse a aba própria para explorar 100 desafios, montar uma rotina matinal e acompanhar seu ranking pessoal."),
        ("📡 Campanhas do QG", "O proprietário pode destacar uma missão e publicar um comunicado com período definido para aparecer no Quiz."),
        ("👋 Guia Inicial", "Passos rápidos para configurar a rotina e conhecer os módulos principais."),
        ("📲 Acesso como aplicativo", "Instruções para criar um atalho no Community Cloud e explicação dos requisitos para uma PWA nativa."),
        ("🎨 Interface QG Enterprise", "Navegação, cartões, métricas, gráficos e telas menores com identidade visual unificada."),
    ]
    for titulo_novidade, texto_novidade in novidades_qg:
        with st.container(border=True):
            st.markdown(f"#### {titulo_novidade}")
            st.write(texto_novidade)

elif menu == "🧩 Quiz Diário Adaptativo":
    st.title("🧩 Quiz Diário")
    st.caption("Questões objetivas com quatro alternativas, gabarito e explicação imediata. A sessão diária traz até 20 questões.")
    campanha_missoes_quiz = _carregar_campanha_missoes_qg()
    hoje_campanha_qg = datetime.date.today().isoformat()
    inicio_campanha_qg = str(campanha_missoes_quiz.get("inicio_em", "0001-01-01"))
    fim_campanha_qg = str(campanha_missoes_quiz.get("fim_em", "9999-12-31"))
    campanha_visivel_qg = bool(campanha_missoes_quiz.get("ativa")) and inicio_campanha_qg <= hoje_campanha_qg <= fim_campanha_qg
    if campanha_visivel_qg:
        missao_destaque_quiz = QG_MISSOES_POR_ID.get(str(campanha_missoes_quiz.get("missao_id", "")))
        with st.container(border=True):
            st.markdown("#### 📡 Destaque do QG")
            if missao_destaque_quiz:
                st.success(f"🎯 {missao_destaque_quiz['titulo']} · {missao_destaque_quiz['pontos']} pontos")
            if campanha_missoes_quiz.get("aviso"):
                st.info(str(campanha_missoes_quiz["aviso"]))
    quiz_cards = db.get("quiz_cards", [])
    if not isinstance(quiz_cards, list):
        quiz_cards = []
    quiz_cards = [x for x in quiz_cards if isinstance(x, dict) and x.get("id") and x.get("pergunta") and x.get("resposta")]
    materia_quiz = sorted({str(x.get("Matéria", "Geral")) for x in db.get("edital", []) if isinstance(x, dict) and x.get("Matéria")}) or ["Geral"]

    with st.expander("＋ Criar questão de múltipla escolha", expanded=not any(isinstance(x.get("alternativas"), list) for x in quiz_cards)):
        with st.form("form_novo_cartao_quiz", clear_on_submit=True):
            materia_cartao = st.selectbox("Matéria", materia_quiz)
            pergunta_cartao = st.text_area("Enunciado", max_chars=1000, placeholder="Escreva uma questão clara e objetiva.")
            alternativas_cartao = [
                st.text_input(f"Alternativa {letra}", max_chars=500, key=f"nova_questao_alt_{letra}")
                for letra in "ABCD"
            ]
            gabarito_cartao = st.selectbox(
                "Alternativa correta",
                options=list(range(4)),
                format_func=lambda idx: f"{chr(65 + idx)} · {alternativas_cartao[idx] or 'preencha esta alternativa'}",
            )
            explicacao_cartao = st.text_area("Explicação do gabarito", max_chars=3000, placeholder="Explique por que a alternativa correta está certa.")
            if st.form_submit_button("Salvar questão", type="primary", use_container_width=True):
                alternativas_validas = [item.strip() for item in alternativas_cartao]
                if not pergunta_cartao.strip() or not explicacao_cartao.strip() or any(not item for item in alternativas_validas):
                    st.error("Preencha o enunciado, as quatro alternativas e a explicação.")
                elif len({item.casefold() for item in alternativas_validas}) != 4:
                    st.error("As quatro alternativas precisam ser diferentes.")
                else:
                    quiz_cards.append({
                        "id": uuid.uuid4().hex[:12], "materia": materia_cartao,
                        "pergunta": pergunta_cartao.strip(), "resposta": explicacao_cartao.strip(),
                        "explicacao": explicacao_cartao.strip(), "alternativas": alternativas_validas,
                        "correta": int(gabarito_cartao),
                        "criado_em": datetime.date.today().isoformat(), "revisao_em": datetime.date.today().isoformat(),
                        "ultima_revisao": "", "acertos": 0, "erros": 0, "repeticoes": 0,
                    })
                    db["quiz_cards"] = quiz_cards
                    salvar_dados_usuario(user_atual, db)
                    st.toast("Questão objetiva adicionada ao quiz.", icon="🧩")
                    st.rerun()

    with st.expander("Importar cartões do Caderno de Bizus", expanded=False):
        st.caption("Seus bizus continuam salvos como material de revisão. Para aparecer no quiz objetivo, crie uma questão com quatro alternativas e gabarito.")
        if st.button("Importar bizus pendentes", key="importar_bizus_quiz"):
            conn_import = sqlite3.connect("dados_qg.db")
            try:
                conn_import.execute("""CREATE TABLE IF NOT EXISTS caderno_erros (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, data TEXT, materia TEXT, assunto TEXT,
                    motivo TEXT, bisu TEXT, sanado INTEGER DEFAULT 0
                )""")
                bizus_pendentes = conn_import.execute(
                    "SELECT id, materia, assunto, motivo, bisu FROM caderno_erros WHERE sanado = 0 ORDER BY id DESC"
                ).fetchall()
            finally:
                conn_import.close()
            ids_importados = {str(x.get("fonte_bisu_id")) for x in quiz_cards if x.get("fonte_bisu_id") is not None}
            novos_cartoes = 0
            for bisu_id, materia_bisu, assunto_bisu, motivo_bisu, resposta_bisu in bizus_pendentes:
                if str(bisu_id) in ids_importados:
                    continue
                resposta_limpa = str(resposta_bisu or "").strip()
                if not resposta_limpa:
                    continue
                quiz_cards.append({
                    "id": uuid.uuid4().hex[:12], "fonte_bisu_id": bisu_id,
                    "materia": str(materia_bisu or "Geral"),
                    "pergunta": f"Explique de memória: {str(assunto_bisu or 'este assunto')}" + (f" — {motivo_bisu}" if motivo_bisu else ""),
                    "resposta": resposta_limpa, "criado_em": datetime.date.today().isoformat(),
                    "revisao_em": datetime.date.today().isoformat(), "ultima_revisao": "",
                    "acertos": 0, "erros": 0, "repeticoes": 0,
                })
                novos_cartoes += 1
            if novos_cartoes:
                db["quiz_cards"] = quiz_cards
                salvar_dados_usuario(user_atual, db)
                st.success(f"{novos_cartoes} cartão(ões) importado(s).")
                st.rerun()
            else:
                st.info("Não há bizus pendentes novos com resposta preenchida para importar.")

    hoje_quiz = datetime.date.today().isoformat()
    historico_quiz = db.get("quiz_historico", [])
    if not isinstance(historico_quiz, list):
        historico_quiz = []
    cartoes_objetivos_salvos = [
        x for x in quiz_cards
        if isinstance(x.get("alternativas"), list) and len(x.get("alternativas", [])) == 4
        and str(x.get("correta", "")).isdigit() and 0 <= int(x.get("correta", -1)) < 4
    ]
    tentativas_totais = sum(int(x.get("acertos", 0)) + int(x.get("erros", 0)) for x in cartoes_objetivos_salvos)
    acertos_totais = sum(int(x.get("acertos", 0)) for x in cartoes_objetivos_salvos)
    precisao_quiz = round(acertos_totais / tentativas_totais * 100) if tentativas_totais else 0
    revisoes_quiz = sum(str(x.get("revisao_em", "")) <= hoje_quiz for x in cartoes_objetivos_salvos)
    qm1, qm2, qm3 = st.columns(3)
    qm1.metric("Questões próprias", len(cartoes_objetivos_salvos))
    qm2.metric("Revisões vencidas", revisoes_quiz)
    qm3.metric("Precisão histórica", f"{precisao_quiz}%")

    def _montar_questoes_missao_diaria():
        """Monta a sessão objetiva do dia com cartões próprios vencidos e banco curado."""
        semente_quiz = hashlib.sha256(f"{user_atual}|{hoje_quiz}".encode("utf-8")).hexdigest()
        rng_quiz = random.Random(semente_quiz)
        banco_bruto = [
            ("Matemática", "Qual é a forma fatorada de x² − 9?", ["(x − 3)(x + 3)", "(x − 9)(x + 1)", "(x − 3)²", "(x + 9)(x − 1)"], 0, "É uma diferença de quadrados: a² − b² = (a − b)(a + b)."),
            ("Matemática", "A soma dos ângulos internos de um triângulo plano é:", ["90°", "180°", "270°", "360°"], 1, "Em geometria euclidiana, os três ângulos internos de um triângulo somam 180°."),
            ("Matemática", "Em uma PA de primeiro termo 4 e razão 3, qual é o 6º termo?", ["16", "18", "19", "22"], 2, "aₙ = a₁ + (n − 1)r; portanto, a₆ = 4 + 5·3 = 19."),
            ("Matemática", "Ao lançar um dado honesto de seis faces, a probabilidade de sair um número par é:", ["1/6", "1/3", "1/2", "2/3"], 2, "Há três resultados pares entre os seis resultados possíveis: 2, 4 e 6."),
            ("Matemática", "Se log₁₀(x) = 2, então x é igual a:", ["10", "20", "100", "1.000"], 2, "Pela definição de logaritmo, log₁₀(x)=2 equivale a 10²=x."),
            ("Matemática", "As raízes da equação x² − 5x + 6 = 0 são:", ["1 e 6", "2 e 3", "−2 e −3", "−1 e −6"], 1, "A fatoração é (x − 2)(x − 3)=0; logo, x=2 ou x=3."),
            ("Matemática", "Qual é a área de um círculo de raio 3?", ["3π", "6π", "9π", "12π"], 2, "A área é A=πr². Com r=3, A=9π."),
            ("Matemática", "Se f(x)=2x−1, quanto vale f(4)?", ["5", "6", "7", "8"], 2, "Substituindo x por 4: f(4)=2·4−1=7."),
            ("Física", "No Sistema Internacional, a unidade de força é:", ["Joule", "Watt", "Newton", "Pascal"], 2, "A força é medida em newtons (N), equivalente a kg·m/s²."),
            ("Física", "Um móvel parte do repouso e acelera a 2 m/s² por 5 s. Sua velocidade final é:", ["2 m/s", "5 m/s", "10 m/s", "20 m/s"], 2, "Pela relação v=v₀+at, temos v=0+2·5=10 m/s."),
            ("Física", "Uma onda tem frequência de 5 Hz. Seu período é:", ["0,1 s", "0,2 s", "2 s", "5 s"], 1, "O período é o inverso da frequência: T=1/f=1/5=0,2 s."),
            ("Física", "Pela lei de Ohm, a corrente elétrica é calculada por:", ["I=R/V", "I=V/R", "I=V·R", "I=V+R"], 1, "A lei de Ohm estabelece V=R·I; isolando I, temos I=V/R."),
            ("Física", "A imagem formada por um espelho plano é, em relação ao objeto:", ["Real e invertida", "Virtual e do mesmo tamanho", "Real e ampliada", "Virtual e sempre menor"], 1, "O espelho plano forma uma imagem virtual, direita e de mesmo tamanho, à mesma distância atrás do espelho."),
            ("Física", "Qual expressão representa a energia cinética de um corpo de massa m e velocidade v?", ["mv", "mv²", "½mv²", "mgh"], 2, "A energia cinética translacional é Ec=½mv²."),
            ("Física", "Na ausência de resistência do ar, corpos em queda livre perto da superfície terrestre:", ["Caem com acelerações diferentes conforme a massa", "Caem com a mesma aceleração gravitacional", "Mantêm velocidade constante", "Não sofrem força resultante"], 1, "Sem resistência do ar, a aceleração da queda é a aceleração gravitacional local, independentemente da massa."),
            ("Química", "O número atômico de um elemento corresponde ao número de:", ["Nêutrons", "Prótons", "Prótons mais nêutrons", "Elétrons da camada de valência"], 1, "O número atômico Z é a quantidade de prótons no núcleo."),
            ("Química", "Uma solução aquosa com pH 3 é classificada como:", ["Ácida", "Neutra", "Básica", "Saturada"], 0, "A 25 °C, pH menor que 7 indica meio ácido."),
            ("Química", "Em uma ligação covalente, os átomos tendem a:", ["Compartilhar pares de elétrons", "Transferir prótons", "Compartilhar nêutrons", "Perder todos os elétrons"], 0, "A ligação covalente se caracteriza pelo compartilhamento de pares de elétrons."),
            ("Química", "Isótopos de um mesmo elemento possuem:", ["Mesmo número de nêutrons e prótons diferentes", "Mesmo número de prótons e nêutrons diferentes", "Números atômicos diferentes", "Mesmo número de massa obrigatoriamente"], 1, "Isótopos têm o mesmo número atômico (prótons) e diferentes números de nêutrons."),
            ("Química", "A massa molar aproximada da água (H₂O) é:", ["10 g/mol", "16 g/mol", "18 g/mol", "20 g/mol"], 2, "Somando 2·1 g/mol do hidrogênio e 16 g/mol do oxigênio, obtém-se 18 g/mol."),
            ("Química", "Em uma oxidação, uma espécie química:", ["Recebe elétrons", "Perde elétrons", "Recebe nêutrons", "Mantém sempre o mesmo número de oxidação"], 1, "Oxidação corresponde à perda de elétrons e ao aumento do número de oxidação."),
            ("Português", "Na frase “Havia muitos candidatos no local”, o verbo haver tem sentido de existir. Nesse uso, ele:", ["Concorda com “candidatos”", "Permanece na 3ª pessoa do singular", "Vai para o plural por atração", "Concorda com “local”"], 1, "O verbo haver impessoal, com sentido de existir, fica na 3ª pessoa do singular."),
            ("Português", "Assinale a frase em que o uso da crase está adequado:", ["Entreguei o documento à diretora", "Começou à estudar cedo", "Fui à pé para casa", "Enviei a carta à ele"], 0, "Ocorre crase na fusão da preposição “a” com o artigo feminino “a”: entregar algo a + a diretora."),
            ("Português", "Quanto à acentuação, todas as palavras proparoxítonas:", ["São acentuadas", "Nunca recebem acento", "Só recebem acento se terminarem em vogal", "São acentuadas apenas quando têm quatro sílabas"], 0, "A regra geral determina acento gráfico em todas as palavras proparoxítonas."),
            ("Português", "O pronome “onde” deve ser usado preferencialmente para retomar:", ["Uma ideia abstrata", "Um lugar", "Uma pessoa", "Uma causa"], 1, "“Onde” retoma lugar; em outros contextos, podem ser mais adequados “em que” ou “no qual”."),
            ("Português", "A oração adjetiva explicativa é normalmente separada da oração principal por:", ["Parênteses obrigatoriamente", "Vírgulas", "Dois-pontos obrigatoriamente", "Nenhum sinal"], 1, "A oração adjetiva explicativa acrescenta uma informação e costuma ser isolada por vírgulas."),
            ("História", "A Independência do Brasil foi proclamada em:", ["1789", "1808", "1822", "1889"], 2, "A Independência foi proclamada em 7 de setembro de 1822."),
            ("História", "A Lei Áurea, que aboliu juridicamente a escravidão no Brasil, foi assinada em:", ["1822", "1850", "1871", "1888"], 3, "A Lei Áurea foi assinada em 13 de maio de 1888."),
            ("História", "A Revolução Francesa teve início em:", ["1648", "1776", "1789", "1815"], 2, "O marco inicial tradicional da Revolução Francesa é 1789, com a crise política e a tomada da Bastilha."),
            ("Geografia", "A maior parte da Região Norte do Brasil está situada na zona climática:", ["Polar", "Temperada do Norte", "Intertropical", "Temperada do Sul"], 2, "A Região Norte está majoritariamente na zona intertropical e apresenta predomínio de clima equatorial."),
            ("Geografia", "Em uma escala cartográfica 1:100.000, 1 cm no mapa representa no terreno:", ["100 m", "500 m", "1 km", "10 km"], 2, "100.000 cm correspondem a 1.000 m, isto é, 1 km."),
            ("Geografia", "O movimento de rotação da Terra está diretamente associado à:", ["Alternância entre dias e noites", "Sucessão das estações do ano", "Variação anual da distância Terra-Sol", "Formação das marés"], 0, "A rotação é o giro da Terra em torno do próprio eixo e produz a alternância de dias e noites."),
            ("Inglês", "Qual é a forma correta do passado simples do verbo “go”?", ["Goed", "Gone", "Went", "Going"], 2, "“Go” é irregular: seu passado simples é “went”; o particípio passado é “gone”."),
            ("Inglês", "Complete: “I have lived here ___ 2020.”", ["for", "since", "during", "while"], 1, "“Since” indica o ponto de início de um período; “for” indica sua duração."),
            ("Inglês", "Na frase “The letters are written every week”, a voz verbal é:", ["Ativa", "Passiva", "Condicional", "Imperativa"], 1, "A estrutura “are + particípio passado” forma a voz passiva no presente simples."),
            ("Inglês", "Qual palavra completa corretamente: “How ___ water do you need?”", ["many", "much", "few", "several"], 1, "“Water” é incontável nesse uso; perguntas sobre quantidade usam “how much”."),
        ]
        banco_objetivo = [
            {"id": f"banco_{idx:03d}", "tipo": "banco", "materia": item[0], "pergunta": item[1],
             "alternativas": item[2], "correta": item[3], "explicacao": item[4]}
            for idx, item in enumerate(banco_bruto, start=1)
        ]

        cartoes_objetivos = []
        for cartao in quiz_cards:
            alternativas = cartao.get("alternativas")
            try:
                correta = int(cartao.get("correta"))
            except (TypeError, ValueError):
                continue
            pergunta = str(cartao.get("pergunta", "")).strip()
            if (cartao.get("id") and pergunta and isinstance(alternativas, list) and len(alternativas) == 4
                    and all(str(opcao).strip() for opcao in alternativas) and 0 <= correta < 4):
                cartoes_objetivos.append(cartao)

        def _prioridade_diaria(cartao):
            revisao = str(cartao.get("revisao_em", ""))[:10]
            vencido = not revisao or revisao <= hoje_quiz
            acertos = int(cartao.get("acertos", 0) or 0)
            erros = int(cartao.get("erros", 0) or 0)
            precisao = acertos / (acertos + erros) if acertos + erros else 0.0
            return (0 if vencido else 1, precisao, str(cartao.get("ultima_revisao", "")), rng_quiz.random())

        cartoes_objetivos.sort(key=_prioridade_diaria)
        perguntas = []
        for cartao in cartoes_objetivos[:10]:
            perguntas.append({
                "id": f"cartao_{cartao['id']}", "cartao_id": str(cartao["id"]), "tipo": "cartao",
                "materia": str(cartao.get("materia", "Geral")), "pergunta": str(cartao["pergunta"]).strip(),
                "alternativas": [str(opcao).strip() for opcao in cartao["alternativas"]],
                "correta": int(cartao["correta"]),
                "explicacao": str(cartao.get("explicacao") or cartao.get("resposta", "Gabarito registrado no seu cartão.")),
            })

        rng_quiz.shuffle(banco_objetivo)
        perguntas.extend(banco_objetivo[:max(0, 20 - len(perguntas))])
        rng_quiz.shuffle(perguntas)
        for pergunta_objetiva in perguntas:
            ordem_alternativas = list(range(len(pergunta_objetiva["alternativas"])))
            rng_quiz.shuffle(ordem_alternativas)
            gabarito_original = int(pergunta_objetiva["correta"])
            pergunta_objetiva["alternativas"] = [pergunta_objetiva["alternativas"][idx] for idx in ordem_alternativas]
            pergunta_objetiva["correta"] = ordem_alternativas.index(gabarito_original)
        return perguntas[:20]

    st.markdown("### Sua sessão de hoje")
    st.markdown("""
    <style>
    .qg-quiz-mission-banner{position:relative;overflow:hidden;margin:8px 0 18px;padding:26px 28px;border:1px solid rgba(115,205,221,.24);border-radius:22px;background:radial-gradient(circle at 88% 12%,rgba(68,129,233,.24),transparent 34%),linear-gradient(125deg,#0b1b2b 0%,#10263d 55%,#191f3c 100%);box-shadow:0 20px 50px rgba(0,0,0,.2)}
    .qg-quiz-mission-banner:after{content:"";position:absolute;left:0;right:0;bottom:0;height:2px;background:linear-gradient(90deg,transparent,#59e1d2,#5d91ff,#a88cff,transparent)}
    .qg-quiz-mission-label{color:#70e3db;font:700 .7rem 'DM Mono',monospace;letter-spacing:.16em;text-transform:uppercase}
    .qg-quiz-mission-title{margin:8px 0;color:#f3f7ff;font-size:1.5rem;font-weight:850;letter-spacing:-.045em}
    .qg-quiz-mission-copy{max-width:780px;color:#b2c2d3;font-size:.94rem;line-height:1.65}
    .qg-quiz-question-card{padding:20px 22px;border:1px solid rgba(112,175,210,.22);border-radius:18px;background:linear-gradient(145deg,rgba(17,39,59,.98),rgba(9,22,36,.98));box-shadow:0 18px 44px rgba(0,0,0,.17);color:#f4f7fc;font-size:1.12rem;line-height:1.65}
    .qg-quiz-question-meta{color:#75ddd7;font:700 .72rem 'DM Mono',monospace;letter-spacing:.12em;text-transform:uppercase}
    .qg-quiz-result{padding:15px 18px;border-radius:14px;border:1px solid rgba(103,208,190,.26);background:linear-gradient(135deg,rgba(15,61,58,.72),rgba(13,39,52,.78));color:#e8fff9;line-height:1.6}
    .qg-quiz-result-wrong{border-color:rgba(255,130,140,.3);background:linear-gradient(135deg,rgba(74,31,44,.75),rgba(35,31,52,.78));color:#fff1f2}
    @media(max-width:640px){.qg-quiz-mission-banner{padding:19px}.qg-quiz-mission-title{font-size:1.22rem}.qg-quiz-question-card{padding:16px;font-size:1rem}}
    @media(prefers-reduced-motion:reduce){.qg-quiz-mission-banner:after{animation:none!important}}
    </style>
    <section class="qg-quiz-mission-banner"><div class="qg-quiz-mission-label">QG · módulo de conhecimento</div><div class="qg-quiz-mission-title">Treino objetivo, claro e mensurável</div><div class="qg-quiz-mission-copy">Cada questão tem quatro alternativas, gabarito automático e explicação. O bloco diário combina suas questões objetivas vencidas com questões de referência de várias matérias.</div></section>
    """, unsafe_allow_html=True)

    sessao_salva_quiz = db.get("quiz_missao_diaria", {})
    sessao_antiga_quiz = isinstance(sessao_salva_quiz, dict) and sessao_salva_quiz.get("data") == hoje_quiz
    sessao_missao = {}
    if sessao_antiga_quiz:
        questoes_salvas_quiz = sessao_salva_quiz.get("questoes", [])
        sessao_mcq_valida = (
            isinstance(questoes_salvas_quiz, list) and bool(questoes_salvas_quiz)
            and all(
                isinstance(item, dict)
                and isinstance(item.get("alternativas"), list)
                and len(item.get("alternativas", [])) >= 2
                and str(item.get("correta", "")).isdigit()
                and 0 <= int(item.get("correta", -1)) < len(item.get("alternativas", []))
                for item in questoes_salvas_quiz
            )
        )
        if sessao_mcq_valida:
            sessao_missao = sessao_salva_quiz
        else:
            st.info("A sessão salva hoje usava o formato anterior. Ao iniciar o novo quiz, ela será substituída por questões de múltipla escolha; seu histórico e seus cartões permanecem salvos.")

    if not sessao_missao:
        perguntas_preview = _montar_questoes_missao_diaria()
        if perguntas_preview:
            cartoes_no_bloco = sum(item.get("tipo") == "cartao" for item in perguntas_preview)
            st.caption(f"{len(perguntas_preview)} questões prontas · {cartoes_no_bloco} de seus cartões objetivos e {len(perguntas_preview) - cartoes_no_bloco} questões de referência.")
            if st.button("🚀 Iniciar quiz diário", type="primary", use_container_width=True):
                sessao_missao = {
                    "data": hoje_quiz, "questoes": perguntas_preview,
                    "indice": 0, "respostas": {}, "concluida": False,
                    "iniciada_em": datetime.datetime.now().isoformat(timespec="seconds"),
                }
                db["quiz_missao_diaria"] = sessao_missao
                salvar_dados_usuario(user_atual, db)
                st.rerun()
        else:
            st.warning("Não foi possível montar o bloco de questões. Recarregue a página ou tente novamente.")
    else:
        questoes_missao = sessao_missao.get("questoes", [])
        respostas_missao = sessao_missao.get("respostas", {})
        if not isinstance(respostas_missao, dict):
            respostas_missao = {}
        total_perguntas_missao = len(questoes_missao)
        indice_missao = max(0, min(int(sessao_missao.get("indice", 0)), total_perguntas_missao))
        respostas_validas_missao = [
            item for item in respostas_missao.values()
            if isinstance(item, dict) and isinstance(item.get("correta"), bool)
        ]
        avaliadas_missao = len(respostas_validas_missao)
        acertos_missao = sum(item.get("correta") is True for item in respostas_validas_missao)
        st.progress(avaliadas_missao / total_perguntas_missao if total_perguntas_missao else 0, text=f"Progresso · {avaliadas_missao} de {total_perguntas_missao} respondidas")

        if sessao_missao.get("concluida") or indice_missao >= total_perguntas_missao:
            sessao_missao["concluida"] = True
            db["quiz_missao_diaria"] = sessao_missao
            salvar_dados_usuario(user_atual, db)
            st.success("Quiz de hoje concluído. Seu resultado foi salvo no histórico.")
            resultado_q1, resultado_q2, resultado_q3 = st.columns(3)
            resultado_q1.metric("Respondidas", avaliadas_missao)
            resultado_q2.metric("Acertos", acertos_missao)
            resultado_q3.metric("Precisão", f"{round(acertos_missao / avaliadas_missao * 100) if avaliadas_missao else 0}%")
            st.progress(1.0, text="Sessão diária finalizada")
        elif questoes_missao:
            pergunta_missao = questoes_missao[indice_missao]
            id_pergunta_missao = str(pergunta_missao.get("id", f"q{indice_missao}"))
            resposta_atual_missao = respostas_missao.get(id_pergunta_missao, {})
            if not isinstance(resposta_atual_missao, dict):
                resposta_atual_missao = {}
            alternativas_missao = pergunta_missao.get("alternativas", [])
            correta_missao = int(pergunta_missao.get("correta", 0))

            st.markdown(f"<div class='qg-quiz-question-meta'>{html.escape(str(pergunta_missao.get('materia', 'Geral')))} &nbsp;·&nbsp; QUESTÃO {indice_missao + 1:02d} / {total_perguntas_missao:02d}</div>", unsafe_allow_html=True)
            st.markdown(f"<div class='qg-quiz-question-card'>{html.escape(str(pergunta_missao.get('pergunta', '')))}</div>", unsafe_allow_html=True)
            if not resposta_atual_missao.get("revelada"):
                with st.form(f"form_mcq_missao_{user_atual}_{hoje_quiz}_{indice_missao}"):
                    opcao_escolhida_missao = st.radio(
                        "Selecione uma alternativa",
                        options=[-1] + list(range(len(alternativas_missao))),
                        index=0,
                        format_func=lambda valor: "Escolha uma opção" if valor == -1 else f"{chr(65 + valor)}. {alternativas_missao[valor]}",
                        key=f"mcq_resposta_{user_atual}_{hoje_quiz}_{indice_missao}",
                    )
                    enviar_resposta_missao = st.form_submit_button("Confirmar resposta", type="primary", use_container_width=True)
                if enviar_resposta_missao:
                    if opcao_escolhida_missao < 0:
                        st.error("Selecione uma alternativa antes de confirmar.")
                    else:
                        resposta_correta_missao = opcao_escolhida_missao == correta_missao
                        respostas_missao[id_pergunta_missao] = {
                            "opcao": int(opcao_escolhida_missao), "correta": bool(resposta_correta_missao),
                            "revelada": True, "respondida_em": datetime.datetime.now().isoformat(timespec="seconds"),
                        }
                        if pergunta_missao.get("tipo") == "cartao":
                            cartao_alvo = next((item for item in quiz_cards if str(item.get("id")) == str(pergunta_missao.get("cartao_id"))), None)
                            if cartao_alvo:
                                acertos_cartao = int(cartao_alvo.get("acertos", 0) or 0)
                                erros_cartao = int(cartao_alvo.get("erros", 0) or 0)
                                repeticoes_cartao = int(cartao_alvo.get("repeticoes", 0) or 0)
                                if resposta_correta_missao:
                                    cartao_alvo["acertos"] = acertos_cartao + 1
                                    intervalos_missao = (1, 3, 7, 14, 30)
                                    dias_revisao = intervalos_missao[min(repeticoes_cartao, len(intervalos_missao) - 1)]
                                    cartao_alvo["repeticoes"] = repeticoes_cartao + 1
                                else:
                                    cartao_alvo["erros"] = erros_cartao + 1
                                    cartao_alvo["repeticoes"] = 0
                                    dias_revisao = 1
                                cartao_alvo["ultima_revisao"] = hoje_quiz
                                cartao_alvo["revisao_em"] = str(datetime.date.today() + datetime.timedelta(days=dias_revisao))
                                db["quiz_cards"] = quiz_cards
                                historico_quiz.append({
                                    "data": hoje_quiz, "cartao_id": cartao_alvo["id"],
                                    "materia": cartao_alvo.get("materia", "Geral"),
                                    "lembrou": bool(resposta_correta_missao), "correta": bool(resposta_correta_missao),
                                    "origem": "quiz_objetivo_diario",
                                })
                        historico_sessao_missoes = db.get("quiz_missao_historico", [])
                        if not isinstance(historico_sessao_missoes, list):
                            historico_sessao_missoes = []
                        historico_sessao_missoes.append({
                            "data": hoje_quiz, "pergunta_id": id_pergunta_missao,
                            "materia": str(pergunta_missao.get("materia", "Geral")),
                            "lembrou": bool(resposta_correta_missao), "correta": bool(resposta_correta_missao),
                            "tipo": str(pergunta_missao.get("tipo", "banco")),
                        })
                        db["quiz_historico"] = historico_quiz[-1000:]
                        db["quiz_missao_historico"] = historico_sessao_missoes[-5000:]
                        sessao_missao["respostas"] = respostas_missao
                        db["quiz_missao_diaria"] = sessao_missao
                        salvar_dados_usuario(user_atual, db)
                        st.rerun()
            else:
                opcao_marcada_missao = int(resposta_atual_missao.get("opcao", -1))
                resposta_correta_registrada = resposta_atual_missao.get("correta") is True
                if resposta_correta_registrada:
                    st.markdown("<div class='qg-quiz-result'><strong>✓ Resposta correta</strong><br>Boa leitura. Confira a explicação e siga para a próxima questão.</div>", unsafe_allow_html=True)
                else:
                    st.markdown("<div class='qg-quiz-result qg-quiz-result-wrong'><strong>↻ Resposta incorreta</strong><br>Revise a alternativa correta e a explicação antes de continuar.</div>", unsafe_allow_html=True)
                st.markdown(f"**Sua resposta:** {chr(65 + opcao_marcada_missao)}. {html.escape(str(alternativas_missao[opcao_marcada_missao]))}")
                st.markdown(f"**Gabarito:** {chr(65 + correta_missao)}. {html.escape(str(alternativas_missao[correta_missao]))}")
                st.info(str(pergunta_missao.get("explicacao", "Revise o conteúdo relacionado a esta questão.")))
                rotulo_proxima = "Finalizar quiz" if indice_missao + 1 >= total_perguntas_missao else "Próxima questão →"
                if st.button(rotulo_proxima, type="primary", use_container_width=True, key=f"proxima_mcq_{user_atual}_{hoje_quiz}_{indice_missao}"):
                    sessao_missao["indice"] = indice_missao + 1
                    sessao_missao["concluida"] = indice_missao + 1 >= total_perguntas_missao
                    db["quiz_missao_diaria"] = sessao_missao
                    salvar_dados_usuario(user_atual, db)
                    st.rerun()

    with st.expander("Minhas questões objetivas", expanded=False):
        if cartoes_objetivos_salvos:
            for cartao in cartoes_objetivos_salvos:
                acertos = int(cartao.get("acertos", 0) or 0)
                erros = int(cartao.get("erros", 0) or 0)
                st.markdown(f"**{html.escape(str(cartao.get('materia', 'Geral')))}** · {acertos} acertos · {erros} erros")
                st.caption(str(cartao.get("pergunta", ""))[:180])
        else:
            st.caption("Suas questões criadas aparecerão aqui.")

    cartoes_legados_quiz = [cartao for cartao in quiz_cards if cartao not in cartoes_objetivos_salvos]
    with st.expander(f"Cartões e bizus anteriores · {len(cartoes_legados_quiz)}", expanded=False):
        if cartoes_legados_quiz:
            st.caption("Os registros antigos continuam guardados. Como não têm quatro alternativas e gabarito, ficam disponíveis para consulta e não entram na sessão objetiva.")
            for cartao in cartoes_legados_quiz:
                with st.container(border=True):
                    st.markdown(f"**{html.escape(str(cartao.get('materia', 'Geral')))}** · {html.escape(str(cartao.get('pergunta', 'Cartão salvo')))}")
                    resposta_legada = str(cartao.get("resposta", "")).strip()
                    if resposta_legada:
                        st.caption(resposta_legada)
        else:
            st.caption("Não há cartões antigos neste perfil.")


elif menu == "🎯 Missões Diárias":
    # Catálogo separado de missões diárias.
    st.title(f"🎯 Missões Diárias · {len(QG_MISSOES)} desafios")
    st.caption("Monte um plano curto para hoje, conclua as tarefas e acompanhe seu ranking pessoal por pontos e constância.")

    hoje_missoes = datetime.date.today().isoformat()
    planos_catalogo = db.get("missoes_catalogo_por_dia", {})
    if not isinstance(planos_catalogo, dict):
        planos_catalogo = {}
    plano_hoje = planos_catalogo.get(hoje_missoes, [])
    if not isinstance(plano_hoje, list):
        plano_hoje = []
    plano_hoje = list(dict.fromkeys(str(item) for item in plano_hoje if str(item) in QG_MISSOES_POR_ID))[:8]

    historico_missoes = db.get("missoes_diarias", {})
    if not isinstance(historico_missoes, dict):
        historico_missoes = {}
    conclusoes_por_missao = {missao["id"]: 0 for missao in QG_MISSOES}
    for estado_data in historico_missoes.values():
        if not isinstance(estado_data, dict):
            continue
        for missao_id in conclusoes_por_missao:
            if estado_data.get(f"catalogo_{missao_id}"):
                conclusoes_por_missao[missao_id] += 1
    pontos_totais_missoes = sum(
        conclusoes_por_missao[missao["id"]] * missao["pontos"] for missao in QG_MISSOES
    )
    estado_hoje_missoes = historico_missoes.get(hoje_missoes, {})
    if not isinstance(estado_hoje_missoes, dict):
        estado_hoje_missoes = {}
    concluidas_hoje_missoes = sum(bool(estado_hoje_missoes.get(f"catalogo_{missao_id}")) for missao_id in plano_hoje)

    mm1, mm2, mm3, mm4 = st.columns(4)
    mm1.metric("Missões no catálogo", len(QG_MISSOES))
    mm2.metric("No plano de hoje", len(plano_hoje))
    mm3.metric("Concluídas hoje", concluidas_hoje_missoes)
    mm4.metric("Pontos acumulados", pontos_totais_missoes)

    aba_plano_missoes, aba_ranking_missoes = st.tabs(["☀️ Plano diário", "🏆 Ranking das missões"])
    with aba_plano_missoes:
        categorias_missoes = list(dict.fromkeys(missao["categoria"] for missao in QG_MISSOES))
        categoria_missao = st.selectbox(
            "Escolha uma categoria para explorar",
            categorias_missoes,
            key="qg_categoria_catalogo_missoes",
        )
        missoes_categoria = [missao for missao in QG_MISSOES if missao["categoria"] == categoria_missao]
        mapa_rotulos_missoes = {
            missao["id"]: f"{missao['titulo']} · {missao['minutos']} min · {missao['pontos']} pts"
            for missao in missoes_categoria
        }
        adicionar_missoes = st.multiselect(
            "Selecione desafios para adicionar ao plano de hoje",
            options=list(mapa_rotulos_missoes),
            format_func=lambda missao_id: mapa_rotulos_missoes[missao_id],
            key=f"qg_seletor_missoes_{user_atual}_{hoje_missoes}_{categoria_missao}",
            placeholder="Pesquise uma missão desta categoria",
        )
        col_add_missoes, col_manha_missoes = st.columns(2)
        if col_add_missoes.button("＋ Adicionar ao plano de hoje", type="primary", use_container_width=True):
            novo_plano = list(dict.fromkeys(plano_hoje + adicionar_missoes))
            if len(novo_plano) > 8:
                st.error("O plano diário aceita até 8 missões. Remova uma tarefa antes de adicionar outras.")
            else:
                planos_catalogo[hoje_missoes] = novo_plano
                db["missoes_catalogo_por_dia"] = planos_catalogo
                salvar_dados_usuario(user_atual, db)
                st.toast("Plano diário atualizado.", icon="🎯")
                st.rerun()

        if col_manha_missoes.button("☀️ Adicionar missões da manhã", use_container_width=True):
            ids_manha = [missao["id"] for missao in QG_MISSOES if missao["periodo"] == "Manhã"][:5]
            novo_plano = list(dict.fromkeys(plano_hoje + ids_manha))
            if len(novo_plano) > 8:
                st.error("Há pouco espaço no plano. Ele aceita até 8 missões; remova algumas e tente novamente.")
            else:
                planos_catalogo[hoje_missoes] = novo_plano
                db["missoes_catalogo_por_dia"] = planos_catalogo
                salvar_dados_usuario(user_atual, db)
                st.toast("Cinco missões matinais foram adicionadas.", icon="☀️")
                st.rerun()

        if plano_hoje:
            st.markdown("#### Plano de hoje")
            estado_dia_catalogo = historico_missoes.get(hoje_missoes, {})
            if not isinstance(estado_dia_catalogo, dict):
                estado_dia_catalogo = {}
            for missao_id in plano_hoje:
                missao = QG_MISSOES_POR_ID[missao_id]
                tarefa_id = f"catalogo_{missao_id}"
                widget_key = f"missao_{user_atual}_{hoje_missoes}_{tarefa_id}"
                if widget_key not in st.session_state:
                    st.session_state[widget_key] = bool(estado_dia_catalogo.get(tarefa_id, False))
                check_col, meta_col, remove_col = st.columns([7, 2, 1])
                check_col.checkbox(
                    f"{missao['periodo']} · {missao['titulo']}",
                    key=widget_key,
                    on_change=_salvar_check_missao,
                    args=(user_atual, hoje_missoes, tarefa_id, widget_key),
                )
                meta_col.caption(f"{missao['minutos']} min · {missao['pontos']} pts")
                if remove_col.button("✕", key=f"remover_{user_atual}_{hoje_missoes}_{missao_id}", help="Remover do plano de hoje"):
                    planos_catalogo[hoje_missoes] = [item for item in plano_hoje if item != missao_id]
                    db["missoes_catalogo_por_dia"] = planos_catalogo
                    salvar_dados_usuario(user_atual, db)
                    st.rerun()
            concluidas_plano = sum(
                bool(st.session_state.get(f"missao_{user_atual}_{hoje_missoes}_catalogo_{missao_id}", False))
                for missao_id in plano_hoje
            )
            st.progress(concluidas_plano / len(plano_hoje), text=f"{concluidas_plano} de {len(plano_hoje)} missões concluídas")
        else:
            st.info("Escolha missões do catálogo ou use o botão de manhã para montar seu primeiro plano.")

    with aba_ranking_missoes:
        st.caption("Ranking pessoal das missões por conclusões registradas; os pontos valorizam tarefas de diferentes durações. Não compara contas de outros usuários.")
        linhas_ranking_missoes = []
        for missao in QG_MISSOES:
            linhas_ranking_missoes.append({
                "Código": missao["id"], "Missão": missao["titulo"], "Categoria": missao["categoria"],
                "Período": missao["periodo"], "Duração": f"{missao['minutos']} min",
                "Pontos": missao["pontos"], "Vezes concluída": conclusoes_por_missao[missao["id"]],
            })
        linhas_ranking_missoes.sort(key=lambda linha: (-linha["Vezes concluída"], -linha["Pontos"], linha["Missão"]))
        for indice, linha in enumerate(linhas_ranking_missoes, start=1):
            linha["Posição"] = indice
        ranking_df = pd.DataFrame(linhas_ranking_missoes)
        if not ranking_df.empty and ranking_df["Vezes concluída"].sum() > 0:
            top_missoes_df = ranking_df.head(10).sort_values("Vezes concluída", ascending=True)
            fig_missoes = px.bar(
                top_missoes_df, x="Vezes concluída", y="Missão", orientation="h",
                color="Pontos", title="Suas 10 missões mais concluídas", template="qg_enterprise",
            )
            fig_missoes.update_layout(height=430, margin=dict(t=55, b=30, l=10, r=20), showlegend=False)
            st.plotly_chart(fig_missoes, use_container_width=True)
        st.dataframe(ranking_df, use_container_width=True, hide_index=True, height=520)
        st.download_button(
            "📥 Baixar meu ranking em CSV",
            data=ranking_df.to_csv(index=False).encode("utf-8-sig"),
            file_name=f"ranking_missoes_qg_{hoje_missoes}.csv",
            mime="text/csv",
        )



else:
    st.info(
        f"A tela **{menu}** ainda não tem uma implementação correspondente no código fornecido. "
        "As demais telas disponíveis continuam acessíveis pelo menu lateral."
    )

# Acabamento final: garante que o tema 3D também prevaleça sobre estilos de módulos.
st.markdown("""<style>
.stApp {
  background:radial-gradient(ellipse at 12% 0%,rgba(27,112,139,.22),transparent 36%),radial-gradient(ellipse at 88% 13%,rgba(73,80,163,.23),transparent 35%),linear-gradient(145deg,#050b14 0%,#081523 46%,#0c1022 100%) !important;
}
/* Entrada suave compartilhada: mantém a mesma abertura visual do Módulo 14 em cada tela. */
@keyframes qgM14PageEntrance { from { opacity:0; transform:translateY(10px) scale(.995); } to { opacity:1; transform:translateY(0) scale(1); } }
/* O container padrão conserva o layout largo do Módulo 14 e anima apenas ao carregar a tela. */
.block-container { animation:qgM14PageEntrance .55s ease-out both; }
[data-testid="stVerticalBlockBorderWrapper"] > div,
[data-testid="stExpander"], [data-testid="stForm"], [data-testid="stTabs"] {
  border-color:rgba(123,196,222,.23) !important;
  border-radius:19px !important;
  background:linear-gradient(145deg,rgba(17,39,59,.79),rgba(7,18,32,.91)) !important;
  box-shadow:0 17px 40px rgba(0,0,0,.33),inset 0 1px rgba(255,255,255,.07),inset 0 -1px rgba(0,0,0,.22) !important;
  backdrop-filter:blur(15px); -webkit-backdrop-filter:blur(15px);
  transform-style:preserve-3d;
}
[data-testid="stMetric"] {
  border:1px solid rgba(99,229,220,.32) !important; border-radius:18px !important;
  background:radial-gradient(ellipse at 18% 0%,rgba(99,229,220,.17),transparent 55%),linear-gradient(145deg,rgba(21,52,76,.98),rgba(7,18,32,.99)) !important;
  box-shadow:0 17px 34px rgba(0,0,0,.39),inset 0 2px rgba(255,255,255,.10),inset 0 -2px rgba(0,0,0,.27) !important;
  padding:15px !important; transform-style:preserve-3d;
  animation:qgM14MetricFloat 4s ease-in-out infinite;
  transition:transform .3s ease,border-color .3s ease,box-shadow .3s ease !important;
}
/* Elevação leve e contínua das métricas, inspirada nos cartões flutuantes do Módulo 14. */
@keyframes qgM14MetricFloat { 0%,100% { translate:0 0; } 50% { translate:0 -5px; } }
[data-testid="stMetric"]:hover {
  transform:perspective(800px) scale(1.035) rotateX(2deg) !important;
  border-color:#92fe9d !important;
  box-shadow:0 12px 30px rgba(0,201,255,.22),0 0 17px rgba(146,254,157,.13),inset 0 2px rgba(255,255,255,.12) !important;
}
[data-testid="stMetricValue"] { color:#7cf5e6 !important; text-shadow:0 0 20px rgba(99,229,220,.31) !important; }
[data-testid="stMetricLabel"] { color:#a9bdd0 !important; }
.stButton > button, .stFormSubmitButton > button, [data-testid="stDownloadButton"] > button {
  border:1px solid rgba(99,229,220,.48) !important; border-radius:12px !important;
  color:#f1ffff !important; background:linear-gradient(45deg,rgba(0,0,0,.38),rgba(0,201,255,.17),rgba(146,254,157,.12)) !important;
  box-shadow:0 5px 0 #074b5a,0 14px 27px rgba(24,143,177,.25),inset 0 1px rgba(255,255,255,.24) !important;
  transition:transform .2s ease,filter .2s ease,box-shadow .2s ease !important;
}
.stButton > button:hover, .stFormSubmitButton > button:hover, [data-testid="stDownloadButton"] > button:hover {
  transform:perspective(700px) translateY(-4px) rotateX(4deg) scale(1.02) !important; filter:brightness(1.16) !important;
  border-color:#92fe9d !important;
  box-shadow:0 8px 0 #074b5a,0 22px 38px rgba(24,143,177,.35),0 0 24px rgba(99,229,220,.20) !important;
}
[data-baseweb="input"] > div, [data-baseweb="select"] > div, [data-baseweb="textarea"] > div, textarea, input {
  border-color:rgba(99,229,220,.29) !important; border-radius:12px !important;
  background:rgba(5,16,27,.88) !important; box-shadow:inset 0 3px 9px rgba(0,0,0,.38),0 3px 9px rgba(0,0,0,.12) !important;
}
[data-baseweb="input"] > div:focus-within, [data-baseweb="select"] > div:focus-within, textarea:focus, input:focus {
  border-color:#63e5dc !important; box-shadow:0 0 0 2px rgba(99,229,220,.14),0 0 22px rgba(99,229,220,.23) !important;
}
[data-testid="stPlotlyChart"], [data-testid="stGraph"], [data-testid="stDataFrame"], [data-testid="stTable"] {
  border-radius:17px !important; filter:drop-shadow(0 15px 25px rgba(0,0,0,.20));
  transition:transform .3s ease,filter .3s ease !important;
}
[data-testid="stPlotlyChart"]:hover, [data-testid="stGraph"]:hover, [data-testid="stDataFrame"]:hover {
  transform:perspective(1100px) translateY(-4px) rotateX(1.5deg) !important;
  filter:drop-shadow(0 22px 30px rgba(39,177,199,.20)) !important;
}
[data-testid="stSidebar"] {
  background:radial-gradient(ellipse at 0 0,rgba(26,129,143,.19),transparent 42%),linear-gradient(180deg,#071320,#040811) !important;
  border-right:1px solid rgba(99,229,220,.23) !important; box-shadow:22px 0 70px rgba(0,0,0,.48) !important;
}
/* O seletor móvel fica oculto em telas grandes e aparece como acesso rápido em celulares/tablets. */
.st-key-qg_mobile_navigation { display:none; }
@media(max-width:768px) {
  .st-key-qg_mobile_navigation { display:block !important; margin:.35rem 0 1rem; padding:10px 13px 2px; border:1px solid rgba(99,229,220,.25); border-radius:15px; background:linear-gradient(145deg,rgba(17,39,59,.88),rgba(7,18,32,.96)); box-shadow:0 12px 28px rgba(0,0,0,.28),inset 0 1px rgba(255,255,255,.08); }
  .st-key-qg_mobile_navigation [data-testid="stWidgetLabel"] { color:#91eee2 !important; font-size:.78rem; font-weight:750; letter-spacing:.04em; }
  .st-key-qg_mobile_navigation [data-baseweb="select"] { min-height:46px; }
}
[data-testid="stAlert"], [data-testid="stToast"] { border-radius:15px !important; box-shadow:0 15px 34px rgba(0,0,0,.27) !important; backdrop-filter:blur(14px); }
[data-testid="stProgress"] > div > div { background:linear-gradient(90deg,#63e5dc,#78a5ff,#b09aff) !important; box-shadow:0 0 20px rgba(99,229,220,.31) !important; }
.routine-card { border-color:rgba(99,229,220,.35) !important; background:radial-gradient(ellipse at 90% 0%,rgba(120,165,255,.16),transparent 47%),linear-gradient(145deg,#14354d,#06111f) !important; box-shadow:12px 17px 32px rgba(0,0,0,.43),inset 2px 2px rgba(255,255,255,.09) !important; }
.qg-hero { border-color:rgba(99,229,220,.47) !important; box-shadow:0 28px 66px rgba(0,0,0,.48),inset 0 2px rgba(255,255,255,.14),0 0 38px rgba(66,202,220,.10) !important; }
hr { border-color:rgba(99,229,220,.18) !important; }
@media (prefers-reduced-motion:reduce) { *,*::before,*::after { transition-duration:.01ms !important; animation-duration:.01ms !important; animation-iteration-count:1 !important; } }
</style>""", unsafe_allow_html=True)

# Nitidez tipográfica: usa fontes presentes no dispositivo, remove brilhos sobre os glifos
# e deixa o relevo nos cartões e fundos, onde ele não interfere na leitura.
st.markdown("""<style>
.stApp {
  font-family:system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif !important;
  -webkit-font-smoothing:antialiased;
  -moz-osx-font-smoothing:grayscale;
  text-rendering:optimizeLegibility;
}
.stApp [data-testid="stMarkdownContainer"] *,
.stApp [data-testid="stCaptionContainer"],
.stApp [data-testid="stWidgetLabel"] *,
.stApp [data-testid="stMetric"] *,
.stApp [data-testid="stDataFrame"] *,
.stApp [data-testid="stTable"] *,
.stApp button, .stApp input, .stApp textarea, .stApp select {
  text-shadow:none !important;
  -webkit-font-smoothing:antialiased;
  text-rendering:optimizeLegibility;
}
/* Títulos ficam planos e definidos; o cubo, a luz e as sombras das superfícies continuam em 3D. */
.stMarkdown h1, .stMarkdown h2, .stMarkdown h3, .stMarkdown h4,
.qg-hero-title, .qg-hero-auth .qg-hero-title,
.qg-hero-auth .qg-hero-kicker, .qg-hero-auth .qg-hero-subtitle,
.routine-card h4, [data-testid="stMetricValue"] {
  text-shadow:none !important;
}
/* O texto dos cartões permanece no plano de leitura, mesmo quando a superfície ganha perspectiva. */
.qg-hero-auth .qg-hero-copy,
.qg-start-card h3, .qg-start-card p {
  transform:none !important;
}
/* Faz os títulos de todos os módulos seguirem o título premium do Módulo 14. */
@keyframes qgM14TitleGradient { 0%,100% { background-position:0% 50%; } 50% { background-position:100% 50%; } }
[data-testid="stMain"] [data-testid="stHeading"] h1,
[data-testid="stMain"] [data-testid="stHeading"] h2,
[data-testid="stMain"] .premium-title {
  color:#8df2dc !important;
  background:linear-gradient(100deg,#00c9ff 0%,#92fe9d 48%,#00c9ff 100%);
  background-size:200% 200%;
  background-clip:text;
  -webkit-background-clip:text;
  -webkit-text-fill-color:transparent;
  font-weight:800 !important;
  letter-spacing:.035em;
  text-transform:uppercase;
  text-shadow:none !important;
  animation:qgM14TitleGradient 7s ease-in-out infinite;
}
/* Alinha o espaço dos títulos para que cada página comece com a mesma hierarquia visual. */
[data-testid="stMain"] [data-testid="stHeading"] { margin-top:.35rem; margin-bottom:.75rem; }
@media(prefers-reduced-motion:reduce) { [data-testid="stMain"] [data-testid="stHeading"] h1, [data-testid="stMain"] [data-testid="stHeading"] h2, [data-testid="stMain"] .premium-title { animation:none !important; } }
</style>""", unsafe_allow_html=True)
