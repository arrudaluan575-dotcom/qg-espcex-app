import streamlit as st
import streamlit.components.v1 as components
import pandas as pd
import plotly.express as px
import datetime
import json
import os
import time
import hashlib
import random
from supabase import create_client, Client

# -----------------------------------------------------------------------------
# 1. Configuração da Página & Tema Visual Verde Oliva / EsPCEx
# -----------------------------------------------------------------------------
st.set_page_config(
    page_title="QG EsPCEx - Preparação de Cadetes",
    page_icon="⚔️",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Proteção contra erros do DOM decorrentes de tradução automática do Google Translate
st.markdown('<meta name="google" content="notranslate">', unsafe_allow_html=True)

# Customização CSS Tática Verde Oliva / Exército
st.markdown("""
    <style>
    .main { background-color: #0f140d; color: #e0e6ed; }
    .stMetric {
        background-color: #1a2318;
        padding: 14px;
        border-radius: 8px;
        border-left: 5px solid #4caf50;
        box-shadow: 0 2px 4px rgba(0,0,0,0.3);
    }
    .espcex-badge {
        background-color: #2e4d25;
        color: #d4af37;
        padding: 6px 12px;
        border-radius: 4px;
        font-weight: bold;
        font-family: monospace;
        border: 1px solid #d4af37;
    }
    .routine-card {
        background-color: #162014;
        border: 1px solid #2e4d25;
        border-radius: 8px;
        padding: 14px;
        margin-bottom: 10px;
    }
    .stButton>button {
        background-color: #2e4d25;
        color: #ffffff;
        border: 1px solid #4caf50;
        border-radius: 6px;
        font-weight: bold;
        transition: all 0.2s ease-in-out;
    }
    .stButton>button:hover {
        background-color: #3e6832;
        border-color: #d4af37;
        color: #ffffff;
    }
    </style>
""", unsafe_allow_html=True)

# -----------------------------------------------------------------------------
# 2. Inicialização Global e Variáveis de Ambiente (Supabase)
# -----------------------------------------------------------------------------
@st.cache_resource
def init_supabase():
    url = st.secrets.get("SUPABASE_URL", "https://xnihuagckdimywmujtrt.supabase.co")
    key = st.secrets.get("SUPABASE_KEY", "sb_publishable_P_JcgLMuU9kbClJjBDN3Jg_HSLkE_cA")
    if not url or not key:
        return None
    try:
        return create_client(url, key)
    except Exception:
        return None

supabase = init_supabase()

# Listagem oficial de disciplinas da EFA (Escola Família Agrícola)
MATERIAS_TECNICAS_EFA = [
    # Instrumentos da Alternância e AMSP
    "Plano de Estudo (PEF) / Caderno da Realidade",
    "Projeto Profissional do Jovem (PPJ)",
    
    # Formação Técnica em Agropecuária
    "Agricultura (Cafeicultura, Olericultura e Culturas)",
    "Zootecnia (Manejo e Produção Animal)",
    "Agroecologia, SAFs e Meio Ambiente",
    "Solos e Manejo do Solo",
    "Agroindústria e Processamento de Alimentos",
    "Administração Rural e Cooperativismo",
    "Mecanização e Construções Rurais",
    "Extensão Rural e Desenvolvimento Sustentável",
    
    # Base Comum (BNCC)
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

# -----------------------------------------------------------------------------
# 3. Funções Utilitárias e Persistência de Dados (Supabase / Nuvem)
# -----------------------------------------------------------------------------
def tocar_alarme_sonoro():
    components.html("""
        <script>
        var audioCtx = new (window.AudioContext || window.webkitAudioContext)();
        function playBeep(freq, duration) {
            var osc = audioCtx.createOscillator();
            var gain = audioCtx.createGain();
            osc.type = 'sine';
            osc.frequency.value = freq;
            gain.gain.setValueAtTime(0.3, audioCtx.currentTime);
            osc.connect(gain);
            gain.connect(audioCtx.destination);
            osc.start();
            osc.stop(audioCtx.currentTime + duration);
        }
        playBeep(880, 0.3);
        setTimeout(function(){ playBeep(880, 0.3); }, 400);
        setTimeout(function(){ playBeep(1046.50, 0.6); }, 800);
        </script>
    """, height=0)

def hash_senha(senha):
    return hashlib.sha256(senha.encode('utf-8')).hexdigest()

def carregar_usuarios():
    if not supabase:
        return {}
    try:
        res = supabase.table("usuarios").select("*").execute()
        users_dict = {}
        for row in res.data:
            users_dict[row["username"]] = {
                "nome": row["nome"],
                "senha": row["senha"],
                "criado_em": row.get("criado_em", "")
            }
        return users_dict
    except Exception as e:
        st.error(f"Erro ao carregar usuários do Supabase: {e}")
        return {}

def salvar_usuarios(usuarios):
    if not supabase:
        return
    try:
        data_to_upsert = []
        for u, v in usuarios.items():
            data_to_upsert.append({
                "username": u,
                "nome": v.get("nome", u),
                "senha": v.get("senha", ""),
                "criado_em": v.get("criado_em", str(datetime.date.today()))
            })
        if data_to_upsert:
            supabase.table("usuarios").upsert(data_to_upsert).execute()
    except Exception as e:
        st.error(f"Erro ao salvar usuário no Supabase: {e}")

def get_edital_espcex():
    return [
        # --- 1º DIA DE PROVA ---
        {"Dia": "1º Dia", "Matéria": "Português", "Tópico": "Compreensão e Interpretação de Texto", "Status": "Não Iniciado", "Prioridade": "Alta"},
        {"Dia": "1º Dia", "Matéria": "Português", "Tópico": "Morfologia e Classes Gramaticais", "Status": "Não Iniciado", "Prioridade": "Alta"},
        {"Dia": "1º Dia", "Matéria": "Português", "Tópico": "Sintaxe da Oração e do Período", "Status": "Não Iniciado", "Prioridade": "Alta"},
        {"Dia": "1º Dia", "Matéria": "Português", "Tópico": "Concordância, Regência e Crase", "Status": "Não Iniciado", "Prioridade": "Alta"},
        {"Dia": "1º Dia", "Matéria": "Português", "Tópico": "Pontuação e Ortografia Oficial", "Status": "Não Iniciado", "Prioridade": "Média"},
        {"Dia": "1º Dia", "Matéria": "Português", "Tópico": "Literatura Brasileira (Barroco ao Modernismo)", "Status": "Não Iniciado", "Prioridade": "Alta"},
        
        {"Dia": "1º Dia", "Matéria": "Redação", "Tópico": "Estrutura Dissertativo-Argumentativa Modelo EsPCEx", "Status": "Não Iniciado", "Prioridade": "Alta"},
        {"Dia": "1º Dia", "Matéria": "Redação", "Tópico": "Elaboração de Tese e Projeto de Texto", "Status": "Não Iniciado", "Prioridade": "Alta"},
        {"Dia": "1º Dia", "Matéria": "Redação", "Tópico": "Desenvolvimento e Argumentação Lógica", "Status": "Não Iniciado", "Prioridade": "Alta"},
        {"Dia": "1º Dia", "Matéria": "Redação", "Tópico": "Conclusão e Fechamento das Ideias", "Status": "Não Iniciado", "Prioridade": "Alta"},

        {"Dia": "1º Dia", "Matéria": "Física", "Tópico": "Cinemática Escalar e Vetorial", "Status": "Não Iniciado", "Prioridade": "Alta"},
        {"Dia": "1º Dia", "Matéria": "Física", "Tópico": "Leis de Newton e Dinâmica", "Status": "Não Iniciado", "Prioridade": "Alta"},
        {"Dia": "1º Dia", "Matéria": "Física", "Tópico": "Trabalho, Energia e Potência", "Status": "Não Iniciado", "Prioridade": "Alta"},
        {"Dia": "1º Dia", "Matéria": "Física", "Tópico": "Termologia, Calorimetria e Gases", "Status": "Não Iniciado", "Prioridade": "Alta"},
        {"Dia": "1º Dia", "Matéria": "Física", "Tópico": "Eletrostática e Eletrodinâmica", "Status": "Não Iniciado", "Prioridade": "Alta"},

        {"Dia": "1º Dia", "Matéria": "Química", "Tópico": "Estrutura Atômica e Tabela Periódica", "Status": "Não Iniciado", "Prioridade": "Alta"},
        {"Dia": "1º Dia", "Matéria": "Química", "Tópico": "Ligações Químicas e Funções Inorgânicas", "Status": "Não Iniciado", "Prioridade": "Alta"},
        {"Dia": "1º Dia", "Matéria": "Química", "Tópico": "Reações Químicas e Estequiometria", "Status": "Não Iniciado", "Prioridade": "Alta"},
        {"Dia": "1º Dia", "Matéria": "Química", "Tópico": "Química Orgânica", "Status": "Não Iniciado", "Prioridade": "Alta"},

        # --- 2º DIA DE PROVA ---
        {"Dia": "2º Dia", "Matéria": "Matemática", "Tópico": "Conjuntos, Relações e Funções", "Status": "Não Iniciado", "Prioridade": "Alta"},
        {"Dia": "2º Dia", "Matéria": "Matemática", "Tópico": "Função Exponencial e Logarítmica", "Status": "Não Iniciado", "Prioridade": "Alta"},
        {"Dia": "2º Dia", "Matéria": "Matemática", "Tópico": "Trigonometria", "Status": "Não Iniciado", "Prioridade": "Alta"},
        {"Dia": "2º Dia", "Matéria": "Matemática", "Tópico": "Geometria Plana", "Status": "Não Iniciado", "Prioridade": "Alta"},
        {"Dia": "2º Dia", "Matéria": "Matemática", "Tópico": "Geometria Espacial", "Status": "Não Iniciado", "Prioridade": "Alta"},
        {"Dia": "2º Dia", "Matéria": "Matemática", "Tópico": "Geometria Analítica", "Status": "Não Iniciado", "Prioridade": "Alta"},

        {"Dia": "2º Dia", "Matéria": "Geografia", "Tópico": "Geografia Física do Brasil", "Status": "Não Iniciado", "Prioridade": "Alta"},
        {"Dia": "2º Dia", "Matéria": "Geografia", "Tópico": "Demografia e População", "Status": "Não Iniciado", "Prioridade": "Alta"},
        {"Dia": "2º Dia", "Matéria": "Geografia", "Tópico": "Espaço Agropecuário e Questão Agrária", "Status": "Não Iniciado", "Prioridade": "Alta"},
        {"Dia": "2º Dia", "Matéria": "Geografia", "Tópico": "Geopolítica Global", "Status": "Não Iniciado", "Prioridade": "Alta"},

        {"Dia": "2º Dia", "Matéria": "História", "Tópico": "Brasil Colônia e Império", "Status": "Não Iniciado", "Prioridade": "Alta"},
        {"Dia": "2º Dia", "Matéria": "História", "Tópico": "República e Era Vargas", "Status": "Não Iniciado", "Prioridade": "Alta"},
        {"Dia": "2º Dia", "Matéria": "História", "Tópico": "História Geral: Guerras Mundiais e Guerra Fria", "Status": "Não Iniciado", "Prioridade": "Alta"},

        {"Dia": "2º Dia", "Matéria": "Inglês", "Tópico": "Compreensão de Textos Táticos", "Status": "Não Iniciado", "Prioridade": "Alta"},
        {"Dia": "2º Dia", "Matéria": "Inglês", "Tópico": "Gramática Aplicada", "Status": "Não Iniciado", "Prioridade": "Alta"}
    ]

def carregar_dados_usuario(username):
    u_clean = username.lower().strip()
    padrao = {
        "sessoes": [],
        "erros": [],
        "redacoes": [],
        "taf": [],
        "tarefas_casa": [],
        "efa_ciclo": {
            "modo": "Em Casa",
            "inicio_ciclo": str(datetime.date.today())
        },
        "edital": get_edital_espcex(),
        "simulados": [],
        "pomodoro_ciclos_totais": 0,
        "data_prova": str(datetime.date.today() + datetime.timedelta(days=320)),
        "kanban": [
            {"id": 1, "titulo": "Relatório de Solo e Adubação", "status": "A Fazer"},
            {"id": 2, "titulo": "Pesquisa sobre Cooperativismo", "status": "Em Andamento"},
            {"id": 3, "titulo": "Apresentação AMSP 01", "status": "Entregue"}
        ],
        "diario_bordo": [],
        "habits": {"streak": 12, "historico": {}},
        "temas_custom": []
    }
    
    if supabase:
        try:
            res = supabase.table("dados_usuario").select("dados").eq("username", u_clean).execute()
            if res.data and len(res.data) > 0:
                dados = res.data[0]["dados"]
                for k, v in padrao.items():
                    if k not in dados:
                        dados[k] = v
                return dados
        except Exception as e:
            st.warning(f"Erro ao carregar dados do Supabase: {e}")
            
    return padrao

def salvar_dados_usuario(username, dados):
    u_clean = username.lower().strip()
    if supabase:
        try:
            payload = {
                "username": u_clean,
                "dados": dados
            }
            supabase.table("dados_usuario").upsert(payload).execute()
        except Exception as e:
            st.error(f"Erro ao salvar na nuvem (Supabase): {e}")

# -----------------------------------------------------------------------------
# 4. Controle de Autenticação / Login
# -----------------------------------------------------------------------------
if "usuario_logado" not in st.session_state:
    st.session_state.usuario_logado = None

if st.session_state.usuario_logado is None:
    st.title("🛡️ QG EsPCEx - Acesso Restrito")
    st.caption("Sistema Tático de Estudos & Treinamento Físico Militar")

    tab_entrar, tab_cadastrar = st.tabs(["🔐 Entrar na Conta", "📝 Criar Nova Conta"])

    with tab_entrar:
        st.subheader("Login do Cadete")
        login_user = st.text_input("Utilizador / Nome de Guerra", key="input_login_user")
        login_pass = st.text_input("Senha", type="password", key="input_login_pass")

        if st.button("🔑 Entrar no QG"):
            usuarios = carregar_usuarios()
            u_clean = login_user.lower().strip()
            if u_clean in usuarios and usuarios[u_clean]["senha"] == hash_senha(login_pass):
                st.session_state.usuario_logado = u_clean
                st.session_state.nome_exibicao = usuarios[u_clean].get("nome", login_user)
                st.success(f"Bem-vindo, Cadete {st.session_state.nome_exibicao}!")
                st.rerun()
            else:
                st.error("Utilizador ou senha incorretos.")

    with tab_cadastrar:
        st.subheader("Cadastro de Novo Cadete")
        cad_nome = st.text_input("Nome Completo / Guerra", key="input_cad_nome")
        cad_user = st.text_input("Nome de Utilizador (Login)", key="input_cad_user")
        cad_pass = st.text_input("Criar Senha", type="password", key="input_cad_pass")
        cad_conf = st.text_input("Confirmar Senha", type="password", key="input_cad_conf")

        if st.button("📝 Criar Minha Conta"):
            usuarios = carregar_usuarios()
            u_clean = cad_user.lower().strip()
            if not u_clean or not cad_pass:
                st.warning("Preencha todos os campos.")
            elif u_clean in usuarios:
                st.error("Nome de utilizador já existe.")
            elif cad_pass != cad_conf:
                st.error("Senhas não coincidem.")
            else:
                usuarios[u_clean] = {
                    "nome": cad_nome if cad_nome else cad_user,
                    "senha": hash_senha(cad_pass),
                    "criado_em": str(datetime.date.today())
                }
                salvar_usuarios(usuarios)
                st.success("Conta criada! Acesse a aba 'Entrar na Conta'.")

    st.stop()

# -----------------------------------------------------------------------------
# 5. Carregamento do Usuário Logado & Barra Lateral (Sidebar)
# -----------------------------------------------------------------------------
user_atual = st.session_state.usuario_logado
db = carregar_dados_usuario(user_atual)
nome_cadete = st.session_state.get("nome_exibicao", user_atual)

st.sidebar.title("⚔️ QG ESPCEX")
st.sidebar.markdown(f"**Cadete:** `{nome_cadete}`")

if st.sidebar.button("🚪 Sair (Logout)"):
    st.session_state.usuario_logado = None
    st.rerun()

st.sidebar.markdown("---")

menu = st.sidebar.radio(
    "Central de Operações",
    [
        "📊 Painel & Rotina Diária",
        "🦉 Portal Estratégia Militares",
        "🏋️ Treino TAF + Spotify",
        "🌾 Alternância EFA & AMSP (15/15)",
        "⏱️ Pomodoro Tático (com Alarme)",
        "✍️ Módulo de Redação",
        "📋 Edital Verticalizado EsPCEx",
        "📝 Registro de Questões",
        "📕 Caderno de Bizus & Erros",
        "🎯 Simulados (1º e 2º Dia)",
        "📊 Visualizador de Planilhas",
        "🎲 Sorteador de Temas & Textos Base",
        "🏃 Módulo 13: Pacer TAF",
        "🥗 Nutrição & Calorias",
        "🌾 Calc. Agropecuária",
        "📌 Kanban EFA",
        "🔥 Habit Tracker",
        "📅 Cronograma Dinâmico",
        "🔗 Hub de Links",
        "📖 Diário de Bordo",
        "🎧 Player de Áudio"
    ]
)

st.sidebar.markdown("---")

# Cálculo do ciclo EFA (15 dias)
efa_info = db.get("efa_ciclo", {"modo": "Em Casa", "inicio_ciclo": str(datetime.date.today())})
try:
    dt_inicio = datetime.datetime.strptime(efa_info["inicio_ciclo"], "%Y-%m-%d").date()
except Exception:
    dt_inicio = datetime.date.today()

dias_decorridos = (datetime.date.today() - dt_inicio).days + 1
dia_no_ciclo = min(max(dias_decorridos, 1), 15)

st.sidebar.markdown(f"**Sessão EFA:** `{efa_info['modo']}`")
st.sidebar.markdown(f"**Progresso EFA:** `Dia {dia_no_ciclo} de 15`")

st.sidebar.markdown("---")
st.sidebar.subheader("⏳ Contagem Regressiva EsPCEx")

data_prova_str = db.get("data_prova", str(datetime.date.today()))
try:
    data_prova = datetime.datetime.strptime(data_prova_str, "%Y-%m-%d").date()
except ValueError:
    data_prova = datetime.date.today()

nova_data = st.sidebar.date_input("Data da Prova", data_prova)
if nova_data != data_prova:
    db["data_prova"] = str(nova_data)
    salvar_dados_usuario(user_atual, db)
    st.rerun()

dias_restantes = (nova_data - datetime.date.today()).days
st.sidebar.metric("Dias até a Prova", f"{dias_restantes} dias")
st.sidebar.markdown(f"**Ciclos Pomodoro:** `{db.get('pomodoro_ciclos_totais', 0)}`")

# -----------------------------------------------------------------------------
# MÓDULO 1: Painel & Cronograma da Rotina Diária
# -----------------------------------------------------------------------------
if menu == "📊 Painel & Rotina Diária":
    st.title(f"📊 QG EsPCEx - Painel de Prontidão de {nome_cadete}")
    st.caption("Visão integrada dos estudos noturnos, treino físico da madrugada, trabalho no campo e realização da AMSP.")

    modo_efa = efa_info["modo"]

    df_sessoes = pd.DataFrame(db["sessoes"])
    df_red = pd.DataFrame(db.get("redacoes", []))
    df_taf = pd.DataFrame(db.get("taf", []))
    amsp_pendentes = [t for t in db.get("tarefas_casa", []) if not t.get("concluida", False)]

    total_horas = df_sessoes["Horas"].sum() if not df_sessoes.empty else 0.0
    total_questoes = df_sessoes["Questões"].sum() if not df_sessoes.empty else 0
    total_acertos = df_sessoes["Acertos"].sum() if not df_sessoes.empty else 0
    taxa_acerto = (total_acertos / total_questoes * 100) if total_questoes > 0 else 0.0

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("⏱️ Horas Noturnas", f"{total_horas:.1f} h")
    c2.metric("📝 Questões Feitas", f"{total_questoes}")
    c3.metric("🎯 Taxa de Acerto", f"{taxa_acerto:.1f}%")
    c4.metric("🏋️ Treinos TAF (05h)", f"{len(df_taf)}")
    if modo_efa == "Em Casa":
        c5.metric("📚 AMSP Pendentes", f"{len(amsp_pendentes)}")
    else:
        c5.metric("🏫 Período Ativo", "Na Escola")

    st.markdown("---")
    st.subheader(f"📅 Estrutura da Sua Rotina - Período [{modo_efa.upper()}] (Dia {dia_no_ciclo}/15)")

    if modo_efa == "Em Casa":
        r1, r2, r3, r4 = st.columns(4)
        with r1:
            st.markdown("""
            <div class="routine-card">
            <h4>🌅 05:00 - 06:15</h4>
            <b>Treino TAF + SPOTIFY</b><br>
            Corrida, Flexão, Abdominal e Barra Fixa ao som de Marchas do Exército / Funk TAF.
            </div>
            """, unsafe_allow_html=True)

        with r2:
            st.markdown("""
            <div class="routine-card">
            <h4>🚜 06:30 - 17:00</h4>
            <b>Trabalho no Campo / Propriedade</b><br>
            Atividades agrícolas e braçais da família na alternância.
            </div>
            """, unsafe_allow_html=True)

        with r3:
            st.markdown("""
            <div class="routine-card">
            <h4>📖 17:00 - 18:30</h4>
            <b>Descanso & AMSP (Matérias EFA)</b><br>
            Pausa de recuperação e resolução da AMSP da EFA.
            </div>
            """, unsafe_allow_html=True)

        with r4:
            st.markdown("""
            <div class="routine-card">
            <h4>📚 18:30 - 22:30</h4>
            <b>Estudos Noturnos EsPCEx</b><br>
            Blocos de Pomodoro e plataforma Estratégia Militares.
            </div>
            """, unsafe_allow_html=True)
    else:
        r1, r2, r3 = st.columns(3)
        with r1:
            st.markdown("""
            <div class="routine-card">
            <h4>🌅 05:00 - 06:15</h4>
            <b>Treino TAF Madrugada</b><br>
            Treino de aptidão física militar com áudio motivacional.
            </div>
            """, unsafe_allow_html=True)

        with r2:
            st.markdown("""
            <div class="routine-card">
            <h4>🏫 07:00 - 17:00</h4>
            <b>Aulas Presenciais EFA</b><br>
            Disciplinas teóricas e práticas da Escola Família Agrícola.
            </div>
            """, unsafe_allow_html=True)

        with r3:
            st.markdown("""
            <div class="routine-card">
            <h4>📚 18:30 - 22:30</h4>
            <b>QG Noturno EsPCEx</b><br>
            Revisão teórica, LDI, BQEM e simulados do Estratégia.
            </div>
            """, unsafe_allow_html=True)

    st.markdown("---")

    col_g1, col_g2 = st.columns(2)
    with col_g1:
        st.subheader("📈 Rendimento em Questões")
        if not df_sessoes.empty:
            df_mat = df_sessoes.groupby("Matéria")[["Acertos", "Questões"]].sum().reset_index()
            df_mat["Erros"] = (df_mat["Questões"] - df_mat["Acertos"]).clip(lower=0)
            fig_mat = px.bar(df_mat, x="Matéria", y=["Acertos", "Erros"], barmode="stack", color_discrete_sequence=["#2e7d32", "#c62828"])
            fig_mat.update_layout(paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)", font_color="#e0e6ed")
            st.plotly_chart(fig_mat, use_container_width=True)
        else:
            st.info("Nenhuma bateria de questões registrada.")

    with col_g2:
        st.subheader("📋 Progresso do Edital")
        df_edital = pd.DataFrame(db["edital"])
        if not df_edital.empty:
            df_status = df_edital["Status"].value_counts().reset_index()
            df_status.columns = ["Status", "Quantidade"]
            fig_edital = px.pie(df_status, names="Status", values="Quantidade", hole=0.4, color_discrete_map={"Não Iniciado": "#333333", "Teoria Lida": "#f57c00", "Exercícios Feitos": "#0288d1", "Revisado / Dominado": "#2e7d32"})
            fig_edital.update_layout(paper_bgcolor="rgba(0,0,0,0)", font_color="#e0e6ed")
            st.plotly_chart(fig_edital, use_container_width=True)

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
    st.title("🏋️ Treinamento Físico Militar - TAF EsPCEx")
    st.caption("Acompanhamento dos treinos das 05h00 e player oficial do Spotify integrado.")

    aba_registro, aba_evolucao, aba_spotify = st.tabs([
        "➕ Cadastrar Treino (05h)",
        "📈 Evolução Física",
        "🎧 Spotify Player Integrado"
    ])

    with aba_registro:
        st.markdown("""
        **Índices Mínimos Masculinos (EsPCEx):**
        - 🏃‍♂️ **Corrida 12 min:** Mínimo `2.450 metros`
        - 💪 **Flexão de Braço:** Mínimo `21 repetições`
        - 🧘‍♂️ **Abdominal Supra:** Mínimo `30 repetições` (em 1 minuto)
        - 🏋️‍♂️ **Barra Fixa:** Mínimo `3 repetições`
        """)

        with st.form("form_taf", clear_on_submit=True):
            col_t1, col_t2 = st.columns(2)
            data_treino = col_t1.date_input("Data do Treino", datetime.date.today())
            modalidade = col_t2.selectbox("Foco Principal", ["Treino Completo TAF", "Corrida de Tiro/Resistência", "Calistenia (Barra/Flexão/Abs)", "Simulado TAF Oficial"])

            c_f1, c_f2, c_f3, c_f4 = st.columns(4)
            flexoes = c_f1.number_input("Flexões de Braço", min_value=0, value=22)
            abdominais = c_f2.number_input("Abdominais (1 min)", min_value=0, value=32)
            barras = c_f3.number_input("Barra Fixa", min_value=0, value=4)
            corrida_m = c_f4.number_input("Corrida 12 min (Metros)", min_value=0, value=2500, step=50)

            obs_taf = st.text_input("Observações do Treino")

            if st.form_submit_button("⚔️ Registrar Treino TAF"):
                ok_flex = flexoes >= 21
                ok_abs = abdominais >= 30
                ok_bar = barras >= 3
                ok_cor = corrida_m >= 2450
                aprovado_taf = ok_flex and ok_abs and ok_bar and ok_cor

                novo_t = {
                    "Data": str(data_treino),
                    "Foco": modalidade,
                    "Flexões": flexoes,
                    "Abdominais": abdominais,
                    "Barra": barras,
                    "Corrida (m)": corrida_m,
                    "Índice Mínimo": "✅ APROVADO" if aprovado_taf else "⚠️ ABAIXO",
                    "Observações": obs_taf
                }
                db["taf"].append(novo_t)
                salvar_dados_usuario(user_atual, db)

                if aprovado_taf:
                    st.success("🎉 EXCELENTE! Atingiu todos os índices mínimos do TAF da EsPCEx!")
                else:
                    st.warning("Manter a constância nos treinos!")
                st.rerun()

    with aba_evolucao:
        df_taf = pd.DataFrame(db.get("taf", []))
        if not df_taf.empty:
            col_g_taf1, col_g_taf2 = st.columns(2)
            with col_g_taf1:
                fig_cor = px.line(df_taf, x="Data", y="Corrida (m)", markers=True, title="Evolução na Corrida de 12 Minutos")
                fig_cor.add_hline(y=2450, line_dash="dash", line_color="green", annotation_text="Mínimo EsPCEx (2450m)")
                fig_cor.update_layout(paper_bgcolor="rgba(0,0,0,0)", font_color="#e0e6ed")
                st.plotly_chart(fig_cor, use_container_width=True)

            with col_g_taf2:
                fig_rep = px.line(df_taf, x="Data", y=["Flexões", "Abdominais", "Barra"], markers=True, title="Repetições nos Exercícios de Força")
                fig_rep.update_layout(paper_bgcolor="rgba(0,0,0,0)", font_color="#e0e6ed")
                st.plotly_chart(fig_rep, use_container_width=True)

            st.dataframe(df_taf, use_container_width=True)
        else:
            st.info("Nenhum treino TAF cadastrado até o momento.")

    with aba_spotify:
        st.subheader("🎧 Player Oficial do Spotify no QG EsPCEx")
        st.caption("Ouça suas músicas diretamente pelo Spotify na plataforma durante o treino das 05h00:")

        st.markdown("""
        <div style="margin-bottom: 15px;">
            <a href="spotify://" style="background-color: #1DB954; color: black; padding: 10px 18px; border-radius: 20px; text-decoration: none; font-weight: bold; display: inline-block;">
                📱 Abrir App do Spotify no Telemóvel
            </a>
        </div>
        """, unsafe_allow_html=True)

        playlist_escolhida = st.selectbox("Escolher Estilo / Playlist do Spotify", [
            "🥁 Canções e Marchas Militares do Exército Brasileiro",
            "🔥 Funk Motivacional / Treino TAF (BPM Alto)",
            "🎧 Workout Beats (Eletrónica / Hip-Hop)",
            "⚡ Rock Workout (Treino Pesado)",
            "🤠 Sertanejo / Gospel Workout",
            "🔗 Cole o Seu Próprio Link do Spotify (Playlist/Música)"
        ])

        if playlist_escolhida == "🥁 Canções e Marchas Militares do Exército Brasileiro":
            components.iframe("https://open.spotify.com/embed/playlist/37i9dQZF1DXdLENI3B52e1?utm_source=generator&theme=0", height=380)

        elif playlist_escolhida == "🔥 Funk Motivacional / Treino TAF (BPM Alto)":
            components.iframe("https://open.spotify.com/embed/playlist/37i9dQZF1DX8nt3K81J24A?utm_source=generator&theme=0", height=380)

        elif playlist_escolhida == "🎧 Workout Beats (Eletrónica / Hip-Hop)":
            components.iframe("https://open.spotify.com/embed/playlist/37i9dQZF1DX76Wlfd11S3M?utm_source=generator&theme=0", height=380)

        elif playlist_escolhida == "⚡ Rock Workout (Treino Pesado)":
            components.iframe("https://open.spotify.com/embed/playlist/37i9dQZF1DX3531lA3283T?utm_source=generator&theme=0", height=380)

        elif playlist_escolhida == "🤠 Sertanejo / Gospel Workout":
            components.iframe("https://open.spotify.com/embed/playlist/37i9dQZF1DXdS1C2By3I9e?utm_source=generator&theme=0", height=380)

        elif playlist_escolhida == "🔗 Cole o Seu Próprio Link do Spotify (Playlist/Música)":
            url_spotify = st.text_input("Cole aqui a URL da sua Playlist/Música do Spotify (ex: https://open.spotify.com/playlist/...)")
            if url_spotify:
                if "spotify.com" in url_spotify:
                    embed_url = url_spotify.replace("open.spotify.com/", "open.spotify.com/embed/")
                    components.iframe(embed_url, height=380)
                else:
                    st.warning("Insira uma URL válida do Spotify.")

# -----------------------------------------------------------------------------
# MÓDULO 4: Alternância EFA & AMSP (15/15) - Disciplinas da EFA
# -----------------------------------------------------------------------------
elif menu == "🌾 Alternância EFA & AMSP (15/15)":
    st.title("🌾 Gestão da Alternância EFA & AMSP")
    st.caption("Controle dos ciclos de 15 dias e gestão da AMSP focada nas matérias da EFA.")

    with st.expander("⚙️ Configurar Ciclo Atual de Alternância", expanded=True):
        col_c1, col_c2, col_c3 = st.columns(3)
        modo_selecionado = col_c1.selectbox("Período Ativo do Ciclo", ["Em Casa", "Na Escola"], index=0 if efa_info["modo"] == "Em Casa" else 1)
        
        try:
            dt_init_val = datetime.datetime.strptime(efa_info["inicio_ciclo"], "%Y-%m-%d").date()
        except Exception:
            dt_init_val = datetime.date.today()

        data_inicio_input = col_c2.date_input("Data de Início dos 15 Dias", dt_init_val)

        if col_c3.button("💾 Atualizar Estado do Ciclo"):
            db["efa_ciclo"] = {
                "modo": modo_selecionado,
                "inicio_ciclo": str(data_inicio_input)
            }
            salvar_dados_usuario(user_atual, db)
            st.success("Estado do ciclo EFA salvo!")
            st.rerun()

    st.markdown("---")

    modo_atual = db["efa_ciclo"]["modo"]

    if modo_atual == "Em Casa":
        st.subheader("🏠 Período EM CASA - Central da AMSP (Atividades da EFA)")
        st.info("💡 Registre aqui as pesquisas do Caderno da Realidade, PEF e tarefas de todas as matérias da EFA para o horário das 17h00 - 18h30.")

        with st.form("form_tarefa_casa", clear_on_submit=True):
            st.markdown("#### ➕ Cadastrar Nova AMSP por Matéria da EFA")
            
            col_tf1, col_tf2 = st.columns(2)
            disciplina_amsp = col_tf1.selectbox("Matéria da EFA", MATERIAS_TECNICAS_EFA)
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
                nova_tarefa = {
                    "id": len(db.get("tarefas_casa", [])) + 1,
                    "disciplina": disciplina_amsp,
                    "titulo": titulo_tf,
                    "categoria": categoria_tf,
                    "prazo": str(prazo_tf),
                    "prioridade": prioridade_tf,
                    "detalhes": detalhes_tf,
                    "concluida": False,
                    "criada_em": str(datetime.date.today())
                }
                db["tarefas_casa"].append(nova_tarefa)
                salvar_dados_usuario(user_atual, db)
                st.success("AMSP cadastrada com sucesso!")
                st.rerun()

        st.markdown("---")
        st.subheader("📋 Painel de AMSP (Filtro por Matéria da EFA)")

        tarefas = db.get("tarefas_casa", [])
        if not tarefas:
            st.info("Nenhuma AMSP cadastrada até o momento.")
        else:
            filtro_disc = st.selectbox("Filtrar AMSP por Matéria da EFA", ["Todas as Matérias"] + MATERIAS_TECNICAS_EFA)
            
            tarefas_exibir = tarefas if filtro_disc == "Todas as Matérias" else [t for t in tarefas if t.get("disciplina") == filtro_disc]

            p_tab1, p_tab2 = st.tabs(["⏳ AMSP Pendentes", "✅ AMSP Concluídas"])

            with p_tab1:
                pendentes = [t for t in tarefas_exibir if not t.get("concluida", False)]
                if not pendentes:
                    st.success("🎉 Nenhuma AMSP pendente para a seleção atual!")
                else:
                    for t in pendentes:
                        disc_nome = t.get("disciplina", "Geral")
                        with st.container():
                            st.markdown(f"### 🌾 [{disc_nome}] - {t['titulo']}")
                            st.markdown(f"**Tipo:** `{t['categoria']}` | **Prazo:** `{t['prazo']}` | **Prioridade:** `{t['prioridade']}`")
                            st.write(t["detalhes"])

                            if st.button(f"✅ Marcar AMSP Concluída #{t['id']}", key=f"btn_tf_{t['id']}"):
                                t["concluida"] = True
                                salvar_dados_usuario(user_atual, db)
                                st.success("AMSP concluída!")
                                st.rerun()
                            st.markdown("---")

            with p_tab2:
                concluidas = [t for t in tarefas_exibir if t.get("concluida", False)]
                if not concluidas:
                    st.info("Nenhuma AMSP concluída nesta seleção.")
                else:
                    for t in concluidas:
                        disc_nome = t.get("disciplina", "Geral")
                        st.markdown(f"~~**[{disc_nome}]** - {t['titulo']} ({t['categoria']})~~ ✅ Concluída")

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
# MÓDULO 7: Edital Verticalizado
# -----------------------------------------------------------------------------
elif menu == "📋 Edital Verticalizado EsPCEx":
    st.title("📋 Edital Verticalizado EsPCEx")
    df_edital = pd.DataFrame(db["edital"])

    col_f1, col_f2 = st.columns(2)
    dia_f = col_f1.selectbox("Dia da Prova", ["Todos", "1º Dia", "2º Dia"])
    mat_f = col_f2.selectbox("Disciplina", ["Todas"] + list(df_edital["Matéria"].unique()))

    df_exib = df_edital.copy()
    if dia_f != "Todos":
        df_exib = df_exib[df_exib["Dia"] == dia_f]
    if mat_f != "Todas":
        df_exib = df_exib[df_exib["Matéria"] == mat_f]

    edited_df = st.data_editor(
        df_exib,
        column_config={
            "Status": st.column_config.SelectboxColumn("Status", options=["Não Iniciado", "Teoria Lida", "Exercícios Feitos", "Revisado / Dominado"], required=True),
            "Prioridade": st.column_config.SelectboxColumn("Relevância", options=["Baixa", "Média", "Alta"], required=True)
        },
        use_container_width=True,
        key=f"edital_edit_{dia_f}_{mat_f}"
    )

    if st.button("💾 Salvar Edital"):
        novos = edited_df.to_dict("records")
        dict_editados = {x["Tópico"]: x for x in novos}
        edital_atualizado = []
        for item in db["edital"]:
            if item["Tópico"] in dict_editados:
                edital_atualizado.append(dict_editados[item["Tópico"]])
            else:
                edital_atualizado.append(item)
        db["edital"] = edital_atualizado
        salvar_dados_usuario(user_atual, db)
        st.success("Edital atualizado com sucesso!")

# -----------------------------------------------------------------------------
# MÓDULO 8: Registro de Questões
# -----------------------------------------------------------------------------
elif menu == "📝 Registro de Questões":
    st.title("📝 Registro de Baterias de Questões")
    df_edital = pd.DataFrame(db["edital"])

    with st.form("form_q", clear_on_submit=True):
        col1, col2 = st.columns(2)
        data_q = col1.date_input("Data", datetime.date.today())
        materia_q = col2.selectbox("Matéria", sorted(list(df_edital["Matéria"].unique())))

        topicos_mat = df_edital[df_edital["Matéria"] == materia_q]["Tópico"].tolist()
        topico_q = st.selectbox("Assunto", topicos_mat if topicos_mat else ["Geral"])

        col3, col4, col5 = st.columns(3)
        qtd_q = col3.number_input("Total Questões", min_value=1, value=20)
        qtd_a = col4.number_input("Acertos", min_value=0, max_value=int(qtd_q), value=15)
        tempo_h = col5.number_input("Horas Dedicadas", min_value=0.1, max_value=10.0, value=1.0, step=0.1)

        if st.form_submit_button("⚔️ Salvar Bateria"):
            nova_s = {
                "Data": str(data_q), "Matéria": materia_q, "Tópico": topico_q,
                "Questões": int(qtd_q), "Acertos": int(qtd_a), "Horas": float(tempo_h)
            }
            db["sessoes"].append(nova_s)
            salvar_dados_usuario(user_atual, db)
            st.success("Bateria registrada!")
            st.rerun()

    st.markdown("---")
    if db["sessoes"]:
        st.dataframe(pd.DataFrame(db["sessoes"]).sort_values(by="Data", ascending=False), use_container_width=True)

# -----------------------------------------------------------------------------
# MÓDULO 9: Caderno de Bizus & Erros
# -----------------------------------------------------------------------------
elif menu == "📕 Caderno de Bizus & Erros":
    st.title("📕 Caderno de Bizus Táticos e Erros")

    with st.expander("➕ Cadastrar Novo Bisu", expanded=True):
        with st.form("form_e", clear_on_submit=True):
            df_edital = pd.DataFrame(db["edital"])
            c1, c2 = st.columns(2)
            mat_e = c1.selectbox("Matéria", sorted(list(df_edital["Matéria"].unique())))
            assunto_e = c2.text_input("Assunto / Questão")

            motivo = st.text_area("Qual foi a pegadinha / erro?")
            bisu = st.text_area("Bisu de Correção Tática")

            if st.form_submit_button("📌 Salvar Bisu"):
                novo_e = {
                    "id": len(db["erros"]) + 1, "Data": str(datetime.date.today()),
                    "Matéria": mat_e, "Assunto": assunto_e, "Motivo": motivo,
                    "Bisu": bisu, "Sanado": False
                }
                db["erros"].append(novo_e)
                salvar_dados_usuario(user_atual, db)
                st.success("Bisu registrado!")
                st.rerun()

    st.markdown("---")
    erros_pendentes = [e for e in db.get("erros", []) if not e.get("Sanado", False)]
    if not erros_pendentes:
        st.success("🎉 Nenhum bisu pendente de revisão!")
    else:
        for err in erros_pendentes:
            st.markdown(f"### ⚔️ [{err['Matéria']}] - {err['Assunto']}")
            st.markdown(f"**Causa:** {err['Motivo']}")
            st.info(f"💡 **BISU:** {err['Bisu']}")
            if st.button(f"✅ Marcar como Sanado #{err['id']}", key=f"btn_s_{err['id']}"):
                err["Sanado"] = True
                salvar_dados_usuario(user_atual, db)
                st.rerun()
            st.markdown("---")

# -----------------------------------------------------------------------------
# MÓDULO 10: Simulados
# -----------------------------------------------------------------------------
elif menu == "🎯 Simulados (1º e 2º Dia)":
    st.title("🎯 Simulados EsPCEx (1º e 2º Dia)")

    with st.expander("➕ Lançar Simulado", expanded=True):
        with st.form("form_sim"):
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
                pct_obj = (tot_obj / 100) * 100
                aprovado = all(minimos.values())

                novo_s = {
                    "Nome": nome_sim, "Data": str(data_sim),
                    "Objetiva": f"{tot_obj}/100 ({pct_obj:.1f}%)",
                    "Redação": p_red, "Mínimos Atingidos": "✅ SIM" if aprovado else "❌ NÃO"
                }
                db["simulados"].append(novo_s)
                salvar_dados_usuario(user_atual, db)

                if aprovado:
                    st.success(f"🎉 APROVADO NOS MÍNIMOS! {tot_obj}/100 acertos e Redação {p_red}.")
                else:
                    reprov = [m for m, ok in minimos.items() if not ok]
                    st.error(f"⚠️ Mínimo não atingido em: {', '.join(reprov)}")
                st.rerun()

    st.markdown("---")
    if db["simulados"]:
        st.dataframe(pd.DataFrame(db["simulados"]), use_container_width=True)

# -----------------------------------------------------------------------------
# MÓDULO 11: Cronograma Integrado EFA & EsPCEx
# -----------------------------------------------------------------------------
elif menu == "📊 Visualizador de Planilhas":
    st.title("📊 Cronograma Integrado EFA, AMSP & EsPCEx")
    st.caption("Planilha oficial de planeamento carregada diretamente do repositório ou por upload.")

    NOME_DO_ARQUIVO = "Plano_de_Estudos_EFA_Status_Automatico.xlsx"
    uploaded_file = st.file_uploader("Enviar Planilha Personalizada (.xlsx)", type=["xlsx"])

    arquivo_usar = uploaded_file if uploaded_file else (NOME_DO_ARQUIVO if os.path.exists(NOME_DO_ARQUIVO) else None)

    if arquivo_usar is not None:
        try:
            df_plano = pd.read_excel(arquivo_usar, skiprows=3)
            df_plano = df_plano.dropna(how='all')

            total_atividades = len(df_plano)
            concluidas = len(df_plano[df_plano["Status"] == "Concluído"]) if "Status" in df_plano.columns else 0
            em_andamento = len(df_plano[df_plano["Status"] == "Em Andamento"]) if "Status" in df_plano.columns else 0
            pendentes = len(df_plano[df_plano["Status"] == "Pendente"]) if "Status" in df_plano.columns else 0

            m1, m2, m3, m4 = st.columns(4)
            m1.metric("Total de Blocos", total_atividades)
            m2.metric("Concluídos ✅", concluidas)
            m3.metric("Em Andamento ⏳", em_andamento)
            m4.metric("Pendentes 📌", pendentes)

            st.markdown("---")

            if "Semana" in df_plano.columns:
                semanas_opcoes = ["Todas as Semanas"] + list(df_plano["Semana"].dropna().unique())
                semana_selecionada = st.selectbox("🔍 Filtrar por Período da Alternância:", semanas_opcoes)

                if semana_selecionada != "Todas as Semanas":
                    df_exibicao = df_plano[df_plano["Semana"] == semana_selecionada]
                else:
                    df_exibicao = df_plano
            else:
                df_exibicao = df_plano

            st.dataframe(df_exibicao, use_container_width=True, hide_index=True)

        except Exception as e:
            st.error(f"Erro ao carregar a planilha: {e}")
    else:
        st.info("Envie um arquivo `.xlsx` acima ou adicione `Plano_de_Estudos_EFA_Status_Automatico.xlsx` à pasta da aplicação.")

# -----------------------------------------------------------------------------
# MÓDULO 12: SORTEADOR DE TEMAS & TEXTOS BASE
# -----------------------------------------------------------------------------
elif menu == "🎲 Sorteador de Temas & Textos Base":
    st.header("🎲 Módulo 12: Sorteador Tático de Temas & Textos Motivadores")
    st.markdown("Sorteie propostas completas de redação com **Textos Base Motivadores (Texto I e Texto II)**, eixos temáticos e repertórios para treino.")

    temas_padrao = [
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
            "id": 8,
            "tema": "A vulnerabilidade das infraestruturas críticas nacionais diante de ataques cibernéticos",
            "eixo": "Cibersegurança & Tecnologia",
            "origem": "Simulado EsPCEx",
            "texto_base_1": "TEXTO I: Redes elétricas, sistemas bancários, abastecimento de água e redes de telecomunicação dependem diretamente de sistemas digitais. Ataques promovidos por hackers ou grupos estatais estrangeiros podem paralisar serviços essenciais e comprometer a segurança da nação.",
            "texto_base_2": "TEXTO II: A criação do Comando de Defesa Cibernética é resposta à necessidade de blindar dados estratégicos do Estado. No entanto, a formação continuada de especialistas e o uso de criptografia própria são urgentes diante de invasões sofisticadas.",
            "repertorios": ["Comando de Defesa Cibernética (ComDCiber)", "Zygmunt Bauman (Sociedade de Risco)", "Marco Civil da Internet"]
        },
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
            "id": 20,
            "tema": "A relevância do civismo e dos valores éticos na formação da juventude brasileira",
            "eixo": "Cidadania & Ética",
            "origem": "Prova EsPCEx",
            "texto_base_1": "TEXTO I: O civismo, o respeito aos símbolos nacionais e a consciência dos deveres sociais são fundamentos da coesão de um país. Estimular valores como disciplina, responsabilidade coletiva e patriotismo fortalece a democracia.",
            "texto_base_2": "TEXTO II: Cidadania plena envolve não apenas usufruir de direitos, mas praticar ativamente a ética no cotidiano, respeitando as leis e colaborando para a melhoria da comunidade.",
            "repertorios": ["Immanuel Kant (Imperativo Categórico)", "Émile Durkheim (Fato Social e Moral)", "LDB - Lei de Diretrizes e Bases"]
        }
    ]

    banco_completo = temas_padrao + db.get("temas_custom", [])

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
            lista_filtrada = banco_completo
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

        st.info(f"### 📝 Proposta: '{t['tema']}'")
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
                novo_item = {
                    "id": len(banco_completo) + 1,
                    "tema": n_tema,
                    "eixo": n_eixo,
                    "origem": n_origem,
                    "texto_base_1": n_texto_1,
                    "texto_base_2": n_texto_2,
                    "repertorios": lista_r if lista_r else ["Repertório Geral do Eixo"]
                }
                db["temas_custom"].append(novo_item)
                salvar_dados_usuario(user_atual, db)
                st.success("Novo tema cadastrado com sucesso e salvo permanentemente!")
                st.rerun()

