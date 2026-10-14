import streamlit as st
import streamlit.components.v1 as components
import pandas as pd
import plotly.express as px
import datetime
import json
import os
import time
import hashlib
# -----------------------------------------------------------------------------
# Configuração da Página, Proteção Contra Tradução e Tema EsPCEx / EFA
# -----------------------------------------------------------------------------
st.set_page_config(
    page_title="QG EsPCEx - Preparação de Cadetes",
    page_icon="⚔️",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Proteção contra erros de remoção de nós do DOM causados por tradução automática
st.markdown('<meta name="google" content="notranslate">', unsafe_allow_html=True)

# Estilização CSS Verde Oliva / Exército / EFA
st.markdown("""
    <style>
    .main { background-color: #0f140d; color: #e0e6ed; }
    .stMetric {
        background-color: #1a2318;
        padding: 14px;
        border-radius: 8px;
        border-left: 5px solid #4caf50;
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
    }
    .stButton>button:hover {
        background-color: #3e6832;
        border-color: #d4af37;
    }
    </style>
""", unsafe_allow_html=True)

USERS_FILE = "usuarios.json"

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
# Função para Tocar Alarme Sonoro no Navegador (Web Audio API)
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

# -----------------------------------------------------------------------------
# Autenticação e Gestão de Ficheiros
# -----------------------------------------------------------------------------
def hash_senha(senha):
    return hashlib.sha256(senha.encode('utf-8')).hexdigest()

def carregar_usuarios():
    if os.path.exists(USERS_FILE):
        try:
            with open(USERS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def salvar_usuarios(usuarios):
    with open(USERS_FILE, "w", encoding="utf-8") as f:
        json.dump(usuarios, f, ensure_ascii=False, indent=4)

def get_data_file(username):
    return f"dados_espcex_{username.lower().strip()}.json"

# -----------------------------------------------------------------------------
# Edital Oficial EsPCEx
# -----------------------------------------------------------------------------
def get_edital_espcex():
    return [
        # --- 1º DIA DE PROVA ---
        {"Dia": "1º Dia", "Matéria": "Português", "Tópico": "Compreensão e Interpretação de Texto", "Status": "Não Iniciado", "Prioridade": "Alta"},
        {"Matéria": "Português", "Dia": "1º Dia", "Tópico": "Morfologia e Classes Gramaticais", "Status": "Não Iniciado", "Prioridade": "Alta"},
        {"Matéria": "Português", "Dia": "1º Dia", "Tópico": "Sintaxe da Oração e do Período", "Status": "Não Iniciado", "Prioridade": "Alta"},
        {"Matéria": "Português", "Dia": "1º Dia", "Tópico": "Concordância, Regência e Crase", "Status": "Não Iniciado", "Prioridade": "Alta"},
        {"Matéria": "Português", "Dia": "1º Dia", "Tópico": "Pontuação e Ortografia Oficial", "Status": "Não Iniciado", "Prioridade": "Média"},
        {"Matéria": "Português", "Dia": "1º Dia", "Tópico": "Literatura Brasileira (Barroco ao Modernismo)", "Status": "Não Iniciado", "Prioridade": "Alta"},
        
        # Redação
        {"Matéria": "Redação", "Dia": "1º Dia", "Tópico": "Estrutura Dissertativo-Argumentativa Modelo EsPCEx", "Status": "Não Iniciado", "Prioridade": "Alta"},
        {"Matéria": "Redação", "Dia": "1º Dia", "Tópico": "Elaboração de Tese e Projeto de Texto", "Status": "Não Iniciado", "Prioridade": "Alta"},
        {"Matéria": "Redação", "Dia": "1º Dia", "Tópico": "Desenvolvimento e Argumentação Lógica", "Status": "Não Iniciado", "Prioridade": "Alta"},
        {"Matéria": "Redação", "Dia": "1º Dia", "Tópico": "Conclusão e Fechamento das Ideias", "Status": "Não Iniciado", "Prioridade": "Alta"},

        # Física
        {"Matéria": "Física", "Dia": "1º Dia", "Tópico": "Cinemática Escalar e Vetorial", "Status": "Não Iniciado", "Prioridade": "Alta"},
        {"Matéria": "Física", "Dia": "1º Dia", "Tópico": "Leis de Newton e Dinâmica", "Status": "Não Iniciado", "Prioridade": "Alta"},
        {"Matéria": "Física", "Dia": "1º Dia", "Tópico": "Trabalho, Energia e Potência", "Status": "Não Iniciado", "Prioridade": "Alta"},
        {"Matéria": "Física", "Dia": "1º Dia", "Tópico": "Termologia, Calorimetria e Gases", "Status": "Não Iniciado", "Prioridade": "Alta"},
        {"Matéria": "Física", "Dia": "1º Dia", "Tópico": "Eletrostática e Eletrodinâmica", "Status": "Não Iniciado", "Prioridade": "Alta"},

        # Química
        {"Matéria": "Química", "Dia": "1º Dia", "Tópico": "Estrutura Atômica e Tabela Periódica", "Status": "Não Iniciado", "Prioridade": "Alta"},
        {"Matéria": "Química", "Dia": "1º Dia", "Tópico": "Ligações Químicas e Funções Inorgânicas", "Status": "Não Iniciado", "Prioridade": "Alta"},
        {"Matéria": "Química", "Dia": "1º Dia", "Tópico": "Reações Químicas e Estequiometria", "Status": "Não Iniciado", "Prioridade": "Alta"},
        {"Matéria": "Química", "Dia": "1º Dia", "Tópico": "Química Orgânica", "Status": "Não Iniciado", "Prioridade": "Alta"},

        # --- 2º DIA DE PROVA ---
        # Matemática
        {"Matéria": "Matemática", "Dia": "2º Dia", "Tópico": "Conjuntos, Relações e Funções", "Status": "Não Iniciado", "Prioridade": "Alta"},
        {"Matéria": "Matemática", "Dia": "2º Dia", "Tópico": "Função Exponencial e Logarítmica", "Status": "Não Iniciado", "Prioridade": "Alta"},
        {"Matéria": "Matemática", "Dia": "2º Dia", "Tópico": "Trigonometria", "Status": "Não Iniciado", "Prioridade": "Alta"},
        {"Matéria": "Matemática", "Dia": "2º Dia", "Tópico": "Geometria Plana", "Status": "Não Iniciado", "Prioridade": "Alta"},
        {"Matéria": "Matemática", "Dia": "2º Dia", "Tópico": "Geometria Espacial", "Status": "Não Iniciado", "Prioridade": "Alta"},
        {"Matéria": "Matemática", "Dia": "2º Dia", "Tópico": "Geometria Analítica", "Status": "Não Iniciado", "Prioridade": "Alta"},

        # Geografia
        {"Matéria": "Geografia", "Dia": "2º Dia", "Tópico": "Geografia Física do Brasil", "Status": "Não Iniciado", "Prioridade": "Alta"},
        {"Matéria": "Geografia", "Dia": "2º Dia", "Tópico": "Demografia e População", "Status": "Não Iniciado", "Prioridade": "Alta"},
        {"Matéria": "Geografia", "Dia": "2º Dia", "Tópico": "Espaço Agropecuário e Questão Agrária", "Status": "Não Iniciado", "Prioridade": "Alta"},
        {"Matéria": "Geografia", "Dia": "2º Dia", "Tópico": "Geopolítica Global", "Status": "Não Iniciado", "Prioridade": "Alta"},

        # História
        {"Matéria": "História", "Dia": "2º Dia", "Tópico": "Brasil Colônia e Império", "Status": "Não Iniciado", "Prioridade": "Alta"},
        {"Matéria": "História", "Dia": "2º Dia", "Tópico": "República e Era Vargas", "Status": "Não Iniciado", "Prioridade": "Alta"},
        {"Matéria": "História", "Dia": "2º Dia", "Tópico": "História Geral: Guerras Mundiais e Guerra Fria", "Status": "Não Iniciado", "Prioridade": "Alta"},

        # Inglês
        {"Matéria": "Inglês", "Dia": "2º Dia", "Tópico": "Compreensão de Textos Táticos", "Status": "Não Iniciado", "Prioridade": "Alta"},
        {"Matéria": "Inglês", "Dia": "2º Dia", "Tópico": "Gramática Aplicada", "Status": "Não Iniciado", "Prioridade": "Alta"}
    ]

def carregar_dados_usuario(username):
    data_file = get_data_file(username)
    if os.path.exists(data_file):
        try:
            with open(data_file, "r", encoding="utf-8") as f:
                dados = json.load(f)
                if "redacoes" not in dados: dados["redacoes"] = []
                if "taf" not in dados: dados["taf"] = []
                if "tarefas_casa" not in dados: dados["tarefas_casa"] = []
                if "efa_ciclo" not in dados:
                    dados["efa_ciclo"] = {
                        "modo": "Em Casa",
                        "inicio_ciclo": str(datetime.date.today())
                    }
                return dados
        except Exception:
            pass
    return {
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
        "data_prova": str(datetime.date.today() + datetime.timedelta(days=320))
    }

def salvar_dados_usuario(username, dados):
    data_file = get_data_file(username)
    with open(data_file, "w", encoding="utf-8") as f:
        json.dump(dados, f, ensure_ascii=False, indent=4)

# -----------------------------------------------------------------------------
# Autenticação de Utilizador
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
# Carregamento do Utilizador Logado
# -----------------------------------------------------------------------------
user_atual = st.session_state.usuario_logado
db = carregar_dados_usuario(user_atual)
nome_cadete = st.session_state.get("nome_exibicao", user_atual)

# -----------------------------------------------------------------------------
# Barra Lateral
# -----------------------------------------------------------------------------
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
      "🎧 Player de Áudio",
        "⚙️ Configurações do QG",
        "📅 Calendário Tático",
        "🛡️ Módulo 24 - Painel do Administrador"


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
            df_mat["Erros"] = df_mat["Questões"] - df_mat["Acertos"]
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
    import sqlite3
    import datetime
    import pandas as pd
    import plotly.express as px
    import streamlit.components.v1 as components

    st.title("🏋️ Treinamento Físico Militar - TAF EsPCEx")
    st.caption("Acompanhamento dos treinos das 05h00 e player oficial do Spotify integrado.")

    # 1. Garante a criação da tabela SQLite local do Treino TAF
    def init_db_taf():
        conn = sqlite3.connect("dados_qg.db")
        c = conn.cursor()
        c.execute('''
            CREATE TABLE IF NOT EXISTS treinos_taf (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                data TEXT,
                foco TEXT,
                flexoes INTEGER,
                abdominais INTEGER,
                barras INTEGER,
                corrida_m INTEGER,
                indice_minimo TEXT,
                observacoes TEXT
            )
        ''')
        conn.commit()
        conn.close()

    init_db_taf()

    aba_registro, aba_evolucao, aba_spotify = st.tabs([
        "➕ Cadastrar Treino (05h)",
        "📈 Evolução Física",
        "🎧 Spotify Player Integrado"
    ])


    # ----------------------------------------------------
    # ABA 3: SPOTIFY PLAYER INTEGRADO
    # ----------------------------------------------------
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
    import random

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


# ====================================================
# MÓDULO 14: NUTRIÇÃO & CALORIAS
# ====================================================
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
        st.markdown("##### 📱 Gestão de Conexões Salvas (Cookies de 30 Dias)")
        st.caption("Caso deseje encerrar a sessão em todos os dispositivos onde o login foi salvo:")

        if st.button("🧹 Revogar Todas as Sessões Ativas", key="btn_clear_sessions_master", use_container_width=True):
            conn = sqlite3.connect("dados_qg.db")
            c = conn.cursor()
            try:
                c.execute("DELETE FROM sessoes_usuario")
                conn.commit()
                st.success("✅ Todas as conexões salvas foram apagadas. Será necessário efetuar login novamente.")
            except Exception as e:
                st.error(f"Erro ao limpar sessões: {e}")
            finally:
                conn.close()
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
elif "administrador" in menu.lower() or "admin" in menu.lower() or "módulo 24" in menu.lower():
    st.markdown("### `#Modulo24_Painel_Administrador`")
    st.title("🛡️ Módulo 24 - Central Restrita do Administrador")
    st.caption("Painel de controlo exclusivo: gestão de alertas Telegram, emissão de relatórios master, auditoria e manutenção avançada.")

    # -----------------------------------------------------------------------------
    # 1. REGISTO DE LOGS E CONFIGURAÇÃO INICIAL
    # -----------------------------------------------------------------------------
    import sqlite3

    def init_db_config():
        conn = sqlite3.connect("dados_qg.db")
        conn.execute("CREATE TABLE IF NOT EXISTS config_qg (chave TEXT PRIMARY KEY, valor TEXT)")
        conn.commit()
        conn.close()

    def get_config(chave, padrao=""):
        conn = sqlite3.connect("dados_qg.db")
        c = conn.cursor()
        c.execute("SELECT valor FROM config_qg WHERE chave = ?", (chave,))
        res = c.fetchone()
        conn.close()
        return res[0] if res else padrao

    def set_config(chave, valor):
        conn = sqlite3.connect("dados_qg.db")
        conn.execute("CREATE TABLE IF NOT EXISTS config_qg (chave TEXT PRIMARY KEY, valor TEXT)")
        conn.execute("INSERT OR REPLACE INTO config_qg (chave, valor) VALUES (?, ?)", (chave, str(valor)))
        conn.commit()
        conn.close()

    def get_all_configs():
        conn = sqlite3.connect("dados_qg.db")
        conn.execute("CREATE TABLE IF NOT EXISTS config_qg (chave TEXT PRIMARY KEY, valor TEXT)")
        rows = conn.execute("SELECT chave, valor FROM config_qg").fetchall()
        conn.close()
        return dict(rows)

    def get_total_registros(tabela):
        tabelas_permitidas = {
            "simulados",
            "baterias_questoes",
            "caderno_erros",
            "diario_bordo",
            "calendario_qg",
        }
        if tabela not in tabelas_permitidas:
            return 0

        conn = sqlite3.connect("dados_qg.db")
        try:
            existe = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                (tabela,)
            ).fetchone()
            if not existe:
                return 0
            return conn.execute(f'SELECT COUNT(*) FROM "{tabela}"').fetchone()[0]
        finally:
            conn.close()

    init_db_config()

    def registrar_log(acao, detalhe=""):
        try:
            conn = sqlite3.connect("dados_qg.db")
            c = conn.cursor()
            c.execute("""
                CREATE TABLE IF NOT EXISTS logs_sistema (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    data_hora TEXT NOT NULL,
                    acao TEXT NOT NULL,
                    detalhe TEXT
                )
            """)
            c.execute(
                "INSERT INTO logs_sistema (data_hora, acao, detalhe) VALUES (?, ?, ?)",
                (datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), acao, detalhe)
            )
            conn.commit()
            conn.close()
        except Exception:
            pass

    pin_admin_salvo = set_config("senha_admin", "1911")

    if "admin_autenticado" not in st.session_state:
        st.session_state["admin_autenticado"] = False

    # -----------------------------------------------------------------------------
    # 2. AUTENTICAÇÃO RESTRITA (TELA DE DESBLOQUEIO)
    # -----------------------------------------------------------------------------
    if not st.session_state["admin_autenticado"]:
        st.warning("🔒 **Acesso Restrito:** Digite o PIN / Senha Master do Administrador para desbloquear as ferramentas restritas.")
        
        with st.form("form_login_admin_master"):
            pin_digitado = st.text_input("🔑 PIN / Senha Master", type="password", placeholder="Digite o PIN master")
            
            if st.form_submit_button("🔓 Desbloquear Painel Restrito", use_container_width=True):
                if pin_digitado == pin_admin_salvo:
                    st.session_state["admin_autenticado"] = True
                    registrar_log("LOGIN_ADMIN", "Acesso autorizado ao Painel Master")
                    st.success("✅ Acesso de Administrador concedido!")
                    st.rerun()
                else:
                    registrar_log("FALHA_LOGIN_ADMIN", "Tentativa com PIN incorreto")
                    st.error("❌ PIN incorreto! Acesso negado.")

    # -----------------------------------------------------------------------------
    # 3. PAINEL RESTRITO DESBLOQUEADO (TODAS AS FUNCIONALIDADES EXCLUSIVAS)
    # -----------------------------------------------------------------------------
    else:
        col_adm_h1, col_adm_h2 = st.columns([3, 1])
        col_adm_h1.success("🟢 **Sessão de Administrador Ativa**")
        if col_adm_h2.button("🔒 Bloquear Painel", use_container_width=True):
            st.session_state["admin_autenticado"] = False
            registrar_log("LOGOUT_ADMIN", "Sessão encerrada")
            st.rerun()

        st.markdown("---")

        aba_telegram, aba_relatorio, aba_import, aba_terminal, aba_logs, aba_telemetria, aba_manutencao, aba_arquivos, aba_pin = st.tabs([
            "🤖 Bot Telegram",
            "📄 Relatório Master",
            "📥 Carga Massiva",
            "💻 Console SQL",
            "📜 Logs & Auditoria",
            "📊 Telemetria",
            "🚨 Modo Manutenção",
            "📂 Arquivos",
            "🔑 Alterar PIN"
        ])

        # -------------------------------------------------------------------------
        # ABA 1: CONFIGURAÇÃO DO BOT TELEGRAM (ENVIO DE NOTIFICAÇÕES)
        # -------------------------------------------------------------------------
        with aba_telegram:
            st.subheader("🤖 Configuração e Disparo de Alertas via Telegram")
            st.caption("Configure as credenciais do seu bot para enviar alertas de simulados, metas e lemas direto para o telemóvel.")

            with st.form("form_config_telegram"):
                bot_token = st.text_input("Token do Bot Telegram", value=get_config("telegram_bot_token", ""), type="password", placeholder="Ex: 123456789:ABCdefGhIJKlmNoPQRstuVWXyz")
                chat_id = st.text_input("ID do Chat / Canal Telegram", value=get_config("telegram_chat_id", ""), placeholder="Ex: 987654321 ou @meuchanal")

                if st.form_submit_button("💾 Salvar Configurações do Telegram"):
                    set_config("telegram_bot_token", bot_token)
                    set_config("telegram_chat_id", chat_id)
                    registrar_log("CONFIG_TELEGRAM", "Credenciais do Telegram atualizadas")
                    st.success("✅ Credenciais do Telegram salvas no banco local!")
                    st.rerun()

            st.markdown("---")
            st.markdown("##### 🚀 Testar Disparo de Alerta Instantâneo")
            mensagem_teste = st.text_area("Mensagem do Alerta", value=f"🎖️ *QG EsPCEx — Mensagem de Teste*\n\nCadete {get_config('nome_guerra', 'Arruda')}, o sistema de notificações restritas está operacional!")

            if st.button("📲 Enviar Notificação de Teste", type="primary", use_container_width=True):
                token_atual = get_config("telegram_bot_token", "")
                chat_atual = get_config("telegram_chat_id", "")

                if not token_atual or not chat_atual:
                    st.error("Configure e salve o Token e Chat ID do Telegram antes de enviar!")
                else:
                    try:
                        import urllib.parse
                        import urllib.request
                        
                        texto_encoded = urllib.parse.quote(mensagem_teste)
                        url = f"https://api.telegram.org/bot{token_atual}/sendMessage?chat_id={chat_atual}&text={texto_encoded}&parse_mode=Markdown"
                        
                        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
                        with urllib.request.urlopen(req) as response:
                            if response.status == 200:
                                registrar_log("ENVIO_TELEGRAM_SUCESSO", "Mensagem enviada com sucesso")
                                st.success("✅ Notificação enviada com sucesso para o Telegram!")
                            else:
                                st.error(f"Erro ao enviar: Código {response.status}")
                    except Exception as e:
                        registrar_log("ENVIO_TELEGRAM_ERRO", str(e))
                        st.error(f"❌ Falha no disparo do Telegram: {e}")

        # -------------------------------------------------------------------------
        # ABA 2: GERADOR DE RELATÓRIO MASTER
        # -------------------------------------------------------------------------
        with aba_relatorio:
            st.subheader("📄 Emissão de Relatório Geral de Desempenho")
            st.caption("Compila o status de todos os módulos num documento formatado pronto para impressão.")

            nome_c = get_config("nome_guerra", "Arruda")
            meta_c = get_config("meta_concurso", "EsPCEx")
            arma_c = get_config("arma_interesse", "Infantaria (Selva)")
            data_hoje = datetime.date.today().strftime("%d/%m/%Y")

            html_relatorio = f"""
            <html>
            <head>
                <style>
                    body {{ font-family: Arial, sans-serif; margin: 20px; color: #111; }}
                    h1 {{ color: #2e7d32; border-bottom: 2px solid #2e7d32; padding-bottom: 5px; }}
                    h2 {{ color: #1b5e20; margin-top: 20px; }}
                    .box {{ background: #f4f6f4; border-left: 4px solid #2e7d32; padding: 12px; margin-bottom: 15px; }}
                    table {{ width: 100%; border-collapse: collapse; margin-top: 10px; }}
                    th, td {{ border: 1px solid #ddd; padding: 8px; text-align: left; }}
                    th {{ background-color: #e8f5e9; }}
                </style>
            </head>
            <body>
                <h1>🎖️ BOLETIM DE DESEMPENHO E TELEMETRIA DO QG</h1>
                <div class="box">
                    <p><b>Cadete:</b> {nome_c} | <b>Alvo:</b> {meta_c} | <b>Especialização:</b> {arma_c}</p>
                    <p><b>Data de Emissão:</b> {data_hoje} | <b>Status do QG:</b> Operacional</p>
                </div>

                <h2>📊 Resumo das Bases do SQLite (`dados_qg.db`)</h2>
                <table>
                    <tr><th>Módulo / Tabela</th><th>Total de Registos</th></tr>
                    <tr><td>Simulados Cadastrados</td><td>{get_total_registros('simulados')}</td></tr>
                    <tr><td>Baterias de Questões</td><td>{get_total_registros('baterias_questoes')}</td></tr>
                    <tr><td>Bizus & Erros Salvos</td><td>{get_total_registros('caderno_erros')}</td></tr>
                    <tr><td>Notas no Diário de Bordo</td><td>{get_total_registros('diario_bordo')}</td></tr>
                    <tr><td>Eventos no Calendário</td><td>{get_total_registros('calendario_qg')}</td></tr>
                </table>

                <h2>🏋️ Índices Configurados para o TAF</h2>
                <ul>
                    <li><b>Corrida 12 min:</b> {get_config('taf_corrida', '2450')} metros</li>
                    <li><b>Flexões de Braço:</b> {get_config('taf_flexao', '21')} repetições</li>
                    <li><b>Barra Fixa:</b> {get_config('taf_barra', '3')} repetições</li>
                    <li><b>Abdominais:</b> {get_config('taf_abdominal', '30')} repetições</li>
                </ul>
            </body>
            </html>
            """

            st.components.v1.html(html_relatorio, height=380, scrolling=True)

            st.download_button(
                label="📥 Baixar Relatório Master em HTML (Imprimível)",
                data=html_relatorio,
                file_name=f"relatorio_master_qg_{datetime.date.today()}.html",
                mime="text/html",
                use_container_width=True
            )

        # -------------------------------------------------------------------------
        # ABA 3: CARGA MASSIVA DE DADOS (CSV / EXCEL)
        # -------------------------------------------------------------------------
        with aba_import:
            st.subheader("📥 Carga Massiva de Dados (Upload CSV / Excel)")
            st.caption("Suba planilhas prontas para alimentar as tabelas do sistema em lote.")

            conn = sqlite3.connect("dados_qg.db")
            c = conn.cursor()
            c.execute("SELECT name FROM sqlite_master WHERE type='table';")
            tabelas_alvo = [r[0] for r in c.fetchall() if r[0] != "sqlite_sequence"]
            conn.close()

            if tabelas_alvo:
                tabela_destino = st.selectbox("Selecione a Tabela de Destino", tabelas_alvo)
                arquivo_upload = st.file_uploader("Selecione o arquivo CSV ou Excel", type=["csv", "xlsx"])

                if arquivo_upload is not None:
                    try:
                        if arquivo_upload.name.endswith(".csv"):
                            df_import = pd.read_csv(arquivo_upload)
                        else:
                            df_import = pd.read_excel(arquivo_upload)

                        st.write("🔍 **Pré-visualização dos dados a importar:**")
                        st.dataframe(df_import.head(5), use_container_width=True)

                        if st.button("🚀 Confirmar Importação em Lote", type="primary", use_container_width=True):
                            conn = sqlite3.connect("dados_qg.db")
                            df_import.to_sql(tabela_destino, conn, if_exists="append", index=False)
                            conn.close()
                            
                            registrar_log("CARGA_MASSIVA", f"Importados {len(df_import)} registos para {tabela_destino}")
                            st.success(f"✅ {len(df_import)} registos importados com sucesso para a tabela `{tabela_destino}`!")
                            st.rerun()
                    except Exception as e:
                        st.error(f"❌ Erro ao processar arquivo: {e}")

        # -------------------------------------------------------------------------
        # ABA 4: CONSOLE SQL DIRETO
        # -------------------------------------------------------------------------
        with aba_terminal:
            st.subheader("💻 Execução SQL e Inspeção Direta")
            query_sql = st.text_area("Comando SQL", value="SELECT name FROM sqlite_master WHERE type='table';", height=90)

            if st.button("⚡ Executar Query no SQLite", type="primary", use_container_width=True):
                if query_sql.strip():
                    try:
                        conn = sqlite3.connect("dados_qg.db")
                        if query_sql.strip().upper().startswith("SELECT"):
                            df_res = pd.read_sql_query(query_sql, conn)
                            st.dataframe(df_res, use_container_width=True)
                        else:
                            c = conn.cursor()
                            c.execute(query_sql)
                            conn.commit()
                            registrar_log("EXEC_SQL_DIRECT", f"Query: {query_sql[:50]}...")
                            st.success(f"✅ Comando executado! Linhas afetadas: {c.rowcount}")
                        conn.close()
                    except Exception as e:
                        st.error(f"❌ Erro ao executar SQL: {e}")

        # -------------------------------------------------------------------------
        # ABA 5: LOGS E AUDITORIA DE ATIVIDADES
        # -------------------------------------------------------------------------
        with aba_logs:
            st.subheader("📜 Logs & Registo de Auditoria")
            try:
                conn = sqlite3.connect("dados_qg.db")
                df_logs = pd.read_sql_query("SELECT * FROM logs_sistema ORDER BY id DESC", conn)
                conn.close()

                if df_logs.empty:
                    st.info("Nenhum log registrado até o momento.")
                else:
                    st.dataframe(df_logs, use_container_width=True)
                    if st.button("🧹 Limpar Histórico de Logs", use_container_width=True):
                        conn = sqlite3.connect("dados_qg.db"); c = conn.cursor()
                        c.execute("DELETE FROM logs_sistema"); conn.commit(); conn.close()
                        st.success("Logs apagados com sucesso!")
                        st.rerun()
            except Exception:
                st.info("A tabela de logs será criada no primeiro evento do sistema.")

        # -------------------------------------------------------------------------
        # ABA 6: TELEMETRIA DO SISTEMA
        # -------------------------------------------------------------------------
        with aba_telemetria:
            st.subheader("📊 Telemetria do Sistema e Base de Dados")
            
            tamanho_db_mb = 0
            if os.path.exists("dados_qg.db"):
                tamanho_db_mb = round(os.path.getsize("dados_qg.db") / (1024 * 1024), 2)

            col_tel1, col_tel2, col_tel3 = st.columns(3)
            col_tel1.metric("Tamanho da Base SQLite", f"{tamanho_db_mb} MB")
            col_tel2.metric("Arquivo de Banco", "dados_qg.db")
            col_tel3.metric("Status do Armazenamento", "🟢 Íntegro")

            st.markdown("---")
            st.write("##### 📋 Mapeamento de Tabelas Ativas:")
            conn = sqlite3.connect("dados_qg.db")
            c = conn.cursor()
            c.execute("SELECT name FROM sqlite_master WHERE type='table';")
            tbs = [r[0] for r in c.fetchall() if r[0] != "sqlite_sequence"]
            
            tele_data = []
            for tb in tbs:
                try:
                    c.execute(f"SELECT COUNT(*) FROM {tb}")
                    cnt = c.fetchone()[0]
                    tele_data.append({"Tabela": tb, "Total de Registos": cnt, "Integridade": "OK"})
                except Exception:
                    pass
            conn.close()

            if tele_data:
                st.dataframe(pd.DataFrame(tele_data), use_container_width=True)

        # -------------------------------------------------------------------------
        # ABA 7: MODO DE MANUTENÇÃO (LOCKOUT SWITCH)
        # -------------------------------------------------------------------------
        with aba_manutencao:
            st.subheader("🚨 Modo de Manutenção (Bloqueio Geral do QG)")
            
            status_manutencao = get_config("modo_manutencao", "Desativado")

            if status_manutencao == "Ativado":
                st.error("🔴 **O MODO DE MANUTENÇÃO ESTÁ ATIVO!** O acesso aos outros módulos está bloqueado.")
            else:
                st.success("🟢 **SISTEMA OPERANDO NORMALMENTE.** Todos os módulos estão acessíveis.")

            st.markdown("---")
            col_lock1, col_lock2 = st.columns(2)

            if col_lock1.button("🔒 ATIVAR Modo de Manutenção", use_container_width=True, type="primary"):
                set_config("modo_manutencao", "Ativado")
                registrar_log("MODO_MANUTENCAO", "Ativado pelo Administrador")
                st.warning("Modo de manutenção ativado!")
                st.rerun()

            if col_lock2.button("🔓 DESATIVAR Modo de Manutenção", use_container_width=True):
                set_config("modo_manutencao", "Desativado")
                registrar_log("MODO_MANUTENCAO", "Desativado pelo Administrador")
                st.success("Sistema restaurado para uso normal!")
                st.rerun()

        # -------------------------------------------------------------------------
        # ABA 8: GERENCIADOR DE ARQUIVOS
        # -------------------------------------------------------------------------
        with aba_arquivos:
            st.subheader("📂 Gerenciador de Arquivos do Projeto")
            extensoes_permitidas = [".db", ".json", ".csv", ".pdf", ".txt", ".png", ".jpg", ".html"]
            arquivos_pasta = [f for f in os.listdir(".") if os.path.isfile(f) and any(f.endswith(ext) for ext in extensoes_permitidas)]

            if arquivos_pasta:
                df_arq = []
                for f in arquivos_pasta:
                    size_kb = round(os.path.getsize(f) / 1024, 2)
                    df_arq.append({"Nome do Arquivo": f, "Tamanho (KB)": size_kb})

                st.dataframe(pd.DataFrame(df_arq), use_container_width=True)

                st.markdown("---")
                col_arq1, col_arq2 = st.columns(2)
                arq_sel = col_arq1.selectbox("Selecione um Arquivo", arquivos_pasta)

                if arq_sel:
                    with open(arq_sel, "rb") as fp:
                        col_arq2.download_button(
                            label=f"📥 Baixar {arq_sel}",
                            data=fp,
                            file_name=arq_sel,
                            use_container_width=True
                        )
            else:
                st.info("Nenhum arquivo relevante encontrado no diretório.")

        # -------------------------------------------------------------------------
        # ABA 9: ALTERAR PIN MASTER
        # -------------------------------------------------------------------------
        with aba_pin:
            st.subheader("🔑 Alterar PIN / Senha Master")
            
            with st.form("form_alterar_pin_master"):
                novo_pin = st.text_input("Novo PIN Master", type="password")
                confirma_pin = st.text_input("Confirmar Novo PIN Master", type="password")

                if st.form_submit_button("💾 Salvar Novo PIN Master", use_container_width=True):
                    if novo_pin.strip() != "" and novo_pin == confirma_pin:
                        set_config("senha_admin", novo_pin)
                        registrar_log("ALTERA_PIN_ADMIN", "PIN Master do Administrador alterado")
                        st.success("✅ PIN Master atualizado com sucesso!")
                        st.rerun()
                    else:
                        st.error("Os PINs informados não coincidem ou estão vazios.")
