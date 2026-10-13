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
        "🎯 Simulados (1º e 2º Dia)"
        "📊 Visualizador de Planilhas"
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
# MÓDULO 7: Edital Verticalizado (com Chave Dinâmica)
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
        key=f"edital_edit_{dia_f}_{mat_f}"  # Key dinâmica previne erros no DOM ao filtrar
    )

    if st.button("💾 Salvar Edital"):
        novos = edited_df.to_dict("records")
        topicos_editados = [x["Tópico"] for x in novos]
        preservados = [item for item in db["edital"] if item["Tópico"] not in topicos_editados]
        db["edital"] = preservados + novos
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