# -----------------------------------------------------------------------------
# MÓDULO 13: PACER TAF
# -----------------------------------------------------------------------------
elif menu == "🏃 Módulo 13: Pacer TAF":
    st.header("🏃 Módulo 13: Simulador Pacer de Corrida TAF")
    st.markdown("Calcule o ritmo exato por volta e acompanhe metas progressivas para os **12 minutos de corrida**.")

    col1, col2 = st.columns(2)
    with col1:
        distancia_meta = st.number_input("Meta de Distância (metros)", min_value=1000, max_value=4000, value=2450, step=50)
        tamanho_pista = st.number_input("Tamanho da Pista/Volta (metros)", min_value=100, max_value=1000, value=400, step=50)

    tempo_min = 12.0
    vel_media_kmh = (distancia_meta / 1000) / (tempo_min / 60)
    total_voltas = distancia_meta / tamanho_pista
    tempo_por_volta_seg = (tempo_min * 60) / total_voltas

    with col2:
        st.subheader("🎯 Métricas Táticas")
        st.metric("Velocidade Média Necessária", f"{vel_media_kmh:.2f} km/h")
        st.metric("Total de Voltas na Pista", f"{total_voltas:.2f} voltas")
        st.metric("Tempo Exato por Volta (400m)", f"{int(tempo_por_volta_seg // 60)}min {int(tempo_por_volta_seg % 60)}seg")

    st.markdown("---")
    st.subheader("📈 Plano de Evolução Progressiva (8 Semanas)")
    
    metas_semanais = []
    base_dist = min(2000, distancia_meta - 450)
    incremento = (distancia_meta + 100 - base_dist) / 7

    for sem in range(1, 9):
        dist_sem = base_dist + (incremento * (sem - 1))
        vel_sem = (dist_sem / 1000) / (12 / 60)
        metas_semanais.append({
            "Semana": f"Semana {sem}",
            "Meta Corrida (m)": int(dist_sem),
            "Velocidade (km/h)": round(vel_sem, 2),
            "Status": "Concluído" if sem == 1 else "Pendente"
        })

    df_pacer = pd.DataFrame(metas_semanais)
    st.dataframe(df_pacer, use_container_width=True, hide_index=True)

# -----------------------------------------------------------------------------
# MÓDULO 14: NUTRIÇÃO & CALORIAS
# -----------------------------------------------------------------------------
elif menu == "🥗 Nutrição & Calorias":
    st.header("🥗 Módulo 14: Calculadora Nutricional & Gasto Calórico")
    st.markdown("Ajuste o aporte calórico para conciliar o trabalho na roça com o rendimento no TAF e estudos.")

    c_peso, c_altura, c_idade = st.columns(3)
    peso = c_peso.number_input("Peso Atual (kg)", min_value=40.0, max_value=150.0, value=68.0, step=0.5)
    altura = c_altura.number_input("Altura (cm)", min_value=130, max_value=220, value=175)
    idade = c_idade.number_input("Idade", min_value=14, max_value=60, value=17)

    c_roca, c_taf = st.columns(2)
    nivel_roca = c_roca.selectbox("Intensidade do Trabalho na Roça/Propriedade", ["Leve / Interno", "Moderado (Manejo/Café)", "Pesado (Capina/Carga)"])
    treino_taf = c_taf.selectbox("Treino TAF do Dia", ["Nenhum / Descanso", "Corrida Moderada", "Treino Intenso (Corrida + Barra + Abdominal)"])

    tmb = (10 * peso) + (6.25 * altura) - (5 * idade) + 5
    fator_roca = 1.2 if "Leve" in nivel_roca else (1.45 if "Moderado" in nivel_roca else 1.7)
    fator_taf = 0 if "Nenhum" in treino_taf else (250 if "Moderada" in treino_taf else 450)
    
    gasto_total = (tmb * fator_roca) + fator_taf
    agua_ml = peso * 40
    proteina_g = peso * 1.8
    carbo_g = peso * 5.0

    st.markdown("---")
    st.subheader("📊 Metas Diárias de Nutrição & Hidratação")
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Gasto Calórico Estimado", f"{int(gasto_total)} kcal")
    m2.metric("Meta de Água", f"{agua_ml / 1000:.2f} Litros")
    m3.metric("Proteína Recomendada", f"{int(proteina_g)} g")
    m4.metric("Carboidratos (Energia)", f"{int(carbo_g)} g")

# -----------------------------------------------------------------------------
# MÓDULO 15: CALC. AGROPECUÁRIA
# -----------------------------------------------------------------------------
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

# -----------------------------------------------------------------------------
# MÓDULO 16: KANBAN EFA
# -----------------------------------------------------------------------------
elif menu == "📌 Kanban EFA":
    st.header("📌 Módulo 16: Quadro Kanban de Trabalhos & Pesquisas EFA")
    st.markdown("Gestão visual de projetos escolares, maquetes e relatórios agrícolas.")

    kanban_tasks = db.get("kanban", [])

    with st.expander("➕ Adicionar Novo Projeto/Trabalho"):
        novo_proj = st.text_input("Título do Trabalho")
        if st.button("Cadastrar no Kanban") and novo_proj:
            novo_id = len(kanban_tasks) + 1
            kanban_tasks.append({"id": novo_id, "titulo": novo_proj, "status": "A Fazer"})
            db["kanban"] = kanban_tasks
            salvar_dados_usuario(user_atual, db)
            st.success("Tarefa adicionada!")
            st.rerun()

    k_fazer, k_and, k_ent = st.columns(3)

    with k_fazer:
        st.subheader("🔴 A Fazer")
        for item in list(kanban_tasks):
            if item["status"] == "A Fazer":
                with st.expander(f"📌 {item['titulo']}"):
                    if st.button("Mover ➔ Em Andamento", key=f"kan_and_{item['id']}"):
                        item["status"] = "Em Andamento"
                        salvar_dados_usuario(user_atual, db)
                        st.rerun()

    with k_and:
        st.subheader("🟡 Em Andamento")
        for item in list(kanban_tasks):
            if item["status"] == "Em Andamento":
                with st.expander(f"⏳ {item['titulo']}"):
                    if st.button("Mover ➔ Entregue", key=f"kan_ent_{item['id']}"):
                        item["status"] = "Entregue"
                        salvar_dados_usuario(user_atual, db)
                        st.rerun()

    with k_ent:
        st.subheader("🟢 Entregue")
        for item in list(kanban_tasks):
            if item["status"] == "Entregue":
                with st.expander(f"✅ {item['titulo']}"):
                    st.caption("Concluído e arquivado")

# -----------------------------------------------------------------------------
# MÓDULO 17: HABIT TRACKER
# -----------------------------------------------------------------------------
elif menu == "🔥 Habit Tracker":
    st.header("🔥 Módulo 17: Habit Tracker & Disciplina Militar")
    st.markdown("Acompanhe o cumprimento diário das tarefas fundamentais para a aprovação.")

    habits_data = db.get("habits", {"streak": 12, "historico": {}})
    streak_dias = habits_data.get("streak", 12)

    st.subheader(f"🔥 Sequência Atual: **{streak_dias} Dias Consecutivos de Foco!**")

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

# -----------------------------------------------------------------------------
# MÓDULO 18: CRONOGRAMA DINÂMICO
# -----------------------------------------------------------------------------
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

# -----------------------------------------------------------------------------
# MÓDULO 19: HUB DE LINKS
# -----------------------------------------------------------------------------
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

# -----------------------------------------------------------------------------
# MÓDULO 20: DIÁRIO DE BORDO
# -----------------------------------------------------------------------------
elif menu == "📖 Diário de Bordo":
    st.header("📖 Módulo 20: Diário de Bordo Tático")

    frases_eb = [
        "'A farda não é uma veste que se despe com facilidade e indiferença, mas uma outra pele, que adere à própria alma.' — Gen. Leônidas Pires Gonçalves",
        "'O êxito é a soma de pequenos esforços repetidos dia após dia.'",
        "'A vitória sorri para aqueles que estão dispostos a pagar o preço do suor no treinamento.'",
        "'Brasil acima de tudo!'",
        "'Na selva, a disciplina não é apenas uma virtude, é a diferença entre a vida e a derrota.'"
    ]

    st.info(f"💡 **Frase do Dia:** {random.choice(frases_eb)}")

    diario_historico = db.get("diario_bordo", [])

    st.markdown("---")
    st.subheader("📝 Reflexão do Fim de Dia")

    with st.form("form_diario_bordo"):
        foco_dia = st.slider("Nível de Foco nos Estudos (1 a 5)", 1, 5, 4)
        cansaco_fisico = st.slider("Desgaste Físico da Roça/Treino (1 a 5)", 1, 5, 3)
        resumo_dia = st.text_area("O que correu bem hoje e o que precisa ser ajustado para amanhã?")
        btn_salvar_diario = st.form_submit_button("Salvar Diário de Bordo")

        if btn_salvar_diario:
            if resumo_dia.strip():
                data_registro = datetime.datetime.now().strftime("%d/%m/%Y às %H:%M")
                
                diario_historico.insert(0, {
                    "data": data_registro,
                    "foco": foco_dia,
                    "cansaco": cansaco_fisico,
                    "resumo": resumo_dia
                })
                db["diario_bordo"] = diario_historico
                salvar_dados_usuario(user_atual, db)
                st.success("Reflexão salva com sucesso no histórico!")
                st.rerun()
            else:
                st.warning("Escreva sua reflexão antes de clicar em salvar.")

    st.markdown("---")
    st.subheader("📜 Histórico de Reflexões Salvas")

    if not diario_historico:
        st.info("Nenhuma reflexão registrada ainda. Preencha o formulário acima para salvar sua primeira anotação.")
    else:
        for idx, reg in enumerate(diario_historico):
            with st.expander(f"📅 Registro de {reg['data']} — Foco: {reg['foco']}/5 | Cansaço: {reg['cansaco']}/5", expanded=(idx == 0)):
                st.write("**💬 O que correu bem e o que precisa ser ajustado:**")
                st.write(reg["resumo"])

# -----------------------------------------------------------------------------
# MÓDULO 21: PLAYER DE ÁUDIO
# -----------------------------------------------------------------------------
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
